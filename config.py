#!/usr/bin/env python3
"""
config.py — configuration, paths, environment variables, and static data.
No dependency on the rest of the project's modules (stdlib only).
"""
import os, sys, hashlib
from pathlib import Path

# ── Paths (PyInstaller-aware) ─────────────────────────────────────────────────
# The code and frontend (public/, rigs/) are READ-ONLY and can be bundled
# into the EXE. User data (config.json, users.json, .env, logs) MUST be
# persistent and writable - next to the EXE, not in PyInstaller's temp folder.
#
# Environment detection:
#   - PyInstaller onefile: sys.frozen=True, static data in sys._MEIPASS,
#     but the EXE physically lives at sys.executable (that's where we keep user data).
#   - Regular Python: everything next to config.py.

def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _writable_data_dir() -> "Path":
    """
    Choose a PERSISTENT, WRITABLE directory for user data (config, users, .env, logs).

    Logic:
      1. Dev mode (not frozen): next to config.py.
      2. Frozen (EXE):
         a) If the directory next to the EXE is writable (e.g. EXE on the
            desktop, in Downloads, portable) -> use next to the EXE (simple, portable).
         b) If NOT (e.g. installed in Program Files, read-only) ->
            use %APPDATA%\\HAMCTRL (Windows) / ~/.hamctrl (other OSes).

    This lets the same EXE work both as a portable app (data alongside it)
    and as an install in Program Files (data in APPDATA).
    """
    if not _is_frozen():
        return Path(__file__).parent

    exe_dir = Path(sys.executable).parent

    # CRITICAL (data loss on update): for an install in Program Files,
    # ALWAYS use APPDATA — no writability test. The "can I write next to
    # the EXE" test passes when the server is run AS ADMINISTRATOR, which
    # meant qso.db/users.json/config loaded FROM Program Files and every
    # update/uninstall deleted them ("installing a new version wipes the
    # log"). Portable mode (data next to the EXE) only applies OUTSIDE
    # Program Files.
    _pf = [os.environ.get("ProgramFiles", r"C:\Program Files"),
           os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
    _in_program_files = any(
        str(exe_dir).lower().startswith(str(p).lower())
        for p in _pf if p
    )

    if not _in_program_files:
        # Test whether the directory next to the EXE is writable (portable mode)
        try:
            _test = exe_dir / ".write_test"
            _test.write_text("x", encoding="utf-8")
            _test.unlink()
            return exe_dir  # writable - portable mode
        except Exception:
            pass  # read-only - fall back to APPDATA

    # Application data directory
    appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if appdata:
        d = Path(appdata) / "HAMCTRL"
    else:
        d = Path.home() / ".hamctrl"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = Path.home()  # last-resort fallback

    # RESCUE MIGRATION: if old data is sitting NEXT TO the EXE (a side
    # effect of an earlier bug: running as admin -> writing to Program
    # Files succeeded), move it to APPDATA before an update/uninstall
    # deletes it. We only copy a file if it does NOT already exist in
    # APPDATA (never overwrite newer data with older data).
    try:
        import shutil as _sh
        # qso.db-wal / -shm are SQLite's WAL-mode companion files —
        # they hold the MOST RECENT transactions, not yet merged into the
        # main database. Without them, the migration would lose the most
        # recently logged QSOs.
        for _fn in ("qso.db", "qso.db-wal", "qso.db-shm",
                    "users.json", "config.json", ".env"):
            _src = exe_dir / _fn
            _dst = d / _fn
            if _src.exists() and not _dst.exists():
                _sh.copy2(str(_src), str(_dst))
                print(f"[config] Data migration: {_fn} (next to EXE -> APPDATA)",
                      flush=True)
        # DIRECTORIES: the Let's Encrypt certificate (letsencrypt/) and the
        # tunnel (cloudflared/) also used to live next to the EXE — without
        # migrating them the cert "disappeared" (the code looks in
        # DATA=APPDATA, but the files stayed in Program Files).
        for _dn in ("letsencrypt", "cloudflared", "logs"):
            _src = exe_dir / _dn
            _dst = d / _dn
            if _src.is_dir() and not _dst.exists():
                try:
                    _sh.copytree(str(_src), str(_dst))
                    print(f"[config] Directory migration: {_dn}/ "
                          f"(next to EXE -> APPDATA)", flush=True)
                except Exception as _de:
                    # The private key in Program Files may have been created
                    # by certbot as admin — reading it without admin rights
                    # may be blocked. A clear instruction instead of a silent failure.
                    print(f"[config] COULD NOT migrate {_dn}/ ({_de}). "
                          f"Copy it manually: '{_src}' -> '{_dst}' "
                          f"(e.g. as administrator).", flush=True)
    except Exception as _e:
        print(f"[config] Data migration skipped: {_e}", flush=True)

    return d


# BUNDLE = directory with read-only assets (public/, rigs/)
if _is_frozen():
    BUNDLE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    DATA = _writable_data_dir()
else:
    BUNDLE = Path(__file__).parent
    DATA = Path(__file__).parent

BASE   = DATA            # name kept for compatibility (user data)
PUBLIC = BUNDLE / "public"   # frontend - read-only, from the bundle
CFG_F  = DATA / "config.json"
USR_F  = DATA / "users.json"
LOG_F  = DATA / "qso_log.json"
ENV_F  = DATA / ".env"


def load_env():
    e = {}
    if ENV_F.exists():
        for line in ENV_F.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k, _, v = line.partition("=")
                e[k.strip()] = v.strip()
    return e


def ensure_env():
    """
    On first run (no .env), creates .env with SAFE values for this
    specific installation:

    - JWT_SECRET: RANDOM (secrets.token_hex) - each install has its own
      key. CRITICAL for a multi-club product: without this every
      installation would share the same key and tokens could be forged.
    - FIRST_RUN: 1 - a flag meaning the setup wizard hasn't run yet
      (forces the admin password change and station configuration).

    Returns the updated ENV dict. If .env already exists - leaves it alone.
    """
    import secrets
    if ENV_F.exists():
        return load_env()
    # First start - generate a fresh .env
    jwt_secret = secrets.token_hex(32)  # 256-bit random key
    lines = [
        "# HAM RADIO CTRL - installation configuration",
        "# Generated automatically on first run.",
        "# DO NOT SHARE this file - it contains this installation's JWT key.",
        "",
        f"JWT_SECRET={jwt_secret}",
        "FIRST_RUN=1",
        "",
    ]
    try:
        ENV_F.write_text("\n".join(lines), encoding="utf-8")
        print(f"[config] Created .env with a random JWT key (first start)",
              flush=True)
    except Exception as e:
        print(f"[config] WARNING: could not write .env: {e}", flush=True)
    return load_env()


ENV          = ensure_env()
PORT         = int(os.environ.get("PORT", ENV.get("PORT", 8000)))
CALLSIGN     = ENV.get("CALLSIGN", "SP0ABC")
LOCATOR      = ENV.get("STATION_LOCATOR", "KO02")
# SECRET always comes from .env (ensure_env guarantees it exists and is random).
# The fallback is only an emergency measure if writing .env failed - still
# random per-process (not a shared hardcoded value like the old "hamradio2025").
SECRET       = ENV.get("JWT_SECRET") or __import__("secrets").token_hex(32)
ADMIN_PW     = ENV.get("ADMIN_PASSWORD", "Admin1234!")
FIRST_RUN    = ENV.get("FIRST_RUN", "0") == "1"
# VERBOSE: chatty logs (radio status every 2s etc). Disabled by default -
# the product has a clean console. Enable via HAM_VERBOSE=1 in the
# environment or VERBOSE=1 in .env when diagnosing.
VERBOSE      = (os.environ.get("HAM_VERBOSE", ENV.get("VERBOSE", "0")) == "1")
HAMLIB       = ENV.get("HAMLIB_PATH", "rigctld")
HAMLIB_PORT  = int(ENV.get("HAMLIB_PORT", 4532))
RIGCTLD_LOG  = str(Path(__file__).resolve().parent / "rigctld.log")

MIME = {
    ".html": "text/html;charset=utf-8", ".css": "text/css",
    ".js": "application/javascript", ".json": "application/json",
    ".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml",
    ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
    ".mp3": "audio/mpeg", ".wav": "audio/wav",
}

# Models with a built-in spectroscope (scope) → direct CI-V mode (control + scope).
# The rest of the radios (IC-746 etc.) still go through rigctld/RigCAT — unchanged.
SCOPE_MODELS = {"3073", "3078", "3085", "3068", "3070", "3081"}  # IC-7300/7610/705/9100/7100/9700

HAMLIB_MODELS = {
    "Icom": [
        {"id":"3073","name":"IC-7300"},{"id":"3046","name":"IC-746 Pro"},
        {"id":"3023","name":"IC-746"},{"id":"3085","name":"IC-705"},
        {"id":"3078","name":"IC-7610"},{"id":"3070","name":"IC-7100"},
        {"id":"3060","name":"IC-7000"},{"id":"3068","name":"IC-9100"},
        {"id":"3081","name":"IC-9700"},
        {"id":"3011","name":"IC-706MkIIG"},
    ],
    "Yaesu": [
        {"id":"1035","name":"FT-991A"},{"id":"1034","name":"FT-991"},
        {"id":"1031","name":"FT-857D"},{"id":"1030","name":"FT-817"},
        {"id":"1066","name":"FT-DX10"},{"id":"1067","name":"FT-710"},
    ],
    "Kenwood": [
        {"id":"2014","name":"TS-590SG"},{"id":"2022","name":"TS-890S"},
        {"id":"2010","name":"TS-2000"},{"id":"2017","name":"TS-990S"},
    ],
    "Elecraft": [
        {"id":"2050","name":"K3"},{"id":"2051","name":"K3S"},
        {"id":"2054","name":"KX3"},{"id":"2053","name":"K4"},
    ],
    "Inne": [{"id":"2","name":"Symulacja (test)"}],
}
