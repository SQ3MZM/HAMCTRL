"""
Stage 2 (FT4): Demodulation - from audio + a candidate position (freq_hz,
time_offset_s) we extract 103 symbols (hard decision) and 174 soft LLRs
(for full LDPC belief-propagation later).

Bit<->symbol mapping: the 103 symbols contain 4x4=16 Costas sync symbols
(ignored when decoding data) + 87 data symbols. Each data symbol encodes 2
Gray-mapped bits from the 174-bit LDPC(174,91) code. Matches our encoder
(ft4_encoder.py): _GRAYMAP_FT4 and the 4x Costas placement at positions
[0:4], [33:37], [66:70], [99:103].
"""
import numpy as np
from params_ft4 import (SAMPLE_RATE, SAMPLES_PER_SYMBOL, N_TONES,
                         COSTAS_PATTERNS, COSTAS_POS, N_SYM, TONE_SPACING,
                         GRAYMAP, INV_GRAYMAP)

# Positions of data symbols in the 103-symbol frame (after removing the
# 4x4=16 Costas symbols):
# symbols103 = C1[0:4] + data[0:29] + C2[33:37] + data[29:58] + C3[66:70] +
#              data[58:87] + C4[99:103]
DATA_SYM_INDICES = (list(range(4, 33)) + list(range(37, 66)) + list(range(70, 99)))
assert len(DATA_SYM_INDICES) == 87


def extract_tone_power(audio, freq_hz, time_offset_s, freq_osr=2):
    """
    For a given position (freq_hz = frequency of tone 0, time_offset_s =
    start of the first symbol), extracts the power matrix [103 symbols x 4
    tones] via correlation with pure tones (FFT on each symbol window).

    VECTORIZED (unlike FT8's demod.py, which uses a plain Python loop with
    a separate FFT per symbol): for FT4 this function is called ~1500x
    within a single refine_sync() (the freq x time grid), and the
    non-vectorized loop version took ~8s per decode_window call — too slow
    relative to FT4's 7.5s window. Here we build the matrix of all 103
    segments at once and do ONE batched FFT (axis=1), instead of 103
    separate np.fft.rfft calls. Result is numerically identical to the
    loop-based version.
    """
    start_sample = int(round(time_offset_s * SAMPLE_RATE))
    n = SAMPLES_PER_SYMBOL
    nfft = n * 4
    window = np.hanning(n)

    # Build the matrix of segments [N_SYM x n], zero-filled for symbols
    # falling outside the available audio (same behavior as the loop
    # version: power[sym,:]=0 for such positions)
    segs = np.zeros((N_SYM, n), dtype=np.float64)
    valid = np.ones(N_SYM, dtype=bool)
    for sym in range(N_SYM):
        s0 = start_sample + sym * n
        s1 = s0 + n
        if s0 < 0 or s1 > len(audio):
            valid[sym] = False
            continue
        segs[sym] = audio[s0:s1]

    segs *= window[None, :]  # Hann window applied to every row at once

    # ONE batched FFT instead of 103 separate calls
    spec = np.fft.rfft(segs, n=nfft, axis=1)  # (N_SYM, nfft//2+1)
    freqs = np.fft.rfftfreq(nfft, d=1.0 / SAMPLE_RATE)

    # Bin indices for the 4 tones are the same for every symbol (they only
    # depend on freq_hz), so compute them once, not in the loop
    tone_idx = np.array([np.argmin(np.abs(freqs - (freq_hz + tone * TONE_SPACING)))
                          for tone in range(N_TONES)])

    power = np.abs(spec[:, tone_idx]) ** 2  # (N_SYM, N_TONES)
    power[~valid, :] = 0
    return power


def costas_sync_quality(power):
    """Measures how well the symbols at the Costas positions match EACH
    block's OWN pattern (different patterns at different positions, unlike
    FT8 where it's the same pattern everywhere)."""
    correct = 0
    total = 0
    for offset, pattern in zip(COSTAS_POS, COSTAS_PATTERNS):
        for k, expected_tone in enumerate(pattern):
            sym_idx = offset + k
            if sym_idx >= power.shape[0]:
                continue
            tone_detected = np.argmax(power[sym_idx])
            if tone_detected == expected_tone:
                correct += 1
            total += 1
    return correct / total if total else 0.0


def hard_decode_symbols(power):
    """Returns 103 tone values (0-3) via a hard decision (argmax)."""
    return np.argmax(power, axis=1)


def extract_bits174(power):
    """
    From the power matrix [103 x 4], extracts 174 hard LDPC code bits
    (BEFORE de-scrambling), using EXACTLY the same Gray table as the
    encoder (inverted, 2 bits/symbol instead of 3).
    """
    tones = hard_decode_symbols(power)
    bits = []
    for sym_idx in DATA_SYM_INDICES:
        tone = tones[sym_idx]
        idx2 = INV_GRAYMAP[tone]
        bits.append((idx2 >> 1) & 1)
        bits.append(idx2 & 1)
    assert len(bits) == 174
    return bits


def extract_llr174(power):
    """
    From the power matrix [103 x 4], computes soft LLRs for the 174 LDPC
    bits (BEFORE de-scrambling — the scrambling is only undone AFTER LDPC
    decode, at the plaintext-bits level, mirroring ft4_encoder.py where
    scrambling is applied BEFORE the CRC/LDPC encode).

    Same max-log-MAP method as FT8, adapted for 2 bits/symbol (4 tones)
    instead of 3 bits/symbol (8 tones).
    """
    llrs = []
    tone_to_bits = {}
    for idx2 in range(4):
        tone = GRAYMAP[idx2]
        b0 = (idx2 >> 1) & 1
        b1 = idx2 & 1
        tone_to_bits[tone] = (b0, b1)

    eps = 1e-12
    for sym_idx in DATA_SYM_INDICES:
        p = power[sym_idx] + eps
        logp = np.log(p)
        for bit_pos in range(2):
            tones_0 = [t for t in range(4) if tone_to_bits[t][bit_pos] == 0]
            tones_1 = [t for t in range(4) if tone_to_bits[t][bit_pos] == 1]
            max0 = np.max(logp[tones_0])
            max1 = np.max(logp[tones_1])
            llrs.append(max0 - max1)
    assert len(llrs) == 174
    return llrs


def refine_sync(audio, freq_hz, time_offset_s,
                 freq_search_hz=12.0, freq_step_hz=3.0,
                 time_search_s=0.03, time_step_s=0.006):
    """
    Refines a candidate's position via a local search, maximizing the
    quality of the 4x Costas match.

    The default ranges are DELIBERATELY NARROW: limited to roughly +-1
    oversampling grid step from find_candidates() (freq_osr=2 -> ~10.4Hz
    step, time_osr=2 -> ~24ms step), because sync_ft4.find_candidates
    already runs on an oversampled grid and typically lands very close to
    the true position. Testing showed: this narrow grid (99 iterations
    instead of 1558 for a wider, FT8-style grid) still gives
    sync_quality=1.0 and >=100/103 correct symbols even with added noise —
    the remaining up to 3 errors get corrected by LDPC belief propagation
    anyway. This ~15x speedup is NECESSARY for decode_window to fit within
    FT4's 7.5s window with many simultaneous candidates in the band
    (unlike FT8, where the 15s window gives more time margin).
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
