"""
relay_controller.py — Arduino relay controller (SP5IOU SDR220 emulator).

Talks to the Arduino over a serial port using a text protocol:
  SKn  - turn relay n on (n=0..7)
  RKn  - turn relay n off
  RPK  - status of all relays (binary)
  IDN? - identification

Supports two operating modes per relay (configured in webapp.py):
  - "manual"    - toggle on/off (the user clicks, the state changes)
  - "momentary" - pulse: turn on -> wait T seconds -> turn off automatically

Safety:
  - Max pulse duration: 10.0s (hardcoded MAX_PULSE_S)
  - If the server crashes mid-pulse, the timer won't complete — the relay
    stays ON. A hardware fail-safe or an Arduino watchdog is recommended.
"""

import asyncio
import serial
import serial.tools.list_ports


MAX_PULSE_S = 10.0
RELAY_COUNT = 8


class RelayController:
    """Relay controller over Arduino UART."""

    def __init__(self, port: str, baudrate: int = 9600):
        self.port = port
        self.baudrate = baudrate
        self._serial: serial.Serial | None = None
        self._states: list[bool] = [False] * RELAY_COUNT  # cached state
        self._lock = asyncio.Lock()
        self._pulse_tasks: dict[int, asyncio.Task] = {}
        self._connected = False

    async def connect(self) -> bool:
        """Opens the serial port. Returns True on success."""
        try:
            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=1.0,
                write_timeout=1.0,
            )
            # The Arduino resets when the port is opened - wait for it to boot
            await asyncio.sleep(2.0)
            self._serial.reset_input_buffer()
            self._connected = True
            print(f"[relay] Connected to Arduino on {self.port} @ {self.baudrate} bps")
            # Read the initial state
            await self.read_all_states()
            return True
        except Exception as e:
            print(f"[relay] Connection error on {self.port}: {e}")
            self._connected = False
            return False

    def is_connected(self) -> bool:
        return self._connected and self._serial is not None and self._serial.is_open

    async def disconnect(self):
        # Stop all active pulse tasks
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
        """Sends a command and reads the reply. Returns the reply or an empty string."""
        if not self.is_connected():
            return ""
        async with self._lock:
            try:
                # Send the command (the Arduino expects CR/LF)
                self._serial.write((cmd + "\r\n").encode("ascii"))
                self._serial.flush()
                # Brief wait for the reply
                await asyncio.sleep(0.05)
                if self._serial.in_waiting > 0:
                    data = self._serial.read(self._serial.in_waiting)
                    return data.decode("ascii", errors="ignore").strip()
                return ""
            except Exception as e:
                print(f"[relay] Error sending '{cmd}': {e}")
                self._connected = False
                return ""

    async def set_on(self, relay_num: int) -> bool:
        """Turn a relay on (SKn)."""
        if not 0 <= relay_num < RELAY_COUNT:
            return False
        await self._send_command(f"SK{relay_num}")
        self._states[relay_num] = True
        return True

    async def set_off(self, relay_num: int) -> bool:
        """Turn a relay off (RKn)."""
        if not 0 <= relay_num < RELAY_COUNT:
            return False
        await self._send_command(f"RK{relay_num}")
        self._states[relay_num] = False
        return True

    async def toggle(self, relay_num: int) -> bool:
        """Flip a relay's state."""
        if not 0 <= relay_num < RELAY_COUNT:
            return False
        if self._states[relay_num]:
            return await self.set_off(relay_num)
        else:
            return await self.set_on(relay_num)

    async def pulse(self, relay_num: int, duration_s: float) -> bool:
        """Pulse: turn on, wait, turn off. Cancels a previous pulse if one exists.
        Safety: duration_s is clamped to <0, MAX_PULSE_S>."""
        if not 0 <= relay_num < RELAY_COUNT:
            return False
        duration_s = max(0.0, min(MAX_PULSE_S, float(duration_s)))
        # Cancel any previous pulse task for this relay
        prev = self._pulse_tasks.pop(relay_num, None)
        if prev and not prev.done():
            prev.cancel()

        async def _pulse_task():
            try:
                await self.set_on(relay_num)
                await asyncio.sleep(duration_s)
                await self.set_off(relay_num)
            except asyncio.CancelledError:
                # If cancelled - turn off just in case
                await self.set_off(relay_num)
                raise

        task = asyncio.create_task(_pulse_task())
        self._pulse_tasks[relay_num] = task
        return True

    async def read_all_states(self) -> list[bool]:
        """Read the state of all relays from the Arduino (RPK)."""
        resp = await self._send_command("RPK")
        # The Arduino returns a binary string, e.g. "10110000" (bit 0 = REL0)
        # The format may include extra characters: "String: RPK\r\n10110000"
        if resp:
            # Find a run of 8 '0'/'1' characters
            for line in resp.split("\n"):
                line = line.strip()
                if len(line) >= 8 and all(c in "01" for c in line[-8:]):
                    bits = line[-8:]
                    # LSB first in the SDR220: bit 0 = REL0
                    self._states = [bits[7 - i] == "1" for i in range(RELAY_COUNT)]
                    break
        return list(self._states)

    def get_states(self) -> list[bool]:
        """Return the current cached states (without querying the Arduino)."""
        return list(self._states)

    def get_state(self, relay_num: int) -> bool:
        if 0 <= relay_num < RELAY_COUNT:
            return self._states[relay_num]
        return False


def list_serial_ports() -> list[dict]:
    """Return the list of available serial ports (for the config UI)."""
    ports = []
    for p in serial.tools.list_ports.comports():
        ports.append({
            "device": p.device,
            "description": p.description or "",
            "manufacturer": p.manufacturer or "",
        })
    return ports
