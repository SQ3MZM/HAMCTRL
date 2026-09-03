#!/usr/bin/env python3
"""
civ_profiles.py — profile CI-V dla modeli ze wbudowanym scope (SCOPE_MODELS).

Kazdy profil zawiera parametry specyficzne dla danego modelu radia:
  - default_addr   : domyslny adres CI-V (hex int) — uzywany gdy uzytkownik
                      nie poda wlasnego w ustawieniach
  - default_baud   : domyslna predkosc portu szeregowego
  - mode_map       : mapowanie bajtu trybu (0x04/0x01) -> nazwa trybu
                      (wiekszosc Icomow uzywa tej samej tabeli, ale niektore
                      modele maja dodatkowe/inne tryby — np. D-STAR w IC-9100)
  - scope_max      : maksymalna wartosc amplitudy w danych scope (do
                      normalizacji 0-255 w UI)
  - scope_header_len: dlugosc naglowka pierwszej ramki scope (przed danymi)
  - notes          : uwagi/zrodlo dla osoby aktualizujacej profil

UWAGA: Wartosci dla modeli innych niz IC-7300 (referencyjny, przetestowany)
NIE byly zweryfikowane na sprzecie. Jezeli cos nie dziala dla danego modelu —
sprawdz oficjalna instrukcje "CI-V Reference Guide" dla tego radia
(dostepna na stronie icomeurope.com/icomamerica.com) i zaktualizuj profil.
"""

# Wspolna baza trybow — wiekszosc Icomow CI-V uzywa tego mapowania
_BASE_MODE_MAP = {
    0: "LSB", 1: "USB", 2: "AM", 3: "CW", 4: "RTTY",
    5: "FM", 6: "WFM", 7: "CW-R", 8: "RTTY-R", 17: "DV",
}

# Standardowy zestaw capabilities dla radia CI-V z bezposrednim sterowaniem.
# Kazdy klucz odpowiada jednej funkcji widocznej w panelu (przyciski pod waterfallem).
# Wartosc = True/False oznacza czy radio TECHNICZNIE wspiera te funkcje
# (czy CivRig potrafi ja wykonac). To NIE jest whitelist dla uzytkownikow —
# to jest zrodlo prawdy "co radio umie", a admin decyduje co WLACZYC w config.json.
_BASE_CAPS_CIV = {
    "freq_set":     True,   # zmiana czestotliwosci (0x05)
    "mode_set":     True,   # zmiana trybu pracy (0x06)
    "ptt":          True,   # PTT przez CI-V (0x1C 00)
    "split":        True,   # split VFO A/B (0x0F)
    "smeter":       True,   # odczyt S-metru (0x15 02)
    "scope":        True,   # waterfall/scope (0x27 00)
    "rit":          False,  # RIT/XIT — niezaimplementowane w civ.py
    "memory":       False,  # kanaly pamieci — niezaimplementowane
    "vfo_ab":       True,   # Select VFO A/B (0x07 00/01)
    "power":        True,   # Power ON/OFF radia (0x18 01/00)
    "dstar":        False,  # D-STAR (tylko niektore modele)
    # Dodane 2026-09-03: TUNER i AUTOTUNE faktycznie byly zaimplementowane
    # w civ.py (set_tuner/start_tuner_autotune) od dawna, ale nigdy nie
    # mialy wlasnego wpisu tutaj ani w features.py - ich widocznosc dla
    # zwyklego usera byla przypadkowo podpieta pod mode_set/ptt (patrz
    # webapp.py, elif t == "tuner"/"tuner_autotune"). Teraz wlasne klucze,
    # zeby admin mogl je wlaczac/wylaczac niezaleznie w KONFIGURACJA ->
    # FUNKCJE RADIA, tak jak split.
    "tuner":        True,   # Tuner ON/OFF (CI-V 1C 01 00/01)
    "autotune":     True,   # Autotune - cykl dopasowania, generuje TX (CI-V 1C 01 02)
}


# ── Set Level (CI-V cmd 0x14) — IC-7300 ──────────────────────────────────────
# Kazdy wpis: nazwa -> {sub: subcommand (0x14 XX), label, min/max (jednostki
# w UI), civ_max: max wartosc CI-V (0..255 lub 0..120 — zalezy od parametru)
# Wiekszosc poziomow IC-7300 uzywa zakresu CI-V 0000-0255 (BCD, 2 bajty).
# KEYSPD: 0000-0060 (6-48 WPM mapowane na 0-255 przez Hamlib, ale natywnie
#         IC-7300 uzywa wlasnej skali 0000-0060 dla 6-48 WPM).
_IC7300_LEVELS = {
    "AF":       {"sub": 0x01, "min": 0,   "max": 1.0,  "civ_max": 255, "label": "AF"},
    "RF":       {"sub": 0x02, "min": 0,   "max": 1.0,  "civ_max": 255, "label": "RF GAIN"},
    "SQL":      {"sub": 0x03, "min": 0,   "max": 1.0,  "civ_max": 255, "label": "SQL"},
    "NR":       {"sub": 0x06, "min": 0,   "max": 1.0,  "civ_max": 255, "label": "NR"},
    "PBT_IN":   {"sub": 0x07, "min": 0,   "max": 1.0,  "civ_max": 255, "label": "PBT IN"},
    "PBT_OUT":  {"sub": 0x08, "min": 0,   "max": 1.0,  "civ_max": 255, "label": "PBT OUT"},
    "CWPITCH":  {"sub": 0x09, "min": 300, "max": 900,  "civ_max": 255, "label": "CW PITCH"},
    "RFPOWER":  {"sub": 0x0A, "min": 0,   "max": 1.0,  "civ_max": 255, "label": "RF PWR"},
    "MICGAIN":  {"sub": 0x0B, "min": 0,   "max": 1.0,  "civ_max": 255, "label": "MIC GAIN"},
    "KEYSPD":   {"sub": 0x0C, "min": 6,   "max": 48,   "civ_max": 255, "label": "KEYSPD"},
    "NOTCHF":   {"sub": 0x0D, "min": 0,   "max": 1.0,  "civ_max": 255, "label": "NOTCH"},
    "COMP":     {"sub": 0x0E, "min": 0,   "max": 1.0,  "civ_max": 255, "label": "COMP"},
    # POPRAWKA: Break-IN Delay to CI-V 14 0F (00 00=2.0d .. 02 55=13.0d),
    # NIE 14 12 (to jest NB level, patrz NB_LEVEL ponizej). Wczesniejsza
    # wersja mapowala BKINDL na 0x12 — kolidowalo to z NB_LEVEL i bylo
    # niezgodne z dokumentacja CI-V (str.19-3).
    "BKINDL":   {"sub": 0x0F, "min": 0,   "max": 1.0,  "civ_max": 255, "label": "BK-IN DELAY"},
    "NB_LEVEL": {"sub": 0x12, "min": 0,   "max": 1.0,  "civ_max": 255, "label": "NB LEVEL"},
    # USUNIETO: AGC_TIME (poprzednio blednie 14 37 — taki subcommand nie
    # istnieje w tabeli 0x14). Faktyczny AGC time constant to CI-V 1A 04
    # (00=OFF, AM:01-13=0.3-8.0s, SSB/CW/RTTY:01-13=0.1-6.0s) — INNA
    # struktura komendy (1A, nie 14), niezaimplementowana w set_level().
    # Wymaga osobnej obslugi w civ.py (set_agc_time) przed przywroceniem
    # tego slidera.
}

# ── Set Func (CI-V cmd 0x16) — IC-7300 ───────────────────────────────────────
# Kazdy wpis: nazwa -> subcommand (0x16 XX 00/01)
_IC7300_FUNCS = {
    "NB":     0x22,  # Noise Blanker
    "NR":     0x40,  # Noise Reduction
    "ANF":    0x41,  # Auto Notch Filter
    "COMP":   0x44,  # Compressor
    "VOX":    0x46,  # VOX
    "TONE":   0x42,  # Tone (repeater tone)
    "TSQL":   0x43,  # Tone Squelch
    # POPRAWKA: 0x47 to "BK-IN function" (00=OFF,01=Semi,02=Full) wg
    # dokumentacji CI-V — NIE "Twin PBT" jak bylo wczesniej oznaczone.
    # Twin PBT to regulacja ciagla (sliders PBT_IN/PBT_OUT, cmd 14 07/08),
    # nie prosty toggle 0x16. Przemianowano na BKIN zgodnie z faktyczna
    # funkcja CI-V.
    "BKIN":   0x47,  # BK-IN function (semi/full break-in)
    # POPRAWKA: 0x55 nie istnieje w tabeli 0x16 (Set Func). "Monitor
    # function" to faktycznie 0x45.
    "MON":    0x45,  # Monitor (sidetone TX)
}

# Etykiety dla funkcji (zgodne z hamlib_caps.FUNC_LABELS gdzie mozliwe).
# Format "SKROT (opis)": frontend (radiofunctions.js) tnie po '(' -> na
# przycisku widac tylko uniwersalny skrot (SKROT), pelny string (z polskim
# opisem) trafia do title (tooltip po najechaniu) — tak przyciski dzialaja
# identycznie w PL/EN bez osobnego tlumaczenia backendu.
_FUNC_LABELS = {
    "NB":   "NB (Noise Blanker)",
    "NR":   "NR (Redukcja szumow)",
    "ANF":  "ANF (Auto Notch Filter)",
    "COMP": "COMP (Kompresor)",
    "VOX":  "VOX",
    "TONE": "TONE (Tone CTCSS TX)",
    "TSQL": "TSQL (Tone Squelch)",
    "BKIN": "BKIN (Break-In)",
    "MON":  "MON (Monitor sidetone TX)",
}


# ── Pasma per model radia ────────────────────────────────────────────────────
# Kazde radio obejmuje inny zakres pasm. IC-7300 to HF + 6m (bez VHF/UHF),
# IC-9700 to czysto VHF/UHF/SHF (bez HF). Zeby UI pokazywalo tylko pasma,
# ktore dane radio FAKTYCZNIE ma, wiazemy liste pasm z profilem modelu.
#
# Format: nazwa -> (min_hz, max_hz, default_hz). Transwertery (13cm/23cm na
# radiach bez natywnego SHF) obsluguje sie osobno przez offset — patrz uwaga
# przy pasmach SHF.
_BANDS_HF = {
    "160m": (1810000,   2000000,   1850000),
    "80m":  (3500000,   3800000,   3650000),
    "60m":  (5351500,   5366500,   5357000),
    "40m":  (7000000,   7200000,   7100000),
    "30m":  (10100000,  10150000,  10125000),
    "20m":  (14000000,  14350000,  14200000),
    "17m":  (18068000,  18168000,  18100000),
    "15m":  (21000000,  21450000,  21200000),
    "12m":  (24890000,  24990000,  24930000),
    "10m":  (28000000,  29700000,  28400000),
}
_BAND_6M   = {"6m":   (50000000,   52000000,   50150000)}
_BAND_4M   = {"4m":   (70000000,   70500000,   70150000)}
_BAND_2M   = {"2m":   (144000000,  146000000,  144300000)}
_BAND_70CM = {"70cm": (430000000,  440000000,  432100000)}
_BAND_23CM = {"23cm": (1240000000, 1300000000, 1296200000)}
# 13cm: na Icomach zwykle przez transwerter (radio pokazuje IF, np. 144 lub
# 432 MHz, a nadaje na 2320/2400). Podajemy pasmo docelowe; obsluga offsetu
# transwertera to osobne ustawienie admina (transverter_offset w config).
_BAND_13CM = {"13cm": (2320000000, 2450000000, 2320200000)}

# Zlozone zestawy pasm per typ radia
_BANDS_HF_6M       = {**_BANDS_HF, **_BAND_6M}
_BANDS_HF_6M_4M    = {**_BANDS_HF, **_BAND_6M, **_BAND_4M}
_BANDS_HF_6M_2M    = {**_BANDS_HF, **_BAND_6M, **_BAND_2M}   # IC-746: HF+50+144MHz, BEZ 70cm
_BANDS_HF_VU       = {**_BANDS_HF, **_BAND_6M, **_BAND_2M, **_BAND_70CM}
_BANDS_HF_VU_23    = {**_BANDS_HF, **_BAND_6M, **_BAND_2M, **_BAND_70CM, **_BAND_23CM}
_BANDS_VUSHF       = {**_BAND_2M, **_BAND_70CM, **_BAND_23CM}   # IC-9700


CIV_PROFILES = {

    # ── IC-7300 — profil REFERENCYJNY, przetestowany ────────────────────────
    "3073": {
        "name":             "IC-7300",
        "default_addr":     0x94,
        "default_baud":     115200,
        "mode_map":         dict(_BASE_MODE_MAP),
        "bands":            dict(_BANDS_HF_6M_4M),   # HF + 6m + 4m (70 MHz)
        "scope_max":        160,   # 0x00..0xA0
        "scope_header_len": 15,
        "capabilities":     dict(_BASE_CAPS_CIV),
        "levels":           dict(_IC7300_LEVELS),
        "funcs":            dict(_IC7300_FUNCS),
        "func_labels":      dict(_FUNC_LABELS),
        "notes":            "Profil referencyjny — przetestowany na sprzecie.",
    },

    # ── IC-746 — stary CI-V (BEZ scope, BEZ trybu DATA/USB-D) ────────────────
    # Dodane 2026-09-02, zeby ominac rigctld/Hamlib (padal w trakcie sesji,
    # patrz _respawn_rigctld w rigcat.py) - IC-746 jest CI-V, wiec civ.py
    # moze mowic z nim bezposrednio, TRZEBA jednak innej ramki set_mode.
    # Zrodlo: oficjalny CI-V reference (adres domyslny 0x56, 19200bd, pasma
    # HF+50+144MHz bez 70cm - universal-radio.com/qsl.net specs) +
    # Hamlib rigs/icom/icom.c (RIG_IS_IC746 jest jawnie na liscie radii ze
    # "icmode_ext = -1" w icom_set_mode_without_data() - komenda 06 <mode>
    # BEZ trzeciego bajtu filtra, w przeciwienstwie do IC-7300's
    # 06 <mode> <fil>). Poziomy (0x14 xx) i funkcje (0x16 xx) to ten sam,
    # stary, uniwersalny zestaw Icoma co IC-7300 (potwierdzone w
    # icom_defs.h) - stad "levels"/"funcs" ponizej to bezposrednia kopia
    # IC-7300, NIE zgadywanie.
    "3023": {
        "name":             "IC-746",
        "default_addr":     0x56,
        "default_baud":     19200,
        "mode_map":         dict(_BASE_MODE_MAP),
        "bands":            dict(_BANDS_HF_6M_2M),   # HF + 6m + 2m, BEZ 70cm
        "scope_max":        160,   # nieuzywane (capabilities.scope=False), placeholder
        "scope_header_len": 15,    # jw.
        # power=False: Hamlib's own IC-746 backend (rigs/icom/ic746.c) never
        # assigns a set_powerstat function at all - CI-V remote power-ON
        # needs the radio's standby circuit to listen on the bus while
        # "off", a feature Icom only added starting with later designs.
        # Leaving power=True here would show a PWR ON/OFF button that's
        # unverified and likely does nothing on this radio.
        "capabilities":     {**_BASE_CAPS_CIV, "scope": False, "power": False},
        "levels":           dict(_IC7300_LEVELS),
        "funcs":            dict(_IC7300_FUNCS),
        "func_labels":      dict(_FUNC_LABELS),
        # Radio-specific quirks czytane przez civ.py::set_mode() - patrz
        # komentarz tam. Domyslnie (brak klucza) = True, jak IC-7300.
        "mode_has_filter_byte": False,   # 06 <mode> TYLKO, bez bajtu filtra
        "data_mode_supported":  False,   # brak 1A 06 (USB-D/DATA) - starsze radio
        # has_vfo_b_read=False: CI-V 25/26 (odczyt VFO-B/nieaktywnego VFO)
        # to komenda NOWEJ generacji - Hamlib sam to wykrywa i loguje przy
        # starcie: "icom_set_x25x26_ability: Hamlib thinks rig does not
        # support x25/x26 command" (potwierdzone live na tym IC-746, log
        # 2026-09-02). Bez tej flagi self.freq_b zostaje na starej/domyslnej
        # wartosci NA STALE (0x25 zawsze NG/timeout) - realna luka
        # bezpieczenstwa: zabezpieczenie cross-band-split (webapp.py
        # _is_split_cross_band) porownywalo pasmo na podstawie tej
        # zamrozonej, falszywej wartosci i nigdy nie wykrywalo prawdziwego
        # cross-band. Flaga wylacza zarowno zbedny poll 0x25 (radio i tak
        # go odrzuci) jak i każe cross-band-checkowi failować BEZPIECZNIE
        # (blokować TX na SPLIT) zamiast ufać nieznanej wartosci.
        "has_vfo_b_read":       False,
        "notes":            ("NIEZWERYFIKOWANE na sprzecie (dopiero po tym "
                              "profilu) - ale komendy sprawdzone zrodlowo w "
                              "Hamlibie, nie zgadywane. Jesli mode/levels nie "
                              "dzialaja: sprawdz najpierw czy radio faktycznie "
                              "ma adres CI-V 0x56 (MENU > SET > CI-V), "
                              "19200bd."),
    },

    # ── IC-7610 ──────────────────────────────────────────────────────────────
    "3078": {
        "name":             "IC-7610",
        "default_addr":     0x98,
        "default_baud":     115200,
        "mode_map":         dict(_BASE_MODE_MAP),
        "bands":            dict(_BANDS_HF_6M_4M),   # HF + 6m + 4m
        "scope_max":        160,
        "scope_header_len": 15,
        "capabilities":     dict(_BASE_CAPS_CIV),
        "notes":            ("NIEZWERYFIKOWANE na sprzecie. IC-7610 ma dual-RX "
                              "(Main/Sub) — naglowek scope zawiera dodatkowy bajt "
                              "wyboru scope'u (Main=0x00/Sub=0x01) na pozycji "
                              "zaraz po seq/total. Jezeli scope nie dziala "
                              "poprawnie, sprawdz CI-V Reference Guide IC-7610 "
                              "sekcja 'Scope waveform data' (cmd 27 00)."),
    },

    # ── IC-705 ───────────────────────────────────────────────────────────────
    "3085": {
        "name":             "IC-705",
        "default_addr":     0xA4,
        "default_baud":     115200,
        "mode_map":         dict(_BASE_MODE_MAP),
        "bands":            dict(_BANDS_HF_VU),   # HF, 6m, 2m, 70cm
        "scope_max":        160,
        "scope_header_len": 15,
        "capabilities":     dict(_BASE_CAPS_CIV),
        "notes":            ("NIEZWERYFIKOWANE na sprzecie. IC-705 czesto "
                              "laczy sie przez Bluetooth/WLAN zamiast USB-serial "
                              "— jesli uzywasz USB, sprawdz baudrate w "
                              "MENU > SET > Connectors > CI-V (domyslnie 115200 "
                              "dla USB)."),
    },

    # ── IC-9100 ──────────────────────────────────────────────────────────────
    "3068": {
        "name":             "IC-9100",
        "default_addr":     0x7C,
        "default_baud":     115200,
        # IC-9100 dodaje tryb DV (D-STAR) — juz w _BASE_MODE_MAP (17)
        "mode_map":         dict(_BASE_MODE_MAP),
        "bands":            dict(_BANDS_HF_VU_23),   # HF, 6m, 2m, 70cm, 23cm (z opcja UX-9100)
        "scope_max":        160,
        "scope_header_len": 15,
        "capabilities":     {**_BASE_CAPS_CIV, "dstar": True},
        "notes":            ("NIEZWERYFIKOWANE na sprzecie. IC-9100 to radio "
                              "VHF/UHF/HF z dual-band — moze wymagac dodatkowego "
                              "parametru wyboru pasma (Main/Sub = HF czy V/UHF) "
                              "przed komendami scope. Sprawdz CI-V Reference "
                              "Guide IC-9100, sekcja 'Scope waveform data'."),
    },

    # ── IC-7100 ──────────────────────────────────────────────────────────────
    "3070": {
        "name":             "IC-7100",
        "default_addr":     0x88,
        "default_baud":     115200,
        "mode_map":         dict(_BASE_MODE_MAP),
        "bands":            dict(_BANDS_HF_VU),   # HF, 6m, 2m, 70cm
        "scope_max":        160,
        "scope_header_len": 15,
        "capabilities":     {**_BASE_CAPS_CIV, "dstar": True},
        "notes":            ("NIEZWERYFIKOWANE na sprzecie. IC-7100 ma scope "
                              "tylko w trybie 'Center' (nie Fixed) wedlug "
                              "niektorych zrodel — jesli scope nie wysyla "
                              "danych, sprawdz ustawienie SCOPE > SPAN/MODE "
                              "na radiu."),
    },

    # ── IC-9700 — czysto VHF/UHF/SHF (2m/70cm/23cm, BEZ HF) ─────────────────
    "3081": {
        "name":             "IC-9700",
        "default_addr":     0xA2,
        "default_baud":     115200,
        "mode_map":         dict(_BASE_MODE_MAP),   # ma DV (D-STAR), juz w bazie
        "bands":            dict(_BANDS_VUSHF),      # 2m, 70cm, 23cm — bez HF
        "scope_max":        160,
        "scope_header_len": 15,
        "capabilities":     {**_BASE_CAPS_CIV, "dstar": True},
        "notes":            ("NIEZWERYFIKOWANE na sprzecie. IC-9700 to radio "
                              "czysto VHF/UHF/SHF (2m/70cm/23cm) — NIE ma HF. "
                              "Ma satelitarny dual-watch (Main/Sub) — scope moze "
                              "wymagac wyboru pasma przed komenda 27 00, podobnie "
                              "jak IC-9100. 23cm (1296) natywne, bez transwertera. "
                              "Adres CI-V domyslny 0xA2. Sprawdz CI-V Reference "
                              "Guide IC-9700 sekcja 'Scope waveform data' jesli "
                              "scope nie dziala."),
    },
}


# Profil domyslny — uzywany gdy model nie jest w CIV_PROFILES
# (nie powinno sie zdarzyc jesli SCOPE_MODELS i CIV_PROFILES sa zsynchronizowane,
#  ale zabezpiecza przed KeyError przy literowce/nowym modelu)
DEFAULT_PROFILE = {
    "name":             "Nieznany model CI-V",
    "default_addr":     0x94,
    "default_baud":     115200,
    "mode_map":         dict(_BASE_MODE_MAP),
    "bands":            dict(_BANDS_HF_6M_4M),   # fallback: zakres jak IC-7300
    "scope_max":        160,
    "scope_header_len": 15,
    "capabilities":     dict(_BASE_CAPS_CIV),
    "levels":           {},
    "funcs":            {},
    "func_labels":      {},
    "notes":            "Profil domyslny (fallback) — model nie ma dedykowanego profilu.",
}


def get_civ_profile(model_id: str) -> dict:
    """
    Zwroc profil CI-V dla danego model_id (np. '3073').
    Jesli model nie ma profilu, zwroc DEFAULT_PROFILE i wypisz ostrzezenie.
    Brakujace klucze 'levels'/'funcs'/'func_labels' sa dopelniane pustymi
    dict — pozwala to civ.py bezpiecznie wolac .get('levels', {}) bez
    sprawdzania kazdego profilu osobno.
    """
    model_id = str(model_id)
    profile = CIV_PROFILES.get(model_id)
    if profile is None:
        print(f"[civ_profiles] UWAGA: brak profilu dla model={model_id}, "
              f"uzywam DEFAULT_PROFILE")
        return DEFAULT_PROFILE
    # Dopelnij brakujace klucze (modele bez levels/funcs/bands zdefiniowanych jawnie)
    for k in ("levels", "funcs", "func_labels"):
        profile.setdefault(k, {})
    # bands: gdyby jakis profil go nie mial, daj zakres jak IC-7300 (bezpieczny
    # fallback HF+6m+4m) zamiast pustej listy, ktora ukrylaby wszystkie pasma
    profile.setdefault("bands", dict(_BANDS_HF_6M_4M))
    return profile
