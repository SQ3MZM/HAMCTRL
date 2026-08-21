/**
 * waterfall.js — waterfall/spectrum panel
 * IC-7300-like style: dark background, green spectrum line,
 * blue→cyan→yellow→red palette, kHz offset grid,
 * clear filter overlay, axis with offsets relative to the VFO.
 */
(function () {
'use strict';

const S = window.AppState;

let specCanvas = null, wfCanvas = null, axisCanvas = null;
let specCtx = null, wfCtx = null, axisCtx = null;
let source = 'sim';
let audioAnalyser = null;
let animFrame = null;
let lastScopeData = null;

let lastMeta = {
  centerHz: 14200000, spanHz: 25000, loHz: 14187500, hiHz: 14212500,
  mode: 'USB', dataMode: false, filterHz: 2400, scopeMode: 0, outOfRange: false,
};

// ── IC-7300 palette: black → navy → blue → cyan → yellow → red ────────────────
const PALETTE = (() => {
  const p = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let r, g, b;
    if (i < 40) {
      // Black → very dark navy (background noise)
      r = 0; g = 0; b = Math.round(i * 3.5);
    } else if (i < 90) {
      // Navy → blue
      r = 0; g = Math.round((i-40)*1.5); b = Math.min(255, 140 + Math.round((i-40)*2));
    } else if (i < 140) {
      // Blue → cyan
      r = 0; g = Math.min(255, 75 + Math.round((i-90)*3.6)); b = 255 - Math.round((i-90)*2);
    } else if (i < 190) {
      // Cyan → yellow
      r = Math.round((i-140)*5.1); g = 255; b = Math.max(0, 155 - Math.round((i-140)*3.1));
    } else {
      // Yellow → red
      r = 255; g = Math.max(0, 255 - Math.round((i-190)*5.1)); b = 0;
    }
    p[i] = (255<<24)|(b<<16)|(g<<8)|r; // ABGR
  }
  return p;
})();

// ── Init ──────────────────────────────────────────────────────────────────────
function init() {
  specCanvas = document.getElementById('wf-spectrum');
  wfCanvas   = document.getElementById('wf-waterfall');
  axisCanvas = document.getElementById('wf-axis');
  if (!specCanvas || !wfCanvas) return;

  if (!axisCanvas) {
    axisCanvas = document.createElement('canvas');
    axisCanvas.id = 'wf-axis';
    axisCanvas.style.cssText = 'display:block;width:100%;height:18px;';
    wfCanvas.parentElement?.appendChild(axisCanvas);
  }

  resize();
  specCtx = specCanvas.getContext('2d');
  wfCtx   = wfCanvas.getContext('2d');
  axisCtx = axisCanvas.getContext('2d');

  attachClickToTune();
  window.addEventListener('resize', resize);
  drawAxis();
  start();
}

function resize() {
  [specCanvas, wfCanvas, axisCanvas].forEach(c => {
    if (!c) return;
    const W = c.offsetWidth || c.parentElement?.offsetWidth || 600;
    const H = parseInt(c.style.height) ||
              (c.id === 'wf-spectrum' ? 65 : c.id === 'wf-axis' ? 18 : 180);
    if (c.width !== W || c.height !== H) { c.width = W; c.height = H; }
  });
}

// ── Click-to-tune ─────────────────────────────────────────────────────────────
function freqFromX(x, W) {
  const { loHz, hiHz } = lastMeta;
  if (loHz == null || hiHz == null) return null;
  return Math.round(loHz + (x / W) * (hiHz - loHz));
}

function tuneToClick(e, canvas) {
  const rect = canvas.getBoundingClientRect();
  const x = (e.clientX - rect.left) * (canvas.width / rect.width);
  const freq = freqFromX(x, canvas.width);
  if (freq == null) return;
  if (S) S.freq = freq;
  window.UI?.updateFreqDisplay?.();
  window.VFO?.updateVFODisplay?.();
  window.WS?.send({ type: 'freq', freq });
}

function attachClickToTune() {
  [specCanvas, wfCanvas].forEach(c => {
    if (!c) return;
    c.style.cursor = 'crosshair';
    let lastPointerTs = 0;
    c.addEventListener('pointerdown', (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      lastPointerTs = e.timeStamp;
      tuneToClick(e, c);
    });
    c.addEventListener('click', (e) => {
      if (e.timeStamp - lastPointerTs < 50) return;
      tuneToClick(e, c);
    });
  });
}

// ── Data from WS ──────────────────────────────────────────────────────────────
function handleScopeData(msg) {
  if (msg.type !== 'scope_frame' && msg.type !== 'scope_data') return;
  lastScopeData = msg.data;
  source = msg.source === 'radio' ? 'radio' : 'sim';
  if (msg.centerHz != null) lastMeta.centerHz = msg.centerHz;
  if (msg.spanHz   != null) lastMeta.spanHz   = msg.spanHz;
  if (msg.loHz     != null) lastMeta.loHz     = msg.loHz;
  if (msg.hiHz     != null) lastMeta.hiHz     = msg.hiHz;
  if (msg.mode     != null) lastMeta.mode     = msg.mode;
  if (msg.dataMode != null) lastMeta.dataMode = msg.dataMode;
  if (msg.filterHz != null) lastMeta.filterHz = msg.filterHz;
  if (msg.scopeMode!= null) lastMeta.scopeMode= msg.scopeMode;
  if (msg.outOfRange!=null) lastMeta.outOfRange = msg.outOfRange;
  drawFrame(msg.data);
  drawAxis();
}

function handleFilterWidth(msg) {
  if (msg.type !== 'filter_width') return;
  lastMeta.filterHz = msg.hz;
  if (msg.mode) lastMeta.mode = msg.mode;
  if (msg.dataMode != null) lastMeta.dataMode = msg.dataMode;
  drawAxis();
}

// ── Draw a frame ────────────────────────────────────────────────────────────
// Peak-hold state — the highest value in each bin with a defined decay.
// Reset when the bin count or canvas width changes.
let _peakHold = null;
let _peakDecay = 0.985; // per frame — the closer to 1, the slower the fade

function drawFrame(data) {
  if (!specCtx || !wfCtx || !data?.length) return;
  const W  = specCanvas.width, Hs = specCanvas.height;
  const Ww = wfCanvas.width,   Hw = wfCanvas.height;

  // Update peak-hold (match the data length)
  if (!_peakHold || _peakHold.length !== data.length) {
    _peakHold = new Float32Array(data.length);
  }
  for (let i = 0; i < data.length; i++) {
    const v = data[i] || 0;
    if (v > _peakHold[i]) {
      _peakHold[i] = v;
    } else {
      _peakHold[i] *= _peakDecay;
    }
  }

  // Linear interpolation between bins — instead of Math.floor(i *
  // data.length / W) which produces "stepping" when W < data.length.
  // Interpolation gives a smooth plot even when pixels map to fractional bins.
  const interpolate = (arr, x) => {
    const scale = arr.length / W;
    const src = x * scale;
    const i0 = Math.floor(src);
    const i1 = Math.min(arr.length - 1, i0 + 1);
    const frac = src - i0;
    return (arr[i0] || 0) * (1 - frac) + (arr[i1] || 0) * frac;
  };

  // ── SPECTRUM ────────────────────────────────────────────────────────────────
  // Background
  specCtx.fillStyle = '#050708';
  specCtx.fillRect(0, 0, W, Hs);

  // Horizontal grid (dB) — like the IC-7300
  drawSpecGrid(specCtx, W, Hs);

  // Filter overlay (before the curve)
  drawFilterOverlay(specCtx, W, Hs);

  // Gradient under the spectrum curve
  const grad = specCtx.createLinearGradient(0, 0, 0, Hs);
  grad.addColorStop(0,   'rgba(0,255,80,0.28)');
  grad.addColorStop(0.5, 'rgba(0,200,60,0.10)');
  grad.addColorStop(1,   'rgba(0,100,30,0.02)');

  specCtx.beginPath();
  for (let i = 0; i < W; i++) {
    const v  = interpolate(data, i) / 255;
    const y  = Hs - v * (Hs - 4) - 2;
    i === 0 ? specCtx.moveTo(i, y) : specCtx.lineTo(i, y);
  }
  const lastV = interpolate(data, W - 1) / 255;
  specCtx.lineTo(W, Hs - lastV * (Hs-4) - 2);
  specCtx.lineTo(W, Hs); specCtx.lineTo(0, Hs);
  specCtx.fillStyle = grad;
  specCtx.fill();

  // Spectrum line — bright green like the IC-7300
  // CRITICAL for latency: shadowBlur used to fire on EVERY one of 15
  // scope_frame frames/sec whenever source==='radio' (i.e. all the time
  // during actual listening) - rendering a blur over a whole ~900-point
  // line is one of the most expensive Canvas 2D effects, on the same JS
  // thread as ping/pong (WS) and audio decoding (_audioWs). Removed - just
  // a thicker bright line gives a similar visual "glow" effect at no cost.
  specCtx.beginPath();
  specCtx.strokeStyle = source === 'radio' ? '#00e040' : 'rgba(0,200,60,0.5)';
  specCtx.lineWidth   = source === 'radio' ? 2 : 1.5;
  for (let i = 0; i < W; i++) {
    const v  = interpolate(data, i) / 255;
    const y  = Hs - v * (Hs - 4) - 2;
    i === 0 ? specCtx.moveTo(i, y) : specCtx.lineTo(i, y);
  }
  specCtx.stroke();

  // Peak-hold — thin orange line (if enabled)
  if (_peakEnabled !== false && _peakHold) {
    specCtx.beginPath();
    specCtx.strokeStyle = 'rgba(255,180,60,0.55)';
    specCtx.lineWidth   = 1;
    for (let i = 0; i < W; i++) {
      const v = interpolate(_peakHold, i) / 255;
      const y = Hs - v * (Hs - 4) - 2;
      i === 0 ? specCtx.moveTo(i, y) : specCtx.lineTo(i, y);
    }
    specCtx.stroke();
  }

  // VFO center marker — vertical yellow line like the IC-7300
  specCtx.strokeStyle = 'rgba(255,220,0,0.85)';
  specCtx.lineWidth   = 1;
  specCtx.setLineDash([]);
  specCtx.beginPath();
  specCtx.moveTo(W/2, 0); specCtx.lineTo(W/2, Hs);
  specCtx.stroke();

  // Source badge
  specCtx.font = '9px Share Tech Mono,monospace';
  specCtx.fillStyle = source === 'radio' ? 'rgba(0,230,60,0.7)' : 'rgba(200,160,60,0.45)';
  specCtx.textAlign = 'right';
  specCtx.fillText(source === 'radio' ? '● RADIO' : '● SIM', W - 4, 11);

  if (lastMeta.outOfRange) {
    specCtx.fillStyle = 'rgba(255,80,80,0.8)';
    specCtx.textAlign = 'left';
    specCtx.fillText('OUT OF RANGE', 4, 11);
  }

  // ── WATERFALL ───────────────────────────────────────────────────────────────
  // Shift the whole waterfall down by 1 pixel (the oldest lines scroll off).
  // CRITICAL for latency: this runs 15x/sec (scope_frame from the backend)
  // on the same JS thread as ping/pong (the WS badge) and audio decoding
  // (_audioWs). Previously getImageData/putImageData allocated a new
  // ~several-hundred-KB array on EVERY frame (15x/sec) - periodic GC
  // pauses blocked the thread long enough that pong arrived late (a false
  // "WS" spike) and audio actually stuttered (delayed
  // playOpusFrame/buffer scheduling). drawImage copies the canvas onto
  // itself with no allocation visible from JS.
  wfCtx.drawImage(wfCanvas, 0, 0, Ww, Hw - 1, 0, 1, Ww, Hw - 1);

  // Draw the new line WITH INTERPOLATION — every canvas pixel gets an
  // interpolated value, instead of a discrete "step" via Math.floor
  const lineImg = wfCtx.createImageData(Ww, 1);
  const d32     = new Uint32Array(lineImg.data.buffer);
  const scale = data.length / Ww;
  for (let i = 0; i < Ww; i++) {
    const src = i * scale;
    const i0 = Math.floor(src);
    const i1 = Math.min(data.length - 1, i0 + 1);
    const frac = src - i0;
    let val = (data[i0] || 0) * (1 - frac) + (data[i1] || 0) * frac;
    val = Math.min(255, Math.max(0, Math.round(val)));
    d32[i] = PALETTE[val];
  }
  wfCtx.putImageData(lineImg, 0, 0);

  // Vertical center line on the waterfall (yellow, short)
  wfCtx.strokeStyle = 'rgba(255,220,0,0.5)';
  wfCtx.lineWidth   = 1;
  wfCtx.beginPath();
  wfCtx.moveTo(Ww/2, 0); wfCtx.lineTo(Ww/2, 4);
  wfCtx.stroke();
}

// ── Spectrum grid (horizontal dB lines, vertical kHz offset lines) ───────────
function drawSpecGrid(ctx, W, H) {
  ctx.strokeStyle = 'rgba(80,120,100,0.18)';
  ctx.lineWidth   = 0.5;
  ctx.setLineDash([2, 4]);
  ctx.beginPath();

  // Horizontal — every 25% of height (4 lines)
  for (let i = 1; i < 4; i++) {
    const y = Math.round(H * i / 4) + 0.5;
    ctx.moveTo(0, y); ctx.lineTo(W, y);
  }

  // Vertical — kHz offset grid (every 5kHz at a 25kHz span)
  const span = lastMeta.spanHz || 25000;
  const step = span <= 10000 ? 1000 :
               span <= 25000 ? 5000 :
               span <= 50000 ? 10000 : 25000;
  const center = lastMeta.centerHz || 0;
  const lo     = lastMeta.loHz || (center - span/2);
  for (let f = Math.ceil(lo/step)*step; f <= (lo+span); f += step) {
    const x = Math.round((f - lo) / span * W) + 0.5;
    if (Math.abs(f - center) < step * 0.1) continue; // skip the center (has its own marker)
    ctx.moveTo(x, 0); ctx.lineTo(x, H);
  }

  ctx.stroke();
  ctx.setLineDash([]);
}

// ── Filter overlay ────────────────────────────────────────────────────────────
function drawFilterOverlay(ctx, W, H) {
  const span = lastMeta.spanHz || 25000;
  const filt = lastMeta.filterHz || 0;
  if (!filt || !span) return;

  const filtPx = Math.max(2, (filt / span) * W);
  const cx = W / 2;

  // Brighter than before, closer to the IC-7300
  ctx.fillStyle = 'rgba(100,180,130,0.10)';
  ctx.fillRect(cx - filtPx/2, 0, filtPx, H);

  ctx.strokeStyle = 'rgba(100,200,140,0.35)';
  ctx.lineWidth = 1;
  ctx.setLineDash([]);
  ctx.beginPath();
  ctx.moveTo(cx - filtPx/2, 0); ctx.lineTo(cx - filtPx/2, H);
  ctx.moveTo(cx + filtPx/2, 0); ctx.lineTo(cx + filtPx/2, H);
  ctx.stroke();
}

// ── Frequency axis — kHz offsets relative to the VFO (like the IC-7300) ──────
function drawAxis() {
  if (!axisCtx) return;
  const W = axisCanvas.width, H = axisCanvas.height;

  axisCtx.fillStyle = '#040607';
  axisCtx.fillRect(0, 0, W, H);

  const span   = lastMeta.spanHz || 25000;
  const center = lastMeta.centerHz || 14200000;
  const lo     = lastMeta.loHz || (center - span/2);

  axisCtx.font = '13px Share Tech Mono,monospace';
  axisCtx.lineWidth = 1;

  // Vertical grid — the same lines as in the spectrum
  const step = span <= 10000 ? 1000 :
               span <= 25000 ? 5000 :
               span <= 50000 ? 10000 : 25000;

  for (let f = Math.ceil(lo/step)*step; f <= (lo+span); f += step) {
    const x = Math.round((f - lo) / span * W);
    const offsetKhz = Math.round((f - center) / 1000);
    const isCtr = Math.abs(f - center) < step * 0.1;

    // Tick
    axisCtx.strokeStyle = isCtr ? 'rgba(255,220,0,0.7)' : 'rgba(100,160,130,0.35)';
    axisCtx.beginPath();
    axisCtx.moveTo(x + 0.5, 0); axisCtx.lineTo(x + 0.5, isCtr ? 7 : 5);
    axisCtx.stroke();

    // Label
    const label = isCtr ? '0' : (offsetKhz > 0 ? '+'+offsetKhz : String(offsetKhz));
    axisCtx.textAlign = 'center';
    axisCtx.fillStyle = isCtr ? 'rgba(255,220,0,0.95)' : 'rgba(160,210,180,0.85)';
    axisCtx.fillText(label, x, H - 2);
  }

  // Mode + filter in the top-left corner
  if (lastMeta.filterHz) {
    axisCtx.textAlign = 'left';
    axisCtx.fillStyle = 'rgba(100,180,130,0.5)';
    axisCtx.font = '11px Share Tech Mono,monospace';
    const modeLabel = (lastMeta.mode || '') + (lastMeta.dataMode ? '-D' : '');
    axisCtx.fillText(`${modeLabel} ${lastMeta.filterHz}Hz`, 2, H - 1);
  }
}

// ── Simulation / audio FFT ────────────────────────────────────────────────────
function startAudioFFT() {
  if (!window.AudioContext && !window.webkitAudioContext) return;
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  audioAnalyser = ctx.createAnalyser();
  audioAnalyser.fftSize = 1024;
  audioAnalyser.smoothingTimeConstant = 0.7;
  source = 'audio';
  const draw = () => {
    animFrame = requestAnimationFrame(draw);
    if (!audioAnalyser) return;
    const buf = new Uint8Array(audioAnalyser.frequencyBinCount);
    audioAnalyser.getByteFrequencyData(buf);
    drawFrame(Array.from(buf));
  };
  draw();
}

function start() {
  let lastFrame = Date.now();
  drawAxis();
  const tick = () => {
    animFrame = requestAnimationFrame(tick);
    if (Date.now() - lastFrame < 80) return;
    lastFrame = Date.now();
    if (!lastScopeData) {
      const W = specCanvas?.width || 600;
      // Simulation: noise floor + random "signals"
      const sim = new Array(W).fill(0).map((_, i) => {
        let v = Math.floor(Math.random() * 18 + 8); // background noise
        // a few "signals" at fixed positions
        const cx = W / 2;
        [cx-80, cx-20, cx, cx+35, cx+110].forEach(sx => {
          const d = Math.abs(i - sx);
          if (d < 6) v = Math.min(255, v + Math.floor((160 + Math.random()*40) * Math.exp(-d*0.4)));
        });
        return v;
      });
      drawFrame(sim);
    }
  };
  tick();
}

function stop() {
  if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
}

function updateSourceBadge() {
  const el = document.getElementById('wf-source-badge');
  if (!el) return;
  if (source === 'off') {
    el.textContent = '○ RADIO WYŁĄCZONE';
    el.style.color = 'var(--red)';
    return;
  }
  el.textContent = source === 'radio' ? '● IC-7300 SCOPE' :
                   source === 'audio' ? '● AUDIO FFT' : '● SYMULACJA';
  el.style.color = source === 'radio' ? 'var(--green)' :
                   source === 'audio' ? 'var(--amber)' : 'rgba(200,200,200,0.3)';
}

// Radio turned off by the user (scope_reset broadcast). We clear the
// waterfall and show the OFF state instead of a misleading "SIMULATION"
// (the user had the impression the waterfall was "in test mode"). When
// the radio comes back (power ON + confirmation), the backend starts
// sending scope_frame source=radio again -> the badge goes back.
function onPowerReset() {
  source = 'off';
  updateSourceBadge();
  // Clear the canvas (black)
  const canvas = document.getElementById('waterfall-canvas') ||
                 document.querySelector('.tile-wf canvas');
  if (canvas) {
    const ctx = canvas.getContext('2d');
    if (ctx) { ctx.fillStyle = '#000'; ctx.fillRect(0, 0, canvas.width, canvas.height); }
  }
}

async function startScope(port, civAddr) {
  try {
    const r   = await fetch('/api/scope/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ port, civAddr }),
    });
    const res = await r.json();
    window.UI?.showToast(res.sim
      ? '⚠ Scope: symulacja (sprawdź baud 115200)'
      : '✓ Scope: połączono z radiem');
    updateSourceBadge();
    return res;
  } catch(e) { window.UI?.showToast('✗ Błąd uruchamiania scope', 'error'); }
}

// Change the waterfall span (Hz) — sends it to the server, which sets it on the radio.
// The IC-7300 supports: 2500, 5000, 10000, 25000, 50000, 100000, 250000, 500000 Hz.
async function setSpan(spanHz) {
  try {
    const token = localStorage.getItem('token');
    const r = await fetch('/api/scope/span', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' },
        token ? { 'Authorization': `Bearer ${token}` } : {}),
      body: JSON.stringify({ span_hz: spanHz }),
    });
    const data = await r.json();
    if (data.ok) {
      window.UI?.showToast?.(`Span waterfall: ${spanHz/1000}kHz`, 'info');
      // Reset peak-hold on span change (different scale)
      _peakHold = null;
    } else {
      window.UI?.showToast?.('Blad zmiany spanu: ' + (data.error || ''), 'error');
    }
  } catch(e) {
    console.warn('[wf] setSpan error:', e);
  }
}

// Enable/disable peak-hold
function setPeakHold(enabled) {
  if (!enabled) {
    _peakHold = null; // disabled - it will keep drawing anyway
    _peakEnabled = false;
  } else {
    _peakEnabled = true;
  }
}

let _peakEnabled = true;

// Fullscreen waterfall — native Fullscreen API on tile-wf.
// The F key also toggles it (handled by the listener in ui.js).
function toggleFullscreen() {
  const el = document.querySelector('.tile-wf');
  if (!el) return;
  if (!document.fullscreenElement) {
    el.classList.add('wf-fullscreen');
    if (el.requestFullscreen) el.requestFullscreen().catch(e => console.warn('[wf] fs error:', e));
    else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
  } else {
    if (document.exitFullscreen) document.exitFullscreen();
    else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
    el.classList.remove('wf-fullscreen');
  }
}

// Listen for fullscreen changes to clear the class when the user presses Esc
document.addEventListener('fullscreenchange', () => {
  const el = document.querySelector('.tile-wf');
  if (!document.fullscreenElement && el) el.classList.remove('wf-fullscreen');
  // Force a canvas resize on fullscreen change
  if (typeof resize === 'function') resize();
});

window.Waterfall = { init, handleScopeData, handleFilterWidth, startScope, updateSourceBadge, stop,
                     setSpan, setPeakHold, toggleFullscreen, onPowerReset };
document.addEventListener('DOMContentLoaded', () => setTimeout(init, 300));

})();
