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
 *   2. TX microphone (2026-11-14, same model as desktop ws.js _txMic):
 *      the WebRTC connection is negotiated ONCE and kept warm, and the
 *      microphone is opened in the background while this phone holds the
 *      TRX (setWarm, driven by radio_lock_state in mobile.js). Without PTT
 *      the mic track is NOT attached to the connection - nothing is sent.
 *      PTT only attaches the ready track and tells the server to open the
 *      TX sound card (webrtc_tx_on - before this the phone never sent it
 *      after the 2026-10-18 hot-PTT server change, so the server never
 *      opened the TX audio device for a phone at all).
 *
 * Backend contract (see webapp.py / webrtc_rx_audio.py / webrtc_audio.py):
 *   RX and TX both go over WebRTC (aiortc), signaling over the MAIN app WS.
 *   RX: server creates the offer ({type:'webrtc_rx_offer'}), browser answers
 *       ({type:'webrtc_rx_answer'}) - opposite direction from TX, since the
 *       server has the media to send. ICE both ways via
 *       webrtc_rx_ice/webrtc_ice. Source: AudioStream._rx_loop's PCM
 *       capture (audio_stream.py), the same one that already fed DeepCW/
 *       the local waterfall - not a second capture.
 *   TX: browser/mic offers ({type:'webrtc_offer'}), server answers
 *       ({type:'webrtc_answer'}), sent here via window.WS.send (mobile.js's
 *       shim), errors via webrtc_error (mobile.js forwards those to
 *       MobileAudio.onAnswer/onRemoteIce/onWebrtcError).
 */
(function () {
'use strict';

// ── RX (listen) — over WebRTC ────────────────────────────────────────────
// Was direct-to-ham_audio.exe WS/Opus, A/B tested against WebRTC, then
// switched over for good (2026-08-24, live-confirmed clearly better with
// 1 listener; WS stalled everything behind one lost/delayed TCP segment on
// LTE - classic head-of-line blocking - while a lost UDP packet here is
// just a small glitch). Server has the media (creates the offer, opposite
// of TX mic below, where the browser/mic offers). Playback via a plain
// <audio> element - the browser's own built-in WebRTC jitter buffer/PLC
// handles it, no hand-tuned buffer logic needed here.
let audioEnabled = false;
let rxPc = null;
let rxAudioEl = null;

// Returns the new enabled state (false if it failed to start, e.g. no
// WebRTC support — caller should reflect that back into the UI).
function enableRx(on) {
  if (!on) {
    audioEnabled = false;
    closeRxWebRTC();
    return false;
  }
  audioEnabled = true;
  connectRxWebRTC();
  return true;
}

function connectRxWebRTC() {
  if (rxPc) return;
  if (typeof RTCPeerConnection === 'undefined') {
    window.Mobile?.showToast?.(I18n.t('m_no_webrtc'), 'error');
    return;
  }
  rxPc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
  rxPc.onicecandidate = (ev) => {
    if (ev.candidate) window.WS?.send({ type: 'webrtc_rx_ice', candidate: ev.candidate.toJSON() });
  };
  rxPc.ontrack = (ev) => {
    if (!rxAudioEl) rxAudioEl = new Audio();
    rxAudioEl.srcObject = ev.streams[0];
    rxAudioEl.autoplay = true;
    rxAudioEl.play().catch((e) => console.warn('[maudio] RX play() error:', e));
  };
  rxPc.onconnectionstatechange = () => {
    console.log(`[maudio] RX WebRTC connectionState=${rxPc?.connectionState}`);
    // No retry here for 'disconnected' - that state is often transient
    // (brief ICE hiccup) and can self-recover; only 'failed'/'closed' are
    // terminal. Auto-reconnect after a short delay if RX is still
    // supposed to be on - previously this just closed and gave up,
    // matching exactly what was reported live: audio died the moment
    // something (FT8 decode CPU load) stressed the connection, and only a
    // full page reload (a brand new RTCPeerConnection) brought it back.
    if (rxPc && (rxPc.connectionState === 'failed' || rxPc.connectionState === 'closed')) {
      closeRxWebRTC();
      if (audioEnabled) setTimeout(() => { if (audioEnabled) connectRxWebRTC(); }, 2000);
    }
  };
  window.WS?.send({ type: 'webrtc_rx_start' });
}

async function onRxOffer(msg) {
  if (!rxPc) return;
  try {
    await rxPc.setRemoteDescription({ type: msg.sdpType || 'offer', sdp: msg.sdp });
    const answer = await rxPc.createAnswer();
    await rxPc.setLocalDescription(answer);
    window.WS?.send({ type: 'webrtc_rx_answer', sdp: answer.sdp, sdpType: answer.type });
  } catch (e) {
    console.warn('[maudio] RX offer handling error:', e);
    closeRxWebRTC();
  }
}

function closeRxWebRTC() {
  if (rxAudioEl) { try { rxAudioEl.pause(); rxAudioEl.srcObject = null; } catch (e) {} rxAudioEl = null; }
  if (rxPc) {
    try { rxPc.close(); } catch (e) {}
    rxPc = null;
    window.WS?.send({ type: 'webrtc_rx_stop' });
  }
}

function onRxWebrtcError(msg) {
  window.Mobile?.showToast?.(I18n.t('m_mic_error_prefix') + (msg.error || ''), 'error');
  closeRxWebRTC();
}

// ── TX (microphone) — warm connection + mic in readiness, see file header ──
let pc = null;
let sender = null;
let micStream = null;
let micTrack = null;
let micOpenedFor = null;
let micActive = false;
let stopRequested = false;
let warm = false;
let _pcPromise = null, _openPromise = null, _startPromise = null;
// Diagnostic timing (each phase of a PTT press), see webrtc_audio.py.
let _txT0 = 0;

async function _ensurePc() {
  if (pc && pc.connectionState === 'connected') return true;
  if (_pcPromise) return _pcPromise;
  _pcPromise = _ensurePcInner();
  try { return await _pcPromise; } finally { _pcPromise = null; }
}

async function _ensurePcInner() {
  if (pc) { try { pc.close(); } catch (e) {} pc = null; sender = null; }
  if (typeof RTCPeerConnection === 'undefined') {
    window.Mobile?.showToast?.(I18n.t('m_no_webrtc'), 'error');
    return false;
  }
  const myPc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
  pc = myPc;
  myPc.onicecandidate = (ev) => {
    if (ev.candidate) window.WS?.send({ type: 'webrtc_ice', candidate: ev.candidate.toJSON() });
  };
  myPc.onconnectionstatechange = () => {
    console.log(`[maudio] connectionState=${myPc.connectionState}`);
    if ((myPc.connectionState === 'failed' || myPc.connectionState === 'closed') && pc === myPc) {
      if (micActive) { micActive = false; _detach(); }
      pc = null; sender = null;
    }
  };
  sender = myPc.addTransceiver('audio', { direction: 'sendonly' }).sender;
  try {
    const offer = await myPc.createOffer();
    await myPc.setLocalDescription(offer);
    window.WS?.send({ type: 'webrtc_offer', sdp: offer.sdp, sdpType: offer.type });
  } catch (e) {
    try { myPc.close(); } catch (e2) {}
    if (pc === myPc) { pc = null; sender = null; }
    return false;
  }
  await new Promise((resolve) => {
    if (myPc.connectionState === 'connected') { resolve(); return; }
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      myPc.removeEventListener('connectionstatechange', onState);
      clearTimeout(timer);
      resolve();
    };
    const onState = () => {
      if (['connected', 'failed', 'closed'].includes(myPc.connectionState)) finish();
    };
    myPc.addEventListener('connectionstatechange', onState);
    const timer = setTimeout(finish, 2500);
  });
  return pc === myPc && myPc.connectionState === 'connected';
}

function _micIsOpen() {
  return !!(micTrack && micTrack.readyState === 'live');
}

async function _ensureMic() {
  if (_micIsOpen() && micOpenedFor === (localStorage.getItem('ham_audio_micId') || '')) return true;
  if (_micIsOpen()) closeMic();
  if (_openPromise) return _openPromise;
  _openPromise = _openMic();
  try { return await _openPromise; } finally { _openPromise = null; }
}

async function _openMic() {
  try {
    const preferredMicId = localStorage.getItem('ham_audio_micId') || '';
    const constraint = { echoCancellation: false, noiseSuppression: false, autoGainControl: false, sampleRate: 48000 };
    // {ideal}, not {exact} - see ws.js's desktop _txMic for the same fix
    // (a saved mic id that's gone must fall back, not block PTT).
    if (preferredMicId) constraint.deviceId = { ideal: preferredMicId };
    micStream = await navigator.mediaDevices.getUserMedia({ audio: constraint });
    micOpenedFor = preferredMicId;
  } catch (e) {
    const _detail = e.constraint ? `${e.name}: ${e.constraint}` : (e.message || e.name || 'unknown error');
    window.Mobile?.showToast?.(I18n.t('profile_toast_mic_no_access') + _detail, 'error');
    return false;
  }
  micTrack = micStream.getAudioTracks()[0] || null;
  if (!micTrack) { closeMic(); return false; }
  micTrack.onended = () => { if (!micActive) closeMic(); };
  return true;
}

// Stop sending - nothing goes to the server; the mic itself stays open.
function _detach() {
  if (sender) { try { sender.replaceTrack(null); } catch (e) {} }
}

function closeMic() {
  _detach();
  if (micStream) { micStream.getTracks().forEach(t => t.stop()); micStream = null; }
  micTrack = null;
  micOpenedFor = null;
}

async function startMicTx() {
  if (micActive) return true;
  if (_startPromise) return _startPromise;
  _startPromise = _doStart();
  try { return await _startPromise; } finally { _startPromise = null; }
}

async function _doStart() {
  stopRequested = false;
  _txT0 = performance.now();
  if (!navigator.mediaDevices?.getUserMedia) {
    window.Mobile?.showToast?.(I18n.t('profile_toast_mic_unavailable'), 'error');
    return false;
  }
  if (!(await _ensurePc())) {
    window.Mobile?.showToast?.(I18n.t('m_mic_error_prefix') + 'WebRTC', 'error');
    return false;
  }
  if (stopRequested) { stopRequested = false; return false; }
  if (!(await _ensureMic())) return false;
  if (stopRequested) { stopRequested = false; if (!warm) closeMic(); return false; }
  // Server opens the TX sound card, then the ready track is attached.
  window.WS?.send({ type: 'webrtc_tx_on' });
  try {
    await sender.replaceTrack(micTrack);
  } catch (e) {
    window.WS?.send({ type: 'webrtc_tx_off' });
    _detach();
    if (!warm) closeMic();
    return false;
  }
  micActive = true;
  console.log(`[maudio] mic on air after ${(performance.now() - _txT0).toFixed(0)}ms`);
  if (stopRequested) { stopRequested = false; stopMicTx(); return false; }
  return true;
}

function stopMicTx() {
  if (!micActive) { stopRequested = true; return; }
  micActive = false;
  window.WS?.send({ type: 'webrtc_tx_off' });
  _detach();
  if (!warm) closeMic();
}

// From mobile.js on every radio_lock_state: mic ready in the background
// while this phone holds the TRX, closed when the TRX is given up.
function setWarm(on) {
  on = !!on;
  if (on === warm) return;
  warm = on;
  if (on) {
    if (!navigator.mediaDevices?.getUserMedia) return;
    _ensurePc().then(ok => { if (ok && warm) return _ensureMic(); })
      .catch(e => console.warn('[maudio] background mic open failed:', e && e.message));
  } else if (!micActive) {
    closeMic();
  }
}

function onAnswer(msg) {
  if (!pc) return;
  pc.setRemoteDescription({ type: msg.sdpType || 'answer', sdp: msg.sdp }).catch(() => {});
}

function onRemoteIce(msg) {
  if (!pc || !msg.candidate) return;
  pc.addIceCandidate(msg.candidate).catch(() => {});
}

function onWebrtcError(msg) {
  window.Mobile?.showToast?.(I18n.t('m_mic_error_prefix') + (msg.error || ''), 'error');
  stopMicTx();
}

window.MobileAudio = {
  enableRx, isRxEnabled: () => audioEnabled,
  startMicTx, stopMicTx, onAnswer, onRemoteIce, onWebrtcError, setWarm,
  isMicActive: () => micActive,
  onRxOffer, onRxWebrtcError,
};

})();
