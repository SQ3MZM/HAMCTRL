#!/usr/bin/env python3
"""
audio_stream.py — Opus audio streaming przez WebSocket
RX: karta dzwiekowa → PyAudio → opuslib → binary WS → przegladarka
TX: przegladarka → binary WS (WebM) → ffmpeg → PyAudio → karta dzwiekowa
"""
import asyncio, threading, time, struct, queue
import numpy as np

try:
    import pyaudio
    _PA = True
except ImportError:
    _PA = False
    print("[audio] pyaudio niedostepne")

try:
    import opuslib, opuslib.api
    _OPUS = True
    print("[audio] opuslib OK")
except Exception as e:
    _OPUS = False
    print(f"[audio] opuslib niedostepne: {e}")

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
    """Dekoder stereo dla TX — MediaRecorder enkoduje 2 kanaly nawet z mono mikrofonu."""
    if not _OPUS: return None
    try:
        return opuslib.Decoder(OPUS_RATE, 2)
    except Exception as e:
        print(f"[audio] Decoder stereo error: {e}"); return None


def _webm_to_pcm(webm_data: bytes, volume: float = 1.0) -> bytes:
    """Dekoduj WebM/Opus do PCM przez ffmpeg."""
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
        # Kolejka PCM do TX playback watku.
        # FT8 = 12.64s = 632 kawałki po 20ms. FT4 = 4.48s = 224 kawałki.
        # SSB/CW przez WebRTC = strumien ciagly (male porcje).
        # maxsize=800 = 16s bufora, mieści FT8 z bezpiecznym marginesem.
        # Przy stringu 12s FT8 wysylamy WSZYSTKO naraz (nie async trickle),
        # wiec kolejka MUSI być wystarczająco duża, inaczej queue.Full drop-old
        # zaczyna wyrzucać dane audio -> radio ma PTT ale nic nie leci na anteny.
        self._webrtc_pcm_queue = queue.Queue(maxsize=800)
        # Tryb bulk TX (FT8/FT4): caly sygnal wrzucany do kolejki NARAZ
        # (200+ ramek). Anty-lag drop NIE MOZE wtedy dzialac - wyrzucalby
        # zamierzone buforowanie jako "zaleglosc" (drop 239/248 ramek =
        # 160ms bzyku zamiast 4.94s sygnalu = moc/ALC zero, nic nie wychodzi
        # w eter). Ustawiane przez webapp na czas transmisji FT8/FT4.
        self.bulk_tx = False
        self._webrtc_thread = None
        self._tx_dec    = None
        self._webm_buf  = b""
        self._pa = None
        self.rx_frames = 0; self.tx_frames = 0; self.tx_frames_received = 0
        # Bufor surowego PCM dla dekodera CW (DeepCW), niezalezny od buforow
        # waterfall — kazdy oprozniany na wlasnym cyklu przez swojego konsumenta.
        self._cw_rx_buf = bytearray()
        self._cw_rx_buf_lock = threading.Lock()
        self.cw_rx_enabled = False      # wlaczane gdy operator otworzy dekoder
        # Osobny, MALY bufor do waterfall (podglad widmowy) — pobierany czesto
        # (co ~0.5-1s), niezalezny od cyklu dekodowania FT8 co 15s.
        self._waterfall_buf = bytearray()
        self._waterfall_buf_lock = threading.Lock()

    def set_loop(self, loop): self.loop = loop

    def _pa_inst(self):
        if not _PA: raise RuntimeError("pyaudio niedostepne")
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
            print(f"[audio] RX START | '{device or 'domyslne'}' | {use_rate}Hz {use_ch}ch")
            return True
        except Exception as e:
            print(f"[audio] RX blad: {e}"); self.rx_active = False; return False

    def stop_rx(self):
        self.rx_active = False
        if self._rx_stream:
            try: self._rx_stream.stop_stream(); self._rx_stream.close()
            except: pass
            self._rx_stream = None
        print("[audio] RX STOP")

    def _rx_loop(self):
        """Wlasny odczyt PyAudio, ROWNOLEGLY do niezaleznego przechwytywania
        Rust/cpal — realne sluchanie idzie bezposrednio przegladarka<->Rust
        WS (patrz CLAUDE.md), ta petla zasila WYLACZNIE dekoder CW (DeepCW)
        i podglad waterfall."""
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

                # Bufor dla dekodera CW — SUROWY PCM prosto z karty.
                #
                # Wczesniej DeepCW dostawal audio z przegladarki, czyli PO
                # kompresji Opus. Kodek stratny rozmywa krawedzie kluczowania
                # i podbija szum w przerwach: zmierzony kontrast obwiedni
                # spadal do 6.4x, podczas gdy model potrzebuje >20x. Stad kasza
                # w dekodowaniu mimo czystego, mocnego sygnalu — CW Skimmer
                # sluchajacy MIKROFONEM z powietrza czytal lepiej, bo nigdy nie
                # przechodzil przez kodek.
                if getattr(self, "cw_rx_enabled", False):
                    with self._cw_rx_buf_lock:
                        self._cw_rx_buf.extend(mono_native)
                        max_cw = int(self._rx_rate * 20 * 2)
                        if len(self._cw_rx_buf) > max_cw:
                            del self._cw_rx_buf[:len(self._cw_rx_buf) - max_cw]

                with self._waterfall_buf_lock:
                    self._waterfall_buf.extend(mono_native)
                    max_wf_bytes = int(self._rx_rate * 3 * 2)  # max ~3s zapasu
                    if len(self._waterfall_buf) > max_wf_bytes:
                        del self._waterfall_buf[:len(self._waterfall_buf) - max_wf_bytes]

                self.rx_frames += 1
                if self.rx_frames % log_n == 0:
                    # RMS liczone tanio przez numpy (bylo: petla Pythona
                    # sum(s*s) po 960 probkach — zbedna praca co log).
                    _a = np.frombuffer(mono_native, dtype=np.int16)
                    rms = int(np.sqrt(np.mean(_a.astype(np.float32)**2))) if _a.size else 0
                    print(f"[audio] RX {self.rx_frames} ramek | RMS={rms}")
            except OSError as e:
                if self.rx_active: print(f"[audio] RX IO: {e}"); time.sleep(0.1)
            except Exception as e:
                if self.rx_active: print(f"[audio] RX err: {e}")
        print(f"[audio] Watek RX koniec — {self.rx_frames} ramek")

    def pop_waterfall_chunk(self):
        """
        Wyciaga i czysci maly bufor waterfall. Zwraca (samples, sample_rate)
        gdzie samples to numpy float64 znormalizowany -1..1 przy NATYWNEJ
        stawce (bez resamplingu do 12000Hz — waterfall.compute_waterfall_column
        przyjmuje sample_rate jako parametr). Zwraca (None, None) jesli
        RX nieaktywne lub bufor pusty.
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
            print(f"[audio] pop_waterfall_chunk blad: {e}")
            return None, None

    def pop_cw_rx_audio(self, target_rate: int = 3200):
        """Zwraca surowy PCM dla dekodera CW, zresamplowany do target_rate.

        Sygnal pochodzi PROSTO Z KARTY — nie przechodzi przez kodek Opus,
        wiec zachowuje ostre krawedzie kluczowania, ktorych potrzebuje model.
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
            # Zwracamy surowe probki i ich stawke — filtr antyaliasingowy
            # i decymacje robi silnik CW (ma stan miedzy wywolaniami).
            return samples, self._rx_rate
        except Exception as e:
            print(f"[audio] pop_cw_rx_audio blad: {e}", flush=True)
            return None

    def start_tx(self, device=None):
        if not _PA: return False
        if self.tx_active: self.stop_tx()
        try:
            pa = self._pa_inst(); idx, info = self._dev_info(device, False)
            self._tx_dec = _make_decoder_stereo()
            self._webm_buf = b""
            kw = dict(format=pyaudio.paInt16, channels=1, rate=OPUS_RATE,
                      output=True, frames_per_buffer=OPUS_FRAMES * 2)  # 40ms bufor
            if idx is not None: kw["output_device_index"] = idx
            self._tx_stream = pa.open(**kw)
            self.tx_active = True; self.tx_frames = 0; self.tx_frames_received = 0
            self._tx_thread = threading.Thread(target=self._tx_loop, daemon=True, name="audio-tx")
            self._tx_thread.start()
            self._webrtc_thread = threading.Thread(target=self._webrtc_playback_loop, daemon=True, name="audio-tx-webrtc")
            self._webrtc_thread.start()
            print(f"[audio] TX START | '{device or 'domyslne'}'")
            return True
        except Exception as e:
            print(f"[audio] TX blad: {e}"); self.tx_active = False; return False

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
        Przyjmij juz zdekodowany PCM int16 mono @ 48kHz (np. z WebRTC lub FT8).
        NIE blokuje event loop — kolejkuje do osobnego watku odtwarzajacego.

        UWAGA (fix bug 2026-07-04): stara logika przy pelnej kolejce robila
        drop-old (usuwaj najstarsze, dodaj nowe). To dziala dla live audio
        (SSB WebRTC) gdzie preferujesz świezy strumien, ale KATASTROFALNIE
        dla FT8/FT4 gdzie musisz odtworzyc dokladnie ten sam sekwencyjny
        strumien. Efekt: FT8 12s slot z PTT ON, ale w eter szly tylko
        pierwsze ~2s audio (100 ramek maxsize) — reszta wyrzucona.

        Nowa logika: log warning ale nadal drop, bo maxsize=800 juz mieści
        FT8 z zapasem. Warning pokazuje jesli cos naprawde pojdzie nie tak.
        """
        if not self.tx_active or not pcm:
            return
        try:
            self._webrtc_pcm_queue.put_nowait(pcm)
        except queue.Full:
            # Kolejka pelna — nie powinno sie zdarzyc przy maxsize=800.
            # Jesli sie zdarza -> playback watek zablokowany, karta zapchana,
            # lub ktos wysyla wiele godzin audio naraz.
            print(f"[audio] UWAGA: kolejka TX PCM pelna ({self._webrtc_pcm_queue.qsize()}), "
                  f"drop kawalka {len(pcm)}B — audio moze mieć luki!")
            try: self._webrtc_pcm_queue.get_nowait()
            except: pass
            try: self._webrtc_pcm_queue.put_nowait(pcm)
            except: pass

    def _webrtc_playback_loop(self):
        """Watek odtwarzajacy PCM z kolejki WebRTC na karte audio."""
        print("[audio] WebRTC playback watek start")
        while self.tx_active:
            try:
                pcm = self._webrtc_pcm_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            # Gdy kolejka narosla (karta odtwarza wolniej niz WebRTC produkuje),
            # wyrzuc zalegle ramki i wez najswiezsza. Bez tego bufor puchnie do
            # setek ramek = wiele sekund opoznienia, audio rozjechane. Lepiej
            # stracic ulamek dzwieku niz miec 9s lagu. Prog 8 ramek = ~160ms.
            # WYJATEK: bulk_tx (FT8/FT4) - caly sygnal celowo w kolejce naraz,
            # dropowanie NISZCZYLO transmisje (zostawalo 160ms z 4.94s).
            _drop = 0
            while not self.bulk_tx and self._webrtc_pcm_queue.qsize() > 8:
                try:
                    pcm = self._webrtc_pcm_queue.get_nowait()
                    _drop += 1
                except queue.Empty:
                    break
            if _drop and self.tx_frames % 200 == 0:
                print(f"[audio] TX drop {_drop} zaleglych ramek (anty-lag)")
            if not self._tx_stream:
                continue
            try:
                # bulk_tx=True -> to jest PCM z enkodera FT8/FT4 (ton o stalej
                # amplitudzie, wlasny mnoznik). bulk_tx=False -> to jest zdekodowane
                # audio z mikrofonu WebRTC (SSB/glos) - inna charakterystyka
                # sygnalu (juz blisko pelnej skali), wlasny, osobny mnoznik. Bez
                # tego rozdzielenia jeden suwak musial kompromisowo obslugiwac
                # oba tak rozne sygnaly, utrudniajac trafienie w prawidlowe ALC.
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
                    print(f"[audio] TX (WebRTC) odtworzono {self.tx_frames} ramek "
                          f"(kolejka={self._webrtc_pcm_queue.qsize()})")
            except OSError as e:
                print(f"[audio] TX PCM write err: {e}")
        print(f"[audio] WebRTC playback watek koniec — {self.tx_frames} ramek")

    def _flush_webm(self):
        webm = self._webm_buf
        self._webm_buf = b""
        # Ta sciezka to WYLACZNIE zdekodowane audio z mikrofonu WebRTC (FT8/FT4
        # idzie inna droga - feed_tx_pcm, patrz webapp.py) - zawsze txVolumeSsb.
        vol = float(self.cfg.get("txVolumeSsb", 1.0))
        print(f"[audio] TX flush: {len(webm)}B WebM, volume={vol}x")
        pcm_data = _webm_to_pcm(webm, vol)
        if not pcm_data:
            print("[audio] TX flush: ffmpeg nie zwrocil PCM")
            return
        frame_size = OPUS_FRAMES * 2
        pos = 0
        while pos + frame_size <= len(pcm_data):
            try:
                self._tx_stream.write(pcm_data[pos:pos+frame_size], exception_on_underflow=False)
                self.tx_frames += 1
            except OSError: break
            pos += frame_size
        print(f"[audio] TX flush: odtworzono {self.tx_frames} ramek ({self.tx_frames*20}ms)")

    async def feed_tx(self, data):
        if not self.tx_active or not data: return
        self.tx_frames_received += 1

        self._webm_buf += data

        # Debug pierwszych kilku chunków
        if self.tx_frames_received <= 5:
            print(f"[audio] TX chunk #{self.tx_frames_received}: {len(data)}B "
                  f"hex={data[:16].hex()} buf={len(self._webm_buf)}B")

        # Wyciagnij ramki Opus z bufora WebM i dekoduj przez opuslib
        frames, consumed = extract_opus_frames(self._webm_buf)
        if self.tx_frames_received <= 8:
            stuck_hex = self._webm_buf[consumed:consumed+12].hex() if consumed < len(self._webm_buf) else ""
            print(f"[audio] TX parser: frames={len(frames)} consumed={consumed}/{len(self._webm_buf)} "
                  f"stuck_at_hex={stuck_hex}")
        # Przytnij bufor nawet jesli frames puste
        if consumed > 0:
            self._webm_buf = self._webm_buf[consumed:]
        # Anty-deadlock: jesli bufor rosnie bez postepu, wymus przesuniecie
        elif len(self._webm_buf) > 4096:
            if self.tx_frames_received % 20 == 0:  # nie zaśmiecaj logów
                print(f"[audio] TX deadlock: buf={len(self._webm_buf)}B "
                      f"start={self._webm_buf[:16].hex()}")
            self._webm_buf = self._webm_buf[1:]
        if not frames:
            return

        dec       = self._tx_dec
        # feed_tx() dekoduje Opus z WebM od mikrofonu WebRTC - to zawsze SSB/glos.
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

                    # Konwertuj stereo -> mono (usrednij L+R)
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
                # Wzmocnienie - numpy jesli dostepne
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
                    print(f"[audio] TX odtworzono {self.tx_frames} ramek")
            except Exception as e:
                if "corrupted" not in str(e).lower():
                    print(f"[audio] TX decode err: {e}")

    def _tx_loop(self):
        print("[audio] Watek TX gotowy (zbiera WebM, odtwarza po PTT OFF)")
        while self.tx_active:
            time.sleep(0.05)
        print(f"[audio] Watek TX koniec — {self.tx_frames} ramek")

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
    """EBML variable-length integer — zwraca (wartosc, nowa_pozycja)."""
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
        # 8-bajtowy vint — obejmuje "unknown size" 0x01FFFFFFFFFFFFFF
        if pos+7 >= len(data): return None, pos
        v = 0
        for i in range(8): v = (v << 8) | data[pos+i]
        return v & 0x00FFFFFFFFFFFFFF, pos+8
    return None, pos+1


def _ebml_id_len(b):
    """Ile bajtow zajmuje EBML ID zaczynajacy sie od bajtu b."""
    if   b & 0x80: return 1
    elif b & 0x40: return 2
    elif b & 0x20: return 3
    elif b & 0x10: return 4
    return 0


def extract_opus_frames(webm_buf: bytes):
    """Wyciagnij ramki Opus z bufora WebM. Zwraca (frames, consumed_pos)."""
    frames  = []
    pos     = 0
    n       = len(webm_buf)
    UNKNOWN = 0x00FFFFFFFFFFFFFF
    last_consumed = 0  # pozycja po ostatniej kompletnej ramce

    # Kontenery z nieznanym rozmiarem (streaming) — wchodzimy w zawartosc
    STREAM_CONTAINERS = {
        b'\x1f\x43\xb6\x75',  # Cluster
        b'\x18\x53\x80\x67',  # Segment
    }
    # Elementy ze znanym rozmiarem ktore trzeba POMINAC (nie zawieraja Opus)
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
                continue  # wejdz w zawartosc (nieznany lub znany rozmiar)
            if id4 in SKIP_CONTAINERS:
                pos += 4
                size, pos = _vint_size(webm_buf, pos)
                if size is None: break
                if size == UNKNOWN or pos + size > n:
                    break  # nie mozemy pominac — czekaj na wiecej danych
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
                break  # niepelna ramka — czekaj na wiecej danych
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
                break  # niepelny element
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

        # Nieznany bajt — jesli blisko konca bufora, zatrzymaj sie
        # (moze byc poczatkiem 4-bajtowego ID przecietego miedzy chunkami)
        if n - pos <= 4:
            break
        pos += 1
        last_consumed = pos

    return frames, last_consumed

