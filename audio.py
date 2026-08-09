#!/usr/bin/env python3
"""
audio.py — wykrywanie kart dźwiękowych systemu (rx=wejścia, tx=wyjścia).
"""
import sys

# Cache dostepnosci sounddevice — sprawdzamy import RAZ, nie przy kazdym
# wywolaniu. Bez tego enumerate_audio_devices probowal importowac sounddevice
# w kolko i przy kazdej nieudanej probie wypisywal blad, zasmiecajac log
# (dziesiatki linii "[audio] sounddevice niedostępny" przy odswiezaniu statusu).
# None = jeszcze nie sprawdzone; False = brak; modul = dostepny.
_SD_CACHED = None


def _get_sounddevice():
    """Zwroc modul sounddevice jesli dostepny, inaczej None. Import probowany
    tylko RAZ — wynik zapamietany, wiec brak modulu nie spamuje logu."""
    global _SD_CACHED
    if _SD_CACHED is None:
        try:
            import sounddevice as _sd
            _SD_CACHED = _sd
        except Exception as e:
            print(f"[audio] sounddevice niedostępny "
                  f"({type(e).__name__}) — używam natywnej detekcji kart")
            _SD_CACHED = False
    return _SD_CACHED or None


def _enumerate_audio_winmm() -> dict:
    """Fallback (Windows): waveIn/waveOut przez ctypes — bez zależności.
    Nazwy ograniczone do ~31 znaków (limit MME)."""
    import ctypes
    from ctypes import wintypes
    winmm = ctypes.windll.winmm
    # ABI: id urządzenia to UINT_PTR (c_size_t) — istotne na 64-bit Windows.
    winmm.waveInGetNumDevs.restype = wintypes.UINT
    winmm.waveOutGetNumDevs.restype = wintypes.UINT
    winmm.waveInGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.c_void_p, wintypes.UINT]
    winmm.waveInGetDevCapsW.restype = wintypes.UINT
    winmm.waveOutGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.c_void_p, wintypes.UINT]
    winmm.waveOutGetDevCapsW.restype = wintypes.UINT

    class WAVEINCAPS(ctypes.Structure):
        _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                    ("vDriverVersion", wintypes.UINT),
                    ("szPname", wintypes.WCHAR * 32),
                    ("dwFormats", wintypes.DWORD),
                    ("wChannels", wintypes.WORD),
                    ("wReserved1", wintypes.WORD)]

    class WAVEOUTCAPS(ctypes.Structure):
        _fields_ = [("wMid", wintypes.WORD), ("wPid", wintypes.WORD),
                    ("vDriverVersion", wintypes.UINT),
                    ("szPname", wintypes.WCHAR * 32),
                    ("dwFormats", wintypes.DWORD),
                    ("wChannels", wintypes.WORD),
                    ("wReserved1", wintypes.WORD),
                    ("dwSupport", wintypes.DWORD)]

    rx, tx = [], []
    for i in range(winmm.waveInGetNumDevs()):
        caps = WAVEINCAPS()
        if winmm.waveInGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            nm = (caps.szPname or "").strip()
            if nm and nm not in rx:
                rx.append(nm)
    for i in range(winmm.waveOutGetNumDevs()):
        caps = WAVEOUTCAPS()
        if winmm.waveOutGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            nm = (caps.szPname or "").strip()
            if nm and nm not in tx:
                tx.append(nm)
    return {"rx": rx, "tx": tx, "source": "winmm"}


def enumerate_audio_devices() -> dict:
    """Karty dźwiękowe systemu: rx=wejścia (capture), tx=wyjścia (render).
    1) sounddevice (PortAudio) — pełne nazwy, rozdziela in/out.
    2) Windows: winmm (ctypes) — fallback bez zależności.
    Zwraca {'rx':[...], 'tx':[...], 'source':...}."""
    rx, tx = [], []
    sd = _get_sounddevice()
    if sd is not None:
        try:
            seen_rx, seen_tx = set(), set()
            for d in sd.query_devices():
                name = (d.get("name") or "").strip()
                if not name:
                    continue
                if d.get("max_input_channels", 0) > 0 and name not in seen_rx:
                    seen_rx.add(name); rx.append(name)
                if d.get("max_output_channels", 0) > 0 and name not in seen_tx:
                    seen_tx.add(name); tx.append(name)
            if rx or tx:
                return {"rx": rx, "tx": tx, "source": "sounddevice"}
        except Exception as e:
            print(f"[audio] sounddevice query blad: {type(e).__name__}: {e}")

    if sys.platform == "win32":
        try:
            return _enumerate_audio_winmm()
        except Exception as e:
            print(f"[audio] winmm fallback błąd: {type(e).__name__}: {e}")

    return {"rx": rx, "tx": tx, "source": "none"}


# Wzorce nazw kart audio typowych dla radia amatorskiego. Kolejność ma
# znaczenie — priorytet od najbardziej specyficznych do ogólnych.
_RADIO_CARD_PATTERNS = [
    # Icom - IC-7300, IC-705, IC-7610, IC-9700 zwykle wystawiaja "USB Audio CODEC"
    "USB Audio CODEC",
    # Yaesu - FT-991, FT-DX10, FT-710 wystawiaja "USB AUDIO CODEC" (wersalik) lub "SCU-17"
    "USB AUDIO CODEC",
    "SCU-17",
    # Kenwood - TS-590 wystawia "USB Audio Device" (ale to samo co generyczne USB!)
    # Ogolne wzorce - moga byc false-positive dla innych USB audio
    "USB Audio Device",
    "USB Audio",
]


def auto_detect_radio_audio() -> dict:
    """Automatycznie wykryj karte audio radia amatorskiego.

    Szuka wsrod kart w systemie takich ktore odpowiadaja typowym nazwom
    interfejsow radiowych (IC-7300, FT-991, TS-590 itp.). Zwraca:
        {
            "detected": bool,          # czy znaleziono karte radia
            "rx": str | None,          # nazwa karty do RX (capture)
            "tx": str | None,          # nazwa karty do TX (playback)
            "pattern": str | None,     # ktory wzorzec pasowal
            "all_rx": list[str],       # pelna lista dostepnych
            "all_tx": list[str],
        }

    Wykrywanie preferuje karty ktore MAJA i wejscie i wyjscie (typowe dla
    radia — jedna karta obsluguje oba kierunki). Jesli nie znajdzie takiej,
    dopuszcza rozne karty dla RX i TX.
    """
    devs = enumerate_audio_devices()
    rx_list = devs.get("rx", [])
    tx_list = devs.get("tx", [])

    # Strategia 1: znajdz karte ktora wystepuje w OBU listach (RX + TX)
    # — to typowy przypadek dla radia (jedna karta USB obsluguje oba)
    for pattern in _RADIO_CARD_PATTERNS:
        matches_rx = [d for d in rx_list if pattern.lower() in d.lower()]
        matches_tx = [d for d in tx_list if pattern.lower() in d.lower()]
        # Znajdz karte ktora jest zarowno na RX jak i TX (moze byc pod
        # rozna nazwa w PortAudio, np. "USB Audio CODEC" vs "USB Audio CODEC 1")
        for rx_dev in matches_rx:
            for tx_dev in matches_tx:
                # Prefer identyczne nazwy, ale zaakceptuj czesciowe dopasowanie
                if rx_dev == tx_dev or (
                    pattern.lower() in rx_dev.lower() and
                    pattern.lower() in tx_dev.lower()
                ):
                    return {
                        "detected": True,
                        "rx": rx_dev,
                        "tx": tx_dev,
                        "pattern": pattern,
                        "all_rx": rx_list,
                        "all_tx": tx_list,
                    }

    # Strategia 2: dowolne dopasowanie (nawet gdy tylko na RX lub tylko na TX)
    for pattern in _RADIO_CARD_PATTERNS:
        rx_match = next((d for d in rx_list if pattern.lower() in d.lower()), None)
        tx_match = next((d for d in tx_list if pattern.lower() in d.lower()), None)
        if rx_match or tx_match:
            return {
                "detected": True,
                "rx": rx_match,
                "tx": tx_match,
                "pattern": pattern,
                "all_rx": rx_list,
                "all_tx": tx_list,
            }

    return {
        "detected": False,
        "rx": None,
        "tx": None,
        "pattern": None,
        "all_rx": rx_list,
        "all_tx": tx_list,
    }
