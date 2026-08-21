"""
audio_rust_bridge.py — Python ↔ Rust ham_audio bridge
"""
import asyncio, json, subprocess, pathlib, struct, os, time
from config import VERBOSE

CTRL_PORT   = int(os.environ.get("HAM_CTRL_PORT",   9400))
WS_PORT     = int(os.environ.get("HAM_WS_PORT",     9401))
DEEPCW_PORT = int(os.environ.get("HAM_DEEPCW_PORT", 9402))

EXE_PATHS = [
    pathlib.Path(__file__).parent / "ham_audio.exe",
    pathlib.Path(__file__).parent / "ham_audio" / "target" / "release" / "ham_audio.exe",
]


# ── Windows Job Object: guarantee ham_audio.exe dies with us ────────────────
# LIVE BUG (2026-08-21): RustAudioBridge.stop() sends {"cmd":"Shutdown"} and
# terminate()s the child - but nothing in the whole codebase ever calls
# stop() on app exit (no atexit/signal handler anywhere). Closing HAMCTRL
# via the console window's X button, Ctrl+C, or a crash all leave
# ham_audio.exe running as an orphan. After enough restart cycles, orphaned
# instances pile up - each new launch panics trying to re-bind the ctrl
# port ("address already in use") because an old orphan still holds it,
# and the app ends up talking to a STALE ham_audio.exe from a previous
# session/build instead of the current one - confirmed live: RX audio from
# a fresh install/rebuild was actually being served (or not served) by an
# old orphaned process, invisible to the Python-side log entirely (it just
# connects to whatever's listening on the port, doesn't know it's stale).
#
# Fix: a Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE - the
# OS itself kills every process assigned to the job the moment the job
# handle closes, which Windows does automatically when THIS process exits
# for ANY reason (clean shutdown, Ctrl+C, console X button, or a hard
# crash) - no explicit cleanup code path to forget to call. This is the
# standard pattern browsers/IDEs use for exactly this problem; a plain
# atexit handler would only cover the clean-exit case, which isn't the one
# that actually bit us here.
_job_handle = None

def _get_or_create_job_object():
    """Returns a Job Object handle with kill-on-close set, creating it on
    first use. Windows-only; returns None on any failure (best-effort -
    if this doesn't work, behavior just falls back to the pre-existing
    "may orphan on abrupt exit" state, not a functional regression)."""
    global _job_handle
    if os.name != 'nt':
        return None
    if _job_handle is not None:
        return _job_handle
    try:
        import ctypes
        from ctypes import wintypes

        class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit",     ctypes.c_int64),
                ("LimitFlags",              wintypes.DWORD),
                ("MinimumWorkingSetSize",   ctypes.c_size_t),
                ("MaximumWorkingSetSize",   ctypes.c_size_t),
                ("ActiveProcessLimit",      wintypes.DWORD),
                ("Affinity",                ctypes.c_size_t),
                ("PriorityClass",           wintypes.DWORD),
                ("SchedulingClass",         wintypes.DWORD),
            ]

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount",  ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount",   ctypes.c_uint64),
                ("WriteTransferCount",  ctypes.c_uint64),
                ("OtherTransferCount",  ctypes.c_uint64),
            ]

        class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo",                _IO_COUNTERS),
                ("ProcessMemoryLimit",    ctypes.c_size_t),
                ("JobMemoryLimit",        ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed",     ctypes.c_size_t),
            ]

        JobObjectExtendedLimitInformation = 9
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

        kernel32 = ctypes.windll.kernel32
        h = kernel32.CreateJobObjectW(None, None)
        if not h:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(
            h, JobObjectExtendedLimitInformation,
            ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            kernel32.CloseHandle(h)
            return None
        _job_handle = h
        return h
    except Exception as e:
        print(f"[audio_bridge] Job Object setup failed (non-fatal, "
              f"ham_audio.exe may orphan on abrupt exit): {e}", flush=True)
        return None


def _assign_to_kill_on_close_job(proc: subprocess.Popen):
    """Assigns `proc` to the shared kill-on-close Job Object, so Windows
    kills it automatically when THIS process exits. Best-effort - failure
    just means the pre-existing orphan risk remains for that process."""
    job = _get_or_create_job_object()
    if not job:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        # subprocess.Popen on Windows exposes the raw process HANDLE via
        # ._handle (an int) - documented CPython implementation detail on
        # this platform, stable since it backs Popen.pid/wait() themselves.
        h_process = int(proc._handle)
        if not kernel32.AssignProcessToJobObject(job, h_process):
            print("[audio_bridge] AssignProcessToJobObject failed "
                  "(non-fatal)", flush=True)
    except Exception as e:
        print(f"[audio_bridge] AssignProcessToJobObject error (non-fatal): {e}", flush=True)


class RustAudioBridge:
    def __init__(self):
        self._proc      = None
        self._ctrl_r    = None
        self._ctrl_w    = None
        self._connected = False
        self._hub       = None
        self._cfg       = {}
        self._ft8_receiver = None  # Ft8RustReceiver instance
        self._stopping     = False   # set by stop() so the watchdog doesn't "helpfully" relaunch a deliberate shutdown
        self._watchdog_task = None
        self._restart_count = 0      # consecutive QUICK (<5s) unexpected exits — crash-loop guard
        self._last_start_at = 0.0

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

        # FIX: ham_audio.exe used to ALWAYS get its own visible console
        # (CREATE_NEW_CONSOLE), added at the time to see its own "[build]
        # ..." version marker separately from the Python log while
        # debugging the Rust decoder. For a normal release this means TWO
        # windows pop up (HAM RADIO CTRL's own console + this one) for
        # every user, most of whom never need Rust's own console at all.
        # Tied to the same VERBOSE flag as the rest of the reduced-noise
        # logging: hidden by default (CREATE_NO_WINDOW - the process still
        # runs exactly the same, just without a visible window), shown
        # again with HAM_VERBOSE=1 / VERBOSE=1 in .env for anyone who
        # actually wants to watch ham_audio's own console output live.
        if os.name == 'nt':
            _creationflags = subprocess.CREATE_NEW_CONSOLE if VERBOSE else subprocess.CREATE_NO_WINDOW
        else:
            _creationflags = 0
        # CREATE_NO_WINDOW gives the child no console at all, so its
        # stdio handles are invalid unless explicitly redirected here -
        # send them to DEVNULL rather than risk ham_audio.exe erroring out
        # on a print() with nowhere to go. Not needed (and not applied)
        # in verbose mode, where it gets a real console of its own.
        _stdio = {} if VERBOSE else {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        self._proc = subprocess.Popen(
            [str(exe)], env=env,
            cwd=str(exe.parent),
            creationflags=_creationflags,
            **_stdio,
        )
        self._stopping     = False
        self._last_start_at = time.time()
        # Kill-on-close Job Object - see the big comment above EXE_PATHS.
        # Guarantees this ham_audio.exe dies with us even if we exit
        # abruptly (console X button, crash) and never reach stop().
        _assign_to_kill_on_close_job(self._proc)
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

        # Watchdog: restarts ham_audio.exe if it exits on its own (crash, or
        # a deliberate self-exit — see the Rust-side comment in audio.rs
        # about giving up on in-process WASAPI recovery after repeated
        # failed reopen attempts and exiting instead, so a full fresh
        # process/COM-enumerator state can pick the device up cleanly).
        # FIX: reported live — a full HAMCTRL restart always restored RX
        # audio after the radio's USB Audio CODEC disappeared/reappeared
        # (power loss), but the in-process WASAPI reopen path alone did
        # not, even though it kept sending frames (silent ones). Nothing
        # anywhere previously restarted ham_audio.exe if IT exited - one
        # was needed to make a Rust-side "just exit and let Python redo
        # it fresh" strategy safe to use at all.
        if self._watchdog_task is None or self._watchdog_task.done():
            self._watchdog_task = asyncio.create_task(self._watchdog())

        return True

    async def _watchdog(self):
        # NOTE: this loop deliberately never returns after restarting (only
        # on stop()/crash-loop-giveup) and instead keeps monitoring
        # self._proc as start() replaces it - see the guard in start()
        # ("if self._watchdog_task is None or ...done()"): while THIS
        # coroutine is still suspended inside its own `await self.start(...)`
        # call below, that guard correctly sees the running task as "not
        # done" and skips spawning a duplicate - which only works because
        # this loop keeps going afterward instead of returning. Returning
        # here would silently leave the freshly-restarted process
        # unmonitored until some unrelated future start() call happened to
        # run again.
        while True:
            await asyncio.sleep(2.0)
            if self._stopping:
                return
            proc = self._proc
            if proc is None or proc.poll() is None:
                continue  # still running (or not started yet)
            ran_for = time.time() - self._last_start_at
            if ran_for < 5.0:
                self._restart_count += 1
            else:
                self._restart_count = 0
            if self._restart_count >= 5:
                print(f"[audio_bridge] ham_audio.exe exited {self._restart_count} times in a row "
                      f"within 5s of starting — giving up auto-restart (crash loop guard). "
                      f"Check the exe/device manually.", flush=True)
                return
            print(f"[audio_bridge] ham_audio.exe exited unexpectedly (ran {ran_for:.1f}s) "
                  f"— restarting automatically", flush=True)
            await self.start(self._hub, self._cfg)

    async def stop(self):
        self._stopping = True
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

    async def set_ap_hints(self, own_call: str, partner_call: str | None, queue: list[str]):
        """Push AP (a priori) decode hints to Rust ham_audio.exe - operator's
        own callsign, current QSO partner (if any), and the Call-1st queue.
        Rust biases the LDPC channel LLR toward these when a normal (blind)
        decode fails, to recover weak replies addressed to us. Mirrors
        ft8_enable_rx's fire-and-forget SetXxx pattern - safe to call often
        (e.g. every time the QSO engine's state changes), Rust just keeps
        whatever was pushed most recently."""
        await self._send_ctrl({
            "cmd": "SetApHints",
            "own_call": own_call or "",
            "partner_call": partner_call,
            "queue": list(queue or []),
        })

    async def ft8_get_decode(self) -> dict | None:
        """Fetch one FT8 decode result from the Rust queue (timeout=0.1s)."""
        if not self._ft8_receiver:
            return None
        return await self._ft8_receiver.get_decode()

    @property
    def ft8_rx_enabled(self) -> bool:
        return self._ft8_receiver.enabled if self._ft8_receiver else False


rust_audio = RustAudioBridge()
