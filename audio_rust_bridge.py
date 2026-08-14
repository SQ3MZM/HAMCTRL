"""
audio_rust_bridge.py — Python ↔ Rust ham_audio bridge
"""
import asyncio, json, subprocess, pathlib, struct, os

CTRL_PORT   = int(os.environ.get("HAM_CTRL_PORT",   9400))
WS_PORT     = int(os.environ.get("HAM_WS_PORT",     9401))
DEEPCW_PORT = int(os.environ.get("HAM_DEEPCW_PORT", 9402))

EXE_PATHS = [
    pathlib.Path(__file__).parent / "ham_audio.exe",
    pathlib.Path(__file__).parent / "ham_audio" / "target" / "release" / "ham_audio.exe",
]


class RustAudioBridge:
    def __init__(self):
        self._proc      = None
        self._ctrl_r    = None
        self._ctrl_w    = None
        self._connected = False
        self._hub       = None
        self._cfg       = {}
        self._ft8_receiver = None  # Ft8RustReceiver instance

    async def start(self, hub=None, cfg: dict = None):
        self._hub = hub
        if cfg is not None:
            self._cfg = cfg
        self._connected = False
        self._ctrl_r = None
        self._ctrl_w = None

        exe = next((p for p in EXE_PATHS if p.exists()), None)
        if not exe:
            print("[audio_bridge] ham_audio.exe nie znaleziony", flush=True)
            return False

        # Zatrzymaj stary proces
        if self._proc:
            try: self._proc.terminate()
            except Exception: pass
            await asyncio.sleep(0.5)

        env = os.environ.copy()
        # Ustaw karte przez env
        rx_dev = self._cfg.get("rxDevice", "")
        tx_dev = self._cfg.get("txDevice", "")
        if rx_dev: env["HAM_RX_DEVICE"] = rx_dev
        if tx_dev: env["HAM_TX_DEVICE"] = tx_dev
        env["HAM_BITRATE"] = str(self._cfg.get("bitrate", 24000))

        # ── SCIEZKA CERTYFIKATU SSL DLA RUSTA (WSS 9443) ──────────────────────
        # PROBLEM: Rust nie znajdowal certu -> "SSL cert not found" -> WSS off
        # -> brak dzwieku. Python ZNA sciezke certu -> przekazujemy ja Rustowi
        # przez HAM_SSL_CERT/HAM_SSL_KEY (Rust czyta te zmienne — potwierdzone
        # w config.rs). KLUCZOWE: szukamy certu w KATALOGU DANYCH (APPDATA),
        # NIE sciezka wzgledna — bo w EXE katalog roboczy to _MEIxxxx i wzgledny
        # tunnel_config.json sie nie znajduje. Uzywamy DATA z config.py (to samo
        # zrodlo prawdy co reszta aplikacji).
        try:
            import json as _json
            import pathlib as _pl
            _cp = _kp = ""
            # 1. Katalog danych z config.py (APPDATA\HAMCTRL) — deterministyczny.
            try:
                from config import DATA as _DATA
                _data_dir = _pl.Path(_DATA)
            except Exception:
                _data_dir = _pl.Path(__file__).parent
            # 2. Sprobuj tunnel_config.json W KATALOGU DANYCH (nie wzglednie).
            for _cand in (_data_dir / "tunnel_config.json",
                          _pl.Path("tunnel_config.json")):
                if _cand.exists():
                    try:
                        _t = _json.loads(_cand.read_text())
                        _cp = _t.get("certPath", "") or _cp
                        _kp = _t.get("keyPath", "") or _kp
                        if _cp and _kp:
                            break
                    except Exception:
                        pass
            # 3. Fallback: standardowa lokalizacja Let's Encrypt w katalogu danych.
            #    letsencrypt\config\live\<domena>\{fullchain,privkey}.pem
            if not (_cp and _kp and _pl.Path(_cp).exists() and _pl.Path(_kp).exists()):
                _le = _data_dir / "letsencrypt" / "config" / "live"
                if _le.exists():
                    for _dom in _le.iterdir():
                        _fc = _dom / "fullchain.pem"
                        _pk = _dom / "privkey.pem"
                        if _fc.exists() and _pk.exists():
                            _cp, _kp = str(_fc), str(_pk)
                            break
            # Przekaz Rustowi jesli znaleziono realne pliki.
            if _cp and _kp and _pl.Path(_cp).exists() and _pl.Path(_kp).exists():
                env["HAM_SSL_CERT"] = str(_cp)
                env["HAM_SSL_KEY"] = str(_kp)
                print(f"[audio_bridge] cert SSL dla Rusta: {_cp}", flush=True)
            else:
                print(f"[audio_bridge] UWAGA: nie znalazlem certu dla Rusta "
                      f"(szukalem w {_data_dir}) — WSS bedzie OFF, brak dzwieku. "
                      f"Sprawdz czy cert jest w letsencrypt\\config\\live\\", flush=True)
        except Exception as _e:
            print(f"[audio_bridge] UWAGA: nie odczytalem certu dla Rusta: {_e}", flush=True)

        self._proc = subprocess.Popen(
            [str(exe)], env=env,
            cwd=str(exe.parent),
            creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0,
        )
        # Znacznik czasu modyfikacji EXE w logu Pythona - ham_audio.exe biegnie
        # jako osobny proces w OSOBNEJ konsoli (CREATE_NEW_CONSOLE), wiec jego
        # wlasny "[build] ..." print (main.rs) NIE trafia do tego samego logu,
        # ktory operator faktycznie wkleja/sprawdza. Bez tego nie ma jak z tego
        # loga stwierdzic, czy po `cargo build` faktycznie testowana jest nowa
        # binarka, a nie stara sprzed rebuildu.
        import datetime as _dt
        _mtime = _dt.datetime.fromtimestamp(exe.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[audio_bridge] Uruchomiono ham_audio.exe PID={self._proc.pid} "
              f"(cwd={exe.parent}, plik zmieniony={_mtime})", flush=True)

        for _ in range(20):
            await asyncio.sleep(0.3)
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", CTRL_PORT)
                self._ctrl_r = reader
                self._ctrl_w = writer
                self._connected = True
                break
            except Exception:
                pass

        if not self._connected:
            print("[audio_bridge] Nie można połączyć z ham_audio", flush=True)
            return False

        print("[audio_bridge] Połączono z ham_audio", flush=True)

        # Auto-start RX
        rx_dev  = self._cfg.get("rxDevice", "")
        bitrate = int(self._cfg.get("bitrate", 24000))
        vol     = float(self._cfg.get("volume", 1.0))
        if rx_dev:
            await self._send_ctrl({"cmd": "SetRxDevice", "name": rx_dev})
        await self._send_ctrl({"cmd": "SetBitrate", "bps": bitrate})
        await self._send_ctrl({"cmd": "SetVolume",  "vol": vol})
        print(f"[audio_bridge] RX auto-start: dev='{rx_dev}' bitrate={bitrate}", flush=True)

        # Uruchom FT8 receiver (Python nasluchuje, Rust sie laczy)
        from ft8_rust_receiver import Ft8RustReceiver
        self._ft8_receiver = Ft8RustReceiver(port=9444)
        await self._ft8_receiver.start()

        return True

    async def stop(self):
        try: await self._send_ctrl({"cmd": "Shutdown"})
        except Exception: pass
        if self._proc: self._proc.terminate()
        self._connected = False

    # ── Komendy kontrolne ────────────────────────────────────────────────────
    async def _send_ctrl(self, cmd: dict) -> dict:
        if not self._ctrl_w: return {"error": "not connected"}
        self._ctrl_w.write((json.dumps(cmd) + "\n").encode())
        await self._ctrl_w.drain()
        try:
            resp = await asyncio.wait_for(self._ctrl_r.readline(), timeout=2.0)
            return json.loads(resp.decode())
        except Exception as e:
            return {"error": str(e)}

    async def get_status(self) -> dict:
        return await self._send_ctrl({"cmd": "GetStatus"})

    async def list_devices(self) -> list:
        r = await self._send_ctrl({"cmd": "ListDevices"})
        return r if isinstance(r, list) else []

    async def set_rx_device(self, name: str):
        return await self._send_ctrl({"cmd": "SetRxDevice", "name": name})

    async def set_tx_device(self, name: str):
        return await self._send_ctrl({"cmd": "SetTxDevice", "name": name})

    async def set_volume(self, vol: float):
        return await self._send_ctrl({"cmd": "SetVolume", "vol": vol})

    # ── WebSocket proxy ───────────────────────────────────────────────────────
    async def ws_proxy_rx(self, client_ws):
        """Jednostronny proxy RX: Rust ham_audio → przeglądarka."""
        import aiohttp
        print("[audio_bridge] ws_proxy_rx start", flush=True)
        frames = 0
        while True:
            try:
                timeout = aiohttp.ClientTimeout(total=None, connect=5)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.ws_connect(
                        f"ws://127.0.0.1:{WS_PORT}",
                        heartbeat=30,
                        max_msg_size=0
                    ) as rust_ws:
                        print("[audio_bridge] ws_proxy_rx połączono z Rust", flush=True)
                        async for msg in rust_ws:
                            if msg.type == aiohttp.WSMsgType.BINARY:
                                frames += 1
                                if frames <= 5 or frames % 200 == 0:
                                    print(f"[audio_bridge] RX frame #{frames} len={len(msg.data)}", flush=True)
                                try:
                                    await client_ws.send_bytes(msg.data)
                                except Exception as e:
                                    print(f"[audio_bridge] send_bytes error: {e}", flush=True)
                                    return  # klient rozłączony — koniec
                            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                                print(f"[audio_bridge] Rust WS close/error: {msg.type}", flush=True)
                                break
            except Exception as e:
                print(f"[audio_bridge] ws_proxy_rx error: {e}", flush=True)
            # Sprawdz czy klient nadal połączony
            if client_ws.closed:
                print(f"[audio_bridge] klient rozłączony, frames={frames}", flush=True)
                return
            await asyncio.sleep(1)
            print("[audio_bridge] ws_proxy_rx reconnect...", flush=True)

    async def ws_proxy(self, client_ws):
        """Proxy aiohttp WS przeglądarki ↔ ham_audio WS port 9401."""
        print(f"[audio_bridge] ws_proxy start port={WS_PORT}", flush=True)
        import aiohttp
        session = aiohttp.ClientSession()
        try:
            async with session.ws_connect(f"ws://127.0.0.1:{WS_PORT}") as rust_ws:
                async def fwd_to_client():
                    async for msg in rust_ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            try: await client_ws.send_bytes(msg.data)
                            except Exception: break
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                            break

                async def fwd_to_rust():
                    async for msg in client_ws:
                        if msg.type == aiohttp.WSMsgType.BINARY:
                            try: await rust_ws.send_bytes(msg.data)
                            except Exception: break
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                            break

                await asyncio.gather(fwd_to_client(), fwd_to_rust(), return_exceptions=True)
        except Exception as e:
            print(f"[audio_bridge] ws_proxy error: {e}", flush=True)
        finally:
            await session.close()

    # ── FT8 decode (z Rust) ─────────────────────────────────────────────────
    async def ft8_enable_rx(self, enabled: bool, mode: str = "FT8"):
        """Wlacz/wylacz FT8 RX decode w Rust ham_audio.exe."""
        await self._send_ctrl({"cmd": "SetFt8Mode", "mode": mode})
        await self._send_ctrl({"cmd": "SetFt8Rx",   "enabled": enabled})
        if self._ft8_receiver:
            self._ft8_receiver.enable(enabled)
            self._ft8_receiver.set_mode(mode)
        print(f"[audio_bridge] FT8 RX enabled={enabled} mode={mode}", flush=True)

    async def ft8_get_decode(self) -> dict | None:
        """Pobierz jeden wynik dekodowania FT8 z kolejki Rust (timeout=0.1s)."""
        if not self._ft8_receiver:
            return None
        return await self._ft8_receiver.get_decode()

    @property
    def ft8_rx_enabled(self) -> bool:
        return self._ft8_receiver.enabled if self._ft8_receiver else False


rust_audio = RustAudioBridge()
