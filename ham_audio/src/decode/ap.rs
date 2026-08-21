/// AP (a priori) decoding — retries a failed BP decode with the callsign
/// field(s) biased toward a KNOWN hypothesis (e.g. "this message is
/// probably addressed to MY callsign"), recovering weak replies a blind
/// decode would miss. Port of the bias-LLR technique from the (dead,
/// unwired) Python prototype ap_decode.py, which already validated it
/// against the old pure-Python decode primitives.
///
/// v1 scope: no report-value enumeration, unlike ap_decode.py (which
/// loops over candidate report values because it needs a full reference
/// codeword up front). Here, only the callsign field bits implied by the
/// hypothesis are biased — the report field, i3, and CRC stay governed by
/// the real, unbiased channel LLR, exactly as in a normal decode. BP
/// infers those itself. Simpler than enumerating reports; add enumeration
/// later only if live testing shows a real gap from this simplification.
use super::ldpc::bp_decode;
use super::unpack::pack_c28;

/// Raw hints as pushed from Python (see CtrlCmd::SetApHints in main.rs and
/// Config::ap_own_call/ap_partner_call/ap_queue) — read fresh once per
/// decode cycle in rx_loop.rs, same pattern as ft8_decode_mode/
/// ft8_rx_enabled, then turned into an ApHypothesis list once per pass
/// (build_hypotheses below) rather than per candidate.
#[derive(Debug, Clone, Default)]
pub struct ApHints {
    pub own_call:     String,
    pub partner_call: Option<String>,
    #[allow(dead_code)] // queue-based hypotheses deferred past v1 - see build_hypotheses
    pub queue:        Vec<String>,
}

/// Builds the v1 hypothesis list from raw hints: "own call as TO" (highest
/// value, cheapest, always tried first when hints are present) plus
/// "active QSO, both directions" when a partner is set. Queue-based
/// hypotheses are deferred - see the AP module doc comment and the plan
/// this was built from: cost is one full extra bp_decode retry (60 iters)
/// per hypothesis per BP-failing candidate, so v1 ships narrow and adds
/// more once that cost is measured live against the FT8/FT4 time budget.
pub fn build_hypotheses(hints: &ApHints) -> Vec<ApHypothesis> {
    let mut hyps = Vec::new();
    if hints.own_call.is_empty() { return hyps; }
    hyps.push(ApHypothesis { call_to: Some(hints.own_call.clone()), call_de: None });
    if let Some(partner) = &hints.partner_call {
        hyps.push(ApHypothesis { call_to: Some(hints.own_call.clone()), call_de: Some(partner.clone()) });
        hyps.push(ApHypothesis { call_to: Some(partner.clone()), call_de: Some(hints.own_call.clone()) });
    }
    hyps
}

/// One (call_to, call_de) hypothesis to bias-retry BP decode against.
/// `call_de: None` means a "TO-only" hint ("someone is calling us" — we
/// know WHO it's addressed to, not who's sending it): only c28_1's 28
/// bits get biased, c28_2 stays governed by the real channel LLR.
#[derive(Debug, Clone)]
pub struct ApHypothesis {
    pub call_to: Option<String>,
    pub call_de: Option<String>,
}

// Bias strength added to the channel LLR for each bit implied by a
// hypothesis. Ported from ap_decode.py's prior_strength — large enough to
// dominate a weak/noisy channel LLR for that bit.
const AP_PRIOR_STRENGTH: f32 = 6.0;
// More iterations than the normal 30 (ldpc.rs/mod.rs) — the biased LLR
// starts further from convergence on the still-unbiased portion of the
// message, needs more room. Ported from ap_decode.py's max_iters.
const AP_MAX_ITERS: usize = 60;

/// Adds ±AP_PRIOR_STRENGTH to llr174[start..start+28], sign per the known
/// c28 bit (LLR convention throughout this crate: positive = bit likely
/// 0). Mutates in place.
fn apply_bias(llr174: &mut [f32; 174], start: usize, c28: u32) {
    for i in 0..28 {
        let bit = (c28 >> (27 - i)) & 1;
        llr174[start + i] += if bit == 0 { AP_PRIOR_STRENGTH } else { -AP_PRIOR_STRENGTH };
    }
}

/// Bias the callsign field(s) implied by `hyp` (c28_1 = bits[0..28] for
/// call_to, c28_2 = bits[29..57] for call_de — see unpack_standard in
/// unpack.rs for the same layout on the decode side), retry bp_decode.
/// Returns the 174-bit codeword if BP converges under the bias — the
/// CALLER (mod.rs, after CRC + unpack77) must additionally verify the
/// unpacked message's actual callsigns match the hypothesis before
/// accepting: biasing toward a WRONG hypothesis can, rarely, still
/// converge to something self-consistent, the same residual risk OSD's
/// CRC-only gate already carries.
fn ap_try_hypothesis(llr174: &[f32; 174], hyp: &ApHypothesis) -> Option<[u8; 174]> {
    let mut biased = *llr174;
    let mut any = false;
    if let Some(to) = &hyp.call_to {
        apply_bias(&mut biased, 0, pack_c28(to)?);
        any = true;
    }
    if let Some(de) = &hyp.call_de {
        apply_bias(&mut biased, 29, pack_c28(de)?);
        any = true;
    }
    if !any { return None; }
    let (bits, ok, _iters) = bp_decode(&biased, AP_MAX_ITERS);
    if ok { Some(bits) } else { None }
}

/// Try hypotheses in order, first convergent one wins. Enforces the same
/// shared `deadline` as OSD/the rest of this decode pass — AP is the MORE
/// expensive fallback (a full extra bp_decode run per hypothesis, not a
/// cheap re-encode like OSD's), so callers should keep the hypothesis
/// list short (see mod.rs's hypothesis construction: "own call as TO"
/// only in v1).
pub fn try_ap(llr174: &[f32; 174], hyps: &[ApHypothesis], deadline: std::time::Instant) -> Option<[u8; 174]> {
    for hyp in hyps {
        if std::time::Instant::now() >= deadline { return None; }
        if let Some(cw) = ap_try_hypothesis(llr174, hyp) {
            return Some(cw);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::decode::crc14::{crc14, check_crc};
    use crate::decode::osd::ldpc_encode_systematic;

    /// Builds a valid 174-bit codeword for a standard (i3=1) message with
    /// the given call_to/call_de c28 values and an all-zero report field,
    /// for testing only — mirrors ft8_encoder.py's pack77+CRC+LDPC-encode
    /// pipeline just enough to exercise try_ap against a real codeword.
    fn build_test_codeword(c28_to: u32, c28_de: u32) -> [u8; 174] {
        let mut bits91 = [0u8; 91];
        for i in 0..28 { bits91[i] = ((c28_to >> (27 - i)) & 1) as u8; }
        // bit 28 = r1_1 (left 0), bits 29..57 = c28_de
        for i in 0..28 { bits91[29 + i] = ((c28_de >> (27 - i)) & 1) as u8; }
        // bit 57 = r1_2, bit 58 = r_flag, bits 59..74 = g15 (leave 0 =
        // grid AA00), bits 74..77 = i3 (001 = 1)
        bits91[76] = 1; // i3 = 1 (bits 74,75,76 = 0,0,1)
        let mut padded = [0u8; 82];
        padded[..77].copy_from_slice(&bits91[..77]);
        let crc = crc14(&padded);
        for i in 0..14 { bits91[77 + i] = ((crc >> (13 - i)) & 1) as u8; }
        assert!(check_crc(&bits91), "test setup: constructed CRC must be valid");
        ldpc_encode_systematic(&bits91)
    }

    #[test]
    fn ap_recovers_message_with_correct_hypothesis() {
        let c28_to = pack_c28("K1ABC").unwrap();
        let c28_de = pack_c28("W9XYZ").unwrap();
        let good_cw = build_test_codeword(c28_to, c28_de);

        // Correct-sign LLR for every bit EXCEPT the whole call_to field
        // (bits 0..28), which is given WEAK, WRONG-leaning LLR - simulates
        // a marginal signal where the callsign field is too noisy for
        // blind BP to converge, but the rest of the message is readable.
        let mut llr = [0f32; 174];
        for i in 0..174 {
            llr[i] = if good_cw[i] == 0 { 4.0 } else { -4.0 };
        }
        for i in 0..28 {
            // CONFIDENTLY wrong, not just weak/uncertain: BP's min-sum
            // message-passing can often still correct a merely weak/
            // uncertain field using redundancy from the other 146 bits
            // (that's the LDPC code doing its normal job) - a first
            // version of this test used weak corruption (+-0.3) and blind
            // BP converged to the CORRECT codeword anyway, which doesn't
            // exercise AP at all. Confidently wrong (same magnitude as
            // the "confident correct" bits elsewhere) is what a real
            // faded/interfered callsign field looks like and is what
            // actually defeats blind BP.
            llr[i] = if good_cw[i] == 0 { -3.5 } else { 3.5 };
        }
        let (_bits, blind_ok, _it) = bp_decode(&llr, 30);
        assert!(!blind_ok, "test setup: blind BP should fail to converge on the corrupted callsign field");

        let hyps = vec![ApHypothesis { call_to: Some("K1ABC".to_string()), call_de: None }];
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(1);
        let recovered = try_ap(&llr, &hyps, deadline);
        assert_eq!(recovered, Some(good_cw));
    }

    #[test]
    fn ap_does_not_falsely_accept_wrong_hypothesis() {
        let c28_to = pack_c28("K1ABC").unwrap();
        let c28_de = pack_c28("W9XYZ").unwrap();
        let good_cw = build_test_codeword(c28_to, c28_de);

        let mut llr = [0f32; 174];
        for i in 0..174 {
            llr[i] = if good_cw[i] == 0 { 4.0 } else { -4.0 };
        }
        for i in 0..28 {
            llr[i] = if good_cw[i] == 0 { -0.3 } else { 0.3 };
        }

        // Wrong hypothesis: biases toward a DIFFERENT callsign than the
        // one actually sent.
        let hyps = vec![ApHypothesis { call_to: Some("N0CALL".to_string()), call_de: None }];
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(1);
        let recovered = try_ap(&llr, &hyps, deadline);
        // Either BP fails to converge under the wrong bias (most likely -
        // the rest of the message's real channel LLR fights it), or it
        // converges to something that is NOT the real codeword (the
        // caller-side unpack77+hypothesis-match check is what catches
        // that case in the real pipeline - not tested here, this only
        // covers try_ap's own contract).
        assert_ne!(recovered, Some(good_cw));
    }

    #[test]
    fn try_ap_gives_up_past_deadline() {
        let llr = [1.0f32; 174];
        let hyps = vec![ApHypothesis { call_to: Some("K1ABC".to_string()), call_de: None }];
        let expired = std::time::Instant::now() - std::time::Duration::from_millis(1);
        assert_eq!(try_ap(&llr, &hyps, expired), None);
    }
}
