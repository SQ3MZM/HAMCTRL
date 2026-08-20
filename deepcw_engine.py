"""
deepcw_engine.py — DeepCW server-side decoder

Model: e04/deepcw-engine (MIT)
- ONNX Runtime inference
- Audio PCM Float32 @ 3200 Hz via WebSocket
- CTC greedy decode -> text -> broadcast to the UI

NOTE ON TRANSLATION SCOPE: the "msg" field passed to _bcast() in download()
reaches the browser verbatim via the "deepcw_progress" WS broadcast —
public/js/ws.js renders msg.msg directly into the DOM with no I18n lookup
(same pattern as deepcw_model.py). Those four progress strings are
deliberately left in Polish; everything else (comments, docstrings,
print() logs) is translated.
"""
from __future__ import annotations
import asyncio
import json
import math
import pathlib
import time
import urllib.request

import numpy as np
from config import VERBOSE

# The model is kept in the DATA directory (APPDATA), not next to the EXE —
# otherwise every update would delete the downloaded model (the same
# problem as with the QSO log).
try:
    from config import DATA as _DATA
    MODEL_DIR = pathlib.Path(_DATA) / "deepcw"
except Exception:
    MODEL_DIR = pathlib.Path("deepcw")
MODEL_FILE = MODEL_DIR / "model.onnx"
META_FILE  = MODEL_DIR / "model.onnx.json"
MODEL_URL  = "https://raw.githubusercontent.com/e04/deepcw-engine/main/model.onnx"
META_URL   = "https://raw.githubusercontent.com/e04/deepcw-engine/main/model.onnx.json"

# ── Model metadata (from model.onnx.json) ───────────────────────────────────────
DEFAULT_META = {
    "chars": list(",./0123456789?ABCDEFGHIJKLMNOPQRSTUVWXYZ "),
    "blank_index": 41,
    "sample_rate": 3200,
    "fft_length": 256,
    "hop_length": 48,
    "spectrogram_min_freq_hz": 400.0,
    "spectrogram_max_freq_hz": 1200.0,
    "spectrogram_frequency_bins": 65,
    "normalization": "log1p",
    "onnx_input_name": "spectrogram",
    "onnx_output_name": "log_probs",
    "num_classes": 42,
}


class DeepCWEngine:
    def __init__(self):
        self.session  = None
        self.meta     = DEFAULT_META
        self._ready   = False
        # Narrow-band tone analysis (like Skimmer). Enabled by default;
        # HAM_CW_BANDPASS=0 disables it for a live comparison.
        import os as _os
        self._bandpass_on = _os.environ.get("HAM_CW_BANDPASS", "1") != "0"
        self._tone_hz = None
        self._dbg_n = 0        # window counter for sparse logging
        self._buf     = np.zeros(0, dtype=np.float32)
        # Window length is a KEY accuracy parameter. The CTC model uses
        # context: with a long window it sees a character surrounded by its
        # neighbors and self-corrects, with a short one it guesses more
        # often. Trying to shorten it to 6s (to save CPU) clearly degraded
        # decoding — instead of 'QSO HR OP JURE RST' it returned garbage
        # like '3LGDEDLTHYN'. Back to 10s; load is tuned via the STEP, not
        # the window.
        self._win_sec  = 10.0
        # 10s window — a PROVEN value at which the decoder works well.
        # Tried 8s to save CPU, but that change was "on faith" — and
        # quality matters more. Load is cut a DIFFERENT way: skipping
        # inference on an empty band (tone test below), which doesn't touch
        # the quality of decoding a real signal. 6s broke the model
        # (garbage '3LGDEDLTHYN').
        self._hop_sec  = 2.0
        # 2s step — a PROVEN value at which the decoder works as intended.
        # Tried 2.5s to lighten a dual-core test machine, but the target
        # machine will be 4-core and can handle full speed. Each part of
        # the window gets decoded 5x — more than enough for voting.
        # Recomputed in load() from the model's metadata, but they need to
        # exist right away — feed() may be called before the model loads.
        self._win_samp = int(self._win_sec * DEFAULT_META["sample_rate"])
        self._hop_samp = int(self._hop_sec * DEFAULT_META["sample_rate"])
        self._last_decode_time = 0.0
        # Energy threshold — ADAPTIVE, tracks the noise floor.
        #
        # A fixed threshold doesn't work, because the noise floor changes
        # over time: with noise from LED lighting RMS reaches 0.020, at
        # night on a clean band it can be 10x lower. A threshold of 0.008
        # would then let pure noise through (the model returns '' and
        # wastes ~700ms per window), while hard-raising it would cut off
        # weak stations once the interference stops.
        #
        # So instead: we track the lowest RMS from recent windows (that's
        # the floor) and require the signal to be clearly above it.
        # Absolute lower bound — protects against decoding on a completely
        # dead input (e.g. disconnected card). Kept low, since on a clean
        # band weak stations have an RMS around 0.006.
        self._rms_threshold    = 0.003
        self._rms_floor        = None      # current noise floor (adaptive)
        self._rms_hist: list   = []        # history for determining the floor
        # Voting — history of the last N decodes
        self._history    : list[str] = []  # recent decodes (windows)
        self._history_max = 3              # vote over 3 windows (was 5)
        # Voting threshold: a character is accepted once it appears in
        # >=50% of windows. A shorter history = faster confirmation (a
        # character appears after 2 windows instead of 3), and the server
        # does the SAME AMOUNT of work — voting is just text comparison,
        # zero extra inference. Tradeoff: with a weak signal, slightly more
        # misreads get through, but on contests speed matters. The language
        # layer cleans up the output anyway.
        self._vote_ratio  = 0.5
        self._confirmed  : str = ""        # confirmed (winning) text
        self._sent_len   : int = 0         # how many characters already sent

    # ── Model download ───────────────────────────────────────────────────────
    def get_status(self) -> dict:
        has = MODEL_FILE.exists()
        size = MODEL_FILE.stat().st_size if has else 0
        return {
            "hasModel": has,
            "ready": self._ready,
            "sizeBytes": size,
            "sizeMB": round(size / 1e6, 1),
        }

    async def download(self, broadcast_fn=None) -> dict:
        MODEL_DIR.mkdir(exist_ok=True)

        async def _bcast(msg, pct, recv=0, total=0):
            if not broadcast_fn: return
            detail = f"{recv/1e6:.1f}/{total/1e6:.1f} MB" if total else f"{recv/1e6:.1f} MB"
            await broadcast_fn({"type": "deepcw_progress", "msg": msg,
                                "pct": pct, "detail": detail})

        try:
            await _bcast("Pobieranie metadanych...", 0)
            req = urllib.request.Request(META_URL, headers={"User-Agent": "curl/7.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                META_FILE.write_bytes(r.read())
            print("[deepcw] Metadata downloaded", flush=True)

            await _bcast("Pobieranie modelu (15 MB)...", 2)
            req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "curl/7.0"})

            # Download in an executor thread so as not to block the event loop
            loop = asyncio.get_event_loop()

            def _download_sync():
                with urllib.request.urlopen(req, timeout=120) as r:
                    total = int(r.headers.get("Content-Length", 0))
                    data  = bytearray()
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        data.extend(chunk)
                return bytes(data), total

            data, total = await loop.run_in_executor(None, _download_sync)
            MODEL_FILE.write_bytes(data)
            print(f"[deepcw] Model downloaded: {len(data)/1e6:.1f} MB", flush=True)
            await _bcast(f"✓ Model gotowy ({len(data)/1e6:.1f} MB)", 100, len(data), total)
            await self.load()
            return {"ok": True, "sizeBytes": len(data)}
        except Exception as e:
            print(f"[deepcw] Download error: {e}", flush=True)
            await _bcast(f"✗ {e}", -1)
            return {"ok": False, "error": str(e)}

    # ── Load the model into ONNX Runtime ─────────────────────────────────────────
    async def load(self):
        if not MODEL_FILE.exists():
            return False
        try:
            import onnxruntime as ort
            if META_FILE.exists():
                self.meta = json.loads(META_FILE.read_text())
            loop = asyncio.get_event_loop()
            # ONNX parallelizes inference across ALL cores by default. With
            # a 15MB model and a 10s window, the thread-coordination
            # overhead can exceed the gain, and each thread is extra
            # working memory. One thread = less RAM and less contention
            # with the audio path.
            _so = ort.SessionOptions()
            _so.intra_op_num_threads = 1
            _so.inter_op_num_threads = 1
            _so.enable_mem_pattern   = False   # lower memory overhead
            self.session = await loop.run_in_executor(
                None, lambda: ort.InferenceSession(str(MODEL_FILE),
                    sess_options=_so,
                    providers=["CPUExecutionProvider"])
            )
            # Pre-warm scipy in the SAME background thread. scipy is used later
            # in feed() for the anti-aliasing filter, and its first import
            # unpacks the library from the PyInstaller bundle — a one-off but
            # loop-blocking cost (looplag stack showed rthook_scipy/extract).
            # Doing it here, off the event loop, means the first real decode
            # does not stall the loop.
            try:
                await loop.run_in_executor(
                    None, lambda: __import__("scipy.signal"))
            except Exception:
                pass
            self._ready = True
            sr = self.meta["sample_rate"]
            self._win_samp = int(self._win_sec * sr)
            self._hop_samp = int(self._hop_sec * sr)
            print(f"[deepcw] Model loaded, SR={sr}", flush=True)
            return True
        except Exception as e:
            print(f"[deepcw] Load error: {e}", flush=True)
            return False

    # ── Accept PCM Float32 from the browser ───────────────────────────────────
    async def feed(self, pcm_f32: np.ndarray, src_rate: int, broadcast_fn=None) -> str | None:
        if not self._ready or self.session is None:
            return None

        tgt_rate = self.meta["sample_rate"]
        if src_rate != tgt_rate:
            # Anti-aliasing filter + resample. _lowpass uses numpy.convolve which
            # blocks the event loop (looplag stack: feed -> _lowpass -> convolve).
            # Run this DSP in a thread — it is pure CPU work with no awaits.
            def _resample(_pcm):
                _p = self._lowpass(_pcm, src_rate, tgt_rate * 0.45)
                _tl = int(round(len(_p) * tgt_rate / src_rate))
                if _tl > 0:
                    _xp = np.linspace(0, len(_p)-1, _tl)
                    _p = np.interp(_xp, np.arange(len(_p)), _p).astype(np.float32)
                return _p
            pcm_f32 = await asyncio.get_event_loop().run_in_executor(
                None, _resample, pcm_f32)

        self._buf = np.concatenate([self._buf, pcm_f32])
        # Diagnostics: save exactly what's being fed into the model (after
        # resampling and the filter). Listening to this file settles
        # whether the problem is in the audio path or in the model itself.
        self._capture_feed(pcm_f32)
        # MEMORY LEAK (fixed): the buffer used to be trimmed only AFTER the
        # time gate below, and that gate rejects most calls (return None).
        # The browser sends audio dozens of times per second, so between
        # decodes _buf grew without bound. We only keep as much as the
        # decode window needs — the rest is useless.
        if len(self._buf) > self._win_samp:
            self._buf = self._buf[-self._win_samp:]

        # DETECTING THE END OF A SHORT TRANSMISSION — for fast QSOs.
        #
        # For a short exchange ('RR 5NN TU', ~3s), waiting for the full
        # 10-second window and 2s step added several seconds of delay — the
        # text appeared once the operator was already transmitting a reply.
        # So: when a signal was present and has JUST stopped, we
        # immediately re-decode what came in, without waiting for the full
        # window or the step. This costs ONE extra inference at the end of
        # a transmission — it doesn't add load during longer operation,
        # since long transmissions don't have such gaps. Silence is
        # detected on a SHORT tail (0.3s), not over the whole step —
        # otherwise a gap shorter than the step would drown in the
        # averaging and the end of the transmission wouldn't be visible.
        _tail_n = int(0.3 * self.meta["sample_rate"])
        _rms_now = float(np.sqrt(np.mean(self._buf[-_tail_n:]**2))) \
            if len(self._buf) >= _tail_n else 0.0
        _was_active = getattr(self, "_sig_active", False)
        _is_active = _rms_now >= self._rms_threshold
        _just_ended = _was_active and not _is_active
        self._sig_active = _is_active
        if _is_active:
            self._close_hold = 0        # signal came back — reset the close counter

        if len(self._buf) < self._win_samp and not _just_ended:
            return None

        now = time.time()
        # Normal time gate — BUT the end of a short transmission bypasses
        # it, to react immediately.
        if not _just_ended and now - self._last_decode_time < self._hop_sec * 0.9:
            return None
        # Don't start another inference while the previous one is still
        # running. Without this, on a slower CPU tasks would pile up in the
        # executor, eating memory and delaying audio more and more.
        if getattr(self, "_infer_busy", False):
            return None
        self._infer_busy = True
        try:
            return await self._decode_window(now, broadcast_fn,
                                             short_end=_just_ended)
        finally:
            # The flag MUST be released on every exit path (including an
            # early return or exception), otherwise the decoder would go
            # silent forever.
            self._infer_busy = False

    async def _decode_window(self, now, broadcast_fn, short_end=False):
        self._last_decode_time = now
        self._dbg_n += 1        # window counter (kept at the top — also used by bandpass)

        # At the end of a SHORT transmission we take the fragment WITH
        # SIGNAL (the last few seconds before silence), not the current
        # silence — otherwise the model would get an empty tail and have
        # nothing to read.
        if short_end:
            # find the last fragment with energy (up to ~4s back)
            _look = min(len(self._buf), int(4.0 * self.meta["sample_rate"]))
            window = self._buf[-_look:] if _look > 0 else self._buf.copy()
        else:
            window = self._buf[-self._win_samp:] if len(self._buf) >= self._win_samp \
                else self._buf.copy()

        # NARROW-BAND ANALYSIS around the detected tone.
        #
        # The model used to get the whole 400-1200 Hz band with all the
        # noise in the middle. Isolating a single signal with a narrow
        # filter gives a better signal/noise ratio. For remote operation we
        # listen to ONE station (measured: one tone in the recording), so a
        # single filter is enough. A width of +-120 Hz doesn't clip the
        # keying sidebands even at 40 WPM (needs +-67 Hz), and raises the
        # envelope contrast. Measured: 6.2x -> ~7x.
        window = self._bandpass_tone(window)

        # NO PROCESSING — the model gets raw audio, same as the reference.
        #
        # The model's author (e04/deepcw-engine) feeds it plain
        # downsampling:
        #   ffmpeg -ac 1 -ar 3200 -sample_fmt s16
        # with no normalization, filter, or noise subtraction. The model
        # was TRAINED on raw, noisy CW and achieves 0% error down to -4 dB
        # SNR — it handles noise on its own. Every processing step we added
        # (normalization, spectral subtraction, filter) turned the signal
        # into something the model never saw during training, and
        # DRASTICALLY degraded results. Noise reduction exists in the
        # original product, but that's a SEPARATE model for listening by
        # ear, not for feeding the decoder.
        self._buf = self._buf[-self._win_samp:]

        rms = float(np.sqrt(np.mean(window**2)))

        # Update the noise floor — but ONLY from windows without a clear
        # signal.
        #
        # Previously every window went into the history, including ones
        # with CW. The floor would then rise together with the signal, the
        # gate would climb and start rejecting real transmissions
        # (observed: floor jumped to 0.033, gate to 0.038, while the CW was
        # at 0.033 — three windows in a row dropped mid-QSO). A classic
        # positive feedback loop: more signal means less decoding. So floor
        # samples are taken only from QUIET windows. Floor level = MINIMUM
        # of recent windows, with a slow upward drift.
        #
        # A percentile of the history failed when a transmission runs
        # without a gap: every sample then contained signal, the floor rose
        # together with it, the gate exceeded the CW level and the decoder
        # rejected real windows (observed: floor 0.033, gate 0.038, CW
        # 0.033 — gaps in the middle of a QSO). The minimum is robust: even
        # in a continuous transmission there are weaker moments, and gaps
        # between transmissions give a true floor.
        self._rms_hist.append(rms)
        if len(self._rms_hist) > 40:
            self._rms_hist.pop(0)

        # GATE — deliberately simple and absolute.
        #
        # Tried an adaptive gate here (floor from a percentile, then from
        # the RMS minimum) and both failed for the same reason: in CW the
        # silence between characters lasts milliseconds, while the window
        # averages 10 seconds. In a continuous transmission even the
        # minimum is only slightly below the average, so the "floor" would
        # land at the signal level, the gate would climb above it, and the
        # decoder would go silent until the end of the transmission.
        #
        # So the gate only filters out CLEARLY dead input (disconnected
        # card, muted radio). Whether there's content in the window is
        # decided by the model itself — when there's nothing to read, it
        # returns an empty string, which costs one inference and breaks
        # nothing.
        _gate = self._rms_threshold

        # CPU SAVING: skip inference when the window has NO CW tone.
        #
        # On an empty band (noise, no transmission), the decoder used to
        # run a full inference every step — wasting CPU for nothing. Cheap
        # test: is there a clear peak above the noise in the 400-1200 Hz
        # band. If there's no tone, there's nothing to decode — ONNX (the
        # heaviest operation) is skipped. This is NOT a gate on the CW
        # signal itself (the model handles that), just a filter for a
        # completely empty band. FFT is cheap compared to inference.
        if rms >= _gate and not short_end:
            try:
                _sr = self.meta["sample_rate"]
                _X = np.abs(np.fft.rfft(window * np.hanning(len(window))))
                _f = np.fft.rfftfreq(len(window), 1.0 / _sr)
                _b = (_f >= 400) & (_f <= 1200)
                if _b.any():
                    _peak = _X[_b].max()
                    _med  = np.median(_X[_b]) + 1e-9
                    # Threshold DELIBERATELY low (4x): we only skip a truly
                    # empty band (pure noise gives ~3-4x). A weak but real
                    # CW signal gives tens to hundreds of x, so it always
                    # passes. We'd rather compute unnecessarily once than
                    # miss a station — decode quality matters more than
                    # savings.
                    if _peak / _med < 4.0:
                        self._no_tone_run = getattr(self, "_no_tone_run", 0) + 1
                        # after a few empty windows in a row, skip
                        # inference, but still check every ~10th window
                        # (so as not to miss a weak start of a transmission)
                        if self._no_tone_run % 10 != 0:
                            return None
                    else:
                        self._no_tone_run = 0
            except Exception:
                pass

        # Diagnostics: every ~10s show the signal level and consensus
        # state, so it's visible WHETHER audio is arriving and whether the
        # model returns anything at all.
        # Diagnostics every ~10 windows: signal level and consensus state
        if VERBOSE and self._dbg_n % 10 == 1:
            print(f"[deepcw] RMS={rms:.5f} (gate {_gate:.5f}) "
                  f"windows={len(self._history)}", flush=True)
        if rms < _gate:
            # Count consecutive blocked windows — after a few in a row the
            # gate loosens itself (see above), so an over-raised floor
            # doesn't permanently silence the decoder.
            self._blocked_run = getattr(self, "_blocked_run", 0) + 1
            # Silence = end of the transmission. Clear the consensus so the
            # next station starts fresh (without appending to the previous
            # text).
            self._history.clear()
            self._confirmed = ""
            self._sent_len  = 0
            self._last_block = None
            # Reset the line state — the next station starts from scratch.
            self._last_consensus = ""
            self._idle_windows   = 0
            # After silence we do NOT close the line immediately — we hold
            # the last reading visible for a moment, so the operator has
            # time to read it. On fast QSOs the text used to disappear
            # before it could be noticed.
            _hold = getattr(self, "_close_hold", 0)
            if getattr(self, "_line_open", False) or getattr(self, "_had_preview", False):
                if _hold < 3:
                    # keep showing what was there for a few more windows
                    self._close_hold = _hold + 1
                    if broadcast_fn and getattr(self, "_preview_hold", ""):
                        await broadcast_fn({"type": "deepcw_text",
                                            "block": getattr(self, "_last_block", "") or "",
                                            "preview": self._preview_hold})
                    return None
                # only close it now
                if broadcast_fn:
                    await broadcast_fn({"type": "deepcw_text",
                                        "block": "", "preview": "", "close": True})
                self._had_preview = False
                self._line_open = False
                self._preview_hold = ""
                self._preview_ttl = 0
                self._close_hold = 0
            return None

        # Window passed the gate — reset the blocked counter.
        self._blocked_run = 0

        # Inference + spectrogram computation in a WORKER THREAD.
        #
        # Previously _infer was called directly in the coroutine — for the
        # duration of the computation (tens of ms every second) the event
        # loop stalled, so the server handled neither the audio stream nor
        # the WebSockets. Symptom: "audio glitches under load". ONNX
        # releases the GIL, so a separate thread computes in parallel and
        # the audio path isn't interrupted.
        _t0 = time.time()
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, self._infer, window)
        _ms = (time.time() - _t0) * 1000.0
        # Warn when inference doesn't fit inside the window step — then the
        # server computes without a break and audio starts to stutter.
        # Fix: shorten the window (_win_sec) or increase the step (_hop_sec).
        if _ms > self._hop_sec * 1000 * 0.7:
            self._slow_warn = getattr(self, "_slow_warn", 0) + 1
            if self._slow_warn <= 3 or self._slow_warn % 20 == 0:
                print(f"[deepcw] WARNING: inference {_ms:.0f} ms with step "
                      f"{self._hop_sec*1000:.0f} ms — CPU can't keep up, "
                      f"audio may stutter", flush=True)
        if VERBOSE and self._dbg_n % 10 == 1:
            print(f"[deepcw] model returned: {text!r} ({_ms:.0f} ms)", flush=True)
        if not text:
            return None

        self._history.append(text)
        if len(self._history) > self._history_max:
            self._history.pop(0)

        # At the end of a SHORT transmission, voting makes no sense — there
        # is one pass, nothing to vote over. We show the reading directly,
        # so a fast QSO ('RR 5NN TU') appears right away. For longer
        # operation, normal voting applies (filters out misreads across
        # multiple windows).
        if short_end:
            consensus = text
        else:
            consensus = self._consensus(self._history)
        if not consensus:
            return None

        # Send only what's NEW at the end of the consensus.
        #
        # NOTE — there used to be a bug here that caused the SAME fragment
        # to be sent MULTIPLE times (in the log: "+'ANETM RIGIC'" several
        # times in a row). Previously the consensus was compared to the
        # sent text from the START (common prefix). But the consensus comes
        # from a window sliding every second — its start keeps drifting
        # along with the audio, so after a dozen or so seconds it no longer
        # had anything in common with the start of the previous one. Prefix
        # comparison failed and the code resent the entire window content
        # from scratch on every decode.
        #
        # Now we look for the OVERLAP: how many characters from the end of
        # the already-sent text coincide with the start of the current
        # consensus. Whatever's past the overlap is new and is the only
        # thing sent to the UI.
        # ASSEMBLING TEXT — without appending increments.
        #
        # Previously only what "arrived" relative to the previous consensus
        # (the overlap) was sent. Problem: the consensus doesn't grow
        # predictably, it REBUILDS itself on every window ('CQ CQ CQDE' ->
        # 'CQ CQCQDE' -> 'CQ CQDE'). The match hit sometimes and missed
        # other times, so the text came out right sometimes and chopped
        # into 2-3-character fragments other times. This can't be tuned
        # with a parameter — it's a flaw in the approach itself.
        #
        # Now we send the ENTIRE current consensus as a block, and the
        # browser replaces the last line with it. There's nothing to lose
        # or duplicate, since there's no appending. When a transmission
        # ends (silence), the block is closed and the next station starts a
        # new line.
        # LINE — the current window, closed after a gap in TRANSMITTING.
        #
        # Tried a GROWING line here (appending whatever fell out of the
        # window). Bug: there's always something noisy in the band, so
        # silence never arrived, the line grew endlessly and glued several
        # transmissions into one unreadable strip — with repeats in the
        # middle, since prefix matching can also miss.
        #
        # Now: the line shows the current consensus, and closes when no new
        # content arrives for a few windows (end of transmission, even
        # though noise continues). We get a series of short lines — one per
        # transmission fragment — instead of one endlessly growing strip.
        # Closing the line is based on SIGNAL LEVEL, not on text
        # repetition. Previously the condition 'consensus == previous'
        # never held, since with noise the model returns a slightly
        # different string every time — the line grew endlessly and
        # everything ended up on one line. End of transmission means a drop
        # in energy: when RMS stays near the floor for a few windows, the
        # transmission has ended.
        _quiet_now = rms < (self._rms_threshold * 3.0)
        if _quiet_now:
            self._idle_windows = getattr(self, "_idle_windows", 0) + 1
        else:
            self._idle_windows = 0
        self._last_consensus = consensus

        # Signal level for the browser (audio bar graph). Rebuilt after
        # switching to card audio — the browser no longer has its own
        # stream, so the level is supplied by the server.
        if broadcast_fn:
            _vu = min(1.0, rms * 8.0)   # scale to ~0..1 for typical CW
            await broadcast_fn({"type": "deepcw_vu", "level": round(_vu, 3)})

        # ASSEMBLING THE LINE — grows as content arrives; closes when
        # nothing new comes in for a few windows.
        #
        # Closing is NOT based on signal level (there's always something
        # noisy in the band, RMS doesn't drop) or on consensus repetition
        # (the model returns a slightly different string every time).
        # Criterion: whether new text was added to the LINE. When the model
        # adds nothing for 3 windows — end of transmission.
        _acc = getattr(self, "_line_acc", "")
        _grew = False
        if not _acc:
            _acc = consensus
            _grew = bool(consensus)
        else:
            _tail = self._tail_after_overlap(_acc, consensus)
            if _tail and _tail.strip():
                _acc = (_acc + _tail)[-200:]     # hard cap on line length
                _grew = True
        self._line_acc = _acc

        if _grew:
            self._idle_windows = 0
        else:
            self._idle_windows = getattr(self, "_idle_windows", 0) + 1

        # Three windows with no new content = end of transmission -> close the line
        if self._idle_windows >= 3 and getattr(self, "_line_open", False):
            if broadcast_fn:
                await broadcast_fn({"type": "deepcw_text",
                                    "block": "", "preview": "", "close": True})
            self._line_open = False
            self._last_block = None
            self._line_acc = ""
            self._history.clear()
            self._idle_windows = 0
            return consensus

        # Splitting a long, continuous transmission into readable lines.
        # Without this the line grows endlessly, and every window sends the
        # whole strip to the browser (server and client load). After ~72
        # characters we close the current line at a word boundary and start
        # a new one — the transmission keeps going.
        if len(_acc) >= 64 and getattr(self, "_line_open", False):
            _cut = _acc.rfind(" ", 32, 68)    # cut at a space in a reasonable range
            if _cut < 0:
                _cut = 64
            _done = self._correct_calls(_acc[:_cut].strip())
            try:
                import deepcw_lang
                _done = deepcw_lang.correct(_done, self._known_calls)
            except Exception:
                pass
            if broadcast_fn:
                # close the line with the finished content, then signal a new one
                await broadcast_fn({"type": "deepcw_text",
                                    "block": _done, "preview": "", "close": False})
                await broadcast_fn({"type": "deepcw_text",
                                    "block": "", "preview": "", "close": True})
            self._line_acc = _acc[_cut:].lstrip()
            self._last_block = None
            _acc = self._line_acc

        block = self._correct_calls(_acc) if _acc else ""
        # Language layer — corrects CW phrases, reports, and names based on
        # the structure of a contact (like Skimmer). Works on text, zero
        # audio cost.
        if block:
            try:
                import deepcw_lang
                block = deepcw_lang.correct(block, self._known_calls)
            except Exception:
                pass
        # Filter out isolated junk characters (e.g. a lone 'X' from interference)
        if block and len(block.strip()) <= 1:
            block = self._filter_noise(block)

        # PREVIEW: the freshest model reading beyond the consensus — what
        # voting hasn't confirmed yet. The operator sees it right away
        # (grayed out), instead of waiting 3-4s for confirmation.
        preview = ""
        if text and consensus:
            # How much of the end of the consensus overlaps with the latest reading
            for n in range(min(len(text), len(consensus)), 0, -1):
                if consensus.endswith(text[:n]):
                    preview = text[n:]
                    break
            else:
                preview = ""
        elif text:
            preview = text
        # Preview length sized for the fastest correspondents (40 WPM ~ 8
        # characters per 2.5s; with margin for late windows).
        preview = preview[:24]

        if block != getattr(self, "_last_block", None):
            print(f"[deepcw] ={block!r}", flush=True)
            self._last_block = block
            self._line_open = True

        # HOLDING THE PREVIEW: when the current reading no longer has a new
        # tail (preview empty), we do NOT clear the gray bar right away —
        # we leave the last visible content for a few windows, so the
        # operator has time to read it. On fast QSOs the text used to
        # appear and disappear before it could be noticed.
        if preview:
            self._preview_hold = preview
            self._preview_ttl = 4          # hold for ~4 windows
        elif getattr(self, "_preview_ttl", 0) > 0:
            self._preview_ttl -= 1
            preview = getattr(self, "_preview_hold", "")   # show the last one
        else:
            self._preview_hold = ""

        _prev_had = getattr(self, "_had_preview", False)
        if broadcast_fn and (block or preview or _prev_had):
            await broadcast_fn({"type": "deepcw_text",
                                # 'block' = the ENTIRE current line to REPLACE
                                # (not append). The browser overwrites the
                                # last line with it instead of gluing fragments.
                                "block": block,
                                "preview": preview})
        self._had_preview = bool(preview)
        return consensus

    # Cut numbers — digit shortcuts used in CW to save time.
    # Instead of the full digit, the sender keys a shorter letter with a
    # similar pattern:
    #   0 -> T (one dash instead of five)
    #   9 -> N,  5 -> E,  1 -> A,  7 -> G,  8 -> D,  2 -> U,  6 -> B
    # These are NOT decoder errors — the model read exactly what went out
    # over the air. So they are NEVER "corrected" in the displayed text;
    # they're only expanded for the duration of COMPARING against the known
    # calls database.
    _CUT_NUMBERS = {"T": "0", "N": "9", "E": "5", "A": "1",
                    "G": "7", "D": "8", "U": "2", "B": "6"}

    @classmethod
    def _expand_cut_numbers(cls, token: str, known: str) -> str:
        """Expand cut numbers in 'token' — but ONLY at positions where the
        known call has a digit.

        Expanding blindly would give false matches, since T/N/E are also
        ordinary letters in real calls (e.g. 'T' in G8KHF-like calls). So we
        look positionally: if the database has a digit at this spot, and we
        have a letter that's its shortcut — we treat it as that same digit.
        """
        if len(token) != len(known):
            return token
        out = []
        for ch, kh in zip(token.upper(), known.upper()):
            if kh.isdigit() and cls._CUT_NUMBERS.get(ch) == kh:
                out.append(kh)
            else:
                out.append(ch)
        return "".join(out)

    @staticmethod
    def _looks_like_report(tok: str) -> bool:
        """Is this token an RST report / contest number?

        There, cut numbers are the NORM (599 -> 5NN, 001 -> TT1), and any
        "correction" would misrepresent what the operator actually sent.
        Such tokens are left untouched entirely.
        """
        t = tok.upper()
        if not t:
            return False
        # Pure digits / pure cut-number shortcuts / a mix of both
        digits_or_cuts = set("0123456789") | set(
            DeepCWEngine._CUT_NUMBERS.keys())
        return len(t) <= 5 and all(c in digits_or_cuts for c in t)

    def _correct_calls(self, text: str) -> str:
        """Correct garbled callsigns against the pool of known calls.

        The neural network confuses similar Morse characters (A/N, U/D,
        S/H, E/I), so the real call comes out with one letter off. If the
        pool has EXACTLY ONE call at edit distance 1, we substitute it —
        with several candidates it's better to leave the original than guess.

        IMPORTANT: cut numbers (IC73TT, 5NN) are NOT errors — they're a
        deliberate shortcut by the sender. They are only expanded in order
        to MATCH the token against the database; the result keeps the form
        that actually went out over the air.
        """
        if not text or not self._known_calls:
            return text
        out = []
        for tok in text.split(" "):
            t = tok.upper()
            # Reports and contest numbers are left untouched
            if self._looks_like_report(t):
                out.append(tok)
                continue
            # Call candidate: 4-10 characters, has a letter, and has a
            # digit OR a cut number (a call may be sent entirely in
            # shorthand: IC73TT)
            has_alpha = any(c.isalpha() for c in t)
            has_digit = any(c.isdigit() for c in t)
            has_cut   = any(c in self._CUT_NUMBERS for c in t)
            if (4 <= len(t) <= 10 and has_alpha and (has_digit or has_cut)
                    and t not in self._known_calls):
                fixed = self._match_with_cuts(t)
                if fixed:
                    out.append(fixed)
                    continue
            out.append(tok)
        return " ".join(out)

    def _match_with_cuts(self, token: str) -> str | None:
        """Match a token against the database, accounting for cut numbers.

        Returns the ORIGINAL token when, after expanding shortcuts, it
        turns out to already be correct (nothing to fix), or a known call
        when the token is actually garbled. None when there's no
        unambiguous match.
        """
        # 1) After expanding cut numbers, is the token already correct?
        for known in self._known_calls:
            if len(known) == len(token):
                if self._expand_cut_numbers(token, known) == known:
                    return token   # sent as a shorthand — leave it as sent
        # 2) Regular one-character correction (a real garble)
        return self.match_known_call(token)

    @staticmethod
    def _tail_after_overlap(sent: str, cons: str) -> str:
        """Return the part of 'cons' that doesn't overlap with the end of 'sent'.

        Used for GROWING the line: 'sent' is the text since the start of
        the transmission, 'cons' is the current window. Only what newly
        arrived at the end of the window is appended. The match is
        tolerant, since the consensus rebuilds itself slightly (spaces,
        single characters) — a rigid comparison would lose continuity.
        """
        if not sent:
            return cons
        if not cons:
            return ""
        import difflib
        tail = sent[-len(cons):] if len(sent) > len(cons) else sent
        sm = difflib.SequenceMatcher(None, tail, cons, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b.size >= 3]
        if blocks:
            last = max(blocks, key=lambda b: b.b + b.size)
            if last.a + last.size >= len(tail) - 3:
                return cons[last.b + last.size:]
        for n in range(min(len(sent), len(cons)), 0, -1):
            if sent.endswith(cons[:n]):
                return cons[n:]
        if cons in sent:
            return ""
        return " " + cons

    def _consensus(self, history: list[str]) -> str:
        """Majority voting over ALIGNED decode windows.

        Each window is aligned to a reference window (median length) via
        difflib, then at every position of the reference the most frequent
        character wins — provided it gathered at least _vote_ratio of the
        votes. Alignment lets us tolerate lost/inserted characters (a rigid
        index-based comparison would fall apart after the first dropped
        letter).
        """
        h = [x for x in history if x]
        # Minimum threshold lowered from 3 to 2 windows — the consensus
        # kicks in sooner, a character appears one window (2s) earlier.
        # With 2 windows they still need to agree (need=2), so it's still a
        # confirmation, not a single reading. Matters in contests, where
        # speed counts.
        if len(h) < 2:
            return ""
        import difflib
        from collections import Counter
        # Reference = the window MOST SIMILAR to the others, not arbitrary
        # (previously: median length). When the reference landed on a
        # window with an error, difflib wouldn't align positions there and
        # the correct character from the other windows was lost — even
        # though it had the majority of votes.
        if len(h) > 2:
            from collections import Counter as _C
            # First: does any reading REPEAT? A repeat is the strongest
            # evidence of correctness — the model read the same thing
            # twice. Without this the reference would sometimes land on a
            # TRUNCATED window (a shorter one can be "more similar" to all
            # of them), causing trailing characters to be lost despite
            # having a majority (observed: 'I1GS' instead of 'I1GIS' with 3
            # of 4 windows correct).
            _cnt = _C(h)
            _top, _n = _cnt.most_common(1)[0]
            if _n >= 2:
                ref = _top
            else:
                def _sim(x):
                    return sum(difflib.SequenceMatcher(None, x, y).ratio()
                               for y in h if y is not x)
                ref = max(h, key=_sim)
        else:
            ref = h[-1]
        votes: list[list[str]] = [[] for _ in range(len(ref))]
        for cand in h:
            sm = difflib.SequenceMatcher(None, ref, cand, autojunk=False)
            # opcodes give FULL coverage: not just matching fragments
            # ('equal'), but also substitutions ('replace'). With only
            # matching_blocks, positions with an error in the reference
            # collected no votes at all — the correct character from the
            # other windows was lost despite having the majority (observed:
            # 'DG8G' -> 'DGG').
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == "equal":
                    for k in range(i2 - i1):
                        votes[i1 + k].append(cand[j1 + k])
                elif tag == "replace":
                    # Substitution: vote positionally for as many
                    # characters as overlap
                    for k in range(min(i2 - i1, j2 - j1)):
                        votes[i1 + k].append(cand[j1 + k])
        need = max(2, int(len(h) * self._vote_ratio))
        out = []
        for v in votes:
            if not v:
                continue
            ch, cnt = Counter(v).most_common(1)[0]
            if cnt >= need:
                out.append(ch)
        result = "".join(out).strip()

        # Recover the TAIL missing from the reference. When the reference
        # lands on a truncated window (e.g. 'JUR' while most say 'JURE'),
        # the positions for the trailing characters simply don't exist and
        # voting can't produce them. So we check whether most windows share
        # a common extension.
        if result:
            ext = []
            for cand in h:
                i = cand.find(result)
                if i >= 0 and len(cand) > i + len(result):
                    ext.append(cand[i + len(result):])
            if ext:
                tail = ""
                for pos in range(max(len(x) for x in ext)):
                    chars = [x[pos] for x in ext if pos < len(x)]
                    ch, cnt = Counter(chars).most_common(1)[0]
                    if cnt >= need:
                        tail += ch
                    else:
                        break
                result += tail
        return result

    def _filter_noise(self, text: str) -> str:
        """Strip obvious noise — isolated random characters."""
        if not text:
            return text
        stripped = text.strip()
        if len(stripped) <= 1 and stripped not in 'EIT ':
            return ''
        return text

    # ── Known-call database (validation/correction of decodes) ───────────────────────
    # Fed from ALL sources: FT8 decodes, the QSO log, DX cluster spots from
    # all users. The decoded string is compared against real calls and
    # typical garbles are corrected (the CW neural network confuses similar
    # characters, e.g. A/N, U/D, S/H).
    _known_calls: set = set()

    @classmethod
    def add_known_calls(cls, calls):
        """Add calls to the validation pool (uppercase, no junk)."""
        for c in calls or ():
            c = (c or "").strip().upper()
            # Callsign: 3-10 characters, contains a digit and a letter
            if (3 <= len(c) <= 10 and any(ch.isdigit() for ch in c)
                    and any(ch.isalpha() for ch in c)):
                cls._known_calls.add(c)
        # Memory limit — the pool lives in RAM, can't grow without bound
        if len(cls._known_calls) > 20000:
            cls._known_calls = set(list(cls._known_calls)[-10000:])

    @classmethod
    def match_known_call(cls, token: str) -> str | None:
        """Return a known call matching the token (exactly or 1 character off).

        A match is returned only when it's UNAMBIGUOUS — with several
        candidates at distance 1, it's better not to guess.
        """
        t = (token or "").strip().upper()
        if len(t) < 3:
            return None
        if t in cls._known_calls:
            return t
        # One-character correction (substitution/deletion/insertion)
        cands = []
        for known in cls._known_calls:
            if abs(len(known) - len(t)) > 1:
                continue
            if cls._dist1(t, known):
                cands.append(known)
                if len(cands) > 1:
                    return None  # ambiguous — don't correct
        return cands[0] if cands else None

    @staticmethod
    def _dist1(a: str, b: str) -> bool:
        """Is the edit distance between a and b exactly 1?"""
        if a == b:
            return False
        la, lb = len(a), len(b)
        if la == lb:
            return sum(1 for x, y in zip(a, b) if x != y) == 1
        if la > lb:
            a, b, la, lb = b, a, lb, la
        # lb == la + 1 : check whether a is b with one character removed
        i = j = diff = 0
        while i < la and j < lb:
            if a[i] != b[j]:
                diff += 1
                if diff > 1:
                    return False
                j += 1
                continue
            i += 1
            j += 1
        return True

    # ── ONNX inference ────────────────────────────────────────────────────────
    def _denoise(self, x: np.ndarray) -> np.ndarray:
        """Spectral noise subtraction — raises the CW envelope contrast.

        We compute the STFT, estimate the noise level in each bin (20th
        percentile of magnitude — the floor persists over most of the
        window, characters only briefly), subtract 1.5x that level with a
        5% floor, and reconstruct the signal. Phase stays original.
        Measured: contrast 14.6x -> 22x.
        """
        if x.size < 512:
            return x
        nfft, hop = 256, 64      # hop = nfft/4: a Hanning window sums to 1.5
        win = np.hanning(nfft).astype(np.float32)
        n_frames = (len(x) - nfft) // hop + 1
        if n_frames < 4:
            return x
        F = np.array([np.fft.rfft(x[i*hop:i*hop+nfft] * win)
                      for i in range(n_frames)])
        mag = np.abs(F)
        ph = np.angle(F)
        noise = np.percentile(mag, 20, axis=0)
        mag_clean = np.maximum(mag - 1.5 * noise, 0.05 * mag)
        Fc = mag_clean * np.exp(1j * ph)
        # Overlap-add reconstruction. Hop = nfft/4 with a Hanning window
        # gives a constant window sum (COLA), so we normalize by the known
        # value rather than the local sum — the local sum could drop to
        # zero at the edges and blow the amplitude up to the hundreds
        # (observed: peak 202, RMS 2.0 in the log).
        out = np.zeros(n_frames * hop + nfft, dtype=np.float32)
        for i, fr in enumerate(Fc):
            seg = np.fft.irfft(fr).astype(np.float32)
            out[i*hop:i*hop+nfft] += seg * win
        # The Hanning sum at hop=nfft/4 is 1.5 over the fully-covered region
        out /= 1.5
        return out[:len(x)].astype(np.float32)

    def reset(self):
        """Clear the decoder's state (when toggling the window on/off)."""
        self._buf = np.zeros(0, dtype=np.float32)
        self._history.clear()
        self._confirmed = ""
        self._last_block = None
        self._last_consensus = ""
        self._idle_windows = 0
        self._line_open = False
        self._had_preview = False

    def start_capture(self, seconds: float = 15.0) -> str:
        """Enable audio recording EXACTLY as it's fed to the model.

        Diagnostics: lets you listen to what the decoder actually gets —
        after resampling to 3200Hz and after the anti-aliasing filter,
        right before inference. If the file has a clean CW tone, the
        problem is in the model itself; if it's garbled — it's in the
        audio path before it.
        """
        self._cap_buf = []
        self._cap_left = int(seconds * self.meta["sample_rate"])
        self._cap_path = str(MODEL_DIR / "deepcw_capture.wav")
        print(f"[deepcw] Audio recording enabled ({seconds:.0f}s) -> "
              f"{self._cap_path}", flush=True)
        return self._cap_path

    def _capture_feed(self, pcm: np.ndarray):
        """Append samples to the recording; once complete — write the WAV file."""
        if not getattr(self, "_cap_left", 0):
            return
        self._cap_buf.append(pcm.copy())
        self._cap_left -= len(pcm)
        if self._cap_left > 0:
            return
        try:
            import wave
            data = np.concatenate(self._cap_buf)
            # float32 -1..1 -> int16
            pcm16 = np.clip(data, -1.0, 1.0)
            pcm16 = (pcm16 * 32767).astype(np.int16)
            with wave.open(self._cap_path, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.meta["sample_rate"])
                w.writeframes(pcm16.tobytes())
            _peak = float(np.max(np.abs(data))) if data.size else 0.0
            _rms  = float(np.sqrt(np.mean(data**2))) if data.size else 0.0
            print(f"[deepcw] RECORDING DONE: {self._cap_path} "
                  f"({len(data)/self.meta['sample_rate']:.1f}s, "
                  f"peak={_peak:.3f}, rms={_rms:.4f})", flush=True)
        except Exception as e:
            print(f"[deepcw] Audio recording error: {e}", flush=True)
        finally:
            self._cap_buf = []
            self._cap_left = 0

    def _bandpass_tone(self, x: np.ndarray) -> np.ndarray:
        """Isolate the detected CW tone with a narrow bandpass filter (like Skimmer).

        Detects the dominant tone in the 300-1200 Hz band, tracks it with
        smoothing (so it doesn't jump on transient noise), and passes
        +-120 Hz around it. When the window has no clear tone, the signal
        is returned unchanged — we don't guess.
        """
        if x.size < 512:
            return x
        # Can be disabled for a live comparison (HAM_CW_BANDPASS=0)
        if not getattr(self, "_bandpass_on", True):
            return x
        sr = self.meta["sample_rate"]
        X = np.fft.rfft(x)
        f = np.fft.rfftfreq(len(x), 1.0 / sr)
        band = (f >= 300) & (f <= 1200)
        if not band.any():
            return x
        mag = np.abs(X)
        idx = np.where(band)[0]
        peak_i = idx[np.argmax(mag[idx])]
        peak_f = f[peak_i]
        med = np.median(mag[idx])
        if mag[peak_i] < med * 4:
            return x                      # no clear tone — don't filter

        # Filter ONLY a noisy signal. On clean, strong CW, a bandpass
        # filter hurts — cutting the band introduces ringing that blurs the
        # sharp keying edges (measured: the envelope contrast of a clean
        # signal dropped from 263x to 98x). The noise metric is ENVELOPE
        # CONTRAST: the ratio of mark level to silence level. High contrast
        # = clean signal, leave it; low = noise in the gaps, the filter helps.
        _wl = int(sr * 0.01)
        if _wl > 0 and len(x) > _wl * 4:
            _env = np.sqrt(np.convolve(x**2, np.ones(_wl)/_wl, mode="valid"))
            _contrast = np.percentile(_env, 90) / max(np.percentile(_env, 10), 1e-9)
            if _contrast > 15:
                return x                  # signal already clean — don't spoil it
        prev = getattr(self, "_tone_hz", None)
        if prev is None or abs(peak_f - prev) > 200:
            tone = peak_f                 # new station / first time
        else:
            tone = prev * 0.7 + peak_f * 0.3
        self._tone_hz = tone
        if self._dbg_n % 10 == 1:
            print(f"[deepcw] tone {tone:.0f}Hz (filter +-120Hz)", flush=True)
        bw = 120.0
        mask = (f >= tone - bw) & (f <= tone + bw)
        Xf = np.where(mask, X, 0)
        return np.fft.irfft(Xf, n=len(x)).astype(np.float32)

    def _lowpass(self, x: np.ndarray, sr: int, cutoff: float) -> np.ndarray:
        """Low-pass filter (FIR, Hamming window) before decimation.

        Keeps STATE between successive audio chunks — the browser sends
        sound in pieces, so filtering each one separately would leave
        discontinuities at the seams (clicks that the model sees as
        characters).

        Coefficients are computed once and cached; the filtering cost is a
        fraction of a millisecond, negligible compared to inference itself.
        """
        if x.size == 0:
            return x
        key = (sr, round(cutoff))
        if getattr(self, "_lp_key", None) != key:
            # 63 taps = a compromise between rolloff steepness and cost
            n_taps = 63
            fc = cutoff / sr                      # normalized frequency
            m = np.arange(n_taps) - (n_taps - 1) / 2
            h = np.sinc(2 * fc * m) * np.hamming(n_taps)
            self._lp_taps = (h / h.sum()).astype(np.float32)
            self._lp_key = key
            self._lp_tail = np.zeros(n_taps - 1, dtype=np.float32)

        # Prepend the tail from the previous call, filter, save the new tail
        xin = np.concatenate([self._lp_tail, x])
        self._lp_tail = xin[-(len(self._lp_taps) - 1):].copy()
        return np.convolve(xin, self._lp_taps, mode="valid").astype(np.float32)

    def _infer(self, audio: np.ndarray) -> str:
        # Lower the priority of THIS thread (inference), so the audio path
        # and networking always get precedence. Without this, a heavy ONNX
        # inference could momentarily occupy every core and choke audio
        # (Python at 100% in /perf). Via ctypes on Windows; via os.nice
        # elsewhere — best-effort, errors are ignored (not critical).
        if not getattr(self, "_prio_lowered", False):
            try:
                import sys as _sys
                if _sys.platform == "win32":
                    import ctypes
                    # THREAD_PRIORITY_BELOW_NORMAL = -1
                    ctypes.windll.kernel32.SetThreadPriority(
                        ctypes.windll.kernel32.GetCurrentThread(), -1)
                else:
                    import os as _os
                    _os.nice(5)
                self._prio_lowered = True
            except Exception:
                self._prio_lowered = True   # don't keep retrying
        m    = self.meta
        sr   = m["sample_rate"]
        nfft = m["fft_length"]
        hop  = m["hop_length"]
        fmin = m["spectrogram_min_freq_hz"]
        fmax = m["spectrogram_max_freq_hz"]
        bins = m["spectrogram_frequency_bins"]

        spec = self._stft_spectrogram(audio, nfft, hop, sr, fmin, fmax, bins)
        spec = np.log1p(spec).astype(np.float32)
        inp  = spec[np.newaxis, np.newaxis, :, :]

        out  = self.session.run([m["onnx_output_name"]], {m["onnx_input_name"]: inp})[0]
        return self._ctc_decode(out[0], m["chars"], m["blank_index"])

    def _stft_spectrogram(self, audio, nfft, hop, sr, fmin, fmax, n_bins):
        # VECTORIZATION — critical for CPU load.
        #
        # The previous version computed FFT in a Python LOOP, frame by
        # frame (~660 iterations for a 10s window at hop=48). An FFT loop
        # in pure Python is one of the slowest operations — it's what was
        # eating the CPU (DeepCW spiked to 100%, choking audio). Now all
        # frames are assembled into one matrix (sliding_window_view) and
        # FFT is computed ONCE, vectorized. Result is identical, but tens
        # of times faster.
        if len(audio) < nfft:
            return np.zeros((1, n_bins), dtype=np.float32)
        window = np.hanning(nfft).astype(np.float32)
        # Sliding-window view: [n_frames, nfft] without copying data
        try:
            from numpy.lib.stride_tricks import sliding_window_view
            frames = sliding_window_view(audio, nfft)[::hop]
        except Exception:
            # Fallback for very old numpy — manual via as_strided
            n_frames = (len(audio) - nfft) // hop + 1
            idx = np.arange(nfft)[None, :] + hop * np.arange(n_frames)[:, None]
            frames = audio[idx]
        # All frames * window, then FFT of all of them at once (one operation)
        frames = frames * window                          # [T, nfft]
        spec = np.abs(np.fft.rfft(frames, n=nfft, axis=1)) ** 2   # [T, nfft//2+1]
        # Select the fmin..fmax band
        freqs = np.fft.rfftfreq(nfft, d=1.0/sr)
        i0 = np.searchsorted(freqs, fmin)
        i1 = np.searchsorted(freqs, fmax)
        spec = spec[:, i0:i1]
        # Interpolate to n_bins — vectorized, no per-row loop.
        # np.interp is 1D, but the column scale is the same for every
        # frame, so the interpolation weights are computed ONCE and applied
        # to the whole matrix.
        if spec.shape[1] != n_bins:
            ncol = spec.shape[1]
            xp   = np.linspace(0, ncol - 1, n_bins)
            lo   = np.floor(xp).astype(np.int64)
            hi   = np.minimum(lo + 1, ncol - 1)
            frac = (xp - lo).astype(np.float32)
            # linear interpolation of all frames at once: [T, n_bins]
            spec = spec[:, lo] * (1.0 - frac) + spec[:, hi] * frac
        return spec.astype(np.float32)

    def _ctc_decode(self, log_probs, chars, blank_idx):
        # Greedy decode
        indices = np.argmax(log_probs, axis=-1)
        prev, out = -1, []
        for idx in indices:
            if idx != blank_idx and idx != prev:
                if 0 <= idx < len(chars):
                    out.append(chars[idx])
            prev = idx
        return "".join(out).strip()


# Singleton
deepcw_engine = DeepCWEngine()
