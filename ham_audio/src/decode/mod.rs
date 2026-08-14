/// FT8/FT4 full decode pipeline orchestrator.
/// Integrates sync → demod → LDPC → CRC → unpack → subtract → re-scan.

pub mod buffer;
pub mod crc14;
pub mod demod;
pub mod fft_cache;
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
use rayon::prelude::*;

/// Per-pass phase timing, reported alongside that pass's results via
/// on_pass_results - see decode_ft8's doc comment. Four fixes in a row
/// (streaming delivery, FFT-plan caching, LDPC-graph caching, tokio
/// blocking-pool keep-alive) each targeted a DIFFERENT plausible mechanism
/// and NONE moved the live-measured pass_elapsed_s number at all (stayed
/// ~1.08s across all of them, regardless of n_results 0-26). That flatness
/// pattern plus zero response to four independent fixes means guessing
/// again isn't productive - this struct exists to make the NEXT live log
/// show the exact phase breakdown directly instead of another single
/// ambiguous total, so whatever is actually slow becomes visible by name.
#[derive(Debug, Clone, Copy, Default)]
pub struct PassTiming {
    pub spec_ms:       f64,
    pub find_cand_ms:  f64,
    pub par_decode_ms: f64,
    pub n_cand:        usize,
}

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

// Wall-clock budget for the ENTIRE decode_ft8/decode_ft4 call (all passes +
// subtraction combined), measured from when the window's audio snapshot is
// handed in. Each subtraction is an FFT over close to the whole window
// buffer (subtract.rs's nfft is ~audio.len()), done once per successfully
// decoded signal - on a busy band (measured live: 20-25+ decodes per FT8
// window) that cost stacked up enough to blow past the 15s/7.5s window
// itself, confirmed by a receive-side timestamp diagnostic in webapp.py
// showing decode results landing 14.5-14.7s into their own 15s window
// (should be ~0-3s). That left the operator's correct reply window already
// expired by the time a decode was even visible to click - every reply
// then had to wait a full extra ~15s cycle. Once this budget is exceeded,
// already-decoded signals are kept (no lost QSOs) but further subtraction/
// extra passes are skipped, trading the multi-pass masked-signal recovery
// for staying inside the real-time deadline - see the deadline checks below.
//
// Budget is tight ON PURPOSE: an FT8 window is 15s and one transmission is
// 12.64s long, so the operator's OWN reply window only has 15-12.64=2.36s
// of slack total between "the reply window starts" and "there's no longer
// enough room left in it to fit our transmission" - and that whole 2.36s
// budget has to cover RX settle+decode, TCP/JSON, Python-side processing,
// AND the human noticing/clicking the decode. A first attempt at 6.0s
// still left decodes landing 10s into their window (measured live) -
// pass 0's OWN end-of-pass subtractions alone (one FFT-heavy call per
// decoded signal) were eating most of that before the "start pass 2?"
// check even ran. Tightened hard so subtraction only gets whatever time is
// left after the base candidate-search+LDPC decode (which was already
// real-time-safe before SIC was added) - full multi-pass benefit survives
// on quiet bands where that finishes fast, degrades gracefully toward
// "no subtraction, decode only" (the pre-SIC baseline) as band activity
// (and thus decode cost) rises, instead of blowing the deadline outright.
//
// Tightened further (1.0s -> 0.5s) after parallelizing candidate decoding
// (see decode_and_subtract's rayon par_iter phase below): parallelism
// alone recovered full pre-parallel decode quality at 1.0s, so a live test
// after that change measured pos_in_win at a MUCH better ~1.4s (was
// 10-14s) - but even so, a manual "click a fresh CQ" reply still missed
// its immediate reply window almost every time. The reason: the window's
// total slack is only 2.36s (15-12.64s), and 1.0s of RX alone was already
// eating most of it, leaving under a second for TCP/Python/UI/human
// reaction - nowhere near enough margin for a human to reliably click in
// time, even though the user reports real WSJT-X rarely misses this.
// Measured against the WSJT-X reference file: 0.5s costs only 1 signal
// out of 67 vs 1.0s (parallelism had already made the budget nearly
// irrelevant to quality - the real cost only shows up below ~0.3s).
const FT8_TIME_BUDGET_S: f64 = 0.5;
const FT4_TIME_BUDGET_S: f64 = 0.25;

/// Decode one FT8 window (nominally 15s, at least 12.5s, 12000 Hz float32).
///
/// `on_pass_results` is called once per pass, with that pass's newly
/// decoded (deduped, NOT-yet-subtracted) results, as soon as they're known -
/// see decode_and_subtract's doc comment for why this doesn't wait on
/// subtraction. This is what lets rx_loop.rs stream pass 0's results (the
/// overwhelming majority on a busy band - subtraction/later passes mostly
/// recover a handful of extra masked signals) to Python within roughly
/// spectrogram+candidate-search+parallel-LDPC time (~150-200ms measured
/// offline), instead of after the full multi-pass+subtraction budget
/// (~500ms-1s+ measured live) - see FT8_TIME_BUDGET_S's comment for the
/// full history of why that gap mattered.
pub fn decode_ft8(audio: &[f32], on_pass_results: &mut dyn FnMut(&[DecodeResult], &PassTiming)) -> Vec<DecodeResult> {
    let p = &FT8;
    let min_samples = (12.5 * p.sample_rate as f64) as usize;
    if audio.len() < min_samples { return vec![]; }

    let deadline = std::time::Instant::now() + std::time::Duration::from_secs_f64(FT8_TIME_BUDGET_S);
    let mut residual = audio.to_vec();
    let mut all_decoded: Vec<DecodeResult> = Vec::new();

    for _pass in 0..MAX_SUBTRACT_PASSES {
        if _pass > 0 && std::time::Instant::now() >= deadline { break; }
        let t_spec = std::time::Instant::now();
        let spec = compute_spectrogram(&residual, p, 2, 2);
        let spec_ms = t_spec.elapsed().as_secs_f64() * 1000.0;
        let t_cand = std::time::Instant::now();
        // prog 0.4 dla nowego scoringu (ton-srednia)/srednia; stary log-scoring mial 0.15
        let candidates = find_candidates_ft8(&spec, p, 60, 0.4);
        let find_cand_ms = t_cand.elapsed().as_secs_f64() * 1000.0;
        let noise_floor = noise_floor_from_spec(&spec);
        let new = decode_and_subtract(&mut residual, &candidates, p, true, noise_floor, &spec, &all_decoded, deadline, spec_ms, find_cand_ms, on_pass_results);
        if new.is_empty() { break; }
        all_decoded.extend(new);
    }

    all_decoded.sort_by(|a, b| a.freq_hz.partial_cmp(&b.freq_hz).unwrap_or(std::cmp::Ordering::Equal));
    all_decoded
}

/// Decode one FT4 window (nominally 7.5s, at least 4.5s, 12000 Hz float32).
/// See decode_ft8 for `on_pass_results`.
pub fn decode_ft4(audio: &[f32], on_pass_results: &mut dyn FnMut(&[DecodeResult], &PassTiming)) -> Vec<DecodeResult> {
    let p = &FT4;
    let min_samples = (4.5 * p.sample_rate as f64) as usize;
    if audio.len() < min_samples { return vec![]; }

    let deadline = std::time::Instant::now() + std::time::Duration::from_secs_f64(FT4_TIME_BUDGET_S);
    let mut residual = audio.to_vec();
    let mut all_decoded: Vec<DecodeResult> = Vec::new();

    for _pass in 0..MAX_SUBTRACT_PASSES {
        if _pass > 0 && std::time::Instant::now() >= deadline { break; }
        let t_spec = std::time::Instant::now();
        let spec = compute_spectrogram(&residual, p, 2, 2);
        let spec_ms = t_spec.elapsed().as_secs_f64() * 1000.0;
        let t_cand = std::time::Instant::now();
        // prog 0.4 dla nowego scoringu (jak FT8)
        let candidates = find_candidates_ft4(&spec, p, 60, 0.4);
        let find_cand_ms = t_cand.elapsed().as_secs_f64() * 1000.0;
        let noise_floor = noise_floor_from_spec(&spec);
        let new = decode_and_subtract(&mut residual, &candidates, p, false, noise_floor, &spec, &all_decoded, deadline, spec_ms, find_cand_ms, on_pass_results);
        if new.is_empty() { break; }
        all_decoded.extend(new);
    }

    all_decoded.sort_by(|a, b| a.freq_hz.partial_cmp(&b.freq_hz).unwrap_or(std::cmp::Ordering::Equal));
    all_decoded
}

/// Decode every candidate against `residual`, call `on_pass_results` with
/// this pass's newly-decoded (deduped) results, THEN subtract each of them
/// from `residual` in place (so the caller's next pass sees it removed).
/// `already` holds decodes from earlier passes, checked for dedup alongside
/// this pass's own results. Once `deadline` has passed, candidates keep
/// decoding normally (cheap) but subtraction is skipped (expensive,
/// near-full-window FFT) - see FT8_TIME_BUDGET_S above.
///
/// Three phases, mirroring how JTDX itself gets real-time FT8 decoding done:
/// its decoder.f90 splits the frequency range across N OpenMP threads for
/// the expensive sync-refine+demod+LDPC work. Candidate search/decode here
/// is likewise parallelized (rayon) - it only ever READS `residual`, so
/// there's no shared mutable state to race on.
///
/// Dedup (2nd phase) is cheap (just comparisons) and its result is what the
/// operator actually needs to see and click - it does NOT depend on
/// subtraction in any way, subtraction only prepares `residual` for a LATER
/// pass to find additional masked signals. So `on_pass_results` fires right
/// after dedup, before subtraction even starts: on a busy live band this
/// was measured to cut the delay before a decode is visible to the operator
/// from ~500ms-1s+ (full pass incl. subtraction) down to ~150-200ms
/// (spectrogram+candidate-search+parallel-decode alone) for the pass-0
/// results that make up the overwhelming majority of decodes.
///
/// Subtraction (3rd phase), which DOES mutate `residual`, stays strictly
/// sequential and runs AFTER results are already handed to the caller: SIC
/// is order-sensitive by nature (subtracting signal A can change whether a
/// nearby signal B decodes), so keeping it serial preserves the exact same
/// multi-pass recovery behavior as before - it just no longer blocks
/// delivery of this pass's own results.
fn decode_and_subtract(
    residual: &mut [f32],
    candidates: &[Candidate],
    p: &Params,
    is_ft8: bool,
    noise_floor: f32,
    spec: &Spectrogram,
    already: &[DecodeResult],
    deadline: std::time::Instant,
    spec_ms: f64,
    find_cand_ms: f64,
    on_pass_results: &mut dyn FnMut(&[DecodeResult], &PassTiming),
) -> Vec<DecodeResult> {
    let mode = if is_ft8 { "FT8" } else { "FT4" };
    let sample_rate = p.sample_rate;
    let n_cand = candidates.len();

    // Reborrow as shared/immutable for the parallel phase - subtraction
    // (the only mutation) happens later, sequentially, once this borrow
    // and the parallel iterator over it have gone out of scope.
    let residual_ro: &[f32] = residual;

    let t_par = std::time::Instant::now();
    let decoded: Vec<(Candidate, [u8; 174], DecodeResult)> = candidates
        .par_iter()
        .filter_map(|coarse_cand| {
            // Fine-tune time/freq before demodulation - the coarse candidate
            // grid (~0.08s / ~3Hz spacing) leaves enough residual offset to
            // measurably degrade LLR quality, confirmed against captured
            // band audio: several strong, unambiguously-detected candidates
            // failed LDPC convergence at the coarse position and decoded
            // cleanly once refined. See sync::refine_offset for the local
            // search itself.
            let cand = if is_ft8 {
                refine_candidate_ft8(residual_ro, coarse_cand, p)
            } else {
                refine_candidate_ft4(residual_ro, coarse_cand, p)
            };
            let power = extract_tone_power(residual_ro, &cand, p);

            let llr174: [f32; 174] = if is_ft8 {
                extract_llr_ft8(&power, p)
            } else {
                extract_llr_ft4(&power, p)
            };

            // JTDX's own reference decoder (ft8b.f90: max_iterations=30) uses
            // 30, not 50 - this port had been running 67% more BP iterations
            // than the software it's modeled after. Matters most for
            // candidates that DON'T converge (the majority on a real band -
            // e.g. 60 candidates searched, ~18-20 typically succeed): those
            // run the FULL iteration budget before giving up, so this is a
            // direct, proportional cut to the dominant cost of the
            // par_decode phase - confirmed live (i7-3612QM, 8 rayon threads
            // actually in use) to be ~1.05s for 60 candidates, ~7-8x the
            // ~130-165ms measured offline, with no other explanation found
            // (thread count, CPU affinity and power plan all ruled out).
            let (bits174, success, _iters) = bp_decode(&llr174, 30);
            if !success { return None; }

            // bits174[0..91] = [data77/scrambled77 | crc14]
            let data77: Vec<u8> = if is_ft8 {
                // FT8: CRC liczone na data77, sprawdz bezposrednio
                let mut b91 = [0u8; 91];
                b91.copy_from_slice(&bits174[..91]);
                if !check_crc(&b91) { return None; }
                bits174[..77].to_vec()
            } else {
                // FT4: CRC liczone na SCRAMBLED bitach. Sprawdz CRC na scrambled,
                // potem descrambluj zeby dostac plaintext do unpack77.
                let mut b91 = [0u8; 91];
                b91.copy_from_slice(&bits174[..91]);
                if !check_crc(&b91) { return None; }
                // Descrambling: data77 = scrambled77 XOR RVEC
                (0..77).map(|i| bits174[i] ^ FT4_RVEC[i]).collect()
            };

            let msg = unpack77(&data77)?;
            let snr = estimate_snr(spec, cand.freq_hz, noise_floor);

            let result = DecodeResult {
                freq_hz:        cand.freq_hz,
                time_offset_s:  cand.time_offset_s,
                snr_db:         snr,
                message:        msg.message,
                call_to:        msg.call_to,
                call_de:        msg.call_de,
                report_or_grid: msg.report_or_grid,
                mode:           mode.to_string(),
            };
            Some((cand, bits174, result))
        })
        .collect();
    let par_decode_ms = t_par.elapsed().as_secs_f64() * 1000.0;

    // Dedup phase: cheap (just comparisons), no subtraction yet. par_iter()
    // doesn't preserve candidate order, but that's fine here - dedup is
    // symmetric (checked both ways).
    let mut new_decoded: Vec<(Candidate, [u8; 174], DecodeResult)> = Vec::new();
    for (cand, bits174, result) in decoded {
        let dup_check = |d: &DecodeResult| {
            d.message == result.message
            && (d.freq_hz - cand.freq_hz).abs() < 8.0
            && (d.time_offset_s - cand.time_offset_s).abs() < 0.1
        };
        if already.iter().any(dup_check) { continue; }
        if new_decoded.iter().any(|(_, _, d)| dup_check(d)) { continue; }
        new_decoded.push((cand, bits174, result));
    }

    // Hand results to the caller NOW - see this function's doc comment for
    // why this doesn't need to wait for subtraction below.
    let results: Vec<DecodeResult> = new_decoded.iter().map(|(_, _, r)| r.clone()).collect();
    let timing = PassTiming { spec_ms, find_cand_ms, par_decode_ms, n_cand };
    on_pass_results(&results, &timing);

    // Subtract each signal from the residual, so a weaker signal that was
    // overlapping it in frequency has a chance to surface on the next
    // pass's candidate search. Reconstructs the tone sequence from the bits
    // we already decoded - no separate encoder needed. Skipped once over
    // the wall-clock deadline - this decode result is kept either way, only
    // the (expensive) residual cleanup for LATER candidates/passes is what
    // gets sacrificed.
    for (cand, bits174, _result) in &new_decoded {
        if std::time::Instant::now() < deadline {
            if is_ft8 {
                let itone = bits_to_symbols_ft8(bits174);
                subtract_ft8(residual, &itone, cand.freq_hz, cand.time_offset_s, sample_rate);
            } else {
                let itone = bits_to_symbols_ft4(bits174);
                subtract_ft4(residual, &itone, cand.freq_hz, cand.time_offset_s, sample_rate);
            }
        }
    }

    results
}

/// FT4 scrambling mask (RVEC, 77 bits) — from params_ft4.py.
const FT4_RVEC: [u8; 77] = [
    0,1,0,0,1,0,1,0,0,1,0,1,1,1,1,0,1,0,0,0,1,0,0,1,1,0,1,1,0,
    1,0,0,1,0,1,1,0,0,0,0,1,0,0,0,1,0,1,0,0,1,1,1,1,0,0,1,0,1,
    0,1,0,1,0,1,1,0,1,1,1,1,1,0,0,0,1,0,1,
];
