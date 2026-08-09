"""
dxcluster.py — Klient DX Cluster (Telnet).

Nawiazuje polaczenie TCP z serwerem DX Cluster, wysyla login i haslo (jesli
wymagane), a nastepnie parsuje przychodzace linie w formacie DX de:
  DX de <spotter>:  <freq>  <call>       <comment>  <utc>

Kazdy zalogowany uzytkownik moze miec wlasne polaczenie z wlasnymi
danymi (adres, port, login, haslo) — zarzadzane per-user w ClusterManager.

Spoty sa broadcastowane przez callback (WS do konkretnego usera) w formacie:
  {"type": "dx_spot", "freq_hz": int, "call": str, "spotter": str,
   "comment": str, "utc": str, "ts": epoch_seconds, "band": str, "mode": str}
"""

import asyncio
import re
import time
from typing import Callable, Optional


# Prefixy pasm z tabeli IARU (do przyporzadkowania spota na waterfall)
_BAND_RANGES = [
    ('160m', 1800000,   2000000),
    ('80m',  3500000,   3800000),
    ('60m',  5300000,   5410000),
    ('40m',  7000000,   7200000),
    ('30m',  10100000,  10150000),
    ('20m',  14000000,  14350000),
    ('17m',  18068000,  18168000),
    ('15m',  21000000,  21450000),
    ('12m',  24890000,  24990000),
    ('10m',  28000000,  29700000),
    ('6m',   50000000,  54000000),
    ('4m',   70000000,  70500000),
    ('2m',   144000000, 146000000),
    ('70cm', 430000000, 440000000),
]


def _get_band(freq_hz: int) -> str:
    for name, lo, hi in _BAND_RANGES:
        if lo <= freq_hz <= hi:
            return name
    return '?'


def _guess_mode(freq_hz: int, comment: str) -> str:
    """
    Heurystyka trybu spota: komentarz -> dokladne czestotliwosci cyfrowe ->
    bandplan IARU R1.

    Kolejnosc ma znaczenie:
      1. Komentarz (jako OSOBNE SLOWA \\b...\\b, zeby 'OK1CW' nie dalo CW)
      2. DOKLADNE czestotliwosci FT8/FT4/MSK144 z tolerancja +-2 kHz.
         To musi byc PRZED bandplanem, bo np. 2m FT8 (144.174) lezy w
         segmencie fonii 144.150-144.400 i bandplan zwrocilby bledne SSB.
      3. Bandplan (segmenty CW/DIGI/SSB/FM) - HF, VHF, UHF, mikrofale.
    """
    c = (comment or '').upper()

    # ── 1. Tryb wprost w komentarzu ─────────────────────────────────────────
    for pat, mode in (
        (r'\bFT8\b',                'FT8'),
        (r'\bFT4\b',                'FT4'),
        (r'\bMSK144\b',             'MSK144'),
        (r'\bJS8\b',                'DIGI'),
        (r'\bQ65\b',                'DIGI'),
        (r'\bJT(?:65|9|6M)\b',      'DIGI'),
        (r'\bRTTY\b',               'RTTY'),
        (r'\bPSK(?:31|63)?\b',      'PSK'),
        (r'\bWSPR\b',               'DIGI'),
        (r'\bSSB\b',                'SSB'),
        (r'\b(?:LSB|USB)\b',        'SSB'),
        (r'\bCW\b',                 'CW'),
        (r'\bFM\b',                 'FM'),
        (r'\bAM\b',                 'AM'),
    ):
        if re.search(pat, c):
            return mode

    khz = freq_hz / 1000.0

    # ── 2. Dokladne czestotliwosci cyfrowe (tolerancja +-2 kHz) ─────────────
    # Sygnal FT8 ma ~50 Hz nosnych w pasmie 3 kHz, spotty roznia sie o kilkaset
    # Hz, stad tolerancja. MUSI byc przed bandplanem (patrz docstring).
    FT8_KHZ = [
        1840, 3573, 5357, 7074, 10136, 14074, 18100, 21074, 24915, 28074,
        50313, 50323,            # 6m (dwie czestotliwosci)
        70100, 70154,            # 4m
        144174,                  # 2m
        222065,                  # 1.25m
        432174,                  # 70cm
        1296174,                 # 23cm
        2320174,                 # 13cm
        3400174, 5760174, 10368174, 24048174,   # 9cm/6cm/3cm/1.2cm
    ]
    FT4_KHZ = [
        3575, 7047.5, 10140, 14080, 18104, 21140, 24919, 28180,
        50318,                   # 6m
        144170,                  # 2m
    ]
    MSK144_KHZ = [50260, 70230, 144360, 432360]   # meteor scatter

    def _near(target_list, tol=2.0):
        return any(abs(khz - t) <= tol for t in target_list)

    if _near(FT8_KHZ):    return 'FT8'
    if _near(FT4_KHZ):    return 'FT4'
    if _near(MSK144_KHZ): return 'MSK144'

    # WSPR (waskie, tolerancja 1 kHz)
    WSPR_KHZ = [1836.6, 3568.6, 7038.6, 10138.7, 14095.6, 18104.6,
                21094.6, 24924.6, 28124.6, 50293, 144489]
    if _near(WSPR_KHZ, tol=1.0): return 'DIGI'

    # ── 3. Satelity — PRZED bandplanem, bo segmenty FM by je przykryly ──────
    # 2m: 145.800-146.000, 70cm: 435.000-438.000 (segmenty satelitarne IARU)
    if 145800 <= khz <= 146000 or 435000 <= khz <= 438000:
        return 'SAT'

    # ── 4. Bandplan IARU Region 1 (kHz) ─────────────────────────────────────
    BANDPLAN = [
        # ── HF ──
        (1810, 1838, 'CW'), (1838, 1843, 'DIGI'), (1843, 2000, 'SSB'),
        (3500, 3570, 'CW'), (3570, 3600, 'DIGI'), (3600, 3800, 'SSB'),
        (5250, 5450, 'SSB'),                       # 60m (kanalowe, USB)
        (7000, 7040, 'CW'), (7040, 7060, 'DIGI'), (7060, 7200, 'SSB'),
        (10100, 10130, 'CW'), (10130, 10150, 'DIGI'),   # 30m - brak fonii
        (14000, 14070, 'CW'), (14070, 14099, 'DIGI'), (14101, 14350, 'SSB'),
        (18068, 18095, 'CW'), (18095, 18109, 'DIGI'), (18111, 18168, 'SSB'),
        (21000, 21070, 'CW'), (21070, 21150, 'DIGI'), (21151, 21450, 'SSB'),
        (24890, 24915, 'CW'), (24915, 24929, 'DIGI'), (24931, 24990, 'SSB'),
        (28000, 28070, 'CW'), (28070, 28190, 'DIGI'), (28225, 29000, 'SSB'),
        (29000, 29700, 'FM'),
        # ── 6m ──
        (50000, 50100, 'CW'), (50100, 50300, 'SSB'),
        (50300, 50400, 'DIGI'), (50400, 52000, 'SSB'),
        # ── 4m (70 MHz) — SSB od 70.200, DIGI wezszy ──
        (69900, 70100, 'CW'), (70100, 70200, 'DIGI'),
        (70200, 70300, 'SSB'), (70300, 70500, 'FM'),
        # ── 2m ──
        (144000, 144150, 'CW'), (144150, 144400, 'SSB'),
        (144400, 144500, 'DIGI'), (144500, 145800, 'FM'),
        (146000, 148000, 'FM'),                    # region 2
        # ── 1.25m (222 MHz, region 2) ──
        (222000, 222150, 'CW'), (222150, 222250, 'SSB'), (222250, 225000, 'FM'),
        # ── 70cm ──
        (430000, 432000, 'FM'), (432000, 432100, 'CW'),
        (432100, 432400, 'SSB'), (432400, 432500, 'DIGI'),
        (432500, 435000, 'FM'), (438000, 440000, 'FM'),
        # ── 23cm ──
        (1296000, 1296150, 'CW'), (1296150, 1296400, 'SSB'),
        (1296400, 1296600, 'DIGI'), (1296600, 1300000, 'FM'),
        # ── 13cm ──
        (2320000, 2320150, 'CW'), (2320150, 2320400, 'SSB'), (2320400, 2450000, 'FM'),
        # ── 9cm ──
        (3400000, 3400150, 'CW'), (3400150, 3400400, 'SSB'),
        # ── 6cm ──
        (5760000, 5760150, 'CW'), (5760150, 5760400, 'SSB'),
        # ── 3cm ──
        (10368000, 10368150, 'CW'), (10368150, 10368400, 'SSB'),
        # ── 1.2cm ──
        (24048000, 24048150, 'CW'), (24048150, 24048400, 'SSB'),
    ]
    for lo, hi, mode in BANDPLAN:
        if lo <= khz < hi:
            return mode

    return '?'


# Regex dla linii "DX de <spotter>:  <freq_khz>  <call>  <comment>  <utc>"
# Przyklad: "DX de SP3ABC-#:  14074.0  DX1CALL      FT8 -12 dB               1234Z"
_DX_RE = re.compile(
    r'^DX\s+de\s+([\w\-#/]+):?\s+([\d.]+)\s+([\w/\-]+)\s+(.*?)(\d{4}Z)?\s*$',
    re.IGNORECASE
)


class DXClusterClient:
    """Pojedyncze polaczenie z serwerem DX Cluster dla jednego uzytkownika."""

    def __init__(self, host: str, port: int, login: str, password: str = "",
                 on_spot: Optional[Callable[[dict], None]] = None,
                 on_status: Optional[Callable[[str, str], None]] = None):
        self.host = host
        self.port = port
        self.login = login
        self.password = password
        self.on_spot = on_spot
        self.on_status = on_status  # (status, message) - "connecting" / "connected" / "disconnected" / "error"

        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._task: Optional[asyncio.Task] = None
        self._connected = False
        self._should_run = False
        self._reconnect_delay = 5.0
        self._max_reconnect_delay = 60.0

    def is_connected(self) -> bool:
        return self._connected

    async def _status(self, status: str, msg: str = ""):
        if self.on_status:
            try:
                res = self.on_status(status, msg)
                if asyncio.iscoroutine(res): await res
            except Exception as e:
                print(f"[dx] on_status callback blad: {e}")

    async def connect(self):
        """Rozpocznij polaczenie z auto-reconnect w tle."""
        if self._task and not self._task.done():
            return
        self._should_run = True
        self._task = asyncio.create_task(self._run_loop())

    async def disconnect(self):
        """Zatrzymaj polaczenie."""
        self._should_run = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
        self._writer = None
        self._reader = None
        self._connected = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._status("disconnected", "")

    async def _run_loop(self):
        """Petla polaczenia z auto-reconnect w razie utraty."""
        delay = self._reconnect_delay
        while self._should_run:
            try:
                await self._status("connecting", f"{self.host}:{self.port}")
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port),
                    timeout=10.0
                )
                self._connected = True
                delay = self._reconnect_delay  # reset delay po sukcesie
                await self._status("connected", "")

                # Login sequence
                await self._do_login()

                # Odbior linii
                while self._should_run:
                    line = await self._reader.readline()
                    if not line:
                        break  # server closed
                    try:
                        text = line.decode('utf-8', errors='ignore').strip()
                    except Exception:
                        continue
                    if text:
                        await self._process_line(text)

            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                await self._status("error", "Timeout polaczenia")
            except Exception as e:
                await self._status("error", str(e))

            # Cleanup i czekaj przed reconnect
            self._connected = False
            if self._writer:
                try: self._writer.close()
                except: pass
                self._writer = None

            if not self._should_run:
                break

            await self._status("disconnected", f"reconnect za {delay:.0f}s")
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            delay = min(delay * 1.5, self._max_reconnect_delay)

    async def _do_login(self):
        """Wyslij login i haslo. Serwer moze pytac 'login:', 'Please enter your call:', 'password:' itd.
        Uzywamy prostej strategii: wysylamy call, potem czekamy 500ms, jesli haslo — wysylamy."""
        if not self._writer:
            return
        # Podstawowa strategia — poczekaj na prompt, potem wyslij login
        await asyncio.sleep(0.5)
        try:
            self._writer.write((self.login + "\r\n").encode('ascii'))
            await self._writer.drain()
        except Exception as e:
            print(f"[dx] blad wysylania loginu: {e}")
            return
        # Jesli jest haslo, poczekaj krotko i wyslij
        if self.password:
            await asyncio.sleep(1.0)
            try:
                self._writer.write((self.password + "\r\n").encode('ascii'))
                await self._writer.drain()
            except Exception as e:
                print(f"[dx] blad wysylania hasla: {e}")

    async def _process_line(self, text: str):
        """Sparsuj linie i (jesli spot) wywolaj callback."""
        # Log co wpada do konsoli (przydatne przy diagnostyce nowych serwerow)
        # Nie logujemy zeby nie spamowac - odkomentuj jesli potrzeba
        # print(f"[dx {self.host}] {text}")

        m = _DX_RE.match(text)
        if not m:
            return

        spotter    = m.group(1)
        freq_str   = m.group(2)
        call       = m.group(3)
        comment    = (m.group(4) or '').strip()
        utc        = m.group(5) or ''

        # Freq zwykle w kHz z ulamkiem (14074.0)
        try:
            freq_hz = int(round(float(freq_str) * 1000))
        except ValueError:
            return

        spot = {
            "type":    "dx_spot",
            "freq_hz": freq_hz,
            "call":    call.upper(),
            "spotter": spotter.upper(),
            "comment": comment[:60],  # cap na 60 znakow
            "utc":     utc,
            "ts":      time.time(),
            "band":    _get_band(freq_hz),
            "mode":    _guess_mode(freq_hz, comment),
        }

        if self.on_spot:
            try:
                res = self.on_spot(spot)
                if asyncio.iscoroutine(res): await res
            except Exception as e:
                print(f"[dx] on_spot callback blad: {e}")

    async def send_command(self, cmd: str):
        """Wyslij komende do serwera (np. 'sh/dx 20m FT8', 'set/qth', 'q')."""
        if not self._writer or not self._connected:
            return False
        try:
            self._writer.write((cmd + "\r\n").encode('ascii'))
            await self._writer.drain()
            return True
        except Exception as e:
            print(f"[dx] send_command blad: {e}")
            return False


class ClusterManager:
    """Zarzadza polaczeniami DX Cluster dla wszystkich uzytkownikow.
    Kazdy user ma max 1 polaczenie. Broadcast callback dostarcza wiadomosci
    do wlasciwego WebSocketa uzytkownika."""

    def __init__(self, on_broadcast: Callable[[str, dict], None]):
        """on_broadcast(user_id, message) — wysyla WS do konkretnego usera."""
        self.on_broadcast = on_broadcast
        self._clients: dict[str, DXClusterClient] = {}
        # Cache ostatnich N spotow per user - zwracane przy re-open zakladki
        self._spot_history: dict[str, list[dict]] = {}
        self._max_history = 100

    def get_client(self, user_id: str) -> Optional[DXClusterClient]:
        return self._clients.get(user_id)

    def get_history(self, user_id: str) -> list[dict]:
        return list(self._spot_history.get(user_id, []))

    async def connect_user(self, user_id: str, host: str, port: int,
                            login: str, password: str = ""):
        """Utworz/zresetuj polaczenie dla uzytkownika."""
        # Zamknij poprzednie polaczenie jesli istnieje
        old = self._clients.pop(user_id, None)
        if old:
            await old.disconnect()

        async def _on_spot(spot):
            # Cache w historii
            hist = self._spot_history.setdefault(user_id, [])
            hist.append(spot)
            if len(hist) > self._max_history:
                del hist[:len(hist) - self._max_history]
            # Broadcast do usera
            res = self.on_broadcast(user_id, spot)
            if asyncio.iscoroutine(res): await res

        async def _on_status(status, msg):
            res = self.on_broadcast(user_id, {
                "type": "dx_status", "status": status, "message": msg,
            })
            if asyncio.iscoroutine(res): await res

        client = DXClusterClient(host, port, login, password,
                                  on_spot=_on_spot, on_status=_on_status)
        self._clients[user_id] = client
        await client.connect()
        return client

    async def disconnect_user(self, user_id: str):
        client = self._clients.pop(user_id, None)
        if client:
            await client.disconnect()

    async def send_command(self, user_id: str, cmd: str) -> bool:
        client = self._clients.get(user_id)
        if not client: return False
        return await client.send_command(cmd)

    async def shutdown_all(self):
        for user_id in list(self._clients.keys()):
            await self.disconnect_user(user_id)
