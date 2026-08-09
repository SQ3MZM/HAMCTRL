#!/usr/bin/env python3
"""server.py — Ham Radio Control Server — punkt wejscia"""
import os as _os
# Limit watkow BLAS/numpy przed importem numpy (patrz launcher.py) — zeby FFT
# audio nie porywalo wszystkich rdzeni i nie zatykalo dzwieku.
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
        print(f"[setup] Instaluje: {', '.join(missing)}")
        subprocess.run(
            [sys.executable,"-m","pip","install"]+missing+
            ["--quiet","--no-warn-script-location"],
            check=True
        )
    # Audio — opcjonalne
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

# Importy po _ensure_deps (aiohttp musi byc juz zainstalowane)
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

async def amain():
    app = App()
    loop = asyncio.get_running_loop()
    app.hub.set_loop(loop)

    # Audio RX zawsze wlaczone od startu serwera (wczesniej wymagalo recznego
    # kliknieca przycisku w UI, ktory teraz jest usuniety — patrz settings.js/
    # index.html). Uzywamy zapisanego urzadzenia z konfiguracji, identycznie
    # jak robil to dotychczasowy endpoint POST /api/audio/rx/start.
    try:
        _rx_dev = app.cfg.get("audio", {}).get("rxDevice")
        if app.audio.start_rx(device=_rx_dev):
            print(f"[audio] RX auto-start OK (device={_rx_dev or 'domyslne'})")
        else:
            print("[audio] RX auto-start NIEUDANE — sprawdz urzadzenie audio")
    except Exception as e:
        print(f"[audio] RX auto-start blad: {e}")

    async def _initial_rig_connect():
        await app.rig.connect(app.cfg)
        await app._refresh_caps_cache()
        # Auto-start waterfall scope gdy podlaczono REALNE radio (nie SIM).
        # Bez tego _enable_scope nigdy nie bylo wolane -> radio nie wysylalo
        # ramek 0x27 -> waterfall pokazywal tylko symulacje. Frontend ma
        # startScope() ale nikt go nie wolal - stad regresja "waterfall przestal
        # dzialac". Teraz backend wlacza scope sam po polaczeniu.
        try:
            if not app.rig.sim and hasattr(app.rig, "scope_start"):
                await asyncio.sleep(1.0)  # daj radiu chwile po connect
                # scope_start robi blokujace zapisy do portu (time.sleep) —
                # w watku, zeby nie zamrozic petli przy starcie (looplag).
                await asyncio.to_thread(app.rig.scope_start)
                print("[rig] scope auto-wlaczony po polaczeniu z radiem", flush=True)
        except Exception as e:
            print(f"[rig] auto scope_start blad: {e}", flush=True)

    asyncio.create_task(_initial_rig_connect())
    app.init_rotators()

    async def rot_poll():
        tick = 0
        while True:
            await asyncio.sleep(0.5)
            tick += 1
            for r in app.rotators:
                if r.moving:
                    # Podczas ruchu: broadcastuj co 0.5s
                    await app.hub.broadcast({"type": "rotator_update", "rotator": r.state()})
                elif tick % 4 == 0:
                    # Gdy stoi: odpytaj sprzęt o pozycję (STATUS) co 2s, potem broadcast
                    if not r.sim:
                        pos = await asyncio.to_thread(r._read_pos, 1.5)
                        if pos is not None:
                            r.az = pos
                    await app.hub.broadcast({"type": "rotator_update", "rotator": r.state()})

    # Polling radia (tylko gdy podłączony realny sprzęt — w sim pomijany)
    async def rig_poll():
        cnt = 0
        while True:
            await asyncio.sleep(0.25)
            if app.rig.sim or not app.rig.connected:
                continue
            cnt += 1
            # S-meter/mode/freq broadcast robi juz reader w civ.py (_poller_loop
            # wysyla przez self.bcast). Tutaj tylko zywy status w konsoli.
            # WCZESNIEJ byl tu dodatkowy polling z bugiem (get_smeter zwraca
            # cached self.s_meter, wiec 'abs(lvl - s_meter)' zawsze 0 - nigdy
            # nie broadcastowal, a przy okazji nadpisywal poprawne wartosci).
            # Fix 2026-07-05.

            # Żywy status w konsoli co ~2s (freq/mode/S-metr) - tylko w trybie
            # VERBOSE (HAM_VERBOSE=1). Domyslnie cicho - nie zalewa konsoli.
            if VERBOSE and cnt % 8 == 0:
                print(f"[rig] {app.rig.freq/1e6:.6f} MHz  {app.rig.mode}  "
                      f"S-metr={app.rig.s_meter:.1f}", flush=True)

    # Petle tla pod nadzorem (_supervise restartuje je gdy padna).
    # rot_poll/rig_poll maja wlasne try/except w petli, ale supervisor
    # chroni przed nieprzewidzianym crashem calego taska.
    app._supervise(lambda: rot_poll(), "rot_poll")
    app._supervise(lambda: rig_poll(), "rig_poll")
    app.audio.set_loop(asyncio.get_running_loop())
    app._supervise(lambda: app._ft8_rx_loop(), "ft8_rx_loop")
    app._supervise(lambda: app._waterfall_loop(), "waterfall_loop")
    asyncio.create_task(app.tunnel.autostart())
    print(f"[audio] Stream gotowy | opus={app.audio.get_status()['opus_lib']}")

    # ── WSJT-X UDP monitor — autostart ───────────────────────────────────────
    # UWAGA: WSJT-X sam uzywa portu 2237 jako lokalny endpoint.
    # Nasz serwer musi nasluchiwac na INNYM porcie (domyslnie 2238).
    # W WSJT-X Settings → Reporting → UDP Server: localhost, Port: 2238
    wsjtx_port = app.cfg.get("wsjtxUdpPort", 2238)
    wsjtx_auto = app.cfg.get("wsjtxAutostart", True)
    if wsjtx_auto:
        ok = await app.wsjtx.start(port=wsjtx_port)
        if ok:
            print(f"[wsjtx] Monitor UDP aktywny na porcie {wsjtx_port}")
        else:
            print(f"[wsjtx] Autostart nieudany — port {wsjtx_port} zajety")

    # ── Emulator rigctld (Hamlib NET rigctl) — port 4532 ─────────────────────
    # UWAGA: NIE uruchamiamy prawdziwego rigctld.exe rownolegle do naszego
    # CivRig — oba probowalyby otworzyc ten sam port COM (np. COM13 dla
    # IC-7300), co powoduje konflikt: rigctld dostaje TCP connection ale
    # "write_block() failed" przy kazdej komendzie, bo port szeregowy jest
    # juz zajety przez nasz glowny serwer (civ.py).
    #
    # Zamiast tego nasz emulator (hamlib_server.py) rozmawia z TYM SAMYM
    # obiektem self.rig (CivRig) ktorego juz uzywa caly serwer — bez
    # otwierania dodatkowego portu szeregowego. To jedyne bezkonfliktowe
    # podejscie gdy jeden proces ma kontrolowac radio.
    try:
        from hamlib_server import HamlibManager
        app.hamlib = HamlibManager(app)
        # WYMUS swiezy config przy KAZDYM starcie — nie ufaj zapisanej
        # konfiguracji z cfg.json, ktora mogla zostac ustawiona na
        # enabled=False podczas wczesniejszych testow w panelu UI i
        # cicho blokowac wszystkie sloty bez zadnego logu bledu.
        app.cfg['hamlibServers'] = [
            {"port": 4532, "enabled": True,  "label": "Radio 1 — WSJT-X"},
            {"port": 4533, "enabled": False, "label": "Radio 2"},
            {"port": 4534, "enabled": False, "label": "Radio 3"},
        ]
        await app.hamlib.start_all()
        _n = len(app.hamlib.servers)
        if _n > 0:
            _ports = [s.port for s in app.hamlib.servers]
            print(f"[hamlib] {_n} serwer(y) aktywne na portach: {_ports}", flush=True)
        else:
            print(f"[hamlib] UWAGA: ZERO serwerow wystartowalo!", flush=True)
    except Exception as e:
        import traceback
        print(f"[hamlib] WYJATEK przy starcie: {e}", flush=True)
        traceback.print_exc()
        app.hamlib = None

    web_app = web.Application()
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
            print("[server] Rust audio bridge aktywny (niska latencja)", flush=True)
            app.rust_audio = rust_audio  # dostepny ale nie zastepuje app.audio
        else:
            print("[server] ham_audio.exe niedostępny — używam PyAudio", flush=True)
            app.rust_audio = None
    except Exception as e:
        print(f"[server] Rust bridge error: {e} — używam PyAudio", flush=True)
        app.rust_audio = None

    # Uruchom HTTP (zawsze na PORT)
    site_http = web.TCPSite(runner, BIND_HOST, PORT)

    # DeepCW - opcjonalne. Jesli brak modulow deepcw_model/deepcw_engine
    # server startuje bez neural CW decodera. Dostarczone w Sesji 6 (2026-06)
    # ale moze byc usuniete lub przeniesione.
    try:
        from deepcw_model import deepcw_manager
        deepcw_manager.load_from_disk()
        asyncio.ensure_future(deepcw_manager.auto_check_loop(app.hub.broadcast))

        # Auto-instalacja zaleznosci DeepCW jesli brak
        async def _ensure_deepcw_deps():
            import importlib, subprocess, sys
            missing = []
            for pkg in ('onnxruntime', 'numpy'):
                if importlib.util.find_spec(pkg) is None:
                    missing.append(pkg)
            if missing:
                print(f"[deepcw] Instaluję brakujące pakiety: {missing}", flush=True)
                subprocess.check_call([sys.executable, '-m', 'pip', 'install'] + missing)
                print("[deepcw] Instalacja zakończona", flush=True)
            from deepcw_engine import deepcw_engine
            await asyncio.sleep(1)
            ok = await deepcw_engine.load()
            print(f"[deepcw] load result: {ok}", flush=True)

        asyncio.ensure_future(_ensure_deepcw_deps())
    except ImportError as e:
        print(f"[server] DeepCW modul niedostepny ({e}) - server startuje bez neural CW decodera", flush=True)
    except Exception as e:
        print(f"[server] DeepCW init blad: {e} - server kontynuuje", flush=True)

    # ── SSL / HTTPS ───────────────────────────────────────────────────────────
    # getUserMedia (mikrofon TX) wymaga HTTPS lub localhost
    # Sprawdz czy sa certyfikaty
    import ssl as _ssl
    import pathlib as _pl
    import json as _json

    _ssl_ctx = None

    # Najpierw sprawdz certyfikat z konfiguracji tunelu (Let's Encrypt)
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
                print(f"[ssl] Let's Encrypt cert znaleziony: {_cp}")
        except Exception as _e:
            print(f"[ssl] Blad odczytu tunnel_config.json: {_e}")

    # cert.pem/key.pem zapisywane do DATA (zapisywalny katalog - obok EXE albo
    # APPDATA gdy Program Files). Bez tego "Permission denied" przy instalacji
    # do Program Files (read-only).
    _cert = _letsencrypt_cert or (DATA / "cert.pem")
    _key  = _letsencrypt_key  or (DATA / "key.pem")

    if _cert.exists() and _key.exists():
        # Uzywaj istniejacego certyfikatu (Let's Encrypt lub self-signed)
        try:
            _ssl_ctx = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
            _ssl_ctx.load_cert_chain(str(_cert), str(_key))
            print(f"[ssl] Certyfikat zaladowany: {_cert}")
        except Exception as e:
            print(f"[ssl] Blad certyfikatu: {e} — uruchamiam HTTP")
            _ssl_ctx = None
    else:
        # Auto-generuj self-signed cert (nie wymaga instalacji)
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            import datetime

            print("[ssl] Generuje self-signed certyfikat...")
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
            print(f"[ssl] Self-signed cert wygenerowany — cert.pem + key.pem")
        except ImportError:
            print("[ssl] Brak 'cryptography' — uruchamiam HTTP")
            print("[ssl] Aby wlaczyc HTTPS: pip install cryptography")
            _ssl_ctx = None

    # Uruchom HTTP (zawsze na PORT)
    site_http = web.TCPSite(runner, BIND_HOST, PORT)
    await site_http.start()

    # Uruchom HTTPS na PORT+1 jesli mamy certyfikat
    _https_port = None
    if _ssl_ctx:
        _https_port = PORT + 1
        try:
            site_https = web.TCPSite(runner, BIND_HOST, _https_port, ssl_context=_ssl_ctx)
            await site_https.start()
            print(f"[ssl] HTTPS dostepny na porcie {_https_port}")
        except Exception as e:
            print(f"[ssl] HTTPS blad: {e}")
            _https_port = None

    # ── Hot-reload certyfikatu (dla pracy 24/7) ──────────────────────────────
    # Serwer laduje cert przy starcie. Gdy odnowisz go (--gen-cert albo
    # auto-renew certbota), nowy plik trafia na dysk, ale zywy serwer trzyma
    # stary w pamieci. Ten watek sprawdza co 6h czy plik certu sie zmienil i
    # przeladowuje go NA ZYWO (load_cert_chain na istniejacym SSLContext) -
    # nowe polaczenia dostaja nowy cert, BEZ restartu serwera. Kluczowe dla
    # softu chodzacego 24h - cert odnawia sie sam, zero przerwy.
    if _ssl_ctx and _cert and _key:
        async def _cert_reload_watcher():
            import os as _os
            try:
                last_mtime = _os.path.getmtime(str(_cert))
            except Exception:
                last_mtime = 0
            while True:
                await asyncio.sleep(6 * 3600)  # sprawdzaj co 6 godzin
                try:
                    # Preferuj Let's Encrypt jesli pojawil sie swiezy
                    cur_cert, cur_key = _cert, _key
                    m = _os.path.getmtime(str(cur_cert))
                    if m != last_mtime:
                        _ssl_ctx.load_cert_chain(str(cur_cert), str(cur_key))
                        last_mtime = m
                        print(f"[ssl] Certyfikat przeladowany na zywo (bez restartu): "
                              f"{cur_cert}", flush=True)
                except Exception as _e:
                    print(f"[ssl] Hot-reload certu nieudany: {_e}", flush=True)
        asyncio.create_task(_cert_reload_watcher())
        print("[ssl] Hot-reload certu aktywny (sprawdzanie co 6h) - "
              "odnowienie nie wymaga restartu", flush=True)

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
{f'► HTTPS: https://{local_ip}:{_https_port}  ← UZYJ TEGO (mikrofon TX)' if _https_port else '► HTTPS: niedostepny (pip install cryptography)'}
► Login: admin / {ADMIN_PW}
► WebSocket: ws://localhost:{PORT}/ws
{f'► WebSocket SSL: wss://localhost:{_https_port}/ws' if _https_port else ''}
► Python {sys.version.split()[0]} | {sys.platform}
{f'► UWAGA: self-signed cert — przegladarka pokaze ostrzezenie, kliknij Zaawansowane → Kontynuuj' if _https_port and not ((DATA / 'cert.pem').stat().st_size > 5000 if (DATA / 'cert.pem').exists() else False) else ''}
""")

    await asyncio.Event().wait()

def main():
    # Wycisz blad Windows WinError 10054 (ConnectionResetError) w asyncio —
    # pojawia sie gdy przegladarka zamknie polaczenie zanim serwer to zrobi
    # (np. odswiezenie strony, zamkniecie zakladki). To normalne zachowanie,
    # nie blad aplikacji — domyslnie asyncio loguje to jako wyjatek co
    # zasmiecal konsole.
    import logging
    # UWAGA (stabilnosc): NIE tlumimy calego loggera asyncio na CRITICAL, bo
    # ukrywaloby to "Task exception was never retrieved" - sygnal ze petla tla
    # cicho umarla. Zamiast tego filtrujemy tylko szum ConnectionResetError.
    logging.getLogger("asyncio").setLevel(logging.WARNING)

    def _silence_reset(loop, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError):
            return  # ignoruj WinError 10054 (klient zerwal polaczenie)
        # STABILNOSC: crash taska tla (np. _ft8_rx_loop, _device_watchdog)
        # musi byc GLOSNY - inaczej funkcja przestaje dzialac po cichu.
        _msg = context.get("message", "")
        _task = context.get("future") or context.get("task")
        if exc is not None:
            print(f"[asyncio] NIEOBSLUZONY WYJATEK w tasku: "
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
