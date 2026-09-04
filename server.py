#!/usr/bin/env python3
"""server.py — Ham Radio Control Server — entry point"""
import os as _os
# Limit BLAS/numpy threads before importing numpy (see launcher.py) — so
# audio FFT doesn't grab every core and stall the audio.
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    _os.environ.setdefault(_v, "2")
import asyncio, sys, socket, subprocess, pathlib


def _ensure_deps():
    missing = []
    try: import aiohttp
    except ImportError: missing.append("aiohttp>=3.9")
    try: import serial
    except ImportError: missing.append("pyserial>=3.5")
    try: import cryptography
    except ImportError: missing.append("cryptography")
    try: import websockets
    except ImportError: missing.append("websockets")
    try: import opuslib
    except ImportError:
        try: import opus
        except ImportError: missing.append("opuslib")
    if missing:
        print(f"[setup] Installing: {', '.join(missing)}")
        subprocess.run(
            [sys.executable,"-m","pip","install"]+missing+
            ["--quiet","--no-warn-script-location"],
            check=True
        )
    # Audio — optional
    try:
        import opuslib, opuslib.api
        print("[setup] opuslib OK")
    except Exception:
        subprocess.run(
            [sys.executable,"-m","pip","install","opuslib",
             "--quiet","--no-warn-script-location"],
            capture_output=True, timeout=120
        )
    try:
        import pyaudio
        print("[setup] pyaudio OK")
    except ImportError:
        if sys.platform == "win32":
            r = subprocess.run(
                [sys.executable,"-m","pip","install","pyaudio",
                 "--only-binary",":all:","--quiet","--no-warn-script-location"],
                capture_output=True, timeout=60
            )
            if r.returncode != 0:
                try:
                    subprocess.run(
                        [sys.executable,"-m","pip","install","pipwin",
                         "--quiet","--no-warn-script-location"],
                        capture_output=True, timeout=60
                    )
                    subprocess.run(
                        [sys.executable,"-m","pipwin","install","pyaudio"],
                        capture_output=True, timeout=120
                    )
                except Exception:
                    pass
        else:
            subprocess.run(
                [sys.executable,"-m","pip","install","pyaudio",
                 "--quiet","--no-warn-script-location"],
                capture_output=True, timeout=120
            )


_ensure_deps()

# Imports after _ensure_deps (aiohttp must already be installed)
import aiohttp.web as web
from config import PORT, ADMIN_PW, HAMLIB_PORT, DATA, VERBOSE
import os as _os
# Which interface to listen on. Default 0.0.0.0 (all interfaces) so operators on
# the LAN and the Cloudflare tunnel can both reach the server — this is required
# for normal club use. A cautious admin who only wants remote access via the
# tunnel (and nothing exposed on the LAN) can set HAM_BIND_HOST=127.0.0.1, which
# limits listening to localhost; the tunnel still works because cloudflared
# connects to localhost. Documented for clubs so 0.0.0.0 is a known choice.
BIND_HOST = _os.environ.get("HAM_BIND_HOST", "0.0.0.0")
from webapp import App

@web.middleware
async def _security_headers_middleware(request, handler):
    """
    Adds standard hardening headers to every HTTP response. Found missing
    entirely during a live security pass 2026-09-02 (curl against the
    public duckdns deployment showed zero of these on any response).
    Deliberately does NOT add script-src/style-src to the CSP below - the
    frontend relies heavily on inline onclick=/style= attributes
    throughout (201+838 in index.html, 48+30 in mobile.html, counted
    2026-09-03), so locking those two directives down would mean
    rewriting every single one to addEventListener/CSS classes first -
    a real, dedicated refactor, not a header bolted on blind. The four
    CSP directives actually shipped below don't touch inline
    scripts/styles at all, so they carry none of that risk. These are
    all safe defaults with no such risk:
      - X-Content-Type-Options: stops the browser from guessing a
        different content type than what the server declared (MIME-sniffing).
      - X-Frame-Options: DENY - this control panel is never meant to be
        embedded in another site's frame (clickjacking protection).
      - Referrer-Policy: don't leak the full URL (which can carry a
        session token in ?token=...) to a third-party site via the
        Referer header if a link is ever clicked out.
      - Strict-Transport-Security: the app is HTTPS-only already (see
        launcher.py) - tells the browser to never even try plain HTTP for
        this host.
      - Content-Security-Policy (added 2026-09-03, the safe subset only):
        object-src 'none' blocks <object>/<embed>/<applet> (this app uses
        none); base-uri 'self' stops an injected <base> tag from
        hijacking every relative URL/script src on the page (verified no
        <base> tag is used legitimately); form-action 'self' stops a
        compromised page from redirecting a form submit to an attacker
        domain (verified no form here submits externally);
        frame-ancestors 'none' is the modern equivalent of the
        X-Frame-Options above. None of these four restrict script-src or
        style-src, so none of them touch the inline onclick=/style=
        attributes mentioned above - zero risk of breaking existing UI.
    """
    resp = await handler(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    resp.headers.setdefault("Content-Security-Policy",
                             "object-src 'none'; base-uri 'self'; "
                             "form-action 'self'; frame-ancestors 'none'")
    return resp

async def amain():
    app = App()
    loop = asyncio.get_running_loop()
    app.hub.set_loop(loop)

    # Audio RX is always enabled from server startup (previously required a
    # manual button click in the UI, which has since been removed — see
    # settings.js/index.html). Uses the saved device from config, the same
    # way the old POST /api/audio/rx/start endpoint did.
    try:
        _rx_dev = app.cfg.get("audio", {}).get("rxDevice")
        if app.audio.start_rx(device=_rx_dev):
            print(f"[audio] RX auto-start OK (device={_rx_dev or 'default'})")
        else:
            print("[audio] RX auto-start FAILED — check the audio device")
    except Exception as e:
        print(f"[audio] RX auto-start error: {e}")

    async def _initial_rig_connect():
        await app.rig.connect(app.cfg)
        await app._refresh_caps_cache()
        # Auto-start the waterfall scope when a REAL radio is connected (not
        # SIM). Without this, _enable_scope was never called -> the radio
        # never sent 0x27 frames -> the waterfall only showed the
        # simulation. The frontend has startScope() but nothing was calling
        # it - hence the "waterfall stopped working" regression. The
        # backend now enables the scope itself after connecting.
        try:
            # hasattr(scope_start) alone isn't enough - EVERY CivRig
            # instance has the method regardless of model.
            # CIV_NATIVE_MODELS (added 2026-09-02 for IC-746) covers CI-V
            # radios with NO scope hardware at all - without this check,
            # startup would issue 0x27 scope commands to a radio that
            # doesn't understand them (live-seen: "scope requires 115200!"
            # + "radio REJECTED (NG)" spam on an IC-746 at 19200bd, right
            # before ALC/PWR/SWR meter reads started failing too - the same
            # capabilities gate already added to webapp.py's
            # _on_rig_reconnected() for the reconnect case, missed here for
            # the initial-startup connect).
            _has_scope = getattr(app.rig, "profile", {}).get("capabilities", {}).get("scope", False)
            if not app.rig.sim and _has_scope and hasattr(app.rig, "scope_start"):
                await asyncio.sleep(1.0)  # give the radio a moment after connect
                # scope_start does blocking writes to the port (time.sleep) —
                # run it in a thread so it doesn't freeze the loop on startup (looplag).
                await asyncio.to_thread(app.rig.scope_start)
                print("[rig] scope auto-enabled after connecting to the radio", flush=True)
        except Exception as e:
            print(f"[rig] auto scope_start error: {e}", flush=True)
        # Force the radio's own MON (CI-V 0x45, "Monitor sidetone TX") OFF
        # on the initial connect too - mirrors the same defensive call in
        # webapp.py's _on_rig_reconnected() (2026-09-04), which only covers
        # later reconnects, not this separate first-connect-at-startup path.
        # See that call site for the full rationale.
        try:
            if not app.rig.sim and hasattr(app.rig, "set_func"):
                await app.rig.set_func("MON", False)
                print("[rig] MON forced OFF on initial connect (defensive)", flush=True)
        except Exception as e:
            print(f"[rig] MON force-off on initial connect error: {e}", flush=True)

    asyncio.create_task(_initial_rig_connect())
    app.init_rotators()

    async def rot_poll():
        tick = 0
        while True:
            await asyncio.sleep(0.5)
            tick += 1
            for r in app.rotators:
                if r.moving:
                    # While moving: broadcast every 0.5s
                    await app.hub.broadcast({"type": "rotator_update", "rotator": r.state()})
                elif tick % 4 == 0:
                    # While stationary: poll the hardware for position (STATUS) every 2s, then broadcast
                    if not r.sim:
                        pos = await asyncio.to_thread(r._read_pos, 1.5)
                        if pos is not None:
                            r.az = pos
                    await app.hub.broadcast({"type": "rotator_update", "rotator": r.state()})

    # Radio polling (only when real hardware is connected — skipped in sim)
    async def rig_poll():
        cnt = 0
        while True:
            await asyncio.sleep(0.25)
            if app.rig.sim or not app.rig.connected:
                continue
            cnt += 1
            # S-meter/mode/freq broadcast is already done by the reader in
            # civ.py (_poller_loop sends it via self.bcast). This is just a
            # console heartbeat.
            # PREVIOUSLY there was extra polling here with a bug (get_smeter
            # returns the cached self.s_meter, so 'abs(lvl - s_meter)' was
            # always 0 - it never broadcast, and it also overwrote correct
            # values). Fixed 2026-07-05.

            # Console heartbeat every ~2s (freq/mode/S-meter) - only in
            # VERBOSE mode (HAM_VERBOSE=1). Silent by default - doesn't flood the console.
            if VERBOSE and cnt % 8 == 0:
                print(f"[rig] {app.rig.freq/1e6:.6f} MHz  {app.rig.mode}  "
                      f"S-meter={app.rig.s_meter:.1f}", flush=True)

    # Supervised background loops (_supervise restarts them if they crash).
    # rot_poll/rig_poll have their own try/except in the loop, but the
    # supervisor protects against an unforeseen crash of the whole task.
    app._supervise(lambda: rot_poll(), "rot_poll")
    app._supervise(lambda: rig_poll(), "rig_poll")
    app.audio.set_loop(asyncio.get_running_loop())
    app._supervise(lambda: app._ft8_rx_loop(), "ft8_rx_loop")
    app._supervise(lambda: app._waterfall_loop(), "waterfall_loop")
    asyncio.create_task(app.tunnel.autostart())
    print(f"[audio] Stream ready | opus={app.audio.get_status()['opus_lib']}")

    # ── WSJT-X UDP monitor — autostart ───────────────────────────────────────
    # NOTE: WSJT-X itself uses port 2237 as its local endpoint.
    # Our server must listen on a DIFFERENT port (default 2238).
    # In WSJT-X: Settings → Reporting → UDP Server: localhost, Port: 2238
    wsjtx_port = app.cfg.get("wsjtxUdpPort", 2238)
    wsjtx_auto = app.cfg.get("wsjtxAutostart", True)
    if wsjtx_auto:
        ok = await app.wsjtx.start(port=wsjtx_port)
        if ok:
            print(f"[wsjtx] UDP monitor active on port {wsjtx_port}")
        else:
            print(f"[wsjtx] Autostart failed — port {wsjtx_port} in use")

    # ── rigctld emulator (Hamlib NET rigctl) — port 4532 ─────────────────────
    # NOTE: we do NOT run a real rigctld.exe alongside our own CivRig — both
    # would try to open the same COM port (e.g. COM13 for the IC-7300),
    # causing a conflict: rigctld gets a TCP connection but "write_block()
    # failed" on every command, because the serial port is already held by
    # our main server (civ.py).
    #
    # Instead our emulator (hamlib_server.py) talks to the SAME self.rig
    # (CivRig) object already used by the rest of the server — without
    # opening an extra serial port. This is the only conflict-free approach
    # when one process is meant to control the radio.
    try:
        from hamlib_server import HamlibManager
        app.hamlib = HamlibManager(app)
        # FORCE a fresh config on EVERY startup — don't trust the saved
        # config from cfg.json, which could have been set to enabled=False
        # during earlier UI-panel testing and silently block every slot
        # with no error log at all.
        app.cfg['hamlibServers'] = [
            {"port": 4532, "enabled": True,  "label": "Radio 1 — WSJT-X"},
            {"port": 4533, "enabled": False, "label": "Radio 2"},
            {"port": 4534, "enabled": False, "label": "Radio 3"},
        ]
        await app.hamlib.start_all()
        _n = len(app.hamlib.servers)
        if _n > 0:
            _ports = [s.port for s in app.hamlib.servers]
            print(f"[hamlib] {_n} server(s) active on ports: {_ports}", flush=True)
        else:
            print(f"[hamlib] WARNING: ZERO servers started!", flush=True)
    except Exception as e:
        import traceback
        print(f"[hamlib] EXCEPTION on startup: {e}", flush=True)
        traceback.print_exc()
        app.hamlib = None

    web_app = web.Application(middlewares=[_security_headers_middleware])
    web_app.router.add_route("GET",    "/ws",           app.ws_handler)
    web_app.router.add_route("GET",    "/hamlib",       app.hamlib_ws_handler)
    web_app.router.add_route("GET",    "/ws/com-bridge", app.com_bridge_ws_handler)
    web_app.router.add_route("OPTIONS","/{path:.*}", app.http_handler)
    web_app.router.add_route("GET",    "/{path:.*}", app.http_handler)
    web_app.router.add_route("POST",   "/{path:.*}", app.http_handler)
    web_app.router.add_route("PUT",    "/{path:.*}", app.http_handler)
    web_app.router.add_route("PATCH",  "/{path:.*}", app.http_handler)
    web_app.router.add_route("DELETE", "/{path:.*}", app.http_handler)

    runner = web.AppRunner(web_app)
    await runner.setup()

    # ── Rust audio bridge (ham_audio.exe) ────────────────────────────────────
    try:
        from audio_rust_bridge import rust_audio
        audio_cfg = app.cfg.get("audio", {})
        started = await rust_audio.start(hub=app.hub, cfg=audio_cfg)
        if started:
            print("[server] Rust audio bridge active (low latency)", flush=True)
            app.rust_audio = rust_audio  # available but doesn't replace app.audio
        else:
            print("[server] ham_audio.exe unavailable — using PyAudio", flush=True)
            app.rust_audio = None
    except Exception as e:
        print(f"[server] Rust bridge error: {e} — using PyAudio", flush=True)
        app.rust_audio = None

    # Start HTTP (always on PORT)
    site_http = web.TCPSite(runner, BIND_HOST, PORT)

    # DeepCW - optional. If the deepcw_model/deepcw_engine modules are
    # missing, the server starts without the neural CW decoder.
    try:
        from deepcw_model import deepcw_manager
        deepcw_manager.load_from_disk()
        asyncio.ensure_future(deepcw_manager.auto_check_loop(app.hub.broadcast))

        # Auto-install DeepCW dependencies if missing
        async def _ensure_deepcw_deps():
            import importlib, subprocess, sys
            missing = []
            for pkg in ('onnxruntime', 'numpy'):
                if importlib.util.find_spec(pkg) is None:
                    missing.append(pkg)
            if missing:
                print(f"[deepcw] Installing missing packages: {missing}", flush=True)
                subprocess.check_call([sys.executable, '-m', 'pip', 'install'] + missing)
                print("[deepcw] Installation complete", flush=True)
            from deepcw_engine import deepcw_engine
            await asyncio.sleep(1)
            ok = await deepcw_engine.load()
            print(f"[deepcw] load result: {ok}", flush=True)

        asyncio.ensure_future(_ensure_deepcw_deps())
    except ImportError as e:
        print(f"[server] DeepCW module unavailable ({e}) - server starting without the neural CW decoder", flush=True)
    except Exception as e:
        print(f"[server] DeepCW init error: {e} - server continuing", flush=True)

    # ── SSL / HTTPS ───────────────────────────────────────────────────────────
    # getUserMedia (TX microphone) requires HTTPS or localhost
    # Check whether certificates exist
    import ssl as _ssl
    import pathlib as _pl
    import json as _json

    _ssl_ctx = None

    # First check for a certificate from the tunnel config (Let's Encrypt)
    _tunnel_cfg_path = DATA / "tunnel_config.json"
    _letsencrypt_cert = None
    _letsencrypt_key  = None
    if _tunnel_cfg_path.exists():
        try:
            _tcfg = _json.loads(_tunnel_cfg_path.read_text())
            _cp = _pl.Path(_tcfg.get("certPath", ""))
            _kp = _pl.Path(_tcfg.get("keyPath", ""))
            if _cp.exists() and _kp.exists():
                _letsencrypt_cert = _cp
                _letsencrypt_key  = _kp
                print(f"[ssl] Let's Encrypt cert found: {_cp}")
        except Exception as _e:
            print(f"[ssl] Error reading tunnel_config.json: {_e}")

    # cert.pem/key.pem are saved to DATA (the writable directory - next to
    # the EXE or APPDATA when installed in Program Files). Without this,
    # "Permission denied" occurs on a Program Files install (read-only).
    _cert = _letsencrypt_cert or (DATA / "cert.pem")
    _key  = _letsencrypt_key  or (DATA / "key.pem")

    if _cert.exists() and _key.exists():
        # Use the existing certificate (Let's Encrypt or self-signed)
        try:
            _ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            _ssl_ctx.load_cert_chain(str(_cert), str(_key))
            print(f"[ssl] Certificate loaded: {_cert}")
        except Exception as e:
            print(f"[ssl] Certificate error: {e} — starting HTTP")
            _ssl_ctx = None
    else:
        # Auto-generate a self-signed cert (no install required)
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            import datetime

            print("[ssl] Generating a self-signed certificate...")
            _privkey = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            _subject = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, u"your-server.ddns.net"),
            ])
            _cert_obj = (
                x509.CertificateBuilder()
                .subject_name(_subject)
                .issuer_name(_subject)
                .public_key(_privkey.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
                .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
                .add_extension(x509.SubjectAlternativeName([
                    x509.DNSName(u"your-server.ddns.net"),
                    x509.DNSName(u"localhost"),
                ]), critical=False)
                .sign(_privkey, hashes.SHA256())
            )
            _cert.write_bytes(_cert_obj.public_bytes(serialization.Encoding.PEM))
            _key.write_bytes(_privkey.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()
            ))
            _ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            _ssl_ctx.load_cert_chain(str(_cert), str(_key))
            print(f"[ssl] Self-signed cert generated — cert.pem + key.pem")
        except ImportError:
            print("[ssl] 'cryptography' missing — starting HTTP")
            print("[ssl] To enable HTTPS: pip install cryptography")
            _ssl_ctx = None

    # Start HTTP (always on PORT)
    site_http = web.TCPSite(runner, BIND_HOST, PORT)
    await site_http.start()

    # Start HTTPS on PORT+1 if we have a certificate
    _https_port = None
    if _ssl_ctx:
        _https_port = PORT + 1
        try:
            site_https = web.TCPSite(runner, BIND_HOST, _https_port, ssl_context=_ssl_ctx)
            await site_https.start()
            print(f"[ssl] HTTPS available on port {_https_port}")
        except Exception as e:
            print(f"[ssl] HTTPS error: {e}")
            _https_port = None

    # ── Certificate hot-reload (for 24/7 operation) ──────────────────────────
    # The server loads the cert at startup. When you renew it (--gen-cert,
    # manually or via the installer's scheduled task), the new file lands
    # on disk, but the running
    # server still holds the old one in memory. This task checks every 6h
    # whether the cert file changed and reloads it LIVE
    # (load_cert_chain on the existing SSLContext) - new connections get the
    # new cert, WITHOUT restarting the server. Essential for software
    # running 24/7 - the cert renews itself with zero downtime.
    if _ssl_ctx and _cert and _key:
        async def _cert_reload_watcher():
            import os as _os
            try:
                last_mtime = _os.path.getmtime(str(_cert))
            except Exception:
                last_mtime = 0
            while True:
                await asyncio.sleep(6 * 3600)  # check every 6 hours
                try:
                    # Prefer Let's Encrypt if a fresh one has appeared
                    cur_cert, cur_key = _cert, _key
                    m = _os.path.getmtime(str(cur_cert))
                    if m != last_mtime:
                        _ssl_ctx.load_cert_chain(str(cur_cert), str(cur_key))
                        last_mtime = m
                        print(f"[ssl] Certificate reloaded live (no restart): "
                              f"{cur_cert}", flush=True)
                except Exception as _e:
                    print(f"[ssl] Certificate hot-reload failed: {_e}", flush=True)
        asyncio.create_task(_cert_reload_watcher())
        print("[ssl] Certificate hot-reload active (checking every 6h) - "
              "renewal doesn't require a restart", flush=True)

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except:
        local_ip = "127.0.0.1"

    print(f"""
╔══════════════════════════════════════════════╗
║   Ham Radio Control v2.5  [Python+aiohttp]  ║
╚══════════════════════════════════════════════╝
► HTTP:  http://localhost:{PORT}
► HTTP:  http://{local_ip}:{PORT}
{f'► HTTPS: https://{local_ip}:{_https_port}  ← USE THIS (TX microphone)' if _https_port else '► HTTPS: unavailable (pip install cryptography)'}
► Login: admin / {ADMIN_PW}
► WebSocket: ws://localhost:{PORT}/ws
{f'► WebSocket SSL: wss://localhost:{_https_port}/ws' if _https_port else ''}
► Python {sys.version.split()[0]} | {sys.platform}
{f'► NOTE: self-signed cert — the browser will show a warning, click Advanced -> Proceed' if _https_port and not ((DATA / 'cert.pem').stat().st_size > 5000 if (DATA / 'cert.pem').exists() else False) else ''}
""")

    await asyncio.Event().wait()

def main():
    # Silence the Windows WinError 10054 (ConnectionResetError) in asyncio —
    # it shows up when the browser closes the connection before the server
    # does (e.g. a page refresh, closing a tab). This is normal behavior,
    # not an application bug — by default asyncio logs it as an exception,
    # cluttering the console.
    import logging
    # NOTE (stability): we do NOT silence the whole asyncio logger to
    # CRITICAL, since that would hide "Task exception was never retrieved" -
    # a signal that a background loop died silently. Instead we filter out
    # only the ConnectionResetError noise.
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    def _silence_reset(loop, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError):
            return  # ignore WinError 10054 (client dropped the connection)
        # STABILITY: a background task crash (e.g. _ft8_rx_loop,
        # _device_watchdog) must be LOUD - otherwise the feature silently
        # stops working.
        _msg = context.get("message", "")
        _task = context.get("future") or context.get("task")
        if exc is not None:
            print(f"[asyncio] UNHANDLED EXCEPTION in task: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            if _task is not None:
                print(f"[asyncio]   task: {_task!r}", flush=True)
            import traceback as _tb
            _tb.print_exception(type(exc), exc, exc.__traceback__)
        elif _msg:
            print(f"[asyncio] {_msg}", flush=True)
        loop.default_exception_handler(context)

    async def _run():
        loop = asyncio.get_event_loop()
        loop.set_exception_handler(_silence_reset)
        await amain()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
