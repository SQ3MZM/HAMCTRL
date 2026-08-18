"""
FT4 RX Decoder - full pipeline: audio (~7.5s, 12000Hz, mono float) -> list
of decoded messages.

Stages (each verified independently):
  1. sync_ft4.find_candidates      - coarse position search (time, freq),
                                      uses 4 different Costas patterns (not 1x3)
  2. demod_ft4.extract_tone_power   - demodulation directly from the
                                      find_candidates position (WITHOUT
                                      refine_sync — see the rationale in
                                      decode_window below: profiling showed
                                      refine_sync was the bottleneck at
                                      ~8s/candidate for marginal quality gain)
  3. demod_ft4.extract_llr174       - soft LLRs for the 174 LDPC bits (2bit/symbol)
  4. ldpc_decode.bp_decode          - belief propagation (REUSED 1:1 from FT8 —
                                      the same Nm/Mn parity-check matrix)
  5. CRC-14 check                   - on the bits BEFORE de-scrambling (since
                                      the encoder computes the CRC on scrambled77,
                                      see ft4_encoder.py::encode_message_ft4)
  6. DESCRAMBLING                   - XOR with RVEC, the only FT4-specific
                                      step with no FT8 counterpart
  7. unpack.unpack77                - bits -> readable text (REUSED 1:1 from
                                      FT8 — operates on the "clean" 77 bits,
                                      identical regardless of the protocol)

Verified against a synthetic signal:
  - sync_ft4.find_candidates: detects a generated signal accurately to
    0.0Hz / 4ms on the raw grid, with no false positives
  - Full pipeline (WITHOUT refine_sync): 103/103 symbols recovered
    perfectly (100% match) on a clean signal; quality 0.94-1.00 and
    correct LDPC decode in tests with noise (std 0.0-0.5) and 8 simultaneous
    signals in the band (all 8/8 decoded correctly)
  - Performance: decode_window for 8 simultaneous signals = ~0.05s (vs 18.4s
    BEFORE removing refine_sync) — fits comfortably within the 7.5s window
  - Full round-trip (this file): TX->RX for various message types (CQ,
    report, R-report, RRR, 73, RR73) with correct output text

NOT YET VERIFIED against a real FT4 decoder or a real radio signal —
needs a live test, same as FT8 (see ft8_rx_decoder.py's docstring about
the 210703_133430.wav recording).

Usage:
    from ft4_rx_decoder import decode_window
    messages = decode_window(audio_75s_float_array)
    for m in messages:
        print(m['freq_hz'], m['message'], m['snr_db'])
"""
import numpy as np

import ft8_encoder as fe  # _crc14 is protocol-independent, reused directly
from params_ft4 import RVEC
from sync_ft4 import find_candidates
from demod_ft4 import extract_tone_power, costas_sync_quality, extract_llr174
from ldpc_decode import bp_decode
from unpack import unpack77, format_message


def _descramble77(bits77):
    """XOR with RVEC — its own inverse, the same operation as
    ft4_encoder._scramble77 (the same XOR works both ways)."""
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
    Decodes one audio window (typically 7.5s, but works with any length
    >= roughly 6s) and returns a list of decoded messages.

    Returns: a list of dicts, sorted by freq_hz:
        {freq_hz, time_offset_s, message, call_to, call_de, report_or_grid,
         snr_db, ldpc_iters, sync_quality}
    """
    if sample_rate != 12000:
        raise ValueError(f"decode_window expects sample_rate=12000, got {sample_rate}")

    candidates = find_candidates(audio, max_candidates=max_candidates, min_score=min_score)
    _psd, _freqs, _noise_floor = _compute_window_noise_floor(audio, sample_rate)

    decoded = []
    for c in candidates:
        # NOTE: refine_sync is DELIBERATELY skipped here — profiling showed
        # it was the dominant bottleneck (~8s/candidate with its freq x
        # time search loop), while find_candidates' default oversampling
        # grid (time_osr=2, freq_osr=2) already gives sync_quality
        # 0.94-1.00 and flawless LDPC decoding in tests with 1-8
        # simultaneous signals and noise levels 0.0-0.5. Removing this step
        # sped up decode_window from 18.4s to ~0.05s for 8 signals,
        # necessary to fit within FT4's 7.5s window. The refine_sync
        # function still exists in demod_ft4.py (available for
        # debugging/future tuning), it's just not used in the default pipeline.
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
        # CRC is computed on the SCRAMBLED bits (matching the encoder — see
        # ft4_encoder.encode_message_ft4, where crc = _crc14(scrambled77+pad))
        padded82 = scrambled77 + [0, 0, 0, 0, 0]
        crc_check = fe._crc14(padded82)
        if crc_received != crc_check:
            continue

        # Descrambling happens ONLY AFTER CRC verification — recovers the
        # original 77 bits, structurally identical to what FT8's pack77
        # produces, so unpack77/format_message work without any changes.
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
    """Identical logic to FT8's _dedup."""
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
