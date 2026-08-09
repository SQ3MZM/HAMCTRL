#!/usr/bin/env python3
"""
data.py — magazyn JSON (config, użytkownicy, log QSO) i wartości domyślne.

STABILNOSC (2026-07-05):
- save_json zapisuje ATOMOWO (tmp + rename) i trzyma kopie .bak.
  Bez tego crash/brak pradu w trakcie zapisu uszkadza users.json -> utrata kont.
- load_json przy uszkodzonym pliku probuje odtworzyc z .bak i GLOSNO loguje.
  Wczesniej cicho zwracal default (pusta lista userow!) i nikt nie wiedzial.
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
    Wczytaj JSON. Przy uszkodzonym pliku sprobuj kopii .bak.
    Bledy sa LOGOWANE (nie cicho ignorowane) - uszkodzony users.json
    oznaczalby utrate wszystkich kont bez ostrzezenia.
    """
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[data] BLAD odczytu {path.name}: {e}", flush=True)

    # Plik uszkodzony - sprobuj kopii zapasowej
    bak = path.with_suffix(path.suffix + ".bak")
    if bak.exists():
        try:
            data = json.loads(bak.read_text(encoding="utf-8"))
            print(f"[data] ODZYSKANO {path.name} z kopii {bak.name}", flush=True)
            # Odtworz glowny plik z backupu
            try:
                shutil.copy2(bak, path)
            except Exception:
                pass
            return data
        except Exception as e:
            print(f"[data] kopia {bak.name} tez uszkodzona: {e}", flush=True)

    # Zachowaj uszkodzony plik do analizy (nie nadpisuj go slepo)
    try:
        broken = path.with_suffix(path.suffix + ".broken")
        shutil.copy2(path, broken)
        print(f"[data] uszkodzony plik zachowany jako {broken.name}", flush=True)
    except Exception:
        pass
    print(f"[data] UWAGA: uzywam wartosci domyslnych dla {path.name}!", flush=True)
    return default


def save_json(path: Path, data):
    """
    Zapis ATOMOWY: najpierw do pliku tymczasowego w tym samym katalogu,
    fsync, potem atomowy os.replace(). Dzieki temu plik docelowy jest
    ZAWSZE kompletny - crash w trakcie zapisu nie moze go uszkodzic.

    Przed podmiana robi kopie .bak (poprzednia dobra wersja).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)

    # Kopia poprzedniej dobrej wersji (jesli istnieje i jest niepusta)
    try:
        if path.exists() and path.stat().st_size > 0:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    except Exception as e:
        print(f"[data] nie udalo sie zrobic .bak dla {path.name}: {e}", flush=True)

    tmp_path = None
    try:
        # Plik tymczasowy MUSI byc na tym samym dysku co docelowy,
        # inaczej os.replace nie bedzie atomowy.
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())   # wymus zapis na dysk (nie tylko cache OS)
        # Atomowa podmiana - na Windows i POSIX os.replace jest atomowe
        os.replace(tmp_path, path)
        tmp_path = None
    except Exception as e:
        print(f"[data] BLAD zapisu {path.name}: {e}", flush=True)
        raise
    finally:
        # Posprzataj plik tymczasowy jesli cos poszlo nie tak
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
