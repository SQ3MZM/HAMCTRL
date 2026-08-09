"""
Etap 1: Wykrywanie kandydatow sygnalow FT4 (sync search).

Identyczna metoda co sync.py (FT8): spektrogram STFT z nadprobkowaniem,
przesuwanie wzorca Costas po siatce (czas, czestotliwosc), szukanie
lokalnych maksimow korelacji (margin = moc na oczekiwanym tonie minus
max mocy na pozostalych tonach).

KLUCZOWA ROZNICA wzgledem FT8: FT4 ma CZTERY ROZNE wzorce Costas (nie jeden
powtorzony 3x), kazdy na innej pozycji w ramce (symbole 0, 33, 66, 99).
Wymaga to dopasowania KAZDEGO bloku do JEGO WLASNEGO wzorca, a nie tego
samego wzorca we wszystkich trzech/czterech miejscach.
"""
import numpy as np
from params_ft4 import (SAMPLE_RATE, SAMPLES_PER_SYMBOL, TONE_SPACING, N_TONES,
                         COSTAS_PATTERNS, COSTAS_POS, N_SYM)


def compute_magnitude_spectrogram(audio, freq_osr=2, time_osr=2, f_min=200, f_max=3000):
    """Identyczne jak w sync.py (FT8) — w pelni parametryczne wzgledem
    SAMPLES_PER_SYMBOL, ktore tutaj jest mniejsze (576 zamiast 1920), wiec
    okna analizy sa krotsze i bardziej liczne dla tego samego czasu trwania
    audio. Zaimportowane SAMPLE_RATE/SAMPLES_PER_SYMBOL pochodza z
    params_ft4, nie z params (FT8)."""
    n = SAMPLES_PER_SYMBOL
    nfft = n * freq_osr
    freq_step = SAMPLE_RATE / nfft
    time_step = SAMPLES_PER_SYMBOL // time_osr

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
        spec = np.fft.rfft(seg * window, n=nfft)
        power = np.abs(spec) ** 2
        mag[i, :] = power[bin_min:bin_max]

    return mag, n_blocks, mag.shape[1], freq_step, time_step, bin_min


def find_candidates(audio, freq_osr=2, time_osr=2, f_min=200, f_max=3000,
                     max_candidates=30, min_score=0.3):
    """
    Przeszukuje audio w poszukiwaniu kandydatow sygnalow FT4.
    Zwraca liste dict: {freq_hz, time_offset_s, score, block0, bin0}

    Roznica wzgledem FT8: petla po COSTAS_POS jest sparowana z
    COSTAS_PATTERNS (zip), bo kazda pozycja ma SWOJ WLASNY wzorzec tonow,
    nie wspolny COSTAS jak w FT8. Reszta algorytmu (wektoryzacja, margin,
    non-max suppression) identyczna jak w sync.py.
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

    log_mag = np.log(mag + 1e-12)

    score_accum = np.zeros((n_t0, n_f0), dtype=np.float64)
    n_terms = 0

    # KLUCZOWA ROZNICA wzgledem FT8: zip(COSTAS_POS, COSTAS_PATTERNS) zamiast
    # zagniezdzonej petli "ten sam wzorzec na kazdej pozycji". Kazdy z 4
    # blokow Costas ma SWOJ WLASNY wzorzec tonow.
    for costas_sym_offset, pattern in zip(COSTAS_POS, COSTAS_PATTERNS):
        for k, tone in enumerate(pattern):
            sym_idx = costas_sym_offset + k
            tblocks = t0_vals + sym_idx * sym_blocks
            valid_t = tblocks < n_blocks
            if not np.any(valid_t):
                continue

            tone_offsets = np.arange(N_TONES) * bins_per_tone
            fbins = f0_vals[:, None] + tone_offsets[None, :]
            valid_f = np.all((fbins >= 0) & (fbins < n_bins), axis=1)
            if not np.any(valid_f):
                continue

            tb_idx = np.clip(tblocks, 0, n_blocks - 1)
            fb_idx = np.clip(fbins, 0, n_bins - 1)
            block_rows = log_mag[tb_idx]
            powers = block_rows[:, fb_idx]

            expected = powers[:, :, tone]
            mask = np.ones(N_TONES, dtype=bool)
            mask[tone] = False
            max_other = np.max(powers[:, :, mask], axis=2)
            margin = expected - max_other

            valid_mask = valid_t[:, None] & valid_f[None, :]
            score_accum += np.where(valid_mask, margin, 0.0)
            n_terms += 1

    if n_terms == 0:
        return []
    score_map = score_accum / n_terms

    candidates = []
    flat_idx = np.argsort(score_map, axis=None)[::-1]
    taken = np.zeros_like(score_map, dtype=bool)
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
