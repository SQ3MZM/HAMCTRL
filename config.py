#!/usr/bin/env python3
"""
config.py — konfiguracja, ścieżki, zmienne środowiskowe i dane statyczne.
Bez zależności od pozostałych modułów projektu (tylko stdlib).
"""
import os, sys, hashlib
from pathlib import Path

# ── Ścieżki (świadome PyInstaller) ────────────────────────────────────────────
# Kod i frontend (public/, rigs/) sa READ-ONLY i moga byc spakowane w EXE.
# Dane uzytkownika (config.json, users.json, .env, logi) MUSZA byc trwale i
# zapisywalne - obok EXE, nie w tymczasowym folderze PyInstaller.
#
# Wykrywanie srodowiska:
#   - PyInstaller onefile: sys.frozen=True, dane statyczne w sys._MEIPASS,
#     ale EXE fizycznie lezy w sys.executable (tam trzymamy dane usera).
#   - Normalny python: wszystko obok config.py.

def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _writable_data_dir() -> "Path":
    """
    Wybierz TRWALY, ZAPISYWALNY katalog na dane usera (config, users, .env, logi).

    Logika:
      1. Tryb dev (nie-frozen): obok config.py.
      2. Frozen (EXE):
         a) Jesli katalog obok EXE jest zapisywalny (np. EXE na pulpicie,
            w Downloads, przenosny) -> uzyj obok EXE (proste, przenosne).
         b) Jesli NIE (np. zainstalowany w Program Files, read-only) ->
            uzyj %APPDATA%\\HAMCTRL (Windows) / ~/.hamctrl (inne).

    Dzieki temu ten sam EXE dziala i jako przenosny (dane obok), i jako
    zainstalowany w Program Files (dane w APPDATA).
    """
    if not _is_frozen():
        return Path(__file__).parent

    exe_dir = Path(sys.executable).parent

    # KRYTYCZNE (utrata danych przy aktualizacji): dla instalacji w Program
    # Files ZAWSZE uzywaj APPDATA — bez testu zapisywalnosci. Test "czy moge
    # zapisac obok EXE" przechodzi gdy serwer uruchomiony JAKO ADMINISTRATOR,
    # wtedy qso.db/users.json/config ladowaly W Program Files i kazda
    # aktualizacja/deinstalacja je kasowala ("wgrywam nowa wersje - wywala
    # log"). Tryb przenosny (dane obok EXE) tylko POZA Program Files.
    _pf = [os.environ.get("ProgramFiles", r"C:\Program Files"),
           os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")]
    _in_program_files = any(
        str(exe_dir).lower().startswith(str(p).lower())
        for p in _pf if p
    )

    if not _in_program_files:
        # Test zapisywalnosci katalogu obok EXE (tryb przenosny)
        try:
            _test = exe_dir / ".write_test"
            _test.write_text("x", encoding="utf-8")
            _test.unlink()
            return exe_dir  # zapisywalny - tryb przenosny
        except Exception:
            pass  # read-only - idziemy do APPDATA

    # Katalog danych aplikacji
    appdata = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if appdata:
        d = Path(appdata) / "HAMCTRL"
    else:
        d = Path.home() / ".hamctrl"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        d = Path.home()  # ostateczny fallback

    # MIGRACJA RATUNKOWA: jesli stare dane leza OBOK EXE (efekt wczesniejszego
    # bledu: uruchamianie jako admin -> zapis do Program Files przechodzil),
    # przenies je do APPDATA zanim aktualizacja/deinstalacja je skasuje.
    # Kopiujemy tylko gdy plik NIE istnieje jeszcze w APPDATA (nie nadpisujemy
    # nowszych danych starymi).
    try:
        import shutil as _sh
        # qso.db-wal / -shm to pliki towarzyszace SQLite w trybie WAL —
        # zawieraja NAJNOWSZE transakcje, jeszcze nie scalone z glowna baza.
        # Bez nich migracja gubilaby ostatnio zapisane QSO.
        for _fn in ("qso.db", "qso.db-wal", "qso.db-shm",
                    "users.json", "config.json", ".env"):
            _src = exe_dir / _fn
            _dst = d / _fn
            if _src.exists() and not _dst.exists():
                _sh.copy2(str(_src), str(_dst))
                print(f"[config] Migracja danych: {_fn} (obok EXE -> APPDATA)",
                      flush=True)
        # KATALOGI: certyfikat Let's Encrypt (letsencrypt/) i tunel
        # (cloudflared/) tez zyly obok EXE — bez migracji cert "znikal"
        # (kod szuka w DATA=APPDATA, a pliki zostaly w Program Files).
        for _dn in ("letsencrypt", "cloudflared", "logs"):
            _src = exe_dir / _dn
            _dst = d / _dn
            if _src.is_dir() and not _dst.exists():
                try:
                    _sh.copytree(str(_src), str(_dst))
                    print(f"[config] Migracja katalogu: {_dn}/ "
                          f"(obok EXE -> APPDATA)", flush=True)
                except Exception as _de:
                    # Klucz prywatny w Program Files mogl byc utworzony przez
                    # certbota jako admin — odczyt bez admina moze byc
                    # zablokowany. Jasna instrukcja zamiast cichej awarii.
                    print(f"[config] NIE moge zmigrowac {_dn}/ ({_de}). "
                          f"Skopiuj recznie: '{_src}' -> '{_dst}' "
                          f"(np. jako administrator).", flush=True)
    except Exception as _e:
        print(f"[config] Migracja danych pominieta: {_e}", flush=True)

    return d


# BUNDLE = katalog z read-only zasobami (public/, rigs/)
if _is_frozen():
    BUNDLE = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    DATA = _writable_data_dir()
else:
    BUNDLE = Path(__file__).parent
    DATA = Path(__file__).parent

BASE   = DATA            # zachowana nazwa dla kompatybilnosci (dane usera)
PUBLIC = BUNDLE / "public"   # frontend - read-only, z paczki
CFG_F  = DATA / "config.json"
USR_F  = DATA / "users.json"
LOG_F  = DATA / "qso_log.json"
ENV_F  = DATA / ".env"


def load_env():
    e = {}
    if ENV_F.exists():
        for line in ENV_F.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            if "=" in line:
                k, _, v = line.partition("=")
                e[k.strip()] = v.strip()
    return e


def ensure_env():
    """
    Przy pierwszym uruchomieniu (brak .env) tworzy .env z BEZPIECZNYMI
    wartosciami dla tej konkretnej instalacji:

    - JWT_SECRET: LOSOWY (secrets.token_hex) - kazda instalacja ma wlasny
      klucz. KRYTYCZNE dla produktu wieloklubowego: bez tego wszystkie
      instalacje dzielilyby ten sam klucz i mozna by podrabiac tokeny.
    - FIRST_RUN: 1 - flaga ze kreator konfiguracji jeszcze nie przeszedl
      (wymusza zmiane hasla admina i konfiguracje stacji).

    Zwraca zaktualizowany slownik ENV. Jesli .env juz istnieje - nie rusza.
    """
    import secrets
    if ENV_F.exists():
        return load_env()
    # Pierwszy start - generuj swiezy .env
    jwt_secret = secrets.token_hex(32)  # 256-bit losowy klucz
    lines = [
        "# HAM RADIO CTRL - konfiguracja instalacji",
        "# Wygenerowane automatycznie przy pierwszym uruchomieniu.",
        "# NIE UDOSTEPNIAJ tego pliku - zawiera klucz JWT tej instalacji.",
        "",
        f"JWT_SECRET={jwt_secret}",
        "FIRST_RUN=1",
        "",
    ]
    try:
        ENV_F.write_text("\n".join(lines), encoding="utf-8")
        print(f"[config] Utworzono .env z losowym kluczem JWT (pierwszy start)",
              flush=True)
    except Exception as e:
        print(f"[config] UWAGA: nie moge zapisac .env: {e}", flush=True)
    return load_env()


ENV          = ensure_env()
PORT         = int(os.environ.get("PORT", ENV.get("PORT", 8000)))
CALLSIGN     = ENV.get("CALLSIGN", "SP0ABC")
LOCATOR      = ENV.get("STATION_LOCATOR", "KO02")
# JWT_SECRET zawsze z .env (ensure_env gwarantuje ze istnieje i jest losowy).
# Fallback tylko awaryjny gdyby zapis .env sie nie powiodl - i tak losowy
# per-proces (nie wspoldzielony hardcode jak wczesniej "hamradio2025").
SECRET       = ENV.get("JWT_SECRET") or __import__("secrets").token_hex(32)
ADMIN_PW     = ENV.get("ADMIN_PASSWORD", "Admin1234!")
FIRST_RUN    = ENV.get("FIRST_RUN", "0") == "1"
# VERBOSE: gadatliwe logi (status radia co 2s itp). Domyslnie WYLACZONE -
# produkt ma czysta konsole. Wlacz ustawiajac HAM_VERBOSE=1 w srodowisku
# albo VERBOSE=1 w .env gdy trzeba diagnozowac.
VERBOSE      = (os.environ.get("HAM_VERBOSE", ENV.get("VERBOSE", "0")) == "1")
HAMLIB       = ENV.get("HAMLIB_PATH", "rigctld")
HAMLIB_PORT  = int(ENV.get("HAMLIB_PORT", 4532))
RIGCTLD_LOG  = str(Path(__file__).resolve().parent / "rigctld.log")

MIME = {
    ".html": "text/html;charset=utf-8", ".css": "text/css",
    ".js": "application/javascript", ".json": "application/json",
    ".png": "image/png", ".ico": "image/x-icon", ".svg": "image/svg+xml",
    ".woff2": "font/woff2", ".woff": "font/woff", ".ttf": "font/ttf",
    ".mp3": "audio/mpeg", ".wav": "audio/wav",
}

# Modele z wbudowanym spektroskopem (scope) → tryb bezpośredni CI-V (control + scope).
# Pozostałe radia (IC-746 itd.) dalej przez rigctld/RigCAT — bez zmian.
SCOPE_MODELS = {"3073", "3078", "3085", "3068", "3070", "3081"}  # IC-7300/7610/705/9100/7100/9700

HAMLIB_MODELS = {
    "Icom": [
        {"id":"3073","name":"IC-7300"},{"id":"3046","name":"IC-746 Pro"},
        {"id":"3023","name":"IC-746"},{"id":"3085","name":"IC-705"},
        {"id":"3078","name":"IC-7610"},{"id":"3070","name":"IC-7100"},
        {"id":"3060","name":"IC-7000"},{"id":"3068","name":"IC-9100"},
        {"id":"3081","name":"IC-9700"},
        {"id":"3011","name":"IC-706MkIIG"},
    ],
    "Yaesu": [
        {"id":"1035","name":"FT-991A"},{"id":"1034","name":"FT-991"},
        {"id":"1031","name":"FT-857D"},{"id":"1030","name":"FT-817"},
        {"id":"1066","name":"FT-DX10"},{"id":"1067","name":"FT-710"},
    ],
    "Kenwood": [
        {"id":"2014","name":"TS-590SG"},{"id":"2022","name":"TS-890S"},
        {"id":"2010","name":"TS-2000"},{"id":"2017","name":"TS-990S"},
    ],
    "Elecraft": [
        {"id":"2050","name":"K3"},{"id":"2051","name":"K3S"},
        {"id":"2054","name":"KX3"},{"id":"2053","name":"K4"},
    ],
    "Inne": [{"id":"2","name":"Symulacja (test)"}],
}
