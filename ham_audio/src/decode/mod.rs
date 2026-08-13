/// FT8/FT4 full decode pipeline orchestrator.
/// Integrates sync → demod → LDPC → CRC → unpack → subtract → re-scan.

pub mod buffer;
pub mod crc14;
pub mod demod;
pub mod ldpc;
pub mod params;
pub mod rx_loop;
pub mod subtract;
pub mod sync;
pub mod unpack;

use params::{FT8, FT4, Params};
use sync::{compute_spectrogram, find_candidates_ft8, find_candidates_ft4, noise_floor_from_spec,
           refine_candidate_ft8, refine_candidate_ft4, Candidate, Spectrogram};
use demod::{extract_tone_power, extract_llr_ft8, extract_llr_ft4, estimate_snr};
use ldpc::bp_decode;
use crc14::check_crc;
use unpack::unpack77;
use subtract::{bits_to_symbols_ft8, bits_to_symbols_ft4, subtract_ft8, subtract_ft4};
use serde::Serialize;

#[derive(Debug, Clone, Serialize)]
pub struct DecodeResult {
    pub freq_hz:        f32,
    pub time_offset_s:  f32,
    pub snr_db:         f32,
    pub message:        String,
    pub call_to:        String,
    pub call_de:        String,
    pub report_or_grid: String,
    pub mode:           String,
}

// Passes over the window after subtracting each pass's successful decodes:
// pass 1 finds whatever is cleanly visible, subtracting those signals can
// reveal weaker ones that were masked by/overlapping with them in
// frequency. Diminishing returns after a few passes, so capped low to
// bound CPU cost (each pass re-runs the full candidate search + LDPC over
// every candidate).
const MAX_SUBTRACT_PASSES: usize = 3;

/// Decode one FT8 window (nominally 15s, at least 12.5s, 12000 Hz float32).
pub fn decode_ft8(audio: &[f32]) -> Vec<DecodeResult> {
    let p = &FT8;
    let min_samples = (12.5 * p.sample_rate as f64) as usize;
    if audio.len() < min_samples { return vec![]; }

    let mut residual = audio.to_vec();
    let mut all_decoded: Vec<DecodeResult> = Vec::new();

    for _pass in 0..MAX_SUBTRACT_PASSES {
        let spec = compute_spectrogram(&residual, p, 2, 2);
        // prog 0.4 dla nowego scoringu (ton-srednia)/srednia; stary log-scoring mial 0.15
        let candidates = find_candidates_ft8(&spec, p, 60, 0.4);
        let noise_floor = noise_floor_from_spec(&spec);
        let new = decode_and_subtract(&mut residual, &candidates, p, true, noise_floor, &spec, &all_decoded);
        if new.is_empty() { break; }
        all_decoded.extend(new);
    }

    all_decoded.sort_by(|a, b| a.freq_hz.partial_cmp(&b.freq_hz).unwrap_or(std::cmp::Ordering::Equal));
    all_decoded
}

/// Decode one FT4 window (nominally 7.5s, at least 4.5s, 12000 Hz float32).
pub fn decode_ft4(audio: &[f32]) -> Vec<DecodeResult> {
    let p = &FT4;
    let min_samples = (4.5 * p.sample_rate as f64) as usize;
    if audio.len() < min_samples { return vec![]; }

    let mut residual = audio.to_vec();
    let mut all_decoded: Vec<DecodeResult> = Vec::new();

    for _pass in 0..MAX_SUBTRACT_PASSES {
        let spec = compute_spectrogram(&residual, p, 2, 2);
        // prog 0.4 dla nowego scoringu (jak FT8)
        let candidates = find_candidates_ft4(&spec, p, 60, 0.4);
        let noise_floor = noise_floor_from_spec(&spec);
        let new = decode_and_subtract(&mut residual, &candidates, p, false, noise_floor, &spec, &all_decoded);
        if new.is_empty() { break; }
        all_decoded.extend(new);
    }

    all_decoded.sort_by(|a, b| a.freq_hz.partial_cmp(&b.freq_hz).unwrap_or(std::cmp::Ordering::Equal));
    all_decoded
}

/// Decode every candidate against `residual`, then subtract each newly
/// successful signal from `residual` in place (so the caller's next pass
/// sees it removed). `already` holds decodes from earlier passes, checked
/// for dedup alongside this pass's own results.
fn decode_and_subtract(
    residual: &mut [f32],
    candidates: &[Candidate],
    p: &Params,
    is_ft8: bool,
    noise_floor: f32,
    spec: &Spectrogram,
    already: &[DecodeResult],
) -> Vec<DecodeResult> {
    let mut new_decoded: Vec<DecodeResult> = Vec::new();
    let mode = if is_ft8 { "FT8" } else { "FT4" };
    let sample_rate = p.sample_rate;

    for coarse_cand in candidates {
        // Fine-tune time/freq before demodulation - the coarse candidate
        // grid (~0.08s / ~3Hz spacing) leaves enough residual offset to
        // measurably degrade LLR quality, confirmed against captured band
        // audio: several strong, unambiguously-detected candidates failed
        // LDPC convergence at the coarse position and decoded cleanly once
        // refined. See sync::refine_offset for the local search itself.
        let cand = if is_ft8 {
            refine_candidate_ft8(residual, coarse_cand, p)
        } else {
            refine_candidate_ft4(residual, coarse_cand, p)
        };
        let cand = &cand;
        let power = extract_tone_power(residual, cand, p);

        let llr174: [f32; 174] = if is_ft8 {
            extract_llr_ft8(&power, p)
        } else {
            extract_llr_ft4(&power, p)
        };

        let (bits174, success, _iters) = bp_decode(&llr174, 50);
        if !success { continue; }

        // bits174[0..91] = [data77/scrambled77 | crc14]
        let data77: Vec<u8> = if is_ft8 {
            // FT8: CRC liczone na data77, sprawdz bezposrednio
            let mut b91 = [0u8; 91];
            b91.copy_from_slice(&bits174[..91]);
            if !check_crc(&b91) { continue; }
            bits174[..77].to_vec()
        } else {
            // FT4: CRC liczone na SCRAMBLED bitach. Sprawdz CRC na scrambled,
            // potem descrambluj zeby dostac plaintext do unpack77.
            let mut b91 = [0u8; 91];
            b91.copy_from_slice(&bits174[..91]);
            if !check_crc(&b91) { continue; }
            // Descrambling: data77 = scrambled77 XOR RVEC
            (0..77).map(|i| bits174[i] ^ FT4_RVEC[i]).collect()
        };

        let msg = match unpack77(&data77) {
            Some(m) => m,
            None => continue,
        };

        let snr = estimate_snr(spec, cand.freq_hz, noise_floor);

        // Dedup against both earlier passes and this pass's own results.
        let dup_check = |d: &&DecodeResult| {
            d.message == msg.message
            && (d.freq_hz - cand.freq_hz).abs() < 8.0
            && (d.time_offset_s - cand.time_offset_s).abs() < 0.1
        };
        if already.iter().any(|d| dup_check(&d)) { continue; }
        if new_decoded.iter().any(|d| dup_check(&d)) { continue; }

        // Subtract this signal from the residual before moving on, so a
        // weaker signal that was overlapping it in frequency has a chance
        // to surface on the next pass's candidate search. Reconstructs the
        // tone sequence from the bits we already decoded - no separate
        // encoder needed.
        if is_ft8 {
            let itone = bits_to_symbols_ft8(&bits174);
            subtract_ft8(residual, &itone, cand.freq_hz, cand.time_offset_s, sample_rate);
        } else {
            let itone = bits_to_symbols_ft4(&bits174);
            subtract_ft4(residual, &itone, cand.freq_hz, cand.time_offset_s, sample_rate);
        }

        new_decoded.push(DecodeResult {
            freq_hz:        cand.freq_hz,
            time_offset_s:  cand.time_offset_s,
            snr_db:         snr,
            message:        msg.message,
            call_to:        msg.call_to,
            call_de:        msg.call_de,
            report_or_grid: msg.report_or_grid,
            mode:           mode.to_string(),
        });
    }

    new_decoded
}

/// FT4 scrambling mask (RVEC, 77 bits) — from params_ft4.py.
const FT4_RVEC: [u8; 77] = [
    0,1,0,0,1,0,1,0,0,1,0,1,1,1,1,0,1,0,0,0,1,0,0,1,1,0,1,1,0,
    1,0,0,1,0,1,1,0,0,0,0,1,0,0,0,1,0,1,0,0,1,1,1,1,0,0,1,0,1,
    0,1,0,1,0,1,1,0,1,1,1,1,1,0,0,0,1,0,1,
];
