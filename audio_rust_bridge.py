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
            print("[audio_bridge] ham_audio.exe not found", flush=True)
            return False

        # Stop the old process
        if self._proc:
            try: self._proc.terminate()
            except Exception: pass
            await asyncio.sleep(0.5)

        env = os.environ.copy()
        # Set the audio device via env vars
        rx_dev = self._cfg.get("rxDevice", "")
        tx_dev = self._cfg.get("txDevice", "")
        if rx_dev: env["HAM_RX_DEVICE"] = rx_dev
        if tx_dev: env["HAM_TX_DEVICE"] = tx_dev
        env["HAM_BITRATE"] = str(self._cfg.get("bitrate", 24000))

        # ── SSL CERTIFICATE PATH FOR RUST (WSS 9443) ──────────────────────
        # PROBLEM: Rust couldn't find the cert -> "SSL cert not found" -> WSS
        # off -> no audio. Python KNOWS the cert path -> we pass it to Rust
        # via HAM_SSL_CERT/HAM_SSL_KEY (Rust reads these env vars — confirmed
        # in config.rs). KEY POINT: we look for the cert in the DATA
        # DIRECTORY (APPDATA), NOT a relative path — because in the EXE the
        # working directory is _MEIxxxx and a relative tunnel_config.json
        # can't be found there. We use DATA from config.py (the same source
        # of truth as the rest of the app).
        try:
            import json as _json
            import pathlib as _pl
            _cp = _kp = ""
            # 1. Data directory from config.py (APPDATA\HAMCTRL) — deterministic.
            try:
                from config import DATA as _DATA
                _data_dir = _pl.Path(_DATA)
            except Exception:
                _data_dir = _pl.Path(__file__).parent
            # 2. Try tunnel_config.json IN THE DATA DIRECTORY (not relatively).
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
            # 3. Fallback: the standard Let's Encrypt location in the data directory.
            #    letsencrypt\config\live\<domain>\{fullchain,privkey}.pem
            if not (_cp and _kp and _pl.Path(_cp).exists() and _pl.Path(_kp).exists()):
                _le = _data_dir / "letsencrypt" / "config" / "live"
                if _le.exists():
                    for _dom in _le.iterdir():
                        _fc = _dom / "fullchain.pem"
                        _pk = _dom / "privkey.pem"
                        if _fc.exists() and _pk.exists():
                            _cp, _kp = str(_fc), str(_pk)
                            break
            # Pass it to Rust if real files were found.
            if _cp and _kp and _pl.Path(_cp).exists() and _pl.Path(_kp).exists():
                env["HAM_SSL_CERT"] = str(_cp)
                env["HAM_SSL_KEY"] = str(_kp)
                print(f"[audio_bridge] SSL cert for Rust: {_cp}", flush=True)
            else:
                print(f"[audio_bridge] WARNING: could not find a cert for Rust "
                      f"(looked in {_data_dir}) — WSS will be OFF, no audio. "
                      f"Check whether the cert is in letsencrypt\\config\\live\\", flush=True)
        except Exception as _e:
            print(f"[audio_bridge] WARNING: could not read the cert for Rust: {_e}", flush=True)

        self._proc = subprocess.Popen(
            [str(exe)], env=env,
            cwd=str(exe.parent),
            creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == 'nt' else 0,
        )
        # Log the EXE's modification timestamp on the Python side -
        # ham_audio.exe runs as a separate process in its OWN console
        # (CREATE_NEW_CONSOLE), so its own "[build] ..." print (main.rs)
        # does NOT end up in the same log the operator actually pastes/
        # checks. Without this there's no way to tell from this log whether
        # a `cargo build` actually produced the binary under test, or
        # whether it's stale from before the rebuild.
        import datetime as _dt
        _mtime = _dt.datetime.fromtimestamp(exe.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[audio_bridge] Started ham_audio.exe PID={self._proc.pid} "
              f"(cwd={exe.parent}, file modified={_mtime})", flush=True)

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
            print("[audio_bridge] Could not connect to ham_audio", flush=True)
            return False

        print("[audio_bridge] Connected to ham_audio", flush=True)

        # Auto-start RX
        rx_dev  = self._cfg.get("rxDevice", "")
        bitrate = int(self._cfg.get("bitrate", 24000))
        vol     = float(self._cfg.get("volume", 1.0))
        if rx_dev:
            await self._send_ctrl({"cmd": "SetRxDevice", "name": rx_dev})
        await self._send_ctrl({"cmd": "SetBitrate", "bps": bitrate})
        await self._send_ctrl({"cmd": "SetVolume",  "vol": vol})
        print(f"[audio_bridge] RX auto-start: dev='{rx_dev}' bitrate={bitrate}", flush=True)

        # Start the FT8 receiver (Python listens, Rust connects)
        from ft8_rust_receiver import Ft8RustReceiver
        self._ft8_receiver = Ft8RustReceiver(port=9444)
        await self._ft8_receiver.start()

        return True

    async def stop(self):
        try: await self._send_ctrl({"cmd": "Shutdown"})
        except Exception: pass
        if self._proc: self._proc.terminate()
        self._connected = False

    # ── Control commands ─────────────────────────────────────────────────────
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
        # OBSERVED IN PRODUCTION: ListDevices can be unstable — it sometimes
        # returns an empty list (WASAPI/cpal momentarily busy, e.g. during an
        # RX device hot-swap) even though the cards genuinely exist.
        # Confirmed via frontend logs: the same call moments later returns
        # the correct 3+ cards. Instead of returning an empty list (which
        # the frontend interprets as "no cards" and silently loses the
        # saved card selection in the UI even though config.json has it
        # correctly), we retry 2 more times with a short delay before
        # concluding the cards genuinely aren't there.
        for attempt in range(3):
            r = await self._send_ctrl({"cmd": "ListDevices"})
            if isinstance(r, list) and r:
                return r
            if attempt < 2:
                await asyncio.sleep(0.3)
        return r if isinstance(r, list) else []

    async def set_rx_device(self, name: str):
        return await self._send_ctrl({"cmd": "SetRxDevice", "name": name})

    async def set_tx_device(self, name: str):
        return await self._send_ctrl({"cmd": "SetTxDevice", "name": name})

    async def set_volume(self, vol: float):
        return await self._send_ctrl({"cmd": "SetVolume", "vol": vol})

    # ── FT8 decode (from Rust) ───────────────────────────────────────────────
    async def ft8_enable_rx(self, enabled: bool, mode: str = "FT8"):
        """Enable/disable FT8 RX decode in Rust ham_audio.exe."""
        await self._send_ctrl({"cmd": "SetFt8Mode", "mode": mode})
        await self._send_ctrl({"cmd": "SetFt8Rx",   "enabled": enabled})
        if self._ft8_receiver:
            self._ft8_receiver.enable(enabled)
            self._ft8_receiver.set_mode(mode)
        print(f"[audio_bridge] FT8 RX enabled={enabled} mode={mode}", flush=True)

    async def ft8_get_decode(self) -> dict | None:
        """Fetch one FT8 decode result from the Rust queue (timeout=0.1s)."""
        if not self._ft8_receiver:
            return None
        return await self._ft8_receiver.get_decode()

    @property
    def ft8_rx_enabled(self) -> bool:
        return self._ft8_receiver.enabled if self._ft8_receiver else False


rust_audio = RustAudioBridge()
