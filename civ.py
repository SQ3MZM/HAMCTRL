#!/usr/bin/env python3
"""
civ.py — direct CI-V (Icom) driver for a radio with a scope.

DIRECT mode: the server opens the COM port itself and handles
SIMULTANEOUSLY:
  - control (frequency 0x03/0x05, mode 0x04/0x06, PTT 0x1C00, S-meter 0x15 02)
  - wideband SCOPE (waterfall) — CI-V 0x27 0x00 stream

Used ONLY for radios with a built-in spectrum scope (IC-7300/7610/705/9100/7100).
Other radios (IC-746 etc.) still go through rigctld (RigCAT) — unchanged.

The (async) interface is IDENTICAL to RigCAT, so App.rig can be swapped on the fly.

Model-specific parameters (CI-V address, baud rate, mode mapping, scope
parameters) are loaded from rigs/civ_profiles.py based on model_id — so
adding/fixing a model needs no changes to this file.

No pyserial / no COM port (e.g. on Replit) -> SIMULATION mode: generates a
moving spectrum + telemetry, so the panel works even without hardware.

NOTE ON TRANSLATION SCOPE: self.last_err/self.last_msg reach the browser
verbatim — webapp.py returns them directly in an API response body (same
pattern as rigcat.py's last_err/last_msg, see webapp.py:2783-2784). Every
literal string assigned to them in this file is deliberately left in
Polish; only comments, docstrings, and print()/self.log() console text are
translated.
"""
import asyncio, threading, time, math, random
import numpy as np

try:
    import serial  # pyserial
    HAS_SERIAL = True
except Exception:
    HAS_SERIAL = False

from rigs import get_civ_profile
from config import VERBOSE

CTRL_ADDR = 0xE0  # adres kontrolera (PC)


# ── BCD <-> numbers ────────────────────────────────────────────────────────────
def bcd_to_freq(b: bytes) -> int:
    """Icom freq: 5 bytes BCD little-endian (least significant digits first)."""
    hz = 0
    mult = 1
    for byte in b:
        hz += (byte & 0x0F) * mult; mult *= 10
        hz += (byte >> 4) * mult;  mult *= 10
    return hz


def freq_to_bcd(hz: int, n: int = 5) -> bytes:
    s = f"{int(hz):0{n*2}d}"[-n * 2:]   # last n*2 digits
    rev = s[::-1]                        # rev[0] = units
    out = bytearray()
    for i in range(0, n * 2, 2):
        lo = int(rev[i]); hi = int(rev[i + 1])
        out.append((hi << 4) | lo)
    return bytes(out)


def bcd2(b: bytes) -> int:
    """2-byte BCD (e.g. S-meter 0000..0255) -> int."""
    if len(b) < 2:
        return 0
    return (b[0] >> 4) * 1000 + (b[0] & 0x0F) * 100 + (b[1] >> 4) * 10 + (b[1] & 0x0F)


# Mapping for subcommand 27 15 (Scope span, Center/SCROLL-C mode) -> kHz.
# FIX: this used to be {2500, 5000, 10000, 25000, 50000, 100000, 250000,
# 500000} - a guessed "1-2.5-5" decade sequence. The IC-7300 actually
# uses a "1-2-5" sequence instead (confirmed against Hamlib's ic7300.c
# .spectrum_spans table, reverse-engineered from the real radio) - so
# 2500/25000/250000 (2.5/25/250 kHz) don't exist on this radio at all
# and got NGed on every attempt, while 5000/10000/50000/100000/500000
# happened to coincide with real values and worked. Also adds 20000
# (20kHz) and 1000000 (1MHz, the widest span) which weren't offered
# before.
SCOPE_SPAN_KHZ = {
    5000: 5, 10000: 10, 20000: 20, 50000: 50,
    100000: 100, 200000: 200, 500000: 500, 1000000: 1000,
}

# Exact IC-7300 filter width table for SSB/CW/RTTY (CI-V 1A 03,
# idx 00-31 = 50Hz/100Hz steps, per the Instruction Manual):
#   idx 0-9:  50Hz steps   ->  50, 100, 150, ..., 500
#   idx 10-31: 100Hz steps -> 600, 700, ..., 2700
_SSB_FILTER_TABLE = {}
for _i in range(0, 32):
    _SSB_FILTER_TABLE[_i] = (50 + _i*50) if _i <= 9 else (600 + (_i-10)*100)

# idx 32-40: SSB-D range (DATA mode, USB-D/LSB-D) — wider filter up to
# 3600Hz, available ONLY when the radio is in DATA mode (1A 06). civ.py
# doesn't currently track the data-mode flag here, so when we encounter
# idx>31 (which shouldn't happen in voice SSB mode), we linearly
# interpolate 2700->3600Hz as an approximation instead of returning an
# incorrectly clamped 2700Hz.
for _i in range(32, 41):
    _SSB_FILTER_TABLE[_i] = round(2700 + (_i-31) * (3600-2700) / 9)


# AM/FM (CI-V 1A 03, idx 00=200Hz .. 49=10000Hz per the docs p.19-6).
# The step isn't uniform in Icom's documentation — we use the known AM
# filter table for the IC-7300 (typical values from 200Hz to 10kHz in
# uneven steps, roughly matching the AM receiver's bandwidth). For idx>0 we
# apply linear interpolation of 200..10000Hz over 50 points (0-49) as the
# best available approximation without access to the full firmware table.
_AMFM_FILTER_TABLE = {i: round(200 + i * (10000-200) / 49) for i in range(0, 50)}


def filter_width_hz(mode: str, idx: int, data_mode: bool = False) -> int:
    """Exact (SSB/CW/RTTY) or approximate (AM/FM) filter width in Hz for a
    given CI-V 1A 03 index, depending on the operating mode.

    idx 32-40 (SSB-D, up to 3600Hz) is only available when the radio is in
    DATA mode (data_mode=True, polled via 1A 06). In voice SSB mode
    (data_mode=False) the radio shouldn't return idx>31 — if it did anyway,
    clamp to 2700Hz (idx 31) instead of extrapolating."""
    if mode in ("AM", "FM"):
        return _AMFM_FILTER_TABLE.get(idx, _AMFM_FILTER_TABLE[49])
    if idx > 31 and not data_mode:
        idx = 31
    return _SSB_FILTER_TABLE.get(idx, _SSB_FILTER_TABLE[40])


def smeter_to_sunit(raw: int) -> float:
    """Raw Icom S-meter 0..255 -> S-units (S9 = 9, S9+60dB = 15)."""
    if raw <= 120:
        return raw / 120.0 * 9.0
    return 9.0 + (raw - 120) / (241 - 120) * 60.0 / 10.0


class CivRig:
    def __init__(self, cfg: dict, broadcast_sync=None, log=print):
        self.cfg   = cfg or {}
        self.bcast = broadcast_sync or (lambda m: None)
        self.log   = log

        # ── attributes compatible with RigCAT ──
        self.freq      = 14074000
        self.freq_b    = 14074000
        # Time and value of the last LOCAL frequency set (set_freq, e.g.
        # from click-to-tune). Used in _handle_frame to ignore a "stale"
        # 0x03 reading from the radio that may arrive right after sending
        # 0x05 (a delayed CI-V echo/response), so it doesn't overwrite the
        # newly set frequency in the UI before the radio actually updates.
        self._local_freq_set_at  = 0.0
        self._local_freq_set_val = None
        self.mode      = "USB"
        # The radio's active DSP filter (FIL1/2/3, CI-V 06 <mode> <fil>) —
        # selects WHICH mechanically/programmatically configured filter to
        # use, not a change to its width (that's set in the radio's menu).
        self.filter_num = 1
        # Preamp (16 02): 0=OFF, 1=P.AMP1, 2=P.AMP2
        self.preamp = 0
        # Attenuator (11): False=OFF, True=ATT 20dB ON
        self.attenuator = False
        # Antenna Tuner (1C 01): False=OFF (bypass), True=ON (in the chain)
        self.tuner = False
        self.bw        = 2400
        self.data_mode = False   # DATA mode (USB-D/LSB-D), from 1A 06 — affects the filter table
        self.ptt       = False
        # ALC/PWR peak-hold for the CURRENT transmission (reset in
        # set_ptt() on PTT-on). Live SSB voice makes single instantaneous
        # readings look "jumpy"/uncorrelated (huge dynamic range between a
        # loud syllable and a gap between words, sampled a few tens of ms
        # apart) — the peak seen across the whole TX, like a real ALC
        # meter's needle memory, is what actually tells you whether you're
        # driving the radio properly.
        self._alc_peak     = 0.0
        self._alc_peak_raw = 0
        self._pwr_peak     = 0.0
        self._pwr_peak_raw = 0
        self.split     = False
        self.powered   = True     # radio's power state (CI-V 0x18). After
                                  # power OFF the radio doesn't respond — polling is skipped
        self.vfo       = "VFOA"   # active VFO — tracked locally (the IC-7300
                                  # has no CI-V query for the active VFO). Default A
                                  # = the radio is always on VFO A after power-on.
        self.s_meter   = 0.0
        # Current Set Level slider values (CI-V 14 <sub>), in UI units
        # (lvl['min']..lvl['max']) — read from the radio on connect and
        # periodically in the poller, so the UI sliders start from the
        # radio's actual settings, not from 0.
        self.level_values = {}
        self.connected = False
        self.sim       = True
        self.last_err  = ""
        self.last_msg  = ""
        self.rigctld_path  = "(tryb bezposredni CI-V)"
        self.rigctld_found = True   # rigctld isn't used, but the UI expects this

        # ── connection configuration ──
        self.addr  = 0x94
        self.port  = "COM3"
        self.speed = 115200
        self.model = "3073"

        # ── model profile (CI-V) — set in connect() ──
        self.profile    = get_civ_profile(self.model)
        self.mode_map   = self.profile["mode_map"]
        self.mode_rev   = {v: k for k, v in self.mode_map.items()}
        self.scope_max  = self.profile["scope_max"]
        self.scope_header_len = self.profile["scope_header_len"]

        # ── internal state ──
        self._ser = None
        self._running = False
        self._reader_th = None
        self._poller_th = None
        self._sim_th = None
        self._reconnect_th = None

        # ── DTR/RTS keyer ─────────────────────────────────────────────────────
        # Serial port for DTR/RTS keying (may be the same one as CI-V or a
        # separate COM port from a USB adapter). Keys the DTR or RTS line
        # to generate a Morse signal from text (instead of CI-V cmd 17).
        self._keyer_port  = None   # separate port for DTR/RTS, or None (=use _ser)
        self._keyer_line  = None   # 'DTR' | 'RTS' | None
        self._keyer_ser   = None   # serial.Serial for a separate port, or a ref to _ser
        self._keyer_stop  = threading.Event()
        self._keyer_th    = None

        # transactions (one at a time)
        self._txlock = threading.Lock()
        self._wlock  = threading.Lock()
        self._resp_ev = threading.Event()
        self._resp_cmd = None
        self._resp_sub = None
        self._resp_payload = None
        self._resp_matched_cmd = None  # which of the 'expect' codes actually matched (e.g. 0xFB vs 0xFA) — see _transact

        # CI-V TCP Bridge — subscribers to raw bytes from the radio.
        # Callback called for every chunk of bytes received from the port,
        # BEFORE it's parsed in _reader_loop. Used by civ_bridge.py to
        # broadcast raw CI-V to TCP clients (Logger32, HRD, etc.).
        # The callback must be thread-safe (called from the _reader_loop thread).
        self._bridge_listeners: list = []
        self._bridge_lock = threading.Lock()

        # scope
        self.scope_running = False
        self._scope_acc = bytearray()
        self._latest_scope = None   # freshest complete frame (read by the scope pump)
        self._scope_total = 0
        self._scope_logged = 0
        self._tx_logged = 0
        self._rx_logged = 0
        self._scope_last = 0.0
        # scope header (parsed from 0x27 0x00, see _handle_scope)
        self._scope_mode_code = 0       # 00=Center,01=Fixed,02=SCROLL-C,03=SCROLL-F
        self._scope_center = self.freq
        self._scope_span_hz = 20000     # default 20kHz span (real IC-7300 value, see SCOPE_SPAN_KHZ)
        self._scope_lo = self.freq - self._scope_span_hz // 2
        self._scope_hi = self.freq + self._scope_span_hz // 2
        self._scope_oor = 0
        # filter width (CI-V 1A 03), polled in the poller
        self._filter_idx = 0
        self._filter_width_hz = 2400    # default SSB width

    # ════════════════════════════════════════════════════════════════════════
    # Async interface (called by endpoints / rig_poll)
    # ════════════════════════════════════════════════════════════════════════
    async def connect(self, cfg: dict, override: dict | None = None) -> bool:
        cfg = cfg or self.cfg or {}
        rigs = cfg.get("rigs") or [{}]
        rig = rigs[0]
        if override:
            rid = override.get("rigId") or override.get("id")
            if rid is not None:
                rig = next((r for r in rigs if str(r.get("id")) == str(rid)), rig)
            rig = {**rig, **{k: v for k, v in override.items() if v}}

        self.model = str(rig.get("model", "3073"))

        # Load the profile for this model
        self.profile   = get_civ_profile(self.model)
        self.mode_map  = self.profile["mode_map"]
        self.mode_rev  = {v: k for k, v in self.mode_map.items()}
        self.scope_max = self.profile["scope_max"]
        self.scope_header_len = self.profile["scope_header_len"]

        self.port  = rig.get("port", "COM3")
        try:
            self.speed = int(rig.get("speed", self.profile["default_baud"]))
        except Exception:
            self.speed = self.profile["default_baud"]

        civ = rig.get("civ") or rig.get("civAddr")
        if civ:
            self.addr = self._parse_civ(civ)
        else:
            self.addr = self.profile["default_addr"]

        self._rig_name = rig.get("name", self.profile.get("name", "Radio"))

        self.log(f"[civ] DIRECT CI-V mode — model={self.model} "
                 f"({self.profile.get('name','?')}) port={self.port} "
                 f"{self.speed}bd CI-V=0x{self.addr:02X}")
        if self.profile is not None and "NIEZWERYFIKOWANE" in self.profile.get("notes", ""):
            self.log(f"[civ] WARNING for {self.profile.get('name')}: {self.profile['notes']}")

        # If this object already had an open port (e.g. clicking "Connect"
        # again without changing the model — webapp.py doesn't create a new
        # CivRig in that case) — close the old connection before opening a
        # new one, otherwise serial.Serial() gets a PermissionError (port
        # already open by this same process).
        if self._ser is not None:
            self.log("[civ] Reconnecting — closing existing port")
            self.close()
            await asyncio.sleep(0.3)

        return await asyncio.to_thread(self._open)

    async def get_freq(self) -> int:
        return self.freq

    async def get_freq_live(self, timeout: float = 0.3):
        """Fresh CI-V frequency read straight from the radio (cmd 03),
        bypassing self.freq — set_freq() sets self.freq OPTIMISTICALLY,
        before the write is even attempted, so self.freq/get_freq() being
        "correct" proves nothing about whether the write actually reached
        the radio. Used by webapp.py's ft8_qsy handler to verify+retry a
        band-select retune instead of firing the set_freq() write blind.
        Returns None on timeout/no response (sim mode returns self.freq)."""
        if self.sim or not self._ser:
            return self.freq
        bp = await asyncio.to_thread(self._transact, bytes([0x03]), {0x03, 0x00}, timeout)
        if not bp:
            return None
        f = bcd_to_freq(bp[:5])
        return f if f and f > 0 else None

    async def get_mode(self):
        return self.mode, self.bw

    async def get_smeter(self) -> float:
        return self.s_meter

    async def set_freq(self, hz: int):
        self.freq = int(hz)
        self._local_freq_set_at  = time.time()
        self._local_freq_set_val = int(hz)
        if self.sim or not self._ser:
            return
        # Fire-and-forget: we don't wait for ACK (0xFB/0xFA) — the radio
        # rarely NAKs a frequency set, and waiting up to 0.4s for the ACK
        # caused a noticeable delay on click-to-tune / fast retuning.
        try:
            await asyncio.to_thread(self._write, bytes([0x05]) + freq_to_bcd(hz))
        except Exception as e:
            self.log(f"[civ] set_freq write error: {e}")

    async def set_freq_b(self, hz: int):
        """Set the VFO-B (inactive VFO) frequency without switching to it.
        CI-V: 25 01 [5B freq BCD] (per doc p.19-13 "Selected/unselected
        VFO frequency settings", 01=unselected). Fire-and-forget like
        set_freq — the radio rarely NAKs a frequency set.
        """
        self.freq_b = int(hz)
        if self.sim or not self._ser:
            return
        try:
            await asyncio.to_thread(self._write, bytes([0x25, 0x01]) + freq_to_bcd(hz))
        except Exception as e:
            self.log(f"[civ] set_freq_b write error: {e}")

    def set_scope_span(self, span_hz: int) -> bool:
        """Set the waterfall span (Center mode). CI-V: 27 15 [VFO-select][5B BCD LE].
        Returns True only if the radio actually ACKed (0xFB) the command,
        False on NG (0xFA), timeout, or an unsupported span_hz.

        Supported values (Hz): 5000, 10000, 20000, 50000, 100000, 200000,
        500000, 1000000 — see SCOPE_SPAN_KHZ; anything else is rejected.

        FIX: the radio NGed every span change with the old encoding.
        Verified against Hamlib's icom.c (rig_set_level,
        RIG_LEVEL_SPECTRUM_SPAN case) - the real CI-V frame for 27 15 is
        6 DATA bytes, not 5:
          byte 0   = spectrum VFO select (icom_get_spectrum_vfo(): 0x00
                     for a radio without a Sub receiver, which is the
                     IC-7300 - our old code sent straight into the BCD
                     span without this byte at all)
          byte 1-5 = 5-byte BCD-LE of span_hz/2, NOT span_hz - Icom
                     represents scope span on the wire as a +/- (radius)
                     value (confirmed symmetric in Hamlib: the SET path
                     does val/2 before to_bcd, the GET/read-back path
                     does *2 after from_bcd). Our old code encoded the
                     full span_hz, which for every value except by pure
                     coincidence produces a completely different (and
                     invalid) number on the wire - explains the NG on
                     every span tested live.
        Also see _transact's docstring — this was the only CI-V "set"
        command in this file that used to be fire-and-forget with zero
        response check, which is why the NG went unnoticed until logged.
        """
        if span_hz not in SCOPE_SPAN_KHZ:
            return False
        if self.sim or not self._ser:
            self._scope_span_hz = span_hz
            return True
        half = span_hz // 2
        # Break half into 5 BCD little-endian bytes (byte 0 = lowest 2 digits)
        digits = f"{half:010d}"  # e.g. 12500 -> "0000012500"
        b = []
        for i in range(5):
            lo = int(digits[9-i*2])
            hi = int(digits[8-i*2])
            b.append((hi << 4) | lo)
        hexstr = ' '.join(f'{x:02X}' for x in b)
        resp = self._transact(bytes([0x27, 0x15, 0x00]) + bytes(b), {0xFB, 0xFA}, 0.4)
        matched = self._resp_matched_cmd
        if resp is None:
            self.log(f"[civ] scope span -> {span_hz} Hz (VFO=00, half={half}, bytes: {hexstr}) — NO RESPONSE (timeout)")
            return False
        if matched == 0xFA:
            self.log(f"[civ] scope span -> {span_hz} Hz (VFO=00, half={half}, bytes: {hexstr}) — radio REJECTED (NG)")
            return False
        self._scope_span_hz = span_hz
        self.log(f"[civ] scope span -> {span_hz} Hz (VFO=00, half={half}, bytes: {hexstr}) — OK")
        return True

    async def set_mode(self, mode: str, bw: int = 0, fil: int = 0):
        """
        Set the mode (CI-V 06 <mode_byte> <filter_byte>).
        fil: 1=FIL1, 2=FIL2, 3=FIL3 (mechanical/DSP filters configured
        directly in the radio — we only select WHICH one is active, we
        don't change its width). 0 = don't change (keep self.filter_num,
        or default to FIL1 if unknown).

        NOTE on USB-D/LSB-D: these are NOT separate CI-V mode bytes on the
        IC-7300 — the radio only understands the base mode (USB=1, LSB=0) +
        a separate "DATA mode" command (1A 06 01=ON/00=OFF). An earlier
        version of this function tried to find "USB-D" in mode_rev (which
        only contains the base modes from _BASE_MODE_MAP), didn't find it,
        and SILENTLY sent the default byte (1=USB) WITHOUT enabling DATA
        mode — the effect was the radio staying in plain USB, even though
        the UI showed "USB-D". Now: we recognize the "-D" suffix, send the
        base mode + 1A 06 01, and default to FIL1 (the widest/most commonly
        used DATA filter, matching the configuration already set in the
        radio — we don't change the filter's WIDTH, only which slot is active).
        """
        is_data = mode.endswith("-D")
        base_mode = mode[:-2] if is_data else mode  # "USB-D" -> "USB"

        self.mode = mode  # keep the FULL name (with "-D") for the UI/higher-level logic
        if bw:
            self.bw = bw
        if fil in (1, 2, 3):
            self.filter_num = fil
        elif is_data and not getattr(self, "_data_filter_initialized", False):
            # First entry into DATA mode with no explicit filter given —
            # default to FIL1 (FIL1 already has a width of 3000Hz
            # configured directly in the radio).
            self.filter_num = 1
        fb = getattr(self, "filter_num", 1)

        if self.sim or not self._ser:
            self.data_mode = is_data
            return

        mb = self.mode_rev.get(base_mode, 1)
        await asyncio.to_thread(self._transact,
                                bytes([0x06, mb, fb]), {0xFB, 0xFA}, 0.4)

        # Turn DATA mode on/off with a separate command (1A 06), independent
        # of the base mode — this is the actual USB<->USB-D switch on the IC-7300.
        if is_data != self.data_mode:
            await asyncio.to_thread(self._transact,
                                    bytes([0x1A, 0x06, 0x01 if is_data else 0x00]),
                                    {0x1A}, 0.4, sub=0x06)
            self.data_mode = is_data
            self._data_filter_initialized = True
            self._filter_width_hz = filter_width_hz(self.mode, getattr(self, "_filter_idx", 0), self.data_mode)
            self.bcast({"type": "filter_width", "hz": self._filter_width_hz,
                        "idx": getattr(self, "_filter_idx", 0), "mode": self.mode,
                        "dataMode": self.data_mode})

    async def set_ptt(self, on: bool):
        on = bool(on)
        if on and not self.ptt:
            # New transmission starting — reset ALC/PWR peak-hold so it
            # reflects THIS transmission, not a leftover from the last one.
            self._alc_peak = 0.0
            self._alc_peak_raw = 0
            self._pwr_peak = 0.0
            self._pwr_peak_raw = 0
        self.ptt = on
        if self.sim or not self._ser:
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x1C, 0x00, 0x01 if on else 0x00]), {0xFB, 0xFA}, 0.4)

    # Mapping of characters to CI-V CW message codes (cmd 17, doc p.19-12).
    # Most are standard ASCII (0-9, A-Z, a-z) — sent directly. Additional
    # symbols: / ? . - , : ' ( ) = + " @ space — also ASCII. "^" joins
    # characters with no gap between them (prosody); "FF" aborts sending.
    _CW_ALLOWED = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                      "/?.-,:'()=+\"@ ^")

    async def send_cw_message(self, text: str):
        """
        Send text as CW over CI-V (cmd 17, max 30 characters at a time per
        the IC-7300/IC-746 documentation).

        Conditions required by the radio:
        1. Mode must be CW or CW-R (cmd 06 03 / 06 07)
        2. Break-In must be enabled (cmd 16 47 01) — otherwise the radio
           processes cmd 17 but doesn't enter TX
        3. For IC-746: identical sequence, same commands

        The function automatically:
        - Switches to CW if the current mode isn't CW/CW-R
        - Enables BK-IN if it isn't already enabled
        - Restores the previous mode once sending is finished
        """
        text = "".join(c for c in text.upper() if c in self._CW_ALLOWED)
        if not text:
            return
        if self.sim or not self._ser:
            self.log(f"[civ] send_cw_message (SIM): {text!r}")
            return

        # FIX (reported live 2026-08-24, mid-contest): this used to trust
        # the CACHED self.mode to decide whether a mode switch is needed.
        # self.mode is normally kept fresh by CI-V transceive echoes, but
        # under heavy CI-V load (rapid-fire CW sends back to back, exactly
        # a contest scenario) a manual mode change on the radio's own
        # front panel — or any change whose echo lands while we're mid
        # -transaction on something else — can be missed. Effect observed
        # live: PTT visibly keys (ALC would move for a normal transmit)
        # but NO CW actually goes out, because cmd 17 silently does
        # nothing when the radio isn't really in CW — self.mode said CW,
        # the radio wasn't, and the log showed a totally normal-looking
        # PTT ON/chunk/PTT OFF/done sequence with nothing on the air. A
        # fresh CI-V mode query here (bounded 0.3s) costs a little time
        # but is the only way to be SURE — cheaper than a silently lost
        # exchange.
        real_mode = self.mode
        try:
            bp = await asyncio.to_thread(self._transact, bytes([0x04]), {0x04, 0x01}, 0.3)
            if bp:
                real_mode = self.mode_map.get(bp[0]) or self.mode
        except Exception as e:
            self.log(f"[civ] CW send: mode re-check failed ({e}), trusting cached mode")
        if real_mode != self.mode:
            self.log(f"[civ] CW send: cached mode {self.mode!r} was stale, radio is actually {real_mode!r}")
            self.mode = real_mode

        prev_mode = self.mode

        # Step 1: Switch to CW if needed
        cw_modes = ('CW', 'CW-R')
        if self.mode not in cw_modes:
            self.log(f"[civ] CW send: switching from {self.mode!r} to CW")
            await asyncio.to_thread(
                self._transact, bytes([0x06, 0x03]), {0xFB, 0xFA}, 0.4)
            self.mode = 'CW'
            await asyncio.sleep(0.15)

        # Step 2: PTT ON (1C 00 01) — over USB the radio requires PTT before cmd 17
        # BK-IN over USB causes a continuous carrier — PTT is used instead
        self.log("[civ] CW send: PTT ON")
        await asyncio.to_thread(
            self._transact, bytes([0x1C, 0x00, 0x01]), {0xFB, 0xFA}, 0.4)
        self.ptt = True
        # PTT->keying delay. The IC-7300 over USB needs time to switch to
        # transmit (T/R relay, possibly the tuner). With too short a delay,
        # the first character goes out before the radio is fully
        # transmitting, and the start gets clipped (observed: 'XX0XXX' ->
        # dropped 'S'/'SQ'). 50ms was too little; 250ms gives margin.
        # Configurable via cwPttDelay in settings (ms), since tuners need more.
        _ptt_delay = float(self.profile.get("cwPttDelay", 250)) / 1000.0
        await asyncio.sleep(max(0.05, _ptt_delay))

        # Step 3: Send the text via CI-V cmd 17
        # The radio modulates CW while PTT is active
        for i in range(0, len(text), 30):
            chunk = text[i:i+30]
            self.log(f"[civ] CW send chunk: {chunk!r}")
            await asyncio.to_thread(
                self._write, bytes([0x17]) + chunk.encode("ascii"))
            if i + 30 < len(text):
                await asyncio.sleep(0.1)

        # Step 4: Read the current WPM from the radio and wait
        try:
            lvl = self.profile.get("levels", {}).get("KEYSPD")
            if lvl:
                fresh_wpm = await asyncio.to_thread(self._read_level, "KEYSPD", lvl)
                if fresh_wpm is not None:
                    self.level_values["KEYSPD"] = fresh_wpm
        except Exception:
            pass
        wpm = int(self.level_values.get("KEYSPD", 18) or 18)
        # Exact CW duration instead of the old flat "len*10 units *1.5" guess.
        # The old formula assumed every character is 10 units and then padded
        # 50%, so PTT hung long after the keyer finished (audible carrier after
        # the last character) and, for text with long characters, could even cut
        # the macro short. We now sum the real Morse timing (dot=1, dash=3,
        # intra-char gap=1, inter-char gap=3, word gap=7 units) so the hold
        # matches what the rig's keyer actually sends.
        wait_s = self._cw_text_duration_s(text, wpm) + 0.15
        wait_s = max(0.3, min(wait_s, 120.0))
        self.log(f"[civ] CW send: waiting {wait_s:.1f}s ({wpm} WPM)")
        await asyncio.sleep(wait_s)

        # Step 5: PTT OFF
        self.log("[civ] CW send: PTT OFF")
        await asyncio.to_thread(
            self._transact, bytes([0x1C, 0x00, 0x00]), {0xFB, 0xFA}, 0.4)
        self.ptt = False
        # T/R recovery: after PTT OFF the rig needs a moment to leave transmit
        # (T/R relay, internal sequencing) before it can cleanly accept a new CW
        # buffer. Without this, a macro fired immediately after the previous one
        # finished would push its cmd-17 text into the rig mid-transition and the
        # rig would drop it — carrier keys up but nothing is sent (empty carrier
        # with a valid chunk in the log). Configurable via cwTrRecovery (ms).
        _tr_recovery = float(self.profile.get("cwTrRecovery", 200)) / 1000.0
        await asyncio.sleep(max(0.05, _tr_recovery))
        self.log("[civ] CW send: done")

    async def stop_cw_message(self):
        """Abort CW message sending (cmd 17 FF = clear buffer)."""
        if self.sim or not self._ser:
            return
        # Clear the CW buffer — the radio drops TX on its own once it gets FF
        await asyncio.to_thread(self._write, bytes([0x17, 0xFF]))
        self.log("[civ] CW stop: buffer cleared")

    # ── DTR/RTS Keyer ─────────────────────────────────────────────────────────

    # Morse table — character -> string of '.' and '-'
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
        """Exact time to send `text` in CW at `wpm`, in seconds.

        Uses standard Morse timing (PARIS): dit = 1 unit, dah = 3, gap between
        elements = 1, gap between letters = 3, gap between words = 7. Summing
        the real element counts (instead of assuming 10 units/char) makes the
        PTT hold match the keyer, so the carrier drops right after the last
        element and long macros are never cut short.
        """
        wpm = max(5, min(60, wpm))
        dit = 1.200 / wpm
        units = 0.0
        first_char = True
        for ch in text.upper():
            if ch == ' ':
                units += 7.0          # word gap
                first_char = True
                continue
            code = self._MORSE_TABLE.get(ch)
            if not code:
                continue
            if not first_char:
                units += 3.0          # gap before this letter
            first_char = False
            for i, sym in enumerate(code):
                units += 3.0 if sym == '-' else 1.0   # dah / dit
                if i < len(code) - 1:
                    units += 1.0      # intra-character gap
        return units * dit

    def configure_keyer(self, port: str, line: str):
        """
        Configure the DTR/RTS keyer.
        port: 'COM3', 'COM5' etc. (may be the same as CI-V or a separate one)
        line: 'DTR' or 'RTS'
        If port == '' (empty) — use the same port as CI-V (_ser).
        """
        import serial as _serial
        self._keyer_line = line.upper() if line else 'DTR'
        if port and port != getattr(self, '_port', ''):
            # Separate port for the keyer
            try:
                if self._keyer_ser and self._keyer_ser is not self._ser:
                    try: self._keyer_ser.close()
                    except: pass
                self._keyer_ser = _serial.Serial(port, baudrate=9600, timeout=0.1)
                # Zero DTR/RTS right away - otherwise opening the port throws a KEY DOWN
                try:
                    self._keyer_ser.dtr = False
                    self._keyer_ser.rts = False
                except Exception:
                    pass
                self._keyer_port = port
                self.log(f"[keyer] Opened port {port} for {line} keying")
            except Exception as e:
                self.log(f"[keyer] Error opening port {port}: {e}")
                self._keyer_ser = None
        else:
            # Use the same port as CI-V
            self._keyer_ser = self._ser
            self._keyer_port = ''

    def _set_key(self, state: bool):
        """Set the DTR or RTS line (True=KEY DOWN, False=KEY UP)."""
        ser = self._keyer_ser or self._ser
        if not ser:
            return
        try:
            if self._keyer_line == 'RTS':
                ser.rts = state
            else:
                ser.dtr = state
        except Exception as e:
            self.log(f"[keyer] _set_key error: {e}")

    def _send_morse_blocking(self, text: str, wpm: int, stop_event):
        """
        Synchronous CW sending via DTR/RTS (called in a separate thread).
        dit_ms = 1200 / wpm  (standard Morse timing, PARIS word = 50 units).
        stop_event: threading.Event — aborts sending immediately.
        """
        import time as _time
        wpm = max(5, min(60, wpm))
        dit = 1.200 / wpm   # dit duration in seconds

        # Delay at the start — the radio needs a moment to switch to
        # transmit (T/R relay, tuner). Without this, the first dit goes out
        # before the radio is transmitting, and the start of the character
        # gets clipped (dropped 'S'/'SQ' at the operator's start).
        # Configurable via cwPttDelay (ms).
        _ptt_delay = float(self.profile.get("cwPttDelay", 250)) / 1000.0
        _time.sleep(max(0.05, _ptt_delay))

        def key_down(t):
            self._set_key(True)
            _time.sleep(t)
            self._set_key(False)

        def pause(t):
            _time.sleep(t)

        for char in text.upper():
            if stop_event.is_set():
                break
            if char == ' ':
                pause(dit * 7)  # word gap
                continue
            code = self._MORSE_TABLE.get(char)
            if not code:
                continue
            for i, sym in enumerate(code):
                if stop_event.is_set():
                    break
                if sym == '.':
                    key_down(dit)
                else:
                    key_down(dit * 3)
                if i < len(code) - 1:
                    pause(dit)   # gap between elements
            pause(dit * 3)   # gap between letters
        self._set_key(False)   # make sure the key is released

    async def send_cw_dtr_rts(self, text: str, wpm: int):
        """
        Send CW text via DTR/RTS keying (asynchronously, in a separate thread).
        Aborts a previous send if one is active.
        """
        # Stop a previous send
        self._keyer_stop.set()
        if self._keyer_th and self._keyer_th.is_alive():
            self._keyer_th.join(timeout=0.5)
        self._keyer_stop.clear()

        if self.sim:
            self.log(f"[keyer] SIM DTR/RTS: {text!r} @ {wpm} WPM")
            return

        stop_ev = self._keyer_stop
        th = threading.Thread(
            target=self._send_morse_blocking,
            args=(text, wpm, stop_ev),
            daemon=True, name="morse-keyer"
        )
        self._keyer_th = th
        th.start()

    async def stop_cw_dtr_rts(self):
        """Abort DTR/RTS sending immediately."""
        self._keyer_stop.set()
        self._set_key(False)

    async def set_split(self, on: bool):
        self.split = bool(on)
        if self.sim or not self._ser:
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x0F, 0x01 if on else 0x00]), {0xFB, 0xFA}, 0.4)

    async def set_power(self, on: bool):
        """
        Turn the radio on/off (CI-V cmd 0x18 0x01/0x00).

        IMPORTANT — power ON requires a "wakeup": when the radio is fully
        off, the UART isn't listening at full speed. Standard Icom
        sequence: send a long preamble of 0xFE bytes (~150ms at the given
        baud rate) BEFORE the 0x18 0x01 frame — this "wakes up" the
        radio's UART receiver.

        Requires in the radio: MENU > SET > Connectors > CI-V > "CI-V
        Transceive" = ON, and 12V power applied to the radio (USB alone
        isn't enough when the radio is in full OFF mode on some models).

        After "power OFF" the CI-V link stops responding to normal
        transactions (the radio isn't listening) — this is expected until
        we send wakeup+power ON.
        """
        if self.sim or not self._ser:
            return

        # Remember the power state — after power OFF the radio doesn't
        # respond to any CI-V transactions. Without this, the telemetry
        # loop keeps polling the S-meter and prints "S-metr transact fail"
        # every second, spamming the log.
        self.powered = bool(on)

        if on:
            await asyncio.to_thread(self._wakeup_and_power_on)
        else:
            await asyncio.to_thread(self._transact,
                                    bytes([0x18, 0x00]), {0xFB, 0xFA}, 0.6)

    def _wakeup_and_power_on(self):
        """
        Sends the 0xFE preamble (wakeup) + the 0x18 0x01 frame (power ON).
        Preamble duration depends on baud rate: at 115200bd Icom recommends
        at least ~150ms of continuous 0xFE transmission (about 150 bytes).
        After power-on the radio needs a few seconds to boot — we don't
        wait for an ACK (the transaction usually times out, since the
        radio isn't ready yet).
        """
        try:
            preamble_len = max(150, self.speed // 768)  # ~150ms of data
            preamble = b"\xfe" * preamble_len
            frame = bytes([0xFE, 0xFE, self.addr, CTRL_ADDR, 0x18, 0x01, 0xFD])
            with self._wlock:
                if self._ser:
                    self._ser.write(preamble)
                    time.sleep(0.05)
                    self._ser.write(frame)
                    self._write_fail_count = 0
            self.log(f"[civ] wakeup+power ON sent (preamble {preamble_len}B)")
        except Exception as e:
            self.log(f"[civ] wakeup_and_power_on error: {e}")
            # Same dead-port counter as _write() - this is the exact path
            # that failed to recover live during a simulated power-loss
            # test (radio came back, but the COM handle didn't) - without
            # this, a stuck port here just logs forever and never triggers
            # the close+reconnect self-heal.
            self._write_fail_count = getattr(self, "_write_fail_count", 0) + 1
            if self._write_fail_count >= 3:
                self._handle_dead_port()

    async def set_vfo(self, vfo: str):
        """
        Switch the active VFO — CI-V cmd 0x07.
        vfo: 'VFOA' -> 0x00, 'VFOB' -> 0x01
        """
        _name = "VFOA" if vfo.upper() in ("VFOA", "A") else "VFOB"
        self.vfo = _name  # track the state (also in sim — the UI needs to know the truth)
        if self.sim or not self._ser:
            return
        sub = 0x00 if _name == "VFOA" else 0x01
        await asyncio.to_thread(self._transact,
                                bytes([0x07, sub]), {0xFB, 0xFA}, 0.4)

    async def vfo_equalize(self):
        """A->B: copy the frequency/mode from VFO A to VFO B (CI-V 07 A0)."""
        if self.sim or not self._ser:
            self.freq_b = self.freq
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x07, 0xA0]), {0xFB, 0xFA}, 0.4)

    async def vfo_swap(self):
        """A<->B: swap VFO A and B (CI-V 07 B0)."""
        if self.sim or not self._ser:
            self.freq, self.freq_b = self.freq_b, self.freq
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x07, 0xB0]), {0xFB, 0xFA}, 0.4)

    async def set_func(self, func: str, on: bool):
        """
        Turn a radio function on/off — CI-V cmd 0x16 <sub> <00|01>.
        The name->subcommand mapping is in the model profile (profile['funcs']).
        """
        if self.sim or not self._ser:
            return
        funcs = self.profile.get("funcs", {})
        sub = funcs.get(func.upper())
        if sub is None:
            self.log(f"[civ] set_func: unknown function '{func}' for {self.profile.get('name')}")
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x16, sub, 0x01 if on else 0x00]), {0xFB, 0xFA}, 0.4)

    async def set_preamp(self, level: int):
        """
        Preamp — CI-V 16 02 <00|01|02>.
        level: 0=OFF, 1=Preamp1 (P.AMP1), 2=Preamp2 (P.AMP2, IC-7300/7610 etc. only)
        """
        level = max(0, min(2, int(level)))
        self.preamp = level
        if self.sim or not self._ser:
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x16, 0x02, level]), {0xFB, 0xFA}, 0.4)

    async def set_attenuator(self, on: bool):
        """
        Attenuator — CI-V 11 <00|20>.
        The IC-7300 has one 20dB attenuator stage: 00=OFF, 20=ON (20dB).
        """
        on = bool(on)
        self.attenuator = on
        if self.sim or not self._ser:
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x11, 0x20 if on else 0x00]), {0xFB, 0xFA}, 0.4)

    async def set_tuner(self, on: bool):
        """
        Antenna Tuner ON/OFF — CI-V 1C 01 <00|01>.
        00=OFF (tuner bypassed), 01=ON (tuner switched into the signal
        path — required before AUTOTUNE).
        """
        on = bool(on)
        self.tuner = on
        if self.sim or not self._ser:
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x1C, 0x01, 0x01 if on else 0x00]), {0xFB, 0xFA}, 0.4)

    async def start_tuner_autotune(self):
        """
        Antenna Tuner START (auto-tuning cycle) — CI-V 1C 01 02.
        The radio starts searching for a match (a few seconds, generates a
        low-power TX signal). The tuner must be ON (01) before calling this
        — in practice the IC-7300 turns the tuner on automatically together
        with START, but we set it ON right before, just to be safe.
        """
        self.tuner = True
        if self.sim or not self._ser:
            return
        # Step 1: Make sure the tuner is ON
        await asyncio.to_thread(self._transact,
                                bytes([0x1C, 0x01, 0x01]), {0xFB, 0xFA}, 0.5)
        # Step 2: Short delay — the radio needs a moment to switch over
        await asyncio.sleep(0.1)
        # Step 3: START autotune — the radio generates a TX signal and tunes the ATU
        # 2.0s timeout since the radio may not respond right away (busy tuning)
        await asyncio.to_thread(self._transact,
                                bytes([0x1C, 0x01, 0x02]), {0xFB, 0xFA}, 2.0)

    async def set_level(self, level_name: str, value: float):
        """
        Set a level (e.g. RFPOWER, AF, MICGAIN) — CI-V cmd 0x14 <sub> <BCD 0000-0255>.
        'value' is in UI units (the level['min']..level['max'] range from
        the profile) and is linearly scaled to 0-255 for CI-V.
        The name->subcommand+range mapping is in the model profile (profile['levels']).
        """
        levels = self.profile.get("levels", {})
        lvl = levels.get(level_name.upper())
        if lvl is None:
            self.log(f"[civ] set_level: unknown level '{level_name}' for {self.profile.get('name')}")
            return

        lo, hi = lvl["min"], lvl["max"]
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        # Scale value (lo..hi) -> civ_val (0..civ_max)
        civ_max = lvl.get("civ_max", 255)
        if hi != lo:
            frac = (value - lo) / (hi - lo)
        else:
            frac = 0.0
        frac = max(0.0, min(1.0, frac))
        civ_val = int(round(frac * civ_max))

        if self.sim or not self._ser:
            return

        # CI-V 0x14: subcommand + 2-byte BCD (0000-0255)
        bcd = self._int_to_bcd2(civ_val)

        # Update the local cache right away (optimistically) — a read from
        # the radio will happen on the next _read_all_levels()/poller cycle
        # and may correct it, but this lets the UI/other clients see the
        # new value immediately after the WS broadcast (if webapp.py does one).
        self.level_values[level_name.upper()] = value

        if self.sim or not self._ser:
            return

        await asyncio.to_thread(self._transact,
                                bytes([0x14, lvl["sub"]]) + bcd, {0xFB, 0xFA}, 0.4)

    def _read_level(self, level_name: str, lvl: dict) -> float | None:
        """
        Read the current Set Level slider value (CI-V 14 <sub>) from the
        radio and rescale the BCD (0..civ_max) to UI units
        (lvl['min']..lvl['max']). Returns None if the radio didn't
        respond. Called synchronously (in the connection/poller thread,
        not in the asyncio loop).
        """
        p = self._transact(bytes([0x14, lvl["sub"]]), {0x14}, 0.3, sub=lvl["sub"])
        if not p or len(p) < 3 or p[0] != lvl["sub"]:
            return None
        civ_val = bcd2(p[1:3])
        civ_max = lvl.get("civ_max", 255)
        lo, hi = lvl["min"], lvl["max"]
        frac = civ_val / civ_max if civ_max else 0.0
        frac = max(0.0, min(1.0, frac))
        return lo + frac * (hi - lo)

    def _read_all_levels(self):
        """
        Read the current values of ALL Set Level sliders defined in the
        radio profile and store them in self.level_values. Called once
        after connecting (_open) — so the UI starts from the radio's
        actual settings, not from 0/min.

        NOTE: NB_LEVEL and BKINDL have different subcommands (0x12 vs 0x0F
        after the fix), so there's no read collision between them anymore.
        """
        levels = self.profile.get("levels", {})
        for name, lvl in levels.items():
            try:
                val = self._read_level(name, lvl)
                if val is not None:
                    self.level_values[name] = val
            except Exception as e:
                self.log(f"[civ] _read_all_levels: error reading {name}: {e}")

    @staticmethod
    def _int_to_bcd2(v: int) -> bytes:
        """
        0-255 (or up to 999) -> 2-byte BCD like Icom CI-V Set Level
        (e.g. 0255 -> bytes([0x02, 0x55]), per the documentation:
        "00 00=min. to 02 55=max."). Inverse of bcd2().

        FIX: the previous version returned bytes([(d1<<4)|d0, d2]) which
        for 255 gave [0x55, 0x02] — bytes swapped relative to bcd2() and
        the CI-V spec. After this fix, bcd2(_int_to_bcd2(v)) == v for v in 0..255.
        """
        v = max(0, min(255, int(v)))
        hi = v // 100          # 0-2
        lo = v % 100           # 0-99
        byte0 = hi              # 0x00, 0x01, or 0x02 (BCD == binary for 0-2)
        byte1 = ((lo // 10) << 4) | (lo % 10)
        return bytes([byte0, byte1])

    async def get_capabilities(self) -> dict:
        """
        Return the full structure of discovered CI-V capabilities:
        {"actions": [VFO A/B, func toggles], "sliders": [Set Level],
         "raw_caps": {feature_id: bool}}

        Builds the list from the model profile (profile['levels']/['funcs'])
        — analogous to hamlib_caps.discover_capabilities, but the source is
        the static CI-V profile (the radio isn't queried for its
        capabilities, since CI-V has no "dump_caps" command).
        """
        raw_caps = dict(self.profile.get("capabilities", {}))

        actions = []
        if raw_caps.get("vfo_ab"):
            actions.append({"id": "vfo_a", "label": "VFO A", "group": "vfo",
                             "kind": "vfo_select", "value": "VFOA"})
            actions.append({"id": "vfo_b", "label": "VFO B", "group": "vfo",
                             "kind": "vfo_select", "value": "VFOB"})

        if raw_caps.get("power"):
            # Universal label (PL/EN) instead of a Polish description — this
            # button renders the same in both frontend languages, the color
            # (green/red) shows the actual ON/OFF state.
            actions.append({"id": "power_toggle", "label": "PWR ON/OFF", "group": "tx",
                             "kind": "power_toggle", "value": "POWER"})

        func_labels = self.profile.get("func_labels", {})
        for func_name in self.profile.get("funcs", {}):
            label = func_labels.get(func_name, func_name)
            actions.append({
                "id": f"func_{func_name.lower()}", "label": label, "group": "func",
                "kind": "func_toggle", "value": func_name,
            })

        sliders = []
        for level_name, lvl in self.profile.get("levels", {}).items():
            # Default value = lvl['min'] if not read from the radio yet
            # (e.g. simulation mode) — otherwise the real setting from
            # self.level_values (filled in by _read_all_levels() after
            # connecting, see _open()).
            current = self.level_values.get(level_name, lvl["min"])
            # Slider step: for 0.0-1.0 ranges (scaled to 0-100% in the UI)
            # we compute a step proportional to the 255 CI-V levels. For
            # integer ranges (e.g. KEYSPD 6-48 WPM, CWPITCH 300-900Hz) the
            # step is 1 UI unit — more sensible than 0.16 WPM.
            if lvl["max"] <= 1.0:
                step = (lvl["max"] - lvl["min"]) / 255.0 if lvl["max"] != lvl["min"] else 1
            else:
                step = 1
            sliders.append({
                "id": f"level_{level_name.lower()}", "label": lvl.get("label", level_name),
                "group": "level", "kind": "set_level", "param": level_name,
                "min": lvl["min"], "max": lvl["max"], "value": current,
                "step": step,
            })

        return {"actions": actions, "sliders": sliders, "raw_caps": raw_caps}

    def close(self):
        self._running = False
        self.scope_running = False
        try:
            if self._ser:
                # turn off the scope stream, so the radio doesn't flood the port after disconnecting
                try:
                    self._write(bytes([0x27, 0x11, 0x00]))
                    time.sleep(0.05)
                except Exception:
                    pass
                self._ser.close()
        except Exception:
            pass
        self._ser = None

    # ── scope control (from the /api/scope/start|stop endpoint) ──
    def scope_start(self):
        if self.sim or not self._ser:
            self.scope_running = True
            return {"ok": True, "sim": True, "running": True}
        self._enable_scope(True)
        self.scope_running = True
        return {"ok": True, "sim": False, "running": True}

    def scope_stop(self):
        self.scope_running = False
        if self._ser:
            try:
                self._enable_scope(False)
            except Exception:
                pass
        return {"ok": True, "running": False}

    # ════════════════════════════════════════════════════════════════════════
    # Serial layer (threads)
    # ════════════════════════════════════════════════════════════════════════
    def _parse_civ(self, civ) -> int:
        try:
            s = str(civ).strip().lower().replace("0x", "").replace("h", "")
            return int(s, 16)
        except Exception:
            return self.profile.get("default_addr", 0x94)

    def _open(self) -> bool:
        if not HAS_SERIAL:
            self.last_err = ("Brak biblioteki pyserial — tryb bezposredni CI-V wymaga pyserial. "
                             "Uruchamiam SYMULACJE scope.")
            self.log(f"[civ] {self.last_err}")
            return self._start_sim()
        try:
            self._ser = serial.Serial(self.port, self.speed, timeout=0.05, write_timeout=1.0)
        except Exception as e:
            self.last_err = (f"Nie moge otworzyc {self.port} @ {self.speed}bd: {type(e).__name__}: {e}. "
                             f"Sprawdz: wlasciwy COM, baud CI-V w radiu "
                             f"(domyslnie {self.profile['default_baud']} dla {self.profile.get('name','tego modelu')}), "
                             f"port wolny (zamknij rigctld/N1MM/RCForb). Uruchamiam SYMULACJE scope.")
            self.log(f"[civ] {self.last_err}")
            return self._start_sim()

        # CRITICAL: pyserial sets DTR=True and RTS=True by default when
        # opening a port. If the radio has DTR/RTS configured as PTT/CW-key
        # (typical for the IC-7300), the radio IMMEDIATELY goes into TX. We
        # force a low state right after opening - BEFORE any communication.
        # The keyer will raise the line itself when it's actually keying
        # (KEY DOWN).
        try:
            self._ser.dtr = False
            self._ser.rts = False
        except Exception as _e:
            self.log(f"[civ] Warning: can't zero out DTR/RTS: {_e}")

        self.sim = False
        self._running = True
        self._reader_th = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_th.start()

        # initial frequency / mode read
        ok_freq = False
        for _ in range(3):
            p = self._transact(bytes([0x03]), {0x03, 0x00}, 0.6)
            if p:
                f = bcd_to_freq(p)
                if f:
                    self.freq = f; ok_freq = True; break
            time.sleep(0.4)

        if not ok_freq:
            # The radio may be physically OFF — the USB-CI-V port exists
            # (it's powered from USB) but the radio's UART is "asleep" and
            # doesn't respond to normal queries. Send wakeup+power-on
            # (0xFE preamble + 0x18 0x01, see IC-7300 manual *3) and retry
            # after giving the radio time to boot (~2-3s).
            self.log("[civ] 0x03 no response — trying wakeup+power ON (radio may be OFF)")
            self._wakeup_and_power_on()
            time.sleep(2.5)
            for _ in range(4):
                p = self._transact(bytes([0x03]), {0x03, 0x00}, 0.6)
                if p:
                    f = bcd_to_freq(p)
                    if f:
                        self.freq = f; ok_freq = True
                        self.log("[civ] Radio woken up via wakeup+power ON")
                        break
                time.sleep(0.5)

        pm = self._transact(bytes([0x04]), {0x04, 0x01}, 0.6)
        if pm and len(pm) >= 1:
            base_pm = self.mode_map.get(pm[0])
            if base_pm:
                self.mode = f"{base_pm}-D" if self.data_mode else base_pm

        if not ok_freq:
            self.last_err = (f"Radio nie odpowiada na CI-V (0x03), nawet po wakeup+power ON. "
                             f"Sprawdz: model, CI-V address (domyslnie 0x{self.profile['default_addr']:02X} "
                             f"dla {self.profile.get('name','tego modelu')}), "
                             f"baud, kabel/COM, zasilanie 12V radia. Uruchamiam SYMULACJE scope.")
            self.log(f"[civ] {self.last_err}")
            self._running = False
            try: self._ser.close()
            except Exception: pass
            self._ser = None
            return self._start_sim()

        self.connected = True
        self.last_err = ""
        self.last_msg = (f"{getattr(self,'_rig_name','Radio')} (CI-V bezposredni) {self.port} "
                         f"{self.speed}bd — {self.freq}Hz {self.mode}")
        self.log(f"[civ] CONNECTED: {self.last_msg}")

        # turn on the scope stream + start the telemetry poller
        self._enable_scope(True)
        self.scope_running = True

        # Read the current settings of all sliders (Set Level, CI-V 14
        # <sub>) from the radio BEFORE announcing the connection — this way
        # get_capabilities() (called by _on_rig_reconnected in webapp.py)
        # returns real values and the UI sliders start from the radio's
        # actual settings, not from 0.
        self._read_all_levels()

        # Read frequency and mode once before starting the poller —
        # without this, self.freq holds its hardcoded default (14074000)
        # until the first _poller_loop cycle (a few seconds). Clients
        # connecting in that window get the wrong frequency in the 'init'
        # message and the VFO display shows e.g. 14.074 instead of the
        # radio's actual frequency. Same pattern as _read_all_levels() above.
        try:
            # freq: command 03, response 03 or 00
            bp = self._transact(bytes([0x03]), {0x03, 0x00}, 0.4)
            if bp:
                f = bcd_to_freq(bp[:5])
                if f and f > 0:
                    self.freq = f
                    self.log(f"[civ] startup read freq={f}Hz")
            # mode: command 04, response 04 or 01
            bm = self._transact(bytes([0x04]), {0x04, 0x01}, 0.3)
            if bm and len(bm) >= 1:
                mode_byte = bm[0]
                MODE_MAP = {0x00:'LSB',0x01:'USB',0x02:'AM',0x03:'CW',
                            0x04:'RTTY',0x05:'FM',0x06:'CW-R',0x07:'RTTY-R',
                            0x08:'LSB-D',0x09:'USB-D',0x11:'PKTUSB',0x12:'PKTLSB'}
                if mode_byte in MODE_MAP:
                    self.mode = MODE_MAP[mode_byte]
                    self.log(f"[civ] startup read mode={self.mode}")
        except Exception as e:
            self.log(f"[civ] startup freq/mode read error (non-blocking): {e}")

        # Safety: disable BK-IN at startup so the radio doesn't go into
        # continuous TX. BK-IN (CI-V 16 47 00) may be left on from a
        # previous session or by mistake. The user can enable it manually
        # in the panel.
        try:
            self._transact(bytes([0x16, 0x47, 0x00]), {0xFB, 0xFA}, 0.4)
            self.log("[civ] BK-IN disabled at startup (safety)")
        except Exception as e:
            self.log(f"[civ] BK-IN disable error (non-blocking): {e}")

        self._poller_th = threading.Thread(target=self._poller_loop, daemon=True)
        self._poller_th.start()

        # Notify webapp.py (via WS broadcast) that a connection has been
        # (re-)established — the receiver should refresh _caps_cache and
        # send a fresh /api/rig/features (rig_features) to clients, since
        # the VFO/PWR/sliders panel could be empty if the connection
        # happened in the background (e.g. via _reconnect_loop after the
        # radio was turned on after the server started).
        try:
            self.bcast({"type": "rig_reconnected"})
        except Exception as e:
            self.log(f"[civ] bcast rig_reconnected error: {e}")

        return True

    def _enable_scope(self, on: bool):
        # DIAGNOSTIC TEST: HAM_NO_SCOPE=1 forces scope OFF, to check whether
        # it's the scope stream causing loop stalls / RTT spikes / audio
        # tearing. If audio is smooth and RTT stable with the scope off —
        # the scope is the culprit (on 2 cores: the reader thread competing
        # for CPU). Remove the variable to return to normal operation.
        import os as _os
        if _os.environ.get("HAM_NO_SCOPE") == "1":
            on = False
        """
        Turn the CI-V scope waveform data stream on/off.

        When turning it on, we set Center mode (27 14 00 00) — in this
        mode the radio sends center freq + span in the header and the
        scope window "follows" the current VFO (unlike Fixed mode, where
        the window is a fixed band-edge range and does NOT track the
        VFO). This achieves the "like the original scope screen" behavior
        that's expected.

        We also set the span to 25kHz (via set_scope_span) — gives a good
        compromise between preview width and filter-tracking precision.
        """
        try:
            # Baud 115200 is REQUIRED for the scope. At 19200 (CI-V USB =
            # "Link to REMOTE") the radio doesn't send waveform data - the
            # most common cause of "no waterfall".
            if on and self.speed < 115200:
                self.log(f"[civ] WARNING baud={self.speed} — scope requires 115200! "
                         f"Set MENU>SET>Connectors>CI-V baud=115200 and "
                         f"CI-V USB Port=Unlink from REMOTE")
            self._write(bytes([0x27, 0x10, 0x01 if on else 0x00]))  # scope ON/OFF
            time.sleep(0.05)
            self._write(bytes([0x27, 0x11, 0x01 if on else 0x00]))  # data output ON/OFF
            if on:
                time.sleep(0.05)
                # Center mode (00) — the scope tracks the VFO
                self._write(bytes([0x27, 0x14, 0x00, 0x00]))
                time.sleep(0.05)
                # 20kHz span (closest real value to the old, nonexistent
                # 25kHz default - see SCOPE_SPAN_KHZ) - via set_scope_span
                # (was a hand-rolled write here with the same wrong byte
                # layout set_scope_span used to have before it got fixed:
                # missing the VFO-select byte and encoding the full span
                # instead of span/2. Reusing the now-correct, ACK-checked
                # method instead of duplicating the fix in two places.)
                self.set_scope_span(20000)
            self.log(f"[civ] scope output {'ON' if on else 'OFF'} (baud={self.speed})"
                     + (" + Center mode 20kHz" if on else ""))
        except Exception as e:
            self.log(f"[civ] _enable_scope error: {e}")

    def _write(self, payload: bytes):
        frame = bytes([0xFE, 0xFE, self.addr, CTRL_ADDR]) + payload + bytes([0xFD])
        # NOTE (perf): removed the per-write log — polling calls write
        # every ~100ms (poll freq/smeter/scope), and every print is a
        # blocking syscall (~0.5-2ms). Under load the logs piled up and
        # blocked the reader/writer thread. To enable for debugging:
        # uncomment the line below.
        # self.log(f"[civ] TX: {frame.hex()}")
        with self._wlock:
            if self._ser:
                try:
                    self._ser.write(frame)
                    self._write_fail_count = 0
                except Exception:
                    # SELF-HEAL — live-tested simulated power loss: after the
                    # radio's USB power drops and comes back, Windows leaves
                    # the COM handle "stuck" (PermissionError/ERROR_BAD_COMMAND,
                    # "Urzadzenie nie rozpoznaje polecenia" on every write,
                    # while reads keep timing out silently instead of raising
                    # — nothing ever noticed the port was dead). The only
                    # existing recovery path was webapp.py's _device_watchdog,
                    # which only checks once an HOUR — the radio stayed
                    # unreachable that whole time. A few consecutive write
                    # failures in a row means the port itself is dead (not
                    # one bad command), so close it and hand off to
                    # _start_sim()/_reconnect_loop (the SAME recovery path
                    # the watchdog uses) immediately instead of waiting.
                    self._write_fail_count = getattr(self, "_write_fail_count", 0) + 1
                    if self._write_fail_count >= 3:
                        self._handle_dead_port()
                    raise

    def _handle_dead_port(self):
        self._write_fail_count = 0
        self.connected = False
        self.log("[civ] port unresponsive after repeated write errors — "
                  "closing and reconnecting")
        try:
            if self._ser:
                self._ser.close()
        except Exception:
            pass
        self._ser = None
        self._start_sim()

    def write_bytes_raw(self, data: bytes):
        """Write raw bytes to the CI-V port without the FE FE addr ctrl ... FD
        wrapper. Used by civ_bridge for TCP clients that send whole CI-V
        frames themselves (Logger32, HRD, etc.). Bytes are passed through
        exactly as received. Protected by self._wlock to avoid colliding
        with commands from the server's own UI.
        """
        if self.sim or not self._ser:
            return
        with self._wlock:
            try:
                self._ser.write(data)
                self._write_fail_count = 0
            except Exception as e:
                self.log(f"[civ] write_bytes_raw error: {e}")
                # Same dead-port counter as _write() - a bridge client
                # (Logger32/HRD) polling over TCP would otherwise hit this
                # every time too and never recover on its own either.
                self._write_fail_count = getattr(self, "_write_fail_count", 0) + 1
                if self._write_fail_count >= 3:
                    self._handle_dead_port()

    def add_bridge_listener(self, callback):
        """Register a callback called for every chunk of bytes from the radio.
        Callback: fn(bytes). Used by civ_bridge to broadcast to TCP.
        Note: the callback is called from the _reader_loop thread, it must be thread-safe."""
        with self._bridge_lock:
            if callback not in self._bridge_listeners:
                self._bridge_listeners.append(callback)

    def remove_bridge_listener(self, callback):
        with self._bridge_lock:
            if callback in self._bridge_listeners:
                self._bridge_listeners.remove(callback)

    def _transact(self, payload: bytes, expect, timeout: float, sub: int = None):
        """Send a command and wait for a response (a cmd code in 'expect' or ACK 0xFB/0xFA).

        sub: FIX — the whole "15 xx" meter family (S-meter=02, ALC=13,
        PWR/PO=11, SWR=12, VOLT=15) replies with the SAME top-level cmd
        0x15, differentiated only by the first payload byte. The poller
        fires these back-to-back (all 5 during active PTT), each with its
        own short 0.25s timeout. Without 'sub', the dispatch in
        _handle_frame matched on cmd alone — so a LATE reply to one 15-xx
        query (e.g. ALC, delayed under heavy CI-V+audio load) could get
        consumed by whichever 15-xx transact happened to be waiting at
        that moment. The "stolen" transact got a payload with the wrong
        first byte (its own caller-side check, e.g. pp[0]==0x11, catches
        THAT and discards it) — but the reply that should have satisfied
        it is now gone too, so it also times out. Net effect live: ALC and
        PWR readings went stale/missing independently of each other,
        making them look completely uncorrelated when compared side by
        side (reported: "ALC 58%/PWR 0%" back to back with "ALC 3%/PWR
        40%" — each half was a stale leftover from a DIFFERENT instant,
        not a real simultaneous pair). When 'sub' is given, only a frame
        whose payload[0] == sub can satisfy this wait; a mismatched 15-xx
        frame is left for whichever OTHER transact is actually waiting for it.
        """
        if not self._ser:
            return None
        if isinstance(expect, int):
            expect = {expect}
        with self._txlock:
            self._resp_cmd = expect
            self._resp_sub = sub
            self._resp_payload = None
            self._resp_matched_cmd = None
            self._resp_ev.clear()
            try:
                self._write(payload)
            except Exception as e:
                self.log(f"[civ] write error: {e}")
                self._resp_cmd = None
                self._resp_sub = None
                return None
            got = self._resp_ev.wait(timeout)
            self._resp_cmd = None
            self._resp_sub = None
            # self._resp_matched_cmd is left set (not cleared here) so a
            # caller that cares which of 'expect' actually matched (e.g.
            # 0xFB ok vs 0xFA reject) can read it right after this call
            # returns - see set_scope_span for why this matters (this was
            # the ONLY CI-V "set" command in the file that never checked
            # for a response at all before this fix, so a rejected span
            # value looked identical to a silently-ignored one).
            return self._resp_payload if got else None

    def _reader_loop(self):
        buf = bytearray()
        while self._running:
            try:
                data = self._ser.read(512)
            except Exception as e:
                self.log(f"[civ] reader read error: {e}")
                break
            if not data:
                continue
            if self._rx_logged < 10:
                self.log(f"[civ] RX: {data.hex()}")
                self._rx_logged += 1
            # Bridge: forward raw bytes to listeners (civ_bridge TCP clients).
            # This is done BEFORE parsing so TCP clients get exactly what
            # our own parsing logic gets (same events, same order).
            if self._bridge_listeners:
                with self._bridge_lock:
                    listeners = list(self._bridge_listeners)
                for cb in listeners:
                    try:
                        cb(bytes(data))
                    except Exception as e:
                        self.log(f"[civ] bridge listener error: {e}")
            buf += data
            # cut out FE FE ... FD frames
            while True:
                i = buf.find(b"\xfe\xfe")
                if i < 0:
                    if len(buf) > 4096:
                        del buf[:-2]
                    break
                j = buf.find(b"\xfd", i + 2)
                if j < 0:
                    if i > 0:
                        del buf[:i]
                    break
                frame = bytes(buf[i:j + 1])
                del buf[:j + 1]
                try:
                    self._handle_frame(frame)
                except Exception as e:
                    self.log(f"[civ] handle_frame error: {e}")

    def _handle_frame(self, frame: bytes):
        if len(frame) < 6:
            return
        to, frm, cmd = frame[2], frame[3], frame[4]
        payload = frame[5:-1]
        # echo of our own command (to the radio) — ignore
        if to == self.addr:
            return
        # only accept frames addressed to the controller (0xE0) or transceive broadcast (0x00)
        if to not in (CTRL_ADDR, 0x00):
            return

        # SCOPE: 0x27 0x00 <data>
        if cmd == 0x27 and len(payload) >= 1 and payload[0] == 0x00:
            # Scope frame counter - used by the scope-restart-after-power-ON
            # logic (webapp: _verify_radio_awake_and_start_scope checks
            # whether the counter is increasing = frames are flowing).
            self._scope_rx_count = getattr(self, "_scope_rx_count", 0) + 1
            _hs_t0 = time.monotonic()
            self._handle_scope(payload[1:])
            _hs_ms = (time.monotonic() - _hs_t0) * 1000.0
            # Diagnostics: how long processing ONE scope frame takes. If
            # it's hundreds of ms, the culprit is _handle_scope; if it's
            # microseconds — the problem is in frame rate/core contention,
            # not the processing itself.
            if _hs_ms > 20:
                print(f"[scopetime] _handle_scope: {_hs_ms:.1f}ms", flush=True)
            return

        # frequency (response 0x03 or transceive 0x00)
        if cmd in (0x00, 0x03) and len(payload) >= 4:
            f = bcd_to_freq(payload[:5])
            if f and abs(f - self.freq) >= 1:
                # If we recently (< 0.8s) set the frequency locally (e.g.
                # click-to-tune) and the radio returns a DIFFERENT value,
                # it's probably a "stale" reading that started before 0x05
                # took effect — ignore it, so it doesn't overwrite the UI
                # with the old freq right after the click.
                grace = (time.time() - self._local_freq_set_at) < 0.8
                if grace and self._local_freq_set_val is not None and f != self._local_freq_set_val:
                    pass  # ignore the stale reading
                else:
                    # Protection against a corrupted BCD reading from the
                    # radio (seen when a bridge client sent a bad frame -
                    # the poller read freq=1 Hz and the UI showed
                    # 0.000001 MHz). IC-7300 min freq = 30kHz, max = 74MHz.
                    # We reject readings below 100kHz as invalid (very few
                    # people use LF). This only guards against drastic
                    # corruption, not subtle few-Hz errors.
                    if f < 100_000:
                        self.log(f"[civ] IGNORING absurd freq={f} Hz (BCD corruption?)")
                    else:
                        self.freq = f
                        self.bcast({"type": "freq", "freq": f})
        # mode (0x04 response / 0x01 transceive) — payload[0]=mode,
        # payload[1]=filter (01/02/03=FIL1/2/3, when the radio reports it)
        #
        # CRITICAL NOTE: payload[0] is ALWAYS the base CI-V mode (e.g.
        # USB=1), it NEVER carries DATA-mode information — that's a
        # separate command (1A 06), which the radio sends INDEPENDENTLY.
        # An earlier version of this handler did self.mode = nm DIRECTLY
        # from mode_map (which only knows base modes), which WIPED the
        # "-D" suffix every time the radio echoed/transceived its mode
        # (which happens often — including on every set_mode() we send
        # ourselves, since the IC-7300 sends back a confirmation through
        # the same transceive channel). Effect observed live: the UI
        # showed USB-D for a fraction of a second after clicking, and
        # polling every ~2s (rig_poll in server.py) reverted it back to
        # USB. Fix: append "-D" based on the ALREADY-TRACKED self.data_mode,
        # exactly the way set_mode does it when sending.
        elif cmd in (0x01, 0x04) and len(payload) >= 1:
            base_nm = self.mode_map.get(payload[0])
            nm = f"{base_nm}-D" if (base_nm and self.data_mode) else base_nm
            if nm and nm != self.mode:
                self.mode = nm
                self.bcast({"type": "mode", "mode": self.mode, "bandwidth": self.bw,
                            "filterNum": self.filter_num})
            if len(payload) >= 2 and payload[1] in (1, 2, 3) and payload[1] != self.filter_num:
                self.filter_num = payload[1]
                self.bcast({"type": "mode", "mode": self.mode, "bandwidth": self.bw,
                            "filterNum": self.filter_num})
        # PTT
        elif cmd == 0x1C and len(payload) >= 2 and payload[0] == 0x00:
            self.ptt = bool(payload[1])

        # forward to a waiting transaction — ONLY when the cmd code is
        # expected (set commands explicitly give {0xFB,0xFA}, so an
        # ACK/NAK won't falsely complete a freq/mode/S-meter read under
        # mixed CI-V traffic). When the waiter also gave 'sub' (the 15-xx
        # meter family — see _transact's docstring), the first payload
        # byte must match too, otherwise this frame belongs to a DIFFERENT
        # 15-xx query that's still in flight — leave it unconsumed so the
        # transact actually waiting for it can still catch it.
        ec = self._resp_cmd
        if ec and cmd in ec:
            es = self._resp_sub
            if es is None or (payload and payload[0] == es):
                self._resp_payload = payload
                self._resp_matched_cmd = cmd
                self._resp_ev.set()
            elif getattr(self, "_crosstalk_logged", 0) < 20:
                # DIAGNOSTIC: proves live whether 15-xx cross-talk actually
                # happens (see _transact docstring) — first 20 occurrences only.
                self._crosstalk_logged = getattr(self, "_crosstalk_logged", 0) + 1
                self.log(f"[civ] 15-xx cross-talk avoided: waiting for sub=0x{es:02X}, "
                         f"got 0x{payload[0]:02X} (belongs to another in-flight query)")

    def _handle_scope(self, d: bytes):
        if self._scope_logged < 4:
            self.log(f"[scope] raw frame ({len(d)}B): {d.hex()}")
            self._scope_logged += 1
        if len(d) < 3:
            return
        seq = d[1]
        total = d[2]

        if seq <= 1:
            # First frame: full header per CI-V Ref. p.19-14
            #   d[3]    = spectrum scope mode (00=Center,01=Fixed,02=SCROLL-C,03=SCROLL-F)
            #   d[4:9]  = center freq (Center mode) OR lower edge (Fixed/SCROLL)  — 5B BCD
            #   d[9:14] = span (Center mode, 5B "BCD-like" per the SPAN table)
            #             OR higher edge (Fixed/SCROLL) — 5B BCD
            #   d[14]   = out-of-range (00=in range, 01=out of range)
            #   d[15:]  = waveform data (only when total==1 and oor==00, or
            #             skipped in the 1st frame when total>1 — see the note in the docs)
            self._scope_mode_code = d[3] if len(d) > 3 else 0
            self._scope_oor = d[14] if len(d) > 14 else 0

            if self._scope_mode_code == 0x00:
                # Center mode: center freq + span
                try:
                    self._scope_center = bcd_to_freq(d[4:9])
                except Exception:
                    self._scope_center = self.freq
                try:
                    span_raw = bcd_to_freq(d[9:14])
                    self._scope_span_hz = span_raw if span_raw else self._scope_span_hz
                except Exception:
                    pass
                self._scope_lo = self._scope_center - self._scope_span_hz // 2
                self._scope_hi = self._scope_center + self._scope_span_hz // 2
            else:
                # Fixed / SCROLL-C / SCROLL-F: lower + higher edge
                try:
                    self._scope_lo = bcd_to_freq(d[4:9])
                    self._scope_hi = bcd_to_freq(d[9:14])
                    self._scope_center = (self._scope_lo + self._scope_hi) // 2
                    self._scope_span_hz = self._scope_hi - self._scope_lo
                except Exception:
                    pass

            hdr_len = self.scope_header_len
            data = d[hdr_len:] if len(d) > hdr_len else b""
            self._scope_acc = bytearray(data)
            self._scope_total = total
        else:
            self._scope_acc += d[3:]

        if total and seq >= total and self._scope_acc:
            # Rate limit: max 20 frames/s (50ms interval). The IC-7300
            # sends the scope at ~30-60fps but the WS client doesn't need
            # that much - 20fps is a smooth waterfall + half the
            # asyncio/JSON/WebSocket load. Note: this is OPTIONAL, it
            # helps a lot with many clients.
            now = time.time()
            if (now - self._scope_last) < 0.050:
                # Too early - drop this frame, wait for the next one.
                # We do NOT reset _scope_acc so accumulation keeps going.
                # But it needs to start fresh on the next packet:
                self._scope_acc = bytearray()
                self._scope_total = 0
                return

            smax = self.scope_max
            # Scaling scope data to 0-255. Used to be: a Python loop over
            # ~475 spectrum points, called a DOZEN-PLUS times per second —
            # this is what was blocking the event loop for 100-250ms
            # (confirmed: lag grows with the number of scope frames). numpy
            # does the same thing vectorized, ~100x faster.
            _raw = np.frombuffer(bytes(self._scope_acc), dtype=np.uint8)
            arr = np.minimum(255, (_raw.astype(np.uint32) * 255) // max(smax, 1)).astype(np.uint8).tolist()

            # In Center mode (scopeMode==0) we use self.freq (from the
            # 0x03 poller, confirmed accurate) as centerHz — instead of the
            # value from the scope header (_scope_center), which in
            # testing showed a discrepancy against the radio's actual
            # frequency (up to a few kHz, possibly a BCD field parsing
            # artifact in this particular firmware). Lo/Hi are recomputed
            # relative to self.freq and the span from the header, so the
            # frequency axis and click-to-tune stay consistent with the
            # actual VFO.
            if self._scope_mode_code == 0x00:
                center = self.freq
                lo = center - self._scope_span_hz // 2
                hi = center + self._scope_span_hz // 2
            else:
                center = self._scope_center
                lo, hi = self._scope_lo, self._scope_hi

            # DECOUPLING the scope from the reader thread.
            #
            # Previously the reader called broadcast_sync
            # (run_coroutine_threadsafe) on every frame — this thread->loop
            # sync, done a dozen-plus times per second, collided with the
            # asyncio loop and blocked it (confirmed: lag grows linearly
            # with the number of frames, while processing ONE frame is
            # fast). Now the reader ONLY writes the freshest complete frame
            # to a variable — zero synchronization with the loop. A
            # separate, lightweight asyncio loop (_scope_pump in webapp)
            # reads that variable and sends it at a steady 15fps. The
            # reader hands off the data and goes back to reading the port,
            # without waiting for the loop.
            self._latest_scope = {
                "type": "scope_frame",
                "data": arr,
                "source": "radio",
                "centerHz": center,
                "spanHz": self._scope_span_hz,
                "loHz": lo,
                "hiHz": hi,
                "mode": self.mode,
                "dataMode": self.data_mode,
                "filterHz": self._filter_width_hz,
                "scopeMode": self._scope_mode_code,
                "outOfRange": bool(self._scope_oor),
            }
            self._scope_acc = bytearray()
            self._scope_last = time.time()
            return

    def _poller_loop(self):
        """Periodically poll freq/mode/S-meter/filter (responses update state in the reader)."""
        n = 0
        _off_logged = False
        while self._running and self._ser:
            try:
                # A powered-off radio (power OFF via CI-V) doesn't respond
                # to any transactions. Without this check every loop cycle
                # ended in a read error and spammed the log ("S-metr
                # transact fail" repeatedly).
                if not self.powered:
                    if not _off_logged:
                        self.log("[civ] Radio off — telemetry paused")
                        _off_logged = True
                    time.sleep(1.0)
                    continue
                if _off_logged:
                    self.log("[civ] Radio on — telemetry resumed")
                    _off_logged = False
                self._transact(bytes([0x03]), {0x03, 0x00}, 0.3)        # freq
                if n % 4 == 0:
                    self._transact(bytes([0x04]), {0x04, 0x01}, 0.3)    # mode
                # VFO B (inactive VFO): CI-V 25 01 -> response [01, 5B freq BCD]
                # (per doc p.19-13: "Selected or unselected VFO frequency
                # settings", 00=selected/active, 01=unselected). Polled
                # every ~1.2s — changes rarely (split/A-B swap).
                if n % 4 == 2:
                    bp = self._transact(bytes([0x25, 0x01]), {0x25}, 0.3)
                    if bp and len(bp) >= 6 and bp[0] == 0x01:
                        fb = bcd_to_freq(bp[1:6])
                        if fb and abs(fb - self.freq_b) >= 1:
                            self.freq_b = fb
                            self.bcast({"type": "freqB", "freqB": fb})
                # S-meter: 0x15 0x02 -> response 0x15 ...
                p = self._transact(bytes([0x15, 0x02]), {0x15}, 0.3, sub=0x02)
                if p and len(p) >= 3 and p[0] == 0x02:
                    raw = bcd2(p[1:3])
                    lvl = smeter_to_sunit(raw)
                    if abs(lvl - self.s_meter) > 0.2:
                        self.s_meter = lvl
                        # The frontend (ws.js) listens for msg.value - NOT msg.smeter!
                        self.bcast({"type": "smeter", "value": round(lvl, 1)})
                elif getattr(self, '_smeter_fail_logged', 0) < 5:
                    self.log(f"[civ] S-metr transact fail: p={p.hex() if p else None}")
                    self._smeter_fail_logged = getattr(self, '_smeter_fail_logged', 0) + 1

                # TX Meters: ALC (15 13), PWR (15 11), SWR (15 12), VOLT (15 15)
                # Polled only while PTT is active (or when the meter is
                # selected by the user)
                # 2-byte BCD -> values (calibration points from the
                # official IC-7300MK2 CI-V Reference Guide, see the
                # comments at each meter below):
                #   ALC:  0..120 -> 0..100% (linear)
                #   PWR:  0=0%, 143=50%, 213=100% (nonlinear)
                #   SWR:  0=1.0, 48=1.5, 80=2.0, 120=3.0 (nonlinear)
                #   VOLT: 0=0V, 13=10V, 241=16V (strongly nonlinear)
                if self.ptt or n % 8 == 0:
                    _dbg_alc = None
                    _dbg_pwr = None
                    _dbg_alc_raw = None
                    _dbg_pwr_raw = None
                    _dbg_swr = None
                    _dbg_swr_raw = None
                    _dbg_volt = None
                    _dbg_volt_raw = None
                    _dbg_rfpower_raw = None
                    # ALC. FIX: command "15 11" used to be used here, which
                    # per the official IC-7300 CI-V documentation is
                    # actually "PO" (output power), NOT ALC — the ALC/PWR
                    # labels in the code were swapped relative to the
                    # actual commands. The correct command for ALC is
                    # "15 13", with a documented range of 0000=Min to
                    # 0120=Max (NOT 0-241).
                    ap = self._transact(bytes([0x15, 0x13]), {0x15}, 0.25, sub=0x13)
                    if ap and len(ap) >= 3 and ap[0] == 0x13:
                        alc_raw = bcd2(ap[1:3])
                        alc_pct = min(100.0, max(0.0, alc_raw / 120 * 100))
                        _dbg_alc = alc_pct
                        _dbg_alc_raw = alc_raw
                        if alc_pct > self._alc_peak:
                            self._alc_peak = alc_pct
                            self._alc_peak_raw = alc_raw
                        self.bcast({"type": "txmeter", "meter": "ALC",
                                    "raw": alc_raw,
                                    "value": round(alc_pct, 1),
                                    "pct": min(1.0, alc_raw / 120),
                                    "peak": round(self._alc_peak, 1),
                                    "peakRaw": self._alc_peak_raw})
                    # PWR output. FIX: command "15 14" used to be used here
                    # (that's actually COMP — the speech compressor in dB,
                    # NOT power). The correct command for PO (output power)
                    # is "15 11", with a documented NONLINEAR scale:
                    # 0000=0%, 0143=50%, 0213=100% (full scale reached
                    # already at raw=213, not 241) — piecewise
                    # interpolation between these points.
                    pp = self._transact(bytes([0x15, 0x11]), {0x15}, 0.25, sub=0x11)
                    if pp and len(pp) >= 3 and pp[0] == 0x11:
                        pwr_raw = bcd2(pp[1:3])
                        _po_points = [(0, 0.0), (143, 50.0), (213, 100.0)]
                        if pwr_raw <= _po_points[0][0]:
                            pwr_pct = _po_points[0][1]
                        elif pwr_raw >= _po_points[-1][0]:
                            pwr_pct = _po_points[-1][1]
                        else:
                            pwr_pct = _po_points[-1][1]
                            for _i in range(len(_po_points) - 1):
                                _x0, _y0 = _po_points[_i]
                                _x1, _y1 = _po_points[_i + 1]
                                if _x0 <= pwr_raw <= _x1:
                                    _t = (pwr_raw - _x0) / (_x1 - _x0)
                                    pwr_pct = round(_y0 + _t * (_y1 - _y0), 1)
                                    break
                        _dbg_pwr = pwr_pct
                        _dbg_pwr_raw = pwr_raw
                        if pwr_pct > self._pwr_peak:
                            self._pwr_peak = pwr_pct
                            self._pwr_peak_raw = pwr_raw
                        self.bcast({"type": "txmeter", "meter": "PWR",
                                    "raw": pwr_raw,
                                    "value": pwr_pct,
                                    "pct": pwr_pct / 100,
                                    "peak": round(self._pwr_peak, 1),
                                    "peakRaw": self._pwr_peak_raw})
                    # SWR
                    sp = self._transact(bytes([0x15, 0x12]), {0x15}, 0.25, sub=0x12)
                    if sp and len(sp) >= 3 and sp[0] == 0x12:
                        swr_raw = bcd2(sp[1:3])
                        # IC-7300 SWR: 0=1.0, 48=1.5, 80=2.0, 120=3.0, 241=50
                        # FIX: the previous formula was linear over the
                        # WHOLE 0-241 range, which didn't match the
                        # calibration points above from the IC-7300
                        # documentation (SWR rises quickly to 3.0 already
                        # at half the raw=120 range, then much more slowly
                        # to 50 at raw=241) — this gave heavily inflated
                        # values (e.g. raw=48 -> 10.76 instead of the
                        # documented 1.5). Piecewise interpolation between
                        # the actual calibration points.
                        _swr_points = [(0, 1.0), (48, 1.5), (80, 2.0), (120, 3.0), (241, 50.0)]
                        swr_val = _swr_points[-1][1]
                        for _i in range(len(_swr_points) - 1):
                            _x0, _y0 = _swr_points[_i]
                            _x1, _y1 = _swr_points[_i + 1]
                            if _x0 <= swr_raw <= _x1:
                                _t = (swr_raw - _x0) / (_x1 - _x0)
                                swr_val = round(_y0 + _t * (_y1 - _y0), 2)
                                break
                        _dbg_swr = swr_val
                        _dbg_swr_raw = swr_raw
                        self.bcast({"type": "txmeter", "meter": "SWR",
                                    "raw": swr_raw,
                                    "value": swr_val,
                                    "pct": min(1.0, swr_raw / 120)})  # scale to SWR=3
                    # Supply voltage (Vd — drain voltage / power amp supply
                    # voltage). Command "15 15" (NOT "15 16" — that's Id,
                    # current, a different meter). Calibration verified
                    # directly against the official IC-7300MK2 CI-V
                    # Reference Guide (icomuk.co.uk), command table, entry
                    # "15 15": 0000=0V, 0013=10V, 0241=16V.
                    # FIX: the previous points (0=0V, 151=10V, 211=16V) were
                    # wrong — the scale is actually strongly nonlinear (the
                    # first 10V is only 13 raw units, the rest, 10-16V,
                    # stretches over the remaining 228 units, since that's
                    # the range where the 12-13.8V supply actually
                    # operates). The wrong points gave, e.g. for a real
                    # 13.8V (raw~157), a reading of ~10.6V — exactly the
                    # kind of low reading that was reported live.
                    vp = self._transact(bytes([0x15, 0x15]), {0x15}, 0.25, sub=0x15)
                    if vp and len(vp) >= 3 and vp[0] == 0x15:
                        v_raw = bcd2(vp[1:3])
                        _vd_points = [(0, 0.0), (13, 10.0), (241, 16.0)]
                        if v_raw <= _vd_points[0][0]:
                            volt = _vd_points[0][1]
                        elif v_raw >= _vd_points[-1][0]:
                            volt = _vd_points[-1][1]
                        else:
                            volt = _vd_points[-1][1]
                            for _i in range(len(_vd_points) - 1):
                                _x0, _y0 = _vd_points[_i]
                                _x1, _y1 = _vd_points[_i + 1]
                                if _x0 <= v_raw <= _x1:
                                    _t = (v_raw - _x0) / (_x1 - _x0)
                                    volt = round(_y0 + _t * (_y1 - _y0), 2)
                                    break
                        _dbg_volt = volt
                        _dbg_volt_raw = v_raw
                        self.bcast({"type": "txmeter", "meter": "VOLT",
                                    "raw": v_raw,
                                    "value": volt,
                                    "pct": volt / 16.0})  # scale to nominal 16V

                    # RFPOWER SET level (14 0A) — direct read-back of the
                    # radio's CONFIGURED power ceiling, independent of the
                    # UI slider display. DIAGNOSTIC: with PO/PWR staying
                    # flat around ~25% across a big txVolume sweep
                    # (0.7x-1.5x) and ALC pinned at 0 the whole time, one
                    # remaining explanation is that the "100%" the UI shows
                    # never actually reached raw=255 on the radio itself —
                    # this confirms or rules that out directly instead of
                    # guessing again.
                    rfp = self._transact(bytes([0x14, 0x0A]), {0x14}, 0.25, sub=0x0A)
                    if rfp and len(rfp) >= 3 and rfp[0] == 0x0A:
                        _dbg_rfpower_raw = bcd2(rfp[1:3])

                    if VERBOSE and self.ptt and n % 4 == 0:
                        self.log(f"[txmeter] TX read: ALC={_dbg_alc if _dbg_alc is not None else '?'}% "
                                 f"(raw={_dbg_alc_raw}, peak={self._alc_peak:.1f}% raw={self._alc_peak_raw}) "
                                 f"PWR={_dbg_pwr}% (raw={_dbg_pwr_raw}, peak={self._pwr_peak:.1f}% raw={self._pwr_peak_raw}) "
                                 f"SWR={_dbg_swr} (raw={_dbg_swr_raw}) VOLT={_dbg_volt} (raw={_dbg_volt_raw}) "
                                 f"RFPOWER_set_raw={_dbg_rfpower_raw} (0-255, 255=100%) "
                                 f"freq={self.freq/1e6:.4f}MHz mode={self.mode} (bcast sent)")
                # Filter width: 1A 03 -> response [03, idx_bcd_2B] (00..49)
                # DATA mode: 1A 06 -> response [06, data_mode(0/1), filter(1-3)]
                #   (per doc p.19-10) — affects the interpretation of
                #   idx>31 in _SSB_FILTER_TABLE (SSB-D range up to 3600Hz,
                #   DATA mode only).
                # Polled less often (every ~1.2s) — both values rarely change
                if n % 4 == 1:
                    # FIX: same "shared response cmd, no sub-command check"
                    # bug as the 15-xx meter family (see _transact's
                    # docstring) — data mode (1A 06) and filter width
                    # (1A 03) BOTH reply with cmd 0x1A. A late/crossed
                    # response here is worse than a stale meter reading: if
                    # a stale "1A 06" answer (e.g. from BEFORE the radio was
                    # ever switched into DATA mode) gets consumed here, the
                    # code below actively WRITES data_mode=False back and
                    # strips "-D" from self.mode — silently kicking a radio
                    # that IS in USB-D back to being TRACKED as plain USB
                    # (symptom reported live: mode=USB in the FT8 TX log,
                    # PWR stuck low regardless of drive level, because plain
                    # USB takes modulation from the physical MIC input, not
                    # from our USB audio at all).
                    dp = self._transact(bytes([0x1A, 0x06]), {0x1A}, 0.3, sub=0x06)
                    if dp and len(dp) >= 2 and dp[0] == 0x06:
                        new_dm = bool(dp[1])
                        if new_dm != self.data_mode:
                            self.data_mode = new_dm
                            # CRITICAL: also update self.mode (the -D
                            # suffix). Without this, a radio in USB-D had
                            # self.mode="USB" -> the UI held "USB" -> every
                            # t='mode' from the UI (e.g. a filter change)
                            # did set_mode("USB") -> 1A 06 00 -> TURNED OFF
                            # data mode in the radio. Symptom: "keeps
                            # switching from USB-D back to USB in a digital mode".
                            base = self.mode[:-2] if self.mode.endswith("-D") else self.mode
                            if base in ("USB", "LSB"):
                                self.mode = f"{base}-D" if new_dm else base
                                self.bcast({"type": "mode", "mode": self.mode,
                                            "bandwidth": self.bw})
                            self._filter_width_hz = filter_width_hz(self.mode, self._filter_idx, self.data_mode)
                            self.bcast({"type": "filter_width", "hz": self._filter_width_hz,
                                        "idx": self._filter_idx, "mode": self.mode,
                                        "dataMode": self.data_mode})

                    fp = self._transact(bytes([0x1A, 0x03]), {0x1A}, 0.3, sub=0x03)
                    # FIX: the response is [0x03, idx_bcd_1B] - ONE byte
                    # holding a 2-digit BCD index (00..49), not two bytes -
                    # confirmed live (fp.hex()=='0336', i.e. only 2 bytes
                    # total). The old code required len(fp)>=3 and read a
                    # 2-byte BCD via bcd2(fp[1:3]) - always failed the
                    # length check, so _filter_idx/_filter_width_hz never
                    # updated past their __init__ defaults (2400Hz) no
                    # matter what filter was actually selected on the radio.
                    if fp and len(fp) >= 2 and fp[0] == 0x03:
                        idx = (fp[1] >> 4) * 10 + (fp[1] & 0x0F)
                        if idx != self._filter_idx:
                            self._filter_idx = idx
                            self._filter_width_hz = filter_width_hz(self.mode, idx, self.data_mode)
                            self.bcast({"type": "filter_width", "hz": self._filter_width_hz,
                                        "idx": idx, "mode": self.mode, "dataMode": self.data_mode})
                    elif getattr(self, "_filter_poll_fail_logged", 0) < 10:
                        self._filter_poll_fail_logged = getattr(self, "_filter_poll_fail_logged", 0) + 1
                        self.log(f"[civ] filter poll FAILED: fp={fp.hex() if fp else None}")
                # Round-robin read of the Set Level sliders (CI-V 14 <sub>) —
                # one level per cycle (~0.3s), a full sweep of 14 levels
                # takes ~4.2s. Lets the UI notice changes made manually on
                # the radio's front panel (e.g. turning the AF/RF knob on
                # the IC-7300).
                level_items = list(self.profile.get("levels", {}).items())
                if level_items:
                    name, lvl = level_items[n % len(level_items)]
                    new_val = self._read_level(name, lvl)
                    if new_val is not None:
                        old_val = self.level_values.get(name)
                        if old_val is None or abs(new_val - old_val) > 1e-6:
                            self.level_values[name] = new_val
                            self.bcast({"type": "level_value",
                                        "id": f"level_{name.lower()}", "value": new_val})
                # Preamp (16 02 -> response [02, 00|01|02]) and Attenuator
                # (11 -> response [00|20]) — polled every ~1.2s (rarely
                # change, usually set manually on the radio's panel).
                if n % 4 == 3:
                    pp = self._transact(bytes([0x16, 0x02]), {0x16}, 0.3)
                    if pp and len(pp) >= 2 and pp[0] == 0x02:
                        new_pre = pp[1]
                        if new_pre in (0, 1, 2) and new_pre != self.preamp:
                            self.preamp = new_pre
                            self.bcast({"type": "preamp", "value": new_pre})

                    ap = self._transact(bytes([0x11]), {0x11}, 0.3)
                    if ap and len(ap) >= 1:
                        new_att = (ap[0] != 0x00)
                        if new_att != self.attenuator:
                            self.attenuator = new_att
                            self.bcast({"type": "attenuator", "value": new_att})

                    # Tuner ON/OFF: 1C 01 -> response [01, 00|01]. We don't
                    # poll the "START" state (1C 01 02 is a one-shot
                    # command, the radio doesn't report it as a persistent
                    # state).
                    tp = self._transact(bytes([0x1C, 0x01]), {0x1C}, 0.3)
                    if tp and len(tp) >= 2 and tp[0] == 0x01:
                        new_tuner = (tp[1] != 0x00)
                        if new_tuner != self.tuner:
                            self.tuner = new_tuner
                            self.bcast({"type": "tuner", "value": new_tuner})
            except Exception as e:
                self.log(f"[civ] poller error: {e}")
            n += 1
            time.sleep(0.3)

    # ════════════════════════════════════════════════════════════════════════
    # SIMULATION (no hardware / Replit)
    # ════════════════════════════════════════════════════════════════════════
    def _start_sim(self) -> bool:
        self.sim = True
        self.connected = False
        self.scope_running = True
        if not self.last_msg:
            self.last_msg = "Tryb bezposredni CI-V — SYMULACJA scope (brak sprzetu COM)"
        self._running = True
        if not (self._sim_th and self._sim_th.is_alive()):
            self._sim_th = threading.Thread(target=self._sim_loop, daemon=True)
            self._sim_th.start()
            self.log("[civ] Scope SIMULATION started")
        # Background reconnect-attempt thread (e.g. the radio was off when
        # the server started, the user will power it on later physically
        # or via the Power button after an earlier wakeup)
        if not (self._reconnect_th and self._reconnect_th.is_alive()):
            self._reconnect_th = threading.Thread(target=self._reconnect_loop, daemon=True)
            self._reconnect_th.start()
        return False

    def _reconnect_loop(self):
        """
        Every RECONNECT_INTERVAL seconds (in simulation mode) try to reopen
        the port and poll 0x03. If the radio responds — switch from
        simulation to a real connection (analogous to a successful _open()).
        """
        RECONNECT_INTERVAL = 10.0
        while self._running and self.sim:
            time.sleep(RECONNECT_INTERVAL)
            if not self.sim or not self._running:
                return  # someone already connected manually / it's been closed
            if not HAS_SERIAL:
                continue
            try:
                test_ser = serial.Serial(self.port, self.speed, timeout=0.3, write_timeout=1.0)
            except Exception:
                continue  # port still unavailable — try again later

            try:
                # Send 0x03 (get freq) and check the response
                frame = bytes([0xFE, 0xFE, self.addr, CTRL_ADDR, 0x03, 0xFD])
                test_ser.write(frame)
                time.sleep(0.3)
                resp = test_ser.read(64)
                test_ser.close()
            except Exception:
                try: test_ser.close()
                except: pass
                continue

            if not resp or 0xFD not in resp:
                continue  # no response — radio not ready yet

            self.log("[civ] Reconnect: radio responded — switching out of simulation")
            # Stop the simulation, open a real connection
            self.sim = False
            self._running = False  # stops _sim_loop
            time.sleep(0.2)
            self._running = True
            self._open()
            return  # _open() either connects for real, or falls back to
                    # _start_sim (which spawns a new _reconnect_th if still in sim)

    def _sim_loop(self):
        N = 320
        t = 0.0
        # a few "signals" that drift across the band
        peaks = [{"pos": random.uniform(0.15, 0.85),
                  "amp": random.uniform(0.5, 1.0),
                  "w":   random.uniform(0.004, 0.02),
                  "drift": random.uniform(-0.0008, 0.0008)} for _ in range(4)]
        # STABILITY: try INSIDE the loop. Without this, a single error in
        # bcast() (e.g. the hub in a bad state while disconnecting clients)
        # kills the simulation thread permanently - the waterfall in SIM
        # mode stops working until the server restarts.
        _errs = 0
        while self._running and self.sim:
            try:
                arr = []
                noise_base = 18 + 6 * math.sin(t * 0.7)
                for i in range(N):
                    x = i / N
                    v = noise_base + random.uniform(0, 10)
                    for pk in peaks:
                        d = x - pk["pos"]
                        v += pk["amp"] * 230 * math.exp(-(d * d) / (2 * pk["w"]))
                    arr.append(max(0, min(255, int(v))))
                for pk in peaks:
                    pk["pos"] += pk["drift"]
                    if pk["pos"] < 0.1 or pk["pos"] > 0.9:
                        pk["drift"] *= -1
                    pk["amp"] = max(0.25, min(1.0, pk["amp"] + random.uniform(-0.05, 0.05)))
                self.bcast({"type": "scope_frame", "data": arr, "source": "sim",
                            "centerHz": self.freq, "spanHz": self._scope_span_hz,
                            "loHz": self.freq - self._scope_span_hz // 2,
                            "hiHz": self.freq + self._scope_span_hz // 2,
                            "mode": self.mode, "dataMode": self.data_mode,
                            "filterHz": self._filter_width_hz,
                            "scopeMode": 0, "outOfRange": False})
                _errs = 0
            except Exception as e:
                _errs += 1
                if _errs <= 3:
                    print(f"[civ] _sim_loop error ({_errs}): {e}", flush=True)
                elif _errs == 4:
                    print("[civ] _sim_loop: further errors suppressed in the log", flush=True)
                time.sleep(0.5)  # backoff to avoid flooding the log
            t += 0.08
            time.sleep(0.08)
