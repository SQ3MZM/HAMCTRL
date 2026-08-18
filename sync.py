"""
Stage 1: FT8 signal candidate detection (sync search).

Method: compute a spectrogram (STFT) of the whole audio window with time
oversampling (e.g. every 1/2 symbol) and frequency oversampling (every 1/2
tone), then slide the 7-symbol Costas pattern across the (time, frequency)
grid and look for local correlation maxima (sum of power on the expected
Costas tones minus the average power on the other tones in that window).

An FT8 signal has THREE occurrences of the Costas pattern (at symbol
positions 0, 36, 72), so we sum the correlation across all three for more
reliable detection in noise.
"""
import numpy as np
from params import (SAMPLE_RATE, SAMPLES_PER_SYMBOL, TONE_SPACING, N_TONES,
                     COSTAS, COSTAS_POS, N_SYM)


def compute_magnitude_spectrogram(audio, freq_osr=2, time_osr=2, f_min=200, f_max=3000):
    """
    Computes an oversampled power spectrogram.
    freq_osr: frequency oversampling relative to TONE_SPACING (2 = 3.125Hz
        steps), achieved via ZERO-PADDING (not lengthening the window), so
        each FFT window still covers exactly 1 symbol and doesn't mix
        energy from adjacent symbols.
    time_osr: time oversampling relative to SAMPLES_PER_SYMBOL (2 = half-symbol steps)

    Returns: (mag, n_blocks, n_bins, freq_step, time_step_samples, bin_min)
        mag[time_block, freq_bin] = power
    """
    n = SAMPLES_PER_SYMBOL          # analysis window length = ALWAYS 1 symbol
    nfft = n * freq_osr             # zero-pad to this length (frequency resolution)
    freq_step = SAMPLE_RATE / nfft  # Hz per FFT bin
    time_step = SAMPLES_PER_SYMBOL // time_osr  # samples between consecutive STFT windows

    n_samples = len(audio)
    n_blocks = (n_samples - n) // time_step + 1
    if n_blocks < 1:
        return None

    window = np.hanning(n)
    bin_min = int(f_min / freq_step)
    bin_max = int(f_max / freq_step)

    mag = np.zeros((n_blocks, bin_max - bin_min), dtype=np.float64)
    for i in range(n_blocks):
        start = i * time_step
        seg = audio[start:start + n]
        if len(seg) < n:
            seg = np.pad(seg, (0, n - len(seg)))
        spec = np.fft.rfft(seg * window, n=nfft)  # zero-padding here, window is still length n
        power = np.abs(spec) ** 2
        mag[i, :] = power[bin_min:bin_max]

    return mag, n_blocks, mag.shape[1], freq_step, time_step, bin_min


def find_candidates(audio, freq_osr=2, time_osr=2, f_min=200, f_max=3000,
                     max_candidates=30, min_score=0.4):
    """
    Searches audio for FT8 signal candidates.
    Returns a list of dicts: {freq_hz, time_offset_s, score, block0, bin0}

    Scoring robust to overlapping stations: for each Costas position we
    compute (power_of_correct_tone - average_of_8_tones) / average_of_8_tones
    on LINEAR power. The old method (log(expected) - log(max_of_the_other_7))
    penalized neighbors: a strong signal nearby raised the max and pushed the
    margin below threshold, losing even strong stations (e.g. E20LXN -3dB
    with a neighbor at 2009Hz). The "relative to the average" method is
    robust to a single neighbor.
    Vectorized version: 3D arrays, one numpy operation per Costas term.
    """
    result = compute_magnitude_spectrogram(audio, freq_osr, time_osr, f_min, f_max)
    if result is None:
        return []
    mag, n_blocks, n_bins, freq_step, time_step, bin_min = result

    bins_per_tone = freq_osr
    sym_blocks = time_osr

    max_time_block = n_blocks - N_SYM * sym_blocks
    max_freq_bin = n_bins - (N_TONES - 1) * bins_per_tone

    if max_time_block < 1 or max_freq_bin < 1:
        return []

    t0_step = max(1, sym_blocks // 2)
    f0_step = max(1, bins_per_tone // 2)
    t0_vals = np.arange(0, max_time_block, t0_step)
    f0_vals = np.arange(0, max_freq_bin, f0_step)
    n_t0 = len(t0_vals)
    n_f0 = len(f0_vals)

    score_accum = np.zeros((n_t0, n_f0), dtype=np.float64)
    count_accum = 0

    for costas_sym_offset in COSTAS_POS:
        for k, tone in enumerate(COSTAS):
            sym_idx = costas_sym_offset + k
            tblocks = t0_vals + sym_idx * sym_blocks  # (n_t0,)
            valid_t = tblocks < n_blocks
            if not np.any(valid_t):
                continue

            # For each f0 (n_f0,) and each tone (8,), compute the bin index
            tone_offsets = np.arange(N_TONES) * bins_per_tone  # (8,)
            fbins = f0_vals[:, None] + tone_offsets[None, :]   # (n_f0, 8)
            valid_f = np.all((fbins >= 0) & (fbins < n_bins), axis=1)  # (n_f0,)
            if not np.any(valid_f):
                continue

            # Slice out the power (LINEAR, not log) for (tblock, fbin): (n_t0, n_f0, 8)
            tb_idx = np.clip(tblocks, 0, n_blocks - 1)
            fb_idx = np.clip(fbins, 0, n_bins - 1)
            block_rows = mag[tb_idx]               # (n_t0, n_bins)
            powers = block_rows[:, fb_idx]          # (n_t0, n_f0, 8)

            expected = powers[:, :, tone]                       # (n_t0, n_f0)
            mean_all = np.mean(powers, axis=2)                  # (n_t0, n_f0)
            # contribution: how much the correct tone exceeds the AVERAGE (normalized).
            # Robust to 1 neighbor (a neighbor only raises the average by ~1/8).
            with np.errstate(divide='ignore', invalid='ignore'):
                contrib = np.where(mean_all > 1e-12,
                                   (expected - mean_all) / mean_all, 0.0)

            valid_mask = valid_t[:, None] & valid_f[None, :]
            score_accum += np.where(valid_mask, contrib, 0.0)
            count_accum += 1

    if count_accum == 0:
        return []
    score_map = score_accum / (len(COSTAS_POS) * len(COSTAS))

    candidates = []

    # Non-max suppression: find local maxima in score_map
    flat_idx = np.argsort(score_map, axis=None)[::-1]
    taken = np.zeros_like(score_map, dtype=bool)
    # suppression radius expressed in score_map INDEX UNITS (not t0_step/f0_step
    # directly from samples), so we convert it against the actual grid step
    suppress_t = max(1, (sym_blocks * 2) // t0_step)
    suppress_f = max(1, (bins_per_tone * 2) // f0_step)

    for idx in flat_idx:
        i_t, i_f = np.unravel_index(idx, score_map.shape)
        sc = score_map[i_t, i_f]
        if sc < min_score or not np.isfinite(sc):
            break
        if taken[i_t, i_f]:
            continue
        t_lo, t_hi = max(0, i_t - suppress_t), min(n_t0, i_t + suppress_t + 1)
        f_lo, f_hi = max(0, i_f - suppress_f), min(n_f0, i_f + suppress_f + 1)
        if taken[t_lo:t_hi, f_lo:f_hi].any():
            continue
        taken[t_lo:t_hi, f_lo:f_hi] = True

        f0 = f0_vals[i_f]
        t0 = t0_vals[i_t]
        freq_hz = (f0 + bin_min) * freq_step
        time_offset_s = t0 * time_step / SAMPLE_RATE
        candidates.append({
            'freq_hz': freq_hz,
            'time_offset_s': time_offset_s,
            'score': float(sc),
            'block0': t0,
            'bin0': f0 + bin_min,
        })
        if len(candidates) >= max_candidates:
            break

    candidates.sort(key=lambda c: -c['score'])
    return candidates
