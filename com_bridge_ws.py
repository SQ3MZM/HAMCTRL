"""
COM Bridge WS — server-side module for the WebSocket bridge to Windows clients.

The EXE client on the user's PC creates N virtual COM ports via com0com,
connects to this server over WSS, authenticates with a Bearer token, and
receives a mapping: COM10 -> CI-V IC-7300, COM11 -> Yaesu FT-991 (future), etc.

It then forwards bytes in BOTH directions:
  - Client sends data from COM10 (from CW Skimmer) -> server -> physical CI-V port
  - Radio responds -> server -> client -> appears on COM10 for CW Skimmer

WS PROTOCOL (JSON messages):

Client -> Server:
  {type: 'hello', ports: ['COM10', 'COM11', ...]}
    Client tells the server which physical COM ports it created. The server
    replies with an 'assignment' mapping - which COM maps to which function.

  {type: 'data', com: 'COM10', hex: 'FEFE94E00300FD'}
    Data from the client (from the app, via the virtual COM) - forward to the server.

  {type: 'open', com: 'COM10'}
    Client reports that the app opened this port (informational).

  {type: 'close', com: 'COM10'}
    Client reports that the app closed the port.

  {type: 'ping'}
    Keepalive.

Server -> Client:
  {type: 'assignment', assignments: [
    {com: 'COM10', service: 'civ',        baud: 19200, parity: 'N', bits: 8, stop: 1},
    {com: 'COM11', service: 'yaesu_cat',  baud: 38400, parity: 'N', bits: 8, stop: 2},
    ...
  ]}
    Reply to 'hello' - port configuration. The client assigns services to its
    COM ports. If the client created fewer COMs than assignments, only the
    first N are active. The client can bind them to the com0com driver.

  {type: 'data', com: 'COM10', hex: 'FEFEE094FBFD'}
    Radio response - the client writes it to virtual COM10, the client app
    (CW Skimmer) reads it from the port as usual.

  {type: 'error', code: 'auth', msg: '...'}
    Auth error, config error, etc.

  {type: 'pong'}
    Reply to ping.

  {type: 'service_status', service: 'civ', online: true}
    Status of a given service on the server (radio online/offline).

SECURITY:
  - Bearer token auth in the HTTP Upgrade header
  - Rate limit: max 1000 bytes/s per client/port (since CI-V 19200 baud = 2400 B/s)
  - Hex format validated before forwarding
  - Client can be disconnected on failed auth (403)
"""
import asyncio
import time
import threading as _threading
from typing import Dict, List, Optional


class ComBridgeWs:
    """
    Server-side WS bridge for Windows COM clients.

    Works together with:
      - civ.CivRig     (for service='civ')                  — CI-V IC-7300
      - yaesu.YaesuRig (for service='yaesu_cat', future)
      - kenwood.KenwoodRig (for service='kenwood_cat', future)

    Usage in webapp.py:
        self.com_bridge = ComBridgeWs(
            civ_rig=self.rig,      # object with write_bytes_raw + add_bridge_listener
            hub=self.hub,          # to broadcast that a WSS client connected
            log=print,
        )
        # In the route handler for /ws/com-bridge:
        await self.com_bridge.handle_client(ws, user)
    """

    # Internal service name + validation
    SERVICES = {
        'civ': {
            'name':          'Icom CI-V (IC-7300 / IC-9700)',
            'default_baud':  19200,
            'default_parity': 'N',
            'default_bits':  8,
            'default_stop':  1,
            'available':     True,
        },
        'yaesu_cat': {
            'name':          'Yaesu CAT (FT-891/991/DX10)',
            'default_baud':  38400,
            'default_parity': 'N',
            'default_bits':  8,
            'default_stop':  2,  # Yaesu requires 2 stop bits!
            'available':     False,  # placeholder - needs a physical port on the server
        },
        'kenwood_cat': {
            'name':          'Kenwood CAT (TS-590/2000/Elecraft K3)',
            'default_baud':  9600,
            'default_parity': 'N',
            'default_bits':  8,
            'default_stop':  1,
            'available':     False,  # placeholder
        },
    }

    def __init__(self, civ_rig=None, hub=None, log=print, can_write=None):
        self.civ_rig = civ_rig  # CivRig from civ.py
        self.hub     = hub
        self.log     = log
        # can_write(user_id) -> bool: whether the user is allowed to WRITE to
        # the radio (holds radio_lock or is admin). READ commands (read
        # freq/mode/S-meter) are always allowed. WRITE commands (change
        # freq/mode/PTT) only for the lock holder. Without this a user
        # without the lock could tune the frequency via the COM Bridge,
        # bypassing the UI locks!
        self.can_write = can_write or (lambda uid: True)

        # Active clients: dict[ws] = ClientInfo
        # ClientInfo = {
        #   'user_id':      str,
        #   'peer':         str,  # 'ip:port'
        #   'ports':        [str, ...],       # its COM ports reported in hello
        #   'assignments':  [dict, ...],       # what we assigned it
        #   'rate_bytes':   int,               # byte count in the 1s window
        #   'rate_window':  float,             # window start time
        #   'connected_at': float,
        # }
        self._clients: Dict = {}
        self._clients_lock = asyncio.Lock()
        self._main_loop = None  # set on first use
        # Counter of in-flight broadcasts (backpressure). Modified from TWO
        # places: the CI-V thread (increment) and the event loop
        # (decrement), and 'x += 1' isn't atomic in Python - hence the
        # thread lock.
        self._bc_inflight = 0
        self._bc_lock = _threading.Lock()

        # Register as a listener on the CI-V bridge (from civ.py).
        # The callback fires for every chunk of bytes from the radio ->
        # forward to all clients that have service='civ' assigned.
        if self.civ_rig and hasattr(self.civ_rig, 'add_bridge_listener'):
            self.civ_rig.add_bridge_listener(self._on_civ_data_from_radio)
            self.log("[com_bridge_ws] registered as CI-V listener")
        else:
            self.log(f"[com_bridge_ws] WARNING: NOT registered as CI-V listener! "
                     f"civ_rig={type(self.civ_rig).__name__ if self.civ_rig else None} "
                     f"has_method={hasattr(self.civ_rig, 'add_bridge_listener') if self.civ_rig else False}")

    def attach_civ_rig(self, new_civ_rig):
        """
        Rewire the listener to a new CivRig (after a reconnect/radio model change).

        webapp.py creates a new CivRig on reconnect - the old reference is
        dead (no longer reads the radio). This method detaches the listener
        from the old one (if any) and attaches it to the new one.

        Without this, CW Skimmer/HRD stopped getting radio responses after
        every rig reconnect - the listener was hanging off a dead object.
        """
        # Detach from the old one (if it existed and has the method)
        if self.civ_rig and hasattr(self.civ_rig, 'remove_bridge_listener'):
            try:
                self.civ_rig.remove_bridge_listener(self._on_civ_data_from_radio)
            except Exception as e:
                self.log(f"[com_bridge_ws] remove old listener: {e}")

        self.civ_rig = new_civ_rig

        # Attach to the new one
        if self.civ_rig and hasattr(self.civ_rig, 'add_bridge_listener'):
            self.civ_rig.add_bridge_listener(self._on_civ_data_from_radio)
            self.log("[com_bridge_ws] rewired to new CivRig - listener active")
        else:
            self.log(f"[com_bridge_ws] attach_civ_rig: new rig is not a CivRig "
                     f"({type(new_civ_rig).__name__ if new_civ_rig else None})")

    # ── Per-user configuration ──────────────────────────────────────────────
    def build_assignments(self, user_config: List[dict]) -> List[dict]:
        """
        Returns assignments for the client based on the user config.
        user_config: [{"service": "civ", "baud": 19200}, ...]
        Returns:      [{"com": "COM10", "service": "civ", "baud": 19200, ...}, ...]
        (com is assigned dynamically depending on the ports reported by the client)

        Each CI-V port gets a GLOBALLY UNIQUE controller address (civ_addr)
        across ALL clients. Without this, two users (admin + operator) would
        get the same E1/E2 addresses - radio responses would get mixed up
        between users. Addresses are now allocated from the global
        _civ_addr_pool.
        """
        assignments = []
        for idx, entry in enumerate(user_config or []):
            service = entry.get('service', 'civ')
            if service not in self.SERVICES:
                self.log(f"[com_bridge_ws] unknown service '{service}', skipping")
                continue
            defaults = self.SERVICES[service]
            asn = {
                'index':   idx,   # order - the client assigns COMs in this order
                'service': service,
                'name':    defaults['name'],
                'baud':    int(entry.get('baud',   defaults['default_baud'])),
                'parity':  str(entry.get('parity', defaults['default_parity']))[:1],
                'bits':    int(entry.get('bits',   defaults['default_bits'])),
                'stop':    int(entry.get('stop',   defaults['default_stop'])),
            }
            # Assign a GLOBALLY unique CI-V address for each civ port.
            # Pool E1-EF (15 addresses) shared across all clients.
            if service == 'civ':
                asn['civ_addr'] = self._alloc_civ_addr()
            assignments.append(asn)
        return assignments

    def _alloc_civ_addr(self) -> int:
        """Allocate a free CI-V address from the global E1-EF pool."""
        if not hasattr(self, '_civ_addr_used'):
            self._civ_addr_used = set()
        for addr in range(0xE1, 0xF0):  # E1..EF
            if addr not in self._civ_addr_used:
                self._civ_addr_used.add(addr)
                return addr
        # All taken (>15 ports total) - recycle from E1
        # (rare case, may cause collisions but better than a crash)
        self.log("[com_bridge_ws] WARNING: CI-V address pool exhausted (>15 ports)")
        return 0xE1

    def _free_civ_addrs(self, assignments: list):
        """Free the CI-V addresses used by these assignments (on client disconnect)."""
        if not hasattr(self, '_civ_addr_used'):
            return
        for asn in assignments:
            addr = asn.get('civ_addr')
            if addr is not None:
                self._civ_addr_used.discard(addr)

    # ── Client handler ─────────────────────────────────────────────────────
    async def handle_client(self, ws, user: dict, user_config: List[dict]):
        """
        Main client handling loop. Called from webapp.py after authorization
        (JWT verified) in the route handler for /ws/com-bridge.

        user:        {"user_id": "...", "callsign": "...", "role": "..."}
        user_config: this user's list of ports from config (from /api/com/config)
        """
        # Save a reference to the main event loop (for _on_civ_data_from_radio,
        # which is called from a different thread and has no access to the loop).
        if self._main_loop is None:
            self._main_loop = asyncio.get_running_loop()
            self.log(f"[com_bridge_ws] event loop saved for the thread callback")

        peer = self._peer_str(ws)
        info = {
            'user_id':      user.get('user_id', ''),
            'peer':         peer,
            'ports':        [],
            'assignments':  [],
            'rate_bytes':   0,
            'rate_window':  time.monotonic(),
            'connected_at': time.monotonic(),
        }
        async with self._clients_lock:
            self._clients[ws] = info
        self.log(f"[com_bridge_ws] client {peer} ({info['user_id']}) connected "
                 f"(total: {len(self._clients)})")

        try:
            async for msg in ws:
                # Log the first 10 messages - to see the message TYPE
                if info.get('msg_count', 0) < 10:
                    self.log(f"[com_bridge_ws] {peer}: WS msg #{info.get('msg_count', 0)+1} "
                             f"type={msg.type.name} data_len={len(str(msg.data)) if msg.data else 0}")
                    info['msg_count'] = info.get('msg_count', 0) + 1
                # aiohttp WSMessage - we only read TEXT (JSON)
                if msg.type.name != 'TEXT':
                    continue
                try:
                    data = self._parse_json(msg.data)
                except Exception as _pe:
                    if info.get('parse_err_logged', 0) < 3:
                        self.log(f"[com_bridge_ws] {peer}: JSON parse err: {_pe} "
                                 f"raw={str(msg.data)[:80]}")
                        info['parse_err_logged'] = info.get('parse_err_logged', 0) + 1
                    continue
                if not isinstance(data, dict):
                    continue
                t = data.get('type')

                if t == 'hello':
                    ports = data.get('ports', [])
                    if not isinstance(ports, list):
                        await ws.send_json({'type': 'error', 'code': 'bad_hello',
                                             'msg': 'ports must be a list'})
                        continue
                    info['ports'] = [str(p)[:20] for p in ports[:16]]  # limit of 16 ports
                    info['assignments'] = self.build_assignments(user_config)
                    # Assign COMs to assignments in order
                    for i, asn in enumerate(info['assignments']):
                        if i < len(info['ports']):
                            asn['com'] = info['ports'][i]
                        else:
                            asn['com'] = None  # no COM available - client will ignore it
                    self.log(f"[com_bridge_ws] {peer}: hello - {len(info['ports'])} ports, "
                             f"{len(info['assignments'])} assignments")
                    await ws.send_json({'type': 'assignment',
                                         'assignments': info['assignments']})
                    # Send the status of all services (client knows what's online)
                    await self._send_service_status(ws)

                elif t == 'data':
                    com   = data.get('com', '')
                    hex_s = data.get('hex', '')
                    # Log only the first few times per client (don't spam)
                    if info.get('data_logged', 0) < 5:
                        self.log(f"[com_bridge_ws] {peer}: data from client com={com} "
                                 f"hex={hex_s[:40]}{'...' if len(hex_s)>40 else ''}")
                        info['data_logged'] = info.get('data_logged', 0) + 1
                    await self._on_client_data(ws, info, com, hex_s)

                elif t == 'open':
                    com = data.get('com', '')
                    self.log(f"[com_bridge_ws] {peer}: app opened {com}")

                elif t == 'close':
                    com = data.get('com', '')
                    self.log(f"[com_bridge_ws] {peer}: app closed {com}")

                elif t == 'ping':
                    await ws.send_json({'type': 'pong'})

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log(f"[com_bridge_ws] {peer}: loop error - {e}")
        finally:
            async with self._clients_lock:
                self._clients.pop(ws, None)
            # Free the CI-V addresses used by this client (back to the pool)
            self._free_civ_addrs(info.get('assignments', []))
            self.log(f"[com_bridge_ws] client {peer} disconnected "
                     f"(total: {len(self._clients)})")

    def _rewrite_ctrl_addr(self, data: bytes, from_addr: int, to_addr: int) -> bytes:
        """
        Rewrite the controller address (the 'from' field in a CI-V frame)
        from from_addr to to_addr. Handles a stream with multiple
        concatenated frames.

        CI-V frame: FE FE <to> <from> <cmd> ... FD
        The 'from' field is position 3 (0-indexed) in every frame.
        """
        if from_addr not in data:
            return data  # fast path - nothing to change
        out = bytearray(data)
        i = 0
        n = len(out)
        while i < n - 3:
            # Look for a frame start FE FE
            if out[i] == 0xFE and out[i+1] == 0xFE:
                # position 3 = from addr
                if out[i+3] == from_addr:
                    out[i+3] = to_addr
                # Skip to the end of this frame (FD)
                j = out.find(b'\xFD', i+2)
                if j < 0:
                    break
                i = j + 1
            else:
                i += 1
        return bytes(out)

    # CI-V commands that CHANGE the radio's state (WRITE). Blocked for users
    # without radio_lock. All others (read freq 0x03, read mode 0x04, read
    # S-meter 0x15, read VFO 0x25 with sub=read, etc.) are always allowed.
    # Ref: IC-7300 CI-V reference manual.
    _CIV_WRITE_CMDS = {
        0x00,  # set freq (transceive)
        0x01,  # set mode (transceive)
        0x02,  # (unused set)
        0x05,  # set freq
        0x06,  # set mode
        0x07,  # set VFO / VFO operations
        0x08,  # set memory channel
        0x09,  # memory write
        0x0A,  # memory->VFO
        0x0B,  # memory clear
        0x0F,  # set split/duplex
        0x10,  # set tuning step
        0x16,  # set various (AGC, preamp, atten, etc)
        0x1A,  # set various (data mode, etc) - NOTE: 1A 05 is also read/write
        0x1C,  # set PTT / ATU (0x1C 0x00=PTT, 0x1C 0x01=ATU)
    }
    # Commands 0x25 (VFO freq) and 0x26 (VFO mode): sub-command 0x00=read,
    # 0x01=set. The sub-command is checked separately.
    # Command 0x1A: 0x1A 0x05 <nn> read/write of settings - we block writes
    # (when it has a payload beyond just the setting number).

    def _strip_write_commands(self, data: bytes) -> bytes:
        """
        Return only the READ frames from the stream. WRITE frames (radio
        state changes) are removed. Used when the user doesn't have
        radio_lock - can only read.

        Frame: FE FE <to> <from> <cmd> [sub] ... FD
        """
        out = bytearray()
        i = 0
        n = len(data)
        while i < n - 3:
            if data[i] == 0xFE and data[i+1] == 0xFE:
                j = data.find(b'\xFD', i+2)
                if j < 0:
                    break
                frame = data[i:j+1]
                if len(frame) >= 6:
                    cmd = frame[4]
                    sub = frame[5] if len(frame) >= 7 else None
                    is_write = self._is_write_command(cmd, sub, len(frame))
                    if not is_write:
                        out += frame
                    else:
                        if getattr(self, '_blocked_write_logged', 0) < 10:
                            self.log(f"[com_bridge_ws] BLOCKED write (no lock): "
                                     f"cmd={cmd:02X} sub={sub if sub is None else f'{sub:02X}'}")
                            self._blocked_write_logged = getattr(self, '_blocked_write_logged', 0) + 1
                else:
                    out += frame  # too short - pass through (harmless)
                i = j + 1
            else:
                i += 1
        return bytes(out)

    def _is_write_command(self, cmd: int, sub, frame_len: int) -> bool:
        """Does this CI-V command change the radio's state (WRITE)?

        Tightened: HRD tunes with command 0x25/0x01 (set VFO freq) and
        0x00/0x05 (set freq). We ALWAYS block these when sub indicates a
        set, regardless of frame length (HRD may send different formats).
        """
        # 0x00 (set freq transceive), 0x05 (set freq), 0x01/0x06 (set mode):
        # ALWAYS write. Read freq/mode are separate commands (0x03/0x04).
        if cmd in (0x00, 0x01, 0x05, 0x06):
            return True
        # 0x25 (VFO freq), 0x26 (VFO mode): KEY POINT - both sub 0x00 and
        # 0x01 can be SET if they carry freq/mode data!
        #   25 00 (7B)          = READ VFO A freq
        #   25 00 <5B freq> (12B) = SET VFO A freq  <- GAP! this is how HRD tunes
        #   25 01 (7B)          = READ VFO B freq (but also possibly a short HRD SET?)
        #   25 01 <5B freq> (12B) = SET VFO B freq
        # Rule: if the frame is LONGER than just cmd+sub (flen>7) = SET (write).
        # Short (flen==7, cmd+sub only) = READ.
        # EXCEPTION: HRD sometimes sends a short 25/01 as a poll - but to be
        # safe we block EVERY 25/01 (sub=01) and 26/01, and 25/00 and 26/00
        # when they carry data (flen>7).
        if cmd in (0x25, 0x26):
            if sub == 0x01:
                return True  # sub=01 always block (SET VFO B / tuning poll)
            if sub == 0x00:
                return frame_len > 7  # 25/00 with data = SET VFO A freq!
            return frame_len > 7  # other sub with data = write
        # 0x07 (VFO select/ops), 0x08-0x0B (memory), 0x0F (split), 0x10 (step):
        # write
        if cmd in (0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0F, 0x10):
            return True
        # 0x1A: read/write of radio settings. Format 1A 05 <2B number> [value].
        # DECISION: we let 1A 05 (config parameters) through even for a user
        # without the lock. These commands SET interface parameters (e.g.
        # 0075, 0071, 0053) that CW Skimmer/HRD configure at startup -
        # without them the app will NOT initialize and won't show a
        # frequency. They don't change the frequency or trigger
        # transmission, so they're safe. Critical writes (freq/mode/PTT/
        # split) are still blocked.
        if cmd == 0x1A:
            return False  # let configuration through (doesn't change freq/TX)
        # 0x1C: PTT/ATU. Read = 7B (1C 00), set = >7B
        if cmd == 0x1C:
            return frame_len > 7
        # 0x16: radio toggles (AGC, preamp, atten, NB, NR, filters).
        # DECISION: let through - CW Skimmer/HRD set these toggles at
        # startup (16/22, 16/40-46). Without them the app may fail to
        # initialize. They don't change the frequency or trigger
        # transmission. A user without the lock can therefore flip e.g. the
        # preamp, but can't tune the radio or start transmitting - an
        # acceptable tradeoff for a working preview in CAT apps.
        if cmd == 0x16:
            return False  # let toggles through (doesn't change freq/TX)
        # 0x14: levels (AF/RF/squelch). Read = 7B (14 nn), set = >7B (with a value)
        if cmd == 0x14:
            return frame_len > 7
        # READ commands - always allowed:
        # 0x03 read freq, 0x04 read mode, 0x15 read meters, 0x19 read id,
        # 0x1A 0x05 read (handled above), 0x21 read RIT/data, 0x27 scope
        if cmd in (0x03, 0x04, 0x15, 0x19, 0x21, 0x27):
            return False
        # Everything else from the WRITE list (fallback)
        return cmd in self._CIV_WRITE_CMDS

    def _split_read_write(self, data: bytes):
        """
        Split the stream into (read_frames, write_frames).
        read_frames - bytes to send to the radio (allowed).
        write_frames - list of bytes (each write frame separately) to echo back.
        """
        read_out = bytearray()
        write_frames = []
        i = 0
        n = len(data)
        while i < n - 3:
            if data[i] == 0xFE and data[i+1] == 0xFE:
                j = data.find(b'\xFD', i+2)
                if j < 0:
                    break
                frame = data[i:j+1]
                if len(frame) >= 6:
                    cmd = frame[4]
                    sub = frame[5] if len(frame) >= 7 else None
                    if self._is_write_command(cmd, sub, len(frame)):
                        write_frames.append(bytes(frame))
                    else:
                        read_out += frame
                else:
                    read_out += frame
                i = j + 1
            else:
                i += 1
        return bytes(read_out), write_frames

    async def _send_fake_echo(self, ws, assignment, write_frames, civ_addr):
        """
        Send an echo of blocked write commands back to the client (without
        sending them to the radio). The app (CW Skimmer/HRD) gets an echo
        of its own command and can continue initializing. The radio stays
        unchanged.

        For SET commands the radio normally replies with an echo + ACK
        (FB). We send back an echo (the command) + ACK so the app is
        satisfied. Address rewritten civ_addr->E0 because the app sent it as E0.
        """
        com = assignment.get('com')
        if not com:
            return
        out = bytearray()
        for fr in write_frames:
            # Echo: the command sent back, address civ_addr->E0
            echo = self._rewrite_both_addr(fr, from_addr=civ_addr, to_addr=0xE0)
            out += echo
            # ACK (radio confirms SET): FE FE E0 94 FB FD
            # to=E0 (controller), from=94 (radio), FB=OK
            out += bytes([0xFE, 0xFE, 0xE0, 0x94, 0xFB, 0xFD])
        if out:
            hex_s = out.hex().upper()
            if getattr(self, '_fake_echo_logged', 0) < 15:
                self.log(f"[com_bridge_ws] fake-echo -> {com} "
                         f"(write blocked, radio untouched): {hex_s}")
                self._fake_echo_logged = getattr(self, '_fake_echo_logged', 0) + 1
            try:
                await ws.send_json({'type': 'data', 'com': com, 'hex': hex_s})
            except Exception:
                pass

    async def _on_client_data(self, ws, info: dict, com: str, hex_s: str):
        """Data from the client - forward to the physical radio based on the assignment."""
        # Find the service assigned to this COM
        assignment = None
        for asn in info['assignments']:
            if asn.get('com') == com:
                assignment = asn
                break
        if not assignment:
            return  # unassigned COM - silently ignore

        # Validate hex
        try:
            raw = bytes.fromhex(hex_s.replace(' ', ''))
        except ValueError:
            self.log(f"[com_bridge_ws] {info['peer']}: bad hex - {hex_s[:20]}")
            return
        if len(raw) == 0 or len(raw) > 512:
            return  # protection against oversized frames

        # ── ADDRESS REWRITE ──────────────────────────────────────────────────
        # Problem: the server has its own CI-V poller (controller address
        # 0xE0). CW Skimmer/HRD also use 0xE0. When both query the radio at
        # the same time, responses get mixed up - the server picks up a
        # response meant for the client (and vice versa), which breaks the
        # S-meter/freq display on both sides.
        #
        # Solution: each CI-V port has a UNIQUE controller address from its
        # assignment (civ_addr: E1, E2, E3...). We rewrite the app's
        # controller address 0xE0 -> civ_addr in frames GOING TO the radio.
        # The radio replies to civ_addr. The server ignores it (only
        # accepts 0xE0/0x00). Different apps (CW Skimmer=E1, HRD=E2) also
        # don't get mixed up with each other.
        civ_addr = assignment.get('civ_addr', 0xE1)
        raw = self._rewrite_ctrl_addr(raw, from_addr=0xE0, to_addr=civ_addr)

        # ── RADIO_LOCK PROTECTION ─────────────────────────────────────────────
        # A user without radio_lock can only READ (freq/mode/S-meter).
        # Commands that change the radio's state (set freq, set mode, PTT)
        # are BLOCKED - without this a user without the lock could tune the
        # frequency via the COM Bridge, bypassing the UI locks!
        #
        # WRITE commands are recognized by their CI-V code (position 4 in
        # the frame, after FE FE to from). The stream is filtered - only
        # READ passes through, WRITE is blocked if the user doesn't have
        # permission.
        uid = info.get('user_id', '')
        _can = self.can_write(uid)
        # Diagnostics - log EVERY WRITE command (to catch what the user is
        # tuning). No limit for WRITE - we want to see every attempted change.
        _i = 0
        while _i < len(raw) - 3:
            if raw[_i] == 0xFE and raw[_i+1] == 0xFE:
                _j = raw.find(b'\xFD', _i+2)
                if _j < 0:
                    break
                _fr = raw[_i:_j+1]
                if len(_fr) >= 5:
                    _cmd = _fr[4]
                    _sub = _fr[5] if len(_fr) >= 7 else None
                    _w = self._is_write_command(_cmd, _sub, len(_fr))
                    if _w:
                        # EVERY write attempt - log with the full hex and can_write
                        self.log(f"[com_bridge_ws] {info['peer']} WRITE-ATTEMPT: "
                                 f"cmd={_cmd:02X}"
                                 f"{'/'+format(_sub,'02X') if _sub is not None else ''}"
                                 f"[{len(_fr)}B] can_write={_can} "
                                 f"hex={_fr.hex().upper()}")
                _i = _j + 1
            else:
                _i += 1
        if not _can:
            # Split into write (blocked) and read (passed through).
            # IMPORTANT: apps (CW Skimmer/HRD) wait for an ECHO of each
            # command before the next one. If we simply drop the write, the
            # app hangs waiting for the echo -> never initializes -> shows
            # 0.00. Solution: for blocked writes we GENERATE an echo (send
            # the command back to the client as if the radio had
            # acknowledged it) but DON'T send it to the radio. The app
            # thinks it went through, the radio is unchanged.
            read_part, blocked_writes = self._split_read_write(raw)
            # Send the echo of blocked commands back to the client (on its COM)
            if blocked_writes:
                await self._send_fake_echo(ws, assignment, blocked_writes, civ_addr)
            raw = read_part
            if not raw:
                return  # it was write only - echo sent, nothing to send to the radio

        # Rate limit (per client, globally): raised to 10000 B/s.
        # CI-V is normally ~2400 B/s @ 19200 baud, but with CW Skimmer/HRD
        # polling + responses, short peaks can occur. 1000 B/s was too low.
        now = time.monotonic()
        if now - info['rate_window'] >= 1.0:
            if info.get('rate_hit', 0) > 0:
                self.log(f"[com_bridge_ws] {info['peer']}: rate hits in the last second: "
                         f"{info.get('rate_hit', 0)}")
            info['rate_window'] = now
            info['rate_bytes'] = 0
            info['rate_hit'] = 0
        info['rate_bytes'] += len(raw)
        if info['rate_bytes'] > 10000:
            info['rate_hit'] = info.get('rate_hit', 0) + 1
            return

        # Dispatch by service
        service = assignment.get('service')
        if service == 'civ' and self.civ_rig:
            try:
                # write_bytes_raw uses a threading lock - run in an executor thread
                await asyncio.to_thread(self.civ_rig.write_bytes_raw, raw)
            except Exception as e:
                self.log(f"[com_bridge_ws] civ write error: {e}")
        elif service in ('yaesu_cat', 'kenwood_cat'):
            # PLACEHOLDER - needs a physical port on the server
            # Future: self.yaesu_rig.write_bytes_raw(raw)
            pass
        else:
            self.log(f"[com_bridge_ws] unsupported service: {service}")

    # ── Data from the radio -> clients ────────────────────────────────────────────
    def _on_civ_data_from_radio(self, data: bytes):
        """
        Callback from CivRig - every chunk of bytes from the physical CI-V
        radio. Called IN THE CivRig THREAD (not the event loop). We must
        schedule the broadcast on the event loop.

        Caches a reference to the main event loop because
        asyncio.get_event_loop() in a non-main thread raises RuntimeError
        in Python 3.12. The reference is saved on the first call from a
        coroutine (where we have a running loop).
        """
        if not self._clients:
            return
        loop = self._main_loop
        if loop is None:
            # We don't have a saved loop yet - try to get one
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                # No loop in this thread - unfortunately we have to skip it
                if not hasattr(self, '_no_loop_logged'):
                    self.log(f"[com_bridge_ws] WARNING: _on_civ_data_from_radio "
                             f"has no event loop - radio data LOST")
                    self._no_loop_logged = True
                return

        # FRAME BUFFERING: the civ reader reads in chunks that are NOT
        # aligned to CI-V frames. A frame can be split across chunks -
        # especially when both CW Skimmer and HRD are polling (dense
        # stream). Without buffering, _filter_frames_for_client loses split
        # frames -> freq flickers in the app. Complete FEFE...FD frames are
        # assembled here.
        if not hasattr(self, '_rx_buf'):
            self._rx_buf = bytearray()
        self._rx_buf += data
        # Cut out all COMPLETE frames, keep the tail (incomplete frame)
        complete = bytearray()
        buf = self._rx_buf
        while True:
            i = buf.find(b'\xFE\xFE')
            if i < 0:
                # no frame start - clear garbage (keep the last byte in
                # case it's the first FE of a new frame)
                if len(buf) > 1:
                    del buf[:-1]
                break
            if i > 0:
                del buf[:i]  # remove garbage before the frame
            j = buf.find(b'\xFD', 2)
            if j < 0:
                # incomplete frame - wait for more data
                # protection against unbounded buffer growth
                if len(buf) > 512:
                    del buf[:256]
                break
            complete += buf[:j+1]
            del buf[:j+1]

        if not complete:
            return  # nothing complete yet

        data = bytes(complete)

        # Log the first few times
        if not hasattr(self, '_radio_cb_logged'):
            self._radio_cb_logged = 0
        if self._radio_cb_logged < 5:
            self.log(f"[com_bridge_ws] _on_civ_data_from_radio called "
                     f"({len(data)}B of complete frames), clients={len(self._clients)}")
            self._radio_cb_logged += 1
        try:
            # STABILITY: backpressure. If the broadcast can't keep up with
            # the CI-V stream (slow client / dense poll), tasks would pile
            # up eating memory. We cap the number of concurrent broadcasts -
            # excess frames are dropped (CI-V data is ephemeral, fresh
            # matters more than old).
            with self._bc_lock:
                if self._bc_inflight >= 32:
                    if not hasattr(self, "_bc_drop_logged"):
                        self._bc_drop_logged = 0
                    if self._bc_drop_logged < 3:
                        self.log("[com_bridge_ws] WARNING: broadcast can't keep up, "
                                 "dropping frame (slow client?)")
                        self._bc_drop_logged += 1
                    return
                # Increment HERE (not inside the task) - between create_task
                # and the task actually starting, the counter must already
                # reflect the scheduled send.
                self._bc_inflight += 1
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._broadcast_civ_guarded(data))
            )
        except RuntimeError as e:
            with self._bc_lock:
                self._bc_inflight = max(0, self._bc_inflight - 1)
            if not hasattr(self, '_cb_err_logged'):
                self.log(f"[com_bridge_ws] call_soon_threadsafe err: {e}")
                self._cb_err_logged = True

    async def _broadcast_civ_guarded(self, data: bytes):
        """Wrapper that releases the backpressure counter (incremented by the caller)."""
        try:
            await self._broadcast_civ(data)
        finally:
            with self._bc_lock:
                self._bc_inflight = max(0, self._bc_inflight - 1)

    async def _broadcast_civ(self, data: bytes):
        """Broadcast CI-V data to clients - EACH gets only its own frames.

        Each CI-V port has a unique address (civ_addr E1/E2/...). For each
        client/port we filter frames by ITS civ_addr, rewrite them back to
        E0 (since the app sent them as E0), and send. This way CW Skimmer
        (E1) and HRD (E2) each get only their own responses - they don't
        mix with each other or with the server's own poller (E0).
        """
        async with self._clients_lock:
            clients_snapshot = list(self._clients.items())

        # STABILITY/PERFORMANCE: collect all sends and run them IN PARALLEL.
        # Previously these were sequential (await in a loop) - one slow
        # client (weak connection, tunnel) blocked delivery to the others,
        # and new broadcast tasks would pile up under a dense CI-V stream.
        sends = []
        for ws, info in clients_snapshot:
            for asn in info['assignments']:
                if asn.get('service') != 'civ' or not asn.get('com'):
                    continue
                civ_addr = asn.get('civ_addr', 0xE1)
                # Filter frames for this specific port (its civ_addr)
                filtered = self._filter_frames_for_client(data, client_addr=civ_addr)
                if not filtered:
                    continue
                # Rewrite civ_addr -> E0 (the app sent it as E0)
                rewritten = self._rewrite_both_addr(filtered, from_addr=civ_addr, to_addr=0xE0)
                hex_s = rewritten.hex().upper()
                # Log the broadcast (diagnostics for CW Skimmer 0.00)
                if not hasattr(self, '_broadcast_logged'):
                    self._broadcast_logged = 0
                if self._broadcast_logged < 60:
                    self.log(f"[com_bridge_ws] ->{asn['com']} "
                             f"(E{civ_addr:02X}): {hex_s}")
                    self._broadcast_logged += 1
                sends.append(ws.send_json({'type': 'data',
                                            'com': asn['com'],
                                            'hex': hex_s}))

        if sends:
            # return_exceptions=True: a disconnected client doesn't stop
            # delivery to the others (it gets removed anyway in
            # handle_client/finally)
            await asyncio.gather(*sends, return_exceptions=True)

    def _filter_frames_for_client(self, data: bytes, client_addr: int) -> bytes:
        """
        Return the CI-V frames relevant to this client:
        - to=client_addr (0xE1): radio responses to the client's queries
        - to=0x00: transceive broadcast
        - from=client_addr (0xE1): ECHO of the client's own commands

        The echo matters! Many CI-V implementations (including CW Skimmer)
        wait for the echo of their own command BEFORE reading the response.
        Without the echo, the client may conclude the link isn't working.

        Frame: FE FE <to> <from> ... FD, to=position 2, from=position 3.
        """
        out = bytearray()
        i = 0
        n = len(data)
        while i < n - 3:
            if data[i] == 0xFE and data[i+1] == 0xFE:
                j = data.find(b'\xFD', i+2)
                if j < 0:
                    break
                to_addr   = data[i+2]
                from_addr = data[i+3]
                # Let through: responses to the client, broadcast, echo from the client
                if to_addr == client_addr or to_addr == 0x00 or from_addr == client_addr:
                    out += data[i:j+1]
                i = j + 1
            else:
                i += 1
        return bytes(out)

    def _rewrite_to_addr(self, data: bytes, from_addr: int, to_addr: int) -> bytes:
        """Rewrite the 'to' field (position 2) in every frame from from_addr to to_addr."""
        if from_addr not in data:
            return data
        out = bytearray(data)
        i = 0
        n = len(out)
        while i < n - 3:
            if out[i] == 0xFE and out[i+1] == 0xFE:
                if out[i+2] == from_addr:
                    out[i+2] = to_addr
                j = out.find(b'\xFD', i+2)
                if j < 0:
                    break
                i = j + 1
            else:
                i += 1
        return bytes(out)

    def _rewrite_both_addr(self, data: bytes, from_addr: int, to_addr: int) -> bytes:
        """
        Rewrite BOTH address fields ('to' pos.2 and 'from' pos.3) from
        from_addr to to_addr in every frame.

        Used when broadcasting to a client:
        - radio responses: to=E1 -> to=E0 (client sees it as addressed to it)
        - command echo:    from=E1 -> from=E0 (client recognizes its own echo)
        """
        if from_addr not in data:
            return data
        out = bytearray(data)
        i = 0
        n = len(out)
        while i < n - 3:
            if out[i] == 0xFE and out[i+1] == 0xFE:
                # 'to' field (pos 2)
                if out[i+2] == from_addr:
                    out[i+2] = to_addr
                # 'from' field (pos 3)
                if out[i+3] == from_addr:
                    out[i+3] = to_addr
                j = out.find(b'\xFD', i+2)
                if j < 0:
                    break
                i = j + 1
            else:
                i += 1
        return bytes(out)

    async def _send_service_status(self, ws):
        """Tell the client which services are active on the server."""
        # CI-V online = we have a CivRig and the port is open
        civ_online = bool(self.civ_rig and getattr(self.civ_rig, '_ser', None))
        await ws.send_json({'type': 'service_status', 'service': 'civ',
                             'online': civ_online})

    # ── Helpers ────────────────────────────────────────────────────────────
    def _peer_str(self, ws) -> str:
        try:
            peer = ws._req.transport.get_extra_info('peername')
            return f"{peer[0]}:{peer[1]}" if peer else "?:?"
        except Exception:
            return "?:?"

    def _parse_json(self, text: str) -> dict:
        try:
            import orjson
            return orjson.loads(text)
        except ImportError:
            import json
            return json.loads(text)

    def get_stats(self) -> dict:
        """Returns statistics for the /api/com/stats debug endpoint."""
        return {
            'connected_clients': len(self._clients),
            'clients': [{
                'peer':        info['peer'],
                'user_id':     info['user_id'],
                'ports':       info['ports'],
                'assignments': info['assignments'],
                'rate_bytes':  info['rate_bytes'],
                'connected_s': int(time.monotonic() - info['connected_at']),
            } for info in self._clients.values()],
        }
