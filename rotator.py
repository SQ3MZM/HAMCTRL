#!/usr/bin/env python3
"""
rotator.py — sterowanie rotorem przez port szeregowy (pyserial), dwa protokoly:
  - Alfaspid RAK/RAS (SPID) — modele "901"/"902"
  - Yaesu GS-232A — modele "601"/"603" (patrz komentarz w Rotator._yaesu_*)
Na Replit (brak portow COM) automatyczny fallback do symulacji.
"""
import re, sys, time, threading

try:
    import serial
    HAS_SERIAL = True
except ImportError:
    HAS_SERIAL = False
    print("[warn] Brak pyserial — pip install pyserial")

YAESU_MODELS = {"601", "603"}  # GS-232A / GS-232B (dropdown w admin.js)


class Rotator:
    """
    Dwa wspierane protokoly, wybierane po polu 'model' z konfiguracji
    (self.protocol = 'yaesu' | 'spid'). Reszta klasy (polaczenie szeregowe,
    watek ruchu, symulacja, broadcast WS) jest wspolna dla obu.

    Alfaspid RAK (Rot1Prog) — protokol SPID, pyserial.
      STATUS TX (13B): 57 00..00 1F 20  →  RX (5B): 57 H1 H2 H3 20  (az = H1*100+H2*10+H3-360)
      SET TX:          57 H1 H2 H3 00 01 00 00 00 00 00 2F 20  (H = az+360, cyfry ASCII)
      STOP TX (6B):    57 00 00 00 0F 20

    Yaesu GS-232A — ASCII, komendy zakonczone CR (\\r), 8N1.
      STATUS TX: "C\\r"      →  RX: "+0ddd\\r" (znak + 4 cyfry azymutu, np. "+0180")
      SET TX:    "Mddd\\r"   (3-cyfrowy azymut 000-360, bez znaku)
      STOP TX:   "S\\r"
    UWAGA: oparte na powszechnie udokumentowanym zestawie komend GS-232A
    (Hamlib, PstRotator, N1MM uzywaja tego samego C/M/S + formatu "+0ddd").
    NIEZWERYFIKOWANE na fizycznym kontrolerze — przed podlaczeniem realnego
    Yaesu potwierdz format w instrukcji SWOJEGO modelu (rozne firmware GS-232
    roznia sie w szczegolach ramki). Do czasu potwierdzenia uzywaj trybu
    symulacji (sim=True, patrz connect()).
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

    # ── Protokół SPID ─────────────────────────────────────────────────────────
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
        """Znajdź i zdekoduj 5B ramkę: 57 H1 H2 H3 20"""
        for i in range(len(buf) - 4):
            if buf[i] == 0x57 and buf[i + 4] == 0x20:
                az = buf[i+1] * 100 + buf[i+2] * 10 + buf[i+3] - 360
                if -5 <= az <= 365:
                    return float(az)
        return None

    # ── Protokół Yaesu GS-232A ────────────────────────────────────────────────
    def _yaesu_status_pkt(self) -> bytes:
        return b"C\r"

    def _yaesu_set_pkt(self, az: float) -> bytes:
        """SET: "Mddd\\r" — 3-cyfrowy azymut 000-360, bez znaku."""
        A = str(int(round(az)) % 360).zfill(3)
        return f"M{A}\r".encode("ascii")

    def _yaesu_stop_pkt(self) -> bytes:
        return b"S\r"

    def _yaesu_decode(self, buf: bytes) -> float | None:
        """Parsuj odpowiedz "+0ddd\\r" (znak + 4 cyfry azymutu)."""
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

    # ── Dyspozytor protokolu (uzywany przez _write/_read_pos/_move_worker) ─────
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
        """Wyślij STATUS, odczytaj odpowiedź (format zalezny od protokolu -
        patrz _status_pkt/_decode). W symulacji zwraca self.az."""
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
            proto_label = "Yaesu GS-232A" if self.protocol == "yaesu" else "SPID"
            print(f"[rotator] {self.name} {proto_label} @ {self.port} {self.speed}bd — az={self.az:.0f}°")
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
