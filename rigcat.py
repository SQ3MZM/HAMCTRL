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
import asyncio, re, sys, socket, subprocess, threading
from pathlib import Path
from config import HAMLIB, RIGCTLD_PORT, RIGCTLD_LOG, ENV


class RigCAT:
    def __init__(self):
        self.freq      = 14074000
        self.mode      = "USB"
        self.bw        = 2400
        self.ptt       = False
        self.split     = False
        self.freq_b    = 14074000
        self.s_meter   = 0.0
        # Icom-CI-V-only concepts with no Hamlib-generic equivalent here -
        # kept as inert defaults purely so the shared status-broadcast dict
        # in webapp.py (which reads these unconditionally for both driver
        # types) doesn't crash with AttributeError on a non-CI-V rig.
        self.filter_num  = 1
        self.preamp      = 0
        self.attenuator  = False
        self.tuner       = False
        # KEYSPD (CW keyer speed) etc. - CI-V-populated cache read by the
        # CW-send code path regardless of driver (self.rig.level_values.get(...)).
        self.level_values = {}
        # DTR/RTS CW keyer state (see configure_keyer/send_cw_dtr_rts below)
        self._keyer_port  = ''
        self._keyer_line  = 'DTR'
        self._keyer_ser   = None
        self._keyer_stop  = threading.Event()
        self._keyer_th    = None
        self.connected = False
        self.sim       = True
        self._proc     = None
        self._logf     = None
        self._last_cmd = None   # argv of the last successful rigctld spawn - see _respawn_rigctld
        self._lock     = asyncio.Lock()
        self.last_err  = ""
        self.last_msg  = ""
        self.rigctld_path = ""
        self.rigctld_found = False

    async def _cmd_once(self, cmd: str, timeout: float) -> str:
        r, w = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", RIGCTLD_PORT), timeout=3.0)
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

    async def _cmd(self, cmd: str, timeout=4.0) -> str:
        async with self._lock:
            try:
                return await self._cmd_once(cmd, timeout)
            except (ConnectionRefusedError, ConnectionResetError, BrokenPipeError, OSError) as e:
                # rigctld died mid-session (crashed, or was killed
                # externally) - live-seen 2026-09-02: after ONE such
                # failure, every subsequent slider command failed the same
                # way (WinError 1225 "connection refused") until the
                # operator manually clicked Connect again, even though
                # freq/mode kept working right up to that point (they go
                # through this same _cmd(), so rigctld really was gone, not
                # just this one command). Since _lock already serializes
                # every _cmd() call, only the FIRST caller to hit a dead
                # port pays for the respawn - everyone queued behind it on
                # _lock simply gets a live rigctld by the time their turn
                # comes.
                if not await self._respawn_rigctld():
                    raise
                return await self._cmd_once(cmd, timeout)

    async def _respawn_rigctld(self) -> bool:
        """Re-launch rigctld.exe with the exact argv from the last
        successful connect() (self._last_cmd), so a died/crashed rigctld
        recovers on its own instead of leaving the radio uncontrollable
        until a manual reconnect. Best-effort: returns False (caller then
        raises the original error) if there's no known command to relaunch
        or the relaunch itself fails."""
        if not getattr(self, "_last_cmd", None):
            return False
        print("[rig] rigctld nie odpowiada (padl?) - probuje ponownego startu "
              "z ostatnia znana konfiguracja")
        if self._proc:
            try: self._proc.terminate()
            except: pass
            self._proc = None
        try:
            self._logf = open(RIGCTLD_LOG, "wb")
        except Exception:
            self._logf = None
        try:
            self._proc = subprocess.Popen(
                self._last_cmd,
                stdout=(self._logf or subprocess.DEVNULL),
                stderr=(self._logf or subprocess.PIPE))
            await asyncio.sleep(3.5)
            if self._proc.poll() is not None:
                print(f"[rig] restart rigctld nie powiodl sie: {self._rigctld_log_tail()}")
                return False
            print(f"[rig] rigctld wystartowal ponownie na porcie {RIGCTLD_PORT}")
            return True
        except Exception as e:
            print(f"[rig] restart rigctld error: {e}")
            return False

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
        return await self._parse_freq_response(r)

    async def get_freq_live(self, timeout: float = 0.3):
        """get_freq() here already queries rigctld live every call (no
        optimistic local cache like civ.py's self.freq) - alias for
        call-signature compatibility with civ.py's verify-after-retune
        path (webapp.py's ft8_qsy handler)."""
        return await self.get_freq()

    async def _parse_freq_response(self, r: str):
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

    async def set_mode(self, m, bw=0, fil=0):
        # fil: Icom CI-V filter-slot selector (FIL1/2/3), no Hamlib-generic
        # equivalent - accepted for call-signature compatibility with
        # civ.py's set_mode() (both drivers are called through the same
        # code path in webapp.py) and otherwise ignored.
        if self.sim:
            self.mode = m; self.bw = bw if bw else self.bw; return
        await self._cmd(f"M {m} {bw}"); self.mode = m
        if bw: self.bw = bw

    async def set_ptt(self, on: bool):
        if self.sim: self.ptt = on; return
        await self._cmd(f"T {1 if on else 0}"); self.ptt = on

    # ── CW keying via DTR/RTS ────────────────────────────────────────────
    # Mirrors civ.py's DTR/RTS keyer (same Morse table/timing, same
    # key-down/key-up thread), with two differences forced by how Hamlib
    # works here: PTT goes through set_ptt() (Hamlib 'T' command over TCP)
    # instead of a raw CI-V PTT byte, and there is NO "same port as CAT"
    # fallback — RigCAT talks to rigctld over TCP and never holds a raw
    # serial handle to the radio, so a separate DTR/RTS port is mandatory
    # (unlike civ.py, where an empty port means "reuse the CI-V serial
    # connection").

    _MORSE_TABLE = {
        'A':'.-',   'B':'-...',  'C':'-.-.',  'D':'-..',
        'E':'.',    'F':'..-.',  'G':'--.',   'H':'....',
        'I':'..',   'J':'.---',  'K':'-.-',   'L':'.-..',
        'M':'--',   'N':'-.',    'O':'---',   'P':'.--.',
        'Q':'--.-', 'R':'.-.',   'S':'...',   'T':'-',
        'U':'..-',  'V':'...-',  'W':'.--',   'X':'-..-',
        'Y':'-.--', 'Z':'--..',
        '0':'-----','1':'.----','2':'..---','3':'...--',
        '4':'....-','5':'.....','6':'-....','7':'--...',
        '8':'---..','9':'----.',
        '.':'.-.-.-', ',':'--..--', '?':'..--..',
        '/':'-..-.',  '+':'.-.-.',  '-':'-....-',
        '=':'-...-',  ':':'---...',  '\'':'.----.',
        '@':'.--.-.', '(':'-.--.', ')':'-.--.-',
        ' ': None,   # word gap
    }

    def _cw_text_duration_s(self, text: str, wpm: int) -> float:
        """Exact time to send `text` in CW at `wpm`, in seconds - same
        PARIS timing as civ.py's version, used by webapp.py's CW safety
        timer to know when a background keying thread will finish."""
        wpm = max(5, min(60, wpm))
        dit = 1.200 / wpm
        units = 0.0
        first_char = True
        for ch in text.upper():
            if ch == ' ':
                units += 7.0
                first_char = True
                continue
            code = self._MORSE_TABLE.get(ch)
            if not code:
                continue
            if not first_char:
                units += 3.0
            first_char = False
            for i, sym in enumerate(code):
                units += 3.0 if sym == '-' else 1.0
                if i < len(code) - 1:
                    units += 1.0
        return units * dit

    def configure_keyer(self, port: str, line: str):
        """
        Configure the DTR/RTS keyer on a dedicated serial port. Unlike
        civ.py, an empty port has nothing to fall back to here — RigCAT has
        no serial handle of its own — so it just leaves the keyer
        unconfigured (send_cw_dtr_rts then raises a clear error instead of
        silently doing nothing).
        """
        import serial as _serial
        self._keyer_line = (line or 'DTR').upper()
        if self._keyer_ser:
            try: self._keyer_ser.close()
            except Exception: pass
            self._keyer_ser = None
        self._keyer_port = (port or '').strip()
        if not self._keyer_port:
            print("[rigcat] configure_keyer: no CW DTR/RTS port set - "
                  "CW keying will fail until one is configured (Hamlib has "
                  "no CI-V port to fall back to)", flush=True)
            return
        try:
            self._keyer_ser = _serial.Serial(self._keyer_port, baudrate=9600, timeout=0.1)
            # Zero DTR/RTS right away - otherwise opening the port throws a KEY DOWN.
            try:
                self._keyer_ser.dtr = False
                self._keyer_ser.rts = False
            except Exception:
                pass
            print(f"[rigcat] configure_keyer: opened {self._keyer_port} for {self._keyer_line} keying", flush=True)
        except Exception as e:
            print(f"[rigcat] configure_keyer: error opening {self._keyer_port}: {e}", flush=True)
            self._keyer_ser = None

    def _set_key(self, state: bool):
        """Set the DTR or RTS line (True=KEY DOWN, False=KEY UP)."""
        ser = self._keyer_ser
        if not ser:
            return
        try:
            if self._keyer_line == 'RTS':
                ser.rts = state
            else:
                ser.dtr = state
        except Exception as e:
            print(f"[rigcat] _set_key error: {e}", flush=True)

    def _send_morse_blocking(self, text: str, wpm: int, stop_event):
        """Synchronous CW sending via DTR/RTS (called in a separate thread).
        dit_ms = 1200 / wpm (standard Morse timing, PARIS word = 50 units).
        PTT is handled by the async caller BEFORE this thread starts /
        AFTER it ends - this function only toggles the key line.
        """
        import time as _time
        wpm = max(5, min(60, wpm))
        dit = 1.200 / wpm

        def key_down(t):
            self._set_key(True)
            _time.sleep(t)
            self._set_key(False)

        for char in text.upper():
            if stop_event.is_set():
                break
            if char == ' ':
                _time.sleep(dit * 7)  # word gap
                continue
            code = self._MORSE_TABLE.get(char)
            if not code:
                continue
            for i, sym in enumerate(code):
                if stop_event.is_set():
                    break
                key_down(dit * 3 if sym == '-' else dit)
                if i < len(code) - 1:
                    _time.sleep(dit)   # gap between elements
            _time.sleep(dit * 3)   # gap between letters
        self._set_key(False)   # make sure the key is released

    async def send_cw_dtr_rts(self, text: str, wpm: int):
        """Send CW text via DTR/RTS keying. Returns as soon as PTT/keying is
        SCHEDULED (matches civ.py's contract - webapp.py broadcasts the PTT
        toast right after calling this and expects it back near-instantly,
        not after the whole message finishes sending). The actual PTT-on ->
        keying -> PTT-off sequence runs in a background task."""
        self._keyer_stop.set()
        if self._keyer_th and self._keyer_th.is_alive():
            self._keyer_th.join(timeout=0.5)
        self._keyer_stop.clear()

        if self.sim:
            print(f"[rigcat] SIM DTR/RTS: {text!r} @ {wpm} WPM", flush=True)
            return

        if not self._keyer_ser:
            raise RuntimeError(
                "Brak skonfigurowanego portu DTR/RTS do CW (ustaw osobny port "
                "w ustawieniach CW - Hamlib nie ma wspolnego portu z CAT).")

        asyncio.create_task(self._send_cw_dtr_rts_task(text, wpm))

    async def _send_cw_dtr_rts_task(self, text: str, wpm: int):
        await self.set_ptt(True)
        try:
            # Give the radio a moment to switch to transmit before the first dit.
            await asyncio.sleep(0.25)
            stop_ev = self._keyer_stop
            th = threading.Thread(
                target=self._send_morse_blocking,
                args=(text, wpm, stop_ev),
                daemon=True, name="rigcat-morse-keyer")
            self._keyer_th = th
            th.start()
            await asyncio.to_thread(th.join)
        finally:
            await self.set_ptt(False)

    async def stop_cw_dtr_rts(self):
        """Abort DTR/RTS sending immediately."""
        self._keyer_stop.set()
        self._set_key(False)
        await self.set_ptt(False)

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

    # ── Generic-radio mappings for features webapp.py otherwise only
    # calls on civ.py (CI-V) ─────────────────────────────────────────────
    # Universality: a rig added later via "any Hamlib-supported model" must
    # not crash just because it isn't an Icom with civ.py's exact CI-V
    # command set. Where Hamlib has a real generic equivalent, use it here
    # (rigctld itself returns RPRT<0 for anything a specific rig backend
    # doesn't actually support - safe, non-destructive, no guessing about
    # rig-specific byte sequences the way a raw CI-V command would need).

    async def set_tuner(self, on: bool):
        """Antenna tuner on/off - Hamlib func TUNER, a real 1:1 generic
        equivalent of civ.py's CI-V 1C 01 <00|01>."""
        await self.set_func("TUNER", on)

    async def set_preamp(self, level: int):
        """Preamp on/off/stage - Hamlib level PREAMP. NOTE: unlike civ.py's
        Icom-specific 0/1/2 (OFF/P.AMP1/P.AMP2) index, Hamlib's PREAMP level
        is a raw dB gain value whose valid steps are rig-specific (queried
        via 'l PREAMP' range, which we don't do here) - passing the index
        through as-is is a best-effort default, not a verified mapping for
        every rig. rigctld rejects an out-of-range value harmlessly."""
        if self.sim: self.preamp = level; return
        await self.set_level("PREAMP", level)
        self.preamp = level

    async def set_attenuator(self, on: bool):
        """Attenuator on/off - Hamlib level ATT. Same caveat as set_preamp:
        Hamlib expects a dB value from the rig's own supported steps: 20dB
        matches civ.py's fixed Icom attenuator step (CI-V 11), used here as
        a reasonable default, not a verified value for every rig."""
        if self.sim: self.attenuator = on; return
        await self.set_level("ATT", 20 if on else 0)
        self.attenuator = on

    async def vfo_swap(self):
        """Exchange VFO A/B - Hamlib VFO_OP 'G XCHG' (real generic
        equivalent of civ.py's CI-V 07 B0)."""
        if self.sim:
            self.freq, self.freq_b = self.freq_b, self.freq
            return
        await self._cmd("G XCHG")
        try:
            self.freq = await self.get_freq()
        except Exception:
            pass

    async def vfo_equalize(self):
        """Copy VFO A -> B - Hamlib VFO_OP 'G CPY' (real generic
        equivalent of civ.py's CI-V 07 A0)."""
        if self.sim:
            self.freq_b = self.freq
            return
        await self._cmd("G CPY")

    async def set_freq_b(self, hz: int):
        """Set VFO B's frequency without disturbing the active VFO.
        Hamlib's basic rigctld protocol has no direct 'set this Hz on VFOB
        specifically' command - switch to B, set freq, switch back to A.
        Not atomic (a callsign-selecting client could see a brief VFOB->A
        flicker), but civ.py's own version isn't atomic either (multiple
        CI-V transactions) and this path is only used for occasional split
        setup, not hot-path tuning."""
        if self.sim: self.freq_b = hz; return
        await self._cmd("V VFOB")
        await self._cmd(f"F {hz}")
        await self._cmd("V VFOA")
        self.freq_b = hz

    async def start_tuner_autotune(self):
        """Hamlib has no generic 'trigger an autotune cycle now' action
        (TUNER is a plain on/off func, unlike civ.py's CI-V 1C 01 01+02
        two-step start sequence) - raise a clear, caught-by-the-caller
        error instead of silently doing nothing or guessing at a
        rig-specific command that would generate unintended TX."""
        raise RuntimeError(
            "Autotune tunera nie jest dostepny dla tego sterownika (Hamlib) - "
            "wlacz/wylacz tuner recznie (funkcja TUNER dziala), a strojenie "
            "wykonaj na samym radiu.")

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
        return await discover_capabilities(RIGCTLD_PORT)

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
                   "-t", str(RIGCTLD_PORT), "--set-conf=serial_handshake=None"]
            if civ:
                civ_clean = (str(civ).strip().replace("0x", "").replace("0X", "")
                             .replace("h", "").replace("H", ""))
                # Hamlib's real config token is 'civaddr' (no underscore) -
                # 'civ_addr' is silently REJECTED ("no such token"), live-
                # confirmed via rigctld -vvvvv log 2026-09-01: rigctld then
                # falls back to the Icom backend's own hardcoded default CI-V
                # address for that model instead of the one actually
                # configured on the radio. Harmless when they happen to
                # match (as for this IC-746, default=0x56); silently wrong
                # for any radio whose CI-V address was changed from factory
                # default, or when addressing one of several rigs on a
                # shared CI-V bus.
                cmd += [f"--set-conf=civaddr=0x{civ_clean}"]
                print(f"[rig] CIV addr: 0x{civ_clean}")
            # Verbose rigctld -> rigctld.log file (diagnostics, for when the radio doesn't respond)
            cmd += ["-vvvvv"]
            print(f"[rig] CMD: {' '.join(cmd)}")
            print(f"[rig] rigctld log: {RIGCTLD_LOG}")
            # Remembered for _respawn_rigctld() - if rigctld dies mid-session
            # (crash, or killed externally), _cmd() relaunches it with this
            # exact argv instead of leaving every command broken until the
            # operator manually reconnects from the UI.
            self._last_cmd = cmd
            # Clean up any of OUR OWN orphaned rigctld.exe processes first -
            # e.g. surviving an earlier abrupt close/EXE update within the
            # same dev/test session. An orphan holds the COM port
            # exclusively, so a freshly spawned rigctld here can open the
            # TCP port fine but then fails to open the (already-locked)
            # serial port and exits moments later - leaving nothing on
            # RIGCTLD_PORT (ConnectionRefusedError on the next _cmd() call,
            # live-seen 2026-09-01 mid-CW-test). Filtered by OUR specific
            # "-t {RIGCTLD_PORT}" command-line marker so an independently
            # run rigctld (e.g. started by N1MM for its own use, on a
            # different port) is never touched.
            if sys.platform == "win32":
                try:
                    _ps = (f"Get-CimInstance Win32_Process -Filter \"Name='rigctld.exe'\" | "
                           f"Where-Object {{ $_.CommandLine -like '*-t {RIGCTLD_PORT}*' }} | "
                           f"ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}")
                    subprocess.run(["powershell", "-NoProfile", "-Command", _ps],
                                    capture_output=True, timeout=5)
                except Exception:
                    pass
            # Is rigctld already running on this port (e.g. started by N1MM)?
            _port_busy = False
            try:
                _t = socket.socket(); _t.settimeout(0.5)
                _port_busy = _t.connect_ex(("127.0.0.1", RIGCTLD_PORT)) == 0
                _t.close()
            except: pass
            if _port_busy:
                print(f"[rig] Port {RIGCTLD_PORT} in use — connecting to the existing rigctld")
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
                print(f"[rig] rigctld started on port {RIGCTLD_PORT}")
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
