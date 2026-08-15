"""
qso_engine.py — silnik automatycznego prowadzenia QSO w stylu WSJT-X.

Czysta logika (bez asyncio/sieci), latwa do testowania w izolacji.
Odpowiada za:
  1. Parsowanie odebranych wiadomosci FT8 (kto/do kogo/co).
  2. Maszyne stanow pojedynczego QSO (Tx1..Tx5, klasyczna sekwencja RRR->73).
  3. Kolejke stacji odpowiadajacych na nasze CQ ("Call 1st" / FIFO).

Standardowa sekwencja wiadomosci WSJT-X (gdy MY zaczynamy, odpowiadajac
na cudze CQ — czyli Tx1 jako pierwszy krok):
  Tx1: <ICH_CALL> <NASZ_CALL> <NASZ_GRID>
  Tx2: <ICH_CALL> <NASZ_CALL> <RAPORT_SNR>           (gdy oni nam wysyla raport)
  Tx3: <ICH_CALL> <NASZ_CALL> R<RAPORT_SNR>          (potwierdzenie + nasz raport)
  Tx4: <ICH_CALL> <NASZ_CALL> RRR                    (potwierdzenie odbioru raportu)
  Tx5: <ICH_CALL> <NASZ_CALL> 73                     (pozegnanie, koniec QSO)

Gdy MY wolamy CQ i KTOS nam odpowiada, sekwencja startuje od Tx2 (bo Tx1
to ich pierwsza wiadomosc do nas, na ktora odpowiadamy raportem).

Ta implementacja uzywa klasycznej (nie skroconej RR73) koncowki: osobne
Tx4=RRR i Tx5=73, zgodnie z wyborem uzytkownika — RRR wysylane natychmiast
po otrzymaniu raportu, 73 wysylane dopiero gdy partner potwierdzi (wysle
RRR lub 73 ze swojej strony). To wierniej odwzorowuje tradycyjna sekwencje
(patrz WSJT-X User Guide, sekcja 7.3 Auto-Sequencing) niz nowszy skrot RR73.
"""

import re
import time


# ─────────────────────────────────────────────────────────────────────────────
# Parsowanie wiadomosci FT8
# ─────────────────────────────────────────────────────────────────────────────

# CQ MODIFIERS - lista wszystkich znanych modyfikatorow uzywanych w WSJT-X.
# Format: "CQ <MODIFIER> <CALL> <GRID>" np. "CQ POTA W1XYZ FN42".
# Modifiery to slowa 2-6 znakow ktore nie sa callsignami. Wiekszosc to skroty
# programow zawodowych/aktywacji (POTA, SOTA), regionow (EU, NA, AS) lub
# kraje (USA, JA, DL). Pelen zestaw z WSJT-X messages guide + praktyki na
# pasmie amatorskim.
_CQ_MODIFIERS = frozenset({
    # Kierunkowe / regionalne (kontynenty i regiony)
    "DX", "NA", "SA", "EU", "AS", "AF", "OC", "WW", "WWDX",
    # Kraje (prefixy 1-4 znaki) - najczestsze
    "USA", "JA", "DL", "F", "G", "GM", "GI", "GW", "GD", "GJ",
    "PA", "OE", "OK", "OM", "SP", "SM", "OZ", "LA", "OH", "OY",
    "EA", "CT", "I", "IT", "IS", "S5", "9A", "T7", "T9", "E7",
    "YO", "YU", "LY", "YL", "ES", "UR", "R", "UA", "UN", "UK",
    "VE", "VK", "ZL", "ZS", "ZL7", "KH6", "KL7", "PY", "LU", "CE",
    "HK", "HC", "HI", "HP", "TI", "TG", "XE", "CO", "9Y", "8P",
    "VP", "VP2", "VP6", "VP8", "VP9",
    # Contesty i eventy
    "TEST", "CONTEST", "SPRINT", "FD", "FIELD",
    # Programy aktywacji
    "POTA", "SOTA", "IOTA", "WWFF", "COTA", "BUNKER", "REF",
    "USI", "USIS", "ILLW", "WCA", "WFF", "TQP",
    # Kluby i organizacje
    "ARRL", "RSGB", "DARC", "IARU", "SP",
    # Moc
    "QRP", "QRO", "QRPP",
    # Specjalne
    "FF", "SKCC", "SOWP", "PODXS",
})

# Znane suffixy w callsignie (po "/")
# Uwaga: portable district /0..9 obslugujemy osobno jako regex.
_CALL_SUFFIXES = frozenset({
    "M",     # mobile (samochod)
    "MM",    # maritime mobile (statek)
    "AM",    # aeronautical mobile (samolot)
    "P",     # portable (przenosny)
    "QRP",   # low power
    "QRPP",  # very low power
    "LH",    # lighthouse
})

# Regex portable district: /0 do /9
_PORTABLE_DISTRICT_RE = re.compile(r'^\d$')

# Format callsigna amatorskiego: 1-2 litery, 0-1 cyfra, 1-2 litery, cyfra, 1-4 litery
# Uproszczone: 3-8 znakow, ma co najmniej 1 cyfre i co najmniej 1 litere
_CALLSIGN_RE = re.compile(r'^[A-Z0-9]{3,8}$')


def is_valid_callsign(s: str) -> bool:
    """Czy string wyglada jak callsign amatorski (bez suffixow/prefixow)."""
    if not s or not _CALLSIGN_RE.fullmatch(s):
        return False
    has_digit  = any(c.isdigit() for c in s)
    has_letter = any(c.isalpha() for c in s)
    return has_digit and has_letter


def is_cq_modifier(s: str) -> bool:
    """Czy string wyglada jak modifier CQ (POTA, DX, USA itp.).

    Klasyfikacja: (1) whitelist znanych modifierow, LUB
    (2) 2-6 znakow, bez cyfr (jesli ma cyfry - to raczej callsign albo prefix).
    Uwaga: to jest heurystyka - w rzadkich przypadkach cos w stylu 'K7RA'
    zostanie zle sklasyfikowane, ale dla 99% praktyki na pasmie dziala.

    (2) jest CELOWO ogolna, nie kolejnym wpisem do whitelisty: prawdziwy
    znak amatorski ZAWSZE zawiera cyfre (patrz is_valid_callsign wyzej),
    wiec kazdy czysto literowy string do 6 znakow jest bezpiecznie
    klasyfikowany jako modifier bez wymieniania z nazwy kazdego programu
    aktywacyjnego osobno. Znaleziono 2026-08-15: whitelist mial explicite
    POTA/SOTA ale NIE BOTA (Bunkers on the Air) - kod faktycznie
    sprawdzal `len(s) <= 3`, mimo ze ten docstring od zawsze mowil "2-6" -
    niezgodnosc kodu z opisem, nie tylko brakujacy wpis. Naprawiono przez
    poprawienie granicy do faktycznych 6 zamiast dopisywania kolejnych
    4-6-literowych skrotow (COTA/GOTA/HOTA/LOTA/MOTA/VOTA/WOTA/ZOTA/...) na
    sztywno - ta sama poprawka jest zwierciedlona w _isCqModifier (JS,
    wsjtx.js) po stronie frontu, ktory ma wlasny, mniejszy podzbior
    whitelisty."""
    if not s:
        return False
    if s in _CQ_MODIFIERS:
        return True
    # Fallback: krotki string bez cyfr moze byc modifierem (np. rzadki kraj
    # lub nieujety w whiteliscie program aktywacyjny) - MUSI byc bez cyfr,
    # bo prawdziwy znak amatorski zawsze ma przynajmniej jedna.
    if len(s) <= 6 and s.isalpha():
        return True
    return False


def strip_suffix(call: str) -> tuple:
    """Rozdziel callsign na (base_call, suffix, prefix).

    Obsluguje:
      - XX0XXX/M       -> base=XX0XXX, suffix=M,   prefix=None
      - XX0XXX/MM      -> base=XX0XXX, suffix=MM,  prefix=None
      - XX0XXX/P       -> base=XX0XXX, suffix=P,   prefix=None
      - XX0XXX/QRP     -> base=XX0XXX, suffix=QRP, prefix=None
      - W1AB/2         -> base=W1AB,   suffix='2', prefix=None (portable district)
      - W1/DL3ABC      -> base=DL3ABC, suffix=None, prefix=W1 (op w innym kraju)
      - W1/DL3ABC/P    -> base=DL3ABC, suffix=P,   prefix=W1 (op portable w innym kraju)
      - XX0XXX         -> base=XX0XXX, suffix=None, prefix=None
      - Puste/brak     -> (call, None, None)

    Uwaga: base_call to callsign uzywany do DXCC lookup i porownania QSO
    (te same stacje bez wzgledu na /M vs stacjonarny). suffix i prefix
    pozostaja do wyswietlania i uzycia w wiadomosciach TX (musimy je
    zachowac zeby stacja rozpoznala ze do niej mowimy)."""
    if not call:
        return (call, None, None)
    parts = call.split('/')
    if len(parts) == 1:
        return (call, None, None)
    if len(parts) == 2:
        # Moze byc: BASE/SUFFIX lub PREFIX/BASE
        a, b = parts
        # Jesli b to znany suffix albo cyfra portable district
        if b in _CALL_SUFFIXES or _PORTABLE_DISTRICT_RE.fullmatch(b):
            return (a, b, None)
        # Jesli a jest bardzo krotkie (1-3 znaki) i b wyglada jak callsign
        # -> a to prefix (kraj/region), b to base
        if len(a) <= 3 and is_valid_callsign(b):
            return (b, None, a)
        # Fallback: traktuj jako BASE/SUFFIX (nawet nieznany)
        return (a, b, None)
    if len(parts) >= 3:
        # PREFIX/BASE/SUFFIX np. W1/DL3ABC/P
        prefix, base, suffix = parts[0], parts[1], parts[2]
        return (base, suffix, prefix)
    return (call, None, None)


def base_call(call: str) -> str:
    """Wyciagnij base callsign (bez suffix/prefix). Uzywane do DXCC lookup
    i porownania czy to ta sama stacja."""
    b, _, _ = strip_suffix(call)
    return b


def parse_message(message: str):
    """
    Rozbija zdekodowana wiadomosc FT8 na skladowe.
    Zwraca dict: {call_to, call_de, extra, is_cq, is_rrr, is_73, is_rr73, report,
                  cq_modifier, de_base, de_suffix, de_prefix, to_base}
    lub None jesli wiadomosc nie pasuje do zadnego rozpoznawanego wzorca.

    Nowe pola:
      cq_modifier - modifier CQ (POTA/SOTA/DX/USA itd.) albo None
      de_base     - base callsign nadawcy (bez suffix /M /P) do DXCC lookup
      de_suffix   - suffix (M/MM/P/QRP/0-9) albo None
      de_prefix   - prefix (W1/, EA5/) albo None (dla stacji operujacych w innym kraju)
      to_base     - base callsign odbiorcy (bez suffix)

    Obslugiwane formaty:
      "CQ CALL GRID"                    -> zwykle CQ
      "CQ MOD CALL GRID"                -> CQ z modifierem (POTA/DX/USA/JA/EU/...)
      "CQ CALL/M GRID"                  -> CQ mobile
      "CQ MOD CALL/P GRID"              -> CQ portable z modifierem
      "CQ W1/DL3ABC GRID"               -> CQ z prefiksem (op w innym kraju)
      "CQ MOD"                          -> broadcast bez konkretnego adresata
      "TO DE GRID"                      -> Tx1
      "TO DE +NN"/"TO DE -NN"           -> Tx1/Tx2 raport SNR
      "TO DE R+NN"/"TO DE R-NN"         -> Tx3 potwierdzenie + raport
      "TO DE RRR"                       -> Tx4
      "TO DE RR73"                      -> Tx4 skroc
      "TO DE 73"                        -> Tx5
    """
    if not message:
        return None
    parts = message.strip().upper().replace('<', '').replace('>', '').split()
    if not parts:
        return None

    if parts[0] == 'CQ':
        # Bez call - "CQ MOD" (2 slowa) - broadcast bez konkretnej stacji
        # np. "CQ FF" (Fox call w Fox/Hound), "CQ DX" bez podpisu
        if len(parts) == 2 and is_cq_modifier(parts[1]):
            return {'call_to': None, 'call_de': None, 'extra': None,
                    'is_cq': True, 'is_rrr': False, 'is_73': False,
                    'is_rr73': False, 'report': None, 'cq_modifier': parts[1],
                    'de_base': None, 'de_suffix': None, 'de_prefix': None,
                    'to_base': None}

        # 3 slowa: "CQ CALL GRID" (bez modifiera)
        if len(parts) == 3:
            call_de = parts[1]
            de_base, de_suffix, de_prefix = strip_suffix(call_de)
            return {'call_to': None, 'call_de': call_de, 'extra': parts[2],
                    'is_cq': True, 'is_rrr': False, 'is_73': False,
                    'is_rr73': False, 'report': None, 'cq_modifier': None,
                    'de_base': de_base, 'de_suffix': de_suffix,
                    'de_prefix': de_prefix, 'to_base': None}

        # 4 slowa: "CQ MOD CALL GRID" (POTA/DX/USA/EU/...)
        # Decyzja czy parts[1] to modifier czy callsign:
        #   - jesli jest w whitelist -> na pewno modifier
        #   - jesli wyglada jak callsign (ma cyfre) -> raczej callsign
        #     (wtedy struktura to nietypowa 4-slowo bez modifiera, rzadkie)
        if len(parts) == 4:
            if is_cq_modifier(parts[1]):
                call_de = parts[2]
                de_base, de_suffix, de_prefix = strip_suffix(call_de)
                return {'call_to': None, 'call_de': call_de, 'extra': parts[3],
                        'is_cq': True, 'is_rrr': False, 'is_73': False,
                        'is_rr73': False, 'report': None, 'cq_modifier': parts[1],
                        'de_base': de_base, 'de_suffix': de_suffix,
                        'de_prefix': de_prefix, 'to_base': None}
            # Nietypowo: 4 slowa ale parts[1] nie wyglada jak modifier
            # Traktuj jak "CQ CALL GRID EXTRA" (ignoruj EXTRA)
            call_de = parts[1]
            de_base, de_suffix, de_prefix = strip_suffix(call_de)
            return {'call_to': None, 'call_de': call_de, 'extra': parts[2],
                    'is_cq': True, 'is_rrr': False, 'is_73': False,
                    'is_rr73': False, 'report': None, 'cq_modifier': None,
                    'de_base': de_base, 'de_suffix': de_suffix,
                    'de_prefix': de_prefix, 'to_base': None}
        return None

    if len(parts) < 3:
        # "<TO> <DE> 73" to minimum 3 slowa; cokolwiek krotszego nie pasuje
        # do standardowego wzorca (moze to byc free-text — ignorujemy).
        return None

    call_to, call_de = parts[0], parts[1]
    to_base = base_call(call_to)
    de_base, de_suffix, de_prefix = strip_suffix(call_de)
    tail = parts[2]

    is_rrr = (tail == 'RRR')
    is_73 = (tail == '73')
    is_rr73 = (tail == 'RR73')
    report = None
    if not (is_rrr or is_73 or is_rr73):
        # Raport SNR: "+NN", "-NN", "R+NN", "R-NN" (R prefix = potwierdzenie + raport)
        if re.fullmatch(r'R?[+-]\d{1,2}', tail):
            report = tail
        # W przeciwnym razie tail to prawdopodobnie grid square (Tx1) — zostaw
        # w 'extra', nie traktuj jako blad parsowania.

    return {
        'call_to': call_to, 'call_de': call_de,
        'extra': tail if report is None and not (is_rrr or is_73 or is_rr73) else None,
        'is_cq': False, 'is_rrr': is_rrr, 'is_73': is_73, 'is_rr73': is_rr73,
        'report': report, 'cq_modifier': None,
        'de_base': de_base, 'de_suffix': de_suffix, 'de_prefix': de_prefix,
        'to_base': to_base,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Stany QSO
# ─────────────────────────────────────────────────────────────────────────────

ST_IDLE = 'IDLE'                  # brak aktywnego QSO
ST_CALLING = 'CALLING'            # wyslalismy Tx1/Tx2, czekamy na odpowiedz
ST_REPORT_SENT = 'REPORT_SENT'    # wyslalismy nasz raport (Tx2/Tx3), czekamy na R+raport lub RRR
ST_RRR_SENT = 'RRR_SENT'          # wyslalismy RRR (Tx4), czekamy na 73 lub powtorke
ST_DONE = 'DONE'                  # QSO zakonczone (otrzymalismy/wyslalismy 73), do zalogowania


class QsoEngine:
    """
    Maszyna stanow pojedynczego, aktywnego QSO + kolejka stacji oczekujacych
    (odpowiedzialy na nasze CQ, ale nie sa jeszcze obslugiwane).

    Uzycie (przez webapp.py):
      engine = QsoEngine(my_call='XX0XXX', my_grid='KO02')
      engine.start_qso('DL1ABC')                 # recznie LUB automatycznie po CQ
      action = engine.on_decode(parsed_message)  # wywolywane dla KAZDEGO dekodowania
      if action: send_tx(action.call_to, action.call_de, action.report_or_grid, action.r_flag)
    """

    def __init__(self, my_call: str, my_grid: str):
        self.my_call = my_call.upper()
        self.my_grid = my_grid.upper()
        self.state = ST_IDLE
        self.partner_call = None
        self.partner_grid = None          # grid partnera, jesli go znamy (z ich Tx1)
        self.partner_report_sent = None   # raport KTORY MY wyslalismy partnerowi
        self.partner_report_recv = None   # raport KTORY MY otrzymalismy od partnera
        self.started_at = None
        self.last_activity_at = None
        self.last_tx_at = None    # kiedy OSTATNI RAZ wyslalismy cokolwiek w tym QSO
        self.retry_count = 0      # ile razy powtorzylismy ostatnia wiadomosc bez odpowiedzi
        self.queue = []   # lista callsignow oczekujacych w kolejce (FIFO), wypelniana CQ-odpowiedziami
        self._queue_seen = set()  # zapobiega duplikatom w kolejce
        # call -> recvEpoch (czas ODBIORU dekodu, ktory dodal te stacje do
        # kolejki). Potrzebne zeby po pop_next_from_queue() webapp.py mogl
        # poprawnie wyliczyc period TX (patrz _period_from_epoch w webapp.py) -
        # bez tego auto-advance z kolejki nadawal z DOWOLNYM/starym periodem
        # zostawionym po poprzednim QSO, co losowo kolidowalo z partnerem.
        self._queue_recv_epoch = {}

    # ── Zarzadzanie QSO ──────────────────────────────────────────────────────

    def record_sent_report(self, report: str):
        """
        Zapisuje raport, ktory FAKTYCZNIE wyslalismy partnerowi — wywolywane
        przez webapp.py PO podstawieniu realnego, zmierzonego SNR (silnik sam
        nie zna tej wartosci w momencie generowania akcji 'reply' z
        needs_measured_report=True, bo pomiar SNR pochodzi z dekodera audio,
        poza silnikiem). Potrzebne m.in. do wypelnienia pola RST Sent w
        formularzu logowania QSO po zakonczeniu.
        """
        self.partner_report_sent = report

    def start_qso(self, partner_call: str, initial_decode: dict = None):
        """
        Rozpoczyna QSO z partner_call. Dwa scenariusze:

        1. MY inicjujemy (np. klikamy "odpowiedz" na cudze CQ) — initial_decode
           NIE jest podawany. Stan -> CALLING, pierwsza wiadomosc do wyslania
           to Tx1 (nasz grid), pobierana przez next_tx_action().

        2. Partner JUZ odpowiedzial na NASZE CQ wiadomoscia zawierajaca
           przydatna informacje (ich grid lub raport) — initial_decode to
           sparsowana wiadomosc (z parse_message). W tym przypadku NIE
           wysylamy wlasnego Tx1 (partner juz zna nasz grid, bo to MY
           wolalismy CQ z grid w tresci) — od razu przetwarzamy ich
           wiadomosc przez on_decode, co odpowiednio przeskakuje do
           wyslania raportu (Tx2/Tx3-styl). Odpowiada to udokumentowanemu
           w WSJT-X zachowaniu "skip Tx1" gdy auto-sequencing jest uzbrojone
           po stronie odpowiadania na wlasne CQ.
        """
        self.state = ST_CALLING
        self.partner_call = partner_call.upper()
        self.partner_grid = None
        self.partner_report_sent = None
        self.partner_report_recv = None
        self.started_at = time.time()
        self.last_activity_at = self.started_at
        self.last_tx_at = None
        self.retry_count = 0
        self._queue_seen.discard(self.partner_call)
        self.queue = [c for c in self.queue if c != self.partner_call]
        self._queue_recv_epoch.pop(self.partner_call, None)
        if initial_decode is not None:
            return self.on_decode(initial_decode)
        return None

    def abort_qso(self):
        """Przerywa biezace QSO bez logowania (np. recznie przez uzytkownika,
        lub gdy partner nie odpowiada zbyt dlugo)."""
        self.state = ST_IDLE
        self.partner_call = None
        self.partner_grid = None
        self.partner_report_sent = None
        self.partner_report_recv = None
        self.last_tx_at = None
        self.retry_count = 0

    def enqueue_caller(self, callsign: str, recv_epoch: float = None):
        """Dodaje stacje do kolejki "Call 1st" (odpowiedziala na nasze CQ,
        ale aktualnie prowadzimy inne QSO lub jeszcze nie zaczelismy).
        FIFO, bez duplikatow. recv_epoch (czas odbioru dekodu ktory ja
        dodal) jest zapamietywany do pozniejszego wyliczenia periodu TX
        przy pop_next_from_queue()."""
        callsign = callsign.upper()
        # Porownanie po BASE call zeby XX0XXX/M nie zostal zdupikowany
        # gdy XX0XXX jest juz partnerem (albo odwrotnie).
        if base_call(callsign) == base_call(self.partner_call or ''):
            return
        if callsign in self._queue_seen:
            return
        self._queue_seen.add(callsign)
        self.queue.append(callsign)
        if recv_epoch is not None:
            self._queue_recv_epoch[callsign] = recv_epoch

    def pop_next_from_queue(self):
        """Zwraca (callsign, recv_epoch) pierwszej stacji z kolejki (FIFO) i
        usuwa ja, lub (None, None) jesli pusta. recv_epoch to None gdy stacja
        trafila do kolejki bez znacznika czasu (nie powinno sie zdarzac w
        normalnym uzyciu, ale wywolujacy musi to obsluzyc)."""
        if not self.queue:
            return None, None
        callsign = self.queue.pop(0)
        self._queue_seen.discard(callsign)
        recv_epoch = self._queue_recv_epoch.pop(callsign, None)
        return callsign, recv_epoch

    def remove_from_queue(self, callsign: str) -> bool:
        """Usuwa wskazana stacje z kolejki (przycisk ✕ w UI). Zwraca True
        jesli stacja byla w kolejce. Czysci tez _queue_seen zeby stacja mogla
        wrocic do kolejki jesli znow odpowie na CQ."""
        callsign = (callsign or "").upper()
        if callsign not in self.queue:
            return False
        self.queue = [c for c in self.queue if c != callsign]
        self._queue_seen.discard(callsign)
        self._queue_recv_epoch.pop(callsign, None)
        return True

    def clear_queue(self):
        """Oproznia cala kolejke "Call 1st" (przycisk "wyczysc" w UI).
        Bez tego stare, dawno nieaktualne zgloszenia (stacje ktore odpowiedzialy
        na CQ minuty/godziny wczesniej i moga juz nie sluchac) nie mialy zadnego
        sposobu opuszczenia kolejki poza reczym ✕ na kazdej z osobna — po dluzszej
        sesji kolejka rosla i Call 1st w koncu "wyplowal" stary, nieaktualny znak."""
        self.queue = []
        self._queue_seen = set()
        self._queue_recv_epoch = {}

    # ── Przetwarzanie odebranych wiadomosci ──────────────────────────────────

    def on_decode(self, parsed: dict, recv_epoch: float = None):
        """
        Wywolywane dla KAZDEGO sparsowanego dekodowania (patrz parse_message).
        Zwraca dict {'action': 'reply'|'enqueue'|None, 'call_to', 'call_de',
        'report_or_grid', 'r_flag'} opisujacy co zrobic, lub None jesli ta
        wiadomosc nie wymaga zadnej reakcji.

        recv_epoch: czas ODBIORU tego dekodu (webapp.py's recvEpoch) — jesli
        stacja trafia do kolejki Call 1st, jest zapamietywany razem z nia
        (patrz enqueue_caller), zeby po pop_next_from_queue() webapp.py mogl
        poprawnie wyliczyc period TX dla tej stacji zamiast dziedziczyc
        przypadkowy period po poprzednim QSO.

        Logika rozpoznawania:
        - Jesli to CQ od kogos: jesli IDLE -> nic (UI moze pokazac liste CQ,
          ale automatyczne wolanie wymaga jawnego start_qso, zgodnie z modelem
          "klikam raz, reszta automatyczna" — sam fakt CQ nie wystarczy, bo
          moglyby sie odzywac dziesiatki stacji jednoczesnie).
        - Jesli wiadomosc jest ADRESOWANA DO NAS (call_to == my_call):
            - jesli call_de == partner_call AKTYWNEGO QSO -> przetworz wg stanu
            - jesli call_de != partner_call ale jestesmy IDLE lub CALLING do
              kogos innego -> ktos inny odpowiada nam (np. na nasze CQ) ->
              enqueue (chyba ze jestesmy IDLE i to pierwsza taka stacja —
              wtedy UI/webapp moze zdecydowac o natychmiastowym start_qso;
              silnik tylko sygnalizuje 'enqueue', decyzje o auto-start
              podejmuje wywolujacy kod na podstawie ustawien Call 1st)
        """
        if parsed is None:
            return None

        if parsed['is_cq']:
            return None  # UI/webapp decyduje czy/kiedy zainicjowac QSO z CQ-callera

        if parsed['call_to'] != self.my_call:
            # Wiadomosc nie do nas — ale jesli to NASZ AKTUALNY PARTNER
            # nadajacy do KOGOS INNEGO, to dowod ze juz zaczal QSO z tamta
            # stacja i dalsze wolanie go nie ma sensu - bez tego retry
            # (should_retransmit) slepo probowal dalej mimo ewidentnego
            # dowodu ze stacja jest zajeta. Zglaszane na zywo: "jesli
            # odpowiadamy stacji na CQ a on juz nadaje do kogos innego, nie
            # mozemy nadawac". Tylko gdy mamy aktywnego partnera (nie IDLE/
            # DONE) — inaczej kazda przypadkowa wymiana miedzy dwiema
            # OBCYMI stacjami wywolywalaby to bez potrzeby.
            if self.is_active() and self.partner_call and parsed.get('call_de'):
                de_base = parsed.get('de_base') or base_call(parsed['call_de'])
                if de_base == base_call(self.partner_call):
                    return {'action': 'partner_busy', 'call_de': self.partner_call}
            return None  # wiadomosc nie do nas — ignoruj (Band Activity i tak ja pokaze)

        call_de = parsed['call_de']

        # Wiadomosc od kogos INNEGO niz nasz aktualny partner QSO.
        # Porownanie po BASE call (bez suffixu /M /P) zeby stacja mobile
        # traktowana byla jako ta sama w calym QSO, nawet gdyby raz wyslala
        # z suffixem a raz bez.
        de_base = parsed.get('de_base') or base_call(call_de) if call_de else None
        partner_base = base_call(self.partner_call) if self.partner_call else None
        if de_base != partner_base:
            # Zawsze faktycznie dodajemy do kolejki (nie tylko sygnalizujemy) —
            # tak ze nawet jesli webapp.py zignoruje zwrocony 'enqueue' i nie
            # odpali natychmiast start_qso, stacja i tak nie zostanie zgubiona.
            self.enqueue_caller(call_de, recv_epoch)
            if self.state == ST_IDLE:
                # Nikt nie jest aktualnie obslugiwany — sygnalizujemy 'enqueue'
                # zeby webapp.py mogl (wg ustawien Call 1st) NATYCHMIAST wywolac
                # start_qso(call_de, initial_decode=parsed) zamiast czekac w kolejce.
                return {'action': 'enqueue', 'call_de': call_de}
            return None  # w trakcie innego QSO — czeka w kolejce na pozniej

        # Wiadomosc OD AKTUALNEGO PARTNERA QSO — przetworz wg maszyny stanow.
        # Partner faktycznie odpowiada, wiec zerujemy licznik powtorzen (patrz
        # note_retry/should_give_up) — nastepna cisza z jego strony dostaje
        # znowu pelna pule prob.
        self.last_activity_at = time.time()
        self.retry_count = 0

        if parsed['is_73']:
            # Partner konczy QSO — wysylamy nasze 73 (jesli jeszcze nie bylo)
            # i logujemy. Dziala z dowolnego stanu (partner moze przeskoczyc
            # RRR i wyslac 73 bezposrednio, lub powtorzyc 73 jesli nasze RRR
            # zgubilo sie w propagacji).
            # NAPRAWA "niepotrzebne przedluzanie": jesli QSO JUZ DONE (wyslalismy
            # 73), NIE odpowiadamy ponownie na echo 73 partnera. Automat
            # odpowiadal 73 na 73 w kolko. 73 wysylamy RAZ; kolejne echa = cisza.
            if self.state == ST_DONE:
                return None
            self.state = ST_DONE
            return {'action': 'reply', 'call_to': self.partner_call,
                    'call_de': self.my_call, 'report_or_grid': '73',
                    'r_flag': False, 'qso_complete': True}

        if parsed['is_rr73']:
            # Skrocona forma od partnera: potwierdzenie + pozegnanie naraz.
            # Odpowiadamy naszym 73 i konczymy.
            # NAPRAWA: tak samo jak 73 — po DONE nie odpowiadaj na echo RR73.
            if self.state == ST_DONE:
                return None
            self.state = ST_DONE
            return {'action': 'reply', 'call_to': self.partner_call,
                    'call_de': self.my_call, 'report_or_grid': '73',
                    'r_flag': False, 'qso_complete': True}

        if parsed['is_rrr']:
            # Partner potwierdzil odbior naszego raportu (RRR = "Roger Roger
            # Roger"). Odpowiadamy OD RAZU naszym 73 - RRR od partnera juz
            # JEST potwierdzeniem, nie trzeba na nie echo-odpowiadac WLASNYM
            # RRR i czekac na kolejny cykl. Dziala z dowolnego nie-DONE stanu,
            # tak samo jak is_73/is_rr73 ponizej.
            # NAPRAWA 2026-08-15: poprzednia wersja w tym miejscu wysylala
            # WLASNE RRR z powrotem (stan RRR_SENT) i dopiero echo TEGO RRR
            # od partnera konczylo QSO wyslaniem 73 - zbedny dodatkowy cykl
            # (zgloszone na zywo: "automat niepotrzebnie przedluza QSO, jesli
            # dostalem RRR to wysylam 73"). Po DONE nie reagujemy na echo RRR.
            if self.state == ST_DONE:
                return None
            self.state = ST_DONE
            return {'action': 'reply', 'call_to': self.partner_call,
                    'call_de': self.my_call, 'report_or_grid': '73',
                    'r_flag': False, 'qso_complete': True}

        if parsed['report'] is not None:
            report = parsed['report']
            if report.startswith('R'):
                # "R-12" — partner POTWIERDZA odbior naszego raportu I wysyla
                # swoj wlasny (ktorego juz nie potrzebujemy ponownie wysylac).
                # Odpowiadamy RRR.
                self.partner_report_recv = report
                self.state = ST_RRR_SENT
                return {'action': 'reply', 'call_to': self.partner_call,
                        'call_de': self.my_call, 'report_or_grid': 'RRR',
                        'r_flag': False, 'qso_complete': False}
            else:
                # "-12" — pierwszy (surowy) raport od partnera, bez R-prefix.
                # MUSIMY odpowiedziec WLASNYM, ZMIERZONYM raportem (R+nasz_SNR),
                # NIE odbitym raportem partnera — to bylby blad protokolu
                # (partner dostalby swoj wlasny raport z powrotem zamiast
                # informacji jak MY go slyszymy). report_or_grid=None tutaj
                # jest celowe: webapp.py MUSI podstawic realny pomiar SNR
                # przed wyslaniem (patrz needs_measured_report).
                self.partner_report_recv = report
                self.state = ST_REPORT_SENT
                return {'action': 'reply', 'call_to': self.partner_call,
                        'call_de': self.my_call,
                        'report_or_grid': None, 'r_flag': True,
                        'qso_complete': False, 'needs_measured_report': True}

        # Wiadomosc z gridem (Tx1) — partner inicjuje, odpowiadamy raportem.
        # report_or_grid=None: webapp.py MUSI podstawic realny pomiar SNR
        # przed wyslaniem (patrz needs_measured_report), tak samo jak wyzej.
        #
        # OBSLUGA POWTORKI (naprawa "automat milczy gdy partner dalej wola"):
        # akceptujemy grid nie tylko w CALLING, ale tez w REPORT_SENT. W FT8
        # partner POWTARZA swoj grid gdy nie uslyszal naszej odpowiedzi — my
        # MUSIMY wtedy POWTORZYC raport (nie milczec). Bez tego automat wysylal
        # raport RAZ i milkl, partner wolal w kolko i w koncu szedl na CQ do
        # kogos innego (dokladnie blad z logu Toma: EA3AT wolal JN00 x3, potem
        # "CQ EA3AT"). Raport jest juz zamrozony (partner_report_sent), wiec
        # webapp poda te sama wartosc — spojnie.
        if parsed['extra'] and self.state in (ST_CALLING, ST_REPORT_SENT):
            # Walidacja formatu grid square (np. KO02, JO80aa) — extra moze
            # teoretycznie zawierac dowolny tekst (parser nie wymusza formatu
            # gridu), wiec nie zapisujemy oczywistych smieci do partner_grid
            # (uzywanego pozniej do wstepnego wypelnienia formularza logowania).
            if re.fullmatch(r'[A-R]{2}\d{2}([A-X]{2})?', parsed['extra']):
                self.partner_grid = parsed['extra']
            self.state = ST_REPORT_SENT
            return {'action': 'reply', 'call_to': self.partner_call,
                    'call_de': self.my_call, 'report_or_grid': None,
                    'r_flag': False, 'qso_complete': False,
                    'needs_measured_report': True}

        # Partner wyslal raport bezposrednio (bez gridu) w stanie CALLING —
        # np. gdy MY wywolalismy stacje i ona odpowiedziala raportem.
        # Odpowiadamy R+raport (potwierdzamy + podajemy nasz SNR).
        if parsed['report'] is not None and self.state == ST_CALLING:
            report = parsed['report']
            if not report.startswith('R'):
                self.partner_report_recv = report
                self.state = ST_REPORT_SENT
                return {'action': 'reply', 'call_to': self.partner_call,
                        'call_de': self.my_call, 'report_or_grid': None,
                        'r_flag': True, 'qso_complete': False,
                        'needs_measured_report': True}

        return None

    def next_tx_action(self):
        """
        Zwraca wiadomosc do wyslania PRZY STARCIE nowego QSO (stan CALLING,
        zanim partner cokolwiek odpowiedzial) — czyli nasz Tx1: ich call,
        nasz call, nasz grid.
        """
        if self.state != ST_CALLING or not self.partner_call:
            return None
        return {'call_to': self.partner_call, 'call_de': self.my_call,
                'report_or_grid': self.my_grid, 'r_flag': False}

    def is_active(self):
        return self.state not in (ST_IDLE, ST_DONE)

    def record_tx_sent(self):
        """Wywolywane przez webapp.py zaraz po faktycznym zaplanowaniu
        transmisji (pierwsze wyslanie LUB powtorka) - ustawia punkt
        odniesienia dla should_retransmit()."""
        self.last_tx_at = time.time()

    def should_retransmit(self, period_s: float) -> bool:
        """Czy minelo juz co najmniej jedno pelne okno TX (period_s) od
        naszej ostatniej transmisji bez zadnej nowej odpowiedzi partnera w
        miedzyczasie - czyli czy pora powtorzyc ostatnia wiadomosc.

        Real WSJT-X/JTDX (patrz process_Auto/genStdMsgs w mainwindow.cpp)
        NIE ma osobnego mechanizmu "powtorki" - po prostu wysyla wiadomosc
        pasujaca do aktualnego stanu na KAZDYM swoim oknie TX, wiec jesli
        stan sie nie zmienil (partner nie odpowiedzial), ta sama wiadomosc
        wychodzi ponownie automatycznie. Nasz silnik jest zdarzeniowy
        (reaguje tylko na nowy dekod), wiec potrzebuje tego jawnego
        sprawdzenia zamiast bezwarunkowego okresowego nadawania - bez
        niego jedna zgubiona transmisja (normalne przy QSB) konczyla sie
        cisza zamiast powtorki, ktora prawdziwy WSJT-X/JTDX wysylaby
        automatycznie."""
        if not self.is_active() or self.last_tx_at is None:
            return False
        return (time.time() - self.last_tx_at) >= period_s

    def note_retry(self):
        """Wywolywane TUZ PRZED faktyczna retransmisja (nie przy pierwszym
        wyslaniu) - zwieksza licznik prob dla should_give_up()."""
        self.retry_count += 1

    def should_give_up(self, max_retries: int) -> bool:
        """Czy wyczerpalismy limit powtorzen bez odpowiedzi partnera.

        JTDX ma analogiczny, ale DOMYSLNIE WYLACZONY licznik (np.
        SeqSentRReportCounter, domyslna wartosc 3-5 prob, ale flaga
        wlaczajaca go jest false) - bez recznego wlaczenia prawdziwy
        WSJT-X/JTDX probuje w nieskonczonosc, bo operator patrzy na ekran
        i moze sam zdecydowac kiedy przerwac. Nasz automat dziala bez
        nadzoru (Call 1st ma przejsc do kolejnej stacji w kolejce), wiec
        limit jest u nas WLACZONY na stale, w przeciwienstwie do JTDX."""
        return self.retry_count >= max_retries
