/// FT8/FT4 spectrogram and candidate sync search.
/// Port of sync.py and sync_ft4.py — vectorized Costas correlation.

use rustfft::num_complex::Complex;
use super::fft_cache::cached_fft_forward;
use super::params::{Params, FT8_COSTAS, FT4_COSTAS};


/// Magnitude spectrogram: [time_blocks x freq_bins]
pub struct Spectrogram {
    pub mag:        Vec<f32>,  // [n_blocks * n_bins]
    pub n_blocks:   usize,
    pub n_bins:     usize,
    pub freq_step:  f32,
    pub time_step:  usize,     // samples between blocks
    pub bin_min:    usize,
}

const F_MIN: f32 = 100.0;
// 4000 (bylo 3000): z 3000 zaden sygnal nie mogl ZACZYNAC sie powyzej
// ~2940 Hz (FT4 zajmuje ~60 Hz), a na probkach referencyjnych gubilismy
// przez to mocne stacje FT4 na 2995-3337 Hz. Koszt czasu pomijalny (~+0.02s).
const F_MAX: f32 = 4000.0;

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

/// Usrednione po czasie widmo (moc na bin) - liczone raz na przebieg,
/// wejscie dla local_noise.
pub fn avg_psd(spec: &Spectrogram) -> Vec<f32> {
    (0..spec.n_bins).map(|fi| {
        let mut acc = 0f32;
        for bi in 0..spec.n_blocks { acc += spec.mag[bi * spec.n_bins + fi]; }
        acc / spec.n_blocks.max(1) as f32
    }).collect()
}

/// Poziom szumu w okolicy `freq_hz` (+-300 Hz): 10. percentyl usrednionego
/// widma. Lokalnie, nie z calego pasma - filtr radia (zbocza, wezszy filtr
/// niz 3 kHz) zmienia tlo wzdluz pasma i globalny percentyl dawal stacjom
/// przy srodku pasma SNR zawyzony o kilka-kilkanascie dB.
pub fn local_noise(avg: &[f32], spec: &Spectrogram, freq_hz: f32) -> f32 {
    let half = (300.0 / spec.freq_step) as isize;
    let c = (freq_hz / spec.freq_step) as isize - spec.bin_min as isize;
    let lo = (c - half).max(0) as usize;
    let hi = ((c + half) as usize).min(avg.len().saturating_sub(1));
    if lo >= hi { return 1e-9; }
    let mut v: Vec<f32> = avg[lo..=hi].iter().copied().filter(|x| x.is_finite() && *x > 0.0).collect();
    if v.is_empty() { return 1e-9; }
    v.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    v[((v.len() as f32 * 0.10) as usize).min(v.len() - 1)].max(1e-9)
}

#[derive(Debug, Clone)]
pub struct Candidate {
    pub freq_hz:      f32,
    pub time_offset_s: f32,
    pub score:        f32,
}

// Ile blokow spektrogramu (po 0.08 s) przed poczatkiem okna szukamy startu
// sygnalu (13 -> ok. -1.0 s od poczatku nagrania).
const FT8_EARLY_BLOCKS: usize = 13;
// Minimalna liczba tonow Costas (z 21) w oknie, zeby kandydat sie liczyl.
const MIN_COSTAS_TERMS: usize = 14;

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

    // Zakres czasu: sygnal moze zaczac sie do FT8_EARLY_BLOCKS blokow PRZED
    // poczatkiem okna i konczyc PO jego koncu - wystarczy, ze w oknie sa
    // co najmniej dwa z trzech blokow Costas (MIN_COSTAS_TERMS). Wczesniej
    // caly sygnal musial sie miescic, wiec stacje nadajace z DT ok. -0.7 s
    // albo +2 s (zywe przyklady na probkach referencyjnych, w tym +12 dB)
    // w ogole nie trafialy do kandydatow. Brakujace symbole demodulator
    // (baseband.rs) traktuje jak zera.
    let _ = n_sym;
    let late_sym = costas_pos[1] + 7; // dwa pierwsze bloki Costas
    let max_tb = if spec.n_blocks > late_sym * sym_blocks {
        spec.n_blocks - late_sym * sym_blocks
    } else { return vec![]; };
    let max_fb = if spec.n_bins > (n_tones - 1) * bins_per_tone {
        spec.n_bins - (n_tones - 1) * bins_per_tone
    } else { return vec![]; };

    let n_t0 = max_tb + FT8_EARLY_BLOCKS;
    let n_f0 = (max_fb + bins_per_tone / 2 - 1) / (bins_per_tone / 2).max(1);

    let mut score_map = vec![0f32; n_t0 * n_f0];
    let mut term_count = vec![0u8; n_t0];

    // Scoring odporny na sasiadow: (moc_tonu - srednia_8_tonow) / srednia,
    // na LINIOWEJ mocy (spec.mag). Stara metoda log(ton)-log(max_z_7) karala
    // nakladajace sie stacje (sasiad podnosil max, spychal margin ponizej progu).
    // Odpowiednik find_candidates w Pythonie (sync.py). Uzywamy spec.mag wprost.

    for &csym_offset in costas_pos {
        for (k, &tone) in costas.iter().enumerate() {
            let sym_idx = csym_offset + k;

            for it0 in 0..n_t0 {
                let tb = it0 as isize - FT8_EARLY_BLOCKS as isize + (sym_idx * sym_blocks) as isize;
                if tb < 0 || tb >= spec.n_blocks as isize { continue; }
                let tb = tb as usize;
                term_count[it0] += 1;

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
                    score_map[it0 * n_f0 + if0] += (expected - mean_all) / mean_all;
                }
            }
        }
    }
    // Srednia po wyrazach Costas, ktore faktycznie sa w oknie.
    for it0 in 0..n_t0 {
        let nt = term_count[it0] as usize;
        for if0 in 0..n_f0 {
            let v = &mut score_map[it0 * n_f0 + if0];
            *v = if nt >= MIN_COSTAS_TERMS { *v / nt as f32 } else { 0.0 };
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
        let t0 = it0 as isize - FT8_EARLY_BLOCKS as isize;
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
