"""
Etap 2: Demodulacja - z audio + pozycji kandydata (freq_hz, time_offset_s)
wyciagamy 79 symboli (twarda decyzja, do szybkiej weryfikacji syncu) oraz
174 miekkie LLR-y (do pelnego LDPC belief-propagation pozniej).

Mapowanie bit<->symbol: 79 symboli zawiera 3x7=21 symboli synchronizacji
Costas (ignorowane przy dekodowaniu danych) + 58 symboli danych. Kazdy
symbol danych koduje GRAY-mapped 3 bity z 174-bitowego kodu LDPC(174,91).
Zgodne z naszym enkoderem (ft8_encoder.py): _SYMBOL_GRAY tablica Gray coding
i ulozenie Costas na pozycjach [0:7], [36:43], [72:79].
"""
import numpy as np
from params import SAMPLE_RATE, SAMPLES_PER_SYMBOL, N_TONES, COSTAS, COSTAS_POS, N_SYM

# DOKLADNIE ta sama tabela co w ft8_encoder.py (_GRAYMAP), zweryfikowana
# przez odczyt zrodla. idx (3-bit value 0-7) -> tone (symbol nadawany).
GRAYMAP = [0, 1, 3, 2, 5, 6, 4, 7]
# Odwrotnosc: tone -> idx (3-bit value), potrzebna do dekodowania
GRAYMAP_INV = [0] * 8
for _idx, _tone in enumerate(GRAYMAP):
    GRAYMAP_INV[_tone] = _idx

# Pozycje symboli danych w 79-symbolowej ramce (po pominieciu 3x7=21 Costas):
# symbols79 = Costas[0:7] + data[0:29] + Costas[36:43] + data[29:58] + Costas[72:79]
DATA_SYM_INDICES = list(range(7, 36)) + list(range(43, 72))
assert len(DATA_SYM_INDICES) == 58


def extract_tone_power(audio, freq_hz, time_offset_s, freq_osr=2):
    """
    Dla danej pozycji (freq_hz = czestotliwosc tonu 0, time_offset_s = poczatek
    pierwszego symbolu), wyciaga macierz mocy [79 symboli x 8 tonow] poprzez
    korelacje z czystymi tonami (Goertzel-like, przez FFT na każdym oknie symbolu).
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
        spec = np.fft.rfft(seg, n=n * 4)  # zero-padding dla lepszej rozdzielczosci czest.
        freqs = np.fft.rfftfreq(n * 4, d=1.0 / SAMPLE_RATE)
        for tone in range(N_TONES):
            f_target = freq_hz + tone * tone_spacing
            idx = np.argmin(np.abs(freqs - f_target))
            power[sym, tone] = np.abs(spec[idx]) ** 2

    return power


def costas_sync_quality(power):
    """Mierzy jak dobrze symbole na pozycjach Costas pasuja do wzorca (0..1)."""
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
    """Zwraca 79 wartosci tonow (0-7) przez twarda decyzje (argmax)."""
    return np.argmax(power, axis=1)


def extract_bits174(power):
    """
    Z macierzy mocy [79 x 8] wyciaga 174 twarde bity kodu LDPC, uzywajac
    DOKLADNIE tej samej tabeli Gray co enkoder (odwroconej).
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
    Z macierzy mocy [79 x 8] liczy miekkie LLR dla 174 bitow LDPC.
    LLR dodatni = bit bardziej prawdopodobnie 0, ujemny = bit bardziej
    prawdopodobnie 1 (konwencja: log(P(bit=0)/P(bit=1))).

    Uproszczone podejscie: dla kazdej z 3 pozycji bitowych w symbolu,
    sumujemy (w dziedzinie log) moc tonow ktore daja bit=0 vs bit=1
    wedlug odwroconej tabeli Gray, na zasadzie max-log-MAP.
    """
    llrs = []
    # tone_to_bits[tone] = (b0,b1,b2) odpowiadajace temu tonowi
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
    Dopracowuje pozycje kandydata (zgrubna z sync.py, kwantyzowana do
    siatki nadpróbkowania) przez lokalne przeszukanie wokol niej,
    maksymalizujac jakosc dopasowania Costas. Niezbedne bo demodulacja
    jest bardzo czula na dokladnosc pozycji (np. 100% zgodnosc symboli
    przy idealnej pozycji vs 14% przy przesunieciu o pol kroku siatki).
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
