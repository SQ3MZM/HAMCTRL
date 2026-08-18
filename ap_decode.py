"""
AP (a-priori) decoding for FT8 — targeted, not brute force.
Recovers weak signals when the context (callsigns) is known.
Three hypothesis sources:
  A) active QSO: pair (our_call, clicked_partner)
  B) someone is calling us: call_to = our_call
  C) band context: callsigns seen in previous windows
"""
import numpy as np
import re
from demod import extract_llr174
from ldpc_decode import bp_decode
from unpack import unpack77, format_message
import ft8_encoder as enc


def _bits174(call_to, call_de, rpt):
    """Message -> 174 bits (via pack77 + crc14 + ldpc_encode)."""
    b = enc.pack77(call_to, call_de, rpt)
    c = enc._crc14(b + [0] * 5)
    a91 = b + list(c[:14])
    return np.array(enc._ldpc_encode(np.array(a91[:91], dtype=np.int32))[:174]).astype(int)


def _check_crc(hard):
    c = enc._crc14(list(hard[:77]) + [0] * 5)
    return list(c[:14]) == list(hard[77:91])


# most common reports in an FT8 QSO (order = probability priority)
_COMMON_REPORTS = ['-01', '-05', '-10', '-15', 'R-01', 'R-05', 'R-10',
                   'RR73', '73', '+00', '-03', '-08', '-12', '-18']


def ap_try_candidate(power, hypotheses, prior_strength=6.0, max_iters=60):
    """
    For a single candidate (its 'power' from extract_tone_power), tries the
    list of hypotheses (call_to, call_de). Returns (msg, i3) for the first
    one that decodes with a valid CRC and matches the hypothesis, or None.
    The report is iterated from _COMMON_REPORTS (since it's usually unknown).
    """
    llr_ch = extract_llr174(power)
    for (to, de) in hypotheses:
        for rpt in _COMMON_REPORTS:
            try:
                full = _bits174(to, de, rpt)
            except Exception:
                continue
            prior = np.zeros(174)
            # prior only on the CALLSIGN bits [0:57] (leave the report to the channel)
            prior[:57] = np.where(full[:57] == 0, prior_strength, -prior_strength)
            hard, ok, it = bp_decode(llr_ch + prior, max_iters=max_iters)
            if not ok or not _check_crc(hard):
                continue
            msg = format_message(unpack77(list(hard[:77]))).strip()
            # VALIDATION: the decode must contain the hypothesized callsigns
            if de in msg and (to in msg or to == 'CQ'):
                return msg
    return None


def build_band_context(decoded_messages):
    """
    Mode C: extracts the set of callsigns (band context) from already
    decoded messages.
    """
    calls = set()
    for msg in decoded_messages:
        for tok in msg.split():
            if tok in ('CQ', 'DE', 'QRZ', 'RR73', 'RRR', '73'):
                continue
            if re.match(r'^R?[+-]?\d+$', tok):
                continue
            if re.match(r'^[A-R]{2}\d{2}([a-x]{2})?$', tok):
                continue
            if tok.startswith('<') or '...' in tok:
                continue
            calls.add(tok)
    return calls


def make_hypotheses_band(known_calls, max_pairs=40):
    """
    Mode C: builds a TARGETED list of pair hypotheses from known callsigns.
    NOT all pairs (that would time out) — prioritizes:
      1. CQ from a known callsign (someone might be answering)
      2. pairs where both callsigns are known (ongoing QSO)
    Caps at the max_pairs most plausible ones.
    """
    calls = list(known_calls)
    hyps = []
    # 1. CQ from each known callsign (someone calling a known callsign)
    for de in calls:
        hyps.append(('CQ', de))
    # 2. pairs of known callsigns (reply in a QSO) — capped
    # priority: pairs that could plausibly be talking (heuristic: all of
    # them, but capped). In a full implementation: pairs from actually
    # observed QSOs.
    for i, de in enumerate(calls):
        for to in calls:
            if to == de:
                continue
            hyps.append((to, de))
            if len(hyps) >= max_pairs:
                return hyps
    return hyps


def make_hypotheses_qso(my_calls, partner):
    """
    Mode A: active QSO. Very narrow, the safest mode.
    Pair (our_call, partner) in both directions.
    """
    hyps = []
    for my in my_calls:
        hyps.append((my, partner))   # we call the partner / they answer us
        hyps.append((partner, my))   # they call us
    return hyps


def make_hypotheses_calling_us(my_calls):
    """
    Mode B: someone is calling us. call_to = our callsign, call_de unknown.
    Returns hypotheses with our callsign as call_to (de='CQ' placeholder
    won't work here — this mode uses a prior on call_to [0:28] only).
    """
    # this mode needs a different prior (call_to only) — handled separately
    return [(my, None) for my in my_calls]
