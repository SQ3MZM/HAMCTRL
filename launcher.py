#!/usr/bin/env python3
"""
launcher.py — punkt wejscia dla spakowanego EXE (HAM RADIO CTRL).

Zadania:
  1. Uruchomic serwer (server.main()) w watku tla.
  2. Poczekac az serwer zacznie odpowiadac na porcie HTTPS.
  3. Otworzyc przegladarke na https://localhost:<port>.
  4. Trzymac proces przy zyciu (serwer dziala w tle); Ctrl+C / zamkniecie
     okna konczy prace.

Dziala tak samo w trybie dev (python launcher.py) jak i spakowanym EXE.
"""
import sys
import time
import socket
import threading
import webbrowser
import os

# ── Limit watkow BLAS/numpy — KRYTYCZNE dla plynnosci audio ──────────────────
# numpy/scipy domyslnie odpalaja BLAS na WSZYSTKICH rdzeniach. Pojedyncza
# operacja FFT (filtr CW, waterfall, resampling) potrafila na moment zajac caly
# procesor (100% w /perf) i zatkac tor audio — stad chwilowe przyciecia dzwieku.
# Ograniczenie do 2 watkow: FFT dalej szybkie, ale zostaje CPU dla audio/sieci.
# MUSI byc przed pierwszym importem numpy (inaczej BLAS juz sie zainicjalizuje).
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "2")


def _setup_dll_path():
    """
    W spakowanym EXE (PyInstaller) natywne DLL (libopus, opus) trafiaja do
    _MEIPASS, ktory NIE jest w systemowym PATH. opuslib laduje libopus przez
    ctypes szukajac w PATH - wiec bez tego nie znajdzie DLL i audio padnie.
    Dodajemy katalog z DLL do sciezki wyszukiwania (Windows).
    """
    if not getattr(sys, "frozen", False):
        return  # tryb dev - DLL w systemie/venv
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    try:
        # Python 3.8+ na Windows: oficjalny sposob dodania sciezki DLL
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(base)
        # Dodatkowo do PATH (dla ctypes.util.find_library i starszych mechanizmow)
        os.environ["PATH"] = base + os.pathsep + os.environ.get("PATH", "")
    except Exception as e:
        print(f"[launcher] Ostrzezenie: nie moge ustawic sciezki DLL: {e}",
              flush=True)


_setup_dll_path()


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    """Czekaj az port zacznie akceptowac polaczenia (serwer wstal)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.3)
    return False


def _open_browser_when_ready(https_port: int, http_port: int):
    """
    W osobnym watku: poczekaj az serwer wstanie, potem otworz przegladarke.
    Preferuj HTTPS (mikrofon TX wymaga bezpiecznego kontekstu), z fallbackiem
    na HTTP jesli HTTPS nie wystartuje.
    """
    # Najpierw probuj HTTPS
    if _wait_for_port("127.0.0.1", https_port, timeout=30.0):
        url = f"https://localhost:{https_port}"
    elif _wait_for_port("127.0.0.1", http_port, timeout=5.0):
        url = f"http://localhost:{http_port}"
    else:
        print("[launcher] Serwer nie wstal w oczekiwanym czasie — "
              "otworz przegladarke recznie.", flush=True)
        return

    print(f"[launcher] Serwer gotowy — otwieram przegladarke: {url}", flush=True)
    print(f"[launcher] Jesli sie nie otworzylo, wejdz recznie: {url}", flush=True)
    # Krotka pauza zeby serwer w pelni zainicjalizowal handlery
    time.sleep(0.5)
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"[launcher] Nie moge otworzyc przegladarki: {e}", flush=True)


def _reset_admin():
    """
    Awaryjny reset hasla admina do domyslnego (Admin1234!). Uzywa sciezek
    z config (czyli trafia we wlasciwy users.json - obok EXE albo APPDATA).
    Po resecie admin loguje sie Admin1234! i kreator znowu wymusi zmiane.

    Wywolanie: HAM-RADIO-CTRL.exe --reset-admin
    """
    import json
    import hashlib
    try:
        from config import USR_F, ADMIN_PW
    except Exception as e:
        print(f"[reset] Nie moge zaladowac config: {e}", flush=True)
        return 1

    def hash_pw(pw):
        return hashlib.sha256(pw.encode()).hexdigest()

    print("=" * 56, flush=True)
    print("  RESET HASLA ADMINA", flush=True)
    print("=" * 56, flush=True)
    print(f"Plik uzytkownikow: {USR_F}", flush=True)

    try:
        if USR_F.exists():
            users = json.loads(USR_F.read_text(encoding="utf-8"))
        else:
            users = []
    except Exception as e:
        print(f"[reset] Blad odczytu users.json: {e}", flush=True)
        users = []

    # Znajdz admina, zresetuj haslo + flagi
    found = False
    for u in users:
        if u.get("role") == "admin" or u.get("username") == "admin":
            u["password"] = hash_pw(ADMIN_PW)
            u["active"] = True
            u["pw_changed"] = False           # kreator znowu wymusi zmiane
            u["pw_ver"] = int(u.get("pw_ver", 0)) + 1  # uniewaznij stare tokeny
            found = True
            print(f"[reset] Zresetowano admina '{u.get('username')}'", flush=True)

    if not found:
        # Brak admina - stworz nowego
        users.append({"id": "1", "username": "admin",
                      "password": hash_pw(ADMIN_PW), "role": "admin",
                      "active": True, "pw_changed": False, "pw_ver": 1})
        print("[reset] Utworzono nowe konto admin", flush=True)

    try:
        USR_F.write_text(json.dumps(users, indent=2, ensure_ascii=False),
                         encoding="utf-8")
        print(f"\n[reset] GOTOWE. Zaloguj sie:", flush=True)
        print(f"        login:  admin", flush=True)
        print(f"        haslo:  {ADMIN_PW}", flush=True)
        print(f"\n        Przy logowaniu kreator poprosi o nowe haslo.", flush=True)
    except Exception as e:
        print(f"[reset] BLAD zapisu: {e}", flush=True)
        return 1
    return 0


def _gen_cert():
    """
    Wygeneruj/odnow certyfikat Let's Encrypt - tryb STANDALONE (jako admin).
    Oddzielony od normalnego startu serwera: certbot wymaga admina, ale sam
    serwer NIE. Dzieki temu serwer dziala normalnie (bez admina, bez krzyku
    Defendera), a cert generujesz osobno raz na ~90 dni tym skrotem.

    Wywolanie: HAM-RADIO-CTRL.exe --gen-cert   (uruchom jako administrator)
    """
    import asyncio
    print("=" * 56, flush=True)
    print("  GENEROWANIE CERTYFIKATU LET'S ENCRYPT", flush=True)
    print("=" * 56, flush=True)

    # certbot on Windows must run elevated (writes the system cert store).
    # If we are NOT admin, re-launch this same command through UAC so the user
    # just clicks "Yes" instead of hitting a cryptic certbot error. This makes
    # the Start-menu shortcut work even without an elevation flag.
    if sys.platform == "win32":
        try:
            import ctypes
            _is_admin = ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            _is_admin = 0
        if not _is_admin:
            print("Brak uprawnien administratora — prosze o podniesienie (UAC)...",
                  flush=True)
            try:
                import ctypes
                if getattr(sys, "frozen", False):
                    _exe, _params = sys.executable, "--gen-cert"
                else:
                    _exe, _params = sys.executable, f'"{os.path.abspath(sys.argv[0])}" --gen-cert'
                # ShellExecute with "runas" = UAC prompt; relaunches as admin
                _rc = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", _exe, _params, None, 1)
                if _rc <= 32:
                    print(f"Nie udalo sie podniesc uprawnien (kod {_rc}). "
                          "Kliknij skrot prawym → Uruchom jako administrator.",
                          flush=True)
                    if getattr(sys, "frozen", False):
                        input("\nNacisnij Enter aby zamknac...")
                    return 1
                # Podniesiona instancja przejmuje robote — ta konczy sie cicho.
                return 0
            except Exception as e:
                print(f"Blad podnoszenia uprawnien: {e}", flush=True)
                print("Kliknij skrot prawym przyciskiem → Uruchom jako administrator.",
                      flush=True)
                if getattr(sys, "frozen", False):
                    input("\nNacisnij Enter aby zamknac...")
                return 1

    print("Tryb administratora OK — generuje certyfikat.", flush=True)
    print("", flush=True)

    class _DummyHub:
        """Minimalny hub - gen_cert_task tylko broadcastuje status."""
        async def broadcast(self, msg):
            t = msg.get("type", "")
            if t == "tunnel_msg" or "msg" in msg:
                print(f"  {msg.get('msg', msg)}", flush=True)

    async def _run():
        try:
            from tunnel_manager import TunnelManager
            tm = TunnelManager(_DummyHub())
            await tm.gen_cert_task()
        except Exception as e:
            print(f"[gen-cert] BLAD: {e}", flush=True)
            return 1
        return 0

    code = asyncio.run(_run())
    print("", flush=True)
    if code == 0:
        print("GOTOWE. Teraz uruchom serwer NORMALNIE (bez admina) -", flush=True)
        print("automatycznie uzyje nowego certyfikatu.", flush=True)
    if getattr(sys, "frozen", False):
        input("\nNacisnij Enter aby zamknac...")
    return code


def main():
    # Awaryjny reset hasla admina: HAM-RADIO-CTRL.exe --reset-admin
    if "--reset-admin" in sys.argv:
        code = _reset_admin()
        if getattr(sys, "frozen", False):
            input("\nNacisnij Enter aby zamknac...")
        sys.exit(code)

    # Generowanie certu Let's Encrypt (jako admin): HAM-RADIO-CTRL.exe --gen-cert
    if "--gen-cert" in sys.argv:
        sys.exit(_gen_cert())

    # Porty: domyslne z config (HTTP 8000, HTTPS 8001). Importujemy PO to zeby
    # config wykryl sciezki (frozen/nie-frozen) i wygenerowal .env przy 1. starcie.
    try:
        from config import PORT
    except Exception:
        PORT = 8000
    http_port  = PORT
    https_port = PORT + 1  # server.py: HTTPS = PORT+1 (8001 gdy PORT=8000)

    print("=" * 56, flush=True)
    print("  HAM RADIO CTRL — uruchamianie serwera...", flush=True)
    print("=" * 56, flush=True)

    # Watek otwierajacy przegladarke gdy serwer wstanie
    t = threading.Thread(
        target=_open_browser_when_ready,
        args=(https_port, http_port),
        daemon=True,
    )
    t.start()

    # Uruchom serwer (blokuje do zamkniecia). server.main() sam robi asyncio.run.
    try:
        import server
        server.main()
    except KeyboardInterrupt:
        print("\n[launcher] Zatrzymano (Ctrl+C).", flush=True)
    except Exception as e:
        print(f"[launcher] BLAD serwera: {e}", flush=True)
        import traceback
        traceback.print_exc()
        # W trybie EXE okno konsoli zamknie sie od razu - daj userowi zobaczyc blad
        if getattr(sys, "frozen", False):
            input("\nNacisnij Enter aby zamknac...")
        sys.exit(1)


if __name__ == "__main__":
    main()
