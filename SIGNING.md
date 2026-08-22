# Code Signing Policy

## What gets signed

The Windows installer produced by the release build:
`HAMCTRL-Setup.exe` (built with Inno Setup, bundles the PyInstaller-built
`HAM-RADIO-CTRL.exe` and the Rust `ham_audio.exe`). This is the only
artifact end users download and run — see the
[latest release](https://github.com/SQ3MZM/HAMCTRL/releases/latest).

## Build process

1. `python build_server.py` — PyInstaller packages the Python backend
   (`webapp.py` and dependencies) plus the prebuilt `ham_audio.exe` into
   `dist/HAM-RADIO-CTRL.exe`.
2. `ham_audio/` (the Rust real-time audio/FT8 engine) is built separately
   with `cargo build --release` and its output copied to the repo root as
   `ham_audio.exe` before step 1, so PyInstaller can bundle it.
3. Inno Setup (`HAMCTRL-installer.iss`) compiles `dist/HAM-RADIO-CTRL.exe`
   into the single-file installer `Output/HAMCTRL-Setup.exe`.
4. `publish_release.ps1` uploads that installer to the project's GitHub
   Releases (the rolling `latest` tag).

All of the above runs on the maintainer's own build machine (not a
GitHub-hosted CI runner). Source, build scripts, and release history are
all public in this repository, so the full path from source to shipped
binary is auditable.

## Roles

HAMCTRL currently has a single maintainer (SQ3MZM / github.com/SQ3MZM),
who acts as Author, Reviewer, and Approver for every release. External
contributions go through pull requests reviewed before merge; nothing
gets built or signed from a branch the maintainer didn't review.

## Distribution

Only via [GitHub Releases](https://github.com/SQ3MZM/HAMCTRL/releases)
on this repository. HAMCTRL is never distributed through any third-party
download site, mirror, or store.
