#!/usr/bin/env python3
"""
build_server.py — budowanie HAM-RADIO-CTRL.exe (serwer) przez PyInstaller.

URUCHOMIENIE (na Windows, w katalogu z kodem serwera):
    py build_server.py

Kroki:
  1. Sprawdz Python 3.10+
  2. Zainstaluj zaleznosci (pyinstaller + biblioteki serwera)
  3. Sprawdz obecnosc ham_audio.exe + opus DLL (ostrzez jesli brak)
  4. Uruchom PyInstaller wg hamctrl.spec
  5. Pokaz wynik: dist/HAM-RADIO-CTRL.exe

Wynik: dist/HAM-RADIO-CTRL.exe (szac. 150-250 MB - aiortc+scipy sa duze)
"""
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).parent
DIST = BASE / "dist"

# Zaleznosci wymagane do dzialania serwera
REQUIRED = ["pyinstaller", "aiohttp", "pyserial", "numpy", "scipy"]
# Opcjonalne - audio/WebRTC (bez nich serwer dziala, ale bez streamingu audio)
OPTIONAL = ["aiortc", "pyaudio", "opuslib", "cryptography"]


def check_python():
    v = sys.version_info
    if v < (3, 10):
        print(f"[X] Wymagany Python 3.10+, masz {v.major}.{v.minor}")
        return False
    print(f"[OK] Python {v.major}.{v.minor}.{v.micro}")
    return True


def install_deps():
    print("\n[*] Instaluje zaleznosci wymagane...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               "--upgrade", *REQUIRED])
        print("[OK] Zaleznosci wymagane zainstalowane")
    except subprocess.CalledProcessError as e:
        print(f"[X] pip fail (wymagane): {e}")
        return False

    print("\n[*] Instaluje zaleznosci opcjonalne (audio/WebRTC)...")
    print("    (jesli ktoras padnie, serwer dziala bez tej funkcji)")
    for pkg in OPTIONAL:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "--upgrade", pkg])
            print(f"[OK] {pkg}")
        except subprocess.CalledProcessError:
            print(f"[!] {pkg} - nie udalo sie (pomijam, EXE zbuduje sie bez)")
    return True


def check_binaries():
    print("\n[*] Sprawdzam binaria zewnetrzne...")
    ham = BASE / "ham_audio.exe"
    if ham.exists():
        mb = ham.stat().st_size / 1024 / 1024
        print(f"[OK] ham_audio.exe ({mb:.1f} MB) - zostanie zbundlowany")
    else:
        print("[!] BRAK ham_audio.exe - EXE zbuduje sie, ale audio nie zadziala")
        print("    Skopiuj ham_audio.exe do tego katalogu przed buildem.")

    opus_found = []
    for name in ("libopus.dll", "opus.dll", "opuslib.dll"):
        if (BASE / name).exists():
            print(f"[OK] {name} - zbundlowany")
            opus_found.append(name)
    if not opus_found:
        print("[!] BRAK opus DLL - audio Opus moze nie dzialac")
        print("    Skopiuj libopus.dll, opus.dll, opuslib.dll do tego katalogu.")
    else:
        print(f"[OK] opus: {len(opus_found)}/3 DLL znalezione")

    return True  # nie blokuj - build moze przejsc bez audio


def patch_scipy():
    """
    Napraw bug scipy.stats._distn_infrastructure 'del obj' NameError, ktory
    wywala aplikacje pod PyInstaller + Python 3.12.

    Plik konczy sie:
        for obj in [...]:
            exec('del ' + obj)
        del obj          # <- to wybucha
    Zamieniamy 'del obj' na bezpieczna wersje w NAMESPACE (try/except).
    Patchujemy zainstalowany pakiet scipy (nie kod projektu).
    """
    print("\n[*] Sprawdzam/lataam bug scipy 'del obj' (Py3.12 + PyInstaller)...")
    try:
        import scipy.stats._distn_infrastructure as _m
        fpath = Path(_m.__file__)
    except Exception as e:
        print(f"[!] Nie moge znalezc scipy do zalatania: {e}")
        return True  # nie blokuj

    try:
        src = fpath.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[!] Nie moge odczytac {fpath}: {e}")
        return True

    # Szukamy samotnego 'del obj' (po petli czyszczacej docstringi)
    if "\ndel obj\n" in src and "try:\n    del obj\nexcept NameError" not in src:
        patched = src.replace(
            "\ndel obj\n",
            "\ntry:\n    del obj\nexcept NameError:\n    pass\n",
            1,
        )
        try:
            fpath.write_text(patched, encoding="utf-8")
            print(f"[OK] Zalatano {fpath.name} (del obj -> try/except)")
        except Exception as e:
            print(f"[!] Nie moge zapisac patcha (moze brak praw): {e}")
            print("    Uruchom jako administrator albo zalataj recznie.")
    else:
        print("[OK] scipy juz zalatane lub inna wersja - pomijam")
    return True


def build():
    spec = BASE / "hamctrl.spec"
    if not spec.exists():
        print(f"[X] Brak {spec}")
        return False

    # Wyczysc poprzedni build
    import shutil
    for d in ("build", "dist", "__pycache__"):
        p = BASE / d
        if p.exists():
            print(f"[*] Czyszcze {d}/")
            shutil.rmtree(p, ignore_errors=True)

    print("\n[*] PyInstaller build (to potrwa kilka minut)...")
    try:
        subprocess.check_call([sys.executable, "-m", "PyInstaller",
                               "--clean", "--noconfirm", str(spec)])
    except subprocess.CalledProcessError as e:
        print(f"[X] PyInstaller fail: {e}")
        return False

    exe = DIST / "HAM-RADIO-CTRL.exe"
    if not exe.exists():
        print(f"[X] Brak {exe} po buildzie")
        return False
    mb = exe.stat().st_size / 1024 / 1024
    print(f"\n[OK] {exe} ({mb:.1f} MB)")
    return True


def main():
    print("=" * 56)
    print("  HAM RADIO CTRL - build serwera (EXE)")
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
    print("  BUILD SUKCES!")
    print("=" * 56)
    print(f"Wynik: {DIST / 'HAM-RADIO-CTRL.exe'}")
    print("\nTest:")
    print("  1. Uruchom EXE na maszynie BEZ Pythona")
    print("  2. Powinno otworzyc przegladarke na https://localhost:8001")
    print("  3. Zaloguj admin / Admin1234! -> kreator zmiany hasla")
    print("\nDane usera (config.json, users.json, .env) tworza sie OBOK exe.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
