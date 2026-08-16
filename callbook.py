"""
callbook.py — lookup znakow wywolawczych przez QRZ.com i HamQTH.com.

Kazdy user ma WLASNE dane logowania (przechowywane w users.json, patrz
webapp.py /api/callbook/config) - QRZ wymaga platnej subskrypcji "XML Data"
na koncie QRZ, HamQTH jest darmowe ale tez wymaga wlasnego konta. Zadania
robimy SERWEROWO (nie z przegladarki) z dwoch powodow: obie uslugi nie maja
CORS dla przegladarek, i nie chcemy wystawiac hasel usera w JS.

Sesje (session key/id) cache'owane w pamieci procesu per (user_id, serwis) -
logowanie na kazdy pojedynczy lookup byloby zbyt czeste dla obu serwisow.

Parsowanie XML jest NAMESPACE-AGNOSTYCZNE (spłaszczamy cale drzewo do
{lokalna_nazwa_tagu: tekst}) - obie uslugi maja plytki XML bez kolizji nazw
tagow, a nie mamy tu mozliwosci przetestowania na zywo dokladnego namespace
URI/struktury, wiec bezpieczniej nie polegac na dopasowaniu 1:1.
"""
import time
import xml.etree.ElementTree as ET

import aiohttp

_AGENT = "HamRadioCTRL1.0"
_TIMEOUT = aiohttp.ClientTimeout(total=8)

# Cache sesji: {(user_id, service): (session_token, expires_at)}
_sessions: dict = {}
_SESSION_TTL = 3600 * 12  # obie uslugi trzymaja sesje dlugo, ale nie ufamy w nieskonczonosc


def _flatten_xml(text: str) -> dict:
    """Splaszcz XML do {lokalna_nazwa_tagu: tekst} ignorujac namespace i
    zagniezdzenie. Pierwsze wystapienie tagu wygrywa (oba API maja plytkie,
    nieskolidowane drzewo, wiec to bezpieczne uproszczenie)."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return {}
    out: dict = {}
    for el in root.iter():
        tag = el.tag.split('}')[-1] if '}' in el.tag else el.tag
        if el.text and el.text.strip() and tag not in out:
            out[tag] = el.text.strip()
    return out


async def _get(url: str, params: dict) -> str:
    # WAZNE: parametry (haslo w szczegolnosci) MUSZA byc URL-encoded - hasla
    # ze znakiem &, %, +, =, spacja itp. psuly zapytanie przy recznym
    # sklejaniu f-stringiem (np. haslo "abc&xyz" ucinalo sie na "abc" i
    # doklejalo "xyz" jako osobny, nieznany parametr) - QRZ/HamQTH dostawaly
    # zle dane i zwracaly blad logowania mimo poprawnego hasla. aiohttp z
    # params= koduje to poprawnie samo.
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, params=params, timeout=_TIMEOUT) as resp:
            return await resp.text()


# ── QRZ.com ──────────────────────────────────────────────────────────────────
async def _qrz_session(username: str, password: str, user_id: str, force: bool = False) -> str | None:
    key = (user_id, "qrz")
    if not force:
        cached = _sessions.get(key)
        if cached and cached[1] > time.time():
            return cached[0]
    try:
        txt = await _get("https://xmldata.qrz.com/xml/current/",
                          {"username": username, "password": password, "agent": _AGENT})
    except Exception as e:
        print(f"[callbook] QRZ sesja - blad polaczenia: {e}", flush=True)
        return None
    data = _flatten_xml(txt)
    if data.get("Error"):
        print(f"[callbook] QRZ blad logowania: {data['Error']}", flush=True)
        return None
    skey = data.get("Key")
    if not skey:
        return None
    _sessions[key] = (skey, time.time() + _SESSION_TTL)
    return skey


async def _qrz_lookup(call: str, session_key: str) -> dict | None:
    try:
        txt = await _get("https://xmldata.qrz.com/xml/current/",
                          {"s": session_key, "callsign": call})
    except Exception as e:
        print(f"[callbook] QRZ lookup - blad polaczenia: {e}", flush=True)
        return None
    data = _flatten_xml(txt)
    if data.get("Error") or not data.get("call"):
        return None
    fname = data.get("fname", "")
    lname = data.get("name", "")
    name = (fname + " " + lname).strip()
    return {
        "name":       name,
        "qth":        data.get("addr2", ""),
        "country":    data.get("country", ""),
        "gridsquare": data.get("grid", ""),
        "dxcc":       data.get("dxcc", ""),
        "cqz":        data.get("cqzone", ""),
        "ituz":       data.get("ituzone", ""),
        "state":      data.get("state", ""),
        "iota":       data.get("iota", ""),
        "source":     "QRZ.com",
    }


# ── HamQTH.com ───────────────────────────────────────────────────────────────
async def _hamqth_session(username: str, password: str, user_id: str, force: bool = False) -> str | None:
    key = (user_id, "hamqth")
    if not force:
        cached = _sessions.get(key)
        if cached and cached[1] > time.time():
            return cached[0]
    try:
        txt = await _get("https://www.hamqth.com/xml.php",
                          {"u": username, "p": password})
    except Exception as e:
        print(f"[callbook] HamQTH sesja - blad polaczenia: {e}", flush=True)
        return None
    data = _flatten_xml(txt)
    if data.get("error"):
        print(f"[callbook] HamQTH blad logowania: {data['error']}", flush=True)
        return None
    sid = data.get("session_id")
    if not sid:
        return None
    _sessions[key] = (sid, time.time() + _SESSION_TTL)
    return sid


async def _hamqth_lookup(call: str, session_id: str) -> dict | None:
    try:
        txt = await _get("https://www.hamqth.com/xml.php",
                          {"id": session_id, "callsign": call, "prg": _AGENT})
    except Exception as e:
        print(f"[callbook] HamQTH lookup - blad polaczenia: {e}", flush=True)
        return None
    data = _flatten_xml(txt)
    if data.get("error") or not data.get("callsign"):
        return None
    return {
        "name":       data.get("nick", ""),
        "qth":        data.get("qth", ""),
        "country":    data.get("country", ""),
        "gridsquare": data.get("grid", ""),
        "dxcc":       data.get("adif", ""),
        "cqz":        data.get("cq", ""),
        "ituz":       data.get("itu", ""),
        "state":      data.get("us_state", ""),
        "iota":       data.get("iota", ""),
        "source":     "HamQTH",
    }


# ── Public API ───────────────────────────────────────────────────────────────
async def lookup(call: str, user_id: str,
                  qrz_creds: tuple | None, hamqth_creds: tuple | None) -> dict | None:
    """Sprobuj QRZ najpierw (jesli skonfigurowany), potem HamQTH. Kazdy
    zrodlo probowane dwa razy - jesli cache'owana sesja wygasla po stronie
    serwisu (a jeszcze nie po naszej TTL), pierwszy lookup zwroci pusto;
    wtedy wymuszamy swieza sesje i probujemy raz jeszcze."""
    call = (call or "").strip().upper()
    if not call:
        return None

    if qrz_creds:
        u, p = qrz_creds
        skey = await _qrz_session(u, p, user_id)
        if skey:
            res = await _qrz_lookup(call, skey)
            if res:
                return res
            skey = await _qrz_session(u, p, user_id, force=True)
            if skey:
                res = await _qrz_lookup(call, skey)
                if res:
                    return res

    if hamqth_creds:
        u, p = hamqth_creds
        sid = await _hamqth_session(u, p, user_id)
        if sid:
            res = await _hamqth_lookup(call, sid)
            if res:
                return res
            sid = await _hamqth_session(u, p, user_id, force=True)
            if sid:
                res = await _hamqth_lookup(call, sid)
                if res:
                    return res

    return None


async def test_connection(service: str, username: str, password: str, user_id: str) -> dict:
    """Test logowania (przycisk TEST w ustawieniach) - tylko sesja, bez lookupu."""
    if service == "qrz":
        skey = await _qrz_session(username, password, user_id, force=True)
        return {"ok": bool(skey)}
    if service == "hamqth":
        sid = await _hamqth_session(username, password, user_id, force=True)
        return {"ok": bool(sid)}
    return {"ok": False, "error": "Nieznany serwis"}
