"""
COM Bridge WS — serwerowy modul dla WebSocket bridge do klientow Windows.

Klient EXE na PC uzytkownika tworzy N wirtualnych COM portow przez com0com,
laczy sie WSS z tym serwerem, autoryzuje sie tokenem Bearer i otrzymuje
mapowanie: COM10 -> CI-V IC-7300, COM11 -> Yaesu FT-991 (przyszlosc), itd.

Nastepnie forwarduje bajty w OBU stronach:
  - Klient wysyla dane z COM10 (od CW Skimmer) -> serwer -> fizyczny port CI-V
  - Radio odpowiada -> serwer -> klient -> pojawiaja sie w COM10 dla CW Skimmer

PROTOKOL WS (JSON messages):

Klient -> Serwer:
  {type: 'hello', ports: ['COM10', 'COM11', ...]}
    Klient informuje serwer ktore fizyczne COM stworzyl. Serwer odpowiada
    mapowaniem 'assignment' - ktory COM do jakiej funkcji.

  {type: 'data', com: 'COM10', hex: 'FEFE94E00300FD'}
    Dane od klienta (z aplikacji przez virtual COM) - przekazac do serwera.

  {type: 'open', com: 'COM10'}
    Klient zglasza ze aplikacja otworzyla ten port (informacyjne).

  {type: 'close', com: 'COM10'}
    Klient zglasza ze aplikacja zamknela port.

  {type: 'ping'}
    Keepalive.

Serwer -> Klient:
  {type: 'assignment', assignments: [
    {com: 'COM10', service: 'civ',        baud: 19200, parity: 'N', bits: 8, stop: 1},
    {com: 'COM11', service: 'yaesu_cat',  baud: 38400, parity: 'N', bits: 8, stop: 2},
    ...
  ]}
    Odpowiedz na 'hello' - konfiguracja portow. Klient przypisuje services do
    swoich COM. Jesli klient stworzyl mniej COM niz assignments, tylko
    pierwsze N dziala. Klient moze dolaczyc ich do drivera com0com.

  {type: 'data', com: 'COM10', hex: 'FEFEE094FBFD'}
    Odpowiedz radia - klient wpisuje do virtual COM10, aplikacja klienta
    (CW Skimmer) odczytuje jak zwykle z portu.

  {type: 'error', code: 'auth', msg: '...'}
    Blad autoryzacji, konfiguracji, itp.

  {type: 'pong'}
    Odpowiedz na ping.

  {type: 'service_status', service: 'civ', online: true}
    Status danej uslugi na serwerze (radio online/offline).

BEZPIECZENSTWO:
  - Autoryzacja Bearer token w naglowku HTTP Upgrade
  - Rate limit: max 1000 bajtow/s per klient/port (bo CI-V 19200 baud = 2400 B/s)
  - Walidacja hex format przed forwardem
  - Klient moze byc odlaczony przy nieudanej auth (403)
"""
import asyncio
import time
import threading as _threading
from typing import Dict, List, Optional


class ComBridgeWs:
    """
    Serwerowy WS bridge dla klientow COM Windows.

    Wspoldziala z:
      - civ.CivRig     (dla service='civ')                  — CI-V IC-7300
      - yaesu.YaesuRig (dla service='yaesu_cat', przyszlosc)
      - kenwood.KenwoodRig (dla service='kenwood_cat', przyszlosc)

    Uzycie w webapp.py:
        self.com_bridge = ComBridgeWs(
            civ_rig=self.rig,      # obiekt z write_bytes_raw + add_bridge_listener
            hub=self.hub,          # do broadcastu ze WSS klienci sie polaczyli
            log=print,
        )
        # W route handler dla /ws/com-bridge:
        await self.com_bridge.handle_client(ws, user)
    """

    # Wewnetrzna nazwa uslugi + walidacja
    SERVICES = {
        'civ': {
            'name':          'Icom CI-V (IC-7300 / IC-9700)',
            'default_baud':  19200,
            'default_parity': 'N',
            'default_bits':  8,
            'default_stop':  1,
            'available':     True,
        },
        'yaesu_cat': {
            'name':          'Yaesu CAT (FT-891/991/DX10)',
            'default_baud':  38400,
            'default_parity': 'N',
            'default_bits':  8,
            'default_stop':  2,  # Yaesu wymaga 2 stop bits!
            'available':     False,  # placeholder - potrzebny fizyczny port na serwerze
        },
        'kenwood_cat': {
            'name':          'Kenwood CAT (TS-590/2000/Elecraft K3)',
            'default_baud':  9600,
            'default_parity': 'N',
            'default_bits':  8,
            'default_stop':  1,
            'available':     False,  # placeholder
        },
    }

    def __init__(self, civ_rig=None, hub=None, log=print, can_write=None):
        self.civ_rig = civ_rig  # CivRig z civ.py
        self.hub     = hub
        self.log     = log
        # can_write(user_id) -> bool : czy user ma prawo PISAC do radia
        # (ma radio_lock albo jest admin). Komendy READ (odczyt freq/mode/
        # S-meter) sa zawsze dozwolone. Komendy WRITE (zmiana freq/mode/PTT)
        # tylko dla trzymajacego lock. Fix 2026-07-05 - bez tego
        # user bez locka mogl kręcic freq przez COM Bridge omijajac blokady!
        self.can_write = can_write or (lambda uid: True)

        # Aktywni klienci: dict[ws] = ClientInfo
        # ClientInfo = {
        #   'user_id':      str,
        #   'peer':         str,  # 'ip:port'
        #   'ports':        [str, ...],       # ich COM zgloszone w hello
        #   'assignments':  [dict, ...],       # co przydzielilismy
        #   'rate_bytes':   int,               # bytes count w oknie 1s
        #   'rate_window':  float,             # czas rozpoczecia okna
        #   'connected_at': float,
        # }
        self._clients: Dict = {}
        self._clients_lock = asyncio.Lock()
        self._main_loop = None  # bedzie ustawione przy pierwszym uzyciu
        # Licznik aktywnych broadcastow (backpressure). Modyfikowany z DWOCH
        # miejsc: watku CI-V (inkrement) i event loopa (dekrement), a 'x += 1'
        # nie jest atomowe w Pythonie - stad lock watkowy.
        self._bc_inflight = 0
        self._bc_lock = _threading.Lock()

        # Rejestrujemy sie jako listener na CI-V bridge (od civ.py)
        # Callback wywolywany dla kazdej porcji bajtow z radia -> forward do
        # wszystkich klientow ktorzy maja przypisany service='civ'.
        if self.civ_rig and hasattr(self.civ_rig, 'add_bridge_listener'):
            self.civ_rig.add_bridge_listener(self._on_civ_data_from_radio)
            self.log("[com_bridge_ws] zarejestrowany jako CI-V listener")
        else:
            self.log(f"[com_bridge_ws] UWAGA: NIE zarejestrowany jako CI-V listener! "
                     f"civ_rig={type(self.civ_rig).__name__ if self.civ_rig else None} "
                     f"has_method={hasattr(self.civ_rig, 'add_bridge_listener') if self.civ_rig else False}")

    def attach_civ_rig(self, new_civ_rig):
        """
        Przepnij listener na nowy CivRig (po reconnect/zmianie modelu radia).

        webapp.py tworzy nowy CivRig przy reconnect - stara referencja jest
        martwa (nie czyta juz radia). Ta metoda odpina listener od starego
        (jesli byl) i podpina do nowego.

        Fix 2026-07-05: bez tego CW Skimmer/HRD nie dostawaly
        odpowiedzi radia po kazdym rig reconnect - listener wisiał na
        martwym obiekcie.
        """
        # Odepnij od starego (jesli byl i ma metode)
        if self.civ_rig and hasattr(self.civ_rig, 'remove_bridge_listener'):
            try:
                self.civ_rig.remove_bridge_listener(self._on_civ_data_from_radio)
            except Exception as e:
                self.log(f"[com_bridge_ws] remove old listener: {e}")

        self.civ_rig = new_civ_rig

        # Podepnij do nowego
        if self.civ_rig and hasattr(self.civ_rig, 'add_bridge_listener'):
            self.civ_rig.add_bridge_listener(self._on_civ_data_from_radio)
            self.log("[com_bridge_ws] przepiety na nowy CivRig - listener aktywny")
        else:
            self.log(f"[com_bridge_ws] attach_civ_rig: nowy rig nie jest CivRig "
                     f"({type(new_civ_rig).__name__ if new_civ_rig else None})")

    # ── Konfiguracja per user ──────────────────────────────────────────────
    def build_assignments(self, user_config: List[dict]) -> List[dict]:
        """
        Zwraca assignments dla klienta bazujac na user config.
        user_config: [{"service": "civ", "baud": 19200}, ...]
        Zwraca:      [{"com": "COM10", "service": "civ", "baud": 19200, ...}, ...]
        (com nadawany dynamicznie zaleznie od portow zgloszonych przez klienta)

        Fix 2026-07-05: kazdy port CI-V dostaje GLOBALNIE UNIKALNY adres
        kontrolera (civ_addr) przez WSZYSTKICH klientow. Bez tego dwoch userow
        (admin + operator) dostawalo te same adresy E1/E2 - odpowiedzi radia sie
        mieszaly miedzy userami. Teraz alokujemy z globalnej puli _civ_addr_pool.
        """
        assignments = []
        for idx, entry in enumerate(user_config or []):
            service = entry.get('service', 'civ')
            if service not in self.SERVICES:
                self.log(f"[com_bridge_ws] nieznany service '{service}', pomijam")
                continue
            defaults = self.SERVICES[service]
            asn = {
                'index':   idx,   # kolejnosc - klient przydziela do COM w tej kolejnosci
                'service': service,
                'name':    defaults['name'],
                'baud':    int(entry.get('baud',   defaults['default_baud'])),
                'parity':  str(entry.get('parity', defaults['default_parity']))[:1],
                'bits':    int(entry.get('bits',   defaults['default_bits'])),
                'stop':    int(entry.get('stop',   defaults['default_stop'])),
            }
            # Przydziel GLOBALNIE unikalny adres CI-V dla kazdego portu civ.
            # Pula E1-EF (15 adresow) wspoldzielona przez wszystkich klientow.
            if service == 'civ':
                asn['civ_addr'] = self._alloc_civ_addr()
            assignments.append(asn)
        return assignments

    def _alloc_civ_addr(self) -> int:
        """Przydziel wolny adres CI-V z globalnej puli E1-EF."""
        if not hasattr(self, '_civ_addr_used'):
            self._civ_addr_used = set()
        for addr in range(0xE1, 0xF0):  # E1..EF
            if addr not in self._civ_addr_used:
                self._civ_addr_used.add(addr)
                return addr
        # Wszystkie zajete (>15 portow lacznie) - recykling od E1
        # (rzadki przypadek, moze powodowac kolizje ale lepsze niz crash)
        self.log("[com_bridge_ws] UWAGA: pula adresow CI-V wyczerpana (>15 portow)")
        return 0xE1

    def _free_civ_addrs(self, assignments: list):
        """Zwolnij adresy CI-V uzyte przez assignments (przy rozlaczeniu klienta)."""
        if not hasattr(self, '_civ_addr_used'):
            return
        for asn in assignments:
            addr = asn.get('civ_addr')
            if addr is not None:
                self._civ_addr_used.discard(addr)

    # ── Klient handler ─────────────────────────────────────────────────────
    async def handle_client(self, ws, user: dict, user_config: List[dict]):
        """
        Glowny loop obslugi klienta. Wywolywany z webapp.py po autoryzacji
        (JWT sprawdzony) w route handler dla /ws/com-bridge.

        user:        {"user_id": "...", "callsign": "...", "role": "..."}
        user_config: lista portow tego usera z config (z /api/com/config)
        """
        # Zapisz referencje do main event loop (dla _on_civ_data_from_radio ktore
        # jest wywolywane w innym watku i nie ma dostepu do loopa).
        if self._main_loop is None:
            self._main_loop = asyncio.get_running_loop()
            self.log(f"[com_bridge_ws] event loop zachowany dla callback z watku")

        peer = self._peer_str(ws)
        info = {
            'user_id':      user.get('user_id', ''),
            'peer':         peer,
            'ports':        [],
            'assignments':  [],
            'rate_bytes':   0,
            'rate_window':  time.monotonic(),
            'connected_at': time.monotonic(),
        }
        async with self._clients_lock:
            self._clients[ws] = info
        self.log(f"[com_bridge_ws] klient {peer} ({info['user_id']}) polaczony "
                 f"(razem: {len(self._clients)})")

        try:
            async for msg in ws:
                # Loguj pierwsze 10 wiadomosci - zobaczymy TYP wiadomosci
                if info.get('msg_count', 0) < 10:
                    self.log(f"[com_bridge_ws] {peer}: WS msg #{info.get('msg_count', 0)+1} "
                             f"type={msg.type.name} data_len={len(str(msg.data)) if msg.data else 0}")
                    info['msg_count'] = info.get('msg_count', 0) + 1
                # aiohttp WSMessage - odczytujemy tylko TEXT (JSON)
                if msg.type.name != 'TEXT':
                    continue
                try:
                    data = self._parse_json(msg.data)
                except Exception as _pe:
                    if info.get('parse_err_logged', 0) < 3:
                        self.log(f"[com_bridge_ws] {peer}: JSON parse err: {_pe} "
                                 f"raw={str(msg.data)[:80]}")
                        info['parse_err_logged'] = info.get('parse_err_logged', 0) + 1
                    continue
                if not isinstance(data, dict):
                    continue
                t = data.get('type')

                if t == 'hello':
                    ports = data.get('ports', [])
                    if not isinstance(ports, list):
                        await ws.send_json({'type': 'error', 'code': 'bad_hello',
                                             'msg': 'ports musi byc lista'})
                        continue
                    info['ports'] = [str(p)[:20] for p in ports[:16]]  # limit 16 portow
                    info['assignments'] = self.build_assignments(user_config)
                    # Przypisujemy COM do assignments zgodnie z kolejnoscia
                    for i, asn in enumerate(info['assignments']):
                        if i < len(info['ports']):
                            asn['com'] = info['ports'][i]
                        else:
                            asn['com'] = None  # brak COM - klient zignoruje
                    self.log(f"[com_bridge_ws] {peer}: hello - {len(info['ports'])} portow, "
                             f"{len(info['assignments'])} assignments")
                    await ws.send_json({'type': 'assignment',
                                         'assignments': info['assignments']})
                    # Wyslij status wszystkich services (klient wie co jest online)
                    await self._send_service_status(ws)

                elif t == 'data':
                    com   = data.get('com', '')
                    hex_s = data.get('hex', '')
                    # Loguj tylko pierwsze kilka razy per klient (nie spamuj)
                    if info.get('data_logged', 0) < 5:
                        self.log(f"[com_bridge_ws] {peer}: data od klienta com={com} "
                                 f"hex={hex_s[:40]}{'...' if len(hex_s)>40 else ''}")
                        info['data_logged'] = info.get('data_logged', 0) + 1
                    await self._on_client_data(ws, info, com, hex_s)

                elif t == 'open':
                    com = data.get('com', '')
                    self.log(f"[com_bridge_ws] {peer}: aplikacja otworzyla {com}")

                elif t == 'close':
                    com = data.get('com', '')
                    self.log(f"[com_bridge_ws] {peer}: aplikacja zamknela {com}")

                elif t == 'ping':
                    await ws.send_json({'type': 'pong'})

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.log(f"[com_bridge_ws] {peer}: blad loop - {e}")
        finally:
            async with self._clients_lock:
                self._clients.pop(ws, None)
            # Zwolnij adresy CI-V uzyte przez tego klienta (do puli)
            self._free_civ_addrs(info.get('assignments', []))
            self.log(f"[com_bridge_ws] klient {peer} rozlaczony "
                     f"(razem: {len(self._clients)})")

    def _rewrite_ctrl_addr(self, data: bytes, from_addr: int, to_addr: int) -> bytes:
        """
        Przepisz adres kontrolera (pole 'from' w ramce CI-V) z from_addr na
        to_addr. Obsluguje strumien z wieloma sklejonymi ramkami.

        Ramka CI-V: FE FE <to> <from> <cmd> ... FD
        Pole 'from' to pozycja 3 (0-indexed) w kazdej ramce.
        """
        if from_addr not in data:
            return data  # szybka sciezka - nic do zmiany
        out = bytearray(data)
        i = 0
        n = len(out)
        while i < n - 3:
            # Szukaj poczatku ramki FE FE
            if out[i] == 0xFE and out[i+1] == 0xFE:
                # pozycja 3 = from addr
                if out[i+3] == from_addr:
                    out[i+3] = to_addr
                # Przeskocz do konca tej ramki (FD)
                j = out.find(b'\xFD', i+2)
                if j < 0:
                    break
                i = j + 1
            else:
                i += 1
        return bytes(out)

    # Komendy CI-V ktore ZMIENIAJA stan radia (WRITE). Blokowane dla userow
    # bez radio_lock. Wszystkie inne (read freq 0x03, read mode 0x04, read
    # S-meter 0x15, read VFO 0x25 z sub=read, itd.) sa dozwolone.
    # Ref: IC-7300 CI-V reference manual.
    _CIV_WRITE_CMDS = {
        0x00,  # set freq (transceive)
        0x01,  # set mode (transceive)
        0x02,  # (nieuzywane set)
        0x05,  # set freq
        0x06,  # set mode
        0x07,  # set VFO / VFO operations
        0x08,  # set memory channel
        0x09,  # memory write
        0x0A,  # memory->VFO
        0x0B,  # memory clear
        0x0F,  # set split/duplex
        0x10,  # set tuning step
        0x16,  # set various (AGC, preamp, atten, etc)
        0x1A,  # set various (data mode, etc) - UWAGA: 1A 05 to tez read/write
        0x1C,  # set PTT / ATU (0x1C 0x00=PTT, 0x1C 0x01=ATU)
    }
    # Komendy 0x25 (VFO freq) i 0x26 (VFO mode): sub-command 0x00=read,
    # 0x01=set. Sprawdzamy sub-command osobno.
    # Komenda 0x1A: 0x1A 0x05 <nn> read/write ustawien - blokujemy write
    # (gdy ma payload poza samym numerem ustawienia).

    def _strip_write_commands(self, data: bytes) -> bytes:
        """
        Zwroc tylko ramki READ z strumienia. Ramki WRITE (zmiana stanu radia)
        sa usuniete. Uzywane gdy user nie ma radio_lock - moze tylko czytac.

        Ramka: FE FE <to> <from> <cmd> [sub] ... FD
        """
        out = bytearray()
        i = 0
        n = len(data)
        while i < n - 3:
            if data[i] == 0xFE and data[i+1] == 0xFE:
                j = data.find(b'\xFD', i+2)
                if j < 0:
                    break
                frame = data[i:j+1]
                if len(frame) >= 6:
                    cmd = frame[4]
                    sub = frame[5] if len(frame) >= 7 else None
                    is_write = self._is_write_command(cmd, sub, len(frame))
                    if not is_write:
                        out += frame
                    else:
                        if getattr(self, '_blocked_write_logged', 0) < 10:
                            self.log(f"[com_bridge_ws] BLOKADA write (brak locka): "
                                     f"cmd={cmd:02X} sub={sub if sub is None else f'{sub:02X}'}")
                            self._blocked_write_logged = getattr(self, '_blocked_write_logged', 0) + 1
                else:
                    out += frame  # za krotka - przepusc (nie zaszkodzi)
                i = j + 1
            else:
                i += 1
        return bytes(out)

    def _is_write_command(self, cmd: int, sub, frame_len: int) -> bool:
        """Czy komenda CI-V zmienia stan radia (WRITE)?

        Fix 2026-07-05: zaostrzone. HRD stroi komenda 0x25/0x01 (set VFO freq)
        i 0x00/0x05 (set freq). Blokujemy je ZAWSZE gdy sub wskazuje na set,
        niezaleznie od dlugosci ramki (HRD moze wysylac rozne formaty).
        """
        # 0x00 (set freq transceive), 0x05 (set freq), 0x01/0x06 (set mode):
        # ZAWSZE write. Read freq/mode to osobne komendy (0x03/0x04).
        if cmd in (0x00, 0x01, 0x05, 0x06):
            return True
        # 0x25 (VFO freq), 0x26 (VFO mode): KLUCZOWE - oba sub 0x00 i 0x01
        # moga byc SET jesli maja dane freq/mode!
        #   25 00 (7B)          = READ VFO A freq
        #   25 00 <5B freq> (12B) = SET VFO A freq  <- LUKA! HRD tak stroi
        #   25 01 (7B)          = READ VFO B freq (ale tez HRD SET krotki?)
        #   25 01 <5B freq> (12B) = SET VFO B freq
        # Zasada: jesli ramka DLUZSZA niz sam cmd+sub (flen>7) = SET (write).
        # Krotka (flen==7, tylko cmd+sub) = READ.
        # WYJATEK: HRD czasem wysyla 25/01 krotkie jako poll - ale zeby byc
        # bezpiecznym blokujemy KAZDE 25/01 (sub=01) i 26/01, oraz 25/00 i
        # 26/00 gdy maja dane (flen>7).
        if cmd in (0x25, 0x26):
            if sub == 0x01:
                return True  # sub=01 zawsze blokuj (SET VFO B / poll strojenia)
            if sub == 0x00:
                return frame_len > 7  # 25/00 z danymi = SET VFO A freq!
            return frame_len > 7  # inne sub z danymi = write
        # 0x07 (VFO select/ops), 0x08-0x0B (memory), 0x0F (split), 0x10 (step):
        # write
        if cmd in (0x07, 0x08, 0x09, 0x0A, 0x0B, 0x0F, 0x10):
            return True
        # 0x1A: read/write ustawien radia. Format 1A 05 <2B numer> [wartosc].
        # DECYZJA (2026-07-05): przepuszczamy 1A 05 (parametry konfiguracyjne)
        # nawet dla usera bez locka. Te komendy USTAWIAJA parametry interfejsu
        # (np. 0075, 0071, 0053) ktore CW Skimmer/HRD konfiguruja przy starcie -
        # bez nich aplikacja sie NIE zainicjalizuje i nie pokaze freq.
        # NIE zmieniaja czestotliwosci ani nie powoduja nadawania, wiec sa
        # bezpieczne. Krytyczne write (freq/mode/PTT/split) sa dalej blokowane.
        if cmd == 0x1A:
            return False  # przepusc konfiguracje (nie zmienia freq/TX)
        # 0x1C: PTT/ATU. Read = 7B (1C 00), set = >7B
        if cmd == 0x1C:
            return frame_len > 7
        # 0x16: przelaczniki radia (AGC, preamp, atten, NB, NR, filtry).
        # DECYZJA (2026-07-05): przepuszczamy - CW Skimmer/HRD ustawiaja te
        # przelaczniki przy starcie (16/22, 16/40-46). Bez nich aplikacja moze
        # sie nie zainicjalizowac. NIE zmieniaja czestotliwosci ani nie
        # powoduja nadawania. User bez locka moze wiec przelaczyc np. preamp,
        # ale nie zestroi radia ani nie zacznie nadawac - akceptowalny
        # kompromis dla dzialajacego podgladu w aplikacjach CAT.
        if cmd == 0x16:
            return False  # przepusc przelaczniki (nie zmienia freq/TX)
        # 0x14: poziomy (AF/RF/squelch). Read = 7B (14 nn), set = >7B (z wartoscia)
        if cmd == 0x14:
            return frame_len > 7
        # READ komendy - zawsze dozwolone:
        # 0x03 read freq, 0x04 read mode, 0x15 read metery, 0x19 read id,
        # 0x1A 0x05 read (obsluzone wyzej), 0x21 read RIT/dane, 0x27 scope
        if cmd in (0x03, 0x04, 0x15, 0x19, 0x21, 0x27):
            return False
        # Reszta z listy WRITE (fallback)
        return cmd in self._CIV_WRITE_CMDS

    def _split_read_write(self, data: bytes):
        """
        Podziel strumien na (read_frames, write_frames).
        read_frames - bytes do wyslania do radia (dozwolone).
        write_frames - lista bytes (kazda ramka write osobno) do echa.
        """
        read_out = bytearray()
        write_frames = []
        i = 0
        n = len(data)
        while i < n - 3:
            if data[i] == 0xFE and data[i+1] == 0xFE:
                j = data.find(b'\xFD', i+2)
                if j < 0:
                    break
                frame = data[i:j+1]
                if len(frame) >= 6:
                    cmd = frame[4]
                    sub = frame[5] if len(frame) >= 7 else None
                    if self._is_write_command(cmd, sub, len(frame)):
                        write_frames.append(bytes(frame))
                    else:
                        read_out += frame
                else:
                    read_out += frame
                i = j + 1
            else:
                i += 1
        return bytes(read_out), write_frames

    async def _send_fake_echo(self, ws, assignment, write_frames, civ_addr):
        """
        Odeslij echo blokowanych komend write do klienta (bez wysylania do
        radia). Aplikacja (CW Skimmer/HRD) dostaje echo swojej komendy i moze
        kontynuowac inicjalizacje. Radio pozostaje niezmienione.

        Dla komend SET radio normalnie odpowiada echem + ACK (FB). Odsylamy
        echo (komenda) + ACK zeby aplikacja byla zadowolona.
        Adres przepisany civ_addr->E0 bo aplikacja wyslala jako E0.
        """
        com = assignment.get('com')
        if not com:
            return
        out = bytearray()
        for fr in write_frames:
            # Echo: komenda z powrotem, adres civ_addr->E0
            echo = self._rewrite_both_addr(fr, from_addr=civ_addr, to_addr=0xE0)
            out += echo
            # ACK (radio potwierdza SET): FE FE E0 94 FB FD
            # to=E0 (kontroler), from=94 (radio), FB=OK
            out += bytes([0xFE, 0xFE, 0xE0, 0x94, 0xFB, 0xFD])
        if out:
            hex_s = out.hex().upper()
            if getattr(self, '_fake_echo_logged', 0) < 15:
                self.log(f"[com_bridge_ws] fake-echo -> {com} "
                         f"(blokada write, radio nietkniete): {hex_s}")
                self._fake_echo_logged = getattr(self, '_fake_echo_logged', 0) + 1
            try:
                await ws.send_json({'type': 'data', 'com': com, 'hex': hex_s})
            except Exception:
                pass

    async def _on_client_data(self, ws, info: dict, com: str, hex_s: str):
        """Dane od klienta - forward do fizycznego radia bazujac na assignment."""
        # Znajdz service przypisany do tego COM
        assignment = None
        for asn in info['assignments']:
            if asn.get('com') == com:
                assignment = asn
                break
        if not assignment:
            return  # nieprzypisany COM - cicho ignoruj

        # Waliduj hex
        try:
            raw = bytes.fromhex(hex_s.replace(' ', ''))
        except ValueError:
            self.log(f"[com_bridge_ws] {info['peer']}: bad hex - {hex_s[:20]}")
            return
        if len(raw) == 0 or len(raw) > 512:
            return  # ochrona przed za duzymi ramkami

        # ── ADDRESS REWRITE (fix 2026-07-05 operator) ────────────────────────
        # Problem: server ma wlasny CI-V poller (adres kontrolera 0xE0).
        # CW Skimmer/HRD tez uzywaja 0xE0. Gdy oba pytaja radio jednoczesnie,
        # odpowiedzi sie mieszaja - server bierze odpowiedz przeznaczona dla
        # klienta (i odwrotnie), co psuje S-metr/freq po obu stronach.
        #
        # Rozwiazanie: kazdy port CI-V ma UNIKALNY adres kontrolera z assignment
        # (civ_addr: E1, E2, E3...). Przepisujemy adres kontrolera aplikacji
        # 0xE0 -> civ_addr w ramkach IDACYCH DO radia. Radio odpowiada na
        # civ_addr. Server ignoruje (akceptuje tylko 0xE0/0x00). Rozne aplikacje
        # (CW Skimmer=E1, HRD=E2) tez sie nie mieszaja miedzy soba.
        civ_addr = assignment.get('civ_addr', 0xE1)
        raw = self._rewrite_ctrl_addr(raw, from_addr=0xE0, to_addr=civ_addr)

        # ── ZABEZPIECZENIE RADIO_LOCK (fix 2026-07-05 operator) ──────────────
        # User bez radio_lock moze tylko CZYTAC (freq/mode/S-meter). Komendy
        # zmieniajace stan radia (set freq, set mode, PTT) sa BLOKOWANE - bez
        # tego user bez locka mogl kręcic freq przez COM Bridge omijajac
        # blokady UI!
        #
        # Rozpoznajemy komendy WRITE po kodzie CI-V (pozycja 4 w ramce, po
        # FE FE to from). Filtrujemy strumien - przepuszczamy tylko READ,
        # blokujemy WRITE jesli user nie ma prawa.
        uid = info.get('user_id', '')
        _can = self.can_write(uid)
        # Diagnostyka - loguj KAZDA komende WRITE (zeby zlapac czym user stroi).
        # Bez limitu dla WRITE - chcemy widziec kazda probe zmiany.
        _i = 0
        while _i < len(raw) - 3:
            if raw[_i] == 0xFE and raw[_i+1] == 0xFE:
                _j = raw.find(b'\xFD', _i+2)
                if _j < 0:
                    break
                _fr = raw[_i:_j+1]
                if len(_fr) >= 5:
                    _cmd = _fr[4]
                    _sub = _fr[5] if len(_fr) >= 7 else None
                    _w = self._is_write_command(_cmd, _sub, len(_fr))
                    if _w:
                        # KAZDA proba write - loguj z pelnym hex i can_write
                        self.log(f"[com_bridge_ws] {info['peer']} WRITE-PROBA: "
                                 f"cmd={_cmd:02X}"
                                 f"{'/'+format(_sub,'02X') if _sub is not None else ''}"
                                 f"[{len(_fr)}B] can_write={_can} "
                                 f"hex={_fr.hex().upper()}")
                _i = _j + 1
            else:
                _i += 1
        if not _can:
            # Podziel na write (blokowane) i read (przepuszczane).
            # WAZNE: aplikacje (CW Skimmer/HRD) czekaja na ECHO kazdej komendy
            # przed nastepna. Jesli po prostu usuniemy write, aplikacja zawiesza
            # sie czekajac na echo -> nie inicjalizuje sie -> pokazuje 0.00.
            # Rozwiazanie: dla blokowanych write GENERUJEMY echo (odsylamy
            # komende z powrotem do klienta jakby radio potwierdzilo) ale NIE
            # wysylamy do radia. Aplikacja mysli ze przeszlo, radio niezmienione.
            read_part, blocked_writes = self._split_read_write(raw)
            # Odeslij echo blokowanych komend do klienta (na jego COM)
            if blocked_writes:
                await self._send_fake_echo(ws, assignment, blocked_writes, civ_addr)
            raw = read_part
            if not raw:
                return  # tylko write bylo - echo wyslane, nic do radia

        # Rate limit (per klient globalnie): zwiekszony do 10000 B/s
        # CI-V normalnie ~2400 B/s @ 19200 baud, ale w CW Skimmer/HRD polling
        # + odpowiedzi moga dawac krotkie peaki. 1000 B/s bylo za malo.
        now = time.monotonic()
        if now - info['rate_window'] >= 1.0:
            if info.get('rate_hit', 0) > 0:
                self.log(f"[com_bridge_ws] {info['peer']}: rate hits w ostatniej sekundzie: "
                         f"{info.get('rate_hit', 0)}")
            info['rate_window'] = now
            info['rate_bytes'] = 0
            info['rate_hit'] = 0
        info['rate_bytes'] += len(raw)
        if info['rate_bytes'] > 10000:
            info['rate_hit'] = info.get('rate_hit', 0) + 1
            return

        # Dispatch wg service
        service = assignment.get('service')
        if service == 'civ' and self.civ_rig:
            try:
                # write_bytes_raw uzywa threading lock - odpal w wątku executora
                await asyncio.to_thread(self.civ_rig.write_bytes_raw, raw)
            except Exception as e:
                self.log(f"[com_bridge_ws] blad write civ: {e}")
        elif service in ('yaesu_cat', 'kenwood_cat'):
            # PLACEHOLDER - wymaga fizycznego portu na serwerze
            # Na przyszlosc: self.yaesu_rig.write_bytes_raw(raw)
            pass
        else:
            self.log(f"[com_bridge_ws] nieobslugiwany service: {service}")

    # ── Dane z radia -> klienci ────────────────────────────────────────────
    def _on_civ_data_from_radio(self, data: bytes):
        """
        Callback od CivRig - kazda porcja bajtow z fizycznego radia CI-V.
        Wywolywany W WATKU CivRig (nie w event loop). Musimy zaplanowac
        broadcast w event loop.

        Fix 2026-07-05: cache reference do main event loop bo
        asyncio.get_event_loop() w watku nie-glownym rzuca RuntimeError
        w Python 3.12. Zapisujemy referencje przy pierwszym wywolaniu
        z coroutine (gdzie mamy running loop).
        """
        if not self._clients:
            return
        loop = self._main_loop
        if loop is None:
            # Nie mamy jeszcze zapisanego loopa - sprobuj
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                # Brak loopa w tym watku - niestety musimy pominac
                if not hasattr(self, '_no_loop_logged'):
                    self.log(f"[com_bridge_ws] UWAGA: _on_civ_data_from_radio "
                             f"nie ma event loop - dane z radia GUBIONE")
                    self._no_loop_logged = True
                return

        # BUFOROWANIE RAMEK (fix 2026-07-05): reader civ czyta w porcjach ktore
        # NIE sa wyrownane do ramek CI-V. Ramka moze byc podzielona miedzy
        # porcje - szczegolnie gdy CW Skimmer + HRD oba polluja (gesty strumien).
        # Bez buforowania _filter_frames_for_client gubi podzielone ramki ->
        # freq miga w aplikacji. Skladamy kompletne ramki FEFE...FD tutaj.
        if not hasattr(self, '_rx_buf'):
            self._rx_buf = bytearray()
        self._rx_buf += data
        # Wytnij wszystkie KOMPLETNE ramki, zostaw ogon (niekompletna ramke)
        complete = bytearray()
        buf = self._rx_buf
        while True:
            i = buf.find(b'\xFE\xFE')
            if i < 0:
                # brak poczatku ramki - wyczysc smieci (zostaw ostatni bajt
                # na wypadek gdyby byl pierwszym FE nowej ramki)
                if len(buf) > 1:
                    del buf[:-1]
                break
            if i > 0:
                del buf[:i]  # usun smieci przed ramka
            j = buf.find(b'\xFD', 2)
            if j < 0:
                # niekompletna ramka - czekaj na wiecej danych
                # ochrona przed nieskonczonym rostem bufora
                if len(buf) > 512:
                    del buf[:256]
                break
            complete += buf[:j+1]
            del buf[:j+1]

        if not complete:
            return  # nic kompletnego jeszcze

        data = bytes(complete)

        # Loguj pierwsze kilka razy
        if not hasattr(self, '_radio_cb_logged'):
            self._radio_cb_logged = 0
        if self._radio_cb_logged < 5:
            self.log(f"[com_bridge_ws] _on_civ_data_from_radio wywolany "
                     f"({len(data)}B kompletnych ramek), klientow={len(self._clients)}")
            self._radio_cb_logged += 1
        try:
            # STABILNOSC: backpressure. Jesli broadcast nie nadaza za strumieniem
            # CI-V (wolny klient / gesty poll), taski by sie pietrzyly zjadajac
            # pamiec. Ograniczamy liczbe rownoczesnych broadcastow - nadmiarowe
            # ramki porzucamy (dane CI-V sa ulotne, swieze wazniejsze od starych).
            with self._bc_lock:
                if self._bc_inflight >= 32:
                    if not hasattr(self, "_bc_drop_logged"):
                        self._bc_drop_logged = 0
                    if self._bc_drop_logged < 3:
                        self.log("[com_bridge_ws] UWAGA: broadcast nie nadaza, "
                                 "porzucam ramke (wolny klient?)")
                        self._bc_drop_logged += 1
                    return
                # Inkrement TU (nie w tasku) - miedzy create_task a startem taska
                # licznik musi juz odzwierciedlac zaplanowana wysylke.
                self._bc_inflight += 1
            loop.call_soon_threadsafe(
                lambda: asyncio.create_task(self._broadcast_civ_guarded(data))
            )
        except RuntimeError as e:
            with self._bc_lock:
                self._bc_inflight = max(0, self._bc_inflight - 1)
            if not hasattr(self, '_cb_err_logged'):
                self.log(f"[com_bridge_ws] call_soon_threadsafe err: {e}")
                self._cb_err_logged = True

    async def _broadcast_civ_guarded(self, data: bytes):
        """Wrapper zwalniajacy licznik backpressure (inkrement w callerze)."""
        try:
            await self._broadcast_civ(data)
        finally:
            with self._bc_lock:
                self._bc_inflight = max(0, self._bc_inflight - 1)

    async def _broadcast_civ(self, data: bytes):
        """Broadcast danych CI-V do klientow - KAZDY dostaje tylko swoje ramki.

        Fix 2026-07-05: kazdy port CI-V ma unikalny adres (civ_addr E1/E2/...).
        Dla kazdego klienta/portu filtrujemy ramki po JEGO civ_addr, przepisujemy
        z powrotem na E0 (bo aplikacja wysylala jako E0) i wysylamy. Dzieki temu
        CW Skimmer (E1) i HRD (E2) dostaja tylko swoje odpowiedzi - nie mieszaja
        sie ani ze soba ani z pollerem servera (E0).
        """
        async with self._clients_lock:
            clients_snapshot = list(self._clients.items())

        # STABILNOSC/WYDAJNOSC: zbierz wszystkie wysylki i wykonaj ROWNOLEGLE.
        # Wczesniej byly sekwencyjne (await w petli) - jeden wolny klient
        # (slaba sieć, tunel) blokowal wysylke do pozostalych, a nowe taski
        # broadcastu sie kumulowaly przy gestym strumieniu CI-V.
        sends = []
        for ws, info in clients_snapshot:
            for asn in info['assignments']:
                if asn.get('service') != 'civ' or not asn.get('com'):
                    continue
                civ_addr = asn.get('civ_addr', 0xE1)
                # Filtruj ramki dla tego konkretnego portu (jego civ_addr)
                filtered = self._filter_frames_for_client(data, client_addr=civ_addr)
                if not filtered:
                    continue
                # Przepisz civ_addr -> E0 (aplikacja wysylala jako E0)
                rewritten = self._rewrite_both_addr(filtered, from_addr=civ_addr, to_addr=0xE0)
                hex_s = rewritten.hex().upper()
                # Loguj broadcast (diagnostyka CW Skimmer 0.00)
                if not hasattr(self, '_broadcast_logged'):
                    self._broadcast_logged = 0
                if self._broadcast_logged < 60:
                    self.log(f"[com_bridge_ws] →{asn['com']} "
                             f"(E{civ_addr:02X}): {hex_s}")
                    self._broadcast_logged += 1
                sends.append(ws.send_json({'type': 'data',
                                            'com': asn['com'],
                                            'hex': hex_s}))

        if sends:
            # return_exceptions=True: rozlaczony klient nie przerywa wysylki
            # do pozostalych (jest i tak usuwany w handle_client/finally)
            await asyncio.gather(*sends, return_exceptions=True)

    def _filter_frames_for_client(self, data: bytes, client_addr: int) -> bytes:
        """
        Zwroc ramki CI-V ktore sa istotne dla klienta:
        - to=client_addr (0xE1): odpowiedzi radia na zapytania klienta
        - to=0x00: broadcast transceive
        - from=client_addr (0xE1): ECHO wlasnych komend klienta

        Echo jest wazne! Wiele implementacji CI-V (w tym CW Skimmer) czeka na
        echo wlasnej komendy PRZED odczytaniem odpowiedzi. Bez echa klient
        moze uznac ze komunikacja nie dziala (fix 2026-07-05 operator).

        Ramka: FE FE <to> <from> ... FD, to=pozycja 2, from=pozycja 3.
        """
        out = bytearray()
        i = 0
        n = len(data)
        while i < n - 3:
            if data[i] == 0xFE and data[i+1] == 0xFE:
                j = data.find(b'\xFD', i+2)
                if j < 0:
                    break
                to_addr   = data[i+2]
                from_addr = data[i+3]
                # Przepusc: odpowiedzi do klienta, broadcast, echo od klienta
                if to_addr == client_addr or to_addr == 0x00 or from_addr == client_addr:
                    out += data[i:j+1]
                i = j + 1
            else:
                i += 1
        return bytes(out)

    def _rewrite_to_addr(self, data: bytes, from_addr: int, to_addr: int) -> bytes:
        """Przepisz pole 'to' (pozycja 2) w kazdej ramce z from_addr na to_addr."""
        if from_addr not in data:
            return data
        out = bytearray(data)
        i = 0
        n = len(out)
        while i < n - 3:
            if out[i] == 0xFE and out[i+1] == 0xFE:
                if out[i+2] == from_addr:
                    out[i+2] = to_addr
                j = out.find(b'\xFD', i+2)
                if j < 0:
                    break
                i = j + 1
            else:
                i += 1
        return bytes(out)

    def _rewrite_both_addr(self, data: bytes, from_addr: int, to_addr: int) -> bytes:
        """
        Przepisz OBA pola adresowe ('to' poz.2 i 'from' poz.3) z from_addr na
        to_addr w kazdej ramce.

        Uzywane przy broadcast do klienta:
        - odpowiedzi radia: to=E1 -> to=E0 (klient widzi ze odpowiedz do niego)
        - echo komend:      from=E1 -> from=E0 (klient rozpoznaje swoje echo)
        """
        if from_addr not in data:
            return data
        out = bytearray(data)
        i = 0
        n = len(out)
        while i < n - 3:
            if out[i] == 0xFE and out[i+1] == 0xFE:
                # pole 'to' (poz 2)
                if out[i+2] == from_addr:
                    out[i+2] = to_addr
                # pole 'from' (poz 3)
                if out[i+3] == from_addr:
                    out[i+3] = to_addr
                j = out.find(b'\xFD', i+2)
                if j < 0:
                    break
                i = j + 1
            else:
                i += 1
        return bytes(out)

    async def _send_service_status(self, ws):
        """Poinformuj klienta ktore services sa aktywne na serwerze."""
        # CI-V online = mamy CivRig i port jest otwarty
        civ_online = bool(self.civ_rig and getattr(self.civ_rig, '_ser', None))
        await ws.send_json({'type': 'service_status', 'service': 'civ',
                             'online': civ_online})

    # ── Helpery ────────────────────────────────────────────────────────────
    def _peer_str(self, ws) -> str:
        try:
            peer = ws._req.transport.get_extra_info('peername')
            return f"{peer[0]}:{peer[1]}" if peer else "?:?"
        except Exception:
            return "?:?"

    def _parse_json(self, text: str) -> dict:
        try:
            import orjson
            return orjson.loads(text)
        except ImportError:
            import json
            return json.loads(text)

    def get_stats(self) -> dict:
        """Zwraca statystyki dla debug endpointu /api/com/stats."""
        return {
            'connected_clients': len(self._clients),
            'clients': [{
                'peer':        info['peer'],
                'user_id':     info['user_id'],
                'ports':       info['ports'],
                'assignments': info['assignments'],
                'rate_bytes':  info['rate_bytes'],
                'connected_s': int(time.monotonic() - info['connected_at']),
            } for info in self._clients.values()],
        }
