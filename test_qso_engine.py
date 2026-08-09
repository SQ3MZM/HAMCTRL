#!/usr/bin/env python3
"""
test_qso_engine.py — Zestaw testow maszyny stanow QSO (HAMCTRL).

Uruchomienie:
    python3 test_qso_engine.py

Cel: zlapac REGRESJE w logice QSO PRZED wgraniem na produkcje. Kazda naprawa
z sesji ma tu swoj test — jesli ktos przypadkiem cofnie poprawke, test to
wychwyci od razu, zamiast lapac blad na pasmie.

Pokrycie:
  - parse_message: wszystkie formaty (CQ, grid, raport, R-raport, RRR/RR73/73)
  - Pelne QSO oba kierunki (my wolamy / my CQ)
  - Przypadki brzegowe naprawione w sesji:
      * przedluzanie po DONE (echo 73/RR73/RRR -> cisza)
      * powtorka wolania partnera (grid w REPORT_SENT -> powtorz raport)
      * zamrozenie raportu SNR (staly przez cale QSO)
  - Kolejka (Call 1st): enqueue, pop, remove, brak duplikatow
  - Reset stanu miedzy QSO

Zwraca exit code 0 gdy wszystko OK, 1 gdy sa bledy (do CI/skryptow).
"""
import sys
import os
import importlib.util

# ── Zaladuj qso_engine.py jako modul ────────────────────────────────────────
# Szukamy qso_engine.py OBOK tego pliku (nie w katalogu biezacym), zeby test
# dzialal niezaleznie skad go uruchomisz (np. z pulpitu, nie tylko z D:\HAMCTRL).
_here = os.path.dirname(os.path.abspath(__file__))
_engine_path = os.path.join(_here, "qso_engine.py")
if not os.path.exists(_engine_path):
    print(f"BLAD: nie znaleziono qso_engine.py obok testu ({_engine_path}).")
    print("Umiesc test_qso_engine.py w tym samym katalogu co qso_engine.py.")
    sys.exit(2)
_spec = importlib.util.spec_from_file_location("qso_engine", _engine_path)
_qe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_qe)

QsoEngine = _qe.QsoEngine
parse_message = _qe.parse_message
ST_IDLE = _qe.ST_IDLE
ST_CALLING = _qe.ST_CALLING
ST_REPORT_SENT = _qe.ST_REPORT_SENT
ST_RRR_SENT = _qe.ST_RRR_SENT
ST_DONE = _qe.ST_DONE

# ── Infrastruktura testowa ──────────────────────────────────────────────────
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


def _fmt_report(snr):
    """Odtwarza _format_report z webapp.py (dla testow zamrozenia)."""
    return f"{max(-30, min(49, int(round(snr)))):+03d}"


def _dispatch(eng, msg, snr=0):
    """
    Symuluje pelna sciezke webapp._maybe_measure_and_send Z ZAMROZENIEM:
    on_decode + podstawienie/zamrozenie raportu. Zwraca (akcja, tekst_raportu).
    """
    parsed = parse_message(msg)
    if parsed is None:
        return None, None
    result = eng.on_decode(parsed)
    if result is None:
        return None, None
    if result.get("needs_measured_report"):
        result = dict(result)
        frozen = eng.partner_report_sent
        if frozen:
            result["report_or_grid"] = frozen
        else:
            result["report_or_grid"] = _fmt_report(snr)
            eng.record_sent_report(result["report_or_grid"])
    return result, result.get("report_or_grid")


# ════════════════════════════════════════════════════════════════════════════
# 1. PARSOWANIE WIADOMOSCI
# ════════════════════════════════════════════════════════════════════════════
def test_parsing():
    section("Parsowanie wiadomosci")

    p = parse_message("CQ SP9XYZ JO90")
    check(p is not None and p["is_cq"], "CQ: rozpoznane jako CQ")
    check(p["call_de"] == "SP9XYZ", "CQ: call_de = SP9XYZ")
    check(p["extra"] == "JO90", "CQ: grid = JO90")

    p = parse_message("SQ3MZM SP9XYZ JO90")
    check(p is not None and not p["is_cq"], "Grid: nie-CQ")
    check(p["call_to"] == "SQ3MZM", "Grid: call_to = SQ3MZM")
    check(p["call_de"] == "SP9XYZ", "Grid: call_de = SP9XYZ")
    check(p["extra"] == "JO90", "Grid: extra = JO90")

    p = parse_message("SQ3MZM SP9XYZ -12")
    check(p is not None and p["report"] == "-12", "Raport: -12 rozpoznany")
    check(not p["is_rrr"] and not p["is_73"], "Raport: nie RRR/73")

    p = parse_message("SQ3MZM SP9XYZ R-08")
    check(p is not None and p["report"] == "R-08", "R-raport: R-08 rozpoznany")

    p = parse_message("SQ3MZM SP9XYZ RRR")
    check(p is not None and p["is_rrr"], "RRR rozpoznane")

    p = parse_message("SQ3MZM SP9XYZ RR73")
    check(p is not None and p["is_rr73"], "RR73 rozpoznane")

    p = parse_message("SQ3MZM SP9XYZ 73")
    check(p is not None and p["is_73"], "73 rozpoznane")

    check(parse_message("") is None, "Pusta wiadomosc -> None")
    check(parse_message("XYZ") is None, "Pojedynczy token -> None")


# ════════════════════════════════════════════════════════════════════════════
# 2. PELNE QSO — MY ODPOWIADAMY NA CQ
# ════════════════════════════════════════════════════════════════════════════
def test_full_qso_we_answer():
    section("Pelne QSO: my odpowiadamy na CQ SP9XYZ")
    eng = QsoEngine("SQ3MZM", "JO82")
    eng.start_qso("SP9XYZ", parse_message("CQ SP9XYZ JO90"))
    check(eng.state == ST_CALLING, "Po start_qso: CALLING")

    # Partner daje nam raport -12; slyszymy go jako -08 -> nasz raport zamrozi na -08
    act, rpt = _dispatch(eng, "SQ3MZM SP9XYZ -12", snr=-8)
    check(act is not None, "Raport partnera: jest akcja (nie cisza)")
    check(rpt == "-08", "Nasz raport zamrozony na -08 (nasz pomiar)")
    check(eng.state == ST_REPORT_SENT, "Po raporcie: REPORT_SENT")

    # Partner RR73 -> my 73, DONE
    act, rpt = _dispatch(eng, "SQ3MZM SP9XYZ RR73", snr=-8)
    check(act is not None and rpt == "73", "RR73 partnera -> nasze 73")
    check(act.get("qso_complete"), "QSO oznaczone jako complete")
    check(eng.state == ST_DONE, "Po RR73: DONE")


# ════════════════════════════════════════════════════════════════════════════
# 3. PELNE QSO — MY NADAJEMY CQ, PARTNER ODPOWIADA
# ════════════════════════════════════════════════════════════════════════════
def test_full_qso_we_cq():
    section("Pelne QSO: my CQ, SP9XYZ odpowiada")
    eng = QsoEngine("SQ3MZM", "JO82")
    # Partner odpowiada na nasze CQ swoim gridem -> start_qso zwraca akcje raport
    act = eng.start_qso("SP9XYZ", parse_message("SQ3MZM SP9XYZ JO90"))
    check(act is not None and act.get("needs_measured_report"),
          "start_qso z gridem -> akcja raport (needs_measured)")
    check(eng.state == ST_REPORT_SENT, "Po start z gridem: REPORT_SENT")

    # Partner R-08 -> my RRR
    act, rpt = _dispatch(eng, "SQ3MZM SP9XYZ R-08", snr=-8)
    check(act is not None and rpt == "RRR", "R-08 partnera -> nasze RRR")
    check(eng.state == ST_RRR_SENT, "Po R-raporcie: RRR_SENT")

    # Partner 73 -> my 73, DONE
    act, rpt = _dispatch(eng, "SQ3MZM SP9XYZ 73", snr=-8)
    check(act is not None and rpt == "73", "73 partnera -> nasze 73")
    check(eng.state == ST_DONE, "Po 73: DONE")


# ════════════════════════════════════════════════════════════════════════════
# 4. REGRESJA: przedluzanie po DONE (naprawa sesji)
# ════════════════════════════════════════════════════════════════════════════
def test_no_extending_after_done():
    section("Regresja: brak przedluzania QSO po DONE")
    eng = QsoEngine("SQ3MZM", "JO82")
    eng.start_qso("SP9XYZ", parse_message("CQ SP9XYZ JO90"))
    _dispatch(eng, "SQ3MZM SP9XYZ -12", snr=-8)
    _dispatch(eng, "SQ3MZM SP9XYZ RR73", snr=-8)  # -> DONE
    check(eng.state == ST_DONE, "Osiagnieto DONE")

    # Echo 73 po DONE -> CISZA (nie nadawaj 73 w kolko)
    act, _ = _dispatch(eng, "SQ3MZM SP9XYZ 73", snr=-8)
    check(act is None, "Echo 73 po DONE -> cisza")

    # Echo RR73 po DONE -> CISZA
    act, _ = _dispatch(eng, "SQ3MZM SP9XYZ RR73", snr=-8)
    check(act is None, "Echo RR73 po DONE -> cisza")

    # Echo RRR po DONE -> CISZA
    act, _ = _dispatch(eng, "SQ3MZM SP9XYZ RRR", snr=-8)
    check(act is None, "Echo RRR po DONE -> cisza")


# ════════════════════════════════════════════════════════════════════════════
# 5. REGRESJA: powtorka wolania partnera (naprawa sesji)
# ════════════════════════════════════════════════════════════════════════════
def test_repeated_call():
    section("Regresja: powtorka wolania partnera (automat nie moze milczec)")
    eng = QsoEngine("SQ3MZM", "JO82")
    # Partner wola nas gridem (Call 1st)
    act = eng.start_qso("SP9XYZ", parse_message("SQ3MZM SP9XYZ JO90"))
    check(act is not None, "1. wolanie -> akcja raport")
    # zamroz raport
    if act.get("needs_measured_report"):
        eng.record_sent_report(_fmt_report(3))
    check(eng.state == ST_REPORT_SENT, "Po 1. wolaniu: REPORT_SENT")

    # Partner POWTARZA grid (nie uslyszal) -> musimy POWTORZYC raport (nie cisza)
    act, rpt = _dispatch(eng, "SQ3MZM SP9XYZ JO90", snr=3)
    check(act is not None, "Powtorka wolania -> akcja (NIE cisza)")
    check(rpt == "+03", "Powtorka -> ten sam zamrozony raport +03")

    # I jeszcze raz
    act, rpt = _dispatch(eng, "SQ3MZM SP9XYZ JO90", snr=3)
    check(act is not None and rpt == "+03", "Druga powtorka -> nadal +03")

    # Partner w koncu uslyszal -> R-raport -> my RRR (QSO idzie dalej)
    act, rpt = _dispatch(eng, "SQ3MZM SP9XYZ R+03", snr=3)
    check(act is not None and rpt == "RRR", "Po R-raporcie -> RRR (QSO postepuje)")


# ════════════════════════════════════════════════════════════════════════════
# 6. REGRESJA: zamrozenie raportu SNR (naprawa sesji)
# ════════════════════════════════════════════════════════════════════════════
def test_frozen_report():
    section("Regresja: raport SNR zamrozony przez cale QSO")
    eng = QsoEngine("SQ3MZM", "JO82")
    eng.start_qso("SP9XYZ", parse_message("CQ SP9XYZ JO90"))

    # 1. raport partnera, slyszymy -10 -> zamrozi na -10
    _, r1 = _dispatch(eng, "SQ3MZM SP9XYZ -12", snr=-10)
    check(r1 == "-10", "1. raport zamrozony na -10")

    # SNR partnera SKACZE (-3), ale raport ma zostac -10
    _, r2 = _dispatch(eng, "SQ3MZM SP9XYZ -12", snr=-3)
    check(r2 == "-10", "SNR skoczyl na -3, raport WCIAZ -10 (zamrozony)")

    # SNR znowu inny (-18), raport nadal -10
    _, r3 = _dispatch(eng, "SQ3MZM SP9XYZ -12", snr=-18)
    check(r3 == "-10", "SNR -18, raport WCIAZ -10")

    # partner_report_sent (uzywany przez log) = ta sama wartosc
    check(eng.partner_report_sent == "-10", "Log (partner_report_sent) = -10, spojne")

    # Nowe QSO liczy SWIEZY raport (reset)
    eng.abort_qso()
    eng.start_qso("DL1ABC", parse_message("CQ DL1ABC JN40"))
    _, rn = _dispatch(eng, "SQ3MZM DL1ABC -05", snr=-7)
    check(rn == "-07", "Nowe QSO -> swiezy raport -07 (nie -10)")


# ════════════════════════════════════════════════════════════════════════════
# 7. RAPORT PARTNERA (RST_RCVD) — ostatnia wartosc (zgodne z WSJT-X)
# ════════════════════════════════════════════════════════════════════════════
def test_partner_report_last_value():
    section("Raport partnera (RST_RCVD) = ostatnia jego wartosc")
    eng = QsoEngine("SQ3MZM", "JO82")
    eng.start_qso("SP9XYZ", parse_message("CQ SP9XYZ JO90"))
    _dispatch(eng, "SQ3MZM SP9XYZ -12", snr=-8)
    check(eng.partner_report_recv == "-12", "Partner dal -12 -> zapisane")
    # Partner koryguje na -05
    _dispatch(eng, "SQ3MZM SP9XYZ -05", snr=-8)
    check(eng.partner_report_recv == "-05", "Partner skorygowal -> -05 (ostatnia)")


# ════════════════════════════════════════════════════════════════════════════
# 8. KOLEJKA (Call 1st): enqueue / pop / remove / brak duplikatow
# ════════════════════════════════════════════════════════════════════════════
def test_queue():
    section("Kolejka Call 1st")
    eng = QsoEngine("SQ3MZM", "JO82")
    eng.enqueue_caller("SP1AAA")
    eng.enqueue_caller("SP2BBB")
    check(list(eng.queue) == ["SP1AAA", "SP2BBB"], "Enqueue: kolejnosc FIFO")

    eng.enqueue_caller("SP1AAA")  # duplikat
    check(eng.queue.count("SP1AAA") == 1, "Brak duplikatow w kolejce")

    nxt = eng.pop_next_from_queue()
    check(nxt == "SP1AAA", "Pop: FIFO (najpierw SP1AAA)")

    ok = eng.remove_from_queue("SP2BBB")
    check(ok and "SP2BBB" not in eng.queue, "Remove: usuwa z kolejki")

    eng.enqueue_caller("SP3CCC")
    eng.enqueue_caller("SP4DDD")
    eng.clear_queue()
    check(eng.queue == [], "Clear: oproznia cala kolejke")
    eng.enqueue_caller("SP3CCC")  # po clear stacja moze wrocic (nie jest "widziana")
    check(eng.queue == ["SP3CCC"], "Clear: resetuje tez dedup (_queue_seen)")


# ════════════════════════════════════════════════════════════════════════════
# 9. RESET STANU miedzy QSO
# ════════════════════════════════════════════════════════════════════════════
def test_reset_between_qso():
    section("Reset stanu miedzy QSO")
    eng = QsoEngine("SQ3MZM", "JO82")
    eng.start_qso("SP9XYZ", parse_message("CQ SP9XYZ JO90"))
    _dispatch(eng, "SQ3MZM SP9XYZ -12", snr=-8)
    eng.abort_qso()
    check(eng.state == ST_IDLE, "Po abort: IDLE")
    check(eng.partner_call is None, "Po abort: partner_call = None")
    check(eng.partner_report_sent is None, "Po abort: raport zresetowany")


def main():
    print("╔══════════════════════════════════════════════════════╗")
    print("║  TEST SUITE — Maszyna stanow QSO (HAMCTRL)            ║")
    print("╚══════════════════════════════════════════════════════╝")

    test_parsing()
    test_full_qso_we_answer()
    test_full_qso_we_cq()
    test_no_extending_after_done()
    test_repeated_call()
    test_frozen_report()
    test_partner_report_last_value()
    test_queue()
    test_reset_between_qso()

    print("\n" + "═" * 56)
    total = _passed + _failed
    if _failed == 0:
        print(f"  WYNIK: {_passed}/{total} — WSZYSTKO OK ✓")
        print("  Maszyna stanow QSO dziala poprawnie. Mozna wgrywac.")
    else:
        print(f"  WYNIK: {_passed}/{total} — {_failed} BLEDOW ✗")
        print("  NIE WGRYWAJ — napraw bledy najpierw:")
        for d in _fail_details:
            print(f"    - {d}")
    print("═" * 56)
    return 0 if _failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
