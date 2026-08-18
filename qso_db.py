"""
qso_db.py — SQLite backend for the QSO Log

One .db file for all users, but every QSO has a user_id — isolation
per user at the SQL query level.

Thread-safe: each connection is created in the thread that uses it
(check_same_thread=False + WAL mode for concurrent reads).
"""

import sqlite3
import uuid
import time
import csv
import io
from datetime import datetime, timezone
from pathlib import Path


# ── Database path ───────────────────────────────────────────────────────────
def _db_path() -> Path:
    """Data directory from config.DATA (APPDATA when installed under Program Files).

    NOTE — THIS IS WHERE A BUG ONCE CAUSED THE LOG TO BE LOST ON UPDATE:
    the code used to import 'DATA_DIR', but config.py exports 'DATA'. The
    import ALWAYS raised ImportError, so the database fell back to a path
    next to the script — i.e. Program Files, from where every new installer
    deleted it. The other modules (cert, config, accounts) used the correct
    name and survived updates — hence the confusing impression that "only
    the log" was disappearing.
    """
    try:
        from config import DATA
        return Path(DATA) / "qso.db"
    except Exception as _e:
        # Fallback next to the script — when installed under Program Files
        # this is the exact location every update will wipe the log from.
        # Loud warning so this situation never goes unnoticed again.
        _p = Path(__file__).parent / "qso.db"
        print(f"[qso_db] WARNING: cannot read the data directory ({_e}). "
              f"QSO database goes to {_p} — when installed under Program "
              f"Files, an update MAY DELETE IT!", flush=True)
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
    cloudlog_id     TEXT DEFAULT '',
    prop_mode       TEXT DEFAULT '',
    sat_name        TEXT DEFAULT '',
    sat_mode        TEXT DEFAULT '',
    freq_rx         TEXT DEFAULT '',
    band_rx         TEXT DEFAULT '',
    name            TEXT DEFAULT '',
    qth             TEXT DEFAULT '',
    dxcc            TEXT DEFAULT '',
    country         TEXT DEFAULT '',
    cont            TEXT DEFAULT '',
    cqz             TEXT DEFAULT '',
    ituz            TEXT DEFAULT '',
    state           TEXT DEFAULT '',
    iota            TEXT DEFAULT '',
    qsl_sent        TEXT DEFAULT '',
    qsl_rcvd        TEXT DEFAULT '',
    lotw_qsl_sent   TEXT DEFAULT '',
    lotw_qsl_rcvd   TEXT DEFAULT '',
    lotw_qslsdate   TEXT DEFAULT '',
    lotw_qslrdate   TEXT DEFAULT '',
    eqsl_qsl_sent   TEXT DEFAULT '',
    eqsl_qsl_rcvd   TEXT DEFAULT '',
    pota_ref        TEXT DEFAULT '',
    sota_ref        TEXT DEFAULT '',
    wwff_ref        TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_qso_user      ON qso(user_id);
CREATE INDEX IF NOT EXISTS idx_qso_user_date ON qso(user_id, qso_date);
CREATE INDEX IF NOT EXISTS idx_qso_user_call ON qso(user_id, call);
CREATE INDEX IF NOT EXISTS idx_qso_user_band ON qso(user_id, band);
CREATE INDEX IF NOT EXISTS idx_qso_user_mode ON qso(user_id, mode);
"""

_conn = None

import threading as _threading

# Lock protecting the shared SQLite connection. Since heavy operations
# (bulk import, delete_all, list_qsos) are called via asyncio.to_thread,
# two threads could write at the same time. SQLite serializes internally,
# but an explicit lock eliminates races at the cursor/transaction level.
_conn_lock = _threading.RLock()


def _get_conn() -> sqlite3.Connection:
    global _conn
    with _conn_lock:
        if _conn is None:
            path = _db_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            # Explicitly show WHERE the log lives — so it's immediately
            # visible whether it's in APPDATA (survives updates) or next to
            # the EXE (gets wiped).
            _existed = path.exists()
            print(f"[qso_db] QSO log: {path} "
                  f"({'exists' if _existed else 'new - empty'})", flush=True)
            _conn = sqlite3.connect(str(path), check_same_thread=False,
                                     timeout=10.0)
            _conn.row_factory = sqlite3.Row
            # WAL: much better concurrency (readers don't block the writer)
            # + resilience against corruption on crash.
            try:
                _conn.execute("PRAGMA journal_mode=WAL")
                _conn.execute("PRAGMA synchronous=NORMAL")
                _conn.execute("PRAGMA busy_timeout=10000")
            except Exception as e:
                print(f"[qso_db] PRAGMA error: {e}", flush=True)
            _conn.executescript(_SCHEMA)
            _conn.commit()
            _migrate(_conn)
        return _conn


# Columns added AFTER the first release — CREATE TABLE IF NOT EXISTS won't
# add them to an already-existing database (SQLite creates the table only
# once), so they have to be added manually via ALTER TABLE. Safe: DEFAULT ''
# on existing rows, no data is touched. See the comment at _db_path() — this
# file is the real contact log, so migration must be additive, never
# destructive.
_NEW_COLUMNS = {
    "prop_mode": "TEXT DEFAULT ''",
    "sat_name":  "TEXT DEFAULT ''",
    "sat_mode":  "TEXT DEFAULT ''",
    "freq_rx":   "TEXT DEFAULT ''",
    "band_rx":   "TEXT DEFAULT ''",
    # NAME/QTH/COUNTRY — filled in manually or (COUNTRY/CONT already now)
    # locally from the prefix table (dxcc.js); DXCC/CQZ/ITUZ/STATE/IOTA are
    # meant to eventually come from a QRZ/HamQTH lookup (not implemented
    # yet - fields are ready for that data).
    "name":          "TEXT DEFAULT ''",
    "qth":           "TEXT DEFAULT ''",
    "dxcc":          "TEXT DEFAULT ''",
    "country":       "TEXT DEFAULT ''",
    "cont":          "TEXT DEFAULT ''",
    "cqz":           "TEXT DEFAULT ''",
    "ituz":          "TEXT DEFAULT ''",
    "state":         "TEXT DEFAULT ''",
    "iota":          "TEXT DEFAULT ''",
    # QSL tracking — fields ready, no UI yet (activation later: needs
    # LoTW/eQSL integration before there's anything real to write here).
    "qsl_sent":      "TEXT DEFAULT ''",
    "qsl_rcvd":      "TEXT DEFAULT ''",
    "lotw_qsl_sent": "TEXT DEFAULT ''",
    "lotw_qsl_rcvd": "TEXT DEFAULT ''",
    "lotw_qslsdate": "TEXT DEFAULT ''",
    "lotw_qslrdate": "TEXT DEFAULT ''",
    "eqsl_qsl_sent": "TEXT DEFAULT ''",
    "eqsl_qsl_rcvd": "TEXT DEFAULT ''",
    # Activation program references — fields ready, no UI yet (activation later).
    "pota_ref":      "TEXT DEFAULT ''",
    "sota_ref":      "TEXT DEFAULT ''",
    "wwff_ref":      "TEXT DEFAULT ''",
}

# Fields added TOGETHER WITH prop_mode/sat_* (satellite) - but the ones
# below don't have UI for manual entry yet (except NAME/QTH/COUNTRY - see
# qsolog.js openNew/openEdit/saveQSO). Kept as a shared list so
# add_qso/update_qso/bulk handle them identically without repeating 20
# field names in every function.
_EXTRA_FIELDS = [
    "name", "qth", "dxcc", "country", "cont", "cqz", "ituz", "state", "iota",
    "qsl_sent", "qsl_rcvd", "lotw_qsl_sent", "lotw_qsl_rcvd",
    "lotw_qslsdate", "lotw_qslrdate", "eqsl_qsl_sent", "eqsl_qsl_rcvd",
    "pota_ref", "sota_ref", "wwff_ref",
]


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(qso)").fetchall()}
    for col, decl in _NEW_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE qso ADD COLUMN {col} {decl}")
            print(f"[qso_db] migration: added column '{col}'", flush=True)
    conn.commit()


# ── CRUD ──────────────────────────────────────────────────────────────────────
def add_qsos_bulk(user_id: str, qsos: list) -> dict:
    """
    Insert many QSOs at once — efficient bulk insert.
    Called via asyncio.to_thread (ADIF import takes seconds), so protected
    by a lock against concurrent writes from another thread.
    """
    globals().pop("_WORKED_CACHE", None)   # QSO import — invalidate the worked-calls cache
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

                # DUPLICATE CHECK — don't insert the same contact twice
                # (e.g. when re-importing the same file). A QSO is a
                # duplicate when the call, band, mode, date, and time match
                # within +-2 minutes (different programs record time
                # slightly differently). Time is compared as HHMM, with
                # tolerance.
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

                bulk_row = {
                    "id": qso_id, "user_id": user_id, "call": call,
                    "qso_date": _date, "time_on": _time,
                    "time_off": str(data.get("time_off", data.get("time_on", "000000"))),
                    "band": _band, "mode": _mode,
                    "freq":          str(data.get("freq", "")),
                    "rst_sent":      str(data.get("rst_sent", "")),
                    "rst_rcvd":      str(data.get("rst_rcvd", "")),
                    "gridsquare":    str(data.get("gridsquare", "")),
                    "my_call":       str(data.get("my_call", "")),
                    "my_gridsquare": str(data.get("my_gridsquare", "")),
                    "power":         str(data.get("power", "")),
                    "comment":       str(data.get("comment", "")),
                    "source":        str(data.get("source", "adif_import")),
                    "created_at":    now,
                    "prop_mode":     str(data.get("prop_mode", "")).upper(),
                    "sat_name":      str(data.get("sat_name", "")).upper(),
                    "sat_mode":      str(data.get("sat_mode", "")).upper(),
                    "freq_rx":       str(data.get("freq_rx", "")),
                    "band_rx":       str(data.get("band_rx", "")),
                    **{k: str(data.get(k, "")) for k in _EXTRA_FIELDS},
                }
                cols = ", ".join(bulk_row.keys())
                placeholders = ", ".join(f":{k}" for k in bulk_row.keys())
                conn.execute(f"INSERT INTO qso ({cols}) VALUES ({placeholders})", bulk_row)
                inserted += 1
            except Exception:
                skipped += 1
    return {"inserted": inserted, "skipped": skipped, "duplicates": dupes}


def add_qso(user_id: str, data: dict) -> dict:
    """Add a QSO to the database. Returns the saved QSO with its id."""
    globals().pop("_WORKED_CACHE", None)   # new QSO — invalidate the worked-calls cache
    conn = _get_conn()
    qso_id    = str(uuid.uuid4())
    now_utc   = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    qso_date  = data.get("qso_date", "")
    time_on   = data.get("time_on", "")
    time_off  = data.get("time_off", time_on)
    call      = (data.get("call") or "").strip().upper()

    if not call or not qso_date or not time_on:
        # NOTE: this message reaches the browser as-is — webapp.py's
        # /api/qsolog POST returns str(e) directly in the error response
        # body, and qsolog.js's saveQSO() displays res.error raw with no
        # I18n lookup. Kept in Polish deliberately, do not translate.
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
        # Lacznosc satelitarna (ADIF): PROP_MODE=SAT, SAT_NAME/SAT_MODE opisuja
        # satelite, FREQ_RX/BAND_RX to downlink (istniejace freq/band = uplink).
        "prop_mode":     (data.get("prop_mode") or "").strip().upper(),
        "sat_name":      (data.get("sat_name") or "").strip().upper(),
        "sat_mode":      (data.get("sat_mode") or "").strip().upper(),
        "freq_rx":       data.get("freq_rx", ""),
        "band_rx":       data.get("band_rx", ""),
        # NAME/QTH/geo/QSL/activation refs — see the comment at _NEW_COLUMNS.
        # Values are stored as received (no case-forcing - NAME/QTH/COUNTRY
        # are proper names, uppercasing them would mangle them).
        **{k: data.get(k, "") for k in _EXTRA_FIELDS},
    }

    # Columns built FROM row ITSELF (not a manually maintained VALUES list) -
    # eliminates a whole class of "forgot to add the new field to VALUES
    # after adding it to row" bugs (exactly what already had to be fixed once
    # during a migration).
    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{k}" for k in row.keys())
    conn.execute(f"INSERT INTO qso ({cols}) VALUES ({placeholders})", row)
    conn.commit()
    return dict(row)


def get_qso(user_id: str, qso_id: str, is_admin: bool = False) -> dict | None:
    """Fetch a single QSO."""
    conn = _get_conn()
    if is_admin:
        row = conn.execute("SELECT * FROM qso WHERE id=?", (qso_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM qso WHERE id=? AND user_id=?", (qso_id, user_id)).fetchone()
    return dict(row) if row else None


def update_qso(user_id: str, qso_id: str, data: dict, is_admin: bool = False) -> bool:
    """Update a QSO. Returns True if it was found and updated."""
    conn  = _get_conn()
    call  = (data.get("call") or "").strip().upper()
    if not call:
        # NOTE: same as in add_qso — this reaches the browser verbatim via
        # the PUT /api/qsolog/<id> error response. Kept in Polish, do not
        # translate.
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
        "prop_mode":     (data.get("prop_mode") or "").strip().upper(),
        "sat_name":      (data.get("sat_name") or "").strip().upper(),
        "sat_mode":      (data.get("sat_mode") or "").strip().upper(),
        "freq_rx":       data.get("freq_rx", ""),
        "band_rx":       data.get("band_rx", ""),
        **{k: data.get(k, "") for k in _EXTRA_FIELDS},
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
    """Delete all of a user's QSOs. Protected by a lock (called from a thread)."""
    with _conn_lock, _get_conn() as conn:
        cur = conn.execute("DELETE FROM qso WHERE user_id=?", (user_id,))
        return cur.rowcount

def delete_qso(user_id: str, qso_id: str, is_admin: bool = False) -> bool:
    """Delete a QSO. Returns True if it was deleted."""
    conn = _get_conn()
    if is_admin:
        cur = conn.execute("DELETE FROM qso WHERE id=?", (qso_id,))
    else:
        cur = conn.execute(
            "DELETE FROM qso WHERE id=? AND user_id=?", (qso_id, user_id))
    conn.commit()
    return cur.rowcount > 0


def worked_before(call: str, band: str = None, mode: str = None) -> dict:
    """Check whether the given call (optionally band/mode) has already been
    worked by ANYONE in the database. Returns:
      {
        "worked":       bool,  # True if there's any QSO with this call
        "worked_band":  bool,  # True if there's a QSO on THIS band
        "worked_mode":  bool,  # True if there's a QSO in THIS mode
        "worked_all":   bool,  # True if there's a QSO with this exact call+band+mode
        "count":        int,   # total number of QSOs with this call
        "last_qso":     dict|None,  # most recent QSO {qso_date, band, mode}
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
    List QSOs with filtering, sorting, and pagination.

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
    # query may be a MultiDict (aiohttp) — take the first value
    def _get(key, default=None):
        v = filters.get(key, default)
        return v[0] if isinstance(v, list) else v
    page   = max(1, int(_get("page", 1)))
    per    = min(200, max(1, int(_get("per", 50))))
    sort   = _get("sort", "qso_date")
    direction = "DESC" if str(_get("dir", "desc")).lower() != "asc" else "ASC"

    # Whitelist of sort columns
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

    # Filter by specific IDs — for exporting SELECTED QSOs. Comma-separated
    # id list. Takes priority: when given, the other filters still narrow
    # further, but it's normally used on its own (the operator selected
    # rows in the table).
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
    """Returns the list of UNIQUE calls from the log (to mark 'worked'
    stations in Band Activity). A lightweight SELECT DISTINCT instead of a
    full QSO list — list_qsos caps per<=200, which meant the frontend only
    saw the 200 MOST RECENT entries and older contacts weren't marked as
    worked.

    CACHE: the frontend polls this list periodically (Band Activity), and
    worked calls don't change every second. We hold the result for ~5s —
    eliminates most of the table scans that were blocking the event loop
    (found via looplag)."""
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
    # Return call+mode+band tuples, not just calls — FT8 and FT4 are
    # DIFFERENT modes and separate QSOs, so a station worked on FT8 must
    # not be marked as worked on FT4. The frontend matches by call+mode.
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
    """Export QSOs to ADIF format (.adi)."""
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
        # STATION_CALLSIGN is the correct ADIF tag for the logging station's
        # call (this used to be "MY_CALL" - not a standard tag, so other
        # programs importing it simply ignored it and had no idea whose log
        # it was). OPERATOR added alongside - same call, but this is the
        # ADIF field for "who was at the key" (relevant when several users
        # share one station/call).
        rec += field("STATION_CALLSIGN", q["my_call"])
        rec += field("OPERATOR",         q["my_call"])
        rec += field("MY_GRIDSQUARE", q["my_gridsquare"])
        rec += field("TX_PWR",        q["power"])
        rec += field("COMMENT",       q["comment"])
        # Satellite contact — ADIF standard: PROP_MODE=SAT, SAT_NAME/
        # SAT_MODE describe the satellite, FREQ_RX/BAND_RX are the downlink
        # (FREQ/BAND above are the uplink). Empty for regular QSOs —
        # field() skips empty values.
        rec += field("PROP_MODE",     q.get("prop_mode", ""))
        rec += field("SAT_NAME",      q.get("sat_name", ""))
        rec += field("SAT_MODE",      q.get("sat_mode", ""))
        rec += field("FREQ_RX",       q.get("freq_rx", ""))
        rec += field("BAND_RX",       q.get("band_rx", ""))
        # NAME/QTH/geo for the other station — NAME/QTH entered manually or
        # (COUNTRY/CONT) from the local prefix table (dxcc.js); DXCC/CQZ/
        # ITUZ/STATE/IOTA meant to eventually come from a QRZ/HamQTH lookup
        # (not wired up yet - fields are ready).
        rec += field("NAME",          q.get("name", ""))
        rec += field("QTH",           q.get("qth", ""))
        rec += field("DXCC",          q.get("dxcc", ""))
        rec += field("COUNTRY",       q.get("country", ""))
        rec += field("CONT",          q.get("cont", ""))
        rec += field("CQZ",           q.get("cqz", ""))
        rec += field("ITUZ",          q.get("ituz", ""))
        rec += field("STATE",         q.get("state", ""))
        rec += field("IOTA",          q.get("iota", ""))
        # QSL tracking and activation-program refs — fields ready in the
        # database, no UI for manual entry yet (see the comment at
        # _EXTRA_FIELDS). Exported anyway so a round-trip through ADIF
        # import doesn't lose anything.
        rec += field("QSL_SENT",      q.get("qsl_sent", ""))
        rec += field("QSL_RCVD",      q.get("qsl_rcvd", ""))
        rec += field("LOTW_QSL_SENT", q.get("lotw_qsl_sent", ""))
        rec += field("LOTW_QSL_RCVD", q.get("lotw_qsl_rcvd", ""))
        rec += field("LOTW_QSLSDATE", q.get("lotw_qslsdate", ""))
        rec += field("LOTW_QSLRDATE", q.get("lotw_qslrdate", ""))
        rec += field("EQSL_QSL_SENT", q.get("eqsl_qsl_sent", ""))
        rec += field("EQSL_QSL_RCVD", q.get("eqsl_qsl_rcvd", ""))
        rec += field("POTA_REF",      q.get("pota_ref", ""))
        rec += field("SOTA_REF",      q.get("sota_ref", ""))
        rec += field("WWFF_REF",      q.get("wwff_ref", ""))
        rec += "<EOR>"
        lines.append(rec)

    return "\n".join(lines)


def export_csv(user_id: str, is_admin: bool = False, **filters) -> str:
    """Export QSOs to CSV."""
    data = list_qsos(user_id, is_admin, per=999999, **filters)
    qsos = data["qsos"]

    out  = io.StringIO()
    cols = ["call","qso_date","time_on","time_off","band","mode","freq",
            "rst_sent","rst_rcvd","gridsquare","my_call","my_gridsquare",
            "power","comment","source",
            "prop_mode","sat_name","sat_mode","freq_rx","band_rx",
            "name","qth","dxcc","country","cont","cqz","ituz","state","iota",
            "qsl_sent","qsl_rcvd","lotw_qsl_sent","lotw_qsl_rcvd",
            "lotw_qslsdate","lotw_qslrdate","eqsl_qsl_sent","eqsl_qsl_rcvd",
            "pota_ref","sota_ref","wwff_ref"]
    writer = csv.DictWriter(out, fieldnames=cols, extrasaction="ignore",
                            lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(qsos)
    return out.getvalue()


def export_edi(user_id: str, is_admin: bool = False, **filters) -> str:
    """
    Export QSOs to REG1TEST format (.edi) — the standard for VHF/UHF contests.
    EDI (Electronic Data Interchange) format per IARU Region 1.
    """
    data = list_qsos(user_id, is_admin, per=999999, **filters)
    qsos = data["qsos"]

    # Get station data from the first QSO
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
        # EDI format: Date;Time;Call;Mode;SentNr;SentRST;RcvdNr;RcvdRST;Grid;Points;...
        date = q["qso_date"]       # YYYYMMDD
        time = q["time_on"][:4]    # HHMM
        lines.append(
            f"{date};{time};{q['call']};{q['mode']};"
            f"001;{q['rst_sent']};001;{q['rst_rcvd']};"
            f"{q['gridsquare']};0"
        )

    lines.append("[END;OF;FILE]")
    return "\n".join(lines)
