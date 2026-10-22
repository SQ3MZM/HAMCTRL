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
    /// Czestotliwosc audio RX (Hz) - AP z partnerem tylko w jej poblizu.
    pub rx_freq:      Option<f32>,
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

// More iterations than the normal 30 (ldpc.rs/mod.rs) — the biased LLR
// starts further from convergence on the still-unbiased portion of the
// message, needs more room. Ported from ap_decode.py's max_iters.
const AP_MAX_ITERS: usize = 60;

/// Bit tresci komunikatu -> bit w slowie kodowym. FT4 nadaje komunikat
/// XOR-owany z RVEC (scrambling), wiec znany bit trzeba przeksztalcic tak
/// samo - wczesniej AP w FT4 biasowal NIEscramblowane bity, czyli w polowie
/// pozycji w zla strone.
fn known_bit(bit: u8, pos: usize, scramble: Option<&[u8; 77]>) -> u8 {
    match scramble { Some(r) if pos < 77 => bit ^ r[pos], _ => bit }
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
fn ap_try_hypothesis(llr174: &[f32; 174], hyp: &ApHypothesis, scramble: Option<&[u8; 77]>) -> Option<[u8; 174]> {
    let to = match &hyp.call_to { Some(c) => Some(pack_c28(c)?), None => None };
    let de = match &hyp.call_de { Some(c) => Some(pack_c28(c)?), None => None };
    if to.is_none() && de.is_none() { return None; }
    // Znane bity: pola c28 z hipotezy, ich bity r1 (=0, bez /R) oraz typ
    // komunikatu i3=1 (standardowy) - jak przy AP dla CQ (try_ap_cq).
    let mut known: Vec<(usize, u8)> = Vec::with_capacity(64);
    if let Some(c) = to {
        for i in 0..28 { known.push((i, ((c >> (27 - i)) & 1) as u8)); }
        known.push((28, 0));
    }
    if let Some(c) = de {
        for i in 0..28 { known.push((29 + i, ((c >> (27 - i)) & 1) as u8)); }
        known.push((57, 0));
    }
    known.extend_from_slice(&[(74, 0), (75, 0), (76, 1)]);
    try_known_bits(llr174, &known, scramble)
}

/// Znane bity komunikatu dostaja pelna pewnosc (wieksza niz najpewniejszy bit
/// z kanalu), potem BP, a gdy nie zbiegnie - pelny OSD (znane bity trafiaja
/// wtedy na pewno do bazy informacyjnej). Wynik musi miec dokladnie te bity -
/// zbieznosc pod wymuszeniem nie znaczy jeszcze, ze to TEN komunikat.
fn try_known_bits(llr174: &[f32; 174], known: &[(usize, u8)], scramble: Option<&[u8; 77]>) -> Option<[u8; 174]> {
    let apmag = llr174.iter().fold(0f32, |m, v| m.max(v.abs())) * 1.01;
    let mut z = *llr174;
    for &(pos, b) in known {
        z[pos] = if known_bit(b, pos, scramble) == 0 { apmag } else { -apmag };
    }
    let ok_known = |cw: &[u8; 174]| known.iter().all(|&(pos, b)| cw[pos] == known_bit(b, pos, scramble));
    let (bits, ok, _) = bp_decode(&z, AP_MAX_ITERS);
    if ok && ok_known(&bits) { return Some(bits); }
    let (cw, _d) = super::osd::osd_full(&z, 40)?;
    if ok_known(&cw) { Some(cw) } else { None }
}

pub fn try_ap(llr174: &[f32; 174], hyps: &[ApHypothesis], scramble: Option<&[u8; 77]>, deadline: std::time::Instant) -> Option<[u8; 174]> {
    for hyp in hyps {
        if std::time::Instant::now() >= deadline { return None; }
        if let Some(cw) = ap_try_hypothesis(llr174, hyp, scramble) {
            return Some(cw);
        }
    }
    None
}

/// AP "to jest CQ": znane 32 bity - c28 = CQ (2) z r1=0 (bity 0..29) oraz
/// i3=1 (bity 74..77) - nie wymaga zadnej wiedzy o stacjach, wiec dziala dla
/// kazdego slabego CQ na pasmie. Znane bity dostaja pelna pewnosc (wieksza
/// niz najpewniejszy bit z kanalu), potem BP, a gdy nie zbiegnie - pelny OSD
/// (znane bity trafiaja wtedy na pewno do bazy informacyjnej). Wynik musi
/// miec dokladnie te 32 bity.
pub fn try_ap_cq(llr174: &[f32; 174], scramble: Option<&[u8; 77]>) -> Option<[u8; 174]> {
    let mut known: Vec<(usize, u8)> = Vec::with_capacity(32);
    for i in 0..28 { known.push((i, ((2u32 >> (27 - i)) & 1) as u8)); }
    known.extend_from_slice(&[(28, 0), (74, 0), (75, 0), (76, 1)]);
    try_known_bits(llr174, &known, scramble)
}

/// Pelne slowa kodowe (FT8 i FT4) wszystkich komunikatow, ktore partner QSO
/// moze do nas wyslac na etapie raportow i konczenia: "MOJ PARTNER RRR /
/// RR73 / 73 / +-NN / R+-NN" (raporty -30..+20). Dla takiego komunikatu
/// znane jest CALE slowo kodowe, wiec zamiast dekodowac wystarczy sprawdzic,
/// ktore z nich pasuje do odebranego sygnalu (try_ap_full).
pub struct FullHypotheses {
    cw: Vec<[[u8; 174]; 2]>,
}

pub fn build_full_hypotheses(hints: &ApHints) -> FullHypotheses {
    let mut cw = Vec::new();
    let (Some(partner), false) = (&hints.partner_call, hints.own_call.is_empty()) else {
        return FullHypotheses { cw };
    };
    let (Some(c1), Some(c2)) = (pack_c28(&hints.own_call), pack_c28(partner)) else {
        return FullHypotheses { cw };
    };
    // RRR=32402, 73=32404; RR73 nadawane jest jako LOKATOR "RR73" (32373),
    // nie kod 32403 - oba wyswietlaja sie tak samo, ale maja inne slowa kodowe.
    let mut tails: Vec<(bool, u32)> = vec![(false, 32402), (false, 32373), (false, 32404)];
    for rep in -30i32..=20 {
        let g15 = (32400 + 35 + rep) as u32;
        tails.push((false, g15));
        tails.push((true, g15));
    }
    for (rflag, g15) in tails {
        let mut bits77 = [0u8; 77];
        for i in 0..28 { bits77[i] = ((c1 >> (27 - i)) & 1) as u8; }
        for i in 0..28 { bits77[29 + i] = ((c2 >> (27 - i)) & 1) as u8; }
        bits77[58] = rflag as u8;
        for i in 0..15 { bits77[59 + i] = ((g15 >> (14 - i)) & 1) as u8; }
        bits77[76] = 1; // i3 = 1
        let mut scr = bits77;
        for i in 0..77 { scr[i] ^= super::FT4_RVEC[i]; }
        cw.push([encode77(&bits77), encode77(&scr)]);
    }
    FullHypotheses { cw }
}

fn encode77(bits77: &[u8; 77]) -> [u8; 174] {
    let mut padded = [0u8; 82];
    padded[..77].copy_from_slice(bits77);
    let crc = super::crc14::crc14(&padded);
    let mut b91 = [0u8; 91];
    b91[..77].copy_from_slice(bits77);
    for i in 0..14 { b91[77 + i] = ((crc >> (13 - i)) & 1) as u8; }
    super::osd::ldpc_encode_systematic(&b91)
}

/// Najlepiej pasujace pelne slowo kodowe. Miara: wazona niezgodnosc (suma
/// |LLR| na bitach niezgodnych ze slowem, wzgledem sumy |LLR|) USREDNIONA po
/// wszystkich zestawach LLR z demodulacji - dla prawdziwego komunikatu
/// wszystkie zestawy wskazuja to samo slowo, dla szumu usrednianie zmniejsza
/// przypadkowe wahania (najlepszy z kilku zestawow dawal szumowi kilka szans).
/// Zwraca (slowo, niezgodnosc najlepszego, niezgodnosc drugiego) - progi
/// (bezwzgledny i jednoznacznosci) ustawia wywolujacy.
pub fn try_ap_full(llr_sets: &[[f32; 174]], h: &FullHypotheses, is_ft8: bool) -> Option<([u8; 174], f32, f32)> {
    if h.cw.is_empty() || llr_sets.is_empty() { return None; }
    let tots: Vec<f32> = llr_sets.iter().map(|l| l.iter().map(|v| v.abs()).sum()).collect();
    if tots.iter().any(|&t| t <= 0.0) { return None; }
    let k = if is_ft8 { 0 } else { 1 };
    let mut best: Option<([u8; 174], f32)> = None;
    let mut second = f32::MAX;
    for pair in &h.cw {
        let cw = &pair[k];
        let mut acc = 0f32;
        for (l, t) in llr_sets.iter().zip(tots.iter()) {
            let d: f32 = (0..174).filter(|&i| ((l[i] < 0.0) as u8) != cw[i]).map(|i| l[i].abs()).sum();
            acc += d / t;
        }
        let rel = acc / llr_sets.len() as f32;
        match &best {
            Some(b) if rel >= b.1 => { if rel < second { second = rel; } }
            _ => { if let Some(b) = &best { second = b.1; } best = Some((*cw, rel)); }
        }
    }
    // (slowo, niezgodnosc najlepszego, niezgodnosc drugiego najlepszego)
    best.map(|(cw, rel)| (cw, rel, second))
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
        let recovered = try_ap(&llr, &hyps, None, deadline);
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
        let recovered = try_ap(&llr, &hyps, None, deadline);
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
        assert_eq!(try_ap(&llr, &hyps, None, expired), None);
    }
}
