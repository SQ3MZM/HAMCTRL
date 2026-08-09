# -*- mode: python ; coding: utf-8 -*-
"""
hamctrl.spec — PyInstaller spec dla HAM RADIO CTRL (serwer jako EXE).

Buduje jeden plik: dist/HAM-RADIO-CTRL.exe

Uruchomienie (na Windows, w tym katalogu):
    py -m pip install pyinstaller
    py -m PyInstaller --clean --noconfirm hamctrl.spec

WYMAGANE PLIKI OBOK SPEC (dolozyc przed buildem):
    ham_audio.exe        — Rust audio server (binarny)
    libopus.dll, opus.dll, opuslib.dll   — biblioteki Opus (wszystkie trzy)

Zaleznosci Python do zainstalowania przed buildem:
    py -m pip install aiohttp aiortc numpy scipy pyserial pyaudio opuslib cryptography
    (winloop i orjson opcjonalne - przyspieszaja, ale nie wymagane)
"""
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

BASE = Path(os.getcwd())

# ── scipy: zbierz WSZYSTKO (podmoduly, dane, binaria) ─────────────────────────
# scipy.stats/scipy.signal maja duzo dynamicznych importow i plikow danych
# ktorych PyInstaller nie lapie sam. collect_all rozwiazuje "NameError obj"
# i "DLL load failed" ze scipy.
_scipy_datas, _scipy_binaries, _scipy_hidden = collect_all("scipy")
_numpy_datas, _numpy_binaries, _numpy_hidden = collect_all("numpy")

# onnxruntime (DeepCW) ma natywne DLL-e — bez collect_all PyInstaller ich nie
# zabierze i przy starcie dostaniemy "DLL load failed". Opcjonalne: gdy pakiet
# nie jest zainstalowany, build i tak przejdzie (DeepCW bedzie niedostepny).
try:
    _ort_datas, _ort_binaries, _ort_hidden = collect_all("onnxruntime")
except Exception:
    _ort_datas, _ort_binaries, _ort_hidden = [], [], []

# ── Pliki danych (read-only, ida do paczki) ───────────────────────────────────
datas = [
    ("public", "public"),   # cały frontend (html/css/js)
    ("rigs",   "rigs"),     # profile radia (jesli PyInstaller nie zlapie jako modul)
]
# Most COM (HAM-RADIO-CTRL.exe dla CW Skimmer/Logger32) - do pobrania przez
# operatorow przez /download/com-bridge. Dokladany jesli lezy obok spec.
_com_bridge = BASE / "HAM-RADIO-CTRL-bridge.exe"
if _com_bridge.exists():
    datas.append((str(_com_bridge), "bridge"))
    print(f"[spec] dolaczam most COM: {_com_bridge.name}")
else:
    # Fallback: oryginalna nazwa TYLKO z bridge_client/dist (tam jest most,
    # NIE serwer). NIE szukamy w BASE bo tam lezy serwer o tej samej nazwie!
    _cand = BASE / "bridge_client" / "dist" / "HAM-RADIO-CTRL.exe"
    if _cand.exists():
        datas.append((str(_cand), "bridge"))
        print(f"[spec] dolaczam most COM z bridge_client/dist: {_cand}")
    else:
        print("[spec] UWAGA: brak mostu COM (HAM-RADIO-CTRL-bridge.exe) - "
              "/download/com-bridge nie zadziala. Skopiuj most pod nazwa "
              "HAM-RADIO-CTRL-bridge.exe obok spec.")
datas += _scipy_datas + _numpy_datas + _ort_datas

# ── Binaria zewnetrzne (ham_audio.exe + opus DLL) ─────────────────────────────
# Dokladane tylko jesli istnieja obok spec - build nie pada gdy ich brak
# (mozna zbudowac wersje bez audio do testow).
binaries = []
for _bin in ("ham_audio.exe", "libopus.dll", "opus.dll", "opuslib.dll"):
    _p = BASE / _bin
    if _p.exists():
        binaries.append((str(_p), "."))
        print(f"[spec] dolaczam binarke: {_bin}")
    else:
        print(f"[spec] pomijam (brak): {_bin}")
binaries += _scipy_binaries + _numpy_binaries + _ort_binaries

# ── Hidden imports ────────────────────────────────────────────────────────────
# PyInstaller nie zawsze wykrywa dynamiczne importy. Te paczki maja duzo
# podmodulow ladowanych dynamicznie - trzeba je wskazac jawnie.
hiddenimports = [
    # aiohttp + async
    "aiohttp", "aiohttp.web", "multidict", "yarl", "async_timeout",
    "attr", "frozenlist", "aiosignal",
    # aiortc (WebRTC - TX audio) - CIEZKIE, duzo zaleznosci
    "aiortc", "aiortc.contrib.media", "av", "pylibsrtp",
    "cryptography", "cryptography.hazmat.bindings._openssl",
    "google.protobuf", "pyee",
    # numpy / scipy (DSP - FT8/FT4 decode)
    "numpy", "scipy", "scipy.signal", "scipy.special",
    "scipy.fft", "scipy.fftpack", "scipy._lib.messagestream",
    # audio
    "pyaudio", "opuslib", "opuslib.api",
    # serial
    "serial", "serial.tools", "serial.tools.list_ports",
    # przyspieszacze (opcjonalne - jesli zainstalowane)
    # "winloop", "orjson",  # odkomentuj jesli uzywasz
    # moduly projektu (zeby na pewno weszly)
    "config", "webapp", "server", "civ", "data", "auth",
    "audio", "audio_stream", "audio_rust_bridge", "webrtc_audio",
    "qso_db", "qso_engine", "clock_check", "dxcluster", "rotator", "relay_controller",
    "tunnel_manager", "wsjtx_udp", "wsjtx_local", "com_bridge_ws",
    "rigcat", "hamlib_server", "hamlib_caps",
    "ft8_encoder", "ft8_rx_decoder", "ft8_rust_receiver",
    "ft4_encoder", "ft4_rx_decoder",
    # DeepCW (dekoder CW przez siec neuronowa - ONNX)
    "deepcw_engine", "deepcw_model", "deepcw_lang",
    "onnxruntime", "onnxruntime.capi", "onnxruntime.capi._pybind_state",
    "demod", "demod_ft4", "sync", "sync_ft4", "ldpc_decode",
    "params", "params_ft4", "unpack", "waterfall",
    "rigs", "rigs.civ_profiles", "rigs.features",
]
hiddenimports += _scipy_hidden + _numpy_hidden + _ort_hidden

# ── Wykluczenia (odchudzenie EXE) ─────────────────────────────────────────────
# Rzeczy ktorych na pewno nie uzywamy - zmniejsza rozmiar.
excludes = [
    "tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "pytest", "IPython", "notebook", "jupyter", "pandas",
    "PIL",  # jesli nie uzywasz Pillow po stronie serwera
]

block_cipher = None

a = Analysis(
    ["launcher.py"],
    pathex=[str(BASE)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["rthook_scipy.py"],
    excludes=excludes,
    # optimize=0: zachowaj docstringi (scipy.stats generuje z nich kod).
    optimize=0,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="HAM-RADIO-CTRL",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX moze psuc niektore DLL - wylaczone dla stabilnosci
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,         # KONSOLA WIDOCZNA - user widzi logi/bledy serwera
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="icon.ico" if (BASE / "icon.ico").exists() else None,
)
