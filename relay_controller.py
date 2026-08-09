"""
relay_controller.py — Kontroler przekaznikow Arduino (SP5IOU SDR220 emulator).

Komunikuje sie z Arduino przez port szeregowy protokolem tekstowym:
  SKn  - zalacz przekaznik n (n=0..7)
  RKn  - wylacz przekaznik n
  RPK  - status wszystkich przekaznikow (binary)
  IDN? - identyfikacja

Obsluguje dwa tryby pracy per przekaznik (skonfigurowane w webapp.py):
  - "manual"    - toggle on/off (uzytkownik klika, stan sie zmienia)
  - "momentary" - impuls: zalacz -> czekaj T sekund -> wylacz automatycznie

Bezpieczenstwo:
  - Max czas impulsu: 10.0s (hardcoded MAX_PULSE_S)
  - Jesli serwer padnie w trakcie impulsu, timer nie dokonczy - przekaznik
    zostanie w pozycji ON. Zalecany fail-safe hardware'owy albo watchdog Arduino.
"""

import asyncio
import serial
import serial.tools.list_ports


MAX_PULSE_S = 10.0
RELAY_COUNT = 8


class RelayController:
    """Kontroler przekaznikow przez Arduino UART."""

    def __init__(self, port: str, baudrate: int = 9600):
        self.port = port
        self.baudrate = baudrate
        self._serial: serial.Serial | None = None
        self._states: list[bool] = [False] * RELAY_COUNT  # cached state
        self._lock = asyncio.Lock()
        self._pulse_tasks: dict[int, asyncio.Task] = {}
        self._connected = False

    async def connect(self) -> bool:
        """Otwiera port szeregowy. Zwraca True jesli OK."""
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=1.0,
                write_timeout=1.0,
            )
            # Arduino resetuje sie przy otwarciu portu - poczekaj na boot
            await asyncio.sleep(2.0)
            self._serial.reset_input_buffer()
            self._connected = True
            print(f"[relay] Polaczono z Arduino na {self.port} @ {self.baudrate} bps")
            # Odczytaj poczatkowy stan
            await self.read_all_states()
            return True
        except Exception as e:
            print(f"[relay] Blad polaczenia z {self.port}: {e}")
            self._connected = False
            return False

    def is_connected(self) -> bool:
        return self._connected and self._serial is not None and self._serial.is_open

    async def disconnect(self):
        # Zatrzymaj wszystkie aktywne pulse taski
        for task in list(self._pulse_tasks.values()):
            task.cancel()
        self._pulse_tasks.clear()
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
        self._connected = False

    async def _send_command(self, cmd: str) -> str:
        """Wysyla komende i odczytuje odpowiedz. Zwraca odpowiedz lub pusty string."""
        if not self.is_connected():
            return ""
        async with self._lock:
            try:
                # Wyslij komende (Arduino oczekuje CR/LF)
                self._serial.write((cmd + "\r\n").encode("ascii"))
                self._serial.flush()
                # Krotkie czekanie na odpowiedz
                await asyncio.sleep(0.05)
                if self._serial.in_waiting > 0:
                    data = self._serial.read(self._serial.in_waiting)
                    return data.decode("ascii", errors="ignore").strip()
                return ""
            except Exception as e:
                print(f"[relay] Blad wysylania '{cmd}': {e}")
                self._connected = False
                return ""

    async def set_on(self, relay_num: int) -> bool:
        """Zalacz przekaznik (SKn)."""
        if not 0 <= relay_num < RELAY_COUNT:
            return False
        await self._send_command(f"SK{relay_num}")
        self._states[relay_num] = True
        return True

    async def set_off(self, relay_num: int) -> bool:
        """Wylacz przekaznik (RKn)."""
        if not 0 <= relay_num < RELAY_COUNT:
            return False
        await self._send_command(f"RK{relay_num}")
        self._states[relay_num] = False
        return True

    async def toggle(self, relay_num: int) -> bool:
        """Zmien stan przekaznika na przeciwny."""
        if not 0 <= relay_num < RELAY_COUNT:
            return False
        if self._states[relay_num]:
            return await self.set_off(relay_num)
        else:
            return await self.set_on(relay_num)

    async def pulse(self, relay_num: int, duration_s: float) -> bool:
        """Impuls: zalacz, poczekaj, wylacz. Anuluje poprzedni pulse jesli istnieje.
        Zabezpieczenie: duration_s clampowane do <0, MAX_PULSE_S>."""
        if not 0 <= relay_num < RELAY_COUNT:
            return False
        duration_s = max(0.0, min(MAX_PULSE_S, float(duration_s)))
        # Anuluj poprzedni pulse task dla tego przekaznika
        prev = self._pulse_tasks.pop(relay_num, None)
        if prev and not prev.done():
            prev.cancel()

        async def _pulse_task():
            try:
                await self.set_on(relay_num)
                await asyncio.sleep(duration_s)
                await self.set_off(relay_num)
            except asyncio.CancelledError:
                # W przypadku anulowania - wylacz na wszelki wypadek
                await self.set_off(relay_num)
                raise

        task = asyncio.create_task(_pulse_task())
        self._pulse_tasks[relay_num] = task
        return True

    async def read_all_states(self) -> list[bool]:
        """Odczytaj stan wszystkich przekaznikow z Arduino (RPK)."""
        resp = await self._send_command("RPK")
        # Arduino zwraca binarny string np. "10110000" (bit 0 = REL0)
        # Format moze zawierac dodatkowe znaki "String: RPK\r\n10110000"
        if resp:
            # Znajdz ciag 8 znakow 0/1
            for line in resp.split("\n"):
                line = line.strip()
                if len(line) >= 8 and all(c in "01" for c in line[-8:]):
                    bits = line[-8:]
                    # LSB first w SDR220: bit 0 = REL0
                    self._states = [bits[7 - i] == "1" for i in range(RELAY_COUNT)]
                    break
        return list(self._states)

    def get_states(self) -> list[bool]:
        """Zwroc aktualny cache stanow (bez zapytania Arduino)."""
        return list(self._states)

    def get_state(self, relay_num: int) -> bool:
        if 0 <= relay_num < RELAY_COUNT:
            return self._states[relay_num]
        return False


def list_serial_ports() -> list[dict]:
    """Zwroc liste dostepnych portow szeregowych (do UI konfiguracji)."""
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append({
            "device": p.device,
            "description": p.description or "",
            "manufacturer": p.manufacturer or "",
        })
    return ports
