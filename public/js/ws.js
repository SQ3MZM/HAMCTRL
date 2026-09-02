/**
 * ws.js — WebSocket client + Opus audio decoder (Web Audio API)
 *
 * Opus audio pipeline (RX — receiving from the radio):
 *   Server → Opus binary frames (WS) → OpusDecoderWorker (WASM) → PCM → AudioContext
 *
 * Opus audio pipeline (TX — transmitting via the radio):
 *   Microphone → MediaStream → ScriptProcessor/AudioWorklet → PCM → opusEncoder (JS WASM) → WS → server
 */

(function() {
'use strict';

const S = window.AppState;
let ws = null;
let reconnectTimer = null;

// DIAGNOSTICS: PerformanceObserver on 'longtask' - the browser itself
// reports EVERY main JS thread block >50ms, regardless of the cause. Used
// to settle the question "is a WS badge spike JS jank or a real network
// delay (LTE)" - if there's NO entry here at the moment of a WS spike,
// the JS thread was not slow and the cause is network/server-side, not frontend.
// Compare the timestamp with the "[ws] HIGH RTT" warnings (case 'pong' below).
if (window.PerformanceObserver) {
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        const t = new Date();
        console.warn(`[longtask] ${Math.round(e.duration)}ms at ${t.toLocaleTimeString('pl-PL', {hour12:false})}.${String(t.getMilliseconds()).padStart(3,'0')} (start offset ${Math.round(e.startTime)}ms from navigation)`);
      }
    }).observe({ entryTypes: ['longtask'] });
  } catch(e) { /* longtask API unavailable in this browser */ }
}

// ── Audio (Opus RX) ───────────────────────────────────────────────────────────
let audioCtx = null;
Object.defineProperty(window, 'audioCtx', { get: () => audioCtx, set: v => { audioCtx = v; } });
let audioQueue = [];
let isPlayingAudio = false;
let opusWorker = null;   // Web Worker with the opus-decoder WASM decoder
window._audioEnabled = false;

// Picks the RX output device (saved choice > first non-virtual real
// device > 'default') and applies it to the shared audioCtx. Called at
// context creation AND on every 'devicechange' event (see
// initAudioContext) so a headphone plug/unplug mid-session actually
// re-routes audio instead of leaving it stuck on whatever was the
// default at page-load time.
function pickOutputSinkId() {
  return navigator.mediaDevices.enumerateDevices().then(devs => {
    const outputs = devs.filter(d => d.kind === 'audiooutput');
    const saved = localStorage.getItem('ham_audio_sinkId');
    const savedStillExists = saved && outputs.some(d => d.deviceId === saved);
    const nonVB = outputs.find(d =>
      d.deviceId !== 'default' &&
      d.deviceId !== 'communications' &&
      !d.label.toLowerCase().includes('virtual') &&
      !d.label.toLowerCase().includes('cable')
    );
    return savedStillExists ? saved : ((nonVB && nonVB.deviceId) || 'default');
  });
}
window.pickOutputSinkId = pickOutputSinkId;

// Applies the shared headphones/speaker choice to an arbitrary
// AudioContext — used here for the main RX context, and externally
// (tx_eq.js's SSB monitor, cw_sidetone.js) so the MON button's local
// monitor follows the SAME device as normal RX instead of always
// landing on whatever the browser considers its own separate "default".
function applyOutputSinkId(ctx) {
  if (!ctx || !ctx.setSinkId) return Promise.resolve();
  return pickOutputSinkId().then(target =>
    ctx.setSinkId(target).then(() => target)
  ).catch(e => { console.warn('[audio] setSinkId error:', e); });
}
window.applyOutputSinkId = applyOutputSinkId;

function _applyOutputSinkId() {
  applyOutputSinkId(audioCtx).then(target => {
    if (target) console.log('[audio] sinkId set:', target);
  });
}

// Re-applies the output device to the RX context AND to the SSB/CW
// local-monitor contexts (if currently active) on the same event, so a
// headphone plug/unplug mid-session re-routes MON the same way it
// re-routes normal RX audio, not just the RX path alone.
function _onOutputDeviceChange() {
  _applyOutputSinkId();
  window.TxEq?.reapplyOutputSink?.();
  window.CwSidetone?.reapplyOutputSink?.();
}

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
    window._masterGain.gain.value = 0.7;  // fallback until the real per-user level lands below
    // _masterGain is created HERE, only once audio actually starts (user
    // gesture / autoplay policy) — that is, AFTER initRxVol() already
    // tried once to apply the saved level (app:ready, see below in this
    // file). At that point _masterGain didn't exist yet, so setRxVol()
    // only updated the slider in the UI while the real gain stayed at
    // the hardcoded 0.7 from the line above — the slider looked
    // remembered, but the volume always reverted to 70% anyway. Calling
    // it again now, once _masterGain already exists, applies the saved
    // per-user value to the actual sound.
    window.AudioControls?.initRxVol?.();

    // Mute RX during OUR OWN TX (FT8/FT4). With MONI enabled, the radio
    // puts its TX signal on the USB-out — in the browser you hear
    // squealing tones of your own transmission (fatiguing). Duck: gain=0
    // during TX, restore the previous level afterward. Called from
    // wsjtx.js (ft8_tx_status).
    window.setTxAudioDuck = function(on) {
      const g = window._masterGain;
      if (!g) return;
      if (window._duckUnmuteTimer) {
        clearTimeout(window._duckUnmuteTimer);
        window._duckUnmuteTimer = null;
      }
      if (on) {
        if (window._duckPrevGain == null) window._duckPrevGain = g.gain.value;
        g.gain.value = 0.0;
      } else if (window._duckPrevGain != null) {
        // FIX: un-muting used to happen the INSTANT PTT went off. With MONI
        // enabled the radio's own TX audio rides the RX stream, which sits
        // behind the RX jitter buffer (_audioTarget, ~180-400ms, below) —
        // so what's about to play right after PTT-off isn't "now", it's
        // whatever was captured _audioTarget ago, i.e. still the tail end
        // of what we just said. Restoring gain instantly surfaced that
        // queued tail as "hearing myself with a delay right after I
        // stopped talking" (reported live). Hold the mute for one more
        // buffer-length so that stale audio drains silently first.
        const delayMs = Math.round((_audioTarget || 0.18) * 1000);
        window._duckUnmuteTimer = setTimeout(() => {
          window._duckUnmuteTimer = null;
          if (window._duckPrevGain != null) {
            g.gain.value = window._duckPrevGain;
            window._duckPrevGain = null;
          }
        }, delayMs);
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

    // Set the output device (not VB-Cable)
    // We look for Realtek/Speakers as "default" — not communications/VB
    if (audioCtx.setSinkId) {
      _applyOutputSinkId();
      // FIX: reported live - RX audio (incl. the SSB MONI echo) kept
      // playing through the laptop's built-in speakers even with
      // headphones plugged in and PROFIL's output device left on
      // "Domyślne" (default). Root cause: an AudioContext's implicit
      // 'default' sink does NOT automatically follow later OS
      // default-output changes in Chromium - it stays bound to whatever
      // was the default at the moment the context was created (page
      // load), so plugging in headphones AFTER that point doesn't
      // re-route audio that's already flowing. Re-applying setSinkId
      // on every 'devicechange' event (headphones plugged/unplugged,
      // any output added/removed) forces it to pick up the CURRENT
      // default instead of a stale one from page-load time.
      if (navigator.mediaDevices?.addEventListener) {
        navigator.mediaDevices.addEventListener('devicechange', _onOutputDeviceChange);
      }
    }
  } catch(e) {
    console.warn('[audio] AudioContext error:', e);
  }
}

// Load the per-user RX volume and TX GAIN after login
window.addEventListener('app:ready', () => {
  window.AudioControls?.initRxVol?.();
  window.AudioControls?.initTxGain?.();
});

// REVERTED (2026-08-21) - this used to auto-start the TX microphone
// stream right after login so PTT alone would be enough for SSB. Live
// test: as soon as the mic stream opened (getUserMedia), RX audio went
// silent/near-silent for the WHOLE session, not just during PTT. Root
// cause is a Windows OS setting, not this app - Control Panel > Sound >
// Communications tab has "Reduce the volume of other sounds by 80%" /
// "Mute all other sounds" as options, and Windows applies that to ALL
// other system audio (including our RX playback) for as long as ANY app
// holds the microphone open - which an always-on mic does permanently,
// not just while actually transmitting. Back to manual: the "Nadawanie
// TX - mikrofon" button in RADIO must be clicked once before SSB, same
// as before this session. If the user sets that Windows option to "Do
// nothing", auto-start can safely come back - ask before re-adding it.

// (click, touch, key) — the browser's autoplay policy
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

// Browsers create the AudioContext in 'suspended' state if there's no
// prior user gesture (click/touch/key) — since RX audio now turns on
// automatically when the WS connects (no button), the context can get
// stuck in 'suspended' even though frames are decoding correctly. We
// resume it on the first interaction with the page.
let _audioResumeBound = false;
function _bindAudioResumeOnFirstInteraction() {
  if (_audioResumeBound) return;
  _audioResumeBound = true;
  const resume = () => {
    if (audioCtx && audioCtx.state === 'suspended') {
      audioCtx.resume().then(() => console.log('[audio] AudioContext resumed after interaction'));
    }
  };
  ['click', 'keydown', 'touchstart'].forEach(ev =>
    document.addEventListener(ev, resume, { once: true, passive: true }));
}

// Opus decoder via the Web Codecs AudioDecoder API (native, no WASM)
// Confirmed working: 960 samples/frame, 48kHz, 20ms frame
function initOpusDecoder() {
  if (!window.AudioDecoder) {
    console.warn('[audio] AudioDecoder (Web Codecs) unavailable in this browser');
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
      error: (e) => console.error('[audio] AudioDecoder error:', e),
    });
    dec.configure({ codec: 'opus', sampleRate: 48000, numberOfChannels: 1 });
    // Interface compatible with the previous _opusDecoder API
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
        this._ts += 20000; // 20ms in microseconds
      },
      ready: Promise.resolve(),
    };
    console.log('[audio] AudioDecoder (Web Codecs) ready');
  } catch(e) {
    console.error('[audio] AudioDecoder init error:', e);
    window._opusDecoder = null;
  }
}

// ── Audio scheduler — smooth playback with no gaps between frames ───────────
// Problem: src.start(audioCtx.currentTime) plays each frame "now", but
// "now" shifts between frames due to network/JS event loop delays, which
// causes micro-gaps (clicks) in the played sound.
// Solution: schedule each frame at a precisely defined time in the
// future, back to back with no gaps. _nextAudioTime acts as a "pointer"
// to where the previous frame ends.
let _nextAudioTime = 0;
let _aheadAvg = 0;
// A fixed 180ms target left ALMOST NO margin against the real jitter
// observed live by the user over an LTE tunnel (sq3mzmremote.duckdns.org)
// - a single 183ms RTT spike (no JS blocking, no reaction from the
// adaptive bitrate in ham_audio.exe - too small to overflow the 5.12s
// Rust buffer) was fully enough to drain this buffer and produce an
// audible glitch. Instead of hard-raising the target (which pays higher
// latency EVEN when the link happens to be good), the target is now
// ADAPTIVE: starts low (base), grows in steps after each real shortage
// (ahead<0 in _scheduleAudioBuffer), and slowly decays back to base once
// the link has been clean for a while — the same hysteresis pattern as
// the adaptive HI/LO bitrate in ham_audio.exe (see RECOVERY_CLEAN_SECS in main.rs).
let _audioTarget = 0.18;          // current target — starts at base
const _AUDIO_TARGET_BASE = 0.18;  // lower bound of adaptation (good link)
const _AUDIO_TARGET_CEIL = 0.30;  // upper bound of adaptation (200-300ms budget, RCForb)
const _AUDIO_TARGET_STEP = 0.03;  // growth step after a shortage (30ms)
const _AUDIO_TARGET_DECAY_STEP = 0.02; // step back down when calm
// Live-observed shortages repeated at EXACTLY ~60s intervals
// (13:26:10.289 -> 13:27:10.289, then 13:31:10.311 - too precise for
// random jitter, looks like a network infrastructure cycle, e.g. LTE
// RRC/DRX). With 20s of silence before decaying, the buffer would return
// to base BEFORE the next identical ~60s cycle hit, so it re-learned
// from scratch every time instead of remembering. Extended to 90s, so
// the learned value survives at least one full 60s cycle before we start
// decaying.
const _AUDIO_TARGET_CLEAN_MS = 90000;  // ms without a shortage before we start decaying
let _lastUnderrunAt = 0;
const _AUDIO_MIN = 0.05;
const _AUDIO_MAX = 0.40;     // hard ceiling (outside adaptation) — above it, TRIM the buffer
let _audioBadgeAt = 0;       // throttle DOM updates (frames arrive every 20ms)

// Slow return to a low buffer once the link calms down. Checked every
// 5s, regardless of whether audio is currently playing (harmless when disabled).
setInterval(() => {
  if (_audioTarget <= _AUDIO_TARGET_BASE) return;
  if (performance.now() - _lastUnderrunAt < _AUDIO_TARGET_CLEAN_MS) return;
  const before = _audioTarget;
  _audioTarget = Math.max(_AUDIO_TARGET_BASE, _audioTarget - _AUDIO_TARGET_DECAY_STEP);
  if (_audioTarget !== before) {
    console.log(`[audio] link calm for ${Math.round(_AUDIO_TARGET_CLEAN_MS/1000)}s — buffer target ${Math.round(before*1000)}ms -> ${Math.round(_audioTarget*1000)}ms`);
  }
}, 5000);

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
  // Adaptive target 180-300ms (_audioTarget), hard ceiling 400ms
  // (_AUDIO_MAX) — above it, the scheduler trims the buffer anyway, so
  // red = the buffer keeps hitting the ceiling regardless of the current adaptation target.
  badge.style.color = ms < 300 ? 'var(--green)' : ms < 400 ? 'var(--amber)' : 'var(--red)';
  badge.style.borderColor = ms < 300 ? 'var(--green2)' : ms < 400 ? 'rgba(240,180,41,0.4)' : 'rgba(217,119,106,0.4)';
}

function _scheduleAudioBuffer(audioBuffer) {
  if (!audioCtx) return;
  const now = audioCtx.currentTime;
  let ahead = _nextAudioTime - now;

  if (_aheadAvg === 0) _aheadAvg = ahead > 0 ? ahead : _audioTarget;
  _aheadAvg = _aheadAvg * 0.9 + ahead * 0.1;

  let rate = 1.0;
  if (ahead > 0) {
    const err = _aheadAvg - _audioTarget;
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

  // TWO-SIDED clamp — key to keeping the buffer from growing unbounded.
  // When it drops too low: top it up (against clicks).
  // When it grows too high: TRIM it (against latency creeping up to 700ms).
  if (ahead > 0 && ahead < _AUDIO_MIN) {
    _nextAudioTime = now + _audioTarget; ahead = _audioTarget; _aheadAvg = _audioTarget;
  } else if (ahead > _AUDIO_MAX) {
    // the buffer bloated (Python choked) — cut the excess, return to the target
    _nextAudioTime = now + _audioTarget; ahead = _audioTarget; _aheadAvg = _audioTarget;
  }
  if (ahead < 0) {
    // Real shortage — the buffer completely drained, this is an AUDIBLE
    // glitch. Proof the current target is too tight for the link's
    // current jitter — raise it (up to the adaptation ceiling), instead
    // of waiting for the next identical glitch. The interval above handles the decay back down.
    const grown = Math.min(_AUDIO_TARGET_CEIL, _audioTarget + _AUDIO_TARGET_STEP);
    if (grown > _audioTarget) {
      console.warn(`[audio] buffer shortage — target ${Math.round(_audioTarget*1000)}ms -> ${Math.round(grown*1000)}ms`);
      _audioTarget = grown;
    }
    _lastUnderrunAt = performance.now();
    _nextAudioTime = now + _audioTarget; ahead = _audioTarget; _aheadAvg = _audioTarget; rate = 1.0;
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
  // Header format:
  // Rust ham_audio: [0xA1][seq 4B LE][opus...] — skip 5 bytes
  // Python fallback: [0xA1][opus...]            — skip 1 byte
  const view = new Uint8Array(buffer);
  const skip = (view[0] === 0xA1 && window._rustAudio) ? 5 : 1;
  const opusData = view.slice(skip);
  window._opusDecoder.ready.then(() => {
    try { window._opusDecoder.decodeFrame(opusData); } catch(e) {}
  });
}


// Debounce helper — limits how many messages get sent
function _debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

// VFO change buffer — send at most once per 50ms (20Hz) instead of on every scroll tick
const _sendFreqDebounced = _debounce((freq) => {
  // Check the radio lock before sending the freq
  const lock  = window.AppState?.radio_lock;
  const myUid = String(window.AppState?.my_uid || window.CurrentUser?.id || '');
  const role  = window.CurrentUser?.role;
  if (role !== 'admin' && (!lock?.locked || String(lock.user_id) !== myUid)) {
    // Revert the display to the radio's actual freq
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
  // WebSocket on the same host as HTTP, endpoint /ws
  // Works locally and through the Replit proxy (wss://hostname/ws)
  const wsBase = `${proto}://${location.host}/ws`;
  const wsUrl  = token
    ? `${wsBase}?token=${encodeURIComponent(token)}`
    : wsBase;
  ws = new WebSocket(wsUrl);
  window._mainWS = ws;

  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {
    console.log('[ws] Connected');
    // Auto-ping every 30s for ongoing latency measurement
    clearInterval(window._autoPingInterval);
    window._autoPingInterval = setInterval(() => { if (window.WS) window.WS.ping(); }, 30000);
    setTimeout(() => { if (window.WS) window.WS.ping(); }, 500);
    S.connected = true;
    window.UI?.updateConnectionStatus(true);

    // Subscribe to channels per the CURRENTLY active tab (not just
    // 'control'). Without this, after a reconnect the client wouldn't get
    // scope_frame/ft8_waterfall until switching tabs.
    const activePage = document.querySelector('.tab-btn.active')?.getAttribute('onclick')?.match(/'(\w+)'/)?.[1] || 'radio';
    const channelsForPage = {
      radio:     ['control', 'scope'],
      wsjtx:     ['control', 'scope', 'ft8'],
      dxcluster: ['control', 'dxcluster'],
    };
    const channels = channelsForPage[activePage] || ['control'];
    ws.send(JSON.stringify({ type: 'subscribe', channels }));

    // RX audio is always enabled (the manual enable button was removed —
    // the server now auto-starts the physical audio stream on startup,
    // so the client subscribes to receive it right away).
    _bindAudioResumeOnFirstInteraction();
    window.WS?.enableAudio(true);
  };

  ws.onclose = () => {
    console.log('[ws] Disconnected');
    S.connected = false;
    window.UI?.updateConnectionStatus(false);
    reconnectTimer = setTimeout(connect, 3000);
  };

  ws.onerror = (e) => {
    console.warn('[ws] Error:', e);
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
      console.warn('[ws] Parse error:', err);
    }
  };
}

// Keeps the "#tx-mic-btn" button (PROFILE/RADIO) in sync with the actual
// TX microphone state, regardless of what triggered it - a manual click
// (onclick in index.html) or the remote WSJT-X bridge (wsjtx_tx_start/stop
// below). Same visual states as the previous onclick, so there's no
// mismatch.
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
        // CRITICAL: the backend (webapp.py) NEVER sends a "bands" key in
        // the init message — the band list arrives separately via
        // loadBandsConfig() (-> /api/config/bands), called independently
        // after 'app:ready'. 'init' (WS) and 'app:ready' (auth.js) are two
        // INDEPENDENT async events that can arrive in any order. The
        // previous code (`msg.bands || {}`) unconditionally overwrote
        // S.bands with an EMPTY object when 'init' arrived AFTER
        // loadBandsConfig() had already correctly set 14 bands — which
        // triggered the fallback in buildBandGrid() showing only '20m'.
        // Now: when msg.bands doesn't exist, we keep whatever is already
        // in S.bands (the same defensive pattern as freq/mode/etc. below, ?? instead of ||).
        bands:     msg.bands     ?? S.bands,
        modes:     msg.modes     || [],
        rigs:      msg.rigs      || [],
        callsign:  msg.callsign  || '',
        stationLocator:  msg.stationLocator  || S.stationLocator,
        operatorLocator: msg.operatorLocator || S.operatorLocator,
      });
      window.UI?.fullRefresh();
      // Highlight the VFO A/B and SPLIT buttons per the state from init —
      // a new user must see which VFO is active and whether the radio is
      // split from the moment they log in (previously everything stayed
      // dimmed until the first click).
      window.UI?.updateVFOBadges?.();
      window.RadioFunctions?.syncStates?.({vfo: S.vfo, split: S.split});
      window.Settings?.populateModels(S.models, S.rigs);
      if (msg.rotators && typeof window.RotW?.handleWS === 'function') RotW.handleWS(msg);
      // Radio power state from get_status - CRUCIAL when a new user logs
      // in: they must see whether the radio is ON/OFF matching reality
      // (otherwise the button shows ON when someone else turned the radio off).
      if (msg.rigPowerOn !== undefined) {
        if (window.AppState) window.AppState.rigPowerOn = !!msg.rigPowerOn;
        window.RadioFunctions?.handlePowerState?.(!!msg.rigPowerOn);
      }
      // Set the radio lock state from init
      if (window.AppState && msg.locked !== undefined) {
        window.AppState.radio_lock = {
          locked:   msg.locked,
          user_id:  msg.user_id,
          username: msg.username,
          callsign: msg.callsign,
        };
      }
      // Pass the operator state to OpPanel
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
      // Slider value change (the CIV poller detected a change on the
      // radio, or another admin moved the slider — WS broadcast to everyone)
      console.log(`[ws] level_value id=${msg.id} value=${msg.value}`);
      window.RadioFunctions?.handleLevelValue?.(msg);
      break;
    case 'rig_slider_ack':
      // ACK of a slider change from another user - update the slider for
      // whoever didn't change it (skip=ws on the server skips the author of the change).
      console.log(`[ws] rig_slider_ack id=${msg.id} value=${msg.value}`);
      window.RadioFunctions?.handleLevelValue?.(msg);
      break;
    case 'func_state':
      window.RadioFunctions?.handleFuncState?.(msg);
      break;
    case 'cw_sending':
    case 'cw_done':
    case 'cw_stopped':
    case 'cw_error':
      // FIX (reported live 2026-08-24: "bardzo dlugo czeka zanim mi
      // pozwoli makro znow nadac" - every CW send left the keyer looking
      // "busy" for ~20s). cw.js's own handleWS() (clears cwActive on
      // cw_done/cw_stopped/cw_error, sets it on cw_sending) was never
      // wired into this dispatcher on desktop at all - only mobile.js
      // forwarded these types to CW.handleWS. Without it, cwActive here
      // could only ever be cleared by cw.js's own 20s safety-timeout
      // fallback (added earlier the same day for a different failure mode),
      // never by the real, near-instant confirmation - so EVERY
      // successful send left the "tap again = stop" state armed until
      // that timeout finally expired, not until the transmission actually
      // finished a few seconds later as intended.
      window.CW?.handleWS?.(msg);
      break;
    case 'rig_features':
      window.RadioFunctions?.handleWsMessage?.(msg);
      break;
    case 'audio_ready':
      window._rustAudio = !!(msg.status && msg.status.rust);
      console.log('[audio] ready rust=' + window._rustAudio);
      // After ham_audio restarts (e.g. a card change), the Opus stream
      // starts over. The WebCodecs decoder must treat the new stream's
      // first frame as a 'key', not a 'delta' - otherwise it loses sync
      // and there's no sound even though the WS audio connection is up
      // and frames are arriving. Resetting _first forces a keyframe.
      if (window._opusDecoder) {
        window._opusDecoder._first = true;
        window._opusDecoder._ts = 0;
      }
      // Force an audio WS reconnect to the new Rust process (the old stream died)
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
      console.warn('[txmic] server:', msg.error);
      _txMic.stop();
      break;
    case 'webrtc_rx_offer':
      _onRxOffer(msg);
      break;
    case 'webrtc_rx_error':
      _onRxWebrtcError(msg);
      break;
    case 'wsjtx_tx_start':
      // An external WSJT-X/JTDX (via the wsjtx_local.py bridge + Hamlib
      // emulation) turned on PTT - start the microphone stream (here: a
      // virtual audio cable like VB-Audio, selected in PROFILE as "TX
      // MICROPHONE"), the same as with manual PTT. This used to be
      // completely unhandled - the radio got PTT but with no audio at all (silence on air).
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
      // DIAGNOSTICS: log every suspiciously high RTT with the exact
      // wall-clock time, so it can be compared with the ham_audio.exe
      // console log (LOW/HIGH bitrate markers) and the longtask observer
      // below - settles whether a spike is a JS thread block or a real network delay.
      if (rtt >= 150) {
        console.warn(`[ws] HIGH RTT ${rtt}ms at ${new Date().toLocaleTimeString('pl-PL', {hour12:false})}.${String(new Date().getMilliseconds()).padStart(3,'0')}`);
      }
      break;
    }
    case 'txmeter': window.UI?.updateTxMeter(msg); break;
    case 'freq':
      // Grace period: ignore an incoming freq from the server for ~1s
      // after our own, local change (sendFreq). Without this: during fast
      // scroll-tuning (many steps in a short time), the CI-V radio may
      // not have physically settled between consecutive steps yet, and
      // the server (civ.py transceive handler) can, in that window, catch
      // an "intermediate"/stale frequency reading from the radio and
      // broadcast it to ALL clients (including the one that was just
      // scrolling) — without this guard it overwrote S.freq with the old
      // value, which reverted the band-button highlight to the previous band.
      if (S._localFreqSetAt && (Date.now() - S._localFreqSetAt) < 1000) break;
      S.freq = msg.freq;
      window.UI?.updateFreqDisplay();
      window.UI?.updateVFOBadges?.();  // also update the band badge
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
      window.UI?.updateVFOBadges?.();  // also update the mode badge
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
      // Partial state update (e.g. the admin changed the station
      // locator). Without a page reload — the rotator azimuth recomputes on its own.
      if (msg.stationLocator !== undefined) {
        S.stationLocator = msg.stationLocator;
        if (window.AppState) window.AppState.stationLocator = msg.stationLocator;
      }
      break;
    case 'vfo':
    case 'vfo_sel':
      // Active VFO state from the server — highlight the A/B buttons for
      // ALL clients (not just the one who clicked).
      S.vfo = msg.vfo || 'VFOA';
      window.UI?.updateFreqB?.();
      window.UI?.updateVFOBadges?.();
      window.RadioFunctions?.syncStates?.({vfo: S.vfo});
      break;
    case 'config_update':
      // The admin changed the list of enabled bands in settings — refresh
      // S.bands and the button grid without reloading the page.
      window.UI?.loadBandsConfig?.();
      break;
    case 'online_update':
    case 'radio_lock_state':
      // Save the lock state to AppState — used by VFO and sendFreq
      if (window.AppState) {
        window.AppState.radio_lock = {
          locked:   msg.locked,
          user_id:  msg.user_id,
          username: msg.username,
          callsign: msg.callsign,
        };
      }
      if (typeof window.OpPanel?.handleWS === 'function') window.OpPanel.handleWS(msg);
      // Update the visual graying of the Radio panel (readonly mode)
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
      // Radio turned off - clear the waterfall and show the OFF state
      if (typeof window.Waterfall?.onPowerReset === 'function') Waterfall.onPowerReset();
      break;
    case 'rotator_update':
      if (typeof window.RotW?.handleWS === 'function') RotW.handleWS(msg);
      if (typeof window.Rotator?.handleWS   === 'function') Rotator.handleWS(msg);
      // This type has its own case (above), so it NEVER fell through to
      // 'default' below, where it would normally reach WSJTX.handleWS —
      // the live rotator position reading in the WSJT-X panel (ANTENNA
      // row) never got ongoing updates, only a one-time fetch from
      // init(). Reported live as: "the rotator position only refreshes
      // when you switch tabs" — in reality that was the only time it ever got a value at all.
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
    case 'cw_tx_start':
      window.CwSidetone?.handleWS(msg);
      break;
    default:
      // WSJTX and other modules
      if (typeof window.WSJTX?.handleWS  === 'function') WSJTX.handleWS(msg);
      if (typeof window.Chat?.handleWS   === 'function') Chat.handleWS(msg);
      if (typeof window._wsRotatorHandler === 'function') window._wsRotatorHandler(msg);
      break;
  }
}

// ── Public API ─────────────────────────────────────────────────────────────

// ── Local audio (when FFmpeg is unavailable on the server) ─────────────────
// Uses getUserMedia → Web Audio API → plays through the local sound card
// Works when the browser and radio are on the same computer
async function initLocalAudio() {
  if (!navigator.mediaDevices?.getUserMedia) {
    console.warn('[audio] getUserMedia unavailable');
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
    console.log('[audio] Local audio active (getUserMedia)');
    return true;
  } catch(e) {
    console.warn('[audio] getUserMedia error:', e.message);
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
// ── AudioControls — TX GAIN with a VU meter + RX VOL ─────────────────────────
window.AudioControls = (function() {
  let _txGain    = 1.0;
  let _rxVol     = 0.7;
  let _analyser  = null;
  let _vuFrame   = null;
  let _txActive  = false;

  // ── TX GAIN ────────────────────────────────────────────────────────────────
  // Per-user save in localStorage (same pattern as RX VOL below) — without
  // this, the slider reverted after every refresh/relogin to the
  // hardcoded 0.15 from the HTML, so every user had to manually re-set
  // their SSB modulation level every time.
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
    // Apply to the GainNode if it exists (TX active)
    if (window._txGainNode) window._txGainNode.gain.value = _txGain;
    // Update the UI
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

  // TX GAIN slider color by level
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
    // BELT AND SUSPENDERS (2026-08-24): reported live - masterGain.gain
    // confirmed set to 0 (via window._debugAudio()) while RX audio kept
    // playing at full volume regardless. The RX <audio> element (see
    // ws.js's ontrack handler) is tapped into masterGain via
    // createMediaElementSource, but per the Web Audio spec the element's
    // OWN .volume still applies as a separate stage even when redirected
    // - it was never being set, so it stayed at its default (1.0) no
    // matter what masterGain did. Setting it directly here guarantees
    // control regardless of the exact interaction between the two.
    if (window._rxAudioEl) window._rxAudioEl.volume = _rxVol;
    const el = document.getElementById('rx-vol-val');
    if (el) el.textContent = pct + '%';
    const sl = document.getElementById('rx-vol-slider');
    if (sl) sl.value = pct;
    // Save per user
    try { localStorage.setItem(_rxVolKey(), pct); } catch(e) {}
  }

  function initRxVol() {
    const saved = _loadRxVol();
    setRxVol(saved);
  }

  // ── VU meter — live measurement of the TX microphone level ──────────────────
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
      // Amplitude RMS
      let sum = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = (buf[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buf.length);
      const pct = Math.min(100, rms * 300); // visual scaling

      // Color by level
      let color;
      if (pct < 50)      color = 'var(--green)';
      else if (pct < 80) color = 'var(--amber)';
      else               color = 'var(--red)';

      const fill = document.getElementById('tx-vu-fill');
      if (fill) {
        fill.style.width  = pct + '%';
        fill.style.background = color;
      }
      // The TX GAIN slider color also reacts to the actual level
      const sl = document.getElementById('tx-gain-slider');
      if (sl) sl.style.accentColor = color;

      _vuFrame = requestAnimationFrame(loop);
    }
    loop();
  }

  function stopVU() {
    _txActive = false;
    if (_vuFrame) { cancelAnimationFrame(_vuFrame); _vuFrame = null; }
    // Reset the VU bar
    const fill = document.getElementById('tx-vu-fill');
    if (fill) { fill.style.width = '0%'; fill.style.background = 'var(--green)'; }
    // Reset the slider color by position
    _updateSliderColor(_txGain);
    if (_analyser) { try { _analyser.disconnect(); } catch(e) {} _analyser = null; }
  }

  // Monitor _txGainNode — when TX starts, hook up the VU meter
  // We check every 200ms whether _txGainNode appeared or disappeared
  let _vuWasActive = false;
  setInterval(() => {
    const hasNode = !!window._txGainNode;
    if (hasNode && !_vuWasActive) {
      // TX enabled — start the VU meter
      startVU(window._txGainNode);
      _vuWasActive = true;
    } else if (!hasNode && _vuWasActive) {
      // TX disabled — stop the VU meter
      stopVU();
      _vuWasActive = false;
    }
  }, 200);

  // Public API
  return { setTxGain, setRxVol, startVU, stopVU, initRxVol, initTxGain };
})();

// ── RX audio over WebRTC ─────────────────────────────────────────────────
// Was direct-to-ham_audio.exe WS/Opus, A/B tested against WebRTC, then
// switched over for good (2026-08-24, live-confirmed clearly better with
// 1 listener; WS stalled everything behind one lost/delayed TCP segment on
// LTE - classic head-of-line blocking - while a lost UDP packet here is
// just a small glitch). Server has the media (creates the offer) —
// opposite direction from _txMic below (browser/mic offers, server
// answers).
//
// Routed through the SAME Web Audio graph as everything else
// (_masterGain -> _audioCompressor -> destination), NOT a standalone
// <audio> element - a plain element would play, but bypasses _masterGain
// entirely, so the RX VOL slider (setRxVol, below) and setTxAudioDuck
// (mutes RX during our own TX so MONI doesn't feed back our own tone)
// would both silently do nothing. Reported live exactly this: "audio
// latency badge doesn't work, RX vol slider doesn't work" - both because
// this used to be a plain <audio> element with none of that wiring.
let _rxPc = null;
let _rxSourceNode = null;
let _rxAudioEl = null;
let _rxStatsTimer = null;

// DIAGNOSTIC (2026-08-24): audioCtx/_rxPc live inside this file's closure,
// not on window - reported live as "audioCtx is not defined"/"_rxPc is not
// defined" when trying to inspect them directly from the DevTools console.
// This exposes a read-only snapshot so state can actually be checked live
// without needing to touch closure internals.
window._debugAudio = function() {
  const snap = {
    audioCtxState: audioCtx ? audioCtx.state : '(no audioCtx yet)',
    rxConnectionState: _rxPc ? _rxPc.connectionState : '(no _rxPc)',
    rxIceState: _rxPc ? _rxPc.iceConnectionState : '(no _rxPc)',
    masterGainValue: window._masterGain ? window._masterGain.gain.value : '(no _masterGain)',
    rxAudioElVolume: window._rxAudioEl ? window._rxAudioEl.volume : '(no _rxAudioEl)',
    rxAudioElMuted: window._rxAudioEl ? window._rxAudioEl.muted : '(no _rxAudioEl)',
    rxAudioElPaused: window._rxAudioEl ? window._rxAudioEl.paused : '(no _rxAudioEl)',
    audioEnabled: !!window._audioEnabled,
  };
  console.log('[audio debug]', snap);
  return snap;
};

function _connectRxWebRTC() {
  if (_rxPc) return;
  if (typeof RTCPeerConnection === 'undefined') {
    window.UI?.showToast?.('WebRTC niedostepny w tej przegladarce', 'error');
    return;
  }
  initAudioContext();  // ensures audioCtx/_masterGain/_audioCompressor exist
  _rxPc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
  _rxPc.onicecandidate = (ev) => {
    if (ev.candidate) window.WS?.send({ type: 'webrtc_rx_ice', candidate: ev.candidate.toJSON() });
  };
  _rxPc.ontrack = (ev) => {
    if (!audioCtx) return;
    // Live-diagnosed 2026-08-24: chrome://webrtc-internals showed ~98% of
    // arriving RTP audio packets discarded (jitterBufferEmittedCount
    // stuck at 0) ONLY on this desktop code path - confirmed on the SAME
    // machine/browser/network by switching Chrome's device toolbar to a
    // mobile UA, which serves mobile.html/mobile_audio.js instead and
    // played fine immediately. That file decodes via a plain <audio>
    // element; this one fed the track straight into
    // audioCtx.createMediaStreamSource(stream) - Chrome's WebRTC jitter
    // buffer / playout timing is tuned around HTMLMediaElement playback,
    // and bypassing that via a raw MediaStreamAudioSourceNode is a known
    // source of exactly this kind of silent packet loss. Fix: let a real
    // <audio> element do the actual decode/playout (proven working by
    // mobile), then tap ITS output into the Web Audio graph via
    // createMediaElementSource so the RX VOL slider / duck-on-TX still
    // work. createMediaElementSource can only be bound to a given element
    // ONCE ever, so a fresh <audio> element is created on every ontrack
    // (each reconnect) rather than reusing one.
    try {
      if (_rxSourceNode) { try { _rxSourceNode.disconnect(); } catch (e) {} }
      if (_rxAudioEl) { try { _rxAudioEl.pause(); _rxAudioEl.srcObject = null; } catch (e) {} }
      const el = new Audio();
      el.autoplay = true;
      el.srcObject = ev.streams[0];
      el.play().catch((e) => console.warn('[audio] RX play() error:', e));
      _rxAudioEl = el;
      window._rxAudioEl = el;  // AudioControls.setRxVol (separate closure) needs this
      window.AudioControls?.setRxVol?.(Math.round((window._masterGain ? window._masterGain.gain.value / 0.9 : 0.7) * 100));
      _rxSourceNode = audioCtx.createMediaElementSource(el);
      _rxSourceNode.connect(window._masterGain || audioCtx.destination);
    } catch (e) { console.warn('[audio] RX WebRTC track routing error:', e); }
  };
  _rxPc.onconnectionstatechange = () => {
    console.log(`[audio] RX WebRTC connectionState=${_rxPc?.connectionState}`);
    // Auto-reconnect on a terminal failure, same as _connectAudioWs's
    // setTimeout retry for the WS path — previously this just closed and
    // gave up, so a transient stall (e.g. FT8 decode CPU load stressing
    // the connection) killed RX audio permanently until a full page
    // reload, since nothing was left trying to reconnect.
    if (_rxPc && (_rxPc.connectionState === 'failed' || _rxPc.connectionState === 'closed')) {
      _closeRxWebRTC();
      if (window._audioEnabled) setTimeout(() => { if (window._audioEnabled) _connectRxWebRTC(); }, 2000);
    }
  };
  window.WS?.send({ type: 'webrtc_rx_start' });
  _startRxStatsPoll();
}

async function _onRxOffer(msg) {
  if (!_rxPc) return;
  try {
    await _rxPc.setRemoteDescription({ type: msg.sdpType || 'offer', sdp: msg.sdp });
    const answer = await _rxPc.createAnswer();
    await _rxPc.setLocalDescription(answer);
    window.WS?.send({ type: 'webrtc_rx_answer', sdp: answer.sdp, sdpType: answer.type });
  } catch (e) {
    console.warn('[audio] RX WebRTC offer handling error:', e);
    _closeRxWebRTC();
  }
}

// Audio latency badge — was fed by the WS path's own jitter-buffer target
// (_scheduleAudioBuffer/_aheadAvg), which never runs for WebRTC (the
// browser's own internal jitter buffer handles that, invisibly). Poll
// getStats() instead for the REAL measured jitterBufferDelay and feed it
// into the same _aheadAvg/_updateAudioLatencyBadge the badge already knows
// how to render, rather than inventing a second display path.
function _startRxStatsPoll() {
  _stopRxStatsPoll();
  _rxStatsTimer = setInterval(async () => {
    if (!_rxPc) return;
    try {
      const stats = await _rxPc.getStats();
      stats.forEach((report) => {
        if (report.type === 'inbound-rtp' && report.kind === 'audio' && report.jitterBufferEmittedCount) {
          _aheadAvg = report.jitterBufferDelay / report.jitterBufferEmittedCount;
          _updateAudioLatencyBadge();
        }
      });
    } catch (e) { /* getStats can throw briefly during teardown - ignore */ }
  }, 1000);
}

function _stopRxStatsPoll() {
  if (_rxStatsTimer) { clearInterval(_rxStatsTimer); _rxStatsTimer = null; }
}

function _closeRxWebRTC() {
  _stopRxStatsPoll();
  _aheadAvg = 0;
  _updateAudioLatencyBadge();
  if (_rxSourceNode) { try { _rxSourceNode.disconnect(); } catch (e) {} _rxSourceNode = null; }
  if (_rxAudioEl) { try { _rxAudioEl.pause(); _rxAudioEl.srcObject = null; } catch (e) {} _rxAudioEl = null; window._rxAudioEl = null; }
  if (_rxPc) {
    try { _rxPc.close(); } catch (e) {}
    _rxPc = null;
    window.WS?.send({ type: 'webrtc_rx_stop' });
  }
}

function _onRxWebrtcError(msg) {
  console.warn('[audio] RX WebRTC server error:', msg.error);
  _closeRxWebRTC();
}

// ── Audio WebSocket — direct connection to the Rust WSS (port 9443) ─────────
let _audioWs = null;

function _connectAudioWs() {
  if (_audioWs && _audioWs.readyState <= 1) return;

  // Rust WSS on port 9443 — directly, no Python proxy
  const proto   = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const rustPort = location.protocol === 'https:' ? 9443 : 9401;
  const rustUrl  = `${proto}//${location.hostname}:${rustPort}`;

  _audioWs = new WebSocket(rustUrl);
  _audioWs.binaryType = 'arraybuffer';

  _audioWs.onopen = () => {
    console.log('[audio] Rust WSS connected:', rustUrl);
    window._rustAudio = true;
  };

  _audioWs.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) {
      playOpusFrame(e.data);
    }
  };

  _audioWs.onclose = (e) => {
    console.log('[audio] Rust WSS disconnected:', e.code, '— retry in 3s');
    window._rustAudio = false;
    _audioWs = null;
    if (window._audioEnabled) {
      setTimeout(_connectAudioWs, 3000);
    }
  };

  _audioWs.onerror = (e) => {
    console.warn('[audio] Rust WSS error:', e);
  };
}

// ── TX microphone (WebRTC: getUserMedia -> RTCPeerConnection -> server) ─────
// Sends audio from the user's microphone to the server, which plays it
// back on the radio's sound card. The server handles signaling over
// WebSocket (webrtc_offer/answer/ice).
const _txMic = (() => {
  let pc = null;              // RTCPeerConnection
  let stream = null;          // MediaStream z mikrofonu
  let active = false;
  let vuRaf = null;            // requestAnimationFrame id dla VU meter
  let audioAnalyzer = null;
  // FOUND live, 2026-08-21: PTT is now tied directly to start()/stop()
  // (ui.js's setPTT) - a quick PTT tap (press, release again fast, e.g.
  // testing) calls stop() WHILE start() is still awaiting getUserMedia/
  // WebRTC negotiation, i.e. before `active` ever became true. stop()'s
  // old guard ("if (!active) return") silently dropped that request -
  // the mic stream getUserMedia() had already opened was never released,
  // and once start() finished moments later and set active=true, nothing
  // was left watching for a stop it had already missed. The mic (and the
  // Windows "Communications" audio-ducking that comes with any open mic -
  // see ws.js's REVERTED auto-start note above) then stayed on for the
  // rest of the session. This flag lets stop() during an in-flight
  // start() register the request instead of dropping it.
  let stopRequested = false;

  async function start() {
    if (active) return true;
    stopRequested = false;
    if (!navigator.mediaDevices?.getUserMedia) {
      window.UI?.showToast('Mikrofon niedostepny (brak getUserMedia w przegladarce)', 'error');
      return false;
    }
    if (typeof RTCPeerConnection === 'undefined') {
      window.UI?.showToast('WebRTC niedostepny w tej przegladarce', 'error');
      return false;
    }
    try {
      // Saved selection from PROFILE ("TX MICROPHONE", see
      // profile_audio.js) - KEY FIX: this used to read "ham_tx_micId",
      // which NOTHING in the entire codebase ever wrote
      // (profile_audio.js saves under "ham_audio_micId") - the microphone
      // choice in PROFILE had NO effect at all on the actually
      // transmitted audio, the auto-select heuristic below always ran
      // instead. Critical for the bridge to an external WSJT-X/JTDX via a
      // virtual audio cable (VB-Audio) - this heuristic DELIBERATELY
      // skips devices with "virtual"/"cable" in the name, so a cable
      // manually chosen in PROFILE was ignored and some other, real card
      // got used instead (or silence, if none was found).
      let preferredMicId = localStorage.getItem('ham_audio_micId');
      // Auto-select a microphone (when the user hasn't configured
      // anything in PROFILE): avoid virtual cables and built-in arrays.
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
            console.log('[txmic] Auto-selected microphone:', chosen.label);
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
        // FIX (live-seen 2026-09-02): {exact: id} throws OverconstrainedError
        // - with an EMPTY .message, so both the console line and the toast
        // showed no actual info ("brak dostepu do mikrofonu: ") - and PTT
        // was blocked outright - whenever the saved mic id (PROFILE ->
        // ham_audio_micId) no longer matches a currently connected device
        // (USB re-enumeration, unplugged headset, different PC). {ideal: id}
        // asks for the same device when present but falls back to any
        // working mic instead of hard-failing when it isn't - PTT should
        // never go silent just because a once-saved mic is gone.
        audioConstraint.deviceId = { ideal: preferredMicId };
      }
      stream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraint });
    } catch (e) {
      // e.message is often EMPTY for OverconstrainedError - e.name/e.constraint
      // are where the actual reason lives, so log/show those too instead of
      // a blank "brak dostepu do mikrofonu: " with zero diagnostic value.
      const _detail = e.constraint ? `${e.name}: ${e.constraint}` : (e.message || e.name || 'nieznany blad');
      console.warn('[txmic] getUserMedia error:', e.name, e.constraint, e.message);
      window.UI?.showToast('Brak dostepu do mikrofonu: ' + _detail, 'error');
      return false;
    }

    pc = new RTCPeerConnection({
      iceServers: [{ urls: 'stun:stun.l.google.com:19302' }],
    });

    // Send every ICE candidate to the server
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

    // Route through the EQ chain from the TxEq module (per-user
    // presets/custom). The filters are registered in TxEq so that preset/
    // slider changes update them live without restarting the connection.
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
      console.warn('[txmic] EQ chain not connected, sending raw stream:', e.message);
    }

    // Add the processed track (with EQ + limiter) to the peer connection
    processedStream.getAudioTracks().forEach(t => pc.addTrack(t, processedStream));

    // Generate the offer and send it to the server
    try {
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      window.WS?.send({ type: 'webrtc_offer', sdp: offer.sdp, sdpType: offer.type });
    } catch (e) {
      console.warn('[txmic] offer error:', e.message);
      cleanup();
      return false;
    }

    // VU meter (uses the Web Audio API to show the microphone level locally)
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
    } catch (e) { /* the VU meter is nice-to-have, don't block on it */ }

    active = true;
    console.log('[txmic] active (WebRTC to the server)');
    if (stopRequested) {
      // A stop() came in while we were still connecting - honor it NOW
      // that there's actually a stream/pc to clean up, instead of it
      // having been silently dropped (see the stopRequested comment above).
      stopRequested = false;
      stop();
      return false;
    }
    return true;
  }

  function onAnswer(msg) {
    if (!pc) return;
    pc.setRemoteDescription({ type: msg.sdpType || 'answer', sdp: msg.sdp })
      .catch(e => console.warn('[txmic] setRemoteDescription error:', e.message));
  }

  function onRemoteIce(msg) {
    if (!pc || !msg.candidate) return;
    pc.addIceCandidate(msg.candidate)
      .catch(e => console.warn('[txmic] addIceCandidate error:', e.message));
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
    if (!active) {
      // Might still be mid-start() (getUserMedia/WebRTC negotiation in
      // flight, `stream`/`pc` possibly not even assigned yet) - can't
      // clean up yet, remember the request so start() can honor it the
      // moment it actually reaches a cleanable state.
      stopRequested = true;
      return;
    }
    active = false;
    window.WS?.send({ type: 'webrtc_stop' });
    cleanup();
    console.log('[txmic] stopped');
  }

  return {
    start, stop, onAnswer, onRemoteIce,
    isActive: () => active,
  };
})();

window.WS = {
  ping() {
    // Send a ping over WS and measure RTT (round-trip time)
    const t0 = Date.now();
    const badge = document.getElementById('badge-latency');
    if (badge) { badge.textContent = '...'; badge.style.color = 'var(--dim)'; }
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      if (badge) { badge.textContent = 'OFFLINE'; badge.style.color = 'var(--red)'; }
      // Try to reconnect
      connect();
      return;
    }
    // Temporary handler for the pong message
    window._pingT0 = t0;
    this.send({ type: 'ping', t: t0 });
    // Fallback - if the server doesn't respond within 3s
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
      _connectRxWebRTC();
    } else {
      _closeRxWebRTC();
      this.send({ type: 'audio_stop' });
      _updateAudioLatencyBadge();
    }
  },
  isConnected:   () => ws && ws.readyState === WebSocket.OPEN,
  initLocalAudio,
  stopLocalAudio,
  setLocalAudioGain,
  sendFreqFast:  (f) => _sendFreqDebounced(f),
  // TX microphone over WebRTC (used by the tx-mic-btn button in the UI)
  startTX:       () => _txMic.start(),
  stopTX:        () => _txMic.stop(),
  isTxActive:    () => _txMic.isActive(),
};

// Start
document.addEventListener('DOMContentLoaded', () => {
  connect();
});

})();