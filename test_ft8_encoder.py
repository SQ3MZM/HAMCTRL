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
    section("Format standardowy (bez zmian) - XX0XXX")
    d = roundtrip("CQ", "SQ3MZM", "JO72")
    check(d["i3"] == 1, "CQ SQ3MZM JO72: i3=1")
    check(d["call_to"] == "CQ" and d["call_de"] == "SQ3MZM", "CQ SQ3MZM JO72: znaki poprawne")
    check(d["report_or_grid"] == "JO72", "CQ SQ3MZM JO72: grid poprawny")

    d = roundtrip("SQ3MZM", "SP9XYZ", "-12")
    check(d["report_or_grid"] == "-12", "SQ3MZM SP9XYZ -12: raport poprawny")


def test_pr_suffix():
    section("Sufiks /P i /R (bity r1, i3=2 dla /P)")
    d = roundtrip("SP9XYZ", "SQ3MZM/P", "JO90")
    check(d["call_de"] == "SQ3MZM", "/P: base call bez sufiksu po odpakowaniu")
    check(d["r1_2"] == 1, "/P: bit r1 dla call_de ustawiony")
    check(d["i3"] == 2, "/P: i3=2 (portable)")

    d = roundtrip("SP9XYZ", "SQ3MZM/R", "JO90")
    check(d["call_de"] == "SQ3MZM", "/R: base call bez sufiksu po odpakowaniu")
    check(d["r1_2"] == 1, "/R: bit r1 dla call_de ustawiony")
    check(d["i3"] == 1, "/R: i3=1 (rover)")

    d = roundtrip("SQ3MZM/P", "SP9XYZ", "JO90")
    check(d["call_to"] == "SQ3MZM" and d["r1_1"] == 1 and d["i3"] == 2,
          "/P na call_to (nie tylko call_de) dziala tak samo")


def test_nonstandard_prefix_call():
    section("Znak zlozony z prefiksem (WX/SQ3MZM) - typ i3=4")
    d = roundtrip("CQ", "WX/SQ3MZM", "")
    check(d["i3"] == 4, "CQ WX/SQ3MZM: typ i3=4 (niestandardowy)")
    check(d["call_to"] == "CQ" and d["call_de"] == "WX/SQ3MZM",
          "CQ WX/SQ3MZM: pelny znak odtworzony poprawnie")

    # Typ i3=4 fizycznie nie ma pola na grid przy CQ - enkoder ma go
    # pominac zamiast blokowac cale wywolanie CQ.
    d = roundtrip("CQ", "WX/SQ3MZM", "JO72")
    check(d["report_or_grid"] == "", "CQ ze znakiem niestandardowym: grid ucinany (limit protokolu)")

    # Po ogloszeniu pelnego znaku (dekoder go zapamietal), dalsza wymiana
    # raportu odbywa sie przez odniesienie hashem w zwyklej wiadomosci
    # typu 1 - to jest dokladnie ta sama sciezka co prawdziwy WSJT-X.
    d = roundtrip("WX/SQ3MZM", "SP9XYZ", "-12")
    check(d["i3"] == 1, "WX/SQ3MZM SP9XYZ -12: odpowiedz z raportem = typ 1 (hash)")
    check(d["call_de"] == "SP9XYZ" and d["report_or_grid"] == "-12",
          "WX/SQ3MZM SP9XYZ -12: druga strona i raport nietkniete")
    check(d["call_to"] == "<WX/SQ3MZM>",
          "WX/SQ3MZM SP9XYZ -12: hash odwrocony (cache zasilony wczesniejszym CQ)")


def test_nonstandard_long_call():
    section("Znak za dlugi na standardowe pakowanie (SQ3MZMXX, 8 znakow)")
    d = roundtrip("CQ", "SQ3MZMXX", "")
    check(d["i3"] == 4, "CQ SQ3MZMXX: typ i3=4")
    check(d["call_de"] == "SQ3MZMXX", "CQ SQ3MZMXX: pelny (dlugi) znak odtworzony")


def test_mixed_qso_sequence():
    section("Pelna sekwencja QSO ze znakiem zlozonym (symulacja realnej lacznosci)")
    # 1) Oni woluja CQ pelnym znakiem zlozonym - my (i kazdy inny odbiornik)
    #    poznajemy ich pelny tekst i zapamietujemy w cache.
    d1 = roundtrip("CQ", "PJ4/K1ABC", "")
    check(d1["call_de"] == "PJ4/K1ABC", "Krok 1 (ich CQ): pelny znak poprawny")

    # 2) My odpowiadamy naszym gridem - ich znak jeszcze nie musi byc
    #    hashowany (my go jeszcze nie musimy powtarzac w tej wiadomosci,
    #    bo to MY jestesmy nadawca ze standardowym znakiem, call_to jest
    #    ich niestandardowym znakiem, wiec i tak trzeba hash - sprawdz).
    d2 = roundtrip("PJ4/K1ABC", "SQ3MZM", "JO72")
    check(d2["i3"] == 1, "Krok 2 (nasza odpowiedz z gridem): typ 1 (hash-reference)")
    check(d2["call_to"] == "<PJ4/K1ABC>" and d2["call_de"] == "SQ3MZM",
          "Krok 2: hash rozwiazany z cache zbudowanego w kroku 1, nasz znak jawny")

    # 3) Oni odpowiadaja raportem - ich pelny znak (nadawca) musi byc
    #    znowu opakowany hashem (typ 1, call_de=hash).
    d3 = roundtrip("SQ3MZM", "PJ4/K1ABC", "-08")
    check(d3["call_de"] == "<PJ4/K1ABC>" and d3["report_or_grid"] == "-08",
          "Krok 3 (ich raport): hash + raport poprawne")

    # 4) 73 na koniec
    d4 = roundtrip("PJ4/K1ABC", "SQ3MZM", "73")
    check(d4["report_or_grid"] == "73", "Krok 4 (73): koniec QSO poprawny")


def main():
    print("=" * 60)
    print("  TEST SUITE - FT8 encoder callsign packing (HAMCTRL)")
    print("=" * 60)

    test_standard_baseline()
    test_pr_suffix()
    test_nonstandard_prefix_call()
    test_nonstandard_long_call()
    test_mixed_qso_sequence()

    print("\n" + "=" * 60)
    total = _passed + _failed
    if _failed == 0:
        print(f"  WYNIK: {_passed}/{total} - WSZYSTKO OK")
    else:
        print(f"  WYNIK: {_passed}/{total} - {_failed} BLEDOW")
        for d in _fail_details:
            print(f"    - {d}")
    print("=" * 60)
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    main()
