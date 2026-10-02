# Relay board firmware (ADMIN -> PRZEKAŹNIKI ARDUINO)

Firmware for the 8-relay board driven by HAMCTRL's **ADMIN -> PRZEKAŹNIKI
ARDUINO (SP5IOU SDR220)** panel (`relay_controller.py` on the HAMCTRL
side). Each relay can be used for anything wired to it — antenna
switching, amplifier bypass, powering an accessory, etc. — HAMCTRL only
turns outputs on/off (or pulses them); what each one is physically
connected to is up to you.

Original protocol/firmware design by **Marcin Boboli, SP5IOU**, emulating
the SDR220 relay-set command set. `relay_sp5iou.ino` in this folder is
that same firmware with one bug fixed (see the comment at the top of the
file) — otherwise unchanged.

## 1. Hardware

- Any Arduino with 8 free digital output pins (Uno, Nano, Pro Mini...).
- An 8-channel relay module. The firmware drives a pin **HIGH on `SKn`
  (energize) and LOW on `RKn`** — there's no polarity-inversion setting
  anywhere in HAMCTRL or in this firmware. Most common 8-channel relay
  modules are **active-LOW** (the relay actually clicks on when its
  input pin goes LOW), which means ON/OFF in the HAMCTRL UI will look
  inverted from what you'd expect. Check which kind yours is with a
  multimeter before wiring anything that matters.
- Relay module power: usually needs its own 5V supply for the relay
  coils, separate from the Arduino's own 5V if you're switching
  anything with real current draw — share only GND between the two.

## 2. Pin map (fixed in the firmware, `relay_sp5iou.ino`)

| Pin | Function |
|---|---|
| D2 | Relay 0 |
| D3 | Relay 1 |
| D4 | Relay 2 |
| D5 | Relay 3 |
| D6 | Relay 4 |
| D7 | Relay 5 |
| D8 | Relay 6 |
| D9 | Relay 7 |
| D13 | Status LED (lights briefly on every command received) |

D10–D12 and A0–A5 are used by the original SP5IOU design for digital/
analog inputs and events (see the protocol summary in the `.ino`) — not
read by HAMCTRL today, free for your own use if you need them.

## 3. Flashing

Arduino IDE, board = whatever matches your actual Arduino, baud rate
**9600** (fixed in the firmware — if you change it, also change it in
HAMCTRL's relay config). Upload `relay_sp5iou.ino` as-is.

## 4. Protocol (the subset HAMCTRL actually uses)

Text commands over serial, 9600 8N1, each ending with CR or LF:

| Command | Action |
|---|---|
| `SKn` | Energize relay `n` (0–7) |
| `RKn` | De-energize relay `n` (0–7) |
| `RPK` | Returns all 8 relay states as an 8-bit binary string |
| `RPKn` | Returns the state of relay `n` alone (`0`/`1`) |
| `PK` | Returns all 8 relay states as one decimal number |

The full original SP5IOU command set (analog inputs, event counter,
interrupts) is documented in the comment block at the top of
`relay_sp5iou.ino` — implemented in the firmware, just not driven by
HAMCTRL's own UI.

## 5. HAMCTRL-side setup

ADMIN tab -> **PRZEKAŹNIKI ARDUINO** -> enable, pick the COM port the
Arduino enumerated as, baud 9600, then name/configure each of the 8
relays (manual toggle or momentary pulse) and save. Per-user visibility
is granted in **Użytkownicy** with the "Przekaźnik 0..7" checkboxes.
