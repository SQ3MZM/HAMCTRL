/// FT8/FT4 full decode pipeline orchestrator.
/// Integrates sync → demod → LDPC → CRC → unpack.

pub mod buffer;
pub mod crc14;
pub mod demod;
pub mod ldpc;
pub mod params;
pub mod rx_loop;
pub mod sync;
pub mod unpack;

use params::{FT8, FT4, Params};
use sync::{compute_spectrogram, find_candidates_ft8, find_candidates_ft4, noise_floor_from_spec, Candidate, Spectrogram};
use demod::{extract_tone_power, extract_llr_ft8, extract_llr_ft4, estimate_snr};
use ldpc::bp_decode;
use crc14::check_crc;
use unpack::unpack77;
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

/// Decode one FT8 window (nominally 15s, at least 12.5s, 12000 Hz float32).
pub fn decode_ft8(audio: &[f32]) -> Vec<DecodeResult> {
    let p = &FT8;
    let min_samples = (12.5 * p.sample_rate as f64) as usize;
    if audio.len() < min_samples { return vec![]; }

    let spec = compute_spectrogram(audio, p, 2, 2);
    // prog 0.4 dla nowego scoringu (ton-srednia)/srednia; stary log-scoring mial 0.15
    let candidates = find_candidates_ft8(&spec, p, 60, 0.4);
    let noise_floor = noise_floor_from_spec(&spec);
    decode_candidates(audio, &candidates, p, true, noise_floor, &spec)
}

/// Decode one FT4 window (nominally 7.5s, at least 4.5s, 12000 Hz float32).
pub fn decode_ft4(audio: &[f32]) -> Vec<DecodeResult> {
    let p = &FT4;
    let min_samples = (4.5 * p.sample_rate as f64) as usize;
    if audio.len() < min_samples { return vec![]; }

    let spec = compute_spectrogram(audio, p, 2, 2);
    // prog 0.4 dla nowego scoringu (jak FT8)
    let candidates = find_candidates_ft4(&spec, p, 60, 0.4);
    let noise_floor = noise_floor_from_spec(&spec);
    decode_candidates(audio, &candidates, p, false, noise_floor, &spec)
}

fn decode_candidates(
    audio: &[f32],
    candidates: &[Candidate],
    p: &Params,
    is_ft8: bool,
    noise_floor: f32,
    spec: &Spectrogram,
) -> Vec<DecodeResult> {
    let mut decoded: Vec<DecodeResult> = Vec::new();
    let mode = if is_ft8 { "FT8" } else { "FT4" };

    for cand in candidates {
        let power = extract_tone_power(audio, cand, p);

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

        // Dedup
        if decoded.iter().any(|d: &DecodeResult| {
            d.message == msg.message
            && (d.freq_hz - cand.freq_hz).abs() < 8.0
            && (d.time_offset_s - cand.time_offset_s).abs() < 0.1
        }) { continue; }

        decoded.push(DecodeResult {
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

    decoded.sort_by(|a, b| a.freq_hz.partial_cmp(&b.freq_hz).unwrap_or(std::cmp::Ordering::Equal));
    decoded
}

/// FT4 scrambling mask (RVEC, 77 bits) — from params_ft4.py.
const FT4_RVEC: [u8; 77] = [
    0,1,0,0,1,0,1,0,0,1,0,1,1,1,1,0,1,0,0,0,1,0,0,1,1,0,1,1,0,
    1,0,0,1,0,1,1,0,0,0,0,1,0,0,0,1,0,1,0,0,1,1,1,1,0,0,1,0,1,
    0,1,0,1,0,1,1,0,1,1,1,1,1,0,0,0,1,0,1,
];

