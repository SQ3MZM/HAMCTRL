#!/usr/bin/env python3
"""
features.py — centralny rejestr funkcji radia widocznych jako przyciski w panelu
("Panel funkcji" pod oknem PTT, nad waterfallem).

Kazda funkcja ma:
  - id      : klucz uzywany w API/config (np. "split")
  - label   : etykieta po polsku do UI
  - icon    : nazwa ikony (emoji jako fallback, frontend moze zmapowac na svg)
  - group   : grupowanie w UI ("vfo", "mode", "tx", "scope", "advanced")
  - backend : "civ" | "rigcat" | "both" — ktory backend musi wspierac te funkcje

Przeplyw:
  1. CivRig/RigCAT.get_capabilities() -> dict {feature_id: bool}
     "co radio TECHNICZNIE umie" (na podstawie profilu modelu / dump_caps)
  2. config.json["rigs"][i]["enabledFeatures"] -> dict {feature_id: bool}
     "co admin WLACZYL dla uzytkownikow" (whitelist, edytowana w panelu admina)
  3. effective_features() laczy oba: funkcja jest aktywna w UI uzytkownika
     TYLKO gdy capabilities[id]==True ORAZ enabledFeatures[id]==True
  4. webapp.py sprawdza effective_features() przed wykonaniem akcji
     (oprocz admina, ktory ma dostep do wszystkiego co radio wspiera)
"""

# Rejestr wszystkich mozliwych funkcji — jedno miejsce prawdy.
# Domyslna wartosc enabled_default = True oznacza ze nowo wykryta funkcja
# (wspierana przez radio) jest domyslnie WIDOCZNA dla userow, dopoki admin
# jej nie wylaczy. Funkcje oznaczone enabled_default=False wymagaja
# recznej akceptacji admina (np. funkcje "ryzykowne" jak zmiana mocy).
FEATURES = [
    # ── VFO / czestotliwosc ──────────────────────────────────────────────────
    {"id": "freq_set",  "label": "Zmiana czestotliwosci", "icon": "📻",
     "group": "vfo", "backend": "both", "enabled_default": True},

    {"id": "mode_set",  "label": "Zmiana trybu pracy (USB/CW/...)", "icon": "🎛️",
     "group": "mode", "backend": "both", "enabled_default": True},

    {"id": "split",     "label": "Split VFO A/B", "icon": "⇄",
     "group": "vfo", "backend": "both", "enabled_default": True},

    {"id": "rit",       "label": "RIT / XIT", "icon": "🎯",
     "group": "vfo", "backend": "both", "enabled_default": False},

    # ── TX / PTT ────────────────────────────────────────────────────────────
    {"id": "ptt",       "label": "PTT (nadawanie)", "icon": "🎙️",
     "group": "tx", "backend": "both", "enabled_default": True},

    {"id": "tx_power",  "label": "Regulacja mocy TX", "icon": "⚡",
     "group": "tx", "backend": "both", "enabled_default": False},

    {"id": "tuner",     "label": "Tuner (ATU) wl/wyl", "icon": "⚙️",
     "group": "tuner", "backend": "both", "enabled_default": True},

    {"id": "autotune",  "label": "Autotune (generuje TX)", "icon": "🔧",
     "group": "tuner", "backend": "civ", "enabled_default": True},

    # ── Odbior / S-metr / scope ─────────────────────────────────────────────
    {"id": "smeter",    "label": "Wskaznik S-metr", "icon": "📶",
     "group": "scope", "backend": "both", "enabled_default": True},

    {"id": "scope",     "label": "Waterfall / Scope", "icon": "🌊",
     "group": "scope", "backend": "civ", "enabled_default": True},

    # ── Inne ────────────────────────────────────────────────────────────────
    {"id": "memory",    "label": "Kanaly pamieci", "icon": "💾",
     "group": "advanced", "backend": "both", "enabled_default": False},

    {"id": "dstar",     "label": "D-STAR", "icon": "🛰️",
     "group": "advanced", "backend": "civ", "enabled_default": False},
]

FEATURES_BY_ID = {f["id"]: f for f in FEATURES}


def default_enabled_features() -> dict:
    """Domyslna whitelista admina — uzywana gdy config.json nie ma jeszcze
    sekcji enabledFeatures dla danego radia (pierwsze uruchomienie)."""
    return {f["id"]: f["enabled_default"] for f in FEATURES}


def effective_features(capabilities: dict, enabled_features: dict | None = None) -> dict:
    """
    Polacz "co radio umie" (capabilities) z "co admin wlaczyl" (enabled_features).
    Zwraca dict {feature_id: bool} — True = funkcja widoczna/aktywna dla usera.

    capabilities moze byc:
    - stary format: {feature_id: bool}  (kompatybilnosc)
    - nowy format: {"raw_caps": {...}, "actions": [...], "sliders": [...]}
      — w tym przypadku uzywany jest tylko raw_caps dla statycznych FEATURES

    Jesli enabled_features is None, uzyj default_enabled_features().
    Funkcje nieobecne w capabilities sa traktowane jako False (radio nie wspiera).
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
    Filtruj dynamiczne actions/sliders (z hamlib_caps.discover_capabilities)
    wedlug whitelisty admina (enabled_dynamic = {dynamic_id: bool}).

    Domyslnie WSZYSTKIE wykryte dynamiczne elementy sa WLACZONE (enabled_default=True)
    — admin musi recznie WYLACZYC te ktore nie powinny byc widoczne dla userow.
    To odwrotna logika niz statyczne FEATURES (tam default zalezy od enabled_default),
    bo dynamiczne elementy to z reguly bezpieczne regulacje audio/funkcje radia.

    Zwraca {"actions": [...], "sliders": [...]} — tylko elementy z
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
    Zwroc liste funkcji do panelu admina — kazda z metadanymi + 3 flagami:
      - supported : czy radio technicznie wspiera (capabilities)
      - enabled   : czy admin wlaczyl (enabled_features) — TYLKO informacyjnie
                     gdy supported=False (admin moze i tak przygotowac config
                     na przyszlosc, np. przed podlaczeniem innego radia)
      - effective : supported AND enabled — czy faktycznie widoczne dla usera

    Admin moze edytowac 'enabled' nawet dla supported=False (np. przygotowanie
    konfiguracji dla radia ktore bedzie podlaczone pozniej), ale przelacznik
    bedzie wizualnie wyszarzony w UI z etykieta "radio nie wspiera".
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
    Zwroc dynamiczne actions/sliders z flaga 'enabled' (dla panelu admina) —
    kazdy element + {"enabled": bool}. Domyslnie enabled=True dla wszystkich
    wykrytych elementow (admin moze wylaczyc konkretne).
    """
    if enabled_dynamic is None:
        enabled_dynamic = {}

    actions = [{**a, "enabled": enabled_dynamic.get(a["id"], True)}
               for a in capabilities.get("actions", [])]
    sliders = [{**s, "enabled": enabled_dynamic.get(s["id"], True)}
               for s in capabilities.get("sliders", [])]
    return {"actions": actions, "sliders": sliders}
