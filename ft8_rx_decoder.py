"""
FT8 RX Decoder - full pipeline: audio (15s, 12000Hz, mono float) -> list of
decoded messages.

Stages (each verified independently):
  1. sync.find_candidates       - coarse position search (time, freq)
  2. demod.extract_tone_power   - demodulation (79-symbol x 8-tone power matrix)
  3. demod.extract_llr174       - soft LLRs for the 174 LDPC bits
  4. ldpc_decode.bp_decode      - belief propagation, corrects bit errors
  5. CRC-14 check                - rejects false/bad decodes
  6. unpack.unpack77             - bits -> readable text (callsign/grid/report)

Verified end-to-end against a real FT8 recording (210703_133430.wav):
5/12 sync candidates decoded correctly into sensible FT8 messages.

Usage:
    from ft8_rx_decoder import decode_window
    messages = decode_window(audio_15s_float_array)
    for m in messages:
        print(m['freq_hz'], m['message'], m['snr_db'])
"""
import numpy as np

import ft8_encoder as fe
from sync import find_candidates
from demod import extract_tone_power, costas_sync_quality, extract_llr174
from ldpc_decode import bp_decode
from unpack import unpack77, format_message


def _compute_window_noise_floor(audio, sample_rate=12000):
    """Compute the spectral power density and noise floor for the whole window,
    once. Returns (psd, freqs, noise_floor). The noise floor is a low percentile
    of in-band power — the background between signals — which is the right
    reference for SNR."""
    nfft = 2048
    step = nfft // 2
    win = np.hanning(nfft)
    powers = []
    for i in range(0, len(audio) - nfft, step):
        seg = audio[i:i+nfft] * win
        powers.append(np.abs(np.fft.rfft(seg)) ** 2)
    if not powers:
        return None, None, 1e-12
    psd = np.mean(powers, axis=0)
    freqs = np.fft.rfftfreq(nfft, 1.0 / sample_rate)
    band = (freqs >= 200) & (freqs <= 3000)
    noise_floor = float(np.percentile(psd[band], 10)) if band.any() else 1e-12
    return psd, freqs, noise_floor


# Calibration from measurement against reference FT8 decodes (ft8.wav):
# correlation 0.74, snr_db ≈ 1.09*raw − 20.2. Kept simple and linear.
_SNR_SCALE = 1.09
_SNR_OFFSET = -20.2

def _estimate_snr_db(psd, freqs, noise_floor, freq_hz):
    """Estimate SNR from real signal strength vs the window's spectral noise
    floor — NOT tone purity. The old method compared the winning tone to the
    other tones of the SAME symbol, which is near-constant for any decoded
    signal, so every station reported almost the same SNR (the "+20 for
    everyone" bug). Here we take the signal's power at its own frequency
    relative to the background, which actually differentiates strong from weak
    stations, then map to a familiar dB scale with the measured calibration."""
    if psd is None or freqs is None:
        return 0.0
    b = (freqs >= freq_hz - 50) & (freqs <= freq_hz + 50)
    sig_power = float(np.max(psd[b])) if b.any() else 1e-12
    raw = 10.0 * np.log10(sig_power / (noise_floor + 1e-12))
    snr = _SNR_SCALE * raw + _SNR_OFFSET
    return float(np.clip(snr, -28, 20))


def decode_window(audio, sample_rate=12000, min_score=0.15, max_candidates=60,
                   ldpc_max_iters=50, dedup_freq_hz=8.0, dedup_time_s=0.1):
    """
    Decodes one audio window (typically 15s, but works with any length
    >= roughly 13s) and returns a list of decoded messages.

    Returns: a list of dicts, sorted by freq_hz:
        {freq_hz, time_offset_s, message, call_to, call_de, report_or_grid,
         snr_db, ldpc_iters, sync_quality}
    """
    if sample_rate != 12000:
        raise ValueError(f"decode_window expects sample_rate=12000, got {sample_rate}")

    candidates = find_candidates(audio, max_candidates=max_candidates, min_score=min_score)

    # Compute the window's spectral noise floor once, up front, so every
    # station's SNR is measured against the same background (and we don't redo
    # the FFT per candidate).
    _psd, _freqs, _noise_floor = _compute_window_noise_floor(audio, sample_rate)

    decoded = []
    for c in candidates:
        power = extract_tone_power(audio, c['freq_hz'], c['time_offset_s'])
        quality = costas_sync_quality(power)
        llr174 = extract_llr174(power)

        hard, success, n_iters = bp_decode(llr174, max_iters=ldpc_max_iters)
        if not success:
            continue

        bits91 = hard[:91]
        data77 = bits91[:77]
        crc_received = bits91[77:91]
        padded82 = data77 + [0, 0, 0, 0, 0]
        crc_check = fe._crc14(padded82)
        if crc_received != crc_check:
            continue

        parsed = unpack77(data77)
        msg = format_message(parsed)
        snr_db = _estimate_snr_db(_psd, _freqs, _noise_floor, c['freq_hz'])

        decoded.append({
            'freq_hz': c['freq_hz'],
            'time_offset_s': c['time_offset_s'],
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
    """
    Removes duplicates: different sync candidates can sometimes converge on
    the same physical frame (e.g. with a wide non-max-suppression search).
    Two decoded messages are duplicates if they have IDENTICAL message text
    AND are close to each other in time/frequency.
    """
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
