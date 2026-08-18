#!/usr/bin/env python3
"""
rotator.py — rotor control over a serial port (pyserial), two protocols:
  - Alfaspid RAK/RAS (SPID) — models "901"/"902"
  - Yaesu GS-232A — models "601"/"603" (see docstring in Rotator._yaesu_*)
Automatic fallback to simulation when no COM port is available (e.g. Replit).
"""
import re, sys, time, threading

try:
    import serial
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False
    print("[warn] pyserial missing — pip install pyserial")

YAESU_MODELS = {"601", "603"}  # GS-232A / GS-232B (dropdown in admin.js)


class Rotator:
    """
    Two supported protocols, selected from the 'model' field in config
    (self.protocol = 'yaesu' | 'spid'). The rest of the class (serial
    connection, motion thread, simulation, WS broadcast) is shared by both.

    Alfaspid RAK (Rot1Prog) — SPID protocol, pyserial.
      STATUS TX (13B): 57 00..00 1F 20  →  RX (5B): 57 H1 H2 H3 20  (az = H1*100+H2*10+H3-360)
      SET TX:          57 H1 H2 H3 00 01 00 00 00 00 00 2F 20  (H = az+360, ASCII digits)
      STOP TX (6B):    57 00 00 00 0F 20

    Yaesu GS-232A — ASCII, commands terminated with CR (\\r), 8N1.
      STATUS TX: "C\\r"      →  RX: "+0ddd\\r" (sign + 4-digit azimuth, e.g. "+0180")
      SET TX:    "Mddd\\r"   (3-digit azimuth 000-360, unsigned)
      STOP TX:   "S\\r"
    NOTE: based on the commonly documented GS-232A command set (Hamlib,
    PstRotator, N1MM all use the same C/M/S + "+0ddd" format).
    UNVERIFIED against physical hardware — before connecting a real Yaesu
    controller, confirm the framing in YOUR model's manual (GS-232 firmware
    revisions vary in framing details). Use simulation mode (sim=True, see
    connect()) until confirmed.
    """

    STATUS_PKT = bytes([0x57, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x1F, 0x20])  # SPID

    def __init__(self, cfg: dict, broadcast_fn=None):
        self.id         = int(cfg.get("id", 1))
        self.name       = cfg.get("name", "Rotator")
        self.port       = cfg.get("port", "COM8")
        self.speed      = int(cfg.get("speed", 1200))
        self.model      = str(cfg.get("model", "901"))
        self.protocol   = "yaesu" if self.model in YAESU_MODELS else "spid"
        self.enabled    = bool(cfg.get("enabled", True))
        self.az         = 0.0
        self.el         = 0.0
        self.taz        = 0.0
        self.moving     = False
        self.connected  = False
        self.sim        = False
        self._ser       = None
        self._lock      = threading.Lock()
        self._stop_ev   = threading.Event()
        self._broadcast = broadcast_fn

    def bcast(self):
        if self._broadcast:
            self._broadcast({"type": "rotator_update", "rotator": self.state()})

    def state(self) -> dict:
        return {
            "id": self.id, "name": self.name, "model": self.model,
            "port": self.port, "speed": self.speed, "enabled": self.enabled,
            "azimuth":    round(self.az,  1),
            "elevation":  round(self.el,  1),
            "target_az":  round(self.taz, 1),
            "moving":     self.moving,
            "connected":  self.connected,
            "sim":        self.sim,
        }

    # ── SPID protocol ────────────────────────────────────────────────────────
    def _spid_set_pkt(self, az: float) -> bytes:
        """SET 13B: 57 H1 H2 H3 00 01 00 00 00 00 00 2F 20 (H = az+360, ASCII)."""
        A = str(int(round(az + 360)) % 1000).zfill(3)
        return bytes([0x57,
                      ord(A[0]), ord(A[1]), ord(A[2]),
                      0x00, 0x01,
                      0x00, 0x00, 0x00, 0x00, 0x00,
                      0x2F, 0x20])

    def _spid_stop_pkt(self) -> bytes:
        """STOP 6B: 57 00 00 00 0F 20"""
        return bytes([0x57, 0, 0, 0, 0x0F, 0x20])

    def _spid_decode(self, buf: bytes) -> float | None:
        """Find and decode the 5B frame: 57 H1 H2 H3 20"""
        for i in range(len(buf) - 4):
            if buf[i] == 0x57 and buf[i + 4] == 0x20:
                az = buf[i+1] * 100 + buf[i+2] * 10 + buf[i+3] - 360
                if -5 <= az <= 365:
                    return float(az)
        return None

    # ── Yaesu GS-232A protocol ───────────────────────────────────────────────
    def _yaesu_status_pkt(self) -> bytes:
        return b"C\r"

    def _yaesu_set_pkt(self, az: float) -> bytes:
        """SET: "Mddd\\r" — 3-digit azimuth 000-360, unsigned."""
        A = str(int(round(az)) % 360).zfill(3)
        return f"M{A}\r".encode("ascii")

    def _yaesu_stop_pkt(self) -> bytes:
        return b"S\r"

    def _yaesu_decode(self, buf: bytes) -> float | None:
        """Parse the "+0ddd\\r" reply (sign + 4-digit azimuth)."""
        try:
            txt = buf.decode("ascii", errors="ignore")
        except Exception:
            return None
        m = re.search(r'([+-]\d{4})', txt)
        if m:
            az = int(m.group(1))
            if -5 <= az <= 365:
                return float(az)
        return None

    # ── Protocol dispatcher (used by _write/_read_pos/_move_worker) ────────────
    def _set_pkt(self, az: float) -> bytes:
        return self._yaesu_set_pkt(az) if self.protocol == "yaesu" else self._spid_set_pkt(az)

    def _stop_pkt(self) -> bytes:
        return self._yaesu_stop_pkt() if self.protocol == "yaesu" else self._spid_stop_pkt()

    def _decode(self, buf: bytes) -> float | None:
        return self._yaesu_decode(buf) if self.protocol == "yaesu" else self._spid_decode(buf)

    def _status_pkt(self) -> bytes:
        return self._yaesu_status_pkt() if self.protocol == "yaesu" else self.STATUS_PKT

    # ── Serial I/O ────────────────────────────────────────────────────────────
    def _write(self, pkt: bytes) -> bool:
        with self._lock:
            try:
                if self._ser and self._ser.is_open:
                    self._ser.write(pkt)
                    return True
            except Exception as e:
                print(f"[rot:{self.name}] write error: {e}")
        return False

    def _read_pos(self, timeout: float = 2.5) -> float | None:
        """Send STATUS, read the reply (format depends on protocol - see
        _status_pkt/_decode). Returns self.az in simulation mode."""
        if self.sim:
            return self.az
        if not self._ser or not self._ser.is_open:
            return None
        with self._lock:
            try:
                self._ser.reset_input_buffer()
                self._ser.write(self._status_pkt())
                buf      = bytearray()
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    n = self._ser.in_waiting
                    if n:
                        buf.extend(self._ser.read(n))
                        if len(buf) >= 5:
                            az = self._decode(bytes(buf))
                            if az is not None:
                                return az
                    else:
                        time.sleep(0.05)
            except Exception as e:
                print(f"[rot:{self.name}] read error: {e}")
        return None

    # ── Connect ───────────────────────────────────────────────────────────────
    def connect(self) -> bool:
        if not HAS_SERIAL:
            print(f"[rotator] {self.name}: pyserial unavailable → simulation")
            self.sim = True
            return False
        try:
            # Windows: COM1..COM9 work as-is, COM10+ needs \\.\COMx
            p = ("\\\\.\\"+self.port
                 if sys.platform == "win32" and re.match(r"^COM[0-9]+$", self.port, re.I)
                 else self.port)
            self._ser = serial.Serial(
                port=p, baudrate=self.speed,
                bytesize=8, parity="N", stopbits=1, timeout=0.1)
            az = self._read_pos(3.0)
            if az is None:
                raise IOError("no response to STATUS (3s timeout)")
            self.az        = az
            self.connected = True
            self.sim       = False
            proto_label = "Yaesu GS-232A" if self.protocol == "yaesu" else "SPID"
            print(f"[rotator] {self.name} {proto_label} @ {self.port} {self.speed}bd — az={self.az:.0f}°")
            return True
        except Exception as e:
            print(f"[rotator] {self.name}: {e} → simulation")
            if self._ser:
                try: self._ser.close()
                except: pass
                self._ser = None
            self.sim       = True
            self.connected = False
            return False

    # ── Go To ─────────────────────────────────────────────────────────────────
    def go_to(self, az: float):
        self.taz = ((float(az) % 360) + 360) % 360
        self._stop_ev.clear()
        self.moving = True
        self.bcast()
        if self.sim:
            threading.Thread(target=self._sim_worker, daemon=True).start()
        else:
            threading.Thread(target=self._move_worker, daemon=True).start()

    def _move_worker(self):
        """RAK rotation loop: STOP → SET → poll position every 0.5s (smooth
        readout) → repeat (max 10 steps). Instead of blindly waiting 12s, we
        poll STATUS roughly every 0.5s during the move and broadcast the
        position, so the UI shows the rotor turning smoothly."""
        MAX_STEPS = 10
        step      = 0
        while step < MAX_STEPS and not self._stop_ev.is_set():
            diff = abs(self.az - self.taz)
            if diff < 2.0:
                print(f"[rotator] {self.name} ✓ reached {self.taz:.0f}° (az={self.az:.0f}°)")
                break
            step += 1
            print(f"[rotator] {self.name} step {step}/{MAX_STEPS}: "
                  f"az={self.az:.0f}° → target={self.taz:.0f}° (Δ={diff:.0f}°)")
            self._write(self._stop_pkt())
            if self._stop_ev.wait(0.5): break
            self._write(self._set_pkt(self.taz))
            # Instead of a blind wait(12s): poll position every 0.5s for up
            # to 12s, broadcasting after every successful read for smooth UI motion.
            poll_deadline = time.monotonic() + 12.0
            last_az = self.az
            stable_count = 0
            while time.monotonic() < poll_deadline and not self._stop_ev.is_set():
                if self._stop_ev.wait(0.5): break
                pos = self._read_pos(1.0)
                if pos is not None:
                    self.az = pos
                    self.bcast()
                    # If the position stops changing (rotor arrived/stalled)
                    # for 3 consecutive reads (~1.5s) — stop waiting.
                    if abs(pos - last_az) < 1.0:
                        stable_count += 1
                        if stable_count >= 3:
                            break
                    else:
                        stable_count = 0
                    last_az = pos
                    # Target reached — done.
                    if abs(pos - self.taz) < 2.0:
                        break
        self.moving = False
        self.bcast()

    def _sim_worker(self):
        """Motion simulation ~3°/s, updated every 100ms for smoothness."""
        while self.moving and not self._stop_ev.is_set():
            diff = self.taz - self.az
            if abs(diff) < 0.5:
                self.az = self.taz
                break
            # 3°/s at a 0.1s update interval = 0.3° per step
            self.az += 3.0 * (1 if diff > 0 else -1) * 0.1
            self.bcast()
            time.sleep(0.1)
        self.moving = False
        self.bcast()

    def stop(self):
        self._stop_ev.set()
        self.moving = False
        if not self.sim:
            self._write(self._stop_pkt())
        self.bcast()
        print(f"[rotator] {self.name} STOP az={self.az:.0f}°")

    def poll_pos(self) -> bool:
        """Read position while the rotor is stationary. True if it changed."""
        if self.moving or self.sim:
            return False
        pos = self._read_pos(2.0)
        if pos is not None and abs(pos - self.az) > 0.3:
            self.az = pos
            return True
        return False

    def close(self):
        self._stop_ev.set()
        if self._ser:
            try: self._ser.close()
            except: pass
            self._ser = None
