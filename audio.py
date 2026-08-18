#!/usr/bin/env python3
"""
audio.py — system sound card detection (rx=inputs, tx=outputs).
"""
import sys

# Cache for sounddevice availability — the import is tried ONCE, not on
# every call. Without this, enumerate_audio_devices() kept retrying the
# sounddevice import and printing an error on every failed attempt,
# flooding the log (dozens of "[audio] sounddevice unavailable" lines on
# every status refresh).
# None = not checked yet; False = unavailable; module = available.
_SD_CACHED = None


def _get_sounddevice():
    """Returns the sounddevice module if available, otherwise None. The
    import is only tried ONCE — the result is cached, so a missing module
    doesn't spam the log."""
    global _SD_CACHED
    if _SD_CACHED is None:
        try:
            import sounddevice as _sd
            _SD_CACHED = _sd
        except Exception as e:
            print(f"[audio] sounddevice unavailable "
                  f"({type(e).__name__}) — using native card detection")
            _SD_CACHED = False
    return _SD_CACHED or None


def _enumerate_audio_winmm() -> dict:
    """Fallback (Windows): waveIn/waveOut via ctypes — no dependencies.
    Names limited to ~31 characters (MME limit)."""
    import ctypes
    from ctypes import wintypes
    winmm = ctypes.windll.winmm
    # ABI: the device id is UINT_PTR (c_size_t) — matters on 64-bit Windows.
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
    """System sound cards: rx=inputs (capture), tx=outputs (render).
    1) sounddevice (PortAudio) — full names, distinguishes in/out.
    2) Windows: winmm (ctypes) — dependency-free fallback.
    Returns {'rx':[...], 'tx':[...], 'source':...}."""
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
            print(f"[audio] sounddevice query error: {type(e).__name__}: {e}")

    if sys.platform == "win32":
        try:
            return _enumerate_audio_winmm()
        except Exception as e:
            print(f"[audio] winmm fallback error: {type(e).__name__}: {e}")

    return {"rx": rx, "tx": tx, "source": "none"}


# Name patterns for sound cards typical of ham radio interfaces. Order
# matters — most specific to most general.
_RADIO_CARD_PATTERNS = [
    # Icom - IC-7300, IC-705, IC-7610, IC-9700 usually expose "USB Audio CODEC"
    "USB Audio CODEC",
    # Yaesu - FT-991, FT-DX10, FT-710 expose "USB AUDIO CODEC" (uppercase) or "SCU-17"
    "USB AUDIO CODEC",
    "SCU-17",
    # Kenwood - TS-590 exposes "USB Audio Device" (but that's the same as the generic USB one!)
    # Generic patterns - may false-positive on other USB audio devices
    "USB Audio Device",
    "USB Audio",
]


def auto_detect_radio_audio() -> dict:
    """Automatically detect the ham radio's sound card.

    Searches the system's sound cards for ones matching typical radio
    interface names (IC-7300, FT-991, TS-590, etc.). Returns:
        {
            "detected": bool,          # whether a radio card was found
            "rx": str | None,          # card name for RX (capture)
            "tx": str | None,          # card name for TX (playback)
            "pattern": str | None,     # which pattern matched
            "all_rx": list[str],       # full list of available ones
            "all_tx": list[str],
        }

    Detection prefers cards that HAVE both input and output (typical for a
    radio — one card handles both directions). If none is found, it falls
    back to accepting different cards for RX and TX.
    """
    devs = enumerate_audio_devices()
    rx_list = devs.get("rx", [])
    tx_list = devs.get("tx", [])

    # Strategy 1: find a card present in BOTH lists (RX + TX)
    # — the typical case for a radio (one USB card handles both)
    for pattern in _RADIO_CARD_PATTERNS:
        matches_rx = [d for d in rx_list if pattern.lower() in d.lower()]
        matches_tx = [d for d in tx_list if pattern.lower() in d.lower()]
        # Find a card that appears on both RX and TX (it may show up under
        # a different name in PortAudio, e.g. "USB Audio CODEC" vs "USB Audio CODEC 1")
        for rx_dev in matches_rx:
            for tx_dev in matches_tx:
                # Prefer identical names, but accept a partial match too
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

    # Strategy 2: any match at all (even if only on RX or only on TX)
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
