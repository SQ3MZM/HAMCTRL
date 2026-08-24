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
  onRxOffer, onRxWebrtcError,
};

})();
