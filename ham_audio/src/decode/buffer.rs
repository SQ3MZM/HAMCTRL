/// Ring buffer for 12kHz PCM audio.
/// Receives 48kHz PCM from audio.rs, resamples to 12kHz, stores last 15s.

use std::sync::{Arc, Mutex};

const SAMPLE_RATE_IN:  u32 = 48_000;
const SAMPLE_RATE_OUT: u32 = 12_000;
const RESAMPLE_RATIO:  usize = (SAMPLE_RATE_IN / SAMPLE_RATE_OUT) as usize; // = 4
const MAX_SECONDS:     usize = 20; // Keep 20s of audio
const MAX_SAMPLES:     usize = SAMPLE_RATE_OUT as usize * MAX_SECONDS;

pub struct Ft8Buffer {
    inner: Arc<Mutex<BufferInner>>,
}

struct BufferInner {
    samples:      Vec<f32>,  // ring buffer @ 12kHz
    write_pos:    usize,
    total_written: usize,
    /// Accumulator for simple 4:1 decimation
    decim_acc:    [f32; RESAMPLE_RATIO],
    decim_pos:    usize,
}

impl Ft8Buffer {
    pub fn new() -> Self {
        Ft8Buffer {
            inner: Arc::new(Mutex::new(BufferInner {
                samples:      vec![0f32; MAX_SAMPLES],
                write_pos:    0,
                total_written: 0,
                decim_acc:    [0f32; RESAMPLE_RATIO],
                decim_pos:    0,
            })),
        }
    }

    /// Push PCM float32 samples at 48kHz. Decimates 4:1 to 12kHz internally.
    pub fn push_pcm_f32(&self, data: &[f32]) {
        let mut inner = self.inner.lock().unwrap();
        for &s in data {
            let dpos = inner.decim_pos;
            inner.decim_acc[dpos] = s;
            inner.decim_pos += 1;
            if inner.decim_pos == RESAMPLE_RATIO {
                let avg = inner.decim_acc.iter().sum::<f32>() / RESAMPLE_RATIO as f32;
                let wpos = inner.write_pos;
                inner.samples[wpos] = avg;
                inner.write_pos = (wpos + 1) % MAX_SAMPLES;
                inner.total_written += 1;
                inner.decim_pos = 0;
            }
        }
    }

    /// Push PCM int16 samples at 48kHz. Decimates 4:1 to 12kHz internally.
    /// Convenience API paired with push_pcm_f32; kept for callers pushing i16.
    #[allow(dead_code)]
    pub fn push_pcm_i16(&self, data: &[i16]) {
        let f32_data: Vec<f32> = data.iter().map(|&s| s as f32 / 32768.0).collect();
        self.push_pcm_f32(&f32_data);
    }

    /// Snapshot the last `seconds` of 12kHz audio without clearing.
    /// Returns None if fewer than min_samples available.
    pub fn snapshot(&self, seconds: f32) -> Option<Vec<f32>> {
        let inner = self.inner.lock().unwrap();
        let n_want = (seconds * SAMPLE_RATE_OUT as f32) as usize;
        let available = inner.total_written.min(MAX_SAMPLES);
        if available == 0 { return None; }
        let n = n_want.min(available);

        let mut out = vec![0f32; n];
        let write_pos = inner.write_pos;
        let start = if available < MAX_SAMPLES {
            // Buffer not full yet — data starts at 0
            available.saturating_sub(n)
        } else {
            // Full ring: oldest data at write_pos
            (write_pos + MAX_SAMPLES - n) % MAX_SAMPLES
        };

        for i in 0..n {
            out[i] = inner.samples[(start + i) % MAX_SAMPLES];
        }
        Some(out)
    }

    /// Take last `seconds` of audio AND reset the buffer.
    /// API buffera zachowane pod przyszle uzycie (np. reset po zmianie trybu).
    #[allow(dead_code)]
    pub fn pop(&self, seconds: f32) -> Option<Vec<f32>> {
        let snap = self.snapshot(seconds);
        let mut inner = self.inner.lock().unwrap();
        inner.write_pos = 0;
        inner.total_written = 0;
        inner.decim_pos = 0;
        snap
    }

    /// Snapshot of the just-completed FT8/FT4 window, aligned to the window
    /// grid. Called by rx_loop shortly (settle ~0.3s) AFTER a window boundary,
    /// so the window that just ended occupies the buffer region ending a touch
    /// before "now". We return `window_s + lead_s` seconds: `window_s` is the
    /// transmission length, `lead_s` a small head margin for signals that start
    /// slightly early. The decoder's own time-search absorbs the small offset.
    ///
    /// Implemented on top of `snapshot`: we grab `window_s + lead_s` seconds
    /// ending at the current write position. Because rx_loop wakes right after
    /// the boundary, this region is the completed window (plus the head margin).
    pub fn snapshot_aligned(&self, window_s: f64, lead_s: f64) -> Option<Vec<f32>> {
        let want = (window_s + lead_s) as f32;
        self.snapshot(want)
    }

    pub fn available_seconds(&self) -> f32 {
        let inner = self.inner.lock().unwrap();
        inner.total_written.min(MAX_SAMPLES) as f32 / SAMPLE_RATE_OUT as f32
    }
}

impl Clone for Ft8Buffer {
    fn clone(&self) -> Self {
        Ft8Buffer { inner: self.inner.clone() }
    }
}
