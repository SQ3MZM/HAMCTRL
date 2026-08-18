#!/usr/bin/env python3
"""
hamlib_caps.py — parser for rigctld's response to the dump_caps command
('1\\n'), extracting a DETAILED list of commands the radio supports
(levels, functions, VFO, mode), not just simple bool capabilities.

Returned structure (discover_capabilities):
{
  "actions": [                      # buttons (VFO select, function on/off)
    {"id": "vfo_a", "label": "VFO A", "group": "vfo", ...},
    {"id": "func_nb", "label": "NB (Noise Blanker)", "group": "func", ...},
    ...
  ],
  "sliders": [                      # Set level with a range -> slider
    {"id": "level_rfpower", "label": "RFPOWER", "min":0.05, "max":1.0,
     "step":0.0039, "group": "level", ...},
    ...
  ],
  "raw_caps": {feature_id: bool}    # legacy simple bool (compatibility)
}

Philosophy: the parser extracts EVERYTHING rigctld reports as "Set X: Y"
or a list with a range. webapp.py/admin decides what to show (a per-id
whitelist). No recognized pattern -> the line is skipped (safe fallback).
"""
import asyncio
import re


# ── Labels for known Hamlib levels (universal PL/EN abbreviations) ─────────
# NOTE: label VALUES below are UI text sent straight to the frontend
# (radiofunctions.js renderSliders/makeTile, truncated to 14 chars) - kept
# short and language-neutral like ALC/PWR/SWR, deliberately NOT translated
# as part of the backend English pass (see backend_english_translation memory).
LEVEL_LABELS = {
    "PREAMP":      "PREAMP",
    "ATT":         "ATT",
    "AF":          "AF",
    "RF":          "RF GAIN",
    "SQL":         "SQL",
    "APF":         "APF",
    "NR":          "NR",
    "PBT_IN":      "PBT IN",
    "PBT_OUT":     "PBT OUT",
    "CWPITCH":     "CW PITCH",
    "RFPOWER":     "RF PWR",
    "MICGAIN":     "MIC GAIN",
    "KEYSPD":      "KEYSPD",
    "COMP":        "COMP",
    "AGC":         "AGC",
    "BKINDL":      "BK-IN DELAY",
    "RAWSTR":      "RAWSTR",
    "SWR":         "SWR",
    "ALC":         "ALC",
    "RFPOWER_METER": "PWR METER",
    "NOTCHF_RAW":  "NOTCH",
    "RFPOWER_METER_WATTS": "PWR (W)",
    "AGC_TIME":    "AGC TIME",
    "VOXDELAY":    "VOX DELAY",
    "ANTIVOX":     "ANTI-VOX",
}

# Levels that are "read-only" / telemetry — we don't generate a slider for
# them even if they show up in "Set level" (rigctld sometimes misreports them there)
LEVEL_READONLY = {"RAWSTR", "SWR", "ALC", "RFPOWER_METER", "RFPOWER_METER_WATTS"}

# Labels for functions (Get/Set functions). Format "ABBREV (description)" —
# the frontend truncates at '(' on the button itself, so only the universal
# abbreviation shows; the full (Polish) description stays in the title
# (tooltip). NOTE: label values below are UI text, deliberately NOT
# translated — see the note above LEVEL_LABELS. Same convention applies to
# rigs/civ_profiles.py::_FUNC_LABELS (the other path: direct CI-V).
FUNC_LABELS = {
    "NB":     "NB (Noise Blanker)",
    "COMP":   "COMP (Kompresor)",
    "VOX":    "VOX",
    "TONE":   "TONE (Tone CTCSS TX)",
    "TSQL":   "TSQL (Tone Squelch)",
    "SBKIN":  "SBKIN (Semi break-in CW)",
    "FBKIN":  "FBKIN (Full break-in CW)",
    "ANF":    "ANF (Auto Notch Filter)",
    "NR":     "NR (Redukcja szumow)",
    "APF":    "APF (Audio Peak Filter)",
    "MON":    "MON (Monitor sidetone TX)",
    "MN":     "MN (Manual Notch)",
    "RF":     "RF (Func)",
    "ARO":    "ARO (Auto Repeater Offset)",
    "RESUME": "RESUME (Resume Scan)",
    "LOCK":   "LOCK (Blokada VFO)",
    "FAGC":   "FAGC (Fast AGC)",
}

# VFO -> button labels (UI text, not translated - see note above LEVEL_LABELS)
VFO_LABELS = {
    "VFOA": "VFO A",
    "VFOB": "VFO B",
    "MEM":  "Pamiec (MEM)",
    "Main": "Main",
    "Sub":  "Sub",
}


# ── Legacy simple bool capabilities (compatibility with rigs/features.py) ───
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
    Parse a line like:
    'RFPOWER(0.050000..1.000000/0.003922) AF(0.000000..1.000000/0.003922) AGC(0..0/0)'
    -> a list of dicts {name, min, max, step}
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
    """Parse a list of words after a colon, e.g. 'VFO list: VFOA VFOB MEM' -> ['VFOA','VFOB','MEM']"""
    if ":" not in line:
        return []
    rhs = line.split(":", 1)[1].strip()
    return rhs.split() if rhs else []


def parse_dump_caps(text: str) -> dict:
    """
    Full dump_caps parser -> {"actions": [...], "sliders": [...], "raw_caps": {...}}
    """
    if not text:
        return {"actions": [], "sliders": [], "raw_caps": {}}

    actions = []
    sliders = []

    # ── simple bool capabilities (as before) ──
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

        # ── VFO list -> VFOA/VFOB/... buttons ──
        if line.startswith("VFO list:") and can_set_vfo:
            for vfo in _parse_word_list(line):
                if vfo == "MEM":
                    continue  # memory is handled separately (the 'memory' feature)
                label = VFO_LABELS.get(vfo, vfo)
                aid = f"vfo_{vfo.lower().replace('vfo','')}"
                if not any(a["id"] == aid for a in actions):
                    actions.append({
                        "id": aid, "label": label, "group": "vfo",
                        "kind": "vfo_select", "value": vfo,
                    })

        # ── Set functions -> toggle buttons (NB/COMP/VOX/...) ──
        if line.startswith("Set functions:") and can_set_func:
            for func in _parse_word_list(line):
                label = FUNC_LABELS.get(func, func)
                aid = f"func_{func.lower()}"
                actions.append({
                    "id": aid, "label": label, "group": "func",
                    "kind": "func_toggle", "value": func,
                })

        # ── Set level -> sliders ──
        if line.startswith("Set level:") and can_set_level:
            for lvl in _parse_level_list(line):
                name = lvl["name"]
                if name in LEVEL_READONLY:
                    continue
                if lvl["min"] == 0 and lvl["max"] == 0:
                    continue  # zero-range = no-op for this Hamlib backend
                label = LEVEL_LABELS.get(name, name)
                sliders.append({
                    "id": f"level_{name.lower()}", "label": label, "group": "level",
                    "kind": "set_level", "param": name,
                    "min": lvl["min"], "max": lvl["max"], "step": lvl["step"],
                })

    return {"actions": actions, "sliders": sliders, "raw_caps": raw_caps}


async def fetch_dump_caps(hamlib_port: int, timeout: float = 3.0) -> str:
    """
    Connect to a running rigctld and send the dump_caps command ('1').
    Returns the raw response text (can be multi-line, several KB).
    Empty string on error/no connection.
    """
    try:
        r, w = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", hamlib_port), timeout=timeout)
    except Exception as e:
        print(f"[hamlib_caps] connection failed: {e}")
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
            if len(chunks) > 50:  # safety cap, ~200KB
                break
        return b"".join(chunks).decode(errors="replace")
    except Exception as e:
        print(f"[hamlib_caps] read error: {e}")
        return ""
    finally:
        try:
            w.close()
        except Exception:
            pass


async def discover_capabilities(hamlib_port: int) -> dict:
    """
    Connect to rigctld, fetch dump_caps, parse it -> the full structure
    {"actions": [...], "sliders": [...], "raw_caps": {...}}.
    An empty structure on error/no response.
    """
    text = await fetch_dump_caps(hamlib_port)
    if not text:
        print("[hamlib_caps] no dump_caps response — capabilities empty (fallback)")
        return {"actions": [], "sliders": [], "raw_caps": {}}

    result = parse_dump_caps(text)
    print(f"[hamlib_caps] detected {len(result['actions'])} actions, "
          f"{len(result['sliders'])} sliders, "
          f"raw_caps={', '.join(k for k,v in result['raw_caps'].items() if v) or '(none)'}")
    return result


async def get_rigctld_capabilities(hamlib_port: int) -> dict:
    """
    Backward compatibility — returns only raw_caps (the simple bool dict)
    for existing code in rigs/features.py / webapp.py.
    """
    full = await discover_capabilities(hamlib_port)
    return full["raw_caps"]
