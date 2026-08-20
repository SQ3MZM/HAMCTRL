# HAMCTRL — Architecture & Developer Guide

This document exists so a new contributor — human or an AI coding
assistant — can get oriented in this codebase quickly, without having to
read every file first. It complements `CLAUDE.md` (repo-root, written for
Claude Code specifically) but is meant for anyone/anything.

## What this project is

A self-hosted web application for remote-controlling an Icom amateur
radio transceiver (reference/tested model: **IC-7300**) over CI-V, plus a
**complete, from-scratch FT8/FT4 digital-mode implementation** (decoder
and encoder) — it does not use or require WSJT-X/JTDX. Multiple operators
can share one physical radio through the web UI, with an ownership/lock
system so only one person transmits at a time.

Core features: VFO/mode/PTT control, band scope + waterfall, FT8/FT4 RX
(own decoder) and TX (own encoder) with a full auto-QSO state machine
(Call 1st queue, Fox/Hound DXpedition mode, MSHV multistream support),
CW keyer with a DeepCW audio-to-text assist layer, rotator control
(Yaesu GS-232A / SPID), DX cluster client, QSO log with CloudLog/WaveLog
push and ADIF import/export, a COM-port bridge for third-party CI-V
clients (CW Skimmer, Ham Radio Deluxe, Logger32), WebRTC-based low-latency
audio for SSB, multi-user accounts with role-based permissions, and PL/EN
UI localization.

## Stack

| Layer | Tech | Where |
|---|---|---|
| Backend | Python 3.12, `aiohttp` (async HTTP + WebSocket server) | one large file, `webapp.py` (~7000 lines) |
| Radio control | Custom CI-V driver over serial | `civ.py`, per-model profiles in `rigs/civ_profiles.py` |
| Frontend | Vanilla JS (no framework, no build step) | `public/js/*.js`, single `public/index.html` |
| Real-time audio | Separate Rust process (`ham_audio.exe`), TCP to Python | `ham_audio/` (Rust crate), bridged via `ft8_rust_receiver.py` / `audio_bridge.py`-style glue in `webapp.py` |
| Packaging | PyInstaller → single EXE, Inno Setup → Windows installer | `hamctrl.spec`, `HAMCTRL-installer.iss`, `build_server.py` |

There is no database server — QSO logs live in a local SQLite file
(`qso_db.py`), user accounts and config in JSON files, all under
`%APPDATA%\HAMCTRL` at runtime (never in the repo — see `.gitignore`).

## Why Python *and* Rust

- **RX (receiving) FT8/FT4 decoding** runs in Rust
  (`ham_audio/src/decode/`) for performance — FFT-heavy work on a live
  audio stream. Python receives already-decoded results over a local TCP
  socket (`ft8_rust_receiver.py`).
- **TX (transmitting) FT8/FT4** — encoding and PCM generation — is pure
  Python (`ft8_encoder.py`, `ft4_encoder.py`). The whole TX path (window
  timing, Fake Split, level scaling) lives in `webapp.py`. If you're
  debugging a transmit issue, start in Python, not Rust.
- Both processes talk to the same physical sound card; Rust owns the
  low-latency audio I/O (`cpal`) and streams PCM to/from Python over TCP.

## The FT8/FT4 pipeline, end to end

```
Rust FFT/decode  →  ft8_rust_receiver.py (TCP)  →  webapp.py::_ft8_rx_loop
   →  _process_auto_qso() → qso_engine.py (pure state machine, no I/O)
   →  WS broadcast (wsjtx_decode / auto_qso_status / ...)
   →  public/js/wsjtx.js (renders the RX window + automation panel)
```

`qso_engine.py` is the single-QSO state machine plus the "Call 1st"
queue. It has no network/asyncio dependency and a large, fast test suite
(`test_qso_engine.py`, run as a plain script — **not** pytest):

```bash
PYTHONIOENCODING=utf-8 python test_qso_engine.py
```

(`PYTHONIOENCODING=utf-8` matters on Windows — the default console
codepage isn't UTF-8 and will crash on non-ASCII output otherwise.) Any
fix to the QSO engine or the automation logic in `webapp.py` should get a
matching test here before it's considered done.

TX (`_ft8_tx_sequence` / `_ft8_tx_sequence_inner` in `webapp.py`) is one
shared code path for both manual and automated transmissions — window
sync to the UTC 15s/7.5s boundary, PTT, and a mutex
(`self._ft8_tx_lock`) that serializes every transmission so a manual
click and the automation never key up at the same time.

## CI-V driver notes (`civ.py`)

- Talks directly to the radio over serial (no rigctld/Hamlib dependency
  for the reference IC-7300 path — other/older radios can go through
  `rigctld` via `rigcat.py`, kept as a separate, unmodified code path).
- **A whole family of CI-V commands shares one response code**, only
  distinguished by the first payload byte: `15 xx` (S-meter/ALC/PWR/SWR/
  VOLT), `1A xx` (filter width/DATA mode), `14 xx` (every Set Level
  slider). `_transact()` takes an optional `sub=` parameter for exactly
  this reason — pass it for any new command in one of these families, or
  a late response to command A can get misattributed to command B.
- `set_freq()` is deliberately fire-and-forget (no ACK wait) for a snappy
  click-to-tune UX; it also sets `self.freq` optimistically *before* the
  write is attempted, so that field can't be used to verify a write
  actually landed — use `get_freq_live()` for an honest re-read from the
  radio when a caller needs to *confirm* a retune (see `ft8_qsy` in
  `webapp.py`).
- Runs in **SIMULATION mode** automatically when no serial port is
  available or the radio doesn't answer CI-V — this is how the app runs
  in CI/dev/screenshots without real hardware attached.

## Frontend conventions

- No build step, no bundler, no framework. Every `public/js/*.js` file is
  a self-contained module, usually an IIFE exporting one `window.Xyz`
  object (`window.WSJTX`, `window.QSOLog`, `window.UI`, ...).
- `public/js/ws.js` owns the single WebSocket connection and dispatches
  incoming messages by `type` to whichever module's `handleWS()` wants
  them — when adding a new server→client message, wire it in there.
- `public/js/state.js` defines `window.AppState`, the shared in-memory
  state object most modules read from (`S = window.AppState` at the top
  of each file).
- **Recurring bug pattern**: the frontend sends/handles a WS message type
  the backend never implemented (or vice versa) — symptom is "the button
  does nothing." Always check both sides: `WS.send({type: ...})` in JS
  vs. `elif t == "..."` in `_ws_msg()` in `webapp.py`.
- **Cache busting**: JS/CSS files are loaded with `?v=...` query strings
  in `index.html`. Bump the version after editing a `public/js/*.js` or
  `public/css/style.css` file, or the browser serves the old cached copy
  and a real fix looks like it "didn't work."

## Build & release

```
webapp.py (BUILD-... marker printed on startup/TX — bump it on real changes)
  → build_server.py  (PyInstaller, --clean, produces dist/HAM-RADIO-CTRL.exe)
  → HAMCTRL-installer.iss compiled with Inno Setup (ISCC.exe) → Output/HAMCTRL-Setup.exe
  → publish_release.ps1 (gh CLI, uploads to the GitHub "latest" release tag)
  → update.bat on a remote machine downloads and installs the new release
```

**A Python source change alone does nothing** for anyone running the
packaged EXE — it has to go through this whole pipeline. If you rebuild
and the startup log still shows an old `BUILD-...` marker, PyInstaller
packaged the wrong `webapp.py` (wrong working directory/import) —
nothing else will work until that's fixed.

## Where secrets and per-install data live

`config.json`, `users.json`, TLS certs, `qso.db` — all under
`%APPDATA%\HAMCTRL`, created at first run, **never committed** (see
`.gitignore`). Third-party credentials (CloudLog/QRZ/HamQTH/DX-Cluster/
tunnel tokens) inside `users.json` are encrypted at rest via
`crypto_secrets.py` (key derived from the server's own JWT secret).

## Workshop conventions worth keeping

- Fix one thing per session/PR. This file is already large and dense —
  targeted patches are worth more here than refactors.
- Don't add abstractions, config flags, or error handling for scenarios
  that can't happen. Trust internal invariants; validate only at real
  boundaries (user input, external APIs).
- A change touching the TX path (frequency, mode, PTT, audio level) is
  safety-relevant — it keys a real transmitter. Prefer verify-then-act
  over fire-and-forget where the existing code doesn't already have a
  documented reason not to (see the `set_freq()` note above for the one
  deliberate exception, and why).
