/**
 * vfo.js — advanced VFO panel
 *
 * Features:
 *   1. Frequency digits — each individually scrollable/keyboard-controllable
 *   2. Virtual SVG knob — drag/scroll → frequency change
 *   3. Mouse-wheel scroll over the whole VFO
 *   4. Arrow keys
 */
(function () {
'use strict';

const S = window.AppState;

// ── Check whether the user can control the radio ─────────────────────────────
function _canControl() {
  const lock  = window.AppState?.radio_lock;
  const myUid = String(window.AppState?.my_uid || window.CurrentUser?.id || '');
  const role  = window.CurrentUser?.role;
  if (role === 'admin') return true;
  // The user must have claimed the radio — regardless of whether it's free or held
  if (!lock?.locked) return false;
  return String(lock.user_id) === myUid;
}
function _blockToast() {
  const holder = window.AppState?.radio_lock?.callsign ||
                 window.AppState?.radio_lock?.username || '?';
  window.UI?.showToast(`⛔ Radio zajęte przez ${holder} — przejmij TRX`, 'error');
}

// ── Format frequency → array of 9 digits [MHz.kHz.Hz] ────────────────────────
//   e.g. 14205000 → ["1","4",".","2","0","5",".","0","0","0"]
function freqToDigits(hz) {
  // Format: XXX.XXX.XXX Hz (9 digits + 2 dots)
  const s = String(hz).padStart(9, '0');
  return [
    s[0], s[1], s[2], // millions (MHz)
    '.',
    s[3], s[4], s[5], // thousands (kHz)
    '.',
    s[6], s[7], s[8], // units (Hz)
  ];
}

// ── Digit value at position i (0-8, dots excluded) ────────────────────────────
// position 0=10MHz, 1=1MHz, 2=100kHz, 3=10kHz, 4=1kHz, 5=100Hz, 6=10Hz, 7=1Hz
const DIGIT_VALUES = [10000000, 1000000, 100000, 10000, 1000, 100, 10, 1, 1]; // last 1 = Hz (idx 8)
// mapping from the digits array's index (with dots) → position in DIGIT_VALUES
const DIGIT_MAP = { 0:0, 1:1, 2:2,  4:3, 5:4, 6:5,  8:6, 9:7, 10:8 };

function getDigitStep(displayIdx) {
  // displayIdx: index in the character string (0-10, with dots at positions 3,7)
  const pos = DIGIT_MAP[displayIdx];
  if (pos === undefined) return 0;
  return DIGIT_VALUES[pos] || 0;
}

// ── Render the digital VFO panel ──────────────────────────────────────────────
function renderVFO() {
  const el = document.getElementById('vfo-digits');
  if (!el) return;

  const digits = freqToDigits(S.freq);
  el.innerHTML = digits.map((ch, i) => {
    if (ch === '.') {
      // The MHz separator is thicker (after position 2)
      const cls = i === 3 ? 'vfo-sep sep-mhz' : 'vfo-sep';
      return `<span class="${cls}">.</span>`;
    }
    const step = getDigitStep(i);
    if (!step) return `<span class="vfo-digit" data-idx="${i}" data-step="0">${ch}</span>`;
    // Up/down arrow triangles above/below each digit (2026-09-04,
    // requested as an alternative to mouse-wheel scrolling over a
    // digit — a click affordance, useful on touchscreens too). Same
    // effect as scrolling: bumps just this digit's position by one
    // step. The digit span itself keeps its exact previous class/
    // attributes (data-idx, data-step, click->selectDigit, wheel
    // handling in _attachDigitListeners) — only wrapped in a column
    // container with the arrows, so nothing about the existing
    // wheel/keyboard/select logic needs to change.
    return `<span class="vfo-digit-col">
      <span class="vfo-arrow vfo-arrow-up" onclick="VFO.bumpDigit(event,${step},1)" title="+${fmtStep(step)}">▲</span>
      <span class="vfo-digit active" data-idx="${i}" data-step="${step}"
        tabindex="0" title="+/- ${fmtStep(step)}"
        onkeydown="VFO.keyDigit(event,${step})"
        onclick="VFO.selectDigit(${step})"
        >${ch}</span>
      <span class="vfo-arrow vfo-arrow-down" onclick="VFO.bumpDigit(event,${step},-1)" title="-${fmtStep(step)}">▼</span>
    </span>`;
  }).join('');

  // Highlight the currently selected step
  if (window._vfoStep) {
    el.querySelectorAll('.vfo-digit.active').forEach(d => {
      const s2 = parseInt(d.dataset.step);
      d.classList.toggle('selected', s2 === window._vfoStep);
    });
  }

  // Attach the container listener (if not already attached)
  _attachDigitListeners();
}

function fmtStep(step) {
  if (step >= 1000000) return (step/1000000) + ' MHz';
  if (step >= 1000)    return (step/1000) + ' kHz';
  return step + ' Hz';
}

function updateVFODisplay() {
  const el = document.getElementById('vfo-digits');
  if (!el) { renderVFO(); return; }
  const digits = freqToDigits(S.freq);
  el.querySelectorAll('.vfo-digit').forEach((span, _) => {
    const idx = parseInt(span.dataset.idx);
    if (span.classList.contains('active')) {
      span.textContent = digits[idx];
    }
  });
}

// ── Wheel on a digit ───────────────────────────────────────────────────────────
// NOTE: an inline onwheel="VFO.wheelDigit(event,step)" on .vfo-digit elements
// DOES NOT WORK when #app-scale as a whole has transform:scale() — wheel
// events are dispatched in physical screen pixels, but the DOM elements
// are in logical pixels before the transform. At scale!=1 (e.g. a 1920px
// screen -> scale=1.33) the wheel event "misses" the elements even though
// the cursor is physically over them. Confirmed:
// window.addEventListener('wheel',...,true) (capture phase) always works —
// the event reaches document/window before it's dispatched to a specific
// DOM element. Solution: ONE global listener in the capture phase on
// document, which uses elementFromPoint() (correctly accounts for the
// transform) to identify the digit under the cursor.
// Shared by wheelDigit (mouse-wheel over a digit) and bumpDigit (click on
// its up/down arrow) — both just bump ONE digit position by +/-1 step.
function _applyDigitStep(step, dir) {
  if (!_canControl()) { _blockToast(); return; }
  const nf = Math.max(100000, S.freq + dir * step);
  S.freq = nf;
  S._localFreqSetAt = Date.now();
  updateVFODisplay();
  window.UI?.updateBandButtons();
  window.UI?.updateVFOBadges?.();  // update band/mode badge immediately
  if (typeof window.WS?.sendFreqFast === 'function') {
    window.WS.sendFreqFast(nf);
  } else if (typeof window.WS?.send === 'function') {
    window.WS.send({ type: 'freq', freq: nf });
  } else {
    window.UI?.sendFreq(nf);
  }
}

function wheelDigit(e, step) {
  e.preventDefault();
  e.stopPropagation();
  const dir = e.deltaY < 0 ? 1 : -1;
  _applyDigitStep(step, dir);
}

// Click on a digit's up/down arrow triangle — same effect as scrolling
// on that digit, dir is +1 (up arrow) or -1 (down arrow) directly.
function bumpDigit(e, step, dir) {
  e.preventDefault();
  e.stopPropagation();
  _applyDigitStep(step, dir);
}

// ── One listener on the #vfo-digits container ─────────────────────────────
// Instead of attaching listeners to every digit individually (which caused
// an off-by-one due to DOM/forEach ordering), we use ONE listener on the
// container. e.target always points at the element under the cursor —
// we identify the digit reliably and read its data-step.
let _digitListenerAttached = false;
function _attachDigitListeners() {
  if (_digitListenerAttached) return;
  const container = document.getElementById('vfo-digits');
  if (!container) return;
  container.addEventListener('wheel', function(e) {
    // We don't use e.target — it's wrong because of transform:scale (off by 1 digit).
    // getBoundingClientRect() always returns the correct screen
    // coordinates accounting for the transform, so we iterate over the
    // digits and check which one actually contains the cursor point.
    let digit = null;
    const x = e.clientX, y = e.clientY;
    // Iterate from RIGHT to left — negative letter-spacing can make digits
    // overlap, and forEach (left->right) would hit the wrong digit.
    // Reversed order: when x matches several digits, take the closest one
    // to the cursor (check the distance from the digit's center).
    const digits2 = Array.from(container.querySelectorAll('.vfo-digit.active'));
    let bestIdx = -1;
    let bestDist = Infinity;
    digits2.forEach(function(el, i) {
      const r = el.getBoundingClientRect();
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
        const cx = (r.left + r.right) / 2;
        const dist = Math.abs(x - cx);
        if (dist < bestDist) { bestDist = dist; bestIdx = i; }
      }
    });
    // Offset -1: the digit always shifts 1 to the right because of
    // transform:scale, so we take the digit 1 earlier in the DOM
    if (bestIdx > 0) digit = digits2[bestIdx - 1];
    else if (bestIdx === 0) digit = digits2[0];
    if (!digit) return;
    const s = parseInt(digit.dataset.step);
    if (!s) return;
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    wheelDigit(e, s);
  }, { capture: true, passive: false });
  _digitListenerAttached = true;
}

// ── Keyboard on a digit ────────────────────────────────────────────────────────
function keyDigit(e, step) {
  if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
    e.preventDefault();
    if (!_canControl()) { _blockToast(); return; }
    if (e.key === 'ArrowUp')   UI.sendFreq(Math.max(100000, S.freq + step));
    if (e.key === 'ArrowDown') UI.sendFreq(Math.max(100000, S.freq - step));
  }
}

// ── Click a digit → select it (highlight and respond to scroll) ─────────────
let selectedStep = 1000;
function selectDigit(step) {
  if (!_canControl()) { _blockToast(); return; }
  selectedStep = step;
  document.querySelectorAll('.vfo-digit.active').forEach(el => el.classList.remove('selected'));
  document.querySelectorAll(`.vfo-digit[data-step="${step}"]`).forEach(el => el.classList.add('selected'));
}

// ── SVG knob ────────────────────────────────────────────────────────────────
let knobAngle    = 0;
let knobDragging = false;
let knobStartY   = 0;
let knobStartAngle = 0;
const KNOB_SENSITIVITY = 1.2; // degrees/pixel

function initKnob() {
  const knob = document.getElementById('vfo-knob');
  if (!knob) return;

  knob.addEventListener('mousedown', e => {
    if (!_canControl()) { _blockToast(); return; }
    knobDragging  = true;
    knobStartY    = e.clientY;
    knobStartAngle = knobAngle;
    e.preventDefault();
    document.body.style.userSelect = 'none';
  });

  document.addEventListener('mousemove', e => {
    if (!knobDragging) return;
    const delta = (knobStartY - e.clientY) * KNOB_SENSITIVITY;
    knobAngle   = knobStartAngle + delta;
    rotateKnob(knobAngle);

    const stepDelta = Math.round((e.clientY - knobStartY) * -1);
    if (Math.abs(stepDelta) >= 2) {
      knobStartY = e.clientY;
      const newFreq = Math.max(100000, S.freq + Math.sign(stepDelta) * selectedStep);
      UI.sendFreq(newFreq);
    }
  });

  document.addEventListener('mouseup', () => {
    knobDragging = false;
    document.body.style.userSelect = '';
  });

  // Touch (mobile)
  knob.addEventListener('touchstart', e => {
    if (!_canControl()) { _blockToast(); return; }
    knobDragging = true;
    knobStartY   = e.touches[0].clientY;
    knobStartAngle = knobAngle;
  }, { passive: true });

  document.addEventListener('touchmove', e => {
    if (!knobDragging) return;
    const dy = knobStartY - e.touches[0].clientY;
    knobAngle = knobStartAngle + dy * KNOB_SENSITIVITY;
    rotateKnob(knobAngle);
    const stepDelta = Math.round(dy);
    if (Math.abs(stepDelta) >= 2) {
      knobStartY = e.touches[0].clientY;
      UI.sendFreq(Math.max(100000, S.freq + Math.sign(stepDelta) * selectedStep));
    }
  }, { passive: true });

  document.addEventListener('touchend', () => { knobDragging = false; });

  // Scroll
  knob.addEventListener('wheel', e => {
    e.preventDefault();
    if (!_canControl()) { _blockToast(); return; }
    const dir = e.deltaY < 0 ? 1 : -1;
    knobAngle += dir * 15;
    rotateKnob(knobAngle);
    UI.sendFreq(Math.max(100000, S.freq + dir * selectedStep));
  }, { passive: false });
}

function rotateKnob(angle) {
  const needle = document.getElementById('knob-needle');
  if (needle) needle.setAttribute('transform', `rotate(${angle % 360}, 50, 50)`);
}

// ── Render the SVG knob ────────────────────────────────────────────────────────
function renderKnobSVG() {
  const el = document.getElementById('vfo-knob-wrap');
  if (!el) return;
  el.innerHTML = `
  <svg id="vfo-knob" viewBox="0 0 100 100" width="90" height="90"
    style="cursor:ns-resize;display:block;margin:0 auto;"
    title="Przeciągnij lub kręć kółkiem myszy">
    <!-- Outer ring -->
    <circle cx="50" cy="50" r="47" fill="#090c09" stroke="rgba(76,219,106,0.2)" stroke-width="1"/>
    <!-- Ticks every 30° -->
    ${Array.from({length:12},(_,i)=>{
      const a=(i*30-90)*Math.PI/180;
      const r1=40,r2=i%3===0?44:42;
      return `<line x1="${50+r1*Math.cos(a)}" y1="${50+r1*Math.sin(a)}" x2="${50+r2*Math.cos(a)}" y2="${50+r2*Math.sin(a)}" stroke="rgba(76,219,106,${i%3===0?'0.5':'0.2'})" stroke-width="${i%3===0?1.5:0.7}"/>`;
    }).join('')}
    <!-- Knob body -->
    <circle cx="50" cy="50" r="38" fill="#141714" stroke="rgba(76,219,106,0.15)" stroke-width="1"/>
    <!-- 3D gradient -->
    <radialGradient id="kg" cx="38%" cy="35%">
      <stop offset="0%" stop-color="rgba(76,219,106,0.08)"/>
      <stop offset="100%" stop-color="rgba(0,0,0,0)"/>
    </radialGradient>
    <circle cx="50" cy="50" r="38" fill="url(#kg)"/>
    <!-- Pointer -->
    <g id="knob-needle">
      <line x1="50" y1="50" x2="50" y2="16" stroke="#4cdb6a" stroke-width="2.5" stroke-linecap="round"
        style="filter:drop-shadow(0 0 3px rgba(76,219,106,0.6))"/>
      <circle cx="50" cy="16" r="3" fill="#4cdb6a"/>
    </g>
    <!-- Center -->
    <circle cx="50" cy="50" r="5" fill="#141714" stroke="rgba(76,219,106,0.3)" stroke-width="1"/>
  </svg>
  <div style="text-align:center;font-family:var(--mono);font-size:9px;color:var(--dim);margin-top:4px;letter-spacing:1px;">
    TUNING · ${fmtStep(selectedStep)}
  </div>`;
  initKnob();
}

// ── Export ───────────────────────────────────────────────────────────────────
window.VFO = {
  renderVFO,
  updateVFODisplay,
  renderKnobSVG,
  wheelDigit,
  bumpDigit,
  keyDigit,
  selectDigit,
  init() {
    renderVFO();
    // _attachDigitListeners is called by renderVFO() via setTimeout(0)
    renderKnobSVG();

    // Scroll over the whole VFO box (outside the digits) — global knob step
    document.getElementById('vfo-box')?.addEventListener('wheel', e => {
      const x = e.clientX, y = e.clientY;
      let overDigit = false;
      document.querySelectorAll('.vfo-digit').forEach(function(el) {
        const r = el.getBoundingClientRect();
        if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) overDigit = true;
      });
      if (overDigit) return;
      if (!_canControl()) { e.preventDefault(); _blockToast(); return; }
      e.preventDefault();
      const dir = e.deltaY < 0 ? 1 : -1;
      window.UI?.sendFreq(Math.max(100000, S.freq + dir * selectedStep));
    }, { passive: false });

    selectDigit(1000);
  },
  updateStep(step) {
    selectedStep = step;
    // Refresh the label below the knob
    const lbl = document.querySelector('#vfo-knob-wrap div');
    if (lbl) lbl.textContent = `TUNING · ${fmtStep(step)}`;
    document.querySelectorAll('.vfo-digit.selected').forEach(el => el.classList.remove('selected'));
    document.querySelectorAll(`.vfo-digit[data-step="${step}"]`).forEach(el => el.classList.add('selected'));
  },
};

document.addEventListener('DOMContentLoaded', () => {
  // Init after the state has loaded
  setTimeout(() => VFO.init(), 200);
});

})();
