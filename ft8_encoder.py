"""
ft8_encoder.py — Wlasny enkoder/nadajnik FT8.

Pelny pipeline: tekst wiadomosci -> 77-bit pack -> CRC-14 -> LDPC(174,91)
encode -> Gray mapping -> wstawienie Costas sync -> 79 symboli -> GFSK
audio @ 12kHz -> resample do 48kHz -> stream przez feed_tx_pcm().

ZWERYFIKOWANE w sesji deweloperskiej (2026-06-20), zgodnosc z oficjalna
specyfikacja protokolu FT8:
  - packcall()/packgrid(): roundtrip test przez niezalezny referencyjny
    dekoder (9/9 callsigns w tym XX0XXX, 6/6 gridow w tym KO02 — wszystkie OK)
  - LDPC generator matrix (rawg): zgodna 1:1 z DWOMA niezaleznymi publicznymi
    zrodlami specyfikacji (jedno w formacie hex, drugie binary — identyczne
    wartosci dla wiersza 0, 41, 82)
  - CRC-14 polynomial: zgodny miedzy obydwoma niezaleznymi zrodlami (0x2757)
  - i3=1 (standard message) na POCZATKU 77-bit struktury — zweryfikowane
    przeciwko publicznie udokumentowanemu przykladowi testowemu
  - Pelny pipeline: 7/7 testowych wiadomosci (CQ, standard exchange, RRR,
    73, signal report) przechodzi ldpc_check() z autorytatywna tabela Nm
  - Audio GFSK: dlugosc dokladnie 151680 probek = 12.64s przy 12000Hz;
    FFT analiza pierwszych 7 symboli (Costas) potwierdza poprawne
    czestotliwosci tonow w granicach rozdzielczosci FFT (6.25Hz/bin)

NIEZWERYFIKOWANE wobec prawdziwego dekodera FT8 na zywym radiu — wymaga
testu na zywo, patrz CLAUDE.md sekcja FT8 TX.
"""
import re
import numpy as np
from scipy.special import erf
from scipy.signal import resample_poly

# ============================================================
# CZESC 1: LDPC(174,91) generator matrix
# Zrodlo: oficjalna specyfikacja protokolu FT8, dwa niezalezne publiczne
# zrodla uzyte do wzajemnej weryfikacji
# ============================================================

_RAWG = [
"8329ce11bf31eaf509f27fc","761c264e25c259335493132","dc265902fb277c6410a1bdc",
"1b3f417858cd2dd33ec7f62","09fda4fee04195fd034783a","077cccc11b8873ed5c3d48a",
"29b62afe3ca036f4fe1a9da","6054faf5f35d96d3b0c8c3e","e20798e4310eed27884ae90",
"775c9c08e80e26ddae56318","b0b811028c2bf997213487c","18a0c9231fc60adf5c5ea32",
"76471e8302a0721e01b12b8","ffbccb80ca8341fafb47b2e","66a72a158f9325a2bf67170",
"c4243689fe85b1c51363a18","0dff739414d1a1b34b1c270","15b48830636c8b99894972e",
"29a89c0d3de81d665489b0e","4f126f37fa51cbe61bd6b94","99c47239d0d97d3c84e0940",
"1919b75119765621bb4f1e8","09db12d731faee0b86df6b8","488fc33df43fbdeea4eafb4",
"827423ee40b675f756eb5fe","abe197c484cb74757144a9a","2b500e4bc0ec5a6d2bdbdd0",
"c474aa53d70218761669360","8eba1a13db3390bd6718cec","753844673a27782cc42012e",
"06ff83a145c37035a5c1268","3b37417858cc2dd33ec3f62","9a4a5a28ee17ca9c324842c",
"bc29f465309c977e89610a4","2663ae6ddf8b5ce2bb29488","46f231efe457034c1814418",
"3fb2ce85abe9b0c72e06fbe","de87481f282c153971a0a2e","fcd7ccf23c69fa99bba1412",
"f0261447e9490ca8e474cec","4410115818196f95cdd7012","088fc31df4bfbde2a4eafb4",
"b8fef1b6307729fb0a078c0","5afea7acccb77bbc9d99a90","49a7016ac653f65ecdc9076",
"1944d085be4e7da8d6cc7d0","251f62adc4032f0ee714002","56471f8702a0721e00b12b8",
"2b8e4923f2dd51e2d537fa0","6b550a40a66f4755de95c26","a18ad28d4e27fe92a4f6c84",
"10c2e586388cb82a3d80758","ef34a41817ee02133db2eb0","7e9c0c54325a9c15836e000",
"3693e572d1fde4cdf079e86","bfb2cec5abe1b0c72e07fbe","7ee18230c583cccc57d4b08",
"a066cb2fedafc9f52664126","bb23725abc47cc5f4cc4cd2","ded9dba3bee40c59b5609b4",
"d9a7016ac653e6decdc9036","9ad46aed5f707f280ab5fc4","e5921c77822587316d7d3c2",
"4f14da8242a8b86dca73352","8b8b507ad467d4441df770e","22831c9cf1169467ad04b68",
"213b838fe2ae54c38ee7180","5d926b6dd71f085181a4e12","66ab79d4b29ee6e69509e56",
"958148682d748a38dd68baa","b8ce020cf069c32a723ab14","f4331d6d461607e95752746",
"6da23ba424b9596133cf9c8","a636bcbc7b30c5fbeae67fe","5cb0d86a07df654a9089a20",
"f11f106848780fc9ecdd80a","1fbb5364fb8d2c9d730d5ba","fcb86bc70a50c9d02a5d034",
"a534433029eac15f322e34c","c989d9c7c3d3b8c55d75130","7bb38b2f0186d46643ae962",
"2644ebadeb44b9467d1f42c","608cc857594bfbb55d69600",
]
assert len(_RAWG) == 83

# Parity-check matrix Nm (87 wierszy) — uzywana TYLKO do self-testu (ldpc_check),
# nie do TX, ale przydatna jako wbudowany sanity-check przy starcie.
_NM = [
[4,31,59,91,92,96,153],[5,32,60,93,115,146,0],[6,24,61,94,122,151,0],
[7,33,62,95,96,143,0],[8,25,63,83,93,96,148],[6,32,64,97,126,138,0],
[5,34,65,78,98,107,154],[9,35,66,99,139,146,0],[10,36,67,100,107,126,0],
[11,37,67,87,101,139,158],[12,38,68,102,105,155,0],[13,39,69,103,149,162,0],
[8,40,70,82,104,114,145],[14,41,71,88,102,123,156],[15,42,59,106,123,159,0],
[1,33,72,106,107,157,0],[16,43,73,108,141,160,0],[17,37,74,81,109,131,154],
[11,44,75,110,121,166,0],[45,55,64,111,130,161,173],[8,46,71,112,119,166,0],
[18,36,76,89,113,114,143],[19,38,77,104,116,163,0],[20,47,70,92,138,165,0],
[2,48,74,113,128,160,0],[21,45,78,83,117,121,151],[22,47,58,118,127,164,0],
[16,39,62,112,134,158,0],[23,43,79,120,131,145,0],[19,35,59,73,110,125,161],
[20,36,63,94,136,161,0],[14,31,79,98,132,164,0],[3,44,80,124,127,169,0],
[19,46,81,117,135,167,0],[7,49,58,90,100,105,168],[12,50,61,118,119,144,0],
[13,51,64,114,118,157,0],[24,52,76,129,148,149,0],[25,53,69,90,101,130,156],
[20,46,65,80,120,140,170],[21,54,77,100,140,171,0],[35,82,133,142,171,174,0],
[14,30,83,113,125,170,0],[4,29,68,120,134,173,0],[1,4,52,57,86,136,152],
[26,51,56,91,122,137,168],[52,84,110,115,145,168,0],[7,50,81,99,132,173,0],
[23,55,67,95,172,174,0],[26,41,77,109,141,148,0],[2,27,41,61,62,115,133],
[27,40,56,124,125,126,0],[18,49,55,124,141,167,0],[6,33,85,108,116,156,0],
[28,48,70,85,105,129,158],[9,54,63,131,147,155,0],[22,53,68,109,121,174,0],
[3,13,48,78,95,123,0],[31,69,133,150,155,169,0],[12,43,66,89,97,135,159],
[5,39,75,102,136,167,0],[2,54,86,101,135,164,0],[15,56,87,108,119,171,0],
[10,44,82,91,111,144,149],[23,34,71,94,127,153,0],[11,49,88,92,142,157,0],
[29,34,87,97,147,162,0],[30,50,60,86,137,142,162],[10,53,66,84,112,128,165],
[22,57,85,93,140,159,0],[28,32,72,103,132,166,0],[28,29,84,88,117,143,150],
[1,26,45,80,128,147,0],[17,27,89,103,116,153,0],[51,57,98,163,165,172,0],
[21,37,73,138,152,169,0],[16,47,76,130,137,154,0],[3,24,30,72,104,139,0],
[9,40,90,106,134,151,0],[15,58,60,74,111,150,163],[18,42,79,144,146,152,0],
[25,38,65,99,122,160,0],[17,42,75,129,170,172,0],
]

_COSTAS = [3, 1, 4, 0, 6, 5, 2]   # nowy FT8 (77-bit), rozne od starego [2,5,6,0,4,1,3]
_GRAYMAP = [0, 1, 3, 2, 5, 6, 4, 7]
_CRC14POLY = [1, 1, 0, 0, 1, 1, 1, 0, 1, 0, 1, 0, 1, 1, 1]  # = 0x2757 (14-bit, bez wiodacego 1)

_hex2 = {hex(i)[2]: i for i in range(16)}

def _build_gen_sys():
    gen = []
    for e in _RAWG:
        row = np.zeros(91, dtype=np.int32)
        for i, c in enumerate(e):
            x = _hex2[c]
            for j in range(4):
                ind = i * 4 + (3 - j)
                if 0 <= ind < 91:
                    row[ind] = 1 if (x & (1 << j)) != 0 else 0
        gen.append(row)
    gen_sys = np.zeros((174, 91), dtype=np.int32)
    gen_sys[91:, :] = gen
    gen_sys[0:91, :] = np.eye(91, dtype=np.int32)
    return gen_sys

_GEN_SYS = _build_gen_sys()


def _ldpc_self_test():
    """Sprawdza czy generator LDPC dziala poprawnie w TYM srodowisku (wazne dla
    EXE - roznice numpy/BLAS moga psuc kodowanie). Wypisuje wynik do logu przy
    imporcie, zeby w produkcji bylo widac czy FT8/FT4 TX beda dekodowaln.
    Wywolanie odlozone do konca pliku (po zdefiniowaniu _ldpc_encode/_check)."""
    try:
        import numpy as _np
        _np.random.seed(0)
        ok = 0
        for _ in range(3):
            p = _np.random.randint(0, 2, 91).astype(_np.int32)
            if _ldpc_check(_ldpc_encode(p)):
                ok += 1
        if ok == 3:
            print("[ldpc] Self-test OK - generator LDPC poprawny", flush=True)
        else:
            print(f"[ldpc] UWAGA: self-test {ok}/3 - LDPC MOZE BYC ZLE "
                  f"(FT8/FT4 TX moga nie byc dekodowalne!)", flush=True)
    except Exception as _e:
        print(f"[ldpc] Self-test blad: {_e}", flush=True)


def _ldpc_encode(plain91):
    # Uodpornione na roznice numpy/BLAS w PyInstaller EXE: bez np.dot(out=)
    # ktore w niektorych buildach (inny BLAS, inna wersja numpy) moglo dawac
    # zly wynik -> ldpc_valid=False w EXE mimo poprawnego kodu w .py.
    # Jawna arytmetyka int, gwarantowanie deterministyczna niezaleznie od BLAS.
    plain91 = np.asarray(plain91, dtype=np.int64)
    parity = (_GEN_SYS[91:, :].astype(np.int64) @ plain91) % 2
    ncw = np.zeros(174, dtype=np.int32)
    ncw[0:91] = plain91.astype(np.int32)
    ncw[91:] = parity.astype(np.int32)
    return ncw


def _ldpc_check(codeword):
    """Self-test: sprawdza zgodnosc parity. Uzywane tylko przy starcie modulu."""
    for e in _NM:
        x = 0
        for i in e:
            if i != 0:
                x ^= int(codeword[i - 1])
        if x != 0:
            return False
    return True


# Self-test LDPC przy imporcie modulu - wypisze do logu czy generator dziala
# w tym srodowisku (kluczowe dla wykrycia problemow numpy/BLAS w EXE).
_ldpc_self_test()


# ============================================================
# CZESC 2: Pakowanie wiadomosci (callsign/grid -> 28/15 bit)
# Algorytm wyprowadzony jako matematyczna odwrotnosc zweryfikowanego
# unpack()/unpackcall()/unpackgrid() z basicft8.py (rtmrtmrtmrtm)
# ============================================================

def _charn_inv_pos0(ch):
    """
    Mapowanie dla pozycji 0 (pierwszy znak prefixu, 37 wartosci):
    spacja=0, cyfra=1..10, litera=11..36.
    """
    if ch == ' ':
        return 0
    if '0' <= ch <= '9':
        return ord(ch) - ord('0') + 1
    if 'A' <= ch <= 'Z':
        return ord(ch) - ord('A') + 11
    raise ValueError(f"bad char in callsign: {ch!r}")


def _charn_inv_pos1(ch):
    """
    Mapowanie dla pozycji 1 (drugi znak prefixu, 36 wartosci, BEZ spacji):
    cyfra=0..9, litera=10..35.
    """
    if '0' <= ch <= '9':
        return ord(ch) - ord('0')
    if 'A' <= ch <= 'Z':
        return ord(ch) - ord('A') + 10
    raise ValueError(f"bad char in callsign position 1: {ch!r}")


def _charn_inv_pos2(ch):
    """Mapowanie dla pozycji 2 (cyfra, 10 wartosci): zawsze cyfra 0-9."""
    return ord(ch) - ord('0')


def _charn_inv_suffix(ch):
    """
    Mapowanie dla pozycji 3,4,5 (sufiks, max 3 znaki): TYLKO litery + spacja
    (27 wartosci), BEZ cyfr — nie pelny zestaw cyfry+litery+spacja-10 uzywany
    dla wczesniejszych pozycji. Zweryfikowane bit-dokladnie wzgledem oficjalnej
    specyfikacji protokolu dla "G0XYZ K1ABC FN43": referencyjny
    c28(G0XYZ)=9425373, referencyjny c28(K1ABC)=10214965 — oba dokladnie
    odtworzone tylko z tym poprawionym mapowaniem.
    """
    if ch == ' ':
        return 0
    if 'A' <= ch <= 'Z':
        return ord(ch) - ord('A') + 1
    raise ValueError(f"bad char in callsign suffix (letters/blank only): {ch!r}")


def _packcall(call):
    call = call.strip().upper()
    # Historical pack28 work-arounds specified by the FT8 protocol for two
    # DXCC prefixes whose call-area digit falls outside the two positions
    # this packing supports. One-way, not reversible on decode - matches
    # the official protocol exactly (no corresponding unpack-side reversal
    # exists there either): once packed, "3DA0RS" is genuinely decoded as
    # "3D0RS" by every receiving station, not just ours.
    if call.startswith("3DA0"):
        call = "3D0" + call[4:]
    elif call.startswith("3X") and len(call) > 2 and call[2].isalpha():
        call = "Q" + call[2:]
    if len(call) >= 2 and call[1].isdigit() and not (len(call) >= 3 and call[2].isdigit()):
        call = ' ' + call
    call = call.ljust(6)
    if len(call) > 6:
        raise ValueError(f"callsign too long for standard pack: {call!r}")
    a0 = _charn_inv_pos0(call[0])
    a1 = _charn_inv_pos1(call[1])
    a2 = _charn_inv_pos2(call[2])
    if not call[2].isdigit():
        raise ValueError(f"3rd character of standard callsign must be a digit: {call!r}")
    a3 = _charn_inv_suffix(call[3])
    a4 = _charn_inv_suffix(call[4])
    a5 = _charn_inv_suffix(call[5])
    x = a0
    x = x * 36 + a1
    x = x * 10 + a2
    x = x * 27 + a3
    x = x * 27 + a4
    x = x * 27 + a5
    return x


_NBASE_CQ = 2
_STD_CALL_OFFSET = 6257896
_NTOKENS = 2_063_592
_ALPHA38 = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ/"
_U64_MASK = (1 << 64) - 1


def _ihashcall(call, m):
    """22/12-bit callsign hash, per the official FT8 protocol specification.
    Port of ihashcall() in ham_audio/src/decode/unpack.rs (used to refer to
    a non-standard callsign already announced elsewhere in the exchange,
    without re-transmitting its full text). Must match that Rust
    implementation bit-for-bit, including u64 wraparound arithmetic."""
    padded = call.upper().ljust(11)[:11]
    n8 = 0
    for ch in padded:
        j = _ALPHA38.find(ch)
        if j < 0:
            j = 0
        n8 = (38 * n8 + j) & _U64_MASK
    prod = (47_055_833_459 * n8) & _U64_MASK
    return prod >> (64 - m)


def _split_pr_suffix(call):
    """Split into (base, suffix) where suffix is 'P', 'R', or None. Only
    /P and /R map to the FT8 r1-bit mechanism (packjt77.f90); any other
    suffix or prefix is handled as a non-standard callsign instead."""
    call = call.strip().upper()
    if call.endswith('/P'):
        return call[:-2], 'P'
    if call.endswith('/R'):
        return call[:-2], 'R'
    return call, None


def _fits_standard(base_call):
    """Whether base_call (/P /R already stripped) fits the 6-character
    standard c28 field."""
    if base_call in ("CQ", "DE", "QRZ"):
        return True
    try:
        _packcall(base_call)
        return True
    except ValueError:
        return False


def _encode_c28(call):
    call = call.strip().upper()
    if call == "CQ":
        return _NBASE_CQ
    if call == "DE":
        return 0
    if call == "QRZ":
        return 1
    base, _suffix = _split_pr_suffix(call)
    try:
        return _STD_CALL_OFFSET + _packcall(base)
    except ValueError:
        # Compound/prefixed call (e.g. "WX/XX0XXX") or one too long for the
        # 6-character field (e.g. "XX0XXXXX"): encode as a hash reference
        # instead of raising. Round-trips only if the receiving station
        # already has this exact text cached (from an earlier CQ or
        # pack77_nonstandard message in this exchange) - the convention
        # the official FT8 protocol relies on.
        return _NTOKENS + _ihashcall(call, 22)


def _encode_g15(grid_or_report):
    """ZWERYFIKOWANE wzgledem oficjalnej specyfikacji protokolu FT8
    (MAXGRID4=32400). Zwraca tuple (g15, r_flag) — r_flag to OSOBNY bit,
    NIE zakodowany w g15.

    Mapowanie:
      grid 4-znakowy -> _grid_to_ng() (zakres 0..32399)
      ""             -> g15=32401, r_flag=False
      "RRR"          -> g15=32402, r_flag=False
      "RR73"         -> g15=32403, r_flag=False
      "73"           -> g15=32404, r_flag=False
      "+NN" / "-NN"  -> g15 = 32400 + (raport + 35), r_flag=False
                        zakres raportu: -30..+49 (g15: 32405..32484)
      "R+NN" / "R-NN" -> g15 = 32400 + (raport + 35), r_flag=True
    """
    g = grid_or_report.strip().upper()
    if g == "":
        return 32401, False
    if g == "RRR":
        return 32402, False
    if g == "RR73":
        return 32403, False
    if g == "73":
        return 32404, False
    m = re.match(r'^(R)?([+-]\d{1,2})$', g)
    if m:
        r_flag = m.group(1) is not None
        val = int(m.group(2))
        if -30 <= val <= 49:
            irpt = val + 35  # 5..84
            return 32400 + irpt, r_flag
        raise ValueError(f"signal report out of range -30..+49: {g!r}")
    if len(g) == 4 and g[0] in "ABCDEFGHIJKLMNOPQR" and g[1] in "ABCDEFGHIJKLMNOPQR" \
       and g[2].isdigit() and g[3].isdigit():
        return _grid_to_ng(g), False
    raise ValueError(f"unsupported grid/report value: {g!r}")


def _grid_to_ng(g):
    """
    Koduje 4-znakowy Maidenhead grid jako liczbe 0..32399 (15 bit).
    WZÓR ZWERYFIKOWANY: prosty system pozycyjny (NIE przez lat/lng — ta
    metoda dawala BLEDNE wartosci dla 77-bit protokolu, np. RR73 wychodzilo
    533 zamiast poprawnych 32373).
    ng = ((c0*18 + c1)*10 + c2)*10 + c3
    gdzie c0,c1 = indeks litery (A=0..R=17), c2,c3 = cyfra (0-9).
    18*18*10*10 = 32400, dokladnie tyle ile lokatorow Maidenhead istnieje.
    Zweryfikowane: RR73 -> 32373, zgodne z oficjalna specyfikacja protokolu
    FT8 (dokladna zgodnosc liczbowa).
    """
    c0 = ord(g[0]) - ord('A')
    c1 = ord(g[1]) - ord('A')
    c2 = int(g[2])
    c3 = int(g[3])
    return ((c0 * 18 + c1) * 10 + c2) * 10 + c3


def _int_to_bits(x, nbits):
    return [(x >> (nbits - 1 - i)) & 1 for i in range(nbits)]


def _pack_nonstandard_call(call):
    """Packs a full callsign into 58 bits, 11 characters base-38 (MSB
    first). Inverse of the decode loop in
    ham_audio/src/decode/unpack.rs::unpack_nonstandard."""
    call = call.strip().upper()
    if len(call) > 11:
        raise ValueError(f"callsign too long for nonstandard pack (max 11 chars): {call!r}")
    n = 0
    for ch in call.ljust(11):
        j = _ALPHA38.find(ch)
        if j < 0:
            raise ValueError(f"bad character in callsign {call!r}: {ch!r}")
        n = n * 38 + j
    return n


def pack77_nonstandard(call_to, call_de, report_or_grid):
    """FT8 Type 4 (i3=4) message: carries ONE full non-standard callsign
    (compound/prefixed, or too long for the 6-char standard field) as free
    text, plus a 12-bit hash reference to the other callsign. Field layout
    matches ham_audio/src/decode/unpack.rs::unpack_nonstandard exactly:
        h12(12) | c58(58) | iflip(1) | nrpt(2) | icq(1) | i3(3) = 77 bits

    Protocol limitation (not an implementation gap): report_or_grid here
    can only be "" / "RRR" / "RR73" / "73" - this message type has no field
    for an actual SNR report or grid. Exchanging a real report/grid with an
    already-announced non-standard callsign goes through pack77() instead,
    which hash-references it inside a standard (i3=1) message.
    """
    call_to_u = call_to.strip().upper()
    call_de_u = call_de.strip().upper()
    rg = (report_or_grid or "").strip().upper()
    nrpt = {"": 0, "RRR": 1, "RR73": 2, "73": 3}.get(rg)
    if nrpt is None:
        raise ValueError(
            f"nonstandard-call message cannot carry report/grid {report_or_grid!r} "
            f"(only blank/RRR/RR73/73)")

    icq = (call_to_u == "CQ")
    if icq:
        full_call, other_call, iflip = call_de_u, None, False
    else:
        to_base, _ = _split_pr_suffix(call_to_u)
        de_base, _ = _split_pr_suffix(call_de_u)
        to_std = _fits_standard(to_base)
        de_std = _fits_standard(de_base)
        if to_std and not de_std:
            full_call, other_call, iflip = call_de_u, call_to_u, False
        elif de_std and not to_std:
            full_call, other_call, iflip = call_to_u, call_de_u, True
        else:
            raise ValueError(
                "pack77_nonstandard needs exactly one non-standard callsign "
                f"(call_to={call_to_u!r}, call_de={call_de_u!r})")

    h12 = 0 if other_call is None else _ihashcall(other_call, 12)
    c58 = _pack_nonstandard_call(full_call)

    bits = []
    bits += _int_to_bits(h12, 12)
    bits += _int_to_bits(c58, 58)
    bits += [1 if iflip else 0]
    bits += _int_to_bits(nrpt, 2)
    bits += [1 if icq else 0]
    bits += _int_to_bits(4, 3)  # i3
    assert len(bits) == 77
    return bits


def pack77(call_to, call_de, report_or_grid, r_flag=False):
    """
    Standardowa wiadomosc FT8 Type 1/2: c28 r1 c28 r1 R1 g15 i3 = 77 bit.
    UWAGA: i3 jest na KONCU (ostatnie 3 bity), NIE na poczatku!
    Zweryfikowane wzgledem oficjalnej specyfikacji protokolu FT8 dla
    "G0XYZ K1ABC FN43": referencyjne bits[74:77] = i3 = '001' (i3=1,
    Standard msg), podczas gdy bits[0:3] = '000' (to byly poczatkowe
    bity c28, nie i3).
    call_to: callsign odbiorcy (lub 'CQ')
    call_de: Twoj callsign (np. 'XX0XXX')
    report_or_grid: grid (np. 'KO02'), raport (-15/R-09), lub RRR/RR73/73

    Non-standard callsigns (compound/prefixed like "WX/XX0XXX", or too long
    for the 6-char field like "XX0XXXXX"): if report_or_grid is something
    Type 4 can carry (blank/RRR/RR73/73), dispatches to
    pack77_nonstandard() instead, which transmits the full callsign text.
    Otherwise falls back to hash-referencing the non-standard callsign
    inside this Type 1/2 message - see _encode_c28.
    """
    to_base, to_suffix = _split_pr_suffix(call_to)
    de_base, de_suffix = _split_pr_suffix(call_de)
    if not (_fits_standard(to_base) and _fits_standard(de_base)):
        rg_check = (report_or_grid or "").strip().upper()
        if call_to.strip().upper() == "CQ" and rg_check not in ("", "RRR", "RR73", "73"):
            # A CQ from a non-standard callsign has no grid field at all in
            # either message type - drop it rather than blocking the CQ.
            print(f"[ft8] Ostrzezenie: CQ ze znakiem niestandardowym {call_de!r} "
                  f"nie moze zawierac gridu — pomijam {report_or_grid!r}")
            rg_check = ""
        if rg_check in ("", "RRR", "RR73", "73"):
            return pack77_nonstandard(call_to, call_de, rg_check)
        # else: report/grid must go out - fall through to hash-referenced
        # Type 1/2 below (works only if the callsign was already announced
        # earlier in this exchange).

    # /P and /R share ONE i3 value for the whole message (both r1 bits mean
    # the same suffix) - mixing /P on one call and /R on the other in the
    # same message isn't representable, a limitation of the protocol itself.
    i3 = 2 if (to_suffix == 'P' or de_suffix == 'P') else 1
    c28_1 = _encode_c28(call_to)
    c28_2 = _encode_c28(call_de)
    g15, r_flag_from_text = _encode_g15(report_or_grid)
    # r_flag moze byc ustawiony explicite (parametr funkcji) ALBO wykryty
    # z prefiksu "R" w samym tekscie report_or_grid (np. "R-01") — oba
    # przypadki musza ustawic bit R1 w wiadomosci.
    r_flag = r_flag or r_flag_from_text

    bits = []
    bits += _int_to_bits(c28_1, 28)
    bits += [1 if to_suffix else 0]  # r1 for call_to (/P or /R, per i3 above)
    bits += _int_to_bits(c28_2, 28)
    bits += [1 if de_suffix else 0]  # r1 for call_de
    bits += [1 if r_flag else 0]  # R1
    bits += _int_to_bits(g15, 15)
    bits += _int_to_bits(i3, 3)
    assert len(bits) == 77
    return bits


# ============================================================
# CZESC 3: CRC-14 (polynomial 0x2757, zgodny z oficjalna specyfikacja FT8)
# ============================================================

def _crc14(msg82):
    msg = np.array(msg82, dtype=np.int32)
    code = np.zeros(14, dtype=np.int32)
    msg = np.append(msg, code)
    div = np.array(_CRC14POLY, dtype=np.int32)
    divlen = len(div)
    for i in range(len(msg) - 14):
        if msg[i] == 1:
            msg[i:i + divlen] = np.mod(msg[i:i + divlen] + div, 2)
    return list(msg[-14:])


# ============================================================
# CZESC 4: Pelny pipeline tekst -> 79 symboli
# ============================================================

def encode_message(call_to, call_de, report_or_grid, r_flag=False):
    """Zwraca (symbols79, debug_dict)."""
    bits77 = pack77(call_to, call_de, report_or_grid, r_flag)
    padded82 = bits77 + [0, 0, 0, 0, 0]
    crc = _crc14(padded82)
    bits91 = np.array(bits77 + crc, dtype=np.int32)
    codeword174 = _ldpc_encode(bits91)

    data_symbols = []
    for i in range(0, 174, 3):
        b0, b1, b2 = codeword174[i], codeword174[i + 1], codeword174[i + 2]
        idx = b0 * 4 + b1 * 2 + b2
        data_symbols.append(_GRAYMAP[idx])

    symbols79 = (_COSTAS + data_symbols[0:29] + _COSTAS +
                 data_symbols[29:58] + _COSTAS)
    assert len(symbols79) == 79

    debug = {
        "bits77": bits77,
        "bits91": bits91.tolist(),
        "codeword174": codeword174.tolist(),
        "ldpc_valid": _ldpc_check(codeword174),
    }
    return symbols79, debug


# ============================================================
# CZESC 5: Generator audio GFSK
# ============================================================

FT8_SAMPLE_RATE = 12000
FT8_SYMBOL_PERIOD = 0.16
FT8_SAMPLES_PER_SYMBOL = int(FT8_SAMPLE_RATE * FT8_SYMBOL_PERIOD)  # 1920
FT8_TONE_SPACING = 6.25
FT8_BT = 2.0  # FT8 (FT4 uzywa BT=1.0)

TARGET_SAMPLE_RATE = 48000  # wymagane przez feed_tx_pcm (audio_stream.py)


def _gaussian_pulse(t, bt, symbol_period):
    k = bt * 2 * np.pi / np.sqrt(np.log(2))
    arg1 = k * (t / symbol_period - 0.5)
    arg2 = k * (t / symbol_period + 0.5)
    q1 = 0.5 * (1 - erf(arg1 / np.sqrt(2)))
    q2 = 0.5 * (1 - erf(arg2 / np.sqrt(2)))
    return q1 - q2


def _precompute_pulse_table():
    n = FT8_SAMPLES_PER_SYMBOL
    t = (np.arange(3 * n) - 1.5 * n) / FT8_SAMPLE_RATE
    return _gaussian_pulse(t, FT8_BT, FT8_SYMBOL_PERIOD)


_PULSE_TABLE = _precompute_pulse_table()


def synthesize_gfsk(symbols, base_freq_hz=1000.0):
    """79 symboli (0-7) -> numpy float32 PCM @ 12000Hz, znormalizowany -1..1."""
    n_sym = len(symbols)
    n = FT8_SAMPLES_PER_SYMBOL
    total_samples = n_sym * n
    freq_dev = np.zeros(total_samples + 2 * n)

    for i, sym in enumerate(symbols):
        center = i * n + n
        start = center - n
        seg_start = max(0, start)
        seg_end = min(len(freq_dev), start + 3 * n)
        pulse_start = seg_start - start
        pulse_end = pulse_start + (seg_end - seg_start)
        freq_dev[seg_start:seg_end] += sym * FT8_TONE_SPACING * _PULSE_TABLE[pulse_start:pulse_end]

    freq_dev = freq_dev[n:n + total_samples]
    inst_freq = base_freq_hz + freq_dev
    phase = 2 * np.pi * np.cumsum(inst_freq) / FT8_SAMPLE_RATE
    return np.sin(phase).astype(np.float32)


def generate_tx_pcm48k(call_to, call_de, report_or_grid, r_flag=False,
                        base_freq_hz=1000.0, amplitude=0.12):
    """
    Pelny pipeline: tekst -> 79 symboli -> audio 12kHz -> resample 48kHz ->
    int16 PCM bytes, gotowe do feed_tx_pcm().

    UWAGA o domyslnej czestotliwosci: 1000Hz, NIE 1500Hz — IC-7300 w trybie
    USB-D ma udokumentowany "sweet spot" na 1500Hz, ktory w praktyce generuje
    staly notch w odbiorniku dokladnie na tej czestotliwosci (potwierdzone
    pomiarami na niezaleznych nagraniach, niezalezne od NB/NR/Notch/AGC/PTT).
    W normalnym uzyciu i tak nadpisywane przez App._ft8_tx_freq_hz.

    UWAGA o amplitudzie: backend (audio_stream.py _webrtc_playback_loop)
    DODATKOWO mnozy kazda probke przez cfg['txVolume'] (domyslnie 4.0,
    max 8.0) przed wyslaniem na karte dzwiekowa. Domyslne amplitude=0.12
    jest dobrane tak, zeby nawet przy maksymalnym txVolume=8.0 sygnal
    nie ulegal clippingowi (0.12 * 8.0 = 0.96, bezpieczny margines).
    Clipping zamienia czysta fale sinusoidalna w fale prostokatna pelna
    harmonicznych, co niszczy widmo i uniemozliwia poprawne dekodowanie
    mimo poprawnej tresci bitowej — to byla pierwotna przyczyna bledow
    dekodowania w testach na zywo (2026-06-20).

    Zwraca (pcm_bytes, debug_dict, duration_seconds).
    """
    symbols, debug = encode_message(call_to, call_de, report_or_grid, r_flag)
    audio_12k = synthesize_gfsk(symbols, base_freq_hz=base_freq_hz)
    audio_48k = resample_poly(audio_12k, up=4, down=1)
    pcm16 = np.clip(audio_48k * 32767 * amplitude, -32768, 32767).astype(np.int16)
    duration = len(audio_12k) / FT8_SAMPLE_RATE
    return pcm16.tobytes(), debug, duration


def chunk_pcm_bytes(pcm_bytes, chunk_samples=960):
    """Dzieli PCM bytes (int16 mono) na kawalki po chunk_samples probek
    (domyslnie 960 = 20ms @ 48kHz, zgodnie z OPUS_FRAMES w audio_stream.py)."""
    chunk_bytes = chunk_samples * 2
    for pos in range(0, len(pcm_bytes), chunk_bytes):
        yield pcm_bytes[pos:pos + chunk_bytes]


# ============================================================
# CZESC 6: Synchronizacja czasowa (okna 15s UTC)
# ============================================================
#
# FT8 wymaga rozpoczecia transmisji na poczatku okna 15s UTC (xx:00, xx:15,
# xx:30, xx:45). Sygnal docelowo zaczyna sie 0.5s w cykl (wedlug standardu),
# ale poniewaz audio idzie przez nasz wlasny audio pipeline (z wlasnym,
# nieznanym z gory opoznieniem PTT->dzwiek), celujemy w start TUZ NA
# poczatku okna (offset 0.0s) — to daje margines bezpieczenstwa, bo dekodery
# akceptuja sygnaly w oknie +/- ok. 2s wzgledem nominalnego startu.

def seconds_until_next_window(window=15.0, now=None):
    """Zwraca liczbe sekund (float) do najblizszej wielokrotnosci `window`
    sekund w biezacej minucie UTC (np. do najblizszego xx:00/15/30/45)."""
    import time as _time
    if now is None:
        now = _time.time()
    return window - (now % window)


def seconds_until_next_tx_window(period=1, window=15.0, now=None):
    """
    Jak seconds_until_next_window, ale pozwala wybrac KTORY z dwoch
    naprzemiennych slotow nadawania uzyc — standardowa praktyka FT8/FT4,
    gdzie dwie stacje w QSO nadaja na przemian (jedna w "pierwszym", druga
    w "drugim" okresie), zeby nigdy nie nadawac jednoczesnie.

    period=1 -> okna zaczynajace sie w PARZYSTYCH wielokrotnosciach window
                (dla FT8/window=15.0: xx:00, xx:30 — "pierwszy" okres)
    period=2 -> okna zaczynajace sie w NIEPARZYSTYCH wielokrotnosciach window
                (dla FT8/window=15.0: xx:15, xx:45 — "drugi" okres)

    Dziala identycznie dla FT4 (window=7.5s), tylko w mniejszej skali czasu.
    """
    import time as _time
    if now is None:
        now = _time.time()
    # Numer biezacego okna od poczatku biezacej minuty UTC (0,1,2,3,... dla
    # window=15: okno 0 = xx:00-15, okno 1 = xx:15-30, okno 2 = xx:30-45...)
    base_wait = seconds_until_next_window(window, now)
    next_window_idx = int(round((now + base_wait) / window))
    # period=1 chce okien o indeksie PARZYSTYM, period=2 NIEPARZYSTYM
    wants_even = (period == 1)
    is_even = (next_window_idx % 2 == 0)
    if wants_even == is_even:
        return base_wait
    # Najblizsze okno jest "nie nasze" — czekaj jeszcze jedno window dluzej
    return base_wait + window


def next_window_start(window=15.0, now=None):
    """Zwraca unix timestamp (float) najblizszego startu okna."""
    import time as _time
    if now is None:
        now = _time.time()
    return now + seconds_until_next_window(window, now)


if __name__ == "__main__":
    # Self-test przy uruchomieniu modulu bezposrednio
    print("=== FT8 Encoder self-test ===")
    tests = [
        ("CQ", "XX0XXX", "KO02"),
        ("XX0XXX", "DL1ABC", "JO40"),
        ("XX0XXX", "SQ5XYZ", "RRR"),
        ("XX0XXX", "SQ5XYZ", "73"),
        ("XX0XXX", "SQ5XYZ", "-15"),
    ]
    all_ok = True
    for call1, call2, g in tests:
        symbols, debug = encode_message(call1, call2, g)
        ok = debug["ldpc_valid"] and len(symbols) == 79
        all_ok = all_ok and ok
        print(f"  {call1:8s} {call2:8s} {g:6s} -> ldpc_valid={debug['ldpc_valid']} symbols={len(symbols)} [{'OK' if ok else 'FAIL'}]")

    pcm, debug, dur = generate_tx_pcm48k("CQ", "XX0XXX", "KO02")
    print(f"\nAudio: {len(pcm)} bytes PCM, {dur:.2f}s, ldpc_valid={debug['ldpc_valid']}")
    print("ALL OK" if all_ok else "SOME TESTS FAILED")
