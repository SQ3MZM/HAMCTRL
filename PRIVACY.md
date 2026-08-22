# Privacy Policy

HAMCTRL is **self-hosted software**, not a service. There is no
HAMCTRL-operated server anywhere — you (or whoever runs the installer)
run your own private copy on your own PC, connected to your own radio.
The project and its maintainer never receive any data from your
installation.

## What data exists, and where it lives

Everything HAMCTRL stores lives locally, next to the installed app, in
`%APPDATA%\HAMCTRL` on the machine it runs on:

- **Accounts**: usernames and password hashes for the operators you
  invite to use your station (`users.json`). Only you control who gets
  an account.
- **QSO log**: your contact log (`qso.db`).
- **Radio/app configuration** (`config.json`), including any
  credentials you enter for optional third-party integrations (see
  below) — these are encrypted at rest.
- **TLS certificate** for serving the web UI over HTTPS, if you use one.

None of this ever leaves your machine unless *you* explicitly enable one
of the integrations below.

## Optional outbound connections (opt-in, admin-configured)

These only run if the admin of a given installation turns them on and
enters their own credentials/settings for them:

- **CI-V to your radio** — local serial connection, nothing leaves the PC.
- **DX Cluster client** — connects to a cluster server of your choosing.
- **QSO log push** to CloudLog / WaveLog, and callsign lookups against
  QRZ.com / HamQTH — uses your own account on those services.
- **Internet tunnel** (optional, for remote access to your own station
  from outside your LAN) — routes through a tunnel provider you
  configure yourself.
- **Update check** — on startup, the app can check GitHub Releases for
  this repository for a newer version. This is a plain,
  unauthenticated request for public release metadata; no information
  about you, your station, or your usage is sent.

## What HAMCTRL never does

No analytics, no telemetry, no crash reporting, no tracking of any kind
sent back to the project or its maintainer. The `"telemetry"` message
type you may see in the source refers only to the local WebSocket
broadcast of radio status (frequency, mode, S-meter) to your own
browser — it never leaves your network.

## Multi-user installations

If an admin invites other operators to share their station, that admin
is responsible for those users' account data on their own installation,
same as running any other self-hosted multi-user software. The project
itself has no visibility into, or access to, any installation's data.

## Questions

Open an issue on [github.com/SQ3MZM/HAMCTRL](https://github.com/SQ3MZM/HAMCTRL).
