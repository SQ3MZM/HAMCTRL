
#!/usr/bin/env python3
"""
webapp.py — WebSocket Hub + aplikacja (routing HTTP/WS, API).
"""
import re, time, json, asyncio, struct
import numpy as np
import aiohttp
import aiohttp.web as web

# ══════════════════════════════════════════════════════════════════════════════
# OPTYMALIZACJE WYDAJNOSCI (asynchroniczna wysylka, szybki JSON)
# ══════════════════════════════════════════════════════════════════════════════
# orjson - jesli zainstalowany, 5-10x szybszy JSON niz stdlib. Fallback do stdlib.
# Uzytkownik moze zainstalowac: py -m pip install orjson
try:
    import orjson
    def _fast_json_bytes(msg):
        """Encoduj wiadomosc do bytes UTF-8. Duzo szybsze niz json.dumps."""
        return orjson.dumps(msg, option=orjson.OPT_SERIALIZE_NUMPY)
    _JSON_BACKEND = "orjson"
except ImportError:
    def _fast_json_bytes(msg):
        return json.dumps(msg, ensure_ascii=False).encode('utf-8')
    _JSON_BACKEND = "stdlib"

# winloop/uvloop - szybszy event loop niz standardowy asyncio.
# Windows: winloop, Linux/Mac: uvloop. Instalacja:
#   py -m pip install winloop  (Windows)
#   pip install uvloop         (Linux/Mac)
def _install_fast_loop():
    import sys
    try:
        if sys.platform == 'win32':
            import winloop
            winloop.install()
            return "winloop"
        else:
            import uvloop
            uvloop.install()
            return "uvloop"
    except ImportError:
        return "asyncio-stdlib"

_LOOP_BACKEND = _install_fast_loop()
print(f"[perf] JSON={_JSON_BACKEND}, event_loop={_LOOP_BACKEND}")

# ══════════════════════════════════════════════════════════════════════════════
# Static file cache — trzyma pliki JS/CSS/HTML w RAM (pre-compressed).
# Kazdy GET /static-plik dostaje bytes bezposrednio z Map, bez read z dysku.
#
# Pliki sa cache'owane leniwie (przy pierwszym zadaniu) i **niezmieniane** przy
# starcie serwera - restart Python = purge cache. Dla dev-time uzytkownikow
# to sluszne: podniosles serwer wiec pobrali dumps.
#
# Kompresja: gzip on-the-fly przy pierwszym cache, bytes zapisane raz.
# Brotli lepszy ale wymaga instalacji, gzip jest w stdlib.
# ══════════════════════════════════════════════════════════════════════════════
import gzip
# Map[str_path] -> (mtime, bytes_raw, bytes_gz | None, mime, etag)
_STATIC_CACHE: dict = {}
_STATIC_CACHE_LOCK = None  # asyncio.Lock stworzony przy starcie loop

# MIME types ktore warto gzipowac (tekstowe). Binarne (png, opus) juz sa
# skompresowane wiec gzip nie da nic ale doda overhead.
_GZIP_MIMES = {
    "text/html", "text/css", "text/plain", "text/javascript",
    "application/javascript", "application/json", "application/xml",
    "image/svg+xml",
}

def _cache_static_file(fpath, mime: str) -> tuple:
    """Wczytaj plik z dysku, pre-compress jesli tekstowy, zwroc entry cache."""
    import hashlib
    raw = fpath.read_bytes()
    mtime = fpath.stat().st_mtime
    # ETag = hash zawartosci (silny) - klient moze wysyłać If-None-Match
    etag = '"' + hashlib.md5(raw).hexdigest()[:16] + '"'
    # gzip jesli tekstowy i > 1KB (mniejsze pliki nie warte overhead-u)
    gz = None
    if mime.split(";")[0].strip() in _GZIP_MIMES and len(raw) > 1024:
        try:
            gz = gzip.compress(raw, compresslevel=6)  # 6 = balans speed/ratio
            # Zapisz gzip tylko jesli faktycznie mniejszy (dla juz skompresowanych ratio ~0.95)
            if len(gz) >= len(raw) * 0.95:
                gz = None
        except Exception:
            gz = None
    return (mtime, raw, gz, mime, etag)

SERVER_VERSION = "1.0"

# ── Update check ──────────────────────────────────────────────────────────────
# HAMCTRL checks GitHub Releases for a newer version and shows the admin a
# notice (it never downloads or installs anything — the admin fetches the new
# installer manually). Set this to your repo once it exists, e.g.
# "YOURCALL/hamctrl". While empty, the update check is simply skipped.
GITHUB_REPO = ""   # TODO: "owner/repo" — fill in after creating the GitHub repo

from config import (CALLSIGN, LOCATOR, PORT, HAMLIB_MODELS, SCOPE_MODELS,
                    MIME, PUBLIC, CFG_F, USR_F, LOG_F, ADMIN_PW, FIRST_RUN)
from auth import (jwt_sign, jwt_verify, hash_pw, hash_pw_secure,
                  verify_pw, needs_rehash)
from data import get_cfg, get_users, load_json, save_json, DEFAULT_MACROS
import qso_db
from audio import enumerate_audio_devices, auto_detect_radio_audio
from audio_stream import AudioStream
try:
    from webrtc_audio import WebRTCAudioReceiver
    _WEBRTC = True
except Exception as e:
    print(f"[webrtc] niedostepne: {e}")
    _WEBRTC = False
from wsjtx_udp import WsjtxUdpServer
from tunnel_manager import TunnelManager
from rotator import Rotator
from rigcat import RigCAT
from civ import CivRig
from com_bridge_ws import ComBridgeWs
import ft8_encoder
import ft4_encoder
try:
    from dxcluster import ClusterManager
    _DXCLUSTER_OK = True
except Exception as e:
    print(f"[dxcluster] modul niedostepny: {e}")
    _DXCLUSTER_OK = False
try:
    from relay_controller import RelayController, list_serial_ports, MAX_PULSE_S, RELAY_COUNT
    _RELAY_OK = True
except Exception as e:
    print(f"[relay] modul niedostepny: {e}")
    _RELAY_OK = False
import ft8_rx_decoder
try:
    from deepcw_engine import deepcw_engine
except Exception as _e:
    deepcw_engine = None
    print(f"[deepcw] silnik niedostepny: {_e}", flush=True)
import ft4_rx_decoder
import waterfall
import qso_engine
from rigs.features import (FEATURES, effective_features, features_for_admin,
                            default_enabled_features, effective_dynamic,
                            dynamic_for_admin)


# ══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET HUB (aiohttp)
# ══════════════════════════════════════════════════════════════════════════════
# Mapowanie typu wiadomosci -> kanal. Wszystko co nie jest tu wymienione
# idzie na 'control' (default). Pozwala na przezroczyste routowanie bez
# modyfikacji setek istniejacych wywolan hub.broadcast() - wystarczy
# ustawic kanaly wg schematu i broadcast sam pouzupelnia channel.
_MSG_TYPE_TO_CHANNEL = {
    # Waterfall CI-V (panel Radio) - duzy volumen, tylko dla Radio tab
    'scope_frame':       'scope',
    'scope_reset':       'scope',

    # FT8/FT4/WSJT-X - duzy volumen decodowan i waterfallu, tylko dla FT8 tab
    'ft8_waterfall':     'ft8',
    'wsjtx_decode':      'ft8',
    'wsjtx_clear':       'ft8',
    'wsjtx_status':      'ft8',
    'wsjtx_call_activity': 'ft8',
    'wsjtx_qso_logged':  'ft8',
    'wsjtx_tx_start':    'ft8',
    'wsjtx_tx_stop':     'ft8',
    'ft8_tx_freq':       'ft8',
    'ft8_tx_status':     'ft8',
    'ft8_tx_error':      'ft8',
    'ft8_tx_halted':     'ft8',
    'ft8_tx_period':     'ft8',
    'ft8_tx_started':    'ft8',
    'ft8_period_set':    'ft8',
    'ft8_rx_status':     'ft8',
    'ft8_rx_freq':       'ft8',
    'ft8_split_status':  'ft8',
    'ft8_decode_mode':   'ft8',
    'tx_watchdog_start': 'ft8',
    'tx_watchdog_countdown': 'ft8',
    'tx_watchdog_cancel': 'ft8',
    'auto_seq_status':   'ft8',
    'auto_qso_status':   'ft8',
    'auto_qso_queue':    'ft8',
    'auto_qso_error':    'ft8',
    'auto_qso_complete': 'ft8',
    'tune_status':       'ft8',
    'hound_status':      'ft8',
    'hound_step':        'ft8',
    # Dekoder CW dziala niezaleznie od zakladki FT8 — okno mozna otworzyc
    # z panelu keyera przy pracy CW. Na kanale 'ft8' tekst dochodzil TYLKO do
    # klientow zasubskrybowanych do FT8: backend logowal dekody, a okno CW
    # milczalo, bo lista odbiorcow byla pusta. 'control' ma kazdy klient,
    # a to kilka znakow na sekunde — koszt zerowy.
    'deepcw_text':       'control',

    # DX Cluster - tylko dla DXCluster tab
    'dx_spot':           'dxcluster',
    'dx_status':         'dxcluster',
}

def _channel_for_msg(msg: dict) -> str:
    """Wybierz kanal na podstawie typu wiadomosci. Default = 'control'."""
    return _MSG_TYPE_TO_CHANNEL.get(msg.get('type', ''), 'control')


class WSHub:
    """Broadcast hub z subskrypcja kanalow.

    Klienci subskrybuja tylko kanaly ktore ich interesuja (np. zakladka Log
    nie potrzebuje scope_frame ani ft8_waterfall). Broadcast filtruje odbiorcow
    wg channel parameter — klient nieзаsubskrybowany nie dostaje wiadomosci.

    Kanaly:
      - 'control':  default - wszystko co dotyczy stanu radia i systemu
                    (freq, mode, ptt, radio_lock, chat, presence, toasts)
      - 'scope':    scope_frame z CI-V (waterfall panelu Radio)
      - 'ft8':      ft8_waterfall, wsjtx_decode, auto_seq_status, tune_status etc.
      - 'dxcluster': dx_spot, dx_status
    Kanal 'audio' NIE jest tutaj - audio idzie przez osobny WebSocket path
    (audio_stream.py) z wlasnym subscribers mechanizmem.
    """

    def __init__(self):
        self._clients: set = set()
        # dict: ws -> set kanalow do ktorych klient subskrybowal
        # Default przy add() to {'control'} - kazdy dostaje kontrole radia.
        self._subs: dict = {}
        self._lock = asyncio.Lock()
        self._loop = None

    def set_loop(self, loop):
        self._loop = loop

    async def add(self, ws):
        async with self._lock:
            self._clients.add(ws)
            # Default: ALL kanaly - klient odejmie te ktorych nie chce przez
            # subscribe {mode:'set'}. To rozwiązuje race condition:
            # nowy klient dostaje wsjtx_decode NAWET gdy nie zdazyl wyslac
            # subscribe {channels:['ft8']} przed pierwszym broadcastem.
            # Wygrana z filtrowania kanalow tak samo zachowana - klient
            # w Log wysyla subscribe {channels:['control']} chwile pozniej
            # i przestaje dostawac scope_frame/ft8_waterfall.
            self._subs[ws] = {'control', 'scope', 'ft8', 'dxcluster'}

    async def remove(self, ws):
        async with self._lock:
            self._clients.discard(ws)
            self._subs.pop(ws, None)

    async def subscribe(self, ws, channels):
        """Klient dodaje kanaly do swojej subskrypcji (nie zastepuje)."""
        async with self._lock:
            if ws not in self._subs:
                self._subs[ws] = set()
            self._subs[ws].update(channels)

    async def unsubscribe(self, ws, channels):
        """Klient usuwa kanaly ze swojej subskrypcji. 'control' zawsze zostaje."""
        async with self._lock:
            if ws not in self._subs:
                return
            self._subs[ws].difference_update(channels)
            self._subs[ws].add('control')  # nigdy nie mozna zrezygnowac

    async def set_channels(self, ws, channels):
        """Ustaw pelen zestaw kanalow (uzywane przy zmianie zakladki)."""
        async with self._lock:
            self._subs[ws] = set(channels) | {'control'}

    async def broadcast(self, msg: dict, skip=None, channel: str = None):
        """Rownolegly broadcast do subskrybentow danego kanalu.

        Jesli channel=None, kanal wybierany automatycznie na podstawie
        msg['type'] przez _channel_for_msg() — dzieki temu istniejacy kod
        wywolujacy hub.broadcast(msg) nie musi byc modyfikowany.

        OPTYMALIZACJE:
        1. orjson zamiast json.dumps (5-10x szybciej dla duzych obiektow)
        2. Pre-encode raz do stringa (nie encoduje N razy dla N klientow)
        3. asyncio.gather zamiast sekwencyjnego await (rownolegly send)
        4. Subscription filtering - klient dostaje tylko kanaly ktore chce
        5. Usuniete printy z hot-path

        UWAGA: uzywamy send_str (nie send_bytes) bo frontend rozroznia:
          - Binary (ArrayBuffer)  -> Opus audio frame
          - Text                  -> JSON control message
        """
        if not self._clients:
            return
        # Auto-routing kanalu jesli nie podano jawnie
        if channel is None:
            channel = _channel_for_msg(msg)
        # Snapshot klientow bez locka + filtrowanie wg kanału
        clients = [
            ws for ws in self._clients
            if ws is not skip and channel in self._subs.get(ws, set())
        ]
        if not clients:
            return
        try:
            if _JSON_BACKEND == "orjson":
                data = _fast_json_bytes(msg).decode('utf-8')
            else:
                data = json.dumps(msg, ensure_ascii=False)
        except Exception:
            data = json.dumps(msg, ensure_ascii=False, default=str)

        # Kanaly ULOTNE (scope, audio, ft8_waterfall) — dane przychodza wiele
        # razy na sekunde, wiec pojedyncza ramka jest nieistotna. Gdy klient
        # odbiera wolno, TLS-owy send blokuje petle czekajac az bufor sie
        # oprozni (backpressure — wykryte przez looplag: sslproto _do_write).
        # Dajemy krotki timeout: nie nadaza = pomijamy go w TEJ ramce, zamiast
        # zamrazac wszystkich. Kanaly wazne (control, chat) wysylamy normalnie.
        _ephemeral = channel in ("scope", "audio", "ft8_waterfall")

        async def _send_one(ws):
            if _ephemeral:
                # Wolny klient: dane pietrza sie w buforze TLS, kolejny zapis
                # blokuje petle (backpressure). Krotki timeout — nie nadaza w
                # 0.25s = pomijamy dla niego te ulotna ramke (scope/audio
                # przyjdzie kolejna). Bufor ograniczony przez writer_limit przy
                # tworzeniu WS (patrz ws_handler), wiec send_str szybko sygnalizuje
                # zapchanie zamiast rosnac w nieskonczonosc.
                try:
                    await asyncio.wait_for(ws.send_str(data), timeout=0.25)
                except asyncio.TimeoutError:
                    return None
            else:
                await ws.send_str(data)

        results = await asyncio.gather(
            *[_send_one(ws) for ws in clients],
            return_exceptions=True
        )
        dead = [ws for ws, res in zip(clients, results) if isinstance(res, Exception)]
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)
                    self._subs.pop(ws, None)

    def broadcast_sync(self, msg: dict, channel: str = None):
        """Broadcast z wątku niesynchronicznego (thread-safe).
        Kanal auto-routowany na podstawie msg['type'] jesli nie podano."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.broadcast(msg, channel=channel), self._loop
            )


# ══════════════════════════════════════════════════════════════════════════════
# APLIKACJA
# ══════════════════════════════════════════════════════════════════════════════


def _adif_field(name: str, value) -> str:
    """Zbuduj pole ADIF: <nazwa:dlugosc>wartosc. Puste -> pomijane."""
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    return f"<{name}:{len(s)}>{s}"


def qso_to_adif(qso: dict) -> str:
    """
    Zamien QSO (dict z naszej bazy) na ciag ADIF wymagany przez Cloudlog/WaveLog.

    Cloudlog API /index.php/api/qso oczekuje:
      {"key":..., "station_profile_id":..., "type":"adif", "string":"<call:5>...<eor>"}

    NIE przyjmuje pol JSON (call, band, mode...) — to byl bug: QSO nie trafialy
    do Cloudloga mimo odpowiedzi HTTP 200.

    Czestotliwosc: ADIF wymaga MHz (np. 14.074). Nasza baza trzyma Hz lub MHz
    zaleznie od zrodla (WSJT-X vs reczny wpis) - normalizujemy.
    """
    freq_raw = str(qso.get("freq", "")).strip()
    freq_mhz = ""
    if freq_raw:
        try:
            f = float(freq_raw)
            if f > 1_000_000:        # Hz
                freq_mhz = f"{f / 1_000_000:.6f}".rstrip("0").rstrip(".")
            elif f > 1000:           # kHz
                freq_mhz = f"{f / 1000:.6f}".rstrip("0").rstrip(".")
            else:                    # juz MHz
                freq_mhz = f"{f:.6f}".rstrip("0").rstrip(".")
        except (ValueError, TypeError):
            freq_mhz = ""

    parts = [
        _adif_field("call",             qso.get("call", "")),
        _adif_field("qso_date",         qso.get("qso_date", "")),
        _adif_field("time_on",          qso.get("time_on", "")),
        _adif_field("time_off",         qso.get("time_off", "")),
        _adif_field("band",             qso.get("band", "")),
        _adif_field("mode",             qso.get("mode", "")),
        _adif_field("freq",             freq_mhz),
        _adif_field("rst_sent",         qso.get("rst_sent", "")),
        _adif_field("rst_rcvd",         qso.get("rst_rcvd", "")),
        _adif_field("gridsquare",       qso.get("gridsquare", "")),
        _adif_field("station_callsign", qso.get("my_call", "")),
        _adif_field("my_gridsquare",    qso.get("my_gridsquare", "")),
        _adif_field("tx_pwr",           qso.get("power", "")),
        _adif_field("comment",          qso.get("comment", "")),
    ]
    return "".join(p for p in parts if p) + "<eor>"


class App:
    def __init__(self):
        self.hub      = WSHub()
        self.cfg      = get_cfg()
        # Wybierz backend od razu na podstawie zapisanego modelu w config.json,
        # zeby na starcie nie tworzyc niepotrzebnie RigCAT (rigctld) dla modelu
        # ktory i tak pojdzie przez CivRig (SCOPE_MODELS) — unika konfliktu
        # o port szeregowy miedzy dwoma backendami.
        _rigs = self.cfg.get("rigs") or [{}]
        _saved_model = str(_rigs[0].get("model", ""))
        if _saved_model in SCOPE_MODELS:
            self.rig = CivRig(self.cfg, self._rig_bcast, log=print)
            print(f"[rig] start backend → CI-V bezposredni (zapisany model {_saved_model})")
        else:
            self.rig = RigCAT()
        self.rotators: list[Rotator] = []
        self.users    = get_users()
        self.log      = load_json(LOG_F, [])
        self.audio    = AudioStream()
        self.audio.cfg = self.cfg.get("audio", {})

        # Auto-detekcja karty audio radia — jesli w konfiguracji NIE ma
        # ustawionych kart RX/TX (albo user_audio_auto=True), wykryj karte
        # automatycznie po nazwie. Rozpoznaje IC-7300, IC-705, FT-991 itp.
        self._audio_auto = self.cfg.setdefault("audio_auto_detect", True)
        if self._audio_auto:
            try:
                detection = auto_detect_radio_audio()
                if detection["detected"]:
                    if "audio" not in self.cfg: self.cfg["audio"] = {}
                    # Nadpisz tylko jesli user nie ustawil recznie
                    if not self.cfg["audio"].get("rxDevice") and detection["rx"]:
                        self.cfg["audio"]["rxDevice"] = detection["rx"]
                    if not self.cfg["audio"].get("txDevice") and detection["tx"]:
                        self.cfg["audio"]["txDevice"] = detection["tx"]
                    self.audio.cfg = self.cfg["audio"]
                    print(f"[audio] auto-detect: {detection['pattern']} -> RX={detection['rx']!r}, TX={detection['tx']!r}")
                    self._audio_detection = detection
                else:
                    print("[audio] auto-detect: karta radia nie wykryta, uzyj recznej konfiguracji")
                    self._audio_detection = detection
            except Exception as e:
                print(f"[audio] auto-detect blad: {e}")
                self._audio_detection = {"detected": False, "rx": None, "tx": None,
                                          "pattern": None, "all_rx": [], "all_tx": []}
        else:
            self._audio_detection = {"detected": False, "rx": None, "tx": None,
                                      "pattern": None, "all_rx": [], "all_tx": []}

        self.rust_audio = None  # ustawiane przez server.py gdy ham_audio.exe dostępny
        self._rig_power_on = True  # stan zasilania radia — aktualizowany przez power_toggle
        self._ft8_tx_abort = False
        self._ft8_rx_enabled = False
        # Kto uruchomil WSJT-X (uid) — potrzebne do auto-stop przy disconnect
        # lub oddaniu radia. Nie ma znaczenia dla wielu userow bo tylko jeden
        # naraz moze dekodowac (backend jest global), ale musimy wiedziec KTO
        # zeby wiedziec kiedy zatrzymac.
        self._ft8_rx_owner_uid: str | None = None
        self._last_auto_tx_key = None
        self._pre_pcm_cache = None  # (call_to, call_de, report, pcm_bytes, duration)  # dedup auto-TX: (call_to, report_or_grid)
        self._tx_watchdog_task = None  # asyncio Task auto-off PTT
        self._tune_stop = False  # przerywa Tune tone

        # COM Bridge WS - dla klientow EXE Windows tworzacych virtual COM.
        # Klienci lacza sie WSS na /ws/com-bridge, dostaja mapping serwis->COM
        # i forwarduja bajty w obu stronach. Uzywane przez CW Skimmer, Logger32,
        # HRD do zdalnego dostepu do CI-V IC-7300 (i w przyszlosci Yaesu/Kenwood).
        # Uwaga: self.rig moze byc RigCAT() gdy CI-V wylaczony - wtedy bridge
        # nie ma nic do zaoferowania i klienci widza service_status civ=false.
        civ_for_bridge = self.rig if isinstance(self.rig, CivRig) else None
        # can_write: user moze PISAC do radia przez COM Bridge tylko gdy ma
        # radio_lock albo jest adminem. Bez tego user bez locka moglby kręcic
        # freq przez CW Skimmer/HRD omijajac blokady UI (fix 2026-07-05).
        def _com_can_write(uid: str) -> bool:
            """
            Czy user moze PISAC do radia przez COM Bridge (zmieniac freq/mode/
            PTT z CW Skimmer/HRD)?

            Uwaga: viewer jest ODRZUCANY juz przy polaczeniu WS (com_bridge_ws_
            handler), wiec tutaj obslugujemy tylko admina i operatorow.

            Prawo do pisania ma:
              - admin (zawsze)
              - operator TRZYMAJACY radio_lock
            Nie ma:
              - operator BEZ locka (tylko podglad)
              - ktokolwiek bez konta
            """
            if not uid:
                return False
            u = next((x for x in self.users if x.get('id') == uid), None)
            if not u:
                return False
            if u.get('role') == 'admin':
                return True
            # Operator - tylko gdy trzyma radio_lock
            # (viewer nie dotrze tutaj - odrzucony przy polaczeniu)
            return self.radio_lock.get('user_id') == uid
        self.com_bridge_ws = ComBridgeWs(civ_rig=civ_for_bridge, hub=self.hub,
                                          log=print, can_write=_com_can_write)

        # ── DX Cluster manager (per-user telnet connections) ──────────────────
        self.dxcluster = None
        self._server_start_time = time.time()  # do liczenia uptime w /api/status
        if _DXCLUSTER_OK:
            async def _dx_broadcast(user_id: str, msg: dict):
                # Wyslij WS tylko do konkretnego usera (znajdz jego ws w online_users)
                for ws, info in list(self.online_users.items()):
                    if info.get("user_id") == user_id:
                        try:
                            await ws.send_json(msg)
                        except Exception:
                            pass
            self.dxcluster = ClusterManager(on_broadcast=_dx_broadcast)

        # ── Relay controller (Arduino SP5IOU SDR220 emulator) ─────────────────
        self.relay: RelayController | None = None
        if _RELAY_OK:
            rcfg = self.cfg.get("relay", {})
            if rcfg.get("enabled") and rcfg.get("port"):
                try:
                    self.relay = RelayController(
                        port=rcfg["port"],
                        baudrate=int(rcfg.get("baudrate", 9600)),
                    )
                    asyncio.ensure_future(self._relay_connect_task())
                except Exception as e:
                    print(f"[relay] init blad: {e}")

        # ── Stan scope/wodospadu i TX marker ──────────────────────────────────
        self._ft8_tx_freq_hz = 1000.0   # aktualna docelowa czestotliwosc nadawania
        # UWAGA: NIE 1500Hz — to udokumentowany "sweet spot"/punkt odniesienia
        # wewnetrznego przetwarzania cyfrowego IC-7300 w trybie USB-D, ktory
        # generuje stały notch (~185Hz szerokosci) dokladnie na tej czestotliwosci
        # w torze audio RX, niezalezny od NB/NR/Notch/AGC/PTT. Potwierdzone na
        # dwoch niezaleznych nagraniach (Audacity, bez naszego kodu) — obecny w
        # USB-D, nieobecny w zwyklym USB. 1000Hz jest bezpiecznym dystansem od
        # tego punktu.
        self._ft8_tx_locked = False     # STARY mechanizm — do usuniecia, zachowany
        # tymczasowo do przyszlego wykorzystania w funkcji "TX Hound" (warstwa
        # zaznaczana, osobna sesja). NIE jest juz uzywany przez biezacy kod.
        self._ft8_tx_frozen = False     # NOWY mechanizm: TX zamrozone na miejscu,
        # a RX automatycznie przeskakuje na czestotliwosc kazdej odebranej
        # wiadomosci adresowanej do NAS (call_to == CALLSIGN) — pozwala
        # operatorowi "zamrozic" TX i sledzic stacje ktora wlasnie do niego
        # nadaje, bez recznego przesuwania znacznika RX.
        self._ft8_split_enabled = False # tryb split: TX tylko powyzej _ft8_split_min_hz
        self._ft8_split_min_hz = 1200.0
        self._ft8_rx_freq_hz = 1000.0   # niezalezny znacznik RX (Rx Frequency panel) —
        # startuje na tej samej wartosci co TX, ale moze byc przesuwany calkowicie
        # niezaleznie (np. nasluch na innej czestotliwosci niz aktualne nadawanie)

        # ── Fake Split (Rig Split) ───────────────────────────────────────────
        # Przesuwa VFO PRZED nadawaniem tak, zeby audio TX bylo blisko srodka
        # filtra SSB (~1500Hz), zamiast blisko jego krawedzi — na krawedziach
        # filtr tlumi sygnal (moc spada, ALC/splattery). Po nadaniu VFO wraca
        # na miejsce. Logika (niezmiennik freq_eteru = dial+audio) pochodzi z
        # fake_split_prototype.py. Stan wlaczenia zapamietany w configu.
        self._fake_split_enabled = bool(self.cfg.get("ft8", {}).get("fakeSplit", False))
        self._fake_split_state = None  # dict {dial_hz, audio_hz} do przywrocenia po TX, albo None

        # ── Automatyka QSO (pelna automatyka w stylu WSJT-X) ────────────────
        self._qso_engine = qso_engine.QsoEngine(my_call=CALLSIGN, my_grid=LOCATOR)
        self._auto_seq_enabled = True    # ZAWSZE aktywne (nie ma juz toggle w UI);
                                          # klik na makro to reczne wymuszenie
        self._auto_call_1st = False      # czy automatycznie startowac QSO z pierwsza
        # stacja ktora odpowie na nasze CQ (zamiast czekac na reczny wybor z kolejki)
        self._auto_cq_text = None        # tresc CQ jaka aktualnie "nadajemy" (do
        # rozpoznawania ze odebrana odpowiedz dotyczy NASZEGO CQ, nie przypadkowej
        # wiadomosci do nas adresowanej spoza kontekstu QSO)
        # Wiadomosc zaplanowana do wyslania w NASTEPNYM dostepnym oknie 15s,
        # generowana przez automatyke (on_decode) — pojedynczy "slot", bo w danym
        # momencie moze byc tylko jedna nastepna automatyczna transmisja oczekujaca.
        self._auto_pending_tx = None  # dict {call_to, call_de, report_or_grid, r_flag} lub None
        # ── Cykliczne wolanie CQ (2026-07-05) ────────────────────────────────
        # Gdy user wola CQ, nie nadajemy raz - powtarzamy co pelny okres (2 okna)
        # az ktos odpowie albo user/timer zatrzyma. Bez tego CQ szlo tylko raz.
        self._cq_calling = False          # czy aktywnie wolamy CQ w petli
        self._cq_call_de = None           # nasz znak (dla powtarzanego CQ)
        self._cq_report = None            # nasz grid (CQ CALL GRID)
        self._cq_task = None              # task petli ponawiania CQ
        # Mutex chroniacy przed ROWNOLEGLYM wykonaniem _ft8_tx_sequence — bez
        # tego, jesli dekoder zwroci wiele wiadomosci w jednym oknie i wiecej
        # niz jedna wyzwoli automatyczna odpowiedz (lub automatyka zbiegnie sie
        # czasowo z recznym TX), dwa rownolegle PTT/audio-feed kolidowalyby ze
        # soba. KAZDE wywolanie _ft8_tx_sequence (reczne i automatyczne) musi
        # przejsc przez ten lock.
        self._ft8_tx_lock = asyncio.Lock()
        self._ft8_tx_period = 1  # 1 = okna xx:00/xx:30 (domyslny), 2 = xx:15/xx:45
        self._qso_period_locked = False  # True gdy period wykryty dla biezacego QSO
        self._ft8_decode_mode = "FT8"  # "FT8" lub "FT4" — ktorego enkodera/timingu uzywac

        self.webrtc   = WebRTCAudioReceiver(
            on_pcm_frame=self.audio.feed_tx_pcm,
            on_track_started=self._webrtc_tx_start,
            on_track_ended=self._webrtc_tx_stop,
        ) if _WEBRTC else None
        self.wsjtx    = WsjtxUdpServer(self.hub.broadcast)
        self.tunnel   = TunnelManager(self.hub)
        self._caps_cache = {}

        # ── Radio Lock — jeden operator na raz ────────────────────────────────
        # Kto aktualnie "trzyma" radio (moze strojic, naciskac PTT, zmienic mod).
        # Pozostali uzytkownicy widza UI w trybie read-only.
        # timeout_min: minuty bezczynnosci po ktorych radio jest zwalniane automatycznie.
        # last_activity: czas ostatniej akcji aktywnego operatora (do auto-release).
        self.radio_lock: dict = {
            "user_id":       None,
            "username":      None,
            "callsign":      None,
            "locked_at":     None,
            "last_activity": None,
            "timeout_min":   self.cfg.get("radio_lock_timeout", 20),
        }
        # Pending requests: {user_id: {"username", "callsign", "requested_at"}}
        self.radio_requests: dict = {}
        # Online users: {ws: {"user_id", "username", "callsign", "role", "joined_at"}}
        self.online_users: dict = {}
        # Rate limiting logowania (brute-force protection) - patrz _login_* metody
        self._login_fails_ip: dict = {}    # {ip: {fails: [ts], blocked_until: ts}}
        self._login_fails_user: dict = {}  # {username: {...}}
        # Petle tla pod NADZOREM - jesli padna, supervisor je restartuje
        # (z rosnacym backoffem). Bez tego jeden nieprzewidziany blad zabija
        # funkcje az do recznego restartu serwera.
        self._supervise(lambda: self._radio_lock_watchdog(), "radio_lock_watchdog")
        # Watchdog urzadzen: co 1h sprawdza radio/rotatory/przekazniki i
        # reconnectuje zawieszone porty COM (tylko gdy radio nie nadaje i
        # nikt nie trzyma locka - zeby nie przerwac QSO).
        self._supervise(lambda: self._device_watchdog(), "device_watchdog")
        # Detektor lagu petli zdarzen — DIAGNOSTYKA skokow latency.
        # Mierzy, czy asyncio nie zamarza. Gdy petla stoi dluzej niz prog
        # (cos synchronicznego ja blokuje), wypisuje to w logu z czasem blokady.
        # To wskazuje winowajce skokow RTT/audio przy niskim CPU.
        self._supervise(lambda: self._loop_lag_monitor(), "loop_lag_monitor")
        # Pompa scope — wysyla najswiezsza ramke waterfalla w stalym tempie
        # 15 fps Z POZIOMU PETLI ASYNCIO (nie z watku readera). Reader tylko
        # zapisuje _latest_scope; ta pompa ja czyta i broadcastuje. Dzieki temu
        # strumien scope z radia NIE synchronizuje sie z petla przy kazdej
        # ramce — co wczesniej blokowalo asyncio na 100-500ms.
        self._supervise(lambda: self._scope_pump(), "scope_pump")
        # Update check: once at startup, in the background, admin-only notice.
        # Never blocks startup and stays silent on any network error.
        self._supervise(lambda: self._update_check_loop(), "update_check")

    async def _scope_pump(self):
        """Czyta najswiezsza ramke scope z radia i wysyla ja co ~66ms (15fps).
        Odsprzega strumien scope (watek readera) od petli zdarzen."""
        while True:
            await asyncio.sleep(0.066)
            rig = self.rig
            if not rig:
                continue
            frame = getattr(rig, "_latest_scope", None)
            if frame is not None:
                rig._latest_scope = None       # skonsumowane
                try:
                    await self.hub.broadcast(frame, channel="scope")
                except Exception:
                    pass

    async def _loop_lag_monitor(self):
        """Uruchamia WATEK-obserwator, ktory wykrywa blokade petli i zrzuca stos
        glownego watku W TRAKCIE blokady (nie po). Petla asyncio aktualizuje co
        chwile znacznik czasu; watek sprawdza go niezaleznie (ma wlasny rdzen /
        wywlaszczenie systemowe) i gdy znacznik sie nie odswieza = petla stoi.

        DIAGNOSTIC — off by default. Developer tool; a released club install
        should not print [looplag]. Enable only when chasing an event-loop stall
        by setting HAM_LOOPLAG=1. We skip the whole watchdog unless turned on."""
        import os as _os
        if _os.environ.get("HAM_LOOPLAG", "") not in ("1", "true", "TRUE", "yes"):
            # Diagnostic disabled. Don't return — the supervisor treats a return
            # as a crash and would restart us every few seconds. Just idle here.
            while True:
                await asyncio.sleep(3600)

        import time as _t
        import sys as _sys
        import traceback as _tb
        import threading as _threading

        self._loop_heartbeat = _t.monotonic()
        _main_id = _threading.main_thread().ident
        _state = {"dumped_at": 0.0}
        # Startup grace: first seconds bring one-off init blocks (scipy import
        # from bundle, launching ham_audio.exe, hamlib, RSA) — harmless, would
        # only spam the log. Only steady-state stalls matter.
        _start_ts = _t.monotonic()
        _grace_s = 15.0

        # Innermost frames that mean the loop is IDLE (sleeping in the OS
        # selector waiting for events), NOT blocked by our code. A "lag" whose
        # deepest frame is one of these is a false alarm — the loop simply had
        # nothing to do — so we skip it. Real blocks have our code or a slow
        # library (ssl, psutil, subprocess, numpy...) at the bottom of the stack.
        _IDLE_MARKERS = ("_poll", "select", "run_forever", "_run_once",
                         "_loop_self_reading")

        def _watchdog():
            while True:
                _t.sleep(0.02)
                if _t.monotonic() - _start_ts < _grace_s:
                    continue   # startup grace — ignore one-off init blocks
                _hb = getattr(self, "_loop_heartbeat", 0.0)
                _behind = (_t.monotonic() - _hb) * 1000.0
                # loop hasn't refreshed heartbeat for >120ms = stalled right now
                if _behind > 120 and (_hb - _state["dumped_at"]) > 0.001:
                    _state["dumped_at"] = _hb
                    try:
                        _f = _sys._current_frames().get(_main_id)
                        if _f is None:
                            continue
                        _stack = _tb.format_stack(_f)
                        # Look at the deepest frame; if it's idle polling, skip.
                        _deepest = _stack[-1] if _stack else ""
                        if any(_m in _deepest for _m in _IDLE_MARKERS):
                            continue   # healthy idle wait, not a block
                        print(f"[looplag] Petla ZABLOKOWANA ~{_behind:.0f}ms "
                              f"(prawdziwa blokada, nie bezczynnosc):", flush=True)
                        for _line in _stack[-5:]:
                            print("  " + _line.strip().replace("\n", " | "),
                                  flush=True)
                    except Exception as _e:
                        print(f"  (zrzut nieudany: {_e})", flush=True)

        _th = _threading.Thread(target=_watchdog, daemon=True, name="loop-watchdog")
        _th.start()

        # Petla odswieza heartbeat tak czesto jak moze — gdy stoi, watek to widzi
        while True:
            self._loop_heartbeat = _t.monotonic()
            await asyncio.sleep(0.02)

    async def _update_check_loop(self):
        """Supervised wrapper: run the update check once at startup, then idle.
        We must NOT return — the supervisor treats a returning task as a crash
        and would restart it every few seconds. So do the one-shot check, then
        sleep forever."""
        try:
            await self._do_update_check()
        except Exception:
            pass   # never let the check crash the supervised task
        while True:
            await asyncio.sleep(3600)

    async def _do_update_check(self):
        """Check GitHub Releases once for a newer HAMCTRL version and store a
        notice for the admin. Never downloads or installs anything — the admin
        fetches the new installer manually. Fully optional and silent:

          - Skipped if GITHUB_REPO is unset, or the admin turned off update
            checks (cfg 'updateCheck' = False).
          - Any network/parse error is swallowed; it must never disrupt startup
            or spam the log. This is the product 'phoning home', so it is a
            single best-effort request, off the hot path, that fails quietly.
        """
        self._update_info = None   # {latest, current, url} once a newer one is found
        # Let the server finish coming up before we touch the network.
        await asyncio.sleep(8.0)

        if not GITHUB_REPO:
            return   # no repo configured yet — nothing to check against
        if self.cfg.get("updateCheck", True) is False:
            return   # admin disabled update checks

        api_url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"

        def _fetch_latest():
            # Runs in a thread — urllib is blocking. Short timeout so a slow or
            # unreachable GitHub never holds anything up.
            import urllib.request, json as _json
            req = urllib.request.Request(api_url, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"HAMCTRL/{SERVER_VERSION}",
            })
            with urllib.request.urlopen(req, timeout=8) as r:
                data = _json.loads(r.read().decode("utf-8"))
            tag = (data.get("tag_name") or "").lstrip("vV")
            html = data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases"
            return tag, html

        try:
            latest, url = await asyncio.to_thread(_fetch_latest)
        except Exception:
            return   # offline, rate-limited, no release yet — stay silent

        if latest and self._version_is_newer(latest, SERVER_VERSION):
            self._update_info = {"latest": latest, "current": SERVER_VERSION,
                                 "url": url}
            print(f"[update] Dostepna nowsza wersja: {latest} "
                  f"(masz {SERVER_VERSION}) — {url}", flush=True)
            # Email the admin — but only ONCE per version. The check runs on
            # every startup, so without this guard a restart would re-send the
            # same notice. We remember the last version we emailed about in cfg;
            # a new version resets it. Opt-in: only if updateEmail is enabled and
            # SMTP is configured.
            already = self.cfg.get("updateEmailedVersion", "")
            if (self.cfg.get("updateEmail", False) and already != latest):
                sent = await self._email_admins_update(latest, url)
                if sent:
                    self.cfg["updateEmailedVersion"] = latest
                    save_json(CFG_F, self.cfg)

    async def _email_admins_update(self, latest: str, url: str) -> bool:
        """Email every admin who has an address about a new version. Returns True
        if at least one email went out (so we can mark this version as notified).
        Best-effort: failures are logged, never raised."""
        subject = f"HAMCTRL — dostepna nowa wersja {latest}"
        body = (
            f"Dostepna jest nowa wersja HAMCTRL: {latest}\n"
            f"Twoja obecna wersja: {SERVER_VERSION}\n\n"
            f"Pobierz nowy instalator ze strony wydania:\n  {url}\n\n"
            f"Instalator zaktualizuje istniejaca instalacje, zachowujac dane "
            f"(konta, konfiguracje, dziennik QSO).\n\n"
            f"To powiadomienie mozna wylaczyc w ustawieniach administratora.\n\n"
            f"73 de HAMCTRL\n"
        )
        any_sent = False
        for u in self.users:
            try:
                if u.get("role") != "admin":
                    continue
                email_addr = u.get("email", "")
                if not email_addr:
                    continue
                ok, _err = await self._send_email(email_addr, subject, body)
                any_sent = any_sent or ok
            except Exception:
                continue
        return any_sent

    @staticmethod
    def _version_is_newer(a: str, b: str) -> bool:
        """True if version string `a` is newer than `b`. Compares dotted numeric
        parts (1.10 > 1.9); a non-numeric suffix like '-dev' sorts as older so a
        clean release always beats a dev build of the same number."""
        def _parts(v):
            v = v.strip().lstrip("vV")
            dev = 1
            if "-" in v:
                v, _suf = v.split("-", 1)
                dev = 0   # '1.0-dev' < '1.0'
            nums = []
            for p in v.split("."):
                try:
                    nums.append(int(p))
                except ValueError:
                    nums.append(0)
            return nums, dev
        pa, da = _parts(a)
        pb, db = _parts(b)
        # pad to equal length
        n = max(len(pa), len(pb))
        pa += [0] * (n - len(pa))
        pb += [0] * (n - len(pb))
        if pa != pb:
            return pa > pb
        return da > db

    def find_user(self, username: str) -> dict | None:
        return next((u for u in self.users if u["username"].lower() == username.lower()), None)

    def find_user_by_id(self, uid: str) -> dict | None:
        return next((u for u in self.users if u["id"] == uid), None)

    def find_user_by_email(self, email: str) -> dict | None:
        return next((u for u in self.users
                     if (u.get("email") or "").lower() == email.lower()), None)

    # ── Radio Lock helpers ────────────────────────────────────────────────────
    def _radio_lock_state(self) -> dict:
        """Serializowalny stan blokady radia (do broadcastu WS i API)."""
        return {
            "type":          "radio_lock_state",
            "locked":        self.radio_lock["user_id"] is not None,
            "user_id":       self.radio_lock["user_id"],
            "username":      self.radio_lock["username"],
            "callsign":      self.radio_lock["callsign"],
            "locked_at":     self.radio_lock["locked_at"],
            "timeout_min":   self.radio_lock["timeout_min"],
            "requests":      [
                {"user_id":    uid,
                 "username":   r["username"],
                 "callsign":   r["callsign"],
                 "requested_at": r["requested_at"]}
                for uid, r in self.radio_requests.items()
            ],
        }

    def _online_users_state(self) -> list:
        """Lista aktualnie polaczonych uzytkownikow."""
        return [
            {"user_id":  v["user_id"],
             "username": v["username"],
             "callsign": v["callsign"],
             "role":     v["role"],
             "has_lock": v["user_id"] == self.radio_lock["user_id"]}
            for v in self.online_users.values()
        ]

    def _user_has_lock(self, user_id: str) -> bool:
        return self.radio_lock["user_id"] == user_id

    def _lock_radio(self, user: dict):
        """Przejmij radio dla uzytkownika."""
        self.radio_lock.update({
            "user_id":       user["id"],
            "username":      user["username"],
            "callsign":      user.get("callsign", user["username"]),
            "locked_at":     time.time(),
            "last_activity": time.time(),
        })
        # Usun prosbe tego usera z kolejki (jesli byla)
        self.radio_requests.pop(user["id"], None)

    def _release_radio(self):
        """Zwolnij radio."""
        # Kto oddaje/traci radio - zatrzymaj tez WSJT-X jesli byl jego wlascicielem.
        # Wywolane BEZ await bo _release_radio jest sync (uzywane tez z watchdog).
        # Faktyczne zatrzymanie odbywa sie asynchronicznie.
        prev_owner = self.radio_lock.get("user_id")
        if prev_owner and prev_owner == self._ft8_rx_owner_uid and self._ft8_rx_enabled:
            asyncio.ensure_future(self._stop_wsjtx_auto("oddanie radia"))
        self.radio_lock.update({
            "user_id": None, "username": None,
            "callsign": None, "locked_at": None, "last_activity": None,
        })

    async def _stop_wsjtx_auto(self, reason: str):
        """Zatrzymaj WSJT-X automatycznie z podanego powodu.

        Wywolywane gdy:
        - Owner WSJT-X rozlaczyl sie (WS close)
        - Owner oddal radio (recznie lub przez watchdog idle)
        - Owner stracil radio przez WYMUS admina
        - Owner wylogowal sie

        Zapewnia że dekodowanie nie leci "w tle" gdy nikt nie kontroluje
        radia, i tez nie blokuje audio TX pipeline dla innych userów."""
        if not self._ft8_rx_enabled:
            return
        print(f"[ft8rx] AUTO-STOP: {reason} (owner={self._ft8_rx_owner_uid})", flush=True)
        self._ft8_rx_enabled = False
        self._ft8_rx_owner_uid = None
        if self.rust_audio:
            try:
                await self.rust_audio.ft8_enable_rx(False, self._ft8_decode_mode)
            except Exception as e:
                print(f"[ft8rx] auto-stop rust_audio blad: {e}")
        # Broadcast żeby wszystkie UI wiedziały że WSJT-X jest zatrzymany
        await self.hub.broadcast({"type": "ft8_rx_status", "enabled": False})
        await self.hub.broadcast({"type": "wsjtx_status", "running": False,
                                   "decoding": False, "transmit": False})
        # Toast informacyjny (idzie na kanał 'ft8' automatycznie? Nie - to
        # jest control level info dla wszystkich)
        await self.hub.broadcast({"type": "toast",
                                   "msg": f"🛑 WSJT-X zatrzymany automatycznie ({reason})",
                                   "level": "warn"})

    def touch_activity(self, user_id: str):
        """Zaktualizuj czas ostatniej aktywnosci aktywnego operatora."""
        if self._user_has_lock(user_id):
            self.radio_lock["last_activity"] = time.time()

    async def _start_tx_watchdog(self):
        """Watchdog TX — auto-off PTT po X sekundach nieprzerwanego nadawania.
        Chroni przed pozostawionym wlaczonym PTT (zawieszenie, zamkniety laptop).
        Timeout konfigurowalny w cfg['tx_watchdog_s'] (domyslnie 180s).
        Ostatnie 30s watchdog wysyla powiadomienia toast co 10s zeby operator
        widzial ze zbliza sie limit."""
        # Anuluj poprzedni watchdog jesli byl aktywny
        if hasattr(self, '_tx_watchdog_task') and self._tx_watchdog_task and not self._tx_watchdog_task.done():
            self._tx_watchdog_task.cancel()

        async def _watchdog():
            timeout = float(self.cfg.get("tx_watchdog_s", 180.0))
            if timeout <= 0:
                return  # wylaczony w konfiguracji
            try:
                # Broadcast start watchdog dla UI (żeby zaczął pokazywać licznik)
                await self.hub.broadcast({
                    "type": "tx_watchdog_start",
                    "timeout": timeout,
                })
                # Ostrzezenia w ostatnich 30s
                warn_at = max(timeout - 30, 0)
                await asyncio.sleep(warn_at)
                if not self.rig.ptt:
                    await self.hub.broadcast({"type": "tx_watchdog_cancel"})
                    return
                # Broadcast co sekunde w ostatnich 30s dla plynnego countdown UI
                # + toast wa`rning na 30/20/10s zeby uzytkownik widzial takze
                # gdy nie ma otwartego panelu FT8 (globalny toast).
                for remain in range(30, 0, -1):
                    if not self.rig.ptt:
                        await self.hub.broadcast({"type": "tx_watchdog_cancel"})
                        return
                    await self.hub.broadcast({
                        "type": "tx_watchdog_countdown",
                        "remaining": remain,
                    })
                    if remain in (30, 20, 10):
                        await self.hub.broadcast({
                            "type": "toast",
                            "msg": f"⚠ Watchdog TX: automatyczne wylaczenie za {remain}s",
                            "level": "warn",
                        })
                    await asyncio.sleep(1)
                # Wymus PTT OFF
                if self.rig.ptt:
                    self.rig.ptt = False
                    if not self.rig.sim:
                        try: await self.rig.set_ptt(False)
                        except Exception as e:
                            print(f"[watchdog] set_ptt(False) blad: {e}")
                    await self.hub.broadcast({"type": "ptt", "ptt": False})
                    await self.hub.broadcast({"type": "tx_watchdog_cancel"})
                    await self.hub.broadcast({
                        "type": "toast",
                        "msg": f"🛑 Watchdog TX zadzialal — PTT wylaczone po {timeout:.0f}s",
                        "level": "error",
                    })
            except asyncio.CancelledError:
                await self.hub.broadcast({"type": "tx_watchdog_cancel"})
                pass

        self._tx_watchdog_task = asyncio.create_task(_watchdog())

    async def _start_tune(self, duration_s: float, tone_hz: float = 1500.0):
        """Nadaj stały ton przez duration_s sekund dla strojenia ATU.

        Generuje PCM sinusoide o czestotliwosci tone_hz (default 1500Hz - w
        srodku pasma SSB), 16-bit, mono, 12000 Hz sample rate. Wysyla przez
        istniejacy audio TX pipeline. PTT wlaczony bezposrednio przez CI-V,
        wylaczony po zakonczeniu. Aktywnie sprawdza self._tune_stop.
        """
        import math
        try:
            self._tune_stop = False
            print(f"[tune] START {tone_hz}Hz na {duration_s}s")
            await self.hub.broadcast({"type": "tune_status", "active": True,
                                      "duration": duration_s, "tone": tone_hz})

            # Wlacz PTT
            self.rig.ptt = True
            if not self.rig.sim:
                try: await self.rig.set_ptt(True)
                except Exception as e: print(f"[tune] set_ptt blad: {e}")
            await self.hub.broadcast({"type": "ptt", "ptt": True})

            # Generuj tone PCM i puszczaj przez audio stream
            sample_rate = 12000
            chunk_size = 4800  # 400ms per chunk
            total_samples = int(duration_s * sample_rate)
            samples_sent = 0
            phase = 0.0
            two_pi_over_sr = 2.0 * math.pi * tone_hz / sample_rate
            vol = min(float(self.cfg.get("audio", {}).get("txVolume", 1.0)), 8.0)
            amp_scale = 0.6 * vol

            while samples_sent < total_samples and not self._tune_stop:
                chunk = bytearray()
                for _ in range(chunk_size):
                    if samples_sent >= total_samples: break
                    v = int(math.sin(phase) * 32767 * amp_scale)
                    v = max(-32767, min(32767, v))
                    chunk += v.to_bytes(2, 'little', signed=True)
                    phase += two_pi_over_sr
                    if phase > math.pi * 2: phase -= math.pi * 2
                    samples_sent += 1
                if hasattr(self.audio, 'feed_tx_pcm'):
                    try: self.audio.feed_tx_pcm(bytes(chunk))
                    except Exception as e: print(f"[tune] feed blad: {e}")
                await asyncio.sleep(chunk_size / sample_rate * 0.9)

            # Wylacz PTT
            self.rig.ptt = False
            if not self.rig.sim:
                try: await self.rig.set_ptt(False)
                except Exception as e: print(f"[tune] set_ptt(False) blad: {e}")
            await self.hub.broadcast({"type": "ptt", "ptt": False})
            await self.hub.broadcast({"type": "tune_status", "active": False})
            print(f"[tune] STOP (stop_flag={self._tune_stop})")
        except Exception as e:
            print(f"[tune] blad: {e}")
            self.rig.ptt = False
            if not self.rig.sim:
                try: await self.rig.set_ptt(False)
                except: pass
            await self.hub.broadcast({"type": "ptt", "ptt": False})
            await self.hub.broadcast({"type": "tune_status", "active": False})

    async def _relay_connect_task(self):
        """Polacz z Arduino relay controller w tle."""
        if self.relay:
            await self.relay.connect()

    async def _relay_reconnect_task(self):
        """Rozlacz i polacz ponownie z nowa konfiguracja."""
        if self.relay:
            await self.relay.disconnect()
            self.relay = None
        rcfg = self.cfg.get("relay", {})
        if _RELAY_OK and rcfg.get("enabled") and rcfg.get("port"):
            try:
                self.relay = RelayController(
                    port=rcfg["port"],
                    baudrate=int(rcfg.get("baudrate", 9600)),
                )
                await self.relay.connect()
            except Exception as e:
                print(f"[relay] reconnect blad: {e}")

    def _supervise(self, coro_factory, name: str, restart_delay: float = 5.0):
        """
        Uruchom petle tla pod nadzorem: jesli mimo wewnetrznych try/except
        zadanie padnie (nieoczekiwany wyjatek, bug), supervisor je RESTARTUJE
        po restart_delay sekund.

        Bez tego pojedynczy nieprzewidziany blad zabija funkcje (np. watchdog
        urzadzen albo dekodowanie FT8) az do recznego restartu serwera.

        coro_factory: funkcja bezargumentowa zwracajaca korutyne (nie korutyna!)
                      np. lambda: self._device_watchdog()
        """
        async def _runner():
            _fails = 0
            while True:
                try:
                    await coro_factory()
                    # Petla sie zakonczyla normalnie (nie powinna) - restart
                    print(f"[supervisor] '{name}' zakonczyl sie sam - restart "
                          f"za {restart_delay}s", flush=True)
                except asyncio.CancelledError:
                    print(f"[supervisor] '{name}' anulowany (zamykanie)", flush=True)
                    raise
                except Exception as e:
                    _fails += 1
                    print(f"[supervisor] '{name}' PADL ({_fails}x): "
                          f"{type(e).__name__}: {e}", flush=True)
                    import traceback as _tb
                    _tb.print_exc()
                    # Rosnacy backoff przy powtarzajacych sie awariach
                    # (5s, 10s, 20s, ... max 300s) zeby nie zapetlic sie
                    _delay = min(restart_delay * (2 ** min(_fails - 1, 6)), 300.0)
                    print(f"[supervisor] '{name}' restart za {_delay:.0f}s",
                          flush=True)
                    await asyncio.sleep(_delay)
                    continue
                await asyncio.sleep(restart_delay)

        return asyncio.ensure_future(_runner())

    async def _device_watchdog(self):
        """
        Co 1h sprawdza czy urzadzenia (radio, rotatory, przekazniki) odpowiadaja
        na porcie COM. Jesli port zawieszony / urzadzenie nie odpowiada -
        rozlacza i laczy ponownie (soft reset).

        BEZPIECZENSTWO: nie rusza niczego gdy:
          - radio nadaje (PTT aktywne) - nie przerywamy transmisji
          - ktos trzyma radio_lock - nie przerywamy cudzego QSO
        W takiej sytuacji sprawdzenie jest odkladane do nastepnego cyklu.

        Radio ma juz wlasny _reconnect_loop w civ.py (auto-wraca z SIM gdy port
        sie zwolni), wiec dla radia tylko logujemy stan i probujemy wybudzic
        gdy jest w SIM mimo dostepnego portu.
        """
        INTERVAL_S = 3600.0   # 1h
        # Pierwsze sprawdzenie po 5 min od startu (dac czas na inicjalizacje)
        await asyncio.sleep(300.0)

        while True:
            try:
                # ── Warunki bezpieczenstwa ────────────────────────────────────
                ptt_active = bool(getattr(self.rig, "ptt", False))
                lock_held = self.radio_lock.get("user_id") is not None
                if ptt_active or lock_held:
                    reason = "PTT aktywne" if ptt_active else "operator trzyma radio"
                    print(f"[watchdog] pomijam sprawdzenie urzadzen ({reason})",
                          flush=True)
                    await asyncio.sleep(INTERVAL_S)
                    continue

                print("[watchdog] sprawdzam urzadzenia...", flush=True)

                await self._watchdog_check_radio()
                await self._watchdog_check_rotators()
                await self._watchdog_check_relay()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[watchdog] blad ogolny: {e}", flush=True)

            await asyncio.sleep(INTERVAL_S)

    async def _watchdog_check_radio(self):
        """
        Radio: sprawdz czy CI-V odpowiada. Radio ma wlasny reconnect_loop w
        civ.py wiec tutaj tylko diagnostyka + wymuszenie proby gdy w SIM.
        """
        try:
            is_sim = bool(getattr(self.rig, "sim", False))
            has_port = getattr(self.rig, "_ser", None) is not None
            if is_sim:
                print("[watchdog] radio w SYMULACJI - civ reconnect_loop "
                      "sprobuje odzyskac port automatycznie", flush=True)
                return
            if not has_port:
                print("[watchdog] radio: brak otwartego portu", flush=True)
                return
            # Health-check: zapytaj o czestotliwosc (komenda 0x03).
            # Jesli radio nie odpowiada, get_freq zwroci None/rzuci wyjatek.
            ok = False
            try:
                if hasattr(self.rig, "get_freq"):
                    # get_freq jest async (CivRig) - wolamy bezposrednio
                    f = await asyncio.wait_for(self.rig.get_freq(), timeout=5.0)
                    ok = f is not None and f > 0
            except Exception:
                ok = False
            if ok:
                print("[watchdog] radio OK (odpowiada na CI-V)", flush=True)
            else:
                print("[watchdog] radio NIE ODPOWIADA - probuje reconnect",
                      flush=True)
                # Zamknij port - civ _reconnect_loop otworzy go ponownie
                try:
                    if hasattr(self.rig, "_ser") and self.rig._ser:
                        self.rig._ser.close()
                        self.rig._ser = None
                    if hasattr(self.rig, "_start_sim"):
                        self.rig._start_sim()  # to odpali _reconnect_loop
                    print("[watchdog] radio: port zamkniety, reconnect_loop "
                          "sprobuje odzyskac", flush=True)
                except Exception as e:
                    print(f"[watchdog] radio reconnect blad: {e}", flush=True)
        except Exception as e:
            print(f"[watchdog] radio check blad: {e}", flush=True)

    async def _watchdog_check_rotators(self):
        """Rotatory: sprawdz czy odpowiadaja na STATUS, reconnect gdy nie."""
        rotators = getattr(self, "rotators", None)
        if not rotators:
            return
        for rot in rotators:
            try:
                if getattr(rot, "sim", False):
                    # W symulacji - sprobuj polaczyc na nowo (moze port wrocil)
                    print(f"[watchdog] rotator {rot.name}: w symulacji, "
                          f"probuje polaczyc", flush=True)
                    await asyncio.to_thread(rot.connect)
                    continue
                if not getattr(rot, "connected", False):
                    print(f"[watchdog] rotator {rot.name}: rozlaczony, "
                          f"probuje polaczyc", flush=True)
                    await asyncio.to_thread(rot.connect)
                    continue
                # Health-check: odczytaj pozycje (realne zapytanie do portu)
                pos = None
                try:
                    pos = await asyncio.wait_for(
                        asyncio.to_thread(rot._read_pos, 3.0), timeout=5.0)
                except Exception:
                    pos = None
                if pos is not None:
                    print(f"[watchdog] rotator {rot.name} OK (az={pos:.0f}°)",
                          flush=True)
                else:
                    print(f"[watchdog] rotator {rot.name} NIE ODPOWIADA - "
                          f"reconnect", flush=True)
                    try:
                        await asyncio.to_thread(rot.close)
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)
                    ok = await asyncio.to_thread(rot.connect)
                    print(f"[watchdog] rotator {rot.name} reconnect: "
                          f"{'OK' if ok else 'NIEUDANY (symulacja)'}", flush=True)
            except Exception as e:
                print(f"[watchdog] rotator {getattr(rot,'name','?')} blad: {e}",
                      flush=True)

    async def _watchdog_check_relay(self):
        """Przekazniki: sprawdz czy Arduino odpowiada na RPK, reconnect gdy nie."""
        rcfg = self.cfg.get("relay", {})
        if not rcfg.get("enabled") or not rcfg.get("port"):
            return
        relay = getattr(self, "relay", None)
        if not relay:
            print("[watchdog] relay: brak instancji, probuje polaczyc", flush=True)
            await self._relay_reconnect_task()
            return
        try:
            if not relay.is_connected():
                print("[watchdog] relay: rozlaczony, reconnect", flush=True)
                await self._relay_reconnect_task()
                return
            # Health-check: odczytaj stany (realne zapytanie RPK do Arduino)
            ok = False
            try:
                states = await asyncio.wait_for(
                    relay.read_all_states(), timeout=5.0)
                ok = states is not None
            except Exception:
                ok = False
            if ok:
                print("[watchdog] relay OK (Arduino odpowiada)", flush=True)
            else:
                print("[watchdog] relay NIE ODPOWIADA - reconnect", flush=True)
                await self._relay_reconnect_task()
                if self.relay and self.relay.is_connected():
                    print("[watchdog] relay reconnect OK", flush=True)
                else:
                    print("[watchdog] relay reconnect NIEUDANY", flush=True)
        except Exception as e:
            print(f"[watchdog] relay check blad: {e}", flush=True)

    async def _radio_lock_watchdog(self):
        """
        Co 30s sprawdza czy aktywny operator nie przekroczyl timeout bezczynnosci.
        Jesli tak — automatycznie zwalnia radio i broadcastuje stan.
        """
        await asyncio.sleep(5)   # daj chwile na inicjalizacje
        while True:
            await asyncio.sleep(30)
            try:
                uid = self.radio_lock["user_id"]
                if not uid:
                    continue
                last = self.radio_lock["last_activity"] or self.radio_lock["locked_at"] or 0
                timeout_s = self.radio_lock["timeout_min"] * 60
                if time.time() - last > timeout_s:
                    uname = self.radio_lock["username"]
                    self._release_radio()
                    await self.hub.broadcast({
                        "type":    "radio_lock_state",
                        **self._radio_lock_state(),
                    })
                    await self.hub.broadcast({
                        "type":    "toast",
                        "message": f"Radio zwolnione automatycznie ({uname} — przekroczono czas bezczynnosci {self.radio_lock['timeout_min']} min)",
                    })
                    print(f"[radio_lock] Auto-release: {uname} przekroczyl timeout {self.radio_lock['timeout_min']} min")
            except Exception as e:
                print(f"[radio_lock] watchdog blad: {e}")

    # ── SMTP — reset hasla ────────────────────────────────────────────────────
    async def _send_email(self, to_email: str, subject: str, body: str):
        """Generic SMTP send (subject + plain-text body) using the same SMTP
        config as password reset. Returns (True, None) or (False, error).
        Used for admin update notices as well as password resets."""
        import smtplib, email.message
        smtp_cfg = self.cfg.get("smtp", {})
        if not smtp_cfg.get("host") or not smtp_cfg.get("user") or not smtp_cfg.get("password"):
            return False, "SMTP niekompletny (host/user/haslo)"
        msg = email.message.EmailMessage()
        msg["Subject"] = subject
        msg["From"]    = smtp_cfg.get("from") or smtp_cfg["user"]
        msg["To"]      = to_email
        msg.set_content(body)
        host = smtp_cfg["host"]; port = int(smtp_cfg.get("port", 587))
        use_tls = smtp_cfg.get("use_tls", True)
        user = smtp_cfg["user"]; password = smtp_cfg["password"]
        def _send():
            if use_tls:
                with smtplib.SMTP(host, port, timeout=15) as s:
                    s.ehlo(); s.starttls(); s.ehlo(); s.login(user, password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                    s.login(user, password); s.send_message(msg)
        try:
            await asyncio.to_thread(_send)
            print(f"[smtp] Email wyslany do {to_email}: {subject}")
            return True, None
        except Exception as e:
            print(f"[smtp] Blad wysylki do {to_email}: {e}")
            return False, str(e)

    async def _send_reset_email(self, to_email: str, username: str, token: str, base_url: str):
        """
        Wyslij email przez SMTP. Zwraca (True, None) lub (False, str_bledu).
        """
        import smtplib, email.message
        smtp_cfg = self.cfg.get("smtp", {})
        if not smtp_cfg.get("host"):
            return False, "Brak hosta SMTP w konfiguracji"
        if not smtp_cfg.get("user"):
            return False, "Brak uzytkownika SMTP w konfiguracji"
        if not smtp_cfg.get("password"):
            return False, "Brak hasla SMTP — zapisz konfiguracje SMTP z haslem"

        reset_url = f"{base_url.rstrip('/')}/reset.html?token={token}"
        msg = email.message.EmailMessage()
        msg["Subject"] = "Ham Radio Control — reset hasla"
        msg["From"]    = smtp_cfg.get("from") or smtp_cfg["user"]
        msg["To"]      = to_email
        msg.set_content(
            f"Czesc {username},\n\n"
            f"Prosba o reset hasla dla konta Ham Radio Control Server.\n\n"
            f"Kliknij ponizszy link (wazny przez 1 godzine):\n\n"
            f"  {reset_url}\n\n"
            f"Jesli nie prosiles o reset hasla — zignoruj ten email.\n\n"
            f"73 de Ham Radio Control Server\n"
        )

        host     = smtp_cfg["host"]
        port     = int(smtp_cfg.get("port", 587))
        use_tls  = smtp_cfg.get("use_tls", True)
        user     = smtp_cfg["user"]
        password = smtp_cfg["password"]

        def _send():
            if use_tls:
                with smtplib.SMTP(host, port, timeout=15) as s:
                    s.ehlo()
                    s.starttls()
                    s.ehlo()
                    s.login(user, password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP_SSL(host, port, timeout=15) as s:
                    s.login(user, password)
                    s.send_message(msg)

        try:
            await asyncio.to_thread(_send)
            print(f"[smtp] Email wyslany do {to_email}")
            return True, None
        except smtplib.SMTPAuthenticationError as e:
            err = f"Blad autoryzacji SMTP ({e.smtp_code}): nieprawidlowy login lub haslo. Dla Gmail uzyj 'App Password'."
            print(f"[smtp] {err}")
            return False, err
        except smtplib.SMTPConnectError as e:
            err = f"Nie mozna polaczyc z {host}:{port} — sprawdz host i port."
            print(f"[smtp] {err}: {e}")
            return False, err
        except smtplib.SMTPException as e:
            err = f"Blad SMTP: {e}"
            print(f"[smtp] {err}")
            return False, err
        except TimeoutError:
            err = f"Timeout polaczenia z {host}:{port} — sprawdz host/port/firewall."
            print(f"[smtp] {err}")
            return False, err
        except Exception as e:
            err = f"Blad wysylania: {type(e).__name__}: {e}"
            print(f"[smtp] {err}")
            return False, err


    def get_rot(self, rid: int) -> Rotator | None:
        return next((r for r in self.rotators if r.id == rid), None)

    def init_rotators(self):
        for r in self.rotators: r.close()
        self.rotators = []
        for rc in self.cfg.get("rotators", []):
            if not rc.get("enabled", True): continue
            rot = Rotator(rc, self.hub.broadcast_sync)
            rot.connect()
            self.rotators.append(rot)
        n = len(self.rotators)
        print(f"[rotator] {n} aktywnych" if n else "[rotator] brak skonfigurowanych")

    def _check_pw_ver(self, payload: dict | None) -> dict | None:
        """
        Sprawdz czy token ma aktualna wersje hasla (pw_ver). Zmiana hasla
        inkrementuje pw_ver w users.json, wiec stare tokeny (z nizsza wersja)
        przestaja dzialac - to uniewaznia sesje na wszystkich urzadzeniach.

        Zwraca payload jesli OK, albo None jesli token przestarzaly/nieprawidlowy.
        Tokeny wystawione przed wprowadzeniem pw_ver (brak pola) sa akceptowane
        tylko gdy user tez nie ma pw_ver (czyli nigdy nie zmienil hasla).
        """
        if not payload:
            return None
        uid = payload.get("id") or payload.get("user_id")
        if not uid:
            return None
        u = next((x for x in self.users if x.get("id") == uid), None)
        if not u:
            return None  # user usuniety
        token_ver = int(payload.get("pw_ver", 0))
        user_ver = int(u.get("pw_ver", 0))
        if token_ver != user_ver:
            print(f"[auth] token odrzucony: pw_ver={token_ver} != {user_ver} "
                  f"(user {u.get('username')!r} zmienil haslo)", flush=True)
            return None
        return payload

    # ── Rate limiting logowania (ochrona przed brute-force) ──────────────────
    # Serwer jest w internecie (duckdns) - boty skanuja i probuja lamac hasla.
    # Limitujemy proby logowania per IP oraz per nazwa uzytkownika.
    #
    # Zasady:
    #   - max 5 nieudanych prob z jednego IP w ciagu 5 minut -> blokada 15 min
    #   - max 5 nieudanych prob na jedna nazwe usera w 5 min -> blokada 15 min
    #     (chroni konkretne konto nawet gdy atak idzie z wielu IP)
    #   - udane logowanie kasuje licznik dla tego IP i usera
    # Dane trzymane w pamieci (reset przy restarcie) - dla klubowego serwera
    # to wystarcza, nie potrzeba Redis.
    _LOGIN_MAX_FAILS = 5           # ile nieudanych prob zanim blokada
    _LOGIN_WINDOW_S = 300.0        # okno liczenia prob (5 min)
    _LOGIN_BLOCK_S = 900.0         # jak dlugo blokada (15 min)

    def _rl_key_state(self, store: dict, key: str) -> dict:
        st = store.get(key)
        if st is None:
            st = {"fails": [], "blocked_until": 0.0}
            store[key] = st
        return st

    def _login_is_blocked(self, ip: str, username: str) -> tuple[bool, int]:
        """Czy logowanie zablokowane? Zwraca (blocked, seconds_left)."""
        now = time.time()
        for store, key in ((self._login_fails_ip, ip),
                           (self._login_fails_user, username)):
            if not key:
                continue
            st = store.get(key)
            if st and st["blocked_until"] > now:
                return True, int(st["blocked_until"] - now)
        return False, 0

    def _login_record_fail(self, ip: str, username: str):
        """Zapisz nieudana probe. Jesli przekroczono limit - zablokuj."""
        now = time.time()
        for store, key, label in ((self._login_fails_ip, ip, "IP"),
                                  (self._login_fails_user, username, "user")):
            if not key:
                continue
            st = self._rl_key_state(store, key)
            # Usun stare proby spoza okna
            st["fails"] = [t for t in st["fails"] if now - t < self._LOGIN_WINDOW_S]
            st["fails"].append(now)
            if len(st["fails"]) >= self._LOGIN_MAX_FAILS:
                st["blocked_until"] = now + self._LOGIN_BLOCK_S
                st["fails"] = []
                print(f"[auth] BLOKADA {label}={key!r} na "
                      f"{int(self._LOGIN_BLOCK_S/60)} min "
                      f"(za duzo nieudanych prob logowania)", flush=True)

    def _login_record_success(self, ip: str, username: str):
        """Udane logowanie - wyczysc liczniki."""
        self._login_fails_ip.pop(ip, None)
        self._login_fails_user.pop(username, None)

    def _login_cleanup(self):
        """
        Usun stare wpisy zeby slowniki nie rosly w nieskonczonosc.
        STABILNOSC: pod atakiem (miliony roznych nazw userow) samo usuwanie
        'pustych' wpisow nie wystarcza - wszystkie maja swieze proby. Dlatego
        dodatkowo usuwamy wpisy ktorych okno prob juz minelo, a jesli slownik
        nadal jest za duzy - twardo przycinamy do najnowszych.
        """
        now = time.time()
        HARD_CAP = 500
        for store in (self._login_fails_ip, self._login_fails_user):
            # 1. Usun wpisy bez aktywnej blokady i bez prob w oknie
            dead = []
            for k, st in store.items():
                st["fails"] = [t for t in st["fails"]
                               if now - t < self._LOGIN_WINDOW_S]
                if st["blocked_until"] < now and not st["fails"]:
                    dead.append(k)
            for k in dead:
                store.pop(k, None)
            # 2. Twardy limit - gdy nadal za duzo (atak), zostaw tylko
            #    aktywne blokady + najnowsze proby
            if len(store) > HARD_CAP:
                blocked = {k: st for k, st in store.items()
                           if st["blocked_until"] > now}
                rest = [(k, st) for k, st in store.items()
                        if st["blocked_until"] <= now]
                # Najnowsze proby na koncu listy fails
                rest.sort(key=lambda kv: (kv[1]["fails"][-1]
                                           if kv[1]["fails"] else 0),
                          reverse=True)
                keep = dict(rest[:max(0, HARD_CAP - len(blocked))])
                keep.update(blocked)  # blokady zawsze zostaja
                store.clear()
                store.update(keep)
                print(f"[auth] cleanup: przyciete do {len(store)} wpisow "
                      f"(mozliwy atak brute-force)", flush=True)

    # ── API ───────────────────────────────────────────────────────────────────
    async def api(self, method: str, path: str, body: dict,
                  user: dict | None, query: dict, client_ip: str = "") -> tuple[int, any]:
        p = path

        if p == "/api/auth/login" and method == "POST":
            un = body.get("username", "").lower()
            pw = body.get("password", "")

            # Rate limiting - sprawdz czy IP/user nie jest zablokowany
            blocked, secs_left = self._login_is_blocked(client_ip, un)
            if blocked:
                mins = max(1, secs_left // 60)
                print(f"[auth] odrzucono logowanie (blokada) ip={client_ip} user={un!r}",
                      flush=True)
                return 429, {"error": f"Za duzo nieudanych prob. "
                                       f"Sprobuj ponownie za {mins} min."}

            u  = self.find_user(un)
            if not u or not u.get("active") or not verify_pw(pw, u.get("password", "")):
                self._login_record_fail(client_ip, un)
                # Sporadyczne czyszczenie starych wpisow. Sprawdzamy OBA slowniki
                # - napastnik moze wysylac miliony roznych nazw userow zeby
                # zapchac pamiec (_login_fails_user rosloby bez limitu).
                if (len(self._login_fails_ip) > 200
                        or len(self._login_fails_user) > 200):
                    self._login_cleanup()
                print(f"[auth] nieudane logowanie ip={client_ip} user={un!r}", flush=True)
                return 401, {"error": "Błędne dane logowania"}

            self._login_record_success(client_ip, un)
            # Transparent upgrade: if this account still has a legacy SHA-256
            # hash, now that we have the correct plaintext, re-hash it with
            # scrypt and persist. Users migrate silently on their next login.
            if needs_rehash(u.get("password", "")):
                u["password"] = hash_pw_secure(pw)
                try:
                    save_json(USR_F, self.users)
                except Exception:
                    pass
            # pw_ver = wersja hasla. Zmiana hasla inkrementuje ja w users.json,
            # co uniewaznia wszystkie stare tokeny (nie pasuja do nowej wersji).
            token = jwt_sign({"id": u["id"], "role": u["role"], "username": u["username"],
                              "pw_ver": int(u.get("pw_ver", 0))})
            # Wymuszenie zmiany domyslnego hasla: gdy admin loguje sie wciaz
            # dziala jacym haslem domyslnym (Admin1234!), frontend pokaze ekran
            # zmiany hasla. KRYTYCZNE dla produktu - kazdy klub startuje z tym
            # samym haslem domyslnym, wiec MUSI je zmienic przy pierwszym wejsciu.
            must_change = (u.get("role") == "admin"
                           and verify_pw(ADMIN_PW, u.get("password", ""))
                           and not u.get("pw_changed"))
            return 200, {"ok": True, "token": token, "role": u["role"],
                         "username": u["username"],
                         "callsign": CALLSIGN, "locator": LOCATOR,
                         "must_change_password": must_change,
                         "first_run": FIRST_RUN}

        # ── Reset hasla: krok 1 — wyslij email z tokenem ──────────────────────
        if p == "/api/auth/reset-request" and method == "POST":
            email_or_user = body.get("email", "").strip().lower()
            # Szukaj po emailu lub nazwie uzytkownika
            u = (self.find_user_by_email(email_or_user) or
                 self.find_user(email_or_user))
            # Zawsze odpowiadaj "ok" — nie ujawniaj czy user istnieje
            if u and u.get("email"):
                from auth import make_reset_token
                token    = make_reset_token(u["id"], u["username"], u["email"])
                base_url = body.get("base_url", f"http://localhost:{PORT}")
                ok, err  = await self._send_reset_email(u["email"], u["username"], token, base_url)
                if not ok:
                    # Do NOT log the token itself — the log is a place a token
                    # could leak from, and it grants account takeover. Log only
                    # that delivery failed so the admin knows to check SMTP.
                    print(f"[auth] SMTP blad przy wysylce resetu dla "
                          f"{u['username']!r}: {err} (token NIE zapisany w logu)")
            return 200, {"ok": True, "message": "Jesli konto istnieje i ma email — link zostal wyslany"}

        # ── Reset hasla: krok 2 — ustaw nowe haslo na podstawie tokenu ────────
        if p == "/api/auth/reset-confirm" and method == "POST":
            token    = body.get("token", "")
            new_pw   = body.get("password", "")
            if len(new_pw) < 6:
                return 400, {"error": "Haslo musi miec minimum 6 znakow"}
            from auth import consume_reset_token
            entry = consume_reset_token(token)
            if not entry:
                return 400, {"error": "Link resetowania jest nieprawid³owy lub wygas³ (wazny 1h)"}
            u = self.find_user_by_id(entry["user_id"])
            if not u:
                return 400, {"error": "Uzytkownik nie istnieje"}
            u["password"] = hash_pw_secure(new_pw)
            u["pw_ver"] = int(u.get("pw_ver", 0)) + 1  # uniewaznij stare tokeny
            save_json(USR_F, self.users)
            print(f"[auth] Haslo zresetowane dla: {u['username']} "
                  f"(pw_ver={u['pw_ver']}, stare sesje uniewaznione)")
            return 200, {"ok": True, "message": "Haslo zostalo zmienione — mozesz sie zalogowac"}

        # ── Reset hasla przez admina (bez emaila) ─────────────────────────────
        if p == "/api/auth/admin-reset" and method == "POST":
            if not user: return 401, {"error": "Wymagane logowanie"}
            if user.get("role") != "admin": return 403, {"error": "Tylko admin"}
            target_id = body.get("user_id")
            new_pw    = body.get("password", "")
            if len(new_pw) < 6:
                return 400, {"error": "Haslo musi miec minimum 6 znakow"}
            u = self.find_user_by_id(target_id)
            if not u: return 404, {"error": "Uzytkownik nie istnieje"}
            u["password"] = hash_pw_secure(new_pw)
            u["pw_ver"] = int(u.get("pw_ver", 0)) + 1  # uniewaznij stare tokeny usera
            save_json(USR_F, self.users)
            print(f"[auth] Admin zresetowal haslo dla {u['username']!r} "
                  f"(pw_ver={u['pw_ver']}, stare sesje uniewaznione)", flush=True)
            return 200, {"ok": True}


        if not user:
            return 401, {"error": "Wymagane logowanie"}

        role = user.get("role", "viewer")
        uid  = user.get("id", "")

        if p == "/api/auth/change-password" and method == "POST":
            old_pw  = body.get("old_password", "")
            new_pw  = body.get("new_password", "")
            u_obj   = self.find_user_by_id(uid)
            if not u_obj: return 404, {"error": "Brak konta"}
            if not verify_pw(old_pw, u_obj.get("password", "")):
                return 403, {"error": "Złe stare hasło"}
            if len(new_pw) < 8:
                return 400, {"error": "Hasło min. 8 znaków"}
            u_obj["password"] = hash_pw_secure(new_pw)
            # Oznacz ze haslo zostalo zmienione z domyslnego - wylacza
            # wymuszenie zmiany hasla przy logowaniu (must_change_password).
            u_obj["pw_changed"] = True
            # Inkrementuj wersje hasla - uniewaznia WSZYSTKIE stare tokeny JWT
            # (takze na innych urzadzeniach). Uzytkownik musi zalogowac sie
            # ponownie wszedzie. Wazne gdy haslo zmieniane bo wyciekło.
            u_obj["pw_ver"] = int(u_obj.get("pw_ver", 0)) + 1
            save_json(USR_F, self.users)
            print(f"[auth] haslo zmienione dla {u_obj.get('username')!r}, "
                  f"pw_ver={u_obj['pw_ver']} (stare tokeny uniewaznione)", flush=True)
            # Wygeneruj nowy token dla biezacej sesji (zeby user nie wylecial)
            new_token = jwt_sign({"id": u_obj["id"], "role": u_obj["role"],
                                   "username": u_obj["username"],
                                   "pw_ver": u_obj["pw_ver"]})
            return 200, {"ok": True, "token": new_token}

        if p == "/api/user/profile" and method == "POST":
            u_obj = self.find_user_by_id(uid)
            if not u_obj: return 404, {"error": "Brak konta"}
            # Walidacja pol (obrona przed XSS - te dane trafiaja do HTML w
            # panelu admina, liscie online itd.)
            if "callsign" in body:
                _cs = str(body.get("callsign", "")).strip().upper()
                if _cs and not re.match(r'^[A-Z0-9/]{1,16}$', _cs):
                    return 400, {"error": "Znak wywolawczy: tylko litery, cyfry i /"}
                u_obj["callsign"] = _cs
            if "locator" in body:
                _loc = str(body.get("locator", "")).strip().upper()
                if _loc and not re.match(r'^[A-Z0-9]{1,8}$', _loc):
                    return 400, {"error": "Lokator: tylko litery i cyfry"}
                u_obj["locator"] = _loc
            if "name" in body:
                _nm = re.sub(r'[<>"\']', '', str(body.get("name", "")).strip())[:64]
                u_obj["name"] = _nm
            if "email" in body:
                u_obj["email"] = str(body.get("email", "")).strip()[:128]
            save_json(USR_F, self.users)
            return 200, {"ok": True}

        if p == "/api/user/tx_eq" and method == "GET":
            u_obj = self.find_user_by_id(uid)
            if not u_obj: return 404, {"error": "Brak konta"}
            return 200, {"ok": True, "tx_eq": u_obj.get("tx_eq", None)}

        if p == "/api/user/tx_eq" and method == "POST":
            u_obj = self.find_user_by_id(uid)
            if not u_obj: return 404, {"error": "Brak konta"}
            # Waliduj strukture: preset (str) i bands (dict z 5 pasmami)
            preset = body.get("preset", "default")
            bands = body.get("bands", {})
            if not isinstance(preset, str) or not isinstance(bands, dict):
                return 400, {"error": "Nieprawidlowa struktura"}
            # Waliduj wartosci pasm (int/float, zakres -20..+15)
            allowed_bands = {"bass", "mud", "clarity", "punch", "air"}
            clean_bands = {}
            for k, v in bands.items():
                if k in allowed_bands and isinstance(v, (int, float)):
                    clean_bands[k] = max(-20, min(15, float(v)))
            u_obj["tx_eq"] = {"preset": preset, "bands": clean_bands}
            save_json(USR_F, self.users)
            return 200, {"ok": True}

        # ── Relay controller (Arduino SP5IOU) ─────────────────────────────────
        if p == "/api/relay/config" and method == "GET":
            # Konfiguracja + lista portow do wyboru (tylko admin)
            if role != "admin":
                return 403, {"error": "Tylko admin"}
            rcfg = self.cfg.get("relay", {})
            # Cache portow na 30s - list_serial_ports() na Windows bywa wolne
            now = time.time()
            if not hasattr(self, '_relay_ports_cache') or now - getattr(self, '_relay_ports_cache_time', 0) > 30:
                self._relay_ports_cache = list_serial_ports() if _RELAY_OK else []
                self._relay_ports_cache_time = now
            return 200, {
                "ok": True,
                "config": rcfg,
                "ports": self._relay_ports_cache,
                "connected": bool(self.relay and self.relay.is_connected()),
                "states": self.relay.get_states() if self.relay else [False] * 8,
                "max_pulse_s": MAX_PULSE_S if _RELAY_OK else 10.0,
            }

        if p == "/api/relay/config" and method == "POST":
            if role != "admin":
                return 403, {"error": "Tylko admin"}
            enabled = bool(body.get("enabled", False))
            port = str(body.get("port", "")).strip()
            baudrate = int(body.get("baudrate", 9600))
            relays = body.get("relays", [])
            if not isinstance(relays, list):
                return 400, {"error": "relays musi byc lista"}
            # Waliduj kazdy przekaznik
            clean_relays = []
            for i, r in enumerate(relays[:8]):
                if not isinstance(r, dict): continue
                mode = r.get("mode", "manual")
                if mode not in ("manual", "momentary"):
                    mode = "manual"
                clean_relays.append({
                    "id": i,
                    "name": str(r.get("name", f"REL{i}"))[:30],
                    "mode": mode,
                    "pulse_s": max(0.1, min(10.0, float(r.get("pulse_s", 1.0)))),
                    "visible": bool(r.get("visible", True)),
                })
            self.cfg["relay"] = {
                "enabled": enabled,
                "port": port,
                "baudrate": baudrate,
                "relays": clean_relays,
            }
            save_json(CFG_F, self.cfg)
            # Restart polaczenia z Arduino
            asyncio.ensure_future(self._relay_reconnect_task())
            return 200, {"ok": True}

        if p == "/api/relay/state" and method == "GET":
            # Dostepne dla wszystkich zalogowanych - stan + widoczne przekazniki per user
            rcfg = self.cfg.get("relay", {})
            configured = rcfg.get("relays", [])
            u_obj = self.find_user_by_id(uid) or {}
            u_perms = u_obj.get("permissions", {})
            # Admin widzi wszystkie, user tylko te ktore ma w permissions[relay_N]
            visible_relays = []
            states = self.relay.get_states() if self.relay else [False] * 8
            for r in configured:
                rid = r.get("id", 0)
                perm_key = f"relay_{rid}"
                allowed = role == "admin" or bool(u_perms.get(perm_key, False))
                if allowed and r.get("visible", True):
                    visible_relays.append({
                        **r,
                        "state": states[rid] if 0 <= rid < 8 else False,
                    })
            return 200, {
                "ok": True,
                "connected": bool(self.relay and self.relay.is_connected()),
                "relays": visible_relays,
            }

        if p == "/api/relay/action" and method == "POST":
            # Uzytkownik klika przycisk przekaznika
            if not self.relay or not self.relay.is_connected():
                return 503, {"error": "Kontroler przekaznikow niepodlaczony"}
            relay_id = int(body.get("id", -1))
            if not 0 <= relay_id < 8:
                return 400, {"error": "Nieprawidlowy id przekaznika"}
            # Sprawdz uprawnienia
            u_obj = self.find_user_by_id(uid) or {}
            u_perms = u_obj.get("permissions", {})
            perm_key = f"relay_{relay_id}"
            if role != "admin" and not u_perms.get(perm_key, False):
                return 403, {"error": "Brak dostepu do tego przekaznika"}
            # Znajdz konfiguracje tego przekaznika
            rcfg = self.cfg.get("relay", {})
            relay_conf = None
            for r in rcfg.get("relays", []):
                if r.get("id") == relay_id:
                    relay_conf = r
                    break
            if not relay_conf:
                return 404, {"error": "Przekaznik nieskonfigurowany"}
            mode = relay_conf.get("mode", "manual")
            if mode == "momentary":
                duration = float(relay_conf.get("pulse_s", 1.0))
                asyncio.ensure_future(self.relay.pulse(relay_id, duration))
                await self.hub.broadcast({"type": "relay_state", "id": relay_id, "state": True})
                # Po impulsie broadcast wylaczenia (uproszczone - opoznione)
                async def _notify_off():
                    await asyncio.sleep(duration + 0.1)
                    await self.hub.broadcast({"type": "relay_state", "id": relay_id, "state": False})
                asyncio.ensure_future(_notify_off())
                return 200, {"ok": True, "mode": "momentary", "duration_s": duration}
            else:  # manual
                await self.relay.toggle(relay_id)
                state = self.relay.get_state(relay_id)
                await self.hub.broadcast({"type": "relay_state", "id": relay_id, "state": state})
                return 200, {"ok": True, "mode": "manual", "state": state}

        # ── DX Cluster ─────────────────────────────────────────────────────────
        if p == "/api/dxcluster/config" and method == "GET":
            u_obj = self.find_user_by_id(uid)
            if not u_obj: return 404, {"error": "Brak konta"}
            cfg = u_obj.get("dxcluster", {})
            # Nie zwracaj hasla w plaintext (tylko flaga czy jest ustawione)
            return 200, {
                "ok": True,
                "config": {
                    "host":         cfg.get("host", ""),
                    "port":         cfg.get("port", 7300),
                    "login":        cfg.get("login", ""),
                    "has_password": bool(cfg.get("password", "")),
                    "auto_connect": bool(cfg.get("auto_connect", False)),
                },
                "connected": bool(self.dxcluster and self.dxcluster.get_client(uid) and self.dxcluster.get_client(uid).is_connected()),
                "history":   self.dxcluster.get_history(uid) if self.dxcluster else [],
            }

        if p == "/api/dxcluster/config" and method == "POST":
            u_obj = self.find_user_by_id(uid)
            if not u_obj: return 404, {"error": "Brak konta"}
            host = str(body.get("host", "")).strip()
            try:
                port = int(body.get("port", 7300))
            except (TypeError, ValueError):
                port = 7300
            login = str(body.get("login", "")).strip()
            password = body.get("password", None)
            auto_connect = bool(body.get("auto_connect", False))
            existing = u_obj.get("dxcluster", {})
            # Zachowaj stare haslo jesli nowe nie zostalo podane (null = brak zmiany)
            if password is None:
                password = existing.get("password", "")
            u_obj["dxcluster"] = {
                "host": host,
                "port": port,
                "login": login,
                "password": password,
                "auto_connect": auto_connect,
            }
            save_json(USR_F, self.users)
            return 200, {"ok": True}

        if p == "/api/dxcluster/connect" and method == "POST":
            if not self.dxcluster:
                return 503, {"error": "DX Cluster niedostepny"}
            u_obj = self.find_user_by_id(uid)
            if not u_obj: return 404, {"error": "Brak konta"}
            cfg = u_obj.get("dxcluster", {})
            if not cfg.get("host") or not cfg.get("login"):
                return 400, {"error": "Skonfiguruj adres serwera i login"}
            asyncio.ensure_future(self.dxcluster.connect_user(
                uid, cfg["host"], int(cfg.get("port", 7300)),
                cfg["login"], cfg.get("password", "")
            ))
            return 200, {"ok": True}

        if p == "/api/dxcluster/disconnect" and method == "POST":
            if not self.dxcluster:
                return 200, {"ok": True}
            asyncio.ensure_future(self.dxcluster.disconnect_user(uid))
            return 200, {"ok": True}

        if p == "/api/dxcluster/command" and method == "POST":
            if not self.dxcluster:
                return 503, {"error": "DX Cluster niedostepny"}
            cmd = str(body.get("cmd", "")).strip()
            if not cmd: return 400, {"error": "Brak komendy"}
            ok = await self.dxcluster.send_command(uid, cmd)
            return 200 if ok else 503, {"ok": ok}

        if p == "/api/dxcluster/spot" and method == "POST":
            # Wyslij spota na klaster DX.
            # Komenda: DX <freq_khz> <call> <komentarz>
            # Klaster sam podpisze spota loginem uzytkownika (per-user polaczenie),
            # wiec spot wychodzi pod znakiem tego kto go wyslal.
            if not self.dxcluster:
                return 503, {"ok": False, "error": "DX Cluster niedostępny"}

            call    = str(body.get("call", "")).strip().upper()
            freq_hz = body.get("freq_hz", 0)
            comment = str(body.get("comment", "")).strip()

            # ── Walidacja (spot idzie na CALY SWIAT - nie wysylamy smieci) ────
            if not call or not re.match(r'^[A-Z0-9/]{3,16}$', call):
                return 400, {"ok": False,
                             "error": "Nieprawidłowy znak (litery, cyfry, /)"}
            try:
                freq_hz = int(float(freq_hz))
            except (ValueError, TypeError):
                return 400, {"ok": False, "error": "Nieprawidłowa częstotliwość"}
            # Rozsadny zakres: 1.8 MHz - 1300 MHz
            if not (1_800_000 <= freq_hz <= 1_300_000_000):
                return 400, {"ok": False,
                             "error": "Częstotliwość poza zakresem (1.8 MHz – 1.3 GHz)"}
            # Komentarz: max 30 znakow (limit wiekszosci klastrow), bez znakow
            # sterujacych ktore moglyby zepsuc protokol telnet
            comment = re.sub(r'[\r\n\t]', ' ', comment)[:30].strip()

            # Klaster oczekuje czestotliwosci w kHz (np. 14074.0 dla 14.074 MHz).
            # Podajemy z jedna cyfra po przecinku - taki jest zwyczaj.
            freq_khz = freq_hz / 1000.0
            khz_str = f"{freq_khz:.1f}".rstrip("0").rstrip(".")

            cmd = f"DX {khz_str} {call}"
            if comment:
                cmd += f" {comment}"

            ok = await self.dxcluster.send_command(uid, cmd)
            if ok:
                u = self.find_user_by_id(uid) or {}
                print(f"[dx] spot wyslany przez {u.get('callsign') or u.get('username')}: "
                      f"{cmd}", flush=True)
                return 200, {"ok": True, "sent": cmd}
            return 503, {"ok": False,
                         "error": "Brak połączenia z klastrem — połącz się najpierw"}

        if p == "/api/dxcluster/history" and method == "GET":
            if not self.dxcluster:
                return 200, {"ok": True, "history": []}
            return 200, {"ok": True, "history": self.dxcluster.get_history(uid)}

        # ── Status serwera + diagnostyka (admin) ─────────────────────────────
        if p == "/api/health" and method == "GET":
            # Lekki status dla zakladki Ustawienia (panel "Stan systemu").
            # Publiczny - to tylko podstawowe zdrowie serwera, bez danych
            # wrazliwych. Pola dopasowane do tego czego oczekuje settings.js
            # loadStatus(). (Pole 'node' zostaje dla zgodnosci z frontendem -
            # to zaszlosc po szablonie Node.js, podajemy wersje Pythona.)
            import platform as _plat
            try:
                _clients = len(getattr(self.hub, "_clients", set()))
            except Exception:
                _clients = 0
            return 200, {
                "ok":        True,
                "uptime":    round(time.time() - self._server_start_time),
                "node":      f"Python {_plat.python_version()}",
                "platform":  _plat.system(),
                "hamlib":    (not bool(getattr(self.rig, "sim", True))),
                "audio":     bool(getattr(self.audio, "rx_active", False)),
                "listeners": _clients,
            }

        if p == "/api/status/perf" and method == "GET":
            # Szczegolowa diagnostyka wydajnosci - CPU, watki, petle, klienci.
            # Pomaga ocenic czy serwer wyrabia przy wielu userach.
            if role != "admin":
                return 403, {"error": "Tylko admin"}
            import threading as _th
            import platform as _plat

            out = {
                "ok": True,
                "uptime_s": round(time.time() - self._server_start_time, 1),
                "python": _plat.python_version(),
                "platform": _plat.system(),
            }

            # ── Event loop (asyncio) ──────────────────────────────────────────
            try:
                _loop = asyncio.get_running_loop()
                _tasks = [t for t in asyncio.all_tasks(_loop) if not t.done()]
                out["event_loop"] = {
                    "backend": _JSON_BACKEND,  # orjson / stdlib
                    "impl": type(_loop).__name__,  # uvloop/winloop = szybszy
                    "active_tasks": len(_tasks),
                    # Nazwy petli tla (do diagnostyki co dziala)
                    "task_names": sorted({
                        (t.get_coro().__qualname__.split('.')[-1]
                         if hasattr(t, 'get_coro') and t.get_coro() else '?')
                        for t in _tasks
                    })[:20],
                }
            except Exception as e:
                out["event_loop"] = {"error": str(e)}

            # ── Watki Pythona ─────────────────────────────────────────────────
            try:
                threads = _th.enumerate()
                out["threads"] = {
                    "count": len(threads),
                    "names": sorted(t.name for t in threads)[:20],
                }
            except Exception as e:
                out["threads"] = {"error": str(e)}

            # ── CPU / RAM / procesy (psutil) ──────────────────────────────────
            # psutil on Windows blocks even with interval=None: cpu_times /
            # process_iter read per-process info from the OS synchronously
            # (looplag stack: cpu_percent -> _proc_info, and process_iter over
            # every system process). Since this endpoint is admin-only and polled
            # on a timer, run the entire psutil gather in a thread so it never
            # freezes the event loop (which was causing the audio hitches).
            def _gather_psutil():
                _r = {}
                try:
                    import psutil as _ps
                    _proc = _ps.Process()
                    with _proc.oneshot():
                        _cpu_proc = _proc.cpu_percent(interval=None)
                        _mem = _proc.memory_info()
                        _nthreads = _proc.num_threads()
                    _r["cpu"] = {
                        "cores_physical": _ps.cpu_count(logical=False),
                        "cores_logical": _ps.cpu_count(logical=True),
                        "system_percent": _ps.cpu_percent(interval=None),
                        "per_core_percent": _ps.cpu_percent(interval=None, percpu=True),
                        "python_process_percent": _cpu_proc,
                        "python_threads": _nthreads,
                    }
                    _vm = _ps.virtual_memory()
                    _r["memory"] = {
                        "system_percent": _vm.percent,
                        "system_total_mb": round(_vm.total / 1048576),
                        "python_rss_mb": round(_mem.rss / 1048576, 1),
                    }
                    _rust = None
                    for pr in _ps.process_iter(["name", "cpu_percent", "memory_info"]):
                        try:
                            if (pr.info["name"] or "").lower().startswith("ham_audio"):
                                _rust = {
                                    "pid": pr.pid,
                                    "cpu_percent": pr.cpu_percent(interval=None),
                                    "rss_mb": round(pr.info["memory_info"].rss / 1048576, 1),
                                }
                                break
                        except Exception:
                            continue
                    _r["rust_audio_process"] = _rust or {"running": False}
                except ImportError:
                    _r["cpu"] = {"error": "psutil niedostepny"}
                return _r
            try:
                out.update(await asyncio.to_thread(_gather_psutil))
            except Exception as _pe:
                out["cpu"] = {"error": str(_pe)}
            # ── Klienci WS + subskrypcje kanalow ──────────────────────────────
            try:
                _clients = getattr(self.hub, "_clients", set())
                _subs = getattr(self.hub, "_subs", {})
                # Policz ilu klientow subskrybuje kazdy kanal
                _chan_count = {}
                for _ws, _chans in _subs.items():
                    for ch in (_chans or []):
                        _chan_count[ch] = _chan_count.get(ch, 0) + 1
                out["websocket"] = {
                    "clients": len(_clients),
                    "online_users": len(getattr(self, "online_users", {})),
                    "channel_subscribers": _chan_count,
                }
            except Exception as e:
                out["websocket"] = {"error": str(e)}

            # ── Stan hot-path (co obciaza CPU) ────────────────────────────────
            out["workload"] = {
                "ft8_rx_enabled": bool(getattr(self, "_ft8_rx_enabled", False)),
                "ft8_decode_mode": getattr(self, "_ft8_decode_mode", None),
                "audio_rx_active": bool(getattr(self.audio, "rx_active", False)),
                "rust_audio_connected": bool(getattr(self, "rust_audio", None)),
                "rig_sim": bool(getattr(self.rig, "sim", True)),
                "ptt": bool(getattr(self.rig, "ptt", False)),
                "cq_calling": bool(getattr(self, "_cq_calling", False)),
                "radio_lock_held": self.radio_lock.get("user_id") is not None,
                "com_bridge_clients": len(getattr(
                    getattr(self, "com_bridge_ws", None), "_clients", {}) or {}),
                "rotators": len(getattr(self, "rotators", [])),
                "relay_connected": bool(getattr(self, "relay", None)
                                          and self.relay.is_connected()),
            }
            return 200, out

        if p == "/api/status" and method == "GET":
            if role != "admin":
                return 403, {"error": "Tylko admin"}
            import platform as _plat
            uptime_s = time.time() - self._server_start_time
            # CI-V / rig
            is_civ = isinstance(self.rig, CivRig)
            # Aktywne radio z listy rigs[] (model/port/speed sa tutaj, nie w
            # plaskim cfg). Pierwszy wpis = aktywne radio.
            _active_rig = (self.cfg.get("rigs") or [{}])[0]
            rig_connected = False
            rig_sim = True
            try:
                rig_sim = bool(getattr(self.rig, "sim", True))
                rig_connected = is_civ and not rig_sim and getattr(self.rig, "_ser", None) is not None
            except Exception:
                pass
            # Audio backend
            audio_backend = "rust" if self.rust_audio else ("ffmpeg" if getattr(self.audio, "_ffmpeg", None) else "python")
            # CPU/RAM przez psutil (jesli dostepny)
            cpu_pct = None
            ram_pct = None
            ram_used_mb = None
            try:
                import psutil as _ps
                cpu_pct = _ps.cpu_percent(interval=None)   # nieblokujace (patrz wyzej)
                vm = _ps.virtual_memory()
                ram_pct = vm.percent
                ram_used_mb = round(vm.used / (1024*1024))
            except Exception:
                pass
            return 200, {
                "ok": True,
                "uptime_s": round(uptime_s),
                "version": SERVER_VERSION,
                # Update notice — only surfaced to admins (operators can't act on
                # it). None unless a newer release was found on GitHub.
                "update": (getattr(self, "_update_info", None)
                           if role == "admin" else None),
                "python": _plat.python_version(),
                "platform": _plat.system(),
                "online_count": len(self.online_users),
                "rig": {
                    "backend": "civ" if is_civ else "hamlib",
                    "connected": rig_connected,
                    "sim": rig_sim,
                    # Model/port sa w cfg["rigs"][0] (lista radiotelefonow),
                    # nie w plaskim cfg["model"]. Czytamy z aktywnego radia,
                    # z fallbackiem na stare pola i wartosc realnie uzywana
                    # przez sterownik (self.rig.port/speed).
                    "model": (_active_rig.get("model")
                              or self.cfg.get("model") or "?"),
                    "port": (getattr(self.rig, "port", None)
                             or _active_rig.get("port")
                             or self.cfg.get("civ_port")
                             or self.cfg.get("port") or "?"),
                    "speed": (getattr(self.rig, "speed", None)
                              or _active_rig.get("speed") or "?"),
                    "freq": getattr(self.rig, "freq", 0),
                },
                "audio": {
                    "backend": audio_backend,
                    "rust": bool(self.rust_audio),
                },
                "dxcluster": {
                    "available": bool(self.dxcluster),
                },
                "relay": {
                    "available": _RELAY_OK,
                    "connected": bool(self.relay and self.relay.is_connected()),
                },
                "system": {
                    "cpu_pct": cpu_pct,
                    "ram_pct": ram_pct,
                    "ram_used_mb": ram_used_mb,
                },
            }

        # Test polaczenia CI-V — wysyla testowy odczyt freq
        if p == "/api/status/test_civ" and method == "POST":
            if role != "admin":
                return 403, {"error": "Tylko admin"}
            if not isinstance(self.rig, CivRig):
                return 200, {"ok": False, "message": "Radio nie jest w trybie CI-V (Hamlib/sim)"}
            try:
                freq = getattr(self.rig, "freq", 0)
                sim = getattr(self.rig, "sim", True)
                if sim:
                    return 200, {"ok": False, "message": "Radio w trybie symulacji — brak fizycznego CI-V"}
                return 200, {"ok": True, "message": f"CI-V odpowiada, freq={freq/1e6:.3f} MHz"}
            except Exception as e:
                return 200, {"ok": False, "message": f"Blad CI-V: {e}"}

        # ── Backup / Restore konfiguracji (admin) ────────────────────────────
        if p == "/api/backup" and method == "GET":
            if role != "admin":
                return 403, {"error": "Tylko admin"}
            # Zwroc config.json + users.json jako jeden JSON do pobrania
            # (hasla userow zostawiamy — to backup, admin ma dostep)
            import datetime as _dt
            return 200, {
                "ok": True,
                "backup_version": 1,
                "created": _dt.datetime.utcnow().isoformat() + "Z",
                "server_version": SERVER_VERSION,
                "config": self.cfg,
                "users": self.users,
            }

        if p == "/api/restore" and method == "POST":
            if role != "admin":
                return 403, {"error": "Tylko admin"}
            data = body.get("backup", body)  # akceptuj {backup:{...}} lub bezposrednio
            if not isinstance(data, dict):
                return 400, {"error": "Nieprawidlowy format backupu"}
            restored = []
            if "config" in data and isinstance(data["config"], dict):
                self.cfg = data["config"]
                save_json(CFG_F, self.cfg)
                restored.append("config")
            if "users" in data and isinstance(data["users"], (list, dict)):
                self.users = data["users"]
                save_json(USR_F, self.users)
                restored.append("users")
            if not restored:
                return 400, {"error": "Backup nie zawiera config ani users"}
            return 200, {"ok": True, "restored": restored,
                         "message": "Przywrocono. Zrestartuj serwer aby zastosowac wszystkie zmiany."}

        if p == "/api/auth/me" and method == "GET":
            u_obj = self.find_user_by_id(uid)
            return 200, {"ok": True, "user": {
                "id":          uid,
                "username":    user.get("username", ""),
                "role":        role,
                "callsign":    (u_obj or {}).get("callsign") or user.get("username", ""),
                "name":        (u_obj or {}).get("name", ""),
                "locator":     (u_obj or {}).get("locator") or LOCATOR,
                "email":       (u_obj or {}).get("email", ""),
                "active":      (u_obj or {}).get("active", True),
                "permissions": (u_obj or {}).get("permissions", {}),
                "ft8_timer":   (u_obj or {}).get("ft8_timer", {"duration_min": 6, "user_can_edit": False}),
            }}

        if p == "/api/radio/state" and method == "GET":
            return 200, {**self._radio_lock_state(), "online": self._online_users_state()}

        if p == "/api/radio/lock" and method == "POST":
            """Przejmij radio (jezeli wolne lub jestes adminem)."""
            u_obj = self.find_user_by_id(uid)
            if not u_obj: return 403, {"error": "Brak konta"}
            if self.radio_lock["user_id"] and self.radio_lock["user_id"] != uid and role != "admin":
                holder = self.radio_lock["username"]
                return 409, {"error": f"Radio zajete przez {holder}. Popros o zwolnienie."}
            self._lock_radio({"id": uid, "username": user.get("username",""),
                              "callsign": u_obj.get("callsign", user.get("username",""))})
            await self.hub.broadcast({**self._radio_lock_state(), "online": self._online_users_state()})
            return 200, {"ok": True}

        if p == "/api/radio/release" and method == "POST":
            """Zwolnij radio (tylko aktywny operator lub admin)."""
            if self.radio_lock["user_id"] != uid and role != "admin":
                return 403, {"error": "Nie masz aktywnej blokady radia"}
            released_by = self.radio_lock["username"] or user.get("username", "")
            self._release_radio()
            await self.hub.broadcast({**self._radio_lock_state(), "online": self._online_users_state()})
            await self.hub.broadcast({"type": "toast",
                                      "message": f"✓ Radio zwolnione przez {released_by}"})
            return 200, {"ok": True}

        if p == "/api/radio/request" and method == "POST":
            """Wyslij prosbe o radio do aktywnego operatora."""
            if self._user_has_lock(uid):
                return 400, {"error": "Juz masz radio"}
            if not self.radio_lock["user_id"]:
                # Radio wolne — od razu przejmij
                u_obj = self.find_user_by_id(uid) or {}
                self._lock_radio({"id": uid, "username": user.get("username",""),
                                  "callsign": u_obj.get("callsign", user.get("username",""))})
                await self.hub.broadcast({**self._radio_lock_state(), "online": self._online_users_state()})
                return 200, {"ok": True, "granted": True}
            u_obj = self.find_user_by_id(uid) or {}
            callsign = u_obj.get("callsign", user.get("username",""))
            self.radio_requests[uid] = {
                "username":     user.get("username",""),
                "callsign":     callsign,
                "requested_at": time.time(),
            }
            # Powiadom aktywnego operatora
            await self.hub.broadcast({
                "type":     "radio_request_received",
                "from_uid": uid,
                "from_cs":  callsign,
                "message":  f"{callsign} prosi o dostep do radia",
            })
            await self.hub.broadcast({**self._radio_lock_state(), "online": self._online_users_state()})
            return 200, {"ok": True, "granted": False, "message": "Prosba wyslana"}

        if p == "/api/radio/cancel-request" and method == "POST":
            """Wycofaj prosbe o radio."""
            self.radio_requests.pop(uid, None)
            await self.hub.broadcast({**self._radio_lock_state(), "online": self._online_users_state()})
            return 200, {"ok": True}

        if p == "/api/radio/force-release" and method == "POST":
            """Admin: wymuś zwolnienie radia."""
            if role != "admin": return 403, {"error": "Tylko admin"}
            holder = self.radio_lock["username"] or "?"
            self._release_radio()
            self.radio_requests.clear()
            await self.hub.broadcast({**self._radio_lock_state(), "online": self._online_users_state()})
            await self.hub.broadcast({"type": "toast",
                                      "message": f"Admin wymusil zwolnienie radia (bylo: {holder})"})
            return 200, {"ok": True}

        if p == "/api/radio/timeout" and method == "POST":
            """Admin: zmien timeout bezczynnosci."""
            if role != "admin": return 403, {"error": "Tylko admin"}
            minutes = int(body.get("minutes", 20))
            if not 1 <= minutes <= 480: return 400, {"error": "Zakres: 1-480 min"}
            self.radio_lock["timeout_min"] = minutes
            self.cfg["radio_lock_timeout"] = minutes
            save_json(CFG_F, self.cfg)
            await self.hub.broadcast({**self._radio_lock_state(), "online": self._online_users_state()})
            return 200, {"ok": True}

        # ── SMTP config (admin) ───────────────────────────────────────────────
        if p == "/api/smtp/config" and method == "GET":
            if role != "admin": return 403, {"error": "Tylko admin"}
            smtp = self.cfg.get("smtp", {})
            # Nie wysylaj hasla SMTP do frontendu — tylko **** jesli ustawione
            return 200, {"host": smtp.get("host",""), "port": smtp.get("port", 587),
                         "user": smtp.get("user",""), "from": smtp.get("from",""),
                         "use_tls": smtp.get("use_tls", True),
                         "has_password": bool(smtp.get("password"))}

        if p == "/api/smtp/config" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            smtp = self.cfg.setdefault("smtp", {})
            smtp.update({
                "host":    body.get("host", smtp.get("host","")),
                "port":    int(body.get("port", smtp.get("port", 587))),
                "user":    body.get("user", smtp.get("user","")),
                "from":    body.get("from", smtp.get("from","")),
                "use_tls": bool(body.get("use_tls", smtp.get("use_tls", True))),
            })
            if body.get("password"):  # Nadpisuj haslo tylko jesli podano nowe
                smtp["password"] = body["password"]
            save_json(CFG_F, self.cfg)
            return 200, {"ok": True}

        if p == "/api/smtp/test" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            u_obj    = self.find_user_by_id(uid) or {}
            to_email = body.get("email") or u_obj.get("email", "")
            if not to_email:
                return 400, {"error": "Podaj adres email docelowy w polu 'EMAIL TESTOWY — WYŚLIJ NA'"}
            base_url = body.get("base_url", f"http://localhost:{PORT}")
            ok, err  = await self._send_reset_email(to_email, "test", "TEST_TOKEN_XXXX", base_url)
            if ok:
                return 200, {"ok": True,  "message": f"✓ Email testowy wysłany do {to_email}"}
            return     200, {"ok": False, "message": f"✗ Błąd: {err}"}
            return 200, {"user": udata, "ok": True}

        if p == "/api/update/config" and method == "GET":
            if role != "admin": return 403, {"error": "Tylko admin"}
            return 200, {
                "enabled": self.cfg.get("updateCheck", True),
                "email": self.cfg.get("updateEmail", False),
                "current": SERVER_VERSION,
                "repo_configured": bool(GITHUB_REPO),
                "update": getattr(self, "_update_info", None),
            }

        if p == "/api/update/config" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            if "enabled" in body:
                self.cfg["updateCheck"] = bool(body["enabled"])
            if "email" in body:
                self.cfg["updateEmail"] = bool(body["email"])
            save_json(CFG_F, self.cfg)
            return 200, {"ok": True,
                         "enabled": self.cfg.get("updateCheck", True),
                         "email": self.cfg.get("updateEmail", False)}

        if p == "/api/config" and method == "GET":
            return 200, {"callsign": CALLSIGN, "locator": LOCATOR, "port": PORT,
                         "rigs": self.cfg.get("rigs", []),
                         "rotators": self.cfg.get("rotators", []),
                         "cwMacros": self.cfg.get("cwMacros", DEFAULT_MACROS),
                         "models": HAMLIB_MODELS,
                         "modes":  ["USB","LSB","AM","FM","CW","RTTY","PKTUSB","PKTLSB"],
                         "enabledBands": self.cfg.get("enabledBands", []),
                         "enabledModes": self.cfg.get("enabledModes", [])}

        if p == "/api/config/bands" and method == "GET":
            # Pasma pochodza z PROFILU podlaczonego radia, nie ze wspolnej listy.
            # Dzieki temu IC-9700 pokaze tylko 2m/70cm/23cm (bez HF), a IC-7300
            # HF+6m+4m (bez 2m). Profil wybiera admin przez model radia; gdy
            # brak profilu, get_civ_profile daje bezpieczny fallback.
            try:
                from rigs import get_civ_profile
                _model = str(self.cfg.get("model") or self.cfg.get("rig_model") or "3073")
                _profile = get_civ_profile(_model)
                _pbands = _profile.get("bands", {})
            except Exception:
                _pbands = {}
            if _pbands:
                # format profilu: nazwa -> (min, max, def); UI chce dict min/max/def
                all_bands = {name: {'min': lo, 'max': hi, 'def': df}
                             for name, (lo, hi, df) in _pbands.items()}
            else:
                # Fallback (gdyby profil nie mial pasm) — pelna lista jak dotad
                all_bands = {
                    '160m': {'min':1810000,  'max':2000000,  'def':1850000},
                    '80m':  {'min':3500000,  'max':3800000,  'def':3650000},
                    '60m':  {'min':5351500,  'max':5366500,  'def':5357000},
                    '40m':  {'min':7000000,  'max':7200000,  'def':7100000},
                    '30m':  {'min':10100000, 'max':10150000, 'def':10125000},
                    '20m':  {'min':14000000, 'max':14350000, 'def':14200000},
                    '17m':  {'min':18068000, 'max':18168000, 'def':18100000},
                    '15m':  {'min':21000000, 'max':21450000, 'def':21200000},
                    '12m':  {'min':24890000, 'max':24990000, 'def':24930000},
                    '10m':  {'min':28000000, 'max':29700000, 'def':28400000},
                    '6m':   {'min':50000000, 'max':52000000, 'def':50150000},
                    '4m':   {'min':70000000, 'max':70500000, 'def':70150000},
                    '2m':   {'min':144000000,'max':146000000,'def':144300000},
                    '70cm': {'min':430000000,'max':440000000,'def':432100000},
                }
            enabled = self.cfg.get("enabledBands", list(all_bands.keys()))
            # odsiej pasma spoza profilu (gdyby config pamietal starsze)
            enabled = [b for b in enabled if b in all_bands]
            return 200, {"allBands": all_bands, "enabledBands": enabled}

        if p == "/api/config/station" and method == "GET":
            # Lokator STACJI — miejsce gdzie fizycznie stoi antena. Sluzy do
            # liczenia azymutu rotora na korespondenta (ten sam dla wszystkich
            # userow, bo antena jest jedna). Domyslnie z .env STATION_LOCATOR.
            return 200, {
                "stationLocator": (self.cfg.get("stationLocator")
                                   or LOCATOR or "").strip().upper(),
                "callsign": CALLSIGN,
            }

        if p == "/api/config/station" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            _loc = str(body.get("stationLocator", "")).strip().upper()
            if _loc and not re.match(r"^[A-R]{2}\d{2}([A-X]{2})?$", _loc):
                return 400, {"error": "Zly format lokatora (np. JO72 lub JO72AB)"}
            self.cfg["stationLocator"] = _loc
            save_json(CFG_F, self.cfg)
            await self.hub.broadcast({"type": "init_patch",
                                       "stationLocator": _loc})
            print(f"[config] Lokator stacji ustawiony: {_loc}", flush=True)
            return 200, {"ok": True, "stationLocator": _loc}

        if p == "/api/config/bands" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            self.cfg["enabledBands"] = body.get("enabledBands", [])
            save_json(CFG_F, self.cfg)
            # Broadcast do klientow zeby odswiezyli siatkę pasm
            await self.hub.broadcast({
                "type": "config_update",
                "enabledBands": self.cfg["enabledBands"],
            })
            return 200, {"ok": True}

        if p == "/api/config/modes" and method == "GET":
            all_modes = ["USB","LSB","AM","FM","CW","CW-R","RTTY","RTTY-R",
                         "USB-D","LSB-D","PSK","PSK-R","PKTUSB","PKTLSB",
                         "WFM","DV"]
            enabled = self.cfg.get("enabledModes", ["USB","LSB","AM","FM","CW","RTTY","PKTUSB","PKTLSB"])
            # Tryby z filtrami — admin może przypisać preferowany filtr
            mode_filters = self.cfg.get("modeFilters", {})
            return 200, {"allModes": all_modes, "enabledModes": enabled, "modeFilters": mode_filters}

        if p == "/api/config/modes" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            if "enabledModes" in body:
                self.cfg["enabledModes"] = body["enabledModes"]
            if "modeFilters" in body:
                self.cfg["modeFilters"] = body["modeFilters"]
            save_json(CFG_F, self.cfg)
            await self.hub.broadcast({
                "type": "config_update",
                "enabledModes": self.cfg.get("enabledModes", []),
                "modeFilters":  self.cfg.get("modeFilters", {}),
            })
            return 200, {"ok": True}

        if p == "/api/hamlib/status" and method == "GET":
            mgr = getattr(self, 'hamlib', None)
            if mgr:
                return 200, {"servers": mgr.status()}
            # Fallback — zwróć konfigurację bez stanu (serwer jeszcze nie wystartował)
            cfg_servers = self.cfg.get('hamlibServers', [
                {"port":4532,"enabled":True, "label":"Radio 1 — główne (WSJT-X)"},
                {"port":4533,"enabled":False,"label":"Radio 2 — log/skimmer"},
                {"port":4534,"enabled":False,"label":"Radio 3 — zapasowy"},
            ])
            return 200, {"servers": [
                {**s, "slot":i, "running":False, "clients":0}
                for i, s in enumerate(cfg_servers)
            ]}

        if p == "/api/hamlib/config" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            servers = body.get("servers", [])
            # Waliduj porty
            ports = [s.get("port", 4532+i) for i,s in enumerate(servers)]
            if len(set(ports)) != len(ports):
                return 400, {"error": "Porty muszą być unikalne"}
            for port in ports:
                if not (1024 <= port <= 65535):
                    return 400, {"error": f"Port {port} poza zakresem 1024-65535"}
            self.cfg["hamlibServers"] = servers
            save_json(CFG_F, self.cfg)
            # Zrestartuj jeśli manager aktywny
            mgr = getattr(self, 'hamlib', None)
            if mgr:
                try:
                    await mgr.restart()
                    return 200, {"ok": True, "message": "✓ Konfiguracja zapisana i serwery zrestartowane"}
                except Exception as e:
                    return 200, {"ok": True, "message": f"✓ Zapisano — restart wymagany ({e})"}
            return 200, {"ok": True, "message": "✓ Konfiguracja zapisana — zrestartuj serwer aby zastosować"}

            self.cfg["cwMacros"] = body.get("macros", self.cfg.get("cwMacros", []))
            save_json(CFG_F, self.cfg)
            return 200, {"ok": True}

        m = re.match(r"^/api/config/rig/(\d+)$", p)
        if m and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            rid = m.group(1)
            if not self.cfg.get("rigs"):
                self.cfg["rigs"] = []
            rigs = self.cfg["rigs"]
            existing = next((r for r in rigs if str(r.get("id")) == str(rid)), None)
            if existing:
                existing.update(body)
            else:
                rigs.append({**body, "id": rid})
            # Synchronizuj karty audio z riga do globalnego audio config
            if "audio" not in self.cfg:
                self.cfg["audio"] = {}
            if body.get("audioRx") is not None:
                self.cfg["audio"]["rxDevice"] = body["audioRx"]
            if body.get("audioTx") is not None:
                self.cfg["audio"]["txDevice"] = body["audioTx"]
            save_json(CFG_F, self.cfg)
            return 200, {"ok": True}

        if p == "/api/rig/status" and method == "GET":
            return 200, {"freq": self.rig.freq, "mode": self.rig.mode,
                         "bandwidth": self.rig.bw, "ptt": self.rig.ptt,
                         "split": self.rig.split, "freqB": self.rig.freq_b,
                         "smeter": self.rig.s_meter,
                         "connected": self.rig.connected, "sim": self.rig.sim}

        if p == "/api/rig/connect" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            # Wybierz backend wg modelu: scope-capable → bezpośredni CI-V (CivRig),
            # pozostałe → rigctld (RigCAT). Podmień app.rig w locie jeśli trzeba.
            sel_model = str((body or {}).get("model") or "").strip()
            want_civ  = sel_model in SCOPE_MODELS
            is_civ    = isinstance(self.rig, CivRig)
            if want_civ != is_civ and sel_model:
                try: self.rig.close()
                except Exception as _ce: print(f"[rig] close starego backendu: {_ce}")
                # Daj Windows chwile na faktyczne zwolnienie portu szeregowego
                # (terminate() procesu rigctld nie jest natychmiastowy)
                await asyncio.sleep(0.5)
                self.rig = CivRig(self.cfg, self._rig_bcast, log=print) if want_civ else RigCAT()
                print(f"[rig] backend → {'CI-V bezpośredni' if want_civ else 'rigctld'} (model {sel_model})")
                # WAZNE: ComBridgeWs trzymal referencje do STAREGO CivRig.
                # Po utworzeniu nowego trzeba przepiac listener, inaczej
                # klienci COM Bridge (CW Skimmer/HRD) nie dostaja odpowiedzi
                # radia (fix 2026-07-05).
                if hasattr(self, 'com_bridge_ws') and self.com_bridge_ws:
                    new_civ = self.rig if isinstance(self.rig, CivRig) else None
                    self.com_bridge_ws.attach_civ_rig(new_civ)
                    print(f"[rig] com_bridge_ws przepiety na nowy CivRig={new_civ is not None}")
            # Użyj wartości z formularza (model/port/speed/civ) jeśli przekazane
            ok = await self.rig.connect(self.cfg, override=body or None)

            # Zapisz parametry polaczenia do config.json — zeby po restarcie
            # serwera App.__init__ wybral wlasciwy backend (CivRig/RigCAT)
            # i connect() na starcie polaczyl sie z tym samym radiem/portem.
            if body:
                rig_id = str(body.get("rigId") or body.get("id") or "1")
                if not self.cfg.get("rigs"):
                    self.cfg["rigs"] = []
                rigs = self.cfg["rigs"]
                existing = next((r for r in rigs if str(r.get("id")) == rig_id), None)
                persist_keys = {"model", "port", "speed", "civ", "civAddr", "name"}
                persist = {k: v for k, v in body.items() if k in persist_keys and v}
                if existing:
                    existing.update(persist)
                else:
                    rigs.append({"id": rig_id, **persist})
                save_json(CFG_F, self.cfg)
                print(f"[rig] zapisano config dla rig={rig_id}: {persist}")

            # Po połączeniu rozeslij pełny stan radia, żeby panel od razu pokazał
            # aktualną częstotliwość/mode/S-meter (rig_poll wysyła tylko zmiany).
            try:
                if ok:
                    self.rig.s_meter = await self.rig.get_smeter()
                await self.hub.broadcast({
                    "type": "telemetry",
                    "freq": self.rig.freq, "freqB": self.rig.freq_b,
                    "mode": self.rig.mode, "bandwidth": self.rig.bw,
                    "filterNum": self.rig.filter_num,
                    "preamp": self.rig.preamp, "attenuator": self.rig.attenuator,
                "tuner": self.rig.tuner,
                    "tuner": self.rig.tuner,
                    "sMeter": self.rig.s_meter, "ptt": self.rig.ptt,
                    "split": self.rig.split,
                    "connected": self.rig.connected, "sim": self.rig.sim,
                })
            except Exception as _be:
                print(f"[rig] broadcast po connect: {_be}")
            await self._refresh_caps_cache()
            return 200, {"ok": ok, "sim": self.rig.sim,
                         "message": self.rig.last_msg,
                         "error": self.rig.last_err,
                         "rigctldPath": self.rig.rigctld_path,
                         "rigctldFound": self.rig.rigctld_found,
                         "freq": self.rig.freq, "mode": self.rig.mode,
                         "bandwidth": self.rig.bw}

        # ── Panel funkcji radia (capabilities + admin whitelist) ──────────────
        if p == "/api/rig/features" and method == "GET":
            return 200, await self._get_rig_features(role)

        if p == "/api/rig/features" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            return 200, await self._set_rig_features(body or {})

        # ── CW Keyer (wysylanie makr CW przez CI-V cmd 17) ────────────────────
        # Frontend (cw.js) woła:
        #   POST /api/cw/send   {text, vars}  -> wysyla tekst jako CW
        #   POST /api/cw/stop                 -> przerywa wysylke (17 FF)
        #   GET  /api/cw/status                -> {method, capabilities}
        #   POST /api/cw/method {method}       -> ustawia metode (auto/cat/dtr/rts)
        #   POST /api/cw/dtr-port {port}       -> (placeholder, DTR niezaimplementowane)
        # Aktualnie zaimplementowana jest tylko metoda CAT (CI-V cmd 17) — dziala
        # dla IC-7300/746 bez dodatkowego sprzetu. DTR/RTS keying wymaga
        # bezposredniego sterowania liniami portu serial (poza CI-V) i nie jest
        # jeszcze obslugiwane — metoda 'dtr'/'rts' zwraca blad.
        if p == "/api/cw/macros" and method == "GET":
            u_obj = self.find_user_by_id(uid) or {}
            macros = u_obj.get("cwMacros") or self.cfg.get("cwMacros", DEFAULT_MACROS)
            return 200, {"macros": macros}

        if p == "/api/cw/macros" and method == "POST":
            macros = body.get("macros", [])
            u_obj = self.find_user_by_id(uid)
            if not u_obj: return 404, {"error": "Brak konta"}
            u_obj["cwMacros"] = macros
            save_json(USR_F, self.users)
            return 200, {"ok": True}

        if p == "/api/cw/send" and method == "POST":
            print(f"[cw] /api/cw/send: method={self.cfg.get('cwMethod','auto')!r} text={body.get('text','')!r} sim={self.rig.sim} ser={self.rig._ser is not None}", flush=True)
            if role == "viewer":
                return 403, {"ok": False, "error": "Brak uprawnien (CW)"}
            if role != "admin" and not self._user_has_lock(uid):
                holder = self.radio_lock["callsign"] or self.radio_lock["username"] or ""
                msg_err = f"CW zablokowany — radio ma {holder}" if holder else "CW zablokowany — najpierw przejmij radio"
                return 403, {"ok": False, "error": msg_err}
            # Reject overlap: a CW transmission holds PTT for its whole duration.
            # Firing a second macro before the first finishes made both texts
            # overlap in the rig's CW buffer (garbled / truncated output). One
            # transmission at a time — the operator waits for it to finish.
            if getattr(self, "_cw_tx_busy", False):
                return 200, {"ok": False, "error": "CW zajete — trwa nadawanie, poczekaj"}
            if not self._is_band_allowed():
                return 403, {"ok": False, "error": "TX zablokowany — pasmo niedozwolone przez admina"}
            cross, band_a, band_b = self._is_split_cross_band()
            if cross:
                return 403, {"ok": False, "error": f"CW TX zablokowany — split cross-band ({band_a} RX / {band_b} TX). Wylacz split lub ustaw VFO-B na to samo pasmo."}
            text = str(body.get("text", ""))
            cw_vars = body.get("vars", {}) or {}
            for key, val in cw_vars.items():
                text = text.replace("{" + key.upper() + "}", str(val))
                text = text.replace("{" + key + "}", str(val))
            text = text.strip()
            if not text:
                return 200, {"ok": False, "error": "Pusty tekst"}
            method_cw = self.cfg.get("cwMethod", "auto")
            wpm = int(self.rig.level_values.get("KEYSPD", 18) or 18)
            # Fallback: jesli metoda to dtr/rts ale brak osobnego portu → uzyj auto (CI-V)
            if method_cw in ("dtr", "rts"):
                keyer_port = self.cfg.get("cwDtrPort", "")
                civ_port   = self.rig._port if hasattr(self.rig, "_port") else ""
                if not keyer_port or keyer_port == civ_port:
                    print(f"[cw] metoda {method_cw!r} bez osobnego portu — fallback na CAT CI-V", flush=True)
                    method_cw = "auto"
            if method_cw in ("dtr", "rts"):
                # Sprawdz czy DTR/RTS ma skonfigurowany OSOBNY port
                # Uzycie DTR/RTS na tym samym porcie co CI-V powoduje
                # konflikty — zmiany linii DTR/RTS zakloca komunikacje CI-V
                keyer_port = self.cfg.get("cwDtrPort", "")
                civ_port   = self.rig._port if hasattr(self.rig, "_port") else ""
                if not keyer_port or keyer_port == civ_port:
                    return 200, {"ok": False,
                        "error": "DTR/RTS keying wymaga osobnego portu COM. "
                                 "Skonfiguruj osobny port w Konfiguracja > CW Keyer, "
                                 "lub uzyj metody CAT CI-V (zalecane dla IC-7300/746)."}
            self._cw_tx_busy = True
            try:
                if method_cw in ("dtr", "rts"):
                    try:
                        await self.rig.send_cw_dtr_rts(text, wpm)
                    except Exception as e:
                        return 200, {"ok": False, "error": str(e)}
                else:
                    if self.rig.sim:
                        print(f"[cw] SIM CAT: {text!r}")
                    else:
                        _cw_err = None
                        try:
                            # Broadcastuj PTT ON przed nadawaniem
                            self.rig.ptt = True
                            await self.hub.broadcast({"type": "ptt", "ptt": True})
                            await self.rig.send_cw_message(text)
                        except Exception as e:
                            _cw_err = str(e)
                            await self.hub.broadcast({"type": "cw_error", "error": str(e)})
                        finally:
                            # Broadcastuj PTT OFF po nadawaniu
                            self.rig.ptt = False
                            await self.hub.broadcast({"type": "ptt", "ptt": False})
                        # Zwroc wynik PO finally: blad tylko gdy faktycznie wystapil.
                        if _cw_err is not None:
                            return 200, {"ok": False, "error": _cw_err}
            finally:
                self._cw_tx_busy = False
            await self.hub.broadcast({"type": "cw_sending", "text": text,
                                       "method": method_cw, "wpm": wpm})
            # Use the SAME exact Morse timing as the rig (civ._cw_text_duration_s)
            # for the UI "done" signal, instead of the old flat len*10 guess that
            # disagreed with the actual transmission length.
            try:
                duration_s = self.rig._cw_text_duration_s(text, wpm) + 0.15
            except Exception:
                dit_ms = 1200.0 / max(5, wpm)
                duration_s = len(text) * 10 * dit_ms / 1000.0
            duration_s = min(60.0, max(0.3, duration_s))
            async def _cw_done_after(delay):
                await asyncio.sleep(delay)
                await self.hub.broadcast({"type": "cw_done"})
            asyncio.create_task(_cw_done_after(duration_s))
            return 200, {"ok": True}

        if p == "/api/cw/stop" and method == "POST":
            if role == "viewer":
                return 403, {"ok": False, "error": "Brak uprawnien (CW)"}
            method_cw = self.cfg.get("cwMethod", "auto")
            if not self.rig.sim:
                try:
                    if method_cw in ("dtr", "rts"):
                        await self.rig.stop_cw_dtr_rts()
                    else:
                        await self.rig.stop_cw_message()
                except Exception as e:
                    return 200, {"ok": False, "error": str(e)}
            await self.hub.broadcast({"type": "cw_stopped"})
            return 200, {"ok": True}

        if p == "/api/cw/status" and method == "GET":
            return 200, {
                "method":   self.cfg.get("cwMethod", "auto"),
                "dtrPort":  self.cfg.get("cwDtrPort", ""),
                "dtrLine":  self.cfg.get("cwDtrLine", "DTR"),
                "wpm":      self.rig.level_values.get("KEYSPD", 18),
                "capabilities": {"catMorse": True, "dtr": True, "rts": True},
            }

        if p == "/api/cw/method" and method == "POST":
            if role != "admin":
                return 403, {"error": "Tylko admin"}
            m = body.get("method", "auto")
            if m not in ("auto", "cat", "dtr", "rts"):
                return 400, {"error": "Nieznana metoda"}
            self.cfg["cwMethod"] = m
            save_json(CFG_F, self.cfg)
            if m in ("dtr", "rts"):
                port = self.cfg.get("cwDtrPort", "")
                self.rig.configure_keyer(port, m.upper())
            return 200, {"ok": True, "method": m}

        if p == "/api/cw/dtr-port" and method == "POST":
            if role != "admin":
                return 403, {"error": "Tylko admin"}
            port = body.get("port", "").strip()
            line = body.get("line", "DTR").upper()
            if line not in ("DTR", "RTS"):
                return 400, {"error": "Linia musi byc DTR lub RTS"}
            self.cfg["cwDtrPort"] = port
            self.cfg["cwDtrLine"] = line
            save_json(CFG_F, self.cfg)
            try:
                self.rig.configure_keyer(port, line)
                return 200, {"ok": True,
                             "message": f"Keyer {line} skonfigurowany: {port or 'port CI-V'}"}
            except Exception as e:
                return 200, {"ok": False, "error": str(e)}

        if p == "/api/rotator" and method == "GET":
            return 200, [r.state() for r in self.rotators]

        m = re.match(r"^/api/rotator/(\d+)/position$", p)
        if m and method == "POST":
            rot = self.get_rot(int(m.group(1)))
            if not rot: return 404, {"error": "Rotator nie znaleziony"}
            rot.go_to(float(body.get("az", 0)))
            return 200, {"ok": True}

        m = re.match(r"^/api/rotator/(\d+)/stop$", p)
        if m and method == "POST":
            rot = self.get_rot(int(m.group(1)))
            if not rot: return 404, {"error": "Rotator nie znaleziony"}
            rot.stop()
            return 200, {"ok": True}

        m = re.match(r"^/api/rotator/(\d+)/test$", p)
        if m and method == "POST":
            rot = self.get_rot(int(m.group(1)))
            if not rot:
                return 200, {"testOk": False, "testMsg": "Rotator nie znaleziony"}
            # PRAWDZIWY test polaczenia: sprobuj polaczyc i odczytac pozycje z
            # portu COM. Poprzednia wersja zwracala tylko zapisany stan (i to w
            # zlych polach — stad 'undefined' w UI). Teraz aktywnie odpytujemy
            # rotor, zeby potwierdzic, ze port odpowiada.
            driver = getattr(rot, "model", "") or getattr(rot, "driver_type", "") or "?"
            _was_sim_before = getattr(rot, "sim", False)
            try:
                # Sprobuj polaczyc, jesli jeszcze nie polaczony. UWAGA: connect()
                # rotora NIE rzuca wyjatku przy bledzie — po cichu ustawia sim=True
                # i zwraca False. Musimy wiec sprawdzic WYNIK, inaczej martwy port
                # udawalby udana "symulacje".
                if not getattr(rot, "connected", False) and not _was_sim_before:
                    try:
                        ok = await asyncio.wait_for(
                            asyncio.to_thread(rot.connect), timeout=6.0)
                    except asyncio.TimeoutError:
                        return 200, {"testOk": False, "driverType": driver,
                                     "testMsg": "Timeout przy otwieraniu portu (6s)"}
                    except Exception as _ce:
                        return 200, {"testOk": False, "driverType": driver,
                                     "testMsg": f"Nie moge otworzyc portu: {_ce}"}
                    # connect() zwrocil False i rotor wpadl w sim => port padl
                    if not ok and getattr(rot, "sim", False) and not _was_sim_before:
                        _err = getattr(rot, "last_err", "") or \
                               f"Port {getattr(rot, 'port', '?')} nie odpowiada"
                        return 200, {"testOk": False, "driverType": driver,
                                     "testMsg": f"Połączenie nieudane: {_err}"}
                # Odczytaj pozycje z portu — to potwierdza, ze rotor odpowiada.
                # _read_pos blokuje na porcie szeregowym, wiec w watku z timeoutem
                # (jak watchdog), zeby martwy port nie zawiesil serwera.
                try:
                    az = await asyncio.wait_for(
                        asyncio.to_thread(rot._read_pos, 3.0), timeout=5.0)
                except asyncio.TimeoutError:
                    return 200, {"testOk": False, "driverType": driver,
                                 "testMsg": "Port nie odpowiada (timeout 5s)"}
                except Exception as _re:
                    return 200, {"testOk": False, "driverType": driver,
                                 "testMsg": f"Brak odpowiedzi z portu: {_re}"}
                if az is None:
                    az = getattr(rot, "az", None)
                # Prawdziwa symulacja (skonfigurowana od poczatku), nie awaryjny fallback
                if _was_sim_before and getattr(rot, "sim", False):
                    return 200, {"testOk": True, "driverType": "SYMULACJA",
                                 "testPos": {"az": round(float(az or 0), 1)},
                                 "testMsg": "Tryb symulacji (brak fizycznego portu)"}
                return 200, {"testOk": True, "driverType": driver,
                             "testPos": {"az": round(float(az or 0), 1)},
                             "testMsg": "Port odpowiada"}
            except Exception as e:
                return 200, {"testOk": False, "driverType": driver,
                             "testMsg": str(e)}

        if p == "/api/rotator/config" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            self.cfg["rotators"] = body.get("rotators", [])
            save_json(CFG_F, self.cfg)
            self.init_rotators()
            return 200, {"ok": True, "count": len(self.rotators)}

        if p == "/api/log" and method == "GET":
            page     = int(query.get("page", ["0"])[0])
            size     = int(query.get("size", ["200"])[0])
            user_filter = query.get("user", [None])[0]
            logs = self.log
            if user_filter == "me":
                logs = [e for e in logs if e.get("user_id") == uid]
            elif user_filter and user_filter != "all":
                logs = [e for e in logs if e.get("user_id") == user_filter]
            return 200, {"entries": logs[page*size:(page+1)*size],
                         "total": len(logs), "page": page, "size": size}

        if p == "/api/log" and method == "POST":
            entry = {
                **body,
                "id":        str(int(time.time()*1000)),
                "timestamp": int(time.time()*1000),
                "user_id":   uid,
                "operator":  user.get("username", ""),
            }
            self.log.insert(0, entry)
            if len(self.log) > 5000: self.log = self.log[:5000]
            save_json(LOG_F, self.log)
            await self.hub.broadcast({"type": "qso_new", "entry": entry})
            return 200, {"ok": True, "entry": entry}

        if p == "/api/log/export" and method == "GET":
            """Eksport logu do ADIF — tylko własne QSO (user=me) lub wszystkie (admin)."""
            user_filter = query.get("user", ["me"])[0]
            if user_filter == "me":
                logs = [e for e in self.log if e.get("user_id") == uid]
            elif role == "admin":
                logs = self.log
            else:
                logs = [e for e in self.log if e.get("user_id") == uid]

            def _adif_field(name, val):
                v = str(val or "")
                return f"<{name}:{len(v)}>{v}"

            lines = ["ADIF Export from Ham Radio Control Server", "<EOH>", ""]
            for e in reversed(logs):
                dt = e.get("date","") or ""
                tm = e.get("time","") or ""
                row = " ".join([
                    _adif_field("CALL",     e.get("dxCall","")),
                    _adif_field("BAND",     e.get("band","")),
                    _adif_field("MODE",     e.get("mode","FT8")),
                    _adif_field("QSO_DATE", dt.replace("-","")),
                    _adif_field("TIME_ON",  tm.replace(":","")[:6]),
                    _adif_field("RST_SENT", e.get("rstSent","599")),
                    _adif_field("RST_RCVD", e.get("rstRcvd","599")),
                    _adif_field("GRIDSQUARE", e.get("dxGrid","")),
                    _adif_field("FREQ",     str(round(e.get("freq",0)/1e6,6))),
                    _adif_field("OPERATOR", e.get("operator","")),
                    _adif_field("COMMENT",  e.get("comment","")),
                    "<EOR>",
                ])
                lines.append(row)
            adif_content = "\n".join(lines)
            return 200, {"adif": adif_content, "count": len(logs)}

        m = re.match(r"^/api/log/(.+)$", p)
        if m and method == "DELETE":
            eid = m.group(1)
            self.log = [e for e in self.log if e.get("id") != eid]
            save_json(LOG_F, self.log)
            return 200, {"ok": True}

        if p == "/api/users" and method == "GET":
            if role != "admin": return 403, {"error": "Tylko admin"}
            # Zwracamy pelen zestaw pol edytowalnych z profilu - inaczej admin
            # nie widzi aktualnych wartosci przy otwarciu edytora usera i
            # jego zmiany "resetuja" pola do pustych.
            # Nie zwracamy password (hash) ze wzgledow bezpieczenstwa.
            return 200, [{"id": u["id"], "username": u["username"],
                          "callsign":    u.get("callsign", u["username"]),
                          "name":        u.get("name", ""),
                          "email":       u.get("email", ""),
                          "locator":     u.get("locator", ""),
                          "role":        u["role"],
                          "active":      u.get("active", True),
                          "permissions": u.get("permissions", {}),
                          "ft8_timer":   u.get("ft8_timer", {}),
                          }
                         for u in self.users]

        if p == "/api/users" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            # Walidacja - username i callsign tylko bezpieczne znaki (bez < > "
            # itp.), zeby nie dalo sie wstrzyknac HTML/JS (obrona przed XSS u
            # zrodla, niezaleznie od escapowania w UI). Fix 2026-07-05.
            _un = body.get("username", "").strip()
            if not _un or len(_un) > 32 or not re.match(r'^[A-Za-z0-9_.\-]+$', _un):
                return 400, {"error": "Nazwa uzytkownika: 1-32 znakow, tylko litery, cyfry, . _ -"}
            _cs = body.get("callsign", "").strip().upper()
            if _cs and not re.match(r'^[A-Z0-9/]{1,16}$', _cs):
                return 400, {"error": "Znak wywolawczy: tylko litery, cyfry i / (max 16)"}
            _loc = body.get("locator", "").strip().upper()
            if _loc and not re.match(r'^[A-Z0-9]{1,8}$', _loc):
                return 400, {"error": "Lokator: tylko litery i cyfry (max 8)"}
            # name i email - obetnij dlugosc i usun znaki HTML-niebezpieczne
            _nm = body.get("name", "").strip()[:64]
            _nm = re.sub(r'[<>"\']', '', _nm)
            new_u = {"id": str(int(time.time()*1000)),
                     "username": _un,
                     "password": hash_pw_secure(body.get("password", "changeme")),
                     "role": body.get("role", "viewer"), "active": True,
                     "name":        _nm,
                     "callsign":    _cs,
                     "locator":     _loc,
                     "email":       body.get("email", "").strip()[:128],
                     "permissions": body.get("permissions", {}),
                     }
            self.users.append(new_u)
            save_json(USR_F, self.users)
            return 200, {"ok": True, "id": new_u["id"]}

        m = re.match(r"^/api/users/(.+)$", p)
        if m and method in ("PUT", "PATCH", "DELETE"):
            if role != "admin": return 403, {"error": "Tylko admin"}
            target_uid = m.group(1)
            if method == "DELETE":
                self.users = [u for u in self.users if u["id"] != target_uid]
                save_json(USR_F, self.users)
                return 200, {"ok": True}
            u = self.find_user_by_id(target_uid)
            if not u: return 404, {"error": "Nie znaleziono"}
            # Pelen zestaw pol edytowalnych - synchronizowane z profil userowy
            # (user edytuje przez /api/user/profile, admin przez tutaj -
            #  oba zapisuja na tych samych polach w users.json).
            if "password"    in body and body["password"]:
                u["password"] = hash_pw_secure(body["password"])
            if "role"        in body: u["role"]        = body["role"]
            if "active"      in body: u["active"]      = bool(body["active"])
            if "name"        in body: u["name"]        = (body["name"] or "").strip()
            if "callsign"    in body: u["callsign"]    = (body["callsign"] or "").strip().upper()
            if "locator"     in body: u["locator"]     = (body["locator"] or "").strip().upper()
            if "email"       in body: u["email"]       = (body["email"] or "").strip()
            if "permissions" in body: u["permissions"] = body["permissions"]
            save_json(USR_F, self.users)
            return 200, {"ok": True}

        if p.startswith("/api/scope"):
            # Scope dostępny tylko w trybie bezpośrednim CI-V (radio ze spektroskopem).
            if not isinstance(self.rig, CivRig):
                return 200, {"ok": False, "sim": True, "running": False,
                             "message": "Scope dostępny tylko dla radia ze scope (IC-7300/7610/705...) "
                                        "w trybie bezpośrednim CI-V. Wybierz taki model i połącz."}
            if p.endswith("/stop"):
                return 200, self.rig.scope_stop()
            if p.endswith("/span") and method == "POST":
                # Zmiana spanu waterfallu (IC-7300 wspiera 2.5/5/10/25/50/100/250/500 kHz)
                try:
                    span_hz = int(body.get("span_hz", 25000))
                except (TypeError, ValueError):
                    return 400, {"ok": False, "error": "Nieprawidlowa wartosc span_hz"}
                if hasattr(self.rig, 'set_scope_span'):
                    try:
                        ok = self.rig.set_scope_span(span_hz)
                        if ok:
                            return 200, {"ok": True, "span_hz": span_hz}
                        return 400, {"ok": False, "error": f"Nieobslugiwany span: {span_hz} Hz"}
                    except Exception as e:
                        return 500, {"ok": False, "error": str(e)}
                return 500, {"ok": False, "error": "set_scope_span niedostepne w tym profilu"}
            return 200, self.rig.scope_start()
        if p == "/api/tunnel/cleanup" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            result = await self.tunnel.cleanup()
            return 200, result

        if p == "/api/tunnel/status" and method == "GET":
            return 200, self.tunnel.get_status()

        if p == "/api/tunnel/config" and method == "GET":
            return 200, self.tunnel.get_config()

        if p == "/api/tunnel/config" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            self.tunnel.save_config(body)
            return 200, {"ok": True}

        if p == "/api/tunnel/start" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            asyncio.create_task(self.tunnel.start(
                mode=body.get("mode"),
                token=body.get("token"),
                hostname=body.get("hostname"),
            ))
            return 200, {"ok": True}

        if p == "/api/tunnel/stop" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            asyncio.create_task(self.tunnel.stop())
            return 200, {"ok": True}

        if p == "/api/tunnel/check" and method == "GET":
            # check_available runs several subprocess.run calls (sc query,
            # cloudflared --version) — all blocking. The frontend polls this
            # status on a timer, so run it in a thread to avoid freezing the
            # event loop (looplag stack pointed at _is_service_installed).
            return 200, await asyncio.to_thread(self.tunnel.check_available)

        if p == "/api/tunnel/install-certbot" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            print("[webapp] install-certbot endpoint wywolany", flush=True)
            asyncio.create_task(self.tunnel.install_certbot_task())
            return 200, {"ok": True}

        if p == "/api/tunnel/gen-cert" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            asyncio.create_task(self.tunnel.gen_cert_task())
            return 200, {"ok": True}

        if p == "/api/wsjtx/start" and method == "POST":
            port = int(body.get("port", body.get("udpPort", 2237)))
            ok   = await self.wsjtx.start(port=port)
            if ok:
                await self.hub.broadcast({"type": "wsjtx_status", "running": True,
                                          "text": f"UDP monitor aktywny (port {port})"})
            return 200, {"ok": ok, "status": self.wsjtx.get_status()}

        if p == "/api/ft8/halt" and method == "POST":
            self._ft8_tx_abort = True
            self._stop_cq_calling()  # zatrzymaj cykliczne CQ
            if not self.rig.sim:
                try: await self.rig.set_ptt(False)
                except: pass
            self.rig.ptt = False
            await self.hub.broadcast({"type": "ptt", "ptt": False})
            await self.hub.broadcast({"type": "ft8_tx_halted"})
            return 200, {"ok": True}

        if p == "/api/wsjtx/stop" and method == "POST":
            await self.wsjtx.stop()
            await self.hub.broadcast({"type": "wsjtx_status", "running": False,
                                      "text": "UDP monitor zatrzymany"})
            return 200, {"ok": True}

        if p == "/api/wsjtx/status" and method == "GET":
            return 200, self.wsjtx.get_status()

        if p == "/api/tunnel/status" and method == "GET":
            return 200, self.tunnel.get_status()

        if p == "/api/tunnel/config" and method == "GET":
            return 200, self.tunnel.get_config()

        if p == "/api/tunnel/config" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            self.tunnel.save_config(body)
            return 200, {"ok": True}

        if p == "/api/tunnel/start" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            asyncio.create_task(self.tunnel.start(
                mode=body.get("mode"),
                token=body.get("token"),
                hostname=body.get("hostname"),
            ))
            return 200, {"ok": True}

        if p == "/api/tunnel/stop" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            asyncio.create_task(self.tunnel.stop())
            return 200, {"ok": True}

        if p == "/api/tunnel/check" and method == "GET":
            # check_available runs several subprocess.run calls (sc query,
            # cloudflared --version) — all blocking. The frontend polls this
            # status on a timer, so run it in a thread to avoid freezing the
            # event loop (looplag stack pointed at _is_service_installed).
            return 200, await asyncio.to_thread(self.tunnel.check_available)

        if p == "/api/audio/enumerate":
            rust = getattr(self, 'rust_audio', None)
            if rust and rust._connected:
                try:
                    devices = await rust.list_devices()
                    if isinstance(devices, list):
                        rx = [d["name"] for d in devices if isinstance(d, dict) and d.get("is_input")]
                        tx = [d["name"] for d in devices if isinstance(d, dict) and not d.get("is_input")]
                        return 200, {"devices":{"rx":rx,"tx":tx},"source":"rust"}
                except Exception as e:
                    print(f"[audio] Rust enumerate error: {e}", flush=True)
            try: dev = enumerate_audio_devices()
            except Exception: dev = {"rx":[],"tx":[],"source":"error"}
            return 200, {"devices":{"rx":dev.get("rx",[]),"tx":dev.get("tx",[])},"source":dev.get("source","none")}

        if p == "/api/audio/status" and method == "GET":
            rust = getattr(self, 'rust_audio', None)
            # txVolume TRZYMA Python (config.json), NIE Rust. Rust status nie zna
            # tej wartosci -> UI czytajac status z Rusta pokazywalo stara/domyslna
            # ("backend pokazuje co innego"). Dlatego ZAWSZE wstrzykujemy txVolume
            # (i karty) z configu Pythona do zwracanego statusu.
            _py_audio = self.cfg.get("audio", {})
            _py_txvol = float(_py_audio.get("txVolume", 1.0))
            if rust and rust._connected:
                try:
                    status = await rust.get_status()
                    if isinstance(status, dict) and "error" not in status:
                        # Karty i txVolume ZAWSZE z configu Pythona (nie z Rusta) —
                        # to Python jest zrodlem prawdy dla zapisanych ustawien.
                        # Rust moze zwracac puste/inne, wiec nadpisujemy.
                        _rxd = _py_audio.get("rxDevice", "")
                        _txd = _py_audio.get("txDevice", "")
                        if _rxd: status["rx_device"] = _rxd
                        if _txd: status["tx_device"] = _txd
                        status["txVolume"] = _py_txvol   # nadpisz wartoscia z config.json
                        return 200, status
                except Exception as e:
                    print(f"[audio] Rust status error: {e}", flush=True)
            _st = self.audio.get_status()
            if isinstance(_st, dict):
                _st["txVolume"] = _py_txvol
            return 200, _st

        if p == "/api/audio/detect" and method == "GET":
            # Zwroc status auto-detekcji karty radia (dla admina).
            # Nie triggeruje ponownej detekcji - to jest cache z init.
            if role != "admin": return 403, {"error": "Tylko admin"}
            return 200, {
                "ok": True,
                "auto_enabled": self._audio_auto,
                "detection":    self._audio_detection,
                "current":      self.cfg.get("audio", {}),
            }

        if p == "/api/audio/detect" and method == "POST":
            # Wymus ponowna detekcje (np. po podpięciu radia po starcie serwera)
            if role != "admin": return 403, {"error": "Tylko admin"}
            try:
                detection = auto_detect_radio_audio()
                self._audio_detection = detection
                if detection["detected"]:
                    if "audio" not in self.cfg: self.cfg["audio"] = {}
                    if detection["rx"]: self.cfg["audio"]["rxDevice"] = detection["rx"]
                    if detection["tx"]: self.cfg["audio"]["txDevice"] = detection["tx"]
                    self.audio.cfg = self.cfg["audio"]
                    save_json(CFG_F, self.cfg)
                return 200, {"ok": True, "detection": detection}
            except Exception as e:
                return 500, {"error": str(e)}

        if p == "/api/audio/detect/toggle" and method == "POST":
            # Wlacz/wylacz auto-detekcje (admin moze przelaczyc na recznie)
            if role != "admin": return 403, {"error": "Tylko admin"}
            enabled = bool(body.get("enabled", True))
            self._audio_auto = enabled
            self.cfg["audio_auto_detect"] = enabled
            save_json(CFG_F, self.cfg)
            return 200, {"ok": True, "auto_enabled": enabled}

        # ── CI-V TCP Bridge (dla starego softu przez com0com) ─────────────────
        # ── COM Bridge WS - konfiguracja portow COM per user ────────────────
        if p == "/api/com/services" and method == "GET":
            # Lista dostepnych serwisow (dla admin GUI - dropdown)
            svc_list = []
            for key, meta in self.com_bridge_ws.SERVICES.items():
                svc_list.append({
                    'key':       key,
                    'name':      meta['name'],
                    'available': meta['available'],
                    'defaults':  {
                        'baud':   meta['default_baud'],
                        'parity': meta['default_parity'],
                        'bits':   meta['default_bits'],
                        'stop':   meta['default_stop'],
                    },
                })
            return 200, {'ok': True, 'services': svc_list}

        if p == "/api/com/config" and method == "GET":
            # Zwroc konfiguracje portow biezacego usera
            # JWT payload uzywa 'id' (nie 'user_id') - konsystentnie z reszta kodu
            if not user:
                return 401, {'error': 'not authenticated'}
            uid = user.get('id') or user.get('user_id')  # fallback dla starych tokenow
            u = next((x for x in self.users if x.get('id') == uid), None)
            if not u:
                return 404, {'error': 'user not found'}
            ports = u.get('com_bridge', {}).get('ports', [])
            return 200, {'ok': True, 'ports': ports}

        if p == "/api/com/config" and method == "POST":
            # Zapisz konfiguracje portow biezacego usera
            if not user:
                return 401, {'error': 'not authenticated'}
            uid = user.get('id') or user.get('user_id')
            u = next((x for x in self.users if x.get('id') == uid), None)
            if not u:
                return 404, {'error': 'user not found'}
            ports = body.get('ports', [])
            if not isinstance(ports, list):
                return 400, {'error': 'ports musi byc lista'}
            if len(ports) > 16:
                return 400, {'error': 'max 16 portow'}
            # Walidacja kazdego portu
            clean = []
            for entry in ports:
                if not isinstance(entry, dict):
                    continue
                service = entry.get('service', '')
                if service not in self.com_bridge_ws.SERVICES:
                    continue
                clean.append({
                    'service': service,
                    'baud':    int(entry.get('baud', 19200)),
                    'parity':  str(entry.get('parity', 'N'))[:1],
                    'bits':    int(entry.get('bits', 8)),
                    'stop':    int(entry.get('stop', 1)),
                })
            if 'com_bridge' not in u:
                u['com_bridge'] = {}
            u['com_bridge']['ports'] = clean
            save_json(USR_F, self.users)
            return 200, {'ok': True, 'ports': clean}

        if p == "/api/com/stats" and method == "GET":
            # Statystyki polaczonych klientow (dla admin)
            if role != "admin":
                return 403, {'error': 'tylko admin'}
            return 200, self.com_bridge_ws.get_stats()

        # ── Debug endpoint dla diagnozy subscription (dostepny dla wszystkich)
        if p == "/api/debug/subscriptions" and method == "GET":
            clients_info = []
            for cli_ws, channels in self.hub._subs.items():
                info = self.online_users.get(cli_ws, {})
                clients_info.append({
                    "user_id":  info.get("user_id", "?"),
                    "username": info.get("username", "?"),
                    "callsign": info.get("callsign", ""),
                    "role":     info.get("role", "?"),
                    "channels": sorted(channels),
                })
            my_uid = user.get("id", "") if user else ""
            my_role = user.get("role", "?") if user else "?"
            return 200, {
                "ok": True,
                "my_uid": my_uid,
                "my_role": my_role,
                "ft8_rx_enabled": self._ft8_rx_enabled,
                "ft8_rx_owner_uid": self._ft8_rx_owner_uid,
                "clients": clients_info,
            }

        # ── CloudLog / WaveLog API — ustawienia PER UZYTKOWNIK ──────────────
        # Kazdy uzytkownik ma swoje API keys, URL i stacje — zapisywane
        # w profilu uzytkownika (users.json), nie w globalnym cfg.
        if p == "/api/cloudlog/config" and method == "GET":
            if not user:
                return 401, {"error": "Brak autoryzacji"}
            u = self.find_user_by_id(user["id"])
            return 200, (u or {}).get("cloudlog", {})

        if p == "/api/cloudlog/config" and method == "POST":
            if not user:
                return 401, {"error": "Brak autoryzacji"}
            u = self.find_user_by_id(user["id"])
            if not u:
                return 404, {"error": "Uzytkownik nie istnieje"}
            u["cloudlog"] = {
                "url":          body.get("url", "").strip(),
                "apiKeyQso":    body.get("apiKeyQso", "").strip(),
                "apiKeyRadio":  body.get("apiKeyRadio", "").strip(),
                "stationId":    int(body.get("stationId", 1)),
                "liveEnabled":  bool(body.get("liveEnabled", False)),
            }
            save_json(USR_F, self.users)
            return 200, {"ok": True}

        if p == "/api/cloudlog/test" and method == "POST":
            if not user:
                return 401, {"error": "Brak autoryzacji"}
            import aiohttp as _aiohttp
            url     = body.get("url", "").rstrip("/")
            api_key = body.get("apiKeyQso", "")
            if not url or not api_key:
                return 400, {"ok": False, "error": "Podaj adres i API Key"}
            # Jesli user wpisal adres z /index.php - nie dubluj go
            base = url[:-10].rstrip("/") if url.endswith("/index.php") else url
            try:
                # Cloudlog: klucz API w URL, endpoint station_info.
                # (user_info z naglowkiem X-Auth-Key NIE ISTNIEJE -> 404)
                # Zwraca liste profili stacji uzytkownika.
                async with _aiohttp.ClientSession() as sess:
                    async with sess.get(
                        f"{base}/index.php/api/station_info/{api_key}",
                        timeout=_aiohttp.ClientTimeout(total=8)
                    ) as resp:
                        txt = await resp.text()
                        if resp.status == 200:
                            try:
                                data = await resp.json(content_type=None)
                            except Exception:
                                return 200, {"ok": False,
                                             "error": "Odpowiedz nie jest JSON — "
                                                      "sprawdz adres Cloudloga"}
                            # Oczekujemy listy profili stacji
                            if isinstance(data, list) and data:
                                stations = [
                                    {"id": s.get("station_id"),
                                     "name": s.get("station_profile_name", ""),
                                     "callsign": s.get("station_callsign", ""),
                                     "active": bool(s.get("station_active"))}
                                    for s in data
                                ]
                                names = ", ".join(
                                    f"#{s['id']} {s['callsign'] or s['name']}"
                                    for s in stations[:4])
                                return 200, {"ok": True,
                                             "message": f"Połączono. Profile stacji: {names}",
                                             "stations": stations}
                            if isinstance(data, dict) and data.get("status") == "failed":
                                return 200, {"ok": False,
                                             "error": f"Cloudlog: {data.get('reason','błędny klucz')}"}
                            return 200, {"ok": False,
                                         "error": "Brak profili stacji — "
                                                  "utwórz Station Location w Cloudlogu"}
                        elif resp.status in (401, 403):
                            return 200, {"ok": False, "error": "Błędny API Key"}
                        elif resp.status == 404:
                            return 200, {"ok": False,
                                         "error": "404 — sprawdź adres (bez /index.php na końcu), "
                                                  "np. https://log.example.com"}
                        else:
                            return 200, {"ok": False,
                                         "error": f"HTTP {resp.status}: {txt[:60]}"}
            except Exception as e:
                return 200, {"ok": False, "error": str(e)[:80]}

        if p == "/api/cloudlog/radio" and method == "POST":
            # Wyslij aktualna czestotliwosc i tryb do CloudLog/WaveLog
            import aiohttp as _aiohttp
            url       = body.get("url", "").rstrip("/")
            api_key   = body.get("apiKey", "")
            freq      = int(body.get("freq", 0))
            mode      = body.get("mode", "USB")
            station   = int(body.get("stationId", 1))
            if not url or not api_key:
                return 400, {"ok": False, "error": "Brak konfiguracji"}
            base = url[:-10].rstrip("/") if url.endswith("/index.php") else url
            try:
                # Format wg dokumentacji Cloudlog API/Radio:
                # {key, radio, frequency (Hz), mode, timestamp "YYYY/MM/DD HH:MM"}
                # station_id NIE jest polem tego endpointu (usuniete).
                payload = {
                    "key":        api_key,
                    "radio":      "Ham Radio CTRL",
                    "frequency":  str(freq),
                    "mode":       mode,
                    "timestamp":  __import__("datetime").datetime.utcnow().strftime("%Y/%m/%d %H:%M"),
                }
                async with _aiohttp.ClientSession() as sess:
                    async with sess.post(
                        f"{base}/index.php/api/radio",
                        json=payload,
                        headers={"Content-Type": "application/json"},
                        timeout=_aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        return 200, {"ok": resp.status in (200, 201)}
            except Exception as e:
                return 200, {"ok": False, "error": str(e)[:80]}

        if p == "/api/cloudlog/qso" and method == "POST":
            # Wyslij QSO do CloudLog/WaveLog (ADIF - patrz qso_to_adif)
            import aiohttp as _aiohttp
            url       = body.get("url", "").rstrip("/")
            api_key   = body.get("apiKey", "")
            station   = body.get("stationId", 1)
            qso       = body.get("qso", {})
            if not url or not api_key or not qso:
                return 400, {"ok": False, "error": "Brak danych QSO lub konfiguracji"}
            base = url[:-10].rstrip("/") if url.endswith("/index.php") else url
            try:
                payload = {
                    "key":                api_key,
                    "station_profile_id": str(station),
                    "type":               "adif",
                    "string":             qso_to_adif(qso),
                }
                async with _aiohttp.ClientSession() as sess:
                    async with sess.post(
                        f"{base}/index.php/api/qso",
                        json=payload,
                        headers={"Content-Type": "application/json",
                                 "Accept": "application/json"},
                        timeout=_aiohttp.ClientTimeout(total=8)
                    ) as resp:
                        txt = await resp.text()
                        if resp.status in (200, 201):
                            try:
                                rdata = await resp.json(content_type=None)
                            except Exception:
                                rdata = {}
                            if isinstance(rdata, dict) and rdata.get("status") == "failed":
                                return 200, {"ok": False,
                                             "error": f"Cloudlog: {rdata.get('reason','?')}"}
                            return 200, {"ok": True}
                        else:
                            return 200, {"ok": False, "error": f"HTTP {resp.status}: {txt[:80]}"}
            except Exception as e:
                return 200, {"ok": False, "error": str(e)[:80]}

        # ── FT8 Timer API ────────────────────────────────────────────────────────
        if p == "/api/ft8timer/global" and method == "GET":
            dur = self.cfg.get("ft8_safety_timer", 6)
            return 200, {"duration_min": dur, "active": True}

        if p == "/api/ft8timer/global" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            dur = max(1, min(60, int(body.get("duration_min", 6))))
            self.cfg["ft8_safety_timer"] = dur
            save_json(CFG_F, self.cfg)
            return 200, {"ok": True, "duration_min": dur}

        if p == "/api/ft8timer/config" and method == "GET":
            # Zwroc ustawienia timera dla aktualnego usera
            u = self.find_user_by_id(uid) or {}
            timer = u.get("ft8_timer", {"duration_min": 6, "user_can_edit": False})
            return 200, timer

        if p == "/api/ft8timer/config" and method == "POST":
            # User ustawia swoj timer (jesli admin dal mu prawo)
            u = self.find_user_by_id(uid)
            if not u: return 404, {"error": "Brak usera"}
            timer = u.get("ft8_timer", {"duration_min": 6, "user_can_edit": False})
            if role != "admin" and not timer.get("user_can_edit", False):
                return 403, {"error": "Brak uprawnień do zmiany timera"}
            mins = int(body.get("duration_min", 6))
            mins = max(1, min(60, mins))  # 1-60 min
            u.setdefault("ft8_timer", {})["duration_min"] = mins
            save_json(USR_F, self.users)
            return 200, {"ok": True, "duration_min": mins}

        if p == "/api/ft8timer/admin" and method == "POST":
            # Admin ustawia timer dla wybranego usera
            if role != "admin": return 403, {"error": "Tylko admin"}
            target_id     = body.get("user_id", "")
            duration_min  = int(body.get("duration_min", 6))
            user_can_edit = bool(body.get("user_can_edit", False))
            duration_min  = max(1, min(60, duration_min))
            target = self.find_user_by_id(target_id)
            if not target: return 404, {"error": "Brak usera"}
            target["ft8_timer"] = {
                "duration_min":  duration_min,
                "user_can_edit": user_can_edit,
            }
            save_json(USR_F, self.users)
            return 200, {"ok": True}

        # ── QSO Log API ──────────────────────────────────────────────────────────
        is_admin_user = (role == "admin")

        if p == "/api/qsolog/worked_before" and method == "GET":
            # Sprawdz czy call juz byl worked. Optional: band/mode.
            call = query.get("call", "")
            band = query.get("band", None)
            mode = query.get("mode", None)
            if isinstance(call, list): call = call[0] if call else ""
            if isinstance(band, list): band = band[0] if band else None
            if isinstance(mode, list): mode = mode[0] if mode else None
            result = qso_db.worked_before(call, band, mode)
            return 200, {"ok": True, **result}

        if p == "/api/qsolog/all" and method == "DELETE":
            target_uid = query.get("user_id") or uid
            if isinstance(target_uid, list): target_uid = target_uid[0]
            # Admin moze kasowac log dowolnego usera, zwykly user tylko swoj
            if target_uid != uid and not is_admin_user:
                return 403, {"error": "Brak uprawnień"}
            # Kasowanie tysiecy QSO moze potrwac - nie blokuj event loopa
            count = await asyncio.to_thread(qso_db.delete_all, target_uid)
            return 200, {"ok": True, "count": count}


        if p == "/api/debug/qso_users" and method == "GET":
            if not is_admin_user: return 403, {}
            conn = qso_db._get_conn()
            rows = conn.execute("SELECT user_id, COUNT(*) as cnt FROM qso GROUP BY user_id").fetchall()
            return 200, {"users": [{"user_id": r[0], "count": r[1]} for r in rows]}

        if p == "/api/qsolog" and method == "GET":
            try:
                # MultiDict — pobierz pierwsze wartosci
                filters = {k: (v[0] if isinstance(v, list) else v)
                           for k, v in query.items()}
                filter_uid = filters.pop("user_id", None) or None
                # Lista moze zwracac tysiace QSO - do watku, zeby nie blokowac
                result = await asyncio.to_thread(
                    qso_db.list_qsos, uid, is_admin=is_admin_user,
                    filter_uid=filter_uid, **filters)
                return 200, result
            except Exception as e:
                import traceback; traceback.print_exc()
                return 500, {"error": str(e)}

        if p == "/api/qsolog/bulk" and method == "POST":
            # Bulk import wielu QSO naraz
            qsos_list = body.get("qsos", [])
            if not qsos_list:
                return 400, {"ok": False, "error": "Brak QSO"}
            try:
                # STABILNOSC: import ADIF z tysiacami QSO trwa sekundy. Wolany
                # synchronicznie ZAMROZILBY event loop - wszyscy userzy by
                # zamarli, WebSockety by timeoutowaly. Wykonujemy w watku.
                result = await asyncio.to_thread(
                    qso_db.add_qsos_bulk, uid, qsos_list)
                return 200, {"ok": True, "inserted": result["inserted"],
                             "skipped": result["skipped"],
                             "duplicates": result.get("duplicates", 0)}
            except Exception as e:
                import traceback; traceback.print_exc()
                return 500, {"ok": False, "error": str(e)}

        if p == "/api/qsolog" and method == "POST":
            # Dodaj nowe QSO
            try:
                body["source"] = body.get("source", "manual")
                qso = qso_db.add_qso(uid, body)
                # Wyslij do CloudLog jesli skonfigurowany
                asyncio.ensure_future(self._cloudlog_push_qso(uid, qso))
                return 200, {"ok": True, "id": qso["id"]}
            except ValueError as e:
                return 400, {"ok": False, "error": str(e)}
            except Exception as e:
                import traceback; traceback.print_exc()
                return 500, {"ok": False, "error": str(e)}

        if p.startswith("/api/deepcw/capture") and method == "POST":
            # Diagnostyka: nagraj kilkanascie sekund audio dokladnie w takiej
            # postaci, w jakiej trafia do modelu (po resamplingu i filtrze).
            if deepcw_engine is None:
                return 503, {"error": "Silnik DeepCW niedostepny"}
            _sec = float(body.get("seconds", 15))
            _path = deepcw_engine.start_capture(_sec)
            return 200, {"ok": True, "path": _path, "seconds": _sec}

        if p.startswith("/api/deepcw/capture_file") and method == "GET":
            # Pobranie nagranego pliku do odsluchu
            try:
                import pathlib as _pl
                _f = _pl.Path(deepcw_engine._cap_path)
                if not _f.exists():
                    return 404, {"error": "Brak nagrania — najpierw uruchom zapis"}
                return "export", {
                    "body": _f.read_bytes(),
                    "content_type": "audio/wav",
                    "filename": "deepcw_capture.wav",
                }
            except Exception as e:
                return 500, {"error": str(e)}

        if p.startswith("/api/deepcw/download") and method == "POST":
            # Pobranie modelu ONNX (~15 MB) z repozytorium. Tylko admin —
            # to operacja sieciowa zapisujaca do katalogu danych.
            if role != "admin": return 403, {"error": "Tylko admin"}
            if deepcw_engine is None:
                return 503, {"error": "Silnik DeepCW niedostepny "
                                      "(brak pakietu onnxruntime)"}
            res = await deepcw_engine.download(broadcast_fn=self.hub.broadcast)
            return (200 if res.get("ok") else 500), res

        if p.startswith("/api/deepcw/engine_status") and method == "GET":
            if deepcw_engine is None:
                return 200, {"hasModel": False, "ready": False,
                             "error": "silnik niedostepny"}
            _st = deepcw_engine.get_status()
            # Whether the decoder is CURRENTLY running on the server (someone has
            # a window open). Lets a freshly-opened/reloaded window sync to the
            # real state instead of always assuming "off" — fixes the mismatch
            # where the server kept decoding but the reopened UI showed nothing.
            _st["running"] = getattr(self, "_cw_task", None) is not None
            _st["viewers"] = len(getattr(self, "_cw_viewers", set()))
            return 200, _st

        if p.startswith("/api/deepcw/known_calls") and method == "GET":
            # Pula znanych znakow do KOLOROWANIA po stronie przegladarki.
            # Zasilana z dekodow FT8 + logu QSO; frontend dolacza wlasne spoty
            # z DX clustera (kazdy user ma swoj) i znaki juz przepracowane.
            if deepcw_engine is None:
                return 200, {"calls": []}
            try:
                # Dorzuc znaki z logu operatora (jednorazowo, tanio)
                deepcw_engine.add_known_calls(
                    qso_db.worked_calls(uid, is_admin=is_admin_user))
            except Exception:
                pass
            return 200, {"calls": sorted(deepcw_engine._known_calls)}

        if p.startswith("/api/qsolog/calls") and method == "GET":
            # Lekka lista UNIKALNYCH znakow z logu — do oznaczania zrobionych
            # stacji w Band Activity. Zastepuje pobieranie pelnej listy QSO
            # (capowanej do 200 wpisow), przez co starsze lacznosci nie byly
            # oznaczane jako zrobione.
            try:
                # worked_calls robi SELECT DISTINCT po calej tabeli QSO —
                # to blokowalo petle zdarzen na 100-150ms (wykryte detektorem
                # looplag: stos wskazal qso_db.worked_calls). Wynosimy zapytanie
                # do watku, zeby SQLite nie zamrazal asyncio (audio, pingi).
                calls = await asyncio.to_thread(
                    qso_db.worked_calls, uid, is_admin_user)
                return 200, {"calls": calls}
            except Exception as e:
                return 500, {"error": str(e)}

        if p.startswith("/api/qsolog/export") and method == "GET":
            # Export ADIF/CSV/EDI
            # UWAGA: query przychodzi jako {klucz: [wartosc]} (listy). Musimy
            # wyciagnac pojedyncze wartosci, inaczej fmt=['adi'] != 'adi' i
            # endpoint zwracal 400 "Nieznany format". Filtry tez musza byc
            # stringami, nie listami (inaczej export_adif dostaje zle typy).
            def _first(v):
                return v[0] if isinstance(v, list) else v
            _raw = {k: _first(v) for k, v in query.items()}
            fmt = _raw.pop("format", "adi")
            # Przekazuj do eksportu TYLKO znane filtry - inaczej nieoczekiwany
            # parametr z query (token, cache-buster, user_id vs filter_uid itp.)
            # trafia przez **filters do list_qsos i moze rzucic TypeError -> 500.
            _allowed = {"from", "to", "call", "band", "mode", "filter_uid", "ids"}
            f = {k: v for k, v in _raw.items() if k in _allowed and v}
            # Frontend wysyla 'user_id' - mapuj na 'filter_uid' ktory zna backend
            if _raw.get("user_id"):
                f["filter_uid"] = _raw["user_id"]
            try:
                if fmt == "adi":
                    content_out = qso_db.export_adif(uid, is_admin=is_admin_user, **f)
                    ct = "application/octet-stream"
                elif fmt == "csv":
                    content_out = qso_db.export_csv(uid, is_admin=is_admin_user, **f)
                    ct = "text/csv"
                else:
                    return 400, {"error": "Nieznany format"}
                # WAZNE: NIE zwracamy obiektu aiohttp Response tutaj - http_handler
                # robi `status, result = await self.api(...)` i rozpakowuje zwrotke
                # jako krotke (status, result). Obiekt Response nie jest krotka
                # -> ValueError -> 500 (dlatego WSZYSTKIE eksporty dawaly 500, a
                # import dzialal bo zwracal normalna krotke). Zwracamy specjalny
                # typ "export" ktory http_handler obsluzy z naglowkami pobierania.
                return "export", {
                    "body": content_out.encode("utf-8"),
                    "content_type": ct,
                    "filename": f"qso_export.{fmt}",
                }
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                print(f"[export] BLAD 500 fmt={fmt} filtry={f}:\n{tb}", flush=True)
                return 500, {"error": str(e)}

        # /api/qsolog/<id>
        qso_id_match = p.startswith("/api/qsolog/") and len(p) > len("/api/qsolog/")
        if qso_id_match:
            qso_id = p[len("/api/qsolog/"):]

            if method == "GET":
                qso = qso_db.get_qso(uid, qso_id, is_admin=is_admin_user)
                if qso: return 200, qso
                return 404, {"error": "QSO nie znalezione"}

            if method == "PUT":
                try:
                    ok = qso_db.update_qso(uid, qso_id, body, is_admin=is_admin_user)
                    return (200, {"ok": True}) if ok else (404, {"error": "QSO nie znalezione"})
                except ValueError as e:
                    return 400, {"ok": False, "error": str(e)}

            if method == "DELETE":
                ok = qso_db.delete_qso(uid, qso_id, is_admin=is_admin_user)
                return (200, {"ok": True}) if ok else (404, {"error": "QSO nie znalezione"})

        if p == "/api/audio/config" and method == "POST":
            rx = body.get("rxDevice")
            tx = body.get("txDevice")
            br = body.get("bitrate", 24000)
            tv = body.get("txVolume")
            if "audio" not in self.cfg: self.cfg["audio"] = {}
            if rx is not None: self.cfg["audio"]["rxDevice"] = rx
            if tx is not None: self.cfg["audio"]["txDevice"] = tx
            if br: self.cfg["audio"]["bitrate"] = int(br)
            if tv is not None:
                self.cfg["audio"]["txVolume"] = float(tv)
                self.audio.cfg = self.cfg["audio"]
            save_json(CFG_F, self.cfg)
            # Gdy Rust aktywny — wyslij nowe urzadzenia do ham_audio
            rust = getattr(self, 'rust_audio', None)
            if rust and rust._connected:
                # ODPORNOSC NA ROZNE KARTY (produkt dla wielu klubow): sprawdz
                # czy wybrana karta ISTNIEJE w systemie. Gdy user wybral karte
                # ktora zniknela (odpieta USB) albo config z innej maszyny
                # wskazuje nieistniejaca karte - ostrzegamy, ale nie blokujemy
                # (Rust zrobi fallback do default_input). Bez tego FT8 cichnie
                # bez wyjasnienia gdy karta z configu nie istnieje.
                try:
                    _devs = await rust.list_devices()
                    _rx_names = [d.get("name","") for d in _devs
                                 if isinstance(d, dict) and d.get("is_input")]
                    _tx_names = [d.get("name","") for d in _devs
                                 if isinstance(d, dict) and not d.get("is_input")]
                    if rx and not any(rx in n or n in rx for n in _rx_names):
                        print(f"[audio] UWAGA: karta RX '{rx}' nie istnieje w systemie "
                              f"(dostepne: {_rx_names}). Rust uzyje domyslnej.", flush=True)
                        await self.hub.broadcast({"type": "audio_warning",
                            "msg": f"Karta RX '{rx}' niedostepna - uzyto domyslnej"})
                    if tx and not any(tx in n or n in tx for n in _tx_names):
                        print(f"[audio] UWAGA: karta TX '{tx}' nie istnieje w systemie "
                              f"(dostepne: {_tx_names}). Rust uzyje domyslnej.", flush=True)
                except Exception as _e:
                    print(f"[audio] Nie moge zweryfikowac kart: {_e}", flush=True)
                # HOT-SWAP karty BEZ restartu procesu ham_audio (wymaga Rusta z
                # obsluga hot-swap: audio.rs RX_DEVICE_GEN + main.rs SetRxDevice).
                # Rust przeladuje sam stream RX w locie (drop starego = czyste
                # WASAPI, bez fantomow). NIE restartujemy procesu, wiec:
                # - brak fantomowych kart WASAPI (glowny problem)
                # - FT8 RX NIE ginie (proces zyje, dekoder dziala dalej)
                # - dekoder Opus w przegladarce NIE gubi sie (strumien ciagly)
                # - brak martwego polaczenia kontrolnego (proces ten sam)
                print(f"[audio] Hot-swap kart RX='{rx}' TX='{tx}' (bez restartu)", flush=True)
                try:
                    if rx is not None:
                        await rust.set_rx_device(rx)
                    if tx is not None:
                        await rust.set_tx_device(tx)
                    if br:
                        await rust._send_ctrl({"cmd": "SetBitrate", "bps": int(br)})
                    print("[audio] Hot-swap OK - Rust przeladowal stream w locie", flush=True)
                except Exception as _e:
                    print(f"[audio] Hot-swap blad: {_e}", flush=True)
                    await self.hub.broadcast({"type": "audio_warning",
                        "msg": "Zmiana karty nie powiodla sie"})
            return 200, {"ok": True, "cfg": self.cfg["audio"]}

        if p == "/api/audio/rx/start" and method == "POST":
            # Bezpieczenstwo: upewnij sie ze PTT jest wylaczone przed uruchomieniem audio
            # IC-7300 moze wejsc w TX przez DATA VOX gdy PC otworzy kanal USB audio
            if self.rig.ptt:
                print("[audio] BEZPIECZENSTWO: reset PTT przed uruchomieniem RX audio")
                self.rig.ptt = False
                try: await self.rig.set_ptt(False)
                except: pass
                await self.hub.broadcast({"type": "ptt", "ptt": False})
            dev     = body.get("device") or self.cfg.get("audio",{}).get("rxDevice")
            bitrate = body.get("bitrate", 24000)
            ok = self.audio.start_rx(device=dev, bitrate=bitrate)
            if ok: await self.hub.broadcast({"type":"audio_status","rx":True,"device":dev})
            return 200, {"ok":ok,"status":self.audio.get_status()}

        if p == "/api/audio/rx/stop" and method == "POST":
            self.audio.stop_rx()
            return 200, {"ok":True}

        if p == "/api/audio/tx/start" and method == "POST":
            dev = body.get("device") or self.cfg.get("audio",{}).get("txDevice")
            ok  = self.audio.start_tx(device=dev)
            return 200, {"ok":ok,"status":self.audio.get_status()}

        if p == "/api/audio/tx/stop" and method == "POST":
            self.audio.stop_tx()
            return 200, {"ok":True}


        return 404, {"error": f"Nieznany endpoint: {method} {p}"}

    # ── WebSocket handler (aiohttp) ────────────────────────────────────────────
    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        # WebSocket z permessage-deflate compression - kompresuje JSON na
        # poziomie ramek WS. Bardzo dobre dla JSON (3-8x mniejsze), koszt: ~5% CPU.
        # heartbeat=30 - ping/pong co 30s, wykrywa martwe polaczenia.
        # compress=9 - zlib max level (najlepszy stosunek).
        # max_msg_size=8MB - default 4MB moze być za malo dla waterfall history.
        # UWAGA: Opus audio (binary bytes) tez idzie tym samym WS. Kompresja
        # na juz-skompresowanym Opusie da minimalny zysk (2-5%) ale mala jest
        # tez cena. Klient sam decyduje czy dekodowac (permessage-deflate spec).
        # compress=False: WebSocket per-message compression runs synchronously
        # on the event loop for EVERY frame (looplag stack: _send_compressed_
        # frame_sync/compress_sync). Scope + audio + deepcw_text push many
        # frames per second, so even level 1 caused audible audio hitches when
        # the buffer drained. Opus and scope arrays are already compact, so the
        # bandwidth saving was marginal — not worth blocking the loop. Off.
        _ws_kwargs = dict(heartbeat=30, compress=False, max_msg_size=8*1024*1024)
        try:
            ws = web.WebSocketResponse(writer_limit=128*1024, **_ws_kwargs)
        except TypeError:
            ws = web.WebSocketResponse(**_ws_kwargs)
        await ws.prepare(request)

        # Auth via token query param
        token = request.rel_url.query.get("token", "")
        user = self._check_pw_ver(jwt_verify(token)) if token else None
        role = (user or {}).get("role", "viewer")
        uid  = (user or {}).get("id", "")

        await self.hub.add(ws)

        # Zarejestruj uzytkownika jako online
        if user:
            u_obj = self.find_user_by_id(uid) or {}
            self.online_users[ws] = {
                "user_id":  uid,
                "username": user.get("username", ""),
                "callsign": u_obj.get("callsign", user.get("username", "")),
                "locator":  u_obj.get("locator", ""),
                "role":     role,
                "joined_at": time.time(),
            }
            # Powiadom wszystkich o nowym uzytkowniku online
            _lock_state_join = self._radio_lock_state()
            _lock_state_join.pop("type", None)  # patrz komentarz przy init (ws_handler)
            await self.hub.broadcast({
                "type":   "online_update",
                "online": self._online_users_state(),
                **_lock_state_join,
            })

            # Auto-connect DX Cluster jesli user ma to wlaczone
            if self.dxcluster:
                dx_cfg = u_obj.get("dxcluster", {})
                if dx_cfg.get("auto_connect") and dx_cfg.get("host") and dx_cfg.get("login"):
                    # Sprawdz czy juz nie jest polaczony
                    existing = self.dxcluster.get_client(uid)
                    if not existing or not existing.is_connected():
                        asyncio.ensure_future(self.dxcluster.connect_user(
                            uid, dx_cfg["host"], int(dx_cfg.get("port", 7300)),
                            dx_cfg["login"], dx_cfg.get("password", "")
                        ))

        try:
            _lock_state = self._radio_lock_state()
            _lock_state.pop("type", None)  # patrz komentarz nizej — krytyczny fix
            await ws.send_str(json.dumps({
                "type": "init",
                "freq": self.rig.freq, "mode": self.rig.mode,
                "bandwidth": self.rig.bw, "ptt": self.rig.ptt,
                "filterNum": self.rig.filter_num,
                "preamp": self.rig.preamp, "attenuator": self.rig.attenuator,
                "split": self.rig.split, "freqB": self.rig.freq_b,
                "vfo": getattr(self.rig, "vfo", "VFOA"),
                "callsign": CALLSIGN, "locator": LOCATOR,
                # Lokator STACJI (STATION_LOCATOR z konfiguracji admina) —
                # tu fizycznie stoi antena. NIE lokator zalogowanego operatora:
                # przy pracy zdalnej kazdy user jest gdzie indziej, a rotor
                # obraca sie w jednym miejscu. Azymut na korespondenta musi byc
                # liczony od anteny, inaczej user w innej lokalizacji dostalby kierunek
                # liczony z lokalizacji usera dla anteny stojacej gdzie indziej.
                "stationLocator": (self.cfg.get("stationLocator")
                                   or LOCATOR or "").strip().upper(),
                # Lokator OPERATORA (z jego konta) — do raportow FT8/logu QSO
                "operatorLocator": (self.online_users.get(ws, {}).get("locator") or "").strip().upper(),
                "rotators": [r.state() for r in self.rotators],
                "sim": self.rig.sim,
                "connected": self.rig.connected,
                "rigPowerOn": self._rig_power_on,
                "models": HAMLIB_MODELS,
                "rigs":   self.cfg.get("rigs", []),
                "modes":  ["USB","LSB","AM","FM","CW","CW-R","RTTY","RTTY-R","USB-D","LSB-D","PKTUSB","PKTLSB"],
                "enabledBands": self.cfg.get("enabledBands", []),
                "enabledModes": self.cfg.get("enabledModes", []),
                "modeFilters":  self.cfg.get("modeFilters", {}),
                # KRYTYCZNE: _radio_lock_state() zwraca WLASNY klucz
                # "type": "radio_lock_state". Rozpakowanie (**) tego slownika
                # PO "type": "init" w tym samym dict literale powodowalo ze
                # PYTHON CICHO NADPISYWAL "type" na "radio_lock_state" —
                # finalna wiadomosc wysylana do frontu NIGDY nie miala
                # type=="init"! Stad case 'init' w ws.js nigdy sie nie
                # wykonywal, msg.modes nigdy nie docieralo, panel TRYB byl
                # pusty po kazdym swiezym polaczeniu/odswiezeniu. Usuwamy
                # kolidujacy klucz "type" z lock_state PRZED rozpakowaniem
                # (patrz _lock_state.pop("type") powyzej).
                **_lock_state,
                "online": self._online_users_state(),
                "my_uid": uid,
            }))
            # Stan automatyki FT8/FT4 (silnik QSO) — bez tego swiezo polaczony
            # klient (nowa karta, odswiezenie strony, powrot po reconnect) nie
            # znal aktualnego stanu backendu i pokazywal domyslne "IDLE" mimo
            # ze automatyka realnie prowadzila juz QSO z kims innym — panel
            # pokazywal nierealne informacje o tym co i z kim nadajemy, dopoki
            # nie nadeszlo kolejne dekodowanie i nie wywolalo broadcastu.
            await ws.send_str(json.dumps({
                "type": "auto_seq_status",
                "enabled": self._auto_seq_enabled,
                "call1st": self._auto_call_1st,
                "state": self._qso_engine.state,
                "partner": self._qso_engine.partner_call,
                "queue": list(self._qso_engine.queue),
            }))
            await ws.send_str(json.dumps({
                "type": "ft8_fake_split_status",
                "enabled": self._fake_split_enabled,
                "targetHz": 1500,
            }))
            _audio_sub = False
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    data = msg.data
                    if data and len(data) > 1 and data[0] == 0xA2:
                        if not self.audio.tx_active:
                            dev = self.cfg.get("audio", {}).get("txDevice")
                            self.audio.start_tx(device=dev)
                            print(f"[audio] TX auto-start dev={dev}")
                        await self.audio.feed_tx(data[1:])
                        self.touch_activity(uid)
                    elif data and len(data) > 5 and data[0] == 0xC1:
                        # DeepCW: [0xC1][src_rate 4B LE][PCM float32]
                        # Dekoder chodzi tylko gdy przegladarka SLE audio
                        # (panel CW otwarty) — zamkniecie panelu = zero CPU.
                        try:
                            import struct as _st
                            _sr = _st.unpack_from("<I", data, 1)[0]
                            _pcm = np.frombuffer(data[5:], dtype=np.float32)
                            if deepcw_engine is not None:
                                await deepcw_engine.feed(
                                    _pcm, _sr, broadcast_fn=self.hub.broadcast)
                        except Exception as _e:
                            print(f"[deepcw] feed blad: {_e}", flush=True)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        t = data.get("type","")
                        print(f"[ws] TEXT t={t!r}", flush=True)
                        if t == "audio_start":
                            # Audio przez Rust WSS bezposrednio — Python nie proxy'uje
                            await ws.send_str(json.dumps({"type":"audio_ready","status":{"running":True,"rust":True}}))
                        elif t == "audio_stop":
                            self.audio.remove_subscriber(ws)
                            _audio_sub = False
                        else:
                            # Dotknij aktywnosc przy kazdej akcji na radiu
                            if t in ("freq","freqB","mode","ptt","rig_slider","rig_action","vfo","vfo_op",
                                     "split","preamp","attenuator","tuner","tuner_autotune"):
                                self.touch_activity(uid)
                            await self._ws_msg(data, ws, role)
                    except Exception as e:
                        print(f"[ws] {e}")
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break
        except Exception as e:
            print(f"[ws] connection error: {e}", flush=True)
        finally:
            await self.hub.remove(ws)
            if _audio_sub:
                self.audio.remove_subscriber(ws)
            # Usun z listy online i powiadom pozostalych
            self.online_users.pop(ws, None)
            self.radio_requests.pop(uid, None)
            # Sprawdz czy uzytkownik ma inne aktywne WS (np. otwarty w innej
            # zakladce/laptop+telefon). Jesli TAK - nie zatrzymuj WSJT-X ani
            # nie zwalniaj radia (bo dalej jest online).
            still_online = any(u.get("user_id") == uid for u in self.online_users.values())
            # Jesli user trzymal radio i to jego OSTATNIE polaczenie — zwolnij
            if uid and self.radio_lock["user_id"] == uid and not still_online:
                self._release_radio()  # zatrzymuje tez WSJT-X jesli owner
            # Jesli user byl ownerem WSJT-X i to jego OSTATNIE polaczenie — stop
            elif uid and uid == self._ft8_rx_owner_uid and not still_online:
                await self._stop_wsjtx_auto("rozłączenie sesji")
            # CW decoder viewer cleanup: a disconnect (logout, closed tab) must
            # drop this client from the viewer set, exactly like closing the
            # window. If it was the last viewer, stop the decoder so the server
            # doesn't keep decoding (and printing to CMD) with nobody watching —
            # this is the "server keeps decoding after logout" bug.
            if hasattr(self, "_cw_viewers") and ws in self._cw_viewers:
                self._cw_viewers.discard(ws)
                if not self._cw_viewers:
                    _cwt = getattr(self, "_cw_task", None)
                    if _cwt:
                        _cwt.cancel()
                        self._cw_task = None
                    if self.audio:
                        self.audio.cw_rx_enabled = False
                    if deepcw_engine is not None:
                        deepcw_engine.reset()
                    print("[deepcw] dekoder wylaczony (ostatni widz rozlaczony)",
                          flush=True)

            _lock_state_disconnect = self._radio_lock_state()
            _lock_state_disconnect.pop("type", None)  # patrz komentarz przy init (ws_handler)
            await self.hub.broadcast({
                "type":   "online_update",
                "online": self._online_users_state(),
                **_lock_state_disconnect,
            })
        return ws


    async def com_bridge_ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        """
        WebSocket endpoint /ws/com-bridge - dla klientow EXE Windows.

        Autoryzacja: Bearer token w query string (?token=...) LUB w naglowku
        Sec-WebSocket-Protocol jako 'bearer,<token>' (WS niepozwala Authorization
        header standardowo).

        Po autoryzacji przekazuje kontrole do ComBridgeWs.handle_client() ktore
        obsluguje protokol (hello/data/ping/etc).
        """
        # Autoryzacja - token z query string
        token = request.query.get('token', '')
        if not token:
            # Alternatywa: Sec-WebSocket-Protocol 'bearer,<token>'
            protos = request.headers.get('Sec-WebSocket-Protocol', '').split(',')
            protos = [p.strip() for p in protos]
            if len(protos) >= 2 and protos[0] == 'bearer':
                token = protos[1]

        user = self._verify_token(token) if token else None
        if not user:
            return web.Response(status=403, text="Unauthorized - brak lub zly token")

        # ── VIEWER NIE MA DOSTEPU DO MOSTOW (2026-07-05 operator) ─────────────
        # Viewer korzysta WYLACZNIE z web UI (gdzie moze zmieniac freq z lockiem,
        # ale nie nadawac). Zewnetrzne aplikacje CAT (CW Skimmer, HRD, Logger32)
        # przez COM Bridge sa zarezerwowane dla operatorow i admina. Viewer
        # probujacy sie polaczyc dostaje odmowe - nie tworzymy dla niego portow.
        _role = user.get('role', 'viewer')
        if _role == 'viewer':
            print(f"[com_bridge_ws] ODMOWA dla viewera "
                  f"{user.get('username','?')} (id={user.get('id','?')}) - "
                  f"mosty tylko dla operatorow/admina", flush=True)
            return web.Response(
                status=403,
                text="Dostep przez COM Bridge tylko dla operatorow. "
                     "Jako obserwator (viewer) korzystaj z panelu web."
            )

        # Pobierz config portow tego usera z users.json
        # JWT payload uzywa 'id' (kompatybilnosc z reszta kodu)
        user_id = user.get('id') or user.get('user_id', '')
        user_full = next((u for u in self.users if u.get('id') == user_id), {})
        # STALE 2 PORTY CI-V DLA WSZYSTKICH USEROW (2026-07-05).
        # Kazdy user dostaje identycznie 2 porty CI-V (dla CW Skimmer + HRD
        # albo dwoch dowolnych aplikacji CAT). Nie ma per-user konfiguracji -
        # sekcja w web UI jest tylko informacyjna. Adresy CI-V (E1, E2)
        # przydzielane automatycznie w build_assignments.
        com_config = [
            {'service': 'civ', 'baud': 19200},
            {'service': 'civ', 'baud': 19200},
        ]

        # compress=False bo permessage-deflate psuje male wiadomosci CI-V
        # (10 bajtowych ramek). Ruch jest maly (~kilkaset B/s), kompresja
        # niepotrzebna a moze psuc komunikacje.
        ws = web.WebSocketResponse(heartbeat=30, compress=False)
        await ws.prepare(request)

        # Deleguj do ComBridgeWs - on trzyma stan klientow, protokol,
        # rate limiting, walidacje.
        try:
            await self.com_bridge_ws.handle_client(ws, user, com_config)
        except Exception as e:
            print(f"[com_bridge_ws] blad handle_client: {e}")
        finally:
            if not ws.closed:
                await ws.close()
        return ws

    def _verify_token(self, token: str) -> dict:
        """
        Weryfikuje JWT token i zwraca dane usera albo None.
        Uzywane w com_bridge_ws_handler (bo standardowy WS nie ma Bearer header).
        Zwraca dict kompatybilny z reszta kodu: {id, callsign, role}.
        """
        if not token:
            return None
        try:
            payload = self._check_pw_ver(jwt_verify(token))
            if not payload:
                return None
            # JWT payload uzywa 'id' (patrz webapp:1300 - jwt_sign({"id": u["id"], ...}))
            uid = payload.get('id') or payload.get('user_id') or payload.get('sub')
            u = next((x for x in self.users if x.get('id') == uid), None)
            if not u:
                return None
            return {
                'id':       u.get('id'),
                'user_id':  u.get('id'),  # alias dla kompatybilnosci
                'callsign': u.get('callsign', ''),
                'role':     u.get('role', 'user'),
            }
        except Exception as e:
            print(f"[com_bridge_ws] weryfikacja tokenu blad: {e}")
            return None

    async def hamlib_ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        """
        WebSocket endpoint /hamlib — tunel Hamlib TCP dla zdalnego WSJT-X.
        Klient (wsjtx_bridge.py) laczy sie tu zamiast bezposrednio na port 4532.
        Komendy Hamlib przychodza jako tekst WS, odpowiedzi wracaja jako tekst WS.
        """
        ws = web.WebSocketResponse(heartbeat=30, compress=9)
        await ws.prepare(request)
        peer = request.remote
        print(f"[hamlib-ws] WSJT-X bridge polaczony: {peer}")

        from hamlib_server import HamlibSession

        # Tworzymy "wirtualny" reader/writer dla HamlibSession
        # uzywajac kolejek asyncio zamiast TCP
        cmd_queue  = asyncio.Queue()
        resp_queue = asyncio.Queue()

        class WSReader:
            async def readline(self):
                line = await cmd_queue.get()
                return (line + '\n').encode()

        class WSWriter:
            def get_extra_info(self, key):
                return peer if key == 'peername' else None
            def write(self, data): pass
            async def drain(self): pass
            def close(self): pass

        # Uruchom HamlibSession w osobnym tasku
        session = HamlibSession(self.rig, self.hub, WSReader(), WSWriter(),
                                "ws-bridge", app=self)

        async def session_loop():
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        cmd = msg.data.strip()
                        if not cmd:
                            continue
                        # Przetworz komende i odpowiedz przez WS
                        response = await session._handle(cmd)
                        await ws.send_str(response)
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                        break
            except Exception as e:
                print(f"[hamlib-ws] Blad: {e}")
            finally:
                print(f"[hamlib-ws] Rozlaczono: {peer}")
        
        await session_loop()
        return ws



    def _current_rig_id(self) -> str:
        """ID aktualnie polaczonego radia w config.json['rigs'] (lub '0' jesli brak)."""
        rigs = self.cfg.get("rigs") or []
        model = str(getattr(self.rig, "model", ""))
        for r in rigs:
            if str(r.get("model", "")) == model:
                return str(r.get("id", "0"))
        return str(rigs[0].get("id", "0")) if rigs else "0"

    def _get_enabled_features(self, rig_id: str) -> dict:
        """Whitelist admina dla danego radia z config.json (lub default)."""
        rigs = self.cfg.get("rigs") or []
        for r in rigs:
            if str(r.get("id", "")) == str(rig_id):
                ef = r.get("enabledFeatures")
                if ef:
                    return ef
        return default_enabled_features()

    def _get_enabled_dynamic(self, rig_id: str) -> dict:
        """Whitelist admina dla dynamicznych actions/sliders (per dynamic_id)."""
        rigs = self.cfg.get("rigs") or []
        for r in rigs:
            if str(r.get("id", "")) == str(rig_id):
                return r.get("enabledDynamic") or {}
        return {}

    async def _get_rig_features(self, role: str) -> dict:
        """
        GET /api/rig/features
        - admin: pelna lista (statyczne features + dynamiczne actions/sliders)
                 z capabilities/enabled/effective (do panelu admina)
        - viewer/operator: tylko effective (statyczne) + dozwolone dynamiczne
        """
        try:
            capabilities = await self.rig.get_capabilities()
        except Exception as e:
            print(f"[features] get_capabilities blad: {e}")
            capabilities = {"actions": [], "sliders": [], "raw_caps": {}}

        # Kompatybilnosc: jesli get_capabilities zwrocilo stary format (plaski dict bool)
        if "raw_caps" not in capabilities and "actions" not in capabilities:
            capabilities = {"actions": [], "sliders": [], "raw_caps": capabilities}

        rig_id = self._current_rig_id()
        enabled = self._get_enabled_features(rig_id)
        enabled_dyn = self._get_enabled_dynamic(rig_id)

        if role == "admin":
            return {
                "ok": True,
                "rigId": rig_id,
                "model": getattr(self.rig, "model", ""),
                "sim": self.rig.sim,
                "features": features_for_admin(capabilities, enabled),
                "dynamic": dynamic_for_admin(capabilities, enabled_dyn),
            }
        else:
            eff = effective_features(capabilities, enabled)
            active = [
                {"id": f["id"], "label": f["label"], "icon": f["icon"], "group": f["group"]}
                for f in FEATURES if eff.get(f["id"])
            ]
            dyn = effective_dynamic(capabilities, enabled_dyn)
            return {"ok": True, "rigId": rig_id, "active": active,
                    "actions": dyn["actions"], "sliders": dyn["sliders"]}

    async def _set_rig_features(self, body: dict) -> dict:
        """
        POST /api/rig/features
        body: {rigId?: str, enabledFeatures?: {feature_id: bool},
               enabledDynamic?: {dynamic_id: bool}}
        Admin-only — zapisuje whitelisty do config.json['rigs'][i].
        """
        rig_id = str(body.get("rigId") or self._current_rig_id())
        new_enabled     = body.get("enabledFeatures") or {}
        new_enabled_dyn = body.get("enabledDynamic") or {}

        # Walidacja statycznych — tylko znane feature_id
        known_ids = {f["id"] for f in FEATURES}
        new_enabled = {k: bool(v) for k, v in new_enabled.items() if k in known_ids}
        # Dynamiczne: walidujemy tylko typ (bool), id sa dynamiczne wiec nie filtrujemy
        new_enabled_dyn = {k: bool(v) for k, v in new_enabled_dyn.items()}

        if not self.cfg.get("rigs"):
            self.cfg["rigs"] = []
        rigs = self.cfg["rigs"]
        found = False
        for r in rigs:
            if str(r.get("id", "")) == rig_id:
                merged = dict(r.get("enabledFeatures") or default_enabled_features())
                merged.update(new_enabled)
                r["enabledFeatures"] = merged

                merged_dyn = dict(r.get("enabledDynamic") or {})
                merged_dyn.update(new_enabled_dyn)
                r["enabledDynamic"] = merged_dyn
                found = True
                break
        if not found and rigs:
            merged = dict(rigs[0].get("enabledFeatures") or default_enabled_features())
            merged.update(new_enabled)
            rigs[0]["enabledFeatures"] = merged

            merged_dyn = dict(rigs[0].get("enabledDynamic") or {})
            merged_dyn.update(new_enabled_dyn)
            rigs[0]["enabledDynamic"] = merged_dyn
            rig_id = str(rigs[0].get("id", "0"))

        save_json(CFG_F, self.cfg)
        print(f"[features] admin zapisal enabledFeatures dla rig={rig_id}: "
              f"{new_enabled} | enabledDynamic: {new_enabled_dyn}")

        # Rozeslij nowy effective do wszystkich klientow (panel sie odswiezy)
        try:
            capabilities = await self.rig.get_capabilities()
        except Exception:
            capabilities = {"actions": [], "sliders": [], "raw_caps": {}}
        if "raw_caps" not in capabilities and "actions" not in capabilities:
            capabilities = {"actions": [], "sliders": [], "raw_caps": capabilities}

        enabled     = self._get_enabled_features(rig_id)
        enabled_dyn = self._get_enabled_dynamic(rig_id)
        eff = effective_features(capabilities, enabled)
        active = [
            {"id": f["id"], "label": f["label"], "icon": f["icon"], "group": f["group"]}
            for f in FEATURES if eff.get(f["id"])
        ]
        dyn = effective_dynamic(capabilities, enabled_dyn)
        await self.hub.broadcast({"type": "rig_features", "rigId": rig_id,
                                   "active": active,
                                   "actions": dyn["actions"], "sliders": dyn["sliders"]})

        return {"ok": True, "rigId": rig_id,
                "enabledFeatures": self._get_enabled_features(rig_id),
                "enabledDynamic": self._get_enabled_dynamic(rig_id)}

    async def _cloudlog_push_qso(self, user_id: str, qso: dict):
        """
        Wyslij QSO do CloudLog/WaveLog jesli uzytkownik ma skonfigurowane API.

        WAZNE: Cloudlog API przyjmuje QSO WYLACZNIE jako ciag ADIF w polu
        'string' (type='adif'). Wysylanie pol JSON (call, band, mode...) NIE
        DZIALA - Cloudlog odpowiada 200 ale QSO nie trafia do logu.
        Nazwa pola profilu to 'station_profile_id' (nie 'station_id').
        """
        try:
            u = self.find_user_by_id(user_id)
            if not u: return
            cl = (u or {}).get("cloudlog", {})
            url     = cl.get("url", "").rstrip("/")
            api_key = cl.get("apiKeyQso", "")
            station = cl.get("stationId", 1)
            if not url or not api_key: return
            # Nie dubluj /index.php jesli user go wpisal
            base = url[:-10].rstrip("/") if url.endswith("/index.php") else url

            import aiohttp as _aiohttp
            adif = qso_to_adif(qso)
            payload = {
                "key":               api_key,
                "station_profile_id": str(station),
                "type":              "adif",
                "string":            adif,
            }
            async with _aiohttp.ClientSession() as sess:
                async with sess.post(
                    f"{base}/index.php/api/qso", json=payload,
                    headers={"Content-Type": "application/json",
                             "Accept": "application/json"},
                    timeout=_aiohttp.ClientTimeout(total=8)
                ) as resp:
                    txt = await resp.text()
                    if resp.status in (200, 201):
                        # Cloudlog zwraca np. {"status":"created","adif_count":1}
                        # albo {"status":"failed","reason":"..."}
                        try:
                            rdata = await resp.json(content_type=None)
                        except Exception:
                            rdata = {}
                        if isinstance(rdata, dict) and rdata.get("status") == "failed":
                            print(f"[cloudlog] {qso.get('call')}: odrzucone — "
                                  f"{rdata.get('reason', '?')}", flush=True)
                            return
                        print(f"[cloudlog] {qso.get('call')} wyslane OK", flush=True)
                        # Zapisz ID z Cloudloga jesli zwrocone
                        cl_id = ""
                        if isinstance(rdata, dict):
                            cl_id = rdata.get("id") or rdata.get("qso_id") or ""
                        if cl_id:
                            try:
                                await asyncio.to_thread(
                                    self._save_cloudlog_id, qso["id"], str(cl_id))
                            except Exception:
                                pass
                    else:
                        print(f"[cloudlog] {qso.get('call')}: HTTP {resp.status} "
                              f"— {txt[:100]}", flush=True)
        except Exception as e:
            print(f"[cloudlog] push QSO blad: {e}", flush=True)

    def _save_cloudlog_id(self, qso_id: str, cloudlog_id: str):
        """Zapisz ID z Cloudloga przy QSO (sync - wolane przez to_thread)."""
        with qso_db._conn_lock:
            conn = qso_db._get_conn()
            conn.execute("UPDATE qso SET cloudlog_id=? WHERE id=?",
                         (cloudlog_id, qso_id))
            conn.commit()

    def _is_band_allowed(self) -> bool:
        """Sprawdz czy aktualna czestotliwosc radia jest w dozwolonym pasmie."""
        freq         = self.rig.freq
        enabled      = self.cfg.get("enabledBands", None)
        if not enabled:          # brak konfiguracji = wszystkie pasma dozwolone
            return True
        all_bands = {
            '160m': (1810000,  2000000),
            '80m':  (3500000,  3800000),
            '60m':  (5351500,  5366500),
            '40m':  (7000000,  7200000),
            '30m':  (10100000, 10150000),
            '20m':  (14000000, 14350000),
            '17m':  (18068000, 18168000),
            '15m':  (21000000, 21450000),
            '12m':  (24890000, 24990000),
            '10m':  (28000000, 29700000),
            '6m':   (50000000, 52000000),
            '4m':   (70000000, 70500000),
            '2m':  (144000000,146000000),
            '70cm':(430000000,440000000),
        }
        for band in enabled:
            lo, hi = all_bands.get(band, (0, 0))
            if lo <= freq <= hi:
                return True
        return False

    # Wspolna tabela pasm dla wszystkich sprawdzen (unikamy powtarzania)
    _BAND_RANGES = {
        '160m': (1800000,   2000000),
        '80m':  (3500000,   3800000),
        '60m':  (5300000,   5410000),
        '40m':  (7000000,   7200000),
        '30m':  (10100000,  10150000),
        '20m':  (14000000,  14350000),
        '17m':  (18068000,  18168000),
        '15m':  (21000000,  21450000),
        '12m':  (24890000,  24990000),
        '10m':  (28000000,  29700000),
        '6m':   (50000000,  54000000),
        '4m':   (70000000,  70500000),
        '2m':   (144000000, 146000000),
        '70cm': (430000000, 440000000),
    }

    def _get_band_for_freq(self, hz: int) -> str | None:
        """Zwroc nazwe pasma dla czestotliwosci lub None gdy poza pasmami."""
        for band, (lo, hi) in self._BAND_RANGES.items():
            if lo <= hz <= hi:
                return band
        return None

    def _is_split_cross_band(self) -> tuple[bool, str, str]:
        """Sprawdz czy split jest cross-band (VFO-A i VFO-B w roznych pasmach).
        Zwraca (is_cross_band, band_a, band_b) — is_cross_band=True gdy split
        aktywny i pasma sie roznia. band_a/b sa nazwami pasm lub 'poza pasmem'.

        Zabezpieczenie chroni przed uszkodzeniem radia/anteny — nadawanie
        w innym pasmie niz strojenie ATU moze uszkodzic wzmacniacz koncowy
        albo antene z niewlasciwa impedancja.
        """
        if not getattr(self.rig, 'split', False):
            return False, '', ''
        freq_a = self.rig.freq
        freq_b = getattr(self.rig, 'freq_b', freq_a)
        band_a = self._get_band_for_freq(freq_a) or 'poza pasmem'
        band_b = self._get_band_for_freq(freq_b) or 'poza pasmem'
        return (band_a != band_b), band_a, band_b

    def _can_control_radio(self, ws, role: str) -> tuple[bool, str]:
        """Sprawdz czy uzytkownik moze wykonac akcje sterujaca radiem.
        Zwraca (allowed, reason). Admin ma zawsze prawo. Zwykli userzy tylko
        gdy trzymaja radio_lock. Bez locka moga tylko OGLADAC (widza
        wszystko na zywo, ale nie mogą zmieniać).
        """
        if role == "admin":
            return True, ""
        sender = self.online_users.get(ws, {})
        sender_uid = sender.get("user_id", "")
        if not sender_uid:
            return False, "Nie jestes zalogowany"
        if not self.radio_lock["user_id"]:
            return False, "Najpierw przejmij TRX (przycisk 'PRZEJMIJ TRX' w panelu OPERATORZY)"
        if self.radio_lock["user_id"] != sender_uid:
            holder = self.radio_lock["callsign"] or self.radio_lock["username"] or "?"
            return False, f"TRX jest w rekach: {holder}"
        return True, ""

    async def _start_tx_watchdog(self):
        """Watchdog PTT - auto-off po skonfigurowanym czasie.
        Chroni radio przed zawieszonym PTT (np. zamkniete okno w trakcie TX).
        Konfiguracja: config['tx_watchdog_s'] (default 180s = 3 min).
        Anulowany przez uzytkownika gdy PTT wylaczy normalnie (patrz handler PTT).
        """
        # Anuluj poprzedni watchdog jesli istnieje (np. szybkie ptt on/off/on)
        if hasattr(self, '_tx_watchdog_task') and self._tx_watchdog_task:
            self._tx_watchdog_task.cancel()

        async def _watchdog():
            timeout = int(self.cfg.get("tx_watchdog_s", 180))
            try:
                # Ostrzezenia w ostatnich 30/20/10 sekundach
                warn_at = [30, 20, 10]
                elapsed = 0
                while elapsed < timeout and self.rig.ptt:
                    await asyncio.sleep(1)
                    elapsed += 1
                    remaining = timeout - elapsed
                    if remaining in warn_at:
                        await self.hub.broadcast({
                            "type": "toast",
                            "msg": f"⚠ Watchdog TX: {remaining}s do auto-off",
                            "level": "warning"
                        })
                # Timeout — wymus PTT OFF
                if self.rig.ptt:
                    self.rig.ptt = False
                    if not self.rig.sim:
                        try: await self.rig.set_ptt(False)
                        except: pass
                    await self.hub.broadcast({"type": "ptt", "ptt": False})
                    await self.hub.broadcast({
                        "type": "toast",
                        "msg": f"⛔ Watchdog TX: PTT wylaczony automatycznie po {timeout}s",
                        "level": "error"
                    })
                    print(f"[watchdog] PTT auto-off po {timeout}s")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"[watchdog] blad: {e}")

        self._tx_watchdog_task = asyncio.create_task(_watchdog())

    def _feature_allowed(self, feature_id: str, role: str) -> bool:
        """
        Sprawdz czy dana akcja (feature_id) jest dozwolona dla uzytkownika
        o danej roli. Admin ma zawsze dostep (do testow/konfiguracji).
        Capabilities sprawdzamy z cache (self._caps_cache) jesli dostepny,
        inaczej domyslnie pozwalamy (fail-open dla kompatybilnosci) i
        logujemy ostrzezenie — webapp powinien wczesniej wywolac
        _refresh_caps_cache() po polaczeniu z radiem.
        """
        if role == "admin":
            return True
        caps = getattr(self, "_caps_cache", None) or {"actions": [], "sliders": [], "raw_caps": {}}
        enabled = self._get_enabled_features(self._current_rig_id())
        eff = effective_features(caps, enabled)
        return bool(eff.get(feature_id, False))

    async def _refresh_caps_cache(self):
        """Wywolaj po polaczeniu/zmianie radia — odswieza cache capabilities
        uzywany przez _feature_allowed() (synchroniczna funkcja w _ws_msg)."""
        try:
            caps = await self.rig.get_capabilities()
            if "raw_caps" not in caps and "actions" not in caps:
                caps = {"actions": [], "sliders": [], "raw_caps": caps}
            self._caps_cache = caps
        except Exception as e:
            print(f"[features] _refresh_caps_cache blad: {e}")
            self._caps_cache = {"actions": [], "sliders": [], "raw_caps": {}}

    def _rig_bcast(self, msg: dict):
        """
        Callback przekazywany do CivRig jako broadcast_sync. Przechwytuje
        wewnetrzny event 'rig_reconnected' (np. z _reconnect_loop po
        wlaczeniu radia po starcie serwera) — odswieza _caps_cache i
        rozsyla nowy /api/rig/features do klientow. Inne typy wiadomosci
        przekazuje dalej do hub.broadcast_sync bez zmian.
        """
        if msg.get("type") == "rig_reconnected":
            if self.hub._loop and self.hub._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._on_rig_reconnected(), self.hub._loop)
            return
        self.hub.broadcast_sync(msg)

    async def _verify_radio_awake_and_start_scope(self):
        """
        Po power ON radio potrzebuje chwili zanim CI-V zacznie odpowiadac.
        Probujemy odczytac czestotliwosc (do 5 prob co 1s). Dopiero gdy radio
        odpowie - startujemy scope/waterfall. Bez tego waterfall startowal
        zanim radio ozylo i ladowal w stanie SIM (test).
        """
        radio_alive = False
        for attempt in range(5):
            await asyncio.sleep(1.0)
            try:
                if hasattr(self.rig, "get_freq"):
                    f = await asyncio.wait_for(self.rig.get_freq(), timeout=2.0)
                    if f and f > 0:
                        radio_alive = True
                        print(f"[rig] radio ozylo po power ON (proba {attempt+1}, "
                              f"freq={f})", flush=True)
                        break
            except Exception:
                pass
            print(f"[rig] czekam na wybudzenie radia... ({attempt+1}/5)", flush=True)

        if not radio_alive:
            print("[rig] radio NIE odpowiada po power ON - scope pozostaje w SIM. "
                  "Sprawdz zasilanie 12V i CI-V Transceive=ON", flush=True)
            # Poinformuj userow ze radio nie wstalo
            await self.hub.broadcast({
                "type": "toast",
                "msg": "⚠️ Radio nie odpowiada po włączeniu — sprawdź zasilanie",
                "level": "warning"})
            return

        # Radio zyje - wznow scope. Po CI-V power OFF->ON radio przechodzi
        # pelny boot i "zapomina" ustawienia scope. Wysylanie komend za wczesnie
        # nie skutkuje - radio ich nie przyjmuje dopoki procesor scope nie wstanie
        # (znacznie dluzej niz odpowiedz na freq).
        #
        # STRATEGIA: ponawiaj _enable_scope co 1.5s i SPRAWDZAJ czy ramki
        # faktycznie zaczely plynac (licznik _scope_rx_count w civ.py rosnie).
        # Konczymy gdy ramki plyna albo po 6 probach (~10s).
        scope_ok = False
        try:
            if hasattr(self.rig, "scope_start"):
                for scope_try in range(6):
                    # Zapamietaj licznik ramek przed proba
                    cnt_before = getattr(self.rig, "_scope_rx_count", 0)
                    # Wymus pelne wlaczenie scope
                    self.rig.scope_start()
                    await asyncio.sleep(1.5)
                    # Czy przyszly nowe ramki?
                    cnt_after = getattr(self.rig, "_scope_rx_count", 0)
                    if cnt_after > cnt_before:
                        scope_ok = True
                        print(f"[rig] scope ramki plyna po power ON "
                              f"(proba {scope_try+1}, +{cnt_after-cnt_before} ramek)",
                              flush=True)
                        break
                    print(f"[rig] scope jeszcze nie wysyla ramek, ponawiam "
                          f"({scope_try+1}/6)...", flush=True)
                if not scope_ok:
                    print("[rig] UWAGA: scope nie wznowil sie po power ON. "
                          "Radio moglo wyjsc z trybu scope - sprawdz czy na "
                          "EKRANIE radia jest widoczny waterfall (przycisk SCOPE).",
                          flush=True)
                    await self.hub.broadcast({
                        "type": "toast",
                        "msg": "⚠️ Waterfall nie wrócił po włączeniu radia — "
                               "sprawdź czy na ekranie radia widać SCOPE",
                        "level": "warning"})
        except Exception as e:
            print(f"[rig] scope_start po power ON blad: {e}", flush=True)
        await self._on_rig_reconnected()
        # Potwierdz userom ze radio dziala
        await self.hub.broadcast({"type": "power_state", "value": True})

    async def _on_rig_reconnected(self):
        """
        Odswiez capabilities cache i rozeslij rig_features po (re)polaczeniu CivRig.

        WAZNE: to jest wolane ZA KAZDYM RAZEM gdy radio przechodzi z SIM na
        realne (przez _reconnect_loop w civ.py). Wlaczamy tu scope, bo w
        momencie _initial_rig_connect radio moglo jeszcze byc w SIM - scope
        nie wystartowal wtedy. Tutaj mamy pewnosc ze radio odpowiada.
        """
        await self._refresh_caps_cache()
        # Auto-wlacz scope (waterfall) - radio jest realne i odpowiada.
        # Bez tego waterfall pokazuje tylko SIM, bo _enable_scope nie bylo
        # wolane (frontend startScope() nikt nie wola).
        try:
            if not self.rig.sim and hasattr(self.rig, "scope_start"):
                # scope_start robi zapisy do portu szeregowego z time.sleep —
                # blokujace. Wywolane wprost w petli zamrazalo asyncio (~100ms,
                # wykryte przez looplag). Wynosimy do watku.
                await asyncio.to_thread(self.rig.scope_start)
                print("[rig] scope wlaczony (radio realne, reconnect)", flush=True)
        except Exception as e:
            print(f"[rig] scope_start w reconnect blad: {e}", flush=True)
        rig_id = self._current_rig_id()
        enabled     = self._get_enabled_features(rig_id)
        enabled_dyn = self._get_enabled_dynamic(rig_id)
        caps = self._caps_cache
        eff = effective_features(caps, enabled)
        active = [
            {"id": f["id"], "label": f["label"], "icon": f["icon"], "group": f["group"]}
            for f in FEATURES if eff.get(f["id"])
        ]
        dyn = effective_dynamic(caps, enabled_dyn)
        await self.hub.broadcast({"type": "rig_features", "rigId": rig_id,
                                   "active": active,
                                   "actions": dyn["actions"], "sliders": dyn["sliders"]})
        print("[rig] reconnect — rig_features rozeslane do klientow")

    def _webrtc_tx_start(self):
        """Hook wywolywany gdy WebRTC track audio zaczyna nadawac."""
        print("[webrtc] TX track aktywny")

    def _webrtc_tx_stop(self):
        """Hook wywolywany gdy WebRTC track audio sie konczy."""
        print("[webrtc] TX track zakonczony")
        if self.audio.tx_active:
            self.audio.stop_tx()

    async def _cw_decode_loop(self):
        """Karmi dekoder CW surowym audio z karty (z pominieciem Opus).

        Kluczowe dla jakosci: sygnal z przegladarki przechodzil przez kodek
        stratny, ktory rozmywa krawedzie kluczowania (zmierzony kontrast
        obwiedni 6.4x wobec >20x potrzebnych modelowi). Tu bierzemy PCM prosto
        z bufora karty, w tej samej postaci co dla FT8.
        """
        try:
            while True:
                await asyncio.sleep(0.25)
                if deepcw_engine is None or not self.audio:
                    continue
                got = self.audio.pop_cw_rx_audio()
                if not got:
                    continue
                pcm, src_rate = got
                if pcm is None or len(pcm) == 0:
                    continue
                await deepcw_engine.feed(pcm, src_rate,
                                         broadcast_fn=self.hub.broadcast)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[deepcw] petla dekodera przerwana: {e}", flush=True)

    async def _ws_msg(self, msg: dict, ws, role: str = "viewer"):
        t = msg.get("type", "")

        # ── Radio Lock: weryfikuj blokade dla akcji sterujacych radiem ────────
        # Admin moze zawsze. Zwykly uzytkownik musi "trzymac" radio.
        # Akcje read-only (subskrypcja audio, waterfall, pobieranie stanu)
        # sa zawsze dozwolone.
        _CONTROL_TYPES = {
            "freq","freqB","mode","ptt","rig_slider","rig_action","vfo","vfo_op",
            "split","preamp","attenuator","tuner","tuner_autotune","ft8_tx",
        }

        # ── VIEWER: TWARDA BIALA LISTA (2026-07-05 operator) ──────────────────
        # Viewer moze robic TYLKO: zmiana czestotliwosci, trybu i pasma
        # (do sluchania). WSZYSTKO inne jest zablokowane na sztywno w backendzie
        # NIEZALEZNIE od uprawnien ktore admin nadal przy tworzeniu usera
        # (nowy user dostaje domyslnie prawa do ptt/buttonow, ale dla viewera
        # sa one IGNOROWANE tutaj). Przyciski w UI pozostaja widoczne (zeby nie
        # psuc ukladu), ale klikniecie nic nie robi - serwer odrzuca.
        #
        # Powod: viewer wczesniej mial dostep do power_toggle (wylaczyl radio!),
        # sliderow i przyciskow funkcyjnych. Teraz moze tylko stroic.
        _VIEWER_ALLOWED = {
            "freq", "freqB", "mode",   # strojenie + pasmo (pasmo = freq+mode)
            # read-only (podglad) - zawsze OK, nie dotykaja radia:
            "subscribe", "unsubscribe", "ping", "pong",
            "ft8_rx_enable", "ft4_rx_enable",  # tylko odbior dekodowania
            "get_state", "get_status",
        }
        if role == "viewer" and t in _CONTROL_TYPES and t not in _VIEWER_ALLOWED:
            await ws.send_json({
                "type": "toast",
                "msg": "⛔ Ta funkcja jest niedostepna dla obserwatora. "
                       "Mozesz zmieniac tylko czestotliwosc, tryb i pasmo. "
                       "Popros admina o role operatora.",
                "level": "error",
            })
            print(f"[viewer-block] odrzucono '{t}' dla viewera (id="
                  f"{self.online_users.get(ws, {}).get('user_id', '?')})", flush=True)
            return

        # Akcje NADAWANIA (TX) - zabronione dla viewera NAWET z lockiem.
        # (redundantne z whitelista wyzej, ale zostawiam jako druga warstwa)
        _TX_TYPES = {"ptt", "ft8_tx", "ft4_tx", "cw", "cw_send", "tune"}
        if t in _TX_TYPES and role == "viewer":
            await ws.send_json({
                "type": "toast",
                "msg": "⛔ Nadawanie niedostepne dla obserwatora (viewer).",
                "level": "error",
            })
            return

        if t in _CONTROL_TYPES and role != "admin":
            # Znajdz uid nadawcy po ws w online_users
            sender = self.online_users.get(ws, {})
            sender_uid = sender.get("user_id", "")
            if not self._user_has_lock(sender_uid):
                # Radio wolne lub zajete przez kogos innego — blokuj TX
                holder = self.radio_lock.get("callsign") or self.radio_lock.get("username") or ""
                msg_err = f"Radio zajete przez {holder} — poproś o dostęp" if holder else "Najpierw przejmij radio (kliknij PRZEJMIJ TRX)"
                await ws.send_str(json.dumps({
                    "type":    "radio_locked",
                    "message": msg_err,
                    "holder":  holder,
                }))
                return

        # Ping — odpowiedz pong bezposrednio do nadawcy (RTT measurement)
        if t == "ping":
            await ws.send_json({"type": "pong", "t": msg.get("t", 0)})
            return

        # ── Subscription channels — klient dodaje/usuwa kanaly przy zmianie zakladki
        if t == "subscribe":
            channels = msg.get("channels", [])
            if isinstance(channels, list):
                # 'set' zastepuje pelen zestaw kanalow (uzywane przy setPage),
                # 'add' dodaje bez usuwania (np. dla dodatkowych features)
                mode = msg.get("mode", "set")
                if mode == "add":
                    await self.hub.subscribe(ws, channels)
                else:
                    await self.hub.set_channels(ws, channels)
            return

        if t == "unsubscribe":
            channels = msg.get("channels", [])
            if isinstance(channels, list):
                await self.hub.unsubscribe(ws, channels)
            return

        if t == "freq":
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            if not self._feature_allowed("freq_set", role):
                print(f"[rig] freq WS: BLOKADA _feature_allowed (role={role})")
                return
            hz = int(msg.get("freq", self.rig.freq))
            # UWAGA (perf): usunieto per-freq print — przy scrollowaniu VFO
            # freq zmienia sie ~20x/s, print zapychal event loop.
            self.rig.freq = hz
            if not self.rig.sim:
                try:
                    await self.rig.set_freq(hz)
                except Exception as _e:
                    print(f"[rig] set_freq blad: {_e}")
            await self.hub.broadcast({"type": "freq", "freq": hz}, skip=ws)

        elif t == "mode":
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            if not self._feature_allowed("mode_set", role):
                return
            prev_mode = self.rig.mode
            self.rig.mode = msg.get("mode", self.rig.mode)
            self.rig.bw   = int(msg.get("bandwidth", self.rig.bw))
            # filterNum: 1/2/3 = FIL1/2/3, wybor KTOREGO filtra skonfigurowanego
            # w menu radia uzyc (CI-V 06 <mode> <fil>) — nie zmienia szerokosci
            # filtra, tylko ktory z trzech "slotow" jest aktywny.
            fil = msg.get("filterNum")
            if fil in (1, 2, 3):
                self.rig.filter_num = fil
            if not self.rig.sim:
                try:
                    await self.rig.set_mode(self.rig.mode, self.rig.bw, fil or 0)
                except Exception as e:
                    print(f"[rig] set_mode BLAD dla mode={self.rig.mode!r} fil={fil}: {e!r}")

            # NIE wlaczamy automatycznie BK-IN przy przejsciu na CW.
            # BK-IN (CI-V 16 47 01) powoduje ze radio wchodzi w ciagle TX
            # czekajac na sygnal z fizycznego klucza CW — niepozadane przy
            # wysylaniu przez CI-V cmd 17. Uzytkownik moze wlaczyc BK-IN
            # recznie przez przycisk Break-In w panelu funkcji radia.

            await self.hub.broadcast({"type": "mode", "mode": self.rig.mode,
                                       "bandwidth": self.rig.bw,
                                       "filterNum": self.rig.filter_num}, skip=ws)

        elif t == "ft8_qsy":
            # Przestrojenie na pasmo FT8/FT4 z listy w panelu WSJT-X (wj-band-
            # select -> tuneToBand() w wsjtx.js). NAPRAWA: front od dawna
            # wysylal ten typ wiadomosci ("jedna atomowa komenda ft8_qsy"),
            # ale backend nigdy nie mial dla niej handlera — wiec wybor pasma
            # z listy nic nie robil, czestotliwosc sie nie przelaczala.
            # Ustawiamy tryb+filtr PRZED czestotliwoscia (sekwencyjnie na tym
            # samym polaczeniu CI-V), tak jak opisuje komentarz w wsjtx.js —
            # zapobiega wyscigowi freq/mode wysylanych osobno.
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            if not (self._feature_allowed("freq_set", role)
                    and self._feature_allowed("mode_set", role)):
                return
            hz = int(msg.get("freq", self.rig.freq))
            # Pasmo docelowe musi byc dozwolone (jak przy CQ/TX) — sprawdzamy
            # PRZED zmiana stanu radia, inaczej lista pasm FT8 obchodzilaby
            # blokade admina, ktora normalny selektor pasm respektuje.
            enabled = self.cfg.get("enabledBands")
            if enabled and self._get_band_for_freq(hz) not in enabled:
                await ws.send_json({"type": "toast",
                                     "msg": "⛔ QSY zablokowany — pasmo niedozwolone przez admina",
                                     "level": "error"})
                return
            # Tryb cyfrowy FT8/FT4 na IC-7300: zawsze USB-D + FIL1 (konwencja
            # calego projektu, niezaleznie od tego co przyszlo w wiadomosci).
            self.rig.mode = msg.get("mode") or "USB-D"
            self.rig.filter_num = 1
            if not self.rig.sim:
                try:
                    await self.rig.set_mode(self.rig.mode, self.rig.bw, self.rig.filter_num)
                except Exception as e:
                    print(f"[ft8] ft8_qsy set_mode blad: {e!r}")
            self.rig.freq = hz
            if not self.rig.sim:
                try:
                    await self.rig.set_freq(hz)
                except Exception as e:
                    print(f"[ft8] ft8_qsy set_freq blad: {e!r}")
            print(f"[ft8] QSY: {hz/1e6:.6f} MHz {self.rig.mode} FIL{self.rig.filter_num}")
            # Bez skip=ws: w przeciwienstwie do zwyklych handlerow freq/mode,
            # tuneToBand() we froncie NIE aktualizuje lokalnie S.freq/S.mode —
            # klient ktory kliknal pasmo tez polega na tym broadcascie.
            await self.hub.broadcast({"type": "mode", "mode": self.rig.mode,
                                       "bandwidth": self.rig.bw,
                                       "filterNum": self.rig.filter_num})
            await self.hub.broadcast({"type": "freq", "freq": hz})

        elif t == "ptt":
            if not self._feature_allowed("ptt", role):
                return
            # Sprawdz czy user ma radio — tylko aktywny operator lub admin moze TX
            if bool(msg.get("ptt")) and role != "admin":
                sender = self.online_users.get(ws, {})
                sender_uid = sender.get("user_id", "")
                if self.radio_lock["user_id"] and not self._user_has_lock(sender_uid):
                    holder = self.radio_lock["callsign"] or self.radio_lock["username"] or "?"
                    await self.hub.broadcast({"type": "toast", "msg": f"⛔ PTT zablokowany — radio ma {holder}", "level": "error"})
                    return
            # Blokada TX na niedozwolonym pasmie
            if bool(msg.get("ptt")) and not self._is_band_allowed():
                await self.hub.broadcast({"type": "toast", "msg": "⛔ TX zablokowany — pasmo niedozwolone przez admina", "level": "error"})
                return
            # Blokada TX w cross-band split — VFO-A i VFO-B w roznych pasmach
            # (chroni radio/antene przed nadawaniem w niewlasciwym pasmie)
            if bool(msg.get("ptt")):
                cross, band_a, band_b = self._is_split_cross_band()
                if cross:
                    await self.hub.broadcast({"type": "toast",
                        "msg": f"⛔ TX zablokowany — split cross-band ({band_a} RX / {band_b} TX). Wylacz split lub ustaw VFO-B na to samo pasmo.",
                        "level": "error"})
                    return
            self.rig.ptt = bool(msg.get("ptt"))
            if not self.rig.sim:
                # Uwaga o audio z USB na IC-7300: audio z USB moduluje TYLKO gdy
                # radio jest w trybie DATA (USB-D) - to ograniczenie sprzetowe
                # radia (DATA MOD=USB dziala tylko w data mode; w zwyklym USB
                # radio bierze z MIC). RCForb "TXd" wchodzi w data mode z tego
                # powodu. NIE przelaczamy trybu automatycznie, bo USB-D ma inne
                # filtry (obawa uzytkownika). Zamiast tego: user nadaje swiadomie
                # w USB-D (z filtrem ustawionym szeroko w radiu), ALBO ustawia
                # DATA OFF MOD=USB w radiu by zwykly USB tez bral audio z USB.
                # Auto-przelaczanie mozliwe przez flage audio_tx_force_data_mode
                # (domyslnie OFF - nie rusza filtrow bez zgody usera).
                _audio_tx = bool(getattr(self.audio, "tx_active", False))
                _force = self.cfg.get("audio_tx_force_data_mode", False)
                if (self.rig.ptt and _audio_tx and _force
                        and not getattr(self.rig, "data_mode", False)):
                    try:
                        base = self.rig.mode.replace("-D", "")
                        await self.rig.set_mode(base + "-D")
                        print(f"[ptt] Audio TX -> DATA ({base}-D) [flaga force ON]", flush=True)
                        await self.hub.broadcast({"type": "mode", "mode": self.rig.mode})
                    except Exception as e:
                        print(f"[ptt] data mode blad: {e}", flush=True)
                try: await self.rig.set_ptt(self.rig.ptt)
                except: pass
            await self.hub.broadcast({"type": "ptt", "ptt": self.rig.ptt})
            # Watchdog TX: uruchom timer max czasu nadawania. Konfigurowalne w
            # config.json ("tx_watchdog_s" domyslnie 180s = 3 minuty).
            # Watchdog chroni przed pozostawionym wlaczonym PTT (np. user
            # zamknal laptop myslac ze wylaczyl TX, albo Chrome zawiesil sie
            # w trakcie TX).
            if self.rig.ptt:
                asyncio.ensure_future(self._start_tx_watchdog())
            else:
                # Anuluj watchdog gdy PTT OFF (user sam zakonczyl TX na czas)
                if hasattr(self, '_tx_watchdog_task') and self._tx_watchdog_task:
                    self._tx_watchdog_task.cancel()
                    self._tx_watchdog_task = None

        elif t == "split":
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            if not self._feature_allowed("split", role):
                return
            self.rig.split  = bool(msg.get("split"))
            self.rig.freq_b = int(msg.get("freqB", self.rig.freq_b))
            if not self.rig.sim:
                try: await self.rig.set_split(self.rig.split)
                except: pass
            await self.hub.broadcast({"type": "split", "split": self.rig.split,
                                       "freqB": self.rig.freq_b})

        elif t == "freqB":
            # Ustawienie czestotliwosci VFO-B bez przelaczania aktywnego VFO
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            if not self._feature_allowed("freq_set", role):
                return
            hz = int(msg.get("freqB", self.rig.freq_b))
            self.rig.freq_b = hz
            if not self.rig.sim and hasattr(self.rig, 'set_freq_b'):
                try:
                    await self.rig.set_freq_b(hz)
                except Exception as _e:
                    print(f"[rig] set_freq_b blad: {_e}")
            await self.hub.broadcast({"type": "freqB", "freqB": hz}, skip=ws)

        elif t == "vfo":
            # {type:'vfo', vfo:'VFOA'|'VFOB'} -> CI-V 07 00/01 (wybor aktywnego VFO)
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            if not self._feature_allowed("freq_set", role):
                return
            vfo = "VFOB" if str(msg.get("vfo", "VFOA")).upper() in ("VFOB", "B") else "VFOA"
            self.rig.vfo = vfo
            if not self.rig.sim:
                try: await self.rig.set_vfo(vfo)
                except Exception as e: print(f"[rig] set_vfo blad: {e}")
            # Do WSZYSTKICH (bez skip): stan vfo synchronizuje przyciski u
            # kazdego klienta, takze u nadawcy (idempotentne).
            await self.hub.broadcast({"type": "vfo", "vfo": vfo})

        elif t == "vfo_op":
            # {type:'vfo_op', op:'swap'|'equalize'}
            #   swap     -> CI-V 07 B0 (A<->B)
            #   equalize -> CI-V 07 A0 (A->B, kopiuje freq/mode z A do B)
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            if not self._feature_allowed("freq_set", role):
                return
            op = msg.get("op", "")
            if op not in ("swap", "equalize"):
                return
            if op == "swap":
                await self.rig.vfo_swap()
            else:
                await self.rig.vfo_equalize()

            # Po operacji odczytaj swiezo freq A z radia (07 A0/B0 zmienia
            # obie wartosci VFO) — w sim/fallback vfo_swap/vfo_equalize juz
            # zaktualizowaly self.rig.freq/freq_b lokalnie.
            await self.hub.broadcast({"type": "freq", "freq": self.rig.freq})
            await self.hub.broadcast({"type": "freqB", "freqB": self.rig.freq_b})

        elif t == "preamp":
            # {type:'preamp', value: 0|1|2} -> CI-V 16 02 (0=OFF,1=P.AMP1,2=P.AMP2)
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            if not self._feature_allowed("mode_set", role):
                return
            val = int(msg.get("value", 0))
            if val not in (0, 1, 2):
                return
            if not self.rig.sim:
                try: await self.rig.set_preamp(val)
                except Exception as e: print(f"[rig] set_preamp blad: {e}")
            else:
                self.rig.preamp = val
            await self.hub.broadcast({"type": "preamp", "value": self.rig.preamp}, skip=ws)

        elif t == "attenuator":
            # {type:'attenuator', value: bool} -> CI-V 11 (00=OFF, 20=ON 20dB)
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            if not self._feature_allowed("mode_set", role):
                return
            val = bool(msg.get("value", False))
            if not self.rig.sim:
                try: await self.rig.set_attenuator(val)
                except Exception as e: print(f"[rig] set_attenuator blad: {e}")
            else:
                self.rig.attenuator = val
            await self.hub.broadcast({"type": "attenuator", "value": self.rig.attenuator}, skip=ws)

        elif t == "tuner":
            # {type:'tuner', value: bool} -> CI-V 1C 01 <00|01> (Tuner OFF/ON)
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            if not self._feature_allowed("mode_set", role):
                return
            val = bool(msg.get("value", False))
            if not self.rig.sim:
                try: await self.rig.set_tuner(val)
                except Exception as e: print(f"[rig] set_tuner blad: {e}")
            else:
                self.rig.tuner = val
            await self.hub.broadcast({"type": "tuner", "value": self.rig.tuner}, skip=ws)

        elif t == "tuner_autotune":
            # {type:'tuner_autotune'} -> CI-V 1C 01 01 + 1C 01 02 (START cyklu
            # auto-tuningu). Jednorazowa akcja — generuje krotki sygnal TX
            # niskiej mocy. Uprawnienia jak PTT (faktycznie wywoluje TX).
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            if not self._feature_allowed("ptt", role):
                return
            if not self.rig.sim:
                try:
                    await self.rig.start_tuner_autotune()
                except Exception as e:
                    print(f"[rig] start_tuner_autotune blad: {e}")
                    return
            else:
                self.rig.tuner = True
            await self.hub.broadcast({"type": "tuner", "value": self.rig.tuner})
            await self.hub.broadcast({"type": "toast", "message": "Tuner: autotune w trakcie..."})

        elif t == "level":
            # UI wysyla: {type:'level', param:'AF', value:50}
            lvl = msg.get("param") or msg.get("level", "RFPOWER")
            val = int(msg.get("value", 0))
            if lvl == "RFPOWER" and not self._feature_allowed("tx_power", role):
                return
            # Zapisz w stanie
            if lvl == "AF":      self.rig.af_gain  = val
            if lvl == "RFPOWER": self.rig.rf_power = val
            if lvl == "SQL":     self.rig.squelch  = val
            if not self.rig.sim:
                try: await self.rig.set_level(lvl, val)
                except: pass
            # Broadcast do innych klientow
            await self.hub.broadcast({"type":"level","param":lvl,"value":val}, skip=ws)

        elif t == "wsjtx_tx_start":
            # WSJT-X zaczyna nadawac — powiadom przegladarke klienta
            # zeby uruchomila TX mikrofon (getUserMedia → Opus → WS)
            await self.hub.broadcast({"type": "wsjtx_tx_start"}, skip=ws)

        elif t == "wsjtx_tx_stop":
            # WSJT-X konczy nadawanie — zatrzymaj TX mikrofon w przegladarce
            await self.hub.broadcast({"type": "wsjtx_tx_stop"}, skip=ws)

        elif t == "audio_tx_start":
            # Przegladarka zaczyna wysylac TX audio — uruchom playback na serwerze
            if not self.audio.tx_active:
                dev = self.cfg.get("audio", {}).get("txDevice")
                ok  = self.audio.start_tx(device=dev)
                print(f"[audio] TX start: {'OK' if ok else 'BLAD'} dev={dev}")

        elif t == "audio_tx_stop":
            # Przegladarka konczy TX — zatrzymaj playback
            if self.audio.tx_active:
                self.audio.stop_tx()
                print("[audio] TX stop")

        elif t == "ft8_tx":
            # Wlasny nadajnik FT8: PTT ON -> generuj+stream audio -> PTT OFF.
            # msg: {call_to, call_de, report (grid/raport/RRR/73/...), rFlag?,
            #       audioFreq? — opcjonalny override freq audio TX (Hz)}
            call_to = (msg.get("callTo") or "").strip().upper()
            call_de = (msg.get("callDe") or "").strip().upper()
            report  = (msg.get("report") or "").strip()
            r_flag  = bool(msg.get("rFlag", False))
            audio_freq_override = msg.get("audioFreq")  # None albo Hz
            if not call_to or not call_de or not report:
                await ws.send_json({"type": "ft8_tx_error", "error": "Brak callTo/callDe/report"})
                return
            # Jesli podano audioFreq — nadpisz TX freq PRZED enkodowaniem.
            # Uzywane przez Hound mode ktory nadaje na roznych freq (CQ wolanie >1000Hz,
            # potem R+RPT w slocie Foxa 300-540 Hz). Nie respektujemy self._ft8_tx_frozen
            # bo Hound mode wie co robi - nie chce zeby freeze go zablokowal.
            if audio_freq_override is not None:
                try:
                    new_freq = int(audio_freq_override)
                    if 100 <= new_freq <= 5000:  # sensowny zakres audio FT8
                        self._ft8_tx_freq_hz = new_freq
                        await self.hub.broadcast({"type": "ft8_tx_freq",
                                                   "freqHz": self._ft8_tx_freq_hz,
                                                   "frozen": self._ft8_tx_frozen})
                except (ValueError, TypeError):
                    pass  # cicho ignoruj zle audioFreq
            # Jesli automat wlaczony i wyslujemy grid (Tx2) lub CQ do konkretnej
            # stacji — automatycznie wstaw engine w stan CALLING zeby automat
            # mogl zareagowac na odpowiedz bez potrzeby dwukliku
            if self._auto_seq_enabled and call_to != "CQ":
                # Jesli wolamy konkretna stacje a leci petla CQ - zatrzymaj CQ.
                # Inaczej CQ i wolanie stacji walcza o TX (ten sam konflikt co
                # przy CQ ze starym QSO). Wolanie stacji ma pierwszenstwo.
                if self._cq_calling:
                    print(f"[cq] Zatrzymuje CQ - user wola konkretna stacje {call_to}")
                    self._stop_cq_calling()
                is_grid = (len(report) in (4, 6) and
                           report[:2].isalpha() and report[2:4].isdigit())
                print(f"[autoqso] ft8_tx check: auto_seq={self._auto_seq_enabled} call_to={call_to} report={report!r} is_grid={is_grid} state={self._qso_engine.state}")
                if is_grid and self._qso_engine.state == "IDLE":
                    self._qso_engine.start_qso(call_to)
                    print(f"[autoqso] Reczny TX grid → auto-start QSO z {call_to}")
                    await self.hub.broadcast({"type": "auto_qso_status",
                                               "state": self._qso_engine.state,
                                               "partner": self._qso_engine.partner_call})
                elif is_grid and self._qso_engine.partner_call == call_to:
                    # Juz mamy QSO z ta stacja — nie robimy nic
                    pass
            # ── CQ: wolanie cykliczne (nie jednorazowe) ──────────────────────
            # Gdy user wola CQ, uruchamiamy petle ktora ponawia CQ co pelny
            # okres (2 okna) az ktos odpowie albo user/timer zatrzyma. Prawdziwy
            # WSJT-X tak dziala - nie wiemy czy ktos odpowie po 1. czy 4. CQ.
            if call_to == "CQ":
                # KRYTYCZNE: rozpoczecie CQ musi wyczyscic niedokonczone QSO.
                # Bez tego, jesli wczesniej wolales stacje (start_qso -> stan
                # ST_CALLING, partner_call ustawiony) i nie dokonczyles QSO,
                # maszyna stanow zostaje w starym stanie. Wtedy pojawia sie
                # KONFLIKT: petla CQ leci, ale engine wciaz probuje dokonczyc
                # stare QSO wg starego stanu -> automatyka "wariuje", backend
                # i UI sie rozjezdzaja. Reset stanu przed CQ to naprawia.
                if self._qso_engine.state != qso_engine.ST_IDLE:
                    print(f"[cq] Reset niedokonczonego QSO ({self._qso_engine.state}, "
                          f"partner={self._qso_engine.partner_call}) przed CQ")
                    self._qso_engine.abort_qso()
                # Przerwij tez ewentualny trwajacy TX sekwencer
                if self._ft8_tx_lock.locked():
                    self._ft8_tx_abort = True
                # KRYTYCZNE (Call 1st): ustaw znak OPERATORA w silniku PRZED
                # wolaniem CQ. Engine startuje z my_call=CALLSIGN z configu
                # (placeholder XX0XXX / znak klubu); znak operatora byl
                # ustawiany TYLKO przy kliku w stacje (ft8_start_auto_qso).
                # Bez tego odpowiedzi na nasze CQ ("XX0XXX XXX ...") byly
                # odrzucane w on_decode jako "nie do nas" (call_to != my_call)
                # -> automat GLUCHY na wolajace stacje mimo wlaczonego 1st.
                _ui = self.online_users.get(ws, {})
                _ucall = (_ui.get("callsign") or "").strip().upper() or \
                         (call_de or "").upper()
                _uuid = _ui.get("user_id")
                if _uuid:
                    self._autoqso_uid = _uuid  # do auto-zapisu QSO z kolejki
                if _ucall:
                    _uobj = self.find_user_by_id(_uuid) or {}
                    _ugrid = (_uobj.get("locator") or LOCATOR).strip().upper()[:4]
                    if (self._qso_engine.my_call != _ucall or
                            self._qso_engine.my_grid != _ugrid):
                        self._qso_engine.my_call = _ucall
                        self._qso_engine.my_grid = _ugrid
                        print(f"[cq] Operator CQ: {_ucall} / {_ugrid}")
                self._cq_call_de = call_de
                self._cq_report = report
                if not self._cq_calling:
                    self._cq_calling = True
                    if self._cq_task and not self._cq_task.done():
                        self._cq_task.cancel()
                    self._cq_task = asyncio.create_task(self._cq_calling_loop())
                    print(f"[cq] Rozpoczeto cykliczne wolanie CQ: {call_de} {report}")
                else:
                    print(f"[cq] CQ juz aktywne - aktualizuje tresc")
                # Rozglos zresetowany stan do UI (zeby front nie zostal ze starym)
                await self.hub.broadcast({"type": "auto_qso_status",
                                           "state": self._qso_engine.state,
                                           "partner": None})
                return
            asyncio.create_task(self._ft8_tx_sequence(call_to, call_de, report, r_flag))

        elif t == "ft8_abort":
            # Awaryjne przerwanie nadawania FT8 (np. drugie klikniecie przycisku)
            self._ft8_tx_abort = True
            self._stop_cq_calling()  # zatrzymaj tez cykliczne CQ

        elif t == "ft8_tx_stop":
            # Zatrzymanie TX z frontendu (przycisk STOP, wygasniecie timera
            # bezpieczenstwa). Zatrzymuje cykliczne CQ i biezaca transmisje.
            print("[ft8] ft8_tx_stop - zatrzymuje TX i CQ")
            self._ft8_tx_abort = True
            self._stop_cq_calling()
            try:
                if not self.rig.sim:
                    await self.rig.set_ptt(False)
                self.rig.ptt = False
                await self.hub.broadcast({"type": "ptt", "ptt": False})
            except Exception:
                pass

        elif t == "ft8_tune":
            # Nadaj stały ton 1500Hz przez X sekund dla strojenia ATU/anteny.
            # Uzytkownik moze przerwac wysylajac ft8_tune_stop.
            # Uwaga: cross-band split i radio_lock sprawdzane.
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            cross, band_a, band_b = self._is_split_cross_band()
            if cross:
                await ws.send_json({"type": "toast",
                    "msg": f"⛔ TUNE zablokowany — split cross-band ({band_a} RX / {band_b} TX)",
                    "level": "error"})
                return
            duration = float(msg.get("duration", 10.0))
            duration = max(1.0, min(30.0, duration))
            tone_hz = float(msg.get("tone", 1500.0))
            asyncio.ensure_future(self._start_tune(duration, tone_hz))

        elif t == "ft8_tune_stop":
            self._tune_stop = True

        elif t == "cw_rx_enable":
            # CW decoder — "each in their own window" model. The engine is
            # global (one model, one task), but multiple operators may open the
            # decoder at once. We track VIEWERS: the task runs while at least one
            # operator has the window open, and stops when the last one closes it
            # or disconnects. This gives everyone the live text, computes it only
            # once, and never burns CPU when nobody is watching.
            _en = bool(msg.get("enabled", True))
            if not hasattr(self, "_cw_viewers"):
                self._cw_viewers = set()
            if _en:
                self._cw_viewers.add(ws)
            else:
                self._cw_viewers.discard(ws)
            _want = len(self._cw_viewers) > 0
            if self.audio:
                self.audio.cw_rx_enabled = _want
            if _want and not getattr(self, "_cw_task", None):
                self._cw_task = asyncio.create_task(self._cw_decode_loop())
                print(f"[deepcw] dekoder WLACZONY (widzow: {len(self._cw_viewers)})",
                      flush=True)
            elif not _want:
                _t = getattr(self, "_cw_task", None)
                if _t:
                    _t.cancel()
                    self._cw_task = None
                if deepcw_engine is not None:
                    deepcw_engine.reset()
                print("[deepcw] dekoder wylaczony (brak widzow)", flush=True)
            # If a new viewer joined an already-running decoder, send them the
            # current state so their window opens in sync (no rozjazd).
            if _en and _want:
                try:
                    await ws.send_str(json.dumps({"type": "cw_rx_state",
                                                  "enabled": True}))
                except Exception:
                    pass

        elif t == "ft8_rx_enable":
            enabled = bool(msg.get("enabled", True))
            mode = self._ft8_decode_mode  # "FT8" lub "FT4"
            # Zapisz kto wlacza (do auto-stop przy disconnect / oddaniu radia).
            # uid pobieramy z online_users bo _ws_msg nie ma go jako parametr.
            sender_uid = self.online_users.get(ws, {}).get("user_id", "")
            if enabled:
                self._ft8_rx_owner_uid = sender_uid
                print(f"[ft8rx] WLACZONE ({mode}) przez uid={sender_uid}", flush=True)
            else:
                self._ft8_rx_owner_uid = None
                print(f"[ft8rx] wylaczone ({mode})", flush=True)
            self._ft8_rx_enabled = enabled
            # Przekaz do Rust — ham_audio.exe uruchamia/zatrzymuje decode loop
            if self.rust_audio:
                await self.rust_audio.ft8_enable_rx(enabled, mode)
            await self.hub.broadcast({"type": "ft8_rx_status", "enabled": enabled})
            await self.hub.broadcast({"type": "wsjtx_status", "running": enabled,
                                       "decoding": enabled, "transmit": False})

        elif t == "ft8_set_tx_freq":
            # Ustawienie docelowej czestotliwosci TX (np. przeciagniecie znacznika
            # TX na wodospadzie). Respektuje zamrozenie (freeze) i tryb split
            # (min. czestotliwosc).
            if self._ft8_tx_frozen:
                # Zamrozone — ignoruj prosby o zmiane, odpowiedz aktualnym stanem
                await ws.send_json({"type": "ft8_tx_freq", "freqHz": self._ft8_tx_freq_hz,
                                     "frozen": True})
                return
            freq = msg.get("freqHz")
            try:
                freq = float(freq)
            except (TypeError, ValueError):
                return
            if self._ft8_split_enabled and freq < self._ft8_split_min_hz:
                freq = self._ft8_split_min_hz
            freq = max(200.0, min(2900.0, freq))
            self._ft8_tx_freq_hz = freq
            await self.hub.broadcast({"type": "ft8_tx_freq", "freqHz": freq,
                                       "frozen": self._ft8_tx_frozen})

        elif t == "ft8_set_rx_freq":
            # Ustawienie znacznika RX (Rx Frequency panel) — calkowicie niezalezne
            # od TX, bez logiki split/lock (mozna nasluchiwac gdziekolwiek w pasmie).
            freq = msg.get("freqHz")
            try:
                freq = float(freq)
            except (TypeError, ValueError):
                return
            freq = max(200.0, min(2900.0, freq))
            self._ft8_rx_freq_hz = freq
            await self.hub.broadcast({"type": "ft8_rx_freq", "freqHz": freq})

        elif t == "ft8_set_both_freq":
            # Lewy klik na wodospadzie (poza juz istniejacymi znacznikami) ustawia
            # OBA znaczniki (RX i TX) naraz na ta sama, nowa pozycje. Kazdy z nich
            # mozna potem przeciagnac osobno (ft8_set_tx_freq / ft8_set_rx_freq).
            freq = msg.get("freqHz")
            try:
                freq = float(freq)
            except (TypeError, ValueError):
                return
            freq = max(200.0, min(2900.0, freq))
            self._ft8_rx_freq_hz = freq
            if not self._ft8_tx_frozen:
                tx_freq = freq
                if self._ft8_split_enabled and tx_freq < self._ft8_split_min_hz:
                    tx_freq = self._ft8_split_min_hz
                self._ft8_tx_freq_hz = tx_freq
            await self.hub.broadcast({"type": "ft8_rx_freq", "freqHz": self._ft8_rx_freq_hz})
            await self.hub.broadcast({"type": "ft8_tx_freq", "freqHz": self._ft8_tx_freq_hz,
                                       "frozen": self._ft8_tx_frozen})

        elif t == "ft8_rx_eq_tx":
            # Przycisk "RX=TX": przesuwa znacznik RX na biezaca pozycje TX.
            self._ft8_rx_freq_hz = self._ft8_tx_freq_hz
            print(f"[ft8] RX=TX -> {self._ft8_rx_freq_hz:.0f}Hz")
            await self.hub.broadcast({"type": "ft8_rx_freq", "freqHz": self._ft8_rx_freq_hz})

        elif t == "ft8_toggle_tx_freeze":
            self._ft8_tx_frozen = bool(msg.get("frozen", not self._ft8_tx_frozen))
            print(f"[ft8] TX {'ZAMROZONE' if self._ft8_tx_frozen else 'odmrozone'} @ {self._ft8_tx_freq_hz:.0f}Hz"
                  + (" — RX bedzie automatycznie podazac za wywolaniami do nas" if self._ft8_tx_frozen else ""))
            await self.hub.broadcast({"type": "ft8_tx_freq", "freqHz": self._ft8_tx_freq_hz,
                                       "frozen": self._ft8_tx_frozen})

        elif t == "ft8_toggle_split":
            self._ft8_split_enabled = bool(msg.get("enabled", not self._ft8_split_enabled))
            min_hz = msg.get("minHz")
            if min_hz is not None:
                try:
                    self._ft8_split_min_hz = max(200.0, min(2900.0, float(min_hz)))
                except (TypeError, ValueError):
                    pass
            # Jesli split wlaczony i obecna czestotliwosc TX jest ponizej progu, podnies ja
            if self._ft8_split_enabled and self._ft8_tx_freq_hz < self._ft8_split_min_hz:
                self._ft8_tx_freq_hz = self._ft8_split_min_hz
            print(f"[ft8] SPLIT {'wlaczony' if self._ft8_split_enabled else 'wylaczony'} (min={self._ft8_split_min_hz:.0f}Hz)")
            await self.hub.broadcast({"type": "ft8_split_status",
                                       "enabled": self._ft8_split_enabled,
                                       "minHz": self._ft8_split_min_hz})
            await self.hub.broadcast({"type": "ft8_tx_freq", "freqHz": self._ft8_tx_freq_hz,
                                       "frozen": self._ft8_tx_frozen})

        # ── Automatyka QSO (pelna automatyka w stylu WSJT-X) ──────────────────
        elif t == "ft8_toggle_auto_seq":
            # UWAGA: automatyka jest ZAWSZE aktywna od kiedy usunelismy toggle
            # z UI. Ta wiadomosc dostajemy tylko dla kompatybilnosci JS/WS
            # handshake — ignorujemy zawartosc 'enabled' i zawsze zwracamy true.
            self._auto_seq_enabled = True
            await self.hub.broadcast({"type": "auto_seq_status",
                                       "enabled": True,
                                       "call1st": self._auto_call_1st,
                                       "state": self._qso_engine.state,
                                       "partner": self._qso_engine.partner_call,
                                       "queue": list(self._qso_engine.queue)})

        elif t == "ft8_toggle_call_1st":
            self._auto_call_1st = bool(msg.get("enabled", not self._auto_call_1st))
            print(f"[autoqso] Call 1st {'WLACZONE' if self._auto_call_1st else 'wylaczone'}")
            await self.hub.broadcast({"type": "auto_seq_status",
                                       "enabled": self._auto_seq_enabled,
                                       "call1st": self._auto_call_1st,
                                       "state": self._qso_engine.state,
                                       "partner": self._qso_engine.partner_call,
                                       "queue": list(self._qso_engine.queue)})

        elif t == "ft8_start_auto_qso":
            call_de = (msg.get("callDe") or "").strip().upper()
            if not call_de or call_de == "CQ":
                return
            if not self._auto_seq_enabled:
                await ws.send_json({"type": "auto_qso_error",
                                     "error": "Wlacz najpierw Auto-Sequencing"})
                return
            # Aktualizuj callsign/grid silnika QSO na aktualnie zalogowanego usera
            user_info = self.online_users.get(ws, {})
            user_call = user_info.get("callsign", "").strip().upper()
            user_uid  = user_info.get("user_id")
            # Zapamietaj uid operatora - potrzebny do AUTO-ZAPISU QSO do jego
            # dziennika przy zakonczeniu (qso_complete).
            self._autoqso_uid = user_uid
            if user_call:
                u_obj = self.find_user_by_id(user_uid) or {}
                user_grid = (u_obj.get("locator") or LOCATOR).strip().upper()[:4]
                if self._qso_engine.my_call != user_call or self._qso_engine.my_grid != user_grid:
                    self._qso_engine.my_call = user_call
                    self._qso_engine.my_grid = user_grid
                    print(f"[autoqso] Uzytkownik: {user_call} / {user_grid}")
            print(f"[autoqso] Reczny start automatycznego QSO z {call_de}")
            # Ignoruj jezeli TX juz jest zaplanowany/trwa dla tego partnera
            if (self._ft8_tx_lock.locked() and
                    self._qso_engine.partner_call == call_de):
                print(f"[autoqso] TX juz trwa dla {call_de} — ignoruje duplikat")
                return
            # Przerwij poprzedni TX jesli to inna stacja
            if self._ft8_tx_lock.locked():
                self._ft8_tx_abort = True
            # Jesli front przekazal TRESC dekodowania (stacja odpowiada nam),
            # sparsuj ja i przekaz jako initial_decode - wtedy engine przeskoczy
            # do wlasciwego kroku (raport) zamiast wysylac Tx1/grid od nowa.
            # To naprawia: "wolam stacje, odpowiada pozniej, klikam RX, ale
            # nadaje sie grid zamiast raportu".
            _msg = (msg.get("message") or "").strip()
            _initial = None
            if _msg:
                try:
                    _initial = qso_engine.parse_message(_msg)
                except Exception as _e:
                    print(f"[autoqso] nie moge sparsowac '{_msg}': {_e}")
                    _initial = None
            self._qso_engine.start_qso(call_de, initial_decode=_initial)
            await self.hub.broadcast({"type": "auto_qso_status",
                                       "state": self._qso_engine.state,
                                       "partner": self._qso_engine.partner_call})
            asyncio.create_task(self._send_auto_tx(self._qso_engine.next_tx_action()))

        elif t == "ft8_queue_remove":
            # Usun stacje z kolejki "Call 1st" (przycisk ✕ na chipie w UI).
            _qcall = (msg.get("call") or "").strip().upper()
            if _qcall and self._qso_engine.remove_from_queue(_qcall):
                print(f"[autoqso] Usunieto {_qcall} z kolejki (reczne ✕)")
                await self.hub.broadcast({"type": "auto_qso_queue",
                                           "queue": list(self._qso_engine.queue)})

        elif t == "ft8_queue_clear":
            # Oproznia cala kolejke "Call 1st" (przycisk "wyczysc" w UI).
            _n = len(self._qso_engine.queue)
            self._qso_engine.clear_queue()
            print(f"[autoqso] Wyczyszczono kolejke ({_n} stacji)")
            await self.hub.broadcast({"type": "auto_qso_queue",
                                       "queue": list(self._qso_engine.queue)})

        elif t == "ft8_abort_auto_qso":
            # Reczny "skip" — operator nie chce czekac na 60s stall-timeout
            # (patrz is_stalled w qso_engine.py), tylko od razu porzuca
            # biezaca stacje i przechodzi do nastepnej z kolejki Call 1st.
            print(f"[autoqso] Reczne przerwanie QSO z {self._qso_engine.partner_call}")
            self._qso_engine.abort_qso()
            self._ft8_tx_abort = True
            # _qso_period_locked=False NIE ustawia zlego okresu na sztywno —
            # okres dla nastepnej stacji i tak zostanie na nowo wykryty z jej
            # PIERWSZEJ realnej odpowiedzi (_dispatch_auto_reply przekazuje
            # swiezy partner_decode do _send_auto_tx), wiec cala kolejna
            # lacznosc trafi w prawidlowe okna, a nie w przypadkowo
            # "zamrozony" okres po poprzedniej stacji.
            self._qso_period_locked = False
            await self.hub.broadcast({"type": "auto_qso_status",
                                       "state": self._qso_engine.state,
                                       "partner": None})
            await self._advance_auto_qso_queue()

        elif t == "ft8_set_tx_period":
            period = int(msg.get("period", 1))
            if period not in (1, 2):
                return
            self._ft8_tx_period = period
            print(f"[ft8] Okres nadawania ustawiony na: {period}")
            await self.hub.broadcast({"type": "ft8_tx_period", "period": period})

        elif t == "ft8_toggle_fake_split":
            # Wlacz/wylacz Fake Split (patrz _apply_fake_split_before_tx).
            # Stan zapamietany w configu — przetrwa restart serwera.
            self._fake_split_enabled = bool(msg.get("enabled", not self._fake_split_enabled))
            self.cfg.setdefault("ft8", {})["fakeSplit"] = self._fake_split_enabled
            save_json(CFG_F, self.cfg)
            print(f"[ft8] Fake Split {'WLACZONY' if self._fake_split_enabled else 'wylaczony'}")
            await self.hub.broadcast({"type": "ft8_fake_split_status",
                                       "enabled": self._fake_split_enabled,
                                       "targetHz": 1500})

        elif t == "ft8_set_decode_mode":
            mode = msg.get("mode", "FT8")
            if mode not in ("FT8", "FT4"):
                return
            self._ft8_decode_mode = mode
            print(f"[ft8] Tryb dekodowania ustawiony na: {mode}")
            # Przekaz do Rust — zmien tryb dekodowania
            if self.rust_audio:
                await self.rust_audio.ft8_enable_rx(self._ft8_rx_enabled, mode)
            await self.hub.broadcast({"type": "ft8_decode_mode", "mode": mode})

        # ── WebRTC sygnalizacja (offer/answer/ICE) ────────────────────────────
        elif t == "webrtc_offer":
            if not self.webrtc:
                await ws.send_json({"type": "webrtc_error", "error": "WebRTC niedostepne na serwerze"})
                return
            # Uruchom TX playback (jak audio_tx_start) zanim podlaczymy track
            if not self.audio.tx_active:
                dev = self.cfg.get("audio", {}).get("txDevice")
                self.audio.start_tx(device=dev)
                print(f"[audio] TX start (WebRTC) dev={dev}")
            answer = await self.webrtc.handle_offer(msg.get("sdp"), msg.get("sdpType","offer"))
            await ws.send_json({"type": "webrtc_answer", "sdp": answer["sdp"], "sdpType": answer["type"]})

        elif t == "webrtc_ice":
            if self.webrtc:
                await self.webrtc.add_ice_candidate(msg.get("candidate", {}))

        elif t == "webrtc_stop":
            if self.webrtc:
                await self.webrtc.close()
            if self.audio.tx_active:
                self.audio.stop_tx()

        # ── Dynamiczne akcje/slidery (z dump_caps: VFO A/B, funkcje, levels) ──
        elif t == "rig_action":
            await self._handle_rig_action(msg, ws, role)

        elif t == "rig_slider":
            await self._handle_rig_slider(msg, ws, role)

    async def _dynamic_allowed(self, dynamic_id: str, role: str, kind: str) -> bool:
        """Sprawdz whitelist dla dynamicznego elementu (action/slider) po id.
        Admin zawsze dozwolony. Domyslnie wlaczone (enabled_dynamic.get(id, True))."""
        if role == "admin":
            return True
        # KEYSPD (WPM) dozwolone dla wszystkich uzytkownikow
        if dynamic_id == "level_keyspd":
            return True
        rig_id = self._current_rig_id()
        enabled_dyn = self._get_enabled_dynamic(rig_id)
        if not enabled_dyn.get(dynamic_id, True):
            return False
        # Sprawdz ze element rzeczywiscie istnieje w aktualnych capabilities
        caps = getattr(self, "_caps_cache", None) or {"actions": [], "sliders": []}
        items = caps.get("actions" if kind == "action" else "sliders", [])
        return any(i["id"] == dynamic_id for i in items)

    async def _cq_calling_loop(self):
        """
        Petla cyklicznego wolania CQ. Nadaje CQ, czeka do konca pelnego okresu
        (2 okna 15s = 30s dla FT8), sprawdza czy wciaz wolamy, ponawia.

        Zatrzymuje sie gdy:
          - self._cq_calling = False (user stop / timer / ktos odpowiedzial)
          - engine wszedl w aktywne QSO (ktos odpowiedzial na nasz CQ)
          - PTT zajete przez cos innego / abort

        Odpowiedz na nasz CQ jest wykrywana w _process_auto_qso (on_decode) —
        gdy stacja odpowie, wywolujemy _stop_cq_calling i engine przejmuje QSO.
        """
        # STABILNOSC: try jest WEWNATRZ while - pojedynczy blad (np. radio
        # chwilowo nie odpowiada) nie zabija petli. Licznik bledow pod rzad
        # chroni przed kreceniem sie w kolko przy trwalej awarii.
        _consecutive_errors = 0
        _MAX_ERRORS = 5
        try:
            while self._cq_calling:
                try:
                    # Jesli engine ma aktywne QSO - ktos odpowiedzial, przestan wolac
                    if self._qso_engine.is_active():
                        print("[cq] QSO aktywne - zatrzymuje wolanie CQ")
                        break
                    call_de = self._cq_call_de
                    report = self._cq_report
                    if not call_de:
                        break
                    # Zapamietaj tresc CQ (do rozpoznania odpowiedzi w on_decode)
                    self._auto_cq_text = f"CQ {call_de} {report}".strip()
                    # Nadaj jeden CQ (przez wspolna sciezke - respektuje okno/PTT/lock)
                    await self._ft8_tx_sequence("CQ", call_de, report, False)
                    _consecutive_errors = 0  # sukces - zeruj licznik
                    # _ft8_tx_sequence konczy sie pod koniec okresu. Sprawdz czy
                    # wciaz wolamy (mogl przyjsc stop/odpowiedz w trakcie).
                    if not self._cq_calling:
                        break
                    if self._qso_engine.is_active():
                        print("[cq] QSO aktywne po CQ - koniec wolania")
                        break
                    # Krotka pauza zeby nie wejsc od razu w to samo okno.
                    await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    raise  # anulowanie przepuszczamy wyzej
                except Exception as e:
                    _consecutive_errors += 1
                    print(f"[cq] blad cyklu CQ ({_consecutive_errors}/{_MAX_ERRORS}): {e}",
                          flush=True)
                    if _consecutive_errors >= _MAX_ERRORS:
                        print("[cq] za duzo bledow pod rzad - zatrzymuje wolanie CQ",
                              flush=True)
                        try:
                            await self.hub.broadcast({
                                "type": "toast",
                                "msg": "⛔ CQ zatrzymane - radio nie odpowiada",
                                "level": "error"})
                        except Exception:
                            pass
                        break
                    # Backoff przed kolejna proba
                    await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            print("[cq] petla CQ anulowana")
        except Exception as e:
            print(f"[cq] blad petli CQ: {e}")
        finally:
            self._cq_calling = False
            self._auto_cq_text = None

    def _stop_cq_calling(self):
        """Zatrzymaj cykliczne wolanie CQ (user stop / odpowiedz / timer).
        Ustawia tez _ft8_tx_abort zeby przerwac BIEZACE nadawanie audio, i
        anuluje task petli. Bez _ft8_tx_abort biezacy _ft8_tx_sequence
        doksztalcilby sie do konca mimo zatrzymania CQ."""
        if self._cq_calling:
            print("[cq] Zatrzymuje cykliczne wolanie CQ")
        self._cq_calling = False
        self._auto_cq_text = None
        # Przerwij biezace nadawanie (jesli wlasnie leci CQ)
        self._ft8_tx_abort = True
        if self._cq_task and not self._cq_task.done():
            self._cq_task.cancel()
        self._cq_task = None

    @staticmethod
    def _compute_fake_split(dial_hz: float, desired_audio_hz: float) -> dict:
        """Oblicza Fake Split: o ile przesunac VFO (dial) zeby audio TX bylo
        blisko srodka filtra SSB (~1500Hz) przy zachowanym niezmienniku
        freq_eteru = dial + audio (musi byc identyczna przed i po). Logika
        1:1 z fake_split_prototype.py (tam przetestowana osobno na sucho,
        bez dotykania radia, zanim wpieta zostala tutaj w tor TX).

        Zwraca dict: on_air_hz, split_needed, new_dial_hz, new_audio_hz,
        restore_dial_hz (= dial_hz, na co wrocic po nadaniu)."""
        TARGET_AUDIO_HZ = 1500.0
        AUDIO_MIN_HZ, AUDIO_MAX_HZ = 300.0, 2700.0
        VFO_STEP_HZ = 500.0  # blokowe przesuniecia — radio nie nadaza na ciagle strojenie

        on_air_hz = dial_hz + desired_audio_hz
        if AUDIO_MIN_HZ <= desired_audio_hz <= AUDIO_MAX_HZ and 600.0 <= desired_audio_hz <= 2400.0:
            # Audio juz wystarczajaco daleko od krawedzi filtra — split zbedny.
            return {"on_air_hz": on_air_hz, "split_needed": False,
                    "new_dial_hz": dial_hz, "new_audio_hz": desired_audio_hz,
                    "restore_dial_hz": dial_hz}

        raw_shift = desired_audio_hz - TARGET_AUDIO_HZ
        vfo_shift = round(raw_shift / VFO_STEP_HZ) * VFO_STEP_HZ
        new_dial_hz = dial_hz + vfo_shift
        new_audio_hz = on_air_hz - new_dial_hz  # dopelnienie do niezmiennika
        return {"on_air_hz": on_air_hz, "split_needed": True,
                "new_dial_hz": new_dial_hz, "new_audio_hz": new_audio_hz,
                "restore_dial_hz": dial_hz}

    async def _apply_fake_split_before_tx(self):
        """Jesli Fake Split jest wlaczony, przesuwa VFO PRZED nadawaniem i
        podmienia self._ft8_tx_freq_hz na bezpieczny offset (~1500Hz) na czas
        TEJ transmisji — wolane raz, na samym poczatku _ft8_tx_sequence_inner,
        PRZED sprawdzeniem cache PCM (self._pre_pcm_cache), ktory kasujemy gdy
        split jest potrzebny: pre-gen PCM (patrz _send_auto_tx) zostal policzony
        dla STAREGO audio offsetu sprzed przesuniecia VFO — uzycie go teraz
        wyslaloby dzwiek niezgodny z nowa pozycja VFO i zlamalo niezmiennik
        czestotliwosci w eterze. Stan do przywrocenia trzymany w
        self._fake_split_state (None jesli nic nie trzeba przywracac —
        _restore_fake_split_after_tx() jest wtedy bezpiecznym no-opem)."""
        self._fake_split_state = None
        if not self._fake_split_enabled:
            return False
        original_audio_hz = self._ft8_tx_freq_hz
        split = self._compute_fake_split(self.rig.freq, original_audio_hz)
        if not split["split_needed"]:
            return False
        self._fake_split_state = {"dial_hz": split["restore_dial_hz"],
                                   "audio_hz": original_audio_hz}
        self.rig.freq = split["new_dial_hz"]
        if not self.rig.sim:
            try:
                await self.rig.set_freq(split["new_dial_hz"])
            except Exception as e:
                print(f"[ft8] Fake Split set_freq blad: {e!r}")
                self._fake_split_state = None
                return False
        self._ft8_tx_freq_hz = split["new_audio_hz"]
        self._pre_pcm_cache = None  # unieważnij — policzony dla starego audio offsetu
        print(f"[ft8] Fake Split: VFO {split['restore_dial_hz']:.0f} -> "
              f"{split['new_dial_hz']:.0f}Hz, audio -> {split['new_audio_hz']:.0f}Hz "
              f"(eter bez zmian: {split['on_air_hz']:.0f}Hz)")
        await self.hub.broadcast({"type": "freq", "freq": int(split["new_dial_hz"])})
        await self.hub.broadcast({"type": "ft8_tx_freq", "freqHz": self._ft8_tx_freq_hz,
                                   "frozen": self._ft8_tx_frozen})
        return True

    async def _restore_fake_split_after_tx(self):
        """Przywraca VFO i audio TX offset do stanu sprzed
        _apply_fake_split_before_tx(). Bezpieczny no-op gdy split nie zostal
        zastosowany dla biezacej transmisji (self._fake_split_state is None) —
        wolane bezwarunkowo w finally: po kazdym TX, wiec musi byc odporne na
        wywolanie gdy nie ma czego przywracac."""
        state = self._fake_split_state
        if not state:
            return
        self._fake_split_state = None
        self.rig.freq = state["dial_hz"]
        if not self.rig.sim:
            try:
                await self.rig.set_freq(state["dial_hz"])
            except Exception as e:
                print(f"[ft8] Fake Split restore set_freq blad: {e!r}")
        self._ft8_tx_freq_hz = state["audio_hz"]
        print(f"[ft8] Fake Split: VFO przywrocone -> {state['dial_hz']:.0f}Hz, "
              f"audio -> {state['audio_hz']:.0f}Hz")
        await self.hub.broadcast({"type": "freq", "freq": int(state["dial_hz"])})
        await self.hub.broadcast({"type": "ft8_tx_freq", "freqHz": self._ft8_tx_freq_hz,
                                   "frozen": self._ft8_tx_frozen})

    async def _ft8_tx_sequence(self, call_to: str, call_de: str, report: str, r_flag: bool = False, auto_respond: bool = False):
        """
        Wrapper z mutexem wokol _ft8_tx_sequence_inner — zapobiega rownoleglemu
        wykonaniu dwoch transmisji (np. automatyka + reczny klik, lub dwie
        automatyczne odpowiedzi z tego samego okna RX). Jesli lock jest juz
        zajety, ta wiadomosc CZEKA w kolejce asyncio.Lock (FIFO), zamiast
        kolidowac z trwajacym PTT — co w praktyce oznacza, ze proba wyslania
        w trakcie innej transmisji bedzie po prostu OPOZNIONA do kolejnego
        wolnego okna (bo _ft8_tx_sequence_inner i tak czeka na nastepne okno
        15s, wiec ostatecznie nic nie ginie, tylko ewentualnie przesuwa sie
        o jeden cykl).
        """
        async with self._ft8_tx_lock:
            await self._ft8_tx_sequence_inner(call_to, call_de, report, r_flag, auto_respond=auto_respond)

    async def _ft8_tx_sequence_inner(self, call_to: str, call_de: str, report: str, r_flag: bool = False, auto_respond: bool = False):
        """
        Pelna sekwencja nadawania FT8: enkoduj -> czekaj na okno 15s UTC ->
        PTT ON -> stream audio (kawalki 20ms przez feed_tx_pcm) -> PTT OFF.
        Uruchamiane jako osobny task (asyncio.create_task), nie blokuje WS loopa.
        """
        # Sprawdz blokade radia — tylko aktywny operator lub admin moze TX
        if self.radio_lock["user_id"]:
            # Znajdz sender_uid z aktualnego kontekstu — jesli FT8 nie ma uid, pomijamy
            pass
        # Blokada TX na niedozwolonym pasmie
        if not self._is_band_allowed():
            await self.hub.broadcast({"type": "toast", "msg": "⛔ FT8 TX zablokowany — pasmo niedozwolone przez admina", "level": "error"})
            return
        # Blokada FT8/FT4 w cross-band split (chroni radio)
        cross, band_a, band_b = self._is_split_cross_band()
        if cross:
            await self.hub.broadcast({"type": "toast",
                "msg": f"⛔ FT8/FT4 TX zablokowany — split cross-band ({band_a} RX / {band_b} TX). Wylacz split.",
                "level": "error"})
            return
        # Nie kasuj abort jesli wlasnie zatrzymano cykliczne CQ (stop/timer) —
        # inaczej to nadanie zignorowaloby zadanie zatrzymania i nadawaloby dalej.
        if not (call_to == "CQ" and not self._cq_calling):
            self._ft8_tx_abort = False
        else:
            print("[cq] sequence CQ pominiete - CQ zatrzymane")
            return
        try:
            is_ft4 = (self._ft8_decode_mode == "FT4")
            # SYNCHRONIZACJA txVolume PRZED TX: audio.cfg to referencja ktora moze
            # sie rozjechac z glownym configiem (przelogowanie, reload) -> audio
            # bralo stara/domyslna wartosc zamiast zapisanej -> ALC wysokie mimo
            # ze UI i config pokazuja 2. Tu WYMUSZAMY zeby audio uzylo aktualnego
            # txVolume z config.json tuz przed nadawaniem.
            if "audio" in self.cfg:
                self.audio.cfg = self.cfg["audio"]
                _tv_now = self.cfg["audio"].get("txVolume", 1.0)
                print(f"[audio] txVolume synchronizowany przed TX = {_tv_now}x", flush=True)
            # Utnij grid do 4 znakow (FT8/FT4 nie obsluguje 6-znakowego locatora)
            if len(report) == 6 and report[:2].isalpha() and report[2:4].isdigit():
                report = report[:4]
                print(f"[ft8] Grid uciety do 4 znakow: {report}")
            import time as _tx_time
            # Fake Split PRZED sprawdzeniem cache PCM — jesli przesuwa VFO i
            # zmienia audio offset, kasuje _pre_pcm_cache (patrz docstring),
            # wiec musi wystapic zanim ponizej odczytamy cache.
            await self._apply_fake_split_before_tx()
            # Użyj pre-wygenerowanego PCM jeśli pasuje (call_to/call_de/report)
            cache = self._pre_pcm_cache
            if (cache and cache[0] == call_to and cache[1] == call_de and
                    cache[2] == report):
                # cache: (call_to, call_de, report, pcm, dur, ldpc_valid)
                pcm_bytes = cache[3]
                duration = cache[4]
                _cached_ldpc = cache[5] if len(cache) > 5 else None
                debug = {"ldpc_valid": _cached_ldpc}
                self._pre_pcm_cache = None
                print(f"[ft8] PCM z cache ({duration:.2f}s, ldpc_valid={_cached_ldpc})")
            elif is_ft4:
                pcm_bytes, debug, duration = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: ft4_encoder.generate_tx_pcm48k_ft4(
                        call_to, call_de, report, r_flag, base_freq_hz=self._ft8_tx_freq_hz))
            else:
                pcm_bytes, debug, duration = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: ft8_encoder.generate_tx_pcm48k(
                        call_to, call_de, report, r_flag, base_freq_hz=self._ft8_tx_freq_hz))
            # ZNACZNIK WERSJI - potwierdza ktora wersja kodu jest w EXE.
            # ZMIENIANY przy kazdej istotnej naprawie. Jesli po przebudowie EXE
            # widzisz STARY znacznik = PyInstaller spakowal zly webapp.py.
            print(f"[build] webapp.py wersja BUILD-2026-08-10-AUTOQSO-SKIP-BTN, ldpc_valid={debug.get('ldpc_valid')}", flush=True)
            if not debug.get("ldpc_valid"):
                print(f"[{'ft4' if is_ft4 else 'ft8'}] OSTRZEZENIE: ldpc_valid=False dla '{call_to} {call_de} {report}' — wysylam mimo to")

            # Tekst do wyswietlenia w UI (status TX, podswietlenie makra) MUSI
            # zawierac prefix "R" gdy r_flag=True, inaczej front nie odroznia
            # wizualnie "R+raport" (potwierdzenie) od zwyklego pierwszego
            # raportu — to byl zrodlowy blad zgloszony 2026-06-21 (podswietlal
            # sie zly przycisk makra przy odpowiedziach typu "R-18").
            display_report = f"R{report}" if r_flag else report
            display_text = f"{call_to} {call_de} {display_report}"

            # Upewnij sie ze audio TX playback dziala (jak w audio_tx_start) —
            # robimy to PRZED oczekiwaniem na okno, zeby nie tracic czasu w
            # krytycznym momencie startu transmisji.
            if not self.audio.tx_active:
                dev = self.cfg.get("audio", {}).get("txDevice")
                ok = self.audio.start_tx(device=dev)
                print(f"[ft8] audio start_tx: {'OK' if ok else 'BLAD'} dev={dev}")
                if not ok:
                    await self.hub.broadcast({"type": "ft8_tx_status", "status": "error",
                                               "error": "Nie udalo sie uruchomic audio TX"})
                    return

            # ── Synchronizacja do wlasciwego okna UTC wg wybranego okresu
            # nadawania i trybu (FT8: 15s xx:00/30 lub xx:15/45; FT4: 7.5s) ──
            window_s = ft4_encoder.FT4_SLOT_TIME if is_ft4 else 15.0
            import time as _time
            now = _time.time()
            pos_in_window = now % window_s

            # Wyznacz poczatek kolejnego okna naszego okresu (1 lub 2)
            # Okres 1: okna 0, 2, 4... (okna parzyste) — xx:00/30 dla FT8
            # Okres 2: okna 1, 3, 5... (okna nieparzyste) — xx:15/45 dla FT8
            if not auto_respond:
                # Reczne TX — czekaj na poczatek wlasciwego okresu
                full_period_s = window_s * 2
                offset = 0.0 if self._ft8_tx_period == 1 else window_s
                # Czas od początku cyklu 2-okienkowego
                pos_in_full = now % full_period_s
                if pos_in_full < offset:
                    wait_s = offset - pos_in_full
                elif pos_in_full < offset + window_s:
                    # Jestesmy w naszym oknie
                    remaining = offset + window_s - pos_in_full
                    if remaining < 1.5:
                        # Za blisko konca — czekaj na nastepne okno
                        wait_s = remaining + full_period_s - window_s
                    elif pos_in_window > 0.5:
                        # Za pozno zeby nadac na czas — czekaj na nastepne
                        wait_s = remaining + full_period_s - window_s
                    else:
                        wait_s = 0.0  # Nadaj teraz
                else:
                    wait_s = full_period_s - pos_in_full + offset
                print(f"[ft8] Reczne TX period={self._ft8_tx_period}, pozycja={pos_in_window:.1f}s, czekam {wait_s:.2f}s")
            else:
                # Auto TX — dekod partnera przychodzi NATURALNIE pod koniec
                # JEGO okna (dekoder potrzebuje calego okna), wiec najblizsza
                # granica okna = poczatek NASZEGO okna odpowiedzi.
                #  - pozycja <= prog: auto_seq zdazyl na poczatek naszego okna
                #    -> nadaj natychmiast (DT ~0)
                #  - pozycja > prog: koniec okna partnera -> czekaj do
                #    najblizszej granicy (window - pos) i nadaj w naszym oknie.
                # UWAGA: wczesniejsza wersja dodawala tu +window_s ("nastepne
                # nasze okno") - blad: odpowiadalismy OKNO ZA POZNO, partner
                # nie widzial odpowiedzi, powtarzal, sekwencja QSO sie
                # rozjezdzala ("automat zglupial", RRR w kolko).
                _max_start = 1.5 if window_s >= 15.0 else 1.0  # FT8 / FT4
                if pos_in_window <= _max_start:
                    wait_s = 0.0
                    print(f"[ft8] Auto TX natychmiast (pozycja {pos_in_window:.1f}s w oknie {window_s}s)")
                else:
                    wait_s = window_s - pos_in_window
                    print(f"[ft8] Auto TX na granicy okna (pozycja {pos_in_window:.1f}s) "
                          f"— czekam {wait_s:.1f}s do POCZATKU naszego okna")
            await self.hub.broadcast({"type": "ft8_tx_status", "status": "waiting",
                                       "text": display_text,
                                       "waitSeconds": round(wait_s, 2)})
            print(f"[ft8] Czekam {wait_s:.2f}s na okno 15s UTC...")
            # Spij w krotkich kawalkach, zeby abort dzialal tez w trakcie oczekiwania
            waited = 0.0
            while waited < wait_s:
                if self._ft8_tx_abort:
                    print("[ft8] TX przerwane (abort) podczas oczekiwania na okno")
                    await self.hub.broadcast({"type": "ft8_tx_status", "status": "done"})
                    return
                step = min(0.1, wait_s - waited)
                await asyncio.sleep(step)
                waited += step

            await self.hub.broadcast({"type": "ft8_tx_status", "status": "starting",
                                       "text": display_text})

            if self._ft8_tx_abort:
                print("[ft8] TX anulowane przed PTT (abort)")
                await self.hub.broadcast({"type": "ft8_tx_status", "status": "done"})
                return

            import time as _ttt
            _t0 = _ttt.time()
            _pos = _t0 % (ft4_encoder.FT4_SLOT_TIME if is_ft4 else 15.0)
            print(f"[ft8] przed PTT: pozycja w oknie={_pos:.3f}s")
            await self.rig.set_ptt(True)
            print(f"[ft8] PTT ON zajelo: {(_ttt.time()-_t0)*1000:.0f}ms")
            await self.hub.broadcast({"type": "ptt", "ptt": True})
            print(f"[ft8] TX START: '{call_to} {call_de} {report}' ({duration:.2f}s) DT={_pos:.2f}s")

            # Wlasna transmisja jako wpis 'wsjtx_decode' (is_tx=True) — bez tego
            # okno RX (Band Activity) nigdy nie widzialo NASZYCH wiadomosci
            # przeplecionych z odebranymi w kolejnosci czasowej podczas QSO,
            # tylko odbierane dekody z Rust (ktore nie wiedza nic o naszym TX).
            await self.hub.broadcast({
                "type": "wsjtx_decode",
                "timeStr": _ttt.strftime("%H%M%S", _ttt.gmtime(_t0)),
                "snr": 0, "deltaTime": 0.0,
                "deltaFreq": int(self._ft8_tx_freq_hz),
                "message": display_text,
                "call_to": call_to, "call_de": call_de,
                "report_or_grid": display_report,
                "mode": self._ft8_decode_mode,
                "is_tx": True,
            })

            # DIAGNOSTYKA TX: sprawdz stan audio przed wyslaniem
            print(f"[ft8] TX audio state: tx_active={self.audio.tx_active} "
                  f"tx_stream={self.audio._tx_stream is not None} "
                  f"pcm_bytes_len={len(pcm_bytes)} "
                  f"chunks={len(pcm_bytes)//1920} "
                  f"txVolume={self.audio.cfg.get('txVolume', 4.0) if hasattr(self.audio, 'cfg') else '?'} "
                  f"txDevice={self.cfg.get('audio', {}).get('txDevice', '?')}")

            # Wysylamy WSZYSTKIE kawałki naraz do kolejki PCM.
            # bulk_tx=True wylacza anty-lag drop w petli TX - bez tego drop
            # wyrzucal 239/248 ramek ("zaleglosc") i z 4.94s sygnalu zostawalo
            # 160ms bzyku -> moc/ALC zero, FT8 nie wychodzil w eter.
            self.audio.bulk_tx = True
            chunks_sent = 0
            for chunk in ft8_encoder.chunk_pcm_bytes(pcm_bytes, chunk_samples=960):
                if self._ft8_tx_abort:
                    print("[ft8] TX przerwane (abort) — czyszcze kolejke PCM")
                    # Wyczysc kolejke PCM zeby nie kontynuowalo nadawania
                    try:
                        while not self.audio._webrtc_pcm_queue.empty():
                            self.audio._webrtc_pcm_queue.get_nowait()
                    except Exception:
                        pass
                    break
                self.audio.feed_tx_pcm(chunk)
                chunks_sent += 1
            print(f"[ft8] wyslano {chunks_sent} kawalkow PCM do kolejki "
                  f"(queue_size={self.audio._webrtc_pcm_queue.qsize()})")

            # Czekaj az PCM sie odtworzy (czas trwania + margines)
            elapsed = 0.0
            while elapsed < duration + 0.3:
                if self._ft8_tx_abort:
                    print("[ft8] TX przerwane podczas odtwarzania")
                    try:
                        while not self.audio._webrtc_pcm_queue.empty():
                            self.audio._webrtc_pcm_queue.get_nowait()
                    except Exception:
                        pass
                    break
                await asyncio.sleep(0.1)
                elapsed += 0.1

        except Exception as e:
            print(f"[ft8] BLAD podczas nadawania: {e}")
            await self.hub.broadcast({"type": "ft8_tx_status", "status": "error", "error": str(e)})
        finally:
            self.audio.bulk_tx = False  # przywroc anty-lag dla fonii WebRTC
            try:
                await self.rig.set_ptt(False)
                await self.hub.broadcast({"type": "ptt", "ptt": False})
            except Exception as e:
                print(f"[ft8] BLAD przy wylaczaniu PTT: {e}")
            # Fake Split: VFO wraca na miejsce zaraz po PTT OFF (no-op jesli
            # split nie zostal zastosowany dla tej transmisji).
            await self._restore_fake_split_after_tx()
            await self.hub.broadcast({"type": "ft8_tx_status", "status": "done"})
            print("[ft8] TX KONIEC")
            # Trzymaj mutex do konca biezacego okresu (PTT juz OFF)
            # zeby kolejny task nie wszedl w okres korespondenta
            import time as _time
            _window_s = ft4_encoder.FT4_SLOT_TIME if self._ft8_decode_mode == "FT4" else 15.0
            _remaining = _window_s - (_time.time() % _window_s)
            if 0.5 < _remaining < _window_s - 0.5:
                print(f"[ft8] Czekam {_remaining:.1f}s do konca okresu (PTT OFF)")
                await asyncio.sleep(_remaining)

    @staticmethod
    def _format_report(snr_db: float) -> str:
        """Formatuje zmierzony SNR (dB) jako standardowy raport FT8 z
        wymuszonym znakiem, np. -12, +05. Zakres protokolu FT8 to -30..+49 —
        przycinamy (clamp) ekstremalne pomiary do tego zakresu, bo encoder
        rzuca ValueError poza nim."""
        snr_int = max(-30, min(49, int(round(snr_db))))
        return f"{snr_int:+03d}"

    async def _process_tx_freeze_rx_follow(self, m: dict):
        """
        Automatyczne podazanie RX/TX prazkow za stacja z ktora prowadzimy QSO.

        WSJT-X standard: jesli klikniesz CQ na freq X, wywolujesz na X, i
        stacja odpowiada rowniez z X, TX/RX zostaja na X. Ale jesli stacja
        przesunie sie na inna freq w kolejnej wiadomosci (bo znalazla mniej
        zaklocone miejsce), TX/RX powinny za nia podazac zeby QSO nie
        zerwalo sie.

        Logika:
          - Wiadomosc jest ADRESOWANA DO NAS (call_to == CALLSIGN)? Tak/nie
          - Nowa freq rozni sie od aktualnej? Tak/nie
          - RX ZAWSZE przesuwane na nowa freq (RX freeze nie istnieje)
          - TX przesuwane TYLKO jesli nie ma "Hold TX" (self._ft8_tx_frozen)
            - Dlatego wczesniejsza nazwa metody _process_tx_freeze_rx_follow
              odnosi sie do specyficznego przypadku - teraz robimy szerzej.

        Wywolywane dla KAZDEGO odebranego dekodowania (w _ft8_rx_loop),
        niezaleznie od tego czy auto_seq_enabled. Bo to ulatwienie namierzania
        stacji dziala nawet przy calkowicie recznej obsludze.
        """
        try:
            parsed = qso_engine.parse_message(m["message"])
            if parsed is None or parsed.get("call_to") != CALLSIGN:
                return
            new_freq = float(m.get("freq_hz", m.get("deltaFreq", self._ft8_rx_freq_hz)))

            # RX zawsze podaza za stacja ktora do nas nadaje
            if abs(new_freq - self._ft8_rx_freq_hz) >= 1.0:
                self._ft8_rx_freq_hz = new_freq
                await self.hub.broadcast({"type": "ft8_rx_freq", "freqHz": self._ft8_rx_freq_hz})

            # TX podaza tylko jesli nie ma Hold TX (uzytkownik nie zamrozil TX)
            if not self._ft8_tx_frozen:
                if abs(new_freq - self._ft8_tx_freq_hz) >= 1.0:
                    self._ft8_tx_freq_hz = new_freq
                    await self.hub.broadcast({"type": "ft8_tx_freq",
                                               "freqHz": self._ft8_tx_freq_hz,
                                               "frozen": False})
        except Exception as e:
            print(f"[ft8] BLAD auto-follow: {e}")

    async def _advance_auto_qso_queue(self):
        """Po zakonczeniu lub porzuceniu QSO: jesli Call 1st jest wlaczone
        i kolejka nie jest pusta, startuje QSO z nastepna stacja. Wywolujacy
        MUSI wczesniej ustawic silnik z powrotem w IDLE (abort_qso())."""
        if self._auto_call_1st and self._qso_engine.queue:
            next_call = self._qso_engine.pop_next_from_queue()
            print(f"[autoqso] Nastepna stacja z kolejki: {next_call}")
            # UWAGA: tu NIE mamy initial_decode (ta stacja odpowiedziala
            # wczesniej, nie w tym samym cyklu) — startujemy normalnie
            # od naszego Tx1 (grid), bo nie wiemy czy jej wczesniejsza
            # wiadomosc nadal jest aktualna (mogla zmienic czestotliwosc/zniknac).
            self._qso_engine.start_qso(next_call)
            await self.hub.broadcast({"type": "auto_qso_status",
                                       "state": self._qso_engine.state,
                                       "partner": next_call})
            asyncio.create_task(self._send_auto_tx(self._qso_engine.next_tx_action()))

    async def _process_auto_qso(self, m: dict):
        """
        Przetwarza POJEDYNCZE zdekodowane FT8 (m, z decode_window) przez
        silnik automatyki QSO. Wywolywane TYLKO gdy self._auto_seq_enabled.

        Krok po kroku:
          1. parse_message() -> jesli None (nierozpoznany format), nic nie rob.
          2. engine.on_decode(parsed) -> dict akcji lub None.
          3. Jesli akcja to 'reply' z needs_measured_report=True, podstaw
             realny zmierzony SNR (m['snr_db']) jako report_or_grid.
          4. Zaplanuj wyslanie (asyncio.create_task na _ft8_tx_sequence) —
             ta sama, sprawdzona sciezka co reczne TX (czeka na okno 15s).
          5. Jesli akcja to 'enqueue' i jestesmy w stanie IDLE i auto_call_1st
             jest wlaczone -> natychmiast start_qso z ta stacja (przekazujac
             parsed jako initial_decode, zeby poprawnie pominac naszego Tx1
             gdy partner juz przeslal grid/raport razem z odpowiedzia).
          6. Jesli qso_complete=True w akcji -> zaplanuj zalogowanie QSO PO
             wyslaniu naszej koncowej wiadomosci (nie przed — partner musi
             dostac potwierdzenie), i sprawdz kolejke na nastepna stacje.
        """
        try:
            parsed = qso_engine.parse_message(m["message"])
            # UWAGA (perf): usunieto print per-decode — logował KAZDE dekodowanie
            # FT8/FT4 (20-40/sekunde przy aktywnym pasmie), kazdy print to
            # blokujacy syscall zapychajacy event loop.
            # Odkomentuj przy debugowaniu autoQSO:
            # print(f"[autoqso] DECODE: msg={m['message']!r} mode={m.get('mode')} parsed={parsed} engine_state={self._qso_engine.state} partner={self._qso_engine.partner_call}")
            if parsed is None:
                return
            # Ignoruj wlasny sygnal (echo z USB audio) — call_de to MY
            if parsed.get('call_de', '').upper() == self._qso_engine.my_call.upper():
                return

            result = self._qso_engine.on_decode(parsed)
            if result is None:
                # Widocznosc w UI: on_decode() mogl cicho dopisac stacje do
                # kolejki Call 1st (bo jestesmy w trakcie innego QSO, wiec
                # zwrocil None zamiast akcji 'enqueue') — bez tego broadcastu
                # operator nie widzial ze cokolwiek sie stalo, dopoki biezace
                # QSO sie nie zakonczylo.
                if parsed.get('call_to') == self._qso_engine.my_call:
                    await self.hub.broadcast({"type": "auto_qso_queue",
                                               "queue": list(self._qso_engine.queue),
                                               "active": self._qso_engine.partner_call})
                # Partner utkniętego QSO przestal odpowiadac (QSB, zaczal inne
                # QSO, wylaczyl automat) — bez tej kontroli silnik zostawal w
                # stanie aktywnym W NIESKONCZONOSC, blokujac Call 1st dla
                # kolejnych stacji z kolejki (byly tylko cicho dopisywane,
                # nigdy nie startowane — objaw "automat nie reaguje na
                # kolejna stacje").
                if self._qso_engine.is_stalled(60.0):
                    print(f"[autoqso] {self._qso_engine.partner_call} nie "
                          f"odpowiada >60s — porzucam QSO")
                    self._qso_engine.abort_qso()
                    self._qso_period_locked = False
                    await self.hub.broadcast({"type": "auto_qso_status",
                                               "state": "IDLE", "partner": None})
                    await self._advance_auto_qso_queue()
                return

            if result.get("action") == "enqueue":
                call_de = result["call_de"]
                # Ktos odpowiedzial na nasz CQ - zatrzymaj cykliczne wolanie CQ
                # (przechodzimy w QSO z ta stacja).
                if self._cq_calling:
                    print(f"[cq] {call_de} odpowiedzial na CQ - koncze wolanie, zaczynam QSO")
                    self._stop_cq_calling()
                if self._auto_call_1st and not self._qso_engine.is_active():
                    print(f"[autoqso] Call 1st: auto-start QSO z {call_de}")
                    start_result = self._qso_engine.start_qso(call_de, initial_decode=parsed)
                    if start_result and start_result.get("action") == "reply":
                        await self._dispatch_auto_reply(start_result, m)
                await self.hub.broadcast({"type": "auto_qso_queue",
                                           "queue": list(self._qso_engine.queue),
                                           "active": self._qso_engine.partner_call})
                return

            if result.get("action") == "reply":
                await self._dispatch_auto_reply(result, m)

                if result.get("qso_complete"):
                    print(f"[autoqso] QSO z {self._qso_engine.partner_call} zakonczone (73)")
                    # Wyslij PELNE dane QSO do wstepnego wypelnienia formularza
                    # logowania PRZED abort_qso() (ktory resetuje partner_call/
                    # grid/raporty z powrotem do None) — uzytkownik musi sam
                    # zatwierdzic (przycisk "+ LOG QSO"), automatyka NIE zapisuje
                    # bezposrednio do dziennika.
                    # UWAGA: prefix "R" (np. "R-15") to marker protokolu FT8
                    # ("potwierdzam + oto moj raport"), NIE czesc wlasciwej
                    # wartosci raportu sygnalu — usuwamy go przed wstawieniem
                    # do pola logu, zeby zachowac standardowy format ADIF.
                    rst_rcvd = self._qso_engine.partner_report_recv or ""
                    if rst_rcvd.startswith("R"):
                        rst_rcvd = rst_rcvd[1:]
                    rst_sent = self._qso_engine.partner_report_sent or ""
                    if rst_sent.startswith("R"):
                        rst_sent = rst_sent[1:]
                    # AUTO-ZAPIS QSO do dziennika operatora (decyzja projektowa
                    # lipiec 2026: automat zapisuje sam, operator moze potem
                    # edytowac w MOJ LOG QSO — zamiast recznego zatwierdzania,
                    # ktore przy stacji klubowej z automatem bylo pomijane i
                    # lacznosci ginely).
                    try:
                        from datetime import datetime as _dtx, timezone as _tzx
                        _now = _dtx.now(_tzx.utc)
                        _uid = getattr(self, "_autoqso_uid", None)
                        _freq_hz = int(getattr(self.rig, "freq", 0) or 0)
                        _band = self._get_band_for_freq(_freq_hz) or ""
                        # Grid: z QSO jesli partner go wyslal; inaczej z cache
                        # dekodow (jego wczesniejsze CQ z gridem).
                        _grid = (self._qso_engine.partner_grid or
                                 getattr(self, "_call_grid_cache", {}).get(
                                     (self._qso_engine.partner_call or "").upper(), ""))
                        if _uid:
                            _saved = qso_db.add_qso(_uid, {
                                "call": self._qso_engine.partner_call,
                                "qso_date": _now.strftime("%Y%m%d"),
                                "time_on": _now.strftime("%H%M%S"),
                                "band": _band,
                                "mode": self._ft8_decode_mode,
                                "freq": f"{_freq_hz/1e6:.6f}" if _freq_hz else "",
                                "rst_sent": rst_sent,
                                "rst_rcvd": rst_rcvd,
                                "gridsquare": _grid or "",
                                "my_call": self._qso_engine.my_call or "",
                                "my_gridsquare": self._qso_engine.my_grid or "",
                                "source": "auto",
                            })
                            print(f"[autoqso] QSO ZAPISANE do logu: "
                                  f"{self._qso_engine.partner_call} {_band} "
                                  f"{self._ft8_decode_mode}")
                            await self.hub.broadcast({"type": "qso_logged",
                                                       "qso": _saved})
                        else:
                            print("[autoqso] UWAGA: brak uid operatora - "
                                  "QSO NIE zapisane automatycznie", flush=True)
                    except Exception as _e:
                        print(f"[autoqso] BLAD auto-zapisu QSO: {_e}", flush=True)
                    await self.hub.broadcast({"type": "auto_qso_complete",
                                               "dxCall": self._qso_engine.partner_call,
                                               "dxGrid": self._qso_engine.partner_grid or "",
                                               "rstSent": rst_sent,
                                               "rstRcvd": rst_rcvd,
                                               "mode": self._ft8_decode_mode})
                    await self.hub.broadcast({"type": "auto_qso_status",
                                               "state": "DONE",
                                               "partner": self._qso_engine.partner_call})
                    # Pozwol UI/logowaniu zareagowac (np. auto-log do dziennika)
                    # zanim ewentualnie podejmiemy kolejna stacje z kolejki.
                    self._qso_engine.abort_qso()  # wraca do IDLE, partner_call=None
                    self._qso_period_locked = False  # odblokuj period dla nastepnego QSO
                    await self._advance_auto_qso_queue()
                else:
                    await self.hub.broadcast({"type": "auto_qso_status",
                                               "state": self._qso_engine.state,
                                               "partner": self._qso_engine.partner_call})
        except Exception as e:
            print(f"[autoqso] BLAD przetwarzania automatyki: {e}")

    async def _dispatch_auto_reply(self, result: dict, m: dict):
        """Podstawia zmierzony SNR (jesli wymagany) i planuje wyslanie
        odpowiedzi wygenerowanej przez silnik QSO."""
        if result.get("needs_measured_report"):
            result = dict(result)  # nie mutuj oryginalnego dict z silnika
            result["report_or_grid"] = self._format_report(m.get("snr_db", m.get("snr", 0)))
            self._qso_engine.record_sent_report(result["report_or_grid"])
        await self._send_auto_tx(result, partner_decode=m)

    async def _send_auto_tx(self, action: dict, partner_decode: dict = None):
        """Uruchamia wyslanie wiadomosci wygenerowanej przez automatyke QSO,
        uzywajac DOKLADNIE tej samej sciezki co reczne TX (_ft8_tx_sequence) —
        co oznacza ze respektuje synchronizacje do okna 15s, PTT, itd.

        Automatycznie wykrywa okno partnera i ustawia PRZECIWNY period nadawania,
        zeby nie kolidowac (jesli partner nadawal w period=1, my nadajemy w period=2).
        """
        if not action:
            return

        # Detect the partner's TX window and set the opposite period.
        if partner_decode and not getattr(self, '_qso_period_locked', False):
            import time as _time
            window_s = ft4_encoder.FT4_SLOT_TIME if self._ft8_decode_mode == "FT4" else 15.0
            now = _time.time()
            # The partner transmitted in the window that just completed, NOT the
            # one we're in now. Decodes arrive at the end of / just after the RX
            # window, so `now` is already one window past the partner's TX. Using
            # `now` directly computed the wrong parity and told us to transmit in
            # the same window we received in (the "will TX in A instead of B"
            # bug). Anchor to the last window boundary, then step back one window
            # to the partner's actual TX window.
            last_boundary = (now // window_s) * window_s
            partner_tx_start = last_boundary - window_s
            partner_window_idx = int(partner_tx_start / window_s)
            partner_period = 1 if (partner_window_idx % 2 == 0) else 2
            my_period = 2 if partner_period == 1 else 1
            if self._ft8_tx_period != my_period:
                self._ft8_tx_period = my_period
                print(f"[autoqso] Auto-period: partner={partner_period} -> my_period={my_period}")
                await self.hub.broadcast({"type": "ft8_tx_period", "period": my_period})
            self._qso_period_locked = True  # hold the period for the whole QSO

        print(f"[autoqso] Planuje TX: {action['call_to']} {action['call_de']} "
              f"{action['report_or_grid']} (r_flag={action.get('r_flag', False)})")

        # Dedup — nie wysylaj tej samej wiadomosci dwa razy z rzedu
        tx_key = (action['call_to'], action.get('report_or_grid'))
        if tx_key == self._last_auto_tx_key and self._ft8_tx_lock.locked():
            print(f"[autoqso] Duplikat TX {tx_key} — ignoruje")
            return
        self._last_auto_tx_key = tx_key

        # Wygeneruj PCM PRZED uruchomieniem TX task — eliminuje ~500ms opoznienia DT
        ct, cd, rg, rf = (action["call_to"], action["call_de"],
                          action["report_or_grid"], action.get("r_flag", False))
        is_ft4 = (self._ft8_decode_mode == "FT4")
        freq_hz = self._ft8_tx_freq_hz
        try:
            import time as _pt
            _t = _pt.time()
            if is_ft4:
                pcm, _dbg, dur = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: ft4_encoder.generate_tx_pcm48k_ft4(
                        ct, cd, rg, rf, base_freq_hz=freq_hz))
            else:
                pcm, _dbg, dur = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: ft8_encoder.generate_tx_pcm48k(
                        ct, cd, rg, rf, base_freq_hz=freq_hz))
            # Zachowaj ldpc_valid w cache - inaczej przy uzyciu cache debug={}
            # i kod falszywie ostrzega ldpc_valid=None (choc PCM jest poprawny).
            self._pre_pcm_cache = (ct, cd, rg, pcm, dur, _dbg.get("ldpc_valid"))
            print(f"[ft8] PCM gotowy ({dur:.2f}s, gen={(_pt.time()-_t)*1000:.0f}ms, "
                  f"ldpc_valid={_dbg.get('ldpc_valid')})")
        except Exception as e:
            print(f"[ft8] pre_gen blad: {e}")

        asyncio.create_task(self._ft8_tx_sequence(ct, cd, rg, rf, auto_respond=True))

    async def _ft8_rx_loop(self):
        """
        Petla w tle: odbiera wyniki dekodowania FT8/FT4 z Rust (ham_audio.exe)
        przez ft8_rust_receiver i rozsyla do klientow WS.
        Rust sam zarządza timingiem okien (pop at boundary, decode, send JSON).
        """
        print("[ft8rx] Rust-based decode loop start", flush=True)
        import time as _time_rx
        while True:
            try:
                if not (self._ft8_rx_enabled and self.rust_audio and
                        self.rust_audio.ft8_rx_enabled):
                    await asyncio.sleep(0.2)
                    continue

                msg = await self.rust_audio.ft8_get_decode()
                if msg is None:
                    continue

                if msg.get("type") != "wsjtx_decode":
                    continue

                # Running total for internal use. The old per-message log was
                # throttled (first 10, then every 20th), which looked like the
                # decoder "only found 1" when it had actually found many — the
                # messages were decoded and broadcast, just not printed. We now
                # log every decode compactly so the count in the log matches
                # what the operator sees in the UI.
                self._ft8_dbg_count = getattr(self, "_ft8_dbg_count", 0) + 1
                print(f"[ft8rx] dekod -> UI: {msg.get('message','?')} "
                      f"(#{self._ft8_dbg_count}, klientow={len(self.online_users)})",
                      flush=True)

                # Broadcast do przegladarek
                await self.hub.broadcast(msg)

                # Cache call->grid ze WSZYSTKICH dekodow. Partner odpowiadajacy
                # na NASZE CQ nie wysyla grida (np. "XX0XXX JA3FYC +11"), wiec
                # partner_grid bywa pusty przy auto-zapisie QSO ("raz zapisuje
                # grid raz nie"). Ale ta stacja zwykle chwile wczesniej
                # CQ-owala Z gridem — lookup z cache uzupelnia locator w logu.
                try:
                    _cd = (msg.get("call_de") or "").strip().upper()
                    _rg = (msg.get("report_or_grid") or "").strip().upper()
                    if (_cd and len(_rg) == 4 and _rg[0].isalpha()
                            and _rg[1].isalpha() and _rg[2].isdigit()
                            and _rg[3].isdigit() and _rg != "RR73"):
                        # RR73 formalnie pasuje do wzorca lokatora (litery A-R
                        # + cyfry) — protokol CELOWO wybral grid z Antarktydy
                        # jako zakonczenie QSO. Bez wykluczenia trafial do
                        # cache i byl logowany jako "grid locator" partnera.
                        _gc = getattr(self, "_call_grid_cache", None)
                        if _gc is None:
                            _gc = self._call_grid_cache = {}
                        if len(_gc) > 1000:
                            _gc.clear()
                        _gc[_cd] = _rg
                except Exception:
                    pass

                # Zasil pule znanych znakow dla walidacji dekodow CW
                # (ta sama zasada co baza w CW Skimmerze — poprawia przekrecone
                # znaki). Zrodla: dekody FT8, log QSO, spoty DX cluster.
                try:
                    _calls = [msg.get("call_de"), msg.get("call_to")]
                    if deepcw_engine is not None:
                        deepcw_engine.add_known_calls(
                        [c for c in _calls if c and not str(c).startswith("<")]
                    )
                except Exception:
                    pass

                # Auto-follow RX/TX za stacja ktora do nas nadaje (respektuje
                # Hold TX). Wywolywane ZAWSZE - metoda sama sprawdza czy TX
                # jest zamrozony i decyduje ktore prazki przesuwac.
                await self._process_tx_freeze_rx_follow(msg)
                if self._auto_seq_enabled:
                    await self._process_auto_qso(msg)

            except Exception as e:
                print(f"[ft8rx] BLAD: {e}", flush=True)
                await asyncio.sleep(1.0)


    async def _waterfall_loop(self):
        """
        Petla w tle: co WF_INTERVAL_S strumieniuje kompaktowa kolumne widma
        (scope/waterfall) do wszystkich klientow WS. Niezalezna od cyklu
        dekodowania FT8 (ktory trwa 15s) — daje plynnie przewijajacy sie
        wodospad jak w prawdziwym WSJT-X/JTDX. Dziala TYLKO gdy FT8 RX jest
        wlaczone (ten sam przelacznik co dekodowanie), zeby nie marnowac
        CPU gdy nikt nie patrzy na zakladke.
        """
        WF_INTERVAL_S = 0.2
        while True:
            await asyncio.sleep(WF_INTERVAL_S)
            try:
                if not (self._ft8_rx_enabled and self.audio.rx_active):
                    continue
                samples, native_rate = self.audio.pop_waterfall_chunk()
                if samples is None or len(samples) < 64:
                    continue
                loop = asyncio.get_running_loop()
                db_column = await loop.run_in_executor(
                    None, waterfall.compute_waterfall_column, samples,
                    waterfall.N_BINS, waterfall.F_MIN, waterfall.F_MAX, native_rate)
                quantized = waterfall.quantize_for_transport(db_column)
                await self.hub.broadcast({
                    "type": "ft8_waterfall",
                    "fMin": waterfall.F_MIN,
                    "fMax": waterfall.F_MAX,
                    "nBins": waterfall.N_BINS,
                    "data": quantized,
                })
            except Exception as e:
                print(f"[waterfall] BLAD: {e}")

    async def _handle_rig_action(self, msg: dict, ws, role: str):
        """
        {type:'rig_action', id:'vfo_a'|'vfo_b'|'func_nb'|..., value?: bool}
        VFO select: id='vfo_a'/'vfo_b' -> RigCAT.set_vfo('VFOA'/'VFOB')
        Func toggle: id='func_XXX', value: bool -> RigCAT.set_func('XXX', value)
        """
        action_id = msg.get("id", "")
        if not action_id:
            return
        # Radio lock: tylko trzymajacy TRX (lub admin) moze sterowac
        can, why = self._can_control_radio(ws, role)
        if not can:
            await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
            return
        if not await self._dynamic_allowed(action_id, role, "action"):
            print(f"[features] odmowa rig_action '{action_id}' dla roli {role}")
            return

        if action_id.startswith("vfo_"):
            vfo_name = "VFOA" if action_id == "vfo_a" else "VFOB" if action_id == "vfo_b" else None
            if vfo_name:
                self.rig.vfo = vfo_name  # stan takze w sim
                if not self.rig.sim and hasattr(self.rig, "set_vfo"):
                    try:
                        await self.rig.set_vfo(vfo_name)
                    except Exception as e:
                        print(f"[rig] set_vfo blad: {e}")
                # Broadcast STANU vfo do wszystkich klientow — przyciski A/B
                # podswietlane wg stanu radia, nie wg lokalnego kliku (nowy
                # user po zalogowaniu widzi ktore VFO aktywne).
                await self.hub.broadcast({"type": "vfo", "vfo": vfo_name})
                await self.hub.broadcast({"type": "rig_action_ack", "id": action_id, "ok": True})

        elif action_id == "power_toggle":
            value = bool(msg.get("value", True))
            print(f"[rig] power_toggle value={value} sim={self.rig.sim} has_set_power={hasattr(self.rig, 'set_power')}", flush=True)
            if not self.rig.sim and hasattr(self.rig, "set_power"):
                try:
                    await self.rig.set_power(value)
                    self._rig_power_on = value
                    # Broadcast pod nazwa 'power_state' (frontend tego slucha!).
                    # Wczesniej bylo 'rig_power' - inna nazwa - wiadomosc sie
                    # gubila i przyciski innych userow sie nie aktualizowaly.
                    await self.hub.broadcast({"type": "power_state", "value": value})
                    if not value:
                        # Radio wyłączone — zresetuj waterfall u wszystkich
                        await self.hub.broadcast({"type": "scope_reset"})
                    await self.hub.broadcast({"type": "rig_action_ack", "id": action_id,
                                               "value": value, "ok": True})
                    if value:
                        # Radio potrzebuje czasu po wybudzeniu zanim CI-V zacznie
                        # odpowiadac. Czekamy i WERYFIKUJEMY ze faktycznie zyje
                        # (odczyt freq) - bez tego waterfall startowal na oslep
                        # i ladowal w stanie SIM gdy radio jeszcze spalo.
                        await self._verify_radio_awake_and_start_scope()
                except Exception as e:
                    print(f"[rig] set_power blad: {e}", flush=True)
                    # Broadcast realnego stanu (nie zmienil sie) zeby przyciski
                    # userow wrocily do poprawnej pozycji
                    await self.hub.broadcast({"type": "power_state",
                                               "value": self._rig_power_on})
                except Exception as e:
                    print(f"[rig] set_power blad: {e}", flush=True)

        elif action_id.startswith("func_"):
            func_name = action_id[len("func_"):].upper()
            value = bool(msg.get("value", True))
            if not self.rig.sim and hasattr(self.rig, "set_func"):
                try:
                    await self.rig.set_func(func_name, value)
                    await self.hub.broadcast({"type": "rig_action_ack", "id": action_id,
                                               "value": value, "ok": True})
                except Exception as e:
                    print(f"[rig] set_func blad: {e}")

    async def _handle_rig_slider(self, msg: dict, ws, role: str):
        """
        {type:'rig_slider', id:'level_rfpower', value: float}
        -> RigCAT.set_level('RFPOWER', value)
        """
        slider_id = msg.get("id", "")
        if not slider_id.startswith("level_"):
            return
        # Radio lock: tylko trzymajacy TRX (lub admin) moze sterowac
        can, why = self._can_control_radio(ws, role)
        if not can:
            await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
            return
        if not await self._dynamic_allowed(slider_id, role, "slider"):
            print(f"[features] odmowa rig_slider '{slider_id}' dla roli {role}")
            return

        param = slider_id[len("level_"):].upper()
        try:
            value = float(msg.get("value", 0))
        except (TypeError, ValueError):
            return

        if not self.rig.sim:
            try:
                await self.rig.set_level(param, value)
            except Exception as e:
                print(f"[rig] set_level({param}) blad: {e}")

        await self.hub.broadcast({"type": "rig_slider_ack", "id": slider_id,
                                   "value": value}, skip=ws)

    # ── HTTP handler (aiohttp) ─────────────────────────────────────────────────
    async def http_handler(self, request: web.Request) -> web.Response:
        method = request.method.upper()
        path   = request.path
        query  = dict(request.rel_url.query)

        # CORS preflight.
        # Origin is left as "*" deliberately. This is safe here because auth is
        # bearer-token in the Authorization header, NOT cookies — so a hostile
        # web page can't ride an existing session (no ambient credentials to
        # abuse). Tightening to a fixed origin would also break the Cloudflare
        # quick tunnel, whose domain (xxx.trycloudflare.com) is random per run.
        # If a named tunnel with a stable domain is ever used, restrict here.
        if method == "OPTIONS":
            return web.Response(status=204, headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
                "Access-Control-Allow-Headers": "Authorization,Content-Type",
            })

        # Auth
        user = None
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token_str = auth[7:].strip()
            user = self._check_pw_ver(jwt_verify(token_str))
            if not user:
                print(f"[auth] jwt_verify FAIL dla tokenu: {token_str[:20]}...", flush=True)
        if not user:
            qt = query.get("token", "")
            if qt: user = self._check_pw_ver(jwt_verify(qt))
        if not user and path.startswith("/api/") and path not in (
            "/api/auth/login", "/api/auth/reset", "/api/auth/reset-request",
            # Popularne endpointy skanowane przez boty (nie loguj spamu):
            "/api/login", "/api/register", "/api/admin", "/api/v1/login",
            "/api/user", "/api/users", "/api/config",
            # Odpytywane cyklicznie przez UI — po wygasnieciu sesji leca co
            # sekunde i zalewaja log
            "/api/status/perf",
        ):
            # Tlumienie powtorzen: ten sam endpoint + IP logujemy raz na minute.
            # Przegladarka odpytuje cyklicznie, wiec po wygasnieciu tokena bez
            # tego log rosnie o setki identycznych linii.
            _k = f"{path}|{request.remote}"
            _now = time.time()
            _seen = getattr(self, "_auth_warn_seen", None)
            if _seen is None:
                _seen = self._auth_warn_seen = {}
            if _now - _seen.get(_k, 0) > 60:
                _seen[_k] = _now
                if len(_seen) > 200:      # nie pozwol rosnac w nieskonczonosc
                    _seen.clear()
                print(f"[auth] BRAK usera dla {path} | ip={request.remote} "
                      f"| auth={auth[:30]!r}", flush=True)

        # Strony publiczne — bez autoryzacji (przed blokiem API)
        if path == "/perf":
            # Strona diagnostyki wydajnosci. Sama strona jest publiczna (to tylko
            # HTML+JS), ale dane pobiera z /api/status/perf ktory wymaga admina.
            html = """<!doctype html><html lang=pl><head><meta charset=utf-8>
<title>HAM RADIO CTRL — Wydajnosc</title>
<meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#c9d1d9;
  --dim:#8b949e;--green:#b8c98f;--amber:#d29922;--red:#f85149;--mono:ui-monospace,Consolas,monospace}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--text);
  font-family:var(--mono);font-size:13px;line-height:1.5}
h1{font-size:16px;color:var(--green);margin:0 0 4px;letter-spacing:1px}
.sub{color:var(--dim);font-size:11px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:12px}
.card h2{font-size:10px;color:var(--amber);letter-spacing:2px;margin:0 0 10px;
  padding-bottom:6px;border-bottom:1px solid var(--border);text-transform:uppercase}
.row{display:flex;justify-content:space-between;padding:3px 0;gap:8px}
.k{color:var(--dim)}
.v{color:var(--text);text-align:right;word-break:break-all}
.bar{height:6px;background:#21262d;border-radius:3px;overflow:hidden;margin-top:3px}
.bar>div{height:100%;background:var(--green);transition:width .3s}
.bar.warn>div{background:var(--amber)} .bar.crit>div{background:var(--red)}
.cores{display:grid;grid-template-columns:repeat(auto-fit,minmax(60px,1fr));gap:6px;margin-top:6px}
.core{background:#21262d;border-radius:3px;padding:4px;text-align:center;font-size:10px}
.tag{display:inline-block;background:rgba(184,201,143,.1);border:1px solid var(--border);
  border-radius:3px;padding:1px 6px;margin:2px 2px 0 0;font-size:10px;color:var(--dim)}
.err{color:var(--red)}
.ok{color:var(--green)} .off{color:var(--dim)}
button{background:transparent;border:1px solid var(--border);color:var(--green);
  padding:6px 14px;border-radius:4px;cursor:pointer;font-family:var(--mono);font-size:11px}
button:hover{border-color:var(--green)}
</style></head><body>
<h1>HAM RADIO CTRL — DIAGNOSTYKA WYDAJNOSCI</h1>
<div class=sub>Odswiezanie co 3s &bull; <span id=ts>—</span>
  &bull; <button onclick="load()">Odswiez teraz</button>
  &bull; <a href="/" style="color:var(--dim)">&#8592; panel</a></div>
<div id=out class=grid><div class=card>Ladowanie...</div></div>
<script>
const $=(s)=>document.querySelector(s);
function bar(pct){const c=pct>85?'crit':pct>60?'warn':'';
  return `<div class="bar ${c}"><div style="width:${Math.min(100,pct||0)}%"></div></div>`;}
function row(k,v){return `<div class=row><span class=k>${k}</span><span class=v>${v}</span></div>`;}
function card(title,body){return `<div class=card><h2>${title}</h2>${body}</div>`;}
function yn(b){return b?'<span class=ok>TAK</span>':'<span class=off>nie</span>';}

async function load(){
  const tok = localStorage.getItem('token')||sessionStorage.getItem('ham_token')||'';
  try{
    const r = await fetch('/api/status/perf',{headers:tok?{'Authorization':'Bearer '+tok}:{}});
    if(r.status===403){$('#out').innerHTML=card('BRAK DOSTEPU',
      '<div class=err>Zaloguj sie jako admin i odswiez.</div>');return;}
    if(!r.ok){$('#out').innerHTML=card('BLAD','<div class=err>HTTP '+r.status+'</div>');return;}
    const d = await r.json();
    render(d);
    $('#ts').textContent = new Date().toLocaleTimeString();
  }catch(e){$('#out').innerHTML=card('BLAD','<div class=err>'+e.message+'</div>');}
}

function render(d){
  let h='';
  // CPU
  const c=d.cpu||{};
  if(c.error){h+=card('CPU','<div class=err>'+c.error+'</div>');}
  else{
    let cores='';
    (c.per_core_percent||[]).forEach((p,i)=>{
      cores+=`<div class=core>#${i}<br><b>${p.toFixed(0)}%</b>${bar(p)}</div>`;});
    h+=card('CPU',
      row('Rdzenie fiz. / log.',`${c.cores_physical||'?'} / ${c.cores_logical||'?'}`)+
      row('System',`${(c.system_percent||0).toFixed(1)}%`)+bar(c.system_percent)+
      row('Proces Python',`${(c.python_process_percent||0).toFixed(1)}%`)+bar(c.python_process_percent)+
      row('Watki Pythona',c.python_threads||'?')+
      `<div class=cores>${cores}</div>`);
  }
  // RAM
  const m=d.memory||{};
  if(m.system_percent!=null){
    h+=card('PAMIEC',
      row('System',`${m.system_percent.toFixed(1)}% z ${m.system_total_mb} MB`)+bar(m.system_percent)+
      row('Python (RSS)',`${m.python_rss_mb} MB`));
  }
  // Rust
  const ru=d.rust_audio_process||{};
  h+=card('RUST AUDIO (dekodowanie FT8)',
    ru.pid?row('PID',ru.pid)+row('CPU',`${(ru.cpu_percent||0).toFixed(1)}%`)+
      bar(ru.cpu_percent)+row('RAM',`${ru.rss_mb} MB`)
    :'<div class=off>Proces ham_audio.exe nie wykryty</div>');
  // Event loop
  const el=d.event_loop||{};
  if(el.error){h+=card('EVENT LOOP','<div class=err>'+el.error+'</div>');}
  else{
    h+=card('EVENT LOOP (asyncio)',
      row('Implementacja',el.impl||'?')+
      row('JSON',el.backend||'?')+
      row('Aktywne taski',el.active_tasks||0)+
      '<div style="margin-top:6px">'+(el.task_names||[]).map(n=>`<span class=tag>${n}</span>`).join('')+'</div>');
  }
  // Watki
  const t=d.threads||{};
  if(t.count!=null){
    h+=card('WATKI ('+t.count+')',
      '<div>'+(t.names||[]).map(n=>`<span class=tag>${n}</span>`).join('')+'</div>');
  }
  // WS
  const w=d.websocket||{};
  if(w.clients!=null){
    let ch='';
    Object.entries(w.channel_subscribers||{}).forEach(([k,v])=>{ch+=row(k,v);});
    h+=card('WEBSOCKET',
      row('Polaczeni klienci',w.clients)+row('Zalogowani userzy',w.online_users)+
      (ch?'<div style="margin-top:6px;border-top:1px solid var(--border);padding-top:6px">'+ch+'</div>':''));
  }
  // Workload
  const wl=d.workload||{};
  h+=card('OBCIAZENIE (co dziala)',
    row('FT8/FT4 RX',yn(wl.ft8_rx_enabled)+(wl.ft8_decode_mode?' ('+wl.ft8_decode_mode+')':''))+
    row('Audio RX',yn(wl.audio_rx_active))+
    row('Rust audio',yn(wl.rust_audio_connected))+
    row('Radio w SIM',wl.rig_sim?'<span class=err>TAK</span>':'<span class=ok>nie (realne)</span>')+
    row('PTT',yn(wl.ptt))+
    row('CQ w petli',yn(wl.cq_calling))+
    row('Radio zablokowane',yn(wl.radio_lock_held))+
    row('Klienci COM Bridge',wl.com_bridge_clients||0)+
    row('Rotatory',wl.rotators||0)+
    row('Przekazniki',yn(wl.relay_connected)));
  // Uptime
  const up=d.uptime_s||0;
  const hh=Math.floor(up/3600),mm=Math.floor((up%3600)/60);
  h+=card('SERWER',
    row('Uptime',`${hh}h ${mm}m`)+row('Python',d.python||'?')+row('Platforma',d.platform||'?'));
  document.getElementById('out').innerHTML=h;
}
load(); setInterval(load,3000);
</script></body></html>"""
            return web.Response(text=html, content_type="text/html",
                                headers={"Access-Control-Allow-Origin": "*"})

        if path == "/wsjtx-setup":
            html = """<!DOCTYPE html>
<html><head><meta charset=UTF-8><title>WSJT-X Setup</title>
<style>
body{background:#0d0f0e;color:#d0e8d0;font-family:monospace;padding:30px;max-width:700px;margin:0 auto}
h1{color:#4ecb6a;letter-spacing:2px}
.step{background:#111811;border:1px solid #2a4a2a;border-radius:6px;padding:16px;margin:12px 0}
.num{color:#4ecb6a;font-size:20px;font-weight:bold;margin-right:10px}
code{background:#0a120a;border:1px solid #1e3a1e;padding:4px 10px;border-radius:4px;color:#4ecb6a;display:inline-block}
.btn{display:inline-block;background:rgba(76,219,106,0.15);border:1px solid #4ecb6a;
     color:#4ecb6a;padding:12px 24px;border-radius:6px;text-decoration:none;
     font-size:14px;letter-spacing:1px;margin:8px 0}
.note{color:#f0b429;font-size:12px;margin-top:6px;display:block}
a.back{color:#557755;font-size:12px;text-decoration:none}
</style></head><body>
<h1>WSJT-X &#8212; Konfiguracja zdalna</h1>
<p>Polacz WSJT-X na swoim komputerze z radiem na serwerze. Wymagany VB-Audio Cable.</p>

<div class=step>
<span class=num>1</span><b>Pobierz adapter WSJT-X</b><br><br>
<a class=btn href="/download/wsjtx">&#11015; Pobierz wsjtx_local.exe</a>
<span class=note>Nie wymaga instalacji. Dwuklik wystarczy. Rozmiar ok. 15MB.</span>
</div>

<div class=step>
<span class=num>2</span><b>Uruchom wsjtx_local.exe</b><br><br>
Dwukliknij plik. Wpisz gdy zapyta:<br><br>
&bull; Adres serwera: <code>https://your-server.example.com</code> (juz wypelniony)<br>
&bull; Login i haslo do panelu www<br><br>
<span class=note>Dane sa zapamietywane &#8212; nastepnym razem bez pytania.</span>
</div>

<div class=step>
<span class=num>3</span><b>WSJT-X &#8594; Settings &#8594; Radio</b><br><br>
Rig: <code>Hamlib NET rigctl</code><br>
Network server host: <code>localhost</code> &nbsp; Port: <code>4532</code><br>
PTT Method: <code>CAT</code><br><br>
Kliknij <b>Test CAT</b> &#8212; powinno dzialac.
</div>

<div class=step>
<span class=num>4</span><b>Audio przez VB-Audio Cable</b><br><br>
Panel www &#8594; <b>&#128266; Wlacz audio RX</b> (slyszysz radio w przegladarce)<br><br>
Windows &#8594; Dzwiek &#8594; Nagrywanie &#8594; (glosniki przegladarki) &#8594;<br>
Wlasciwosci &#8594; Nasluchiwanie &#8594; CABLE Input<br><br>
WSJT-X Audio Input: <code>CABLE Output</code><br><br>
Panel www &#8594; <b>&#127908; TX mikrofon</b> przed nadawaniem FT8
</div>

<br><a class=back href="/">&#8592; Wróc do panelu</a>
</body></html>"""
            return web.Response(text=html, content_type="text/html",
                                headers={"Access-Control-Allow-Origin": "*"})

        if path == "/download/com-bridge":
            # Pobierz klienta HAM-RADIO-CTRL.exe (COM Bridge dla CW Skimmer/HRD).
            #
            # UWAGA (2026-07-05): NIE wymagamy logowania! Klik w <a href> w
            # przegladarce NIE wysyla naglowka Authorization (JWT), wiec wymog
            # auth blokowal pobieranie (401). EXE nie zawiera sekretow - jest
            # publicznym plikiem do pobrania, wiec auth jest zbedny.
            import pathlib as _pl
            import os as _os

            # Katalog 'downloads' szukany w kolejnosci:
            # 1. Zmienna srodowiskowa HAM_DOWNLOADS_DIR (override dla innych PC)
            # 2. downloads/ obok webapp.py (standardowo C:\...\ham\downloads)
            # 3. downloads/ wzgledem katalogu roboczego (fallback)
            _dirs = []
            _env = _os.environ.get("HAM_DOWNLOADS_DIR")
            if _env:
                _dirs.append(_pl.Path(_env))
            _script_dir = _pl.Path(__file__).resolve().parent
            # W spakowanym EXE most COM jest w BUNDLE (_MEIPASS), nie obok __file__
            try:
                from config import BUNDLE as _BUNDLE, DATA as _DATA_DIR
                _dirs.append(_BUNDLE)                       # spakowany w EXE
                _dirs.append(_BUNDLE / "bridge")           # podfolder w paczce
                _dirs.append(_DATA_DIR)                    # obok EXE / APPDATA
                _dirs.append(_DATA_DIR / "downloads")
            except Exception:
                pass
            _dirs.append(_script_dir / "downloads")
            _dirs.append(_pl.Path("downloads"))          # wzgledny fallback
            _dirs.append(_script_dir / "bridge_client" / "dist")  # swiezy build

            # KOLIZJA NAZW: serwer produktu TEZ nazywa sie HAM-RADIO-CTRL.exe.
            # Most COM jest spakowany jako HAM-RADIO-CTRL-bridge.exe (inna nazwa),
            # zeby endpoint nie serwowal przez pomylke samego serwera. Szukamy
            # najpierw wersji -bridge, potem (dla dev) oryginalnej nazwy w
            # miejscach gdzie NIE ma serwera (downloads, bridge_client/dist).
            _bridge_names = ["HAM-RADIO-CTRL-bridge.exe"]
            # W trybie dev (nie spakowany) most moze miec oryginalna nazwe w
            # downloads/ albo bridge_client/dist - tam nie ma serwera-EXE.
            _dev_dirs = [_script_dir / "downloads", _pl.Path("downloads"),
                         _script_dir / "bridge_client" / "dist"]

            _checked = []
            # 1. Szukaj HAM-RADIO-CTRL-bridge.exe wszedzie (produkt)
            for _d in _dirs:
                for _name in _bridge_names:
                    _f = _d / _name
                    _checked.append(str(_f))
                    if _f.exists():
                        print(f"[download] serwuje most COM: {_f} ({_f.stat().st_size:,} B)", flush=True)
                        return web.Response(
                            body=_f.read_bytes(),
                            content_type="application/octet-stream",
                            headers={
                                "Content-Disposition": "attachment; filename=HAM-RADIO-CTRL.exe",
                                "Access-Control-Allow-Origin": "*",
                            }
                        )
            # 2. Dev fallback: oryginalna nazwa, ale TYLKO w katalogach bez serwera
            for _d in _dev_dirs:
                _f = _d / "HAM-RADIO-CTRL.exe"
                _checked.append(str(_f))
                if _f.exists():
                    print(f"[download] serwuje most COM (dev): {_f} ({_f.stat().st_size:,} B)", flush=True)
                    return web.Response(
                        body=_f.read_bytes(),
                        content_type="application/octet-stream",
                        headers={
                            "Content-Disposition": "attachment; filename=HAM-RADIO-CTRL.exe",
                            "Access-Control-Allow-Origin": "*",
                        }
                    )
            print(f"[download] most COM nie znaleziony. Sprawdzono:", flush=True)
            for _c in _checked:
                print(f"[download]   - {_c}", flush=True)
            _list = "".join(f"<li><code>{_c}</code></li>" for _c in _checked)
            return web.Response(
                text=f"<h2>Most COM (HAM-RADIO-CTRL-bridge.exe) nie znaleziony</h2>"
                     f"<p>Zbuduj most COM i umiesc jako <code>HAM-RADIO-CTRL-bridge.exe</code>:</p><ul>{_list}</ul>",
                content_type="text/html", status=404
            )

        if path == "/download/wsjtx":
            import pathlib as _pl
            for _name in ["wsjtx_local.exe", "dist/wsjtx_local.exe"]:
                _f = _pl.Path(_name)
                if _f.exists():
                    _data = _f.read_bytes()
                    return web.Response(
                        body=_data,
                        content_type="application/octet-stream",
                        headers={
                            "Content-Disposition": "attachment; filename=wsjtx_local.exe",
                            "Access-Control-Allow-Origin": "*",
                        }
                    )
            return web.Response(
                text="<h2>wsjtx_local.exe nie znaleziony — uruchom build_exe.bat na serwerze</h2>",
                content_type="text/html", status=404
            )

        # API
        if path.startswith("/api/"):
            body = {}
            if method in ("POST", "PUT", "PATCH"):
                ct = request.headers.get("Content-Type", "")
                if "octet-stream" in ct:
                    body = await request.read()  # raw bytes
                else:
                    try:
                        body = await request.json()
                    except:
                        pass
            # For DELETE with path params, rebuild query dict as list-of-values style
            q = {k: [v] for k, v in query.items()}
            # IP klienta dla rate limitingu. Za reverse proxy / tunelem prawdziwy
            # adres jest w X-Forwarded-For (pierwszy wpis = oryginalny klient).
            _xff = request.headers.get("X-Forwarded-For", "")
            _client_ip = (_xff.split(",")[0].strip() if _xff
                          else (request.remote or ""))
            status, result = await self.api(method, path, body, user, q, _client_ip)
            # Specjalny typ — odpowiedź binarna (np. model ONNX)
            if status == "binary":
                return web.Response(
                    body=result,
                    content_type="application/octet-stream",
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "public, max-age=86400",
                    },
                )
            # Specjalny typ — eksport pliku (ADIF/CSV/EDI) z naglowkiem pobierania
            if status == "export":
                return web.Response(
                    body=result["body"],
                    content_type=result["content_type"],
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Content-Disposition":
                            f'attachment; filename="{result["filename"]}"',
                    },
                )
            return web.Response(
                status=status,
                body=_fast_json_bytes(result) if _JSON_BACKEND == "orjson"
                     else json.dumps(result, ensure_ascii=False).encode('utf-8'),
                content_type="application/json",
                headers={"Access-Control-Allow-Origin": "*"},
            )

        # Static files
        if path in ("/", ""):
            path = "/index.html"
        elif path == "/login":
            path = "/login.html"

        fpath = (PUBLIC / path.lstrip("/")).resolve()
        try:
            fpath.relative_to(PUBLIC.resolve())
        except ValueError:
            fpath = PUBLIC / "index.html"

        if fpath.is_file():
            ext  = fpath.suffix.lower()
            mime = MIME.get(ext, "application/octet-stream")
            mime_clean = mime.split(";")[0].strip()
            key = str(fpath)

            # Sprawdz cache — jesli plik nie zmienil sie na dysku, uzyj z RAM
            entry = _STATIC_CACHE.get(key)
            if entry:
                cached_mtime, raw, gz, cached_mime, etag = entry
                try:
                    cur_mtime = fpath.stat().st_mtime
                    if cur_mtime != cached_mtime:
                        # Plik sie zmienil — invaliduj i zaladuj ponownie
                        entry = None
                except OSError:
                    entry = None
            if not entry:
                entry = _cache_static_file(fpath, mime)
                _STATIC_CACHE[key] = entry
                _, raw, gz, _, etag = entry

            # If-None-Match — HTTP 304 (client cache hit)
            client_etag = request.headers.get("If-None-Match", "")
            if client_etag == etag:
                return web.Response(status=304, headers={
                    "ETag": etag,
                    "Cache-Control": "public, max-age=3600, must-revalidate",
                })

            # Wybierz body: gzip jesli klient akceptuje i mamy pre-compressed
            accept_enc = request.headers.get("Accept-Encoding", "")
            body = raw
            headers = {
                "ETag": etag,
                "Cache-Control": "public, max-age=3600, must-revalidate",
                "Vary": "Accept-Encoding",
            }
            if gz is not None and "gzip" in accept_enc.lower():
                body = gz
                headers["Content-Encoding"] = "gzip"

            return web.Response(
                body=body,
                content_type=mime_clean,
                headers=headers,
            )
        else:
            # SPA fallback
            idx = PUBLIC / "index.html"
            if idx.is_file():
                return web.Response(body=idx.read_bytes(), content_type="text/html")
            return web.Response(status=404, text="Not Found")
