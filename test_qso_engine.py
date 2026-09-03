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
  - Brak kolejki Call 1st: obca stacja w trakcie zajetego QSO jest ignorowana
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
is_cq_modifier = _qe.is_cq_modifier
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

    # CQ z modifierem programu aktywacyjnego (2026-08-15: front i backend
    # traktowaly modifier jako call_de, bo "SOTA"/"POTA" (4 znaki) nie
    # laczyly sie z heurystyka len<=3 - naprawiono do len<=6, tu blokujemy
    # regresje na kilku z calej rodziny "*OTA", nie tylko na tych dwoch co
    # akurat byly na sztywnej whiteliscie.
    p = parse_message("CQ SOTA 5B4AMX KM65")
    check(p is not None and p["call_de"] == "5B4AMX", "CQ SOTA: call_de = 5B4AMX (nie 'SOTA')")
    check(p["cq_modifier"] == "SOTA", "CQ SOTA: modifier rozpoznany")

    p = parse_message("CQ POTA W1XYZ FN42")
    check(p is not None and p["call_de"] == "W1XYZ", "CQ POTA: call_de = W1XYZ (nie 'POTA')")

    p = parse_message("CQ BOTA DL1ABC JO62")
    check(p is not None and p["call_de"] == "DL1ABC",
          "CQ BOTA (nie na whiteliscie): call_de = DL1ABC (nie 'BOTA') - fallback len<=6")
    check(p["cq_modifier"] == "BOTA", "CQ BOTA: modifier rozpoznany przez fallback")

    check(is_cq_modifier("SOTA"), "is_cq_modifier: SOTA (whitelist)")
    check(is_cq_modifier("BOTA"), "is_cq_modifier: BOTA (fallback <=6 liter)")
    check(is_cq_modifier("GOTA"), "is_cq_modifier: GOTA (fallback <=6 liter)")
    check(not is_cq_modifier("SP3GSK"), "is_cq_modifier: SP3GSK (ma cyfre) -> NIE modifier")
    check(not is_cq_modifier("K7RA"), "is_cq_modifier: K7RA (ma cyfre) -> NIE modifier")


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

    # Partner R-08 -> od razu nasze RR73, QSO zakonczone (skracamy o jeden
    # cykl - nie czekamy juz na osobne 73 partnera, patrz test nizej)
    act, rpt = _dispatch(eng, "SQ3MZM SP9XYZ R-08", snr=-8)
    check(act is not None and rpt == "RR73", "R-08 partnera -> nasze RR73 (bez osobnego RRR)")
    check(act.get("qso_complete"), "QSO oznaczone jako complete od razu po RR73")
    check(eng.state == ST_DONE, "Po R-raporcie: od razu DONE (nie RRR_SENT)")


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


def test_no_loop_after_abort_on_partner_echo():
    section("Regresja: brak petli 73<->73 po abort_qso() (zgloszenie live 2026-08-26)")
    # Odtwarza DOKLADNIE sciezke webapp.py: po qso_complete=True webapp.py
    # woła engine.abort_qso() natychmiast (zeby zwolnic partner_call/queue
    # dla nastepnej stacji), ZANIM ewentualne echo 73 od korespondenta
    # (typowe na FT8 - stacja grzecznie powtarza 73) zdazy dotrzec. Bez
    # poprawki: skoro partner_call=None po abort_qso(), silnik traktowal
    # to echo jak NOWA stacja wolajaca -> start_qso -> odpowiadal WLASNYM
    # 73 -> qso_complete=True ZNOWU -> webapp.py loguje duplikat i znow
    # abort_qso() -> petla bez konca na kazdym kolejnym "73" korespondenta.
    eng = QsoEngine("SQ3MZM", "JO82")
    eng.start_qso("SP9XYZ", parse_message("CQ SP9XYZ JO90"))
    _dispatch(eng, "SQ3MZM SP9XYZ -12", snr=-8)
    act, _ = _dispatch(eng, "SQ3MZM SP9XYZ RR73", snr=-8)  # -> DONE, my 73
    check(act is not None and act.get("qso_complete"), "RR73 -> nasze 73, qso_complete")

    # webapp.py: QSO zalogowane, natychmiastowy abort_qso() (jak w _process_auto_qso)
    eng.abort_qso()
    check(eng.state == ST_IDLE, "Po abort_qso(): IDLE")
    check(eng.partner_call is None, "Po abort_qso(): brak partnera")

    # Korespondent grzecznie powtarza 73 (echo, nie nowe wolanie) - dociera
    # PO abort_qso(), zanim jakakolwiek nowa stacja zdazyla sie zglosic.
    result = eng.on_decode(parse_message("SQ3MZM SP9XYZ 73"))
    check(result is None, "Echo 73 po abort_qso() -> cisza (NIE nowe QSO)")
    check(eng.state == ST_IDLE, "Stan pozostaje IDLE (nie CALLING/DONE)")
    check(eng.partner_call is None, "Partner nadal None (nie wystartowalo nowe QSO)")

    # To samo dla RR73 i RRR jako "echo po abort" - zaden nie powinien
    # startowac nowego QSO.
    result = eng.on_decode(parse_message("SQ3MZM SP9XYZ RR73"))
    check(result is None, "Echo RR73 po abort_qso() -> cisza")

    result = eng.on_decode(parse_message("SQ3MZM SP9XYZ RRR"))
    check(result is None, "Echo RRR po abort_qso() -> cisza")


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

    # Partner w koncu uslyszal -> R-raport -> od razu nasze RR73, koniec QSO
    act, rpt = _dispatch(eng, "SQ3MZM SP9XYZ R+03", snr=3)
    check(act is not None and rpt == "RR73", "Po R-raporcie -> RR73 (QSO zakonczone)")
    check(eng.state == ST_DONE, "Po R-raporcie: DONE")


# ════════════════════════════════════════════════════════════════════════════
# REGRESJA: partner powtarza w kolko (grid LUB surowy raport), nigdy nie
# potwierdza -> automat poddaje sie po MAX_REPORT_REPEATS, nie powtarza
# raportu w nieskonczonosc (2026-09-03). Live report: "automat nie
# powtarzal w kolko raportu do klienta jesli powtorzylismy 3 razy i nam
# nie potwierdzil powinien nastapic stop automatycznie". Przed poprawka
# retry_count (uzywany przez CISZE - brak jakiejkolwiek odpowiedzi) resetowal
# sie na KAZDA wiadomosc od partnera, WLACZNIE z bezuzytecznymi powtorkami
# - wiec automat mogl powtarzac swoj raport w nieskonczonosc, dopoki partner
# cokolwiek nadawal, nawet jesli nigdy nie potwierdzal.
# ════════════════════════════════════════════════════════════════════════════
def test_give_up_after_stuck_report_repeats_grid():
    section("Regresja: partner w kolko powtarza GRID, nigdy nie potwierdza -> stop po 3 probach")
    eng = QsoEngine("SQ3MZM", "JO82")
    act = eng.start_qso("SP9XYZ", parse_message("SQ3MZM SP9XYZ JO90"))
    check(act is not None, "1. wolanie -> akcja raport")
    if act.get("needs_measured_report"):
        eng.record_sent_report(_fmt_report(3))
    check(eng.state == ST_REPORT_SENT, "Po 1. wolaniu: REPORT_SENT")

    # 3 powtorki grida partnera, nigdy R-raportu -> 1., 2., 3. powtorka nadal
    # odpowiadamy raportem (repeat_count 1,2,3 wciaz < 3 w momencie sprawdzenia)
    for i in range(3):
        act, rpt = _dispatch(eng, "SQ3MZM SP9XYZ JO90", snr=3)
        check(act is not None and act.get("action") == "reply" and rpt == "+03",
              f"Powtorka {i+1}/3 grida -> nadal odpowiadamy +03")
        check(eng.state == ST_REPORT_SENT, f"Po powtorce {i+1}: nadal REPORT_SENT")

    # 4. powtorka (3 realne powtorki juz wykorzystane) -> STOP, nie kolejny raport
    act = eng.on_decode(parse_message("SQ3MZM SP9XYZ JO90"))
    check(act is not None and act.get("action") == "give_up",
          "4. powtorka grida (po 3 nieudanych probach) -> give_up, nie kolejny raport")
    check(act.get("call_de") == "SP9XYZ", "give_up zawiera porzucanego partnera")
    check(eng.state == ST_IDLE, "Po give_up: IDLE (wolni dla nastepnej stacji)")
    check(eng.partner_call is None, "Po give_up: brak partnera (abort_qso wykonany)")


def test_give_up_after_stuck_report_repeats_raw_report():
    section("Regresja: partner w kolko powtarza SUROWY raport (nie R+), nigdy nie potwierdza -> stop po 3 probach")
    eng = QsoEngine("SQ3MZM", "JO82")
    eng.start_qso("SP9XYZ", parse_message("CQ SP9XYZ JO90"))
    # Pierwszy surowy raport partnera -> nasz raport z r_flag=True (R+raport
    # w eterze - "R" to osobny bit kodowany przez r_flag, nie prefiks w
    # samym tekscie report_or_grid, patrz test_full_qso_we_answer wyzej)
    act, rpt = _dispatch(eng, "SQ3MZM SP9XYZ -12", snr=-8)
    check(act is not None and act.get("r_flag") is True and rpt == "-08",
          "1. surowy raport partnera -> nasz raport -08 z r_flag=True")
    check(eng.state == ST_REPORT_SENT, "Po 1. raporcie: REPORT_SENT")

    # Partner nie uslyszal naszego R+raportu i powtarza SWOJ surowy raport
    # 3 razy -> wciaz odpowiadamy (repeat_count 1,2,3)
    for i in range(3):
        act, rpt = _dispatch(eng, "SQ3MZM SP9XYZ -12", snr=-8)
        check(act is not None and act.get("action") == "reply",
              f"Powtorka {i+1}/3 surowego raportu -> nadal odpowiadamy")
        check(eng.state == ST_REPORT_SENT, f"Po powtorce {i+1}: nadal REPORT_SENT")

    # 4. powtorka -> STOP
    act = eng.on_decode(parse_message("SQ3MZM SP9XYZ -12"))
    check(act is not None and act.get("action") == "give_up",
          "4. powtorka surowego raportu (po 3 probach) -> give_up")
    check(eng.state == ST_IDLE, "Po give_up: IDLE")
    check(eng.partner_call is None, "Po give_up: brak partnera")


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
# 7. RAPORT PARTNERA (RST_RCVD) — ostatnia wartosc
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
# 8. BRAK KOLEJKI Call 1st (usunieta 2026-08-26, live feedback: "kolejka nie
#    ma sensu przy 4 stacjach w kolejce nikt nie bedzie czekal 4 minut")
# ════════════════════════════════════════════════════════════════════════════
def test_no_queue_busy_caller_ignored():
    section("Brak kolejki: stacja wolajaca gdy jestesmy zajeci -> ignorowana")
    eng = QsoEngine("SQ3MZM", "JO82")
    eng.start_qso("SP9XYZ", parse_message("CQ SP9XYZ JO90"))
    check(eng.state == ST_CALLING, "Zajeci: QSO z SP9XYZ w toku")

    # Inna stacja odpowiada na nasze CQ, podczas gdy jestesmy zajeci SP9XYZ.
    result = eng.on_decode(parse_message("SQ3MZM SP1AAA JO70"))
    check(result is None, "Obca stacja w trakcie zajetego QSO -> cisza (brak kolejki)")
    check(eng.partner_call == "SP9XYZ", "Partner sie nie zmienil")

    # Konczymy QSO z SP9XYZ.
    _dispatch(eng, "SQ3MZM SP9XYZ -12", snr=-8)
    _dispatch(eng, "SQ3MZM SP9XYZ RR73", snr=-8)
    eng.abort_qso()
    check(eng.state == ST_IDLE, "Wolni po zakonczeniu QSO")

    # SP1AAA (ktora wolala nas w trakcie zajetosci) NIE zostala zapamietana
    # - dopiero SWIEZY dekod od niej (a nie ten sprzed chwili) dostaje odpowiedz.
    result = eng.on_decode(parse_message("SQ3MZM SP1AAA JO70"))
    check(result is not None and result.get("action") == "new_caller",
          "SP1AAA wola PONOWNIE gdy jestesmy wolni -> natychmiastowa odpowiedz")
    check(result.get("call_de") == "SP1AAA", "new_caller: poprawny call_de")


# ════════════════════════════════════════════════════════════════════════════
# 8b. first_contact_at: logowany czas QSO to PIERWSZA odpowiedz partnera,
#     nie moment kiedy MY zaczelismy go wolac (naprawa live 2026-08-26:
#     "wolalem RI1FJL cale QSO trwa 5min? tyle ile wolalem")
# ════════════════════════════════════════════════════════════════════════════
def test_first_contact_at_not_calling_start():
    section("first_contact_at = pierwsza odpowiedz partnera, nie poczatek wolania")
    import time as _time

    # Scenariusz A: MY wolamy (klik na CQ), partner odpowiada DOPIERO PO
    # KILKU probach (retransmisjach) - typowe przy slabym sygnale/pileupie.
    eng = QsoEngine("SQ3MZM", "JO82")
    eng.start_qso("RI1FJL")  # bez initial_decode -> stan CALLING, Tx1 do wyslania
    check(eng.state == ST_CALLING, "Po start_qso bez initial_decode: CALLING")
    check(eng.first_contact_at is None,
          "Zanim partner odpowie: first_contact_at = None (jeszcze nie wiadomo)")
    check(eng.started_at is not None, "started_at ustawiony od razu (kiedy MY zaczelismy wolac)")

    _time.sleep(0.05)  # symuluje kilka okresow retransmisji bez odpowiedzi
    _calling_duration_before_reply = _time.time() - eng.started_at

    # Partner W KONCU odpowiada (dopiero teraz, po "kilku minutach" wolania)
    act, _ = _dispatch(eng, "SQ3MZM RI1FJL JO40", snr=-10)
    check(act is not None, "Partner odpowiedzial gridem -> akcja")
    check(eng.first_contact_at is not None, "Po odpowiedzi: first_contact_at ustawiony")
    check(eng.first_contact_at > eng.started_at,
          "first_contact_at PO started_at (odpowiedz przyszla PO rozpoczeciu wolania) - "
          "to jest CEL tej poprawki, nie powinny byc rowne przy realnym opoznieniu")
    check((eng.first_contact_at - eng.started_at) >= _calling_duration_before_reply * 0.9,
          "Odstep first_contact_at - started_at odzwierciedla realny czas wolania bez odpowiedzi")

    # first_contact_at NIE zmienia sie na kolejnych wiadomosciach od partnera
    _first = eng.first_contact_at
    _dispatch(eng, "SQ3MZM RI1FJL R-05", snr=-10)
    check(eng.first_contact_at == _first,
          "first_contact_at zamrozony na PIERWSZYM kontakcie, kolejne wiadomosci go nie zmieniaja")

    # Scenariusz B: partner JUZ odpowiedzial (initial_decode) - first_contact_at
    # i started_at powinny byc (prawie) rownoczesne, bo nie bylo fazy "samego wolania".
    eng2 = QsoEngine("SQ3MZM", "JO82")
    eng2.start_qso("RI1FJL", initial_decode=parse_message("SQ3MZM RI1FJL JO40"))
    check(eng2.first_contact_at is not None,
          "start_qso z initial_decode -> first_contact_at ustawiony od razu")
    check(abs(eng2.first_contact_at - eng2.started_at) < 0.01,
          "Bez fazy wolania: first_contact_at ~= started_at (ta sama chwila)")

    # Reset miedzy QSO: nowe QSO nie dziedziczy first_contact_at z poprzedniego
    eng2.abort_qso()
    check(eng2.first_contact_at is None, "Po abort_qso: first_contact_at wyzerowany")
    eng2.start_qso("DL1ABC")
    check(eng2.first_contact_at is None,
          "Nowe QSO (bez initial_decode): first_contact_at znowu None, nie zostaje ze starego")


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


# ════════════════════════════════════════════════════════════════════════════
# 10. RETRANSMISJA / GIVE-UP — partner przestaje odpowiadac w trakcie QSO
# ════════════════════════════════════════════════════════════════════════════
def test_retransmit_and_giveup():
    section("Retransmisja ostatniej wiadomosci i porzucenie po limicie prob")
    eng = QsoEngine("SQ3MZM", "JO82")

    check(eng.should_retransmit(30.0) is False, "IDLE: nigdy nie trzeba retransmitowac")

    eng.start_qso("HB9CNU", parse_message("SQ3MZM HB9CNU JN37"))
    check(eng.state == ST_REPORT_SENT, "Po starcie z gridem partnera: REPORT_SENT")
    check(eng.should_retransmit(30.0) is False, "Przed pierwsza transmisja: nic do powtorzenia")

    eng.record_tx_sent()
    check(eng.should_retransmit(30.0) is False, "Zaraz po wyslaniu: za wczesnie na powtorke")

    eng.last_tx_at -= 31.0  # symuluj 31s bez odpowiedzi partnera
    check(eng.should_retransmit(30.0) is True, "Po pelnym okresie ciszy: pora powtorzyc")

    check(eng.should_give_up(4) is False, "retry_count=0: jeszcze nie poddajemy sie")
    for expected in (1, 2, 3, 4):
        eng.note_retry()
        check(eng.retry_count == expected, f"note_retry: licznik = {expected}")
    check(eng.should_give_up(4) is True, "Po 4 probach (limit=4): poddajemy sie")

    # Odpowiedz partnera zeruje licznik prob (dostal sygnal, warto sprobowac dalej)
    _dispatch(eng, "SQ3MZM HB9CNU -12", snr=-8)
    check(eng.retry_count == 0, "Odpowiedz partnera zeruje retry_count")

    eng.abort_qso()
    check(eng.should_retransmit(30.0) is False, "Po abort: znow IDLE, nic do powtorzenia")
    check(eng.retry_count == 0, "Po abort: licznik prob zresetowany")


# ════════════════════════════════════════════════════════════════════════════
# 11. CALL 1ST: stacja odpowiada od razu raportem (pomija Tx1/grid)
# ════════════════════════════════════════════════════════════════════════════
def test_call1st_start_with_raw_report():
    section("Call 1st: wolajacy pomija grid, od razu raport (np. 'SQ3MZM XX0XXX -10')")
    eng = QsoEngine("SQ3MZM", "JO82")
    parsed = parse_message("SQ3MZM XX0XXX -10")
    check(parsed is not None and parsed.get("report") == "-10",
          "Parsowanie: wiadomosc z surowym raportem (bez R-prefix)")

    # Krok 1 (webapp.py _process_auto_qso): silnik w IDLE, wiadomosc od NOWEJ
    # stacji adresowana do nas -> sygnalizuje 'new_caller' (webapp.py
    # natychmiast wywoluje start_qso z tym samym dekodem).
    result = eng.on_decode(parsed)
    check(result is not None and result.get("action") == "new_caller",
          "IDLE + obca stacja z raportem -> 'new_caller' (tak jak przy CQ-odpowiedzi)")
    check(result.get("call_de") == "XX0XXX", "new_caller: poprawny call_de")

    # Krok 2 (webapp.py) -> natychmiast start_qso z TYM SAMYM dekodem jako
    # initial_decode, zeby nie wysylac zbednego Tx1/grid (partner juz
    # przeslal cos wiecej niz CQ).
    start_result = eng.start_qso("XX0XXX", initial_decode=parsed)
    check(start_result is not None and start_result.get("action") == "reply",
          "start_qso(initial_decode=raport) -> od razu akcja 'reply' (pomija Tx1)")
    check(start_result.get("needs_measured_report") is True,
          "reply wymaga podstawienia ZMIERZONEGO SNR (nasz pomiar, nie ich)")
    check(start_result.get("r_flag") is True,
          "r_flag=True -> odpowiadamy z prefixem R (potwierdzenie + nasz raport)")
    check(eng.state == ST_REPORT_SENT, "Po starcie z samym raportem: REPORT_SENT")
    check(eng.partner_call == "XX0XXX", "partner_call ustawiony poprawnie")

    # Automat powinien pociagnac QSO dalej normalnie az do konca.
    act, rpt = _dispatch(eng, "SQ3MZM XX0XXX RR73", snr=5)
    check(act is not None and rpt == "73", "RR73 partnera -> nasze 73")
    check(eng.state == ST_DONE, "QSO zakonczone poprawnie mimo pominietego Tx1/grid")


# ════════════════════════════════════════════════════════════════════════════
# REGRESJA: odebrane RRR -> od razu 73, bez wlasnego echo RRR (naprawa sesji
# 2026-08-15). Wczesniej odebranie literalnego "RRR" od partnera (w
# odroznieniu od "R+raport") wysylalo WLASNE RRR z powrotem (stan RRR_SENT)
# i dopiero echo TEGO RRR od partnera konczylo QSO wyslaniem 73 - zbedny
# dodatkowy cykl. Zgloszone na zywo: "automat niepotrzebnie przedluza QSO,
# jesli dostalem RRR to wysylam 73".
# ════════════════════════════════════════════════════════════════════════════
def test_rrr_goes_straight_to_73():
    section("Regresja: odebrane RRR -> od razu 73 (bez wlasnego echo RRR)")
    eng = QsoEngine("SQ3MZM", "JO82")
    eng.start_qso("SP9XYZ", parse_message("CQ SP9XYZ JO90"))
    _dispatch(eng, "SQ3MZM SP9XYZ -12", snr=-8)
    check(eng.state == ST_REPORT_SENT, "Po naszym R-raporcie: REPORT_SENT")

    act, rpt = _dispatch(eng, "SQ3MZM SP9XYZ RRR", snr=-8)
    check(act is not None and rpt == "73", "RRR partnera -> nasze 73 BEZPOSREDNIO (nie wlasne RRR)")
    check(act.get("qso_complete"), "QSO oznaczone jako complete po odebranym RRR")
    check(eng.state == ST_DONE, "Po odebranym RRR: od razu DONE (nie RRR_SENT)")


# ════════════════════════════════════════════════════════════════════════════
# REGRESJA: R-raport partnera -> od razu nasze RR73, bez wlasnego RRR (2026-
# 09-03). Symetria do testu wyzej, ale w drugim kierunku: to MY dostajemy
# R+raport od partnera (potwierdzenie naszego raportu + jego wlasny) i to MY
# decydujemy jak odpowiedziec. Wczesniej wysylalismy wlasne RRR (stan
# RRR_SENT) i czekalismy na 73/RR73/RRR partnera, zanim w koncu koriczylismy
# wlasnym 73 - dodatkowy cykl. Zadanie na zywo: "musimy pominac rrr zamienic
# je od razu na rr73 skrocimy czas lacznosci".
# ════════════════════════════════════════════════════════════════════════════
def test_r_report_goes_straight_to_rr73():
    section("Regresja: R-raport partnera -> od razu nasze RR73 (bez wlasnego RRR)")
    eng = QsoEngine("SQ3MZM", "JO82")
    eng.start_qso("SP9XYZ", parse_message("SQ3MZM SP9XYZ JO90"))
    check(eng.state == ST_REPORT_SENT, "Po starcie z gridem: REPORT_SENT")

    act, rpt = _dispatch(eng, "SQ3MZM SP9XYZ R-08", snr=-8)
    check(act is not None and rpt == "RR73", "R-raport partnera -> nasze RR73 BEZPOSREDNIO (nie wlasne RRR)")
    check(act.get("qso_complete"), "QSO oznaczone jako complete od razu po naszym RR73")
    check(eng.state == ST_DONE, "Po R-raporcie partnera: od razu DONE (nie RRR_SENT)")

    # Ewentualne echo tego R-raportu (partner nie uslyszal naszego RR73 i
    # powtarza) po DONE -> cisza, nie wysylamy RR73 w kolko.
    act, _ = _dispatch(eng, "SQ3MZM SP9XYZ R-08", snr=-8)
    check(act is None, "Echo R-raportu po DONE -> cisza")


# ════════════════════════════════════════════════════════════════════════════
# REGRESJA: partner nadaje juz do kogos innego -> porzucamy wolanie zamiast
# slepo probowac dalej (naprawa sesji 2026-08-15). Zglaszane na zywo:
# odpowiedzielismy na CQ, ale zanim partner nas uslyszal, zaczal QSO z inna
# stacja - retry mechanizm i tak dalej go wolal, mimo widocznego dowodu w
# dekodach ze jest juz zajety.
# ════════════════════════════════════════════════════════════════════════════
def test_partner_busy_with_someone_else():
    section("Regresja: partner nadaje do kogos innego -> porzucamy wolanie")
    eng = QsoEngine("SQ3MZM", "JO72")
    eng.start_qso("YO1BRANCUSI", parse_message("CQ YO1BRANCUSI KN34"))
    check(eng.state == ST_CALLING, "Po start_qso: CALLING")

    # YO1BRANCUSI nadaje do UR5WCS, nie do nas
    result = eng.on_decode(parse_message("UR5WCS YO1BRANCUSI -09"))
    check(result is not None and result.get("action") == "partner_busy",
          "Partner zajety inna stacja -> akcja partner_busy")
    check(result.get("call_de") == "YO1BRANCUSI",
          "partner_busy wskazuje wlasciwa (nasza) stacje")

    # Kontrola: wiadomosc miedzy DWIEMA OBCYMI stacjami (nie nasz partner)
    # nie powinna wywolywac partner_busy - to normalna aktywnosc pasma.
    eng2 = QsoEngine("SQ3MZM", "JO72")
    eng2.start_qso("YO1BRANCUSI", parse_message("CQ YO1BRANCUSI KN34"))
    result2 = eng2.on_decode(parse_message("UR5WCS DL1ABC -09"))
    check(result2 is None, "Wymiana miedzy obcymi stacjami -> cisza (nie partner_busy)")


# ════════════════════════════════════════════════════════════════════════════
# Systematyczny przeglad scenariuszy (2026-08-24): partner ZNOWU wola CQ w
# trakcie naszej z nim wymiany - silniejszy dowod ze "poszedl dalej" niz
# partner_busy (odpowiada komus innemu), a wczesniej byl calkowicie
# ignorowany (is_cq zawsze zwracalo None, bez sprawdzenia czy to nasz
# partner). Routowane przez ten sam partner_busy (bounded retry, nie
# natychmiastowy abort).
# ════════════════════════════════════════════════════════════════════════════
def test_partner_calls_cq_again():
    section("Partner wola CQ ponownie w trakcie QSO -> partner_busy")
    eng = QsoEngine("SQ3MZM", "JO72")
    eng.start_qso("YO1BRANCUSI", parse_message("CQ YO1BRANCUSI KN34"))
    check(eng.state == ST_CALLING, "Po start_qso: CALLING")

    result = eng.on_decode(parse_message("CQ YO1BRANCUSI KN34"))
    check(result is not None and result.get("action") == "partner_busy",
          "Partner znow wola CQ -> akcja partner_busy")
    check(result.get("call_de") == "YO1BRANCUSI",
          "partner_busy wskazuje wlasciwa (nasza) stacje")

    # Kontrola: CQ od OBCEJ stacji (nie naszego partnera) to normalna
    # aktywnosc pasma - nie powinno nic wywolywac.
    eng2 = QsoEngine("SQ3MZM", "JO72")
    eng2.start_qso("YO1BRANCUSI", parse_message("CQ YO1BRANCUSI KN34"))
    result2 = eng2.on_decode(parse_message("CQ DL1ABC JO40"))
    check(result2 is None, "CQ od obcej stacji -> cisza (nie partner_busy)")

    # Kontrola: CQ gdy jestesmy IDLE (bez aktywnego QSO) - bez zmian,
    # UI/webapp decyduje czy odpowiedziec.
    eng3 = QsoEngine("SQ3MZM", "JO72")
    result3 = eng3.on_decode(parse_message("CQ YO1BRANCUSI KN34"))
    check(result3 is None, "CQ gdy IDLE -> bez akcji silnika")


# ════════════════════════════════════════════════════════════════════════════
# NOWA FUNKCJA: parse_dxpedition_message (typ 0.1, i3=0/n3=1) - wiadomosc
# ktorej Fox uzywa zeby JEDNOCZESNIE zamknac QSO (RR73) i zaprosic kolejna
# stacje z raportem, w jednej transmisji. Ten sam format wiadomosci uzywaja
# stacje na MSHV w trybie "Multi Answering" nawet w zwyklych, codziennych
# QSO (nie tylko prawdziwe DXpedycje) - stad silnik glownego automatu, nie
# tylko Hound mode, musi to poprawnie rozumiec.
# ════════════════════════════════════════════════════════════════════════════
def test_dxpedition_message_not_for_us():
    section("parse_dxpedition_message: wiadomosc dotyczy dwoch INNYCH stacji")
    parse_dxpedition_message = _qe.parse_dxpedition_message
    result = parse_dxpedition_message("K1ABC", "W9XYZ", "KH1/KH7Z", "-08", "SQ3MZM")
    check(result is None, "Ani call_to ani call_de to nie my -> None")


def test_dxpedition_message_we_get_report():
    section("parse_dxpedition_message: jestesmy zapraszani z nowym raportem")
    parse_dxpedition_message = _qe.parse_dxpedition_message
    # call_to=K1ABC (inny Hound, dostaje RR73), call_de=SQ3MZM (my, dostajemy raport)
    parsed = parse_dxpedition_message("K1ABC", "SQ3MZM", "KH1/KH7Z", "-08", "SQ3MZM")
    check(parsed is not None, "Wiadomosc do nas -> sparsowana")
    check(parsed["call_to"] == "SQ3MZM", "call_to = my znak")
    check(parsed["call_de"] == "KH1/KH7Z", "call_de = prawdziwy nadawca (sender_call)")
    check(parsed["report"] == "-08", "Surowy raport bez R-prefix")
    check(not parsed["is_rr73"] and not parsed["is_73"] and not parsed["is_rrr"],
          "To NIE jest RR73/73/RRR - zwykly nowy raport")

    # Pelna integracja: IDLE -> powinno dac 'new_caller'
    eng = QsoEngine("SQ3MZM", "JO82")
    result = eng.on_decode(parsed)
    check(result is not None and result.get("action") == "new_caller",
          "Stacja MSHV zaprasza nas z raportem gdy IDLE -> new_caller")


def test_dxpedition_message_we_get_rr73():
    section("parse_dxpedition_message: dostajemy RR73 (konczy nasze QSO)")
    parse_dxpedition_message = _qe.parse_dxpedition_message
    # call_to=SQ3MZM (my, dostajemy RR73), call_de=W9XYZ (inny Hound, dostaje raport)
    parsed = parse_dxpedition_message("SQ3MZM", "W9XYZ", "DL1XYZ", "-13", "SQ3MZM")
    check(parsed is not None, "Wiadomosc do nas -> sparsowana")
    check(parsed["is_rr73"] is True, "To jest RR73")
    check(parsed["call_de"] == "DL1XYZ", "call_de = prawdziwy nadawca (sender_call)")

    # Pelna integracja: jestesmy W TRAKCIE QSO z DL1XYZ (np. stacja na MSHV
    # ktora dopiero co wyslala nam surowy raport zwykla wiadomoscia, my
    # odpowiedzielismy R+rpt, i TERAZ ona konczy QSO polaczona wiadomoscia
    # zamiast zwyklego "SQ3MZM DL1XYZ RR73").
    eng = QsoEngine("SQ3MZM", "JO82")
    eng.start_qso("DL1XYZ", parse_message("CQ DL1XYZ JO60"))
    _dispatch(eng, "SQ3MZM DL1XYZ -12", snr=-8)
    check(eng.state == ST_REPORT_SENT, "Przed polaczona wiadomoscia: REPORT_SENT")

    result = eng.on_decode(parsed)
    check(result is not None and result.get("qso_complete"),
          "Polaczona wiadomosc RR73 od aktywnego partnera -> QSO complete")
    check(result.get("report_or_grid") == "73", "Odpowiadamy naszym 73")
    check(eng.state == ST_DONE, "Po polaczonym RR73: DONE")


def test_dxpedition_message_unresolved_sender_falls_back():
    section("parse_dxpedition_message: nierozpoznany hash Foxa (\"...\") -> fallback")
    parse_dxpedition_message = _qe.parse_dxpedition_message
    parsed = parse_dxpedition_message("SQ3MZM", "W9XYZ", "...", "-13", "SQ3MZM")
    check(parsed is not None, "Wiadomosc do nas -> sparsowana mimo nierozpoznanego hasha")
    check(parsed["call_de"] == "W9XYZ", "Fallback na call_de gdy sender_call nierozpoznany")


def main():
    print("╔══════════════════════════════════════════════════════╗")
    print("║  TEST SUITE — Maszyna stanow QSO (HAMCTRL)            ║")
    print("╚══════════════════════════════════════════════════════╝")

    test_parsing()
    test_full_qso_we_answer()
    test_full_qso_we_cq()
    test_no_extending_after_done()
    test_no_loop_after_abort_on_partner_echo()
    test_repeated_call()
    test_give_up_after_stuck_report_repeats_grid()
    test_give_up_after_stuck_report_repeats_raw_report()
    test_frozen_report()
    test_partner_report_last_value()
    test_no_queue_busy_caller_ignored()
    test_first_contact_at_not_calling_start()
    test_reset_between_qso()
    test_retransmit_and_giveup()
    test_call1st_start_with_raw_report()
    test_rrr_goes_straight_to_73()
    test_r_report_goes_straight_to_rr73()
    test_partner_busy_with_someone_else()
    test_partner_calls_cq_again()
    test_dxpedition_message_not_for_us()
    test_dxpedition_message_we_get_report()
    test_dxpedition_message_we_get_rr73()
    test_dxpedition_message_unresolved_sender_falls_back()

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
