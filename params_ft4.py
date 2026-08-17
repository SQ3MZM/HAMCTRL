"""
Parametry protokolu FT4 (zgodne z naszym ft4_encoder.py, zweryfikowanym
matematycznie/strukturalnie wzgledem oficjalnej specyfikacji protokolu
FT4 — patrz ft4_protocol_notes.md).
"""
SAMPLE_RATE = 12000
SYMBOL_PERIOD = 0.048           # s, czas trwania jednego symbolu (48ms)
TONE_SPACING = 20.8333333333    # Hz, odstep miedzy 4 tonami GFSK (12000/576)
SAMPLES_PER_SYMBOL = int(round(SYMBOL_PERIOD * SAMPLE_RATE))  # 576
N_TONES = 4
N_SYM = 103                     # 103 symbole: 4x Costas(4) + 3x dane(29)

# CZTERY rozne wzorce Costas (w przeciwienstwie do FT8, ktory ma JEDEN
# wzorzec powtorzony 3x). To kluczowa roznica strukturalna wymagajaca
# innego algorytmu wyszukiwania synchronizacji w sync.py.
COSTAS_PATTERNS = [
    [0, 1, 3, 2],   # Sync1
    [1, 0, 2, 3],   # Sync2
    [2, 3, 1, 0],   # Sync3
    [3, 2, 0, 1],   # Sync4
]
COSTAS_POS = [0, 33, 66, 99]    # pozycje startowe (0-indexed) kazdego bloku

SLOT_PERIOD = 7.5                # s, dlugosc okna czasowego FT4 (vs 15.0 FT8)

# Maska scramblingu — 77 bitow, XOR z pack77() PRZED CRC. Identyczna z
# ft4_encoder.py::_RVEC (musi pozostac w 1:1 zgodzie — uzywana do
# de-scramblingu po LDPC decode).
RVEC = [
    0, 1, 0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 1, 1, 1, 0, 1, 0, 0, 0, 1, 0, 0, 1, 1, 0, 1, 1, 0,
    1, 0, 0, 1, 0, 1, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 1, 1, 1, 1, 0, 0, 1, 0, 1,
    0, 1, 0, 1, 0, 1, 1, 0, 1, 1, 1, 1, 1, 0, 0, 0, 1, 0, 1
]
assert len(RVEC) == 77

# Gray mapping odwrotny (ton -> 2-bitowy indeks), do dekodowania symboli
# z powrotem na bity. graymap[idx]=ton podczas TX, wiec INV_GRAYMAP[ton]=idx.
GRAYMAP = [0, 1, 3, 2]
INV_GRAYMAP = [0] * 4
for _idx, _tone in enumerate(GRAYMAP):
    INV_GRAYMAP[_tone] = _idx
