/**
 * wsjtx_scope.js — waterfall for the WSJT-X tab / our own FT8 decoder.
 * WebGL version: spectral data held as a GPU texture (LUMINANCE, nBins x
 * MAX_ROWS), updated for every new column via texSubImage2D (without
 * re-uploading the whole texture). The color palette is a separate 1D
 * lookup texture, and value->color mapping happens in the fragment
 * shader. Hardware bilinear interpolation (gl.LINEAR) gives smooth
 * transitions at no extra CPU cost — unlike the previous version (canvas
 * 2D + offscreen + drawImage), all the scaling/interpolation work happens on the GPU.
 *
 * Separate from js/waterfall.js (which handles the whole radio's wide CI-V scope).
 *
 * Functions (API identical to the canvas 2D version, so wsjtx.js/index.html
 * don't need changes):
 *   init, onWaterfallData, onTxFreqUpdate, onSplitStatus, setRxFreq,
 *   toggleTxFreeze
 */
(function () {
'use strict';

let canvas = null, gl = null;
let fMin = 100, fMax = 3000, nBins = 200;
const MAX_ROWS = 300;

// Data texture: LUMINANCE, width=nBins, height=MAX_ROWS. Every new row
// overwrites the next line in the CPU-side buffer, and writeRow points at
// which texture line (cyclically) to write the next column into — this
// way we DON'T need to shift the whole history in memory or re-upload
// the whole texture on every new column.
let dataTex = null;
let dataBuf = null;        // Uint8Array(nBins * MAX_ROWS), CPU-side mirror
let writeRow = 0;           // which row (0..MAX_ROWS-1) to write next
// Position (writeRow) where the LATEST decode-period boundary started
// (xx:00/15/30/45 for FT8, every 7.5s for FT4) — used to draw a thin
// separator line on the waterfall, at the same place as the line in Band
// Activity. null until the first boundary is detected after start.
let periodBoundaryRow = null;
let _lastWindowSlot = null;   // to detect a TRANSITION between windows (not just state)
let _scopeDecodeMode = 'FT8'; // synced with WSJTX._decodeMode via setDecodeMode below
let rowsFilled = 0;         // how many rows already have real data (grows up to MAX_ROWS)

let paletteTex = null;

let txFreqHz = 1000; // default initial value, overwritten by the real
                      // state from the backend; NOT 1500Hz — see the
                      // comment in webapp.py about the IC-7300 USB-D notch
let txFrozen = false;
let rxFreqHz = 1000; // RX marker, INDEPENDENT of TX — starts at the same
                      // value, but can be moved separately (dragging or a
                      // manually typed value)

// Drag state: which marker is currently grabbed ('tx'|'rx'|null).
// Detected on mousedown based on the distance to each marker — if the
// click is close to an existing line, we drag ONLY that marker;
// otherwise we treat it as a new spot and move BOTH at once.
let _dragging = null;
const DRAG_HIT_PX = 8; // radius (in logical CSS px, not device px) for detecting a click on a marker

let _needsRender = false;
let _rafStarted = false;

// Palette Adjust (REF/ZERO/GAIN) — see the comment at uPaletteRef in the
// fragment shader. Default values = a no-op relative to the base palette.
let paletteRef  = 0.15;
let paletteZero = 0.0;
let paletteGain = 1.0;

function setPaletteReference(v) { paletteRef  = v; _requestRender(); }
function setPaletteZero(v)      { paletteZero = v; _requestRender(); }
function setPaletteGain(v)      { paletteGain = v; _requestRender(); }

// Waterfall palette: dark navy (silence/noise) -> blue -> cyan -> green ->
// yellow -> white (strong signals). Most of the range (background noise)
// stays dark and muted, only strong signals get bright. Typical
// background/noise is SATURATED blue [1,12,144], not near-black like in
// the previous version. Curve: black only for absolute silence, quickly
// transitions to saturated blue (typical noise), then cyan/green/
// yellow/white for progressively stronger signals.
const PALETTE_STOPS = [
  [0,   [0,   0,   10]],
  [15,  [0,   5,   60]],
  [40,  [1,   12,  144]],  // "typowy szum"
  [90,  [0,   60,  190]],
  [140, [0,   150, 210]],
  [180, [90,  215, 150]],
  [210, [235, 225, 65]],
  [235, [255, 165, 40]],
  [255, [255, 255, 255]],
];

function _buildPaletteRGBA() {
  const out = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    let lo = PALETTE_STOPS[0], hi = PALETTE_STOPS[PALETTE_STOPS.length - 1];
    for (let s = 0; s < PALETTE_STOPS.length - 1; s++) {
      if (i >= PALETTE_STOPS[s][0] && i <= PALETTE_STOPS[s + 1][0]) { lo = PALETTE_STOPS[s]; hi = PALETTE_STOPS[s + 1]; break; }
    }
    const span = hi[0] - lo[0];
    const t = span > 0 ? (i - lo[0]) / span : 0;
    out[i * 4 + 0] = Math.round(lo[1][0] + t * (hi[1][0] - lo[1][0]));
    out[i * 4 + 1] = Math.round(lo[1][1] + t * (hi[1][1] - lo[1][1]));
    out[i * 4 + 2] = Math.round(lo[1][2] + t * (hi[1][2] - lo[1][2]));
    out[i * 4 + 3] = 255;
  }
  return out;
}

// ── Shadery ──────────────────────────────────────────────────────────────────
const VERT_SRC = `
attribute vec2 aPos;
varying vec2 vUv;
void main() {
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;

const FRAG_SRC = `
precision mediump float;
varying vec2 vUv;
uniform sampler2D uData;
uniform sampler2D uPalette;
uniform float uWriteRow;
uniform float uRowsFilled;
uniform float uMaxRows;
uniform float uTxX;
uniform float uRxX;
uniform float uHasRx;
uniform float uTxFrozen;
uniform float uAspectPx;
uniform float uAspectPxY;
uniform float uPeriodBoundaryRow;
uniform float uHasPeriodBoundary;
uniform float uPaletteRef;
uniform float uPaletteZero;
uniform float uPaletteGain;

void main() {
  float rowsFilled = min(uRowsFilled, uMaxRows);
  if (rowsFilled < 1.0) { gl_FragColor = vec4(0.0,0.0,0.0,1.0); return; }

  // CRITICAL: the screen-pixel -> data-row mapping scale must be FIXED
  // (uMaxRows), NOT change along with rowsFilled. An earlier version used
  // rowsVisible=min(rowsFilled,uMaxRows) as the multiplier — that meant
  // that until the buffer filled up (the first ~90s of running), EVERY
  // screen pixel got remapped to a different data row on every new column
  // (because the multiplier grew every frame), producing visible
  // "jumping"/"ghosting" — already-drawn signals shifted visually even
  // though the underlying data hadn't changed. Now: a fixed scale
  // (uMaxRows rows always corresponds to the full screen height), and
  // rows not yet filled simply stay black until real data arrives there.
  float rowFromTopF = (1.0 - vUv.y) * uMaxRows;
  rowFromTopF = min(rowFromTopF, uMaxRows - 1.0);
  if (rowFromTopF >= rowsFilled) { gl_FragColor = vec4(0.0,0.0,0.0,1.0); return; }

  // MANUAL sampling in LOGICAL space (not directly in texture space), to
  // avoid automatic GPU interpolation at the wrap boundary of the
  // circular buffer (where texRow=writeRow-1 (newest) and
  // texRow=writeRow (oldest) are physically adjacent in the texture, even
  // though logically they're opposite ends of the history — interpolating
  // between them produced a visible "seam"/artifact once per full buffer cycle).
  float rowFromTop0 = floor(rowFromTopF);
  float rowFromTop1 = min(rowFromTop0 + 1.0, rowsFilled - 1.0);
  float frac = rowFromTopF - rowFromTop0;

  float texRow0 = mod(uWriteRow - 1.0 - rowFromTop0 + uMaxRows * 4.0, uMaxRows);
  float texRow1 = mod(uWriteRow - 1.0 - rowFromTop1 + uMaxRows * 4.0, uMaxRows);

  float v0 = texture2D(uData, vec2(vUv.x, (texRow0 + 0.5) / uMaxRows)).r;
  float v1 = texture2D(uData, vec2(vUv.x, (texRow1 + 0.5) / uMaxRows)).r;
  float v = mix(v0, v1, frac);

  // Palette Adjust (REF/ZERO/GAIN). At the default values (ref=0.15,
  // zero=0, gain=1.0) this is a no-op — v passes through unchanged, so
  // the baseline looks exactly like before these sliders were added.
  // REF: shifts brightness relative to the palette's built-in reference
  // point (0.15, where PALETTE_STOPS has "typical noise"). ZERO: clips
  // the bottom (crushes weak signals to black). GAIN: contrast relative
  // to the middle of the range.
  float vAdj = v + (uPaletteRef - 0.15);
  vAdj = max(0.0, vAdj - uPaletteZero);
  vAdj = (vAdj - 0.5) * uPaletteGain + 0.5;
  vAdj = clamp(vAdj, 0.0, 1.0);

  vec3 color = texture2D(uPalette, vec2(vAdj, 0.5)).rgb;

  // A thin horizontal line at the decode-period boundary (xx:00/15/30/45
  // for FT8, every 7.5s for FT4) — indicates that a NEW window starts
  // below this line. Synchronized with the equivalent line in Band
  // Activity (the same boundary-detection logic, see _windowSlot in
  // wsjtx.js and the code in onWaterfallData above). The position is
  // computed with the SAME transform (mod with buffer wraparound) as the
  // texture data read, so the line scrolls perfectly together with the
  // data, without "floating" relative to it.
  if (uHasPeriodBoundary > 0.5) {
    float boundaryRowFromTop = mod(uWriteRow - 1.0 - uPeriodBoundaryRow + uMaxRows * 4.0, uMaxRows);
    if (boundaryRowFromTop < rowsFilled) {
      float rowPx = boundaryRowFromTop / uMaxRows;
      float curPx = 1.0 - vUv.y;
      float lineHalfHeight = 0.75 / uAspectPxY;
      if (abs(curPx - rowPx) < lineHalfHeight) {
        color = mix(color, vec3(1.0, 1.0, 1.0), 0.55);
      }
    }
  }

  float lineHalfWidth = 1.0 / uAspectPx;
  // TX line: red when free, orange when frozen in place
  if (abs(vUv.x - uTxX) < lineHalfWidth * 1.5) {
    color = uTxFrozen > 0.5 ? vec3(1.0, 0.55, 0.0) : vec3(1.0, 0.27, 0.27);
  }
  // RX line: green (only when different from TX)
  if (uHasRx > 0.5 && abs(vUv.x - uRxX) < lineHalfWidth * 1.5) {
    color = vec3(0.27, 0.85, 0.33);
  }

  gl_FragColor = vec4(color, 1.0);
}`;

function _compileShader(type, src) {
  const sh = gl.createShader(type);
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    console.error('[wsjtx_scope] shader compile error:', gl.getShaderInfoLog(sh));
    gl.deleteShader(sh);
    return null;
  }
  return sh;
}

let prog = null, uLoc = {};

function _initGL() {
  gl = canvas.getContext('webgl', { antialias: true, preserveDrawingBuffer: false })
    || canvas.getContext('experimental-webgl');
  if (!gl) {
    console.warn('[wsjtx_scope] WebGL unavailable, scope will not work');
    return false;
  }

  const vs = _compileShader(gl.VERTEX_SHADER, VERT_SRC);
  const fs = _compileShader(gl.FRAGMENT_SHADER, FRAG_SRC);
  if (!vs || !fs) return false;

  prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    console.error('[wsjtx_scope] program link error:', gl.getProgramInfoLog(prog));
    return false;
  }
  gl.useProgram(prog);

  const quad = new Float32Array([-1,-1, 1,-1, -1,1, -1,1, 1,-1, 1,1]);
  const quadBuf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
  gl.bufferData(gl.ARRAY_BUFFER, quad, gl.STATIC_DRAW);
  const aPos = gl.getAttribLocation(prog, 'aPos');
  gl.enableVertexAttribArray(aPos);
  gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

  uLoc = {
    uData: gl.getUniformLocation(prog, 'uData'),
    uPalette: gl.getUniformLocation(prog, 'uPalette'),
    uWriteRow: gl.getUniformLocation(prog, 'uWriteRow'),
    uRowsFilled: gl.getUniformLocation(prog, 'uRowsFilled'),
    uMaxRows: gl.getUniformLocation(prog, 'uMaxRows'),
    uTxX: gl.getUniformLocation(prog, 'uTxX'),
    uRxX: gl.getUniformLocation(prog, 'uRxX'),
    uHasRx: gl.getUniformLocation(prog, 'uHasRx'),
    uSplitX: gl.getUniformLocation(prog, 'uSplitX'),
    uSplitOn: gl.getUniformLocation(prog, 'uSplitOn'),
    uTxFrozen: gl.getUniformLocation(prog, 'uTxFrozen'),
    uAspectPx: gl.getUniformLocation(prog, 'uAspectPx'),
    uAspectPxY: gl.getUniformLocation(prog, 'uAspectPxY'),
    uPeriodBoundaryRow: gl.getUniformLocation(prog, 'uPeriodBoundaryRow'),
    uHasPeriodBoundary: gl.getUniformLocation(prog, 'uHasPeriodBoundary'),
    uPaletteRef: gl.getUniformLocation(prog, 'uPaletteRef'),
    uPaletteZero: gl.getUniformLocation(prog, 'uPaletteZero'),
    uPaletteGain: gl.getUniformLocation(prog, 'uPaletteGain'),
  };

  dataBuf = new Uint8Array(nBins * MAX_ROWS);
  dataTex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, dataTex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, nBins, MAX_ROWS, 0, gl.LUMINANCE, gl.UNSIGNED_BYTE, dataBuf);

  paletteTex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, paletteTex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, _buildPaletteRGBA());

  return true;
}

function _reallocDataTextureIfNeeded() {
  if (dataBuf && dataBuf.length === nBins * MAX_ROWS) return;
  dataBuf = new Uint8Array(nBins * MAX_ROWS);
  writeRow = 0;
  rowsFilled = 0;
  periodBoundaryRow = null;
  _lastWindowSlot = null;
  if (gl && dataTex) {
    gl.bindTexture(gl.TEXTURE_2D, dataTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, nBins, MAX_ROWS, 0, gl.LUMINANCE, gl.UNSIGNED_BYTE, dataBuf);
  }
}

function init() {
  canvas = document.getElementById('wj-scope-canvas');
  if (!canvas) { console.warn('[scope] no canvas'); return; }

  // If GL is already initialized — just resize and render
  if (gl) { _resizeCanvas(); _renderAxis(); _requestRender(); return; }

  canvas.addEventListener('mousedown', _onMouseDown);
  window.addEventListener('mousemove', _onMouseMove);
  window.addEventListener('mouseup', _onMouseUp);
  // Touch support (added for the mobile mini-waterfall — this file
  // previously only handled mouse events, which desktop browsers never
  // needed touch equivalents for, but a phone has no mouse at all: no
  // touch listeners meant the waterfall rendered but tapping/dragging the
  // TX/RX markers (or picking a Hound slot) silently did nothing).
  // {passive:false} is required because touchmove calls preventDefault()
  // to stop the page from scrolling while dragging a marker.
  canvas.addEventListener('touchstart', _onTouchStart, { passive: false });
  window.addEventListener('touchmove', _onTouchMove, { passive: false });
  window.addEventListener('touchend', _onTouchEnd);
  window.addEventListener('touchcancel', _onTouchEnd);

  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => { _resizeCanvas(); _renderAxis(); _requestRender(); });
    ro.observe(canvas);
  } else {
    window.addEventListener('resize', () => { _resizeCanvas(); _renderAxis(); _requestRender(); });
  }

  // Wait until the canvas has dimensions before initializing WebGL
  function _waitAndInit(tries) {
    const rect = canvas.getBoundingClientRect();
    console.log('[scope] init attempt', tries, 'rect:', rect.width, 'x', rect.height);
    if (rect.width < 1 || rect.height < 1) {
      if (tries > 0) setTimeout(() => _waitAndInit(tries - 1), 100);
      else console.warn('[scope] canvas has zero dimensions after 30 attempts');
      return;
    }
    _resizeCanvas();
    if (!_initGL()) { console.warn('[scope] _initGL returned false'); return; }
    _renderAxis();
    _requestRender();
    _updateLabels();
    _startRenderLoop();
    console.log('[scope] initialized, canvas:', canvas.width, 'x', canvas.height);
  }
  _waitAndInit(30);
}

// Frequency scale above the waterfall (e.g. "500", "1000", "1500"...), in
// the style of the reference JTDX. Rendered as lightweight HTML elements
// positioned as a percentage of the container's width — simpler and
// crisper (native browser font) than drawing text in WebGL/Canvas.
function _renderAxis() {
  const axisEl = document.getElementById('wj-scope-axis');
  if (!axisEl || !canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 1) return;

  // Tick step chosen so labels don't overlap: ~500Hz at a typical panel
  // width, denser when the panel is wider.
  const range = fMax - fMin;
  let step = 500;
  if (rect.width > 1400) step = 250;
  else if (rect.width < 700) step = 1000;

  const firstTick = Math.ceil(fMin / step) * step;
  let html = '';
  for (let f = firstTick; f < fMax; f += step) {
    const pct = ((f - fMin) / range) * 100;
    html += `<span class="wj-axis-tick" style="left:${pct.toFixed(2)}%">${f}</span>`;
  }
  axisEl.innerHTML = html;
}

function _startRenderLoop() {
  if (_rafStarted) return;
  _rafStarted = true;
  function tick() {
    if (_needsRender) {
      _needsRender = false;
      _render();
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

function _requestRender() { _needsRender = true; }

function _resizeCanvas() {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 1 || rect.height < 1) {
    return;
  }
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  if (gl) gl.viewport(0, 0, canvas.width, canvas.height);
}

function onWaterfallData(msg) {
  const prevFMin = fMin, prevFMax = fMax;
  if (msg.fMin !== undefined) fMin = msg.fMin;
  if (msg.fMax !== undefined) fMax = msg.fMax;
  if (fMin !== prevFMin || fMax !== prevFMax) _renderAxis();
  if (msg.nBins !== undefined && msg.nBins !== nBins) {
    nBins = msg.nBins;
    _reallocDataTextureIfNeeded();
  }
  if (!Array.isArray(msg.data) || !gl) return;

  // Detect the decode-period boundary (UTC, 15s for FT8 / 7.5s for FT4) —
  // the same "slot number" logic as _windowSlot() in wsjtx.js, so both
  // lines (Band Activity and waterfall) stay consistent. Checked BEFORE
  // writing the new row, since periodBoundaryRow is meant to point at the
  // row where the new transmission started.
  const nowUtc = new Date();
  const totalSec = nowUtc.getUTCHours()*3600 + nowUtc.getUTCMinutes()*60 + nowUtc.getUTCSeconds()
                    + nowUtc.getUTCMilliseconds()/1000;
  const windowS = _scopeDecodeMode === 'FT4' ? 7.5 : 15.0;
  const slot = Math.floor(totalSec / windowS);
  if (_lastWindowSlot !== null && slot !== _lastWindowSlot) {
    periodBoundaryRow = writeRow;
  }
  _lastWindowSlot = slot;

  const row = new Uint8Array(msg.data);
  dataBuf.set(row.subarray(0, nBins), writeRow * nBins);
  gl.bindTexture(gl.TEXTURE_2D, dataTex);
  gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, writeRow, nBins, 1, gl.LUMINANCE, gl.UNSIGNED_BYTE, row);

  writeRow = (writeRow + 1) % MAX_ROWS;
  rowsFilled = Math.min(rowsFilled + 1, MAX_ROWS);
  _requestRender();
}

// Called from wsjtx.js::setDecodeMode, so the waterfall uses the same
// window length (15s/7.5s) as the rest of the UI when detecting period boundaries.
function setScopeDecodeMode(mode) {
  _scopeDecodeMode = mode;
}

function onTxFreqUpdate(msg) {
  if (msg.freqHz !== undefined) txFreqHz = msg.freqHz;
  if (msg.frozen !== undefined) txFrozen = msg.frozen;
  _updateLabels();
  _requestRender();
}


function setRxFreq(freqHz) {
  rxFreqHz = freqHz;
  _updateLabels();
  _requestRender();
}

function _pxToFreq(clientX) {
  const rect = canvas.getBoundingClientRect();
  const xFrac = (clientX - rect.left) / rect.width;
  return fMin + xFrac * (fMax - fMin);
}

function _freqToPx(freqHz) {
  const rect = canvas.getBoundingClientRect();
  return rect.left + _freqToFrac(freqHz) * rect.width;
}

function _onMouseDown(ev) {
  if (!canvas) return;
  const distTx = Math.abs(ev.clientX - _freqToPx(txFreqHz));
  const distRx = Math.abs(ev.clientX - _freqToPx(rxFreqHz));

  // If the click is close to an EXISTING marker, we grab ONLY that one
  // (independent dragging). Otherwise it's a "new spot" — we immediately
  // move BOTH markers there (ft8_set_both_freq), and the user can
  // continue dragging as a new starting point for both.
  if (distTx <= DRAG_HIT_PX && distTx <= distRx) {
    if (txFrozen) {
      window.UI?.showToast(I18n.t('wj_toast_tx_frozen'));
      return;
    }
    _dragging = 'tx';
  } else if (distRx <= DRAG_HIT_PX) {
    _dragging = 'rx';
  } else {
    // Click on an empty spot on the waterfall: move BOTH markers there.
    const freq = _pxToFreq(ev.clientX);
    window.WS?.send({ type: 'ft8_set_both_freq', freqHz: Math.round(freq) });
    _dragging = 'both'; // allows continuing to drag both if the user hasn't released the button
  }
  ev.preventDefault();
}

function _onMouseMove(ev) {
  if (!_dragging || !canvas) return;
  const freq = Math.round(_pxToFreq(ev.clientX));
  if (_dragging === 'tx') {
    if (txFrozen) return;
    window.WS?.send({ type: 'ft8_set_tx_freq', freqHz: freq });
  } else if (_dragging === 'rx') {
    window.WS?.send({ type: 'ft8_set_rx_freq', freqHz: freq });
  } else if (_dragging === 'both') {
    window.WS?.send({ type: 'ft8_set_both_freq', freqHz: freq });
  }
}

function _onMouseUp() {
  _dragging = null;
}

// Touch equivalents — translate the touch point's clientX into the same
// {clientX, preventDefault} shape _onMouseDown/_onMouseMove already
// expect, instead of duplicating their hit-testing/drag logic.
function _onTouchStart(ev) {
  if (ev.touches.length !== 1) return; // ignore pinch/multi-touch
  const t = ev.touches[0];
  _onMouseDown({ clientX: t.clientX, preventDefault: () => ev.preventDefault() });
}
function _onTouchMove(ev) {
  if (!_dragging) return;
  const t = ev.touches[0];
  if (!t) return;
  ev.preventDefault(); // dragging a marker shouldn't also scroll the page
  _onMouseMove({ clientX: t.clientX });
}
function _onTouchEnd() {
  _onMouseUp();
}

function toggleTxFreeze() {
  window.WS?.send({ type: 'ft8_toggle_tx_freeze', frozen: !txFrozen });
}


function setTxFreqManual(val) {
  const freq = parseFloat(val);
  if (Number.isNaN(freq)) return;
  if (txFrozen) {
    window.UI?.showToast(I18n.t('wj_toast_tx_frozen'));
    return;
  }
  window.WS?.send({ type: 'ft8_set_tx_freq', freqHz: Math.round(freq) });
}

function setRxFreqManual(val) {
  const freq = parseFloat(val);
  if (Number.isNaN(freq)) return;
  window.WS?.send({ type: 'ft8_set_rx_freq', freqHz: Math.round(freq) });
}

function rxEqTx() {
  window.WS?.send({ type: 'ft8_rx_eq_tx' });
}

function txEqRx() {
  window.WS?.send({ type: 'ft8_tx_eq_rx' });
}

function getRxFreq() {
  return rxFreqHz;
}

// Is TX frozen (Hold Tx Freq)? Used by _selectRow in wsjtx.js to decide
// whether TX should follow the correspondent when calling.
function isTxFrozen() { return txFrozen; }

function getTxFreq() {
  return txFreqHz;
}

function onRxFreqUpdate(msg) {
  if (msg.freqHz !== undefined) rxFreqHz = msg.freqHz;
  _updateLabels();
  _requestRender();
}

function _updateLabels() {
  const txInput = document.getElementById('wj-tx-freq-input');
  if (txInput && document.activeElement !== txInput) {
    txInput.value = Math.round(txFreqHz);
  }
  const rxInput = document.getElementById('wj-rx-freq-input');
  if (rxInput && document.activeElement !== rxInput) {
    rxInput.value = Math.round(rxFreqHz);
  }
  const freezeBtn = document.getElementById('wj-tx-freeze-btn');
  if (freezeBtn) {
    freezeBtn.textContent = txFrozen ? I18n.t('wj_tx_locked_btn') : I18n.t('wj_tx_free_btn');
    freezeBtn.classList.toggle('active', txFrozen);
  }
}

function _freqToFrac(freqHz) {
  return (freqHz - fMin) / (fMax - fMin);
}

function _render() {
  if (!gl || !canvas) return;
  gl.useProgram(prog);

  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, dataTex);
  gl.uniform1i(uLoc.uData, 0);

  gl.activeTexture(gl.TEXTURE1);
  gl.bindTexture(gl.TEXTURE_2D, paletteTex);
  gl.uniform1i(uLoc.uPalette, 1);

  gl.uniform1f(uLoc.uWriteRow, writeRow);
  gl.uniform1f(uLoc.uRowsFilled, rowsFilled);
  gl.uniform1f(uLoc.uMaxRows, MAX_ROWS);
  gl.uniform1f(uLoc.uTxX, _freqToFrac(txFreqHz));
  gl.uniform1f(uLoc.uHasRx, 1.0);
  gl.uniform1f(uLoc.uRxX, _freqToFrac(rxFreqHz));
  gl.uniform1f(uLoc.uTxFrozen, txFrozen ? 1.0 : 0.0);
  gl.uniform1f(uLoc.uAspectPx, canvas.width);
  gl.uniform1f(uLoc.uAspectPxY, canvas.height);
  gl.uniform1f(uLoc.uHasPeriodBoundary, periodBoundaryRow !== null ? 1.0 : 0.0);
  gl.uniform1f(uLoc.uPeriodBoundaryRow, periodBoundaryRow !== null ? periodBoundaryRow : 0.0);
  gl.uniform1f(uLoc.uPaletteRef, paletteRef);
  gl.uniform1f(uLoc.uPaletteZero, paletteZero);
  gl.uniform1f(uLoc.uPaletteGain, paletteGain);

  gl.drawArrays(gl.TRIANGLES, 0, 6);
}

function onSplitStatus(msg) { /* split status - handled by wsjtx.js */ }

window.WSJTXScope = { init, onWaterfallData, onTxFreqUpdate, onRxFreqUpdate, onSplitStatus,
                        setRxFreq, setTxFreqManual, setRxFreqManual, rxEqTx, txEqRx, getRxFreq, getTxFreq, isTxFrozen,
                        toggleTxFreeze, setScopeDecodeMode,
                        setPaletteReference, setPaletteZero, setPaletteGain };
})();
