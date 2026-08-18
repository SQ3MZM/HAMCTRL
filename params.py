"""
FT8 protocol parameters (matching our ft8_encoder.py, which has already
been verified bit-exact against the real ft8code.exe).
"""
SAMPLE_RATE = 12000
SYMBOL_PERIOD = 0.160          # s, duration of one symbol
TONE_SPACING = 6.25            # Hz, spacing between the 8 GFSK tones
SAMPLES_PER_SYMBOL = int(round(SYMBOL_PERIOD * SAMPLE_RATE))  # 1920
N_TONES = 8
N_SYM = 79                     # 79 symbols per frame (7 Costas + 58 data + 14 Costas... across 3 blocks)
COSTAS = [3, 1, 4, 0, 6, 5, 2]  # sync pattern
# Costas occurs 3x: at the start (sym 0-6), in the middle (sym 36-42), at the end (sym 72-78)
COSTAS_POS = [0, 36, 72]
SLOT_PERIOD = 15.0             # s, FT8 time slot length
