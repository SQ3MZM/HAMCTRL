#!/usr/bin/env python3
r"""
hamlib_server.py — rigctld emulator (Hamlib NET rigctl) — broadcast virtual rig.

Architecture, Option C: N clients can connect simultaneously to the same port.
- Read commands (GET_FREQ, GET_MODE, GET_PTT etc.) → answered for every client
- Write commands (SET_FREQ, SET_MODE, SET_PTT etc.) → only when no one holds
  the radio, or when the radio isn't locked. If locked → RPRT 0 (fake
  success, does nothing) — programs like WSJT-X don't disconnect on RPRT 0.

WSJT-X's startup sequence (Hamlib 4.x):
  1. \chk_rig      → "RPRT 0\n"
  2. \dump_caps    → a "setting=value\n...\nRPRT 0\n" block
  3. \dump_state   → lines with the radio's capabilities
  4. \get_vfo      → "VFOA\nRPRT 0\n"
  5. \get_freq     → "14074000\nRPRT 0\n"
  etc.
"""
import asyncio

DEFAULT_PORTS = [4532, 4533, 4534]

MODE_TO_HAMLIB = {
    "USB":"USB","LSB":"LSB","AM":"AM","FM":"FM","WFM":"WFM",
    "CW":"CW","CW-R":"CWR","RTTY":"RTTY","RTTY-R":"RTTYR",
    "USB-D":"PKTUSB","LSB-D":"PKTLSB",
    "PKTUSB":"PKTUSB","PKTLSB":"PKTLSB",
}
MODE_FROM_HAMLIB = {
    "USB":"USB","LSB":"LSB","AM":"AM","FM":"FM","WFM":"WFM",
    "CW":"CW","CWR":"CW-R","RTTY":"RTTY","RTTYR":"RTTY-R",
    "PKTUSB":"USB-D","PKTLSB":"LSB-D",
}


class HamlibSession:

    def __init__(self, rig, hub, reader, writer, slot_name, app=None):
        self.rig      = rig
        self.hub      = hub
        # Reference to the app — needed to check radio_lock. Without it the
        # session wouldn't know who holds the TRX (the hub doesn't have
        # that), so the network control lock couldn't work.
        self.app      = app
        self.reader   = reader
        self.writer   = writer
        self.addr     = writer.get_extra_info('peername')
        self.slot     = slot_name
        self._running = True

    async def run(self):
        print(f"[hamlib:{self.slot}] Connected: {self.addr}", flush=True)
        try:
            # IMPORTANT: the netrigctl protocol does NOT send any banner
            # after connecting. The client (Hamlib) ALWAYS sends a command
            # first. This used to incorrectly send b'0\n' here, which
            # shifted the WHOLE stream by one line and broke parsing of
            # every subsequent response.
            while self._running:
                line = await asyncio.wait_for(self.reader.readline(), timeout=60.0)
                if not line:
                    break
                cmd = line.decode(errors='replace').strip()
                if not cmd:
                    continue
                print(f"[hamlib:{self.slot}] << {cmd!r}", flush=True)
                resp = await self._handle(cmd)
                out  = (resp if resp.endswith('\n') else resp + '\n').encode()
                print(f"[hamlib:{self.slot}] >> {resp[:80]!r}", flush=True)
                self.writer.write(out)
                await self.writer.drain()
        except (asyncio.TimeoutError, asyncio.IncompleteReadError,
                ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            import traceback; traceback.print_exc()
        finally:
            print(f"[hamlib:{self.slot}] Disconnected: {self.addr}", flush=True)
            try: self.writer.close()
            except: pass

    def _can_control(self) -> bool:
        """
        Are CONTROL commands (changing the radio's state) allowed?

        RULE (same as in the webapp and on COM ports): the radio is
        controlled ONLY by the logged-in operator who has TAKEN the TRX
        (radio_lock). rigctl ports are open to the internet and the
        protocol has NO authentication, so without this any scanner could
        send 'T 1' and leave the radio transmitting.

        Allowed when:
          - the radio is in SIM mode (testing), or
          - SOMEONE holds radio_lock — meaning an operator logged into the
            webapp and took the TRX; their external software (WSJT-X,
            logger) is running.

        Blocked when no one holds the lock — in that case incoming control
        commands are either a mistake or unrelated network traffic.

        NOTE: this is NOT the inverse of the old "block when someone holds
        the lock" logic (which protected against a collision between two
        sources). There's no collision here, because the operator holding
        the lock is the SAME person using their own software.

        READ commands (GET_FREQ, GET_MODE, dump_caps...) always work —
        read-only access doesn't hurt anyone and doesn't break radio
        detection by external software.
        """
        if self.rig.sim:
            return True
        app = (self.app
               or getattr(self.hub, 'app', None)
               or getattr(self.hub, '_app', None))
        if app is None:
            return False  # no reference to app — SAFELY deny control
        lock = getattr(app, 'radio_lock', {})
        return lock.get('user_id') is not None

    async def _handle(self, cmd: str) -> str:
        raw   = cmd.strip()
        parts = raw.split()
        if not parts:
            return 'RPRT 0'

        # Hamlib 4.x: \komenda  →  strip backslash, lowercase
        c  = parts[0].lstrip('\\').upper()
        args = parts[1:]

        try:
            # ── Ping / check ───────────────────────────────────────────────────
            if c in ('CHKRIG', 'CHK_RIG', 'CHECKRIG'):
                return 'RPRT 0'

            # ── dump_caps — setting=value format (WSJT-X checks this first) ────
            if c in ('DUMP_CAPS', 'DUMPCAPS'):
                return self._dump_caps()

            # ── dump_state — numeric format ─────────────────────────────────────
            if c in ('DUMP_STATE', 'DUMPSTATE'):
                return self._dump_state()

            # ── Frequency ──────────────────────────────────────────────────────
            if c in ('F', 'GET_FREQ', 'GETFREQ'):
                if not args:
                    return f"{int(self.rig.freq)}\nRPRT 0"
            if c in ('F', 'SET_FREQ', 'SETFREQ') and args:
                if not self._can_control():
                    return 'RPRT 0'  # radio busy — fake success, do nothing
                hz = int(float(args[0]))
                self.rig.freq = hz
                if not self.rig.sim:
                    try: await self.rig.set_freq(hz)
                    except: pass
                await self.hub.broadcast({"type":"freq","freq":hz,"src":"hamlib"})
                print(f"[hamlib:{self.slot}] freq={hz/1e6:.6f}MHz", flush=True)
                return 'RPRT 0'

            # ── Lock mode (WSJT-X 2.6+) ───────────────────────────────────────
            if c in ('GET_LOCK_MODE', 'LOCK_MODE'):
                return "0\nRPRT 0"

            if c in ('SET_LOCK_MODE',) and args:
                return 'RPRT 0'

            # ── Mode ───────────────────────────────────────────────────────────
            if c in ('M', 'GET_MODE', 'GETMODE') and not args:
                hm = MODE_TO_HAMLIB.get(self.rig.mode, self.rig.mode)
                bw = getattr(self.rig, 'bw', 0) or 0
                # Hamlib requires bw >= 0, -1 means "default"
                return f"{hm}\n{max(0, bw)}\nRPRT 0"

            if c in ('M', 'SET_MODE', 'SETMODE') and args:
                if not self._can_control():
                    return 'RPRT 0'
                hmode = args[0].upper()
                bw    = int(args[1]) if len(args) >= 2 else 0
                mode  = MODE_FROM_HAMLIB.get(hmode, hmode)
                self.rig.mode = mode
                if bw: self.rig.bw = bw
                if not self.rig.sim:
                    try:
                        await self.rig.set_mode(mode, bw)
                    except Exception as e:
                        print(f"[hamlib] set_mode ERROR for mode={mode!r}: {e!r}")
                await self.hub.broadcast({"type":"mode","mode":mode,"bandwidth":bw,"src":"hamlib"})
                return 'RPRT 0'

            # ── PTT ────────────────────────────────────────────────────────────
            if c in ('T', 'GET_PTT', 'GETPTT') and not args:
                return f"{'1' if self.rig.ptt else '0'}\nRPRT 0"

            if c in ('T', 'SET_PTT', 'SETPTT') and args:
                if not self._can_control():
                    return 'RPRT 0'
                # Hamlib PTT values: 0=OFF, 1=ON, 2=ON_MIC, 3=ON_DATA.
                # JTDX/WSJT-X in digital mode (USB-D/PKTUSB) sends '3'
                # (RIG_PTT_ON_DATA) instead of a plain '1' — any nonzero
                # value means TX.
                raw_val = args[0]
                if raw_val.lstrip('-').isdigit():
                    on = int(raw_val) != 0
                else:
                    on = raw_val in ('true', 'True', 'on', 'TX')
                print(f"[hamlib:{self.slot}] PTT SET → {'TX' if on else 'RX'} (sim={self.rig.sim}, ser={self.rig._ser is not None})", flush=True)
                self.rig.ptt = on
                if not self.rig.sim:
                    if self.rig._ser:
                        try:
                            await self.rig.set_ptt(on)
                            print(f"[hamlib:{self.slot}] PTT CI-V OK", flush=True)
                        except Exception as e:
                            print(f"[hamlib:{self.slot}] PTT CI-V ERROR: {e}", flush=True)
                    else:
                        print(f"[hamlib:{self.slot}] PTT: no serial port!", flush=True)
                await self.hub.broadcast({"type":"ptt","ptt":on,"src":"hamlib"})
                return 'RPRT 0'

            # ── VFO ────────────────────────────────────────────────────────────
            if c in ('V', 'GET_VFO', 'GETVFO') and not args:
                return "VFOA\nRPRT 0"
            if c in ('V', 'SET_VFO', 'SETVFO') and args:
                return 'RPRT 0'

            # ── Split ──────────────────────────────────────────────────────────
            if c in ('S', 'GET_SPLIT_VFO') and not args:
                split = getattr(self.rig, 'split', False)
                return f"{'1' if split else '0'}\nVFOB\nRPRT 0"
            if c in ('S', 'SET_SPLIT_VFO') and args:
                if not self._can_control():
                    return 'RPRT 0'
                on = args[0] in ('1','true')
                self.rig.split = on
                if not self.rig.sim:
                    try: await self.rig.set_split(on)
                    except: pass
                await self.hub.broadcast({"type":"split","split":on,"src":"hamlib"})
                return 'RPRT 0'

            # ── Levels ─────────────────────────────────────────────────────────
            if c in ('L', 'GET_LEVEL') and args:
                lname = args[0].upper()
                if lname == 'STRENGTH':
                    sm = getattr(self.rig, 's_meter', 0) or 0
                    db = (sm - 9) * 6 if sm <= 9 else (sm - 9) * 10
                    return f"{db:.1f}\nRPRT 0"
                if lname == 'RFPOWER':
                    pwr = getattr(self.rig, 'rf_power', 100) or 100
                    return f"{pwr/100.0:.4f}\nRPRT 0"
                return "0.0000\nRPRT 0"

            if c in ('L', 'SET_LEVEL') and len(args) >= 2:
                return 'RPRT 0'

            # ── DCD / RIT / XIT ────────────────────────────────────────────────
            if c in ('D', 'GET_DCD', 'GETDCD'):
                return "0\nRPRT 0"
            if c in ('Z', 'GET_RIT', 'GETRIT', 'GET_XIT', 'GETXIT'):
                return "0\nRPRT 0"
            if c in ('Z', 'SET_RIT', 'SETRIT', 'SET_XIT', 'SETXIT') and args:
                return 'RPRT 0'

            # ── TX Freq ────────────────────────────────────────────────────────
            if c in ('I', 'GET_SPLIT_FREQ'):
                return f"{int(self.rig.freq)}\nRPRT 0"
            if c in ('I', 'SET_SPLIT_FREQ') and args:
                return 'RPRT 0'

            # ── TX Mode ────────────────────────────────────────────────────────
            if c in ('X', 'GET_SPLIT_MODE'):
                hm = MODE_TO_HAMLIB.get(self.rig.mode, self.rig.mode)
                bw = getattr(self.rig, 'bw', 0) or 0
                return f"{hm}\n{bw}\nRPRT 0"
            if c in ('X', 'SET_SPLIT_MODE') and args:
                return 'RPRT 0'

            # ── Antennas ───────────────────────────────────────────────────────
            if c in ('Y', 'GET_ANT', 'GETANT'):
                return "1\nRPRT 0"
            if c in ('Y', 'SET_ANT', 'SETANT') and args:
                return 'RPRT 0'

            # ── Info ───────────────────────────────────────────────────────────
            if c in ('_', 'GET_INFO', 'GETINFO'):
                name = getattr(self.rig, '_rig_name', 'Ham Radio Control')
                return f"Info: {name}\nRPRT 0"

            # ── Quit ───────────────────────────────────────────────────────────
            if c == 'Q':
                self._running = False
                return 'RPRT 0'

            # ── Unknown command ──────────────────────────────────────────────────
            # Internet scanners knock with TLS (ClientHello starts with
            # 0x16 0x03) and used to flood the log with hundreds of UNKNOWN
            # lines full of binary junk. Such a connection is NOT a rigctl
            # client — we close it quietly, without logging every packet.
            _binary_junk = any(ch < 32 and ch not in (9, 10, 13)
                               for ch in raw.encode("utf-8", "ignore")[:8])
            if _binary_junk:
                self._running = False   # disconnect — this isn't rigctl
                if not getattr(self, "_junk_logged", False):
                    self._junk_logged = True
                    print(f"[hamlib:{self.slot}] Rejected a non-rigctl "
                          f"connection (binary data — scanner/TLS)", flush=True)
                return 'RPRT -1'
            print(f"[hamlib:{self.slot}] UNKNOWN: {raw!r}", flush=True)
            return 'RPRT 0'

        except Exception as e:
            print(f"[hamlib:{self.slot}] ERR '{raw}': {e}", flush=True)
            return 'RPRT -1'

    def _dump_caps(self) -> str:
        """
        DUMP_CAPS — the "setting=value" format required by WSJT-X / Hamlib 4.x.
        Each line is a key=value pair. The block ends with "RPRT 0".
        Source: hamlib/src/rig.c dump_caps_helper()
        """
        name = getattr(self.rig, '_rig_name', 'Ham Radio Control Server')
        freq = int(self.rig.freq)
        lines = [
            'Caps dump for model: 2',
            'Model name:\t\tNET rigctl',
            'Mfg name:\t\tHamlib',
            'Backend version:\t4.5',
            'Backend copyright:\tLGPL',
            'Backend status:\tBeta',
            'Rig type:\tOther',
            'PTT type:\tCAT',
            'DCD type:\tNone',
            'Port type:\tNetwork',
            'Write delay:\t0mS, timeout 0mS, 3 retr',
            'Post write delay:\t0mS',
            'Has targetable VFO:\tY',
            'Has transceive:\tN',
            'Announce:\t0x0',
            'Max RIT:\t0 Hz',
            'Max XIT:\t0 Hz',
            'Max IF-SHIFT:\t0 Hz',
            'Preamp step:\t0 dB',
            'ATT step:\t0 dB',
            'Get functions:\t',
            'Set functions:\t',
            'Get level: STRENGTH(0..1) RFPOWER(0..1) ',
            'Set level: RFPOWER(0..1) ',
            'Get parm: ',
            'Set parm: ',
            'Mode list:\t USB LSB AM FM CW CWR RTTY RTTYR PKTUSB PKTLSB',
            'VFO list:\t VFOA VFOB',
            'VFO ops:\t',
            'Scan ops:\t',
            'Number of banks:\t0',
            'Memory name desc size:\t0',
            'Memories:',
            'TX ranges #1 for ITU region 1:',
            'RX ranges #1 for ITU region 1:',
            f'  {freq} Hz .. 30000000 Hz, modes: USB LSB AM FM CW RTTY PKTUSB PKTLSB, Low power: -1 W, High power: -1 W, Antenna: 1',
            f'TX ranges #1 for ITU region 2:',
            f'RX ranges #1 for ITU region 2:',
            f'Tuning steps:',
            f'  * 1 Hz  USB LSB AM FM CW RTTY PKTUSB PKTLSB',
            f'Filters:',
            f'  500 Hz   CW CWR RTTY RTTYR',
            f'  2400 Hz  USB LSB PKTUSB PKTLSB',
            f'  3000 Hz  AM',
            f'  15000 Hz FM',
            f'Best frequency resolution = 1 Hz',
            f'Has priv data:\tN',
            'RPRT 0',
        ]
        return '\n'.join(lines)

    def _dump_state(self) -> str:
        """
        DUMP_STATE — format matching hamlib/rigs/dummy/netrigctl.c's netrigctl_open() exactly.
        Order verified against the source (Hamlib master):
          1. prot_ver = atoi(line1)          # "0"
          2. read_string() — line2 IS READ but UNUSED (ignored)
          3. read_string() — line3 = ITU region -> atoi()
          4. loop of HAMLIB_FRQRANGESIZ RX freq range lines (ends with sentinel 0 0 0 0 0 0 0)
          5. loop of HAMLIB_FRQRANGESIZ TX freq range lines (ends with sentinel)
          6. tuning steps loop (ends with "0 0")
          7. filters loop (ends with "0 0")
          8. max_rit, max_xit, max_ifshift
          9. announces (count)
          10. preamp list (space-separated numbers, ends with 0)
          11. attenuator list (same)
          12. has_get_func, has_set_func (hex)
          13. has_get_level, has_set_level (hex)
          14. has_get_parm, has_set_parm (hex)
        """
        lines = [
            '0',                                    # 1. prot_ver
            '2',                                    # 2. (read, ignored by the client)
            '1',                                    # 3. ITU region
            # 4. RX freq ranges — each line: start end modemask low high vfo ant
            '1800000 30000000 0x1ff -1 -1 0x3 0',
            '50000000 54000000 0x1ff -1 -1 0x3 0',
            '0 0 0 0 0 0 0',                       # sentinel ending RX
            # 5. TX freq ranges
            '1800000 30000000 0x1ff -1 -1 0x3 0',
            '50000000 54000000 0x1ff -1 -1 0x3 0',
            '0 0 0 0 0 0 0',                       # sentinel ending TX
            # 6. Tuning steps: mode step
            '0x1ff 1',
            '0 0',                                  # sentinel
            # 7. Filters: mode width
            '0x1ff 500',
            '0x1ff 2400',
            '0x1ff 3000',
            '0 0',                                  # sentinel
            # 8. max_rit max_xit max_ifshift (3 separate lines)
            '0',
            '0',
            '0',
            # 9. announces
            '0',
            # 10. preamp list (ends with 0)
            '0',
            # 11. attenuator list (ends with 0)
            '0',
            # 12. has_get_func has_set_func
            '0',
            '0',
            # 13. has_get_level has_set_level
            '0x40000003',
            '0x40000003',
            # 14. has_get_parm has_set_parm
            '0',
            '0',
            'RPRT 0',
        ]
        return '\n'.join(lines)


# ── VirtualRigServer and HamlibManager ───────────────────────────────────────

class VirtualRigServer:
    def __init__(self, port, slot_idx, app):
        self.port      = port
        self.slot_idx  = slot_idx
        self.app       = app
        self.name      = f"Radio {slot_idx+1} ({port})"
        self._server   = None
        self._sessions = []
        self.running   = False
        self.connected_clients = 0

    @property
    def rig(self):
        return self.app.rig

    async def start(self):
        try:
            # rigctl ports are DELIBERATELY open to the network — club users
            # connect from external networks with their own software
            # (WSJT-X, loggers).
            # SECURITY NOTE: the rigctl protocol has no authentication, so
            # protection has to live at a higher layer (a firewall with an
            # address list, VPN, or server-side authorization — see the
            # TODO below). Anyone using it only locally can narrow this in
            # config.json:
            #   "hamlib_bind": "127.0.0.1"
            _bind = "0.0.0.0"
            try:
                _cfg = getattr(self.app, "cfg", {}) or {}
                _bind = str(_cfg.get("hamlib_bind", "0.0.0.0")).strip() or "0.0.0.0"
            except Exception:
                pass
            # NOTE: family=0 (AF_UNSPEC) together with host='0.0.0.0' is an
            # ambiguous combination on Windows — it can cause a silent
            # binding failure. We use plain IPv4 (verified working via
            # telnet/PowerShell).
            self._server = await asyncio.start_server(
                self._handle_connection, _bind, self.port,
            )
            self.running = True
            sockets_info = [s.getsockname() for s in self._server.sockets]
            print(f"[hamlib] {self.name}: TCP listening on {sockets_info}", flush=True)
            task = asyncio.create_task(self._serve())
            task.add_done_callback(self._on_serve_done)
        except OSError as e:
            print(f"[hamlib] {self.name}: BIND ERROR on port {self.port}: {e}", flush=True)
            self.running = False

    def _on_serve_done(self, task):
        """Log if serve_forever() ends unexpectedly (a silent crash)."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            print(f"[hamlib] {self.name}: SERVER CRASHED: {type(exc).__name__}: {exc}", flush=True)
            import traceback
            traceback.print_exception(type(exc), exc, exc.__traceback__)
        else:
            print(f"[hamlib] {self.name}: serve_forever() ended without an error (unexpected)", flush=True)
        self.running = False

    async def _serve(self):
        async with self._server:
            await self._server.serve_forever()

    async def stop(self):
        if self._server:
            self._server.close()
            try: await self._server.wait_closed()
            except: pass
        self.running = False

    async def _handle_connection(self, reader, writer):
        self.connected_clients += 1
        s = HamlibSession(self.rig, self.app.hub, reader, writer, self.name,
                          app=self.app)
        self._sessions.append(s)
        try:
            await s.run()
        finally:
            self._sessions.remove(s)
            self.connected_clients -= 1

    def status(self):
        return {
            "slot": self.slot_idx, "name": self.name,
            "port": self.port, "running": self.running,
            "clients": self.connected_clients,
        }


class HamlibManager:
    def __init__(self, app):
        self.app     = app
        self.servers = []

    def _get_config(self):
        # Option C: one broadcast port for all users. Everyone connects on
        # port 4532 — N clients simultaneously. The extra ports (4533,
        # 4534) are optional for special uses (e.g. a separate read-only
        # skimmer with no control).
        # NOTE: "label" values below are UI text (admin panel, see
        # public/js/hamlib_ui.js - displayed raw, no I18n, and editable by
        # the admin) - deliberately left in Polish, not in scope for the
        # backend English translation pass.
        cfg = self.app.cfg.get('hamlibServers', [])
        defaults = [
            {"port": 4532, "enabled": True,  "label": "Broadcast (wszyscy uzytkownicy)"},
            {"port": 4533, "enabled": False,  "label": "Dodatkowy port 2"},
            {"port": 4534, "enabled": False,  "label": "Dodatkowy port 3"},
        ]
        result = []
        for i, d in enumerate(defaults):
            c = cfg[i] if i < len(cfg) else {}
            result.append({**d, **c})
        return result

    async def start_all(self):
        for i, c in enumerate(self._get_config()):
            if not c.get('enabled', i == 0):
                continue
            port = c['port']
            # Check actual availability — don't blindly trust the saved
            # config (the port could have been taken by rigctld or another
            # process since the settings panel last saved).
            import socket as _sk
            _t = _sk.socket(); _t.settimeout(0.3)
            _busy = _t.connect_ex(('127.0.0.1', port)) == 0
            _t.close()
            if _busy:
                print(f"[hamlib] Port {port} in use (another process) — skipping slot {i+1}", flush=True)
                continue
            srv = VirtualRigServer(port, i, self.app)
            await srv.start()
            self.servers.append(srv)

    async def stop_all(self):
        for srv in self.servers:
            await srv.stop()
        self.servers.clear()

    async def restart(self):
        await self.stop_all()
        await self.start_all()

    def status(self):
        configs = self._get_config()
        result  = []
        for i, c in enumerate(configs):
            srv = next((s for s in self.servers if s.slot_idx == i), None)
            result.append({
                **c, "slot": i,
                "running": srv.running if srv else False,
                "clients": srv.connected_clients if srv else 0,
            })
        return result
