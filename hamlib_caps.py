#!/usr/bin/env python3
"""
hamlib_caps.py — parser odpowiedzi rigctld na komende dump_caps ('1\\n'),
wyciagajacy SZCZEGOLOWA liste komend wspieranych przez radio (levels,
funkcje, VFO, mode), nie tylko proste bool capabilities.

Zwracana struktura (discover_capabilities):
{
  "actions": [                      # przyciski (VFO select, funkcje on/off)
    {"id": "vfo_a", "label": "VFO A", "group": "vfo", ...},
    {"id": "func_nb", "label": "NB (Noise Blanker)", "group": "func", ...},
    ...
  ],
  "sliders": [                      # Set level z zakresem -> slider
    {"id": "level_rfpower", "label": "RFPOWER", "min":0.05, "max":1.0,
     "step":0.0039, "group": "level", ...},
    ...
  ],
  "raw_caps": {feature_id: bool}    # stare proste bool (kompatybilnosc)
}

Filozofia: parser wyciaga WSZYSTKO co rigctld zglasza jako "Set X: Y" lub
liste z zakresem. webapp.py/admin decyduje co z tego pokazac (whitelist
per id). Brak rozpoznanego wzorca -> pomijamy linie (bezpieczny fallback).
"""
import asyncio
import re


# ── Etykiety dla znanych poziomow Hamlib (ladniejsze nazwy w UI) ─────────────
LEVEL_LABELS = {
    "PREAMP":      "Przedwzmacniacz (Preamp)",
    "ATT":         "Atenuator",
    "AF":          "Glosnosc (AF)",
    "RF":          "Wzmocnienie RF (RF Gain)",
    "SQL":         "Squelch",
    "APF":         "Audio Peak Filter (APF)",
    "NR":          "Redukcja szumow (NR)",
    "PBT_IN":      "Passband Tuning IN",
    "PBT_OUT":     "Passband Tuning OUT",
    "CWPITCH":     "Ton CW (Pitch)",
    "RFPOWER":     "Moc TX (RF Power)",
    "MICGAIN":     "Wzmocnienie mikrofonu",
    "KEYSPD":      "Szybkosc CW (WPM)",
    "COMP":        "Kompresja mikrofonu",
    "AGC":         "AGC",
    "BKINDL":      "Opoznienie break-in (BKINDL)",
    "RAWSTR":      "Surowy S-metr (RAWSTR)",
    "SWR":         "SWR",
    "ALC":         "ALC",
    "RFPOWER_METER": "Wskaznik mocy TX",
    "NOTCHF_RAW":  "Notch (czestotliwosc)",
    "RFPOWER_METER_WATTS": "Moc TX (W)",
    "AGC_TIME":    "Czas reakcji AGC",
    "VOXDELAY":    "Opoznienie VOX",
    "ANTIVOX":     "Anti-VOX",
}

# Poziomy ktore sa "tylko odczyt" / telemetria — nie generujemy slidera
# nawet jesli sa w "Set level" (rigctld czasem zglasza je tam blednie)
LEVEL_READONLY = {"RAWSTR", "SWR", "ALC", "RFPOWER_METER", "RFPOWER_METER_WATTS"}

# Etykiety dla funkcji (Get/Set functions)
FUNC_LABELS = {
    "NB":     "Noise Blanker (NB)",
    "COMP":   "Kompresor (COMP)",
    "VOX":    "VOX",
    "TONE":   "Tone (CTCSS TX)",
    "TSQL":   "Tone Squelch (TSQL)",
    "SBKIN":  "Semi break-in (CW)",
    "FBKIN":  "Full break-in (CW)",
    "ANF":    "Auto Notch Filter (ANF)",
    "NR":     "Noise Reduction (NR)",
    "APF":    "Audio Peak Filter (APF)",
    "MON":    "Monitor (sidetone TX)",
    "MN":     "Manual Notch",
    "RF":     "RF (Func)",
    "ARO":    "Auto Repeater Offset (ARO)",
    "RESUME": "Resume Scan",
    "LOCK":   "Blokada VFO (Lock)",
    "FAGC":   "Fast AGC",
}

# VFO -> etykiety przyciskow
VFO_LABELS = {
    "VFOA": "VFO A",
    "VFOB": "VFO B",
    "MEM":  "Pamiec (MEM)",
    "Main": "Main",
    "Sub":  "Sub",
}


# ── Stare proste bool capabilities (kompatybilnosc z rigs/features.py) ──────
_CAPS_PATTERNS = {
    "freq_set": [r"Can set Frequency:\s*Y"],
    "mode_set": [r"Can set Mode:\s*Y"],
    "ptt":      [r"Can set PTT:\s*Y"],
    "split":    [r"Can set Split (Freq|VFO):\s*Y"],
    "rit":      [r"Can set RIT:\s*Y", r"Can set XIT:\s*Y"],
    "smeter":   [r"Get level:.*\bRAWSTR\b", r"Get level:.*\bSTRENGTH\b"],
    "tx_power": [r"Set level:.*\bRFPOWER\b"],
    "memory":   [r"Can set Mem:\s*Y"],
    "vfo_ab":   [r"Can set VFO:\s*Y"],
    "scope":    [],
    "dstar":    [],
}


def _parse_level_list(line: str) -> list[dict]:
    """
    Sparsuj linie typu:
    'RFPOWER(0.050000..1.000000/0.003922) AF(0.000000..1.000000/0.003922) AGC(0..0/0)'
    -> lista dict {name, min, max, step}
    """
    out = []
    for m in re.finditer(r"(\w+)\(([-\d.]+)\.\.([-\d.]+)/([-\d.]+)\)", line):
        name, lo, hi, step = m.groups()
        try:
            lo_f, hi_f, step_f = float(lo), float(hi), float(step)
        except ValueError:
            continue
        out.append({"name": name, "min": lo_f, "max": hi_f, "step": step_f})
    return out


def _parse_word_list(line: str) -> list[str]:
    """Sparsuj liste slow po dwukropku, np. 'VFO list: VFOA VFOB MEM' -> ['VFOA','VFOB','MEM']"""
    if ":" not in line:
        return []
    rhs = line.split(":", 1)[1].strip()
    return rhs.split() if rhs else []


def parse_dump_caps(text: str) -> dict:
    """
    Pelny parser dump_caps -> {"actions": [...], "sliders": [...], "raw_caps": {...}}
    """
    if not text:
        return {"actions": [], "sliders": [], "raw_caps": {}}

    actions = []
    sliders = []

    # ── proste bool capabilities (jak dawniej) ──
    raw_caps = {}
    for feature_id, patterns in _CAPS_PATTERNS.items():
        if not patterns:
            raw_caps[feature_id] = False
            continue
        raw_caps[feature_id] = any(
            re.search(pat, text, re.IGNORECASE | re.MULTILINE) for pat in patterns
        )

    can_set_vfo   = bool(re.search(r"Can set VFO:\s*Y", text, re.IGNORECASE))
    can_set_func  = bool(re.search(r"Can set Func:\s*Y", text, re.IGNORECASE))
    can_set_level = bool(re.search(r"Can set Level:\s*Y", text, re.IGNORECASE))

    for raw_line in text.splitlines():
        line = raw_line.strip()

        # ── VFO list -> przyciski VFOA/VFOB/... ──
        if line.startswith("VFO list:") and can_set_vfo:
            for vfo in _parse_word_list(line):
                if vfo == "MEM":
                    continue  # pamiec obslugiwana osobno (feature 'memory')
                label = VFO_LABELS.get(vfo, vfo)
                aid = f"vfo_{vfo.lower().replace('vfo','')}"
                if not any(a["id"] == aid for a in actions):
                    actions.append({
                        "id": aid, "label": label, "group": "vfo",
                        "kind": "vfo_select", "value": vfo,
                    })

        # ── Set functions -> przyciski toggle (NB/COMP/VOX/...) ──
        if line.startswith("Set functions:") and can_set_func:
            for func in _parse_word_list(line):
                label = FUNC_LABELS.get(func, func)
                aid = f"func_{func.lower()}"
                actions.append({
                    "id": aid, "label": label, "group": "func",
                    "kind": "func_toggle", "value": func,
                })

        # ── Set level -> slidery ──
        if line.startswith("Set level:") and can_set_level:
            for lvl in _parse_level_list(line):
                name = lvl["name"]
                if name in LEVEL_READONLY:
                    continue
                if lvl["min"] == 0 and lvl["max"] == 0:
                    continue  # zero-range = no-op w tym backendzie Hamlib
                label = LEVEL_LABELS.get(name, name)
                sliders.append({
                    "id": f"level_{name.lower()}", "label": label, "group": "level",
                    "kind": "set_level", "param": name,
                    "min": lvl["min"], "max": lvl["max"], "step": lvl["step"],
                })

    return {"actions": actions, "sliders": sliders, "raw_caps": raw_caps}


async def fetch_dump_caps(hamlib_port: int, timeout: float = 3.0) -> str:
    """
    Polacz sie z dzialajacym rigctld i wyslij komende dump_caps ('1').
    Zwraca surowy tekst odpowiedzi (moze byc wieloliniowy, kilka KB).
    Pusty string przy bledzie/braku polaczenia.
    """
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", hamlib_port), timeout=timeout)
    except Exception as e:
        print(f"[hamlib_caps] polaczenie nieudane: {e}")
        return ""

    try:
        w.write(b"1\n")
        await w.drain()
        chunks = []
        while True:
            try:
                chunk = await asyncio.wait_for(r.read(4096), timeout=0.5)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            chunks.append(chunk)
            if len(chunks) > 50:  # bezpiecznik ~200KB
                break
        return b"".join(chunks).decode(errors="replace")
    except Exception as e:
        print(f"[hamlib_caps] read blad: {e}")
        return ""
    finally:
        try:
            w.close()
        except Exception:
            pass


async def discover_capabilities(hamlib_port: int) -> dict:
    """
    Polacz z rigctld, pobierz dump_caps, sparsuj -> pelna struktura
    {"actions": [...], "sliders": [...], "raw_caps": {...}}.
    Pusta struktura przy bledzie/braku odpowiedzi.
    """
    text = await fetch_dump_caps(hamlib_port)
    if not text:
        print("[hamlib_caps] brak odpowiedzi dump_caps — capabilities puste (fallback)")
        return {"actions": [], "sliders": [], "raw_caps": {}}

    result = parse_dump_caps(text)
    print(f"[hamlib_caps] wykryto {len(result['actions'])} akcji, "
          f"{len(result['sliders'])} sliderow, "
          f"raw_caps={', '.join(k for k,v in result['raw_caps'].items() if v) or '(brak)'}")
    return result


async def get_rigctld_capabilities(hamlib_port: int) -> dict:
    """
    Kompatybilnosc wstecz — zwraca tylko raw_caps (proste bool dict)
    dla istniejacego kodu w rigs/features.py / webapp.py.
    """
    full = await discover_capabilities(hamlib_port)
    return full["raw_caps"]
