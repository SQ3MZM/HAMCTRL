"""
ft4_encoder.py — Wlasny enkoder/nadajnik FT4.

FT4 dzieli z FT8 cala czesc kodowania zrodlowego az do dodania bitow
parzystosci: pack77 (77 bitow), CRC-14 (ten sam wielomian 0x2757), LDPC(174,91)
(te same macierze Nm/Mn/rawg). Te funkcje sa REUZYWANE bez zmian z
ft8_encoder.py — patrz import ponizej.

Roznice wzgledem FT8 zaczynaja sie PO pack77 i trwaja az do koncowej ramki:
  1. SCRAMBLING: 77 bitow zrodlowych XOR z mask 'rvec' PRZED dolaczeniem CRC
     (FT8 nie ma scramblingu — to jedyna prawdziwa roznica w warstwie zrodlowej)
  2. Gray mapping: 2 bity/symbol (nie 3 jak FT8) -> 4 tony (nie 8) -> 87 symboli
     danych (z 174 bitow), nie 58
  3. Struktura ramki: CZTERY rozne 4-symbolowe wzorce Costas (nie jeden
     7-symbolowy jak FT8), przeplatane z trzema blokami po 29 symboli danych:
     Costas1(4) + Block1(29) + Costas2(4) + Block2(29) + Costas3(4) +
     Block3(29) + Costas4(4) = 103 symbole total
  4. Modulacja: 4-GFSK (nie 8-GFSK), tone spacing 20.833Hz (nie 6.25Hz),
     symbol period 0.048s (nie 0.16s), BT=1.0 (nie 2.0) — sygnal znacznie
     szybszy: ~5s transmisji w oknie 7.5s (vs ~12.6s w oknie 15s dla FT8)

ZRODLO SPECYFIKACJI (sesja deweloperska 2026-06-21):
  - Maska scramblingu 'rvec' (77 bitow), costas_symbols (4 wzorce 4x4),
    costas_offsets ([0,33,66,99]) oraz graymap ([0,1,3,2]) zweryfikowane
    krzyzowo miedzy dwoma niezaleznymi publicznymi zrodlami specyfikacji
    protokolu FT4.
  - Struktura ramki (4x Costas + 3x29 blok danych = 103 symbole, plus
    "ramped null symbol" na poczatku/koncu = 105 total) potwierdzona w OBU
    zrodlach (ten sam wzorzec Sync1+Block1+Sync2+Block2+Sync3+Block3+Sync4).
  - Tone spacing 20.833Hz / symbol period 0.048s potwierdzone w OBU
    zrodlach ("576-point FFT @ 12000 sps" / "tone spacing 20.833Hz,
    symbol interval 48ms").

NIEZWERYFIKOWANE wobec prawdziwego dekodera FT4 — wymaga testu na zywo
z prawdziwym radiem lub porownania z referencyjnym nagraniem .wav.

DEKODER (RX) — NIE ZAIMPLEMENTOWANY w tym module. Wymaga osobnego pipeline
analogicznego do ft8_rx_decoder.py, z innym Costas sync (4 wzorce zamiast 1,
szukane na konkretnych offsetach 0/33/66/99) i innym FFT bin size
(576-punktowy FFT @ 12000Hz). To osobny, duzy etap (kolejna sesja).
"""
import numpy as np
from scipy.special import erf

# Reuzywamy bez zmian: pack77 (77-bit source encoding), _crc14, _ldpc_encode,
# _ldpc_check, _build_gen_sys (wywolane juz przy imporcie ft8_encoder).
from ft8_encoder import (
    pack77, _crc14, _ldpc_encode, _ldpc_check,
)

# ============================================================
# CZESC 1: Stale specyficzne dla FT4
# ============================================================

# Maska scramblingu — 77 bitow, XOR z pack77() PRZED dolaczeniem CRC.
# Cel: unikniecie dlugich ciagow zer przy wiadomosciach typu CQ (ktore maja
# duzo zerowych bitow w surowym pakowaniu). FT8 NIE ma tego kroku.
_RVEC = np.array([
    0, 1, 0, 0, 1, 0, 1, 0, 0, 1, 0, 1, 1, 1, 1, 0, 1, 0, 0, 0, 1, 0, 0, 1, 1, 0, 1, 1, 0,
    1, 0, 0, 1, 0, 1, 1, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 1, 0, 0, 1, 1, 1, 1, 0, 0, 1, 0, 1,
    0, 1, 0, 1, 0, 1, 1, 0, 1, 1, 1, 1, 1, 0, 0, 0, 1, 0, 1
], dtype=np.int32)
assert len(_RVEC) == 77

# Cztery rozne 4-symbolowe wzorce Costas (sync), kazdy uzyty raz w ramce.
_COSTAS_FT4 = [
    [0, 1, 3, 2],   # Sync1
    [1, 0, 2, 3],   # Sync2
    [2, 3, 1, 0],   # Sync3
    [3, 2, 0, 1],   # Sync4
]
# Pozycje startowe (0-indexed) kazdego bloku Costas w 103-symbolowej ramce.
_COSTAS_OFFSETS = [0, 33, 66, 99]

# Gray mapping dla par bitow (2 bity/symbol, 4 tony) — analogiczny wzor co
# FT8's _GRAYMAP ale dla 2-bitowych indeksow zamiast 3-bitowych.
# '00'->0, '01'->1, '10'->3, '11'->2 (potwierdzone w g4jnt.com i weakmon)
_GRAYMAP_FT4 = [0, 1, 3, 2]


def _scramble77(bits77):
    """XOR 77 bitow z maska _RVEC. Operacja jest wlasna odwrotnoscia
    (x XOR mask XOR mask == x), wiec ta sama funkcja sluzy do scramblingu
    przy kodowaniu i de-scramblingu przy dekodowaniu."""
    assert len(bits77) == 77
    a = np.array(bits77, dtype=np.int32)
    return list(np.mod(a + _RVEC, 2))


def encode_message_ft4(call_to, call_de, report_or_grid, r_flag=False):
    """
    Pelny pipeline FT4: tekst -> 77-bit pack -> scrambling -> CRC-14 ->
    LDPC(174,91) -> Gray mapping (2bit) -> wstawienie 4x Costas -> 103
    symbole (0-3).

    Zwraca (symbols103, debug_dict). Analogiczne do ft8_encoder.encode_message
    ale z dodatkowym krokiem scramblingu i inna struktura ramki.
    """
    bits77 = pack77(call_to, call_de, report_or_grid, r_flag)
    scrambled77 = _scramble77(bits77)

    # CRC liczone na PRZESKRAMBLOWANYCH bitach (tak jak w pipeline FT8,
    # _crc14 oczekuje 82 bitow = 77 + 5 zer paddingu)
    padded82 = scrambled77 + [0, 0, 0, 0, 0]
    crc = _crc14(padded82)
    bits91 = np.array(scrambled77 + crc, dtype=np.int32)
    codeword174 = _ldpc_encode(bits91)

    # Gray-map pary bitow (174 bity -> 87 symboli, 2 bity kazdy)
    data_symbols = []
    for i in range(0, 174, 2):
        b0, b1 = codeword174[i], codeword174[i + 1]
        idx = b0 * 2 + b1
        data_symbols.append(_GRAYMAP_FT4[idx])
    assert len(data_symbols) == 87

    # Podziel 87 symboli danych na 3 bloki po 29 i przeplec z 4x Costas
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
# CZESC 2: Generator audio 4-GFSK
# ============================================================

FT4_SAMPLE_RATE = 12000
FT4_SYMBOL_PERIOD = 0.048  # 48ms — potwierdzone w 2 niezaleznych zrodlach
FT4_SAMPLES_PER_SYMBOL = round(FT4_SAMPLE_RATE * FT4_SYMBOL_PERIOD)  # 576
FT4_TONE_SPACING = FT4_SAMPLE_RATE / FT4_SAMPLES_PER_SYMBOL  # 20.8333... Hz
FT4_BT = 1.0  # FT4 uzywa filtr Gaussa bardziej wygladzajacy niz FT8 (BT=2.0)

FT4_SLOT_TIME = 7.5  # okno T/R w sekundach (vs 15.0 dla FT8)

TARGET_SAMPLE_RATE = 48000  # wymagane przez feed_tx_pcm (audio_stream.py)


def _gaussian_pulse_ft4(t, bt, symbol_period):
    """Identyczna formula co FT8's _gaussian_pulse, ale BT i symbol_period
    sa parametrami specyficznymi dla FT4 (BT=1.0, period=0.048s)."""
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
    """103 symbole (0-3) -> numpy float32 PCM @ 12000Hz, znormalizowany -1..1.

    Identyczna logika co FT8's synthesize_gfsk (sumowanie nakladajacych sie
    gaussowskich impulsow czestotliwosci), tylko z parametrami FT4
    (4 tony zamiast 8, mniejszy tone spacing, krotszy symbol period)."""
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
    Pelny pipeline: tekst -> 103 symbole -> audio 12kHz -> resample 48kHz ->
    int16 PCM bytes, gotowe do feed_tx_pcm(). Interfejs identyczny z
    ft8_encoder.generate_tx_pcm48k (ten sam typ zwracanej wartosci), zeby
    kod wywolujacy (_ft8_tx_sequence_inner w webapp.py) mogl uzywac obu
    enkoderow zamiennie w zaleznosci od wybranego trybu.

    Domyslna amplitude=0.12 — identyczna wartosc i to samo uzasadnienie
    anty-clippingowe co w ft8_encoder (patrz tamtejszy docstring): backend
    dodatkowo mnozy przez txVolume (max 8.0), wiec 0.12*8.0=0.96 zostaje
    bezpiecznie ponizej clippingu.

    Zwraca (pcm_bytes, debug_dict, duration_seconds).
    """
    from scipy.signal import resample_poly
    symbols103, debug = encode_message_ft4(call_to, call_de, report_or_grid, r_flag)
    audio_12k = synthesize_gfsk_ft4(symbols103, base_freq_hz=base_freq_hz)
    audio_48k = resample_poly(audio_12k, up=4, down=1)
    pcm16 = np.clip(audio_48k * 32767 * amplitude, -32768, 32767).astype(np.int16)
    duration = len(audio_12k) / FT4_SAMPLE_RATE
    return pcm16.tobytes(), debug, duration


if __name__ == "__main__":
    # Self-test przy uruchomieniu modulu bezposrednio
    print("=== FT4 Encoder self-test ===")
    print(f"FT4_SAMPLES_PER_SYMBOL = {FT4_SAMPLES_PER_SYMBOL}")
    print(f"FT4_TONE_SPACING = {FT4_TONE_SPACING:.4f} Hz")
    print(f"Czas transmisji (103 symbole) = {103 * FT4_SYMBOL_PERIOD:.3f}s")

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
        print(f"  '{call_to} {call_de} {rg}' -> {len(symbols)} symboli, "
              f"ldpc_valid={debug['ldpc_valid']} [{status}]")
