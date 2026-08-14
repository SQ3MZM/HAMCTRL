/// Demodulation: extract tone powers and compute soft LLRs.
/// Port of demod.py and demod_ft4.py.

use rustfft::num_complex::Complex;
use super::fft_cache::cached_fft_forward;
use super::params::{Params, FT8_GRAYMAP, FT4_GRAYMAP};
use super::sync::{Candidate, Spectrogram};

/// Extract [n_sym x n_tones] power matrix for a candidate.
pub fn extract_tone_power(audio: &[f32], cand: &Candidate, p: &Params) -> Vec<f32> {
    let n = p.samples_per_sym;
    let nfft = n * 4; // zero-padding for better freq resolution
    let freq_step = p.sample_rate as f32 / nfft as f32;

    let start_sample = (cand.time_offset_s * p.sample_rate as f32) as isize;

    let fft = cached_fft_forward(nfft);

    // Hanning window
    let window: Vec<f32> = (0..n).map(|i| {
        0.5 * (1.0 - (2.0 * std::f32::consts::PI * i as f32 / (n - 1) as f32).cos())
    }).collect();

    let mut power = vec![0f32; p.n_sym * p.n_tones];
    let mut scratch = vec![Complex::new(0f32, 0f32); fft.get_inplace_scratch_len()];

    for sym in 0..p.n_sym {
        let s0 = start_sample + (sym * n) as isize;
        let s1 = s0 + n as isize;
        if s0 < 0 || s1 > audio.len() as isize {
            // Out of range — leave as zero
            continue;
        }
        let s0 = s0 as usize;

        let mut buf = vec![Complex::new(0f32, 0f32); nfft];
        for i in 0..n {
            buf[i] = Complex::new(audio[s0 + i] * window[i], 0.0);
        }
        fft.process_with_scratch(&mut buf, &mut scratch);

        for tone in 0..p.n_tones {
            let f_target = cand.freq_hz + tone as f32 * p.tone_spacing as f32;
            let bin = (f_target / freq_step).round() as usize;
            let bin = bin.min(nfft / 2);
            let re = buf[bin].re;
            let im = buf[bin].im;
            power[sym * p.n_tones + tone] = re * re + im * im;
        }
    }
    power
}

/// Extract 174 soft LLRs for FT8 (3 bits/symbol, 8 tones).
pub fn extract_llr_ft8(power: &[f32], _p: &Params) -> [f32; 174] {
    // Data symbol positions: skip Costas blocks at [0:7], [36:43], [72:79]
    let data_sym_indices: Vec<usize> = (7..36).chain(43..72).collect();
    debug_assert_eq!(data_sym_indices.len(), 58);

    let graymap = FT8_GRAYMAP; // idx3 -> tone

    let mut llrs = [0f32; 174];
    let eps = 1e-12f32;

    for (li, &sym_idx) in data_sym_indices.iter().enumerate() {
        let p_slice = &power[sym_idx * 8..(sym_idx + 1) * 8];
        let logp: Vec<f32> = p_slice.iter().map(|&v| (v + eps).ln()).collect();

        // 3 bits per symbol (MSB first). idx3 in 0..8, tone = graymap[idx3].
        for bit_pos in 0..3usize {
            let shift = 2 - bit_pos; // MSB first
            let max0 = (0..8usize)
                .filter(|&idx3| ((idx3 >> shift) & 1) == 0)
                .map(|idx3| logp[graymap[idx3]])
                .fold(f32::NEG_INFINITY, f32::max);
            let max1 = (0..8usize)
                .filter(|&idx3| ((idx3 >> shift) & 1) == 1)
                .map(|idx3| logp[graymap[idx3]])
                .fold(f32::NEG_INFINITY, f32::max);
            llrs[li * 3 + bit_pos] = max0 - max1;
        }
    }
    llrs
}

/// Extract 174 soft LLRs for FT4 (2 bits/symbol, 4 tones).
pub fn extract_llr_ft4(power: &[f32], _p: &Params) -> [f32; 174] {
    // FT4 data symbol positions: skip 4x Costas at [0:4],[33:37],[66:70],[99:103]
    let data_sym_indices: Vec<usize> = (4..33).chain(37..66).chain(70..99).collect();
    debug_assert_eq!(data_sym_indices.len(), 87);

    let graymap = FT4_GRAYMAP;
    // inv_graymap: tone -> idx2
    let mut inv_graymap = [0usize; 4];
    for idx2 in 0..4usize { inv_graymap[graymap[idx2]] = idx2; }

    let mut llrs = [0f32; 174];
    let eps = 1e-12f32;

    for (li, &sym_idx) in data_sym_indices.iter().enumerate() {
        let p_slice = &power[sym_idx * 4..(sym_idx + 1) * 4];
        let logp: Vec<f32> = p_slice.iter().map(|&v| (v + eps).ln()).collect();

        for bit_pos in 0..2usize {
            let actual_bit_pos = 1 - bit_pos; // MSB first
            let max0 = (0..4usize)
                .filter(|&idx2| ((idx2 >> actual_bit_pos) & 1) == 0)
                .map(|idx2| logp[graymap[idx2]])
                .fold(f32::NEG_INFINITY, f32::max);
            let max1 = (0..4usize)
                .filter(|&idx2| ((idx2 >> actual_bit_pos) & 1) == 1)
                .map(|idx2| logp[graymap[idx2]])
                .fold(f32::NEG_INFINITY, f32::max);
            llrs[li * 2 + bit_pos] = max0 - max1;
        }
    }
    llrs
}

/// Estimate SNR in dB, referenced to WSJT-X's scale.
///
/// Measured & calibrated against WSJT-X reference decodes (ft8.wav): the signal
/// power is taken from the TIME-AVERAGED spectrum in a narrow band around the
/// signal's own frequency, divided by the spectral noise floor, then mapped
/// linearly to WSJT-X. Two details matter and were both wrong before:
///   * Use the time-AVERAGED PSD, not the peak bin over the whole spectrogram.
///     The peak always towers over the floor on a busy band, so every station
///     pinned to the +20 clamp (the "+20 for everyone" bug).
///   * Linear calibration snr = 1.09*raw − 20.2 (correlation ~0.74 with WSJT-X),
///     instead of a bandwidth correction with a hand-tuned offset.
///
/// `spec`    — the window spectrogram (for the averaged PSD and freq mapping)
/// `freq_hz` — the candidate signal's audio frequency
/// `noise_floor` — background power from noise_floor_from_spec
pub fn estimate_snr(spec: &Spectrogram, freq_hz: f32, noise_floor: f32) -> f32 {
    if spec.mag.is_empty() || spec.n_blocks == 0 || spec.n_bins == 0 {
        return -24.0;
    }
    // Time-averaged power per frequency bin.
    // avg[fi] = mean over time blocks of mag[bi*n_bins + fi]
    // Signal power = max averaged power within ±50 Hz of freq_hz.
    let half_bw_bins = (50.0 / spec.freq_step).ceil() as isize;
    let center_bin = ((freq_hz / spec.freq_step) as isize) - spec.bin_min as isize;
    let lo = (center_bin - half_bw_bins).max(0) as usize;
    let hi = (center_bin + half_bw_bins).min(spec.n_bins as isize - 1).max(0) as usize;

    let mut sig_power = 1e-12f32;
    for fi in lo..=hi {
        let mut acc = 0f32;
        for bi in 0..spec.n_blocks {
            acc += spec.mag[bi * spec.n_bins + fi];
        }
        let avg = acc / spec.n_blocks as f32;
        if avg > sig_power {
            sig_power = avg;
        }
    }

    let nf = noise_floor.max(1e-12);
    let raw_db = 10.0 * (sig_power / nf + 1e-12).log10();

    // Linear calibration to the WSJT-X scale (measured).
    let snr = 1.09 * raw_db - 20.2;
    snr.clamp(-28.0, 20.0)
}
