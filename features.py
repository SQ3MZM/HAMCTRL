#!/usr/bin/env python3
"""
features.py — the central registry of radio features shown as buttons in
the panel ("Feature panel" below the PTT window, above the waterfall).

Each feature has:
  - id      : the key used in the API/config (e.g. "split")
  - label   : the Polish-language UI label (NOTE: kept in Polish here
              deliberately — it's data displayed straight to the UI, not
              a comment; do not translate as part of the backend English
              translation pass, see backend_english_translation memory)
  - icon    : icon name (emoji as fallback, the frontend can map it to svg)
  - group   : UI grouping ("vfo", "mode", "tx", "scope", "advanced")
  - backend : "civ" | "rigcat" | "both" — which backend must support this feature

Flow:
  1. CivRig/RigCAT.get_capabilities() -> dict {feature_id: bool}
     "what the radio can TECHNICALLY do" (based on the model profile / dump_caps)
  2. config.json["rigs"][i]["enabledFeatures"] -> dict {feature_id: bool}
     "what the admin ENABLED for users" (a whitelist, edited in the admin panel)
  3. effective_features() combines both: a feature is active in the user's
     UI ONLY when capabilities[id]==True AND enabledFeatures[id]==True
  4. webapp.py checks effective_features() before performing an action
     (except for the admin, who has access to everything the radio supports)
"""

# Registry of every possible feature — a single source of truth.
# The default enabled_default = True means a newly detected feature
# (supported by the radio) is VISIBLE to users by default, until the admin
# disables it. Features marked enabled_default=False require explicit
# admin approval (e.g. "risky" features like changing TX power).
FEATURES = [
    # ── VFO / frequency ───────────────────────────────────────────────────
    {"id": "freq_set",  "label": "Zmiana czestotliwosci", "icon": "📻",
     "group": "vfo", "backend": "both", "enabled_default": True},

    {"id": "mode_set",  "label": "Zmiana trybu pracy (USB/CW/...)", "icon": "🎛️",
     "group": "mode", "backend": "both", "enabled_default": True},

    {"id": "split",     "label": "Split VFO A/B", "icon": "⇄",
     "group": "vfo", "backend": "both", "enabled_default": True},

    {"id": "rit",       "label": "RIT / XIT", "icon": "🎯",
     "group": "vfo", "backend": "both", "enabled_default": False},

    # ── TX / PTT ─────────────────────────────────────────────────────────
    {"id": "ptt",       "label": "PTT (nadawanie)", "icon": "🎙️",
     "group": "tx", "backend": "both", "enabled_default": True},

    {"id": "tx_power",  "label": "Regulacja mocy TX", "icon": "⚡",
     "group": "tx", "backend": "both", "enabled_default": False},

    # ── Tuner (dodane 2026-09-03 - byly wczesniej bez wlasnego wpisu,
    # widocznosc przypadkowo podpieta pod mode_set/ptt) ────────────────────
    {"id": "tuner",     "label": "Tuner (ATU) wl/wyl", "icon": "⚙️",
     "group": "tuner", "backend": "both", "enabled_default": True},

    {"id": "autotune",  "label": "Autotune (generuje TX)", "icon": "🔧",
     "group": "tuner", "backend": "civ", "enabled_default": True},

    # ── Receive / S-meter / scope ───────────────────────────────────────────
    {"id": "smeter",    "label": "Wskaznik S-metr", "icon": "📶",
     "group": "scope", "backend": "both", "enabled_default": True},

    {"id": "scope",     "label": "Waterfall / Scope", "icon": "🌊",
     "group": "scope", "backend": "civ", "enabled_default": True},

    # ── Other ──────────────────────────────────────────────────────────────
    {"id": "memory",    "label": "Kanaly pamieci", "icon": "💾",
     "group": "advanced", "backend": "both", "enabled_default": False},

    {"id": "dstar",     "label": "D-STAR", "icon": "🛰️",
     "group": "advanced", "backend": "civ", "enabled_default": False},
]

FEATURES_BY_ID = {f["id"]: f for f in FEATURES}


def default_enabled_features() -> dict:
    """The default admin whitelist — used when config.json doesn't yet have
    an enabledFeatures section for a given radio (first run)."""
    return {f["id"]: f["enabled_default"] for f in FEATURES}


def effective_features(capabilities: dict, enabled_features: dict | None = None) -> dict:
    """
    Combine "what the radio can do" (capabilities) with "what the admin
    enabled" (enabled_features). Returns dict {feature_id: bool} — True =
    the feature is visible/active for the user.

    capabilities can be:
    - old format: {feature_id: bool}  (compatibility)
    - new format: {"raw_caps": {...}, "actions": [...], "sliders": [...]}
      — in this case only raw_caps is used for the static FEATURES

    If enabled_features is None, use default_enabled_features().
    Features absent from capabilities are treated as False (radio doesn't support them).
    """
    if enabled_features is None:
        enabled_features = default_enabled_features()

    raw_caps = capabilities.get("raw_caps", capabilities) if isinstance(capabilities, dict) else {}

    out = {}
    for f in FEATURES:
        fid = f["id"]
        supported = bool(raw_caps.get(fid, False))
        admin_on  = bool(enabled_features.get(fid, f["enabled_default"]))
        out[fid] = supported and admin_on
    return out


def effective_dynamic(capabilities: dict, enabled_dynamic: dict | None = None) -> dict:
    """
    Filter dynamic actions/sliders (from hamlib_caps.discover_capabilities)
    against the admin whitelist (enabled_dynamic = {dynamic_id: bool}).

    By default ALL detected dynamic elements are ENABLED (enabled_default=True)
    — the admin must manually DISABLE the ones that shouldn't be visible to
    users. This is the opposite logic from the static FEATURES (there the
    default depends on enabled_default), because dynamic elements are
    generally safe audio adjustments/radio features.

    Returns {"actions": [...], "sliders": [...]} — only elements with
    enabled_dynamic.get(id, True) == True.
    """
    if enabled_dynamic is None:
        enabled_dynamic = {}

    actions = [a for a in capabilities.get("actions", [])
               if enabled_dynamic.get(a["id"], True)]
    sliders = [s for s in capabilities.get("sliders", [])
               if enabled_dynamic.get(s["id"], True)]
    return {"actions": actions, "sliders": sliders}


def features_for_admin(capabilities: dict, enabled_features: dict | None = None) -> list:
    """
    Return the feature list for the admin panel — each with its metadata + 3 flags:
      - supported : whether the radio technically supports it (capabilities)
      - enabled   : whether the admin enabled it (enabled_features) — INFORMATIONAL
                     ONLY when supported=False (the admin can still prepare
                     the config ahead of time, e.g. before connecting another radio)
      - effective : supported AND enabled — whether it's actually visible to the user

    The admin can edit 'enabled' even when supported=False (e.g. preparing
    the config for a radio that will be connected later), but the toggle
    will be visually greyed out in the UI with a "radio doesn't support this" label.
    """
    if enabled_features is None:
        enabled_features = default_enabled_features()

    raw_caps = capabilities.get("raw_caps", capabilities) if isinstance(capabilities, dict) else {}

    out = []
    for f in FEATURES:
        fid = f["id"]
        supported = bool(raw_caps.get(fid, False))
        enabled   = bool(enabled_features.get(fid, f["enabled_default"]))
        out.append({
            **f,
            "supported": supported,
            "enabled":   enabled,
            "effective": supported and enabled,
        })
    return out


def dynamic_for_admin(capabilities: dict, enabled_dynamic: dict | None = None) -> dict:
    """
    Return dynamic actions/sliders with an 'enabled' flag (for the admin
    panel) — each element + {"enabled": bool}. Defaults to enabled=True for
    every detected element (the admin can disable specific ones).
    """
    if enabled_dynamic is None:
        enabled_dynamic = {}

    actions = [{**a, "enabled": enabled_dynamic.get(a["id"], True)}
               for a in capabilities.get("actions", [])]
    sliders = [{**s, "enabled": enabled_dynamic.get(s["id"], True)}
               for s in capabilities.get("sliders", [])]
    return {"actions": actions, "sliders": sliders}
