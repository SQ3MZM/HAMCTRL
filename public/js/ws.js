/**
 * ws.js — klient WebSocket + dekoder audio Opus (Web Audio API)
 *
 * Opus audio pipeline (RX — odbiór z radia):
 *   Serwer → Opus binary frames (WS) → OpusDecoderWorker (WASM) → PCM → AudioContext
 *
 * Opus audio pipeline (TX — nadawanie przez radio):
 *   Mikrofon → MediaStream → ScriptProcessor/AudioWorklet → PCM → opusEncoder (JS WASM) → WS → serwer
 */

(function() {
'use strict';

const S = window.AppState;
let ws = null;
let reconnectTimer = null;

// DIAGNOSTYKA: PerformanceObserver na 'longtask' - przegladarka sama zglasza
// KAZDA blokade glownego watku JS >50ms, niezaleznie od przyczyny. Uzyte do
// rozstrzygniecia sporu "czy skok znacznika WS to jank JS czy realne
// opoznienie sieci (LTE)" - jesli w chwili skoku WS NIE ma tu wpisu, to
// watek JS byl wolny i przyczyna jest sieciowa/serwerowa, nie front-end.
// Porownywac czas z ostrzezeniami "[ws] WYSOKI RTT" (case 'pong' nizej).
if (window.PerformanceObserver) {
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        const t = new Date();
        console.warn(`[longtask] ${Math.round(e.duration)}ms o ${t.toLocaleTimeString('pl-PL', {hour12:false})}.${String(t.getMilliseconds()).padStart(3,'0')} (start offset ${Math.round(e.startTime)}ms od nawigacji)`);
      }
    }).observe({ entryTypes: ['longtask'] });
  } catch(e) { /* longtask API niedostepne w tej przegladarce */ }
}

// ── Audio (Opus RX) ───────────────────────────────────────────────────────────
let audioCtx = null;
Object.defineProperty(window, 'audioCtx', { get: () => audioCtx, set: v => { audioCtx = v; } });
let audioQueue = [];
let isPlayingAudio = false;
let opusWorker = null;   // Web Worker z dekoderem opus-decoder WASM
window._audioEnabled = false;

function initAudioContext() {
  if (audioCtx) {
    if (audioCtx.state === 'suspended') audioCtx.resume();
    return;
  }
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({
      sampleRate: 48000,
    });
    window._masterGain = audioCtx.createGain();
    window._masterGain.gain.value = 0.7;  // fallback zanim ponizej dojdzie realny per-user poziom
    // _masterGain powstaje TUTAJ, dopiero gdy realnie startuje audio (gest
    // uzytkownika / autoplay policy) — czyli PO tym jak initRxVol() juz raz
    // probowal zastosowac zapisany poziom (app:ready, patrz nizej w tym
    // pliku). W tamtym momencie _masterGain jeszcze nie istnial, wiec
    // setRxVol() zaktualizowal tylko suwak w UI, a realny gain zostawal na
    // sztywnych 0.7 z linii wyzej — suwak wygladal na zapamietany, ale
    // glosnosc i tak zawsze wracala do 70%. Ponowne wywolanie teraz, gdy
    // _masterGain juz istnieje, dociaga zapisana per-user wartosc do
    // faktycznego dzwieku. Wykryte na zywo 2026-08-15.
    window.AudioControls?.initRxVol?.();

    // Przyciszanie RX podczas WLASNEGO TX (FT8/FT4). Radio z wlaczonym MONI
    // podaje na USB-out swoj sygnal TX — w przegladarce slychac piszczace
    // tony wlasnego nadawania (meczy sluch). Duck: na czas TX gain=0,
    // po TX przywracamy poprzedni poziom. Wolane z wsjtx.js (ft8_tx_status).
    window.setTxAudioDuck = function(on) {
      const g = window._masterGain;
      if (!g) return;
      if (on) {
        if (window._duckPrevGain == null) window._duckPrevGain = g.gain.value;
        g.gain.value = 0.0;
      } else if (window._duckPrevGain != null) {
        g.gain.value = window._duckPrevGain;
        window._duckPrevGain = null;
      }
    };

    window._audioCompressor = audioCtx.createDynamicsCompressor();
    window._audioCompressor.threshold.value = -24;
    window._audioCompressor.knee.value      = 10;
    window._audioCompressor.ratio.value     = 6;
    window._audioCompressor.attack.value    = 0.003;
    window._audioCompressor.release.value   = 0.15;

    window._masterGain.connect(window._audioCompressor);
    window._audioCompressor.connect(audioCtx.destination);

    if (audioCtx.state === 'suspended') audioCtx.resume();

    // Ustaw domyslne urzadzenie wyjsciowe (nie VB-Cable)
    // Szukamy Realtek/Speakers jako "default" — nie communications/VB
    if (audioCtx.setSinkId) {
      navigator.mediaDevices.enumerateDevices().then(devs => {
        const outputs = devs.filter(d => d.kind === 'audiooutput');
        // Priorytet: zapisany w localStorage > 'default' > pierwszy nie-VB
        const saved = localStorage.getItem('ham_audio_sinkId');
        const defaultDev = outputs.find(d => d.deviceId === 'default');
        const nonVB = outputs.find(d =>
          d.deviceId !== 'default' &&
          d.deviceId !== 'communications' &&
          !d.label.toLowerCase().includes('virtual') &&
          !d.label.toLowerCase().includes('cable')
        );
        const target = saved || (nonVB && nonVB.deviceId) || (defaultDev && 'default');
        if (target && target !== 'default') {
          audioCtx.setSinkId(target).then(() => {
            console.log('[audio] sinkId set:', target);
          }).catch(e => console.warn('[audio] setSinkId error:', e));
        }
      }).catch(() => {});
    }
  } catch(e) {
    console.warn('[audio] AudioContext error:', e);
  }
}

// Zaladuj glosnosc RX i TX GAIN per user po zalogowaniu
window.addEventListener('app:ready', () => {
  window.AudioControls?.initRxVol?.();
  window.AudioControls?.initTxGain?.();
});
// (kliknięcie, dotknięcie, klawisz) — polityka autoplay przegladarki
function _resumeAudioOnGesture() {
  if (audioCtx) {
    audioCtx.resume();
  } else {
    initAudioContext();
  }
  document.removeEventListener('click',     _resumeAudioOnGesture);
  document.removeEventListener('touchstart', _resumeAudioOnGesture);
  document.removeEventListener('keydown',   _resumeAudioOnGesture);
}
document.addEventListener('click',      _resumeAudioOnGesture, { once: true });
document.addEventListener('touchstart', _resumeAudioOnGesture, { once: true });
document.addEventListener('keydown',    _resumeAudioOnGesture, { once: true });

// Przegladarki tworza AudioContext w stanie 'suspended' jesli nie ma
// poprzedzajacego gestu uzytkownika (klikniecie/dotkniecie/klawisz) —
// poniewaz audio RX teraz wlacza sie automatycznie przy polaczeniu WS
// (bez przycisku), context moze utknac w 'suspended' mimo poprawnego
// dekodowania ramek. Wznawiamy go przy pierwszej interakcji ze strona.
let _audioResumeBound = false;
function _bindAudioResumeOnFirstInteraction() {
  if (_audioResumeBound) return;
  _audioResumeBound = true;
  const resume = () => {
    if (audioCtx && audioCtx.state === 'suspended') {
      audioCtx.resume().then(() => console.log('[audio] AudioContext wznowiony po interakcji'));
    }
  };
  ['click', 'keydown', 'touchstart'].forEach(ev =>
    document.addEventListener(ev, resume, { once: true, passive: true }));
}

// Dekoder Opus przez Web Codecs AudioDecoder API (natywny, bez WASM)
// Potwierdzono dzialanie: 960 sampli/ramke, 48kHz, 20ms frame
function initOpusDecoder() {
  if (!window.AudioDecoder) {
    console.warn('[audio] AudioDecoder (Web Codecs) niedostepny w tej przegladarce');
    window._opusDecoder = null;
    return;
  }
  try {
    const dec = new AudioDecoder({
      output: (frame) => {
        if (!audioCtx || frame.numberOfFrames === 0) { frame.close(); return; }
        try {
          const buf = audioCtx.createBuffer(
            frame.numberOfChannels,
            frame.numberOfFrames,
            frame.sampleRate
          );
          for (let ch = 0; ch < frame.numberOfChannels; ch++) {
            frame.copyTo(buf.getChannelData(ch), { planeIndex: ch });
          }
          frame.close();
          _scheduleAudioBuffer(buf);
        } catch(e) {
          frame.close();
        }
      },
      error: (e) => console.error('[audio] AudioDecoder blad:', e),
    });
    dec.configure({ codec: 'opus', sampleRate: 48000, numberOfChannels: 1 });
    // Interfejs kompatybilny z poprzednim _opusDecoder API
    window._opusDecoder = {
      _dec: dec,
      _ts: 0,
      _first: true,
      decodeFrame(opusData) {
        this._dec.decode(new EncodedAudioChunk({
          type: this._first ? 'key' : 'delta',
          timestamp: this._ts,
          data: opusData,
        }));
        this._first = false;
        this._ts += 20000; // 20ms w mikrosekundach
      },
      ready: Promise.resolve(),
    };
    console.log('[audio] AudioDecoder (Web Codecs) gotowy');
  } catch(e) {
    console.error('[audio] AudioDecoder init blad:', e);
    window._opusDecoder = null;
  }
}

// ── Audio scheduler — plynne odtwarzanie bez przerw miedzy ramkami ───────────
// Problem: src.start(audioCtx.currentTime) odtwarza kazda ramke "teraz",
// ale "teraz" zmienia sie miedzy ramkami przez opoznienia sieci/JS event loop,
// co daje mikroprzerwy (klikniecia) w odtwarzanym dzwieku.
// Rozwiazanie: planujemy kazda ramke na scisle okreslony czas w przyszlosci,
// jedna po drugiej bez szczelin. _nextAudioTime sluzy jako "wskaznik" gdzie
// skonczy sie poprzednia ramka.
let _nextAudioTime = 0;
let _aheadAvg = 0;
// POPRAWKA 2026-08-17: 180ms dawalo PRAWIE ZERO zapasu wobec realnego jittera
// zaobserwowanego na zywo przez usera przez tunel LTE (sq3mzmremote.duckdns.org)
// - pojedynczy skok RTT 183ms (bez blokady JS, bez reakcji adaptacyjnego
// bitrate w ham_audio.exe - za maly zeby przepelnic 5.12s bufor Rust) w
// zupelnosci wystarczal zeby oproznic ten bufor i dac slyszalne przyciecie.
// Podniesione do 260ms - dalej w udokumentowanym budzecie 200-300ms
// (RCForb), ale z realnym zapasem na tego typu krotkie skoki.
const AUDIO_LATENCY = 0.26;
const _AUDIO_MIN = 0.05;
const _AUDIO_MAX = 0.40;     // gorna granica — powyzej niej PRZYTNIJ bufor (nie rosnij)
let _audioBadgeAt = 0;       // throttle aktualizacji DOM (ramki leca co 20ms)

function _updateAudioLatencyBadge() {
  const now = performance.now();
  if (now - _audioBadgeAt < 500) return;
  _audioBadgeAt = now;
  const badge = document.getElementById('badge-audio-latency');
  if (!badge) return;
  if (!window._audioEnabled || _aheadAvg <= 0) {
    badge.textContent = '--'; badge.style.color = 'var(--dim)';
    badge.style.borderColor = 'var(--border2)';
    return;
  }
  const ms = Math.round(_aheadAvg * 1000);
  badge.textContent = ms + ' ms';
  // Cel 260ms (AUDIO_LATENCY), twardy sufit 400ms (_AUDIO_MAX) — powyzej niego
  // scheduler i tak przycina bufor, wiec czerwony = bufor stale dobija do sufitu.
  badge.style.color = ms < 300 ? 'var(--green)' : ms < 400 ? 'var(--amber)' : 'var(--red)';
  badge.style.borderColor = ms < 300 ? 'var(--green2)' : ms < 400 ? 'rgba(240,180,41,0.4)' : 'rgba(217,119,106,0.4)';
}

function _scheduleAudioBuffer(audioBuffer) {
  if (!audioCtx) return;
  const now = audioCtx.currentTime;
  let ahead = _nextAudioTime - now;

  if (_aheadAvg === 0) _aheadAvg = ahead > 0 ? ahead : AUDIO_LATENCY;
  _aheadAvg = _aheadAvg * 0.9 + ahead * 0.1;

  let rate = 1.0;
  if (ahead > 0) {
    const err = _aheadAvg - AUDIO_LATENCY;
    // playbackRate changes PITCH as well as tempo, so an audible speed-up sounds
    // like the tone rising — very noticeable on CW/SSB where pitch carries
    // meaning. Keep correction tiny (max 0.3%, inaudible) and only engage it
    // outside a wide dead zone, so normal LTE jitter never shifts the pitch.
    // Real starvation/overflow is handled by the hard reset below, not by rate.
    if (Math.abs(err) > 0.04) {
      const corr = Math.max(-0.003, Math.min(0.003, err * 0.02));
      rate = 1.0 + corr;
    }
  }

  // DWUSTRONNE ograniczenie — kluczowe, zeby bufor NIE ROSL bez konca.
  // Gdy spadnie za nisko: dolej (przeciw przycieciom).
  // Gdy urosnie za wysoko: PRZYTNIJ (przeciw narastaniu latency do 700ms).
  if (ahead > 0 && ahead < _AUDIO_MIN) {
    _nextAudioTime = now + AUDIO_LATENCY; ahead = AUDIO_LATENCY; _aheadAvg = AUDIO_LATENCY;
  } else if (ahead > _AUDIO_MAX) {
    // bufor spuchl (Python sie dlawil) — zetnij nadmiar, wroc do celu
    _nextAudioTime = now + AUDIO_LATENCY; ahead = AUDIO_LATENCY; _aheadAvg = AUDIO_LATENCY;
  }
  if (ahead < 0) {
    _nextAudioTime = now + AUDIO_LATENCY; ahead = AUDIO_LATENCY; _aheadAvg = AUDIO_LATENCY; rate = 1.0;
  }

  const src = audioCtx.createBufferSource();
  src.buffer = audioBuffer;
  if (rate !== 1.0) src.playbackRate.value = rate;
  src.connect(window._masterGain || audioCtx.destination);
  src.start(_nextAudioTime);
  _nextAudioTime += audioBuffer.duration / rate;
  _updateAudioLatencyBadge();
}

function playOpusFrame(buffer) {
  if (!window._audioEnabled || !audioCtx || !window._opusDecoder) return;
  // Format naglowka:
  // Rust ham_audio: [0xA1][seq 4B LE][opus...] — pomijamy 5 bajtow
  // Python fallback: [0xA1][opus...]            — pomijamy 1 bajt
  const view = new Uint8Array(buffer);
  const skip = (view[0] === 0xA1 && window._rustAudio) ? 5 : 1;
  const opusData = view.slice(skip);
  window._opusDecoder.ready.then(() => {
    try { window._opusDecoder.decodeFrame(opusData); } catch(e) {}
  });
}


// Debounce helper — ogranicza liczbę wysyłanych wiadomości
function _debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// Bufor zmian VFO — wysyłaj max 1 raz na 50ms (20Hz) zamiast przy każdym ticku scroll
const _sendFreqDebounced = _debounce((freq) => {
  // Sprawdz blokade radia przed wyslaniem freq
  const lock  = window.AppState?.radio_lock;
  const myUid = String(window.AppState?.my_uid || window.CurrentUser?.id || '');
  const role  = window.CurrentUser?.role;
  if (role !== 'admin' && (!lock?.locked || String(lock.user_id) !== myUid)) {
    // Przywroc wyswietlacz do aktualnej freq radia
    if (window.AppState?.freq) {
      window.AppState.freq = window.AppState.freq; // trigger re-render
      window.UI?.updateFreqDisplay?.();
    }
    const holder = lock.callsign || lock.username || '?';
    window.UI?.showToast(`⛔ Radio zajęte przez ${holder} — przejmij TRX`, 'error');
    return;
  }
  WS.send({ type:'freq', freq });
}, 50);

// ── WebSocket ─────────────────────────────────────────────────────────────────
function connect() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }

  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const token = localStorage.getItem('token') || sessionStorage.getItem('ham_token') || '';
  // WebSocket na tym samym hoście co HTTP, endpoint /ws
  // Działa lokalnie i przez proxy Replit (wss://hostname/ws)
  const wsBase = `${proto}://${location.host}/ws`;
  const wsUrl  = token
    ? `${wsBase}?token=${encodeURIComponent(token)}`
    : wsBase;
  ws = new WebSocket(wsUrl);
  window._mainWS = ws;

  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    console.log('[ws] Połączono');
    // Auto-ping co 30s dla biezacego pomiaru latency
    clearInterval(window._autoPingInterval);
    window._autoPingInterval = setInterval(() => { if (window.WS) window.WS.ping(); }, 30000);
    setTimeout(() => { if (window.WS) window.WS.ping(); }, 500);
    S.connected = true;
    window.UI?.updateConnectionStatus(true);

    // Subskrybuj kanaly wg AKTUALNIE aktywnej zakladki (nie tylko 'control').
    // Bez tego po reconnecte klient by nie dostawal scope_frame/ft8_waterfall
    // dopoki nie przelaczy zakladki.
    const activePage = document.querySelector('.tab-btn.active')?.getAttribute('onclick')?.match(/'(\w+)'/)?.[1] || 'radio';
    const channelsForPage = {
      radio:     ['control', 'scope'],
      wsjtx:     ['control', 'scope', 'ft8'],
      dxcluster: ['control', 'dxcluster'],
    };
    const channels = channelsForPage[activePage] || ['control'];
    ws.send(JSON.stringify({ type: 'subscribe', channels }));

    // Audio RX zawsze wlaczone (przycisk recznego wlaczania usuniety —
    // serwer teraz auto-startuje fizyczny strumien audio przy starcie,
    // wiec klient od razu subskrybuje sie do jego odbioru).
    _bindAudioResumeOnFirstInteraction();
    window.WS?.enableAudio(true);
  };

  ws.onclose = () => {
    console.log('[ws] Rozłączono');
    S.connected = false;
    window.UI?.updateConnectionStatus(false);
    reconnectTimer = setTimeout(connect, 3000);
  };

  ws.onerror = (e) => {
    console.warn('[ws] Błąd:', e);
  };

  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) {
      // Binary = Opus audio frame
      playOpusFrame(e.data);
      return;
    }
    try {
      const msg = JSON.parse(e.data);
      handleMessage(msg);
    } catch(err) {
      console.warn('[ws] Błąd parsowania:', err);
    }
  };
}

// Trzyma przycisk "#tx-mic-btn" (PROFIL/RADIO) w zgodzie z realnym stanem
// mikrofonu TX, niezaleznie od zrodla wlaczenia - reczny klik (onclick w
// index.html) czy zdalny most WSJT-X (wsjtx_tx_start/stop ponizej). Te same
// stany wizualne co dotychczasowy onclick, zeby nie bylo rozjazdu.
function _syncTxMicButton(active) {
  const btn = document.getElementById('tx-mic-btn');
  if (!btn) return;
  btn.textContent      = active ? '⏹ Zatrzymaj TX mikrofon' : '🎤 Nadawanie TX — mikrofon';
  btn.style.color      = active ? 'var(--red)' : 'var(--dim)';
  btn.style.borderColor = active ? 'var(--red)' : 'rgba(217,119,106,0.3)';
}

function handleMessage(msg) {
  switch (msg.type) {
    case 'init': {
      Object.assign(S, {
        freq:      msg.freq      ?? S.freq,
        freqB:     msg.freqB     ?? S.freqB,
        mode:      msg.mode      || S.mode,
        bandwidth: msg.bandwidth || S.bandwidth,
        ptt:       msg.ptt       ?? false,
        rfPower:   msg.rfPower   ?? S.rfPower,
        afGain:    msg.afGain    ?? S.afGain,
        squelch:   msg.squelch   ?? S.squelch,
        split:     msg.split     ?? false,
        vfo:       msg.vfo       || 'VFOA',
        sim:       msg.sim       ?? false,
        tuner:     msg.tuner     ?? false,
        connected: msg.connected ?? false,
        models:    msg.models    || {},
        // KRYTYCZNE: backend (webapp.py) NIGDY nie wysyla klucza "bands" w
        // wiadomosci init — lista pasm przychodzi osobno przez
        // loadBandsConfig() (-> /api/config/bands), wolane niezaleznie po
        // 'app:ready'. 'init' (WS) i 'app:ready' (auth.js) to dwa NIEZALEZNE
        // zdarzenia asynchroniczne ktore moga przyjsc w dowolnej kolejnosci.
        // Poprzedni kod (`msg.bands || {}`) bezwarunkowo nadpisywal S.bands
        // na PUSTY obiekt gdy 'init' przychodzilo PO tym jak
        // loadBandsConfig() juz poprawnie ustawilo 14 pasm — co wlaczalo
        // fallback w buildBandGrid() pokazujacy tylko '20m'. Teraz: gdy
        // msg.bands nie istnieje, zachowujemy to co juz jest w S.bands
        // (ten sam defensywny wzorzec co freq/mode/itp. ponizej, ?? zamiast ||).
        bands:     msg.bands     ?? S.bands,
        modes:     msg.modes     || [],
        rigs:      msg.rigs      || [],
        callsign:  msg.callsign  || '',
        stationLocator:  msg.stationLocator  || S.stationLocator,
        operatorLocator: msg.operatorLocator || S.operatorLocator,
      });
      window.UI?.fullRefresh();
      // Podswietl przyciski VFO A/B i SPLIT wg stanu z init — nowy user musi
      // od wejscia widziec ktore VFO aktywne i czy radio w splicie (wczesniej
      // wszystko wygaszone do pierwszego kliku).
      window.UI?.updateVFOBadges?.();
      window.RadioFunctions?.syncStates?.({vfo: S.vfo, split: S.split});
      window.Settings?.populateModels(S.models, S.rigs);
      if (msg.rotators && typeof window.RotW?.handleWS === 'function') RotW.handleWS(msg);
      // Stan zasilania radia z get_status - KLUCZOWE przy logowaniu nowego
      // usera: musi zobaczyc czy radio jest ON/OFF zgodnie z rzeczywistoscia
      // (inaczej przycisk pokazuje ON gdy ktos inny wylaczyl radio).
      if (msg.rigPowerOn !== undefined) {
        if (window.AppState) window.AppState.rigPowerOn = !!msg.rigPowerOn;
        window.RadioFunctions?.handlePowerState?.(!!msg.rigPowerOn);
      }
      // Ustaw stan blokady radia z init
      if (window.AppState && msg.locked !== undefined) {
        window.AppState.radio_lock = {
          locked:   msg.locked,
          user_id:  msg.user_id,
          username: msg.username,
          callsign: msg.callsign,
        };
      }
      // Przekaz stan operatorow do OpPanel
      if (window.OpPanel?.handleWS) window.OpPanel.handleWS(msg);
      break;
    }

    case 'telemetry':
      S.freq      = msg.freq      ?? S.freq;
      S.freqB     = msg.freqB     ?? S.freqB;
      S.mode      = msg.mode      ?? S.mode;
      S.bandwidth = msg.bandwidth ?? S.bandwidth;
      S.sMeter    = msg.sMeter    ?? 0;
      S.ptt       = msg.ptt       ?? false;
      S.vfo       = msg.vfo       ?? S.vfo;
      S.split     = msg.split     ?? S.split;
      S.connected = msg.connected ?? S.connected;
      S.sim       = msg.sim       ?? S.sim;
      window.UI?.updateTelemetry();
      break;

    case 'level_value':
      // Zmiana wartosci suwaka (poller CIV wykryl zmiane na radiu, albo
      // inny admin przesunal slider — WS broadcast dla wszystkich)
      window.RadioFunctions?.handleLevelValue?.(msg);
      break;
    case 'rig_slider_ack':
      // ACK zmiany suwaka od innego usera - aktualizuj slider u tego
      // ktory nie zmienial (skip=ws w serwerze pomija autora zmiany).
      window.RadioFunctions?.handleLevelValue?.(msg);
      break;
    case 'func_state':
      window.RadioFunctions?.handleFuncState?.(msg);
      break;
    case 'rig_features':
      window.RadioFunctions?.handleWsMessage?.(msg);
      break;
    case 'audio_ready':
      window._rustAudio = !!(msg.status && msg.status.rust);
      console.log('[audio] ready rust=' + window._rustAudio);
      // Po restarcie ham_audio (np. zmiana karty) strumien Opus zaczyna sie od
      // nowa. Dekoder WebCodecs musi potraktowac pierwsza ramke nowego strumienia
      // jako 'key', nie 'delta' - inaczej gubi sie i nie ma dzwieku mimo ze WS
      // audio polaczony i ramki naplywaja. Reset _first wymusza keyframe.
      if (window._opusDecoder) {
        window._opusDecoder._first = true;
        window._opusDecoder._ts = 0;
      }
      // Wymus reconnect audio WS na nowy proces Rusta (stary strumien padl)
      if (window._audioEnabled) {
        try { if (window._audioWs) window._audioWs.close(); } catch(e) {}
        setTimeout(() => { if (typeof _connectAudioWs === 'function') _connectAudioWs(); }, 500);
      }
      break;
    case 'relay_state':
      window.RelayUI?.onWSMessage(msg);
      break;
    case 'dx_spot':
      window.DXCluster?.handleSpot?.(msg);
      break;
    case 'dx_status':
      window.DXCluster?.handleStatus?.(msg);
      break;
    case 'webrtc_answer':
      _txMic.onAnswer(msg);
      break;
    case 'webrtc_ice':
      _txMic.onRemoteIce(msg);
      break;
    case 'webrtc_error':
      console.warn('[txmic] serwer:', msg.error);
      _txMic.stop();
      break;
    case 'wsjtx_tx_start':
      // Zewnetrzny WSJT-X/JTDX (przez most wsjtx_local.py + emulacja Hamlib)
      // wlaczyl PTT - uruchom strumien mikrofonu (tu: wirtualny kabel audio
      // typu VB-Audio, wybrany w PROFIL jako "MIKROFON TX"), tak samo jak
      // przy recznym PTT. Bylo calkowicie nieobslugiwane - radio dostawalo
      // PTT ale bez zadnego audio (cisza w eterze).
      _txMic.start();
      _syncTxMicButton(true);
      break;
    case 'wsjtx_tx_stop':
      _txMic.stop();
      _syncTxMicButton(false);
      break;
    case 'smeter': window.UI?.updateSMeter(msg.value ?? 0); break;
    case 'pong': {
      const rtt = Date.now() - (window._pingT0 || Date.now());
      clearTimeout(window._pingTimeout);
      const badge = document.getElementById('badge-latency');
      if (badge) {
        badge.textContent = rtt + ' ms';
        badge.style.color = rtt < 50 ? 'var(--green)' : rtt < 150 ? 'var(--amber)' : 'var(--red)';
        badge.style.borderColor = rtt < 50 ? 'var(--green2)' : rtt < 150 ? 'rgba(240,180,41,0.4)' : 'rgba(217,119,106,0.4)';
      }
      // DIAGNOSTYKA: zapisz kazdy podejrzanie wysoki RTT z dokladnym czasem
      // zegarowym, zeby dalo sie porownac z logiem konsoli ham_audio.exe
      // (znaczniki -> LOW/HIGH bitrate) i z longtask obserwerem ponizej -
      // rozstrzyga czy skok to blokada watku JS czy realne opoznienie sieci.
      if (rtt >= 150) {
        console.warn(`[ws] WYSOKI RTT ${rtt}ms o ${new Date().toLocaleTimeString('pl-PL', {hour12:false})}.${String(new Date().getMilliseconds()).padStart(3,'0')}`);
      }
      break;
    }
    case 'txmeter': window.UI?.updateTxMeter(msg); break;
    case 'freq':
      // Grace period: ignoruj przychodzacy freq z serwera przez ~1s po
      // wlasnej, lokalnej zmianie (sendFreq). Bez tego: przy szybkim scroll-
      // strojeniu (wiele krokow w krotkim czasie), radio CI-V moze jeszcze
      // nie zdazyc fizycznie przestroic sie miedzy kolejnymi krokami, a
      // serwer (civ.py transceive handler) potrafi w tym oknie zlapac
      // "posredni"/nieaktualny odczyt czestotliwosci z radia i rozeslac go
      // do WSZYSTKICH klientow (wlacznie z tym ktory wlasnie scrollowal) —
      // bez tej ochrony nadpisywalo to S.freq stara wartoscia, co cofalo
      // podswietlenie przycisku pasma do poprzedniego pasma. Potwierdzone
      // na zywo 2026-06-21 (scroll 14MHz->28MHz, podswietlony zostawal 20m).
      if (S._localFreqSetAt && (Date.now() - S._localFreqSetAt) < 1000) break;
      S.freq = msg.freq;
      window.UI?.updateFreqDisplay();
      window.UI?.updateVFOBadges?.();  // aktualizuj tez badge pasma
      window.UI?.updatePTT?.();        // cross-band guard
      break;
    case 'mode':
      S.mode = msg.mode;
      S.bandwidth = msg.bandwidth || S.bandwidth;
      if (msg.filterNum) {
        const sel = document.getElementById('bw-select');
        if (sel) sel.value = String(msg.filterNum);
      }
      window.UI?.updateModeButtons();
      window.UI?.updateVFOBadges?.();  // aktualizuj tez badge trybu
      break;
    case 'ptt':   S.ptt  = msg.ptt;  window.UI?.updatePTT(); break;
    case 'tuner':
      S.tuner = msg.value;
      document.getElementById('tuner-btn')?.classList.toggle('active', !!msg.value);
      window.RadioFunctions?.handleLegacyFunc?.('tuner', msg);
      break;
    case 'preamp':
      window.RadioFunctions?.handleLegacyFunc?.('preamp', msg);
      break;
    case 'attenuator':
      window.RadioFunctions?.handleLegacyFunc?.('attenuator', msg);
      break;
    case 'power_state':
      window.RadioFunctions?.handlePowerState?.(!!msg.value);
      break;
    case 'toast':
      window.UI?.showToast(msg.message || msg.msg || '');
      break;
    case 'level':
      if (msg.param === 'RFPOWER') S.rfPower = msg.value;
      if (msg.param === 'AF')      S.afGain  = msg.value;
      if (msg.param === 'SQL')     S.squelch = msg.value;
      break;
    case 'freqB':
      S.freqB = msg.freqB;
      window.UI?.updateFreqB?.();
      window.UI?.updatePTT?.();  // cross-band guard
      break;
    case 'split':
      S.split = msg.split;
      S.freqB = msg.freqB || S.freqB;
      window.UI?.updateFreqB?.();
      window.UI?.updatePTT?.();  // cross-band guard
      window.UI?.updateVFOBadges?.();
      window.RadioFunctions?.syncStates?.({split: S.split});
      break;
    case 'init_patch':
      // Czesciowa aktualizacja stanu (np. admin zmienil lokator stacji).
      // Bez przeladowania strony — azymut rotora przeliczy sie sam.
      if (msg.stationLocator !== undefined) {
        S.stationLocator = msg.stationLocator;
        if (window.AppState) window.AppState.stationLocator = msg.stationLocator;
      }
      break;
    case 'vfo':
    case 'vfo_sel':
      // Stan aktywnego VFO z serwera — podswietl przyciski A/B u WSZYSTKICH
      // klientow (nie tylko u tego kto kliknal).
      S.vfo = msg.vfo || 'VFOA';
      window.UI?.updateFreqB?.();
      window.UI?.updateVFOBadges?.();
      window.RadioFunctions?.syncStates?.({vfo: S.vfo});
      break;
    case 'config_update':
      // Admin zmienil liste wlaczonych pasm w ustawieniach — odswiez S.bands
      // i siatke przyciskow bez przeladowania strony.
      window.UI?.loadBandsConfig?.();
      break;
    case 'online_update':
    case 'radio_lock_state':
      // Zapisz stan blokady do AppState — uzywane przez VFO i sendFreq
      if (window.AppState) {
        window.AppState.radio_lock = {
          locked:   msg.locked,
          user_id:  msg.user_id,
          username: msg.username,
          callsign: msg.callsign,
        };
      }
      if (typeof window.OpPanel?.handleWS === 'function') window.OpPanel.handleWS(msg);
      // Zaktualizuj wizualne wyszarzenie panelu Radio (readonly mode)
      window.UI?.applyRadioLockUI?.();
      break;
    case 'radio_request':
    case 'radio_request_received':
      if (typeof window.OpPanel?.handleRequest === 'function') window.OpPanel.handleRequest(msg);
      break;
    case 'radio_request_rejected':
      if (typeof window.OpPanel?.handleRejected === 'function') window.OpPanel.handleRejected(msg);
      break;
    case 'qso_new': window.QSOLog?.prependEntry(msg.entry); break;
    case 'error': window.UI?.showToast('✗ ' + msg.message, 'error'); break;
    case 'scope_frame':
    case 'scope_data':
      if (typeof window.Waterfall?.handleScopeData === 'function') Waterfall.handleScopeData(msg);
      break;
    case 'scope_reset':
      // Radio wylaczone - wyczysc waterfall i pokaz stan OFF
      if (typeof window.Waterfall?.onPowerReset === 'function') Waterfall.onPowerReset();
      break;
    case 'rotator_update':
      if (typeof window.RotW?.handleWS === 'function') RotW.handleWS(msg);
      if (typeof window.Rotator?.handleWS   === 'function') Rotator.handleWS(msg);
      // Ten typ ma wlasny case (powyzej), wiec NIGDY nie spadal do 'default'
      // ponizej, gdzie normalnie leci do WSJTX.handleWS — zywy odczyt pozycji
      // rotora w panelu WSJT-X (wiersz ANTENA) nigdy nie dostawal biezacych
      // aktualizacji, tylko jednorazowy fetch z init(). Zglaszane na zywo:
      // "odswiezanie pozycji rotora jest tylko wtedy kiedy zmienisz zakladke"
      // — w rzeczywistosci to byl jedyny raz kiedy w ogole dostawal wartosc.
      if (typeof window.WSJTX?.handleWS === 'function') WSJTX.handleWS(msg);
      break;
    case 'deepcw_text':
      window.DeepCW?.handleText?.(msg);
      break;
    case 'deepcw_vu':
      window.DeepCW?.handleVU?.(msg.level);
      break;
    case 'deepcw_progress':
      { const l = document.getElementById('deepcw-admin-log');
        const b = document.getElementById('deepcw-admin-bar');
        if (l) {
          const detail = msg.detail ? ` (${msg.detail})` : '';
          const path   = msg.savePath ? `<br><span style="color:var(--dim);font-size:9px;">→ ${msg.savePath}</span>` : '';
          l.innerHTML  = (msg.pct < 0 ? `<span style="color:var(--red);">${msg.msg}</span>` : msg.msg) + detail + path;
        }
        if (b) {
          b.style.display = (msg.pct >= 0 && msg.pct < 100) ? 'block' : 'none';
          b.querySelector('.deepcw-bar-fill').style.width = Math.max(0, msg.pct) + '%';
        }
        if (msg.pct === 100) setTimeout(() => typeof deepcwAdminRefreshStatus==='function' && deepcwAdminRefreshStatus(), 800); }
      break;
    case 'deepcw_update':
      window.UI?.showToast('🧠 ' + msg.msg, 'warning');
      if (typeof deepcwAdminRefreshStatus==='function') deepcwAdminRefreshStatus();
      break;
    case 'tunnel_update':
      if (typeof window.Tunnel?.handleWS === 'function') Tunnel.handleWS(msg);
      break;
    default:
      // WSJTX i inne moduły
      if (typeof window.WSJTX?.handleWS  === 'function') WSJTX.handleWS(msg);
      if (typeof window.Chat?.handleWS   === 'function') Chat.handleWS(msg);
      if (typeof window._wsRotatorHandler === 'function') window._wsRotatorHandler(msg);
      break;
  }
}

// ── Publiczne API ─────────────────────────────────────────────────────────────

// ── Lokalne audio (gdy FFmpeg niedostępny na serwerze) ─────────────────────
// Używa getUserMedia → Web Audio API → odtwarza lokalną kartę dźwiękową
// Działa gdy przeglądarka i radio są na tym samym komputerze
async function initLocalAudio() {
  if (!navigator.mediaDevices?.getUserMedia) {
    console.warn('[audio] getUserMedia niedostępne');
    return false;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl:  false,
        sampleRate: 48000,
      }
    });
    if (!audioCtx) initAudioContext();
    const src    = audioCtx.createMediaStreamSource(stream);
    const gain   = audioCtx.createGain();
    gain.gain.value = 1.0;
    src.connect(gain);
    gain.connect(window._masterGain || audioCtx.destination);
    window._localAudioStream = stream;
    window._localAudioGain   = gain;
    console.log('[audio] Lokalne audio aktywne (getUserMedia)');
    return true;
  } catch(e) {
    console.warn('[audio] getUserMedia błąd:', e.message);
    return false;
  }
}

function stopLocalAudio() {
  if (window._localAudioStream) {
    window._localAudioStream.getTracks().forEach(t => t.stop());
    window._localAudioStream = null;
  }
}

function setLocalAudioGain(val) {
  if (window._localAudioGain) window._localAudioGain.gain.value = Math.max(0, Math.min(3, val));
}
// ── AudioControls — TX GAIN z VU metrem + RX VOL ─────────────────────────────
window.AudioControls = (function() {
  let _txGain    = 1.0;
  let _rxVol     = 0.7;
  let _analyser  = null;
  let _vuFrame   = null;
  let _txActive  = false;

  // ── TX GAIN ────────────────────────────────────────────────────────────────
  // Per-user zapis w localStorage (taki sam wzorzec jak RX VOL nizej) — bez
  // tego suwak wracal po kazdym odswiezeniu/przelogowaniu do sztywnej
  // wartosci 0.15 z HTML, wiec kazdy user musial reczne ustawiac modulacje
  // SSB od nowa za kazdym razem. Dodane 2026-08-15.
  function _txGainKey() {
    const uid = window.AppState?.my_uid || window.CurrentUser?.id || 'default';
    return `txGain_${uid}`;
  }

  function _loadTxGain() {
    try {
      const saved = localStorage.getItem(_txGainKey());
      return saved !== null ? parseFloat(saved) : 0.15;
    } catch(e) { return 0.15; }
  }

  function setTxGain(val) {
    _txGain = Math.max(0.01, Math.min(1.0, val));
    window._txGain = _txGain;
    // Podepnij do GainNode jesli istnieje (TX aktywne)
    if (window._txGainNode) window._txGainNode.gain.value = _txGain;
    // Aktualizuj UI
    const el = document.getElementById('tx-gain-val');
    if (el) el.textContent = _txGain.toFixed(2) + 'x';
    const sl = document.getElementById('tx-gain-slider');
    if (sl) sl.value = _txGain;
    _updateSliderColor(_txGain);
    try { localStorage.setItem(_txGainKey(), _txGain); } catch(e) {}
  }

  function initTxGain() {
    setTxGain(_loadTxGain());
  }

  // Kolor suwaka TX GAIN wg poziomu
  function _updateSliderColor(gain) {
    const sl = document.getElementById('tx-gain-slider');
    if (!sl) return;
    if (gain < 1.2)      sl.style.accentColor = 'var(--green)';
    else if (gain < 1.6) sl.style.accentColor = 'var(--amber)';
    else                 sl.style.accentColor = 'var(--red)';
  }

  // ── RX VOL ─────────────────────────────────────────────────────────────────
  function _rxVolKey() {
    const uid = window.AppState?.my_uid || window.CurrentUser?.id || 'default';
    return `rxVol_${uid}`;
  }

  function _loadRxVol() {
    try {
      const saved = localStorage.getItem(_rxVolKey());
      return saved !== null ? parseFloat(saved) : 70;
    } catch(e) { return 70; }
  }

  function setRxVol(pct) {
    _rxVol = Math.max(0, Math.min(100, pct)) / 100;
    if (window._masterGain) window._masterGain.gain.value = _rxVol * 0.9;
    const el = document.getElementById('rx-vol-val');
    if (el) el.textContent = pct + '%';
    const sl = document.getElementById('rx-vol-slider');
    if (sl) sl.value = pct;
    // Zapisz per user
    try { localStorage.setItem(_rxVolKey(), pct); } catch(e) {}
  }

  function initRxVol() {
    const saved = _loadRxVol();
    setRxVol(saved);
  }

  // ── VU meter — dynamiczny pomiar poziomu mikrofonu TX ───────────────────────
  function startVU(sourceNode) {
    if (!audioCtx) return;
    _txActive = true;
    _analyser = audioCtx.createAnalyser();
    _analyser.fftSize = 256;
    _analyser.smoothingTimeConstant = 0.6;
    try { sourceNode.connect(_analyser); } catch(e) {}

    const buf = new Uint8Array(_analyser.fftSize);
    function loop() {
      if (!_txActive) return;
      _analyser.getByteTimeDomainData(buf);
      // RMS amplitudy
      let sum = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buf.length);
      const pct = Math.min(100, rms * 300); // skalowanie wizualne

      // Kolor wg poziomu
      let color;
      if (pct < 50)      color = 'var(--green)';
      else if (pct < 80) color = 'var(--amber)';
      else               color = 'var(--red)';

      const fill = document.getElementById('tx-vu-fill');
      if (fill) {
        fill.style.width  = pct + '%';
        fill.style.background = color;
      }
      // Kolor suwaka TX GAIN tez reaguje na faktyczny poziom
      const sl = document.getElementById('tx-gain-slider');
      if (sl) sl.style.accentColor = color;

      _vuFrame = requestAnimationFrame(loop);
    }
    loop();
  }

  function stopVU() {
    _txActive = false;
    if (_vuFrame) { cancelAnimationFrame(_vuFrame); _vuFrame = null; }
    // Reset VU baru
    const fill = document.getElementById('tx-vu-fill');
    if (fill) { fill.style.width = '0%'; fill.style.background = 'var(--green)'; }
    // Reset koloru suwaka wg pozycji
    _updateSliderColor(_txGain);
    if (_analyser) { try { _analyser.disconnect(); } catch(e) {} _analyser = null; }
  }

  // Monitoring _txGainNode — gdy TX startuje, podepnij VU meter
  // Sprawdzamy co 200ms czy _txGainNode pojawil sie lub zniknal
  let _vuWasActive = false;
  setInterval(() => {
    const hasNode = !!window._txGainNode;
    if (hasNode && !_vuWasActive) {
      // TX wlaczone — startuj VU
      startVU(window._txGainNode);
      _vuWasActive = true;
    } else if (!hasNode && _vuWasActive) {
      // TX wylaczone — stopuj VU
      stopVU();
      _vuWasActive = false;
    }
  }, 200);

  // Publiczne API
  return { setTxGain, setRxVol, startVU, stopVU, initRxVol, initTxGain };
})();

// ── Audio WebSocket — bezposrednie polaczenie z Rust WSS (port 9443) ─────────
let _audioWs = null;

function _connectAudioWs() {
  if (_audioWs && _audioWs.readyState <= 1) return;

  // Rust WSS na porcie 9443 — bezposrednio, bez Python proxy
  const proto   = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const rustPort = location.protocol === 'https:' ? 9443 : 9401;
  const rustUrl  = `${proto}//${location.hostname}:${rustPort}`;

  _audioWs = new WebSocket(rustUrl);
  _audioWs.binaryType = 'arraybuffer';

  _audioWs.onopen = () => {
    console.log('[audio] Rust WSS połączony:', rustUrl);
    window._rustAudio = true;
  };

  _audioWs.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) {
      playOpusFrame(e.data);
    }
  };

  _audioWs.onclose = (e) => {
    console.log('[audio] Rust WSS rozłączony:', e.code, '— retry za 3s');
    window._rustAudio = false;
    _audioWs = null;
    if (window._audioEnabled) {
      setTimeout(_connectAudioWs, 3000);
    }
  };

  _audioWs.onerror = (e) => {
    console.warn('[audio] Rust WSS błąd:', e);
  };
}

// ── TX mikrofon (WebRTC: getUserMedia -> RTCPeerConnection -> serwer) ───────
// Wysyla audio z mikrofonu uzytkownika do serwera, ktory odtwarza je na
// karcie dzwiekowej radia. Serwer obsluguje sygnalizacje przez WebSocket
// (webrtc_offer/answer/ice).
const _txMic = (() => {
  let pc = null;              // RTCPeerConnection
  let stream = null;          // MediaStream z mikrofonu
  let active = false;
  let vuRaf = null;            // requestAnimationFrame id dla VU meter
  let audioAnalyzer = null;

  async function start() {
    if (active) return true;
    if (!navigator.mediaDevices?.getUserMedia) {
      window.UI?.showToast('Mikrofon niedostepny (brak getUserMedia w przegladarce)', 'error');
      return false;
    }
    if (typeof RTCPeerConnection === 'undefined') {
      window.UI?.showToast('WebRTC niedostepny w tej przegladarce', 'error');
      return false;
    }
    try {
      // Zapisany wybor z PROFIL ("MIKROFON TX", patrz profile_audio.js) -
      // KLUCZOWA POPRAWKA: tu bylo "ham_tx_micId", ktorego NIC w calym
      // kodzie nigdy nie zapisywalo (profile_audio.js zapisuje pod
      // "ham_audio_micId") - wybor mikrofonu w PROFIL nie mial ZADNEGO
      // wplywu na realnie nadawane audio, zawsze leciala ponizsza
      // heurystyka auto-wyboru. Krytyczne dla mostu do zewnetrznego
      // WSJT-X/JTDX przez wirtualny kabel audio (VB-Audio) - ta heurystyka
      // CELOWO omija urzadzenia z "virtual"/"cable" w nazwie, wiec kabel
      // wybrany recznie w PROFIL byl ignorowany, a leciala inna, prawdziwa
      // karta (albo cisza, jesli zadnej nie znalazla).
      let preferredMicId = localStorage.getItem('ham_audio_micId');
      // Auto-wybor mikrofonu (gdy user nic nie skonfigurowal w PROFIL):
      // unikaj wirtualnych kabli i wbudowanych array.
      if (!preferredMicId) {
        try {
          const devs = await navigator.mediaDevices.enumerateDevices();
          const inputs = devs.filter(d => d.kind === 'audioinput');
          const isBad = (label) => {
            const l = (label || '').toLowerCase();
            return l.includes('virtual') || l.includes('cable') ||
                   l.includes('line ') || l.includes('smart sound') ||
                   l.includes('array') || l.includes('intel');
          };
          const isGood = (label) => {
            const l = (label || '').toLowerCase();
            return l.includes('realtek') || l.includes('usb');
          };
          const realMic = inputs.find(d =>
            d.deviceId !== 'default' && d.deviceId !== 'communications' &&
            isGood(d.label) && !isBad(d.label));
          const anyRealMic = inputs.find(d =>
            d.deviceId !== 'default' && d.deviceId !== 'communications' &&
            !isBad(d.label));
          const chosen = realMic || anyRealMic;
          if (chosen) {
            preferredMicId = chosen.deviceId;
            console.log('[txmic] Auto-wybrany mikrofon:', chosen.label);
          }
        } catch(e) {}
      }
      const audioConstraint = {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl:  false,
        sampleRate: 48000,
      };
      if (preferredMicId) {
        audioConstraint.deviceId = { exact: preferredMicId };
      }
      stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraint });
    } catch (e) {
      console.warn('[txmic] getUserMedia blad:', e.message);
      window.UI?.showToast('Brak dostepu do mikrofonu: ' + e.message, 'error');
      return false;
    }

    pc = new RTCPeerConnection({
      iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
    });

    // Wyslij wszystkie ICE candidates do serwera
    pc.onicecandidate = (ev) => {
      if (ev.candidate) {
        window.WS?.send({ type: 'webrtc_ice', candidate: ev.candidate.toJSON() });
      }
    };

    pc.onconnectionstatechange = () => {
      console.log('[txmic] PC state:', pc.connectionState);
      if (pc.connectionState === 'failed' || pc.connectionState === 'closed') {
        stop();
      }
    };

    // Podepnij przez lancuch EQ z modulu TxEq (presety/custom per-user).
    // Filtry sa rejestrowane w TxEq zeby zmiany presetow/suwakow aktualizowaly
    // je na zywo bez restartu polaczenia.
    let processedStream = stream;
    try {
      if (!window._audioCtx && typeof initAudioContext === 'function') initAudioContext();
      const ctx = window._audioCtx || audioCtx;
      if (ctx && window.TxEq) {
        const src = ctx.createMediaStreamSource(stream);
        const gainNode = ctx.createGain();
        gainNode.gain.value = window._txGain ?? 0.15;
        window._txGainNode = gainNode;

        const chain = window.TxEq.buildFilterChain(ctx);
        window.TxEq.registerTxFilters(chain.filters);

        const dest = ctx.createMediaStreamDestination();
        src.connect(gainNode);
        gainNode.connect(chain.input);
        chain.output.connect(dest);
        processedStream = dest.stream;
      }
    } catch (e) {
      console.warn('[txmic] EQ chain nie podlaczony, wysylam surowy stream:', e.message);
    }

    // Dodaj processed track (z EQ + limiter) do peer connection
    processedStream.getAudioTracks().forEach(t => pc.addTrack(t, processedStream));

    // Wygeneruj offer i wyslij do serwera
    try {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      window.WS?.send({ type: 'webrtc_offer', sdp: offer.sdp, sdpType: offer.type });
    } catch (e) {
      console.warn('[txmic] offer blad:', e.message);
      cleanup();
      return false;
    }

    // VU meter (uzywa Web Audio API zeby pokazywac poziom mikrofonu lokalnie)
    try {
      if (!window._audioCtx && typeof initAudioContext === 'function') initAudioContext();
      const ctx = window._audioCtx || audioCtx;
      if (ctx) {
        const src = ctx.createMediaStreamSource(stream);
        audioAnalyzer = ctx.createAnalyser();
        audioAnalyzer.fftSize = 256;
        src.connect(audioAnalyzer);
        const data = new Uint8Array(audioAnalyzer.frequencyBinCount);
        const tick = () => {
          if (!active || !audioAnalyzer) return;
          audioAnalyzer.getByteTimeDomainData(data);
          let peak = 0;
          for (let i = 0; i < data.length; i++) {
            const v = Math.abs(data[i] - 128);
            if (v > peak) peak = v;
          }
          const pct = Math.min(100, (peak / 128) * 100 * 2);
          const fill = document.getElementById('tx-vu-fill');
          if (fill) {
            fill.style.width = pct + '%';
            fill.style.background = pct > 80 ? 'var(--red)' : pct > 50 ? 'var(--amber)' : 'var(--green)';
          }
          vuRaf = requestAnimationFrame(tick);
        };
        tick();
      }
    } catch (e) { /* VU meter to nice-to-have, nie blokuj */ }

    active = true;
    console.log('[txmic] aktywny (WebRTC do serwera)');
    return true;
  }

  function onAnswer(msg) {
    if (!pc) return;
    pc.setRemoteDescription({ type: msg.sdpType || 'answer', sdp: msg.sdp })
      .catch(e => console.warn('[txmic] setRemoteDescription blad:', e.message));
  }

  function onRemoteIce(msg) {
    if (!pc || !msg.candidate) return;
    pc.addIceCandidate(msg.candidate)
      .catch(e => console.warn('[txmic] addIceCandidate blad:', e.message));
  }

  function cleanup() {
    if (vuRaf) { cancelAnimationFrame(vuRaf); vuRaf = null; }
    audioAnalyzer = null;
    if (window.TxEq) window.TxEq.unregisterTxFilters();
    if (window._txGainNode) {
      try { window._txGainNode.disconnect(); } catch(e) {}
      window._txGainNode = null;
    }
    if (stream) {
      stream.getTracks().forEach(t => t.stop());
      stream = null;
    }
    if (pc) {
      try { pc.close(); } catch(e) {}
      pc = null;
    }
    const fill = document.getElementById('tx-vu-fill');
    if (fill) fill.style.width = '0%';
  }

  function stop() {
    if (!active) return;
    active = false;
    window.WS?.send({ type: 'webrtc_stop' });
    cleanup();
    console.log('[txmic] zatrzymany');
  }

  return {
    start, stop, onAnswer, onRemoteIce,
    isActive: () => active,
  };
})();

window.WS = {
  ping() {
    // Wyslij ping przez WS i zmierz RTT (round-trip time)
    const t0 = Date.now();
    const badge = document.getElementById('badge-latency');
    if (badge) { badge.textContent = '...'; badge.style.color = 'var(--dim)'; }
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      if (badge) { badge.textContent = 'OFFLINE'; badge.style.color = 'var(--red)'; }
      // Sprobuj reconnect
      connect();
      return;
    }
    // Tymczasowy handler na wiadomosc pong
    window._pingT0 = t0;
    this.send({ type: 'ping', t: t0 });
    // Fallback - jesli serwer nie odpowie w 3s
    clearTimeout(window._pingTimeout);
    window._pingTimeout = setTimeout(() => {
      if (badge) { badge.textContent = 'TIMEOUT'; badge.style.color = 'var(--red)'; }
    }, 3000);
  },
  send(data) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(typeof data === 'string' ? data : JSON.stringify(data));
    }
  },
  enableAudio(on) {
    window._audioEnabled = !!on;
    if (on) {
      initAudioContext();
      initOpusDecoder();
      // Sprawdz czy Rust audio jest dostepne (port 9401)
      _connectAudioWs();
    } else {
      if (window._audioWs && window._audioWs.readyState === WebSocket.OPEN) {
        window._audioWs.close();
        window._audioWs = null;
      }
      this.send({ type: 'audio_stop' });
      _nextAudioTime = 0; _aheadAvg = 0; _audioBadgeAt = 0;
      _updateAudioLatencyBadge();
    }
  },
  isConnected:   () => ws && ws.readyState === WebSocket.OPEN,
  initLocalAudio,
  stopLocalAudio,
  setLocalAudioGain,
  sendFreqFast:  (f) => _sendFreqDebounced(f),
  // TX mikrofon przez WebRTC (uzywany przez przycisk tx-mic-btn w UI)
  startTX:       () => _txMic.start(),
  stopTX:        () => _txMic.stop(),
  isTxActive:    () => _txMic.isActive(),
};

// Start
document.addEventListener('DOMContentLoaded', () => {
  connect();
});

})();