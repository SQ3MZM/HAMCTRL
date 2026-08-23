
#!/usr/bin/env python3
"""
webapp.py — WebSocket Hub + application (HTTP/WS routing, API).
"""
import re, time, json, asyncio, struct
import numpy as np
import aiohttp
import aiohttp.web as web

# ══════════════════════════════════════════════════════════════════════════════
# PERFORMANCE OPTIMIZATIONS (async sending, fast JSON)
# ══════════════════════════════════════════════════════════════════════════════
# orjson - if installed, 5-10x faster JSON than stdlib. Falls back to stdlib.
# Can be installed with: py -m pip install orjson
try:
    import orjson
    def _fast_json_bytes(msg):
        """Encode a message to UTF-8 bytes. Much faster than json.dumps."""
        return orjson.dumps(msg, option=orjson.OPT_SERIALIZE_NUMPY)
    _JSON_BACKEND = "orjson"
except ImportError:
    def _fast_json_bytes(msg):
        return json.dumps(msg, ensure_ascii=False).encode('utf-8')
    _JSON_BACKEND = "stdlib"

# winloop/uvloop - a faster event loop than plain asyncio.
# Windows: winloop, Linux/Mac: uvloop. Install with:
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
# Static file cache — keeps JS/CSS/HTML files in RAM (pre-compressed).
# Every GET for a static file gets bytes directly from the Map, no disk read.
#
# Files are cached lazily (on first request) and **unchanged** for the
# server's lifetime - restarting Python purges the cache. Fine for dev-time
# use: you restarted the server so they get the new build.
#
# Compression: gzip on-the-fly on first cache, bytes stored once.
# Brotli would be better but needs installing; gzip is in stdlib.
# ══════════════════════════════════════════════════════════════════════════════
import gzip
# Map[str_path] -> (mtime, bytes_raw, bytes_gz | None, mime, etag)
_STATIC_CACHE: dict = {}
_STATIC_CACHE_LOCK = None  # asyncio.Lock created when the loop starts

# MIME types worth gzipping (text-based). Binary ones (png, opus) are
# already compressed so gzip gains nothing but adds overhead.
_GZIP_MIMES = {
    "text/html", "text/css", "text/plain", "text/javascript",
    "application/javascript", "application/json", "application/xml",
    "image/svg+xml",
}

def _cache_static_file(fpath, mime: str) -> tuple:
    """Read a file from disk, pre-compress if text-based, return a cache entry."""
    import hashlib
    raw = fpath.read_bytes()
    mtime = fpath.stat().st_mtime
    # ETag = content hash (strong) - the client can send If-None-Match
    etag = '"' + hashlib.md5(raw).hexdigest()[:16] + '"'
    # gzip if text-based and > 1KB (smaller files aren't worth the overhead)
    gz = None
    if mime.split(";")[0].strip() in _GZIP_MIMES and len(raw) > 1024:
        try:
            gz = gzip.compress(raw, compresslevel=6)  # 6 = speed/ratio balance
            # Only keep the gzip version if it's actually smaller (already-compressed files have ~0.95 ratio)
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
GITHUB_REPO = "SQ3MZM/HAMCTRL"

from config import (CALLSIGN, LOCATOR, PORT, HAMLIB_MODELS, SCOPE_MODELS,
                    MIME, PUBLIC, CFG_F, USR_F, ADMIN_PW, FIRST_RUN, VERBOSE)
from auth import (jwt_sign, jwt_verify, hash_pw, hash_pw_secure,
                  verify_pw, needs_rehash)
from data import get_cfg, get_users, load_json, save_json, DEFAULT_MACROS
from crypto_secrets import encrypt_secret, decrypt_secret
import qso_db
import callbook
from audio import enumerate_audio_devices, auto_detect_radio_audio
from audio_stream import AudioStream
try:
    from webrtc_audio import WebRTCAudioReceiver
    _WEBRTC = True
except Exception as e:
    print(f"[webrtc] unavailable: {e}")
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
    print(f"[dxcluster] module unavailable: {e}")
    _DXCLUSTER_OK = False
try:
    from relay_controller import RelayController, list_serial_ports, MAX_PULSE_S, RELAY_COUNT
    _RELAY_OK = True
except Exception as e:
    print(f"[relay] module unavailable: {e}")
    _RELAY_OK = False
import ft8_rx_decoder
try:
    from deepcw_engine import deepcw_engine
except Exception as _e:
    deepcw_engine = None
    print(f"[deepcw] engine unavailable: {_e}", flush=True)
import ft4_rx_decoder
import waterfall
import qso_engine
from rigs.features import (FEATURES, effective_features, features_for_admin,
                            default_enabled_features, effective_dynamic,
                            dynamic_for_admin)


# Phone (not tablet) User-Agent heuristic for the / auto-routing to
# mobile.html — the same "Mobi" token approach used by most server-side
# mobile redirects (deliberately excludes iPad/Android tablets, which
# have enough screen for the desktop UI's scale-to-fit).
_MOBILE_UA_RE = re.compile(r'Mobi|iPhone|iPod|Android.*Mobile|Windows Phone|BlackBerry|Opera Mini|IEMobile')

# ══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET HUB (aiohttp)
# ══════════════════════════════════════════════════════════════════════════════
# Message type -> channel mapping. Anything not listed here goes to
# 'control' (the default). This allows transparent routing without
# modifying hundreds of existing hub.broadcast() calls — just set the
# channels per the table below and broadcast fills in the channel itself.
_MSG_TYPE_TO_CHANNEL = {
    # CI-V waterfall (Radio panel) - high volume, only for the Radio tab
    'scope_frame':       'scope',
    'scope_reset':       'scope',

    # FT8/FT4/WSJT-X - high volume of decodes and waterfall, only for the FT8 tab
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
    'auto_seq_status':   'ft8',
    'auto_qso_status':   'ft8',
    'auto_qso_queue':    'ft8',
    'auto_qso_error':    'ft8',
    'auto_qso_complete': 'ft8',
    'tune_status':       'ft8',
    'hound_status':      'ft8',
    'hound_step':        'ft8',
    # The CW decoder works independently of the FT8 tab — the window can
    # be opened from the keyer panel while operating CW. On the 'ft8'
    # channel the text only reached clients subscribed to FT8: the backend
    # logged decodes, but the CW window stayed silent because the
    # recipient list was empty. Every client has 'control', and this is
    # only a few characters per second — zero cost.
    'deepcw_text':       'control',

    # DX Cluster - only for the DXCluster tab
    'dx_spot':           'dxcluster',
    'dx_status':         'dxcluster',
}

def _channel_for_msg(msg: dict) -> str:
    """Pick a channel based on the message type. Default = 'control'."""
    return _MSG_TYPE_TO_CHANNEL.get(msg.get('type', ''), 'control')


class WSHub:
    """Broadcast hub with channel subscriptions.

    Clients subscribe only to the channels they care about (e.g. the Log
    tab doesn't need scope_frame or ft8_waterfall). Broadcast filters
    recipients by the channel parameter — an unsubscribed client doesn't
    get the message.

    Channels:
      - 'control':  default - everything about radio and system state
                    (freq, mode, ptt, radio_lock, chat, presence, toasts)
      - 'scope':    scope_frame from CI-V (Radio panel waterfall)
      - 'ft8':      ft8_waterfall, wsjtx_decode, auto_seq_status, tune_status etc.
      - 'dxcluster': dx_spot, dx_status
    The 'audio' channel is NOT here - audio goes through a separate
    WebSocket path (audio_stream.py) with its own subscriber mechanism.
    """

    def __init__(self):
        self._clients: set = set()
        # dict: ws -> set of channels the client subscribed to
        # Default on add() is {'control'} - everyone gets radio control.
        self._subs: dict = {}
        self._lock = asyncio.Lock()
        self._loop = None

    def set_loop(self, loop):
        self._loop = loop

    async def add(self, ws):
        async with self._lock:
            self._clients.add(ws)
            # Default: ALL channels - the client will drop the ones it
            # doesn't want via subscribe {mode:'set'}. This resolves a
            # race condition: a new client gets wsjtx_decode EVEN IF it
            # hasn't yet sent subscribe {channels:['ft8']} before the
            # first broadcast. Channel filtering still wins the same way
            # — a client on the Log tab sends subscribe
            # {channels:['control']} a moment later and stops getting
            # scope_frame/ft8_waterfall.
            self._subs[ws] = {'control', 'scope', 'ft8', 'dxcluster'}

    async def remove(self, ws):
        async with self._lock:
            self._clients.discard(ws)
            self._subs.pop(ws, None)

    async def subscribe(self, ws, channels):
        """Client adds channels to its subscription (doesn't replace it)."""
        async with self._lock:
            if ws not in self._subs:
                self._subs[ws] = set()
            self._subs[ws].update(channels)

    async def unsubscribe(self, ws, channels):
        """Client removes channels from its subscription. 'control' always stays."""
        async with self._lock:
            if ws not in self._subs:
                return
            self._subs[ws].difference_update(channels)
            self._subs[ws].add('control')  # can never be dropped

    async def set_channels(self, ws, channels):
        """Set the full channel set (used when switching tabs)."""
        async with self._lock:
            self._subs[ws] = set(channels) | {'control'}

    async def broadcast(self, msg: dict, skip=None, channel: str = None):
        """Parallel broadcast to a channel's subscribers.

        If channel=None, the channel is auto-picked based on msg['type']
        via _channel_for_msg() — so existing code calling hub.broadcast(msg)
        doesn't need to be modified.

        OPTIMIZATIONS:
        1. orjson instead of json.dumps (5-10x faster for large objects)
        2. Encode once to a string (not N times for N clients)
        3. asyncio.gather instead of sequential await (parallel send)
        4. Subscription filtering - a client only gets the channels it wants
        5. Removed prints from the hot path

        NOTE: we use send_str (not send_bytes) because the frontend
        distinguishes:
          - Binary (ArrayBuffer)  -> Opus audio frame
          - Text                  -> JSON control message
        """
        if not self._clients:
            return
        # Auto-route the channel if not given explicitly
        if channel is None:
            channel = _channel_for_msg(msg)
        # Snapshot clients without the lock + filter by channel
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

        # EPHEMERAL channels (scope, audio, ft8_waterfall) — data arrives
        # many times per second, so a single dropped frame doesn't matter.
        # When a client reads slowly, a TLS send blocks the loop waiting
        # for the buffer to drain (backpressure — found via looplag:
        # sslproto _do_write). We give it a short timeout: not keeping up
        # = skip it for THIS frame, instead of freezing everyone. Important
        # channels (control, chat) are sent normally.
        _ephemeral = channel in ("scope", "audio", "ft8_waterfall")

        async def _send_one(ws):
            if _ephemeral:
                # Slow client: data piles up in the TLS buffer, the next
                # write blocks the loop (backpressure). Short timeout — not
                # keeping up within 0.25s = skip this ephemeral frame for
                # it (scope/audio, the next one will come). The buffer is
                # capped by writer_limit when the WS is created (see
                # ws_handler), so send_str quickly signals congestion
                # instead of growing without bound.
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
        """Broadcast from a non-async thread (thread-safe).
        The channel is auto-routed based on msg['type'] if not given."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(
                self.broadcast(msg, channel=channel), self._loop
            )


# ══════════════════════════════════════════════════════════════════════════════
# APPLICATION
# ══════════════════════════════════════════════════════════════════════════════


def _adif_field(name: str, value) -> str:
    """Build an ADIF field: <name:length>value. Empty -> skipped."""
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""
    return f"<{name}:{len(s)}>{s}"


def qso_to_adif(qso: dict) -> str:
    """
    Convert a QSO (a dict from our database) into the ADIF string required
    by Cloudlog/WaveLog.

    The Cloudlog API /index.php/api/qso expects:
      {"key":..., "station_profile_id":..., "type":"adif", "string":"<call:5>...<eor>"}

    It does NOT accept JSON fields (call, band, mode...) — that was a bug:
    QSOs weren't reaching Cloudlog despite an HTTP 200 response.

    Frequency: ADIF requires MHz (e.g. 14.074). Our database holds Hz or
    MHz depending on the source (WSJT-X vs manual entry) - we normalize it.
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
            else:                    # already MHz
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
        _adif_field("operator",         qso.get("my_call", "")),
        _adif_field("my_gridsquare",    qso.get("my_gridsquare", "")),
        _adif_field("tx_pwr",           qso.get("power", "")),
        _adif_field("comment",          qso.get("comment", "")),
        _adif_field("prop_mode",        qso.get("prop_mode", "")),
        _adif_field("sat_name",         qso.get("sat_name", "")),
        _adif_field("sat_mode",         qso.get("sat_mode", "")),
        _adif_field("freq_rx",          qso.get("freq_rx", "")),
        _adif_field("band_rx",          qso.get("band_rx", "")),
        _adif_field("name",             qso.get("name", "")),
        _adif_field("qth",              qso.get("qth", "")),
        _adif_field("dxcc",             qso.get("dxcc", "")),
        _adif_field("country",          qso.get("country", "")),
        _adif_field("cont",             qso.get("cont", "")),
        _adif_field("cqz",              qso.get("cqz", "")),
        _adif_field("ituz",             qso.get("ituz", "")),
        _adif_field("state",            qso.get("state", "")),
        _adif_field("iota",             qso.get("iota", "")),
        _adif_field("qsl_sent",         qso.get("qsl_sent", "")),
        _adif_field("qsl_rcvd",         qso.get("qsl_rcvd", "")),
        _adif_field("lotw_qsl_sent",    qso.get("lotw_qsl_sent", "")),
        _adif_field("lotw_qsl_rcvd",    qso.get("lotw_qsl_rcvd", "")),
        _adif_field("lotw_qslsdate",    qso.get("lotw_qslsdate", "")),
        _adif_field("lotw_qslrdate",    qso.get("lotw_qslrdate", "")),
        _adif_field("eqsl_qsl_sent",    qso.get("eqsl_qsl_sent", "")),
        _adif_field("eqsl_qsl_rcvd",    qso.get("eqsl_qsl_rcvd", "")),
        _adif_field("pota_ref",         qso.get("pota_ref", "")),
        _adif_field("sota_ref",         qso.get("sota_ref", "")),
        _adif_field("wwff_ref",         qso.get("wwff_ref", "")),
    ]
    return "".join(p for p in parts if p) + "<eor>"


class App:
    def __init__(self):
        self.hub      = WSHub()
        self.cfg      = get_cfg()
        # Pick the backend right away based on the saved model in
        # config.json, so we don't needlessly create a RigCAT (rigctld) at
        # startup for a model that's going to go through CivRig
        # (SCOPE_MODELS) anyway — avoids a serial-port conflict between the
        # two backends.
        _rigs = self.cfg.get("rigs") or [{}]
        _saved_model = str(_rigs[0].get("model", ""))
        if _saved_model in SCOPE_MODELS:
            self.rig = CivRig(self.cfg, self._rig_bcast, log=print)
            print(f"[rig] start backend -> direct CI-V (saved model {_saved_model})")
        else:
            self.rig = RigCAT()
        self.rotators: list[Rotator] = []
        self.users    = get_users()
        self._migrate_plaintext_secrets()
        self.audio    = AudioStream()
        self.audio.cfg = self.cfg.get("audio", {})

        # Radio audio card auto-detection — if the config has NO RX/TX
        # devices set (or user_audio_auto=True), detect the card
        # automatically by name. Recognizes IC-7300, IC-705, FT-991, etc.
        self._audio_auto = self.cfg.setdefault("audio_auto_detect", True)
        if self._audio_auto:
            try:
                detection = auto_detect_radio_audio()
                if detection["detected"]:
                    if "audio" not in self.cfg: self.cfg["audio"] = {}
                    # Only overwrite if the user hasn't set it manually
                    if not self.cfg["audio"].get("rxDevice") and detection["rx"]:
                        self.cfg["audio"]["rxDevice"] = detection["rx"]
                    if not self.cfg["audio"].get("txDevice") and detection["tx"]:
                        self.cfg["audio"]["txDevice"] = detection["tx"]
                    self.audio.cfg = self.cfg["audio"]
                    print(f"[audio] auto-detect: {detection['pattern']} -> RX={detection['rx']!r}, TX={detection['tx']!r}")
                    self._audio_detection = detection
                else:
                    print("[audio] auto-detect: radio card not detected, use manual configuration")
                    self._audio_detection = detection
            except Exception as e:
                print(f"[audio] auto-detect error: {e}")
                self._audio_detection = {"detected": False, "rx": None, "tx": None,
                                          "pattern": None, "all_rx": [], "all_tx": []}
        else:
            self._audio_detection = {"detected": False, "rx": None, "tx": None,
                                      "pattern": None, "all_rx": [], "all_tx": []}

        self.rust_audio = None  # set by server.py when ham_audio.exe is available
        self._rig_power_on = True  # radio power state — updated by power_toggle
        self._ft8_tx_abort = False
        self._ft8_rx_enabled = False
        # Who started WSJT-X (uid) — needed for auto-stop on disconnect or
        # when the radio is released. Doesn't matter for multiple users
        # since only one can decode at a time (the backend is global), but
        # we need to know WHO in order to know when to stop.
        self._ft8_rx_owner_uid: str | None = None
        self._autoqso_uid: str | None = None  # the operator who started the current CQ/auto-QSO
        self._last_auto_tx_key = None
        self._last_auto_tx_action = None  # dict of the last sent auto-QSO message, for retransmission
        self._pre_pcm_cache = None  # (call_to, call_de, report, pcm_bytes, duration)  # auto-TX dedup: (call_to, report_or_grid)
        self._tx_watchdog_task = None  # asyncio Task for auto-off PTT
        self._tune_stop = False  # aborts the Tune tone

        # COM Bridge WS - for Windows EXE clients that create a virtual COM.
        # Clients connect over WSS to /ws/com-bridge, get a service->COM
        # mapping, and forward bytes in both directions. Used by CW Skimmer,
        # Logger32, HRD for remote access to the CI-V IC-7300 (and Yaesu/
        # Kenwood in the future).
        # Note: self.rig may be RigCAT() when CI-V is disabled - then the
        # bridge has nothing to offer and clients see service_status civ=false.
        civ_for_bridge = self.rig if isinstance(self.rig, CivRig) else None
        # can_write: a user may WRITE to the radio through the COM Bridge
        # only when they hold radio_lock or are an admin. Without this a
        # user without the lock could tune the frequency via CW Skimmer/HRD,
        # bypassing the UI locks.
        def _com_can_write(uid: str) -> bool:
            """
            May this user WRITE to the radio through the COM Bridge
            (change freq/mode/PTT from CW Skimmer/HRD)?

            Note: a viewer is already REJECTED at the WS connection stage
            (com_bridge_ws_handler), so only admin and operators are
            handled here.

            Write access is granted to:
              - admin (always)
              - an operator HOLDING radio_lock
            Not granted to:
              - an operator WITHOUT the lock (view only)
              - anyone without an account
            """
            if not uid:
                return False
            u = next((x for x in self.users if x.get('id') == uid), None)
            if not u:
                return False
            if u.get('role') == 'admin':
                return True
            # Operator - only when holding radio_lock
            # (a viewer won't reach here - rejected at connection)
            return self.radio_lock.get('user_id') == uid
        self.com_bridge_ws = ComBridgeWs(civ_rig=civ_for_bridge, hub=self.hub,
                                          log=print, can_write=_com_can_write)

        # ── DX Cluster manager (per-user telnet connections) ──────────────────
        self.dxcluster = None
        self._server_start_time = time.time()  # for computing uptime in /api/status
        if _DXCLUSTER_OK:
            async def _dx_broadcast(user_id: str, msg: dict):
                # Send WS only to the specific user (find their ws in online_users)
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
                    print(f"[relay] init error: {e}")

        # ── Scope/waterfall state and TX marker ──────────────────────────────────
        self._ft8_tx_freq_hz = 1000.0   # current target transmit frequency
        # NOTE: NOT 1500Hz — that's a documented "sweet spot"/reference
        # point of the IC-7300's internal digital processing in USB-D mode,
        # which produces a fixed notch (~185Hz wide) at exactly that
        # frequency in the RX audio path, independent of NB/NR/Notch/AGC/
        # PTT. Confirmed on two independent recordings (Audacity, no code
        # of ours involved) — present in USB-D, absent in plain USB. 1000Hz
        # is a safe distance from that point.
        self._ft8_tx_locked = False     # OLD mechanism — to be removed, kept
        # temporarily for future use in a "TX Hound" feature (planned
        # layer, separate session). NO LONGER used by the current code.
        self._ft8_tx_frozen = False     # NEW mechanism: TX frozen in place,
        # while RX automatically jumps to the frequency of every received
        # message addressed to US (call_to == CALLSIGN) — lets the operator
        # "freeze" TX and track the station currently transmitting to them,
        # without manually moving the RX marker.
        self._ft8_split_enabled = False # split mode: TX only above _ft8_split_min_hz
        self._ft8_split_min_hz = 1200.0
        self._ft8_rx_freq_hz = 1000.0   # independent RX marker (Rx Frequency panel) —
        # starts at the same value as TX, but can be moved completely
        # independently (e.g. listening on a different frequency than the current TX)

        # ── Fake Split (Rig Split) ───────────────────────────────────────────
        # Moves the VFO BEFORE transmitting so the TX audio lands near the
        # center of the SSB filter (~1500Hz), instead of near its edge —
        # at the edges the filter attenuates the signal (power drops,
        # ALC/splatter). The VFO is restored after transmitting. The logic
        # (the invariant on-air-freq = dial+audio) comes from
        # fake_split_prototype.py. The enabled state is remembered in the config.
        self._fake_split_enabled = bool(self.cfg.get("ft8", {}).get("fakeSplit", False))
        self._fake_split_state = None  # dict {dial_hz, audio_hz} to restore after TX, or None

        # ── QSO automation (full FT8 automation) ────────────────────────────
        self._qso_engine = qso_engine.QsoEngine(my_call=CALLSIGN, my_grid=LOCATOR)
        self._auto_seq_enabled = True    # ALWAYS active (there's no longer a
                                          # UI toggle); clicking a macro is a manual override
        # FT8 safety timer (WSJT-X's "Tx Watchdog") - False means the
        # operator has NOT recently confirmed presence (click/TX macro)
        # despite Call 1st being enabled, and the automation MUST stop
        # reacting to callers (see _process_auto_qso) until an
        # "ft8_timer_confirm" arrives from the frontend (FT8Timer.confirm()
        # in wsjtx.js). DELIBERATELY a separate flag from _auto_call_1st -
        # Call 1st by itself no longer gates auto-start on idleness, so
        # disabling Call 1st wouldn't replace this guard.
        self._ft8_operator_present = True
        self._auto_call_1st = False      # whether to automatically start a
        # QSO with the first station answering our CQ (instead of waiting
        # for a manual pick from the queue)
        self._auto_cq_text = None        # the CQ text we're currently
        # "transmitting" (used to recognize that a received reply concerns
        # OUR CQ, not some unrelated message addressed to us outside a QSO context)
        # Message scheduled to be sent in the NEXT available 15s window,
        # generated by the automation (on_decode) — a single "slot", since
        # at any moment there can be only one pending automatic transmission.
        self._auto_pending_tx = None  # dict {call_to, call_de, report_or_grid, r_flag} or None
        # ── Periodic CQ calling ────────────────────────────────
        # When the user calls CQ, we don't transmit just once - we repeat
        # every full period (2 windows) until someone answers or the
        # user/timer stops it. Without this, CQ only went out once.
        self._cq_calling = False          # whether we're actively calling CQ in a loop
        self._cq_call_de = None           # our call (for the repeating CQ)
        self._cq_report = None            # our grid (CQ CALL GRID)
        self._cq_task = None              # the CQ-repeat loop task
        # Mutex protecting against PARALLEL execution of _ft8_tx_sequence —
        # without this, if the decoder returns multiple messages in one
        # window and more than one triggers an automatic reply (or the
        # automation coincides in time with a manual TX), two parallel
        # PTT/audio-feed calls would collide with each other. EVERY call to
        # _ft8_tx_sequence (manual and automatic) must go through this lock.
        self._ft8_tx_lock = asyncio.Lock()
        self._ft8_tx_period = 1  # 1 = xx:00/xx:30 windows (default), 2 = xx:15/xx:45
        self._qso_period_locked = False  # True once a period is locked in for the current QSO
        # Counter for "which automatic TX action is CURRENTLY the most
        # recently scheduled one" — see the comment where it's used in
        # _send_auto_tx/_ft8_tx_sequence_inner (stale-TX-guard).
        self._autoqso_tx_seq = 0
        self._ft8_decode_mode = "FT8"  # "FT8" or "FT4" — which encoder/timing to use

        self.webrtc   = WebRTCAudioReceiver(
            on_pcm_frame=self.audio.feed_tx_pcm,
            on_track_started=self._webrtc_tx_start,
            on_track_ended=self._webrtc_tx_stop,
        ) if _WEBRTC else None
        self.wsjtx    = WsjtxUdpServer(self.hub.broadcast)
        self.tunnel   = TunnelManager(self.hub)
        self._caps_cache = {}

        # ── Radio Lock — one operator at a time ────────────────────────────────
        # Who currently "holds" the radio (can tune, key PTT, change mode).
        # Other users see the UI in read-only mode.
        # timeout_min: minutes of inactivity after which the radio is auto-released.
        # last_activity: time of the active operator's last action (for auto-release).
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
        # Login rate limiting (brute-force protection) - see the _login_* methods
        self._login_fails_ip: dict = {}    # {ip: {fails: [ts], blocked_until: ts}}
        self._login_fails_user: dict = {}  # {username: {...}}
        # Background loops under SUPERVISION - if they crash, the supervisor
        # restarts them (with growing backoff). Without this, one
        # unexpected error kills the feature until the server is manually restarted.
        self._supervise(lambda: self._radio_lock_watchdog(), "radio_lock_watchdog")
        # Device watchdog: checks the radio/rotators/relays every 1h and
        # reconnects stuck COM ports (only when the radio isn't
        # transmitting and no one holds the lock - so as not to interrupt a QSO).
        self._supervise(lambda: self._device_watchdog(), "device_watchdog")
        # Event-loop lag detector — DIAGNOSTICS for latency spikes.
        # Measures whether asyncio is freezing up. When the loop stalls
        # longer than a threshold (something synchronous is blocking it),
        # it prints this to the log along with the block duration. Points
        # at the culprit behind RTT/audio spikes at low CPU usage.
        self._supervise(lambda: self._loop_lag_monitor(), "loop_lag_monitor")
        # Scope pump — sends the freshest waterfall frame at a steady
        # 15fps FROM THE ASYNCIO LOOP (not from the reader thread). The
        # reader only writes _latest_scope; this pump reads and broadcasts
        # it. This way the radio's scope stream does NOT synchronize with
        # the loop on every frame — which used to block asyncio for 100-500ms.
        self._supervise(lambda: self._scope_pump(), "scope_pump")
        # Update check: once at startup, in the background, admin-only notice.
        # Never blocks startup and stays silent on any network error.
        self._supervise(lambda: self._update_check_loop(), "update_check")

    async def _scope_pump(self):
        """Reads the freshest scope frame from the radio and sends it every
        ~66ms (15fps). Decouples the scope stream (reader thread) from the event loop."""
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
                        print(f"[looplag] Loop BLOCKED ~{_behind:.0f}ms "
                              f"(a real block, not idleness):", flush=True)
                        for _line in _stack[-5:]:
                            print("  " + _line.strip().replace("\n", " | "),
                                  flush=True)
                    except Exception as _e:
                        print(f"  (dump failed: {_e})", flush=True)

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
            print(f"[update] Newer version available: {latest} "
                  f"(you have {SERVER_VERSION}) — {url}", flush=True)
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

    def _migrate_plaintext_secrets(self):
        """One-time startup pass: encrypt any CloudLog/QRZ/HamQTH credentials
        still stored as plaintext from before encryption at rest was added
        (see crypto_secrets.py). Idempotent - encrypt_secret() is a no-op on
        values already carrying the enc1: prefix, so this is safe to run on
        every startup."""
        changed = False
        for u in self.users:
            cl = u.get("cloudlog")
            if cl:
                for key in ("apiKeyQso", "apiKeyRadio"):
                    val = cl.get(key, "")
                    if val:
                        enc = encrypt_secret(val)
                        if enc != val:
                            cl[key] = enc
                            changed = True
            cb = u.get("callbook")
            if cb:
                for key in ("qrzPassword", "hamqthPassword"):
                    val = cb.get(key, "")
                    if val:
                        enc = encrypt_secret(val)
                        if enc != val:
                            cb[key] = enc
                            changed = True
            dx = u.get("dxcluster")
            if dx:
                val = dx.get("password", "")
                if val:
                    enc = encrypt_secret(val)
                    if enc != val:
                        dx["password"] = enc
                        changed = True
        if changed:
            save_json(USR_F, self.users)
            print("[secrets] encrypted CloudLog/QRZ/HamQTH/DX-Cluster credentials in users.json", flush=True)

    def _has_perm(self, uid: str, role: str, key: str) -> bool:
        """Admin always has access. Otherwise check the granular permission
        (permissions[key] in users.json, set from the user-edit form).
        NOTE: the JWT only carries id/role/username/pw_ver (see jwt_sign in
        /api/auth/login) - it does NOT carry permissions, so the full,
        current user record must be re-read from self.users instead of
        relying on the decoded token (which could be stale after an admin
        changes permissions without the user logging in again)."""
        if role == "admin":
            return True
        u = self.find_user_by_id(uid)
        return bool((u or {}).get("permissions", {}).get(key))

    def find_user_by_email(self, email: str) -> dict | None:
        return next((u for u in self.users
                     if (u.get("email") or "").lower() == email.lower()), None)

    # ── Radio Lock helpers ────────────────────────────────────────────────────
    def _radio_lock_state(self) -> dict:
        """Serializable radio-lock state (for WS broadcast and the API)."""
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
        """List of currently connected users."""
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
        """Take over the radio for a user."""
        self.radio_lock.update({
            "user_id":       user["id"],
            "username":      user["username"],
            "callsign":      user.get("callsign", user["username"]),
            "locked_at":     time.time(),
            "last_activity": time.time(),
        })
        # Remove this user's request from the queue (if there was one)
        self.radio_requests.pop(user["id"], None)

    def _release_radio(self):
        """Release the radio."""
        # Whoever is releasing/losing the radio - also stop WSJT-X if they
        # owned it. Called WITHOUT await because _release_radio is sync
        # (also used from the watchdog). The actual stop happens asynchronously.
        prev_owner = self.radio_lock.get("user_id")
        if prev_owner and prev_owner == self._ft8_rx_owner_uid and self._ft8_rx_enabled:
            asyncio.ensure_future(self._stop_wsjtx_auto("oddanie radia"))
        self.radio_lock.update({
            "user_id": None, "username": None,
            "callsign": None, "locked_at": None, "last_activity": None,
        })

    async def _stop_wsjtx_auto(self, reason: str):
        """Automatically stop WSJT-X for the given reason.

        Called when:
        - The WSJT-X owner disconnected (WS close)
        - The owner released the radio (manually or via the idle watchdog)
        - The owner lost the radio via an admin FORCE
        - The owner logged out

        Ensures decoding doesn't keep running "in the background" when no
        one is controlling the radio, and doesn't block the audio TX
        pipeline for other users."""
        if not self._ft8_rx_enabled:
            return
        print(f"[ft8rx] AUTO-STOP: {reason} (owner={self._ft8_rx_owner_uid})", flush=True)
        self._ft8_rx_enabled = False
        self._ft8_rx_owner_uid = None
        if self.rust_audio:
            try:
                await self.rust_audio.ft8_enable_rx(False, self._ft8_decode_mode)
            except Exception as e:
                print(f"[ft8rx] auto-stop rust_audio error: {e}")
        # Broadcast so every UI knows WSJT-X has been stopped
        await self.hub.broadcast({"type": "ft8_rx_status", "enabled": False})
        await self.hub.broadcast({"type": "wsjtx_status", "running": False,
                                   "decoding": False, "transmit": False})
        # Informational toast (does it go on the 'ft8' channel
        # automatically? No - this is control-level info for everyone)
        await self.hub.broadcast({"type": "toast",
                                   "msg": f"🛑 WSJT-X zatrzymany automatycznie ({reason})",
                                   "level": "warn"})

    def touch_activity(self, user_id: str):
        """Update the active operator's last-activity timestamp."""
        if self._user_has_lock(user_id):
            self.radio_lock["last_activity"] = time.time()

    # NOTE: _start_tx_watchdog used to be DEFINED TWICE in this same class
    # (Python takes the second one - see the definition near
    # _feature_allowed below) - the first one, the only one that
    # broadcast tx_watchdog_start/countdown/cancel, was therefore
    # COMPLETELY dead, unreachable code. The frontend never listened for
    # these message types anyway (the #wj-tx-watchdog element in
    # index.html was equally dead), so it was removed with no loss of
    # functionality - the real PTT watchdog is the second definition,
    # works independently, and is left unchanged.

    async def _start_tune(self, duration_s: float, tone_hz: float = 1500.0):
        """Transmit a steady tone for duration_s seconds, for ATU tuning.

        Generates a PCM sine wave at tone_hz (default 1500Hz - the middle
        of the SSB band), 16-bit, mono, 12000 Hz sample rate. Sent through
        the existing audio TX pipeline. PTT is turned on directly via
        CI-V, turned off when finished. Actively checks self._tune_stop.
        """
        import math
        try:
            self._tune_stop = False
            print(f"[tune] START {tone_hz}Hz for {duration_s}s")
            await self.hub.broadcast({"type": "tune_status", "active": True,
                                      "duration": duration_s, "tone": tone_hz})

            # Turn on PTT
            self.rig.ptt = True
            if not self.rig.sim:
                try: await self.rig.set_ptt(True)
                except Exception as e: print(f"[tune] set_ptt error: {e}")
            await self.hub.broadcast({"type": "ptt", "ptt": True})

            # Generate the tone PCM and feed it through the audio stream
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
                    except Exception as e: print(f"[tune] feed error: {e}")
                await asyncio.sleep(chunk_size / sample_rate * 0.9)

            # Turn off PTT
            self.rig.ptt = False
            if not self.rig.sim:
                try: await self.rig.set_ptt(False)
                except Exception as e: print(f"[tune] set_ptt(False) error: {e}")
            await self.hub.broadcast({"type": "ptt", "ptt": False})
            await self.hub.broadcast({"type": "tune_status", "active": False})
            print(f"[tune] STOP (stop_flag={self._tune_stop})")
        except Exception as e:
            print(f"[tune] error: {e}")
            self.rig.ptt = False
            if not self.rig.sim:
                try: await self.rig.set_ptt(False)
                except: pass
            await self.hub.broadcast({"type": "ptt", "ptt": False})
            await self.hub.broadcast({"type": "tune_status", "active": False})

    async def _relay_connect_task(self):
        """Connect to the Arduino relay controller in the background."""
        if self.relay:
            await self.relay.connect()

    async def _relay_reconnect_task(self):
        """Disconnect and reconnect with the new configuration."""
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
                print(f"[relay] reconnect error: {e}")

    def _supervise(self, coro_factory, name: str, restart_delay: float = 5.0):
        """
        Run a background loop under supervision: if, despite internal
        try/except blocks, the task crashes (an unexpected exception, a
        bug), the supervisor RESTARTS it after restart_delay seconds.

        Without this, a single unexpected error kills the feature (e.g.
        the device watchdog or FT8 decoding) until the server is manually restarted.

        coro_factory: a zero-argument function that returns a coroutine
                      (not a coroutine itself!), e.g. lambda: self._device_watchdog()
        """
        async def _runner():
            _fails = 0
            while True:
                try:
                    await coro_factory()
                    # The loop ended normally (it shouldn't) - restart it
                    print(f"[supervisor] '{name}' ended on its own - restarting "
                          f"in {restart_delay}s", flush=True)
                except asyncio.CancelledError:
                    print(f"[supervisor] '{name}' cancelled (shutting down)", flush=True)
                    raise
                except Exception as e:
                    _fails += 1
                    print(f"[supervisor] '{name}' CRASHED ({_fails}x): "
                          f"{type(e).__name__}: {e}", flush=True)
                    import traceback as _tb
                    _tb.print_exc()
                    # Growing backoff on repeated failures (5s, 10s, 20s,
                    # ... max 300s) to avoid a crash loop
                    _delay = min(restart_delay * (2 ** min(_fails - 1, 6)), 300.0)
                    print(f"[supervisor] '{name}' restarting in {_delay:.0f}s",
                          flush=True)
                    await asyncio.sleep(_delay)
                    continue
                await asyncio.sleep(restart_delay)

        return asyncio.ensure_future(_runner())

    async def _device_watchdog(self):
        """
        Every 1h checks whether devices (radio, rotators, relays) respond
        on their COM port. If a port is stuck / a device doesn't respond -
        disconnects and reconnects (soft reset).

        SAFETY: doesn't touch anything when:
          - the radio is transmitting (PTT active) - we don't interrupt a transmission
          - someone holds radio_lock - we don't interrupt someone else's QSO
        In that case the check is deferred to the next cycle.

        The radio already has its own _reconnect_loop in civ.py (auto-
        recovers from SIM once the port frees up), so for the radio we
        only log its state and try to wake it up if it's in SIM despite an
        available port.
        """
        INTERVAL_S = 3600.0   # 1h
        # First check 5 min after startup (give time for initialization)
        await asyncio.sleep(300.0)

        while True:
            try:
                # ── Safety conditions ────────────────────────────────────
                ptt_active = bool(getattr(self.rig, "ptt", False))
                lock_held = self.radio_lock.get("user_id") is not None
                if ptt_active or lock_held:
                    reason = "PTT active" if ptt_active else "operator holding the radio"
                    print(f"[watchdog] skipping device check ({reason})",
                          flush=True)
                    await asyncio.sleep(INTERVAL_S)
                    continue

                print("[watchdog] checking devices...", flush=True)

                await self._watchdog_check_radio()
                await self._watchdog_check_rotators()
                await self._watchdog_check_relay()

            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[watchdog] general error: {e}", flush=True)

            await asyncio.sleep(INTERVAL_S)

    async def _watchdog_check_radio(self):
        """
        Radio: check whether CI-V responds. The radio has its own
        reconnect_loop in civ.py so here we only do diagnostics + force a
        retry if it's in SIM.
        """
        try:
            is_sim = bool(getattr(self.rig, "sim", False))
            has_port = getattr(self.rig, "_ser", None) is not None
            if is_sim:
                print("[watchdog] radio in SIMULATION - civ reconnect_loop "
                      "will try to recover the port automatically", flush=True)
                return
            if not has_port:
                print("[watchdog] radio: no open port", flush=True)
                return
            # Health check: ask for the frequency (command 0x03).
            # If the radio doesn't respond, get_freq returns None/raises.
            ok = False
            try:
                if hasattr(self.rig, "get_freq"):
                    # get_freq is async (CivRig) - called directly
                    f = await asyncio.wait_for(self.rig.get_freq(), timeout=5.0)
                    ok = f is not None and f > 0
            except Exception:
                ok = False
            if ok:
                print("[watchdog] radio OK (responds on CI-V)", flush=True)
            else:
                print("[watchdog] radio NOT RESPONDING - trying reconnect",
                      flush=True)
                # Close the port - civ's _reconnect_loop will reopen it
                try:
                    if hasattr(self.rig, "_ser") and self.rig._ser:
                        self.rig._ser.close()
                        self.rig._ser = None
                    if hasattr(self.rig, "_start_sim"):
                        self.rig._start_sim()  # this kicks off _reconnect_loop
                    print("[watchdog] radio: port closed, reconnect_loop "
                          "will try to recover", flush=True)
                except Exception as e:
                    print(f"[watchdog] radio reconnect error: {e}", flush=True)
        except Exception as e:
            print(f"[watchdog] radio check error: {e}", flush=True)

    async def _watchdog_check_rotators(self):
        """Rotators: check whether they respond to STATUS, reconnect if not."""
        rotators = getattr(self, "rotators", None)
        if not rotators:
            return
        for rot in rotators:
            try:
                if getattr(rot, "sim", False):
                    # In simulation - try connecting again (the port may have come back)
                    print(f"[watchdog] rotator {rot.name}: in simulation, "
                          f"trying to connect", flush=True)
                    await asyncio.to_thread(rot.connect)
                    continue
                if not getattr(rot, "connected", False):
                    print(f"[watchdog] rotator {rot.name}: disconnected, "
                          f"trying to connect", flush=True)
                    await asyncio.to_thread(rot.connect)
                    continue
                # Health check: read the position (a real query to the port)
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
                    print(f"[watchdog] rotator {rot.name} NOT RESPONDING - "
                          f"reconnecting", flush=True)
                    try:
                        await asyncio.to_thread(rot.close)
                    except Exception:
                        pass
                    await asyncio.sleep(1.0)
                    ok = await asyncio.to_thread(rot.connect)
                    print(f"[watchdog] rotator {rot.name} reconnect: "
                          f"{'OK' if ok else 'FAILED (simulation)'}", flush=True)
            except Exception as e:
                print(f"[watchdog] rotator {getattr(rot,'name','?')} error: {e}",
                      flush=True)

    async def _watchdog_check_relay(self):
        """Relays: check whether the Arduino responds to RPK, reconnect if not."""
        rcfg = self.cfg.get("relay", {})
        if not rcfg.get("enabled") or not rcfg.get("port"):
            return
        relay = getattr(self, "relay", None)
        if not relay:
            print("[watchdog] relay: no instance, trying to connect", flush=True)
            await self._relay_reconnect_task()
            return
        try:
            if not relay.is_connected():
                print("[watchdog] relay: disconnected, reconnecting", flush=True)
                await self._relay_reconnect_task()
                return
            # Health check: read the states (a real RPK query to the Arduino)
            ok = False
            try:
                states = await asyncio.wait_for(
                    relay.read_all_states(), timeout=5.0)
                ok = states is not None
            except Exception:
                ok = False
            if ok:
                print("[watchdog] relay OK (Arduino responds)", flush=True)
            else:
                print("[watchdog] relay NOT RESPONDING - reconnecting", flush=True)
                await self._relay_reconnect_task()
                if self.relay and self.relay.is_connected():
                    print("[watchdog] relay reconnect OK", flush=True)
                else:
                    print("[watchdog] relay reconnect FAILED", flush=True)
        except Exception as e:
            print(f"[watchdog] relay check error: {e}", flush=True)

    async def _radio_lock_watchdog(self):
        """
        Every 30s checks whether the active operator has exceeded the idle
        timeout. If so — automatically releases the radio and broadcasts the state.
        """
        await asyncio.sleep(5)   # give it a moment to initialize
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
                    print(f"[radio_lock] Auto-release: {uname} exceeded the {self.radio_lock['timeout_min']} min timeout")
            except Exception as e:
                print(f"[radio_lock] watchdog error: {e}")

    # ── SMTP — password reset ────────────────────────────────────────────────────
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
            print(f"[smtp] Email sent to {to_email}: {subject}")
            return True, None
        except Exception as e:
            print(f"[smtp] Send error to {to_email}: {e}")
            return False, str(e)

    async def _send_reset_email(self, to_email: str, username: str, token: str, base_url: str):
        """
        Send an email via SMTP. Returns (True, None) or (False, error_str).
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
            print(f"[smtp] Email sent to {to_email}")
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
        print(f"[rotator] {n} active" if n else "[rotator] none configured")

    def _check_pw_ver(self, payload: dict | None) -> dict | None:
        """
        Check whether the token has the current password version (pw_ver).
        A password change increments pw_ver in users.json, so old tokens
        (with a lower version) stop working - this invalidates sessions on
        all devices.

        Returns the payload if OK, or None if the token is stale/invalid.
        Tokens issued before pw_ver was introduced (missing field) are
        accepted only when the user also has no pw_ver (i.e. never changed
        their password).
        """
        if not payload:
            return None
        uid = payload.get("id") or payload.get("user_id")
        if not uid:
            return None
        u = next((x for x in self.users if x.get("id") == uid), None)
        if not u:
            return None  # user deleted
        token_ver = int(payload.get("pw_ver", 0))
        user_ver = int(u.get("pw_ver", 0))
        if token_ver != user_ver:
            print(f"[auth] token rejected: pw_ver={token_ver} != {user_ver} "
                  f"(user {u.get('username')!r} changed their password)", flush=True)
            return None
        return payload

    # ── Login rate limiting (brute-force protection) ──────────────────
    # The server is on the internet (duckdns) - bots scan and try to crack
    # passwords. We limit login attempts per IP and per username.
    #
    # Rules:
    #   - max 5 failed attempts from one IP within 5 minutes -> 15 min block
    #   - max 5 failed attempts for one username within 5 min -> 15 min block
    #     (protects a specific account even when the attack comes from many IPs)
    #   - a successful login clears the counter for that IP and user
    # Data is kept in memory (reset on restart) - enough for a club server,
    # no need for Redis.
    _LOGIN_MAX_FAILS = 5           # how many failed attempts before a block
    _LOGIN_WINDOW_S = 300.0        # attempt-counting window (5 min)
    _LOGIN_BLOCK_S = 900.0         # how long the block lasts (15 min)

    def _rl_key_state(self, store: dict, key: str) -> dict:
        st = store.get(key)
        if st is None:
            st = {"fails": [], "blocked_until": 0.0}
            store[key] = st
        return st

    def _login_is_blocked(self, ip: str, username: str) -> tuple[bool, int]:
        """Is login blocked? Returns (blocked, seconds_left)."""
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
        """Record a failed attempt. If the limit is exceeded - block."""
        now = time.time()
        for store, key, label in ((self._login_fails_ip, ip, "IP"),
                                  (self._login_fails_user, username, "user")):
            if not key:
                continue
            st = self._rl_key_state(store, key)
            # Remove old attempts outside the window
            st["fails"] = [t for t in st["fails"] if now - t < self._LOGIN_WINDOW_S]
            st["fails"].append(now)
            if len(st["fails"]) >= self._LOGIN_MAX_FAILS:
                st["blocked_until"] = now + self._LOGIN_BLOCK_S
                st["fails"] = []
                print(f"[auth] BLOCKING {label}={key!r} for "
                      f"{int(self._LOGIN_BLOCK_S/60)} min "
                      f"(too many failed login attempts)", flush=True)

    def _login_record_success(self, ip: str, username: str):
        """Successful login - clear the counters."""
        self._login_fails_ip.pop(ip, None)
        self._login_fails_user.pop(username, None)

    def _login_cleanup(self):
        """
        Remove old entries so the dicts don't grow without bound.
        STABILITY: under attack (millions of different usernames), just
        removing "empty" entries isn't enough - all of them have fresh
        attempts. So we additionally remove entries whose attempt window
        has already passed, and if the dict is still too big - hard-trim
        it to the most recent ones.
        """
        now = time.time()
        HARD_CAP = 500
        for store in (self._login_fails_ip, self._login_fails_user):
            # 1. Remove entries with no active block and no attempts in the window
            dead = []
            for k, st in store.items():
                st["fails"] = [t for t in st["fails"]
                               if now - t < self._LOGIN_WINDOW_S]
                if st["blocked_until"] < now and not st["fails"]:
                    dead.append(k)
            for k in dead:
                store.pop(k, None)
            # 2. Hard limit - if still too many (an attack), keep only
            #    active blocks + the most recent attempts
            if len(store) > HARD_CAP:
                blocked = {k: st for k, st in store.items()
                           if st["blocked_until"] > now}
                rest = [(k, st) for k, st in store.items()
                        if st["blocked_until"] <= now]
                # Most recent attempts at the end of the fails list
                rest.sort(key=lambda kv: (kv[1]["fails"][-1]
                                           if kv[1]["fails"] else 0),
                          reverse=True)
                keep = dict(rest[:max(0, HARD_CAP - len(blocked))])
                keep.update(blocked)  # blocks always stay
                store.clear()
                store.update(keep)
                print(f"[auth] cleanup: trimmed to {len(store)} entries "
                      f"(possible brute-force attack)", flush=True)

    # ── API ───────────────────────────────────────────────────────────────────
    async def api(self, method: str, path: str, body: dict,
                  user: dict | None, query: dict, client_ip: str = "") -> tuple[int, any]:
        p = path

        if p == "/api/auth/login" and method == "POST":
            un = body.get("username", "").lower()
            pw = body.get("password", "")

            # Rate limiting - check whether the IP/user is blocked
            blocked, secs_left = self._login_is_blocked(client_ip, un)
            if blocked:
                mins = max(1, secs_left // 60)
                print(f"[auth] login rejected (blocked) ip={client_ip} user={un!r}",
                      flush=True)
                return 429, {"error": f"Za duzo nieudanych prob. "
                                       f"Sprobuj ponownie za {mins} min."}

            u  = self.find_user(un)
            if not u or not u.get("active") or not verify_pw(pw, u.get("password", "")):
                self._login_record_fail(client_ip, un)
                # Sporadic cleanup of old entries. We check BOTH dicts -
                # an attacker could send millions of different usernames to
                # exhaust memory (_login_fails_user would grow unbounded).
                if (len(self._login_fails_ip) > 200
                        or len(self._login_fails_user) > 200):
                    self._login_cleanup()
                print(f"[auth] failed login ip={client_ip} user={un!r}", flush=True)
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
            # pw_ver = password version. A password change increments it in
            # users.json, invalidating all old tokens (they no longer match the new version).
            token = jwt_sign({"id": u["id"], "role": u["role"], "username": u["username"],
                              "pw_ver": int(u.get("pw_ver", 0))})
            # Force a change of the default password: when an admin logs in
            # still using the working default password (Admin1234!), the
            # frontend shows a password-change screen. CRITICAL for the
            # product - every club starts with the same default password,
            # so it MUST be changed on first login.
            must_change = (u.get("role") == "admin"
                           and verify_pw(ADMIN_PW, u.get("password", ""))
                           and not u.get("pw_changed"))
            return 200, {"ok": True, "token": token, "role": u["role"],
                         "username": u["username"],
                         "callsign": CALLSIGN, "locator": LOCATOR,
                         "must_change_password": must_change,
                         "first_run": FIRST_RUN}

        # ── Password reset: step 1 — send an email with a token ──────────────────
        if p == "/api/auth/reset-request" and method == "POST":
            email_or_user = body.get("email", "").strip().lower()
            # Look up by email or username
            u = (self.find_user_by_email(email_or_user) or
                 self.find_user(email_or_user))
            # Always respond "ok" — don't reveal whether the user exists
            if u and u.get("email"):
                from auth import make_reset_token
                token    = make_reset_token(u["id"], u["username"], u["email"])
                base_url = body.get("base_url", f"http://localhost:{PORT}")
                ok, err  = await self._send_reset_email(u["email"], u["username"], token, base_url)
                if not ok:
                    # Do NOT log the token itself — the log is a place a token
                    # could leak from, and it grants account takeover. Log only
                    # that delivery failed so the admin knows to check SMTP.
                    print(f"[auth] SMTP error sending reset for "
                          f"{u['username']!r}: {err} (token NOT written to the log)")
            return 200, {"ok": True, "message": "Jesli konto istnieje i ma email — link zostal wyslany"}

        # ── Password reset: step 2 — set the new password from the token ────────
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
            u["pw_ver"] = int(u.get("pw_ver", 0)) + 1  # invalidate old tokens
            save_json(USR_F, self.users)
            print(f"[auth] Password reset for: {u['username']} "
                  f"(pw_ver={u['pw_ver']}, old sessions invalidated)")
            return 200, {"ok": True, "message": "Haslo zostalo zmienione — mozesz sie zalogowac"}

        # ── Password reset by an admin (no email) ─────────────────────────────
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
            u["pw_ver"] = int(u.get("pw_ver", 0)) + 1  # invalidate the user's old tokens
            save_json(USR_F, self.users)
            print(f"[auth] Admin reset the password for {u['username']!r} "
                  f"(pw_ver={u['pw_ver']}, old sessions invalidated)", flush=True)
            return 200, {"ok": True}

        if p == "/api/config/lang" and method == "GET":
            # Public (no auth) - deliberately placed BEFORE the "if not
            # user" gate right below. Read by an inline script at the very
            # top of index.html, which runs for EVERY visitor including a
            # fresh browser that has never logged in yet (before the
            # client-side redirect to login.html even happens) - so this
            # can't require a token the visitor doesn't have. Lets i18n.js
            # pick the server-wide default language (set at install time -
            # see HAMCTRL-installer.iss / data.py::get_cfg) instead of
            # always starting in Polish. A per-browser choice saved in
            # localStorage always overrides this once someone actually
            # toggles the language via the UI.
            return 200, {"lang": self.cfg.get("lang", "pl")}

        if not user:
            return 401, {"error": "Wymagane logowanie"}

        role = user.get("role", "viewer")
        uid  = user.get("id", "")
        # FIX: same promotion as ws_handler (see the comment there) - the
        # JWT's role is frozen at login time, so granting the "admin"
        # granular permission afterward only satisfied _has_perm() checks,
        # not the many plain "role == 'admin'" checks throughout this file.
        # A user with the Admin-panel permission should BE an admin, not a
        # partial one.
        if role != "admin":
            _u_obj_perm = self.find_user_by_id(uid) or {}
            if _u_obj_perm.get("permissions", {}).get("admin"):
                role = "admin"

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
            # Mark that the password was changed from the default - disables
            # the forced password-change prompt on login (must_change_password).
            u_obj["pw_changed"] = True
            # Increment the password version - invalidates ALL old JWT
            # tokens (on other devices too). The user has to log in again
            # everywhere. Important when the password is changed because it leaked.
            u_obj["pw_ver"] = int(u_obj.get("pw_ver", 0)) + 1
            save_json(USR_F, self.users)
            print(f"[auth] password changed for {u_obj.get('username')!r}, "
                  f"pw_ver={u_obj['pw_ver']} (old tokens invalidated)", flush=True)
            # Generate a new token for the current session (so the user doesn't get logged out)
            new_token = jwt_sign({"id": u_obj["id"], "role": u_obj["role"],
                                   "username": u_obj["username"],
                                   "pw_ver": u_obj["pw_ver"]})
            return 200, {"ok": True, "token": new_token}

        if p == "/api/user/profile" and method == "POST":
            u_obj = self.find_user_by_id(uid)
            if not u_obj: return 404, {"error": "Brak konta"}
            # Field validation (defense against XSS - this data ends up in
            # HTML in the admin panel, the online-users list, etc.)
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
            # Validate the structure: preset (str) and bands (dict with 5 bands)
            preset = body.get("preset", "default")
            bands = body.get("bands", {})
            if not isinstance(preset, str) or not isinstance(bands, dict):
                return 400, {"error": "Nieprawidlowa struktura"}
            # Validate the band values (int/float, range -20..+15)
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
            # Config + list of ports to choose from (admin only)
            if role != "admin":
                return 403, {"error": "Tylko admin"}
            rcfg = self.cfg.get("relay", {})
            # Cache the port list for 30s - list_serial_ports() can be slow on Windows
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
            # Validate each relay
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
            # Restart the Arduino connection
            asyncio.ensure_future(self._relay_reconnect_task())
            return 200, {"ok": True}

        if p == "/api/relay/state" and method == "GET":
            # Available to all logged-in users - state + relays visible per user
            rcfg = self.cfg.get("relay", {})
            configured = rcfg.get("relays", [])
            u_obj = self.find_user_by_id(uid) or {}
            u_perms = u_obj.get("permissions", {})
            # Admin sees all of them, a regular user only the ones granted via permissions[relay_N]
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
            # User clicks a relay button
            if not self.relay or not self.relay.is_connected():
                return 503, {"error": "Kontroler przekaznikow niepodlaczony"}
            relay_id = int(body.get("id", -1))
            if not 0 <= relay_id < 8:
                return 400, {"error": "Nieprawidlowy id przekaznika"}
            # Check permissions
            u_obj = self.find_user_by_id(uid) or {}
            u_perms = u_obj.get("permissions", {})
            perm_key = f"relay_{relay_id}"
            if role != "admin" and not u_perms.get(perm_key, False):
                return 403, {"error": "Brak dostepu do tego przekaznika"}
            # Find this relay's configuration
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
                # Broadcast the off state after the pulse (simplified - delayed)
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
            # Don't return the password in plaintext (just a flag whether it's set)
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
            # Keep the old (already-encrypted) password if a new one wasn't
            # given (null = no change); a new password is encrypted before saving.
            if password is None:
                password = existing.get("password", "")
            else:
                password = encrypt_secret(password)
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
                cfg["login"], decrypt_secret(cfg.get("password", ""))
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
            # Send a spot to the DX cluster.
            # Command: DX <freq_khz> <call> <comment>
            # The cluster signs the spot with the user's own login
            # (per-user connection), so the spot goes out under the
            # callsign of whoever sent it.
            if not self.dxcluster:
                return 503, {"ok": False, "error": "DX Cluster niedostępny"}

            call    = str(body.get("call", "")).strip().upper()
            freq_hz = body.get("freq_hz", 0)
            comment = str(body.get("comment", "")).strip()

            # ── Validation (a spot goes out to the WHOLE WORLD - don't send junk) ────
            if not call or not re.match(r'^[A-Z0-9/]{3,16}$', call):
                return 400, {"ok": False,
                             "error": "Nieprawidłowy znak (litery, cyfry, /)"}
            try:
                freq_hz = int(float(freq_hz))
            except (ValueError, TypeError):
                return 400, {"ok": False, "error": "Nieprawidłowa częstotliwość"}
            # Reasonable range: 1.8 MHz - 1300 MHz
            if not (1_800_000 <= freq_hz <= 1_300_000_000):
                return 400, {"ok": False,
                             "error": "Częstotliwość poza zakresem (1.8 MHz – 1.3 GHz)"}
            # Comment: max 30 characters (most clusters' limit), no control
            # characters that could break the telnet protocol
            comment = re.sub(r'[\r\n\t]', ' ', comment)[:30].strip()

            # The cluster expects the frequency in kHz (e.g. 14074.0 for 14.074 MHz).
            # Given with one decimal digit - that's the convention.
            freq_khz = freq_hz / 1000.0
            khz_str = f"{freq_khz:.1f}".rstrip("0").rstrip(".")

            cmd = f"DX {khz_str} {call}"
            if comment:
                cmd += f" {comment}"

            ok = await self.dxcluster.send_command(uid, cmd)
            if ok:
                u = self.find_user_by_id(uid) or {}
                print(f"[dx] spot sent by {u.get('callsign') or u.get('username')}: "
                      f"{cmd}", flush=True)
                return 200, {"ok": True, "sent": cmd}
            return 503, {"ok": False,
                         "error": "Brak połączenia z klastrem — połącz się najpierw"}

        if p == "/api/dxcluster/history" and method == "GET":
            if not self.dxcluster:
                return 200, {"ok": True, "history": []}
            return 200, {"ok": True, "history": self.dxcluster.get_history(uid)}

        # ── Server status + diagnostics (admin) ─────────────────────────────
        if p == "/api/health" and method == "GET":
            # Lightweight status for the Settings tab ("System status"
            # panel). Public - this is just basic server health, no
            # sensitive data. Fields match what settings.js's loadStatus()
            # expects. (The 'node' field is kept for frontend compatibility
            # - a leftover from a Node.js template, we report the Python version.)
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
            # Detailed performance diagnostics - CPU, threads, loops, clients.
            # Helps assess whether the server keeps up with many users.
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
                    "impl": type(_loop).__name__,  # uvloop/winloop = faster
                    "active_tasks": len(_tasks),
                    # Names of the background loops (for diagnosing what's running)
                    "task_names": sorted({
                        (t.get_coro().__qualname__.split('.')[-1]
                         if hasattr(t, 'get_coro') and t.get_coro() else '?')
                        for t in _tasks
                    })[:20],
                }
            except Exception as e:
                out["event_loop"] = {"error": str(e)}

            # ── Python threads ─────────────────────────────────────────────────
            try:
                threads = _th.enumerate()
                out["threads"] = {
                    "count": len(threads),
                    "names": sorted(t.name for t in threads)[:20],
                }
            except Exception as e:
                out["threads"] = {"error": str(e)}

            # ── CPU / RAM / processes (psutil) ──────────────────────────────────
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
            # ── WS clients + channel subscriptions ──────────────────────────────
            try:
                _clients = getattr(self.hub, "_clients", set())
                _subs = getattr(self.hub, "_subs", {})
                # Count how many clients subscribe to each channel
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

            # ── Hot-path state (what's loading the CPU) ────────────────────────────────
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
            # Active radio from the rigs[] list (model/port/speed live
            # here, not in the flat cfg). The first entry = active radio.
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
            # CPU/RAM via psutil (if available)
            cpu_pct = None
            ram_pct = None
            ram_used_mb = None
            try:
                import psutil as _ps
                cpu_pct = _ps.cpu_percent(interval=None)   # non-blocking (see above)
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
                    # Model/port live in cfg["rigs"][0] (the radio list),
                    # not in the flat cfg["model"]. Read from the active
                    # radio, falling back to the old fields and the value
                    # actually used by the driver (self.rig.port/speed).
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

        # CI-V connection test — sends a test freq read
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

        # ── Config backup / restore (admin) ────────────────────────────
        if p == "/api/backup" and method == "GET":
            if role != "admin":
                return 403, {"error": "Tylko admin"}
            # Return config.json + users.json as one downloadable JSON
            # (user passwords included — this is a backup, the admin has access)
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
            data = body.get("backup", body)  # accept {backup:{...}} or directly
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
            }}

        if p == "/api/radio/state" and method == "GET":
            return 200, {**self._radio_lock_state(), "online": self._online_users_state()}

        if p == "/api/radio/lock" and method == "POST":
            """Take over the radio (if free, or if you're an admin)."""
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
            """Release the radio (active operator or admin only)."""
            if self.radio_lock["user_id"] != uid and role != "admin":
                return 403, {"error": "Nie masz aktywnej blokady radia"}
            released_by = self.radio_lock["username"] or user.get("username", "")
            self._release_radio()
            # Clear pending requests (same as force-release below) — once
            # the radio is free, everyone sees "TAKE TRX" directly anyway,
            # and stale requests would only leave the requester's "REQUEST
            # TRX" button permanently disabled (hasReq would never clear,
            # since _lock_radio() removes the entry ONLY for the user who
            # actually takes the radio, not for everyone who requested it).
            self.radio_requests.clear()
            await self.hub.broadcast({**self._radio_lock_state(), "online": self._online_users_state()})
            await self.hub.broadcast({"type": "toast",
                                      "message": f"✓ Radio zwolnione przez {released_by}"})
            return 200, {"ok": True}

        if p == "/api/radio/request" and method == "POST":
            """Send a radio request to the active operator."""
            if self._user_has_lock(uid):
                return 400, {"error": "Juz masz radio"}
            if not self.radio_lock["user_id"]:
                # Radio free — take it immediately
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
            # Notify the active operator
            await self.hub.broadcast({
                "type":     "radio_request_received",
                "from_uid": uid,
                "from_cs":  callsign,
                "message":  f"{callsign} prosi o dostep do radia",
            })
            await self.hub.broadcast({**self._radio_lock_state(), "online": self._online_users_state()})
            return 200, {"ok": True, "granted": False, "message": "Prosba wyslana"}

        if p == "/api/radio/cancel-request" and method == "POST":
            """Withdraw a radio request."""
            self.radio_requests.pop(uid, None)
            await self.hub.broadcast({**self._radio_lock_state(), "online": self._online_users_state()})
            return 200, {"ok": True}

        if p == "/api/radio/reject-request" and method == "POST":
            """The active operator (or admin) rejects someone else's radio request.

            The frontend (the "REJECT" button in _showRequestToast,
            index.html) used to ONLY remove the toast locally and never
            call any API — the request stayed in self.radio_requests
            FOREVER (nothing removed it except taking/releasing the radio
            or an admin force-release), so the requester's "REQUEST TRX"
            button stayed permanently disabled (_renderOpPanel:
            hasReq==True -> disabled)."""
            target_uid = str(body.get("uid", ""))
            if not target_uid:
                return 400, {"error": "Brak uid"}
            if role != "admin" and not self._user_has_lock(uid):
                return 403, {"error": "Tylko aktywny operator lub admin moze odrzucic prosbe"}
            req = self.radio_requests.pop(target_uid, None)
            if req is None:
                return 200, {"ok": True}  # already stale (e.g. withdrawn in the meantime)
            u_obj = self.find_user_by_id(uid) or {}
            by_callsign = u_obj.get("callsign", user.get("username", ""))
            await self.hub.broadcast({**self._radio_lock_state(), "online": self._online_users_state()})
            await self.hub.broadcast({
                "type":   "radio_request_rejected",
                "to_uid": target_uid,
                "by":     by_callsign,
            })
            return 200, {"ok": True}

        if p == "/api/radio/force-release" and method == "POST":
            """Admin: force-release the radio."""
            if role != "admin": return 403, {"error": "Tylko admin"}
            holder = self.radio_lock["username"] or "?"
            self._release_radio()
            self.radio_requests.clear()
            await self.hub.broadcast({**self._radio_lock_state(), "online": self._online_users_state()})
            await self.hub.broadcast({"type": "toast",
                                      "message": f"Admin wymusil zwolnienie radia (bylo: {holder})"})
            return 200, {"ok": True}

        if p == "/api/radio/timeout" and method == "POST":
            """Admin: change the idle timeout."""
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
            # Don't send the SMTP password to the frontend — just **** if it's set
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
            if body.get("password"):  # Only overwrite the password if a new one was given
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
            # Bands come from the connected radio's PROFILE, not a shared
            # list. This way an IC-9700 shows only 2m/70cm/23cm (no HF),
            # while an IC-7300 shows HF+6m+4m (no 2m). The admin picks the
            # profile via the radio model; when there's no profile,
            # get_civ_profile gives a safe fallback.
            try:
                from rigs import get_civ_profile
                _model = str(self.cfg.get("model") or self.cfg.get("rig_model") or "3073")
                _profile = get_civ_profile(_model)
                _pbands = _profile.get("bands", {})
            except Exception:
                _pbands = {}
            if _pbands:
                # profile format: name -> (min, max, def); the UI wants a min/max/def dict
                all_bands = {name: {'min': lo, 'max': hi, 'def': df}
                             for name, (lo, hi, df) in _pbands.items()}
            else:
                # Fallback (in case the profile has no bands) — the full list as before
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
            # filter out bands outside the profile (in case the config remembers older ones)
            enabled = [b for b in enabled if b in all_bands]
            return 200, {"allBands": all_bands, "enabledBands": enabled}

        if p == "/api/config/station" and method == "GET":
            # STATION locator — where the antenna is physically located.
            # Used to compute the rotor azimuth toward a correspondent (the
            # same for every user, since there's only one antenna). Defaults
            # from .env STATION_LOCATOR.
            return 200, {
                "stationLocator": (self.cfg.get("stationLocator")
                                   or LOCATOR or "").strip().upper(),
                "callsign": CALLSIGN,
            }

        if p == "/api/config/station" and method == "POST":
            if not self._has_perm(uid, role, "settings"): return 403, {"error": "Brak uprawnien (ustawienia serwera)"}
            _loc = str(body.get("stationLocator", "")).strip().upper()
            if _loc and not re.match(r"^[A-R]{2}\d{2}([A-X]{2})?$", _loc):
                return 400, {"error": "Zly format lokatora (np. JO72 lub JO72AB)"}
            self.cfg["stationLocator"] = _loc
            save_json(CFG_F, self.cfg)
            await self.hub.broadcast({"type": "init_patch",
                                       "stationLocator": _loc})
            print(f"[config] Station locator set: {_loc}", flush=True)
            return 200, {"ok": True, "stationLocator": _loc}

        if p == "/api/config/bands" and method == "POST":
            if not self._has_perm(uid, role, "settings"): return 403, {"error": "Brak uprawnien (ustawienia serwera)"}
            self.cfg["enabledBands"] = body.get("enabledBands", [])
            save_json(CFG_F, self.cfg)
            # Broadcast to clients so they refresh the band grid
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
            # Modes with filters — the admin can assign a preferred filter
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
            # Fallback — return the config without state (the server hasn't started yet)
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
            if not self._has_perm(uid, role, "settings"): return 403, {"error": "Brak uprawnien (ustawienia serwera)"}
            servers = body.get("servers", [])
            # Validate ports
            ports = [s.get("port", 4532+i) for i,s in enumerate(servers)]
            if len(set(ports)) != len(ports):
                return 400, {"error": "Porty muszą być unikalne"}
            for port in ports:
                if not (1024 <= port <= 65535):
                    return 400, {"error": f"Port {port} poza zakresem 1024-65535"}
            self.cfg["hamlibServers"] = servers
            save_json(CFG_F, self.cfg)
            # Restart if the manager is active
            mgr = getattr(self, 'hamlib', None)
            if mgr:
                try:
                    await mgr.restart()
                    return 200, {"ok": True, "message": "✓ Konfiguracja zapisana i serwery zrestartowane"}
                except Exception as e:
                    return 200, {"ok": True, "message": f"✓ Zapisano — restart wymagany ({e})"}
            return 200, {"ok": True, "message": "✓ Konfiguracja zapisana — zrestartuj serwer aby zastosować"}

        m = re.match(r"^/api/config/rig/(\d+)$", p)
        if m and method == "POST":
            if not self._has_perm(uid, role, "settings"): return 403, {"error": "Brak uprawnien (ustawienia serwera)"}
            rid = m.group(1)
            if not self.cfg.get("rigs"):
                self.cfg["rigs"] = []
            rigs = self.cfg["rigs"]
            existing = next((r for r in rigs if str(r.get("id")) == str(rid)), None)
            if existing:
                existing.update(body)
            else:
                rigs.append({**body, "id": rid})
            # Sync audio devices from the rig into the global audio config
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
            # Pick the backend by model: scope-capable -> direct CI-V (CivRig),
            # others -> rigctld (RigCAT). Swaps app.rig on the fly if needed.
            sel_model = str((body or {}).get("model") or "").strip()
            want_civ  = sel_model in SCOPE_MODELS
            is_civ    = isinstance(self.rig, CivRig)
            if want_civ != is_civ and sel_model:
                try: self.rig.close()
                except Exception as _ce: print(f"[rig] closing the old backend: {_ce}")
                # Give Windows a moment to actually release the serial port
                # (terminating the rigctld process isn't instant)
                await asyncio.sleep(0.5)
                self.rig = CivRig(self.cfg, self._rig_bcast, log=print) if want_civ else RigCAT()
                print(f"[rig] backend -> {'direct CI-V' if want_civ else 'rigctld'} (model {sel_model})")
                # IMPORTANT: ComBridgeWs held a reference to the OLD CivRig.
                # After creating the new one, the listener has to be
                # rewired, otherwise COM Bridge clients (CW Skimmer/HRD)
                # get no radio responses.
                if hasattr(self, 'com_bridge_ws') and self.com_bridge_ws:
                    new_civ = self.rig if isinstance(self.rig, CivRig) else None
                    self.com_bridge_ws.attach_civ_rig(new_civ)
                    print(f"[rig] com_bridge_ws rewired to new CivRig={new_civ is not None}")
            # Use values from the form (model/port/speed/civ) if given
            ok = await self.rig.connect(self.cfg, override=body or None)

            # Save the connection parameters to config.json — so after a
            # server restart, App.__init__ picks the right backend
            # (CivRig/RigCAT) and connect() connects to the same radio/port at startup.
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
                print(f"[rig] saved config for rig={rig_id}: {persist}")

            # After connecting, broadcast the full radio state so the panel
            # immediately shows the current freq/mode/S-meter (rig_poll only sends deltas).
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
                print(f"[rig] broadcast after connect: {_be}")
            await self._refresh_caps_cache()
            return 200, {"ok": ok, "sim": self.rig.sim,
                         "message": self.rig.last_msg,
                         "error": self.rig.last_err,
                         "rigctldPath": self.rig.rigctld_path,
                         "rigctldFound": self.rig.rigctld_found,
                         "freq": self.rig.freq, "mode": self.rig.mode,
                         "bandwidth": self.rig.bw}

        # ── Radio features panel (capabilities + admin whitelist) ──────────────
        if p == "/api/rig/features" and method == "GET":
            _admin_view = "admin" if self._has_perm(uid, role, "settings") else role
            return 200, await self._get_rig_features(_admin_view)

        if p == "/api/rig/features" and method == "POST":
            if not self._has_perm(uid, role, "settings"): return 403, {"error": "Brak uprawnien (ustawienia serwera)"}
            return 200, await self._set_rig_features(body or {})

        # ── CW Keyer (sending CW macros via CI-V cmd 17) ────────────────────
        # Frontend (cw.js) calls:
        #   POST /api/cw/send   {text, vars}  -> sends text as CW
        #   POST /api/cw/stop                 -> aborts sending (17 FF)
        #   GET  /api/cw/status                -> {method, capabilities}
        #   POST /api/cw/method {method}       -> sets the method (auto/cat/dtr/rts)
        #   POST /api/cw/dtr-port {port}       -> (placeholder, DTR not implemented)
        # Only the CAT method (CI-V cmd 17) is currently implemented - works
        # for the IC-7300/746 with no extra hardware. DTR/RTS keying needs
        # direct control of serial port lines (outside CI-V) and isn't
        # supported yet - the 'dtr'/'rts' method returns an error.
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
            # Fallback: if the method is dtr/rts but there's no separate port -> use auto (CI-V)
            if method_cw in ("dtr", "rts"):
                keyer_port = self.cfg.get("cwDtrPort", "")
                civ_port   = self.rig._port if hasattr(self.rig, "_port") else ""
                if not keyer_port or keyer_port == civ_port:
                    print(f"[cw] method {method_cw!r} has no separate port — falling back to CAT CI-V", flush=True)
                    method_cw = "auto"
            if method_cw in ("dtr", "rts"):
                # Check whether DTR/RTS has a SEPARATE port configured.
                # Using DTR/RTS on the same port as CI-V causes conflicts —
                # toggling the DTR/RTS line disrupts CI-V communication
                keyer_port = self.cfg.get("cwDtrPort", "")
                civ_port   = self.rig._port if hasattr(self.rig, "_port") else ""
                if not keyer_port or keyer_port == civ_port:
                    return 200, {"ok": False,
                        "error": "DTR/RTS keying wymaga osobnego portu COM. "
                                 "Skonfiguruj osobny port w Konfiguracja > CW Keyer, "
                                 "lub uzyj metody CAT CI-V (zalecane dla IC-7300/746)."}
            self._cw_tx_busy = True
            # Both fired BEFORE the actual sending starts (cw_sending used
            # to fire AFTER send_cw_message() already finished blocking -
            # see the FIX note below, the CAT/CI-V path awaits the full
            # transmission). cw_tx_start additionally drives the live
            # sidetone (public/js/cw_sidetone.js) - the radio does its own
            # Morse keying internally from the text we hand it over CI-V,
            # so the browser can't know the exact dit/dah timing and
            # instead synthesizes a same-content, same-WPM tone locally.
            await self.hub.broadcast({"type": "cw_tx_start", "text": text, "wpm": wpm})
            await self.hub.broadcast({"type": "cw_sending", "text": text,
                                       "method": method_cw, "wpm": wpm})
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
                            # Broadcast PTT ON before transmitting
                            self.rig.ptt = True
                            await self.hub.broadcast({"type": "ptt", "ptt": True})
                            await self.rig.send_cw_message(text)
                        except Exception as e:
                            _cw_err = str(e)
                            await self.hub.broadcast({"type": "cw_error", "error": str(e)})
                        finally:
                            # Broadcast PTT OFF after transmitting
                            self.rig.ptt = False
                            await self.hub.broadcast({"type": "ptt", "ptt": False})
                        # Return the result AFTER finally: an error only when one actually occurred.
                        if _cw_err is not None:
                            return 200, {"ok": False, "error": _cw_err}
            finally:
                self._cw_tx_busy = False

            if method_cw in ("dtr", "rts"):
                # send_cw_dtr_rts() is fire-and-forget (spawns a background
                # keyer thread and returns immediately) - there's no other
                # signal for when that thread actually finishes keying, so
                # this timer is the only way to know. Use the SAME exact
                # Morse timing as the rig (civ._cw_text_duration_s) instead
                # of the old flat len*10 guess that disagreed with the
                # actual transmission length.
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
            else:
                # FIX: the CAT/CI-V path above already BLOCKED (awaited)
                # until send_cw_message() fully finished - PTT on, keying,
                # PTT off, T/R recovery, all done by the time we're here.
                # The old code then waited an ADDITIONAL full duration_s
                # (recomputed with the same formula) before signaling
                # cw_done - reported live during a fast RST exchange as
                # having to wait for the "busy" lock to clear roughly
                # DOUBLE the real keying time before the next macro could
                # be sent. It's actually already done - say so immediately.
                await self.hub.broadcast({"type": "cw_done"})
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
            # Without this check, ANY logged-in user (even a viewer) could
            # turn the antenna via a direct API call, regardless of whether
            # the START/STOP buttons were grayed out in the UI (CSS
            # .radio-readonly). Same pattern as /api/cw/send: a viewer is
            # always blocked, everyone else must hold radio_lock (unless admin).
            if role == "viewer":
                return 403, {"error": "Brak uprawnien (rotator)"}
            if role != "admin" and not self._user_has_lock(uid):
                holder = self.radio_lock["callsign"] or self.radio_lock["username"] or ""
                msg_err = f"Rotator zablokowany — radio ma {holder}" if holder else "Rotator zablokowany — najpierw przejmij radio"
                return 403, {"error": msg_err}
            rot = self.get_rot(int(m.group(1)))
            if not rot: return 404, {"error": "Rotator nie znaleziony"}
            rot.go_to(float(body.get("az", 0)))
            return 200, {"ok": True}

        m = re.match(r"^/api/rotator/(\d+)/stop$", p)
        if m and method == "POST":
            # Same as /position above — see the comment there.
            if role == "viewer":
                return 403, {"error": "Brak uprawnien (rotator)"}
            if role != "admin" and not self._user_has_lock(uid):
                holder = self.radio_lock["callsign"] or self.radio_lock["username"] or ""
                msg_err = f"Rotator zablokowany — radio ma {holder}" if holder else "Rotator zablokowany — najpierw przejmij radio"
                return 403, {"error": msg_err}
            rot = self.get_rot(int(m.group(1)))
            if not rot: return 404, {"error": "Rotator nie znaleziony"}
            rot.stop()
            return 200, {"ok": True}

        m = re.match(r"^/api/rotator/(\d+)/test$", p)
        if m and method == "POST":
            rot = self.get_rot(int(m.group(1)))
            if not rot:
                return 200, {"testOk": False, "testMsg": "Rotator nie znaleziony"}
            # REAL connection test: try connecting and reading the position
            # from the COM port. The previous version only returned the
            # stored state (and in the wrong fields — hence 'undefined' in
            # the UI). Now we actively poll the rotor to confirm the port responds.
            driver = getattr(rot, "model", "") or getattr(rot, "driver_type", "") or "?"
            _was_sim_before = getattr(rot, "sim", False)
            try:
                # Try connecting if not already connected. NOTE: the
                # rotor's connect() does NOT raise on error - it silently
                # sets sim=True and returns False. So we must check the
                # RESULT, otherwise a dead port would masquerade as a
                # successful "simulation".
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
                    # connect() returned False and the rotor fell back to sim => the port is dead
                    if not ok and getattr(rot, "sim", False) and not _was_sim_before:
                        _err = getattr(rot, "last_err", "") or \
                               f"Port {getattr(rot, 'port', '?')} nie odpowiada"
                        return 200, {"testOk": False, "driverType": driver,
                                     "testMsg": f"Połączenie nieudane: {_err}"}
                # Read the position from the port — this confirms the
                # rotor is responding. _read_pos blocks on the serial port,
                # so it's run in a thread with a timeout (like the
                # watchdog), so a dead port doesn't hang the server.
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
                # Real simulation (configured from the start), not an emergency fallback
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
            if not self._has_perm(uid, role, "settings"): return 403, {"error": "Brak uprawnien (ustawienia serwera)"}
            self.cfg["rotators"] = body.get("rotators", [])
            save_json(CFG_F, self.cfg)
            self.init_rotators()
            return 200, {"ok": True, "count": len(self.rotators)}

        if p == "/api/users" and method == "GET":
            if role != "admin": return 403, {"error": "Tylko admin"}
            # Return the full set of editable profile fields - otherwise
            # the admin doesn't see the current values when opening the
            # user editor, and their changes "reset" fields to empty.
            # The password (hash) isn't returned, for security.
            return 200, [{"id": u["id"], "username": u["username"],
                          "callsign":    u.get("callsign", u["username"]),
                          "name":        u.get("name", ""),
                          "email":       u.get("email", ""),
                          "locator":     u.get("locator", ""),
                          "role":        u["role"],
                          "active":      u.get("active", True),
                          "permissions": u.get("permissions", {}),
                          }
                         for u in self.users]

        if p == "/api/users" and method == "POST":
            if role != "admin": return 403, {"error": "Tylko admin"}
            # Validation - username and callsign only allow safe characters
            # (no < > " etc.), so HTML/JS can't be injected (defense
            # against XSS at the source, independent of escaping in the UI).
            _un = body.get("username", "").strip()
            if not _un or len(_un) > 32 or not re.match(r'^[A-Za-z0-9_.\-]+$', _un):
                return 400, {"error": "Nazwa uzytkownika: 1-32 znakow, tylko litery, cyfry, . _ -"}
            _cs = body.get("callsign", "").strip().upper()
            if _cs and not re.match(r'^[A-Z0-9/]{1,16}$', _cs):
                return 400, {"error": "Znak wywolawczy: tylko litery, cyfry i / (max 16)"}
            _loc = body.get("locator", "").strip().upper()
            if _loc and not re.match(r'^[A-Z0-9]{1,8}$', _loc):
                return 400, {"error": "Lokator: tylko litery i cyfry (max 8)"}
            # name and email - truncate length and strip HTML-unsafe characters
            _nm = body.get("name", "").strip()[:64]
            _nm = re.sub(r'[<>"\']', '', _nm)
            _role = body.get("role", "viewer")
            if _role not in ("admin", "operator", "viewer"):
                return 400, {"error": "Nieprawidlowa rola (admin/operator/viewer)"}
            new_u = {"id": str(int(time.time()*1000)),
                     "username": _un,
                     "password": hash_pw_secure(body.get("password", "changeme")),
                     "role": _role, "active": True,
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

            # Nothing used to protect against deleting/demoting/
            # deactivating the LAST active admin - such a request would
            # succeed and leave the whole system with no one able to
            # manage anything (recovery would require manually editing
            # users.json on the server disk). The frontend hides the
            # DELETE button for your OWN account (see renderUsers in
            # admin.js), but that's UI only - nothing stopped deleting
            # ANOTHER admin if it was the only one, or demoting/disabling your own account.
            def _other_active_admins():
                return sum(1 for _u in self.users
                           if _u["id"] != target_uid and _u.get("role") == "admin"
                           and _u.get("active", True))

            if method == "DELETE":
                _target = self.find_user_by_id(target_uid)
                if (_target and _target.get("role") == "admin"
                        and _target.get("active", True) and _other_active_admins() == 0):
                    return 400, {"error": "Nie można usunąć ostatniego aktywnego admina"}
                self.users = [u for u in self.users if u["id"] != target_uid]
                save_json(USR_F, self.users)
                return 200, {"ok": True}
            u = self.find_user_by_id(target_uid)
            if not u: return 404, {"error": "Nie znaleziono"}
            _is_last_admin = u.get("role") == "admin" and u.get("active", True) and _other_active_admins() == 0
            # Full set of editable fields - kept in sync with the user
            # profile (the user edits via /api/user/profile, the admin via
            # here - both write to the same fields in users.json).
            if "password"    in body and body["password"]:
                u["password"] = hash_pw_secure(body["password"])
            if "role"        in body:
                _new_role = body["role"]
                if _new_role not in ("admin", "operator", "viewer"):
                    return 400, {"error": "Nieprawidlowa rola (admin/operator/viewer)"}
                if _is_last_admin and _new_role != "admin":
                    return 400, {"error": "Nie można odebrać roli admina ostatniemu aktywnemu adminowi"}
                u["role"] = _new_role
            if "active"      in body:
                _new_active = bool(body["active"])
                if _is_last_admin and not _new_active:
                    return 400, {"error": "Nie można dezaktywować ostatniego aktywnego admina"}
                u["active"] = _new_active
            if "name"        in body: u["name"]        = (body["name"] or "").strip()
            if "callsign"    in body: u["callsign"]    = (body["callsign"] or "").strip().upper()
            if "locator"     in body: u["locator"]     = (body["locator"] or "").strip().upper()
            if "email"       in body: u["email"]       = (body["email"] or "").strip()
            if "permissions" in body: u["permissions"] = body["permissions"]
            save_json(USR_F, self.users)
            return 200, {"ok": True}

        if p.startswith("/api/scope"):
            # Scope only available in direct CI-V mode (a radio with a spectrum scope).
            if not isinstance(self.rig, CivRig):
                return 200, {"ok": False, "sim": True, "running": False,
                             "message": "Scope dostępny tylko dla radia ze scope (IC-7300/7610/705...) "
                                        "w trybie bezpośrednim CI-V. Wybierz taki model i połącz."}
            if p.endswith("/stop"):
                return 200, self.rig.scope_stop()
            if p.endswith("/span") and method == "POST":
                # Change the waterfall span (the IC-7300 supports 2.5/5/10/25/50/100/250/500 kHz)
                try:
                    span_hz = int(body.get("span_hz", 25000))
                except (TypeError, ValueError):
                    return 400, {"ok": False, "error": "Nieprawidlowa wartosc span_hz"}
                if hasattr(self.rig, 'set_scope_span'):
                    try:
                        # set_scope_span waits (blocking, up to 0.4s) for
                        # the radio's own ACK/NG - run off the event loop
                        # thread, same as every other rig set_* call.
                        ok = await asyncio.get_running_loop().run_in_executor(
                            None, self.rig.set_scope_span, span_hz)
                        if ok:
                            return 200, {"ok": True, "span_hz": span_hz}
                        return 400, {"ok": False, "error": f"Radio nie potwierdzil zmiany spanu na {span_hz} Hz "
                                                            f"(zobacz log [civ] scope span)"}
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
            # get_config() returns the WHOLE config, including the
            # Cloudflare Tunnel token and the DuckDNS token in plaintext
            # (see TunnelManager._cfg / save_config in tunnel_manager.py -
            # no redaction here like e.g. /api/dxcluster/config, which only
            # sends has_password). The whole INTERNET tab is
            # data-perm="admin" in index.html, but that only HIDES the tab
            # button in the UI - the real enforcement MUST be here. Without
            # this gate, any logged-in viewer/operator could read these
            # tokens via fetch('/api/tunnel/config') in the browser console
            # (full control over the Cloudflare tunnel / ability to change
            # the DuckDNS DNS record).
            if role != "admin": return 403, {"error": "Tylko admin"}
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
            print("[webapp] install-certbot endpoint called", flush=True)
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
            self._stop_cq_calling()  # stop periodic CQ
            # See the identical block/comment in WS "ft8_tx_stop" above —
            # the frontend's haltTx() sends BOTH (WS + this REST call) on
            # every HALT click, so the same fix has to be here too.
            if self._qso_engine.is_active():
                print(f"[autoqso] HALT: aborting QSO with {self._qso_engine.partner_call}")
                self._qso_engine.abort_qso()
                self._qso_period_locked = False
                # Invalidate ANY already-scheduled (in-flight, waiting for
                # its window) automatic transmission to this partner -
                # without this the stale-TX-guard in _ft8_tx_sequence_inner
                # had no way to know this was a REAL abort, not just "a
                # newer action was scheduled" (see the comment at
                # _autoqso_tx_seq in _send_auto_tx).
                self._autoqso_tx_seq += 1
                await self.hub.broadcast({"type": "auto_qso_status",
                                           "state": self._qso_engine.state,
                                           "partner": None})
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
            # txVolume is held by Python (config.json), NOT Rust. The Rust
            # status doesn't know this value -> the UI reading status from
            # Rust showed a stale/default one ("the backend shows something
            # else"). So we ALWAYS inject txVolume (and the devices) from
            # the Python config into the returned status.
            _py_audio = self.cfg.get("audio", {})
            _py_txvol = float(_py_audio.get("txVolume", 4.0))
            _py_txvol_ssb = float(_py_audio.get("txVolumeSsb", 1.0))
            # Devices ALWAYS come from the Python config (not from Rust/not
            # from self.audio) — Python is the source of truth for saved
            # settings, in BOTH branches below. FIX: the fallback branch
            # (Rust unavailable/error, e.g. right after saving a device
            # while Rust is restarting the RX stream) had no "tx_device"
            # at all, and took "rx_device" from self.audio.rx_device — an
            # attribute of Python's OWN, UNRELATED capture (the CW
            # decoder), not from the saved configuration. On refresh at
            # that moment the UI got empty/wrong devices and visually
            # "forgot" the selection despite the correct value being saved
            # in config.json.
            _rxd = _py_audio.get("rxDevice", "")
            _txd = _py_audio.get("txDevice", "")
            if rust and rust._connected:
                try:
                    status = await rust.get_status()
                    if isinstance(status, dict) and "error" not in status:
                        if _rxd: status["rx_device"] = _rxd
                        if _txd: status["tx_device"] = _txd
                        status["txVolume"] = _py_txvol       # overwrite with the value from config.json (FT8/FT4)
                        status["txVolumeSsb"] = _py_txvol_ssb  # separate multiplier for the SSB microphone
                        return 200, status
                except Exception as e:
                    print(f"[audio] Rust status error: {e}", flush=True)
            _st = self.audio.get_status()
            if isinstance(_st, dict):
                if _rxd: _st["rx_device"] = _rxd
                if _txd: _st["tx_device"] = _txd
                _st["txVolume"] = _py_txvol
                _st["txVolumeSsb"] = _py_txvol_ssb
            return 200, _st

        if p == "/api/audio/detect" and method == "GET":
            # Return the radio-card auto-detect status.
            # Doesn't trigger a new detection - this is the cache from init.
            if not self._has_perm(uid, role, "settings"): return 403, {"error": "Brak uprawnien (ustawienia serwera)"}
            return 200, {
                "ok": True,
                "auto_enabled": self._audio_auto,
                "detection":    self._audio_detection,
                "current":      self.cfg.get("audio", {}),
            }

        if p == "/api/audio/detect" and method == "POST":
            # Force a fresh detection (e.g. after plugging in the radio after the server started)
            if not self._has_perm(uid, role, "settings"): return 403, {"error": "Brak uprawnien (ustawienia serwera)"}
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

        # ── CI-V TCP Bridge (for legacy software via com0com) ─────────────────
        # ── COM Bridge WS - per-user COM port configuration ────────────────
        if p == "/api/com/config" and method == "GET":
            # Return the current user's port configuration
            # The JWT payload uses 'id' (not 'user_id') - consistent with the rest of the code
            if not user:
                return 401, {'error': 'not authenticated'}
            uid = user.get('id') or user.get('user_id')  # fallback for old tokens
            u = next((x for x in self.users if x.get('id') == uid), None)
            if not u:
                return 404, {'error': 'user not found'}
            ports = u.get('com_bridge', {}).get('ports', [])
            return 200, {'ok': True, 'ports': ports}

        if p == "/api/com/config" and method == "POST":
            # Save the current user's port configuration
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
            # Validate each port
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
            # Stats of connected clients (for the admin)
            if role != "admin":
                return 403, {'error': 'tylko admin'}
            return 200, self.com_bridge_ws.get_stats()

        # ── Debug endpoint for diagnosing subscriptions (available to everyone)
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
            my_role = role if user else "?"  # already promoted for the "admin" permission - see the fix near line 1793
            return 200, {
                "ok": True,
                "my_uid": my_uid,
                "my_role": my_role,
                "ft8_rx_enabled": self._ft8_rx_enabled,
                "ft8_rx_owner_uid": self._ft8_rx_owner_uid,
                "clients": clients_info,
            }

        # ── CloudLog / WaveLog API — PER-USER settings ──────────────
        # Every user has their own API keys, URL, and station — saved in
        # the user's profile (users.json), not in the global cfg.
        if p == "/api/cloudlog/config" and method == "GET":
            if not user:
                return 401, {"error": "Brak autoryzacji"}
            u = self.find_user_by_id(user["id"])
            cl = dict((u or {}).get("cloudlog", {}))
            for key in ("apiKeyQso", "apiKeyRadio"):
                if cl.get(key):
                    cl[key] = decrypt_secret(cl[key])
            return 200, cl

        if p == "/api/cloudlog/config" and method == "POST":
            if not user:
                return 401, {"error": "Brak autoryzacji"}
            u = self.find_user_by_id(user["id"])
            if not u:
                return 404, {"error": "Uzytkownik nie istnieje"}
            u["cloudlog"] = {
                "url":          body.get("url", "").strip(),
                "apiKeyQso":    encrypt_secret(body.get("apiKeyQso", "").strip()),
                "apiKeyRadio":  encrypt_secret(body.get("apiKeyRadio", "").strip()),
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
            # If the user typed an address with /index.php - don't duplicate it
            base = url[:-10].rstrip("/") if url.endswith("/index.php") else url
            try:
                # Cloudlog: API key in the URL, station_info endpoint.
                # (user_info with an X-Auth-Key header DOESN'T EXIST -> 404)
                # Returns the user's list of station profiles.
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
                            # We expect a list of station profiles
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
            # Send the current frequency and mode to CloudLog/WaveLog
            # NOTE: without this, the endpoint took 'url' directly from the
            # body and POSTed it externally with NO authorization at all —
            # the server acting as an open proxy (SSRF) for anyone who hits
            # the server's address, even without a HAMCTRL account.
            # /config and /test already had this check, it was missing here.
            if not user:
                return 401, {"error": "Brak autoryzacji"}
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
                # Format per the Cloudlog API/Radio documentation:
                # {key, radio, frequency (Hz), mode, timestamp "YYYY/MM/DD HH:MM"}
                # station_id is NOT a field of this endpoint (removed).
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
            # Send a QSO to CloudLog/WaveLog (ADIF - see qso_to_adif)
            # Same missing-authorization issue as /api/cloudlog/radio above — added.
            if not user:
                return 401, {"error": "Brak autoryzacji"}
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

        # ── Callbook (QRZ.com / HamQTH.com) — callsign lookup ──────────
        # Each user has their own login credentials (see callbook.py). Done
        # server-side: neither service has CORS, and we don't want passwords in JS.
        if p == "/api/callbook/config" and method == "GET":
            if not user: return 401, {"error": "Brak autoryzacji"}
            u = self.find_user_by_id(user["id"])
            cfg = dict((u or {}).get("callbook", {}))
            for key in ("qrzPassword", "hamqthPassword"):
                if cfg.get(key):
                    cfg[key] = decrypt_secret(cfg[key])
            return 200, cfg

        if p == "/api/callbook/config" and method == "POST":
            if not user: return 401, {"error": "Brak autoryzacji"}
            u = self.find_user_by_id(user["id"])
            if not u: return 404, {"error": "Uzytkownik nie istnieje"}
            u["callbook"] = {
                "qrzUsername":    body.get("qrzUsername", "").strip(),
                "qrzPassword":    encrypt_secret(body.get("qrzPassword", "").strip()),
                "hamqthUsername": body.get("hamqthUsername", "").strip(),
                "hamqthPassword": encrypt_secret(body.get("hamqthPassword", "").strip()),
            }
            save_json(USR_F, self.users)
            return 200, {"ok": True}

        if p == "/api/callbook/test" and method == "POST":
            if not user: return 401, {"error": "Brak autoryzacji"}
            service  = body.get("service", "")
            username = body.get("username", "")
            password = body.get("password", "")
            if service not in ("qrz", "hamqth") or not username or not password:
                return 400, {"ok": False, "error": "Brak danych"}
            res = await callbook.test_connection(service, username, password, uid)
            if not res.get("ok"):
                res["error"] = res.get("error") or "Blad logowania - sprawdz dane"
            return 200, res

        if p == "/api/callbook/lookup" and method == "GET":
            if not user: return 401, {"error": "Brak autoryzacji"}
            call = query.get("call", "")
            if isinstance(call, list): call = call[0] if call else ""
            if not call:
                return 400, {"ok": False, "error": "Brak znaku"}
            u = self.find_user_by_id(uid) or {}
            cb = u.get("callbook", {})
            qrz_creds = ((cb.get("qrzUsername"), decrypt_secret(cb.get("qrzPassword")))
                         if cb.get("qrzUsername") and cb.get("qrzPassword") else None)
            hamqth_creds = ((cb.get("hamqthUsername"), decrypt_secret(cb.get("hamqthPassword")))
                            if cb.get("hamqthUsername") and cb.get("hamqthPassword") else None)
            if not qrz_creds and not hamqth_creds:
                return 200, {"ok": False, "error": "Skonfiguruj QRZ.com lub HamQTH w USTAWIENIACH"}
            res = await callbook.lookup(call, uid, qrz_creds, hamqth_creds)
            if not res:
                return 200, {"ok": False, "error": "Nie znaleziono znaku"}
            return 200, {"ok": True, **res}

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

        # ── QSO Log API ──────────────────────────────────────────────────────────
        is_admin_user = (role == "admin")

        if p == "/api/qsolog/worked_before" and method == "GET":
            # Check whether a call has already been worked. Optional: band/mode.
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
            # Admin can delete any user's log, a regular user only their own
            if target_uid != uid and not is_admin_user:
                return 403, {"error": "Brak uprawnień"}
            # Deleting thousands of QSOs can take a while - don't block the event loop
            count = await asyncio.to_thread(qso_db.delete_all, target_uid)
            return 200, {"ok": True, "count": count}


        if p == "/api/debug/qso_users" and method == "GET":
            if not is_admin_user: return 403, {}
            conn = qso_db._get_conn()
            rows = conn.execute("SELECT user_id, COUNT(*) as cnt FROM qso GROUP BY user_id").fetchall()
            return 200, {"users": [{"user_id": r[0], "count": r[1]} for r in rows]}

        if p == "/api/qsolog" and method == "GET":
            try:
                # MultiDict — take the first values
                filters = {k: (v[0] if isinstance(v, list) else v)
                           for k, v in query.items()}
                filter_uid = filters.pop("user_id", None) or None
                # The list can return thousands of QSOs - offload to a thread so as not to block
                result = await asyncio.to_thread(
                    qso_db.list_qsos, uid, is_admin=is_admin_user,
                    filter_uid=filter_uid, **filters)
                return 200, result
            except Exception as e:
                import traceback; traceback.print_exc()
                return 500, {"error": str(e)}

        if p == "/api/qsolog/bulk" and method == "POST":
            # Bulk import of many QSOs at once
            qsos_list = body.get("qsos", [])
            if not qsos_list:
                return 400, {"ok": False, "error": "Brak QSO"}
            try:
                # STABILITY: an ADIF import with thousands of QSOs takes
                # seconds. Calling it synchronously would FREEZE the event
                # loop - every user would stall, WebSockets would time out.
                # Run it in a thread.
                result = await asyncio.to_thread(
                    qso_db.add_qsos_bulk, uid, qsos_list)
                return 200, {"ok": True, "inserted": result["inserted"],
                             "skipped": result["skipped"],
                             "duplicates": result.get("duplicates", 0)}
            except Exception as e:
                import traceback; traceback.print_exc()
                return 500, {"ok": False, "error": str(e)}

        if p == "/api/qsolog" and method == "POST":
            # Add a new QSO
            try:
                body["source"] = body.get("source", "manual")
                # FIX: my_call (-> ADIF OPERATOR/STATION_CALLSIGN) used to
                # come ONLY from the frontend (S?.callsign, i.e.
                # window.AppState.callsign, set async from /api/auth/me).
                # Reported live: QSOs logged via the quick-log widget
                # reached CloudLog with NO operator/station callsign at
                # all, even though the user's profile definitely had one
                # set — confirmed by exporting the QSO to ADIF and seeing
                # no STATION_CALLSIGN/OPERATOR tag. Rather than chase the
                # exact frontend timing (some page/reconnect path left
                # AppState.callsign empty while window.CurrentUser was
                # already populated - the two aren't kept in sync the same
                # way), the backend now falls back to the AUTHENTICATED
                # user's own profile callsign whenever the request didn't
                # supply one - this is the actual source of truth anyway.
                if not (body.get("my_call") or "").strip():
                    _u_obj = self.find_user_by_id(uid)
                    body["my_call"] = ((_u_obj or {}).get("callsign")
                                        or user.get("username", ""))
                qso = qso_db.add_qso(uid, body)
                # Push to CloudLog if configured
                asyncio.ensure_future(self._cloudlog_push_qso(uid, qso))
                # Live-update the automation mini-log (WSJT-X page) - the
                # same broadcast as on auto-save from the automation (see
                # "QSO SAVED to the log" in _process_auto_qso) - one
                # message type for both save paths (manual and automatic).
                await self.hub.broadcast({"type": "qso_logged", "qso": qso})
                return 200, {"ok": True, "id": qso["id"]}
            except ValueError as e:
                return 400, {"ok": False, "error": str(e)}
            except Exception as e:
                import traceback; traceback.print_exc()
                return 500, {"ok": False, "error": str(e)}

        if p.startswith("/api/deepcw/capture") and method == "POST":
            # Diagnostics: record a dozen or so seconds of audio exactly as
            # it's fed to the model (after resampling and the filter).
            if deepcw_engine is None:
                return 503, {"error": "Silnik DeepCW niedostepny"}
            _sec = float(body.get("seconds", 15))
            _path = deepcw_engine.start_capture(_sec)
            return 200, {"ok": True, "path": _path, "seconds": _sec}

        if p.startswith("/api/deepcw/capture_file") and method == "GET":
            # Download the recorded file for listening back
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
            # Download the ONNX model (~15 MB) from the repository — a
            # network operation that writes to the data directory, so it
            # requires the "server settings" permission (not just the admin role).
            if not self._has_perm(uid, role, "settings"): return 403, {"error": "Brak uprawnien (ustawienia serwera)"}
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
            # Pool of known calls for browser-side HIGHLIGHTING. Fed from
            # FT8 decodes + the QSO log; the frontend adds its own DX
            # cluster spots (each user has their own) and already-worked calls.
            if deepcw_engine is None:
                return 200, {"calls": []}
            try:
                # Add calls from the operator's log (one-off, cheap)
                deepcw_engine.add_known_calls(
                    qso_db.worked_calls(uid, is_admin=is_admin_user))
            except Exception:
                pass
            return 200, {"calls": sorted(deepcw_engine._known_calls)}

        if p.startswith("/api/qsolog/calls") and method == "GET":
            # Lightweight list of UNIQUE calls from the log — for marking
            # worked stations in Band Activity. Replaces fetching the full
            # QSO list (capped at 200 entries), which meant older contacts
            # weren't marked as worked.
            try:
                # worked_calls does a SELECT DISTINCT over the whole QSO
                # table — this was blocking the event loop for 100-150ms
                # (found via the looplag detector: the stack pointed at
                # qso_db.worked_calls). The query is offloaded to a thread
                # so SQLite doesn't freeze asyncio (audio, pings).
                calls = await asyncio.to_thread(
                    qso_db.worked_calls, uid, is_admin_user)
                return 200, {"calls": calls}
            except Exception as e:
                return 500, {"error": str(e)}

        if p.startswith("/api/qsolog/export") and method == "GET":
            # Export ADIF/CSV/EDI
            # NOTE: query arrives as {key: [value]} (lists). We must
            # extract single values, otherwise fmt=['adi'] != 'adi' and the
            # endpoint returned 400 "Nieznany format". Filters must also be
            # strings, not lists (otherwise export_adif gets the wrong types).
            def _first(v):
                return v[0] if isinstance(v, list) else v
            _raw = {k: _first(v) for k, v in query.items()}
            fmt = _raw.pop("format", "adi")
            # Pass only KNOWN filters into the export - otherwise an
            # unexpected query param (token, cache-buster, user_id vs
            # filter_uid, etc.) flows through **filters into list_qsos and
            # can raise a TypeError -> 500.
            _allowed = {"from", "to", "call", "band", "mode", "filter_uid", "ids"}
            f = {k: v for k, v in _raw.items() if k in _allowed and v}
            # The frontend sends 'user_id' - map it to 'filter_uid' which the backend knows
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
                # IMPORTANT: we do NOT return an aiohttp Response object
                # here - http_handler does `status, result = await
                # self.api(...)` and unpacks the return value as a tuple
                # (status, result). A Response object isn't a tuple ->
                # ValueError -> 500 (which is why EVERY export used to give
                # a 500, while import worked because it returned a normal
                # tuple). We return a special "export" type that
                # http_handler handles with download headers.
                return "export", {
                    "body": content_out.encode("utf-8"),
                    "content_type": ct,
                    "filename": f"qso_export.{fmt}",
                }
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                print(f"[export] 500 ERROR fmt={fmt} filters={f}:\n{tb}", flush=True)
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
                    # Same fallback as POST /api/qsolog above - update_qso()
                    # unconditionally overwrites my_call with whatever the
                    # request sends, so an edit from a client with an empty
                    # AppState.callsign would silently WIPE an already-correct
                    # operator field instead of just leaving it as-is.
                    if not (body.get("my_call") or "").strip():
                        _u_obj = self.find_user_by_id(uid)
                        body["my_call"] = ((_u_obj or {}).get("callsign")
                                            or user.get("username", ""))
                    ok = qso_db.update_qso(uid, qso_id, body, is_admin=is_admin_user)
                    return (200, {"ok": True}) if ok else (404, {"error": "QSO nie znalezione"})
                except ValueError as e:
                    return 400, {"ok": False, "error": str(e)}

            if method == "DELETE":
                ok = qso_db.delete_qso(uid, qso_id, is_admin=is_admin_user)
                return (200, {"ok": True}) if ok else (404, {"error": "QSO nie znalezione"})

        if p == "/api/audio/config" and method == "POST":
            # Missing a permission check here was a bug — any logged-in
            # user (even a viewer) could remotely change the whole shared
            # station's audio device and TX Volume (a multiplier also used
            # by the FT8/FT4 encoder, i.e. affecting the real signal on
            # air). Now requires the same "server settings" permission as
            # the rest of this tab.
            if not self._has_perm(uid, role, "settings"): return 403, {"error": "Brak uprawnien (ustawienia serwera)"}
            rx = body.get("rxDevice")
            tx = body.get("txDevice")
            br = body.get("bitrate", 24000)
            tv = body.get("txVolume")
            tv_ssb = body.get("txVolumeSsb")
            if "audio" not in self.cfg: self.cfg["audio"] = {}
            if rx is not None: self.cfg["audio"]["rxDevice"] = rx
            if tx is not None: self.cfg["audio"]["txDevice"] = tx
            if br: self.cfg["audio"]["bitrate"] = int(br)
            if tv is not None:
                self.cfg["audio"]["txVolume"] = float(tv)
                self.audio.cfg = self.cfg["audio"]
            if tv_ssb is not None:
                self.cfg["audio"]["txVolumeSsb"] = float(tv_ssb)
                self.audio.cfg = self.cfg["audio"]
            save_json(CFG_F, self.cfg)
            # When Rust is active — send the new devices to ham_audio
            rust = getattr(self, 'rust_audio', None)
            if rust and rust._connected:
                # RESILIENCE TO DIFFERENT CARDS (a product used by many
                # clubs): check whether the selected card EXISTS on the
                # system. When the user picked a card that's now gone
                # (USB unplugged) or a config from a different machine
                # points at a nonexistent card - warn, but don't block
                # (Rust falls back to default_input). Without this, FT8
                # goes silent with no explanation when the configured card doesn't exist.
                try:
                    _devs = await rust.list_devices()
                    _rx_names = [d.get("name","") for d in _devs
                                 if isinstance(d, dict) and d.get("is_input")]
                    _tx_names = [d.get("name","") for d in _devs
                                 if isinstance(d, dict) and not d.get("is_input")]
                    if rx and not any(rx in n or n in rx for n in _rx_names):
                        print(f"[audio] WARNING: RX card '{rx}' doesn't exist on the system "
                              f"(available: {_rx_names}). Rust will use the default.", flush=True)
                        await self.hub.broadcast({"type": "audio_warning",
                            "msg": f"Karta RX '{rx}' niedostepna - uzyto domyslnej"})
                    if tx and not any(tx in n or n in tx for n in _tx_names):
                        print(f"[audio] WARNING: TX card '{tx}' doesn't exist on the system "
                              f"(available: {_tx_names}). Rust will use the default.", flush=True)
                except Exception as _e:
                    print(f"[audio] Can't verify the devices: {_e}", flush=True)
                # HOT-SWAP the device WITHOUT restarting the ham_audio
                # process (requires a Rust build with hot-swap support:
                # audio.rs RX_DEVICE_GEN + main.rs SetRxDevice). Rust
                # reloads just the RX stream on the fly (dropping the old
                # one = clean WASAPI, no phantoms). We do NOT restart the
                # process, so:
                # - no phantom WASAPI devices (the main problem)
                # - FT8 RX does NOT die (the process stays alive, the decoder keeps running)
                # - the browser's Opus decoder does NOT lose sync (continuous stream)
                # - no dead control connection (same process)
                print(f"[audio] Hot-swap devices RX='{rx}' TX='{tx}' (no restart)", flush=True)
                try:
                    if rx is not None:
                        await rust.set_rx_device(rx)
                    if tx is not None:
                        await rust.set_tx_device(tx)
                    if br:
                        await rust._send_ctrl({"cmd": "SetBitrate", "bps": int(br)})
                    print("[audio] Hot-swap OK - Rust reloaded the stream on the fly", flush=True)
                except Exception as _e:
                    print(f"[audio] Hot-swap error: {_e}", flush=True)
                    await self.hub.broadcast({"type": "audio_warning",
                        "msg": "Zmiana karty nie powiodla sie"})
            return 200, {"ok": True, "cfg": self.cfg["audio"]}

        if p == "/api/audio/rx/start" and method == "POST":
            # Safety: make sure PTT is off before starting audio
            # The IC-7300 can enter TX via DATA VOX when the PC opens the USB audio channel
            if self.rig.ptt:
                print("[audio] SAFETY: resetting PTT before starting RX audio")
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

        return 404, {"error": f"Nieznany endpoint: {method} {p}"}

    # ── WebSocket handler (aiohttp) ────────────────────────────────────────────
    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        # WebSocket with permessage-deflate compression - compresses JSON
        # at the WS frame level. Very good for JSON (3-8x smaller), cost: ~5% CPU.
        # heartbeat=30 - ping/pong every 30s, detects dead connections.
        # compress=9 - zlib max level (best ratio).
        # max_msg_size=8MB - the 4MB default may be too small for waterfall history.
        # NOTE: Opus audio (binary bytes) also goes over this same WS.
        # Compressing already-compressed Opus gives a minimal gain (2-5%)
        # but the cost is small too. The client decides on its own whether
        # to decode (per the permessage-deflate spec).
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
        # FIX: the JWT only ever carries the role from LOGIN time (see the
        # comment at _has_perm) - granting the "admin" GRANULAR permission
        # afterward (Admin panel tab, ADMIN.js "admin" key) made _has_perm()
        # checks pass, but every one of the many plain "role == 'admin'"
        # checks scattered through this file stayed blind to it, since they
        # never looked at permissions at all. Reported live: "granting
        # someone the admin-panel permission should make them a full admin,
        # not a partial one". Re-reading the live user record here (once,
        # per connection) and promoting role locally is far safer than
        # hunting down every "== admin" check in this file individually.
        u_obj = self.find_user_by_id(uid) or {} if user else {}
        if role != "admin" and u_obj.get("permissions", {}).get("admin"):
            role = "admin"

        await self.hub.add(ws)

        # Register the user as online
        if user:
            self.online_users[ws] = {
                "user_id":  uid,
                "username": user.get("username", ""),
                "callsign": u_obj.get("callsign", user.get("username", "")),
                "locator":  u_obj.get("locator", ""),
                "role":     role,
                "joined_at": time.time(),
            }
            # Notify everyone about the new online user
            _lock_state_join = self._radio_lock_state()
            _lock_state_join.pop("type", None)  # see the comment at init (ws_handler)
            await self.hub.broadcast({
                "type":   "online_update",
                "online": self._online_users_state(),
                **_lock_state_join,
            })

            # Auto-connect the DX Cluster if the user has it enabled
            if self.dxcluster:
                dx_cfg = u_obj.get("dxcluster", {})
                if dx_cfg.get("auto_connect") and dx_cfg.get("host") and dx_cfg.get("login"):
                    # Check whether it's not already connected
                    existing = self.dxcluster.get_client(uid)
                    if not existing or not existing.is_connected():
                        asyncio.ensure_future(self.dxcluster.connect_user(
                            uid, dx_cfg["host"], int(dx_cfg.get("port", 7300)),
                            dx_cfg["login"], decrypt_secret(dx_cfg.get("password", ""))
                        ))

        try:
            _lock_state = self._radio_lock_state()
            _lock_state.pop("type", None)  # see the comment below — critical fix
            await ws.send_str(json.dumps({
                "type": "init",
                "freq": self.rig.freq, "mode": self.rig.mode,
                "bandwidth": self.rig.bw, "ptt": self.rig.ptt,
                "filterNum": self.rig.filter_num,
                "preamp": self.rig.preamp, "attenuator": self.rig.attenuator,
                "split": self.rig.split, "freqB": self.rig.freq_b,
                "vfo": getattr(self.rig, "vfo", "VFOA"),
                "callsign": CALLSIGN, "locator": LOCATOR,
                # STATION locator (STATION_LOCATOR from the admin config) —
                # where the antenna physically stands. NOT the logged-in
                # operator's locator: in remote operation each user is
                # somewhere else, but the rotor turns in one place. The
                # azimuth to a correspondent must be computed from the
                # antenna, otherwise a user in a different location would
                # get a bearing computed from the user's own location for
                # an antenna standing somewhere else.
                "stationLocator": (self.cfg.get("stationLocator")
                                   or LOCATOR or "").strip().upper(),
                # OPERATOR locator (from their account) — for FT8 reports/the QSO log
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
                # CRITICAL: _radio_lock_state() returns its OWN key
                # "type": "radio_lock_state". Unpacking (**) that dict
                # AFTER "type": "init" in the same dict literal made
                # PYTHON SILENTLY OVERWRITE "type" with "radio_lock_state"
                # — the final message sent to the frontend NEVER had
                # type=="init"! So the 'init' case in ws.js never ran,
                # msg.modes never arrived, and the MODE panel was empty
                # after every fresh connection/refresh. We remove the
                # colliding "type" key from lock_state BEFORE unpacking
                # (see _lock_state.pop("type") above).
                **_lock_state,
                "online": self._online_users_state(),
                "my_uid": uid,
            }))
            # FT8/FT4 automation state (QSO engine) — without this a
            # freshly connected client (new tab, page refresh, reconnect)
            # didn't know the backend's current state and showed the
            # default "IDLE" even though the automation was actually
            # already running a QSO with someone else — the panel showed
            # unrealistic info about what/who we're transmitting to, until
            # the next decode arrived and triggered a broadcast.
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
                        # The decoder only runs while the browser is
                        # SENDING audio (the CW panel is open) — closing
                        # the panel = zero CPU.
                        try:
                            import struct as _st
                            _sr = _st.unpack_from("<I", data, 1)[0]
                            _pcm = np.frombuffer(data[5:], dtype=np.float32)
                            if deepcw_engine is not None:
                                await deepcw_engine.feed(
                                    _pcm, _sr, broadcast_fn=self.hub.broadcast)
                        except Exception as _e:
                            print(f"[deepcw] feed error: {_e}", flush=True)
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        t = data.get("type","")
                        print(f"[ws] TEXT t={t!r}", flush=True)
                        if t == "audio_start":
                            # Audio goes directly through the Rust WSS — Python doesn't proxy it
                            await ws.send_str(json.dumps({"type":"audio_ready","status":{"running":True,"rust":True}}))
                        elif t == "audio_stop":
                            pass  # audio goes directly over the Rust WS, nothing for Python to release
                        else:
                            # Touch activity on any real operator action — an
                            # EXCLUDE list, not an include list. FIX: this
                            # used to be a hand-picked whitelist of ~12
                            # message types (freq/mode/ptt/rig_slider/...)
                            # that only covered plain radio-control actions.
                            # It never got extended when the FT8 panel grew
                            # its own message types (ft8_qsy, ft8_tx,
                            # ft8_start_auto_qso, ft8_toggle_auto_seq, chat_send,
                            # ...) — so a user actively running the FT8
                            # automation (CQ, answering callers, clicking
                            # bands) never touched last_activity at all.
                            # last_activity then stayed frozen at whatever
                            # _lock_radio() set it to on acquisition, so the
                            # idle watchdog (_radio_lock_watchdog) effectively
                            # measured "time since PRZEJMIJ TRX", not real
                            # idleness — exactly the reported symptom. Only
                            # exclude messages that fire on their own
                            # (automatic, not user-driven): 'ping' (client
                            # heartbeat, every 30s) and 'subscribe' (sent
                            # once on connect/reconnect).
                            if t not in ("ping", "subscribe"):
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
            # Remove from the online list and notify the others
            self.online_users.pop(ws, None)
            self.radio_requests.pop(uid, None)
            # Check whether the user has another active WS (e.g. open in
            # another tab/laptop+phone). If YES - don't stop WSJT-X or
            # release the radio (since they're still online).
            still_online = any(u.get("user_id") == uid for u in self.online_users.values())
            # If the user held the radio and this was their LAST connection — release it
            if uid and self.radio_lock["user_id"] == uid and not still_online:
                self._release_radio()  # also stops WSJT-X if they were the owner
            # If the user was the WSJT-X owner and this was their LAST connection — stop it
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
                    print("[deepcw] decoder disabled (last viewer disconnected)",
                          flush=True)

            _lock_state_disconnect = self._radio_lock_state()
            _lock_state_disconnect.pop("type", None)  # see the comment at init (ws_handler)
            await self.hub.broadcast({
                "type":   "online_update",
                "online": self._online_users_state(),
                **_lock_state_disconnect,
            })
        return ws


    async def com_bridge_ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        """
        WebSocket endpoint /ws/com-bridge - for Windows EXE clients.

        Authorization: Bearer token in the query string (?token=...) OR in
        the Sec-WebSocket-Protocol header as 'bearer,<token>' (WS doesn't
        allow a standard Authorization header).

        After authorization, control is handed to ComBridgeWs.handle_client(),
        which handles the protocol (hello/data/ping/etc).
        """
        # Authorization - token from the query string
        token = request.query.get('token', '')
        if not token:
            # Alternative: Sec-WebSocket-Protocol 'bearer,<token>'
            protos = request.headers.get('Sec-WebSocket-Protocol', '').split(',')
            protos = [p.strip() for p in protos]
            if len(protos) >= 2 and protos[0] == 'bearer':
                token = protos[1]

        user = self._verify_token(token) if token else None
        if not user:
            return web.Response(status=403, text="Unauthorized - missing or invalid token")

        # ── VIEWER HAS NO ACCESS TO THE BRIDGES ─────────────
        # A viewer uses ONLY the web UI (where they can change freq with
        # the lock, but not transmit). External CAT apps (CW Skimmer, HRD,
        # Logger32) via the COM Bridge are reserved for operators and the
        # admin. A viewer trying to connect gets refused - no ports are created for them.
        _role = user.get('role', 'viewer')
        if _role != 'admin':
            _u_obj_bridge = self.find_user_by_id(user.get('id', '')) or {}
            if _u_obj_bridge.get('permissions', {}).get('admin'):
                _role = 'admin'
        if _role == 'viewer':
            print(f"[com_bridge_ws] REFUSED viewer "
                  f"{user.get('username','?')} (id={user.get('id','?')}) - "
                  f"bridges are for operators/admin only", flush=True)
            return web.Response(
                status=403,
                text="COM Bridge access is for operators only. "
                     "As a viewer, use the web panel."
            )

        # Get this user's port config from users.json
        # The JWT payload uses 'id' (consistent with the rest of the code)
        user_id = user.get('id') or user.get('user_id', '')
        user_full = next((u for u in self.users if u.get('id') == user_id), {})
        # FIXED 2 CI-V PORTS FOR ALL USERS.
        # Every user gets exactly 2 CI-V ports (for CW Skimmer + HRD, or
        # any two CAT apps). There's no per-user configuration - the
        # section in the web UI is informational only. CI-V addresses
        # (E1, E2) are assigned automatically in build_assignments.
        com_config = [
            {'service': 'civ', 'baud': 19200},
            {'service': 'civ', 'baud': 19200},
        ]

        # compress=False because permessage-deflate corrupts small CI-V
        # messages (10-byte frames). Traffic is small (~a few hundred
        # B/s), compression is unnecessary and could break communication.
        ws = web.WebSocketResponse(heartbeat=30, compress=False)
        await ws.prepare(request)

        # Delegate to ComBridgeWs - it holds the client state, protocol,
        # rate limiting, validation.
        try:
            await self.com_bridge_ws.handle_client(ws, user, com_config)
        except Exception as e:
            print(f"[com_bridge_ws] handle_client error: {e}")
        finally:
            if not ws.closed:
                await ws.close()
        return ws

    def _verify_token(self, token: str) -> dict:
        """
        Verifies a JWT token and returns the user data, or None.
        Used in com_bridge_ws_handler (since a plain WS has no Bearer header).
        Returns a dict compatible with the rest of the code: {id, callsign, role}.
        """
        if not token:
            return None
        try:
            payload = self._check_pw_ver(jwt_verify(token))
            if not payload:
                return None
            # The JWT payload uses 'id' (see webapp:1300 - jwt_sign({"id": u["id"], ...}))
            uid = payload.get('id') or payload.get('user_id') or payload.get('sub')
            u = next((x for x in self.users if x.get('id') == uid), None)
            if not u:
                return None
            return {
                'id':       u.get('id'),
                'user_id':  u.get('id'),  # alias for compatibility
                'callsign': u.get('callsign', ''),
                'role':     u.get('role', 'user'),
            }
        except Exception as e:
            print(f"[com_bridge_ws] token verification error: {e}")
            return None

    async def hamlib_ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        """
        WebSocket endpoint /hamlib — Hamlib TCP tunnel for remote WSJT-X.
        The client (wsjtx_bridge.py) connects here instead of directly to port 4532.
        Hamlib commands arrive as WS text, responses go back as WS text.
        """
        ws = web.WebSocketResponse(heartbeat=30, compress=9)
        await ws.prepare(request)
        peer = request.remote
        print(f"[hamlib-ws] WSJT-X bridge connected: {peer}")

        from hamlib_server import HamlibSession

        # Build a "virtual" reader/writer for HamlibSession
        # using asyncio queues instead of TCP
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

        # Run HamlibSession in a separate task
        session = HamlibSession(self.rig, self.hub, WSReader(), WSWriter(),
                                "ws-bridge", app=self)

        async def session_loop():
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        cmd = msg.data.strip()
                        if not cmd:
                            continue
                        # Process the command and reply over WS
                        response = await session._handle(cmd)
                        await ws.send_str(response)
                    elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                        break
            except Exception as e:
                print(f"[hamlib-ws] Error: {e}")
            finally:
                print(f"[hamlib-ws] Disconnected: {peer}")
        
        await session_loop()
        return ws



    def _current_rig_id(self) -> str:
        """ID of the currently connected radio in config.json['rigs'] (or '0' if none)."""
        rigs = self.cfg.get("rigs") or []
        model = str(getattr(self.rig, "model", ""))
        for r in rigs:
            if str(r.get("model", "")) == model:
                return str(r.get("id", "0"))
        return str(rigs[0].get("id", "0")) if rigs else "0"

    def _get_enabled_features(self, rig_id: str) -> dict:
        """The admin whitelist for the given radio from config.json (or the default)."""
        rigs = self.cfg.get("rigs") or []
        for r in rigs:
            if str(r.get("id", "")) == str(rig_id):
                ef = r.get("enabledFeatures")
                if ef:
                    return ef
        return default_enabled_features()

    def _get_enabled_dynamic(self, rig_id: str) -> dict:
        """The admin whitelist for dynamic actions/sliders (per dynamic_id)."""
        rigs = self.cfg.get("rigs") or []
        for r in rigs:
            if str(r.get("id", "")) == str(rig_id):
                return r.get("enabledDynamic") or {}
        return {}

    async def _get_rig_features(self, role: str) -> dict:
        """
        GET /api/rig/features
        - admin: full list (static features + dynamic actions/sliders)
                 with capabilities/enabled/effective (for the admin panel)
        - viewer/operator: only effective (static) + allowed dynamic ones
        """
        try:
            capabilities = await self.rig.get_capabilities()
        except Exception as e:
            print(f"[features] get_capabilities error: {e}")
            capabilities = {"actions": [], "sliders": [], "raw_caps": {}}

        # Compatibility: if get_capabilities returned the old format (a flat bool dict)
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
        Admin-only — saves the whitelists to config.json['rigs'][i].
        """
        rig_id = str(body.get("rigId") or self._current_rig_id())
        new_enabled     = body.get("enabledFeatures") or {}
        new_enabled_dyn = body.get("enabledDynamic") or {}

        # Validate the static ones — only known feature_ids
        known_ids = {f["id"] for f in FEATURES}
        new_enabled = {k: bool(v) for k, v in new_enabled.items() if k in known_ids}
        # Dynamic: only validate the type (bool), ids are dynamic so we don't filter them
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
        print(f"[features] admin saved enabledFeatures for rig={rig_id}: "
              f"{new_enabled} | enabledDynamic: {new_enabled_dyn}")

        # Broadcast the new effective set to all clients (the panel will refresh)
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
        Push a QSO to CloudLog/WaveLog if the user has the API configured.

        IMPORTANT: the Cloudlog API accepts a QSO ONLY as an ADIF string in
        the 'string' field (type='adif'). Sending JSON fields (call, band,
        mode...) does NOT WORK - Cloudlog responds 200 but the QSO never
        reaches the log. The profile field is named 'station_profile_id' (not 'station_id').
        """
        try:
            u = self.find_user_by_id(user_id)
            if not u: return
            cl = (u or {}).get("cloudlog", {})
            url     = cl.get("url", "").rstrip("/")
            api_key = decrypt_secret(cl.get("apiKeyQso", ""))
            station = cl.get("stationId", 1)
            if not url or not api_key: return
            # Don't duplicate /index.php if the user already typed it
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
                        # Cloudlog returns e.g. {"status":"created","adif_count":1}
                        # or {"status":"failed","reason":"..."}
                        try:
                            rdata = await resp.json(content_type=None)
                        except Exception:
                            rdata = {}
                        if isinstance(rdata, dict) and rdata.get("status") == "failed":
                            print(f"[cloudlog] {qso.get('call')}: rejected — "
                                  f"{rdata.get('reason', '?')}", flush=True)
                            return
                        print(f"[cloudlog] {qso.get('call')} sent OK", flush=True)
                        # Save the Cloudlog ID if one was returned
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
            print(f"[cloudlog] push QSO error: {e}", flush=True)

    def _save_cloudlog_id(self, qso_id: str, cloudlog_id: str):
        """Save the Cloudlog ID against the QSO (sync - called via to_thread)."""
        with qso_db._conn_lock:
            conn = qso_db._get_conn()
            conn.execute("UPDATE qso SET cloudlog_id=? WHERE id=?",
                         (cloudlog_id, qso_id))
            conn.commit()

    def _is_band_allowed(self) -> bool:
        """Check whether the radio's current frequency is within an allowed band.
        Uses self._BAND_RANGES (the same table as _get_band_for_freq /
        cross-band split) - this used to have a separate, independently
        maintained dict that drifted apart from _BAND_RANGES for
        160m/60m/6m (different boundaries in two places in the same file
        for the same safety guard).
        """
        freq    = self.rig.freq
        enabled = self.cfg.get("enabledBands", None)
        if not enabled:          # no configuration = all bands allowed
            return True
        for band in enabled:
            lo, hi = self._BAND_RANGES.get(band, (0, 0))
            if lo <= freq <= hi:
                return True
        return False

    # Shared band table for all checks (avoids duplication) - 160m/60m/6m
    # are deliberately NARROWER (the real PL/EU allocation) - this is the
    # table used, among others, by the TX guard (_is_band_allowed), so it's
    # safer to be conservative than to risk transmitting outside the real
    # band allocation.
    #
    # MUST cover EVERY band from any radio profile in rigs/civ_profiles.py
    # (the IC-9100/IC-9700 offer "23cm" in their BANDS) - otherwise the TX
    # guard rejects a band as "not allowed" even though the admin
    # explicitly enabled it and the radio physically has it. 23cm added
    # with the same range as _BAND_23CM in civ_profiles.py (1240-1300 MHz).
    # 13cm NOT added - no current radio profile offers it yet in "bands"
    # (see _BAND_13CM in civ_profiles.py - defined, but unused), so adding
    # it now would be guessing without any real hardware backing it.
    _BAND_RANGES = {
        '160m': (1810000,   2000000),
        '80m':  (3500000,   3800000),
        '60m':  (5351500,   5366500),
        '40m':  (7000000,   7200000),
        '30m':  (10100000,  10150000),
        '20m':  (14000000,  14350000),
        '17m':  (18068000,  18168000),
        '15m':  (21000000,  21450000),
        '12m':  (24890000,  24990000),
        '10m':  (28000000,  29700000),
        '6m':   (50000000,  52000000),
        '4m':   (70000000,  70500000),
        '2m':   (144000000, 146000000),
        '70cm': (430000000, 440000000),
        '23cm': (1240000000,1300000000),
    }

    def _get_band_for_freq(self, hz: int) -> str | None:
        """Return the band name for a frequency, or None if outside all bands."""
        for band, (lo, hi) in self._BAND_RANGES.items():
            if lo <= hz <= hi:
                return band
        return None

    def _is_split_cross_band(self) -> tuple[bool, str, str]:
        """Check whether split is cross-band (VFO-A and VFO-B in different bands).
        Returns (is_cross_band, band_a, band_b) — is_cross_band=True when
        split is active and the bands differ. band_a/b are band names or 'poza pasmem'.

        This guard protects against damaging the radio/antenna —
        transmitting on a different band than the one the ATU tuned for
        can damage the final amplifier or an antenna with the wrong impedance.
        """
        if not getattr(self.rig, 'split', False):
            return False, '', ''
        freq_a = self.rig.freq
        freq_b = getattr(self.rig, 'freq_b', freq_a)
        band_a = self._get_band_for_freq(freq_a) or 'poza pasmem'
        band_b = self._get_band_for_freq(freq_b) or 'poza pasmem'
        return (band_a != band_b), band_a, band_b

    def _can_control_radio(self, ws, role: str) -> tuple[bool, str]:
        """Check whether the user may perform a radio-control action.
        Returns (allowed, reason). Admin always has the right. Regular
        users only when holding radio_lock. Without the lock they can only
        WATCH (they see everything live, but can't change anything).
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
        """PTT watchdog - auto-off after a configured time.
        Protects the radio from a stuck PTT (e.g. a closed window mid-TX).
        Configuration: config['tx_watchdog_s'] (default 180s = 3 min).
        Cancelled by the user when PTT turns off normally (see the PTT handler).
        """
        # Cancel the previous watchdog if one exists (e.g. rapid ptt on/off/on)
        if hasattr(self, '_tx_watchdog_task') and self._tx_watchdog_task:
            self._tx_watchdog_task.cancel()

        async def _watchdog():
            timeout = int(self.cfg.get("tx_watchdog_s", 180))
            try:
                # Warnings in the last 30/20/10 seconds
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
                # Timeout — force PTT OFF
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
                    print(f"[watchdog] PTT auto-off after {timeout}s")
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"[watchdog] error: {e}")

        self._tx_watchdog_task = asyncio.create_task(_watchdog())

    def _feature_allowed(self, feature_id: str, role: str) -> bool:
        """
        Check whether the given action (feature_id) is allowed for a user
        with the given role. Admin always has access (for testing/config).
        Capabilities are checked from the cache (self._caps_cache) if
        available, otherwise we default to allowing it (fail-open for
        compatibility) and log a warning — webapp should have already
        called _refresh_caps_cache() after connecting to the radio.
        """
        if role == "admin":
            return True
        caps = getattr(self, "_caps_cache", None) or {"actions": [], "sliders": [], "raw_caps": {}}
        enabled = self._get_enabled_features(self._current_rig_id())
        eff = effective_features(caps, enabled)
        return bool(eff.get(feature_id, False))

    async def _refresh_caps_cache(self):
        """Call after connecting/changing the radio — refreshes the
        capabilities cache used by _feature_allowed() (a synchronous function in _ws_msg)."""
        try:
            caps = await self.rig.get_capabilities()
            if "raw_caps" not in caps and "actions" not in caps:
                caps = {"actions": [], "sliders": [], "raw_caps": caps}
            self._caps_cache = caps
        except Exception as e:
            print(f"[features] _refresh_caps_cache error: {e}")
            self._caps_cache = {"actions": [], "sliders": [], "raw_caps": {}}

    def _rig_bcast(self, msg: dict):
        """
        Callback passed to CivRig as broadcast_sync. Intercepts the
        internal 'rig_reconnected' event (e.g. from _reconnect_loop after
        the radio is powered on after the server started) — refreshes
        _caps_cache and sends a fresh /api/rig/features to clients. Other
        message types are passed straight through to hub.broadcast_sync unchanged.
        """
        if msg.get("type") == "rig_reconnected":
            if self.hub._loop and self.hub._loop.is_running():
                asyncio.run_coroutine_threadsafe(self._on_rig_reconnected(), self.hub._loop)
            return
        self.hub.broadcast_sync(msg)

    async def _verify_radio_awake_and_start_scope(self):
        """
        After power ON the radio needs a moment before CI-V starts
        responding. We try reading the frequency (up to 5 attempts, 1s
        apart). Only once the radio responds do we start the
        scope/waterfall. Without this the waterfall used to start before
        the radio came alive and ended up in a SIM state.
        """
        radio_alive = False
        for attempt in range(5):
            await asyncio.sleep(1.0)
            try:
                if hasattr(self.rig, "get_freq"):
                    f = await asyncio.wait_for(self.rig.get_freq(), timeout=2.0)
                    if f and f > 0:
                        radio_alive = True
                        print(f"[rig] radio woke up after power ON (attempt {attempt+1}, "
                              f"freq={f})", flush=True)
                        break
            except Exception:
                pass
            print(f"[rig] waiting for the radio to wake up... ({attempt+1}/5)", flush=True)

        if not radio_alive:
            print("[rig] radio NOT responding after power ON - scope stays in SIM. "
                  "Check the 12V power and CI-V Transceive=ON", flush=True)
            # Let the users know the radio didn't come up
            await self.hub.broadcast({
                "type": "toast",
                "msg": "⚠️ Radio nie odpowiada po włączeniu — sprawdź zasilanie",
                "level": "warning"})
            return

        # The radio is alive - resume the scope. After a CI-V power
        # OFF->ON the radio goes through a full boot and "forgets" the
        # scope settings. Sending commands too early has no effect - the
        # radio doesn't accept them until the scope processor comes up
        # (much later than the freq response).
        #
        # STRATEGY: retry _enable_scope every 1.5s and CHECK whether
        # frames have actually started flowing (the _scope_rx_count
        # counter in civ.py increases). Stop once frames flow, or after 6
        # attempts (~10s).
        scope_ok = False
        try:
            if hasattr(self.rig, "scope_start"):
                for scope_try in range(6):
                    # Remember the frame counter before the attempt
                    cnt_before = getattr(self.rig, "_scope_rx_count", 0)
                    # Force scope on fully
                    self.rig.scope_start()
                    await asyncio.sleep(1.5)
                    # Did new frames arrive?
                    cnt_after = getattr(self.rig, "_scope_rx_count", 0)
                    if cnt_after > cnt_before:
                        scope_ok = True
                        print(f"[rig] scope frames flowing after power ON "
                              f"(attempt {scope_try+1}, +{cnt_after-cnt_before} frames)",
                              flush=True)
                        break
                    print(f"[rig] scope not sending frames yet, retrying "
                          f"({scope_try+1}/6)...", flush=True)
                if not scope_ok:
                    print("[rig] WARNING: scope didn't resume after power ON. "
                          "The radio may have left scope mode - check whether "
                          "the waterfall is visible on the radio's SCREEN (SCOPE button).",
                          flush=True)
                    await self.hub.broadcast({
                        "type": "toast",
                        "msg": "⚠️ Waterfall nie wrócił po włączeniu radia — "
                               "sprawdź czy na ekranie radia widać SCOPE",
                        "level": "warning"})
        except Exception as e:
            print(f"[rig] scope_start after power ON error: {e}", flush=True)
        await self._on_rig_reconnected()
        # Confirm to users that the radio is working
        await self.hub.broadcast({"type": "power_state", "value": True})

    async def _on_rig_reconnected(self):
        """
        Refresh the capabilities cache and broadcast rig_features after a
        CivRig (re)connect.

        IMPORTANT: this is called EVERY TIME the radio switches from SIM
        to real (via _reconnect_loop in civ.py). We turn the scope on
        here, because at the time of _initial_rig_connect the radio could
        still have been in SIM - the scope didn't start then. Here we know
        for sure the radio is responding.
        """
        await self._refresh_caps_cache()
        # Auto-enable the scope (waterfall) - the radio is real and
        # responding. Without this the waterfall only shows SIM, since
        # _enable_scope was never called (nothing calls the frontend's startScope()).
        try:
            if not self.rig.sim and hasattr(self.rig, "scope_start"):
                # scope_start writes to the serial port with time.sleep —
                # blocking. Calling it directly in the loop froze asyncio
                # (~100ms, found via looplag). Offloaded to a thread.
                await asyncio.to_thread(self.rig.scope_start)
                print("[rig] scope enabled (real radio, reconnect)", flush=True)
        except Exception as e:
            print(f"[rig] scope_start on reconnect error: {e}", flush=True)
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
        print("[rig] reconnect — rig_features sent to clients")

    def _webrtc_tx_start(self):
        """Hook called when the WebRTC audio track starts transmitting."""
        print("[webrtc] TX track active")

    def _webrtc_tx_stop(self):
        """Hook called when the WebRTC audio track ends."""
        print("[webrtc] TX track ended")
        if self.audio.tx_active:
            self.audio.stop_tx()

    async def _cw_decode_loop(self):
        """Feeds the CW decoder raw audio from the card (bypassing Opus).

        Critical for quality: the signal from the browser used to go
        through a lossy codec, which blurs the keying edges (measured
        envelope contrast 6.4x vs the >20x the model needs). Here we take
        PCM straight from the card buffer, the same way as for FT8.
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
            print(f"[deepcw] decoder loop aborted: {e}", flush=True)

    async def _ws_msg(self, msg: dict, ws, role: str = "viewer"):
        t = msg.get("type", "")

        # ── Radio Lock: verify the lock for radio-control actions ────────
        # Admin can always. A regular user must be "holding" the radio.
        # Read-only actions (audio/waterfall subscription, fetching state)
        # are always allowed.
        _CONTROL_TYPES = {
            "freq","freqB","mode","ptt","rig_slider","rig_action","vfo","vfo_op",
            "split","preamp","attenuator","tuner","tuner_autotune","ft8_tx",
            # webrtc_offer starts microphone TX (WebRTC -> radio's TX audio
            # input, see webrtc_audio.py) — it had NO gate at all (found
            # while building mobile mic-TX): any logged-in client, including
            # role=viewer and anyone not holding radio_lock, could call it
            # directly and transmit. Adding it here reuses the SAME two
            # checks every other control type already gets below (viewer
            # hard-block + radio_lock-for-everyone-else) with no extra code.
            "webrtc_offer",
        }

        # ── VIEWER: HARD WHITELIST ──────────────────
        # A viewer can do ONLY: change frequency, mode, and band (for
        # listening). EVERYTHING else is hard-blocked in the backend
        # REGARDLESS of the permissions the admin granted when creating
        # the user (a new user gets ptt/button rights by default, but for
        # a viewer they're IGNORED here). Buttons stay visible in the UI
        # (so as not to break the layout), but clicking does nothing - the
        # server rejects it.
        #
        # Reason: a viewer used to have access to power_toggle (could turn
        # the radio off!), sliders, and function buttons. Now it can only tune.
        _VIEWER_ALLOWED = {
            "freq", "freqB", "mode",   # tuning + band (band = freq+mode)
            # read-only (view-only) - always OK, don't touch the radio:
            "subscribe", "unsubscribe", "ping", "pong",
            "ft8_rx_enable", "ft4_rx_enable",  # decode-receive only
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
            print(f"[viewer-block] rejected '{t}' for a viewer (id="
                  f"{self.online_users.get(ws, {}).get('user_id', '?')})", flush=True)
            return

        # TRANSMIT (TX) actions - forbidden for a viewer EVEN with the
        # lock. (redundant with the whitelist above, but kept as a second layer)
        _TX_TYPES = {"ptt", "ft8_tx", "ft4_tx", "cw", "cw_send", "tune"}
        if t in _TX_TYPES and role == "viewer":
            await ws.send_json({
                "type": "toast",
                "msg": "⛔ Nadawanie niedostepne dla obserwatora (viewer).",
                "level": "error",
            })
            return

        if t in _CONTROL_TYPES and role != "admin":
            # Find the sender's uid via ws in online_users
            sender = self.online_users.get(ws, {})
            sender_uid = sender.get("user_id", "")
            if not self._user_has_lock(sender_uid):
                # Radio free or held by someone else — block TX
                holder = self.radio_lock.get("callsign") or self.radio_lock.get("username") or ""
                msg_err = f"Radio zajete przez {holder} — poproś o dostęp" if holder else "Najpierw przejmij radio (kliknij PRZEJMIJ TRX)"
                await ws.send_str(json.dumps({
                    "type":    "radio_locked",
                    "message": msg_err,
                    "holder":  holder,
                }))
                return

        # Ping — reply pong directly to the sender (RTT measurement)
        if t == "ping":
            await ws.send_json({"type": "pong", "t": msg.get("t", 0)})
            return

        # ── Subscription channels — the client adds/removes channels when switching tabs
        if t == "subscribe":
            channels = msg.get("channels", [])
            if isinstance(channels, list):
                # 'set' replaces the whole channel set (used by setPage),
                # 'add' adds without removing (e.g. for extra features)
                mode = msg.get("mode", "set")
                if mode == "add":
                    await self.hub.subscribe(ws, channels)
                else:
                    await self.hub.set_channels(ws, channels)
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
            # NOTE (perf): removed the per-freq print — while scrolling
            # the VFO, freq changes ~20x/s, the print was flooding the event loop.
            self.rig.freq = hz
            if not self.rig.sim:
                try:
                    await self.rig.set_freq(hz)
                except Exception as _e:
                    print(f"[rig] set_freq error: {_e}")
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
            # filterNum: 1/2/3 = FIL1/2/3, selects WHICH filter configured
            # in the radio's menu to use (CI-V 06 <mode> <fil>) — doesn't
            # change the filter's width, only which of the three "slots" is active.
            fil = msg.get("filterNum")
            if fil in (1, 2, 3):
                self.rig.filter_num = fil
            if not self.rig.sim:
                try:
                    await self.rig.set_mode(self.rig.mode, self.rig.bw, fil or 0)
                except Exception as e:
                    print(f"[rig] set_mode ERROR for mode={self.rig.mode!r} fil={fil}: {e!r}")

            # We do NOT automatically enable BK-IN when switching to CW.
            # BK-IN (CI-V 16 47 01) makes the radio enter continuous TX
            # waiting for a signal from a physical CW key — undesired when
            # sending via CI-V cmd 17. The user can enable BK-IN manually
            # via the Break-In button in the radio features panel.

            await self.hub.broadcast({"type": "mode", "mode": self.rig.mode,
                                       "bandwidth": self.rig.bw,
                                       "filterNum": self.rig.filter_num}, skip=ws)

        elif t == "ft8_qsy":
            # Retune to an FT8/FT4 band from the list in the WSJT-X panel
            # (wj-band-select -> tuneToBand() in wsjtx.js). FIX: the
            # frontend had long been sending this message type ("one
            # atomic ft8_qsy command"), but the backend never had a
            # handler for it — so picking a band from the list did
            # nothing, the frequency never switched. Mode+filter are set
            # BEFORE the frequency (sequentially on the same CI-V
            # connection), as described in the comment in wsjtx.js — this
            # prevents a race between freq/mode sent separately.
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            if not (self._feature_allowed("freq_set", role)
                    and self._feature_allowed("mode_set", role)):
                return
            # FIX: if an FT8 TX is in flight (PTT keyed) when the band is
            # clicked, the set_freq() write below used to race straight into
            # an actively-transmitting radio. The IC-7300 silently ignores a
            # frequency-set CI-V command while modulating (set_freq() is
            # fire-and-forget, no ACK is even checked) — this is why QSY
            # "sometimes worked, sometimes didn't", correlating exactly with
            # whether the automation happened to be mid-TX at click time, and
            # why stopping the automation first always fixed it. Same abort
            # signal already used for CQ/auto-QSO-start (see ~line 5580/5983)
            # cuts the in-flight TX short; then we wait briefly (bounded, NOT
            # the full _ft8_tx_lock/end-of-period hold in the TX finally: a
            # QSY is leaving this band, so there's no correspondent window
            # left to protect) for PTT to actually drop before retuning.
            if self._ft8_tx_lock.locked():
                self._ft8_tx_abort = True
                for _ in range(20):  # up to ~1s
                    if not self.rig.ptt:
                        break
                    await asyncio.sleep(0.05)
                print(f"[ft8] QSY: aborted in-flight TX, ptt={self.rig.ptt} before retune")
            hz = int(msg.get("freq", self.rig.freq))
            # The target band must be allowed (same as for CQ/TX) — checked
            # BEFORE changing the radio's state, otherwise the FT8 band
            # list would bypass the admin's block, which the normal band
            # selector respects.
            enabled = self.cfg.get("enabledBands")
            if enabled and self._get_band_for_freq(hz) not in enabled:
                await ws.send_json({"type": "toast",
                                     "msg": "⛔ QSY zablokowany — pasmo niedozwolone przez admina",
                                     "level": "error"})
                return
            # Digital mode FT8/FT4 on the IC-7300: always USB-D + FIL1
            # (a project-wide convention, regardless of what came in the message).
            self.rig.mode = msg.get("mode") or "USB-D"
            self.rig.filter_num = 1
            if not self.rig.sim:
                try:
                    await self.rig.set_mode(self.rig.mode, self.rig.bw, self.rig.filter_num)
                except Exception as e:
                    print(f"[ft8] ft8_qsy set_mode error: {e!r}")
            self.rig.freq = hz
            if not self.rig.sim:
                # FIX: set_freq() is fire-and-forget (no ACK check, by
                # design — see the comment in civ.py, needed for a snappy
                # click-to-tune) AND it sets self.rig.freq optimistically
                # BEFORE even attempting the write. That means nothing in
                # the normal path ever notices when the write silently
                # doesn't land on the radio (seen live: USB CI-V + USB
                # audio share the IC-7300's single USB port, so a busy
                # moment on one can stall the other) — the UI/log looked
                # "successful" while the radio itself stayed on the old
                # freq. A band-select click is a deliberate, infrequent
                # action (unlike drag-tuning), so it can afford a live
                # read-back + a couple of retries to actually confirm the
                # radio moved, instead of firing blind.
                ok = False
                for attempt in range(3):
                    try:
                        await self.rig.set_freq(hz)
                    except Exception as e:
                        print(f"[ft8] ft8_qsy set_freq error (attempt {attempt+1}): {e!r}")
                        continue
                    live = await self.rig.get_freq_live()
                    if live is not None and abs(live - hz) < 10:
                        ok = True
                        break
                    print(f"[ft8] QSY: verify failed (attempt {attempt+1}), "
                          f"wanted {hz}Hz, radio reports {live!r}Hz — retrying")
                    await asyncio.sleep(0.15)
                if not ok:
                    print(f"[ft8] QSY: FAILED to confirm retune to {hz}Hz after 3 attempts")
                    await ws.send_json({"type": "toast",
                                         "msg": f"⛔ QSY nieudany — radio nie potwierdzilo zmiany na {hz/1e6:.3f} MHz (sprobuj ponownie)",
                                         "level": "error"})
                    return
            print(f"[ft8] QSY: {hz/1e6:.6f} MHz {self.rig.mode} FIL{self.rig.filter_num}")
            # Without skip=ws: unlike the regular freq/mode handlers, the
            # frontend's tuneToBand() does NOT update S.freq/S.mode
            # locally — the client that clicked the band also relies on this broadcast.
            await self.hub.broadcast({"type": "mode", "mode": self.rig.mode,
                                       "bandwidth": self.rig.bw,
                                       "filterNum": self.rig.filter_num})
            await self.hub.broadcast({"type": "freq", "freq": hz})

        elif t == "ptt":
            if not self._feature_allowed("ptt", role):
                return
            # Check whether the user holds the radio — only the active operator or admin may TX
            if bool(msg.get("ptt")) and role != "admin":
                sender = self.online_users.get(ws, {})
                sender_uid = sender.get("user_id", "")
                if self.radio_lock["user_id"] and not self._user_has_lock(sender_uid):
                    holder = self.radio_lock["callsign"] or self.radio_lock["username"] or "?"
                    await self.hub.broadcast({"type": "toast", "msg": f"⛔ PTT zablokowany — radio ma {holder}", "level": "error"})
                    return
            # Block TX on a disallowed band
            if bool(msg.get("ptt")) and not self._is_band_allowed():
                await self.hub.broadcast({"type": "toast", "msg": "⛔ TX zablokowany — pasmo niedozwolone przez admina", "level": "error"})
                return
            # Block TX on a cross-band split — VFO-A and VFO-B on different
            # bands (protects the radio/antenna from transmitting on the wrong band)
            if bool(msg.get("ptt")):
                cross, band_a, band_b = self._is_split_cross_band()
                if cross:
                    await self.hub.broadcast({"type": "toast",
                        "msg": f"⛔ TX zablokowany — split cross-band ({band_a} RX / {band_b} TX). Wylacz split lub ustaw VFO-B na to samo pasmo.",
                        "level": "error"})
                    return
            self.rig.ptt = bool(msg.get("ptt"))
            if not self.rig.sim:
                # Note on USB audio on the IC-7300: audio from USB only
                # modulates when the radio is in DATA mode (USB-D) - this
                # is a hardware limitation of the radio (DATA MOD=USB only
                # works in data mode; in plain USB the radio takes audio
                # from MIC). RCForb's "TXd" enters data mode for this
                # reason. We do NOT switch mode automatically, since USB-D
                # has different filters (user's concern). Instead: the user
                # transmits knowingly in USB-D (with the filter set wide in
                # the radio), OR sets DATA OFF MOD=USB in the radio so
                # plain USB also takes audio from USB. Auto-switching is
                # possible via the audio_tx_force_data_mode flag (OFF by
                # default - doesn't touch filters without user consent).
                _audio_tx = bool(getattr(self.audio, "tx_active", False))
                _force = self.cfg.get("audio_tx_force_data_mode", False)
                if (self.rig.ptt and _audio_tx and _force
                        and not getattr(self.rig, "data_mode", False)):
                    try:
                        base = self.rig.mode.replace("-D", "")
                        await self.rig.set_mode(base + "-D")
                        print(f"[ptt] Audio TX -> DATA ({base}-D) [force flag ON]", flush=True)
                        await self.hub.broadcast({"type": "mode", "mode": self.rig.mode})
                    except Exception as e:
                        print(f"[ptt] data mode error: {e}", flush=True)
                # FIX: releasing PTT used to send "1C 00 00" (PTT off)
                # THE INSTANT the button/key was released, regardless of
                # whether the TX audio pipeline still had mic audio queued
                # (WebRTC/network delivery is not instantaneous — there's
                # always some audio "in flight" that hasn't reached the
                # sound card yet). Reported live and confirmed via an
                # independent SDR in Italy (a real on-air effect, not the
                # local RX-monitor artifact already fixed separately in
                # ws.js): the tail end of speech was getting CLIPPED — the
                # carrier dropped before the last queued audio had actually
                # played, cutting off the end of a word/callsign
                # ("SQ3MZM" -> "...MAJ" instead of "...MIKE"). VOX ruled
                # out live (toggling the VOX function button caused no TX
                # reaction — it isn't what's keying this radio).
                # Fix: on PTT-off, wait (briefly, bounded) for the TX audio
                # queue to actually drain before sending the CI-V PTT-off
                # command — the same "let the tail finish before dropping
                # carrier" idea used elsewhere in this project for FT8 TX.
                if not self.rig.ptt:
                    _q = getattr(self.audio, "_webrtc_pcm_queue", None)
                    if _q is not None:
                        _drain_t0 = time.time()
                        _stable = 0
                        while time.time() - _drain_t0 < 1.5:  # safety cap — never hold PTT hostage
                            if _q.qsize() == 0:
                                _stable += 1
                                if _stable >= 2:  # empty on two consecutive checks (debounce)
                                    break
                            else:
                                _stable = 0
                            await asyncio.sleep(0.05)
                        _drain_ms = (time.time() - _drain_t0) * 1000.0
                        if _drain_ms > 20:
                            print(f"[ptt] OFF: waited {_drain_ms:.0f}ms for the TX audio queue to drain "
                                  f"before dropping carrier (qsize={_q.qsize()})", flush=True)
                _ptt_t0 = time.time()
                _qsize_before = getattr(self.audio, "_webrtc_pcm_queue", None)
                _qsize_before = _qsize_before.qsize() if _qsize_before is not None else "?"
                try: await self.rig.set_ptt(self.rig.ptt)
                except: pass
                _ptt_ms = (time.time() - _ptt_t0) * 1000.0
                if VERBOSE:
                    print(f"[ptt] {'ON' if self.rig.ptt else 'OFF'}: CI-V exchange took {_ptt_ms:.0f}ms, "
                          f"TX audio queue at request time={_qsize_before} frames "
                          f"(~{(_qsize_before*20) if isinstance(_qsize_before, int) else '?'}ms of unplayed audio)",
                          flush=True)
            await self.hub.broadcast({"type": "ptt", "ptt": self.rig.ptt})
            # TX watchdog: start a max-transmit-time timer. Configurable in
            # config.json ("tx_watchdog_s", default 180s = 3 minutes).
            # The watchdog protects against a PTT left on (e.g. the user
            # closed their laptop thinking TX was off, or Chrome froze mid-TX).
            if self.rig.ptt:
                asyncio.ensure_future(self._start_tx_watchdog())
            else:
                # Cancel the watchdog when PTT goes OFF (the user finished TX in time)
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
            # Set the VFO-B frequency without switching the active VFO
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
                    print(f"[rig] set_freq_b error: {_e}")
            await self.hub.broadcast({"type": "freqB", "freqB": hz}, skip=ws)

        elif t == "vfo":
            # {type:'vfo', vfo:'VFOA'|'VFOB'} -> CI-V 07 00/01 (select the active VFO)
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
                except Exception as e: print(f"[rig] set_vfo error: {e}")
            # To EVERYONE (no skip): the vfo state syncs the buttons on
            # every client, including the sender (idempotent).
            await self.hub.broadcast({"type": "vfo", "vfo": vfo})

        elif t == "vfo_op":
            # {type:'vfo_op', op:'swap'|'equalize'}
            #   swap     -> CI-V 07 B0 (A<->B)
            #   equalize -> CI-V 07 A0 (A->B, copies freq/mode from A to B)
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

            # After the operation, freq A is freshly read from the radio
            # (07 A0/B0 changes both VFO values) — in sim/fallback,
            # vfo_swap/vfo_equalize already updated self.rig.freq/freq_b locally.
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
                except Exception as e: print(f"[rig] set_preamp error: {e}")
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
                except Exception as e: print(f"[rig] set_attenuator error: {e}")
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
                except Exception as e: print(f"[rig] set_tuner error: {e}")
            else:
                self.rig.tuner = val
            await self.hub.broadcast({"type": "tuner", "value": self.rig.tuner}, skip=ws)

        elif t == "tuner_autotune":
            # {type:'tuner_autotune'} -> CI-V 1C 01 01 + 1C 01 02 (START of
            # the auto-tuning cycle). A one-shot action — generates a short
            # low-power TX signal. Same permissions as PTT (it actually triggers TX).
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
                    print(f"[rig] start_tuner_autotune error: {e}")
                    return
            else:
                self.rig.tuner = True
            await self.hub.broadcast({"type": "tuner", "value": self.rig.tuner})
            await self.hub.broadcast({"type": "toast", "message": "Tuner: autotune w trakcie..."})

        elif t == "level":
            # The UI sends: {type:'level', param:'AF', value:50}
            lvl = msg.get("param") or msg.get("level", "RFPOWER")
            val = int(msg.get("value", 0))
            if lvl == "RFPOWER" and not self._feature_allowed("tx_power", role):
                return
            # Save in state
            if lvl == "AF":      self.rig.af_gain  = val
            if lvl == "RFPOWER": self.rig.rf_power = val
            if lvl == "SQL":     self.rig.squelch  = val
            if not self.rig.sim:
                try: await self.rig.set_level(lvl, val)
                except: pass
            # Broadcast to other clients
            await self.hub.broadcast({"type":"level","param":lvl,"value":val}, skip=ws)

        elif t == "wsjtx_tx_start":
            # WSJT-X starts transmitting — notify the client's browser
            # so it starts the TX microphone (getUserMedia -> Opus -> WS)
            await self.hub.broadcast({"type": "wsjtx_tx_start"}, skip=ws)

        elif t == "wsjtx_tx_stop":
            # WSJT-X finishes transmitting — stop the TX microphone in the browser
            await self.hub.broadcast({"type": "wsjtx_tx_stop"}, skip=ws)

        elif t == "ft8_tx":
            # Our own FT8 transmitter: PTT ON -> generate+stream audio -> PTT OFF.
            # msg: {call_to, call_de, report (grid/report/RRR/73/...), rFlag?,
            #       audioFreq? — optional TX audio freq override (Hz)}
            call_to = (msg.get("callTo") or "").strip().upper()
            call_de = (msg.get("callDe") or "").strip().upper()
            report  = (msg.get("report") or "").strip()
            r_flag  = bool(msg.get("rFlag", False))
            audio_freq_override = msg.get("audioFreq")  # None albo Hz
            if not call_to or not call_de or not report:
                await ws.send_json({"type": "ft8_tx_error", "error": "Brak callTo/callDe/report"})
                return
            # Radio lock: only the operator holding the radio (or admin)
            # may transmit FT8 - the same condition as for manual PTT
            # (elif t == "ptt" above). Without this, any connected client
            # (even in view-only mode, without the lock) could trigger a
            # real TX via WS, completely bypassing the radio-ownership system.
            _sender = self.online_users.get(ws, {})
            _sender_uid = _sender.get("user_id", "")
            if self.radio_lock["user_id"] and not self._user_has_lock(_sender_uid) and role != "admin":
                _holder = self.radio_lock["callsign"] or self.radio_lock["username"] or "?"
                await self.hub.broadcast({"type": "toast", "msg": f"⛔ FT8 TX zablokowany — radio ma {_holder}", "level": "error"})
                return
            # Track WHO last initiated FT8 TX - needed (a) to auto-save
            # finished QSOs to the right log, (b) as a safety net in
            # _ft8_tx_sequence_inner (the automation's continuation must
            # stop if the radio was taken over by someone else in the
            # meantime). Updated on EVERY call, not just CQ - previously a
            # manual call to a station (without CQ) left this field
            # untouched/stale.
            if _sender_uid:
                self._autoqso_uid = _sender_uid
            # If audioFreq was given — override the TX freq BEFORE encoding.
            # Used by Hound mode, which transmits on different frequencies
            # (CQ call >1000Hz, then R+RPT in the Fox's 300-540 Hz slot). We
            # don't respect self._ft8_tx_frozen here since Hound mode knows
            # what it's doing - we don't want freeze to block it.
            if audio_freq_override is not None:
                try:
                    new_freq = int(audio_freq_override)
                    if 100 <= new_freq <= 5000:  # sensowny zakres audio FT8
                        self._ft8_tx_freq_hz = new_freq
                        await self.hub.broadcast({"type": "ft8_tx_freq",
                                                   "freqHz": self._ft8_tx_freq_hz,
                                                   "frozen": self._ft8_tx_frozen})
                except (ValueError, TypeError):
                    pass  # silently ignore a bad audioFreq
            # If the automation is on and we're sending a grid (Tx2) or a
            # CQ to a specific station — automatically put the engine into
            # CALLING state so the automation can react to the reply without needing a double-click
            if self._auto_seq_enabled and call_to != "CQ":
                # FIX (reported live 2026-08-24, RI1FJL/MSHV multistream):
                # this path (manually typing/sending a grid to a NAMED
                # station, without ever calling CQ or clicking a decoded
                # row first) never synced the engine's my_call/my_grid to
                # the operator's actual profile - unlike the "CQ" branch
                # below (~line 5721) and ft8_start_auto_qso (~line 6093),
                # which both do. self._qso_engine.my_call was left at
                # whatever it was constructed with (a stale default), so
                # EVERY incoming reply's "is this addressed to us"
                # comparison silently failed - the automation was
                # transmitting as the operator's real callsign (taken from
                # elsewhere) but listening for a DIFFERENT one. Total
                # silence on replies, with no error, exactly matching
                # "automat nie reaguje na odpowiedz". Same sync logic as
                # the other two call sites.
                _ui = self.online_users.get(ws, {})
                _ucall = (_ui.get("callsign") or "").strip().upper()
                _uuid = _ui.get("user_id")
                if _ucall:
                    _uobj = self.find_user_by_id(_uuid) or {}
                    _ugrid = (_uobj.get("locator") or LOCATOR).strip().upper()[:4]
                    if (self._qso_engine.my_call != _ucall or
                            self._qso_engine.my_grid != _ugrid):
                        self._qso_engine.my_call = _ucall
                        self._qso_engine.my_grid = _ugrid
                        print(f"[autoqso] Manual-call operator sync: {_ucall} / {_ugrid}")
                # If we're calling a specific station while the CQ loop is
                # running - stop the CQ. Otherwise CQ and the station call
                # fight over TX (the same conflict as CQ with a stale QSO).
                # Calling a station takes priority.
                if self._cq_calling:
                    print(f"[cq] Stopping CQ - the user is calling a specific station {call_to}")
                    self._stop_cq_calling()
                is_grid = (len(report) in (4, 6) and
                           report[:2].isalpha() and report[2:4].isdigit())
                print(f"[autoqso] ft8_tx check: auto_seq={self._auto_seq_enabled} call_to={call_to} report={report!r} is_grid={is_grid} state={self._qso_engine.state}")
                if is_grid and self._qso_engine.state == "IDLE":
                    self._qso_engine.start_qso(call_to)
                    print(f"[autoqso] Manual TX grid -> auto-start QSO with {call_to}")
                    # FIX (reported live 2026-08-24: "powinienem ja wolac w
                    # periodzie 1 a wolam 2, bo inaczej sie na nia
                    # nakladam") - auto-pick OUR TX period from the last
                    # time we heard THIS station transmit (see the
                    # _call_last_heard cache in _ft8_rx_loop), the same way
                    # _send_auto_tx already does for automatic replies via
                    # partner_decode. Without this, a manually-initiated
                    # call to a named station kept whatever period was left
                    # over from unrelated earlier activity - if that
                    # happened to match the station's OWN period, every
                    # transmission collided with theirs on air and neither
                    # side could ever hear the other.
                    _lh_epoch = getattr(self, "_call_last_heard", {}).get(call_to.upper())
                    if _lh_epoch is not None:
                        _window_s = ft4_encoder.FT4_SLOT_TIME if self._ft8_decode_mode == "FT4" else 15.0
                        _my_period = self._period_from_epoch(_lh_epoch, _window_s)
                        if _my_period is not None and self._ft8_tx_period != _my_period:
                            self._ft8_tx_period = _my_period
                            print(f"[autoqso] Auto-period for manual call to {call_to}: {_my_period}")
                            await self.hub.broadcast({"type": "ft8_tx_period", "period": _my_period})
                        self._qso_period_locked = True
                    await self.hub.broadcast({"type": "auto_qso_status",
                                               "state": self._qso_engine.state,
                                               "partner": self._qso_engine.partner_call})
                elif is_grid and self._qso_engine.partner_call == call_to:
                    # Already have a QSO with this station — do nothing
                    pass
            # ── CQ: periodic calling (not a one-shot) ──────────────────────
            # When the user calls CQ, we start a loop that repeats CQ every
            # full period (2 windows) until someone answers or the
            # user/timer stops it. That's how real WSJT-X behaves - we
            # don't know if someone will answer the 1st or the 4th CQ.
            if call_to == "CQ":
                # CRITICAL: starting a CQ must clear an unfinished QSO.
                # Without this, if you previously called a station
                # (start_qso -> ST_CALLING state, partner_call set) and
                # didn't finish the QSO, the state machine stays in the old
                # state. Then a CONFLICT appears: the CQ loop is running,
                # but the engine still tries to finish the old QSO per the
                # old state -> the automation "goes haywire", backend and
                # UI drift apart. Resetting the state before CQ fixes this.
                if self._qso_engine.state != qso_engine.ST_IDLE:
                    print(f"[cq] Resetting an unfinished QSO ({self._qso_engine.state}, "
                          f"partner={self._qso_engine.partner_call}) before CQ")
                    self._qso_engine.abort_qso()
                    self._autoqso_tx_seq += 1  # see the comment at REST /api/ft8/halt
                # Also abort any in-flight TX sequencer
                if self._ft8_tx_lock.locked():
                    self._ft8_tx_abort = True
                # CRITICAL (Call 1st): set the OPERATOR's callsign in the
                # engine BEFORE calling CQ. The engine starts with
                # my_call=CALLSIGN from the config (a placeholder XX0XXX /
                # club callsign); the operator's callsign used to be set
                # ONLY when clicking a station (ft8_start_auto_qso).
                # Without this, replies to our CQ ("XX0XXX XXX ...") were
                # rejected in on_decode as "not for us" (call_to !=
                # my_call) -> the automation was DEAF to callers despite
                # Call 1st being enabled.
                _ui = self.online_users.get(ws, {})
                _ucall = (_ui.get("callsign") or "").strip().upper() or \
                         (call_de or "").upper()
                _uuid = _ui.get("user_id")
                if _uuid:
                    self._autoqso_uid = _uuid  # for auto-saving a QSO from the queue
                if _ucall:
                    _uobj = self.find_user_by_id(_uuid) or {}
                    _ugrid = (_uobj.get("locator") or LOCATOR).strip().upper()[:4]
                    if (self._qso_engine.my_call != _ucall or
                            self._qso_engine.my_grid != _ugrid):
                        self._qso_engine.my_call = _ucall
                        self._qso_engine.my_grid = _ugrid
                        print(f"[cq] CQ operator: {_ucall} / {_ugrid}")
                self._cq_call_de = call_de
                self._cq_report = report
                if not self._cq_calling:
                    self._cq_calling = True
                    if self._cq_task and not self._cq_task.done():
                        self._cq_task.cancel()
                    self._cq_task = asyncio.create_task(self._cq_calling_loop())
                    print(f"[cq] Started periodic CQ calling: {call_de} {report}")
                else:
                    print(f"[cq] CQ already active - updating the content")
                # Broadcast the reset state to the UI (so the frontend doesn't stay with the old one)
                await self.hub.broadcast({"type": "auto_qso_status",
                                           "state": self._qso_engine.state,
                                           "partner": None})
                return
            # FIX (reported live 2026-08-24: "dlaczego wolam tylko raz a nie
            # tyle ile idzie zegar" - a station worked by manually typing a
            # grid and sending, never auto-retransmitted no matter how long
            # we waited). should_retransmit()'s whole timer is driven by
            # self._qso_engine.last_tx_at, but record_tx_sent() (the only
            # thing that sets it) used to be called ONLY from _send_auto_tx
            # - the AUTOMATIC reply path. A manually-triggered transmission
            # like this one never told the engine "we just transmitted", so
            # should_retransmit() saw last_tx_at=None forever and the retry
            # timer never armed for a QSO that was manually started. Any
            # actual transmission - manual or automatic - should count.
            if self._qso_engine.is_active():
                self._qso_engine.record_tx_sent()
            asyncio.create_task(self._ft8_tx_sequence(call_to, call_de, report, r_flag))

        elif t == "ft8_tx_stop":
            # Stop TX from the frontend (HALT button, safety timer expiry).
            # Stops periodic CQ and the current transmission.
            print("[ft8] ft8_tx_stop - stopping TX and CQ")
            self._ft8_tx_abort = True
            self._stop_cq_calling()
            # ALSO stop the QSO automation engine, not just the physical TX
            # — without this, HALT only interrupted the current audio
            # transmission, but the engine (self._qso_engine) stayed in its
            # state (e.g. mid-conversation with a partner) and on the NEXT
            # decode from that station (or its own retransmit from the
            # timer) would schedule a NEW transmission on its own —
            # symptom: "I hit HALT and cleared the queue, and the
            # automation still pushes out a transmission from memory after
            # a while". The queue is DELIBERATELY left untouched (there's a
            # separate "clear queue" button for that) — HALT is meant to
            # stop the current action, not wipe the list of waiting stations.
            if self._qso_engine.is_active():
                print(f"[autoqso] HALT: aborting QSO with {self._qso_engine.partner_call}")
                self._qso_engine.abort_qso()
                self._qso_period_locked = False
                self._autoqso_tx_seq += 1  # see the comment at REST /api/ft8/halt
                await self.hub.broadcast({"type": "auto_qso_status",
                                           "state": self._qso_engine.state,
                                           "partner": None})
            try:
                if not self.rig.sim:
                    await self.rig.set_ptt(False)
                self.rig.ptt = False
                await self.hub.broadcast({"type": "ptt", "ptt": False})
            except Exception:
                pass

        elif t == "ft8_tune":
            # Transmit a steady 1500Hz tone for X seconds, for ATU/antenna tuning.
            # The user can abort by sending ft8_tune_stop.
            # Note: cross-band split and radio_lock are checked.
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
                print(f"[deepcw] decoder ENABLED (viewers: {len(self._cw_viewers)})",
                      flush=True)
            elif not _want:
                _t = getattr(self, "_cw_task", None)
                if _t:
                    _t.cancel()
                    self._cw_task = None
                if deepcw_engine is not None:
                    deepcw_engine.reset()
                print("[deepcw] decoder disabled (no viewers)", flush=True)
            # If a new viewer joined an already-running decoder, send them the
            # current state so their window opens in sync (no rozjazd).
            if _en and _want:
                try:
                    await ws.send_str(json.dumps({"type": "cw_rx_state",
                                                  "enabled": True}))
                except Exception:
                    pass

        elif t == "ft8_rx_enable":
            # Turns the shared RX decoder on/off (for ALL clients, see the
            # broadcast below) - without this gate, any viewer could
            # remotely kill decoding for everyone, including the operator
            # holding the TRX.
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            enabled = bool(msg.get("enabled", True))
            mode = self._ft8_decode_mode  # "FT8" or "FT4"
            # Record who enabled it (for auto-stop on disconnect / releasing
            # the radio). uid is fetched from online_users since _ws_msg
            # doesn't have it as a parameter.
            sender_uid = self.online_users.get(ws, {}).get("user_id", "")
            if enabled:
                self._ft8_rx_owner_uid = sender_uid
                print(f"[ft8rx] ENABLED ({mode}) by uid={sender_uid}", flush=True)
            else:
                self._ft8_rx_owner_uid = None
                print(f"[ft8rx] disabled ({mode})", flush=True)
            self._ft8_rx_enabled = enabled
            # Forward to Rust — ham_audio.exe starts/stops the decode loop
            if self.rust_audio:
                await self.rust_audio.ft8_enable_rx(enabled, mode)
            await self.hub.broadcast({"type": "ft8_rx_status", "enabled": enabled})
            await self.hub.broadcast({"type": "wsjtx_status", "running": enabled,
                                       "decoding": enabled, "transmit": False})

        elif t == "ft8_set_tx_freq":
            # Set the target TX frequency (e.g. dragging the TX marker on
            # the waterfall). Respects freeze and split mode (min
            # frequency). This is the TX parameter (where transmission will
            # actually go), unlike ft8_set_rx_freq (deliberately without a
            # gate - see the comment there) - this requires holding the radio.
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            if self._ft8_tx_frozen:
                # Frozen — ignore change requests, reply with the current state
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
            # Set the RX marker (Rx Frequency panel) — completely
            # independent of TX, no split/lock logic (you can listen
            # anywhere in the band). DELIBERATELY without _can_control_radio -
            # this is only where we're LISTENING, it doesn't affect TX or
            # other clients in a way that would require owning the radio.
            freq = msg.get("freqHz")
            try:
                freq = float(freq)
            except (TypeError, ValueError):
                return
            freq = max(200.0, min(2900.0, freq))
            self._ft8_rx_freq_hz = freq
            await self.hub.broadcast({"type": "ft8_rx_freq", "freqHz": freq})

        elif t == "ft8_set_both_freq":
            # A left click on the waterfall (away from existing markers)
            # sets BOTH markers (RX and TX) at once to the same new
            # position. Each can then be dragged separately
            # (ft8_set_tx_freq / ft8_set_rx_freq). Unlike plain
            # ft8_set_rx_freq - this click typically means "I'm picking
            # this station to call", i.e. it actually targets TX, so
            # (unlike just moving the RX marker) it requires holding the radio.
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
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
            # "RX=TX" button: moves the RX marker to the current TX position.
            # DELIBERATELY without _can_control_radio - just the RX marker,
            # see the comment at ft8_set_rx_freq.
            self._ft8_rx_freq_hz = self._ft8_tx_freq_hz
            print(f"[ft8] RX=TX -> {self._ft8_rx_freq_hz:.0f}Hz")
            await self.hub.broadcast({"type": "ft8_rx_freq", "freqHz": self._ft8_rx_freq_hz})

        elif t == "ft8_tx_eq_rx":
            # "TX=RX" button: the reverse of the above - moves the TX
            # marker to the current RX position. Respects freeze (Hold Tx
            # Freq) and split mode (min frequency) the same way as manually
            # dragging the TX marker (ft8_set_tx_freq above). TX marker ->
            # requires holding the radio, same as ft8_set_tx_freq.
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            if self._ft8_tx_frozen:
                await ws.send_json({"type": "ft8_tx_freq", "freqHz": self._ft8_tx_freq_hz,
                                     "frozen": True})
                return
            freq = self._ft8_rx_freq_hz
            if self._ft8_split_enabled and freq < self._ft8_split_min_hz:
                freq = self._ft8_split_min_hz
            freq = max(200.0, min(2900.0, freq))
            self._ft8_tx_freq_hz = freq
            print(f"[ft8] TX=RX -> {freq:.0f}Hz")
            await self.hub.broadcast({"type": "ft8_tx_freq", "freqHz": freq,
                                       "frozen": self._ft8_tx_frozen})

        elif t == "ft8_toggle_tx_freeze":
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            self._ft8_tx_frozen = bool(msg.get("frozen", not self._ft8_tx_frozen))
            print(f"[ft8] TX {'FROZEN' if self._ft8_tx_frozen else 'unfrozen'} @ {self._ft8_tx_freq_hz:.0f}Hz"
                  + (" — RX will automatically follow calls addressed to us" if self._ft8_tx_frozen else ""))
            await self.hub.broadcast({"type": "ft8_tx_freq", "freqHz": self._ft8_tx_freq_hz,
                                       "frozen": self._ft8_tx_frozen})

        elif t == "ft8_toggle_split":
            # Reserved mechanism (see the comment at self._ft8_split_enabled
            # in __init__) - currently no caller in the frontend, but since
            # it controls the TX frequency threshold, it gets the same gate
            # as the rest of the TX parameters in this block for consistency.
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            self._ft8_split_enabled = bool(msg.get("enabled", not self._ft8_split_enabled))
            min_hz = msg.get("minHz")
            if min_hz is not None:
                try:
                    self._ft8_split_min_hz = max(200.0, min(2900.0, float(min_hz)))
                except (TypeError, ValueError):
                    pass
            # If split is enabled and the current TX freq is below the threshold, raise it
            if self._ft8_split_enabled and self._ft8_tx_freq_hz < self._ft8_split_min_hz:
                self._ft8_tx_freq_hz = self._ft8_split_min_hz
            print(f"[ft8] SPLIT {'enabled' if self._ft8_split_enabled else 'disabled'} (min={self._ft8_split_min_hz:.0f}Hz)")
            await self.hub.broadcast({"type": "ft8_split_status",
                                       "enabled": self._ft8_split_enabled,
                                       "minHz": self._ft8_split_min_hz})
            await self.hub.broadcast({"type": "ft8_tx_freq", "freqHz": self._ft8_tx_freq_hz,
                                       "frozen": self._ft8_tx_frozen})

        # ── QSO automation (full FT8 automation) ──────────────────────────────
        elif t == "ft8_toggle_auto_seq":
            # NOTE: the automation is ALWAYS active since we removed the
            # toggle from the UI. We only get this message for JS/WS
            # handshake compatibility — we ignore the 'enabled' content and always return true.
            self._auto_seq_enabled = True
            await self.hub.broadcast({"type": "auto_seq_status",
                                       "enabled": True,
                                       "call1st": self._auto_call_1st,
                                       "state": self._qso_engine.state,
                                       "partner": self._qso_engine.partner_call,
                                       "queue": list(self._qso_engine.queue)})

        elif t == "ft8_toggle_call_1st":
            # Gate: Call 1st ON lets the automation independently ANSWER
            # every heard CQ and TRANSMIT without a further operator
            # action (see _process_auto_qso -> 'enqueue' -> start_qso ->
            # _send_auto_tx). _ft8_tx_sequence_inner only checks radio_lock
            # if _autoqso_uid was ALREADY set (a safety net for the radio
            # being taken over WHILE the automation runs) - without this
            # check here, any logged-in viewer (who by definition can only
            # WATCH, see _can_control_radio) could enable Call 1st without
            # holding the lock and trigger a real PTT/TX on the first
            # matching decode, even when NO ONE holds the radio.
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            self._auto_call_1st = bool(msg.get("enabled", not self._auto_call_1st))
            print(f"[autoqso] Call 1st {'ENABLED' if self._auto_call_1st else 'disabled'}")
            # Clicking this checkbox is an operator action - it counts as
            # proof of presence on its own (similar to a manual decode
            # click/TX macro in the frontend), regardless of which
            # direction it toggles.
            self._ft8_operator_present = True
            await self.hub.broadcast({"type": "auto_seq_status",
                                       "enabled": self._auto_seq_enabled,
                                       "call1st": self._auto_call_1st,
                                       "state": self._qso_engine.state,
                                       "partner": self._qso_engine.partner_call,
                                       "queue": list(self._qso_engine.queue)})

        elif t == "ft8_timer_expired":
            # The FT8 safety timer (WSJT-X's "Tx Watchdog") expired on the
            # frontend (FT8Timer._stopTX() in wsjtx.js) - the frontend
            # already called WSJTX.haltTx() (aborts the CURRENT
            # transmission), but without THIS flag the automation would
            # catch the next caller again in a moment despite Call 1st
            # being on, which made the whole timer useless (it was
            # supposed to guard the max transmit time, but only aborted at
            # most one send). _process_auto_qso checks this flag right at
            # the start and ignores everything until ft8_timer_confirm arrives.
            self._ft8_operator_present = False
            print("[autoqso] Safety timer expired — automation locked until confirmed")

        elif t == "ft8_timer_confirm":
            self._ft8_operator_present = True
            print("[autoqso] Operator confirmed presence — automation unlocked")

        elif t == "ft8_start_auto_qso":
            call_de = (msg.get("callDe") or "").strip().upper()
            if not call_de or call_de == "CQ":
                return
            if not self._auto_seq_enabled:
                await ws.send_json({"type": "auto_qso_error",
                                     "error": "Wlacz najpierw Auto-Sequencing"})
                return
            # Update the QSO engine's callsign/grid to the currently logged-in user
            user_info = self.online_users.get(ws, {})
            user_call = user_info.get("callsign", "").strip().upper()
            user_uid  = user_info.get("user_id")
            # Radio lock: only the operator holding the radio (or admin)
            # may start an automatic QSO - see the identical condition in
            # "ft8_tx" above.
            if self.radio_lock["user_id"] and not self._user_has_lock(user_uid) and role != "admin":
                _holder = self.radio_lock["callsign"] or self.radio_lock["username"] or "?"
                await self.hub.broadcast({"type": "toast", "msg": f"⛔ FT8 TX zablokowany — radio ma {_holder}", "level": "error"})
                return
            # Remember the operator's uid - needed to AUTO-SAVE the QSO to
            # their log when it completes (qso_complete).
            self._autoqso_uid = user_uid
            if user_call:
                u_obj = self.find_user_by_id(user_uid) or {}
                user_grid = (u_obj.get("locator") or LOCATOR).strip().upper()[:4]
                if self._qso_engine.my_call != user_call or self._qso_engine.my_grid != user_grid:
                    self._qso_engine.my_call = user_call
                    self._qso_engine.my_grid = user_grid
                    print(f"[autoqso] User: {user_call} / {user_grid}")
            print(f"[autoqso] Manual start of an automatic QSO with {call_de}")
            # Ignore if TX is already scheduled/running for this partner
            if (self._ft8_tx_lock.locked() and
                    self._qso_engine.partner_call == call_de):
                print(f"[autoqso] TX already running for {call_de} — ignoring the duplicate")
                return
            # Abort the previous TX if it's a different station
            if self._ft8_tx_lock.locked():
                self._ft8_tx_abort = True
            # Unlock the period — otherwise _send_auto_tx inherits the
            # period frozen by the PREVIOUS station (see the comment at
            # _qso_period_locked=False in _advance_auto_qso_queue) and
            # never switches to the right one for THIS freshly picked
            # station. Every OTHER path that ends a QSO (auto-complete,
            # give-up, manual abort, queue advance) already did this — this
            # manual start was missing it, despite being the most common operator path.
            self._qso_period_locked = False
            # If the frontend passed the decoded TEXT (the station is
            # answering us), parse it and pass it as initial_decode - then
            # the engine skips ahead to the right step (report) instead of
            # sending Tx1/grid from scratch. This fixes: "I call a station,
            # it answers later, I click RX, but a grid gets sent instead of a report".
            _msg = (msg.get("message") or "").strip()
            _initial = None
            if _msg:
                try:
                    _initial = qso_engine.parse_message(_msg)
                except Exception as _e:
                    print(f"[autoqso] can't parse '{_msg}': {_e}")
                    _initial = None
            # partner_decode is used to lock the period based on the EXACT
            # RECEIVE timestamp of the clicked decode (recvEpoch, see
            # _period_from_epoch) - the frontend got it together with this
            # decode in the original broadcast and sends it back
            # UNCHANGED, so it's computed from the receive moment, NOT the
            # "now" clock at the time the click is processed. This way the
            # rest of the QSO lands in the correct windows regardless of
            # how long the operator waited before clicking the station in the list.
            _recv_epoch = msg.get("recvEpoch")
            _partner_decode = ({"recvEpoch": _recv_epoch, "snr": msg.get("snr", 0)}
                                if _recv_epoch is not None else None)
            start_result = self._qso_engine.start_qso(call_de, initial_decode=_initial)
            await self.hub.broadcast({"type": "auto_qso_status",
                                       "state": self._qso_engine.state,
                                       "partner": self._qso_engine.partner_call})
            if start_result and start_result.get("action") == "reply":
                # The clicked decode was already the partner's reply (not a
                # CQ) - the engine skipped Tx1, send the right step right away.
                await self._dispatch_auto_reply(start_result, _partner_decode or {},
                                                 tx_seq=self._reserve_tx_seq())
            else:
                asyncio.create_task(self._send_auto_tx(
                    self._qso_engine.next_tx_action(), partner_decode=_partner_decode,
                    tx_seq=self._reserve_tx_seq()))

        elif t == "ft8_queue_remove":
            # Remove a station from the "Call 1st" queue (the ✕ button on the chip in the UI).
            _qcall = (msg.get("call") or "").strip().upper()
            if _qcall and self._qso_engine.remove_from_queue(_qcall):
                print(f"[autoqso] Removed {_qcall} from the queue (manual ✕)")
                await self.hub.broadcast({"type": "auto_qso_queue",
                                           "queue": list(self._qso_engine.queue)})

        elif t == "ft8_queue_clear":
            # Empty the whole "Call 1st" queue (the "clear" button in the UI).
            _n = len(self._qso_engine.queue)
            self._qso_engine.clear_queue()
            print(f"[autoqso] Cleared the queue ({_n} stations)")
            await self.hub.broadcast({"type": "auto_qso_queue",
                                       "queue": list(self._qso_engine.queue)})

        elif t == "ft8_abort_auto_qso":
            # Manual "skip" — the operator doesn't want to wait for the
            # automatic retransmit-limit exhaustion (should_give_up in
            # qso_engine.py), just drops the current station immediately
            # and moves on to the next one in the Call 1st queue.
            print(f"[autoqso] Manual abort of the QSO with {self._qso_engine.partner_call}")
            self._qso_engine.abort_qso()
            self._autoqso_tx_seq += 1  # see the comment at REST /api/ft8/halt
            self._ft8_tx_abort = True
            # _qso_period_locked=False does NOT hard-set a wrong period —
            # the period for the next station will be freshly detected
            # from its FIRST real reply anyway (_dispatch_auto_reply passes
            # a fresh partner_decode to _send_auto_tx), so the entire next
            # contact lands in the correct windows, instead of a "frozen"
            # period left over from the previous station.
            self._qso_period_locked = False
            await self.hub.broadcast({"type": "auto_qso_status",
                                       "state": self._qso_engine.state,
                                       "partner": None})
            await self._advance_auto_qso_queue()

        elif t == "ft8_set_tx_period":
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            period = int(msg.get("period", 1))
            if period not in (1, 2):
                return
            self._ft8_tx_period = period
            print(f"[ft8] Transmit period set to: {period}")
            await self.hub.broadcast({"type": "ft8_tx_period", "period": period})

        elif t == "ft8_toggle_fake_split":
            # Enable/disable Fake Split (see _apply_fake_split_before_tx).
            # State remembered in the config — survives a server restart.
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            self._fake_split_enabled = bool(msg.get("enabled", not self._fake_split_enabled))
            self.cfg.setdefault("ft8", {})["fakeSplit"] = self._fake_split_enabled
            save_json(CFG_F, self.cfg)
            print(f"[ft8] Fake Split {'ENABLED' if self._fake_split_enabled else 'disabled'}")
            await self.hub.broadcast({"type": "ft8_fake_split_status",
                                       "enabled": self._fake_split_enabled,
                                       "targetHz": 1500})

        elif t == "ft8_set_decode_mode":
            can, why = self._can_control_radio(ws, role)
            if not can:
                await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
                return
            mode = msg.get("mode", "FT8")
            if mode not in ("FT8", "FT4"):
                return
            self._ft8_decode_mode = mode
            print(f"[ft8] Decode mode set to: {mode}")
            # Forward to Rust — change the decode mode
            if self.rust_audio:
                await self.rust_audio.ft8_enable_rx(self._ft8_rx_enabled, mode)
            await self.hub.broadcast({"type": "ft8_decode_mode", "mode": mode})

        # ── WebRTC signaling (offer/answer/ICE) ────────────────────────────
        elif t == "webrtc_offer":
            if not self.webrtc:
                await ws.send_json({"type": "webrtc_error", "error": "WebRTC niedostepne na serwerze"})
                return
            # Start TX playback (self.audio.start_tx) before attaching the track
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

        # ── Dynamic actions/sliders (from dump_caps: VFO A/B, functions, levels) ──
        elif t == "rig_action":
            await self._handle_rig_action(msg, ws, role)

        elif t == "rig_slider":
            await self._handle_rig_slider(msg, ws, role)

    async def _dynamic_allowed(self, dynamic_id: str, role: str, kind: str) -> bool:
        """Check the whitelist for a dynamic element (action/slider) by id.
        Admin is always allowed. Enabled by default (enabled_dynamic.get(id, True))."""
        if role == "admin":
            return True
        # KEYSPD (WPM) is allowed for all users
        if dynamic_id == "level_keyspd":
            return True
        rig_id = self._current_rig_id()
        enabled_dyn = self._get_enabled_dynamic(rig_id)
        if not enabled_dyn.get(dynamic_id, True):
            return False
        # Check that the element actually exists in the current capabilities
        caps = getattr(self, "_caps_cache", None) or {"actions": [], "sliders": []}
        items = caps.get("actions" if kind == "action" else "sliders", [])
        return any(i["id"] == dynamic_id for i in items)

    async def _cq_calling_loop(self):
        """
        Periodic CQ-calling loop. Transmits CQ, waits until the end of the
        full period (2 windows of 15s = 30s for FT8), checks whether we're
        still calling, repeats.

        Stops when:
          - self._cq_calling = False (user stop / timer / someone answered)
          - the engine entered an active QSO (someone answered our CQ)
          - PTT taken by something else / abort

        A reply to our CQ is detected in _process_auto_qso (on_decode) —
        when a station answers, we call _stop_cq_calling and the engine takes over the QSO.
        """
        # STABILITY: the try is INSIDE the while - a single error (e.g. the
        # radio momentarily not responding) doesn't kill the loop. A
        # consecutive-error counter guards against spinning forever during
        # a persistent failure.
        _consecutive_errors = 0
        _MAX_ERRORS = 5
        try:
            while self._cq_calling:
                try:
                    # If the engine has an active QSO - someone answered, stop calling
                    if self._qso_engine.is_active():
                        print("[cq] QSO active - stopping CQ calling")
                        break
                    call_de = self._cq_call_de
                    report = self._cq_report
                    if not call_de:
                        break
                    # Remember the CQ text (to recognize a reply in on_decode)
                    self._auto_cq_text = f"CQ {call_de} {report}".strip()
                    # Transmit one CQ (via the shared path - respects window/PTT/lock)
                    await self._ft8_tx_sequence("CQ", call_de, report, False)
                    _consecutive_errors = 0  # success - reset the counter
                    # _ft8_tx_sequence ends near the end of the period.
                    # Check whether we're still calling (a stop/reply may
                    # have arrived in the meantime).
                    if not self._cq_calling:
                        break
                    if self._qso_engine.is_active():
                        print("[cq] QSO active after CQ - stopping calling")
                        break
                    # Short pause so we don't immediately re-enter the same window.
                    await asyncio.sleep(0.5)
                except asyncio.CancelledError:
                    raise  # let the cancellation propagate upward
                except Exception as e:
                    _consecutive_errors += 1
                    print(f"[cq] CQ cycle error ({_consecutive_errors}/{_MAX_ERRORS}): {e}",
                          flush=True)
                    if _consecutive_errors >= _MAX_ERRORS:
                        print("[cq] too many consecutive errors - stopping CQ calling",
                              flush=True)
                        try:
                            await self.hub.broadcast({
                                "type": "toast",
                                "msg": "⛔ CQ zatrzymane - radio nie odpowiada",
                                "level": "error"})
                        except Exception:
                            pass
                        break
                    # Backoff before the next attempt
                    await asyncio.sleep(2.0)
        except asyncio.CancelledError:
            print("[cq] CQ loop cancelled")
        except Exception as e:
            print(f"[cq] CQ loop error: {e}")
        finally:
            self._cq_calling = False
            self._auto_cq_text = None

    def _stop_cq_calling(self):
        """Stop periodic CQ calling (user stop / reply / timer).
        Also sets _ft8_tx_abort to abort the CURRENT audio transmission,
        and cancels the loop task. Without _ft8_tx_abort, the current
        _ft8_tx_sequence would finish transmitting to the end despite CQ being stopped."""
        if self._cq_calling:
            print("[cq] Stopping periodic CQ calling")
        self._cq_calling = False
        self._auto_cq_text = None
        # Abort the current transmission (if a CQ is currently going out)
        self._ft8_tx_abort = True
        if self._cq_task and not self._cq_task.done():
            self._cq_task.cancel()
        self._cq_task = None

    @staticmethod
    def _compute_fake_split(dial_hz: float, desired_audio_hz: float) -> dict:
        """Computes Fake Split: how much to shift the VFO (dial) so the TX
        audio lands near the center of the SSB filter (~1500Hz) while
        preserving the invariant on-air-freq = dial + audio (must be
        identical before and after). Logic is 1:1 with
        fake_split_prototype.py (tested there separately, dry, without
        touching the radio, before being wired into the TX path here).

        Returns a dict: on_air_hz, split_needed, new_dial_hz, new_audio_hz,
        restore_dial_hz (= dial_hz, what to restore to after transmitting)."""
        TARGET_AUDIO_HZ = 1500.0
        AUDIO_MIN_HZ, AUDIO_MAX_HZ = 300.0, 2700.0
        VFO_STEP_HZ = 500.0  # block shifts — the radio can't keep up with continuous tuning

        on_air_hz = dial_hz + desired_audio_hz
        if AUDIO_MIN_HZ <= desired_audio_hz <= AUDIO_MAX_HZ and 600.0 <= desired_audio_hz <= 2400.0:
            # Audio is already far enough from the filter edge — split is unnecessary.
            return {"on_air_hz": on_air_hz, "split_needed": False,
                    "new_dial_hz": dial_hz, "new_audio_hz": desired_audio_hz,
                    "restore_dial_hz": dial_hz}

        raw_shift = desired_audio_hz - TARGET_AUDIO_HZ
        vfo_shift = round(raw_shift / VFO_STEP_HZ) * VFO_STEP_HZ
        new_dial_hz = dial_hz + vfo_shift
        new_audio_hz = on_air_hz - new_dial_hz  # complement to preserve the invariant
        return {"on_air_hz": on_air_hz, "split_needed": True,
                "new_dial_hz": new_dial_hz, "new_audio_hz": new_audio_hz,
                "restore_dial_hz": dial_hz}

    async def _apply_fake_split_before_tx(self):
        """If Fake Split is enabled, shifts the VFO BEFORE transmitting and
        swaps self._ft8_tx_freq_hz for a safe offset (~1500Hz) for the
        duration of THIS transmission — called once, right at the start of
        _ft8_tx_sequence_inner, BEFORE checking the PCM cache
        (self._pre_pcm_cache), which is cleared when a split is needed: the
        pre-generated PCM (see _send_auto_tx) was computed for the OLD
        audio offset before the VFO shift — using it now would send audio
        inconsistent with the new VFO position and break the on-air
        frequency invariant. The state to restore is kept in
        self._fake_split_state (None if there's nothing to restore —
        _restore_fake_split_after_tx() is then a safe no-op)."""
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
                print(f"[ft8] Fake Split set_freq error: {e!r}")
                self._fake_split_state = None
                return False
        self._ft8_tx_freq_hz = split["new_audio_hz"]
        self._pre_pcm_cache = None  # invalidate — computed for the old audio offset
        print(f"[ft8] Fake Split: VFO {split['restore_dial_hz']:.0f} -> "
              f"{split['new_dial_hz']:.0f}Hz, audio -> {split['new_audio_hz']:.0f}Hz "
              f"(on-air unchanged: {split['on_air_hz']:.0f}Hz)")
        await self.hub.broadcast({"type": "freq", "freq": int(split["new_dial_hz"])})
        await self.hub.broadcast({"type": "ft8_tx_freq", "freqHz": self._ft8_tx_freq_hz,
                                   "frozen": self._ft8_tx_frozen})
        return True

    async def _restore_fake_split_after_tx(self):
        """Restores the VFO and TX audio offset to the state before
        _apply_fake_split_before_tx(). A safe no-op when a split wasn't
        applied for the current transmission (self._fake_split_state is
        None) — called unconditionally in finally: after every TX, so it
        must tolerate being called when there's nothing to restore."""
        state = self._fake_split_state
        if not state:
            return
        self._fake_split_state = None
        self.rig.freq = state["dial_hz"]
        if not self.rig.sim:
            try:
                await self.rig.set_freq(state["dial_hz"])
            except Exception as e:
                print(f"[ft8] Fake Split restore set_freq error: {e!r}")
        self._ft8_tx_freq_hz = state["audio_hz"]
        print(f"[ft8] Fake Split: VFO restored -> {state['dial_hz']:.0f}Hz, "
              f"audio -> {state['audio_hz']:.0f}Hz")
        await self.hub.broadcast({"type": "freq", "freq": int(state["dial_hz"])})
        await self.hub.broadcast({"type": "ft8_tx_freq", "freqHz": self._ft8_tx_freq_hz,
                                   "frozen": self._ft8_tx_frozen})

    async def _ft8_tx_sequence(self, call_to: str, call_de: str, report: str, r_flag: bool = False, auto_respond: bool = False, tx_seq: int = None):
        """
        A mutex wrapper around _ft8_tx_sequence_inner — prevents two
        transmissions from running in parallel (e.g. the automation +
        a manual click, or two automatic replies from the same RX window).
        If the lock is already held, this message WAITS in the
        asyncio.Lock queue (FIFO), instead of colliding with the ongoing
        PTT — which in practice means a send attempt during another
        transmission is simply DELAYED to the next free window (since
        _ft8_tx_sequence_inner waits for the next 15s window anyway, so in
        the end nothing is lost, it just possibly shifts by one cycle).
        """
        async with self._ft8_tx_lock:
            await self._ft8_tx_sequence_inner(call_to, call_de, report, r_flag, auto_respond=auto_respond, tx_seq=tx_seq)

    async def _ft8_tx_sequence_inner(self, call_to: str, call_de: str, report: str, r_flag: bool = False, auto_respond: bool = False, tx_seq: int = None):
        """
        The full FT8 transmit sequence: encode -> wait for the 15s UTC
        window -> PTT ON -> stream audio (20ms chunks via feed_tx_pcm) ->
        PTT OFF. Run as a separate task (asyncio.create_task), doesn't block the WS loop.
        """
        # Radio lock, a safety net for automation ALREADY IN PROGRESS
        # (retransmits, QSO continuation, the Call 1st queue) - these calls
        # don't have a single WS "sender" to check, so we compare against
        # the remembered _autoqso_uid (the operator who actually started
        # this QSO/CQ - see "ft8_tx"/"ft8_start_auto_qso" above, where the
        # real "does the sender hold the lock" check already blocked an
        # unauthorized START of a transmission). If the radio was taken
        # over by SOMEONE ELSE in the meantime (not released - that's
        # fine, simply not holding the lock doesn't block), stop the
        # automation instead of transmitting unsupervised by the operator who started it.
        if (self.radio_lock["user_id"] and self._autoqso_uid and
                self.radio_lock["user_id"] != self._autoqso_uid):
            print(f"[ft8] TX held back — radio taken over by another operator "
                  f"({self.radio_lock.get('callsign') or self.radio_lock.get('username')})")
            return
        # Block TX on a disallowed band
        if not self._is_band_allowed():
            await self.hub.broadcast({"type": "toast", "msg": "⛔ FT8 TX zablokowany — pasmo niedozwolone przez admina", "level": "error"})
            return
        # Block FT8/FT4 on a cross-band split (protects the radio)
        cross, band_a, band_b = self._is_split_cross_band()
        if cross:
            await self.hub.broadcast({"type": "toast",
                "msg": f"⛔ FT8/FT4 TX zablokowany — split cross-band ({band_a} RX / {band_b} TX). Wylacz split.",
                "level": "error"})
            return
        # Don't clear the abort flag if periodic CQ was just stopped
        # (stop/timer) — otherwise this transmission would ignore the stop
        # request and keep transmitting.
        if not (call_to == "CQ" and not self._cq_calling):
            self._ft8_tx_abort = False
        else:
            print("[cq] CQ sequence skipped - CQ stopped")
            return
        # Whether PTT was actually turned on in THIS attempt — distinguishes
        # from an early return (e.g. a stale tx_seq, an abort before PTT, an
        # audio start_tx error). Confirmed live: a "-07" retransmit was
        # correctly rejected as stale (a stale tx_seq) BEFORE PTT, but the
        # finally: block still did "hold the mutex until the end of the
        # period" (up to a 15s sleep) as if something had actually been
        # transmitting — which blocked the real "73" (waiting in the queue
        # for the same _ft8_tx_lock) until the NEXT period. Effect observed
        # live: the correspondent didn't get 73 in time and repeated RRR.
        ptt_was_on = False
        try:
            # EARLY stale-tx_seq check — same condition as the one right
            # before PTT below, but run BEFORE any PCM generation and
            # BEFORE the "ft8_tx_status: waiting" broadcast. Without this,
            # a task queued behind the mutex (e.g. a retry like "-15"
            # scheduled just before the real "73") only discovers it's
            # stale AFTER waiting out the full TX window — but the
            # "waiting" broadcast already went out with THIS action's text
            # moments earlier, so the operator sees a "will transmit -15"
            # popup for a message that was already superseded and would
            # never actually go on air. Observed live twice (ON9DC, F4EIK):
            # the backend correctly skipped the stale retry both times, but
            # the misleading popup made it look like the automation "went
            # crazy" and wanted to resend after the QSO was already logged.
            if auto_respond and call_to != "CQ" and tx_seq is not None:
                if tx_seq != self._autoqso_tx_seq:
                    print(f"[autoqso] TX '{call_to} {call_de} {report}' stale "
                          f"(tx_seq={tx_seq}, current={self._autoqso_tx_seq}) — skipping early")
                    return
            is_ft4 = (self._ft8_decode_mode == "FT4")
            # SYNC txVolume BEFORE TX: audio.cfg is a reference that can
            # drift apart from the main config (re-login, reload) -> audio
            # took the old/default value instead of the saved one -> high
            # ALC despite the UI and config showing 2. Here we FORCE audio
            # to use the current txVolume from config.json right before transmitting.
            if "audio" in self.cfg:
                self.audio.cfg = self.cfg["audio"]
                _tv_now = self.cfg["audio"].get("txVolume", 4.0)
                print(f"[audio] txVolume synced before TX = {_tv_now}x", flush=True)
            # Truncate the grid to 4 characters (FT8/FT4 doesn't support a 6-char locator)
            if len(report) == 6 and report[:2].isalpha() and report[2:4].isdigit():
                report = report[:4]
                print(f"[ft8] Grid truncated to 4 characters: {report}")
            import time as _tx_time
            # Fake Split BEFORE checking the PCM cache — if it shifts the
            # VFO and changes the audio offset, it clears _pre_pcm_cache
            # (see the docstring), so it must happen before we read the cache below.
            await self._apply_fake_split_before_tx()
            # Use the pre-generated PCM if it matches (call_to/call_de/report)
            cache = self._pre_pcm_cache
            if (cache and cache[0] == call_to and cache[1] == call_de and
                    cache[2] == report):
                # cache: (call_to, call_de, report, pcm, dur, ldpc_valid)
                pcm_bytes = cache[3]
                duration = cache[4]
                _cached_ldpc = cache[5] if len(cache) > 5 else None
                debug = {"ldpc_valid": _cached_ldpc}
                self._pre_pcm_cache = None
                print(f"[ft8] PCM from cache ({duration:.2f}s, ldpc_valid={_cached_ldpc})")
            elif is_ft4:
                pcm_bytes, debug, duration = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: ft4_encoder.generate_tx_pcm48k_ft4(
                        call_to, call_de, report, r_flag, base_freq_hz=self._ft8_tx_freq_hz))
            else:
                pcm_bytes, debug, duration = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: ft8_encoder.generate_tx_pcm48k(
                        call_to, call_de, report, r_flag, base_freq_hz=self._ft8_tx_freq_hz))
            # BUILD VERSION MARKER - confirms which code version is in the
            # EXE. CHANGED on every significant fix. If you see an OLD
            # marker after rebuilding the EXE = PyInstaller packaged the wrong webapp.py.
            print(f"[build] webapp.py wersja BUILD-2026-08-24-RETRY-AND-PERIOD-FIX, ldpc_valid={debug.get('ldpc_valid')}", flush=True)
            if not debug.get("ldpc_valid"):
                print(f"[{'ft4' if is_ft4 else 'ft8'}] WARNING: ldpc_valid=False for '{call_to} {call_de} {report}' — sending anyway")

            # The text shown in the UI (TX status, macro highlight) MUST
            # include the "R" prefix when r_flag=True, otherwise the
            # frontend can't visually distinguish "R+report" (an
            # acknowledgment) from a plain first report — this was the
            # root cause of a bug where the wrong macro button got
            # highlighted for replies like "R-18".
            display_report = f"R{report}" if r_flag else report
            display_text = f"{call_to} {call_de} {display_report}"

            # Make sure audio TX playback is running (self.audio.start_tx,
            # same call as the WebRTC path above) — done BEFORE waiting for
            # the window, so we don't waste time at the critical moment
            # the transmission starts.
            if not self.audio.tx_active:
                dev = self.cfg.get("audio", {}).get("txDevice")
                ok = self.audio.start_tx(device=dev)
                print(f"[ft8] audio start_tx: {'OK' if ok else 'ERROR'} dev={dev}")
                if not ok:
                    await self.hub.broadcast({"type": "ft8_tx_status", "status": "error",
                                               "error": "Nie udalo sie uruchomic audio TX"})
                    return

            # ── Sync to the right UTC window per the selected transmit
            # period and mode (FT8: 15s xx:00/30 or xx:15/45; FT4: 7.5s) ──
            window_s = ft4_encoder.FT4_SLOT_TIME if is_ft4 else 15.0
            import time as _time
            now = _time.time()
            pos_in_window = now % window_s

            # Determine the start of the next window of our period (1 or 2)
            # Period 1: windows 0, 2, 4... (even windows) — xx:00/30 for FT8
            # Period 2: windows 1, 3, 5... (odd windows) — xx:15/45 for FT8
            # SHARED logic (manual TX and automation): if "now" falls
            # INSIDE our own window (self._ft8_tx_period), transmit
            # IMMEDIATELY — regardless of how many seconds into this window
            # we already are — as long as there's enough time left in the
            # window for the WHOLE transmission (duration, ~12.64s FT8 /
            # ~4.5s FT4). Only if there ISN'T room for it anymore (or we're
            # in the partner's window at all) do we wait for the next
            # occurrence of our parity.
            #
            # The EARLIER VERSION rejected a window once a fixed 1.5s
            # threshold from its start had passed (regardless of how much
            # time was actually left) and then waited a WHOLE EXTRA PERIOD
            # (up to 30s) — in practice clicking a station 1-2s "too late"
            # (while still having ~13-14s of margin in the window!) delayed
            # a manual QSO start by over half a minute, even though the
            # window itself was still fully usable. The correct criterion
            # is the physical "does the transmission fit", not an arbitrary
            # fixed reaction-time threshold.
            full_period_s = window_s * 2
            offset = 0.0 if self._ft8_tx_period == 1 else window_s
            pos_in_full = now % full_period_s
            if pos_in_full < offset:
                # We're in the partner's window, BEFORE our window starts
                wait_s = offset - pos_in_full
            elif pos_in_full < offset + window_s:
                # We're in OUR window — transmit if there's enough time
                # left for the whole transmission, otherwise wait for the next occurrence
                remaining = offset + window_s - pos_in_full
                if remaining < duration:
                    wait_s = remaining + full_period_s - window_s
                else:
                    wait_s = 0.0
            else:
                # We're already in the partner's window AFTER ours — wait
                # for the next occurrence of our parity
                wait_s = full_period_s - pos_in_full + offset
            print(f"[ft8] TX period={self._ft8_tx_period} auto={auto_respond}, "
                  f"position={pos_in_window:.1f}s, dur={duration:.1f}s, waiting {wait_s:.2f}s")
            await self.hub.broadcast({"type": "ft8_tx_status", "status": "waiting",
                                       "text": display_text,
                                       "waitSeconds": round(wait_s, 2)})
            print(f"[ft8] Waiting {wait_s:.2f}s for the 15s UTC window...")
            # Sleep in short chunks, so abort also works while waiting.
            # CRITICAL: the target is computed as an ABSOLUTE clock
            # timestamp (_time.time() + wait_s), NOT as a sum of nominal
            # steps (waited += step). asyncio.sleep(0.1) guarantees at
            # least 0.1s, but under a loaded event loop (TX audio playback
            # and txmeter polling every ~100ms run concurrently in the
            # background) it actually always takes a bit longer. Over
            # dozens of iterations (e.g. 147 for a 14.69s wait) this small
            # per-iteration overrun accumulated to ~1s of drift — measured
            # live: the DT of a CQ call came out +1.0s instead of near 0.
            # Computing against a FIXED target self-corrects (each
            # iteration measures the real remaining distance), so the drift doesn't accumulate.
            target_time = _time.time() + wait_s
            while True:
                remaining = target_time - _time.time()
                if remaining <= 0:
                    break
                if self._ft8_tx_abort:
                    print("[ft8] TX aborted while waiting for the window")
                    await self.hub.broadcast({"type": "ft8_tx_status", "status": "done"})
                    return
                await asyncio.sleep(min(0.1, remaining))

            await self.hub.broadcast({"type": "ft8_tx_status", "status": "starting",
                                       "text": display_text})

            if self._ft8_tx_abort:
                print("[ft8] TX cancelled before PTT (abort)")
                await self.hub.broadcast({"type": "ft8_tx_status", "status": "done"})
                return

            # A final freshness check RIGHT BEFORE PTT (not at the
            # scheduling stage) — the automation schedules a send as a
            # separate task (asyncio.create_task) that waits for the right
            # window (up to ~15-30s, see above). During that time a NEWER
            # action MAY get scheduled (e.g. the partner answered before
            # this old one reached PTT). Observed live: a retransmit of
            # report "+08" (no reply - repeating) and the final "73" (the
            # partner had actually already replied RR73 IN THE MEANTIME)
            # were scheduled almost simultaneously from the same batch of
            # decodes — "73" managed to transmit and LOG the QSO, while the
            # stale "+08" still went out AFTER the fact.
            #
            # The FIRST version of this guard (partner_call==call_to AND
            # is_active()) was WRONG in a different way: the final "73"
            # itself puts the engine into DONE state (is_active()==False)
            # as a DIRECT consequence of generating THIS VERY action — so
            # it blocked exactly the message that legitimately ends the QSO
            # (observed live: "partner got RR73, the automation never
            # confirmed 73"). So the engine's state at any given moment
            # says nothing about whether THIS SPECIFIC scheduled action is
            # still current or already stale — because the engine
            # "finishes" itself as a natural side effect of SENDING this
            # very action.
            #
            # The correct criterion: a sequence number (_autoqso_tx_seq,
            # see _send_auto_tx) assigned at the moment this action was
            # SCHEDULED. If ANY newer automatic action has been scheduled
            # since then (regardless of what state that left the engine
            # in), this one is by definition stale. Doesn't apply to CQ
            # (auto_respond=False for periodic CQ) or manual TX (tx_seq=None then).
            if auto_respond and call_to != "CQ" and tx_seq is not None:
                if tx_seq != self._autoqso_tx_seq:
                    print(f"[autoqso] TX '{call_to} {call_de} {report}' stale "
                          f"(tx_seq={tx_seq}, current={self._autoqso_tx_seq}) — skipping")
                    await self.hub.broadcast({"type": "ft8_tx_status", "status": "done"})
                    return

            import time as _ttt
            _t0 = _ttt.time()
            _pos = _t0 % (ft4_encoder.FT4_SLOT_TIME if is_ft4 else 15.0)
            print(f"[ft8] before PTT: position in window={_pos:.3f}s")
            await self.rig.set_ptt(True)
            ptt_was_on = True
            print(f"[ft8] PTT ON took: {(_ttt.time()-_t0)*1000:.0f}ms")
            await self.hub.broadcast({"type": "ptt", "ptt": True})
            print(f"[ft8] TX START: '{call_to} {call_de} {report}' ({duration:.2f}s) DT={_pos:.2f}s")

            # Our own transmission as a 'wsjtx_decode' entry (is_tx=True) —
            # without this, the RX window (Band Activity) never saw OUR
            # messages interleaved chronologically with received ones
            # during a QSO, only decodes received from Rust (which know
            # nothing about our TX).
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

            # TX DIAGNOSTICS: check audio state before sending
            print(f"[ft8] TX audio state: tx_active={self.audio.tx_active} "
                  f"tx_stream={self.audio._tx_stream is not None} "
                  f"pcm_bytes_len={len(pcm_bytes)} "
                  f"chunks={len(pcm_bytes)//1920} "
                  f"txVolume={self.audio.cfg.get('txVolume', 4.0) if hasattr(self.audio, 'cfg') else '?'} "
                  f"txDevice={self.cfg.get('audio', {}).get('txDevice', '?')}")

            # Send ALL chunks to the PCM queue at once.
            # bulk_tx=True disables the anti-lag drop in the TX loop -
            # without this the drop discarded 239/248 frames ("backlog") and
            # a 4.94s signal was left as 160ms of buzz -> zero power/ALC,
            # FT8 never made it on air.
            self.audio.bulk_tx = True
            chunks_sent = 0
            for chunk in ft8_encoder.chunk_pcm_bytes(pcm_bytes, chunk_samples=960):
                if self._ft8_tx_abort:
                    print("[ft8] TX aborted — clearing PCM queue")
                    # Clear the PCM queue so it doesn't keep transmitting
                    try:
                        while not self.audio._webrtc_pcm_queue.empty():
                            self.audio._webrtc_pcm_queue.get_nowait()
                    except Exception:
                        pass
                    break
                self.audio.feed_tx_pcm(chunk)
                chunks_sent += 1
            print(f"[ft8] sent {chunks_sent} PCM chunks to the queue "
                  f"(queue_size={self.audio._webrtc_pcm_queue.qsize()})")

            # Wait for the PCM to finish playing (duration + margin)
            elapsed = 0.0
            while elapsed < duration + 0.3:
                if self._ft8_tx_abort:
                    print("[ft8] TX aborted during playback")
                    try:
                        while not self.audio._webrtc_pcm_queue.empty():
                            self.audio._webrtc_pcm_queue.get_nowait()
                    except Exception:
                        pass
                    break
                await asyncio.sleep(0.1)
                elapsed += 0.1

        except Exception as e:
            print(f"[ft8] ERROR while transmitting: {e}")
            await self.hub.broadcast({"type": "ft8_tx_status", "status": "error", "error": str(e)})
        finally:
            self.audio.bulk_tx = False  # restore anti-lag for WebRTC voice
            try:
                await self.rig.set_ptt(False)
                await self.hub.broadcast({"type": "ptt", "ptt": False})
            except Exception as e:
                print(f"[ft8] ERROR while turning off PTT: {e}")
            # Fake Split: VFO moves back right after PTT OFF (no-op if
            # split wasn't applied for this transmission).
            await self._restore_fake_split_after_tx()
            await self.hub.broadcast({"type": "ft8_tx_status", "status": "done"})
            print("[ft8] TX END" if ptt_was_on else "[ft8] TX skipped (no PTT)")
            # Hold the mutex until the end of the current period (PTT
            # already OFF) so the next task doesn't step into the
            # correspondent's period — ONLY if PTT actually fired. On an
            # early return (stale tx_seq, abort before PTT, audio error)
            # nothing went on air, so there's nothing to "wait out" —
            # release the mutex IMMEDIATELY, so the next, still-current send
            # (e.g. the final "73") doesn't get stuck waiting on the lock
            # until the next period (see the comment at ptt_was_on).
            if ptt_was_on:
                import time as _time
                _window_s = ft4_encoder.FT4_SLOT_TIME if self._ft8_decode_mode == "FT4" else 15.0
                _remaining = _window_s - (_time.time() % _window_s)
                if 0.5 < _remaining < _window_s - 0.5:
                    print(f"[ft8] Waiting {_remaining:.1f}s until end of period (PTT OFF)")
                    await asyncio.sleep(_remaining)

    @staticmethod
    def _format_report(snr_db: float) -> str:
        """Formats a measured SNR (dB) as a standard FT8 report with a
        forced sign, e.g. -12, +05. The FT8 protocol range is -30..+49 —
        clamp extreme measurements to this range, since the encoder raises
        ValueError outside it."""
        snr_int = max(-30, min(49, int(round(snr_db))))
        return f"{snr_int:+03d}"

    async def _process_tx_freeze_rx_follow(self, m: dict):
        """
        Automatically follows the RX/TX markers to the station we're having a QSO with.

        WSJT-X standard: if you click CQ on freq X, call on X, and the
        station also replies from X, TX/RX stay on X. But if the station
        moves to a different freq in a later message (because it found a
        less crowded spot), TX/RX should follow it so the QSO doesn't break.

        Logic:
          - Is the message ADDRESSED TO US (call_to == self._qso_engine.my_call)? Yes/no
          - Does the new freq differ from the current one? Yes/no
          - RX ALWAYS moves to the new freq (RX freeze doesn't exist)
          - TX moves ONLY if there's no "Hold TX" (self._ft8_tx_frozen)
            - Hence the method's earlier name, _process_tx_freeze_rx_follow,
              referred to a specific case — now it does more broadly.

        Called for EVERY received decode (in _ft8_rx_loop), regardless of
        auto_seq_enabled. Because this station-tracking convenience works
        even under fully manual operation.
        """
        try:
            parsed = qso_engine.parse_message(m["message"])
            # Compare against the operator's actual live callsign
            # (self._qso_engine.my_call), not the static config CALLSIGN
            # constant. my_call is updated dynamically on CQ start / station
            # click; CALLSIGN stays the config default (placeholder/club
            # call) for the whole process lifetime. Using the wrong one here
            # meant this method's call_to match almost never fired for the
            # operator's real callsign, so the RX/TX frequency markers never
            # followed a correspondent even though QSO automation (which
            # already used my_call) completed normally.
            if parsed is None or parsed.get("call_to") != self._qso_engine.my_call:
                return
            new_freq = float(m.get("freq_hz", m.get("deltaFreq", self._ft8_rx_freq_hz)))

            # RX always follows the station transmitting to us
            if abs(new_freq - self._ft8_rx_freq_hz) >= 1.0:
                self._ft8_rx_freq_hz = new_freq
                await self.hub.broadcast({"type": "ft8_rx_freq", "freqHz": self._ft8_rx_freq_hz})

            # TX follows only if there's no Hold TX (user hasn't frozen TX)
            if not self._ft8_tx_frozen:
                if abs(new_freq - self._ft8_tx_freq_hz) >= 1.0:
                    self._ft8_tx_freq_hz = new_freq
                    await self.hub.broadcast({"type": "ft8_tx_freq",
                                               "freqHz": self._ft8_tx_freq_hz,
                                               "frozen": False})
        except Exception as e:
            print(f"[ft8] auto-follow ERROR: {e}")

    async def _sync_ap_hints(self):
        """Push the current (own call, partner, queue) to the Rust decoder
        as AP (a priori) decode hints - see RustAudioBridge.set_ap_hints
        and ham_audio's decode::ap module. Called once per completed
        FT8/FT4 decode cycle (_ft8_rx_loop's decode_stats handler) rather
        than from every individual state-change site - hints only need to
        be fresh by the next cycle, and this way there's exactly one place
        to keep in sync, not ~15.

        own_call comes from radio_lock (whoever currently holds the
        radio), NOT self._qso_engine.my_call - that engine is the
        AUTOMATION state machine, and my_call is only ever set from the
        two places that arm Call 1st / Auto-Sequencing (see the comments
        at those two assignment sites). A fully manual QSO (macros sent by
        hand, automation never engaged this session) never touches
        my_call at all, so it would stay at its startup placeholder
        forever - meaning AP's highest-value hypothesis ("addressed to MY
        call") would either never fire or fire against the WRONG
        callsign. radio_lock["callsign"] is set the moment ANYONE claims
        the radio (PRZEJMIJ TRX), independent of automation, so AP helps
        manual QSOs too, not just Call 1st/Auto-Sequencing. partner_call
        still comes from the QSO engine, since a fully manual QSO has no
        tracked "partner" concept there at all - the own-call hypothesis
        (the main value) still applies regardless."""
        if not self.rust_audio:
            return
        own_call = self.radio_lock.get("callsign") or ""
        if not own_call:
            return  # nobody holds the radio - no QSO in progress, nothing for AP to help with
        try:
            await self.rust_audio.set_ap_hints(
                own_call,
                self._qso_engine.partner_call,
                list(self._qso_engine.queue),
            )
        except Exception as e:
            print(f"[ap] hint sync error: {e}", flush=True)

    async def _advance_auto_qso_queue(self):
        """After a QSO ends or is abandoned: if Call 1st is enabled and
        the queue isn't empty, starts a QSO with the next station. The
        caller MUST first put the engine back in IDLE (abort_qso())."""
        if self._auto_call_1st and self._qso_engine.queue:
            next_call, next_recv_epoch = self._qso_engine.pop_next_from_queue()
            print(f"[autoqso] Next station from queue: {next_call}")
            # NOTE: no initial_decode here (this station replied earlier,
            # not in the same cycle) — start normally from our Tx1 (grid),
            # since we don't know whether its earlier message is still
            # current (it may have changed frequency/disappeared).
            self._qso_engine.start_qso(next_call)
            await self.hub.broadcast({"type": "auto_qso_status",
                                       "state": self._qso_engine.state,
                                       "partner": next_call})
            # WITHOUT this, _send_auto_tx inherited self._ft8_tx_period left
            # over from the PREVIOUS QSO instead of computing the right one
            # for THIS station — if the parities didn't match, we
            # transmitted in the partner's window (collision, no reply) on
            # random every-other auto-advance from the Call 1st queue.
            # next_recv_epoch is the receive time of the decode that added
            # this station to the queue (see enqueue_caller/
            # _period_from_epoch) — can be None if the station entered the
            # queue before this fix (server restart), in which case
            # _send_auto_tx safely leaves the period unchanged.
            self._qso_period_locked = False
            _partner_decode = ({"recvEpoch": next_recv_epoch}
                                if next_recv_epoch is not None else None)
            asyncio.create_task(self._send_auto_tx(self._qso_engine.next_tx_action(),
                                                    partner_decode=_partner_decode,
                                                    tx_seq=self._reserve_tx_seq()))

    async def _check_retry_or_giveup(self):
        """Bounded retry/give-up timer for 'no reply this period'.

        Shared by two callers (extracted 2026-08-24): the original
        'result is None' case (a decode arrived but wasn't relevant to our
        QSO) and 'partner_busy' (the partner is transmitting to someone
        else THIS period). Both mean the same thing to the timer -
        "no progress on our exchange this period" - so both should be
        governed by the same bounded retry count instead of duplicating
        this logic with a risk of the two copies drifting apart later.
        """
        _retry_period_s = 2 * (ft4_encoder.FT4_SLOT_TIME
                                if self._ft8_decode_mode == "FT4" else 15.0)
        _max_retries = 4
        if self._qso_engine.should_retransmit(_retry_period_s):
            if self._qso_engine.should_give_up(_max_retries):
                print(f"[autoqso] {self._qso_engine.partner_call} not "
                      f"responding after {_max_retries} tries — abandoning QSO")
                self._qso_engine.abort_qso()
                self._qso_period_locked = False
                self._autoqso_tx_seq += 1  # see comment at REST /api/ft8/halt
                await self.hub.broadcast({"type": "auto_qso_status",
                                           "state": "IDLE", "partner": None})
                await self._advance_auto_qso_queue()
            elif self._last_auto_tx_action:
                self._qso_engine.note_retry()
                print(f"[autoqso] No reply from "
                      f"{self._qso_engine.partner_call} — retrying "
                      f"(attempt {self._qso_engine.retry_count}/{_max_retries}): "
                      f"{self._last_auto_tx_action['call_to']} "
                      f"{self._last_auto_tx_action['call_de']} "
                      f"{self._last_auto_tx_action.get('report_or_grid')}")
                asyncio.create_task(self._send_auto_tx(
                    self._last_auto_tx_action, tx_seq=self._reserve_tx_seq()))

    async def _process_auto_qso(self, m: dict):
        """
        Processes a SINGLE decoded FT8 message (m, from decode_window)
        through the QSO automation engine. Called ONLY when self._auto_seq_enabled.

        Step by step:
          1. parse_message() -> if None (unrecognized format), do nothing.
          2. engine.on_decode(parsed) -> an action dict or None.
          3. If the action is 'reply' with needs_measured_report=True,
             substitute the real measured SNR (m['snr_db']) as report_or_grid.
          4. Schedule the send (asyncio.create_task on _ft8_tx_sequence) —
             the same, proven path as manual TX (waits for the 15s window).
          5. If the action is 'enqueue' and we're IDLE and auto_call_1st is
             enabled -> immediately start_qso with this station (passing
             parsed as initial_decode, to correctly skip our own Tx1 when
             the partner already sent grid/report together with the reply).
          6. If qso_complete=True in the action -> schedule logging the QSO
             AFTER sending our final message (not before — the partner
             needs to get the confirmation), and check the queue for the
             next station.
        """
        try:
            # isDxpedition (type 0.1, see unpack_type0_1 in unpack.rs): a Fox
            # (or an MSHV station in "Multi Answering" mode, which uses
            # THIS SAME message format even in regular QSOs) combines RR73
            # for one Hound and a report for another in a single
            # transmission. call_to/call_de here are NOT the addressee/
            # sender in the usual sense - parse_message()'s ordinary text
            # parsing would lose that (or parse nonsense, e.g. "RR73" as a
            # callsign). A dedicated translator is used instead.
            if m.get("isDxpedition"):
                # Diagnostic (reported live 2026-08-24: "automat nie
                # reaguje na multistream, w logu nic nie ma" - RI1FJL,
                # MSHV Multi Answering). The generic per-decode print
                # below is disabled for perf (20-40 decodes/sec), but
                # type-0.1/multistream decodes are rare - printing every
                # ONE of these costs nothing and is the only way to see
                # what the Rust decoder actually extracted (call_to/
                # call_de/senderCall/report) BEFORE parse_dxpedition_message
                # runs, instead of guessing whether the miss is in
                # decoding, parsing, or callsign matching.
                print(f"[autoqso] DXpedition/multistream decode: "
                      f"call_to={m.get('call_to')!r} call_de={m.get('call_de')!r} "
                      f"senderCall={m.get('senderCall')!r} report={m.get('report_or_grid')!r} "
                      f"my_call={self._qso_engine.my_call!r}")
                parsed = qso_engine.parse_dxpedition_message(
                    m.get("call_to"), m.get("call_de"), m.get("senderCall"),
                    m.get("report_or_grid"), self._qso_engine.my_call)
                print(f"[autoqso] DXpedition/multistream parsed -> {parsed!r}")
            else:
                parsed = qso_engine.parse_message(m["message"])
                # Same diagnostic, standard-format side: any raw decode
                # whose sender is our CURRENT partner (bounded to one
                # callsign, not the full band - safe to always print).
                # Confirms whether RI1FJL-style stations sometimes answer
                # in plain (non-multistream) format too, and whether our
                # own parser recognizes it.
                _partner = (self._qso_engine.partner_call or "").upper()
                if _partner and str(m.get("call_de", "")).upper() == _partner:
                    print(f"[autoqso] standard decode from partner {_partner}: "
                          f"{m.get('message')!r} -> parsed={parsed!r}")
            # NOTE (perf): removed the per-decode print — it logged EVERY
            # FT8/FT4 decode (20-40/sec on a busy band), and each print is a
            # blocking syscall clogging the event loop.
            # Uncomment when debugging autoQSO:
            # print(f"[autoqso] DECODE: msg={m['message']!r} mode={m.get('mode')} parsed={parsed} engine_state={self._qso_engine.state} partner={self._qso_engine.partner_call}")
            if parsed is None:
                return
            # Ignore our own signal (echo from USB audio) — call_de is US
            if parsed.get('call_de', '').upper() == self._qso_engine.my_call.upper():
                return

            # The FT8 safety timer expired and the operator hasn't
            # confirmed presence yet (see "ft8_timer_expired"/
            # "ft8_timer_confirm" in _ws_msg) - COMPLETELY ignore the
            # decode for automation purposes (don't start, don't
            # retransmit, don't even reply to a partner mid-QSO). haltTx()
            # on the frontend has already stopped the current transmission
            # before sending this signal; this flag ensures NOTHING new
            # starts until the operator responds.
            if not self._ft8_operator_present:
                return

            result = self._qso_engine.on_decode(parsed, recv_epoch=m.get("recvEpoch"))
            if result is None:
                # UI visibility: on_decode() may have silently added the
                # station to the Call 1st queue (because we're in another
                # QSO, so it returned None instead of an 'enqueue' action)
                # — without this broadcast the operator wouldn't see that
                # anything happened until the current QSO ended.
                if parsed.get('call_to') == self._qso_engine.my_call:
                    await self.hub.broadcast({"type": "auto_qso_queue",
                                               "queue": list(self._qso_engine.queue),
                                               "active": self._qso_engine.partner_call})
                # Real WSJT-X/JTDX resend whatever message matches the
                # current QSO state on EVERY TX period (process_Auto /
                # genStdMsgs in mainwindow.cpp) - if the partner hasn't
                # replied, the SAME message goes out again automatically,
                # because the state didn't advance. Our engine is
                # event-driven (reacts only to a new incoming decode), so
                # it needs this explicit check instead: one lost
                # transmission (routine under QSB) used to mean total
                # silence until a 60s give-up. It now retransmits the last
                # sent message and only gives up after a bounded retry
                # count - JTDX has the same optional feature (default
                # off, 3-5 tries when enabled); ours is always on because
                # Call 1st runs unattended and must free up for the next
                # queued station.
                await self._check_retry_or_giveup()
                return

            if result.get("action") == "enqueue":
                call_de = result["call_de"]
                # Someone replied to our CQ - stop the periodic CQ calling
                # (we're moving into a QSO with this station).
                if self._cq_calling:
                    print(f"[cq] {call_de} replied to CQ - stopping CQ, starting QSO")
                    self._stop_cq_calling()
                # NOTE: auto-start when IDLE always applies, regardless of
                # Call 1st. Call 1st ONLY controls whether, after one QSO
                # ends, the automation moves on by itself to the NEXT
                # station in the queue (_advance_auto_qso_queue) - that's
                # an ORDERING decision for multiple simultaneous callers. A
                # direct call while we're completely idle isn't an
                # ordering decision at all (there's only one station), so
                # it shouldn't depend on that setting. Previously: Call 1st
                # disabled + a call while idle = total silence, reported
                # live as "the automation doesn't respond".
                if not self._qso_engine.is_active():
                    print(f"[autoqso] Auto-starting QSO with {call_de} (idle)")
                    start_result = self._qso_engine.start_qso(call_de, initial_decode=parsed)
                    if start_result and start_result.get("action") == "reply":
                        await self._dispatch_auto_reply(start_result, m,
                                                         tx_seq=self._reserve_tx_seq())
                await self.hub.broadcast({"type": "auto_qso_queue",
                                           "queue": list(self._qso_engine.queue),
                                           "active": self._qso_engine.partner_call})
                return

            if result.get("action") == "partner_busy":
                # FIX (reported live 2026-08-24, working an MSHV "Multi
                # Answering"/multistream DXpedition): this used to
                # abort_qso() INSTANTLY the moment the partner replied to
                # ANYONE else, on the assumption that a normal 1:1 station
                # can only run one exchange at a time, so that's proof
                # they moved on. True for a normal station - WRONG for an
                # MSHV multistream station, which legitimately interleaves
                # replies to several callers within the same pileup and
                # comes back to us a few periods later. Instantly
                # abandoning made Call 1st give up on real DXpeditions
                # after the very first sighting of them answering someone
                # else, forcing the operator to work the whole QSO by
                # hand. Now routed through the SAME bounded retry/give-up
                # timer as "no reply this period" (_check_retry_or_giveup)
                # instead of an instant abort - a normal station that
                # really has moved on still gets abandoned, just after up
                # to 4 retry periods instead of on the first sighting.
                print(f"[autoqso] {result['call_de']} transmitting to another "
                      f"station this period — waiting (retry/give-up timer applies)")
                await self._check_retry_or_giveup()
                return

            if result.get("action") == "reply":
                await self._dispatch_auto_reply(result, m, tx_seq=self._reserve_tx_seq())

                if result.get("qso_complete"):
                    print(f"[autoqso] QSO with {self._qso_engine.partner_call} completed (73)")
                    # Send the FULL QSO data to pre-fill the logging form
                    # BEFORE abort_qso() (which resets partner_call/grid/
                    # reports back to None) — the user must confirm it
                    # themselves ("+ LOG QSO" button), the automation does
                    # NOT write directly to the log.
                    # NOTE: the "R" prefix (e.g. "R-15") is an FT8 protocol
                    # marker ("acknowledging + here's my report"), NOT part
                    # of the actual signal report value — strip it before
                    # putting it in the log field, to keep the standard
                    # ADIF format.
                    rst_rcvd = self._qso_engine.partner_report_recv or ""
                    if rst_rcvd.startswith("R"):
                        rst_rcvd = rst_rcvd[1:]
                    rst_sent = self._qso_engine.partner_report_sent or ""
                    if rst_sent.startswith("R"):
                        rst_sent = rst_sent[1:]
                    # AUTO-SAVE the QSO to the operator's log (design
                    # decision: the automation saves it itself, the operator
                    # can edit it afterward in MY QSO LOG — instead of
                    # manual confirmation, which at a club station running
                    # unattended used to get skipped and QSOs were lost).
                    try:
                        from datetime import datetime as _dtx, timezone as _tzx
                        _now = _dtx.now(_tzx.utc)
                        _uid = getattr(self, "_autoqso_uid", None)
                        _freq_hz = int(getattr(self.rig, "freq", 0) or 0)
                        _band = self._get_band_for_freq(_freq_hz) or ""
                        # Grid: from the QSO if the partner sent it;
                        # otherwise from the decode cache (their earlier CQ with a grid).
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
                            print(f"[autoqso] QSO SAVED to log: "
                                  f"{self._qso_engine.partner_call} {_band} "
                                  f"{self._ft8_decode_mode}")
                            await self.hub.broadcast({"type": "qso_logged",
                                                       "qso": _saved})
                            # FIX: push to CloudLog. Manual "+ LOG QSO"
                            # (POST /api/qsolog, ~line 3864) has always done
                            # this, but this auto-save path (the automation
                            # writing the QSO itself on "73") never did —
                            # QSOs made via Call 1st / auto-answer never
                            # reached CloudLog, only ones added by hand.
                            asyncio.ensure_future(self._cloudlog_push_qso(_uid, _saved))
                        else:
                            print("[autoqso] WARNING: no operator uid - "
                                  "QSO NOT auto-saved", flush=True)
                    except Exception as _e:
                        print(f"[autoqso] QSO auto-save ERROR: {_e}", flush=True)
                    await self.hub.broadcast({"type": "auto_qso_complete",
                                               "dxCall": self._qso_engine.partner_call,
                                               "dxGrid": self._qso_engine.partner_grid or "",
                                               "rstSent": rst_sent,
                                               "rstRcvd": rst_rcvd,
                                               "mode": self._ft8_decode_mode})
                    await self.hub.broadcast({"type": "auto_qso_status",
                                               "state": "DONE",
                                               "partner": self._qso_engine.partner_call})
                    # Let the UI/logging react (e.g. auto-log to the
                    # journal) before we possibly pick up the next station from the queue.
                    self._qso_engine.abort_qso()  # back to IDLE, partner_call=None
                    self._qso_period_locked = False  # unlock the period for the next QSO
                    await self._advance_auto_qso_queue()
                else:
                    await self.hub.broadcast({"type": "auto_qso_status",
                                               "state": self._qso_engine.state,
                                               "partner": self._qso_engine.partner_call})
        except Exception as e:
            print(f"[autoqso] automation processing ERROR: {e}")

    async def _dispatch_auto_reply(self, result: dict, m: dict, tx_seq: int = None):
        """Substitutes the measured SNR (if required) and schedules
        sending the reply generated by the QSO engine.

        tx_seq: see _reserve_tx_seq - MUST be reserved BY THE CALLER,
        synchronously, before the first await (this method has its own
        await below — hub.broadcast — which is exactly the kind of point
        where another task could slip in if the number were reserved only here)."""
        if result.get("needs_measured_report"):
            result = dict(result)  # nie mutuj oryginalnego dict z silnika
            result["report_or_grid"] = self._format_report(m.get("snr_db", m.get("snr", 0)))
            self._qso_engine.record_sent_report(result["report_or_grid"])
        # Broadcast the currently frozen reports (single source of truth for
        # every caller of this method) so clients update their macro-3
        # preview and log fields to the value actually being transmitted.
        # Without this, clients only learned the frozen report at QSO
        # completion and displayed a stale/unrelated SNR during the QSO.
        rst_sent = self._qso_engine.partner_report_sent or ""
        if rst_sent.startswith("R"):
            rst_sent = rst_sent[1:]
        rst_rcvd = self._qso_engine.partner_report_recv or ""
        if rst_rcvd.startswith("R"):
            rst_rcvd = rst_rcvd[1:]
        await self.hub.broadcast({"type": "auto_qso_status",
                                   "state": self._qso_engine.state,
                                   "partner": self._qso_engine.partner_call,
                                   "rstSent": rst_sent, "rstRcvd": rst_rcvd})
        await self._send_auto_tx(result, partner_decode=m, tx_seq=tx_seq)

    def _period_from_epoch(self, recv_epoch, window_s: float):
        """Computes OUR TX period (1 or 2) from the exact (fractional)
        RECEIVE timestamp of the decode (recvEpoch, set ONCE in
        _ft8_rx_loop the moment the result comes in from Rust).

        IMPORTANT - this is NOT the partner's period flipped. Rust only
        sends the decode result AFTER the entire audio window +
        computation finishes (see rx_loop.rs: settle=0.3s + decode time),
        so by the time we receive it we're ALREADY in the next 15s/7.5s
        window relative to the one the partner actually transmitted in —
        and that next window is by definition OUR reply window. So there's
        no need to compute the "partner's period" and flip it (that was
        the bug in the previous version: it used the timeStr string with
        whole-second resolution, set AFTER decoding, so it regularly
        pointed to the window ALREADY AFTER the partner — computing the
        "partner's period" from that and taking the opposite landed back
        on the partner's window, one too many).

        recvEpoch is set ONCE on receive, so the result is stable
        regardless of when this code later runs (instant automation vs a
        human's manual click several-odd seconds later) — exactly like
        the Tx period in WSJT-X/JTDX.

        Returns None if recv_epoch is missing/invalid."""
        if recv_epoch is None:
            return None
        try:
            window_idx = int(float(recv_epoch) // window_s)
        except (TypeError, ValueError):
            return None
        return 1 if (window_idx % 2 == 0) else 2

    def _reserve_tx_seq(self) -> int:
        """Reserves a sequence number SYNCHRONOUSLY, at the exact moment
        the "this is going on air" decision is made — NOT later, inside
        _send_auto_tx (as before). Reason: _send_auto_tx and
        _dispatch_auto_reply have their OWN await points (hub.broadcast,
        PCM generation via run_in_executor) — each such await lets
        ANOTHER, EARLIER-scheduled task (a retransmit via
        asyncio.create_task, which isn't awaited here) run in the
        meantime. If the number were only assigned inside _send_auto_tx, a
        retransmit could get a NEWER number than an actually-later
        decision (e.g. a reply to a partner report just received) —
        because ordering was then decided by whatever order asyncio
        happened to give them CPU time, not the actual decision order in
        _process_auto_qso. Observed live: a stale "JO72" (Tx1) retransmit
        went out in the same window as an already-received partner
        report, instead of being blocked by the stale-TX guard.
        Reserving HERE, before any await, guarantees the numbers reflect
        the TRUE decision order."""
        self._autoqso_tx_seq += 1
        return self._autoqso_tx_seq

    async def _send_auto_tx(self, action: dict, partner_decode: dict = None, tx_seq: int = None):
        """Sends a message generated by the QSO automation, using EXACTLY
        the same path as manual TX (_ft8_tx_sequence) — meaning it
        respects the 15s window sync, PTT, etc.

        Automatically detects the partner's window and sets the OPPOSITE
        TX period, to avoid colliding (if the partner transmitted in
        period=1, we transmit in period=2).

        tx_seq: the number reserved BY THE CALLER (_reserve_tx_seq(),
        called SYNCHRONOUSLY, before any await) - if missing (None), we
        reserve one here as a safety net, but that no longer guarantees
        correct ordering relative to concurrently scheduled sends (see
        _reserve_tx_seq).
        """
        if not action:
            return
        if tx_seq is None:
            tx_seq = self._reserve_tx_seq()

        # Determine OUR TX period from the exact RECEIVE timestamp of the
        # decode (recvEpoch) — see _period_from_epoch for why this ALREADY
        # IS our window (no need to flip the "partner's period"). The
        # timestamp is set ONCE in _ft8_rx_loop, so the result is stable
        # regardless of the operator's reaction time (instant automation
        # vs a human's manual click several-odd seconds later) — exactly
        # like the Tx period in WSJT-X/JTDX.
        if partner_decode and not getattr(self, '_qso_period_locked', False):
            window_s = ft4_encoder.FT4_SLOT_TIME if self._ft8_decode_mode == "FT4" else 15.0
            my_period = self._period_from_epoch(partner_decode.get('recvEpoch'), window_s)
            if my_period is not None:
                if self._ft8_tx_period != my_period:
                    self._ft8_tx_period = my_period
                    print(f"[autoqso] Auto-period (recvEpoch={partner_decode.get('recvEpoch')}): "
                          f"my_period={my_period}")
                    await self.hub.broadcast({"type": "ft8_tx_period", "period": my_period})
                self._qso_period_locked = True  # hold the period for the whole QSO

        print(f"[autoqso] Scheduling TX: {action['call_to']} {action['call_de']} "
              f"{action['report_or_grid']} (r_flag={action.get('r_flag', False)})")

        # Dedup — don't send the same message twice in a row
        tx_key = (action['call_to'], action.get('report_or_grid'))
        if tx_key == self._last_auto_tx_key and self._ft8_tx_lock.locked():
            print(f"[autoqso] Duplicate TX {tx_key} — ignoring")
            return
        self._last_auto_tx_key = tx_key

        # Remember EXACTLY what we sent and when - _process_auto_qso uses
        # this for a possible retransmit (should_retransmit), if the
        # partner doesn't reply in time.
        self._last_auto_tx_action = dict(action)
        self._qso_engine.record_tx_sent()

        # Sequence number for THIS specific action (reserved by the
        # caller, see _reserve_tx_seq) — used by the stale-TX guard in
        # _ft8_tx_sequence_inner right before PTT: if a NEWER action has
        # been scheduled between scheduling this send and its actual PTT,
        # this old one is by definition stale — regardless of what state
        # the QSO engine happens to be in (the first version of this
        # guard, based on partner_call+is_active(), incorrectly blocked
        # EXACTLY the action that legitimately ends the QSO, since the
        # final "73" itself flips the engine to DONE state before it even
        # gets to go on air).
        my_tx_seq = tx_seq

        # Generate PCM BEFORE starting the TX task — eliminates ~500ms of DT delay
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
            # Keep ldpc_valid in the cache - otherwise, when the cache is
            # reused, debug={} and the code falsely warns ldpc_valid=None
            # (even though the PCM is valid).
            self._pre_pcm_cache = (ct, cd, rg, pcm, dur, _dbg.get("ldpc_valid"))
            print(f"[ft8] PCM ready ({dur:.2f}s, gen={(_pt.time()-_t)*1000:.0f}ms, "
                  f"ldpc_valid={_dbg.get('ldpc_valid')})")
        except Exception as e:
            print(f"[ft8] pre_gen error: {e}")

        asyncio.create_task(self._ft8_tx_sequence(ct, cd, rg, rf, auto_respond=True, tx_seq=my_tx_seq))

    async def _ft8_rx_loop(self):
        """
        Background loop: receives FT8/FT4 decode results from Rust
        (ham_audio.exe) via ft8_rust_receiver and broadcasts them to WS
        clients. Rust itself manages window timing (pop at boundary, decode, send JSON).
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

                if msg.get("type") == "startup_stats":
                    # Diagnostics: how many threads rayon (the parallel
                    # candidate-decode pool) actually uses, and how many
                    # logical cores the system sees, sent ONCE on every
                    # Rust->Python connection. Weak/unexpected parallelism
                    # (e.g. a machine with performance/efficiency cores
                    # where rayon by default sees fewer threads than there
                    # are physical cores) is one of the still-unconfirmed
                    # hypotheses for why decode_elapsed_s/pass_elapsed_s
                    # doesn't respond to further fixes - this line settles
                    # it directly, without guessing.
                    if VERBOSE:
                        print(f"[ft8dec] startup: rayon_threads={msg.get('rayon_threads', 0)} "
                              f"cpus={msg.get('cpus', 0)}", flush=True)
                    continue

                if msg.get("type") == "pass_stats":
                    # Diagnostics: time (measured in Rust, from the start
                    # of decoding THIS window) until THIS batch of results
                    # (one pass, BEFORE signal subtraction) was ready to
                    # send. This is exactly what the "stream pass-0
                    # results" change was meant to speed up from ~1.1s to
                    # ~150-200ms. This line appears RIGHT BEFORE the
                    # corresponding "[ft8rx] decode -> UI: ..." lines for
                    # the same batch. spec_ms/find_cand_ms/par_decode_ms/
                    # n_cand: the full phase breakdown of this pass — added
                    # after four consecutive fixes (streaming, FFT cache,
                    # LDPC cache, tokio keep-alive) did NOT move
                    # pass_elapsed_s on real hardware at all, so guessing
                    # the next cause stopped making sense — this breakdown
                    # shows BY NAME which phase actually eats the time.
                    if VERBOSE:
                        print(f"[ft8dec] pass_elapsed_s={msg.get('pass_elapsed_s', 0):.3f} "
                              f"n={msg.get('n', 0)} spec_ms={msg.get('spec_ms', 0):.1f} "
                              f"find_cand_ms={msg.get('find_cand_ms', 0):.1f} "
                              f"par_decode_ms={msg.get('par_decode_ms', 0):.1f} "
                              f"n_cand={msg.get('n_cand', 0)} "
                              f"demod_ms_sum={msg.get('demod_ms_sum', 0):.1f} "
                              f"ldpc_ms_sum={msg.get('ldpc_ms_sum', 0):.1f}", flush=True)
                    continue

                if msg.get("type") == "decode_stats":
                    # Diagnostics: TOTAL decode time in Rust (decode_ft8/
                    # decode_ft4, with ALL signal-subtraction passes) for
                    # THIS window. Since 2026-08-14 Rust streams the
                    # results of EVERY pass as soon as they're
                    # decoded+deduped, BEFORE signal subtraction (see
                    # decode_and_subtract in mod.rs) — so this line (and
                    # its decode_elapsed_s) no longer directly corresponds
                    # to the pos_in_win of the first decodes from this
                    # window (those arrive much earlier, usually right
                    # after pass 0 alone). It's still an upper bound on the
                    # total cost (all passes combined).
                    if VERBOSE:
                        print(f"[ft8dec] decode_elapsed_s={msg.get('decode_elapsed_s', 0):.3f} "
                              f"n_results={msg.get('n_results', 0)}", flush=True)
                    # Refresh AP (a priori) decode hints once per COMPLETED
                    # window (not per-mutation-site - the QSO engine has
                    # ~15 call sites that touch partner_call/queue, scattering
                    # a sync call across all of them is fragile and easy to
                    # miss one; hints only need to be fresh by the START of
                    # the NEXT decode cycle anyway, and this fires exactly
                    # once per cycle regardless of which handler last
                    # changed the state). See ap.rs/build_hypotheses in Rust.
                    await self._sync_ap_hints()
                    continue

                if msg.get("type") != "wsjtx_decode":
                    continue

                # Exact (fractional) RECEIVE timestamp of this decode from
                # Rust — used BELOW instead of timeStr (an HHMMSS string,
                # whole-second resolution, set by Rust AFTER decoding
                # finishes, so it almost always already points to the NEXT
                # window relative to the one the partner actually
                # transmitted in) to compute the TX period (see
                # _period_from_epoch). Set ONCE, here, so it's independent
                # of how long the operator later takes to click the station.
                msg["recvEpoch"] = time.time()

                # Running total for internal use. The old per-message log was
                # throttled (first 10, then every 20th), which looked like the
                # decoder "only found 1" when it had actually found many — the
                # messages were decoded and broadcast, just not printed. We now
                # log every decode compactly so the count in the log matches
                # what the operator sees in the UI.
                self._ft8_dbg_count = getattr(self, "_ft8_dbg_count", 0) + 1
                # pos_in_win: where in the current 15s/7.5s window the
                # MOMENT this decode was RECEIVED from Rust falls.
                # Diagnostics for the suspicion that processing (decoding +
                # up to 3 signal-subtraction passes) on a crowded band can
                # take long enough that recvEpoch already falls in the NEXT
                # window relative to the one the partner actually
                # transmitted in — which would shift the computed TX period
                # by a whole extra round (see _period_from_epoch). Small
                # pos_in_win (~0-3s) = decode arrived fresh, as expected.
                # Large pos_in_win (~10s+) = suspiciously late, worth investigating.
                _win_s = ft4_encoder.FT4_SLOT_TIME if self._ft8_decode_mode == "FT4" else 15.0
                _pos_in_win = msg["recvEpoch"] % _win_s
                if VERBOSE:
                    print(f"[ft8rx] decode -> UI: {msg.get('message','?')} "
                          f"(#{self._ft8_dbg_count}, clients={len(self.online_users)}, "
                          f"pos_in_win={_pos_in_win:.2f}s)",
                          flush=True)

                # Broadcast to browsers
                await self.hub.broadcast(msg)

                # Cache call->grid from ALL decodes. A partner replying to
                # OUR CQ doesn't send a grid (e.g. "XX0XXX JA3FYC +11"), so
                # partner_grid is sometimes empty on QSO auto-save ("saves
                # the grid sometimes, not others"). But this station
                # usually called CQ WITH a grid a moment earlier — a cache
                # lookup fills in the locator in the log.
                try:
                    _cd = (msg.get("call_de") or "").strip().upper()
                    _rg = (msg.get("report_or_grid") or "").strip().upper()
                    if (_cd and len(_rg) == 4 and _rg[0].isalpha()
                            and _rg[1].isalpha() and _rg[2].isdigit()
                            and _rg[3].isdigit() and _rg != "RR73"):
                        # RR73 formally matches the locator pattern (letters
                        # A-R + digits) — the protocol DELIBERATELY chose an
                        # Antarctica grid to signal QSO end. Without this
                        # exclusion it ended up in the cache and got logged
                        # as the partner's "grid locator".
                        _gc = getattr(self, "_call_grid_cache", None)
                        if _gc is None:
                            _gc = self._call_grid_cache = {}
                        if len(_gc) > 1000:
                            _gc.clear()
                        _gc[_cd] = _rg
                except Exception:
                    pass

                # Cache call -> last RECEIVE epoch heard transmitting.
                # Needed to auto-pick the correct TX period (1/2, see
                # _period_from_epoch) when manually starting a QSO with a
                # NAMED station we've only seen in the decode window, not
                # yet exchanged with directly (see the "Manual TX grid"
                # handler above) - without this the operator had to figure
                # out and set the period by hand every time, or risk
                # transmitting in the SAME period as the station they're
                # calling (real on-air collision, reported live 2026-08-24:
                # "powinienem ja wolac w periodzie 1 a wolam 2, bo inaczej
                # sie na nia nakladam"). The odd/even period pattern is
                # absolute (tied to UTC time, not relative to when we heard
                # them), so even a STALE cached epoch from minutes ago still
                # correctly says which period that station uses, as long as
                # they haven't switched mid-session (atypical).
                try:
                    _sender_call = (msg.get("call_de") or "").strip().upper()
                    if _sender_call and not _sender_call.startswith("<"):
                        _lh = getattr(self, "_call_last_heard", None)
                        if _lh is None:
                            _lh = self._call_last_heard = {}
                        if len(_lh) > 1000:
                            _lh.clear()
                        _lh[_sender_call] = msg["recvEpoch"]
                except Exception:
                    pass

                # Feed the known-callsign pool for validating/correcting
                # garbled callsigns in CW decodes. Sources: FT8 decodes, QSO log, DX cluster spots.
                try:
                    _calls = [msg.get("call_de"), msg.get("call_to")]
                    if deepcw_engine is not None:
                        deepcw_engine.add_known_calls(
                        [c for c in _calls if c and not str(c).startswith("<")]
                    )
                except Exception:
                    pass

                # Auto-follow RX/TX to the station transmitting to us
                # (respects Hold TX). Called ALWAYS - the method itself
                # checks whether TX is frozen and decides which markers to move.
                await self._process_tx_freeze_rx_follow(msg)
                if self._auto_seq_enabled:
                    await self._process_auto_qso(msg)

            except Exception as e:
                print(f"[ft8rx] ERROR: {e}", flush=True)
                await asyncio.sleep(1.0)


    async def _waterfall_loop(self):
        """
        Background loop: every WF_INTERVAL_S streams a compact spectrum
        column (scope/waterfall) to all WS clients. Independent of the FT8
        decode cycle (which runs every 15s) — gives a smoothly scrolling
        waterfall regardless of decode cadence. Runs ONLY when FT8 RX is
        enabled (the same switch as decoding), to avoid wasting CPU when
        no one is looking at the tab.
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
                print(f"[waterfall] ERROR: {e}")

    async def _handle_rig_action(self, msg: dict, ws, role: str):
        """
        {type:'rig_action', id:'vfo_a'|'vfo_b'|'func_nb'|..., value?: bool}
        VFO select: id='vfo_a'/'vfo_b' -> RigCAT.set_vfo('VFOA'/'VFOB')
        Func toggle: id='func_XXX', value: bool -> RigCAT.set_func('XXX', value)
        """
        action_id = msg.get("id", "")
        if not action_id:
            return
        # Radio lock: only the TRX holder (or admin) may control it
        can, why = self._can_control_radio(ws, role)
        if not can:
            await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
            return
        if not await self._dynamic_allowed(action_id, role, "action"):
            print(f"[features] rig_action '{action_id}' denied for role {role}")
            return

        if action_id.startswith("vfo_"):
            vfo_name = "VFOA" if action_id == "vfo_a" else "VFOB" if action_id == "vfo_b" else None
            if vfo_name:
                self.rig.vfo = vfo_name  # state also in sim
                if not self.rig.sim and hasattr(self.rig, "set_vfo"):
                    try:
                        await self.rig.set_vfo(vfo_name)
                    except Exception as e:
                        print(f"[rig] set_vfo error: {e}")
                # Broadcast the vfo STATE to all clients — A/B buttons are
                # highlighted based on the radio's state, not the local
                # click (a new user after login sees which VFO is active).
                await self.hub.broadcast({"type": "vfo", "vfo": vfo_name})
                await self.hub.broadcast({"type": "rig_action_ack", "id": action_id, "ok": True})

        elif action_id == "power_toggle":
            value = bool(msg.get("value", True))
            print(f"[rig] power_toggle value={value} sim={self.rig.sim} has_set_power={hasattr(self.rig, 'set_power')}", flush=True)
            if not self.rig.sim and hasattr(self.rig, "set_power"):
                try:
                    await self.rig.set_power(value)
                    self._rig_power_on = value
                    # Broadcast under the name 'power_state' (the frontend
                    # listens for this!). It used to be 'rig_power' - a
                    # different name - the message got lost and other
                    # users' buttons didn't update.
                    await self.hub.broadcast({"type": "power_state", "value": value})
                    if not value:
                        # Radio turned off — reset the waterfall for everyone
                        await self.hub.broadcast({"type": "scope_reset"})
                    await self.hub.broadcast({"type": "rig_action_ack", "id": action_id,
                                               "value": value, "ok": True})
                    if value:
                        # The radio needs time after waking up before CI-V
                        # starts responding. We wait and VERIFY it's
                        # actually alive (read freq) - without this the
                        # waterfall started blindly and loaded in SIM state
                        # while the radio was still asleep.
                        await self._verify_radio_awake_and_start_scope()
                except Exception as e:
                    print(f"[rig] set_power error: {e}", flush=True)
                    # Broadcast the real state (unchanged) so users'
                    # buttons return to the correct position
                    await self.hub.broadcast({"type": "power_state",
                                               "value": self._rig_power_on})
                except Exception as e:
                    print(f"[rig] set_power error: {e}", flush=True)

        elif action_id.startswith("func_"):
            func_name = action_id[len("func_"):].upper()
            value = bool(msg.get("value", True))
            if not self.rig.sim and hasattr(self.rig, "set_func"):
                try:
                    await self.rig.set_func(func_name, value)
                    await self.hub.broadcast({"type": "rig_action_ack", "id": action_id,
                                               "value": value, "ok": True})
                except Exception as e:
                    print(f"[rig] set_func error: {e}")

    async def _handle_rig_slider(self, msg: dict, ws, role: str):
        """
        {type:'rig_slider', id:'level_rfpower', value: float}
        -> RigCAT.set_level('RFPOWER', value)
        """
        slider_id = msg.get("id", "")
        if not slider_id.startswith("level_"):
            return
        # Radio lock: only the TRX holder (or admin) may control it
        can, why = self._can_control_radio(ws, role)
        if not can:
            await ws.send_json({"type": "toast", "msg": f"⛔ {why}", "level": "error"})
            return
        if not await self._dynamic_allowed(slider_id, role, "slider"):
            print(f"[features] rig_slider '{slider_id}' denied for role {role}")
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
                print(f"[rig] set_level({param}) error: {e}")
                # FIX: this used to broadcast rig_slider_ack unconditionally
                # even when set_level failed (e.g. a CI-V transaction
                # collision under load — same contention family as the
                # "[civ] S-metr transact fail" case). Every OTHER client
                # (www, other phones) would then show the slider at the
                # new position even though the radio never actually moved
                # — looked like "the phone and www don't stay in sync"
                # when the real cause was a lost write, not a sync bug.
                # Tell only the sender it didn't take, and don't lie to
                # everyone else about the new value.
                await ws.send_json({"type": "toast",
                                     "msg": f"⚠ Nie udało się ustawić {param}", "level": "error"})
                return

        # Diagnostic (reported live 2026-08-23/24 as "phone slider changes
        # the radio audibly, but www never updates") — print exactly who
        # is about to receive the ack, so the next server log paste tells
        # us whether www's connection is even in the recipient set
        # (auth/subscription problem) or receives it but doesn't redraw
        # (a frontend problem) instead of guessing further.
        try:
            recipients = [
                self.online_users.get(c, {}).get("username", "?")
                for c in self.hub._clients
                if c is not ws and "control" in self.hub._subs.get(c, set())
            ]
            print(f"[rig] slider {slider_id}={value} ok -> rig_slider_ack to {recipients}")
        except Exception:
            pass

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
                print(f"[auth] jwt_verify FAIL for token: {token_str[:20]}...", flush=True)
        if not user:
            qt = query.get("token", "")
            if qt: user = self._check_pw_ver(jwt_verify(qt))
        if not user and path.startswith("/api/") and path not in (
            "/api/auth/login", "/api/auth/reset", "/api/auth/reset-request",
            # Common endpoints scanned by bots (don't log the spam):
            "/api/login", "/api/register", "/api/admin", "/api/v1/login",
            "/api/user", "/api/users", "/api/config",
            # Polled periodically by the UI — after session expiry these
            # fire every second and flood the log
            "/api/status/perf",
        ):
            # Repeat suppression: log the same endpoint + IP once a minute.
            # The browser polls periodically, so after a token expires
            # without this the log grows by hundreds of identical lines.
            _k = f"{path}|{request.remote}"
            _now = time.time()
            _seen = getattr(self, "_auth_warn_seen", None)
            if _seen is None:
                _seen = self._auth_warn_seen = {}
            if _now - _seen.get(_k, 0) > 60:
                _seen[_k] = _now
                if len(_seen) > 200:      # don't let this grow unbounded
                    _seen.clear()
                print(f"[auth] NO user for {path} | ip={request.remote} "
                      f"| auth={auth[:30]!r}", flush=True)

        # Public pages — no auth required (before the API block)
        if path == "/perf":
            # Performance diagnostics page. The page itself is public (it's
            # just HTML+JS), but it fetches data from /api/status/perf which requires admin.
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
            # Download the HAM-RADIO-CTRL.exe client (COM Bridge for CW Skimmer/HRD).
            #
            # NOTE: no login required! Clicking an <a href> in the browser
            # does NOT send an Authorization header (JWT), so requiring
            # auth blocked the download (401). The EXE contains no
            # secrets - it's a public file to download, so auth is unnecessary.
            import pathlib as _pl
            import os as _os

            # 'downloads' directory searched in order:
            # 1. HAM_DOWNLOADS_DIR env var (override for other PCs)
            # 2. downloads/ next to webapp.py (standard C:\...\ham\downloads)
            # 3. downloads/ relative to the working directory (fallback)
            _dirs = []
            _env = _os.environ.get("HAM_DOWNLOADS_DIR")
            if _env:
                _dirs.append(_pl.Path(_env))
            _script_dir = _pl.Path(__file__).resolve().parent
            # In the packaged EXE the COM bridge lives in BUNDLE (_MEIPASS), not next to __file__
            try:
                from config import BUNDLE as _BUNDLE, DATA as _DATA_DIR
                _dirs.append(_BUNDLE)                       # packaged in the EXE
                _dirs.append(_BUNDLE / "bridge")           # subfolder in the bundle
                _dirs.append(_DATA_DIR)                    # next to the EXE / APPDATA
                _dirs.append(_DATA_DIR / "downloads")
            except Exception:
                pass
            _dirs.append(_script_dir / "downloads")
            _dirs.append(_pl.Path("downloads"))          # relative fallback
            _dirs.append(_script_dir / "bridge_client" / "dist")  # fresh build

            # NAME COLLISION: the product's own server is ALSO called
            # HAM-RADIO-CTRL.exe. The COM bridge is packaged as
            # HAM-RADIO-CTRL-bridge.exe (a different name), so this
            # endpoint doesn't accidentally serve the server itself. Look
            # for the -bridge version first, then (for dev) the original
            # name in places where there's NO server (downloads, bridge_client/dist).
            _bridge_names = ["HAM-RADIO-CTRL-bridge.exe"]
            # In dev mode (not packaged) the bridge may have the original
            # name in downloads/ or bridge_client/dist - no server-EXE there.
            _dev_dirs = [_script_dir / "downloads", _pl.Path("downloads"),
                         _script_dir / "bridge_client" / "dist"]

            _checked = []
            # 1. Look for HAM-RADIO-CTRL-bridge.exe everywhere (product)
            for _d in _dirs:
                for _name in _bridge_names:
                    _f = _d / _name
                    _checked.append(str(_f))
                    if _f.exists():
                        print(f"[download] serving COM bridge: {_f} ({_f.stat().st_size:,} B)", flush=True)
                        return web.Response(
                            body=_f.read_bytes(),
                            content_type="application/octet-stream",
                            headers={
                                "Content-Disposition": "attachment; filename=HAM-RADIO-CTRL.exe",
                                "Access-Control-Allow-Origin": "*",
                            }
                        )
            # 2. Dev fallback: original name, but ONLY in directories without a server
            for _d in _dev_dirs:
                _f = _d / "HAM-RADIO-CTRL.exe"
                _checked.append(str(_f))
                if _f.exists():
                    print(f"[download] serving COM bridge (dev): {_f} ({_f.stat().st_size:,} B)", flush=True)
                    return web.Response(
                        body=_f.read_bytes(),
                        content_type="application/octet-stream",
                        headers={
                            "Content-Disposition": "attachment; filename=HAM-RADIO-CTRL.exe",
                            "Access-Control-Allow-Origin": "*",
                        }
                    )
            print(f"[download] COM bridge not found. Checked:", flush=True)
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
            # Client IP for rate limiting. Behind a reverse proxy / tunnel
            # the real address is in X-Forwarded-For (first entry = original client).
            _xff = request.headers.get("X-Forwarded-For", "")
            _client_ip = (_xff.split(",")[0].strip() if _xff
                          else (request.remote or ""))
            status, result = await self.api(method, path, body, user, q, _client_ip)
            # Special type — binary response (e.g. an ONNX model)
            if status == "binary":
                return web.Response(
                    body=result,
                    content_type="application/octet-stream",
                    headers={
                        "Access-Control-Allow-Origin": "*",
                        "Cache-Control": "public, max-age=86400",
                    },
                )
            # Special type — file export (ADIF/CSV/EDI) with a download header
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
        _set_ui_cookie = None
        if path in ("/", ""):
            # Same URL for phone and desktop: detect on the server (UA
            # sniff) instead of making people know about a separate
            # /mobile.html link. ?ui=mobile / ?ui=desktop overrides the
            # detection and is remembered in a cookie (for tablets/foldables
            # the UA heuristic gets wrong, or anyone who just prefers the
            # other layout) - re-picking is one query param away, no
            # settings page needed.
            ui_override = query.get("ui")
            if ui_override in ("mobile", "desktop"):
                _set_ui_cookie = ui_override
                ui_pref = ui_override
            else:
                ui_pref = request.cookies.get("ui_pref", "")
            if ui_pref == "mobile":
                is_mobile = True
            elif ui_pref == "desktop":
                is_mobile = False
            else:
                is_mobile = bool(_MOBILE_UA_RE.search(request.headers.get("User-Agent", "")))
            path = "/mobile.html" if is_mobile else "/index.html"
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

            # Check the cache — if the file hasn't changed on disk, use the in-RAM copy
            entry = _STATIC_CACHE.get(key)
            if entry:
                cached_mtime, raw, gz, cached_mime, etag = entry
                try:
                    cur_mtime = fpath.stat().st_mtime
                    if cur_mtime != cached_mtime:
                        # File changed — invalidate and reload
                        entry = None
                except OSError:
                    entry = None
            if not entry:
                entry = _cache_static_file(fpath, mime)
                _STATIC_CACHE[key] = entry
                _, raw, gz, _, etag = entry

            # HTML (index.html/login.html) MUST NOT get a long max-age —
            # this document is EXACTLY what decides which version of JS/CSS
            # loads (via ?v=... in <script src>). With max-age=3600 for
            # .html, the browser could serve the OLD index.html (pointing
            # at OLD ?v= of the JS files) from its own cache for a whole
            # hour, EVEN after a "hard" refresh if the HTTP cache didn't
            # happen to get reset — a live case: the user had fixed code on
            # the server/in the EXE (verified directly from the archive),
            # yet still saw the old behavior in the browser. Short-lived
            # assets (.js/.css) with ?v= are safely cached long-term (a new
            # version = a new URL), but the .html itself must always be revalidated.
            _cc = "no-cache" if ext in (".html", ".htm") else "public, max-age=3600, must-revalidate"

            # If-None-Match — HTTP 304 (client cache hit)
            client_etag = request.headers.get("If-None-Match", "")
            if client_etag == etag:
                _headers304 = {"ETag": etag, "Cache-Control": _cc}
                if _set_ui_cookie:
                    _headers304["Set-Cookie"] = f"ui_pref={_set_ui_cookie}; Path=/; Max-Age=31536000; SameSite=Lax"
                return web.Response(status=304, headers=_headers304)

            # Choose the body: gzip if the client accepts it and we have a pre-compressed one
            accept_enc = request.headers.get("Accept-Encoding", "")
            body = raw
            headers = {
                "ETag": etag,
                "Cache-Control": _cc,
                "Vary": "Accept-Encoding",
            }
            if gz is not None and "gzip" in accept_enc.lower():
                body = gz
                headers["Content-Encoding"] = "gzip"
            if _set_ui_cookie:
                headers["Set-Cookie"] = f"ui_pref={_set_ui_cookie}; Path=/; Max-Age=31536000; SameSite=Lax"

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
