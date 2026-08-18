#!/usr/bin/env python3
"""
test_ft8_encoder.py - Round-trip tests for ft8_encoder.py's callsign
packing (pack77 / pack77_nonstandard), verified against unpack.py.

Run: python test_ft8_encoder.py
Requires PYTHONIOENCODING=utf-8 on Windows consoles (Polish characters in
check() labels).
"""
import sys

import ft8_encoder as fe
import unpack as up

_passed = 0
_failed = 0
_fail_details = []


def check(cond, name):
    global _passed, _failed
    if cond:
        _passed += 1
    else:
        _failed += 1
        _fail_details.append(name)
        print(f"  FAIL: {name}")


def section(title):
    print(f"\n=== {title} ===")


def roundtrip(call_to, call_de, report_or_grid, r_flag=False):
    """Pack a message and unpack it back, asserting the bit count is exact."""
    bits = fe.pack77(call_to, call_de, report_or_grid, r_flag)
    assert len(bits) == 77
    return up.unpack77(bits)


def test_standard_baseline():
    section("Standard format (no changes) - XX0XXX")
    d = roundtrip("CQ", "SQ3MZM", "JO72")
    check(d["i3"] == 1, "CQ SQ3MZM JO72: i3=1")
    check(d["call_to"] == "CQ" and d["call_de"] == "SQ3MZM", "CQ SQ3MZM JO72: callsigns correct")
    check(d["report_or_grid"] == "JO72", "CQ SQ3MZM JO72: grid correct")

    d = roundtrip("SQ3MZM", "SP9XYZ", "-12")
    check(d["report_or_grid"] == "-12", "SQ3MZM SP9XYZ -12: report correct")


def test_pr_suffix():
    section("/P and /R suffixes (r1 bits, i3=2 for /P)")
    d = roundtrip("SP9XYZ", "SQ3MZM/P", "JO90")
    check(d["call_de"] == "SQ3MZM", "/P: base call without suffix after unpacking")
    check(d["r1_2"] == 1, "/P: r1 bit set for call_de")
    check(d["i3"] == 2, "/P: i3=2 (portable)")

    d = roundtrip("SP9XYZ", "SQ3MZM/R", "JO90")
    check(d["call_de"] == "SQ3MZM", "/R: base call without suffix after unpacking")
    check(d["r1_2"] == 1, "/R: r1 bit set for call_de")
    check(d["i3"] == 1, "/R: i3=1 (rover)")

    d = roundtrip("SQ3MZM/P", "SP9XYZ", "JO90")
    check(d["call_to"] == "SQ3MZM" and d["r1_1"] == 1 and d["i3"] == 2,
          "/P on call_to (not just call_de) works the same way")


def test_swaziland_guinea_workarounds():
    section("Historical pack28 exceptions (3DA0 Eswatini, 3X Guinea)")
    # A one-way substitution from the official FT8 protocol spec - thanks
    # to it the callsign packs as standard (type 1, with grid) instead of
    # falling into the slower non-standard path (type 4, no grid). Not
    # reversing it on receive isn't our bug - that's exactly how the
    # protocol works: "3DA0RS" is received as "3D0RS" by EVERY station.
    d = roundtrip("CQ", "3DA0RS", "JO72")
    check(d["i3"] == 1, "3DA0RS: packs as standard type 1 (not hash/type 4)")
    check(d["call_de"] == "3D0RS", "3DA0RS: 3DA0->3D0 substitution matches the protocol")
    check(d["report_or_grid"] == "JO72", "3DA0RS: grid carried through (type 1 allows it)")

    d = roundtrip("CQ", "3XY1AB", "JO72")
    check(d["i3"] == 1, "3XY1AB: packs as standard type 1")
    check(d["call_de"] == "QY1AB", "3XY1AB: 3X->Q substitution matches the protocol")

    # 3X + a digit (not a letter) at the 3rd position already fits the
    # standard format - the substitution should NOT trigger here.
    d = roundtrip("CQ", "3X2CD", "JO72")
    check(d["call_de"] == "3X2CD", "3X2CD: unmodified, already standard format")


def test_nonstandard_prefix_call():
    section("Compound callsign with a prefix (WX/SQ3MZM) - type i3=4")
    d = roundtrip("CQ", "WX/SQ3MZM", "")
    check(d["i3"] == 4, "CQ WX/SQ3MZM: type i3=4 (non-standard)")
    check(d["call_to"] == "CQ" and d["call_de"] == "WX/SQ3MZM",
          "CQ WX/SQ3MZM: full callsign reconstructed correctly")

    # Type i3=4 has no grid field at all for CQ - the encoder should drop
    # it instead of blocking the whole CQ call.
    d = roundtrip("CQ", "WX/SQ3MZM", "JO72")
    check(d["report_or_grid"] == "", "CQ with a non-standard callsign: grid dropped (protocol limit)")

    # After the full callsign has been announced (the decoder remembered
    # it), further report exchange happens via a hash reference in a
    # regular type-1 message - this is exactly the path described in the
    # protocol spec.
    d = roundtrip("WX/SQ3MZM", "SP9XYZ", "-12")
    check(d["i3"] == 1, "WX/SQ3MZM SP9XYZ -12: reply with report = type 1 (hash)")
    check(d["call_de"] == "SP9XYZ" and d["report_or_grid"] == "-12",
          "WX/SQ3MZM SP9XYZ -12: the other side and the report are untouched")
    check(d["call_to"] == "<WX/SQ3MZM>",
          "WX/SQ3MZM SP9XYZ -12: hash resolved (cache seeded by the earlier CQ)")


def test_nonstandard_long_call():
    section("Callsign too long for standard packing (SQ3MZMXX, 8 characters)")
    d = roundtrip("CQ", "SQ3MZMXX", "")
    check(d["i3"] == 4, "CQ SQ3MZMXX: type i3=4")
    check(d["call_de"] == "SQ3MZMXX", "CQ SQ3MZMXX: full (long) callsign reconstructed")


def test_mixed_qso_sequence():
    section("Full QSO sequence with a compound callsign (simulating a real contact)")
    # 1) They call CQ with the full compound callsign - we (and every other
    #    receiver) learn their full text and cache it.
    d1 = roundtrip("CQ", "PJ4/K1ABC", "")
    check(d1["call_de"] == "PJ4/K1ABC", "Step 1 (their CQ): full callsign correct")

    # 2) We reply with our grid - their callsign doesn't need to be hashed
    #    yet (we don't need to repeat it in this message; we're the sender
    #    with a standard callsign, call_to is their non-standard callsign,
    #    so it still needs a hash - check it).
    d2 = roundtrip("PJ4/K1ABC", "SQ3MZM", "JO72")
    check(d2["i3"] == 1, "Step 2 (our reply with grid): type 1 (hash-reference)")
    check(d2["call_to"] == "<PJ4/K1ABC>" and d2["call_de"] == "SQ3MZM",
          "Step 2: hash resolved from the cache built in step 1, our callsign in the clear")

    # 3) They reply with a report - their full callsign (the sender) must
    #    be wrapped in a hash again (type 1, call_de=hash).
    d3 = roundtrip("SQ3MZM", "PJ4/K1ABC", "-08")
    check(d3["call_de"] == "<PJ4/K1ABC>" and d3["report_or_grid"] == "-08",
          "Step 3 (their report): hash + report correct")

    # 4) 73 to finish
    d4 = roundtrip("PJ4/K1ABC", "SQ3MZM", "73")
    check(d4["report_or_grid"] == "73", "Step 4 (73): QSO ending correct")


def main():
    print("=" * 60)
    print("  TEST SUITE - FT8 encoder callsign packing (HAMCTRL)")
    print("=" * 60)

    test_standard_baseline()
    test_pr_suffix()
    test_swaziland_guinea_workarounds()
    test_nonstandard_prefix_call()
    test_nonstandard_long_call()
    test_mixed_qso_sequence()

    print("\n" + "=" * 60)
    total = _passed + _failed
    if _failed == 0:
        print(f"  RESULT: {_passed}/{total} - ALL OK")
    else:
        print(f"  RESULT: {_passed}/{total} - {_failed} FAILURES")
        for d in _fail_details:
            print(f"    - {d}")
    print("=" * 60)
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    main()
