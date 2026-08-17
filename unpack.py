"""
Etap 4: unpack77 - odwrotnosc pack77 z ft8_encoder.py.
Uzywa DOKLADNIE tych samych tabel (zweryfikowanych dzis bit-dokladnie
przeciwko prawdziwemu ft8code.exe), tylko w odwrotna strone.
"""
import ft8_encoder as fe

_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Alfabet uzywany do hashowania niestandardowych callsigns (39 znakow:
# spacja + 0-9 + A-Z + '/'). Zweryfikowany bit-dokladnie przeciwko dwoch
# autorytatywnym przykladom z listy mailingowej wsjt-devel (YW18FIFA -> 771524,
# VK0MUCHTOOLONGCALLSIGN -> 1137640).
_HASH_ALPHABET = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ/"

# Alfabet 38-znakowy do pakowania niestandardowego znaku w polu c58 (Type 4).
# Ten sam zestaw co _HASH_ALPHABET (spacja + 0-9 + A-Z + '/').
_C58_ALPHABET = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ/"


def _unpack58(n58):
    """
    Rozpakowuje 58-bitowe pole c58 (Type 4) na niestandardowy znak.
    Odwrotnosc pakowania WSJT-X: 11-znakowy string budowany przez kolejne
    reszty z dzielenia przez 38, od konca. Zweryfikowane bit-dokladnie na
    prawdziwym sygnale HB10GBT z pasma 15m (n58=55151927106 -> 'HB10GBT').
    """
    chars = []
    for _ in range(11):
        chars.append(n58 % 38)
        n58 //= 38
    return ''.join(_C58_ALPHABET[i] for i in reversed(chars)).strip()


def ihashcall(callsign, m=22):
    """
    Oblicza m-bitowy hash callsign wedlug oficjalnej specyfikacji protokolu
    FT8/FT4 (zgodnosc bitowa wymagana do poprawnej wspolpracy z innymi
    stacjami w eterze).
    Uzywane do budowania tablicy hash->callsign: gdy widzimy PELNY
    callsign w jednej ramce, zapamietujemy jego hash, zeby podstawic
    prawdziwy tekst gdy ten sam hash pojawi sie w innej (niestandardowej)
    ramce.
    """
    c0 = callsign.upper().strip().ljust(11)[:11]
    n8 = 0
    for ch in c0:
        j = _HASH_ALPHABET.find(ch)
        if j < 0:
            j = 0
        n8 = 38 * n8 + j
    prod = (47055833459 * n8) & ((1 << 64) - 1)
    return prod >> (64 - m)


class HashCallCache:
    """
    Prosta tablica hash22 -> callsign, budowana w czasie (analogicznie do
    prawdziwego WSJT-X). Nie jest trwala miedzy restartami serwera — to
    samo zachowanie co WSJT-X (tablica zeruje sie przy starcie programu).
    """
    def __init__(self, max_size=2000):
        self._map = {}
        self._max_size = max_size

    def remember(self, callsign):
        if not callsign or callsign.startswith('<') or '_' in callsign:
            return
        h = ihashcall(callsign, 22)
        if len(self._map) >= self._max_size and h not in self._map:
            self._map.pop(next(iter(self._map)))
        self._map[h] = callsign

    def lookup(self, hash_val):
        return self._map.get(hash_val)


# Cache globalny modulu (jeden proces serwera = jedna sesja nasluchu, zgodnie
# z zachowaniem WSJT-X)
_hash_cache = HashCallCache()


def _bits_to_int(bits):
    v = 0
    for b in bits:
        v = (v << 1) | b
    return v


def _unpack_c28(c28):
    """Odwrotnosc _encode_c28/_packcall. Zwraca string callsign (lub specjalny token)."""
    if c28 == 0:
        return "DE"
    if c28 == 1:
        return "QRZ"
    if c28 == fe._NBASE_CQ:
        return "CQ"
    if 3 <= c28 < 3 + 1000000:
        # CQ + numeric/alfanumeric suffix - rzadko uzywane, pomijamy szczegoly
        return f"CQ_{c28}"
    NTOKENS = 2063592
    if NTOKENS <= c28 < NTOKENS + 4194304:
        hash_val = c28 - NTOKENS
        known = _hash_cache.lookup(hash_val)
        if known:
            return f"<{known}>"
        return "<...>"
    if c28 >= fe._STD_CALL_OFFSET:
        x = c28 - fe._STD_CALL_OFFSET
        # x = a0*36*10*27^3 + a1*10*27^3 + a2*27^3 + a3*27^2 + a4*27 + a5
        a5 = x % 27
        x //= 27
        a4 = x % 27
        x //= 27
        a3 = x % 27
        x //= 27
        a2 = x % 10
        x //= 10
        a1 = x % 36
        x //= 36
        a0 = x

        def ch_pos0(v):
            if v == 0:
                return ' '
            if 1 <= v <= 10:
                return str(v - 1)
            return _LETTERS[v - 11]

        def ch_pos1(v):
            if 0 <= v <= 9:
                return str(v)
            return _LETTERS[v - 10]

        def ch_suffix(v):
            if v == 0:
                return ' '
            return _LETTERS[v - 1]

        call = ch_pos0(a0) + ch_pos1(a1) + str(a2) + ch_suffix(a3) + ch_suffix(a4) + ch_suffix(a5)
        return call.strip()
    return f"<unknown:{c28}>"


def _ng_to_grid(ng):
    """Odwrotnosc _grid_to_ng: ng (0..32399) -> 4-znakowy grid (system pozycyjny)."""
    c3 = ng % 10
    ng //= 10
    c2 = ng % 10
    ng //= 10
    c1 = ng % 18
    ng //= 18
    c0 = ng % 18
    return chr(ord('A') + c0) + chr(ord('A') + c1) + str(c2) + str(c3)


def _unpack_g15(g15):
    """
    Odwrotnosc _encode_g15. Enkoder koduje (zweryfikowane wzgledem WSJT-X):
      grid 4-znak -> 0..32399
      "" -> 32401, "RRR" -> 32402, "RR73" -> 32403, "73" -> 32404
      raport +/-NN (-30..+49) -> g15 = 32400 + (raport + 35)  [r_flag=False]
      R+/-NN                  -> g15 = 32400 + (raport + 35)  [r_flag=True]
    Bit R (R1) rozroznia raport z prefiksem R od goleg — obslugiwany przez
    wolajacego (format_message dokleja R gdy R1=1), tu zwracamy sam raport.
    """
    if g15 == 0:
        return ""
    if g15 < 32400:
        return _ng_to_grid(g15)
    if g15 == 32401:
        return ""
    if g15 == 32402:
        return "RRR"
    if g15 == 32403:
        return "RR73"
    if g15 == 32404:
        return "73"
    # raporty numeryczne: g15 = 32400 + (raport + 35) => raport = g15 - 32435
    if 32405 <= g15 <= 32484:
        rpt = g15 - 32435
        return f"{rpt:+03d}"
    return f"<g15:{g15}>"


def unpack77(bits77):
    """
    bits77: lista/array 77 bitow (0/1).
    Zwraca dict: {i3, call_to, call_de, r1, report_or_grid, R1}
    """
    bits = list(bits77)
    assert len(bits) == 77
    data74 = bits[0:74]
    i3 = _bits_to_int(bits[74:77])

    # --- Type 4: wiadomosc z niestandardowym (dlugim/zlozonym) znakiem ---
    # Struktura (zweryfikowana na prawdziwym HB10GBT, 15m):
    #   bits[0:12]  = h12  - 12-bitowy hash DRUGIEGO znaku (odbiorcy/partnera)
    #   bits[12:70] = c58  - 58-bitowy niestandardowy znak (nadawca)
    #   bits[70]    = iflip- 0: nadawca=c58; 1: role zamienione
    #   bits[71:73] = nrpt - kod tresci (0:brak,1:RRR,2:RR73,3:73)
    #   bits[73]    = icq  - 1: wiadomosc CQ
    #   bits[74:77] = i3=4
    if i3 == 4:
        h12 = _bits_to_int(bits[0:12])
        n58 = _bits_to_int(bits[12:70])
        iflip = bits[70]
        nrpt = _bits_to_int(bits[71:73])
        icq = bits[73]

        nonstd_call = _unpack58(n58)
        # zapamietaj pelny niestandardowy znak pod jego 12-bit hashem,
        # zeby podstawic go gdy pojawi sie tylko jako hash w innej ramce
        if nonstd_call and not nonstd_call.startswith('<'):
            _hash_cache.remember(nonstd_call)
        # h12 odnosi sie do PARTNERA; sprobuj znalezc pelny znak po 12-bit hashu
        partner = None
        for known in list(_hash_cache._map.values()):
            if ihashcall(known, 12) == h12:
                partner = known
                break
        partner_txt = partner if partner else (f"<...{h12}>" if h12 else "")

        if icq:
            call_to, call_de = "CQ", nonstd_call
        elif iflip:
            call_to, call_de = nonstd_call, partner_txt
        else:
            call_to, call_de = partner_txt, nonstd_call

        rpt_map = {0: "", 1: "RRR", 2: "RR73", 3: "73"}
        return {
            "i3": i3,
            "call_to": call_to,
            "call_de": call_de,
            "r1_1": 0,
            "r1_2": 0,
            "R1": 0,
            "report_or_grid": rpt_map.get(nrpt, ""),
        }

    c28_1 = _bits_to_int(data74[0:28])
    r1_1 = data74[28]
    c28_2 = _bits_to_int(data74[29:57])
    r1_2 = data74[57]
    R1 = data74[58]
    g15 = _bits_to_int(data74[59:74])

    call_to = _unpack_c28(c28_1)
    call_de = _unpack_c28(c28_2)
    grid_or_report = _unpack_g15(g15)

    # Ucz cache: kazdy PELNY (nie-hash, nie-specjalny) callsign widziany w tej
    # ramce zapamietujemy, zeby podstawic go pod ten sam hash w przyszlych,
    # niestandardowych ramkach.
    for call in (call_to, call_de):
        if call and call not in ("CQ", "DE", "QRZ") and not call.startswith("<") and not call.startswith("CQ_"):
            _hash_cache.remember(call)

    return {
        "i3": i3,
        "call_to": call_to,
        "call_de": call_de,
        "r1_1": r1_1,
        "r1_2": r1_2,
        "R1": R1,
        "report_or_grid": grid_or_report,
    }


def format_message(parsed):
    p = parsed
    parts = [p["call_to"], p["call_de"]]
    rg = p["report_or_grid"]
    if p["R1"] and rg and not rg.startswith("R"):
        rg = "R" + rg
    if rg:
        parts.append(rg)
    return " ".join(parts)
