# -*- coding: utf-8 -*-
"""Test suite for the CW decoder's language layer (deepcw_lang.py).

Run as a script (not pytest): python test_deepcw_lang.py
Windows: PYTHONIOENCODING=utf-8 python test_deepcw_lang.py (for Polish characters in the console).
"""
import deepcw_lang as L

passed = 0
failed = 0


def check(label, cond):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print(f"  FAIL: {label}")


print("=== _sub_cost: Morse-close characters are cheap, Morse-far ones are expensive ===")
# E "." vs T "-" -> 1 symbol difference -> cost 1 (like the old flat distance)
check("E<->T cost 1 (morse-close)", L._sub_cost("E", "T") == 1)
# O "---" vs T "-" -> 2 extra dashes -> cost 2 (previously flat=1, now costs more)
check("O<->T cost >1 (morse-far)", L._sub_cost("O", "T") > 1)
check("same char with itself cost 0", L._sub_cost("K", "K") == 0)

print("\n=== _best_match: a morse-close mistake still gets corrected ===")
# TNX: T=- N=-. X=-..-. E<->T mistake (close) -> "ENX" should match "TNX"
check("ENX -> TNX (E/T close)", L._best_match("ENX", {"TNX"}, max_dist=1) == "TNX")

print("\n=== _best_match: a morse-far mistake is NOT corrected (regression guard) ===")
# ONX: O is morse-far from T (cost 2 > max_dist=1) -> should not match
check("ONX -> None (O/T far, max_dist=1)", L._best_match("ONX", {"TNX"}, max_dist=1) is None)

print("\n=== correct(): existing behavior, no regressions ===")
check("RST passthrough 599", L.correct("TNX 599 GL") == "TNX 599 GL")
check("cut numbers 5NN -> 5NN (already a sensible report)", L.correct("UR 5NN") == "UR 5NN")
check("segmentation TKSFER -> TKS FER", L.correct("TKSFER") == "TKS FER")
check("name after NAME — example from the file header (KEITH -> KEHTHA)",
      L.correct("NAME KEHTHA") == "NAME KEITH")
check("unknown garbage passes through unchanged", L.correct("XQZVWK") == "XQZVWK")

print("\n=== correct(): known_calls — close mistake corrected, far one is not ===")
calls = {"SQ3MZM"}
# Are Q<->0 morse-close? Q=--.- O=--- are different, let's check another scenario:
# a single close letter within SQ3MZM, e.g. M<->N (-- vs -.), cost 1
res_close = L.correct("SQ3MZN", known_calls=calls)
check("SQ3MZN -> SQ3MZM (N/M close, cost 1)", res_close == "SQ3MZM")

print("\n=== correct(): extended vocabulary (Q-codes, BT prosign, new names) ===")
check("BT (prosign) passes through unchanged", "BT" in L.correct("DE SQ3MZM BT TEST").split())
check("QSK (new Q-code) recognized", "QSK" in L.correct("QSK PSE").split())
check("PIOTRA -> PIOTR (new name, close mistake)", L.correct("NAME PIOTRA") == "NAME PIOTR")

print("\n=== _segment(): a known callsign and an RST report glued together ===")
check("DESQ3MZM -> DE SQ3MZM (known callsign glued to a word)",
      L.correct("DESQ3MZM", known_calls={"SQ3MZM"}) == "DE SQ3MZM")
check("without known_calls the token is left unchanged (we don't guess)",
      L.correct("DESQ3MZM") == "DESQ3MZM")
check("5NNTU -> 5NN TU (report glued to a word)", L.correct("5NNTU") == "5NN TU")

print("\n=== _segment(): PARTIAL coverage (real-world report) ===")
# Reported case: 'TNX FER QSO 73' got glued into one string with a garbage
# tail 'TFE' stuck on (end of transmission / noise). Previously, WHOLE
# matching required full coverage -> the whole string stayed unreadable
# without spaces.
check("TNXFERQSO73TFE -> partially recovered into sensible words",
      L.correct("TNXFERQSO73TFE") == "TNX FER QSO 73 TFE")
check("mostly garbage (below 50% recognized) is NOT split",
      L.correct("XQZVWKTU") == "XQZVWKTU")

print("\n=== '44' — a POTA/SOTA greeting/sign-off, like 73/88 ===")
check("44 passes through unchanged as a separate token", L.correct("TU 44") == "TU 44")
check("GM5NN44TU -> GM 5NN 44 TU (44 recognized, not treated as an 'island')",
      L.correct("GM5NN44TU") == "GM 5NN 44 TU")

print("\n=== _reglue_split_call: a callsign broken up by false gaps ===")
# Reported case: 'CQ CQ DE HB9TWX PSE K' had the decoder split the callsign
# into individual letters ('H B 9 T WX'). Without known_calls we only glue
# it partially based on shape (we don't guess that WX is the tail of the
# callsign rather than a real word); with known_calls (the callsign seen
# e.g. on the cluster/FT8/log) we glue it back together fully.
check("without known_calls: partial glue based on shape, WX stays separate",
      L.correct("H B 9 T WX") == "HB9T WX")
check("with known_calls: full glue into the known callsign",
      L.correct("H B 9 T WX", known_calls={"HB9TWX"}) == "HB9TWX")
check("real words (CQ/DE/PSE/K) are not swallowed by the gluing",
      L.correct("CQ CQ DE H B 9 T WX PSE K", known_calls={"HB9TWX"})
      == "CQ CQ DE HB9TWX PSE K")
check("a single short real word next to a digit is not glued nonsensically",
      L.correct("TU K") == "TU K")

print(f"\n{'='*50}")
if failed == 0:
    print(f"  RESULT: {passed}/{passed} - ALL OK")
else:
    print(f"  RESULT: {passed}/{passed+failed} - {failed} FAILURES")
print("="*50)
