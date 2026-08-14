/// FT8/FT4 spectrogram and candidate sync search.
/// Port of sync.py and sync_ft4.py — vectorized Costas correlation.

use rustfft::num_complex::Complex;
use super::fft_cache::cached_fft_forward;
use super::params::{Params, FT8_COSTAS, FT4_COSTAS};

/// Local search grid step counts for fine sync refinement below: the
/// candidate grid (find_candidates_ft8/ft4) has ~0.08s time / ~3Hz freq
/// spacing, coarse enough that even a strong, unambiguously-detected
/// candidate can fail LDPC convergence purely from that residual offset.
/// Confirmed 2026-08-13 against captured band audio: several candidates
/// scored well above threshold with no nearby competing signal, yet
/// bp_decode never converged at the coarse position.
const REFINE_TIME_STEPS: i32 = 3;   // ±3 * 0.02s = ±0.06s
const REFINE_TIME_STEP_S: f32 = 0.02;
const REFINE_FREQ_STEPS: i32 = 3;   // ±3 * 1.0Hz = ±3Hz
const REFINE_FREQ_STEP_HZ: f32 = 1.0;

/// Magnitude spectrogram: [time_blocks x freq_bins]
pub struct Spectrogram {
    pub mag:        Vec<f32>,  // [n_blocks * n_bins]
    pub n_blocks:   usize,
    pub n_bins:     usize,
    pub freq_step:  f32,
    pub time_step:  usize,     // samples between blocks
    pub bin_min:    usize,
}

const F_MIN: f32 = 200.0;
const F_MAX: f32 = 3000.0;

pub fn compute_spectrogram(audio: &[f32], p: &Params, freq_osr: usize, time_osr: usize) -> Spectrogram {
    let n = p.samples_per_sym;
    let nfft = n * freq_osr;
    let freq_step = p.sample_rate as f32 / nfft as f32;
    let time_step = n / time_osr;

    let bin_min = (F_MIN / freq_step) as usize;
    let bin_max = (F_MAX / freq_step) as usize;
    let n_bins = bin_max - bin_min;

    let n_samples = audio.len();
    let n_blocks = if n_samples >= n { (n_samples - n) / time_step + 1 } else { 0 };

    let fft = cached_fft_forward(nfft);

    // Hanning window
    let window: Vec<f32> = (0..n).map(|i| {
        0.5 * (1.0 - (2.0 * std::f32::consts::PI * i as f32 / (n - 1) as f32).cos())
    }).collect();

    let mut mag = vec![0f32; n_blocks * n_bins];
    let mut scratch = vec![Complex::new(0f32, 0f32); fft.get_inplace_scratch_len()];

    for bi in 0..n_blocks {
        let start = bi * time_step;
        let mut buf = vec![Complex::new(0f32, 0f32); nfft];
        for i in 0..n {
            let s = if start + i < n_samples { audio[start + i] } else { 0.0 };
            buf[i] = Complex::new(s * window[i], 0.0);
        }
        fft.process_with_scratch(&mut buf, &mut scratch);
        for fi in 0..n_bins {
            let bin = bin_min + fi;
            let re = buf[bin].re;
            let im = buf[bin].im;
            mag[bi * n_bins + fi] = re * re + im * im;
        }
    }

    Spectrogram { mag, n_blocks, n_bins, freq_step, time_step, bin_min }
}

/// Noise floor z USREDNIONEGO po czasie widma (nie z surowego spektrogramu!).
/// Kluczowa naprawa SNR: najpierw usredniamy moc po czasie per bin (avg[fi]),
/// POTEM bierzemy 10. percentyl z usrednionego widma = tlo miedzy sygnalami.
/// Wczesniej percentyl liczony z surowego spektrogramu (blok x bin) dawal
/// noise floor ~54dB za nisko (mnostwo cichych momentow miedzy symbolami),
/// przez co WSZYSTKIE stacje clampowaly na +20. Zweryfikowane na ft8.wav.
pub fn noise_floor_from_spec(spec: &Spectrogram) -> f32 {
    if spec.mag.is_empty() || spec.n_blocks == 0 || spec.n_bins == 0 {
        return 1e-9;
    }
    let mut avg: Vec<f32> = Vec::with_capacity(spec.n_bins);
    for fi in 0..spec.n_bins {
        let mut acc = 0f32;
        for bi in 0..spec.n_blocks {
            acc += spec.mag[bi * spec.n_bins + fi];
        }
        let m = acc / spec.n_blocks as f32;
        if m.is_finite() && m > 0.0 {
            avg.push(m);
        }
    }
    if avg.is_empty() { return 1e-9; }
    avg.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let idx = ((avg.len() as f32 * 0.10) as usize).min(avg.len() - 1);
    avg[idx].max(1e-9)
}

#[derive(Debug, Clone)]
pub struct Candidate {
    pub freq_hz:      f32,
    pub time_offset_s: f32,
    pub score:        f32,
}

/// FT8 candidate search using 3x7-symbol Costas correlation.
pub fn find_candidates_ft8(spec: &Spectrogram, p: &Params, max_cands: usize, min_score: f32)
    -> Vec<Candidate>
{
    let freq_osr = 2usize;
    let time_osr = 2usize;
    let bins_per_tone = freq_osr;
    let sym_blocks = time_osr;

    let n_tones = p.n_tones;
    let n_sym = p.n_sym;
    let costas = FT8_COSTAS;
    let costas_pos = p.costas_pos;

    let max_tb = if spec.n_blocks > n_sym * sym_blocks {
        spec.n_blocks - n_sym * sym_blocks
    } else { return vec![]; };
    let max_fb = if spec.n_bins > (n_tones - 1) * bins_per_tone {
        spec.n_bins - (n_tones - 1) * bins_per_tone
    } else { return vec![]; };

    let n_t0 = (max_tb + sym_blocks / 2 - 1) / (sym_blocks / 2).max(1);
    let n_f0 = (max_fb + bins_per_tone / 2 - 1) / (bins_per_tone / 2).max(1);

    let mut score_map = vec![0f32; n_t0 * n_f0];
    let n_terms = (costas_pos.len() * costas.len()) as f32;

    // Scoring odporny na sasiadow: (moc_tonu - srednia_8_tonow) / srednia,
    // na LINIOWEJ mocy (spec.mag). Stara metoda log(ton)-log(max_z_7) karala
    // nakladajace sie stacje (sasiad podnosil max, spychal margin ponizej progu).
    // Odpowiednik find_candidates w Pythonie (sync.py). Uzywamy spec.mag wprost.

    for &csym_offset in costas_pos {
        for (k, &tone) in costas.iter().enumerate() {
            let sym_idx = csym_offset + k;

            for it0 in 0..n_t0 {
                let t0 = it0 * (sym_blocks / 2).max(1);
                let tb = t0 + sym_idx * sym_blocks;
                if tb >= spec.n_blocks { continue; }

                for if0 in 0..n_f0 {
                    let f0 = if0 * (bins_per_tone / 2).max(1);
                    // Check all tone bins valid
                    if f0 + (n_tones - 1) * bins_per_tone >= spec.n_bins { continue; }

                    // Zbierz moc (liniowa) wszystkich n_tones tonow, policz srednia
                    let mut sum_all = 0f32;
                    for t in 0..n_tones {
                        let bin = f0 + t * bins_per_tone;
                        sum_all += spec.mag[tb * spec.n_bins + bin];
                    }
                    let mean_all = sum_all / (n_tones as f32);
                    if mean_all <= 1e-12 { continue; }

                    let expected_bin = f0 + tone * bins_per_tone;
                    let expected = spec.mag[tb * spec.n_bins + expected_bin];
                    // kontrybucja: o ile wlasciwy ton przewyzsza srednia (znorm.)
                    score_map[it0 * n_f0 + if0] += ((expected - mean_all) / mean_all) / n_terms;
                }
            }
        }
    }

    // Non-max suppression + collect candidates
    let suppress_t = (sym_blocks * 2 / (sym_blocks / 2).max(1)).max(1);
    let suppress_f = (bins_per_tone * 2 / (bins_per_tone / 2).max(1)).max(1);
    let mut taken = vec![false; n_t0 * n_f0];

    let mut indices: Vec<usize> = (0..n_t0 * n_f0).collect();
    indices.sort_by(|&a, &b| score_map[b].partial_cmp(&score_map[a]).unwrap_or(std::cmp::Ordering::Equal));

    let mut candidates = Vec::new();
    for idx in indices {
        let sc = score_map[idx];
        if sc < min_score || !sc.is_finite() { break; }
        if taken[idx] { continue; }

        let it0 = idx / n_f0;
        let if0 = idx % n_f0;

        let tlo = it0.saturating_sub(suppress_t);
        let thi = (it0 + suppress_t + 1).min(n_t0);
        let flo = if0.saturating_sub(suppress_f);
        let fhi = (if0 + suppress_f + 1).min(n_f0);

        let mut any_taken = false;
        'outer: for tt in tlo..thi {
            for ff in flo..fhi {
                if taken[tt * n_f0 + ff] { any_taken = true; break 'outer; }
            }
        }
        if any_taken { continue; }

        for tt in tlo..thi {
            for ff in flo..fhi {
                taken[tt * n_f0 + ff] = true;
            }
        }

        let f0 = if0 * (bins_per_tone / 2).max(1);
        let t0 = it0 * (sym_blocks / 2).max(1);
        let freq_hz = (f0 + spec.bin_min) as f32 * spec.freq_step;
        let time_offset_s = t0 as f32 * spec.time_step as f32 / p.sample_rate as f32;

        candidates.push(Candidate { freq_hz, time_offset_s, score: sc });
        if candidates.len() >= max_cands { break; }
    }

    candidates.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
    candidates
}

/// FT4 candidate search using 4 different Costas patterns.
pub fn find_candidates_ft4(spec: &Spectrogram, p: &Params, max_cands: usize, min_score: f32)
    -> Vec<Candidate>
{
    let freq_osr = 2usize;
    let time_osr = 2usize;
    let bins_per_tone = freq_osr;
    let sym_blocks = time_osr;

    let n_tones = p.n_tones;
    let n_sym = p.n_sym;
    let costas_patterns = FT4_COSTAS;
    let costas_pos = p.costas_pos;

    let max_tb = if spec.n_blocks > n_sym * sym_blocks { spec.n_blocks - n_sym * sym_blocks } else { return vec![]; };
    let max_fb = if spec.n_bins > (n_tones - 1) * bins_per_tone { spec.n_bins - (n_tones - 1) * bins_per_tone } else { return vec![]; };

    let n_t0 = (max_tb + (sym_blocks / 2).max(1) - 1) / (sym_blocks / 2).max(1);
    let n_f0 = (max_fb + (bins_per_tone / 2).max(1) - 1) / (bins_per_tone / 2).max(1);
    let mut score_map = vec![0f32; n_t0 * n_f0];

    // Scoring odporny na sasiadow (jak FT8): (ton - srednia_n_tonow)/srednia
    // na LINIOWEJ mocy. Odpowiednik find_candidates w Pythonie.
    let n_terms = (costas_pos.len() * 4) as f32;

    for (&csym_offset, pattern) in costas_pos.iter().zip(costas_patterns.iter()) {
        for (k, &tone) in pattern.iter().enumerate() {
            let sym_idx = csym_offset + k;
            for it0 in 0..n_t0 {
                let t0 = it0 * (sym_blocks / 2).max(1);
                let tb = t0 + sym_idx * sym_blocks;
                if tb >= spec.n_blocks { continue; }
                for if0 in 0..n_f0 {
                    let f0 = if0 * (bins_per_tone / 2).max(1);
                    if f0 + (n_tones - 1) * bins_per_tone >= spec.n_bins { continue; }
                    // srednia mocy wszystkich tonow (liniowa)
                    let mut sum_all = 0f32;
                    for t in 0..n_tones {
                        sum_all += spec.mag[tb * spec.n_bins + f0 + t * bins_per_tone];
                    }
                    let mean_all = sum_all / (n_tones as f32);
                    if mean_all <= 1e-12 { continue; }
                    let expected_bin = f0 + tone * bins_per_tone;
                    let expected = spec.mag[tb * spec.n_bins + expected_bin];
                    score_map[it0 * n_f0 + if0] += ((expected - mean_all) / mean_all) / n_terms;
                }
            }
        }
    }

    // Same non-max suppression as FT8
    let suppress_t = (sym_blocks * 2 / (sym_blocks / 2).max(1)).max(1);
    let suppress_f = (bins_per_tone * 2 / (bins_per_tone / 2).max(1)).max(1);
    let mut taken = vec![false; n_t0 * n_f0];
    let mut indices: Vec<usize> = (0..n_t0 * n_f0).collect();
    indices.sort_by(|&a, &b| score_map[b].partial_cmp(&score_map[a]).unwrap_or(std::cmp::Ordering::Equal));

    let mut candidates = Vec::new();
    for idx in indices {
        let sc = score_map[idx];
        if sc < min_score || !sc.is_finite() { break; }
        if taken[idx] { continue; }
        let it0 = idx / n_f0;
        let if0 = idx % n_f0;
        let tlo = it0.saturating_sub(suppress_t);
        let thi = (it0 + suppress_t + 1).min(n_t0);
        let flo = if0.saturating_sub(suppress_f);
        let fhi = (if0 + suppress_f + 1).min(n_f0);
        let mut any_taken = false;
        'outer: for tt in tlo..thi {
            for ff in flo..fhi { if taken[tt * n_f0 + ff] { any_taken = true; break 'outer; } }
        }
        if any_taken { continue; }
        for tt in tlo..thi { for ff in flo..fhi { taken[tt * n_f0 + ff] = true; } }

        let f0 = if0 * (bins_per_tone / 2).max(1);
        let t0 = it0 * (sym_blocks / 2).max(1);
        candidates.push(Candidate {
            freq_hz: (f0 + spec.bin_min) as f32 * spec.freq_step,
            time_offset_s: t0 as f32 * spec.time_step as f32 / p.sample_rate as f32,
            score: sc,
        });
        if candidates.len() >= max_cands { break; }
    }
    candidates.sort_by(|a, b| b.score.partial_cmp(&a.score).unwrap_or(std::cmp::Ordering::Equal));
    candidates
}

/// Local Costas-correlation search around a coarse candidate, returning the
/// (time, freq) offset within it that best fits the audio. Run once per
/// candidate before extract_tone_power - see REFINE_* constants above for
/// why this matters. Shared search core parametrized by the Costas symbol
/// index/tone pairs to check (FT8: one pattern repeated at 3 positions;
/// FT4: 4 different patterns, one per position).
fn refine_offset(audio: &[f32], cand: &Candidate, p: &Params, costas_syms: &[(usize, usize)]) -> (f32, f32) {
    let n = p.samples_per_sym;
    let nfft = n * 4;
    let freq_step = p.sample_rate as f32 / nfft as f32;

    let fft = cached_fft_forward(nfft);
    let mut scratch = vec![Complex::new(0f32, 0f32); fft.get_inplace_scratch_len()];

    let window: Vec<f32> = (0..n).map(|i| {
        0.5 * (1.0 - (2.0 * std::f32::consts::PI * i as f32 / (n - 1) as f32).cos())
    }).collect();

    let mut best_score = f32::MIN;
    let mut best_dt = 0f32;
    let mut best_df = 0f32;
    let mut buf = vec![Complex::new(0f32, 0f32); nfft];
    let mut tone_power = [0f32; 8]; // n_tones <= 8 for both FT8 and FT4

    for it in -REFINE_TIME_STEPS..=REFINE_TIME_STEPS {
        let dt = it as f32 * REFINE_TIME_STEP_S;
        let trial_time = cand.time_offset_s + dt;
        let start_sample = (trial_time * p.sample_rate as f32) as isize;

        for jf in -REFINE_FREQ_STEPS..=REFINE_FREQ_STEPS {
            let df = jf as f32 * REFINE_FREQ_STEP_HZ;
            let trial_freq = cand.freq_hz + df;

            let mut score = 0f32;
            let mut n_terms = 0f32;
            for &(sym_idx, tone) in costas_syms {
                let s0 = start_sample + (sym_idx * n) as isize;
                let s1 = s0 + n as isize;
                if s0 < 0 || s1 > audio.len() as isize { continue; }
                let s0 = s0 as usize;

                for i in 0..n { buf[i] = Complex::new(audio[s0 + i] * window[i], 0.0); }
                for i in n..nfft { buf[i] = Complex::new(0.0, 0.0); }
                fft.process_with_scratch(&mut buf, &mut scratch);

                let mut sum_all = 0f32;
                for t in 0..p.n_tones {
                    let f_target = trial_freq + t as f32 * p.tone_spacing as f32;
                    let bin = (f_target / freq_step).round() as usize;
                    let bin = bin.min(nfft / 2);
                    let re = buf[bin].re;
                    let im = buf[bin].im;
                    let pw = re * re + im * im;
                    if t < 8 { tone_power[t] = pw; }
                    sum_all += pw;
                }
                let mean_all = sum_all / p.n_tones as f32;
                if mean_all > 1e-12 {
                    score += (tone_power[tone] - mean_all) / mean_all;
                    n_terms += 1.0;
                }
            }
            if n_terms > 0.0 {
                let avg_score = score / n_terms;
                if avg_score > best_score {
                    best_score = avg_score;
                    best_dt = dt;
                    best_df = df;
                }
            }
        }
    }
    (best_dt, best_df)
}

/// Fine-tune an FT8 candidate's time/frequency before demodulation.
pub fn refine_candidate_ft8(audio: &[f32], cand: &Candidate, p: &Params) -> Candidate {
    let costas_syms: Vec<(usize, usize)> = p.costas_pos.iter()
        .flat_map(|&csym_offset| FT8_COSTAS.iter().enumerate()
            .map(move |(k, &tone)| (csym_offset + k, tone)))
        .collect();
    let (dt, df) = refine_offset(audio, cand, p, &costas_syms);
    Candidate {
        freq_hz: cand.freq_hz + df,
        time_offset_s: cand.time_offset_s + dt,
        score: cand.score,
    }
}

/// Fine-tune an FT4 candidate's time/frequency before demodulation.
pub fn refine_candidate_ft4(audio: &[f32], cand: &Candidate, p: &Params) -> Candidate {
    let costas_syms: Vec<(usize, usize)> = p.costas_pos.iter().zip(FT4_COSTAS.iter())
        .flat_map(|(&csym_offset, pattern)| pattern.iter().enumerate()
            .map(move |(k, &tone)| (csym_offset + k, tone)))
        .collect();
    let (dt, df) = refine_offset(audio, cand, p, &costas_syms);
    Candidate {
        freq_hz: cand.freq_hz + df,
        time_offset_s: cand.time_offset_s + dt,
        score: cand.score,
    }
}
