#!/usr/bin/env python3
"""
rotator.py — Alfaspid RAK/RAS (protokół SPID) przez port szeregowy (pyserial).
Na Replit (brak portów COM) automatyczny fallback do symulacji.
"""
import re, sys, time, threading

try:
    import serial
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False
    print("[warn] Brak pyserial — pip install pyserial")


class Rotator:
    """
    Alfaspid RAK (Rot1Prog) — protokół SPID, pyserial.
      STATUS TX (13B): 57 00..00 1F 20  →  RX (5B): 57 H1 H2 H3 20  (az = H1*100+H2*10+H3-360)
      SET TX:          57 H1 H2 H3 00 01 00 00 00 00 00 2F 20  (H = az+360, cyfry ASCII)
      STOP TX (6B):    57 00 00 00 0F 20
    """

    STATUS_PKT = bytes([0x57, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0x1F, 0x20])

    def __init__(self, cfg: dict, broadcast_fn=None):
        self.id         = int(cfg.get("id", 1))
        self.name       = cfg.get("name", "Rotator")
        self.port       = cfg.get("port", "COM8")
        self.speed      = int(cfg.get("speed", 1200))
        self.model      = str(cfg.get("model", "901"))
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

    # ── Protokół SPID ─────────────────────────────────────────────────────────
    def _set_pkt(self, az: float) -> bytes:
        """SET 13B: 57 H1 H2 H3 00 01 00 00 00 00 00 2F 20 (H = az+360, ASCII)."""
        A = str(int(round(az + 360)) % 1000).zfill(3)
        return bytes([0x57,
                      ord(A[0]), ord(A[1]), ord(A[2]),
                      0x00, 0x01,
                      0x00, 0x00, 0x00, 0x00, 0x00,
                      0x2F, 0x20])

    def _stop_pkt(self) -> bytes:
        """STOP 6B: 57 00 00 00 0F 20"""
        return bytes([0x57, 0, 0, 0, 0x0F, 0x20])

    def _decode(self, buf: bytes) -> float | None:
        """Znajdź i zdekoduj 5B ramkę: 57 H1 H2 H3 20"""
        for i in range(len(buf) - 4):
            if buf[i] == 0x57 and buf[i + 4] == 0x20:
                az = buf[i+1] * 100 + buf[i+2] * 10 + buf[i+3] - 360
                if -5 <= az <= 365:
                    return float(az)
        return None

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
        """Wyślij STATUS (13B), odczytaj odpowiedź (5B). W symulacji zwraca self.az."""
        if self.sim:
            return self.az
        if not self._ser or not self._ser.is_open:
            return None
        with self._lock:
            try:
                self._ser.reset_input_buffer()
                self._ser.write(self.STATUS_PKT)
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
            print(f"[rotator] {self.name}: pyserial niedostępny → symulacja")
            self.sim = True
            return False
        try:
            # Windows: COM1..COM9 normalnie, COM10+ wymaga \\.\COMx
            p = ("\\\\.\\"+self.port
                 if sys.platform == "win32" and re.match(r"^COM[0-9]+$", self.port, re.I)
                 else self.port)
            self._ser = serial.Serial(
                port=p, baudrate=self.speed,
                bytesize=8, parity="N", stopbits=1, timeout=0.1)
            az = self._read_pos(3.0)
            if az is None:
                raise IOError("brak odpowiedzi na STATUS (3s timeout)")
            self.az        = az
            self.connected = True
            self.sim       = False
            print(f"[rotator] {self.name} SPID @ {self.port} {self.speed}bd — az={self.az:.0f}°")
            return True
        except Exception as e:
            print(f"[rotator] {self.name}: {e} → symulacja")
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
        """Pętla obrotu RAK: STOP → SET → poll pozycji co 0.5s (płynny odczyt)
        → powtórz (max 10 kroków). Zamiast czekać 12s w ślepo, w trakcie ruchu
        odpytujemy STATUS co ~0.5s i broadcastujemy pozycje, zeby UI plynnie
        pokazywalo obrot rotora."""
        MAX_STEPS = 10
        step      = 0
        while step < MAX_STEPS and not self._stop_ev.is_set():
            diff = abs(self.az - self.taz)
            if diff < 2.0:
                print(f"[rotator] {self.name} ✓ dotarł {self.taz:.0f}° (az={self.az:.0f}°)")
                break
            step += 1
            print(f"[rotator] {self.name} krok {step}/{MAX_STEPS}: "
                  f"az={self.az:.0f}° → cel={self.taz:.0f}° (Δ={diff:.0f}°)")
            self._write(self._stop_pkt())
            if self._stop_ev.wait(0.5): break
            self._write(self._set_pkt(self.taz))
            # Zamiast slepego wait(12s): poll pozycji co 0.5s przez max 12s.
            # Broadcast po kazdym udanym odczycie -> plynny ruch w UI.
            poll_deadline = time.monotonic() + 12.0
            last_az = self.az
            stable_count = 0
            while time.monotonic() < poll_deadline and not self._stop_ev.is_set():
                if self._stop_ev.wait(0.5): break
                pos = self._read_pos(1.0)
                if pos is not None:
                    self.az = pos
                    self.bcast()
                    # Jesli pozycja przestala sie zmieniac (rotor dotarl/stanal)
                    # przez 3 kolejne odczyty (~1.5s) — przerwij czekanie.
                    if abs(pos - last_az) < 1.0:
                        stable_count += 1
                        if stable_count >= 3:
                            break
                    else:
                        stable_count = 0
                    last_az = pos
                    # Jesli osiagnelismy cel — koniec
                    if abs(pos - self.taz) < 2.0:
                        break
        self.moving = False
        self.bcast()

    def _sim_worker(self):
        """Symulacja ruchu ~3°/s, aktualizacja co 100ms dla plynnosci"""
        while self.moving and not self._stop_ev.is_set():
            diff = self.taz - self.az
            if abs(diff) < 0.5:
                self.az = self.taz
                break
            # 3°/s przy update co 0.1s = 0.3° na krok
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
        """Odczyt pozycji gdy rotor stoi. True jeśli się zmieniła."""
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
