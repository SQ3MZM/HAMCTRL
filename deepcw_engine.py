"""
deepcw_engine.py — DeepCW server-side decoder

Model: e04/deepcw-engine (MIT)
- ONNX Runtime inference
- Audio PCM Float32 @ 3200 Hz via WebSocket
- CTC greedy decode → tekst → broadcast do UI
"""
from __future__ import annotations
import asyncio
import json
import math
import pathlib
import time
import urllib.request

import numpy as np

# Model trzymamy w katalogu DANYCH (APPDATA), nie obok EXE — inaczej kazda
# aktualizacja kasowalaby pobrany model (ten sam problem co z dziennikiem QSO).
try:
    from config import DATA as _DATA
    MODEL_DIR = pathlib.Path(_DATA) / "deepcw"
except Exception:
    MODEL_DIR = pathlib.Path("deepcw")
MODEL_FILE = MODEL_DIR / "model.onnx"
META_FILE  = MODEL_DIR / "model.onnx.json"
MODEL_URL  = "https://raw.githubusercontent.com/e04/deepcw-engine/main/model.onnx"
META_URL   = "https://raw.githubusercontent.com/e04/deepcw-engine/main/model.onnx.json"

# ── Metadane modelu (z model.onnx.json) ───────────────────────────────────────
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
        # Waskofiltrowa analiza tonu (jak Skimmer). Domyslnie wlaczona;
        # HAM_CW_BANDPASS=0 wylacza do porownania na zywo.
        import os as _os
        self._bandpass_on = _os.environ.get("HAM_CW_BANDPASS", "1") != "0"
        self._tone_hz = None
        self._dbg_n = 0        # licznik okien do rzadkiego logowania
        self._buf     = np.zeros(0, dtype=np.float32)
        # Dlugosc okna to KLUCZOWY parametr dokladnosci. Model CTC korzysta
        # z kontekstu: przy dlugim oknie widzi znak w otoczeniu sasiadow
        # i sam sie koryguje, przy krotkim czesciej zgaduje. Proba skrocenia
        # do 6 s (dla oszczednosci CPU) wyraznie pogorszyla dekodowanie —
        # zamiast 'QSO HR OP JURE RST' zwracal ciagi typu '3LGDEDLTHYN'.
        # Wracamy do 10 s; obciazenie regulujemy KROKIEM, nie oknem.
        self._win_sec  = 10.0
        # Okno 10 s — SPRAWDZONA wartosc, przy ktorej dekoder dziala dobrze.
        # Probowalem 8 s dla oszczednosci CPU, ale to zmiana "na wiare" — a
        # jakosc jest wazniejsza. Obciazenie tniemy INACZEJ: pomijaniem
        # inferencji na pustym pasmie (test tonu nizej), co nie dotyka jakosci
        # dekodowania realnego sygnalu. 6 s psulo model (kasza '3LGDEDLTHYN').
        self._hop_sec  = 2.0
        # Krok 2 s — SPRAWDZONA wartosc, przy ktorej dekoder dziala tak, jak
        # ma dzialac. Probowalem 2.5 s dla odciazenia dwurdzeniowego testu, ale
        # docelowa maszyna bedzie 4-rdzeniowa i udzwignie pelna szybkosc.
        # Kazdy fragment okna dekodowany 5x — az nadto dla glosowania.
        # Wyliczane ponownie w load() wg metadanych modelu, ale muszą istnieć
        # od razu — feed() moze zostac wywolane zanim model sie zaladuje.
        self._win_samp = int(self._win_sec * DEFAULT_META["sample_rate"])
        self._hop_samp = int(self._hop_sec * DEFAULT_META["sample_rate"])
        self._last_decode_time = 0.0
        # Prog energii — ADAPTACYJNY, sledzi poziom tla.
        #
        # Staly prog nie dziala, bo tlo zmienia sie w czasie: przy szumie
        # z oswietlenia LED RMS siega 0.020, w nocy przy czystym eterze bywa
        # 10x nizszy. Prog 0.008 przepuszczal wtedy sam szum (model zwracal ''
        # i marnowal ~700 ms na okno), a podniesienie go na sztywno odcieloby
        # slabe stacje po ustaniu zaklocen.
        #
        # Dlatego: obserwujemy najnizsze RMS z ostatnich okien (to jest tlo)
        # i wymagamy, zeby sygnal byl wyraznie powyzej niego.
        # Dolna granica bezwzgledna — chroni przed dekodowaniem przy calkiem
        # martwym wejsciu (np. odlaczona karta). Nisko, bo przy czystym eterze
        # slabe stacje maja RMS rzedu 0.006.
        self._rms_threshold    = 0.003
        self._rms_floor        = None      # biezacy poziom tla (adaptacyjny)
        self._rms_hist: list   = []        # historia do wyznaczania tla
        # Voting — historia ostatnich N dekodowań
        self._history    : list[str] = []  # ostatnie dekodingi (okna)
        self._history_max = 3              # glosowanie po 3 oknach (bylo 5)
        # Prog glosowania: znak wchodzi gdy pojawil sie w >=50% okien.
        # Krotsza historia = szybsze potwierdzanie (znak pojawia sie po 2
        # oknach zamiast 3), a serwer liczy TYLE SAMO — glosowanie to samo
        # porownywanie tekstu, zero dodatkowej inferencji. Kompromis: przy
        # slabym sygnale odrobine wiecej przekłaman przechodzi, ale na
        # zawodach liczy sie tempo. Warstwa jezykowa i tak czysci wyjscie.
        self._vote_ratio  = 0.5
        self._confirmed  : str = ""        # potwierdzony (zwyciezki) tekst
        self._sent_len   : int = 0         # ile znakow juz wyslano

    # ── Pobranie modelu ───────────────────────────────────────────────────────
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
            print("[deepcw] Metadata pobrana", flush=True)

            await _bcast("Pobieranie modelu (15 MB)...", 2)
            req = urllib.request.Request(MODEL_URL, headers={"User-Agent": "curl/7.0"})

            # Pobierz w wątku executor żeby nie blokować event loop
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
            print(f"[deepcw] Model pobrany: {len(data)/1e6:.1f} MB", flush=True)
            await _bcast(f"✓ Model gotowy ({len(data)/1e6:.1f} MB)", 100, len(data), total)
            await self.load()
            return {"ok": True, "sizeBytes": len(data)}
        except Exception as e:
            print(f"[deepcw] Błąd pobierania: {e}", flush=True)
            await _bcast(f"✗ {e}", -1)
            return {"ok": False, "error": str(e)}

    # ── Załaduj model do ONNX Runtime ─────────────────────────────────────────
    async def load(self):
        if not MODEL_FILE.exists():
            return False
        try:
            import onnxruntime as ort
            if META_FILE.exists():
                self.meta = json.loads(META_FILE.read_text())
            loop = asyncio.get_event_loop()
            # ONNX domyslnie zrownolegla inferencje na WSZYSTKIE rdzenie.
            # Przy modelu 15 MB i oknie 10 s narzut koordynacji watkow bywa
            # wiekszy niz zysk, a kazdy watek to dodatkowa pamiec robocza.
            # Jeden watek = mniej RAM i mniej rywalizacji z torem audio.
            _so = ort.SessionOptions()
            _so.intra_op_num_threads = 1
            _so.inter_op_num_threads = 1
            _so.enable_mem_pattern   = False   # mniejszy narzut pamieci
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
            print(f"[deepcw] Model załadowany, SR={sr}", flush=True)
            return True
        except Exception as e:
            print(f"[deepcw] Błąd ładowania: {e}", flush=True)
            return False

    # ── Przyjmij PCM Float32 z przeglądarki ───────────────────────────────────
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
        # Diagnostyka: zapisz to, co WLASNIE trafia do modelu (po resamplingu
        # i filtrze). Odsluch tego pliku rozstrzyga, czy problem jest w torze
        # audio, czy w samym modelu.
        self._capture_feed(pcm_f32)
        # WYCIEK PAMIECI (naprawiony): bufor byl przycinany dopiero PO bramce
        # czasowej ponizej, a ta odrzuca wiekszosc wywolan (return None).
        # Przegladarka sle audio kilkadziesiat razy na sekunde, wiec miedzy
        # kolejnymi dekodowaniami _buf rosl bez ograniczenia. Trzymamy tylko
        # tyle, ile potrzebuje okno dekodujace — reszta jest bezuzyteczna.
        if len(self._buf) > self._win_samp:
            self._buf = self._buf[-self._win_samp:]

        # WYKRYWANIE KONCA KROTKIEJ TRANSMISJI — dla szybkich QSO.
        #
        # Przy krotkiej wymianie ('RR 5NN TU', ~3 s) czekanie na pelne
        # 10-sekundowe okno i krok 2 s dawalo kilka sekund opoznienia — tekst
        # pojawial sie, gdy operator juz nadawal odpowiedz. Dlatego: gdy sygnal
        # byl obecny, a WLASNIE ucichl, przeliczamy natychmiast to, co przyszlo,
        # nie czekajac na pelne okno ani na krok. Kosztuje to JEDNA dodatkowa
        # inferencje na koniec transmisji — nie zwieksza obciazenia przy
        # dluzszej pracy, bo dlugie transmisje nie maja takich przerw.
        # Wykrywanie ciszy na KROTKIM ogonie (0.3 s), nie na calym kroku —
        # inaczej przerwa krotsza niz krok tonelaby w usrednieniu i konca
        # transmisji nie widac.
        _tail_n = int(0.3 * self.meta["sample_rate"])
        _rms_now = float(np.sqrt(np.mean(self._buf[-_tail_n:]**2))) \
            if len(self._buf) >= _tail_n else 0.0
        _was_active = getattr(self, "_sig_active", False)
        _is_active = _rms_now >= self._rms_threshold
        _just_ended = _was_active and not _is_active
        self._sig_active = _is_active
        if _is_active:
            self._close_hold = 0        # sygnal wrocil — zeruj licznik zamkniecia

        if len(self._buf) < self._win_samp and not _just_ended:
            return None

        now = time.time()
        # Normalna bramka czasowa — ALE koniec krotkiej transmisji ja omija,
        # zeby zareagowac od razu.
        if not _just_ended and now - self._last_decode_time < self._hop_sec * 0.9:
            return None
        # Nie zaczynaj kolejnej inferencji, gdy poprzednia jeszcze trwa.
        # Bez tego przy wolniejszym procesorze zadania pietrzylyby sie
        # w executorze, zjadajac pamiec i opozniajac audio coraz bardziej.
        if getattr(self, "_infer_busy", False):
            return None
        self._infer_busy = True
        try:
            return await self._decode_window(now, broadcast_fn,
                                             short_end=_just_ended)
        finally:
            # Flaga MUSI byc zwolniona przy kazdym wyjsciu (takze wczesniejszym
            # przez return albo wyjatek), inaczej dekoder zamilkl by na stale.
            self._infer_busy = False

    async def _decode_window(self, now, broadcast_fn, short_end=False):
        self._last_decode_time = now
        self._dbg_n += 1        # licznik okien (na gorze — uzywany tez w bandpass)

        # Przy koncu KROTKIEJ transmisji bierzemy fragment Z SYGNALEM (ostatnie
        # sekundy przed cisza), nie aktualna cisze — inaczej model dostalby
        # pusty ogon i nie mialby czego czytac.
        if short_end:
            # znajdz ostatni fragment z energia (do ~4 s wstecz)
            _look = min(len(self._buf), int(4.0 * self.meta["sample_rate"]))
            window = self._buf[-_look:] if _look > 0 else self._buf.copy()
        else:
            window = self._buf[-self._win_samp:] if len(self._buf) >= self._win_samp \
                else self._buf.copy()

        # WASKOFILTROWA ANALIZA wokol wykrytego tonu.
        #
        # Model dostawal cale pasmo 400-1200 Hz z calym szumem w srodku.
        # Izolacja pojedynczego sygnalu waskim filtrem daje lepszy stosunek
        # sygnal/szum. Przy pracy zdalnej sluchamy JEDNEJ stacji (zmierzone: w
        # nagraniu jeden ton), wiec wystarczy jeden filtr. Szerokosc +-120 Hz
        # nie obcina bocznych wsteg kluczowania nawet przy 40 WPM (potrzeba
        # +-67 Hz), a podnosi kontrast obwiedni. Zmierzone: 6.2x -> ~7x.
        window = self._bandpass_tone(window)

        # BEZ PRZETWARZANIA — model dostaje surowe audio, tak jak referencja.
        #
        # Autor modelu (e04/deepcw-engine) karmi go zwyklym downsamplingiem:
        #   ffmpeg -ac 1 -ar 3200 -sample_fmt s16
        # bez normalizacji, filtra ani odejmowania szumu. Model byl TRENOWANY
        # na surowym, zaszumionym CW i osiaga 0% bledu do -4 dB SNR — sam
        # radzi sobie z szumem. Kazde nasze przetwarzanie (normalizacja,
        # spectral subtraction, filtr) zmienialo sygnal na cos, czego model
        # nie widzial na treningu, i DRASTYCZNIE pogarszalo wyniki. Redukcja
        # szumu w oryginale istnieje, ale to OSOBNY model do sluchania uchem,
        # nie do karmienia dekodera.
        self._buf = self._buf[-self._win_samp:]

        rms = float(np.sqrt(np.mean(window**2)))

        # Aktualizuj poziom tla — ale TYLKO z okien bez wyraznego sygnalu.
        #
        # Wczesniej do historii trafialy wszystkie okna, takze te z CW. Tlo
        # rosло wiec razem z sygnalem, bramka szla w gore i zaczynala odrzucac
        # realna transmisje (obserwowane: tlo skoczylo do 0.033, bramka do
        # 0.038, a CW mialo 0.033 — trzy okna z rzedu odpadly w srodku QSO).
        # Klasyczne dodatnie sprzezenie zwrotne: im wiecej sygnalu, tym mniej
        # dekodowania. Dlatego probki tla bierzemy z okien CICHYCH.
        # Poziom tla = MINIMUM z ostatnich okien, z powolnym dryfem w gore.
        #
        # Percentyl z historii zawodzil, gdy transmisja trwa bez przerwy:
        # wszystkie probki zawieraly wtedy sygnal, tlo szlo w gore razem z nim,
        # bramka przekraczala poziom CW i dekoder odrzucal realne okna
        # (obserwowane: tlo 0.033, bramka 0.038, CW 0.033 — dziury w srodku
        # QSO). Minimum jest odporne: nawet w ciaglej transmisji sa slabsze
        # momenty, a przerwy miedzy nadawaniem daja prawdziwe tlo.
        self._rms_hist.append(rms)
        if len(self._rms_hist) > 40:
            self._rms_hist.pop(0)

        # BRAMKA — celowo prosta i bezwzgledna.
        #
        # Probowalem tu bramki adaptacyjnej (tlo z percentyla, potem z minimum
        # RMS) i obie zawodzily z tego samego powodu: w CW cisza miedzy znakami
        # trwa milisekundy, a okno usrednia 10 sekund. Przy ciaglej transmisji
        # nawet minimum jest niewiele nizsze od sredniej, wiec "tlo" ladowalo
        # na poziomie sygnalu, bramka szla ponad niego i dekoder milczal do
        # konca transmisji.
        #
        # Dlatego bramka odsiewa tylko WYRAZNIE martwe wejscie (odlaczona
        # karta, wyciszone radio). O tym, czy w oknie jest tresc, decyduje sam
        # model — gdy nie ma czego czytac, zwraca pusty ciag, co kosztuje jedna
        # inferencje i nic nie psuje.
        _gate = self._rms_threshold

        # OSZCZEDNOSC CPU: pomin inferencje, gdy w oknie NIE MA tonu CW.
        #
        # Na pustym pasmie (szum, brak nadawania) dekoder liczyl pelna
        # inferencje co krok — marnujac procesor bez powodu. Tani test: czy w
        # pasmie 400-1200 Hz jest wyrazny pik ponad szum. Jesli nie ma tonu,
        # nie ma czego dekodowac — pomijamy ONNX (najciezsza operacje).
        # To NIE bramka na sygnal CW (tego pilnuje model), tylko odsianie
        # calkowicie pustego pasma. FFT jest tanie wobec inferencji.
        if rms >= _gate and not short_end:
            try:
                _sr = self.meta["sample_rate"]
                _X = np.abs(np.fft.rfft(window * np.hanning(len(window))))
                _f = np.fft.rfftfreq(len(window), 1.0 / _sr)
                _b = (_f >= 400) & (_f <= 1200)
                if _b.any():
                    _peak = _X[_b].max()
                    _med  = np.median(_X[_b]) + 1e-9
                    # Prog CELOWO niski (4x): pomijamy TYLKO naprawde puste
                    # pasmo (czysty szum daje ~3-4x). Slaby ale realny sygnal
                    # CW daje kilkadziesiat-kilkaset x, wiec zawsze przejdzie.
                    # Wolimy raz policzyc niepotrzebnie niz przegapic stacje —
                    # jakosc dekodowania jest wazniejsza niz oszczednosc.
                    if _peak / _med < 4.0:
                        self._no_tone_run = getattr(self, "_no_tone_run", 0) + 1
                        # po kilku pustych oknach z rzedu pomijaj inferencje,
                        # ale co ~10 okno sprawdz mimo to (zeby nie przegapic
                        # slabego poczatku transmisji)
                        if self._no_tone_run % 10 != 0:
                            return None
                    else:
                        self._no_tone_run = 0
            except Exception:
                pass

        # Diagnostyka: co ~10s pokaz poziom sygnalu i stan konsensusu, zeby
        # bylo widac CZY audio dochodzi i czy model cokolwiek zwraca.
        # Diagnostyka co ~10 okien: poziom sygnalu i stan konsensusu
        if self._dbg_n % 10 == 1:
            print(f"[deepcw] RMS={rms:.5f} (bramka {_gate:.5f}) "
                  f"okien={len(self._history)}", flush=True)
        if rms < _gate:
            # Licz kolejne zablokowane okna — po kilku z rzedu bramka sama
            # sie rozluzni (patrz wyzej), zeby zawyzone tlo nie unieruchomilo
            # dekodera na stale.
            self._blocked_run = getattr(self, "_blocked_run", 0) + 1
            # Cisza = koniec transmisji. Czyscimy konsensus, zeby nastepna
            # stacja zaczela od zera (bez doklejania do poprzedniego tekstu).
            self._history.clear()
            self._confirmed = ""
            self._sent_len  = 0
            self._last_block = None
            # Zresetuj stan linii — nastepna stacja zaczyna od zera.
            self._last_consensus = ""
            self._idle_windows   = 0
            # Po ciszy NIE zamykamy linii natychmiast — przytrzymujemy ostatni
            # odczyt widoczny przez chwile, zeby operator zdazyl go przeczytac.
            # Przy szybkich QSO tekst znikal, zanim dalo sie go dostrzec.
            _hold = getattr(self, "_close_hold", 0)
            if getattr(self, "_line_open", False) or getattr(self, "_had_preview", False):
                if _hold < 3:
                    # jeszcze przez kilka okien pokazuj to, co bylo
                    self._close_hold = _hold + 1
                    if broadcast_fn and getattr(self, "_preview_hold", ""):
                        await broadcast_fn({"type": "deepcw_text",
                                            "block": getattr(self, "_last_block", "") or "",
                                            "preview": self._preview_hold})
                    return None
                # dopiero teraz zamknij
                if broadcast_fn:
                    await broadcast_fn({"type": "deepcw_text",
                                        "block": "", "preview": "", "close": True})
                self._had_preview = False
                self._line_open = False
                self._preview_hold = ""
                self._preview_ttl = 0
                self._close_hold = 0
            return None

        # Okno przeszlo bramke — zeruj licznik blokad.
        self._blocked_run = 0

        # Inferencja + liczenie spektrogramu w WATKU ROBOCZYM.
        #
        # Wczesniej _infer bylo wolane wprost w korutynie — na czas obliczen
        # (dziesiatki ms co sekunde) petla zdarzen stala, wiec serwer nie
        # obslugiwal ani strumienia audio, ani WebSocketow. Objaw: "skoki audio
        # od obciazenia". ONNX zwalnia GIL, wiec w osobnym watku liczy sie
        # rownolegle i tor audio nie jest przerywany.
        _t0 = time.time()
        loop = asyncio.get_event_loop()
        text = await loop.run_in_executor(None, self._infer, window)
        _ms = (time.time() - _t0) * 1000.0
        # Ostrzez, gdy inferencja nie miesci sie w kroku okna — wtedy serwer
        # liczy bez przerwy i audio zaczyna szarpac. Rozwiazanie: skrocic
        # okno (_win_sec) albo zwiekszyc krok (_hop_sec).
        if _ms > self._hop_sec * 1000 * 0.7:
            self._slow_warn = getattr(self, "_slow_warn", 0) + 1
            if self._slow_warn <= 3 or self._slow_warn % 20 == 0:
                print(f"[deepcw] UWAGA: inferencja {_ms:.0f} ms przy kroku "
                      f"{self._hop_sec*1000:.0f} ms — procesor nie nadaza, "
                      f"audio moze szarpac", flush=True)
        if self._dbg_n % 10 == 1:
            print(f"[deepcw] model zwrocil: {text!r} ({_ms:.0f} ms)", flush=True)
        if not text:
            return None

        self._history.append(text)
        if len(self._history) > self._history_max:
            self._history.pop(0)

        # Przy koncu KROTKIEJ transmisji glosowanie nie ma sensu — jest jeden
        # przebieg, nie ma czego glosowac. Pokazujemy odczyt wprost, zeby
        # szybkie QSO ('RR 5NN TU') pojawilo sie od razu. Przy dluzszej pracy
        # dziala normalne glosowanie (odsiewa przeklamania z wielu okien).
        if short_end:
            consensus = text
        else:
            consensus = self._consensus(self._history)
        if not consensus:
            return None

        # Wyslij tylko to, co PRZYBYLO na koncu konsensusu.
        #
        # UWAGA — tu byl blad powodujacy WIELOKROTNE wysylanie tego samego
        # fragmentu (w logu: "+'ANETM RIGIC'" po kilka razy pod rzad).
        # Poprzednio porownywalem konsensus z wyslanym tekstem od POCZATKU
        # (wspolny prefiks). Ale konsensus powstaje z okna przesuwajacego sie
        # co sekunde — jego poczatek stale ucieka razem z audio, wiec po
        # kilkunastu sekundach nie mial juz nic wspolnego z poczatkiem
        # poprzedniego. Porownanie prefiksow zawodzilo i kod wysylal cala
        # zawartosc okna od nowa, przy kazdym dekodowaniu.
        #
        # Teraz szukamy NAKLADKI: ile znakow z konca juz wyslanego tekstu
        # pokrywa sie z poczatkiem biezacego konsensusu. To co za nakladka —
        # jest nowe i tylko to leci do UI.
        # SKLADANIE TEKSTU — bez doklejania przyrostow.
        #
        # Wczesniej wysylalem tylko to, co "przybylo" wzgledem poprzedniego
        # konsensusu (nakladka). Problem: konsensus nie rosnie przewidywalnie,
        # tylko PRZEBUDOWUJE sie przy kazdym oknie ('CQ CQ CQDE' -> 'CQ CQCQDE'
        # -> 'CQ CQDE'). Dopasowanie raz trafialo, raz nie, wiec tekst wychodzil
        # raz dobrze, raz posiekany na 2-3 znakowe strzepy. Tego nie da sie
        # dostroic parametrem — to wada samego pomyslu.
        #
        # Teraz wysylamy CALY biezacy konsensus jako blok, a przegladarka
        # podmienia nim ostatnia linie. Nie ma czego gubic ani dublowac, bo
        # nie ma sklejania. Gdy transmisja sie konczy (cisza), blok zostaje
        # zamkniety i nastepna stacja zaczyna nowa linie.
        # LINIA — biezace okno, zamykana po przerwie w NADAWANIU.
        #
        # Probowalem tu linii NARASTAJACEJ (doklejanie tego, co wypadlo
        # z okna). Blad: w paśmie zawsze cos szumi, wiec cisza nigdy nie
        # nadchodzila, linia rosla bez konca i sklejala kilka transmisji
        # w jeden nieczytelny pas — z powtorkami w srodku, bo dopasowanie
        # prefiksu tez potrafi chybic.
        #
        # Teraz: linia pokazuje biezacy konsensus, a zamyka sie gdy przez
        # kilka okien nie przybywa nowej tresci (koniec nadawania, mimo ze
        # szum trwa). Dostajemy kolejne krotkie linie — po jednej na fragment
        # transmisji — zamiast jednego rosnacego pasa.
        # Zamkniecie linii opieramy na POZIOMIE SYGNALU, nie na powtorzeniu
        # tekstu. Wczesniej warunek 'consensus == poprzedni' nigdy nie
        # zachodzil, bo przy szumie model za kazdym razem zwraca troche inny
        # ciag — linia rosla bez konca i wszystko ladowalo w jednej. Koniec
        # nadawania to spadek energii: gdy RMS przez kilka okien jest przy
        # poziomie tla, transmisja sie skonczyla.
        _quiet_now = rms < (self._rms_threshold * 3.0)
        if _quiet_now:
            self._idle_windows = getattr(self, "_idle_windows", 0) + 1
        else:
            self._idle_windows = 0
        self._last_consensus = consensus

        # Poziom sygnalu do przegladarki (bargraf audio). Odbudowany po
        # przejsciu na audio z karty — przegladarka nie ma juz wlasnego
        # strumienia, wiec poziom podaje serwer.
        if broadcast_fn:
            _vu = min(1.0, rms * 8.0)   # skala do ~0..1 dla typowego CW
            await broadcast_fn({"type": "deepcw_vu", "level": round(_vu, 3)})

        # SKLADANIE LINII — narasta, gdy przybywa tresci; zamyka sie, gdy
        # przez kilka okien nic nowego nie dochodzi.
        #
        # Zamkniecia NIE opieramy na poziomie sygnalu (w pasmie zawsze cos
        # szumi, RMS nie spada) ani na powtorzeniu konsensusu (model za kazdym
        # razem zwraca troche inny ciag). Kryterium: czy do LINII przybyl nowy
        # tekst. Gdy model przez 3 okna nie dokłada nic — koniec nadawania.
        _acc = getattr(self, "_line_acc", "")
        _grew = False
        if not _acc:
            _acc = consensus
            _grew = bool(consensus)
        else:
            _tail = self._tail_after_overlap(_acc, consensus)
            if _tail and _tail.strip():
                _acc = (_acc + _tail)[-200:]     # twardy limit dlugosci linii
                _grew = True
        self._line_acc = _acc

        if _grew:
            self._idle_windows = 0
        else:
            self._idle_windows = getattr(self, "_idle_windows", 0) + 1

        # Trzy okna bez nowej tresci = koniec nadawania -> zamknij linie
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

        # Podzial dlugiej, ciaglej transmisji na czytelne wiersze. Bez tego
        # linia rosnie w nieskonczonosc, a co okno caly pas leci do przegladarki
        # (obciazenie serwera i klienta). Po ~72 znakach zamykamy biezacy
        # wiersz na granicy slowa i zaczynamy nowy — transmisja trwa dalej.
        if len(_acc) >= 64 and getattr(self, "_line_open", False):
            _cut = _acc.rfind(" ", 32, 68)    # tnij na spacji w rozsadnym zakresie
            if _cut < 0:
                _cut = 64
            _done = self._correct_calls(_acc[:_cut].strip())
            try:
                import deepcw_lang
                _done = deepcw_lang.correct(_done, self._known_calls)
            except Exception:
                pass
            if broadcast_fn:
                # domknij wiersz gotowa trescia, potem zasygnalizuj nowy
                await broadcast_fn({"type": "deepcw_text",
                                    "block": _done, "preview": "", "close": False})
                await broadcast_fn({"type": "deepcw_text",
                                    "block": "", "preview": "", "close": True})
            self._line_acc = _acc[_cut:].lstrip()
            self._last_block = None
            _acc = self._line_acc

        block = self._correct_calls(_acc) if _acc else ""
        # Warstwa jezykowa — poprawia zwroty CW, raporty i imiona wg struktury
        # lacznosci (jak Skimmer). Dziala na tekscie, zero kosztu audio.
        if block:
            try:
                import deepcw_lang
                block = deepcw_lang.correct(block, self._known_calls)
            except Exception:
                pass
        # Odsiej pojedyncze smieciowe znaki (np. samotne 'X' z zaklocenia)
        if block and len(block.strip()) <= 1:
            block = self._filter_noise(block)

        # PODGLAD: najswiezszy odczyt modelu wykraczajacy poza konsensus —
        # to, czego glosowanie jeszcze nie potwierdzilo. Operator widzi go
        # od razu (szaro), zamiast czekac 3-4 s na potwierdzenie.
        preview = ""
        if text and consensus:
            # Ile z konca konsensusu pokrywa sie z ostatnim odczytem
            for n in range(min(len(text), len(consensus)), 0, -1):
                if consensus.endswith(text[:n]):
                    preview = text[n:]
                    break
            else:
                preview = ""
        elif text:
            preview = text
        # Dlugosc podgladu pod najszybszych korespondentow (40 WPM ~ 8 znakow
        # na 2.5 s; z zapasem na spoznione okna).
        preview = preview[:24]

        if block != getattr(self, "_last_block", None):
            print(f"[deepcw] ={block!r}", flush=True)
            self._last_block = block
            self._line_open = True

        # PODTRZYMANIE PODGLADU: gdy biezacy odczyt nie ma juz nowej koncowki
        # (preview puste), NIE kasujemy szarego paska od razu — zostawiamy
        # ostatnia widoczna tresc przez kilka okien, zeby operator zdazyl ja
        # przeczytac. Przy szybkich QSO tekst pojawial sie i znikal, zanim
        # dalo sie go dostrzec.
        if preview:
            self._preview_hold = preview
            self._preview_ttl = 4          # utrzymaj przez ~4 okna
        elif getattr(self, "_preview_ttl", 0) > 0:
            self._preview_ttl -= 1
            preview = getattr(self, "_preview_hold", "")   # pokazuj ostatni
        else:
            self._preview_hold = ""

        _prev_had = getattr(self, "_had_preview", False)
        if broadcast_fn and (block or preview or _prev_had):
            await broadcast_fn({"type": "deepcw_text",
                                # 'block' = CALA biezaca linia do PODMIANY
                                # (nie doklejenia). Przegladarka nadpisuje nim
                                # ostatnia linie zamiast sklejac fragmenty.
                                "block": block,
                                "preview": preview})
        self._had_preview = bool(preview)
        return consensus

    # Cut numbers — skroty cyfr stosowane w CW dla oszczedzenia czasu.
    # Nadawca zamiast pelnej cyfry nadaje krotsza litere o podobnym wzorcu:
    #   0 -> T (jedna kreska zamiast pieciu)
    #   9 -> N,  5 -> E,  1 -> A,  7 -> G,  8 -> D,  2 -> U,  6 -> B
    # To NIE sa bledy dekodera — model odczytal dokladnie to, co poszlo
    # w eter. Dlatego NIGDY ich nie "poprawiamy" w wyswietlanym tekscie;
    # rozwijamy je wylacznie na czas POROWNANIA z baza znanych znakow.
    _CUT_NUMBERS = {"T": "0", "N": "9", "E": "5", "A": "1",
                    "G": "7", "D": "8", "U": "2", "B": "6"}

    @classmethod
    def _expand_cut_numbers(cls, token: str, known: str) -> str:
        """Rozwin cut numbers w 'token' — ale TYLKO na pozycjach, gdzie
        w znanym znaku stoi cyfra.

        Rozwijanie na slepo dawaloby falszywe trafienia, bo T/N/E to takze
        zwykle litery prawdziwych znakow (np. 'T' w G8KHF-podobnych). Dlatego
        patrzymy pozycyjnie: jesli w bazie na tym miejscu jest cyfra, a my mamy
        litere bedaca jej skrotem — traktujemy jako te sama cyfre.
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
        """Czy token to raport RST / numer w zawodach?

        Tam cut numbers sa NORMA (599 -> 5NN, 001 -> TT1), a kazda "korekta"
        bylaby przeklamaniem tego, co operator nadal. Takich tokenow nie
        ruszamy w ogole.
        """
        t = tok.upper()
        if not t:
            return False
        # Same cyfry / same skroty cyfr / mieszanka obu
        digits_or_cuts = set("0123456789") | set(
            DeepCWEngine._CUT_NUMBERS.keys())
        return len(t) <= 5 and all(c in digits_or_cuts for c in t)

    def _correct_calls(self, text: str) -> str:
        """Popraw przekrecone znaki wywolawcze wg puli znanych znakow.

        Siec neuronowa myli podobne znaki Morse'a (A/N, U/D, S/H, E/I), przez
        co realny znak wychodzi z jedna litera obok. Jesli w puli jest dokladnie
        JEDEN znak w odleglosci 1, podmieniamy — przy kilku kandydatach lepiej
        zostawic oryginal niz zgadywac.

        WAZNE: cut numbers (IC73TT, 5NN) NIE sa bledami — to swiadomy skrot
        nadawcy. Rozwijamy je tylko po to, zeby DOPASOWAC token do bazy;
        w wyniku zostaje forma, ktora naprawde poszla w eter.
        """
        if not text or not self._known_calls:
            return text
        out = []
        for tok in text.split(" "):
            t = tok.upper()
            # Raporty i numery zawodow zostawiamy nietkniete
            if self._looks_like_report(t):
                out.append(tok)
                continue
            # Kandydat na znak: 4-10 znakow, jest litera, i jest cyfra ALBO
            # cut number (znak moze byc nadany calkiem w skrocie: IC73TT)
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
        """Dopasuj token do bazy, uwzgledniajac cut numbers.

        Zwraca ORYGINALNY token, gdy po rozwinieciu skrotow okazuje sie
        poprawny (nie ma czego poprawiac), albo znany znak, gdy token jest
        faktycznie przekrecony. None gdy brak jednoznacznego dopasowania.
        """
        # 1) Czy po rozwinieciu cut numbers token jest juz poprawny?
        for known in self._known_calls:
            if len(known) == len(token):
                if self._expand_cut_numbers(token, known) == known:
                    return token   # nadane w skrocie — zostaw jak nadano
        # 2) Zwykla korekta o jeden znak (prawdziwe przekrecenie)
        return self.match_known_call(token)

    @staticmethod
    def _tail_after_overlap(sent: str, cons: str) -> str:
        """Zwroc te czesc 'cons', ktora nie pokrywa sie z koncem 'sent'.

        Sluzy do NARASTANIA linii: 'sent' to tekst od poczatku nadawania,
        'cons' to biezace okno. Doklejamy tylko to, co w oknie przybylo na
        koncu. Dopasowanie tolerancyjne, bo konsensus lekko sie przebudowuje
        (spacje, pojedyncze znaki) — sztywne porownanie gubiloby ciaglosc.
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
        """Glosowanie wiekszosciowe po WYROWNANYCH oknach dekodowania.

        Kazde okno wyrownujemy do okna-referencji (mediana dlugosci) przez
        difflib, po czym na kazdej pozycji referencji wygrywa najczestszy znak
        — o ile zebral co najmniej _vote_ratio glosow. Dzieki wyrownaniu
        tolerujemy zgubione/wstawione znaki (sztywne porownanie po indeksie
        rozjezdza sie po pierwszym zgubieniu litery).
        """
        h = [x for x in history if x]
        # Prog minimalny obnizony z 3 do 2 okien — konsensus rusza szybciej,
        # znak pojawia sie o jedno okno (2 s) wczesniej. Przy 2 oknach potrzeba
        # ich zgodnosci (need=2), wiec to nadal potwierdzenie, nie pojedynczy
        # odczyt. Wazne na zawodach, gdzie liczy sie tempo.
        if len(h) < 2:
            return ""
        import difflib
        from collections import Counter
        # Referencja = okno NAJBARDZIEJ PODOBNE do pozostalych, nie przypadkowe
        # (wczesniej: mediana dlugosci). Gdy na referencje trafialo okno
        # z bledem, difflib nie dopasowywal tam pozycji i poprawny znak
        # z pozostalych okien przepadal — mimo ze mial wiekszosc glosow.
        if len(h) > 2:
            from collections import Counter as _C
            # Najpierw: czy ktorys odczyt POWTARZA sie? Powtorzenie to
            # najmocniejszy dowod poprawnosci — model dwa razy przeczytal
            # tak samo. Bez tego referencja padala czasem na okno UCIETE
            # (krotsze bywa "podobniejsze" do wszystkich), przez co koncowe
            # znaki gubily sie mimo wiekszosci (obserwowane: 'I1GS' zamiast
            # 'I1GIS' przy 3 z 4 okien poprawnych).
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
            # opcodes daja PELNE odwzorowanie: nie tylko zgodne fragmenty
            # ('equal'), ale takze podmiany ('replace'). Przy samych
            # matching_blocks pozycje z bledem w referencji nie zbieraly
            # zadnych glosow — poprawny znak z pozostalych okien przepadal,
            # mimo ze mial wiekszosc (obserwowane: 'DG8G' -> 'DGG').
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == "equal":
                    for k in range(i2 - i1):
                        votes[i1 + k].append(cand[j1 + k])
                elif tag == "replace":
                    # Podmiana: glosuj pozycyjnie na tyle znakow, ile sie pokrywa
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

        # Odzyskaj KONCOWKE, ktorej zabraklo w referencji. Gdy na referencje
        # trafi okno uciete (np. 'JUR' przy wiekszosci 'JURE'), pozycji na
        # ostatnie znaki po prostu nie ma i glosowanie nie moze ich wskazac.
        # Sprawdzamy wiec, czy wiekszosc okien ma wspolne przedluzenie.
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
        """Usuń oczywisty szum — izolowane losowe znaki."""
        if not text:
            return text
        stripped = text.strip()
        if len(stripped) <= 1 and stripped not in 'EIT ':
            return ''
        return text

    # ── Baza znanych znakow (walidacja/korekta dekodow) ───────────────────────
    # Zasilana ze WSZYSTKICH zrodel: dekody FT8, log QSO, spoty DX cluster
    # wszystkich userow. Zdekodowany ciag porownujemy z realnymi znakami i
    # poprawiamy typowe przekrecenia (CW przez siec neuronowa myli podobne
    # znaki, np. A/N, U/D, S/H).
    _known_calls: set = set()

    @classmethod
    def add_known_calls(cls, calls):
        """Dodaj znaki do puli walidacyjnej (wielkie litery, bez smieci)."""
        for c in calls or ():
            c = (c or "").strip().upper()
            # Znak wywolawczy: 3-10 znakow, zawiera cyfre i litere
            if (3 <= len(c) <= 10 and any(ch.isdigit() for ch in c)
                    and any(ch.isalpha() for ch in c)):
                cls._known_calls.add(c)
        # Limit pamieci — pula zyje w RAM, nie moze rosnac bez konca
        if len(cls._known_calls) > 20000:
            cls._known_calls = set(list(cls._known_calls)[-10000:])

    @classmethod
    def match_known_call(cls, token: str) -> str | None:
        """Zwroc znany znak pasujacy do tokenu (dokladnie lub o 1 znak obok).

        Zwracamy dopasowanie tylko gdy jest JEDNOZNACZNE — przy kilku
        kandydatach w odleglosci 1 lepiej nie zgadywac.
        """
        t = (token or "").strip().upper()
        if len(t) < 3:
            return None
        if t in cls._known_calls:
            return t
        # Korekta o jeden znak (podmiana/brak/wstawka)
        cands = []
        for known in cls._known_calls:
            if abs(len(known) - len(t)) > 1:
                continue
            if cls._dist1(t, known):
                cands.append(known)
                if len(cands) > 1:
                    return None  # niejednoznaczne — nie poprawiamy
        return cands[0] if cands else None

    @staticmethod
    def _dist1(a: str, b: str) -> bool:
        """Czy odleglosc edycyjna miedzy a i b wynosi dokladnie 1?"""
        if a == b:
            return False
        la, lb = len(a), len(b)
        if la == lb:
            return sum(1 for x, y in zip(a, b) if x != y) == 1
        if la > lb:
            a, b, la, lb = b, a, lb, la
        # lb == la + 1 : sprawdz czy a jest b bez jednego znaku
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
        """Odejmowanie widma szumu — podnosi kontrast obwiedni CW.

        Liczymy STFT, szacujemy poziom szumu w kazdym prazku (20. percentyl
        magnitudy — tlo utrzymuje sie przez wiekszosc okna, znaki tylko
        chwilami), odejmujemy 1.5x tego poziomu z podloga 5%, i odtwarzamy
        sygnal. Faza zostaje oryginalna. Zmierzone: kontrast 14.6x -> 22x.
        """
        if x.size < 512:
            return x
        nfft, hop = 256, 64      # hop = nfft/4: okno Hanninga sumuje sie do 1.5
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
        # Overlap-add rekonstrukcja. Hop = nfft/4 z oknem Hanninga daje stala
        # sume okien (COLA), wiec normalizujemy przez znana wartosc, a nie
        # przez lokalna sume — ta potrafila spadac do zera na brzegach i
        # wysadzac amplitude do setek (obserwowane: peak 202, RMS 2.0 w logu).
        out = np.zeros(n_frames * hop + nfft, dtype=np.float32)
        for i, fr in enumerate(Fc):
            seg = np.fft.irfft(fr).astype(np.float32)
            out[i*hop:i*hop+nfft] += seg * win
        # Suma Hanninga przy hop=nfft/4 wynosi 1.5 na obszarze pelnego pokrycia
        out /= 1.5
        return out[:len(x)].astype(np.float32)

    def reset(self):
        """Wyczysc stan dekodera (przy wlaczaniu/wylaczaniu okna)."""
        self._buf = np.zeros(0, dtype=np.float32)
        self._history.clear()
        self._confirmed = ""
        self._last_block = None
        self._last_consensus = ""
        self._idle_windows = 0
        self._line_open = False
        self._had_preview = False

    def start_capture(self, seconds: float = 15.0) -> str:
        """Wlacz zapis audio DOKLADNIE w postaci, w jakiej trafia do modelu.

        Diagnostyka: pozwala odsluchac, co dekoder naprawde dostaje — po
        resamplingu do 3200 Hz i po filtrze antyaliasingowym, tuz przed
        inferencja. Jesli w pliku slychac czysty ton CW, problem jest w samym
        modelu; jesli kasza — w torze audio przed nim.
        """
        self._cap_buf = []
        self._cap_left = int(seconds * self.meta["sample_rate"])
        self._cap_path = str(MODEL_DIR / "deepcw_capture.wav")
        print(f"[deepcw] Zapis audio wlaczony ({seconds:.0f} s) -> "
              f"{self._cap_path}", flush=True)
        return self._cap_path

    def _capture_feed(self, pcm: np.ndarray):
        """Dolóz probki do zapisu; gdy komplet — zapisz plik WAV."""
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
            print(f"[deepcw] ZAPIS GOTOWY: {self._cap_path} "
                  f"({len(data)/self.meta['sample_rate']:.1f} s, "
                  f"peak={_peak:.3f}, rms={_rms:.4f})", flush=True)
        except Exception as e:
            print(f"[deepcw] Blad zapisu audio: {e}", flush=True)
        finally:
            self._cap_buf = []
            self._cap_left = 0

    def _bandpass_tone(self, x: np.ndarray) -> np.ndarray:
        """Izoluj wykryty ton CW waskim filtrem pasmowym (jak Skimmer).

        Wykrywa dominujacy ton w pasmie 300-1200 Hz, sledzi go z wygladzaniem
        (zeby nie skakac na chwilowy szum) i przepuszcza +-120 Hz wokol niego.
        Gdy w oknie nie ma wyraznego tonu, zwraca sygnal bez zmian — nie
        zgadujemy.
        """
        if x.size < 512:
            return x
        # Mozna wylaczyc do porownania na zywo (HAM_CW_BANDPASS=0)
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
            return x                      # brak wyraznego tonu — nie filtruj

        # Filtruj TYLKO sygnal zaszumiony. Na czystym, mocnym CW filtr pasmowy
        # szkodzi — obciecie pasma wprowadza dzwonienie rozmywajace idealne
        # krawedzie kluczowania (zmierzone: kontrast czystego sygnalu spadal
        # z 263x do 98x). Miara szumu to KONTRAST OBWIEDNI: stosunek poziomu
        # znaku do ciszy. Wysoki kontrast = czysty sygnal, zostaw; niski =
        # szum w przerwach, filtr pomoze.
        _wl = int(sr * 0.01)
        if _wl > 0 and len(x) > _wl * 4:
            _env = np.sqrt(np.convolve(x**2, np.ones(_wl)/_wl, mode="valid"))
            _contrast = np.percentile(_env, 90) / max(np.percentile(_env, 10), 1e-9)
            if _contrast > 15:
                return x                  # sygnal juz czysty — nie psuj
        prev = getattr(self, "_tone_hz", None)
        if prev is None or abs(peak_f - prev) > 200:
            tone = peak_f                 # nowa stacja / pierwszy raz
        else:
            tone = prev * 0.7 + peak_f * 0.3
        self._tone_hz = tone
        if self._dbg_n % 10 == 1:
            print(f"[deepcw] ton {tone:.0f}Hz (filtr +-120Hz)", flush=True)
        bw = 120.0
        mask = (f >= tone - bw) & (f <= tone + bw)
        Xf = np.where(mask, X, 0)
        return np.fft.irfft(Xf, n=len(x)).astype(np.float32)

    def _lowpass(self, x: np.ndarray, sr: int, cutoff: float) -> np.ndarray:
        """Filtr dolnoprzepustowy (FIR, okno Hamminga) przed decymacja.

        Zachowuje STAN miedzy kolejnymi porcjami audio — przegladarka sle
        dzwiek w kawalkach, wiec filtrowanie kazdego z osobna zostawialoby
        nieciaglosci na stykach (trzaski, ktore model widzi jako znaki).

        Wspolczynniki liczymy raz i cache'ujemy; koszt filtracji to ulamek
        milisekundy, nieporownywalnie mniej niz sama inferencja.
        """
        if x.size == 0:
            return x
        key = (sr, round(cutoff))
        if getattr(self, "_lp_key", None) != key:
            # 63 wspolczynniki = kompromis miedzy stromoscia a kosztem
            n_taps = 63
            fc = cutoff / sr                      # znormalizowana czestotliwosc
            m = np.arange(n_taps) - (n_taps - 1) / 2
            h = np.sinc(2 * fc * m) * np.hamming(n_taps)
            self._lp_taps = (h / h.sum()).astype(np.float32)
            self._lp_key = key
            self._lp_tail = np.zeros(n_taps - 1, dtype=np.float32)

        # Doklej ogon z poprzedniego wywolania, przefiltruj, zapamietaj nowy
        xin = np.concatenate([self._lp_tail, x])
        self._lp_tail = xin[-(len(self._lp_taps) - 1):].copy()
        return np.convolve(xin, self._lp_taps, mode="valid").astype(np.float32)

    def _infer(self, audio: np.ndarray) -> str:
        # Obniz priorytet TEGO watku (inferencji), zeby tor audio i siec zawsze
        # mialy pierwszenstwo. Bez tego ciezka inferencja ONNX potrafila na
        # moment oblozyc wszystkie rdzenie i zatkac audio (Python 100% w /perf).
        # Na Windows przez ctypes; gdzie indziej przez os.nice — best-effort,
        # bledy ignorujemy (nie sa krytyczne).
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
                self._prio_lowered = True   # nie probuj w kolko
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
        # WEKTORYZACJA — kluczowa dla obciazenia CPU.
        #
        # Poprzednia wersja liczyla FFT w PETLI Pythona, ramka po ramce (~660
        # iteracji na okno 10s przy hop=48). Petla FFT w czystym Pythonie to
        # jedna z najwolniejszych operacji — to ona zjadala procesor (DeepCW
        # skakal na 100%, zatykajac audio). Teraz skladamy wszystkie ramki w
        # jedna macierz (sliding_window_view) i liczymy FFT RAZ, wektorowo.
        # Wynik identyczny, ale dziesiatki razy szybciej.
        if len(audio) < nfft:
            return np.zeros((1, n_bins), dtype=np.float32)
        window = np.hanning(nfft).astype(np.float32)
        # Widok przesuwnego okna: [n_frames, nfft] bez kopiowania danych
        try:
            from numpy.lib.stride_tricks import sliding_window_view
            frames = sliding_window_view(audio, nfft)[::hop]
        except Exception:
            # Fallback dla bardzo starych numpy — recznie przez as_strided
            n_frames = (len(audio) - nfft) // hop + 1
            idx = np.arange(nfft)[None, :] + hop * np.arange(n_frames)[:, None]
            frames = audio[idx]
        # Wszystkie ramki * okno, potem FFT wszystkich naraz (jedna operacja)
        frames = frames * window                          # [T, nfft]
        spec = np.abs(np.fft.rfft(frames, n=nfft, axis=1)) ** 2   # [T, nfft//2+1]
        # Wybierz pasmo fmin..fmax
        freqs = np.fft.rfftfreq(nfft, d=1.0/sr)
        i0 = np.searchsorted(freqs, fmin)
        i1 = np.searchsorted(freqs, fmax)
        spec = spec[:, i0:i1]
        # Interpoluj do n_bins — wektorowo, bez petli po wierszach.
        # np.interp jest 1D, ale skala kolumn jest stala dla wszystkich ramek,
        # wiec liczymy wagi interpolacji RAZ i stosujemy do calej macierzy.
        if spec.shape[1] != n_bins:
            ncol = spec.shape[1]
            xp   = np.linspace(0, ncol - 1, n_bins)
            lo   = np.floor(xp).astype(np.int64)
            hi   = np.minimum(lo + 1, ncol - 1)
            frac = (xp - lo).astype(np.float32)
            # liniowa interpolacja wszystkich ramek naraz: [T, n_bins]
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
