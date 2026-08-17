"""
FT4 RX Decoder - pelny pipeline: audio (~7.5s, 12000Hz, mono float) -> lista
zdekodowanych wiadomosci.

Etapy (kazdy zweryfikowany osobno):
  1. sync_ft4.find_candidates      - zgrubne wyszukiwanie pozycji (czas, freq),
                                      uzywa 4 roznych wzorcow Costas (nie 1x3)
  2. demod_ft4.extract_tone_power   - demodulacja bezposrednio z pozycji
                                      find_candidates (BEZ refine_sync — patrz
                                      uzasadnienie w decode_window ponizej:
                                      profiling wykazal ze refine_sync byl
                                      waskim gardlem ~8s/kandydat dla
                                      marginalnej poprawy jakosci)
  3. demod_ft4.extract_llr174       - miekkie LLR dla 174 bitow LDPC (2bit/symbol)
  4. ldpc_decode.bp_decode          - belief propagation (REUZYWANE 1:1 z FT8 —
                                      ta sama macierz parzystosci Nm/Mn)
  5. CRC-14 check                   - na bitach PRZED de-scramblingiem (bo
                                      enkoder liczy CRC na scrambled77, patrz
                                      ft4_encoder.py::encode_message_ft4)
  6. DESCRAMBLING                   - XOR z RVEC, jedyny krok specyficzny dla
                                      FT4 ktory nie ma odpowiednika w FT8
  7. unpack.unpack77                - bity -> czytelny tekst (REUZYWANE 1:1
                                      z FT8 — operuje na "czystych" 77 bitach,
                                      identycznych niezaleznie od protokolu)

ZWERYFIKOWANE na syntetycznym sygnale (sesja deweloperska 2026-06-21):
  - sync_ft4.find_candidates: wykrywa wygenerowany sygnal z dokladnoscia
    0.0Hz / 4ms na surowej siatce, bez falszywych alarmow
  - Pelny pipeline (BEZ refine_sync): 103/103 symboli odzyskanych idealnie
    poprawnie (100% zgodnosc) w czystym sygnale; quality 0.94-1.00 i poprawne
    LDPC decode w testach z szumem (std 0.0-0.5) i 8 jednoczesnymi sygnalami
    w pasmie (wszystkie 8/8 zdekodowane poprawnie)
  - Wydajnosc: decode_window dla 8 jednoczesnych sygnalow = ~0.05s (vs 18.4s
    PRZED usunieciem refine_sync) — mieszczy sie wielokrotnie w oknie 7.5s
  - Pelny round-trip (ten plik): TX->RX dla wielu typow wiadomosci (CQ,
    raport, R-raport, RRR, 73, RR73) z poprawnym tekstem wyjsciowym

NIEZWERYFIKOWANE wobec prawdziwego dekodera FT4 ani prawdziwego
sygnalu radiowego — wymaga testu na zywo, analogicznie do FT8 (patrz
ft8_rx_decoder.py docstring o nagraniu 210703_133430.wav).

Uzycie:
    from ft4_rx_decoder import decode_window
    messages = decode_window(audio_75s_float_array)
    for m in messages:
        print(m['freq_hz'], m['message'], m['snr_db'])
"""
import numpy as np

import ft8_encoder as fe  # _crc14 jest protokolo-niezalezne, reuzywane wprost
from params_ft4 import RVEC
from sync_ft4 import find_candidates
from demod_ft4 import extract_tone_power, costas_sync_quality, extract_llr174
from ldpc_decode import bp_decode
from unpack import unpack77, format_message


def _descramble77(bits77):
    """XOR z RVEC — wlasna odwrotnosc, identyczna operacja co
    ft4_encoder._scramble77 (ten sam XOR dziala w obie strony)."""
    a = np.array(bits77, dtype=np.int32)
    r = np.array(RVEC, dtype=np.int32)
    return list(np.mod(a + r, 2))


def _compute_window_noise_floor(audio, sample_rate=12000):
    """Spectral PSD + noise floor for the whole FT4 window, computed once.
    See ft8_rx_decoder for the rationale — same approach, FT4 uses a shorter
    (7.5s) window but the SNR reference is identical: signal vs background."""
    nfft = 2048
    step = nfft // 2
    win = np.hanning(nfft)
    powers = []
    for i in range(0, len(audio) - nfft, step):
        powers.append(np.abs(np.fft.rfft(audio[i:i+nfft] * win)) ** 2)
    if not powers:
        return None, None, 1e-12
    psd = np.mean(powers, axis=0)
    freqs = np.fft.rfftfreq(nfft, 1.0 / sample_rate)
    band = (freqs >= 200) & (freqs <= 3000)
    nf = float(np.percentile(psd[band], 10)) if band.any() else 1e-12
    return psd, freqs, nf


# Same calibration as FT8 (measured against reference FT8 decodes).
_SNR_SCALE = 1.09
_SNR_OFFSET = -20.2

def _estimate_snr_db(psd, freqs, noise_floor, freq_hz):
    """Real SNR: signal power at its frequency vs the window's spectral noise
    floor, mapped to a familiar dB scale. Replaces the old tone-purity method
    that gave every station nearly the same value."""
    if psd is None or freqs is None:
        return 0.0
    b = (freqs >= freq_hz - 50) & (freqs <= freq_hz + 50)
    sig_power = float(np.max(psd[b])) if b.any() else 1e-12
    raw = 10.0 * np.log10(sig_power / (noise_floor + 1e-12))
    return float(np.clip(_SNR_SCALE * raw + _SNR_OFFSET, -28, 20))


def decode_window(audio, sample_rate=12000, min_score=0.15, max_candidates=60,
                   ldpc_max_iters=50, dedup_freq_hz=8.0, dedup_time_s=0.1):
    """
    Dekoduje jedno okno audio (typowo 7.5s, ale dziala na dowolnej dlugosci
    >= ok. 6s) i zwraca liste zdekodowanych wiadomosci.

    Zwraca: lista dict, posortowana po freq_hz:
        {freq_hz, time_offset_s, message, call_to, call_de, report_or_grid,
         snr_db, ldpc_iters, sync_quality}
    """
    if sample_rate != 12000:
        raise ValueError(f"decode_window oczekuje sample_rate=12000, otrzymano {sample_rate}")

    candidates = find_candidates(audio, max_candidates=max_candidates, min_score=min_score)
    _psd, _freqs, _noise_floor = _compute_window_noise_floor(audio, sample_rate)

    decoded = []
    for c in candidates:
        # UWAGA: refine_sync ZAMIERZENIE pominiety tutaj — profiling wykazal
        # ze byl dominujacym waskim gardlem (~8s/kandydat z petla freq x time
        # przeszukania), podczas gdy find_candidates' domyslna siatka
        # nadprobkowania (time_osr=2, freq_osr=2) juz daje sync_quality
        # 0.94-1.00 i bezbledne dekodowanie LDPC w testach z 1-8 jednoczesnymi
        # sygnalami i poziomami szumu 0.0-0.5. Usuniecie tego kroku
        # przyspieszylo decode_window z 18.4s do ~0.05s dla 8 sygnalow,
        # niezbedne zeby zmiescic sie w 7.5s oknie FT4. Funkcja refine_sync
        # nadal istnieje w demod_ft4.py (dostepna do debugowania/przyszlego
        # dostrajania), po prostu nie jest uzywana w domyslnym pipeline.
        power = extract_tone_power(audio, c['freq_hz'], c['time_offset_s'])
        quality = costas_sync_quality(power)
        rfreq, rtime = c['freq_hz'], c['time_offset_s']

        llr174 = extract_llr174(power)
        hard, success, n_iters = bp_decode(llr174, max_iters=ldpc_max_iters)
        if not success:
            continue

        bits91 = hard[:91]
        scrambled77 = bits91[:77]
        crc_received = bits91[77:91]
        # CRC liczone na SCRAMBLED bitach (zgodnie z enkoderem — patrz
        # ft4_encoder.encode_message_ft4, gdzie crc = _crc14(scrambled77+pad))
        padded82 = scrambled77 + [0, 0, 0, 0, 0]
        crc_check = fe._crc14(padded82)
        if crc_received != crc_check:
            continue

        # Descrambling DOPIERO PO weryfikacji CRC — odzyskuje oryginalne
        # 77 bitow, identyczne strukturalnie z tym co produkuje FT8's pack77,
        # wiec unpack77/format_message dzialaja bez zadnych zmian.
        data77 = _descramble77(scrambled77)

        parsed = unpack77(data77)
        msg = format_message(parsed)
        snr_db = _estimate_snr_db(_psd, _freqs, _noise_floor, c['freq_hz'])

        decoded.append({
            'freq_hz': rfreq,
            'time_offset_s': rtime,
            'message': msg,
            'call_to': parsed['call_to'],
            'call_de': parsed['call_de'],
            'report_or_grid': parsed['report_or_grid'],
            'snr_db': round(snr_db, 1),
            'ldpc_iters': n_iters,
            'sync_quality': round(quality, 3),
        })

    decoded = _dedup(decoded, dedup_freq_hz, dedup_time_s)
    decoded.sort(key=lambda d: d['freq_hz'])
    return decoded


def _dedup(decoded, freq_tol_hz, time_tol_s):
    """Identyczna logika co FT8's _dedup."""
    if not decoded:
        return decoded
    decoded_sorted = sorted(decoded, key=lambda d: -d['snr_db'])
    kept = []
    for d in decoded_sorted:
        is_dup = False
        for k in kept:
            if (d['message'] == k['message'] and
                    abs(d['freq_hz'] - k['freq_hz']) <= freq_tol_hz and
                    abs(d['time_offset_s'] - k['time_offset_s']) <= time_tol_s):
                is_dup = True
                break
        if not is_dup:
            kept.append(d)
    return kept
