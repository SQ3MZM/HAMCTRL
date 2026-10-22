/// FT8 signal subtraction (successive interference cancellation), ported
/// from ft8v2/subtractft8.f90 + cwfilter.f90 (WSJT-X/JTDX reference
/// source, github.com/jtdx-project/jtdx).
///
/// After a signal decodes successfully, this reconstructs its exact
/// waveform, coherently estimates its actual (phase, amplitude) by
/// demodulating the real audio against that reference, low-pass filters
/// that estimate to reject noise/other overlapping signals, then subtracts
/// it. This is NOT "regenerate a clean tone and subtract" - that would not
/// cancel correctly, since a wrong phase adds energy instead of removing
/// it. The coherent estimate is what makes cancellation actually work.
///
/// Purpose: two signals close in frequency often mask each other - only
/// the stronger one gets decoded, the weaker one is invisible even to an
/// otherwise-correct decoder. Subtracting the strong one after decoding it
/// reveals the weaker one on a second candidate-search pass over the
/// residual audio.

use rustfft::num_complex::Complex;
use super::fft_cache::{cached_fft_forward, cached_fft_inverse};
use super::params::{FT8_COSTAS, FT8_GRAYMAP, FT4_COSTAS, FT4_GRAYMAP};
use std::collections::HashMap;
use std::sync::{Arc, Mutex, OnceLock};

// Filtr dolnoprzepustowy zalezy tylko od nfft (stalego dla danej dlugosci
// okna), a byl liczony od nowa (pelna FFT ~180k punktow) przy KAZDYM
// odejmowanym sygnale - przy 30+ dekodach na przebieg to byla jedna trzecia
// calego czasu dekodowania. Liczony raz na nfft.
static LOWPASS_CACHE: OnceLock<Mutex<HashMap<usize, Arc<(Vec<Complex<f32>>, Vec<f32>)>>>> = OnceLock::new();

fn cached_lowpass(nfft: usize) -> Arc<(Vec<Complex<f32>>, Vec<f32>)> {
    let m = LOWPASS_CACHE.get_or_init(|| Mutex::new(HashMap::new()));
    if let Some(v) = m.lock().unwrap().get(&nfft) { return v.clone(); }
    let v = Arc::new(build_lowpass(nfft));
    m.lock().unwrap().insert(nfft, v.clone());
    v
}

const SAMPLES_PER_SYM_FT8: usize = 1920;
const TONE_SPACING_FT8: f64 = 6.25;
const SYMBOL_PERIOD_FT8: f64 = 0.160;
const BT_FT8: f64 = 2.0;
const NFRAME_FT8: usize = 79 * SAMPLES_PER_SYM_FT8; // 151680

const SAMPLES_PER_SYM_FT4: usize = 576;
const TONE_SPACING_FT4: f64 = 20.833_333;
const SYMBOL_PERIOD_FT4: f64 = 0.048;
const BT_FT4: f64 = 1.0;
const NFRAME_FT4: usize = 103 * SAMPLES_PER_SYM_FT4; // 59328

// Low-pass filter half-width (taps either side of center), matching
// NFILT1 in ft8_mod1.f90. Determines both filter time constant (coherence
// window for the phase/amplitude estimate) and CPU cost per subtraction.
const NFILT1: usize = 4000;

/// erf via Abramowitz & Stegun 7.1.26 (max error ~1.5e-7) - avoids pulling
/// in an external crate for this one GFSK pulse-shaping use.
fn erf(x: f64) -> f64 {
    let sign = if x < 0.0 { -1.0 } else { 1.0 };
    let x = x.abs();
    let a1 = 0.254829592;
    let a2 = -0.284496736;
    let a3 = 1.421413741;
    let a4 = -1.453152027;
    let a5 = 1.061405429;
    let p = 0.3275911;
    let t = 1.0 / (1.0 + p * x);
    let y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * (-x * x).exp();
    sign * y
}

fn gaussian_pulse(t: f64, bt: f64, symbol_period: f64) -> f64 {
    let k = bt * 2.0 * std::f64::consts::PI / (2f64.ln()).sqrt();
    let arg1 = k * (t / symbol_period - 0.5);
    let arg2 = k * (t / symbol_period + 0.5);
    let q1 = 0.5 * (1.0 - erf(arg1 / 2f64.sqrt()));
    let q2 = 0.5 * (1.0 - erf(arg2 / 2f64.sqrt()));
    q1 - q2
}

fn precompute_pulse_table(n: usize, bt: f64, symbol_period: f64, sample_rate: f64) -> Vec<f64> {
    (0..3 * n)
        .map(|i| {
            let t = (i as f64 - 1.5 * n as f64) / sample_rate;
            gaussian_pulse(t, bt, symbol_period)
        })
        .collect()
}

/// Reconstruct the complex (analytic) GFSK reference waveform for a 79/103
/// symbol tone sequence - `exp(j*(2*pi*f0*t + phi(t)))`, phi from the same
/// Gaussian-smoothed frequency deviation as the real transmitter. Complex,
/// not the real `sin()` a transmitter emits, because subtraction needs to
/// measure phase/amplitude against it (see subtract_signal).
fn gen_wave_complex(
    itone: &[usize], base_freq_hz: f32, sample_rate: u32,
    n: usize, tone_spacing: f64, symbol_period: f64, bt: f64,
) -> Vec<Complex<f32>> {
    let n_sym = itone.len();
    let total_samples = n_sym * n;
    let pulse_table = precompute_pulse_table(n, bt, symbol_period, sample_rate as f64);

    let mut freq_dev = vec![0f64; total_samples + 2 * n];
    for (i, &sym) in itone.iter().enumerate() {
        let center = (i * n + n) as isize;
        let start = center - n as isize;
        let seg_start = start.max(0) as usize;
        let seg_end = ((start + 3 * n as isize).max(0) as usize).min(freq_dev.len());
        let pulse_start = (seg_start as isize - start) as usize;
        for (k, fi) in (seg_start..seg_end).enumerate() {
            freq_dev[fi] += sym as f64 * tone_spacing * pulse_table[pulse_start + k];
        }
    }
    let freq_dev = &freq_dev[n..n + total_samples];

    let mut phase = 0f64;
    let mut out = Vec::with_capacity(total_samples);
    for &fd in freq_dev {
        let inst_freq = base_freq_hz as f64 + fd;
        phase += 2.0 * std::f64::consts::PI * inst_freq / sample_rate as f64;
        out.push(Complex::new(phase.cos() as f32, phase.sin() as f32));
    }
    out
}

pub fn gen_ft8wave_complex(itone: &[usize; 79], base_freq_hz: f32, sample_rate: u32) -> Vec<Complex<f32>> {
    gen_wave_complex(itone, base_freq_hz, sample_rate, SAMPLES_PER_SYM_FT8,
                      TONE_SPACING_FT8, SYMBOL_PERIOD_FT8, BT_FT8)
}

pub fn gen_ft4wave_complex(itone: &[usize; 103], base_freq_hz: f32, sample_rate: u32) -> Vec<Complex<f32>> {
    gen_wave_complex(itone, base_freq_hz, sample_rate, SAMPLES_PER_SYM_FT4,
                      TONE_SPACING_FT4, SYMBOL_PERIOD_FT4, BT_FT4)
}

/// Recover the 79-symbol FT8 tone sequence from an already LDPC-decoded
/// 174-bit codeword (bits174[0..174], the raw bp_decode output - no
/// separate encoder needed, this is the exact inverse of the symbol
/// interleaving used when the message was originally packed).
pub fn bits_to_symbols_ft8(bits174: &[u8]) -> [usize; 79] {
    debug_assert!(bits174.len() >= 174);
    let mut data = [0usize; 58];
    for i in 0..58 {
        let b0 = bits174[i * 3] as usize;
        let b1 = bits174[i * 3 + 1] as usize;
        let b2 = bits174[i * 3 + 2] as usize;
        data[i] = FT8_GRAYMAP[b0 * 4 + b1 * 2 + b2];
    }
    let mut symbols = [0usize; 79];
    symbols[0..7].copy_from_slice(&FT8_COSTAS);
    symbols[7..36].copy_from_slice(&data[0..29]);
    symbols[36..43].copy_from_slice(&FT8_COSTAS);
    symbols[43..72].copy_from_slice(&data[29..58]);
    symbols[72..79].copy_from_slice(&FT8_COSTAS);
    symbols
}

/// Recover the 103-symbol FT4 tone sequence from a decoded 174-bit
/// codeword. FT4 has 2 bits/symbol (4 tones) and 4 different Costas
/// patterns at 4 positions, vs FT8's 1 pattern repeated 3 times.
pub fn bits_to_symbols_ft4(bits174: &[u8]) -> [usize; 103] {
    debug_assert!(bits174.len() >= 174);
    let mut data = [0usize; 87];
    for i in 0..87 {
        let b0 = bits174[i * 2] as usize;
        let b1 = bits174[i * 2 + 1] as usize;
        data[i] = FT4_GRAYMAP[b0 * 2 + b1];
    }
    let mut symbols = [0usize; 103];
    let costas_pos = [0usize, 33, 66, 99];
    let data_ranges = [(0usize, 29usize), (29, 58), (58, 87)]; // 3 gaps of 29 data symbols
    symbols[costas_pos[0]..costas_pos[0] + 4].copy_from_slice(&FT4_COSTAS[0]);
    symbols[4..4 + 29].copy_from_slice(&data[data_ranges[0].0..data_ranges[0].1]);
    symbols[costas_pos[1]..costas_pos[1] + 4].copy_from_slice(&FT4_COSTAS[1]);
    symbols[37..37 + 29].copy_from_slice(&data[data_ranges[1].0..data_ranges[1].1]);
    symbols[costas_pos[2]..costas_pos[2] + 4].copy_from_slice(&FT4_COSTAS[2]);
    symbols[70..70 + 29].copy_from_slice(&data[data_ranges[2].0..data_ranges[2].1]);
    symbols[costas_pos[3]..costas_pos[3] + 4].copy_from_slice(&FT4_COSTAS[3]);
    // Dane zaczynaja sie zaraz za kazdym 4-symbolowym blokiem Costas (4, 37,
    // 70) - jak w ft4_encoder.py. Wczesniej wpisywane od 7/40/73, wiec
    // odejmowanie stacji FT4 odejmowalo ZLY przebieg (nadpisany Costas i
    // przesuniete dane) - kolejne przebiegi dostawaly residuum z dodatkowym
    // zaklóceniem zamiast oczyszczonego.
    symbols
}

#[cfg(test)]
mod tests {
    use super::*;

    // Kolejnosc tonow FT4 dla "K1ABC W9XYZ EN37" z ft4_encoder.py (ten sam
    // koder co TX), slowo kodowe z tego samego wywolania.
    #[test]
    fn ft4_symbols_match_encoder() {
        let cw = "010000111110001101101010111001001101000111000011101001010101110111101000011000000000011011100110010101001110010101010011010001000111111010110100111000111000101101001100010100";
        let tones = "0132100223021333231021012002331111023121233013000013231311102311112310021010122332102302303210201103201";
        let bits: Vec<u8> = cw.bytes().map(|b| b - b'0').collect();
        let want: Vec<usize> = tones.bytes().map(|b| (b - b'0') as usize).collect();
        assert_eq!(bits_to_symbols_ft4(&bits).to_vec(), want);
    }
}

/// Zero-phase low-pass filter (raised-cosine-squared, NFILT1+1 taps),
/// returned as its frequency response (ready to multiply against an FFT'd
/// signal) plus the edge-correction factors for the first/last NFILT1/2
/// samples of a filtered region (which see less than the full filter
/// support near a boundary and are otherwise under-estimated).
/// Layout matches cwfilter.f90's cw/endcorr after translating Fortran's
/// 1-indexed cshift to a directly-built 0-indexed wrapped kernel:
/// h[0]=center tap, h[1..=half]=positive lags, h[nfft-half..nfft)=negative
/// lags (wrapped for circular convolution), zero in between.
fn build_lowpass(nfft: usize) -> (Vec<Complex<f32>>, Vec<f32>) {
    let half = NFILT1 / 2;
    let mut window = vec![0f64; 2 * half + 1]; // index m+half <-> natural index m in -half..=half
    let mut sumw = 0f64;
    for j in -(half as isize)..=(half as isize) {
        let v = (std::f64::consts::PI * j as f64 / NFILT1 as f64).cos().powi(2);
        window[(j + half as isize) as usize] = v;
        sumw += v;
    }

    let mut h = vec![Complex::new(0f32, 0f32); nfft];
    h[0] = Complex::new((window[half] / sumw) as f32, 0.0);
    for j in 1..=half {
        h[j] = Complex::new((window[half + j] / sumw) as f32, 0.0);
        h[nfft - j] = Complex::new((window[half - j] / sumw) as f32, 0.0);
    }

    let mut endcorr = vec![1f32; half + 1];
    for j0 in 0..=half {
        let mut s = 0f64;
        for m in j0..=half {
            s += window[m + half];
        }
        endcorr[j0] = (1.0 / (1.0 - s / sumw)) as f32;
    }

    let fft = cached_fft_forward(nfft);
    let mut scratch = vec![Complex::new(0f32, 0f32); fft.get_inplace_scratch_len()];
    fft.process_with_scratch(&mut h, &mut scratch);
    // Not scaled by 1/nfft here - rustfft's forward/inverse pair are both
    // unnormalized (IFFT(FFT(x)) = nfft*x), so exactly one 1/nfft belongs
    // to the whole conv(x, h) = IFFT(FFT(x).*FFT(h))/nfft operation. That
    // single factor is applied in subtract_signal's inv_scale; scaling here
    // too double-divides by nfft (~1.5e5-1.9e5x extra attenuation), which
    // was silently collapsing the coherent estimate to ~0 - confirmed via
    // cfilt_mag_avg logging showing 0.000000 for every subtraction call.
    (h, endcorr)
}

/// Oszacowany przebieg jednego sygnalu do odjecia: (probka startowa, przebieg).
pub struct SignalEstimate {
    nstart: isize,
    wave: Vec<f32>,
}

/// Odejmuje oszacowany przebieg od `audio`.
pub fn apply_estimate(audio: &mut [f32], est: &SignalEstimate) {
    for (i, &w) in est.wave.iter().enumerate() {
        let id = est.nstart + i as isize;
        if id >= 0 && (id as usize) < audio.len() {
            audio[id as usize] -= w;
        }
    }
}

/// Tylko czyta `audio` - mozna liczyc wiele sygnalow rownolegle z tego
/// samego residuum, a potem odjac je wszystkie (apply_estimate).
fn estimate_signal(
    audio: &[f32], cref: &[Complex<f32>], nframe: usize,
    time_offset_s: f32, sample_rate: u32,
) -> SignalEstimate {
    let nstart = (time_offset_s * sample_rate as f32).round() as isize;
    let nfft = audio.len().max(nframe + NFILT1);
    let lp = cached_lowpass(nfft);
    let (h_freq, endcorr) = (&lp.0, &lp.1);

    let mut cfilt = vec![Complex::new(0f32, 0f32); nfft];
    for i in 0..nframe {
        let id = nstart + i as isize;
        if id >= 0 && (id as usize) < audio.len() {
            cfilt[i] = Complex::new(audio[id as usize], 0.0) * cref[i].conj();
        }
    }

    let fft_fwd = cached_fft_forward(nfft);
    let fft_inv = cached_fft_inverse(nfft);
    let mut scratch_f = vec![Complex::new(0f32, 0f32); fft_fwd.get_inplace_scratch_len()];
    let mut scratch_i = vec![Complex::new(0f32, 0f32); fft_inv.get_inplace_scratch_len()];
    fft_fwd.process_with_scratch(&mut cfilt, &mut scratch_f);
    for i in 0..nfft {
        cfilt[i] = cfilt[i] * h_freq[i];
    }
    fft_inv.process_with_scratch(&mut cfilt, &mut scratch_i);
    let inv_scale = 1.0 / nfft as f32; // rustfft's inverse is unnormalized
    for v in cfilt.iter_mut() {
        *v = *v * inv_scale;
    }

    let half = NFILT1 / 2;
    for j in 0..=half.min(nframe.saturating_sub(1)) {
        cfilt[j] = cfilt[j] * endcorr[j];
        cfilt[nframe - 1 - j] = cfilt[nframe - 1 - j] * endcorr[j];
    }

    let wave: Vec<f32> = (0..nframe).map(|i| 2.0 * (cfilt[i] * cref[i]).re).collect();
    SignalEstimate { nstart, wave }
}

pub fn estimate_ft8(audio: &[f32], itone: &[usize; 79], freq_hz: f32, time_offset_s: f32, sample_rate: u32) -> SignalEstimate {
    let cref = gen_ft8wave_complex(itone, freq_hz, sample_rate);
    estimate_signal(audio, &cref, NFRAME_FT8, time_offset_s, sample_rate)
}

pub fn estimate_ft4(audio: &[f32], itone: &[usize; 103], freq_hz: f32, time_offset_s: f32, sample_rate: u32) -> SignalEstimate {
    let cref = gen_ft4wave_complex(itone, freq_hz, sample_rate);
    estimate_signal(audio, &cref, NFRAME_FT4, time_offset_s, sample_rate)
}

