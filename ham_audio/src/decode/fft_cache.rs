/// Shared, process-wide cache of rustfft plans, keyed by FFT size.
///
/// Every FFT call site in this decoder (sync.rs's compute_spectrogram +
/// refine_offset, demod.rs's extract_tone_power, subtract.rs's build_lowpass
/// + subtract_signal) previously did `FftPlanner::<f32>::new()` +
/// `plan_fft_forward(nfft)` on EVERY call. refine_offset and
/// extract_tone_power run once PER CANDIDATE (up to 60x per decode pass,
/// every window, forever), and subtract_signal runs once per subtracted
/// signal with an FFT nearly as large as the whole window buffer. For a
/// non-power-of-two nfft (the common case here), planning is genuinely
/// expensive (mixed-radix/Bluestein setup, not just a lookup) - measured
/// live as the dominant cost behind ~1.0-1.1s decode_elapsed_s even on
/// windows with ZERO successful decodes (i.e. cost independent of
/// subtraction entirely, present already in the base decode-candidate
/// phase). rustfft's own plan objects (`Arc<dyn Fft<f32>>`) are immutable
/// and `Send + Sync` once built, so they're safe to build once and share
/// across rayon's parallel candidate-decode threads indefinitely.
use rustfft::{Fft, FftPlanner};
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

fn forward_cache() -> &'static Mutex<HashMap<usize, Arc<dyn Fft<f32>>>> {
    static CACHE: OnceLock<Mutex<HashMap<usize, Arc<dyn Fft<f32>>>>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

fn inverse_cache() -> &'static Mutex<HashMap<usize, Arc<dyn Fft<f32>>>> {
    static CACHE: OnceLock<Mutex<HashMap<usize, Arc<dyn Fft<f32>>>>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(HashMap::new()))
}

pub fn cached_fft_forward(nfft: usize) -> Arc<dyn Fft<f32>> {
    let mut map = forward_cache().lock().unwrap();
    map.entry(nfft)
        .or_insert_with(|| FftPlanner::<f32>::new().plan_fft_forward(nfft))
        .clone()
}

pub fn cached_fft_inverse(nfft: usize) -> Arc<dyn Fft<f32>> {
    let mut map = inverse_cache().lock().unwrap();
    map.entry(nfft)
        .or_insert_with(|| FftPlanner::<f32>::new().plan_fft_inverse(nfft))
        .clone()
}
