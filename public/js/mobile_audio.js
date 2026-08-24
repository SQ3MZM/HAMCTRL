/**
 * mobile_audio.js — RX (listen) + TX (microphone) audio for HAMCTRL Mobile.
 *
 * Deliberately NOT lifted wholesale from ws.js — that file's audio code is
 * tightly coupled to its own always-on connection lifecycle (RX auto-starts
 * on every WS open, no toggle) and to desktop-only pieces (TxEq filter
 * chain, VU-meter DOM, output-sinkId picker) mobile doesn't need. This
 * ports just the parts that matter (same wire format, same jitter-buffer
 * tuning, same WebRTC signaling contract as the backend already expects),
 * with two deliberate differences from desktop:
 *
 *   1. RX must be started by an explicit tap (Mobile.toggleRxAudio()) —
 *      mobile browsers require a real user gesture before an AudioContext
 *      is allowed to play anything; desktop just "hopes" for an incidental
 *      first click/keypress since it has no RX toggle left in its UI.
 *
 *   2. The TX microphone is acquired FRESH on every PTT press and fully
 *      released (track.stop()) on every release — never kept alive between
 *      presses. Desktop tried keeping a mic stream open continuously once
 *      (see ws.js's "REVERTED (2026-08-21)" comment) and hit a live bug: on
 *      Windows, any app holding a mic open ducks/mutes ALL other system
 *      audio — including this app's OWN RX playback — for as long as the
 *      mic stays open, not just while actually transmitting. A microphone
 *      permission, once granted, re-acquires in tens of ms on every later
 *      getUserMedia call (no repeat prompt) — so there's little latency to
 *      gain by keeping the stream open, and doing so risks hitting the
 *      same class of bug on whatever OS a given phone runs.
 *
 * Backend contract (unchanged, see webapp.py):
 *   RX: separate WS straight to ham_audio.exe (wss://host:9443 / ws://host:9401),
 *       binary frames [0xA1][seq 4B LE][opus payload].
 *   TX: signaling over the MAIN app WS — {type:'webrtc_offer', sdp, sdpType}
 *       sent here via window.WS.send (mobile.js's shim), answered with
 *       webrtc_answer/webrtc_ice/webrtc_error (mobile.js forwards those to
 *       MobileAudio.onAnswer/onRemoteIce/onWebrtcError).
 */
(function () {
'use strict';

// ── RX (listen) ──────────────────────────────────────────────────────────
let audioCtx = null;
let audioWs = null;
let audioEnabled = false;
let opusDecoder = null;

// Jitter buffer — same tuning constants as ws.js::_scheduleAudioBuffer,
// already tuned against real LTE jitter (see memory
// audio_pipeline_deep_analysis_2026-08-16), not reinvented here.
let nextAudioTime = 0;
let aheadAvg = 0;
let audioTarget = 0.18;
const TARGET_BASE = 0.18;
const TARGET_CEIL = 0.30;
const TARGET_STEP = 0.03;
const TARGET_DECAY_STEP = 0.02;
const TARGET_CLEAN_MS = 90000;
let lastUnderrunAt = 0;
const AUDIO_MIN = 0.05;
const AUDIO_MAX = 0.40;

setInterval(() => {
  if (audioTarget <= TARGET_BASE) return;
  if (performance.now() - lastUnderrunAt < TARGET_CLEAN_MS) return;
  audioTarget = Math.max(TARGET_BASE, audioTarget - TARGET_DECAY_STEP);
}, 5000);

function initAudioContext() {
  if (audioCtx) { if (audioCtx.state === 'suspended') audioCtx.resume(); return; }
  try {
    audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 48000 });
    if (audioCtx.state === 'suspended') audioCtx.resume();
  } catch (e) { console.warn('[maudio] AudioContext error:', e); }
}

function initOpusDecoder() {
  if (!window.AudioDecoder) {
    opusDecoder = null;
    return false;
  }
  try {
    const dec = new AudioDecoder({
      output: (frame) => {
        if (!audioCtx || frame.numberOfFrames === 0) { frame.close(); return; }
        try {
          const buf = audioCtx.createBuffer(frame.numberOfChannels, frame.numberOfFrames, frame.sampleRate);
          for (let ch = 0; ch < frame.numberOfChannels; ch++) frame.copyTo(buf.getChannelData(ch), { planeIndex: ch });
          frame.close();
          scheduleAudioBuffer(buf);
        } catch (e) { frame.close(); }
      },
      error: (e) => console.error('[maudio] AudioDecoder error:', e),
    });
    dec.configure({ codec: 'opus', sampleRate: 48000, numberOfChannels: 1 });
    opusDecoder = { dec, ts: 0, first: true };
    return true;
  } catch (e) {
    console.error('[maudio] AudioDecoder init error:', e);
    opusDecoder = null;
    return false;
  }
}

function decodeFrame(opusData) {
  if (!opusDecoder) return;
  opusDecoder.dec.decode(new EncodedAudioChunk({
    type: opusDecoder.first ? 'key' : 'delta',
    timestamp: opusDecoder.ts,
    data: opusData,
  }));
  opusDecoder.first = false;
  opusDecoder.ts += 20000; // 20ms frames, in microseconds
}

function scheduleAudioBuffer(audioBuffer) {
  if (!audioCtx) return;
  const now = audioCtx.currentTime;
  let ahead = nextAudioTime - now;

  if (aheadAvg === 0) aheadAvg = ahead > 0 ? ahead : audioTarget;
  aheadAvg = aheadAvg * 0.9 + ahead * 0.1;

  let rate = 1.0;
  if (ahead > 0) {
    const err = aheadAvg - audioTarget;
    if (Math.abs(err) > 0.04) rate = 1.0 + Math.max(-0.003, Math.min(0.003, err * 0.02));
  }

  if (ahead > 0 && ahead < AUDIO_MIN) {
    nextAudioTime = now + audioTarget; ahead = audioTarget; aheadAvg = audioTarget;
  } else if (ahead > AUDIO_MAX) {
    nextAudioTime = now + audioTarget; ahead = audioTarget; aheadAvg = audioTarget;
  }
  if (ahead < 0) {
    audioTarget = Math.min(TARGET_CEIL, audioTarget + TARGET_STEP);
    lastUnderrunAt = performance.now();
    nextAudioTime = now + audioTarget; ahead = audioTarget; aheadAvg = audioTarget; rate = 1.0;
  }

  const src = audioCtx.createBufferSource();
  src.buffer = audioBuffer;
  if (rate !== 1.0) src.playbackRate.value = rate;
  src.connect(audioCtx.destination);
  src.start(nextAudioTime);
  nextAudioTime += audioBuffer.duration / rate;
}

function playOpusFrame(buffer) {
  if (!audioEnabled || !audioCtx || !opusDecoder) return;
  const view = new Uint8Array(buffer);
  // ham_audio.exe framing: [0xA1][seq 4B LE][opus...] — skip 5 bytes.
  const skip = view[0] === 0xA1 ? 5 : 1;
  try { decodeFrame(view.slice(skip)); } catch (e) {}
}

function connectAudioWs() {
  if (audioWs && audioWs.readyState <= 1) return;
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const port = location.protocol === 'https:' ? 9443 : 9401;
  audioWs = new WebSocket(`${proto}//${location.hostname}:${port}`);
  audioWs.binaryType = 'arraybuffer';
  audioWs.onmessage = (e) => { if (e.data instanceof ArrayBuffer) playOpusFrame(e.data); };
  audioWs.onclose = () => {
    audioWs = null;
    if (audioEnabled) setTimeout(connectAudioWs, 3000);
  };
  audioWs.onerror = () => {};
}

// Returns the new enabled state (false if it failed to start, e.g. no
// WebCodecs support — caller should reflect that back into the UI).
function enableRx(on) {
  if (!on) {
    audioEnabled = false;
    if (audioWs) { try { audioWs.close(); } catch (e) {} audioWs = null; }
    nextAudioTime = 0; aheadAvg = 0;
    return false;
  }
  initAudioContext();
  if (!initOpusDecoder()) {
    window.Mobile?.showToast?.(I18n.t('m_audio_no_webcodecs'), 'error');
    return false;
  }
  audioEnabled = true;
  connectAudioWs();
  return true;
}

// ── TX (microphone) — fresh getUserMedia per PTT press, see file header ──
let pc = null;
let micStream = null;
let micActive = false;
let stopRequested = false;
// Diagnostic timing (reported as "significant TX delay" live 2026-08-23,
// not yet root-caused end-to-end) — logs each phase so the next test
// pinpoints where the time actually goes (mic grant vs. negotiation vs.
// ICE) instead of guessing again. See webrtc_audio.py for the matching
// server-side timing log. Module-level so onAnswer() (called later, from
// the WS dispatcher) can log against the same start point.
let _txT0 = 0;

async function startMicTx() {
  if (micActive) return true;
  stopRequested = false;
  _txT0 = performance.now();
  if (!navigator.mediaDevices?.getUserMedia) {
    window.Mobile?.showToast?.(I18n.t('profile_toast_mic_unavailable'), 'error');
    return false;
  }
  if (typeof RTCPeerConnection === 'undefined') {
    window.Mobile?.showToast?.(I18n.t('m_no_webrtc'), 'error');
    return false;
  }
  try {
    const preferredMicId = localStorage.getItem('ham_audio_micId') || '';
    const constraint = { echoCancellation: false, noiseSuppression: false, autoGainControl: false, sampleRate: 48000 };
    if (preferredMicId) constraint.deviceId = { exact: preferredMicId };
    micStream = await navigator.mediaDevices.getUserMedia({ audio: constraint });
  } catch (e) {
    window.Mobile?.showToast?.(I18n.t('profile_toast_mic_no_access') + e.message, 'error');
    return false;
  }
  console.log(`[maudio] getUserMedia: ${(performance.now() - _txT0).toFixed(0)}ms`);

  pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
  pc.onicecandidate = (ev) => {
    if (ev.candidate) window.WS?.send({ type: 'webrtc_ice', candidate: ev.candidate.toJSON() });
  };
  pc.onconnectionstatechange = () => {
    console.log(`[maudio] connectionState=${pc.connectionState} at ${(performance.now() - _txT0).toFixed(0)}ms`);
    if (pc.connectionState === 'failed' || pc.connectionState === 'closed') stopMicTx();
  };
  micStream.getAudioTracks().forEach(t => pc.addTrack(t, micStream));

  try {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    window.WS?.send({ type: 'webrtc_offer', sdp: offer.sdp, sdpType: offer.type });
    console.log(`[maudio] offer sent: ${(performance.now() - _txT0).toFixed(0)}ms`);
  } catch (e) {
    cleanupMicTx();
    return false;
  }

  micActive = true;
  if (stopRequested) {
    // A stop() (PTT released) arrived while we were still negotiating —
    // honor it now instead of leaving the mic silently armed (same fix as
    // ws.js's _txMic — see file header for why this matters).
    stopRequested = false;
    stopMicTx();
    return false;
  }
  return true;
}

function onAnswer(msg) {
  if (!pc) return;
  console.log(`[maudio] answer received: ${(performance.now() - _txT0).toFixed(0)}ms`);
  pc.setRemoteDescription({ type: msg.sdpType || 'answer', sdp: msg.sdp }).catch(() => {});
}

function onRemoteIce(msg) {
  if (!pc || !msg.candidate) return;
  pc.addIceCandidate(msg.candidate).catch(() => {});
}

function cleanupMicTx() {
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  if (pc) { try { pc.close(); } catch (e) {} pc = null; }
}

function stopMicTx() {
  if (!micActive) { stopRequested = true; return; }
  micActive = false;
  window.WS?.send({ type: 'webrtc_stop' });
  cleanupMicTx();
}

function onWebrtcError(msg) {
  window.Mobile?.showToast?.(I18n.t('m_mic_error_prefix') + (msg.error || ''), 'error');
  stopMicTx();
}

window.MobileAudio = {
  enableRx, isRxEnabled: () => audioEnabled,
  startMicTx, stopMicTx, onAnswer, onRemoteIce, onWebrtcError,
  isMicActive: () => micActive,
};

})();
