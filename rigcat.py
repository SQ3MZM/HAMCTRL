#!/usr/bin/env python3
"""
rigcat.py — radio control via Hamlib (rigctld) over TCP.
On Replit (no rigctld) -> simulation.

NOTE: Hamlib is NOT a Python library. It's a separate program,
`rigctld(.exe)`. The server launches it itself and talks to it over TCP
(default 127.0.0.1:4532). Windows install: download Hamlib
(hamlib-w64-*.zip) from hamlib.github.io, extract it e.g. to C:\\hamlib.
The server looks for rigctld.exe in:
  - HAMLIB_PATH from .env
  - C:\\Program Files\\hamlib-w64-4.7.1\\bin\\rigctld.exe
  - C:\\hamlib\\bin\\rigctld.exe
  - PATH (where rigctld)
"""
import asyncio, re, sys, socket, subprocess
from pathlib import Path
from config import HAMLIB, HAMLIB_PORT, RIGCTLD_LOG, ENV


class RigCAT:
    def __init__(self):
        self.freq      = 14074000
        self.mode      = "USB"
        self.bw        = 2400
        self.ptt       = False
        self.split     = False
        self.freq_b    = 14074000
        self.s_meter   = 0.0
        self.connected = False
        self.sim       = True
        self._proc     = None
        self._logf     = None
        self._lock     = asyncio.Lock()
        self.last_err  = ""
        self.last_msg  = ""
        self.rigctld_path = ""
        self.rigctld_found = False

    async def _cmd(self, cmd: str, timeout=4.0) -> str:
        async with self._lock:
            r, w = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", HAMLIB_PORT), timeout=3.0)
            try:
                w.write((cmd + "\n").encode())
                await w.drain()
                buf = ""
                while "RPRT" not in buf:
                    # 1st read: full timeout; subsequent ones (once we have
                    # data): short, so we don't wait full seconds when
                    # rigctld doesn't send RPRT.
                    t = timeout if not buf else 0.2
                    try:
                        chunk = await asyncio.wait_for(r.read(512), timeout=t)
                    except asyncio.TimeoutError:
                        if buf.strip():
                            break  # we have data without RPRT — good enough
                        raise
                    if not chunk: break
                    buf += chunk.decode(errors="replace")
                if "RPRT" in buf:
                    data = buf[:buf.index("RPRT")].strip()
                    m = re.search(r"RPRT\s+(-?\d+)", buf)
                    code = int(m.group(1)) if m else 0
                    if code != 0:
                        raise IOError(f"RPRT {code}")
                else:
                    # only accept a complete response (with a line ending)
                    if "\n" not in buf:
                        # NOTE: kept in Polish - this message can end up in
                        # self.last_err, which is returned to the frontend UI
                        # (see connect()'s except block), not just logged.
                        raise asyncio.TimeoutError("niekompletna odpowiedz bez RPRT")
                    data = buf.strip()
                return data
            finally:
                try: w.close()
                except: pass

    async def get_freq(self) -> int:
        if self.sim: return self.freq
        try:
            r = await self._cmd("f")
        except Exception as e:
            print(f"[rig] get_freq ERROR: {type(e).__name__}: {e}")
            raise
        # rigctld 'f' returns just the Hz number on its own line — first
        # look for a full number-only line, then fall back to any large
        # token (resilience).
        for l in (ln.strip() for ln in r.splitlines()):
            if re.fullmatch(r"\d{6,}", l):
                return int(l)
        for tok in re.findall(r"\d+", r):
            if int(tok) > 100000:
                return int(tok)
        print(f"[rig] get_freq: could not recognize a frequency in the response: {r!r}")
        return self.freq

    async def get_mode(self) -> tuple[str, int]:
        if self.sim: return self.mode, self.bw
        r = await self._cmd("m")
        lines = [l.strip() for l in r.strip().splitlines() if l.strip()]
        mode = lines[0] if lines else "USB"
        bw = 2400
        for l in lines[1:]:
            if l.isdigit():
                bw = int(l); break
        return mode, bw

    async def get_smeter(self) -> float:
        if self.sim: return self.s_meter
        try:
            r = await self._cmd("l STRENGTH")
        except Exception as e:
            print(f"[rig] get_smeter ERROR: {type(e).__name__}: {e}")
            return self.s_meter
        try:
            db = float(re.findall(r"-?\d+\.?\d*", r)[0])
        except Exception:
            return self.s_meter
        # Hamlib STRENGTH = dB relative to S9 (0 dB = S9). The panel expects
        # S-units: above S9 ~6 dB/S-unit, beyond that the "S9+xx dB" scale
        # (val>9 -> (val-9)*10 dB).
        s = 9 + db / 6.0 if db <= 0 else 9 + db / 10.0
        return max(0.0, s)

    async def set_freq(self, hz: int):
        if self.sim: self.freq = hz; return
        await self._cmd(f"F {hz}"); self.freq = hz

    async def set_mode(self, m, bw=0):
        if self.sim:
            self.mode = m; self.bw = bw if bw else self.bw; return
        await self._cmd(f"M {m} {bw}"); self.mode = m
        if bw: self.bw = bw

    async def set_ptt(self, on: bool):
        if self.sim: self.ptt = on; return
        await self._cmd(f"T {1 if on else 0}"); self.ptt = on

    async def set_level(self, l, v):
        if self.sim: return
        await self._cmd(f"L {l} {v}")

    async def set_split(self, on: bool):
        if self.sim: self.split = on; return
        await self._cmd(f"S {1 if on else 0} VFOB"); self.split = on

    async def set_vfo(self, vfo: str):
        """Switch the active VFO (VFOA/VFOB) — Hamlib command 'V'."""
        if self.sim: return
        await self._cmd(f"V {vfo}")

    async def set_func(self, func: str, on: bool):
        """Enable/disable a radio function (NB/VOX/COMP/...) — Hamlib command 'U'."""
        if self.sim: return
        await self._cmd(f"U {func} {1 if on else 0}")

    async def get_capabilities(self) -> dict:
        """
        Returns the full structure of discovered radio capabilities:
        {"actions": [...], "sliders": [...], "raw_caps": {feature_id: bool}}

        - raw_caps: simple bools for rigs/features.py (freq_set, ptt, split, ...)
        - actions: buttons (VFO A/B, NB/VOX/COMP/... toggles)
        - sliders: adjustments with a range (RFPOWER, AF, MICGAIN, ...)

        In simulation mode (self.sim) returns empty lists/dict — the admin
        can configure manually when testing without hardware.
        """
        if self.sim:
            return {"actions": [], "sliders": [], "raw_caps": {}}
        from hamlib_caps import discover_capabilities
        return await discover_capabilities(HAMLIB_PORT)

    async def connect(self, cfg: dict, override: dict | None = None) -> bool:
        # Pick the radio: by rigId from override, otherwise the first one.
        # Merge in the form values (override), so CIV/port/model from the UI
        # take effect immediately.
        rigs = cfg.get("rigs") or [{}]
        rig  = rigs[0]
        if override:
            rid = override.get("rigId") or override.get("id")
            if rid is not None:
                rig = next((r for r in rigs if str(r.get("id")) == str(rid)), rig)
            rig = {**rig, **{k: v for k, v in override.items() if v}}
        model = str(rig.get("model", ENV.get("RIG1_MODEL", "3073")))
        port  = rig.get("port",  ENV.get("RIG1_PORT", "COM3"))
        speed = str(rig.get("speed", ENV.get("RIG1_SPEED", "19200")))
        # Find rigctld
        ham = HAMLIB
        self.rigctld_found = False
        if Path(ham).exists():
            self.rigctld_found = True
        else:
            import glob as _glob
            _cands = [
                r"C:\Program Files\hamlib-w64-4.7.1\bin\rigctld.exe",
                r"C:\hamlib\bin\rigctld.exe",
            ]
            # Any hamlib-w64-* version in Program Files
            _cands += sorted(_glob.glob(r"C:\Program Files\hamlib-*\bin\rigctld.exe"), reverse=True)
            _cands += sorted(_glob.glob(r"C:\hamlib*\bin\rigctld.exe"), reverse=True)
            for _c in _cands:
                if Path(_c).exists(): ham = _c; self.rigctld_found = True; break
            if not self.rigctld_found:
                try:
                    _which = "where" if sys.platform == "win32" else "which"
                    _r = subprocess.run([_which, "rigctld"], capture_output=True, text=True, timeout=2)
                    if _r.returncode == 0 and _r.stdout.strip():
                        ham = _r.stdout.strip().splitlines()[0]
                        self.rigctld_found = True
                except: pass
        self.rigctld_path = ham
        print(f"[rig] rigctld: {ham} ({'found' if self.rigctld_found else 'NOT found'})")
        if not self.rigctld_found:
            self.last_err = ("Hamlib (rigctld.exe) nie znaleziony. Zainstaluj Hamlib dla Windows "
                             "i rozpakuj do C:\\hamlib (lub Program Files), albo dodaj rigctld do PATH.")
            self.last_msg = ""
            self.sim = True
            self.connected = False
            print(f"[rig] {self.last_err}")
            return False
        try:
            civ = rig.get("civ") or rig.get("civAddr") or ENV.get("RIG1_CIV", "")
            cmd = [ham, "-m", model, "-r", port, "-s", speed,
                   "-t", str(HAMLIB_PORT), "--set-conf=serial_handshake=None"]
            if civ:
                civ_clean = (str(civ).strip().replace("0x", "").replace("0X", "")
                             .replace("h", "").replace("H", ""))
                cmd += [f"--set-conf=civ_addr=0x{civ_clean}"]
                print(f"[rig] CIV addr: 0x{civ_clean}")
            # Verbose rigctld -> rigctld.log file (diagnostics, for when the radio doesn't respond)
            cmd += ["-vvvvv"]
            print(f"[rig] CMD: {' '.join(cmd)}")
            print(f"[rig] rigctld log: {RIGCTLD_LOG}")
            # Is rigctld already running on this port (e.g. started by N1MM)?
            _port_busy = False
            try:
                _t = socket.socket(); _t.settimeout(0.5)
                _port_busy = _t.connect_ex(("127.0.0.1", HAMLIB_PORT)) == 0
                _t.close()
            except: pass
            if _port_busy:
                print(f"[rig] Port {HAMLIB_PORT} in use — connecting to the existing rigctld")
            else:
                try:
                    self._logf = open(RIGCTLD_LOG, "wb")
                except Exception:
                    self._logf = None
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=(self._logf or subprocess.DEVNULL),
                    stderr=(self._logf or subprocess.PIPE))
                await asyncio.sleep(3.5)
                if self._proc.poll() is not None:
                    # NOTE: kept in Polish - this text can end up in
                    # self.last_err via the except block below, which is
                    # returned to the frontend UI, not just logged.
                    se = self._rigctld_log_tail() or "rigctld zamknal sie bez komunikatu"
                    raise IOError(f"rigctld zakonczyl sie: {se}")
                print(f"[rig] rigctld started on port {HAMLIB_PORT}")
            self.sim = False
            # The radio may need a moment — a few attempts at reading the frequency
            last_exc = None
            for _try in range(3):
                try:
                    self.freq = await self.get_freq()
                    last_exc = None
                    break
                except Exception as ex:
                    last_exc = ex
                    await asyncio.sleep(1.0)
            if last_exc:
                raise last_exc
            self.mode, self.bw = await self.get_mode()
            self.connected = True
            self.last_err  = ""
            self.last_msg  = (f"{rig.get('name','Radio')} model={model} {port} "
                              f"{speed}bd — {self.freq}Hz {self.mode}")
            print(f"[rig] {self.last_msg}")
            return True
        except Exception as e:
            tail = self._rigctld_log_tail()
            base = f"{type(e).__name__}: {e}".strip()
            if isinstance(e, (asyncio.TimeoutError, TimeoutError)) and not str(e):
                # NOTE: kept in Polish - assigned to self.last_err below,
                # which is returned to the frontend UI, not just logged.
                base = ("Radio nie odpowiada (timeout). rigctld dziala, ale nie dostaje "
                        "odpowiedzi z radia — sprawdz: zwolnij COM (zamknij RCForb/inny program), "
                        "CI-V baud w radiu = 19200, model, CI-V address.")
            self.last_err  = base + (f"\n--- rigctld.log ---\n{tail}" if tail else "")
            self.last_msg  = ""
            print(f"[rig] connection ERROR ({type(e).__name__}): {e}")
            if tail:
                print(f"[rig] rigctld.log:\n{tail}")
            self.sim       = True
            self.connected = False
            if self._proc:
                try: self._proc.terminate()
                except: pass
                self._proc = None
            if getattr(self, "_logf", None):
                try: self._logf.close()
                except: pass
                self._logf = None
            return False

    def _rigctld_log_tail(self, n: int = 1800) -> str:
        try:
            with open(RIGCTLD_LOG, "rb") as f:
                data = f.read()
            return data[-n:].decode("utf-8", errors="replace").strip()
        except Exception:
            return ""

    def close(self):
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2.0)
            except Exception:
                try: self._proc.kill()
                except: pass
            self._proc = None
        if getattr(self, "_logf", None):
            try: self._logf.close()
            except: pass
            self._logf = None
