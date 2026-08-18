#!/usr/bin/env python3
"""
build_server.py — builds HAM-RADIO-CTRL.exe (the server) via PyInstaller.

RUN (on Windows, in the server code directory):
    py build_server.py

Steps:
  1. Check for Python 3.10+
  2. Install dependencies (pyinstaller + server libraries)
  3. Check for ham_audio.exe + opus DLLs (warn if missing)
  4. Run PyInstaller against hamctrl.spec
  5. Report the result: dist/HAM-RADIO-CTRL.exe

Output: dist/HAM-RADIO-CTRL.exe (approx. 150-250 MB - aiortc+scipy are large)
"""
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
DIST = BASE / "dist"

# Required dependencies for the server to run
REQUIRED = ["pyinstaller", "aiohttp", "pyserial", "numpy", "scipy"]
# Optional - audio/WebRTC (the server works without them, just no audio streaming)
OPTIONAL = ["aiortc", "pyaudio", "opuslib", "cryptography"]


def check_python():
    v = sys.version_info
    if v < (3, 10):
        print(f"[X] Python 3.10+ required, you have {v.major}.{v.minor}")
        return False
    print(f"[OK] Python {v.major}.{v.minor}.{v.micro}")
    return True


def install_deps():
    print("\n[*] Installing required dependencies...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "--upgrade", *REQUIRED])
        print("[OK] Required dependencies installed")
    except subprocess.CalledProcessError as e:
        print(f"[X] pip failed (required): {e}")
        return False

    print("\n[*] Installing optional dependencies (audio/WebRTC)...")
    print("    (if one fails, the server still works without that feature)")
    for pkg in OPTIONAL:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "--upgrade", pkg])
            print(f"[OK] {pkg}")
        except subprocess.CalledProcessError:
            print(f"[!] {pkg} - failed (skipping, the EXE will build without it)")
    return True


def check_binaries():
    print("\n[*] Checking external binaries...")
    ham = BASE / "ham_audio.exe"
    if ham.exists():
        mb = ham.stat().st_size / 1024 / 1024
        print(f"[OK] ham_audio.exe ({mb:.1f} MB) - will be bundled")
    else:
        print("[!] ham_audio.exe MISSING - the EXE will build, but audio won't work")
        print("    Copy ham_audio.exe into this directory before building.")

    opus_found = []
    for name in ("libopus.dll", "opus.dll", "opuslib.dll"):
        if (BASE / name).exists():
            print(f"[OK] {name} - bundled")
            opus_found.append(name)
    if not opus_found:
        print("[!] Opus DLLs MISSING - Opus audio may not work")
        print("    Copy libopus.dll, opus.dll, opuslib.dll into this directory.")
    else:
        print(f"[OK] opus: {len(opus_found)}/3 DLLs found")

    return True  # don't block - the build can proceed without audio


def patch_scipy():
    """
    Fix the scipy.stats._distn_infrastructure 'del obj' NameError that
    crashes the app under PyInstaller + Python 3.12.

    The file ends with:
        for obj in [...]:
            exec('del ' + obj)
        del obj          # <- this blows up
    We replace 'del obj' with a safe version in the NAMESPACE (try/except).
    This patches the installed scipy package (not our project code).
    """
    print("\n[*] Checking/patching the scipy 'del obj' bug (Py3.12 + PyInstaller)...")
    try:
        import scipy.stats._distn_infrastructure as _m
        fpath = Path(_m.__file__)
    except Exception as e:
        print(f"[!] Could not find scipy to patch: {e}")
        return True  # don't block

    try:
        src = fpath.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[!] Could not read {fpath}: {e}")
        return True

    # Look for a lone 'del obj' (after the docstring-cleanup loop)
    if "\ndel obj\n" in src and "try:\n    del obj\nexcept NameError" not in src:
        patched = src.replace(
            "\ndel obj\n",
            "\ntry:\n    del obj\nexcept NameError:\n    pass\n",
            1,
        )
        try:
            fpath.write_text(patched, encoding="utf-8")
            print(f"[OK] Patched {fpath.name} (del obj -> try/except)")
        except Exception as e:
            print(f"[!] Could not write the patch (maybe missing permissions): {e}")
            print("    Run as administrator or patch it manually.")
    else:
        print("[OK] scipy already patched or a different version - skipping")
    return True


def build():
    spec = BASE / "hamctrl.spec"
    if not spec.exists():
        print(f"[X] {spec} missing")
        return False

    # Clean the previous build
    import shutil
    for d in ("build", "dist", "__pycache__"):
        p = BASE / d
        if p.exists():
            print(f"[*] Cleaning {d}/")
            shutil.rmtree(p, ignore_errors=True)

    print("\n[*] PyInstaller build (this will take a few minutes)...")
    try:
        subprocess.check_call([sys.executable, "-m", "PyInstaller",
                               "--clean", "--noconfirm", str(spec)])
    except subprocess.CalledProcessError as e:
        print(f"[X] PyInstaller failed: {e}")
        return False

    exe = DIST / "HAM-RADIO-CTRL.exe"
    if not exe.exists():
        print(f"[X] {exe} missing after the build")
        return False
    mb = exe.stat().st_size / 1024 / 1024
    print(f"\n[OK] {exe} ({mb:.1f} MB)")
    return True


def main():
    print("=" * 56)
    print("  HAM RADIO CTRL - server build (EXE)")
    print("=" * 56)

    if not check_python():
        return 1
    if not install_deps():
        return 1
    check_binaries()
    patch_scipy()
    if not build():
        return 1

    print("\n" + "=" * 56)
    print("  BUILD SUCCESSFUL!")
    print("=" * 56)
    print(f"Output: {DIST / 'HAM-RADIO-CTRL.exe'}")
    print("\nTest:")
    print("  1. Run the EXE on a machine WITHOUT Python")
    print("  2. It should open a browser at https://localhost:8001")
    print("  3. Log in as admin / Admin1234! -> the password-change wizard")
    print("\nUser data (config.json, users.json, .env) is created NEXT TO the exe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
