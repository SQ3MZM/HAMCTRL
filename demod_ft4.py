"""
Etap 2 (FT4): Demodulacja - z audio + pozycji kandydata (freq_hz,
time_offset_s) wyciagamy 103 symbole (twarda decyzja) oraz 174 miekkie
LLR-y (do pelnego LDPC belief-propagation pozniej).

Mapowanie bit<->symbol: 103 symbole zawieraja 4x4=16 symboli synchronizacji
Costas (ignorowane przy dekodowaniu danych) + 87 symboli danych. Kazdy
symbol danych koduje GRAY-mapped 2 bity z 174-bitowego kodu LDPC(174,91).
Zgodne z naszym enkoderem (ft4_encoder.py): _GRAYMAP_FT4 i ulozenie 4x
Costas na pozycjach [0:4], [33:37], [66:70], [99:103].
"""
import numpy as np
from params_ft4 import (SAMPLE_RATE, SAMPLES_PER_SYMBOL, N_TONES,
                         COSTAS_PATTERNS, COSTAS_POS, N_SYM, TONE_SPACING,
                         GRAYMAP, INV_GRAYMAP)

# Pozycje symboli danych w 103-symbolowej ramce (po pominieciu 4x4=16 Costas):
# symbols103 = C1[0:4] + data[0:29] + C2[33:37] + data[29:58] + C3[66:70] +
#              data[58:87] + C4[99:103]
DATA_SYM_INDICES = (list(range(4, 33)) + list(range(37, 66)) + list(range(70, 99)))
assert len(DATA_SYM_INDICES) == 87


def extract_tone_power(audio, freq_hz, time_offset_s, freq_osr=2):
    """
    Dla danej pozycji (freq_hz = czestotliwosc tonu 0, time_offset_s =
    poczatek pierwszego symbolu), wyciaga macierz mocy [103 symboli x 4 tony]
    poprzez korelacje z czystymi tonami (FFT na kazdym oknie symbolu).

    ZWEKTORYZOWANE (w odroznieniu od FT8's demod.py, ktore uzywa prostej
    petli Pythona z osobnym FFT na kazdy symbol): dla FT4 ta funkcja jest
    wywolywana ~1500x w pojedynczym refine_sync() (siatka freq x time), a
    sama petla nieZwektoryzowana zajmowala ~8s na wywolanie decode_window —
    zbyt wolno wzgledem 7.5s okna FT4. Tutaj budujemy macierz wszystkich 103
    segmentow naraz i robimy JEDNO wsadowe FFT (axis=1), zamiast 103 osobnych
    wywolan np.fft.rfft. Wynik numerycznie identyczny z wersja petlowa.
    """
    start_sample = int(round(time_offset_s * SAMPLE_RATE))
    n = SAMPLES_PER_SYMBOL
    nfft = n * 4
    window = np.hanning(n)

    # Zbuduj macierz segmentow [N_SYM x n], z zerowym wypelnieniem dla
    # symboli wykraczajacych poza dostepne audio (identyczne zachowanie co
    # wersja petlowa: power[sym,:]=0 dla takich pozycji)
    segs = np.zeros((N_SYM, n), dtype=np.float64)
    valid = np.ones(N_SYM, dtype=bool)
    for sym in range(N_SYM):
        s0 = start_sample + sym * n
        s1 = s0 + n
        if s0 < 0 or s1 > len(audio):
            valid[sym] = False
            continue
        segs[sym] = audio[s0:s1]

    segs *= window[None, :]  # okno Hanninga na kazdym wierszu naraz

    # JEDNO wsadowe FFT zamiast 103 osobnych wywolan
    spec = np.fft.rfft(segs, n=nfft, axis=1)  # (N_SYM, nfft//2+1)
    freqs = np.fft.rfftfreq(nfft, d=1.0 / SAMPLE_RATE)

    # Indeksy binow dla 4 tonow sa identyczne dla wszystkich symboli (zalezy
    # tylko od freq_hz), wiec licz raz, nie w petli
    tone_idx = np.array([np.argmin(np.abs(freqs - (freq_hz + tone * TONE_SPACING)))
                          for tone in range(N_TONES)])

    power = np.abs(spec[:, tone_idx]) ** 2  # (N_SYM, N_TONES)
    power[~valid, :] = 0
    return power


def costas_sync_quality(power):
    """Mierzy jak dobrze symbole na pozycjach Costas pasuja do WLASCIWEGO
    wzorca KAZDEGO bloku (rozne wzorce na roznych pozycjach, w odroznieniu
    od FT8 gdzie to ten sam wzorzec wszedzie)."""
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
    """Zwraca 103 wartosci tonow (0-3) przez twarda decyzje (argmax)."""
    return np.argmax(power, axis=1)


def extract_bits174(power):
    """
    Z macierzy mocy [103 x 4] wyciaga 174 twarde bity kodu LDPC (PRZED
    de-scramblingiem), uzywajac DOKLADNIE tej samej tabeli Gray co enkoder
    (odwroconej, 2 bity/symbol zamiast 3).
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
    Z macierzy mocy [103 x 4] liczy miekkie LLR dla 174 bitow LDPC (PRZED
    de-scramblingiem — scrambling odwracamy dopiero PO LDPC decode, na
    poziomie bitow planiteksu, analogicznie jak w ft4_encoder.py gdzie
    scrambling jest stosowany PRZED CRC/LDPC encode).

    Identyczna metoda max-log-MAP co FT8, dostosowana do 2 bitow/symbol
    (4 tony) zamiast 3 bitow/symbol (8 tonow).
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
    Dopracowuje pozycje kandydata przez lokalne przeszukanie, maksymalizujac
    jakosc dopasowania 4x Costas.

    Zakresy domyslne sa CELOWO WASKIE: ograniczone do ok. +-1 kroku siatki
    nadpróbkowania z find_candidates() (freq_osr=2 -> krok ~10.4Hz,
    time_osr=2 -> krok ~24ms), bo sync_ft4.find_candidates juz dziala na
    nadprobkowanej siatce i typowo trafia bardzo blisko prawdziwej pozycji.
    Testy (sesja 2026-06-21) pokazaly: ta waska siatka (99 iteracji zamiast
    1558 dla szerszej siatki ~FT8-stylu) nadal daje sync_quality=1.0 i
    >=100/103 poprawnych symboli nawet z dodanym szumem — pozostale do 3
    bledy sa i tak korygowane przez LDPC belief propagation. To ~15x
    przyspieszenie jest KONIECZNE zeby decode_window zmiescilo sie w oknie
    7.5s FT4 przy wielu jednoczesnych kandydatach w pasmie (w odroznieniu
    od FT8, gdzie 15s okno daje wiekszy margines czasowy).
    Zwraca (best_freq, best_time, best_power, best_quality).
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
