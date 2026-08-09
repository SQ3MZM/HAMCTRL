"""
Etap 1: Wykrywanie kandydatow sygnalow FT8 (sync search).

Metoda: liczymy spektrogram (STFT) calego okna audio z nadpróbkowaniem
czasowym (np. co 1/2 symbolu) i czestotliwosciowym (co 1/2 tonu), nastepnie
przesuwamy 7-symbolowy wzorzec Costas po siatce (czas, czestotliwosc) i
szukamy lokalnych maksimow korelacji (sumy mocy na oczekiwanych tonach
Costas minus srednia mocy na pozostalych tonach w danym oknie).

Sygnal FT8 ma TRZY wystapienia wzorca Costas (na pozycjach symboli 0, 36,
72), wiec uzywamy sumy korelacji ze wszystkich trzech dla wiekszej
niezawodnosci wykrywania w szumie.
"""
import numpy as np
from params import (SAMPLE_RATE, SAMPLES_PER_SYMBOL, TONE_SPACING, N_TONES,
                     COSTAS, COSTAS_POS, N_SYM)


def compute_magnitude_spectrogram(audio, freq_osr=2, time_osr=2, f_min=200, f_max=3000):
    """
    Liczy spektrogram mocy z nadprobkowaniem.
    freq_osr: nadprobkowanie czestotliwosciowe wzgledem TONE_SPACING (2 = krok 3.125Hz),
        osiagniete przez ZERO-PADDING (nie wydluzanie okna), zeby kazde okno FFT
        nadal obejmowalo dokladnie 1 symbol i nie mieszalo energii sasiednich symboli.
    time_osr: nadprobkowanie czasowe wzgledem SAMPLES_PER_SYMBOL (2 = krok pol-symbolu)

    Zwraca: (mag, n_blocks, n_bins, freq_step, time_step_samples, bin_min)
        mag[time_block, freq_bin] = moc
    """
    n = SAMPLES_PER_SYMBOL          # dlugosc okna analizy = ZAWSZE 1 symbol
    nfft = n * freq_osr             # zero-padding do tej dlugosci (rozdzielczosc czest.)
    freq_step = SAMPLE_RATE / nfft  # Hz na 1 bin FFT
    time_step = SAMPLES_PER_SYMBOL // time_osr  # probek miedzy kolejnymi oknami STFT

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
        spec = np.fft.rfft(seg * window, n=nfft)  # zero-padding tutaj, okno wciaz dlugosci n
        power = np.abs(spec) ** 2
        mag[i, :] = power[bin_min:bin_max]

    return mag, n_blocks, mag.shape[1], freq_step, time_step, bin_min


def find_candidates(audio, freq_osr=2, time_osr=2, f_min=200, f_max=3000,
                     max_candidates=30, min_score=0.4):
    """
    Przeszukuje audio w poszukiwaniu kandydatow sygnalow FT8.
    Zwraca liste dict: {freq_hz, time_offset_s, score, block0, bin0}

    Scoring odporny na nakladajace sie stacje: dla kazdej pozycji Costas
    liczymy (moc_wlasciwego_tonu - srednia_8_tonow) / srednia_8_tonow na
    LINIOWEJ mocy. Stara metoda (log(oczekiwany) - log(max_z_7_innych))
    karala sasiadow: silny sygnal obok podnosil max i spychal margin ponizej
    progu, gubiac nawet mocne stacje (np. E20LXN -3dB z sasiadem na 2009Hz).
    Metoda "wzgledem sredniej" jest odporna na pojedynczego sasiada.
    Wersja zwektoryzowana: macierze 3D, jedna operacja numpy na skladnik Costas.
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

            # Dla kazdego f0 (n_f0,) i kazdego tonu (8,) policz bin index
            tone_offsets = np.arange(N_TONES) * bins_per_tone  # (8,)
            fbins = f0_vals[:, None] + tone_offsets[None, :]   # (n_f0, 8)
            valid_f = np.all((fbins >= 0) & (fbins < n_bins), axis=1)  # (n_f0,)
            if not np.any(valid_f):
                continue

            # Wytnij moc (LINIOWA, nie log) dla (tblock, fbin): (n_t0, n_f0, 8)
            tb_idx = np.clip(tblocks, 0, n_blocks - 1)
            fb_idx = np.clip(fbins, 0, n_bins - 1)
            block_rows = mag[tb_idx]               # (n_t0, n_bins)
            powers = block_rows[:, fb_idx]          # (n_t0, n_f0, 8)

            expected = powers[:, :, tone]                       # (n_t0, n_f0)
            mean_all = np.mean(powers, axis=2)                  # (n_t0, n_f0)
            # kontrybucja: o ile wlasciwy ton przewyzsza SREDNIA (znormalizowane).
            # Odporne na 1 sasiada (sasiad podnosi srednia tylko o ~1/8).
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

    # Non-max suppression: znajdz lokalne maksima w score_map
    flat_idx = np.argsort(score_map, axis=None)[::-1]
    taken = np.zeros_like(score_map, dtype=bool)
    # promien suppresji wyrazony w JEDNOSTKACH INDEKSU score_map (nie t0_step/f0_step
    # bezposrednio z probek), wiec przeliczamy wzgledem faktycznego kroku siatki
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
