"""
ft4_encoder.py — Custom FT4 encoder/transmitter.

FT4 shares the entire source-encoding stage with FT8 up to the addition of
parity bits: pack77 (77 bits), CRC-14 (the same polynomial 0x2757),
LDPC(174,91) (the same Nm/Mn/rawg matrices). These functions are REUSED
unchanged from ft8_encoder.py — see the import below.

The differences from FT8 start AFTER pack77 and continue through to the
final frame:
  1. SCRAMBLING: the 77 source bits are XORed with the 'rvec' mask BEFORE
     the CRC is appended (FT8 has no scrambling step — this is the only
     real difference at the source-encoding layer)
  2. Gray mapping: 2 bits/symbol (not 3 as in FT8) -> 4 tones (not 8) -> 87
     data symbols (from 174 bits), not 58
  3. Frame structure: FOUR different 4-symbol Costas patterns (not one
     7-symbol pattern like FT8), interleaved with three 29-symbol data
     blocks: Costas1(4) + Block1(29) + Costas2(4) + Block2(29) + Costas3(4)
     + Block3(29) + Costas4(4) = 103 symbols total
  4. Modulation: 4-GFSK (not 8-GFSK), tone spacing 20.833Hz (not 6.25Hz),
     symbol period 0.048s (not 0.16s), BT=1.0 (not 2.0) — a significantly
     faster signal: ~5s of transmission within a 7.5s window (vs ~12.6s
     within a 15s window for FT8)

SPEC SOURCE: the scrambling mask 'rvec' (77 bits), costas_symbols (4
patterns of 4x4), costas_offsets ([0,33,66,99]) and graymap ([0,1,3,2])
were cross-checked between two independent public sources of the FT4
protocol spec.
  - The frame structure (4x Costas + 3x29 data block = 103 symbols, plus a
    "ramped null symbol" at the start/end = 105 total) was confirmed in
    BOTH sources (the same Sync1+Block1+Sync2+Block2+Sync3+Block3+Sync4 pattern).
  - Tone spacing 20.833Hz / symbol period 0.048s confirmed in BOTH sources
    ("576-point FFT @ 12000 sps" / "tone spacing 20.833Hz, symbol interval 48ms").

NOT YET VERIFIED against a real FT4 decoder — needs a live test with real
radio hardware, or comparison against a reference .wav recording.

DECODER (RX) — NOT IMPLEMENTED in this module. Requires a separate
pipeline analogous to ft8_rx_decoder.py, with a different Costas sync (4
patterns instead of 1, searched at specific offsets 0/33/66/99) and a
different FFT bin size (576-point FFT @ 12000Hz). That's a separate, large
piece of work for later.
"""
import numpy as np
from scipy.special import erf

# Reused unchanged: pack77 (77-bit source encoding), _crc14, _ldpc_encode,
# _ldpc_check, _build_gen_sys (already invoked at ft8_encoder's import time).
from ft8_encoder import (
    pack77, _crc14, _ldpc_encode, _ldpc_check,
)

# ============================================================
# PART 1: FT4-specific constants
# ============================================================

# Scrambling mask — 77 bits, XORed with pack77()'s output BEFORE the CRC is
# appended. Purpose: avoiding long runs of zeros for CQ-type messages
# (which have many zero bits in the raw packing). FT8 does NOT have this step.
_RVEC = np.array([
    0, 1, 0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 1, 1, 1, 0, 1, 0, 0, 0, 1, 0, 0, 1, 1, 0, 1, 1, 0,
    1, 0, 0, 1, 0, 1, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 1, 1, 1, 1, 0, 0, 1, 0, 1,
    0, 1, 0, 1, 0, 1, 1, 0, 1, 1, 1, 1, 1, 0, 0, 0, 1, 0, 1
], dtype=np.int32)
assert len(_RVEC) == 77

# Four different 4-symbol Costas (sync) patterns, each used once per frame.
_COSTAS_FT4 = [
    [0, 1, 3, 2],   # Sync1
    [1, 0, 2, 3],   # Sync2
    [2, 3, 1, 0],   # Sync3
    [3, 2, 0, 1],   # Sync4
]
# Starting positions (0-indexed) of each Costas block in the 103-symbol frame.
_COSTAS_OFFSETS = [0, 33, 66, 99]

# Gray mapping for bit pairs (2 bits/symbol, 4 tones) — the same idea as
# FT8's _GRAYMAP but for 2-bit indices instead of 3-bit.
# '00'->0, '01'->1, '10'->3, '11'->2 (confirmed on g4jnt.com and in weakmon)
_GRAYMAP_FT4 = [0, 1, 3, 2]


def _scramble77(bits77):
    """XOR 77 bits with the _RVEC mask. The operation is its own inverse
    (x XOR mask XOR mask == x), so the same function does scrambling at
    encode time and de-scrambling at decode time."""
    assert len(bits77) == 77
    a = np.array(bits77, dtype=np.int32)
    return list(np.mod(a + _RVEC, 2))


def encode_message_ft4(call_to, call_de, report_or_grid, r_flag=False):
    """
    Full FT4 pipeline: text -> 77-bit pack -> scrambling -> CRC-14 ->
    LDPC(174,91) -> Gray mapping (2bit) -> insert 4x Costas -> 103
    symbols (0-3).

    Returns (symbols103, debug_dict). Analogous to
    ft8_encoder.encode_message but with an added scrambling step and a
    different frame structure.
    """
    bits77 = pack77(call_to, call_de, report_or_grid, r_flag)
    scrambled77 = _scramble77(bits77)

    # CRC is computed on the SCRAMBLED bits (same as the FT8 pipeline,
    # _crc14 expects 82 bits = 77 + 5 padding zeros)
    padded82 = scrambled77 + [0, 0, 0, 0, 0]
    crc = _crc14(padded82)
    bits91 = np.array(scrambled77 + crc, dtype=np.int32)
    codeword174 = _ldpc_encode(bits91)

    # Gray-map bit pairs (174 bits -> 87 symbols, 2 bits each)
    data_symbols = []
    for i in range(0, 174, 2):
        b0, b1 = codeword174[i], codeword174[i + 1]
        idx = b0 * 2 + b1
        data_symbols.append(_GRAYMAP_FT4[idx])
    assert len(data_symbols) == 87

    # Split the 87 data symbols into 3 blocks of 29 and interleave with 4x Costas
    block1 = data_symbols[0:29]
    block2 = data_symbols[29:58]
    block3 = data_symbols[58:87]
    symbols103 = (_COSTAS_FT4[0] + block1 + _COSTAS_FT4[1] + block2 +
                  _COSTAS_FT4[2] + block3 + _COSTAS_FT4[3])
    assert len(symbols103) == 103

    debug = {
        "bits77": bits77,
        "scrambled77": scrambled77,
        "bits91": bits91.tolist(),
        "codeword174": codeword174.tolist(),
        "ldpc_valid": _ldpc_check(codeword174),
    }
    return symbols103, debug


# ============================================================
# PART 2: 4-GFSK audio generator
# ============================================================

FT4_SAMPLE_RATE = 12000
FT4_SYMBOL_PERIOD = 0.048  # 48ms — confirmed in 2 independent sources
FT4_SAMPLES_PER_SYMBOL = round(FT4_SAMPLE_RATE * FT4_SYMBOL_PERIOD)  # 576
FT4_TONE_SPACING = FT4_SAMPLE_RATE / FT4_SAMPLES_PER_SYMBOL  # 20.8333... Hz
FT4_BT = 1.0  # FT4 uses a more smoothing Gaussian filter than FT8 (BT=2.0)

FT4_SLOT_TIME = 7.5  # T/R window in seconds (vs 15.0 for FT8)

TARGET_SAMPLE_RATE = 48000  # required by feed_tx_pcm (audio_stream.py)


def _gaussian_pulse_ft4(t, bt, symbol_period):
    """Identical formula to FT8's _gaussian_pulse, but bt and symbol_period
    are FT4-specific parameters (BT=1.0, period=0.048s)."""
    k = bt * 2 * np.pi / np.sqrt(np.log(2))
    arg1 = k * (t / symbol_period - 0.5)
    arg2 = k * (t / symbol_period + 0.5)
    q1 = 0.5 * (1 - erf(arg1 / np.sqrt(2)))
    q2 = 0.5 * (1 - erf(arg2 / np.sqrt(2)))
    return q1 - q2


def _precompute_pulse_table_ft4():
    n = FT4_SAMPLES_PER_SYMBOL
    t = (np.arange(3 * n) - 1.5 * n) / FT4_SAMPLE_RATE
    return _gaussian_pulse_ft4(t, FT4_BT, FT4_SYMBOL_PERIOD)


_PULSE_TABLE_FT4 = _precompute_pulse_table_ft4()


def synthesize_gfsk_ft4(symbols, base_freq_hz=1000.0):
    """103 symbols (0-3) -> numpy float32 PCM @ 12000Hz, normalized to -1..1.

    Identical logic to FT8's synthesize_gfsk (summing overlapping Gaussian
    frequency pulses), just with FT4's parameters (4 tones instead of 8,
    smaller tone spacing, shorter symbol period)."""
    n_sym = len(symbols)
    n = FT4_SAMPLES_PER_SYMBOL
    total_samples = n_sym * n
    freq_dev = np.zeros(total_samples + 2 * n)

    for i, sym in enumerate(symbols):
        center = i * n + n
        start = center - n
        seg_start = max(0, start)
        seg_end = min(len(freq_dev), start + 3 * n)
        pulse_start = seg_start - start
        pulse_end = pulse_start + (seg_end - seg_start)
        freq_dev[seg_start:seg_end] += sym * FT4_TONE_SPACING * _PULSE_TABLE_FT4[pulse_start:pulse_end]

    freq_dev = freq_dev[n:n + total_samples]
    inst_freq = base_freq_hz + freq_dev
    phase = 2 * np.pi * np.cumsum(inst_freq) / FT4_SAMPLE_RATE
    return np.sin(phase).astype(np.float32)


def generate_tx_pcm48k_ft4(call_to, call_de, report_or_grid, r_flag=False,
                             base_freq_hz=1000.0, amplitude=0.12):
    """
    Full pipeline: text -> 103 symbols -> 12kHz audio -> resample to 48kHz ->
    int16 PCM bytes, ready for feed_tx_pcm(). The interface is identical to
    ft8_encoder.generate_tx_pcm48k (same return type), so the calling code
    (_ft8_tx_sequence_inner in webapp.py) can use either encoder
    interchangeably depending on the selected mode.

    Default amplitude=0.12 — the same value and the same anti-clipping
    rationale as in ft8_encoder (see its docstring): the backend further
    multiplies by txVolume (max 8.0), so 0.12*8.0=0.96 stays safely below clipping.

    Returns (pcm_bytes, debug_dict, duration_seconds).
    """
    from scipy.signal import resample_poly
    symbols103, debug = encode_message_ft4(call_to, call_de, report_or_grid, r_flag)
    audio_12k = synthesize_gfsk_ft4(symbols103, base_freq_hz=base_freq_hz)
    audio_48k = resample_poly(audio_12k, up=4, down=1)
    pcm16 = np.clip(audio_48k * 32767 * amplitude, -32768, 32767).astype(np.int16)
    duration = len(audio_12k) / FT4_SAMPLE_RATE
    return pcm16.tobytes(), debug, duration


if __name__ == "__main__":
    # Self-test when the module is run directly
    print("=== FT4 Encoder self-test ===")
    print(f"FT4_SAMPLES_PER_SYMBOL = {FT4_SAMPLES_PER_SYMBOL}")
    print(f"FT4_TONE_SPACING = {FT4_TONE_SPACING:.4f} Hz")
    print(f"Transmission time (103 symbols) = {103 * FT4_SYMBOL_PERIOD:.3f}s")

    test_messages = [
        ("CQ", "XX0XXX", "KO02"),
        ("XX0XXX", "DL1ABC", "-12"),
        ("DL1ABC", "XX0XXX", "R-08"),
        ("XX0XXX", "DL1ABC", "RRR"),
        ("DL1ABC", "XX0XXX", "73"),
    ]
    for call_to, call_de, rg in test_messages:
        symbols, debug = encode_message_ft4(call_to, call_de, rg)
        status = "OK" if debug["ldpc_valid"] else "LDPC FAIL"
        print(f"  '{call_to} {call_de} {rg}' -> {len(symbols)} symbols, "
              f"ldpc_valid={debug['ldpc_valid']} [{status}]")
