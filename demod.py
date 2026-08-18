"""
Stage 2: Demodulation - from audio + a candidate position (freq_hz,
time_offset_s) we extract 79 symbols (hard decision, for fast sync
verification) and 174 soft LLRs (for full LDPC belief-propagation later).

Bit<->symbol mapping: the 79 symbols contain 3x7=21 Costas sync symbols
(ignored when decoding data) + 58 data symbols. Each data symbol encodes
3 Gray-mapped bits from the 174-bit LDPC(174,91) code. Matches our encoder
(ft8_encoder.py): the _SYMBOL_GRAY Gray-coding table and the Costas
placement at positions [0:7], [36:43], [72:79].
"""
import numpy as np
from params import SAMPLE_RATE, SAMPLES_PER_SYMBOL, N_TONES, COSTAS, COSTAS_POS, N_SYM

# EXACTLY the same table as in ft8_encoder.py (_GRAYMAP), verified by
# reading the source. idx (3-bit value 0-7) -> tone (transmitted symbol).
GRAYMAP = [0, 1, 3, 2, 5, 6, 4, 7]
# Inverse: tone -> idx (3-bit value), needed for decoding
GRAYMAP_INV = [0] * 8
for _idx, _tone in enumerate(GRAYMAP):
    GRAYMAP_INV[_tone] = _idx

# Positions of data symbols in the 79-symbol frame (after removing the
# 3x7=21 Costas symbols):
# symbols79 = Costas[0:7] + data[0:29] + Costas[36:43] + data[29:58] + Costas[72:79]
DATA_SYM_INDICES = list(range(7, 36)) + list(range(43, 72))
assert len(DATA_SYM_INDICES) == 58


def extract_tone_power(audio, freq_hz, time_offset_s, freq_osr=2):
    """
    For a given position (freq_hz = frequency of tone 0, time_offset_s =
    start of the first symbol), extracts the power matrix [79 symbols x 8
    tones] via correlation with pure tones (Goertzel-like, via FFT on each
    symbol window).
    """
    start_sample = int(round(time_offset_s * SAMPLE_RATE))
    n = SAMPLES_PER_SYMBOL
    tone_spacing = 6.25

    power = np.zeros((N_SYM, N_TONES))
    window = np.hanning(n)

    for sym in range(N_SYM):
        s0 = start_sample + sym * n
        s1 = s0 + n
        if s0 < 0 or s1 > len(audio):
            power[sym, :] = 0
            continue
        seg = audio[s0:s1] * window
        spec = np.fft.rfft(seg, n=n * 4)  # zero-padding for better frequency resolution
        freqs = np.fft.rfftfreq(n * 4, d=1.0 / SAMPLE_RATE)
        for tone in range(N_TONES):
            f_target = freq_hz + tone * tone_spacing
            idx = np.argmin(np.abs(freqs - f_target))
            power[sym, tone] = np.abs(spec[idx]) ** 2

    return power


def costas_sync_quality(power):
    """Measures how well the symbols at the Costas positions match the pattern (0..1)."""
    correct = 0
    total = 0
    for offset in COSTAS_POS:
        for k, expected_tone in enumerate(COSTAS):
            sym_idx = offset + k
            if sym_idx >= power.shape[0]:
                continue
            tone_detected = np.argmax(power[sym_idx])
            if tone_detected == expected_tone:
                correct += 1
            total += 1
    return correct / total if total else 0.0


def hard_decode_symbols(power):
    """Returns 79 tone values (0-7) via a hard decision (argmax)."""
    return np.argmax(power, axis=1)


def extract_bits174(power):
    """
    From the power matrix [79 x 8], extracts 174 hard LDPC code bits,
    using EXACTLY the same Gray table as the encoder (inverted).
    """
    tones = hard_decode_symbols(power)
    bits = []
    for sym_idx in DATA_SYM_INDICES:
        tone = tones[sym_idx]
        idx3 = GRAYMAP_INV[tone]
        bits.append((idx3 >> 2) & 1)
        bits.append((idx3 >> 1) & 1)
        bits.append(idx3 & 1)
    assert len(bits) == 174
    return bits


def extract_llr174(power):
    """
    From the power matrix [79 x 8], computes soft LLRs for the 174 LDPC bits.
    Positive LLR = bit more likely 0, negative = bit more likely 1
    (convention: log(P(bit=0)/P(bit=1))).

    Simplified approach: for each of the 3 bit positions within a symbol,
    we sum (in the log domain) the power of tones that give bit=0 vs bit=1
    according to the inverted Gray table, on a max-log-MAP basis.
    """
    llrs = []
    # tone_to_bits[tone] = (b0,b1,b2) corresponding to that tone
    tone_to_bits = {}
    for idx3 in range(8):
        tone = GRAYMAP[idx3]
        b0 = (idx3 >> 2) & 1
        b1 = (idx3 >> 1) & 1
        b2 = idx3 & 1
        tone_to_bits[tone] = (b0, b1, b2)

    eps = 1e-12
    for sym_idx in DATA_SYM_INDICES:
        p = power[sym_idx] + eps
        logp = np.log(p)
        for bit_pos in range(3):
            tones_0 = [t for t in range(8) if tone_to_bits[t][bit_pos] == 0]
            tones_1 = [t for t in range(8) if tone_to_bits[t][bit_pos] == 1]
            max0 = np.max(logp[tones_0])
            max1 = np.max(logp[tones_1])
            llrs.append(max0 - max1)
    assert len(llrs) == 174
    return llrs


def refine_sync(audio, freq_hz, time_offset_s,
                 freq_search_hz=40.0, freq_step_hz=1.0,
                 time_search_s=0.3, time_step_s=0.02):
    """
    Refines a candidate's position (coarse from sync.py, quantized to the
    oversampling grid) via a local search around it, maximizing the Costas
    match quality. Necessary because demodulation is very sensitive to
    positional accuracy (e.g. 100% symbol agreement at the ideal position
    vs 14% when shifted by half a grid step).
    Returns (best_freq, best_time, best_power, best_quality).
    """
    best_quality = -1
    best_freq = freq_hz
    best_time = time_offset_s
    best_power = None

    freqs = np.arange(freq_hz - freq_search_hz, freq_hz + freq_search_hz + 1e-9, freq_step_hz)
    times = np.arange(time_offset_s - time_search_s, time_offset_s + time_search_s + 1e-9, time_step_s)

    for f in freqs:
        for t in times:
            if t < 0:
                continue
            power = extract_tone_power(audio, f, t)
            q = costas_sync_quality(power)
            if q > best_quality:
                best_quality = q
                best_freq = f
                best_time = t
                best_power = power

    return best_freq, best_time, best_power, best_quality
