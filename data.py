#!/usr/bin/env python3
"""
data.py — JSON storage (config, users, QSO log) and default values.

STABILITY (2026-07-05):
- save_json writes ATOMICALLY (tmp + rename) and keeps a .bak copy.
  Without this, a crash/power loss mid-write corrupts users.json -> lost accounts.
- load_json falls back to .bak on a corrupted file and logs LOUDLY.
  It used to silently return the default (an empty user list!) with no warning.
"""
import json
import os
import shutil
import tempfile
from pathlib import Path
from config import CFG_F, USR_F, ENV, ADMIN_PW
from auth import hash_pw, hash_pw_secure


def load_json(path: Path, default):
    """
    Load JSON. Falls back to the .bak copy on a corrupted file.
    Errors are LOGGED (not silently ignored) - a corrupted users.json
    would mean losing every account with no warning.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[data] READ ERROR {path.name}: {e}", flush=True)

    # File corrupted - try the backup copy
    bak = path.with_suffix(path.suffix + ".bak")
    if bak.exists():
        try:
            data = json.loads(bak.read_text(encoding="utf-8"))
            print(f"[data] RECOVERED {path.name} from backup {bak.name}", flush=True)
            # Restore the main file from the backup
            try:
                shutil.copy2(bak, path)
            except Exception:
                pass
            return data
        except Exception as e:
            print(f"[data] backup {bak.name} is also corrupted: {e}", flush=True)

    # Keep the corrupted file for analysis (don't blindly overwrite it)
    try:
        broken = path.with_suffix(path.suffix + ".broken")
        shutil.copy2(path, broken)
        print(f"[data] corrupted file kept as {broken.name}", flush=True)
    except Exception:
        pass
    print(f"[data] WARNING: using default values for {path.name}!", flush=True)
    return default


def save_json(path: Path, data):
    """
    ATOMIC write: first to a temp file in the same directory, fsync, then
    an atomic os.replace(). This guarantees the target file is ALWAYS
    complete - a crash mid-write cannot corrupt it.

    Makes a .bak copy of the previous good version before replacing it.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)

    # Copy of the previous good version (if it exists and is non-empty)
    try:
        if path.exists() and path.stat().st_size > 0:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    except Exception as e:
        print(f"[data] could not create .bak for {path.name}: {e}", flush=True)

    tmp_path = None
    try:
        # The temp file MUST be on the same disk as the target,
        # otherwise os.replace won't be atomic.
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())   # force the write to disk (not just the OS cache)
        # Atomic replace - os.replace is atomic on both Windows and POSIX
        os.replace(tmp_path, path)
        tmp_path = None
    except Exception as e:
        print(f"[data] WRITE ERROR {path.name}: {e}", flush=True)
        raise
    finally:
        # Clean up the temp file if something went wrong
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


DEFAULT_MACROS = [
    {"id": 1, "label": "CQ CQ",  "text": "CQ CQ CQ DE $CALL $CALL K"},
    {"id": 2, "label": "CQ KR",  "text": "CQ CQ DE $CALL K"},
    {"id": 3, "label": "RST",    "text": "UR $RST $RST"},
    {"id": 4, "label": "QSO",    "text": "TKS QSO 73 DE $CALL SK"},
    {"id": 5, "label": "TU 73",  "text": "TU 73 DE $CALL SK"},
    {"id": 6, "label": "CALL",   "text": "$CALL"},
    {"id": 7, "label": "NR",     "text": "NR $NR"},
    {"id": 8, "label": "SK",     "text": "SK SK DE $CALL"},
]


def get_cfg() -> dict:
    c = load_json(CFG_F, {})
    if "lang" not in c:
        # Seed the server-wide default UI language from the marker the
        # installer wrote (see HAMCTRL-installer.iss) before config.json
        # existed at all. FIX: this plumbing was half-built before -
        # i18n.js already had a window.__HAM_INITIAL_LANG__ fallback with
        # a comment describing exactly this design, but nothing ever
        # actually wrote the marker or read it into config.json, so the
        # installer's language choice was silently discarded and the app
        # always started in Polish regardless of what was picked during
        # setup. Read once — a per-browser choice (localStorage) always
        # takes priority over this once a user picks a language explicitly,
        # so this is just the install-time default for a fresh browser.
        lang = "pl"
        try:
            marker = CFG_F.parent / "install_lang.txt"
            if marker.exists():
                val = marker.read_text(encoding="utf-8").strip().lower()
                if val in ("pl", "en"):
                    lang = val
        except Exception:
            pass
        c["lang"] = lang
        try:
            save_json(CFG_F, c)  # persist immediately - don't depend on the marker file surviving
        except Exception:
            pass
    if "rigs" not in c:
        c["rigs"] = [{"id": "1", "name": "IC-7300",
                      "model": ENV.get("RIG1_MODEL", "3073"),
                      "port":  ENV.get("RIG1_PORT",  "COM3"),
                      "speed": ENV.get("RIG1_SPEED", "19200"),
                      "civAddr": ENV.get("RIG1_CIV", "0x94"),
                      "active": True}]
    if "rotators"  not in c: c["rotators"]  = []
    if "cwMacros"  not in c: c["cwMacros"]  = DEFAULT_MACROS
    return c


def get_users() -> list:
    u = load_json(USR_F, [])
    if not u:
        u = [{"id": "1", "username": "admin",
              "password": hash_pw_secure(ADMIN_PW),
              "role": "admin", "active": True}]
        save_json(USR_F, u)
    return u
