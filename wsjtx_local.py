#!/usr/bin/env python3
"""
wsjtx_local.py / wsjtx_local.exe
Ham Radio Control — WSJT-X adapter

Run by double-clicking. Doesn't require a Python install (the .exe version).
Connects WSJT-X on your computer to the radio on the remote server.

What it does:
  - asks for the server address and login credentials (once, then remembers them)
  - starts a local port 4532 for WSJT-X
  - forwards PTT / frequency / mode to the server over WebSocket
  - syncs VFO between WSJT-X and the web panel
"""

import asyncio
import json
import os
import sys
import socket
import urllib.request
import urllib.error
import pathlib

# ── Locally saved configuration ───────────────────────────────────────────────
CFG_FILE = pathlib.Path.home() / ".hamradio_wsjtx.json"
LOCAL_PORT = 4532

def load_cfg():
    try:
        return json.loads(CFG_FILE.read_text())
    except Exception:
        return {}

def save_cfg(cfg):
    try:
        CFG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass

# ── Console / GUI mode ──────────────────────────────────────────────────────────
def is_frozen():
    return getattr(sys, 'frozen', False)

def log(msg):
    print(msg, flush=True)

# ── Login and token retrieval ─────────────────────────────────────────────────
def _ssl_ctx():
    """SSL context - ignore certificate errors (self-signed)."""
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode    = ssl.CERT_NONE
    return ctx

def _make_req(url, data=None, token=None):
    """Request with browser-like headers - avoids Cloudflare blocks."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept":       "application/json, */*",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, data=data, headers=headers)

def get_token(server_http, login, password):
    url  = f"{server_http}/api/auth/login"
    data = json.dumps({"username": login, "password": password}).encode()
    try:
        req  = _make_req(url, data=data)
        resp = urllib.request.urlopen(req, timeout=10, context=_ssl_ctx())
        body = json.loads(resp.read())
        if body.get("ok") and body.get("token"):
            return body["token"], None
        return None, body.get("error", "Unknown error")
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors='replace')[:100]
        return None, f"HTTP {e.code}: {msg}"
    except urllib.error.URLError as e:
        return None, f"No connection to {server_http}: {e.reason}"
    except Exception as e:
        return None, str(e)

def test_server(server_http):
    try:
        req = _make_req(f"{server_http}/api/auth/me", token="test")
        urllib.request.urlopen(req, timeout=5, context=_ssl_ctx())
    except urllib.error.HTTPError as e:
        return e.code in (401, 403)
    except Exception:
        return False
    return True

# ── Local state (synced with the server) ─────────────────────────────────────
_state = {
    "freq":  14074000,
    "freqB": 14074000,
    "mode":  "USB",
    "bw":    2400,
    "ptt":   False,
    "split": False,
}

MODE_TO_HAMLIB = {
    "USB":"USB","LSB":"LSB","AM":"AM","FM":"FM",
    "CW":"CW","RTTY":"RTTY","PKTUSB":"PKTUSB","PKTLSB":"PKTLSB",
}
MODE_FROM_HAMLIB = {
    "USB":"USB","LSB":"LSB","AM":"AM","FM":"FM",
    "CW":"CW","CWR":"CW","RTTY":"RTTY","RTTYR":"RTTY",
    "PKTUSB":"PKTUSB","PKTLSB":"PKTLSB",
    "DATA":"PKTUSB","PKT":"PKTUSB","DIGI":"PKTUSB",
}

# ── WebSocket to the server ───────────────────────────────────────────────────
class ServerLink:
    def __init__(self, ws_url):
        self.ws_url   = ws_url
        self._ws      = None
        self._ready   = asyncio.Event()
        self._q       = asyncio.Queue()
        self.connected = False

    async def run(self):
        try:
            import websockets
            import ssl
        except ImportError:
            log("[ERROR] The websockets library is missing")
            return

        # SSL context - ignore the self-signed cert
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode    = ssl.CERT_NONE

        while True:
            try:
                extra_headers = {
                    "User-Agent": "Mozilla/5.0 wsjtx-adapter/1.0",
                    "Origin":     self.ws_url.replace("wss://","https://").replace("ws://","http://").split("/ws")[0],
                }
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    open_timeout=10,
                    ssl=ssl_ctx if self.ws_url.startswith("wss://") else None,
                    additional_headers=extra_headers,
                ) as ws:
                    self._ws       = ws
                    self.connected = True
                    self._ready.set()
                    log(f"[OK] Connected to the server")
                    await asyncio.gather(
                        self._recv(ws),
                        self._send(ws),
                    )
            except Exception as e:
                self.connected = False
                self._ready.clear()
                self._ws = None
                log(f"[!] Server disconnected: {e}")
                log(f"[.] Retrying in 5s...")
                await asyncio.sleep(5)

    async def _recv(self, ws):
        async for raw in ws:
            if not isinstance(raw, str):
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            t = msg.get("type", "")
            if t in ("init", "freq"):
                if "freq"  in msg: _state["freq"]  = int(msg["freq"])
                if "freqB" in msg: _state["freqB"] = int(msg["freqB"])
            if t in ("init", "mode"):
                if "mode"      in msg: _state["mode"] = msg["mode"]
                if "bandwidth" in msg: _state["bw"]   = int(msg["bandwidth"])
            if t in ("init", "ptt"):
                if "ptt" in msg: _state["ptt"] = bool(msg["ptt"])
            if t == "init":
                if "split" in msg: _state["split"] = bool(msg["split"])
                log(f"[Radio] {_state['freq']}Hz  {_state['mode']}  "
                    f"{'TX' if _state['ptt'] else 'RX'}")

    async def _send(self, ws):
        while True:
            msg = await self._q.get()
            await ws.send(json.dumps(msg))

    async def send(self, msg):
        await self._ready.wait()
        await self._q.put(msg)

    async def set_freq(self, hz):
        _state["freq"] = hz
        await self.send({"type": "freq", "freq": hz})

    async def set_mode(self, mode, bw=0):
        _state["mode"] = mode
        if bw: _state["bw"] = bw
        await self.send({"type": "mode", "mode": mode,
                         "bandwidth": bw or _state["bw"]})

    async def set_ptt(self, on):
        _state["ptt"] = on
        await self.send({"type": "ptt", "ptt": on})
        # When PTT turns ON — also send a signal to the browser to start the TX microphone
        if on:
            await self.send({"type": "wsjtx_tx_start"})
        else:
            await self.send({"type": "wsjtx_tx_stop"})
        log(f"[PTT] {'>>> TX <<<' if on else 'RX'}")

# ── Hamlib TCP for WSJT-X ─────────────────────────────────────────────────────
# A valid dump_state for the WSJT-X / Hamlib netrigctl protocol v0
# Matches Hamlib 4.5's network.c read_transaction()
DUMP_STATE_RESPONSE = "\n".join([
    "0",                                          # protocol v0
    "2",                                          # rig model (Dummy=1, NET=2)
    "2",                                          # ITU region 2 (Europe)
    # RX ranges: start end modes low_power high_power vfo ant
    "1800000 2000000 0x900000ff -1 -1 0x10000003 0x3",
    "3500000 4000000 0x900000ff -1 -1 0x10000003 0x3",
    "7000000 7300000 0x900000ff -1 -1 0x10000003 0x3",
    "10100000 10150000 0x900000ff -1 -1 0x10000003 0x3",
    "14000000 14350000 0x900000ff -1 -1 0x10000003 0x3",
    "18068000 18168000 0x900000ff -1 -1 0x10000003 0x3",
    "21000000 21450000 0x900000ff -1 -1 0x10000003 0x3",
    "24890000 24990000 0x900000ff -1 -1 0x10000003 0x3",
    "28000000 29700000 0x900000ff -1 -1 0x10000003 0x3",
    "0 0 0 0 0 0 0",                              # end of RX ranges
    # TX ranges: identical
    "1800000 2000000 0x900000ff -1 -1 0x10000003 0x3",
    "3500000 4000000 0x900000ff -1 -1 0x10000003 0x3",
    "7000000 7300000 0x900000ff -1 -1 0x10000003 0x3",
    "10100000 10150000 0x900000ff -1 -1 0x10000003 0x3",
    "14000000 14350000 0x900000ff -1 -1 0x10000003 0x3",
    "18068000 18168000 0x900000ff -1 -1 0x10000003 0x3",
    "21000000 21450000 0x900000ff -1 -1 0x10000003 0x3",
    "24890000 24990000 0x900000ff -1 -1 0x10000003 0x3",
    "28000000 29700000 0x900000ff -1 -1 0x10000003 0x3",
    "0 0 0 0 0 0 0",                              # end of TX ranges
    # Tuning steps: mode step
    "0x900000ff 1",
    "0 0",                                        # end of tuning steps
    # Filters: mode width
    "0x900000ff 0",
    "0 0",                                        # end of filters
    "0",                                          # max_rit
    "0",                                          # max_xit
    "0",                                          # max_ifshift
    "0",                                          # announces
    "2",                                          # ptt_type: RIG_PTT_RIG=2
    "0",                                          # dcd_type: RIG_DCD_NONE=0
    "7",                                          # port_type: RIG_PORT_NETWORK=7
    "0",                                          # serial_rate
    "0",                                          # serial_databits
    "0",                                          # serial_stopbits
    "0",                                          # serial_parity
    "0",                                          # serial_handshake
    "0",                                          # write_delay
    "0",                                          # post_write_delay
    "0",                                          # timeout
    "0",                                          # retry
    "0",                                          # has_get_func
    "0",                                          # has_set_func
    "0",                                          # has_get_level
    "0",                                          # has_set_level
    "0",                                          # has_get_parm
    "0",                                          # has_set_parm
    "RPRT 0",
])

async def handle_cmd(cmd, link):
    parts = cmd.strip().split()
    if not parts: return "RPRT 0"
    # WSJT-X sends commands with a backslash: \dump_state, \get_freq etc.
    # Normalize: strip the backslash and uppercase it
    c = parts[0].lstrip('\\').upper()

    # ── Frequency ────────────────────────────────────────────────────────────
    if c == "GET_FREQ" or (c == "F" and len(parts) == 1):
        return f"{_state['freq']}\nRPRT 0"

    if c == "SET_FREQ" or (c == "F" and len(parts) == 2):
        try:
            hz = int(float(parts[1]))
            await link.set_freq(hz)
            return "RPRT 0"
        except: return "RPRT -1"

    # ── Mode ─────────────────────────────────────────────────────────────────
    if c == "GET_MODE" or (c == "M" and len(parts) == 1):
        hm = MODE_TO_HAMLIB.get(_state["mode"], _state["mode"])
        return f"{hm}\n{_state['bw']}\nRPRT 0"

    if c == "SET_MODE" or (c == "M" and len(parts) >= 2):
        idx   = 1
        mode  = MODE_FROM_HAMLIB.get(parts[idx].upper(), parts[idx].upper())
        bw    = int(parts[idx+1]) if len(parts) > idx+1 else 0
        await link.set_mode(mode, bw)
        return "RPRT 0"

    # ── PTT ──────────────────────────────────────────────────────────────────
    if c == "GET_PTT" or (c == "T" and len(parts) == 1):
        return f"{'1' if _state['ptt'] else '0'}\nRPRT 0"

    if c == "SET_PTT" or (c == "T" and len(parts) == 2):
        on = parts[1] in ("1", "true", "True")
        await link.set_ptt(on)
        return "RPRT 0"

    # ── VFO ──────────────────────────────────────────────────────────────────
    if c in ("GET_VFO", "V") and len(parts) == 1:
        return "VFOA\nRPRT 0"
    if c in ("SET_VFO", "V"):
        return "RPRT 0"

    # ── Split ────────────────────────────────────────────────────────────────
    if c in ("GET_SPLIT_VFO", "S") and len(parts) == 1:
        return f"{'1' if _state['split'] else '0'}\nVFOB\nRPRT 0"
    if c in ("SET_SPLIT_VFO", "S"):
        return "RPRT 0"

    # ── S-meter ──────────────────────────────────────────────────────────────
    if c in ("GET_LEVEL", "L"):
        lvl = parts[1].upper() if len(parts) > 1 else ""
        if lvl == "STRENGTH":
            return "-54\nRPRT 0"
        return "0\nRPRT 0"

    # ── Info / handshake ─────────────────────────────────────────────────────
    if c in ("_", "GET_INFO"):
        return "Info: Ham Radio Control wsjtx_local\nRPRT 0"

    # ── DUMP_STATE — required by WSJT-X at startup
    if c == "DUMP_STATE":
        return DUMP_STATE_RESPONSE

    if c in ("Q", "QUIT"):
        return "RPRT 0"

    # Unknown — log but don't abort
    log(f"[?] Unknown command: {cmd!r}")
    return "RPRT 0"   # return 0 instead of -11 so WSJT-X doesn't abort

async def handle_wsjtx(reader, writer, link):
    peer = writer.get_extra_info('peername')
    log(f"[WSJT-X] Connected: {peer}")
    try:
        # netrigctl expects an immediate response after connecting.
        # Send an empty byte as a handshake - without this WSJT-X closes the connection
        writer.write(b"\n")
        await writer.drain()

        buf = b""
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=30)
            except asyncio.TimeoutError:
                writer.write(b"\n")
                await writer.drain()
                continue

            if not chunk:
                break

            buf += chunk

            while b'\n' in buf:
                line_b, buf = buf.split(b'\n', 1)
                cmd = line_b.decode(errors='replace').strip()
                if not cmd:
                    continue

                log(f"[<] {cmd!r}")
                resp = await handle_cmd_safe(cmd, link)
                log(f"[>] {resp[:80]!r}{'...' if len(resp)>80 else ''}")

                writer.write((resp + '\n').encode())
                await writer.drain()

    except ConnectionResetError:
        pass
    except Exception as e:
        log(f"[WSJT-X] Error: {type(e).__name__}: {e}")
        import traceback; log(traceback.format_exc())
    finally:
        try: writer.close()
        except: pass
        log(f"[WSJT-X] Disconnected: {peer}")


async def handle_cmd_safe(cmd, link):
    """Version of handle_cmd that doesn't hang when the link isn't ready."""
    parts = cmd.strip().split()
    if not parts:
        return "RPRT 0"
    c = parts[0].lstrip('\\').upper()
    log(f"    cmd={c!r} parts={parts}")

    if c == "DUMP_STATE":
        return DUMP_STATE_RESPONSE

    if c == "_" or c == "GET_INFO":
        return "Info: Ham Radio Control wsjtx_local\nRPRT 0"

    # CHK_VFO — return 0 = VFO mode disabled (required by netrigctl_open)
    if c == "CHK_VFO":
        return "0\nRPRT 0"

    # GET_POWERSTAT — the radio is on
    if c == "GET_POWERSTAT":
        return "1\nRPRT 0"

    if c in ("Q", "QUIT"):
        return "RPRT 0"

    # Read local state — without waiting on the WebSocket
    if c in ("GET_FREQ", "F") and len(parts) == 1:
        return f"{_state['freq']}\nRPRT 0"

    if c in ("GET_MODE", "M") and len(parts) == 1:
        hm = MODE_TO_HAMLIB.get(_state["mode"], _state["mode"])
        return f"{hm}\n{_state['bw']}\nRPRT 0"

    if c in ("GET_PTT", "T") and len(parts) == 1:
        return f"{'1' if _state['ptt'] else '0'}\nRPRT 0"

    if c in ("GET_VFO", "V") and len(parts) == 1:
        return "VFOA\nRPRT 0"

    if c in ("GET_SPLIT_VFO", "S") and len(parts) == 1:
        return f"{'1' if _state['split'] else '0'}\nVFOB\nRPRT 0"

    if c in ("GET_LEVEL", "L"):
        return "0\nRPRT 0"

    # Set commands — sent to the server with a timeout
    if not link.connected:
        log(f"    [!] Server not connected — returning RPRT 0")
        return "RPRT 0"  # fake success so WSJT-X doesn't drop the connection

    try:
        if c in ("SET_FREQ", "F") and len(parts) == 2:
            hz = int(float(parts[1]))
            await asyncio.wait_for(link.set_freq(hz), timeout=3)
            return "RPRT 0"

        if c in ("SET_MODE", "M") and len(parts) >= 2:
            mode = MODE_FROM_HAMLIB.get(parts[1].upper(), parts[1].upper())
            bw   = int(parts[2]) if len(parts) >= 3 else 0
            await asyncio.wait_for(link.set_mode(mode, bw), timeout=3)
            return "RPRT 0"

        if c in ("SET_PTT", "T") and len(parts) == 2:
            on = parts[1] in ("1", "true", "True")
            await asyncio.wait_for(link.set_ptt(on), timeout=3)
            return "RPRT 0"

        if c in ("SET_VFO", "V"):
            return "RPRT 0"

        if c in ("SET_SPLIT_VFO", "S"):
            return "RPRT 0"

    except asyncio.TimeoutError:
        log(f"    [!] Timeout sending to the server")
        return "RPRT 0"
    except Exception as e:
        log(f"    [!] Error: {e}")
        return "RPRT 0"

    log(f"    [?] Unknown command: {c!r}")
    return "RPRT 0"  # always OK - don't break the connection

# ── Check whether port 4532 is free ───────────────────────────────────────────
def port_free(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) != 0

# ── Main loop ──────────────────────────────────────────────────────────────────
async def run(ws_url):
    link = ServerLink(ws_url)
    tcp  = await asyncio.start_server(
        lambda r, w: handle_wsjtx(r, w, link),
        '127.0.0.1', LOCAL_PORT
    )
    log(f"")
    log(f"  WSJT-X Settings → Radio:")
    log(f"    Rig:  Hamlib NET rigctl")
    log(f"    Host: localhost")
    log(f"    Port: {LOCAL_PORT}")
    log(f"    PTT:  CAT")
    log(f"")
    log(f"  Click 'Test CAT' in WSJT-X...")
    log(f"  Close this window to stop.")
    log(f"")
    async with tcp:
        await asyncio.gather(link.run(), tcp.serve_forever())

# ── Setup (first run) ─────────────────────────────────────────────────────────
def setup():
    cfg = load_cfg()

    print("=" * 56)
    print("  Ham Radio Control — WSJT-X Adapter")
    print("=" * 56)
    print()

    # Server address
    default_srv = cfg.get("server", "https://your-server.example.com")
    print(f"  Server address [{default_srv}]: ", end="")
    inp = input().strip()
    server = inp if inp else default_srv
    if not server.startswith("http"):
        server = "http://" + server

    # Check whether the server responds
    print(f"  Checking {server}...", end=" ")
    if test_server(server):
        print("OK")
    else:
        print("NO RESPONSE")
        print(f"  Check the address and whether the server is running.")
        input("  Press Enter to continue anyway...")

    # Login
    default_login = cfg.get("login", "")
    print(f"  Login [{default_login}]: ", end="")
    inp = input().strip()
    login = inp if inp else default_login

    print(f"  Password: ", end="")
    password = input().strip()

    print(f"  Logging in...", end=" ")
    token, err = get_token(server, login, password)
    if not token:
        print(f"ERROR: {err}")
        input("Press Enter to close...")
        sys.exit(1)
    print("OK")

    # Save the config (without the password)
    cfg = {"server": server, "login": login, "token": token}
    save_cfg(cfg)

    return server, token

CURRENT_SERVER = "https://your-server.example.com"

def main():
    cfg = load_cfg()

    # If the saved address differs from the current one — force an update
    if cfg.get("server") and cfg["server"] != CURRENT_SERVER:
        print("=" * 56)
        print("  Ham Radio Control — WSJT-X Adapter")
        print("=" * 56)
        print(f"  NOTE: the server address has changed!")
        print(f"  Old:  {cfg['server']}")
        print(f"  New:  {CURRENT_SERVER}")
        print()
        cfg["server"] = CURRENT_SERVER
        cfg.pop("token", None)  # the token may be stale
        save_cfg(cfg)
        print("  Log in again with the new address.")
        print()

    # If we have a token — try it immediately, only prompt on failure
    if cfg.get("token") and cfg.get("server"):
        server = cfg["server"]
        token  = cfg["token"]
        print("=" * 56)
        print("  Ham Radio Control — WSJT-X Adapter")
        print("=" * 56)
        print(f"  Server: {server}")
        print(f"  Login:  {cfg.get('login','?')}")
        print()
    else:
        server, token = setup()

    # Check port 4532
    if not port_free(LOCAL_PORT):
        print(f"[!] Port {LOCAL_PORT} is in use!")
        print(f"    Check whether another program is using port {LOCAL_PORT}.")
        print(f"    (e.g. rigctld, another adapter)")
        input("Press Enter to close...")
        sys.exit(1)

    # Build the WebSocket URL
    ws_url = server.replace("http://", "ws://").replace("https://", "wss://")
    ws_url = ws_url.rstrip("/") + f"/ws?token={token}"
    print(f"[..] Connecting to {ws_url[:50]}...")

    try:
        asyncio.run(run(ws_url))
    except KeyboardInterrupt:
        print("\n[OK] Stopped")
    except Exception as e:
        print(f"\n[!] Error: {e}")
        # If it's an authorization error - clear the token and run setup again
        if "401" in str(e) or "403" in str(e) or "token" in str(e).lower():
            print("[!] Authorization problem — please log in again")
            cfg.pop("token", None)
            save_cfg(cfg)
            main()
        else:
            input("Press Enter to close...")

if __name__ == "__main__":
    # Install websockets if missing
    try:
        import websockets
    except ImportError:
        print("[..] Installing websockets...")
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install",
                        "websockets", "--quiet"])
        import websockets

    main()
