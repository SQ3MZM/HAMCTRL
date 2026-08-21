/**
 * cw_sidetone.js — locally synthesized CW sidetone for the MON button.
 *
 * The radio does its own Morse keying internally (send_cw_message() in
 * civ.py just hands it the text over CI-V cmd 17) - the server never
 * knows the exact dit/dah timing, only the text and configured WPM. So
 * instead of trying to stream real audio from the radio (the MONI path
 * that turned out to be silenced by our own TX-time RX duck - see the
 * comment in radiofunctions.js), this generates a standard-timing Morse
 * tone LOCALLY in the browser the moment the server signals a CW send is
 * starting (WS 'cw_tx_start': {text, wpm}). Not sample-accurate against
 * the radio's own keyer, but audibly correct: same text, same rhythm
 * (standard PARIS timing), so the operator can actually hear/confirm
 * what's being sent without depending on the radio's MONI/audio path at all.
 */
window.CwSidetone = (function() {
  'use strict';

  // Only A-Z/0-9 + the punctuation civ.py's _CW_ALLOWED accepts. '^' is
  // NOT a real character - see handleWS/_schedule below.
  const MORSE = {
    'A': '.-',    'B': '-...',  'C': '-.-.',  'D': '-..',   'E': '.',
    'F': '..-.',  'G': '--.',   'H': '....',  'I': '..',    'J': '.---',
    'K': '-.-',   'L': '.-..',  'M': '--',    'N': '-.',    'O': '---',
    'P': '.--.',  'Q': '--.-',  'R': '.-.',   'S': '...',   'T': '-',
    'U': '..-',   'V': '...-',  'W': '.--',   'X': '-..-',  'Y': '-.--',
    'Z': '--..',
    '0': '-----', '1': '.----', '2': '..---', '3': '...--', '4': '....-',
    '5': '.....', '6': '-....', '7': '--...', '8': '---..', '9': '----.',
    '/': '-..-.', '?': '..--..', '.': '.-.-.-', '-': '-....-',
    ',': '--..--', ':': '---...', "'": '.----.', '(': '-.--.',
    ')': '-.--.-', '=': '-...-', '+': '.-.-.', '"': '.-..-.', '@': '.--.-.',
  };

  let _enabled = false;
  let _ctx = null;
  let _osc = null;
  let _gain = null;

  function setEnabled(on) {
    _enabled = !!on;
    if (_enabled) {
      // Create (and route) the context up front, on the click that
      // enables MON, so it's already pointed at the right output device
      // by the time a CW send actually schedules a tone.
      _ensureCtx();
      _applySink();
    } else {
      _stopTone();
    }
  }

  function isEnabled() { return _enabled; }

  // Follows the SAME headphones/speaker choice as normal RX audio and
  // the SSB monitor (tx_eq.js) - reported live as "CW tone doesn't come
  // out of the speaker" because this used to just take whatever the
  // browser's own unconfigured 'default' sink was, instead of the
  // device the rest of the app actually plays through.
  function _applySink() {
    if (_ctx && _ctx.setSinkId) window.applyOutputSinkId?.(_ctx);
  }

  // Called from ws.js on 'devicechange' (headphones plugged/unplugged).
  function reapplyOutputSink() { _applySink(); }

  function _ensureCtx() {
    if (_ctx) return _ctx;
    try {
      _ctx = new (window.AudioContext || window.webkitAudioContext)();
      _applySink();
    } catch (e) {
      console.warn('[cwsidetone] AudioContext error:', e);
      _ctx = null;
    }
    return _ctx;
  }

  function _stopTone() {
    if (_osc) {
      try { _osc.stop(); } catch(e) {}
      try { _osc.disconnect(); } catch(e) {}
      _osc = null;
    }
    if (_gain) {
      try { _gain.disconnect(); } catch(e) {}
      _gain = null;
    }
  }

  // Schedules a full Morse tone sequence for `text` at `wpm`, starting at
  // AudioContext time `startAt`. One persistent oscillator, gain ramped
  // on/off per element (avoids audible clicks from hard on/off gain steps).
  function _schedule(text, wpm) {
    const ctx = _ensureCtx();
    if (!ctx) return;
    if (ctx.state === 'suspended') ctx.resume().catch(() => {});
    _stopTone();

    const unit = 1.2 / Math.max(5, Math.min(60, wpm || 18));  // seconds/dit (PARIS standard)
    const freq = 600;  // standard CW sidetone pitch
    const RAMP = 0.003; // 3ms on/off ramp - avoids clicks, short enough to not blur dits

    _osc = ctx.createOscillator();
    _gain = ctx.createGain();
    _osc.frequency.value = freq;
    _osc.type = 'sine';
    _gain.gain.setValueAtTime(0, ctx.currentTime);
    _osc.connect(_gain).connect(ctx.destination);
    _osc.start();

    let t = ctx.currentTime + 0.05;  // small lead-in
    let suppressNextGap = false;  // '^' joins the previous and next char (no inter-char gap)

    const chars = text.toUpperCase().split('');
    for (let i = 0; i < chars.length; i++) {
      const c = chars[i];
      if (c === '^') { suppressNextGap = true; continue; }
      if (c === ' ') { t += unit * 7; continue; }
      const pattern = MORSE[c];
      if (!pattern) continue;  // unknown char - civ.py already filtered, but be defensive
      for (let j = 0; j < pattern.length; j++) {
        const dur = (pattern[j] === '-') ? unit * 3 : unit;
        _gain.gain.setTargetAtTime(1, t, RAMP);
        _gain.gain.setTargetAtTime(0, t + dur, RAMP);
        t += dur + unit;  // + 1 unit gap between elements of the same char
      }
      t -= unit;  // remove the trailing element-gap, replaced by the char-gap below
      t += suppressNextGap ? 0 : unit * 3;  // inter-character gap (3 units), unless '^' joined
      suppressNextGap = false;
    }

    // Stop the oscillator a moment after the last tone ends - keeping it
    // running indefinitely would leak an AudioContext node per CW send.
    const stopAt = t;
    const thisOsc = _osc;
    setTimeout(() => {
      if (_osc === thisOsc) _stopTone();
    }, Math.max(0, (stopAt - ctx.currentTime) * 1000) + 200);
  }

  function handleWS(msg) {
    if (msg.type !== 'cw_tx_start') return;
    if (!_enabled) return;
    _schedule(msg.text || '', msg.wpm || 18);
  }

  return { setEnabled, isEnabled, handleWS, reapplyOutputSink };
})();
