"""
qso_db.py — SQLite backend dla Log QSO

Jeden plik .db na wszystkich użytkowników, ale każde QSO
ma user_id — izolacja per user na poziomie zapytań SQL.

Wątek-bezpieczny: każde połączenie tworzone w wątku który go używa
(check_same_thread=False + WAL mode dla równoległych odczytów).
"""

import sqlite3
import uuid
import time
import csv
import io
from datetime import datetime, timezone
from pathlib import Path


# ── Ścieżka do bazy ───────────────────────────────────────────────────────────
def _db_path() -> Path:
    """Katalog danych z config.DATA (APPDATA przy instalacji w Program Files).

    UWAGA — TU BYL BLAD POWODUJACY UTRATE DZIENNIKA PRZY AKTUALIZACJI:
    importowalismy 'DATA_DIR', a config.py eksportuje 'DATA'. Import ZAWSZE
    rzucal ImportError, wiec baza szla do fallbacku obok skryptu — czyli do
    Program Files, skad kasowal ja kazdy nowy instalator. Reszta modulow
    (cert, konfiguracja, konta) uzywala poprawnej nazwy i przezywala
    aktualizacje — stad mylace wrazenie, ze ginie "tylko log".
    """
    try:
        from config import DATA
        return Path(DATA) / "qso.db"
    except Exception as _e:
        # Fallback obok skryptu — przy instalacji w Program Files to miejsce,
        # z ktorego kazda aktualizacja skasuje dziennik. Glosne ostrzezenie,
        # zeby taki stan nigdy wiecej nie przeszedl niezauwazony.
        _p = Path(__file__).parent / "qso.db"
        print(f"[qso_db] UWAGA: nie moge odczytac katalogu danych ({_e}). "
              f"Baza QSO ladzie w {_p} — przy instalacji w Program Files "
              f"aktualizacja moze ja SKASOWAC!", flush=True)
        return _p


# ── Schema ────────────────────────────────────────────────────────────────────
_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

CREATE TABLE IF NOT EXISTS qso (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    call            TEXT NOT NULL,
    qso_date        TEXT NOT NULL,
    time_on         TEXT NOT NULL,
    time_off        TEXT NOT NULL,
    band            TEXT DEFAULT '',
    mode            TEXT DEFAULT '',
    freq            TEXT DEFAULT '',
    rst_sent        TEXT DEFAULT '599',
    rst_rcvd        TEXT DEFAULT '599',
    gridsquare      TEXT DEFAULT '',
    my_call         TEXT DEFAULT '',
    my_gridsquare   TEXT DEFAULT '',
    power           TEXT DEFAULT '',
    comment         TEXT DEFAULT '',
    source          TEXT DEFAULT 'manual',
    created_at      TEXT NOT NULL,
    cloudlog_id     TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_qso_user      ON qso(user_id);
CREATE INDEX IF NOT EXISTS idx_qso_user_date ON qso(user_id, qso_date);
CREATE INDEX IF NOT EXISTS idx_qso_user_call ON qso(user_id, call);
CREATE INDEX IF NOT EXISTS idx_qso_user_band ON qso(user_id, band);
CREATE INDEX IF NOT EXISTS idx_qso_user_mode ON qso(user_id, mode);
"""

_conn = None

import threading as _threading

# Lock chroniacy wspoldzielone polaczenie SQLite. Odkad ciezkie operacje
# (bulk import, delete_all, list_qsos) sa wolane przez asyncio.to_thread,
# dwa watki moglyby pisac jednoczesnie. SQLite serializuje wewnetrznie, ale
# jawny lock eliminuje wyscigi na poziomie kursorow i transakcji.
_conn_lock = _threading.RLock()


def _get_conn() -> sqlite3.Connection:
    global _conn
    with _conn_lock:
        if _conn is None:
            path = _db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Jawnie pokaz GDZIE jest dziennik — zeby od razu bylo widac, czy
            # siedzi w APPDATA (przezyje aktualizacje) czy obok EXE (zginie).
            _existed = path.exists()
            print(f"[qso_db] Dziennik QSO: {path} "
                  f"({'istnieje' if _existed else 'nowy - pusty'})", flush=True)
            _conn = sqlite3.connect(str(path), check_same_thread=False,
                                     timeout=10.0)
            _conn.row_factory = sqlite3.Row
            # WAL: znacznie lepsza wspolbieznosc (czytelnicy nie blokuja
            # pisarza) + odpornosc na uszkodzenie przy crashu.
            try:
                _conn.execute("PRAGMA journal_mode=WAL")
                _conn.execute("PRAGMA synchronous=NORMAL")
                _conn.execute("PRAGMA busy_timeout=10000")
            except Exception as e:
                print(f"[qso_db] PRAGMA blad: {e}", flush=True)
            _conn.executescript(_SCHEMA)
            _conn.commit()
        return _conn


# ── CRUD ──────────────────────────────────────────────────────────────────────
def add_qsos_bulk(user_id: str, qsos: list) -> dict:
    """
    Wstaw wiele QSO naraz — wydajny bulk insert.
    Wolane przez asyncio.to_thread (import ADIF trwa sekundy), wiec chronione
    lockiem przed rownoczesnym zapisem z innego watku.
    """
    globals().pop("_WORKED_CACHE", None)   # import QSO — uniewaznij cache zrobionych znakow
    import uuid as _uuid
    from datetime import datetime as _dt

    inserted = 0
    skipped  = 0
    dupes    = 0
    now      = _dt.utcnow().strftime("%Y%m%d%H%M%S")

    with _conn_lock, _get_conn() as conn:
        for data in qsos:
            try:
                qso_id   = str(_uuid.uuid4())
                call     = str(data.get("call", "")).upper().strip()
                if not call:
                    skipped += 1
                    continue

                _band = str(data.get("band", "")).upper()
                _mode = str(data.get("mode", "")).upper()
                _date = str(data.get("qso_date", now[:8]))
                _time = str(data.get("time_on", "000000"))

                # SPRAWDZANIE DUPLIKATOW — nie wstawiaj tej samej lacznosci
                # drugi raz (np. przy powtornym imporcie tego samego pliku).
                # QSO to duplikat, gdy zgadza sie znak, pasmo, tryb, data i czas
                # w granicach +-2 minut (rozne programy zapisuja czas nieco
                # inaczej). Porownujemy czas jako HHMM, z tolerancja.
                _t4 = (_time + "0000")[:4]        # HHMM
                try:
                    _tmin = int(_t4[:2]) * 60 + int(_t4[2:4])
                except ValueError:
                    _tmin = -999
                existing = conn.execute(
                    "SELECT time_on FROM qso WHERE user_id=? AND UPPER(call)=? "
                    "AND UPPER(band)=? AND UPPER(mode)=? AND qso_date=?",
                    (user_id, call, _band, _mode, _date)
                ).fetchall()
                is_dupe = False
                for (et,) in existing:
                    _et4 = (str(et) + "0000")[:4]
                    try:
                        _emin = int(_et4[:2]) * 60 + int(_et4[2:4])
                    except ValueError:
                        continue
                    if _tmin != -999 and abs(_emin - _tmin) <= 2:
                        is_dupe = True
                        break
                if is_dupe:
                    dupes += 1
                    continue

                conn.execute("""
                    INSERT INTO qso (
                        id, user_id, call, qso_date, time_on, time_off,
                        band, mode, freq, rst_sent, rst_rcvd,
                        gridsquare, my_call, my_gridsquare,
                        power, comment, source, created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    qso_id, user_id, call,
                    _date, _time,
                    str(data.get("time_off", data.get("time_on", "000000"))),
                    _band, _mode,
                    str(data.get("freq", "")),
                    str(data.get("rst_sent", "")),
                    str(data.get("rst_rcvd", "")),
                    str(data.get("gridsquare", "")),
                    str(data.get("my_call", "")),
                    str(data.get("my_gridsquare", "")),
                    str(data.get("power", "")),
                    str(data.get("comment", "")),
                    str(data.get("source", "adif_import")),
                    now,
                ))
                inserted += 1
            except Exception:
                skipped += 1
    return {"inserted": inserted, "skipped": skipped, "duplicates": dupes}


def add_qso(user_id: str, data: dict) -> dict:
    """Dodaj QSO do bazy. Zwraca zapisane QSO z id."""
    globals().pop("_WORKED_CACHE", None)   # nowy QSO — uniewaznij cache zrobionych znakow
    conn = _get_conn()
    qso_id    = str(uuid.uuid4())
    now_utc   = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    qso_date  = data.get("qso_date", "")
    time_on   = data.get("time_on", "")
    time_off  = data.get("time_off", time_on)
    call      = (data.get("call") or "").strip().upper()

    if not call or not qso_date or not time_on:
        raise ValueError("Brak wymaganych pól: call, qso_date, time_on")

    row = {
        "id":            qso_id,
        "user_id":       user_id,
        "call":          call,
        "qso_date":      qso_date,
        "time_on":       time_on,
        "time_off":      time_off,
        "band":          data.get("band", ""),
        "mode":          data.get("mode", ""),
        "freq":          data.get("freq", ""),
        "rst_sent":      data.get("rst_sent", "599"),
        "rst_rcvd":      data.get("rst_rcvd", "599"),
        "gridsquare":    (data.get("gridsquare") or "").upper(),
        "my_call":       (data.get("my_call") or "").upper(),
        "my_gridsquare": (data.get("my_gridsquare") or "").upper(),
        "power":         data.get("power", ""),
        "comment":       data.get("comment", ""),
        "source":        data.get("source", "manual"),
        "created_at":    now_utc,
        "cloudlog_id":   data.get("cloudlog_id", ""),
    }

    conn.execute("""
        INSERT INTO qso VALUES (
            :id,:user_id,:call,:qso_date,:time_on,:time_off,
            :band,:mode,:freq,:rst_sent,:rst_rcvd,:gridsquare,
            :my_call,:my_gridsquare,:power,:comment,:source,
            :created_at,:cloudlog_id
        )""", row)
    conn.commit()
    return dict(row)


def get_qso(user_id: str, qso_id: str, is_admin: bool = False) -> dict | None:
    """Pobierz pojedyncze QSO."""
    conn = _get_conn()
    if is_admin:
        row = conn.execute("SELECT * FROM qso WHERE id=?", (qso_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM qso WHERE id=? AND user_id=?", (qso_id, user_id)).fetchone()
    return dict(row) if row else None


def update_qso(user_id: str, qso_id: str, data: dict, is_admin: bool = False) -> bool:
    """Zaktualizuj QSO. Zwraca True jeśli znaleziono i zaktualizowano."""
    conn  = _get_conn()
    call  = (data.get("call") or "").strip().upper()
    if not call:
        raise ValueError("Brak znaku wywoławczego")

    fields = {
        "call":          call,
        "qso_date":      data.get("qso_date", ""),
        "time_on":       data.get("time_on", ""),
        "time_off":      data.get("time_off", data.get("time_on", "")),
        "band":          data.get("band", ""),
        "mode":          data.get("mode", ""),
        "freq":          data.get("freq", ""),
        "rst_sent":      data.get("rst_sent", "599"),
        "rst_rcvd":      data.get("rst_rcvd", "599"),
        "gridsquare":    (data.get("gridsquare") or "").upper(),
        "my_call":       (data.get("my_call") or "").upper(),
        "my_gridsquare": (data.get("my_gridsquare") or "").upper(),
        "power":         data.get("power", ""),
        "comment":       data.get("comment", ""),
    }
    set_clause = ", ".join(f"{k}=:{k}" for k in fields)
    fields["id"]      = qso_id
    fields["user_id"] = user_id

    if is_admin:
        cur = conn.execute(f"UPDATE qso SET {set_clause} WHERE id=:id", fields)
    else:
        cur = conn.execute(
            f"UPDATE qso SET {set_clause} WHERE id=:id AND user_id=:user_id", fields)
    conn.commit()
    return cur.rowcount > 0


def delete_all(user_id: str) -> int:
    """Usuń wszystkie QSO usera. Chronione lockiem (wolane z watku)."""
    with _conn_lock, _get_conn() as conn:
        cur = conn.execute("DELETE FROM qso WHERE user_id=?", (user_id,))
        return cur.rowcount

def delete_qso(user_id: str, qso_id: str, is_admin: bool = False) -> bool:
    """Usuń QSO. Zwraca True jeśli usunięto."""
    conn = _get_conn()
    if is_admin:
        cur = conn.execute("DELETE FROM qso WHERE id=?", (qso_id,))
    else:
        cur = conn.execute(
            "DELETE FROM qso WHERE id=? AND user_id=?", (qso_id, user_id))
    conn.commit()
    return cur.rowcount > 0


def worked_before(call: str, band: str = None, mode: str = None) -> dict:
    """Sprawdz czy dany call (opcjonalnie band/mode) juz byl worked przez
    KOGOKOLWIEK w bazie. Zwraca:
      {
        "worked":       bool,  # True gdy jest jakiekolwiek QSO z tym callem
        "worked_band":  bool,  # True gdy jest QSO na TYM band
        "worked_mode":  bool,  # True gdy jest QSO w TYM mode
        "worked_all":   bool,  # True gdy jest QSO z tym call+band+mode (identyczne)
        "count":        int,   # ile lacznie QSO z tym callem
        "last_qso":     dict|None,  # ostatnie QSO {qso_date, band, mode}
      }
    """
    if not call:
        return {"worked": False, "worked_band": False, "worked_mode": False,
                "worked_all": False, "count": 0, "last_qso": None}
    call = call.upper().strip()
    conn = _get_conn()
    cur = conn.execute(
        "SELECT qso_date, band, mode FROM qso WHERE UPPER(call) = ? ORDER BY qso_date DESC",
        (call,)
    )
    rows = cur.fetchall()
    if not rows:
        return {"worked": False, "worked_band": False, "worked_mode": False,
                "worked_all": False, "count": 0, "last_qso": None}
    worked_band = False
    worked_mode = False
    worked_all  = False
    if band:
        worked_band = any(r["band"] == band for r in rows)
    if mode:
        worked_mode = any(r["mode"] == mode for r in rows)
    if band and mode:
        worked_all = any(r["band"] == band and r["mode"] == mode for r in rows)
    last = rows[0]
    return {
        "worked":       True,
        "worked_band":  worked_band,
        "worked_mode":  worked_mode,
        "worked_all":   worked_all,
        "count":        len(rows),
        "last_qso":     {"qso_date": last["qso_date"], "band": last["band"], "mode": last["mode"]},
    }


def list_qsos(user_id: str, is_admin: bool = False, filter_uid: str = None, **filters) -> dict:
    """
    Listuj QSO z filtrowaniem, sortowaniem i paginacją.

    filters:
        page    int  (default 1)
        per     int  (default 50, max 200)
        sort    str  (default 'qso_date')
        dir     str  'asc'|'desc' (default 'desc')
        from    str  YYYY-MM-DD
        to      str  YYYY-MM-DD
        call    str  (partial match)
        band    str
        mode    str
    """
    conn   = _get_conn()
    # query moze byc MultiDict (aiohttp) — pobierz pierwsza wartosc
    def _get(key, default=None):
        v = filters.get(key, default)
        return v[0] if isinstance(v, list) else v
    page   = max(1, int(_get("page", 1)))
    per    = min(200, max(1, int(_get("per", 50))))
    sort   = _get("sort", "qso_date")
    direction = "DESC" if str(_get("dir", "desc")).lower() != "asc" else "ASC"

    # Whitelist kolumn sortowania
    _SORT_COLS = {"qso_date", "call", "band", "mode", "freq", "created_at"}
    if sort not in _SORT_COLS:
        sort = "qso_date"

    where  = []
    params = []

    if not is_admin:
        where.append("user_id=?")
        params.append(user_id)
    elif filter_uid:
        where.append("user_id=?")
        params.append(str(filter_uid))

    if _get("from"):
        where.append("qso_date >= ?")
        params.append(str(_get("from")).replace("-", ""))

    if _get("to"):
        where.append("qso_date <= ?")
        params.append(str(_get("to")).replace("-", ""))

    if _get("call"):
        where.append("call LIKE ?")
        params.append(f"%{str(_get('call')).upper()}%")

    if _get("band"):
        where.append("band=?")
        params.append(str(_get("band")))

    if _get("mode"):
        where.append("mode=?")
        params.append(str(_get("mode")))

    # Filtr po konkretnych ID — do eksportu ZAZNACZONYCH QSO. Lista id przez
    # przecinek. Ma priorytet: gdy podana, pozostale filtry i tak zawezaja,
    # ale zwykle uzywana samodzielnie (operator zaznaczyl wpisy w tabeli).
    _ids = _get("ids")
    if _ids:
        id_list = [x.strip() for x in str(_ids).split(",") if x.strip()]
        if id_list:
            placeholders = ",".join("?" * len(id_list))
            where.append(f"id IN ({placeholders})")
            params.extend(id_list)

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(*) FROM qso {where_sql}", params).fetchone()[0]

    rows = conn.execute(
        f"SELECT * FROM qso {where_sql} "
        f"ORDER BY {sort} {direction}, time_on {direction} "
        f"LIMIT ? OFFSET ?",
        params + [per, (page - 1) * per]
    ).fetchall()

    return {
        "total": total,
        "page":  page,
        "per":   per,
        "qsos":  [dict(r) for r in rows],
    }


# ── Export ────────────────────────────────────────────────────────────────────
def worked_calls(user_id: str, is_admin: bool = False,
                 filter_uid: str = None) -> list:
    """Zwraca liste UNIKALNYCH znakow z logu (do oznaczania 'zrobionych'
    stacji w Band Activity). Lekki SELECT DISTINCT zamiast pelnej listy QSO —
    list_qsos ma cap per<=200, przez co frontend widzial tylko 200 NAJNOWSZYCH
    wpisow i starsze lacznosci nie byly oznaczane jako zrobione.

    CACHE: frontend odpytuje te liste cyklicznie (Band Activity), a zrobione
    znaki nie zmieniaja sie co sekunde. Trzymamy wynik ~5s — eliminuje wiekszosc
    skanow tabeli, ktore blokowaly petle zdarzen (wykryte przez looplag)."""
    import time as _t
    _ck = (user_id, is_admin, filter_uid)
    _now = _t.monotonic()
    _cache = globals().setdefault("_WORKED_CACHE", {})
    _hit = _cache.get(_ck)
    if _hit and (_now - _hit[0]) < 5.0:
        return _hit[1]
    where, params = [], []
    if not is_admin:
        where.append("user_id=?")
        params.append(user_id)
    elif filter_uid:
        where.append("user_id=?")
        params.append(str(filter_uid))
    sql = "SELECT DISTINCT call, mode, band FROM qso"
    if where:
        sql += " WHERE " + " AND ".join(where)
    conn = _get_conn()
    rows = conn.execute(sql, params).fetchall()
    # Zwracamy pary call+mode+band, nie same znaki — FT8 i FT4 to ROZNE
    # modulacje i osobne QSO, wiec stacja zrobiona na FT8 nie moze byc
    # oznaczona jako zrobiona na FT4. Frontend dopasowuje po call+mode.
    out = []
    for r in rows:
        call = (r[0] or "").upper()
        if not call:
            continue
        out.append({"call": call,
                    "mode": (r[1] or "").upper(),
                    "band": (r[2] or "").upper()})
    _cache[_ck] = (_now, out)
    return out


def export_adif(user_id: str, is_admin: bool = False, **filters) -> str:
    """Eksportuj QSO do formatu ADIF (.adi)."""
    data   = list_qsos(user_id, is_admin, per=999999, **filters)
    qsos   = data["qsos"]
    now    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines  = [
        f"Ham Radio CTRL — QSO Log Export",
        f"<ADIF_VER:5>3.1.4",
        f"<PROGRAMID:13>HamRadioCtrl",
        f"<CREATED_TIMESTAMP:15>{datetime.now(timezone.utc).strftime('%Y%m%d %H%M%S')}",
        f"<EOH>",
        "",
    ]

    def field(tag: str, val: str) -> str:
        val = str(val or "")
        return f"<{tag}:{len(val)}>{val} " if val else ""

    for q in qsos:
        rec = ""
        rec += field("CALL",          q["call"])
        rec += field("QSO_DATE",      q["qso_date"])
        rec += field("TIME_ON",       q["time_on"])
        rec += field("TIME_OFF",      q["time_off"])
        rec += field("BAND",          q["band"])
        rec += field("MODE",          q["mode"])
        rec += field("FREQ",          q["freq"])
        rec += field("RST_SENT",      q["rst_sent"])
        rec += field("RST_RCVD",      q["rst_rcvd"])
        rec += field("GRIDSQUARE",    q["gridsquare"])
        rec += field("MY_CALL",       q["my_call"])
        rec += field("MY_GRIDSQUARE", q["my_gridsquare"])
        rec += field("TX_PWR",        q["power"])
        rec += field("COMMENT",       q["comment"])
        rec += "<EOR>"
        lines.append(rec)

    return "\n".join(lines)


def export_csv(user_id: str, is_admin: bool = False, **filters) -> str:
    """Eksportuj QSO do CSV."""
    data = list_qsos(user_id, is_admin, per=999999, **filters)
    qsos = data["qsos"]

    out  = io.StringIO()
    cols = ["call","qso_date","time_on","time_off","band","mode","freq",
            "rst_sent","rst_rcvd","gridsquare","my_call","my_gridsquare",
            "power","comment","source"]
    writer = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore",
                            lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(qsos)
    return out.getvalue()


def export_edi(user_id: str, is_admin: bool = False, **filters) -> str:
    """
    Eksportuj QSO do formatu REG1TEST (.edi) — standard dla zawodów UKF.
    Format EDI (Electronic Data Interchange) wg IARU Region 1.
    """
    data = list_qsos(user_id, is_admin, per=999999, **filters)
    qsos = data["qsos"]

    # Pobierz dane stacji z pierwszego QSO
    my_call = (qsos[0]["my_call"] if qsos else "") or user_id.upper()
    my_grid = (qsos[0]["my_gridsquare"] if qsos else "")
    now     = datetime.now(timezone.utc)

    lines = [
        "[REG1TEST]",
        f"TName=Ham Radio CTRL Export",
        f"TDate={now.strftime('%Y%m%d')};{now.strftime('%Y%m%d')}",
        f"PCall={my_call}",
        f"PWWLo={my_grid}",
        f"PBand=",
        f"PSect=",
        f"RName=",
        f"RBand=",
        "[QSORecords;{len(qsos)}]",
    ]

    for q in qsos:
        # Format EDI: Date;Time;Call;Mode;SentNr;SentRST;RcvdNr;RcvdRST;Grid;Points;...
        date = q["qso_date"]       # YYYYMMDD
        time = q["time_on"][:4]    # HHMM
        lines.append(
            f"{date};{time};{q['call']};{q['mode']};"
            f"001;{q['rst_sent']};001;{q['rst_rcvd']};"
            f"{q['gridsquare']};0"
        )

    lines.append("[END;OF;FILE]")
    return "\n".join(lines)
