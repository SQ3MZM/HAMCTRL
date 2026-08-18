#!/usr/bin/env python3
"""
audio_stream.py — Opus audio streaming over WebSocket
RX: sound card -> PyAudio -> opuslib -> binary WS -> browser
TX: browser -> binary WS (WebM) -> ffmpeg -> PyAudio -> sound card
"""
import asyncio, threading, time, struct, queue
import numpy as np

try:
    import pyaudio
    _PA = True
except ImportError:
    _PA = False
    print("[audio] pyaudio unavailable")

try:
    import opuslib, opuslib.api
    _OPUS = True
    print("[audio] opuslib OK")
except Exception as e:
    _OPUS = False
    print(f"[audio] opuslib unavailable: {e}")

OPUS_RATE   = 48000
OPUS_CH     = 1
OPUS_FRAMES = 960
TX_TAG      = 0xA2


def _make_decoder():
    if not _OPUS: return None
    try:
        return opuslib.Decoder(OPUS_RATE, OPUS_CH)
    except Exception as e:
        print(f"[audio] Decoder error: {e}"); return None


def _make_decoder_stereo():
    """Stereo decoder for TX — MediaRecorder encodes 2 channels even from a mono mic."""
    if not _OPUS: return None
    try:
        return opuslib.Decoder(OPUS_RATE, 2)
    except Exception as e:
        print(f"[audio] Decoder stereo error: {e}"); return None


def _webm_to_pcm(webm_data: bytes, volume: float = 1.0) -> bytes:
    """Decode WebM/Opus to PCM via ffmpeg."""
    import subprocess as _sp
    af = f"volume={volume}" if volume != 1.0 else "anull"
    for exe in ['ffmpeg', r'ffmpeg.exe']:
        try:
            r = _sp.run(
                [exe, '-loglevel', 'quiet',
                 '-f', 'webm', '-i', 'pipe:0',
                 '-af', af,
                 '-f', 's16le', '-ar', str(OPUS_RATE), '-ac', '1', 'pipe:1'],
                input=webm_data, capture_output=True, timeout=30
            )
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception:
            continue
    return b""


class AudioStream:
    def __init__(self):
        self.loop = None
        self.cfg  = {}
        self.rx_active  = False; self.rx_device  = None
        self._rx_stream = None;  self._rx_thread = None
        self._rx_rate   = OPUS_RATE; self._rx_ch = OPUS_CH
        self._rx_frames = OPUS_FRAMES
        self.tx_active  = False
        self._tx_stream = None; self._tx_thread = None
        # Queue of PCM for the TX playback thread.
        # FT8 = 12.64s = 632 chunks of 20ms. FT4 = 4.48s = 224 chunks.
        # SSB/CW over WebRTC = continuous stream (small chunks).
        # maxsize=800 = 16s of buffer, fits FT8 with a safe margin.
        # For a 12s FT8 transmission we push EVERYTHING at once (not an async
        # trickle), so the queue MUST be large enough — otherwise the
        # anti-lag drop-old logic starts discarding audio data and the radio
        # keys PTT but nothing actually goes out on the air.
        self._webrtc_pcm_queue = queue.Queue(maxsize=800)
        # Bulk TX mode (FT8/FT4): the whole signal is pushed into the queue
        # AT ONCE (200+ frames). The anti-lag drop must NOT run then — it
        # would treat the intentional buffering as "backlog" (dropping
        # 239/248 frames = 160ms of buzz instead of a 4.94s signal =
        # zero power/ALC, nothing goes out on the air). Set by webapp for
        # the duration of an FT8/FT4 transmission.
        self.bulk_tx = False
        self._webrtc_thread = None
        self._tx_dec    = None
        self._webm_buf  = b""
        self._pa = None
        self.rx_frames = 0; self.tx_frames = 0; self.tx_frames_received = 0
        # Raw PCM buffer for the CW decoder (DeepCW), independent of the
        # waterfall buffers — each is drained on its own cycle by its own consumer.
        self._cw_rx_buf = bytearray()
        self._cw_rx_buf_lock = threading.Lock()
        self.cw_rx_enabled = False      # enabled when the operator opens the decoder
        # Separate, SMALL buffer for the waterfall (spectrum preview) —
        # polled often (every ~0.5-1s), independent of the 15s FT8 decode cycle.
        self._waterfall_buf = bytearray()
        self._waterfall_buf_lock = threading.Lock()

    def set_loop(self, loop): self.loop = loop

    def _pa_inst(self):
        if not _PA: raise RuntimeError("pyaudio unavailable")
        if self._pa is None: self._pa = pyaudio.PyAudio()
        return self._pa

    def _dev_info(self, name, is_input):
        pa = self._pa_inst()
        if name:
            name_l = name.lower()
            for i in range(pa.get_device_count()):
                try:
                    info = pa.get_device_info_by_index(i)
                    key  = "maxInputChannels" if is_input else "maxOutputChannels"
                    if info.get(key, 0) > 0 and name_l in info.get("name","").lower():
                        return i, info
                except: continue
        try:
            idx  = pa.get_default_input_device_info()["index"] if is_input else pa.get_default_output_device_info()["index"]
            info = pa.get_device_info_by_index(idx)
            return idx, info
        except:
            return None, {}

    def start_rx(self, device=None, bitrate=24000):
        if not _PA: return False
        if self.rx_active: self.stop_rx()
        try:
            pa = self._pa_inst(); idx, info = self._dev_info(device, True)
            self.rx_device = device
            native_rate = int(info.get("defaultSampleRate", OPUS_RATE))
            native_ch   = min(int(info.get("maxInputChannels", 1)), 2)
            try:
                pa.is_format_supported(OPUS_RATE, input_device=idx, input_channels=1, input_format=pyaudio.paInt16)
                use_rate = OPUS_RATE; use_ch = 1
            except:
                use_rate = native_rate; use_ch = native_ch
            self._rx_rate = use_rate; self._rx_ch = use_ch
            self._rx_frames = int(OPUS_FRAMES * use_rate / OPUS_RATE) if use_rate != OPUS_RATE else OPUS_FRAMES
            kw = dict(format=pyaudio.paInt16, channels=use_ch, rate=use_rate,
                      input=True, frames_per_buffer=self._rx_frames)
            if idx is not None: kw["input_device_index"] = idx
            self._rx_stream = pa.open(**kw)
            self.rx_active = True; self.rx_frames = 0
            self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True, name="audio-rx")
            self._rx_thread.start()
            print(f"[audio] RX START | '{device or 'default'}' | {use_rate}Hz {use_ch}ch")
            return True
        except Exception as e:
            print(f"[audio] RX error: {e}"); self.rx_active = False; return False

    def stop_rx(self):
        self.rx_active = False
        if self._rx_stream:
            try: self._rx_stream.stop_stream(); self._rx_stream.close()
            except: pass
            self._rx_stream = None
        print("[audio] RX STOP")

    def _rx_loop(self):
        """Our own PyAudio read loop, running IN PARALLEL to the independent
        Rust/cpal capture — the actual monitoring path goes directly
        browser<->Rust WS (see CLAUDE.md), this loop feeds ONLY the CW
        decoder (DeepCW) and the waterfall preview."""
        log_n = int(OPUS_RATE / OPUS_FRAMES * 10)
        while self.rx_active:
            try:
                raw = self._rx_stream.read(self._rx_frames, exception_on_overflow=False)
                mono_native = raw
                if self._rx_ch == 2:
                    n = len(raw) // 2
                    samples = struct.unpack(f"<{n}h", raw)
                    mono_samples = [(samples[i] + samples[i+1]) // 2 for i in range(0, len(samples)-1, 2)]
                    mono_native = struct.pack(f"<{len(mono_samples)}h", *mono_samples)

                # Buffer for the CW decoder — RAW PCM straight from the card.
                #
                # Previously DeepCW received audio from the browser, i.e.
                # AFTER Opus compression. The lossy codec blurs the keying
                # edges and raises the noise floor in the gaps: measured
                # envelope contrast dropped to 6.4x, while the model needs
                # >20x. Hence garbled decoding despite a clean, strong
                # signal — audio captured straight from the microphone
                # (no codec) decodes better precisely because it never
                # goes through lossy compression.
                if getattr(self, "cw_rx_enabled", False):
                    with self._cw_rx_buf_lock:
                        self._cw_rx_buf.extend(mono_native)
                        max_cw = int(self._rx_rate * 20 * 2)
                        if len(self._cw_rx_buf) > max_cw:
                            del self._cw_rx_buf[:len(self._cw_rx_buf) - max_cw]

                with self._waterfall_buf_lock:
                    self._waterfall_buf.extend(mono_native)
                    max_wf_bytes = int(self._rx_rate * 3 * 2)  # max ~3s of headroom
                    if len(self._waterfall_buf) > max_wf_bytes:
                        del self._waterfall_buf[:len(self._waterfall_buf) - max_wf_bytes]

                self.rx_frames += 1
                if self.rx_frames % log_n == 0:
                    # RMS computed cheaply via numpy (used to be a Python
                    # loop summing s*s over 960 samples — unnecessary work
                    # on every log tick).
                    _a = np.frombuffer(mono_native, dtype=np.int16)
                    rms = int(np.sqrt(np.mean(_a.astype(np.float32)**2))) if _a.size else 0
                    print(f"[audio] RX {self.rx_frames} frames | RMS={rms}")
            except OSError as e:
                if self.rx_active: print(f"[audio] RX IO: {e}"); time.sleep(0.1)
            except Exception as e:
                if self.rx_active: print(f"[audio] RX err: {e}")
        print(f"[audio] RX thread ended — {self.rx_frames} frames")

    def pop_waterfall_chunk(self):
        """
        Drains and clears the small waterfall buffer. Returns (samples, sample_rate)
        where samples is numpy float64 normalized to -1..1 at the NATIVE
        rate (no resampling to 12000Hz — waterfall.compute_waterfall_column
        takes sample_rate as a parameter). Returns (None, None) if
        RX is inactive or the buffer is empty.
        """
        if not self.rx_active:
            return None, None
        with self._waterfall_buf_lock:
            if not self._waterfall_buf:
                return None, None
            raw_bytes = bytes(self._waterfall_buf)
            self._waterfall_buf = bytearray()
        try:
            import numpy as np
            samples = np.frombuffer(raw_bytes, dtype='<i2').astype(np.float64) / 32768.0
            return samples, self._rx_rate
        except Exception as e:
            print(f"[audio] pop_waterfall_chunk error: {e}")
            return None, None

    def pop_cw_rx_audio(self, target_rate: int = 3200):
        """Returns raw PCM for the CW decoder, resampled to target_rate.

        The signal comes STRAIGHT FROM THE CARD — it never passes through
        the Opus codec, so it keeps the sharp keying edges the model needs.
        """
        if not self.rx_active or not self.cw_rx_enabled:
            return None
        with self._cw_rx_buf_lock:
            if not self._cw_rx_buf:
                return None
            raw_bytes = bytes(self._cw_rx_buf)
            self._cw_rx_buf = bytearray()
        try:
            import numpy as np
            samples = np.frombuffer(raw_bytes, dtype='<i2').astype(np.float32) / 32768.0
            # Return raw samples and their rate — the anti-aliasing filter
            # and decimation are handled by the CW engine (it holds state
            # across calls).
            return samples, self._rx_rate
        except Exception as e:
            print(f"[audio] pop_cw_rx_audio error: {e}", flush=True)
            return None

    def start_tx(self, device=None):
        if not _PA: return False
        if self.tx_active: self.stop_tx()
        try:
            pa = self._pa_inst(); idx, info = self._dev_info(device, False)
            self._tx_dec = _make_decoder_stereo()
            self._webm_buf = b""
            kw = dict(format=pyaudio.paInt16, channels=1, rate=OPUS_RATE,
                      output=True, frames_per_buffer=OPUS_FRAMES * 2)  # 40ms buffer
            if idx is not None: kw["output_device_index"] = idx
            self._tx_stream = pa.open(**kw)
            self.tx_active = True; self.tx_frames = 0; self.tx_frames_received = 0
            self._tx_thread = threading.Thread(target=self._tx_loop, daemon=True, name="audio-tx")
            self._tx_thread.start()
            self._webrtc_thread = threading.Thread(target=self._webrtc_playback_loop, daemon=True, name="audio-tx-webrtc")
            self._webrtc_thread.start()
            print(f"[audio] TX START | '{device or 'default'}'")
            return True
        except Exception as e:
            print(f"[audio] TX error: {e}"); self.tx_active = False; return False

    def stop_tx(self):
        self.tx_active = False
        self._webm_buf = b""
        while True:
            try: self._webrtc_pcm_queue.get_nowait()
            except queue.Empty: break
        if self._tx_stream:
            try: self._tx_stream.stop_stream(); self._tx_stream.close()
            except: pass
            self._tx_stream = None
        print("[audio] TX STOP")

    def feed_tx_pcm(self, pcm: bytes):
        """
        Accepts already-decoded int16 mono PCM @ 48kHz (e.g. from WebRTC or FT8).
        Does NOT block the event loop — queues to a separate playback thread.

        NOTE (fix, 2026-07-04): the old logic did drop-old on a full queue
        (discard the oldest, add the newest). That works for live audio
        (SSB WebRTC) where you prefer a fresh stream, but is CATASTROPHIC
        for FT8/FT4 where you must play back exactly the same sequential
        stream. Effect: a 12s FT8 slot with PTT ON, but only the first
        ~2s of audio (100-frame maxsize) actually went out on the air —
        the rest was discarded.

        New logic: log a warning but still drop, since maxsize=800 already
        fits FT8 with margin. The warning fires if something actually goes wrong.
        """
        if not self.tx_active or not pcm:
            return
        try:
            self._webrtc_pcm_queue.put_nowait(pcm)
        except queue.Full:
            # Queue full — shouldn't happen with maxsize=800. If it does,
            # the playback thread is stuck, the card is jammed, or someone
            # is sending hours of audio at once.
            print(f"[audio] WARNING: TX PCM queue full ({self._webrtc_pcm_queue.qsize()}), "
                  f"dropping chunk {len(pcm)}B — audio may have gaps!")
            try: self._webrtc_pcm_queue.get_nowait()
            except: pass
            try: self._webrtc_pcm_queue.put_nowait(pcm)
            except: pass

    def _webrtc_playback_loop(self):
        """Thread that plays PCM from the WebRTC queue out to the sound card."""
        print("[audio] WebRTC playback thread start")
        while self.tx_active:
            try:
                pcm = self._webrtc_pcm_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            # When the queue has grown (card plays back slower than WebRTC
            # produces), discard the backlog and take the freshest frame.
            # Without this the buffer swells to hundreds of frames = many
            # seconds of lag, audio out of sync. Better to lose a fraction
            # of sound than have 9s of lag. Threshold 8 frames = ~160ms.
            # EXCEPTION: bulk_tx (FT8/FT4) - the whole signal is deliberately
            # queued at once, dropping would DESTROY the transmission
            # (only 160ms out of 4.94s would remain).
            _drop = 0
            while not self.bulk_tx and self._webrtc_pcm_queue.qsize() > 8:
                try:
                    pcm = self._webrtc_pcm_queue.get_nowait()
                    _drop += 1
                except queue.Empty:
                    break
            if _drop and self.tx_frames % 200 == 0:
                print(f"[audio] TX dropped {_drop} backlog frames (anti-lag)")
            if not self._tx_stream:
                continue
            try:
                # bulk_tx=True -> this is PCM from the FT8/FT4 encoder
                # (constant-amplitude tone, own multiplier). bulk_tx=False
                # -> this is decoded audio from the WebRTC microphone
                # (SSB/voice) - a different signal characteristic (already
                # close to full scale), its own separate multiplier. Without
                # this split a single slider would have to compromise
                # between two such different signals, making it hard to
                # hit the right ALC.
                _vol_key = "txVolume" if self.bulk_tx else "txVolumeSsb"
                vol_scale = min(float(self.cfg.get(_vol_key, 1.0)), 8.0)
                if vol_scale != 1.0:
                    try:
                        import numpy as np
                        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                        arr = np.clip(arr * vol_scale, -32768, 32767).astype(np.int16)
                        pcm = arr.tobytes()
                    except ImportError:
                        import array
                        arr = array.array('h'); arr.frombytes(pcm)
                        for i in range(len(arr)):
                            arr[i] = max(-32768, min(32767, int(arr[i] * vol_scale)))
                        pcm = arr.tobytes()
                self._tx_stream.write(pcm, exception_on_underflow=False)
                self.tx_frames += 1
                if self.tx_frames % 100 == 0:
                    print(f"[audio] TX (WebRTC) played {self.tx_frames} frames "
                          f"(queue={self._webrtc_pcm_queue.qsize()})")
            except OSError as e:
                print(f"[audio] TX PCM write err: {e}")
        print(f"[audio] WebRTC playback thread ended — {self.tx_frames} frames")

    def _flush_webm(self):
        webm = self._webm_buf
        self._webm_buf = b""
        # This path is EXCLUSIVELY decoded audio from the WebRTC microphone
        # (FT8/FT4 takes a different path - feed_tx_pcm, see webapp.py) -
        # always txVolumeSsb.
        vol = float(self.cfg.get("txVolumeSsb", 1.0))
        print(f"[audio] TX flush: {len(webm)}B WebM, volume={vol}x")
        pcm_data = _webm_to_pcm(webm, vol)
        if not pcm_data:
            print("[audio] TX flush: ffmpeg returned no PCM")
            return
        frame_size = OPUS_FRAMES * 2
        pos = 0
        while pos + frame_size <= len(pcm_data):
            try:
                self._tx_stream.write(pcm_data[pos:pos+frame_size], exception_on_underflow=False)
                self.tx_frames += 1
            except OSError: break
            pos += frame_size
        print(f"[audio] TX flush: played {self.tx_frames} frames ({self.tx_frames*20}ms)")

    async def feed_tx(self, data):
        if not self.tx_active or not data: return
        self.tx_frames_received += 1

        self._webm_buf += data

        # Debug for the first few chunks
        if self.tx_frames_received <= 5:
            print(f"[audio] TX chunk #{self.tx_frames_received}: {len(data)}B "
                  f"hex={data[:16].hex()} buf={len(self._webm_buf)}B")

        # Extract Opus frames from the WebM buffer and decode via opuslib
        frames, consumed = extract_opus_frames(self._webm_buf)
        if self.tx_frames_received <= 8:
            stuck_hex = self._webm_buf[consumed:consumed+12].hex() if consumed < len(self._webm_buf) else ""
            print(f"[audio] TX parser: frames={len(frames)} consumed={consumed}/{len(self._webm_buf)} "
                  f"stuck_at_hex={stuck_hex}")
        # Trim the buffer even if frames is empty
        if consumed > 0:
            self._webm_buf = self._webm_buf[consumed:]
        # Anti-deadlock: if the buffer grows with no progress, force it forward
        elif len(self._webm_buf) > 4096:
            if self.tx_frames_received % 20 == 0:  # don't flood the logs
                print(f"[audio] TX deadlock: buf={len(self._webm_buf)}B "
                      f"start={self._webm_buf[:16].hex()}")
            self._webm_buf = self._webm_buf[1:]
        if not frames:
            return

        dec       = self._tx_dec
        # feed_tx() decodes Opus from WebM coming from the WebRTC microphone - always SSB/voice.
        vol_scale = min(float(self.cfg.get("txVolumeSsb", 1.0)), 8.0)

        for opus_frame in frames:
            if not self.tx_active or not self._tx_stream:
                break
            try:
                if dec:
                    pcm = None
                    used_size = None
                    for frame_size in (OPUS_FRAMES, OPUS_FRAMES*2, OPUS_FRAMES*3, OPUS_FRAMES//2):
                        try:
                            pcm = dec.decode(opus_frame, frame_size, decode_fec=False)
                            used_size = frame_size
                            break
                        except Exception:
                            continue
                    if pcm is None:
                        continue

                    # Convert stereo -> mono (average L+R)
                    n_stereo_samples = len(pcm) // 4
                    if n_stereo_samples > 0:
                        try:
                            import numpy as np
                            stereo = np.frombuffer(pcm, dtype=np.int16).reshape(-1, 2)
                            mono   = ((stereo[:,0].astype(np.int32) + stereo[:,1].astype(np.int32)) // 2).astype(np.int16)
                            pcm = mono.tobytes()
                        except ImportError:
                            samples = struct.unpack(f"<{n_stereo_samples*2}h", pcm)
                            mono = [(samples[i*2] + samples[i*2+1]) // 2 for i in range(n_stereo_samples)]
                            pcm = struct.pack(f"<{n_stereo_samples}h", *mono)
                else:
                    pcm = opus_frame
                if not pcm:
                    continue
                n_samples = len(pcm) // 2
                # Gain — numpy if available
                if vol_scale != 1.0 and n_samples > 0:
                    try:
                        import numpy as np
                        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
                        arr = np.clip(arr * vol_scale, -32768, 32767).astype(np.int16)
                        pcm = arr.tobytes()
                    except ImportError:
                        import array
                        arr = array.array('h')
                        arr.frombytes(pcm)
                        for i in range(len(arr)):
                            v = int(arr[i] * vol_scale)
                            arr[i] = max(-32768, min(32767, v))
                        pcm = arr.tobytes()
                self._tx_stream.write(pcm, exception_on_underflow=False)
                self.tx_frames += 1
                if self.tx_frames % 100 == 0:
                    print(f"[audio] TX played {self.tx_frames} frames")
            except Exception as e:
                if "corrupted" not in str(e).lower():
                    print(f"[audio] TX decode err: {e}")

    def _tx_loop(self):
        print("[audio] TX thread ready (collecting WebM, plays back after PTT OFF)")
        while self.tx_active:
            time.sleep(0.05)
        print(f"[audio] TX thread ended — {self.tx_frames} frames")

    def get_status(self):
        return {
            "rx_active": self.rx_active, "tx_active": self.tx_active,
            "rx_device": self.rx_device, "rx_frames": self.rx_frames,
            "tx_frames": self.tx_frames, "rx_rate": self._rx_rate,
            "rx_ch": self._rx_ch,
            "opus": _OPUS, "pyaudio": _PA,
            "opus_lib": "opuslib" if _OPUS else "none",
            "sample_rate": OPUS_RATE, "frame_ms": 20,
            "txVolume": self.cfg.get("txVolume", 1.0),
            "txVolumeSsb": self.cfg.get("txVolumeSsb", 1.0),
        }

    def stop_all(self):
        self.stop_rx(); self.stop_tx()
        if self._pa:
            try: self._pa.terminate()
            except: pass
            self._pa = None


# ── WebM/Opus parser ──────────────────────────────────────────────────────────

def _vint_size(data, pos):
    """EBML variable-length integer — returns (value, new_pos)."""
    if pos >= len(data): return None, pos
    b = data[pos]
    if   b & 0x80: return b & 0x7F, pos+1
    elif b & 0x40:
        if pos+1 >= len(data): return None, pos
        return ((b & 0x3F) << 8) | data[pos+1], pos+2
    elif b & 0x20:
        if pos+2 >= len(data): return None, pos
        return ((b & 0x1F) << 16) | (data[pos+1] << 8) | data[pos+2], pos+3
    elif b & 0x10:
        if pos+3 >= len(data): return None, pos
        return ((b & 0x0F) << 24) | (data[pos+1]<<16) | (data[pos+2]<<8) | data[pos+3], pos+4
    elif b & 0x08:
        if pos+4 >= len(data): return None, pos
        v = 0
        for i in range(5): v = (v << 8) | data[pos+i]
        return v & 0x07FFFFFFFF, pos+5
    elif b & 0x04:
        if pos+5 >= len(data): return None, pos
        v = 0
        for i in range(6): v = (v << 8) | data[pos+i]
        return v & 0x03FFFFFFFFFF, pos+6
    elif b & 0x02:
        if pos+6 >= len(data): return None, pos
        v = 0
        for i in range(7): v = (v << 8) | data[pos+i]
        return v & 0x01FFFFFFFFFFFF, pos+7
    elif b & 0x01:
        # 8-byte vint — covers the "unknown size" 0x01FFFFFFFFFFFFFF
        if pos+7 >= len(data): return None, pos
        v = 0
        for i in range(8): v = (v << 8) | data[pos+i]
        return v & 0x00FFFFFFFFFFFFFF, pos+8
    return None, pos+1


def _ebml_id_len(b):
    """Number of bytes occupied by the EBML ID starting with byte b."""
    if   b & 0x80: return 1
    elif b & 0x40: return 2
    elif b & 0x20: return 3
    elif b & 0x10: return 4
    return 0


def extract_opus_frames(webm_buf: bytes):
    """Extract Opus frames from a WebM buffer. Returns (frames, consumed_pos)."""
    frames  = []
    pos     = 0
    n       = len(webm_buf)
    UNKNOWN = 0x00FFFFFFFFFFFFFF
    last_consumed = 0  # position after the last complete frame

    # Containers with unknown size (streaming) — descend into their content
    STREAM_CONTAINERS = {
        b'\x1f\x43\xb6\x75',  # Cluster
        b'\x18\x53\x80\x67',  # Segment
    }
    # Elements with known size that must be SKIPPED (contain no Opus)
    SKIP_CONTAINERS = {
        b'\x1a\x45\xdf\xa3',  # EBML Header
        b'\x16\x54\xae\x6b',  # Tracks
        b'\x15\x49\xa9\x66',  # Info
        b'\x1c\x53\xbb\x6b',  # Cues
    }

    while pos < n:
        if pos + 4 <= n:
            id4 = webm_buf[pos:pos+4]
            if id4 in STREAM_CONTAINERS:
                pos += 4
                size, pos = _vint_size(webm_buf, pos)
                if size is None: break
                continue  # descend into its content (unknown or known size)
            if id4 in SKIP_CONTAINERS:
                pos += 4
                size, pos = _vint_size(webm_buf, pos)
                if size is None: break
                if size == UNKNOWN or pos + size > n:
                    break  # can't skip it — wait for more data
                pos += size
                last_consumed = pos
                continue

        if pos >= n: break
        b = webm_buf[pos]

        if b == 0xA3:
            start = pos
            pos += 1
            size, pos = _vint_size(webm_buf, pos)
            if size is None or size < 4 or pos + size > n:
                break  # incomplete frame — wait for more data
            block = webm_buf[pos:pos+size]; pos += size
            p = 0
            _, p = _vint_size(block, p)
            if p is None or p + 3 > len(block): continue
            p += 3
            opus = block[p:]
            if len(opus) >= 4:
                frames.append(bytes(opus))
            last_consumed = pos
            continue

        if b == 0xA0:
            pos += 1
            size, pos = _vint_size(webm_buf, pos)
            if size is None: break
            if size == UNKNOWN or pos + size > n: break
            inner = webm_buf[pos:pos+size]; pos += size
            ip = 0
            while ip < len(inner) - 2:
                ib = inner[ip]
                if ib == 0xA1:
                    ip += 1
                    bs, ip = _vint_size(inner, ip)
                    if bs is None or ip + bs > len(inner): break
                    block = inner[ip:ip+bs]; ip += bs
                    p = 0; _, p = _vint_size(block, p)
                    if p is None or p + 3 > len(block): continue
                    p += 3
                    opus = block[p:]
                    if len(opus) >= 4: frames.append(bytes(opus))
                else:
                    il = _ebml_id_len(ib); ip += il
                    if ip >= len(inner): break
                    es, ip = _vint_size(inner, ip)
                    if es is None or es == UNKNOWN: break
                    ip += es
            last_consumed = pos
            continue

        if b in (0xE7, 0xAB, 0x9B):
            pos += 1
            size, pos = _vint_size(webm_buf, pos)
            if size is None: break
            if size == UNKNOWN or pos + size > n: break
            pos += size
            last_consumed = pos
            continue

        if b & 0x80:
            start = pos
            pos += 1
            size, pos = _vint_size(webm_buf, pos)
            if size is None: break
            if size == UNKNOWN:
                last_consumed = pos
                continue
            if pos + size > n:
                break  # incomplete element
            pos += size
            last_consumed = pos
            continue

        if b & 0x40:
            if pos + 1 >= n: break
            pos += 2
            size, pos = _vint_size(webm_buf, pos)
            if size is None: break
            if size == UNKNOWN:
                last_consumed = pos
                continue
            if pos + size > n: break
            pos += size
            last_consumed = pos
            continue

        # Unknown byte — if close to the end of the buffer, stop here
        # (may be the start of a 4-byte ID cut across chunk boundaries)
        if n - pos <= 4:
            break
        pos += 1
        last_consumed = pos

    return frames, last_consumed
