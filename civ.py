#!/usr/bin/env python3
"""
civ.py — bezposredni sterownik CI-V (Icom) dla radia ze scope.

Tryb BEZPOSREDNI: serwer sam otwiera port COM i obsluguje JEDNOCZESNIE:
  - sterowanie (czestotliwosc 0x03/0x05, mode 0x04/0x06, PTT 0x1C00, S-metr 0x15 02)
  - szerokopasmowy SCOPE (waterfall) — strumien CI-V 0x27 0x00

Uzywane TYLKO dla radia z wbudowanym spektroskopem (IC-7300/7610/705/9100/7100).
Pozostale radia (IC-746 itd.) dalej chodza przez rigctld (RigCAT) — bez zmian.

Interfejs (async) jest IDENTYCZNY jak RigCAT, wiec App.rig mozna podmienic w locie.

Parametry specyficzne dla modelu (adres CI-V, predkosc, mapowanie trybow,
parametry scope) sa ladowane z rigs/civ_profiles.py na podstawie model_id —
dzieki temu dodanie/poprawienie modelu nie wymaga zmian w tym pliku.

Brak pyserial / brak portu COM (np. na Replit) -> tryb SYMULACJA: generujemy
ruchome spektrum + telemetrie, zeby panel dzialal takze bez sprzetu.
"""
import asyncio, threading, time, math, random
import numpy as np

try:
    import serial  # pyserial
    HAS_SERIAL = True
except Exception:
    HAS_SERIAL = False

from rigs import get_civ_profile

CTRL_ADDR = 0xE0  # adres kontrolera (PC)


# ── BCD <-> liczby ────────────────────────────────────────────────────────────
def bcd_to_freq(b: bytes) -> int:
    """Icom freq: 5 bajtow BCD little-endian (mlodsze cyfry pierwsze)."""
    hz = 0
    mult = 1
    for byte in b:
        hz += (byte & 0x0F) * mult; mult *= 10
        hz += (byte >> 4) * mult;  mult *= 10
    return hz


def freq_to_bcd(hz: int, n: int = 5) -> bytes:
    s = f"{int(hz):0{n*2}d}"[-n * 2:]   # ostatnie n*2 cyfr
    rev = s[::-1]                        # rev[0] = jednostki
    out = bytearray()
    for i in range(0, n * 2, 2):
        lo = int(rev[i]); hi = int(rev[i + 1])
        out.append((hi << 4) | lo)
    return bytes(out)


def bcd2(b: bytes) -> int:
    """2-bajtowe BCD (np. S-metr 0000..0255) -> int."""
    if len(b) < 2:
        return 0
    return (b[0] >> 4) * 1000 + (b[0] & 0x0F) * 100 + (b[1] >> 4) * 10 + (b[1] & 0x0F)


# Mapowanie subcommand 27 15 (Scope span, Center/SCROLL-C mode) -> kHz.
# Wartosc to 4 cyfry BCD (0000..0500 wg dokumentacji), patrz CI-V Ref. str.19-14.
SCOPE_SPAN_KHZ = {
    2500: 2.5, 5000: 5, 10000: 10, 25000: 25, 50000: 50,
    100000: 100, 250000: 250, 500000: 500,
}

# Dokladna tabela szerokosci filtra IC-7300 dla SSB/CW/RTTY (CI-V 1A 03,
# idx 00-31 = 50Hz/100Hz kroki, zgodne z Instruction Manual):
#   idx 0-9:  50Hz kroki   ->  50, 100, 150, ..., 500
#   idx 10-31: 100Hz kroki -> 600, 700, ..., 2700
_SSB_FILTER_TABLE = {}
for _i in range(0, 32):
    _SSB_FILTER_TABLE[_i] = (50 + _i*50) if _i <= 9 else (600 + (_i-10)*100)

# idx 32-40: zakres SSB-D (DATA mode, USB-D/LSB-D) — szerszy filtr do 3600Hz,
# dostepny TYLKO gdy radio jest w trybie DATA (1A 06). Civ.py aktualnie nie
# sledzi flagi data-mode, wiec gdy spotkamy idx>31 (co w trybie voice SSB nie
# powinno sie zdarzyc), interpolujemy liniowo 2700->3600Hz jako przyblizenie
# zamiast zwracac blednie zacisniety 2700Hz.
for _i in range(32, 41):
    _SSB_FILTER_TABLE[_i] = round(2700 + (_i-31) * (3600-2700) / 9)


# AM/FM (CI-V 1A 03, idx 00=200Hz .. 49=10000Hz wg dokumentacji str.19-6).
# Krok nie jest jednolity w dokumentacji Icom — uzywamy znanej tabeli AM
# filter dla IC-7300 (typowe wartosci 200Hz do 10kHz w nierownych krokach
# zgrubsza odpowiadajacych szerokosci pasma odbiornika AM). Dla idx>0
# stosujemy interpolacje liniowa 200..10000Hz na 50 punktow (0-49) jako
# najlepsze dostepne przyblizenie bez dostepu do pelnej tabeli firmware.
_AMFM_FILTER_TABLE = {i: round(200 + i * (10000-200) / 49) for i in range(0, 50)}


def filter_width_hz(mode: str, idx: int, data_mode: bool = False) -> int:
    """Dokladna (SSB/CW/RTTY) lub przyblizona (AM/FM) szerokosc filtra w Hz
    dla danego CI-V 1A 03 index, w zaleznosci od trybu pracy.

    idx 32-40 (SSB-D, do 3600Hz) jest dostepny tylko gdy radio jest w trybie
    DATA (data_mode=True, odpytywane przez 1A 06). W trybie voice SSB
    (data_mode=False) radio nie powinno zwracac idx>31 — gdyby jednak
    sie to zdarzylo, zacisk do 2700Hz (idx 31) zamiast ekstrapolacji."""
    if mode in ("AM", "FM"):
        return _AMFM_FILTER_TABLE.get(idx, _AMFM_FILTER_TABLE[49])
    if idx > 31 and not data_mode:
        idx = 31
    return _SSB_FILTER_TABLE.get(idx, _SSB_FILTER_TABLE[40])


def smeter_to_sunit(raw: int) -> float:
    """Surowy S-metr Icom 0..255 -> S-jednostki (S9 = 9, S9+60dB = 15)."""
    if raw <= 120:
        return raw / 120.0 * 9.0
    return 9.0 + (raw - 120) / (241 - 120) * 60.0 / 10.0


class CivRig:
    def __init__(self, cfg: dict, broadcast_sync=None, log=print):
        self.cfg   = cfg or {}
        self.bcast = broadcast_sync or (lambda m: None)
        self.log   = log

        # ── atrybuty zgodne z RigCAT ──
        self.freq      = 14074000
        self.freq_b    = 14074000
        # Czas i wartosc ostatniego LOKALNEGO ustawienia czestotliwosci
        # (set_freq, np. z click-to-tune). Uzywane w _handle_frame, aby
        # zignorowac "stary" odczyt 0x03 z radia ktory moze przyjsc tuz po
        # wyslaniu 0x05 (CI-V echo/odpowiedz z opoznieniem) i nie nadpisac
        # nowo ustawionej czestotliwosci w UI przed faktyczna aktualizacja
        # radia.
        self._local_freq_set_at  = 0.0
        self._local_freq_set_val = None
        self.mode      = "USB"
        # Aktywny filtr DSP radia (FIL1/2/3, CI-V 06 <mode> <fil>) — wybor
        # KTOREGO mechanicznie/programowo skonfigurowanego filtra uzyc, nie
        # zmiana jego szerokosci (to ustawia sie w menu radia).
        self.filter_num = 1
        # Preamp (16 02): 0=OFF, 1=P.AMP1, 2=P.AMP2
        self.preamp = 0
        # Attenuator (11): False=OFF, True=ATT 20dB ON
        self.attenuator = False
        # Antenna Tuner (1C 01): False=OFF (bypass), True=ON (w lancuchu)
        self.tuner = False
        self.bw        = 2400
        self.data_mode = False   # tryb DATA (USB-D/LSB-D), z 1A 06 — wplywa na tabele filtra
        self.ptt       = False
        self.split     = False
        self.powered   = True     # stan zasilania radia (CI-V 0x18). Po power
                                  # OFF radio nie odpowiada — pomijamy odpytywanie
        self.vfo       = "VFOA"   # aktywne VFO — sledzone lokalnie (IC-7300
                                  # nie ma CI-V query aktywnego VFO). Default A
                                  # = radio po wlaczeniu zawsze na VFO A.
        self.s_meter   = 0.0
        # Aktualne wartosci sliderow Set Level (CI-V 14 <sub>), w jednostkach
        # UI (lvl['min']..lvl['max']) — odczytywane z radia przy polaczeniu i
        # okresowo w pollerze, zeby slidery w UI startowaly z faktycznych
        # nastaw radia, nie od 0.
        self.level_values = {}
        self.connected = False
        self.sim       = True
        self.last_err  = ""
        self.last_msg  = ""
        self.rigctld_path  = "(tryb bezposredni CI-V)"
        self.rigctld_found = True   # nie uzywamy rigctld, ale UI tego oczekuje

        # ── konfiguracja polaczenia ──
        self.addr  = 0x94
        self.port  = "COM3"
        self.speed = 115200
        self.model = "3073"

        # ── profil modelu (CI-V) — ustawiany w connect() ──
        self.profile    = get_civ_profile(self.model)
        self.mode_map   = self.profile["mode_map"]
        self.mode_rev   = {v: k for k, v in self.mode_map.items()}
        self.scope_max  = self.profile["scope_max"]
        self.scope_header_len = self.profile["scope_header_len"]

        # ── stan wewnetrzny ──
        self._ser = None
        self._running = False
        self._reader_th = None
        self._poller_th = None
        self._sim_th = None
        self._reconnect_th = None

        # ── DTR/RTS keyer ─────────────────────────────────────────────────────
        # Port szeregowy dla kluczowania DTR/RTS (moze byc ten sam co CI-V
        # lub osobny port COM z konwertera USB). Kluczuje linie DTR lub RTS
        # generujac sygnal Morse'a z tekstu (zamiast CI-V cmd 17).
        self._keyer_port  = None   # osobny port dla DTR/RTS lub None (=uzyj _ser)
        self._keyer_line  = None   # 'DTR' | 'RTS' | None
        self._keyer_ser   = None   # serial.Serial dla osobnego portu, lub ref do _ser
        self._keyer_stop  = threading.Event()
        self._keyer_th    = None

        # transakcje (jedna naraz)
        self._txlock = threading.Lock()
        self._wlock  = threading.Lock()
        self._resp_ev = threading.Event()
        self._resp_cmd = None
        self._resp_payload = None

        # CI-V TCP Bridge — subskrybenci surowych bajtow z radia.
        # Callback wywolywany dla kazdej porcji bajtow odebranej z portu,
        # ZANIM zostana sparsowane w _reader_loop. Uzywane przez civ_bridge.py
        # zeby broadcastowac raw CI-V do TCP klientow (Logger32, HRD itp.).
        # Callback musi byc thread-safe (wywolywany z wątku _reader_loop).
        self._bridge_listeners: list = []
        self._bridge_lock = threading.Lock()

        # scope
        self.scope_running = False
        self._scope_acc = bytearray()
        self._latest_scope = None   # najswiezsza gotowa ramka (czytana przez pompe scope)
        self._scope_total = 0
        self._scope_logged = 0
        self._tx_logged = 0
        self._rx_logged = 0
        self._scope_last = 0.0
        # naglowek scope (parsowany z 0x27 0x00, patrz _handle_scope)
        self._scope_mode_code = 0       # 00=Center,01=Fixed,02=SCROLL-C,03=SCROLL-F
        self._scope_center = self.freq
        self._scope_span_hz = 25000     # domyslny span 25kHz (typowy dla Center mode IC-7300)
        self._scope_lo = self.freq - self._scope_span_hz // 2
        self._scope_hi = self.freq + self._scope_span_hz // 2
        self._scope_oor = 0
        # szerokosc filtra (CI-V 1A 03), odpytywana w pollerze
        self._filter_idx = 0
        self._filter_width_hz = 2400    # domyslna szerokosc SSB

    # ════════════════════════════════════════════════════════════════════════
    # Interfejs async (wolany przez endpointy / rig_poll)
    # ════════════════════════════════════════════════════════════════════════
    async def connect(self, cfg: dict, override: dict | None = None) -> bool:
        cfg = cfg or self.cfg or {}
        rigs = cfg.get("rigs") or [{}]
        rig = rigs[0]
        if override:
            rid = override.get("rigId") or override.get("id")
            if rid is not None:
                rig = next((r for r in rigs if str(r.get("id")) == str(rid)), rig)
            rig = {**rig, **{k: v for k, v in override.items() if v}}

        self.model = str(rig.get("model", "3073"))

        # Zaladuj profil dla tego modelu
        self.profile   = get_civ_profile(self.model)
        self.mode_map  = self.profile["mode_map"]
        self.mode_rev  = {v: k for k, v in self.mode_map.items()}
        self.scope_max = self.profile["scope_max"]
        self.scope_header_len = self.profile["scope_header_len"]

        self.port  = rig.get("port", "COM3")
        try:
            self.speed = int(rig.get("speed", self.profile["default_baud"]))
        except Exception:
            self.speed = self.profile["default_baud"]

        civ = rig.get("civ") or rig.get("civAddr")
        if civ:
            self.addr = self._parse_civ(civ)
        else:
            self.addr = self.profile["default_addr"]

        self._rig_name = rig.get("name", self.profile.get("name", "Radio"))

        self.log(f"[civ] Tryb BEZPOSREDNI CI-V — model={self.model} "
                 f"({self.profile.get('name','?')}) port={self.port} "
                 f"{self.speed}bd CI-V=0x{self.addr:02X}")
        if self.profile is not None and "NIEZWERYFIKOWANE" in self.profile.get("notes", ""):
            self.log(f"[civ] UWAGA dla {self.profile.get('name')}: {self.profile['notes']}")

        # Jesli ten obiekt mial juz otwarty port (np. ponowne kliknniecie
        # "Polacz" bez zmiany modelu — webapp.py nie tworzy wtedy nowego
        # CivRig) — zamknij stare polaczenie przed otwarciem nowego, inaczej
        # serial.Serial() dostanie PermissionError (port juz otwarty przez
        # ten sam proces).
        if self._ser is not None:
            self.log("[civ] Ponowne polaczenie — zamykam istniejacy port")
            self.close()
            await asyncio.sleep(0.3)

        return await asyncio.to_thread(self._open)

    async def get_freq(self) -> int:
        return self.freq

    async def get_mode(self):
        return self.mode, self.bw

    async def get_smeter(self) -> float:
        return self.s_meter

    async def set_freq(self, hz: int):
        self.freq = int(hz)
        self._local_freq_set_at  = time.time()
        self._local_freq_set_val = int(hz)
        if self.sim or not self._ser:
            return
        # Fire-and-forget: nie czekamy na ACK (0xFB/0xFA) — radio rzadko NAK-uje
        # ustawienie czestotliwosci, a oczekiwanie do 0.4s na ACK powodowalo
        # zauwazalne opoznienie przy click-to-tune / szybkim przestrajaniu.
        try:
            await asyncio.to_thread(self._write, bytes([0x05]) + freq_to_bcd(hz))
        except Exception as e:
            self.log(f"[civ] set_freq write blad: {e}")

    async def set_freq_b(self, hz: int):
        """Ustaw czestotliwosc VFO-B (nieaktywnego VFO) bez przelaczania.
        CI-V: 25 01 [5B freq BCD] (per doc str.19-13 "Selected/unselected
        VFO frequency settings", 01=unselected). Fire-and-forget jak
        set_freq — radio rzadko NAK-uje ustawianie freq.
        """
        self.freq_b = int(hz)
        if self.sim or not self._ser:
            return
        try:
            await asyncio.to_thread(self._write, bytes([0x25, 0x01]) + freq_to_bcd(hz))
        except Exception as e:
            self.log(f"[civ] set_freq_b write blad: {e}")

    def set_scope_span(self, span_hz: int) -> bool:
        """Ustaw span waterfallu (Center mode). CI-V: 27 15 [5B BCD little-endian].
        Zwraca True jesli span wspierany, False w przeciwnym razie.

        Wspierane wartosci (Hz): 2500, 5000, 10000, 25000, 50000, 100000,
        250000, 500000 — reszta odrzucana. Format BCD LE: rozklada wartosc
        Hz na 5 bajtow po 2 cyfry (10 cyfr lacznie), gdzie bajt 0 to
        najnizsze cyfry (jednostki + dziesiatki).
        """
        if span_hz not in SCOPE_SPAN_KHZ:
            return False
        if self.sim or not self._ser:
            self._scope_span_hz = span_hz
            return True
        # Rozloz span_hz na 5 bajtow BCD little-endian
        digits = f"{span_hz:010d}"  # np. 25000 -> "0000025000"
        # BCD LE: bajt 0 = digits[8:10], bajt 1 = digits[6:8], ...
        b = [int(digits[8-i*2:10-i*2], 16) if 8-i*2 >= 0 else 0 for i in range(5)]
        # Uwaga: musimy uzyc BCD (nie hex) — kazda cyfra 0-9 w polbajcie
        b = []
        for i in range(5):
            lo = int(digits[9-i*2])
            hi = int(digits[8-i*2])
            b.append((hi << 4) | lo)
        try:
            self._write(bytes([0x27, 0x15]) + bytes(b))
            self._scope_span_hz = span_hz
            self.log(f"[civ] scope span -> {span_hz} Hz (bytes: {' '.join(f'{x:02X}' for x in b)})")
            return True
        except Exception as e:
            self.log(f"[civ] set_scope_span blad: {e}")
            return False

    async def set_mode(self, mode: str, bw: int = 0, fil: int = 0):
        """
        Ustaw mode (CI-V 06 <mode_byte> <filter_byte>).
        fil: 1=FIL1, 2=FIL2, 3=FIL3 (mechaniczne/DSP filtry skonfigurowane
        bezposrednio w radiu — wybieramy tylko KTORY jest aktywny, nie
        zmieniamy ich szerokosci). 0 = nie zmieniaj (zostaw self.filter_num
        lub domyslny FIL1 jesli nieznany).

        UWAGA o USB-D/LSB-D: to NIE sa osobne bajty trybu CI-V na IC-7300 —
        radio rozumie tylko base mode (USB=1, LSB=0) + osobna komenda
        "DATA mode" (1A 06 01=ON/00=OFF). Wczesniejsza wersja tej funkcji
        probowala znalezc "USB-D" w mode_rev (ktore zawiera tylko bazowe
        tryby z _BASE_MODE_MAP), nie znajdowala go i CICHO wysylala bajt
        domyslny (1=USB) BEZ wlaczania trybu DATA — efekt: radio zostawalo
        w zwyklym USB, mimo ze UI pokazywalo "USB-D". Teraz: rozpoznajemy
        sufiks "-D", wysylamy bazowy tryb + 1A 06 01, i domyslnie wybieramy
        FIL1 (najszerszy/najczesciej uzywany filtr DATA, zgodnie z
        konfiguracja juz ustawiona w radiu — nie zmieniamy SZEROKOSCI
        filtra, tylko KTORY slot jest aktywny).
        """
        is_data = mode.endswith("-D")
        base_mode = mode[:-2] if is_data else mode  # "USB-D" -> "USB"

        self.mode = mode  # zachowujemy PELNA nazwe (z "-D") dla UI/logiki wyzszego poziomu
        if bw:
            self.bw = bw
        if fil in (1, 2, 3):
            self.filter_num = fil
        elif is_data and not getattr(self, "_data_filter_initialized", False):
            # Pierwsze wejscie w tryb DATA bez jawnie podanego filtra —
            # domyslnie FIL1, zgodnie z prosba uzytkownika (FIL1 ma juz
            # skonfigurowana szerokosc 3000Hz bezposrednio w radiu).
            self.filter_num = 1
        fb = getattr(self, "filter_num", 1)

        if self.sim or not self._ser:
            self.data_mode = is_data
            return

        mb = self.mode_rev.get(base_mode, 1)
        await asyncio.to_thread(self._transact,
                                bytes([0x06, mb, fb]), {0xFB, 0xFA}, 0.4)

        # Wlacz/wylacz tryb DATA osobna komenda (1A 06), niezaleznie od
        # bazowego trybu — to jest faktyczny przelacznik USB<->USB-D na IC-7300.
        if is_data != self.data_mode:
            await asyncio.to_thread(self._transact,
                                    bytes([0x1A, 0x06, 0x01 if is_data else 0x00]),
                                    {0x1A}, 0.4)
            self.data_mode = is_data
            self._data_filter_initialized = True
            self._filter_width_hz = filter_width_hz(self.mode, getattr(self, "_filter_idx", 0), self.data_mode)
            self.bcast({"type": "filter_width", "hz": self._filter_width_hz,
                        "idx": getattr(self, "_filter_idx", 0), "mode": self.mode,
                        "dataMode": self.data_mode})

    async def set_ptt(self, on: bool):
        self.ptt = bool(on)
        if self.sim or not self._ser:
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x1C, 0x00, 0x01 if on else 0x00]), {0xFB, 0xFA}, 0.4)

    # Mapowanie znakow na kody CI-V CW message (cmd 17, doc str.19-12).
    # Wiekszosc to standardowe ASCII (0-9, A-Z, a-z) — wysylamy bezposrednio.
    # Dodatkowe symbole: / ? . - , : ' ( ) = + " @ spacja — rowniez ASCII.
    # "^" laczy znaki bez przerwy miedzy nimi (prosody); "FF" przerywa wysylke.
    _CW_ALLOWED = set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                      "/?.-,:'()=+\"@ ^")

    async def send_cw_message(self, text: str):
        """
        Wyslij tekst jako CW przez CI-V (cmd 17, max 30 znakow na raz wg
        dokumentacji IC-7300/IC-746).

        Warunki wymagane przez radio:
        1. Tryb musi byc CW lub CW-R (cmd 06 03 / 06 07)
        2. Break-In musi byc wlaczony (cmd 16 47 01) — inaczej radio
           przetwarza cmd 17 ale nie wchodzi w TX
        3. Dla IC-746: identyczna sekwencja, te same komendy

        Funkcja automatycznie:
        - Przelacza na CW jesli aktualny tryb nie jest CW/CW-R
        - Wlacza BK-IN jesli nie jest wlaczony
        - Przywraca poprzedni tryb po zakonczeniu wysylania
        """
        text = "".join(c for c in text.upper() if c in self._CW_ALLOWED)
        if not text:
            return
        if self.sim or not self._ser:
            self.log(f"[civ] send_cw_message (SIM): {text!r}")
            return

        prev_mode = self.mode

        # Krok 1: Przelacz na CW jesli potrzeba
        cw_modes = ('CW', 'CW-R')
        if self.mode not in cw_modes:
            self.log(f"[civ] CW send: przelaczam z {self.mode!r} na CW")
            await asyncio.to_thread(
                self._transact, bytes([0x06, 0x03]), {0xFB, 0xFA}, 0.4)
            self.mode = 'CW'
            await asyncio.sleep(0.15)

        # Krok 2: PTT ON (1C 00 01) — przez USB radio wymaga PTT przed cmd 17
        # BK-IN przez USB powoduje ciagly sygnal — zamiast tego uzywamy PTT
        self.log("[civ] CW send: PTT ON")
        await asyncio.to_thread(
            self._transact, bytes([0x1C, 0x00, 0x01]), {0xFB, 0xFA}, 0.4)
        self.ptt = True
        # Opoznienie PTT->kluczowanie. IC-7300 przez USB potrzebuje czasu na
        # przejscie na nadawanie (przekaznik T/R, ewentualny tuner). Przy zbyt
        # krotkim opoznieniu pierwszy znak idzie, zanim radio w pelni nadaje,
        # i poczatek sie ucina (obserwowane: 'XX0XXX' -> gubione 'S'/'SQ').
        # 50 ms bylo za malo; 250 ms daje zapas. Konfigurowalne przez
        # cwPttDelay w ustawieniach (ms), bo tunery potrzebuja wiecej.
        _ptt_delay = float(self.profile.get("cwPttDelay", 250)) / 1000.0
        await asyncio.sleep(max(0.05, _ptt_delay))

        # Krok 3: Wyslij tekst przez CI-V cmd 17
        # Radio moduluje CW z aktywnym PTT
        for i in range(0, len(text), 30):
            chunk = text[i:i+30]
            self.log(f"[civ] CW send chunk: {chunk!r}")
            await asyncio.to_thread(
                self._write, bytes([0x17]) + chunk.encode("ascii"))
            if i + 30 < len(text):
                await asyncio.sleep(0.1)

        # Krok 4: Odczytaj aktualny WPM z radia i poczekaj
        try:
            lvl = self.profile.get("levels", {}).get("KEYSPD")
            if lvl:
                fresh_wpm = await asyncio.to_thread(self._read_level, "KEYSPD", lvl)
                if fresh_wpm is not None:
                    self.level_values["KEYSPD"] = fresh_wpm
        except Exception:
            pass
        wpm = int(self.level_values.get("KEYSPD", 18) or 18)
        # Exact CW duration instead of the old flat "len*10 units *1.5" guess.
        # The old formula assumed every character is 10 units and then padded
        # 50%, so PTT hung long after the keyer finished (audible carrier after
        # the last character) and, for text with long characters, could even cut
        # the macro short. We now sum the real Morse timing (dot=1, dash=3,
        # intra-char gap=1, inter-char gap=3, word gap=7 units) so the hold
        # matches what the rig's keyer actually sends.
        wait_s = self._cw_text_duration_s(text, wpm) + 0.15
        wait_s = max(0.3, min(wait_s, 120.0))
        self.log(f"[civ] CW send: czekam {wait_s:.1f}s ({wpm} WPM)")
        await asyncio.sleep(wait_s)

        # Krok 5: PTT OFF
        self.log("[civ] CW send: PTT OFF")
        await asyncio.to_thread(
            self._transact, bytes([0x1C, 0x00, 0x00]), {0xFB, 0xFA}, 0.4)
        self.ptt = False
        # T/R recovery: after PTT OFF the rig needs a moment to leave transmit
        # (T/R relay, internal sequencing) before it can cleanly accept a new CW
        # buffer. Without this, a macro fired immediately after the previous one
        # finished would push its cmd-17 text into the rig mid-transition and the
        # rig would drop it — carrier keys up but nothing is sent (empty carrier
        # with a valid chunk in the log). Configurable via cwTrRecovery (ms).
        _tr_recovery = float(self.profile.get("cwTrRecovery", 200)) / 1000.0
        await asyncio.sleep(max(0.05, _tr_recovery))
        self.log("[civ] CW send: zakonczono")

    async def stop_cw_message(self):
        """Przerwij wysylanie CW message (cmd 17 FF = clear buffer)."""
        if self.sim or not self._ser:
            return
        # Przerwij bufor CW — radio samo wylaczy TX po otrzymaniu FF
        await asyncio.to_thread(self._write, bytes([0x17, 0xFF]))
        self.log("[civ] CW stop: bufor wyczyszczony")

    # ── DTR/RTS Keyer ─────────────────────────────────────────────────────────

    # Tabela Morse'a — znak -> ciag '.' i '-'
    _MORSE_TABLE = {
        'A':'.-',   'B':'-...',  'C':'-.-.',  'D':'-..',
        'E':'.',    'F':'..-.',  'G':'--.',   'H':'....',
        'I':'..',   'J':'.---',  'K':'-.-',   'L':'.-..',
        'M':'--',   'N':'-.',    'O':'---',   'P':'.--.',
        'Q':'--.-', 'R':'.-.',   'S':'...',   'T':'-',
        'U':'..-',  'V':'...-',  'W':'.--',   'X':'-..-',
        'Y':'-.--', 'Z':'--..',
        '0':'-----','1':'.----','2':'..---','3':'...--',
        '4':'....-','5':'.....','6':'-....','7':'--...',
        '8':'---..','9':'----.',
        '.':'.-.-.-', ',':'--..--', '?':'..--..',
        '/':'-..-.',  '+':'.-.-.',  '-':'-....-',
        '=':'-...-',  ':':'---...',  '\'':'.----.',
        '@':'.--.-.', '(':'-.--.', ')':'-.--.-',
        ' ': None,   # przerwa miedzy slowami
    }

    def _cw_text_duration_s(self, text: str, wpm: int) -> float:
        """Exact time to send `text` in CW at `wpm`, in seconds.

        Uses standard Morse timing (PARIS): dit = 1 unit, dah = 3, gap between
        elements = 1, gap between letters = 3, gap between words = 7. Summing
        the real element counts (instead of assuming 10 units/char) makes the
        PTT hold match the keyer, so the carrier drops right after the last
        element and long macros are never cut short.
        """
        wpm = max(5, min(60, wpm))
        dit = 1.200 / wpm
        units = 0.0
        first_char = True
        for ch in text.upper():
            if ch == ' ':
                units += 7.0          # word gap
                first_char = True
                continue
            code = self._MORSE_TABLE.get(ch)
            if not code:
                continue
            if not first_char:
                units += 3.0          # gap before this letter
            first_char = False
            for i, sym in enumerate(code):
                units += 3.0 if sym == '-' else 1.0   # dah / dit
                if i < len(code) - 1:
                    units += 1.0      # intra-character gap
        return units * dit

    def configure_keyer(self, port: str, line: str):
        """
        Skonfiguruj keyer DTR/RTS.
        port: 'COM3', 'COM5' itp. (moze byc ten sam co CI-V lub osobny)
        line: 'DTR' lub 'RTS'
        Jezeli port == '' (pusty) — uzyj tego samego portu co CI-V (_ser).
        """
        import serial as _serial
        self._keyer_line = line.upper() if line else 'DTR'
        if port and port != getattr(self, '_port', ''):
            # Osobny port dla keyera
            try:
                if self._keyer_ser and self._keyer_ser is not self._ser:
                    try: self._keyer_ser.close()
                    except: pass
                self._keyer_ser = _serial.Serial(port, baudrate=9600, timeout=0.1)
                # Wyzeruj DTR/RTS od razu - inaczej otwarcie portu wrzuca KEY DOWN
                try:
                    self._keyer_ser.dtr = False
                    self._keyer_ser.rts = False
                except Exception:
                    pass
                self._keyer_port = port
                self.log(f"[keyer] Otwarty port {port} dla {line} keying")
            except Exception as e:
                self.log(f"[keyer] Blad otwierania portu {port}: {e}")
                self._keyer_ser = None
        else:
            # Uzyj tego samego portu co CI-V
            self._keyer_ser = self._ser
            self._keyer_port = ''

    def _set_key(self, state: bool):
        """Ustaw linie DTR lub RTS (True=KEY DOWN, False=KEY UP)."""
        ser = self._keyer_ser or self._ser
        if not ser:
            return
        try:
            if self._keyer_line == 'RTS':
                ser.rts = state
            else:
                ser.dtr = state
        except Exception as e:
            self.log(f"[keyer] _set_key blad: {e}")

    def _send_morse_blocking(self, text: str, wpm: int, stop_event):
        """
        Synchroniczne wysylanie CW przez DTR/RTS (wywolywane w osobnym watku).
        dit_ms = 1200 / wpm  (standard Morse timing, PARIS word = 50 units).
        stop_event: threading.Event — przerywa wysylanie natychmiast.
        """
        import time as _time
        wpm = max(5, min(60, wpm))
        dit = 1.200 / wpm   # czas dita w sekundach

        # Opoznienie na starcie — radio potrzebuje chwili na przejscie na
        # nadawanie (przekaznik T/R, tuner). Bez tego pierwszy dit idzie, zanim
        # radio nadaje, i poczatek znaku sie ucina (gubione 'S'/'SQ' na starcie
        # operator). Konfigurowalne przez cwPttDelay (ms).
        _ptt_delay = float(self.profile.get("cwPttDelay", 250)) / 1000.0
        _time.sleep(max(0.05, _ptt_delay))

        def key_down(t):
            self._set_key(True)
            _time.sleep(t)
            self._set_key(False)

        def pause(t):
            _time.sleep(t)

        for char in text.upper():
            if stop_event.is_set():
                break
            if char == ' ':
                pause(dit * 7)  # przerwa miedzy slowami
                continue
            code = self._MORSE_TABLE.get(char)
            if not code:
                continue
            for i, sym in enumerate(code):
                if stop_event.is_set():
                    break
                if sym == '.':
                    key_down(dit)
                else:
                    key_down(dit * 3)
                if i < len(code) - 1:
                    pause(dit)   # przerwa miedzy elementami
            pause(dit * 3)   # przerwa miedzy literami
        self._set_key(False)   # upewnij sie ze klucz jest zwolniony

    async def send_cw_dtr_rts(self, text: str, wpm: int):
        """
        Wyslij tekst CW przez DTR/RTS keying (asynchronicznie, w osobnym watku).
        Przerywa poprzednie wysylanie jezeli jest aktywne.
        """
        # Zatrzymaj poprzednie wysylanie
        self._keyer_stop.set()
        if self._keyer_th and self._keyer_th.is_alive():
            self._keyer_th.join(timeout=0.5)
        self._keyer_stop.clear()

        if self.sim:
            self.log(f"[keyer] SIM DTR/RTS: {text!r} @ {wpm} WPM")
            return

        stop_ev = self._keyer_stop
        th = threading.Thread(
            target=self._send_morse_blocking,
            args=(text, wpm, stop_ev),
            daemon=True, name="morse-keyer"
        )
        self._keyer_th = th
        th.start()

    async def stop_cw_dtr_rts(self):
        """Przerwij wysylanie DTR/RTS natychmiast."""
        self._keyer_stop.set()
        self._set_key(False)

    async def set_split(self, on: bool):
        self.split = bool(on)
        if self.sim or not self._ser:
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x0F, 0x01 if on else 0x00]), {0xFB, 0xFA}, 0.4)

    async def set_power(self, on: bool):
        """
        Wlacz/wylacz radio (CI-V cmd 0x18 0x01/0x00).

        WAZNE — power ON wymaga "wakeup": gdy radio jest w pelni wylaczone,
        UART nie nasluchuje na pelnej predkosci. Standardowa sekwencja Icom:
        wyslij dlugi preambul bajtow 0xFE (~150ms przy danym baudrate) PRZED
        ramka 0x18 0x01 — to "budzi" odbiornik UART radia.

        Wymaga w radiu: MENU > SET > Connectors > CI-V > "CI-V Transceive" = ON
        oraz zasilanie 12V podane do radia (sam USB nie wystarczy gdy radio
        jest w trybie pelnego OFF na niektorych modelach).

        Po "power OFF" polaczenie CI-V przestanie odpowiadac na zwykle
        transakcje (radio nie nasluchuje) — to oczekiwane, dopoki nie
        wyslemy wakeup+power ON.
        """
        if self.sim or not self._ser:
            return

        # Zapamietaj stan zasilania — po power OFF radio nie odpowiada na
        # zadne transakcje CI-V. Bez tego petla telemetrii dalej odpytuje
        # S-metr i co sekunde wypisuje "S-metr transact fail", zasmiecajac log.
        self.powered = bool(on)

        if on:
            await asyncio.to_thread(self._wakeup_and_power_on)
        else:
            await asyncio.to_thread(self._transact,
                                    bytes([0x18, 0x00]), {0xFB, 0xFA}, 0.6)

    def _wakeup_and_power_on(self):
        """
        Wysyla preambul 0xFE (wakeup) + ramke 0x18 0x01 (power ON).
        Czas trwania preambulu zalezy od baudrate: przy 115200bd Icom
        zaleca min. ~150ms ciaglej transmisji 0xFE (ok. 150 bajtow).
        Po power-on radio potrzebuje kilku sekund na boot — nie czekamy
        na ACK (transakcja zwykle timeoutuje, bo radio jeszcze nie gotowe).
        """
        try:
            preamble_len = max(150, self.speed // 768)  # ~150ms danych
            preamble = b"\xfe" * preamble_len
            frame = bytes([0xFE, 0xFE, self.addr, CTRL_ADDR, 0x18, 0x01, 0xFD])
            with self._wlock:
                if self._ser:
                    self._ser.write(preamble)
                    time.sleep(0.05)
                    self._ser.write(frame)
            self.log(f"[civ] wakeup+power ON wyslany (preambul {preamble_len}B)")
        except Exception as e:
            self.log(f"[civ] wakeup_and_power_on blad: {e}")

    async def set_vfo(self, vfo: str):
        """
        Przelacz aktywne VFO — CI-V cmd 0x07.
        vfo: 'VFOA' -> 0x00, 'VFOB' -> 0x01
        """
        _name = "VFOA" if vfo.upper() in ("VFOA", "A") else "VFOB"
        self.vfo = _name  # sledz stan (takze w sim — UI ma znac prawde)
        if self.sim or not self._ser:
            return
        sub = 0x00 if _name == "VFOA" else 0x01
        await asyncio.to_thread(self._transact,
                                bytes([0x07, sub]), {0xFB, 0xFA}, 0.4)

    async def vfo_equalize(self):
        """A->B: skopiuj czestotliwosc/mode z VFO A do VFO B (CI-V 07 A0)."""
        if self.sim or not self._ser:
            self.freq_b = self.freq
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x07, 0xA0]), {0xFB, 0xFA}, 0.4)

    async def vfo_swap(self):
        """A<->B: zamien VFO A i B (CI-V 07 B0)."""
        if self.sim or not self._ser:
            self.freq, self.freq_b = self.freq_b, self.freq
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x07, 0xB0]), {0xFB, 0xFA}, 0.4)

    async def set_func(self, func: str, on: bool):
        """
        Wlacz/wylacz funkcje radia — CI-V cmd 0x16 <sub> <00|01>.
        Mapowanie nazwa->subcommand jest w profilu modelu (profile['funcs']).
        """
        if self.sim or not self._ser:
            return
        funcs = self.profile.get("funcs", {})
        sub = funcs.get(func.upper())
        if sub is None:
            self.log(f"[civ] set_func: nieznana funkcja '{func}' dla {self.profile.get('name')}")
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x16, sub, 0x01 if on else 0x00]), {0xFB, 0xFA}, 0.4)

    async def set_preamp(self, level: int):
        """
        Preamp — CI-V 16 02 <00|01|02>.
        level: 0=OFF, 1=Preamp1 (P.AMP1), 2=Preamp2 (P.AMP2, tylko IC-7300/7610 itp.)
        """
        level = max(0, min(2, int(level)))
        self.preamp = level
        if self.sim or not self._ser:
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x16, 0x02, level]), {0xFB, 0xFA}, 0.4)

    async def set_attenuator(self, on: bool):
        """
        Attenuator — CI-V 11 <00|20>.
        IC-7300 ma jeden stopien tlumika 20dB: 00=OFF, 20=ON (20dB).
        """
        on = bool(on)
        self.attenuator = on
        if self.sim or not self._ser:
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x11, 0x20 if on else 0x00]), {0xFB, 0xFA}, 0.4)

    async def set_tuner(self, on: bool):
        """
        Antenna Tuner ON/OFF — CI-V 1C 01 <00|01>.
        00=OFF (tuner w obwodzie bypass), 01=ON (tuner wlaczony w lancuch
        sygnalu — wymagane przed AUTOTUNE).
        """
        on = bool(on)
        self.tuner = on
        if self.sim or not self._ser:
            return
        await asyncio.to_thread(self._transact,
                                bytes([0x1C, 0x01, 0x01 if on else 0x00]), {0xFB, 0xFA}, 0.4)

    async def start_tuner_autotune(self):
        """
        Antenna Tuner START (cykl auto-tuningu) — CI-V 1C 01 02.
        Radio rozpoczyna szukanie dopasowania (kilka sekund, generuje sygnal
        TX o niskiej mocy). Tuner musi byc ON (01) przed wywolaniem — w
        praktyce IC-7300 wlacza tuner automatycznie razem ze START, ale dla
        pewnosci ustawiamy ON tuz przed.
        """
        self.tuner = True
        if self.sim or not self._ser:
            return
        # Krok 1: Upewnij sie ze tuner jest ON
        await asyncio.to_thread(self._transact,
                                bytes([0x1C, 0x01, 0x01]), {0xFB, 0xFA}, 0.5)
        # Krok 2: Krotkie opoznienie — radio potrzebuje chwili na przelaczenie
        await asyncio.sleep(0.1)
        # Krok 3: START autotune — radio generuje sygnal TX i stroi ATU
        # Timeout 2.0s bo radio moze nie odpowiedziec od razu (zajete strojeniem)
        await asyncio.to_thread(self._transact,
                                bytes([0x1C, 0x01, 0x02]), {0xFB, 0xFA}, 2.0)

    async def set_level(self, level_name: str, value: float):
        """
        Ustaw poziom (np. RFPOWER, AF, MICGAIN) — CI-V cmd 0x14 <sub> <BCD 0000-0255>.
        'value' jest w jednostkach UI (zakres level['min']..level['max']
        z profilu) i jest liniowo skalowane do 0-255 dla CI-V.
        Mapowanie nazwa->subcommand+zakres jest w profilu modelu (profile['levels']).
        """
        levels = self.profile.get("levels", {})
        lvl = levels.get(level_name.upper())
        if lvl is None:
            self.log(f"[civ] set_level: nieznany poziom '{level_name}' dla {self.profile.get('name')}")
            return

        lo, hi = lvl["min"], lvl["max"]
        try:
            value = float(value)
        except (TypeError, ValueError):
            return
        # Skaluj value (lo..hi) -> civ_val (0..civ_max)
        civ_max = lvl.get("civ_max", 255)
        if hi != lo:
            frac = (value - lo) / (hi - lo)
        else:
            frac = 0.0
        frac = max(0.0, min(1.0, frac))
        civ_val = int(round(frac * civ_max))

        if self.sim or not self._ser:
            return

        # CI-V 0x14: subcommand + 2-bajtowe BCD (0000-0255)
        bcd = self._int_to_bcd2(civ_val)

        # Aktualizuj lokalny cache od razu (optymistycznie) — odczyt z radia
        # nastapi przy nastepnym _read_all_levels()/pollerze i ewentualnie
        # skoryguje, ale to pozwala UI/innym klientom widziec nowa wartosc
        # natychmiast po WS broadcast (jesli webapp.py to zrobi).
        self.level_values[level_name.upper()] = value

        if self.sim or not self._ser:
            return

        await asyncio.to_thread(self._transact,
                                bytes([0x14, lvl["sub"]]) + bcd, {0xFB, 0xFA}, 0.4)

    def _read_level(self, level_name: str, lvl: dict) -> float | None:
        """
        Odczytaj aktualna wartosc slidera Set Level (CI-V 14 <sub>) z radia
        i przeskaluj BCD (0..civ_max) na jednostki UI (lvl['min']..lvl['max']).
        Zwraca None jesli radio nie odpowiedzialo. Wywolywane synchronicznie
        (w wątku polaczenia/pollera, nie w asyncio loop).
        """
        p = self._transact(bytes([0x14, lvl["sub"]]), {0x14}, 0.3)
        if not p or len(p) < 3 or p[0] != lvl["sub"]:
            return None
        civ_val = bcd2(p[1:3])
        civ_max = lvl.get("civ_max", 255)
        lo, hi = lvl["min"], lvl["max"]
        frac = civ_val / civ_max if civ_max else 0.0
        frac = max(0.0, min(1.0, frac))
        return lo + frac * (hi - lo)

    def _read_all_levels(self):
        """
        Odczytaj aktualne wartosci WSZYSTKICH sliderow Set Level zdefiniowanych
        w profilu radia i zapisz w self.level_values. Wywolywane raz po
        polaczeniu (_open) — zeby UI startowal z faktycznych nastaw radia,
        nie od 0/min.

        UWAGA: NB_LEVEL i BKINDL maja rozne subcommand (0x12 vs 0x0F po
        poprawce) wiec nie ma juz kolizji odczytu miedzy nimi.
        """
        levels = self.profile.get("levels", {})
        for name, lvl in levels.items():
            try:
                val = self._read_level(name, lvl)
                if val is not None:
                    self.level_values[name] = val
            except Exception as e:
                self.log(f"[civ] _read_all_levels: blad odczytu {name}: {e}")

    @staticmethod
    def _int_to_bcd2(v: int) -> bytes:
        """
        0-255 (lub do 999) -> 2-bajtowe BCD jak Icom CI-V Set Level
        (np. 0255 -> bytes([0x02, 0x55]), zgodnie z dokumentacja:
        "00 00=min. to 02 55=max."). Odwrotnosc bcd2().

        POPRAWKA: poprzednia wersja zwracala bytes([(d1<<4)|d0, d2]) co dla
        255 dawalo [0x55, 0x02] — bajty zamienione miejscami wzgledem
        bcd2() i specyfikacji CI-V. Po tej poprawce
        bcd2(_int_to_bcd2(v)) == v dla v w 0..255.
        """
        v = max(0, min(255, int(v)))
        hi = v // 100          # 0-2
        lo = v % 100           # 0-99
        byte0 = hi              # 0x00, 0x01, lub 0x02 (BCD == binarne dla 0-2)
        byte1 = ((lo // 10) << 4) | (lo % 10)
        return bytes([byte0, byte1])

    async def get_capabilities(self) -> dict:
        """
        Zwroc pelna strukture odkrytych mozliwosci CI-V:
        {"actions": [VFO A/B, func toggles], "sliders": [Set Level],
         "raw_caps": {feature_id: bool}}

        Buduje liste na podstawie profilu modelu (profile['levels']/['funcs'])
        — analogicznie do hamlib_caps.discover_capabilities, ale zrodlem
        jest statyczny profil CI-V (nie odpytujemy radia o jego capabilities,
        bo CI-V nie ma komendy "dump_caps").
        """
        raw_caps = dict(self.profile.get("capabilities", {}))

        actions = []
        if raw_caps.get("vfo_ab"):
            actions.append({"id": "vfo_a", "label": "VFO A", "group": "vfo",
                             "kind": "vfo_select", "value": "VFOA"})
            actions.append({"id": "vfo_b", "label": "VFO B", "group": "vfo",
                             "kind": "vfo_select", "value": "VFOB"})

        if raw_caps.get("power"):
            # Uniwersalna etykieta (PL/EN) zamiast opisu po polsku — ten
            # przycisk renderuje sie tak samo w obu jezykach frontendu,
            # kolor (zielony/czerwony) pokazuje faktyczny stan ON/OFF.
            actions.append({"id": "power_toggle", "label": "PWR ON/OFF", "group": "tx",
                             "kind": "power_toggle", "value": "POWER"})

        func_labels = self.profile.get("func_labels", {})
        for func_name in self.profile.get("funcs", {}):
            label = func_labels.get(func_name, func_name)
            actions.append({
                "id": f"func_{func_name.lower()}", "label": label, "group": "func",
                "kind": "func_toggle", "value": func_name,
            })

        sliders = []
        for level_name, lvl in self.profile.get("levels", {}).items():
            # Wartosc domyslna = lvl['min'] jesli nie odczytano jeszcze z
            # radia (np. tryb symulacji) — inaczej realna nastawa z
            # self.level_values (wypelniana przez _read_all_levels() po
            # polaczeniu, patrz _open()).
            current = self.level_values.get(level_name, lvl["min"])
            # Step dla slidera: dla zakresow 0.0-1.0 (skalowane na 0-100% w
            # UI) liczymy krok proporcjonalny do 255 poziomow CI-V. Dla
            # zakresow calkowitych (np. KEYSPD 6-48 WPM, CWPITCH 300-900Hz)
            # krok = 1 jednostka UI — sensowniejsze niz 0.16 WPM.
            if lvl["max"] <= 1.0:
                step = (lvl["max"] - lvl["min"]) / 255.0 if lvl["max"] != lvl["min"] else 1
            else:
                step = 1
            sliders.append({
                "id": f"level_{level_name.lower()}", "label": lvl.get("label", level_name),
                "group": "level", "kind": "set_level", "param": level_name,
                "min": lvl["min"], "max": lvl["max"], "value": current,
                "step": step,
            })

        return {"actions": actions, "sliders": sliders, "raw_caps": raw_caps}

    def close(self):
        self._running = False
        self.scope_running = False
        try:
            if self._ser:
                # wylacz strumien scope, zeby radio nie zalewalo portu po rozlaczeniu
                try:
                    self._write(bytes([0x27, 0x11, 0x00]))
                    time.sleep(0.05)
                except Exception:
                    pass
                self._ser.close()
        except Exception:
            pass
        self._ser = None

    # ── sterowanie scope (z endpointu /api/scope/start|stop) ──
    def scope_start(self):
        if self.sim or not self._ser:
            self.scope_running = True
            return {"ok": True, "sim": True, "running": True}
        self._enable_scope(True)
        self.scope_running = True
        return {"ok": True, "sim": False, "running": True}

    def scope_stop(self):
        self.scope_running = False
        if self._ser:
            try:
                self._enable_scope(False)
            except Exception:
                pass
        return {"ok": True, "running": False}

    # ════════════════════════════════════════════════════════════════════════
    # Warstwa szeregowa (watki)
    # ════════════════════════════════════════════════════════════════════════
    def _parse_civ(self, civ) -> int:
        try:
            s = str(civ).strip().lower().replace("0x", "").replace("h", "")
            return int(s, 16)
        except Exception:
            return self.profile.get("default_addr", 0x94)

    def _open(self) -> bool:
        if not HAS_SERIAL:
            self.last_err = ("Brak biblioteki pyserial — tryb bezposredni CI-V wymaga pyserial. "
                             "Uruchamiam SYMULACJE scope.")
            self.log(f"[civ] {self.last_err}")
            return self._start_sim()
        try:
            self._ser = serial.Serial(self.port, self.speed, timeout=0.05, write_timeout=1.0)
        except Exception as e:
            self.last_err = (f"Nie moge otworzyc {self.port} @ {self.speed}bd: {type(e).__name__}: {e}. "
                             f"Sprawdz: wlasciwy COM, baud CI-V w radiu "
                             f"(domyslnie {self.profile['default_baud']} dla {self.profile.get('name','tego modelu')}), "
                             f"port wolny (zamknij rigctld/N1MM/RCForb). Uruchamiam SYMULACJE scope.")
            self.log(f"[civ] {self.last_err}")
            return self._start_sim()

        # KRYTYCZNE: pyserial domyslnie ustawia DTR=True i RTS=True przy
        # otwarciu portu. Jesli radio ma DTR/RTS skonfigurowane jako PTT/CW-key
        # (typowe dla IC-7300), radio NATYCHMIAST wchodzi w TX. Wymuszamy stan
        # niski od razu po otwarciu - PRZED jakakolwiek komunikacja. Keyer sam
        # podniesie linie gdy faktycznie bedzie kluczowal (KEY DOWN).
        try:
            self._ser.dtr = False
            self._ser.rts = False
        except Exception as _e:
            self.log(f"[civ] Ostrzezenie: nie moge wyzerowac DTR/RTS: {_e}")

        self.sim = False
        self._running = True
        self._reader_th = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_th.start()

        # odczyt poczatkowy czestotliwosci / trybu
        ok_freq = False
        for _ in range(3):
            p = self._transact(bytes([0x03]), {0x03, 0x00}, 0.6)
            if p:
                f = bcd_to_freq(p)
                if f:
                    self.freq = f; ok_freq = True; break
            time.sleep(0.4)

        if not ok_freq:
            # Radio moze byc fizycznie OFF — port USB-CI-V istnieje (ma
            # zasilanie z USB) ale UART radia "spi" i nie odpowiada na
            # zwykle zapytania. Wyslij wakeup+power-on (preambul 0xFE +
            # 0x18 0x01, patrz IC-7300 manual *3) i sprobuj ponownie po
            # czasie na boot radia (~2-3s).
            self.log("[civ] 0x03 brak odpowiedzi — probuje wakeup+power ON (radio moze byc OFF)")
            self._wakeup_and_power_on()
            time.sleep(2.5)
            for _ in range(4):
                p = self._transact(bytes([0x03]), {0x03, 0x00}, 0.6)
                if p:
                    f = bcd_to_freq(p)
                    if f:
                        self.freq = f; ok_freq = True
                        self.log("[civ] Radio obudzone przez wakeup+power ON")
                        break
                time.sleep(0.5)

        pm = self._transact(bytes([0x04]), {0x04, 0x01}, 0.6)
        if pm and len(pm) >= 1:
            base_pm = self.mode_map.get(pm[0])
            if base_pm:
                self.mode = f"{base_pm}-D" if self.data_mode else base_pm

        if not ok_freq:
            self.last_err = (f"Radio nie odpowiada na CI-V (0x03), nawet po wakeup+power ON. "
                             f"Sprawdz: model, CI-V address (domyslnie 0x{self.profile['default_addr']:02X} "
                             f"dla {self.profile.get('name','tego modelu')}), "
                             f"baud, kabel/COM, zasilanie 12V radia. Uruchamiam SYMULACJE scope.")
            self.log(f"[civ] {self.last_err}")
            self._running = False
            try: self._ser.close()
            except Exception: pass
            self._ser = None
            return self._start_sim()

        self.connected = True
        self.last_err = ""
        self.last_msg = (f"{getattr(self,'_rig_name','Radio')} (CI-V bezposredni) {self.port} "
                         f"{self.speed}bd — {self.freq}Hz {self.mode}")
        self.log(f"[civ] POLACZONO: {self.last_msg}")

        # wlacz strumien scope + uruchom poller telemetrii
        self._enable_scope(True)
        self.scope_running = True

        # Odczytaj aktualne nastawy wszystkich sliderow (Set Level, CI-V 14
        # <sub>) z radia PRZED ogloszeniem polaczenia — dzieki temu
        # get_capabilities() (wolane przez _on_rig_reconnected w webapp.py)
        # zwroci realne wartosci i slidery w UI wystartuja z faktycznych
        # nastaw radia, nie od 0.
        self._read_all_levels()

        # Odczytaj czestotliwosc i tryb jednorazowo przed startem pollera —
        # bez tego self.freq ma hardkodowana domyslna wartosc (14074000) az
        # do pierwszego cyklu _poller_loop (kilka sekund). Klienci laczacy
        # sie w tym oknie dostaja zla czestotliwosc w wiadomosci 'init' i
        # wyswietlacz VFO pokazuje np. 14.074 zamiast faktycznej czestotliwosci
        # radia. Ten sam wzorzec co _read_all_levels() powyzej.
        try:
            # freq: komenda 03, odpowiedz 03 lub 00
            bp = self._transact(bytes([0x03]), {0x03, 0x00}, 0.4)
            if bp:
                f = bcd_to_freq(bp[:5])
                if f and f > 0:
                    self.freq = f
                    self.log(f"[civ] odczyt startowy freq={f}Hz")
            # mode: komenda 04, odpowiedz 04 lub 01
            bm = self._transact(bytes([0x04]), {0x04, 0x01}, 0.3)
            if bm and len(bm) >= 1:
                mode_byte = bm[0]
                MODE_MAP = {0x00:'LSB',0x01:'USB',0x02:'AM',0x03:'CW',
                            0x04:'RTTY',0x05:'FM',0x06:'CW-R',0x07:'RTTY-R',
                            0x08:'LSB-D',0x09:'USB-D',0x11:'PKTUSB',0x12:'PKTLSB'}
                if mode_byte in MODE_MAP:
                    self.mode = MODE_MAP[mode_byte]
                    self.log(f"[civ] odczyt startowy mode={self.mode}")
        except Exception as e:
            self.log(f"[civ] odczyt startowy freq/mode blad (nieblokujacy): {e}")

        # Bezpieczenstwo: wyłacz BK-IN przy starcie zeby radio nie wchodzilo
        # w ciagle TX. BK-IN (CI-V 16 47 00) moze byc wlaczone z poprzedniej
        # sesji lub przez blad. Uzytkownik moze wlaczyc recznie w panelu.
        try:
            self._transact(bytes([0x16, 0x47, 0x00]), {0xFB, 0xFA}, 0.4)
            self.log("[civ] BK-IN wylaczone przy starcie (bezpieczenstwo)")
        except Exception as e:
            self.log(f"[civ] BK-IN wylaczenie blad (nieblokujace): {e}")

        self._poller_th = threading.Thread(target=self._poller_loop, daemon=True)
        self._poller_th.start()

        # Powiadom webapp.py (przez WS broadcast) ze polaczenie zostalo
        # (po)nawiazane — odbiorca powinien odswiezyc _caps_cache i wyslac
        # nowy /api/rig/features (rig_features) do klientow, bo panel
        # VFO/PWR/sliders moglby byc puste jesli polaczenie nastapilo w
        # tle (np. przez _reconnect_loop po wlaczeniu radia po starcie
        # serwera).
        try:
            self.bcast({"type": "rig_reconnected"})
        except Exception as e:
            self.log(f"[civ] bcast rig_reconnected blad: {e}")

        return True

    def _enable_scope(self, on: bool):
        # TEST DIAGNOSTYCZNY: HAM_NO_SCOPE=1 wymusza scope OFF, zeby sprawdzic
        # czy to strumien scope powoduje blokady petli / skoki RTT / rwanie
        # audio. Jesli z wylaczonym scope audio jest gladkie, a RTT stabilne —
        # winowajca to scope (a na 2 rdzeniach: konkurencja watku readera o
        # procesor). Zdejmij zmienna, by wrocic do normalnej pracy.
        import os as _os
        if _os.environ.get("HAM_NO_SCOPE") == "1":
            on = False
        """
        Wlacz/wylacz wysylanie danych waveform scope przez CI-V.

        Gdy wlaczamy, ustawiamy tryb Center mode (27 14 00 00) — w tym
        trybie radio wysyla center freq + span w naglowku i okno scope
        "podaza" za aktualnym VFO (w przeciwienstwie do Fixed mode, gdzie
        okno to staly zakres band-edge i NIE sledzi VFO). To realizuje
        zachowanie "jak oryginalny ekran scope" zgodnie z oczekiwaniem.

        Ustawiamy rowniez span na 25kHz (27 15 = "0250" wg tabeli SPAN
        str.19-14: 2500 -> 2.5kHz, wartosc 25000 -> indeks "2500"*10... patrz
        komentarz przy SCOPE_SPAN_KHZ) — daje dobry kompromis miedzy
        szerokoscia podgladu i precyzja sledzenia filtra.
        """
        try:
            # Baud 115200 jest WYMAGANY dla scope. Przy 19200 (CI-V USB =
            # "Link to REMOTE") radio nie wysyla waveform - najczestsza
            # przyczyna "brak waterfall".
            if on and self.speed < 115200:
                self.log(f"[civ] UWAGA baud={self.speed} — scope wymaga 115200! "
                         f"Ustaw MENU>SET>Connectors>CI-V baud=115200 i "
                         f"CI-V USB Port=Unlink from REMOTE")
            self._write(bytes([0x27, 0x10, 0x01 if on else 0x00]))  # scope ON/OFF
            time.sleep(0.05)
            self._write(bytes([0x27, 0x11, 0x01 if on else 0x00]))  # wysylanie danych ON/OFF
            if on:
                time.sleep(0.05)
                # Center mode (00) — scope sledzi VFO
                self._write(bytes([0x27, 0x14, 0x00, 0x00]))
                time.sleep(0.05)
                # Span 25kHz: CI-V cmd 27 15, dane = 5 bajtow (10 cyfr BCD)
                self._write(bytes([0x27, 0x15, 0x00, 0x50, 0x02, 0x00, 0x00]))
                self._scope_span_hz = 25000
            self.log(f"[civ] scope output {'ON' if on else 'OFF'} (baud={self.speed})"
                     + (" + Center mode 25kHz" if on else ""))
        except Exception as e:
            self.log(f"[civ] _enable_scope blad: {e}")

    def _write(self, payload: bytes):
        frame = bytes([0xFE, 0xFE, self.addr, CTRL_ADDR]) + payload + bytes([0xFD])
        # UWAGA (perf): usunieto per-write log — polling wywoluje write co ~100ms
        # (poll freq/smeter/scope), kazdy print to blokujacy syscall (~0.5-2ms).
        # Przy zapchaniu logi kumulowaly sie i blokowaly wątek reader/writer.
        # Zeby wlaczyc na debugowanie: odkomentuj ponizsza linie.
        # self.log(f"[civ] TX: {frame.hex()}")
        with self._wlock:
            if self._ser:
                self._ser.write(frame)

    def write_bytes_raw(self, data: bytes):
        """Zapisz surowe bajty do portu CI-V bez wrappera FE FE addr ctrl ... FD.
        Uzywane przez civ_bridge dla TCP klientow ktorzy wysylaja cale ramki
        CI-V (Logger32, HRD itp.). Bajty przekazywane sa dokladnie jak przyszly.
        Chronione self._wlock zeby nie kolidowac z komendami z UI serwera.
        """
        if self.sim or not self._ser:
            return
        with self._wlock:
            try:
                self._ser.write(data)
            except Exception as e:
                self.log(f"[civ] write_bytes_raw blad: {e}")

    def add_bridge_listener(self, callback):
        """Zarejestruj callback wywolywany dla kazdej porcji bajtow z radia.
        Callback: fn(bytes). Uzywane przez civ_bridge do broadcastu do TCP.
        Uwaga: callback wywolywany w watku _reader_loop, musi byc thread-safe."""
        with self._bridge_lock:
            if callback not in self._bridge_listeners:
                self._bridge_listeners.append(callback)

    def remove_bridge_listener(self, callback):
        with self._bridge_lock:
            if callback in self._bridge_listeners:
                self._bridge_listeners.remove(callback)

    def _transact(self, payload: bytes, expect, timeout: float):
        """Wyslij komende i poczekaj na odpowiedz (kod cmd w 'expect' lub ACK 0xFB/0xFA)."""
        if not self._ser:
            return None
        if isinstance(expect, int):
            expect = {expect}
        with self._txlock:
            self._resp_cmd = expect
            self._resp_payload = None
            self._resp_ev.clear()
            try:
                self._write(payload)
            except Exception as e:
                self.log(f"[civ] write blad: {e}")
                self._resp_cmd = None
                return None
            got = self._resp_ev.wait(timeout)
            self._resp_cmd = None
            return self._resp_payload if got else None

    def _reader_loop(self):
        buf = bytearray()
        while self._running:
            try:
                data = self._ser.read(512)
            except Exception as e:
                self.log(f"[civ] reader read blad: {e}")
                break
            if not data:
                continue
            if self._rx_logged < 10:
                self.log(f"[civ] RX: {data.hex()}")
                self._rx_logged += 1
            # Bridge: przekaz surowe bajty do listenerow (TCP klienci civ_bridge).
            # Robimy to PRZED parsowaniem zeby TCP klienci dostali dokladnie
            # to co dostaje nasza logika parsowania (te same eventy, ta sama kolejnosc).
            if self._bridge_listeners:
                with self._bridge_lock:
                    listeners = list(self._bridge_listeners)
                for cb in listeners:
                    try:
                        cb(bytes(data))
                    except Exception as e:
                        self.log(f"[civ] bridge listener blad: {e}")
            buf += data
            # wytnij ramki FE FE ... FD
            while True:
                i = buf.find(b"\xfe\xfe")
                if i < 0:
                    if len(buf) > 4096:
                        del buf[:-2]
                    break
                j = buf.find(b"\xfd", i + 2)
                if j < 0:
                    if i > 0:
                        del buf[:i]
                    break
                frame = bytes(buf[i:j + 1])
                del buf[:j + 1]
                try:
                    self._handle_frame(frame)
                except Exception as e:
                    self.log(f"[civ] handle_frame blad: {e}")

    def _handle_frame(self, frame: bytes):
        if len(frame) < 6:
            return
        to, frm, cmd = frame[2], frame[3], frame[4]
        payload = frame[5:-1]
        # echo wlasnej komendy (do radia) — ignoruj
        if to == self.addr:
            return
        # przyjmuj tylko ramki do kontrolera (0xE0) lub broadcast transceive (0x00)
        if to not in (CTRL_ADDR, 0x00):
            return

        # SCOPE: 0x27 0x00 <dane>
        if cmd == 0x27 and len(payload) >= 1 and payload[0] == 0x00:
            # Licznik ramek scope - uzywany przez logike ponawiania scope
            # po power ON (webapp: _verify_radio_awake_and_start_scope
            # sprawdza czy licznik rosnie = ramki plyna).
            self._scope_rx_count = getattr(self, "_scope_rx_count", 0) + 1
            _hs_t0 = time.monotonic()
            self._handle_scope(payload[1:])
            _hs_ms = (time.monotonic() - _hs_t0) * 1000.0
            # Diagnostyka: ile trwa obrobka JEDNEJ ramki scope. Jesli to setki
            # ms, wina jest w _handle_scope; jesli mikrosekundy — problem lezy
            # w czestotliwosci/konkurencji o rdzen, nie w samej obrobce.
            if _hs_ms > 20:
                print(f"[scopetime] _handle_scope: {_hs_ms:.1f}ms", flush=True)
            return

        # czestotliwosc (odpowiedz 0x03 lub transceive 0x00)
        if cmd in (0x00, 0x03) and len(payload) >= 4:
            f = bcd_to_freq(payload[:5])
            if f and abs(f - self.freq) >= 1:
                # Jesli niedawno (< 0.8s) lokalnie ustawilismy czestotliwosc
                # (np. click-to-tune) i radio zwraca INNA wartosc, to
                # prawdopodobnie "stary" odczyt ktory wystartowal przed
                # zadziallaniem 0x05 — ignoruj, zeby nie nadpisac UI starym
                # freq tuz po kliknieciu.
                grace = (time.time() - self._local_freq_set_at) < 0.8
                if grace and self._local_freq_set_val is not None and f != self._local_freq_set_val:
                    pass  # ignoruj stary odczyt
                else:
                    # Ochrona przed uszkodzonym odczytem BCD z radia (widziano
                    # 2026-07-04 gdy bridge klient wyslal zla ramke - poller
                    # odczytal freq=1 Hz i UI pokazywalo 0.000001 MHz).
                    # IC-7300 min freq = 30kHz, max = 74MHz. Odrzucamy odczyty
                    # ponizej 100kHz jako bledne (bardzo malo kto uzywa LF).
                    # Ochrania to tylko przed drastycznym uszkodzeniem, nie
                    # przed subtelnymi bledami paru Hz.
                    if f < 100_000:
                        self.log(f"[civ] IGNORUJE absurdalny freq={f} Hz (BCD corruption?)")
                    else:
                        self.freq = f
                        self.bcast({"type": "freq", "freq": f})
        # tryb (0x04 odpowiedz / 0x01 transceive) — payload[0]=mode,
        # payload[1]=filter (01/02/03=FIL1/2/3, gdy radio go raportuje)
        #
        # UWAGA KRYTYCZNA: payload[0] to ZAWSZE bazowy tryb CI-V (np. USB=1),
        # NIGDY nie zawiera informacji o DATA mode — to osobna komenda (1A 06),
        # ktora radio wysyla NIEZALEZNIE. Wczesniejsza wersja tego handlera
        # robila self.mode = nm BEZPOSREDNIO z mode_map (ktora zna tylko
        # tryby bazowe), co KASOWALO sufiks "-D" za kazdym razem gdy radio
        # echo'walo/transceive'owalo swoj tryb (co dzieje sie czesto — m.in.
        # przy kazdym set_mode() ktore samo wysylamy, bo IC-7300 odsyla
        # potwierdzenie przez ten sam kanal transceive). Efekt zaobserwowany
        # na zywo: UI pokazywalo USB-D'na ulamek sekundy po klikni eciu, a
        # polling co ~2s (rig_poll w server.py) przywracal je z powrotem do
        # USB. Naprawa: doklej "-D" na podstawie JUZ SLEDZONEGO self.data_mode,
        # dokladnie tak samo jak robi to set_mode przy wysylaniu.
        elif cmd in (0x01, 0x04) and len(payload) >= 1:
            base_nm = self.mode_map.get(payload[0])
            nm = f"{base_nm}-D" if (base_nm and self.data_mode) else base_nm
            if nm and nm != self.mode:
                self.mode = nm
                self.bcast({"type": "mode", "mode": self.mode, "bandwidth": self.bw,
                            "filterNum": self.filter_num})
            if len(payload) >= 2 and payload[1] in (1, 2, 3) and payload[1] != self.filter_num:
                self.filter_num = payload[1]
                self.bcast({"type": "mode", "mode": self.mode, "bandwidth": self.bw,
                            "filterNum": self.filter_num})
        # PTT
        elif cmd == 0x1C and len(payload) >= 2 and payload[0] == 0x00:
            self.ptt = bool(payload[1])

        # przekaz do oczekujacej transakcji — TYLKO gdy kod cmd jest oczekiwany
        # (komendy 'set' jawnie podaja {0xFB,0xFA}, wiec ACK/NAK nie zakonczy
        #  falszywie odczytu freq/mode/S-metr pod mieszanym ruchem CI-V).
        ec = self._resp_cmd
        if ec and cmd in ec:
            self._resp_payload = payload
            self._resp_ev.set()

    def _handle_scope(self, d: bytes):
        if self._scope_logged < 4:
            self.log(f"[scope] surowa ramka ({len(d)}B): {d.hex()}")
            self._scope_logged += 1
        if len(d) < 3:
            return
        seq = d[1]
        total = d[2]

        if seq <= 1:
            # Pierwsza ramka: pelny naglowek wg CI-V Ref. str 19-14
            #   d[3]    = spectrum scope mode (00=Center,01=Fixed,02=SCROLL-C,03=SCROLL-F)
            #   d[4:9]  = center freq (Center mode) LUB lower edge (Fixed/SCROLL)  — 5B BCD
            #   d[9:14] = span (Center mode, 5B "BCD-like" wg tabeli SPAN)
            #             LUB higher edge (Fixed/SCROLL) — 5B BCD
            #   d[14]   = out-of-range (00=in range, 01=out of range)
            #   d[15:]  = dane waveform (tylko gdy total==1 i oor==00, lub
            #             pomijane w 1-ej ramce gdy total>1 — patrz uwaga w doc)
            self._scope_mode_code = d[3] if len(d) > 3 else 0
            self._scope_oor = d[14] if len(d) > 14 else 0

            if self._scope_mode_code == 0x00:
                # Center mode: center freq + span
                try:
                    self._scope_center = bcd_to_freq(d[4:9])
                except Exception:
                    self._scope_center = self.freq
                try:
                    span_raw = bcd_to_freq(d[9:14])
                    self._scope_span_hz = span_raw if span_raw else self._scope_span_hz
                except Exception:
                    pass
                self._scope_lo = self._scope_center - self._scope_span_hz // 2
                self._scope_hi = self._scope_center + self._scope_span_hz // 2
            else:
                # Fixed / SCROLL-C / SCROLL-F: lower + higher edge
                try:
                    self._scope_lo = bcd_to_freq(d[4:9])
                    self._scope_hi = bcd_to_freq(d[9:14])
                    self._scope_center = (self._scope_lo + self._scope_hi) // 2
                    self._scope_span_hz = self._scope_hi - self._scope_lo
                except Exception:
                    pass

            hdr_len = self.scope_header_len
            data = d[hdr_len:] if len(d) > hdr_len else b""
            self._scope_acc = bytearray(data)
            self._scope_total = total
        else:
            self._scope_acc += d[3:]

        if total and seq >= total and self._scope_acc:
            # Rate limit: max 20 klatek/s (50ms interval). IC-7300 wysyla scope
            # ~30-60fps ale klientowi WS nie potrzeba tyle - 20fps to gladki
            # waterfall + o polowe mniejsze obciazenie asyncio/JSON/WebSocket.
            # Uwaga: to jest OPCJONALNE, gdy wielu klientow bardzo pomaga.
            now = time.time()
            if (now - self._scope_last) < 0.050:
                # Za wczesnie - odrzuc tą klatke, poczekaj do nastepnej.
                # NIE resetujemy _scope_acc zeby proces akumulacji trwal.
                # Ale trzeba zeby przy nastepnym packecie zaczal od nowa:
                self._scope_acc = bytearray()
                self._scope_total = 0
                return

            smax = self.scope_max
            # Skalowanie danych scope do 0-255. Bylo: petla Pythona po ~475
            # punktach widma, wywolywana KILKANASCIE razy na sekunde — to ona
            # blokowala petle zdarzen na 100-250ms (potwierdzone: lag rosnie z
            # liczba ramek scope). numpy robi to samo wektorowo, ~100x szybciej.
            _raw = np.frombuffer(bytes(self._scope_acc), dtype=np.uint8)
            arr = np.minimum(255, (_raw.astype(np.uint32) * 255) // max(smax, 1)).astype(np.uint8).tolist()

            # W Center mode (scopeMode==0) uzywamy self.freq (z 0x03 pollera,
            # potwierdzonego jako dokladny) jako centerHz — zamiast wartosci
            # z naglowka scope (_scope_center), ktora w testach wykazywala
            # rozbieznosc wzgledem realnej czestotliwosci radia (do kilku kHz,
            # mozliwy artefakt parsowania pola BCD w tym konkretnym firmware).
            # Lo/Hi przeliczamy wzgledem self.freq i spanu z naglowka, zeby
            # os czestotliwosci i click-to-tune byly zgodne z faktycznym VFO.
            if self._scope_mode_code == 0x00:
                center = self.freq
                lo = center - self._scope_span_hz // 2
                hi = center + self._scope_span_hz // 2
            else:
                center = self._scope_center
                lo, hi = self._scope_lo, self._scope_hi

            # ODSPRZEGNIECIE scope od watku readera.
            #
            # Wczesniej reader wolal broadcast_sync (run_coroutine_threadsafe)
            # przy kazdej ramce — ta synchronizacja watek->petla, robiona
            # kilkanascie razy na sekunde, zderzala sie z petla asyncio i ja
            # blokowala (potwierdzone: lag rosnie liniowo z liczba ramek, a
            # obrobka JEDNEJ ramki jest szybka). Teraz reader TYLKO zapisuje
            # najswiezsza gotowa ramke do zmiennej — zero synchronizacji z
            # petla. Osobna, lekka petla asyncio (_scope_pump w webapp) czyta
            # te zmienna i wysyla w stalym tempie 15 fps. Reader oddaje dane i
            # wraca do czytania portu, nie czekajac na petle.
            self._latest_scope = {
                "type": "scope_frame",
                "data": arr,
                "source": "radio",
                "centerHz": center,
                "spanHz": self._scope_span_hz,
                "loHz": lo,
                "hiHz": hi,
                "mode": self.mode,
                "dataMode": self.data_mode,
                "filterHz": self._filter_width_hz,
                "scopeMode": self._scope_mode_code,
                "outOfRange": bool(self._scope_oor),
            }
            self._scope_acc = bytearray()
            self._scope_last = time.time()
            return

    def _poller_loop(self):
        """Cyklicznie odpytuj freq/mode/S-metr/filter (odpowiedzi aktualizuja stan w readerze)."""
        n = 0
        _off_logged = False
        while self._running and self._ser:
            try:
                # Radio wylaczone (power OFF przez CI-V) nie odpowiada na zadne
                # transakcje. Bez tego kazdy obieg petli konczyl sie bledem
                # odczytu i zasmiecal log ("S-metr transact fail" w kolko).
                if not self.powered:
                    if not _off_logged:
                        self.log("[civ] Radio wylaczone — telemetria wstrzymana")
                        _off_logged = True
                    time.sleep(1.0)
                    continue
                if _off_logged:
                    self.log("[civ] Radio wlaczone — telemetria wznowiona")
                    _off_logged = False
                self._transact(bytes([0x03]), {0x03, 0x00}, 0.3)        # freq
                if n % 4 == 0:
                    self._transact(bytes([0x04]), {0x04, 0x01}, 0.3)    # mode
                # VFO B (nieaktywny VFO): CI-V 25 01 -> odpowiedz [01, 5B freq BCD]
                # (per doc str.19-13: "Selected or unselected VFO frequency
                # settings", 00=selected/aktywny, 01=unselected). Odpytywane
                # co ~1.2s — zmienia sie rzadko (split/A-B swap).
                if n % 4 == 2:
                    bp = self._transact(bytes([0x25, 0x01]), {0x25}, 0.3)
                    if bp and len(bp) >= 6 and bp[0] == 0x01:
                        fb = bcd_to_freq(bp[1:6])
                        if fb and abs(fb - self.freq_b) >= 1:
                            self.freq_b = fb
                            self.bcast({"type": "freqB", "freqB": fb})
                # S-metr: 0x15 0x02 -> odpowiedz 0x15 ...
                p = self._transact(bytes([0x15, 0x02]), {0x15}, 0.3)
                if p and len(p) >= 3 and p[0] == 0x02:
                    raw = bcd2(p[1:3])
                    lvl = smeter_to_sunit(raw)
                    if abs(lvl - self.s_meter) > 0.2:
                        self.s_meter = lvl
                        # Frontend (ws.js) sluchna msg.value - NIE msg.smeter!
                        self.bcast({"type": "smeter", "value": round(lvl, 1)})
                elif getattr(self, '_smeter_fail_logged', 0) < 5:
                    self.log(f"[civ] S-metr transact fail: p={p.hex() if p else None}")
                    self._smeter_fail_logged = getattr(self, '_smeter_fail_logged', 0) + 1

                # TX Metery: ALC (15 13), PWR (15 11), SWR (15 12), VOLT (15 15)
                # Odpytywane tylko gdy PTT aktywne (lub gdy wskaznik wybrany przez usera)
                # BCD 2 bajty -> wartości (punkty kalibracji z oficjalnego
                # IC-7300MK2 CI-V Reference Guide, patrz komentarze przy kazdym
                # mierniku ponizej):
                #   ALC:  0..120 -> 0..100% (liniowo)
                #   PWR:  0=0%, 143=50%, 213=100% (nieliniowo)
                #   SWR:  0=1.0, 48=1.5, 80=2.0, 120=3.0 (nieliniowo)
                #   VOLT: 0=0V, 13=10V, 241=16V (mocno nieliniowo)
                if self.ptt or n % 8 == 0:
                    _dbg_alc = None
                    _dbg_pwr = None
                    # ALC. POPRAWKA: poprzednio uzywano komendy "15 11", ktora
                    # wg oficjalnej dokumentacji CI-V IC-7300 to w rzeczywistosci
                    # "PO" (moc wyjsciowa), NIE ALC — etykiety ALC/PWR w kodzie
                    # byly zamienione miejscami wzgledem faktycznych komend.
                    # Wlasciwa komenda dla ALC to "15 13", z udokumentowanym
                    # zakresem 0000=Min do 0120=Max (NIE 0-241).
                    ap = self._transact(bytes([0x15, 0x13]), {0x15}, 0.25)
                    if ap and len(ap) >= 3 and ap[0] == 0x13:
                        alc_raw = bcd2(ap[1:3])
                        alc_pct = min(100.0, max(0.0, alc_raw / 120 * 100))
                        _dbg_alc = alc_pct
                        self.bcast({"type": "txmeter", "meter": "ALC",
                                    "raw": alc_raw,
                                    "value": round(alc_pct, 1),
                                    "pct": min(1.0, alc_raw / 120)})
                    # PWR output. POPRAWKA: poprzednio uzywano komendy "15 14"
                    # (to w rzeczywistosci COMP — kompresor mowy w dB, NIE moc).
                    # Wlasciwa komenda dla PO (mocy wyjsciowej) to "15 11", z
                    # udokumentowana NIELINIOWA skala: 0000=0%, 0143=50%,
                    # 0213=100% (pelna skala osiagana juz przy raw=213, nie 241)
                    # — interpolacja odcinkowa miedzy tymi punktami.
                    pp = self._transact(bytes([0x15, 0x11]), {0x15}, 0.25)
                    if pp and len(pp) >= 3 and pp[0] == 0x11:
                        pwr_raw = bcd2(pp[1:3])
                        _po_points = [(0, 0.0), (143, 50.0), (213, 100.0)]
                        if pwr_raw <= _po_points[0][0]:
                            pwr_pct = _po_points[0][1]
                        elif pwr_raw >= _po_points[-1][0]:
                            pwr_pct = _po_points[-1][1]
                        else:
                            pwr_pct = _po_points[-1][1]
                            for _i in range(len(_po_points) - 1):
                                _x0, _y0 = _po_points[_i]
                                _x1, _y1 = _po_points[_i + 1]
                                if _x0 <= pwr_raw <= _x1:
                                    _t = (pwr_raw - _x0) / (_x1 - _x0)
                                    pwr_pct = round(_y0 + _t * (_y1 - _y0), 1)
                                    break
                        _dbg_pwr = pwr_pct
                        # DIAGNOSTYKA: podczas TX loguj odczyty co ~1.2s zeby
                        # rozstrzygnac czy mierniki w ogole sa odczytywane
                        # podczas FT8 TX (UI "nie reaguje" - backend czy front?)
                        if self.ptt and n % 4 == 0:
                            self.log(f"[txmeter] TX odczyt: ALC={_dbg_alc if _dbg_alc is not None else '?'}% "
                                     f"PWR={_dbg_pwr}% (bcast poszedl)")
                        self.bcast({"type": "txmeter", "meter": "PWR",
                                    "raw": pwr_raw,
                                    "value": pwr_pct,
                                    "pct": pwr_pct / 100})
                    # SWR
                    sp = self._transact(bytes([0x15, 0x12]), {0x15}, 0.25)
                    if sp and len(sp) >= 3 and sp[0] == 0x12:
                        swr_raw = bcd2(sp[1:3])
                        # IC-7300 SWR: 0=1.0, 48=1.5, 80=2.0, 120=3.0, 241=50
                        # POPRAWKA: poprzedni wzor byl liniowy wzgledem CALEGO
                        # zakresu 0-241, co nie zgadzalo sie z powyzszymi
                        # punktami kalibracyjnymi z dokumentacji IC-7300 (SWR
                        # rosnie szybko do 3.0 juz w polowie zakresu raw=120,
                        # potem znacznie wolniej do 50 przy raw=241) — dawalo
                        # to mocno zawyzone wartosci (np. raw=48 -> 10.76
                        # zamiast udokumentowanego 1.5). Interpolacja
                        # odcinkowa miedzy faktycznymi punktami kalibracyjnymi.
                        _swr_points = [(0, 1.0), (48, 1.5), (80, 2.0), (120, 3.0), (241, 50.0)]
                        swr_val = _swr_points[-1][1]
                        for _i in range(len(_swr_points) - 1):
                            _x0, _y0 = _swr_points[_i]
                            _x1, _y1 = _swr_points[_i + 1]
                            if _x0 <= swr_raw <= _x1:
                                _t = (swr_raw - _x0) / (_x1 - _x0)
                                swr_val = round(_y0 + _t * (_y1 - _y0), 2)
                                break
                        self.bcast({"type": "txmeter", "meter": "SWR",
                                    "raw": swr_raw,
                                    "value": swr_val,
                                    "pct": min(1.0, swr_raw / 120)})  # skala do SWR=3
                    # Napięcie zasilania (Vd — drain voltage / napiecie zasilania
                    # koncowki mocy). Komenda "15 15" (NIE "15 16" — to Id, prad,
                    # inny miernik). Kalibracja zweryfikowana wprost z oficjalnego
                    # IC-7300MK2 CI-V Reference Guide (icomuk.co.uk), tabela
                    # komend, wpis "15 15": 0000=0V, 0013=10V, 0241=16V.
                    # POPRAWKA: poprzednie punkty (0=0V, 151=10V, 211=16V) byly
                    # bledne — skala realnie jest mocno nieliniowa (pierwsze 10V
                    # to tylko 13 jednostek raw, reszta 10-16V rozciaga sie na
                    # pozostale 228 jednostek, bo to zakres w ktorym realnie
                    # pracuje zasilanie 12-13.8V). Bledne punkty dawaly np. dla
                    # realnych 13.8V (raw~157) odczyt ~10.6V — dokladnie taki
                    # zanizony wynik jaki byl zgłaszany na zywo.
                    vp = self._transact(bytes([0x15, 0x15]), {0x15}, 0.25)
                    if vp and len(vp) >= 3 and vp[0] == 0x15:
                        v_raw = bcd2(vp[1:3])
                        _vd_points = [(0, 0.0), (13, 10.0), (241, 16.0)]
                        if v_raw <= _vd_points[0][0]:
                            volt = _vd_points[0][1]
                        elif v_raw >= _vd_points[-1][0]:
                            volt = _vd_points[-1][1]
                        else:
                            volt = _vd_points[-1][1]
                            for _i in range(len(_vd_points) - 1):
                                _x0, _y0 = _vd_points[_i]
                                _x1, _y1 = _vd_points[_i + 1]
                                if _x0 <= v_raw <= _x1:
                                    _t = (v_raw - _x0) / (_x1 - _x0)
                                    volt = round(_y0 + _t * (_y1 - _y0), 2)
                                    break
                        self.bcast({"type": "txmeter", "meter": "VOLT",
                                    "raw": v_raw,
                                    "value": volt,
                                    "pct": volt / 16.0})  # skala do 16V nominal
                # Szerokosc filtra: 1A 03 -> odpowiedz [03, idx_bcd_2B] (00..49)
                # Tryb DATA: 1A 06 -> odpowiedz [06, data_mode(0/1), filter(1-3)]
                #   (per doc p.19-10) — wplywa na interpretacje idx>31 w
                #   _SSB_FILTER_TABLE (zakres SSB-D do 3600Hz tylko w DATA).
                # Odpytywane rzadziej (co ~1.2s) — obie wartosci zmieniaja sie rzadko
                if n % 4 == 1:
                    dp = self._transact(bytes([0x1A, 0x06]), {0x1A}, 0.3)
                    if dp and len(dp) >= 2 and dp[0] == 0x06:
                        new_dm = bool(dp[1])
                        if new_dm != self.data_mode:
                            self.data_mode = new_dm
                            # KRYTYCZNE: zaktualizuj tez self.mode (sufiks -D).
                            # Bez tego radio w USB-D mialo self.mode="USB" ->
                            # UI trzymalo "USB" -> kazde t='mode' z UI (np.
                            # zmiana filtra) robilo set_mode("USB") -> 1A 06 00
                            # -> WYLACZALO data mode w radiu. Objaw: "ciagle
                            # przelacza z USB-D na USB w cyfrowce".
                            base = self.mode[:-2] if self.mode.endswith("-D") else self.mode
                            if base in ("USB", "LSB"):
                                self.mode = f"{base}-D" if new_dm else base
                                self.bcast({"type": "mode", "mode": self.mode,
                                            "bandwidth": self.bw})
                            self._filter_width_hz = filter_width_hz(self.mode, self._filter_idx, self.data_mode)
                            self.bcast({"type": "filter_width", "hz": self._filter_width_hz,
                                        "idx": self._filter_idx, "mode": self.mode,
                                        "dataMode": self.data_mode})

                    fp = self._transact(bytes([0x1A, 0x03]), {0x1A}, 0.3)
                    if fp and len(fp) >= 3 and fp[0] == 0x03:
                        idx = bcd2(fp[1:3])
                        if idx != self._filter_idx:
                            self._filter_idx = idx
                            self._filter_width_hz = filter_width_hz(self.mode, idx, self.data_mode)
                            self.bcast({"type": "filter_width", "hz": self._filter_width_hz,
                                        "idx": idx, "mode": self.mode, "dataMode": self.data_mode})
                # Round-robin odczyt sliderow Set Level (CI-V 14 <sub>) — jeden
                # poziom na cykl (~0.3s), pelny przeglad 14 poziomow ~4.2s.
                # Pozwala UI zauwazyc zmiany dokonane recznie na panelu radia
                # (np. obrot galki AF/RF na froncie IC-7300).
                level_items = list(self.profile.get("levels", {}).items())
                if level_items:
                    name, lvl = level_items[n % len(level_items)]
                    new_val = self._read_level(name, lvl)
                    if new_val is not None:
                        old_val = self.level_values.get(name)
                        if old_val is None or abs(new_val - old_val) > 1e-6:
                            self.level_values[name] = new_val
                            self.bcast({"type": "level_value",
                                        "id": f"level_{name.lower()}", "value": new_val})
                # Preamp (16 02 -> odpowiedz [02, 00|01|02]) i Attenuator
                # (11 -> odpowiedz [00|20]) — odpytywane co ~1.2s (zmieniaja
                # sie rzadko, zwykle recznie na panelu radia).
                if n % 4 == 3:
                    pp = self._transact(bytes([0x16, 0x02]), {0x16}, 0.3)
                    if pp and len(pp) >= 2 and pp[0] == 0x02:
                        new_pre = pp[1]
                        if new_pre in (0, 1, 2) and new_pre != self.preamp:
                            self.preamp = new_pre
                            self.bcast({"type": "preamp", "value": new_pre})

                    ap = self._transact(bytes([0x11]), {0x11}, 0.3)
                    if ap and len(ap) >= 1:
                        new_att = (ap[0] != 0x00)
                        if new_att != self.attenuator:
                            self.attenuator = new_att
                            self.bcast({"type": "attenuator", "value": new_att})

                    # Tuner ON/OFF: 1C 01 -> odpowiedz [01, 00|01]. Nie
                    # odpytujemy stanu "START" (1C 01 02 to jednorazowa
                    # komenda, radio nie raportuje jej jako trwaly stan).
                    tp = self._transact(bytes([0x1C, 0x01]), {0x1C}, 0.3)
                    if tp and len(tp) >= 2 and tp[0] == 0x01:
                        new_tuner = (tp[1] != 0x00)
                        if new_tuner != self.tuner:
                            self.tuner = new_tuner
                            self.bcast({"type": "tuner", "value": new_tuner})
            except Exception as e:
                self.log(f"[civ] poller blad: {e}")
            n += 1
            time.sleep(0.3)

    # ════════════════════════════════════════════════════════════════════════
    # SYMULACJA (brak sprzetu / Replit)
    # ════════════════════════════════════════════════════════════════════════
    def _start_sim(self) -> bool:
        self.sim = True
        self.connected = False
        self.scope_running = True
        if not self.last_msg:
            self.last_msg = "Tryb bezposredni CI-V — SYMULACJA scope (brak sprzetu COM)"
        self._running = True
        if not (self._sim_th and self._sim_th.is_alive()):
            self._sim_th = threading.Thread(target=self._sim_loop, daemon=True)
            self._sim_th.start()
            self.log("[civ] SYMULACJA scope uruchomiona")
        # Watek prob ponownego polaczenia w tle (np. radio bylo wylaczone
        # przy starcie serwera, uzytkownik wlaczy je pozniej fizycznie
        # lub przez przycisk Zasilanie po wczesniejszym wakeup)
        if not (self._reconnect_th and self._reconnect_th.is_alive()):
            self._reconnect_th = threading.Thread(target=self._reconnect_loop, daemon=True)
            self._reconnect_th.start()
        return False

    def _reconnect_loop(self):
        """
        Co RECONNECT_INTERVAL sekund (w trybie symulacji) sprobuj ponownie
        otworzyc port i odpytac 0x03. Jesli radio odpowie — przelacz z
        symulacji na realne polaczenie (analogicznie do _open() sukcesu).
        """
        RECONNECT_INTERVAL = 10.0
        while self._running and self.sim:
            time.sleep(RECONNECT_INTERVAL)
            if not self.sim or not self._running:
                return  # ktos juz polaczyl recznie / zamknieto
            if not HAS_SERIAL:
                continue
            try:
                test_ser = serial.Serial(self.port, self.speed, timeout=0.3, write_timeout=1.0)
            except Exception:
                continue  # port nadal niedostepny — sprobuj pozniej

            try:
                # Wyslij 0x03 (get freq) i sprawdz odpowiedz
                frame = bytes([0xFE, 0xFE, self.addr, CTRL_ADDR, 0x03, 0xFD])
                test_ser.write(frame)
                time.sleep(0.3)
                resp = test_ser.read(64)
                test_ser.close()
            except Exception:
                try: test_ser.close()
                except: pass
                continue

            if not resp or 0xFD not in resp:
                continue  # brak odpowiedzi — radio jeszcze niegotowe

            self.log("[civ] Reconnect: radio odpowiedzialo — przelaczam z symulacji")
            # Zatrzymaj symulacje, otworz realne polaczenie
            self.sim = False
            self._running = False  # zatrzyma _sim_loop
            time.sleep(0.2)
            self._running = True
            self._open()
            return  # _open() albo polaczy realnie, albo wroci do _start_sim
                    # (ktory odpali nowy _reconnect_th jesli znow sim)

    def _sim_loop(self):
        N = 320
        t = 0.0
        # kilka "sygnalow" ktore dryfuja po pasmie
        peaks = [{"pos": random.uniform(0.15, 0.85),
                  "amp": random.uniform(0.5, 1.0),
                  "w":   random.uniform(0.004, 0.02),
                  "drift": random.uniform(-0.0008, 0.0008)} for _ in range(4)]
        # STABILNOSC: try WEWNATRZ petli. Bez tego pojedynczy blad w bcast()
        # (np. hub w zlym stanie przy rozlaczaniu klientow) zabija watek
        # symulacji na stale - waterfall w trybie SIM przestaje dzialac
        # az do restartu serwera.
        _errs = 0
        while self._running and self.sim:
            try:
                arr = []
                noise_base = 18 + 6 * math.sin(t * 0.7)
                for i in range(N):
                    x = i / N
                    v = noise_base + random.uniform(0, 10)
                    for pk in peaks:
                        d = x - pk["pos"]
                        v += pk["amp"] * 230 * math.exp(-(d * d) / (2 * pk["w"]))
                    arr.append(max(0, min(255, int(v))))
                for pk in peaks:
                    pk["pos"] += pk["drift"]
                    if pk["pos"] < 0.1 or pk["pos"] > 0.9:
                        pk["drift"] *= -1
                    pk["amp"] = max(0.25, min(1.0, pk["amp"] + random.uniform(-0.05, 0.05)))
                self.bcast({"type": "scope_frame", "data": arr, "source": "sim",
                            "centerHz": self.freq, "spanHz": self._scope_span_hz,
                            "loHz": self.freq - self._scope_span_hz // 2,
                            "hiHz": self.freq + self._scope_span_hz // 2,
                            "mode": self.mode, "dataMode": self.data_mode,
                            "filterHz": self._filter_width_hz,
                            "scopeMode": 0, "outOfRange": False})
                _errs = 0
            except Exception as e:
                _errs += 1
                if _errs <= 3:
                    print(f"[civ] _sim_loop blad ({_errs}): {e}", flush=True)
                elif _errs == 4:
                    print("[civ] _sim_loop: dalsze bledy pomijane w logu", flush=True)
                time.sleep(0.5)  # backoff zeby nie zalac loga
            t += 0.08
            time.sleep(0.08)
