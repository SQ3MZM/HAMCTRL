"""
Generates a single waterfall column from a short chunk of RX audio. A
lightweight, fast function meant to be streamed to the UI frequently
(e.g. every 0.5-1s) — NOT for FT8 sync (that's what
sync.compute_magnitude_spectrogram is for, which has higher resolution
and is more computationally expensive).
"""
import numpy as np

SAMPLE_RATE = 12000
F_MIN = 200
F_MAX = 3000
N_BINS = 200  # number of frequency bins per waterfall column (~14Hz/bin resolution)

# Global state for SMOOTH, TIME-STABLE color scaling.
# CRITICAL: recomputing db_min/db_max from scratch for EVERY column (e.g.
# from its own percentiles) makes the same physical signal level get a
# different color in every successive time column — it looks like random
# noise instead of stable, horizontal tone lines. Instead we keep a
# slowly-updated (EMA) range that only shifts over many seconds.
_ema_db_lo = None
_ema_db_hi = None
_EMA_ALPHA = 0.05  # smaller = slower to adapt (more stable image)

# TIME smoothing of the column itself (per-bin), separate from the color
# range smoothing above. EACH column is an independent, short FFT window
# (0.3-0.8s of audio) — without smoothing, adjacent time columns have high
# variance (a different noise slice each time), giving a jagged,
# "staircase" look instead of the smooth vertical streaks seen in real
# WSJT-X (which uses a much denser stream and/or longer analysis windows).
# EMA in the POWER domain (before converting to dB) is mathematically more
# correct than averaging the dB values themselves.
_ema_power_column = None
_SMOOTH_ALPHA = 0.35  # larger = less smoothing (faster reaction to changes)


def smooth_column_power(power_column):
    """
    Smooths the power column (BEFORE converting to dB) over time, per-bin
    EMA. Called once per new column in the backend loop, between
    compute_waterfall_column (which computes the RAW column) and the
    conversion to dB. Resets automatically if the size (n_bins) changes.
    """
    global _ema_power_column
    if _ema_power_column is None or len(_ema_power_column) != len(power_column):
        _ema_power_column = power_column.copy()
    else:
        _ema_power_column += _SMOOTH_ALPHA * (power_column - _ema_power_column)
    return _ema_power_column.copy()


def compute_waterfall_column(audio_chunk, n_bins=N_BINS, f_min=F_MIN, f_max=F_MAX,
                              sample_rate=SAMPLE_RATE, smooth=True):
    """
    audio_chunk: numpy float array (mono, sample_rate Hz), any length
        >= a few dozen ms (typically a 0.3-0.8s chunk from the RX buffer).
    smooth: if True (default), applies per-bin EMA time smoothing (see
        smooth_column_power) before converting to dB — eliminates the
        jagged, "staircase" look caused by high variance between
        consecutive, independent short FFT windows.
    Returns: a numpy array of length n_bins, values in dB (normalized to a
        sensible display range, NOT for FT8 decoding).
    """
    if len(audio_chunk) < 64:
        return np.zeros(n_bins, dtype=np.float32)

    window = np.hanning(len(audio_chunk))
    spec = np.fft.rfft(audio_chunk * window)
    power = np.abs(spec) ** 2
    freqs = np.fft.rfftfreq(len(audio_chunk), d=1.0 / sample_rate)

    # Bin into n_bins evenly spaced bins between f_min and f_max
    bin_edges = np.linspace(f_min, f_max, n_bins + 1)
    out = np.zeros(n_bins, dtype=np.float64)
    bin_idx = np.digitize(freqs, bin_edges) - 1
    for i in range(n_bins):
        mask = bin_idx == i
        if np.any(mask):
            out[i] = np.mean(power[mask])

    if smooth:
        out = smooth_column_power(out)

    db = 10 * np.log10(out + 1e-12)
    return db.astype(np.float32)


def quantize_for_transport(db_column, db_min=None, db_max=None, threshold=0.28, steepness=4.5, floor=42):
    """
    Quantizes a dB column to a list of ints 0-255 (1 byte/bin instead of
    float32), to shrink the JSON payload sent roughly every 0.8s over WS.

    If db_min/db_max aren't given, uses the SMOOTHED (EMA) global range,
    updated slowly from each new column's percentiles — the absolute dB
    level depends heavily on the audio input hardware/gain, so the range
    needs to adapt dynamically, BUT must stay STABLE between consecutive
    columns (otherwise the same physical signal gets a different color
    every column, which looks like random noise instead of clean,
    horizontal FT8 tone lines).

    threshold/steepness: a SIGMOID contrast curve (S-curve), NOT a simple
    gamma. Measurement on a real recording showed that ~75% of values
    (typical background/weak noise, not signal) already landed around
    0.2-0.3 of the normalized range — with a simple gamma (<1.0) that
    range got brightened along with real weak signals, making the WHOLE
    band look "busy" and making it impossible to tell empty spectrum from
    an active transmission. The sigmoid clearly DARKENS everything below
    threshold (silence/noise stays black) and BOOSTS everything above it
    (even weak signals become distinct) — giving a sharp, readable
    separation instead of uniform brightening. steepness controls how
    sharp the transition is.

    floor: the minimum value (0-255) for the BACKGROUND — measured
    directly from a reference JTDX screenshot, where typical noise renders
    as a saturated blue (not black). Without this, in very quiet bands the
    whole image went black (mathematically correct, but less readable/
    familiar visually than the original). floor raises the minimum so the
    noise "carpet" is always visible, while contrast against real signals
    is preserved (because floor is added AFTER the sigmoid, as a floor,
    not a shift of the whole curve).
    """
    global _ema_db_lo, _ema_db_hi
    if db_min is None or db_max is None:
        # We use p10 (10th percentile) as the lower bound — spreading the
        # data range wider in the palette so the noise background lands in
        # a colored blue (~40-80/255) instead of near-black (floor=18/255
        # as before). This gives a livelier, more readable waterfall
        # similar to WSJT-X/JTDX.
        # EMA smooths the range between columns so colors stay stable.
        p_lo = float(np.percentile(db_column, 10))  # p10 instead of p50 — wider data range in the palette
        p_hi = float(np.percentile(db_column, 99.5))
        if p_hi - p_lo < 10:
            p_hi = p_lo + 10
        target_lo = p_lo
        target_hi = p_hi + 3
        if _ema_db_lo is None:
            _ema_db_lo, _ema_db_hi = target_lo, target_hi
        else:
            _ema_db_lo += _EMA_ALPHA * (target_lo - _ema_db_lo)
            _ema_db_hi += _EMA_ALPHA * (target_hi - _ema_db_hi)
        db_min, db_max = _ema_db_lo, _ema_db_hi
    clipped = np.clip(db_column, db_min, db_max)
    norm = (clipped - db_min) / (db_max - db_min)  # 0..1

    # Sigmoid centered on 'threshold', normalized so norm=0 -> 0 and
    # norm=1 -> 1 (keeps the full black/white range at the extremes).
    s = 1.0 / (1.0 + np.exp(-steepness * (norm - threshold)))
    s0 = 1.0 / (1.0 + np.exp(steepness * threshold))
    s1 = 1.0 / (1.0 + np.exp(-steepness * (1.0 - threshold)))
    contrast = (s - s0) / (s1 - s0)
    contrast = np.clip(contrast, 0.0, 1.0)

    scaled = contrast * 255
    # Floor: the minimum visible value, so the background is never
    # completely black (as in the reference JTDX). We scale the rest of
    # the range (floor..255) so the strongest signals still reach a full 255.
    scaled = floor + scaled * (255 - floor) / 255.0
    scaled = np.clip(scaled, 0, 255).astype(np.uint8)
    return scaled.tolist()
