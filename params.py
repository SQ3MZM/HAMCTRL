"""
Parametry protokolu FT8 (zgodne z naszym ft8_encoder.py, ktory jest juz
zweryfikowany bit-dokladnie przeciwko prawdziwemu ft8code.exe).
"""
SAMPLE_RATE = 12000
SYMBOL_PERIOD = 0.160          # s, czas trwania jednego symbolu
TONE_SPACING = 6.25            # Hz, odstep miedzy 8 tonami GFSK
SAMPLES_PER_SYMBOL = int(round(SYMBOL_PERIOD * SAMPLE_RATE))  # 1920
N_TONES = 8
N_SYM = 79                     # 79 symboli na ramke (7 Costas + 58 danych + 14 Costas... w 3 blokach)
COSTAS = [3, 1, 4, 0, 6, 5, 2]  # wzorzec synchronizacji
# Costas wystepuje 3x: na poczatku (sym 0-6), w srodku (sym 36-42), na koncu (sym 72-78)
COSTAS_POS = [0, 36, 72]
SLOT_PERIOD = 15.0             # s, dlugosc okna czasowego FT8
