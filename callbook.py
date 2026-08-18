"""
callbook.py — callsign lookup via QRZ.com and HamQTH.com.

Each user has THEIR OWN login credentials (stored in users.json, see
webapp.py /api/callbook/config) - QRZ requires a paid "XML Data"
subscription on the QRZ account, HamQTH is free but also requires its own
account. Lookups are done SERVER-SIDE (not from the browser) for two
reasons: neither service supports CORS for browsers, and we don't want to
expose the user's passwords in JS.

Sessions (session key/id) are cached in process memory per (user_id,
service) - logging in on every single lookup would be too frequent for
both services.

XML parsing is NAMESPACE-AGNOSTIC (we flatten the whole tree to
{local_tag_name: text}) - both services have shallow XML with no tag-name
collisions, and we have no way to live-test the exact namespace URI/
structure here, so it's safer not to rely on an exact 1:1 match.
"""
import time
import xml.etree.ElementTree as ET

import aiohttp

_AGENT = "HamRadioCTRL1.0"
_TIMEOUT = aiohttp.ClientTimeout(total=8)

# Session cache: {(user_id, service): (session_token, expires_at)}
_sessions: dict = {}
_SESSION_TTL = 3600 * 12  # both services keep sessions alive for a while, but don't trust that indefinitely


def _flatten_xml(text: str) -> dict:
    """Flatten XML to {local_tag_name: text}, ignoring namespace and
    nesting. The first occurrence of a tag wins (both APIs have a shallow,
    non-colliding tree, so this simplification is safe)."""
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
    # IMPORTANT: parameters (the password in particular) MUST be
    # URL-encoded - passwords containing &, %, +, =, spaces etc. broke the
    # request when hand-assembled with an f-string (e.g. the password
    # "abc&xyz" got cut to "abc" with "xyz" appended as a separate, unknown
    # parameter) - QRZ/HamQTH received bad data and returned a login error
    # despite a correct password. aiohttp with params= encodes this correctly on its own.
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
        print(f"[callbook] QRZ session - connection error: {e}", flush=True)
        return None
    data = _flatten_xml(txt)
    if data.get("Error"):
        print(f"[callbook] QRZ login error: {data['Error']}", flush=True)
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
        print(f"[callbook] QRZ lookup - connection error: {e}", flush=True)
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
        print(f"[callbook] HamQTH session - connection error: {e}", flush=True)
        return None
    data = _flatten_xml(txt)
    if data.get("error"):
        print(f"[callbook] HamQTH login error: {data['error']}", flush=True)
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
        print(f"[callbook] HamQTH lookup - connection error: {e}", flush=True)
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
    """Try QRZ first (if configured), then HamQTH. Each source is tried
    twice - if the cached session expired on the service's side (but not
    yet by our TTL), the first lookup returns empty; we then force a fresh
    session and try once more."""
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
    """Test the login (the TEST button in settings) - session only, no lookup."""
    if service == "qrz":
        skey = await _qrz_session(username, password, user_id, force=True)
        return {"ok": bool(skey)}
    if service == "hamqth":
        sid = await _hamqth_session(username, password, user_id, force=True)
        return {"ok": bool(sid)}
    return {"ok": False, "error": "Nieznany serwis"}
