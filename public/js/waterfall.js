/**
 * waterfall.js — waterfall/spectrum panel (Icom IC-7300 / IC-7610 style)
 * - Deep navy noise floor, high contrast signals (cyan→green→yellow→red/white)
 * - Horizontal dBm grid with markings (-10dB, -20dB, ...)
 * - Mode-aware filter overlay (USB / LSB / CW passband shading) + red carrier VFO line
 * - Persistent DX spot flags pinned above spectrum (callsign + mode + needle) —
 *   sourced ONLY from the real DX Cluster (see getVisibleSpots below); no
 *   fake/simulated callsigns are ever shown as clickable flags, since
 *   clicking one really retunes the radio (see tuneToClick).
 * - Simulated spectrum shape (noise + generic CW/SSB-looking bumps) only
 *   when there's no real scope data at all (radio not connected yet) -
 *   decorative only, never labeled with a callsign or clickable.
 * - Icom-style touch softkeys: CENT/FIX, SPAN, PEAK, SPOTS, SPEED, REF LEVEL
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
  centerHz: 14205000, spanHz: 25000, loHz: 14192500, hiHz: 14217500,
  mode: 'USB', dataMode: false, filterHz: 2400, scopeMode: 0, outOfRange: false,
};

let _refLevelDb = -15; // Icom REF LEVEL (-30 to 0 dB) — cosmetic only, see cycleRefLevel()
let _speedMultiplier = 1;
let _shiftAccum = 0; // fractional waterfall-scroll accumulator, see drawFrame()
let _spotsEnabled = true;
let _peakEnabled = true;
let _peakHold = null;
let _peakDecay = 0.985;
let _simTick = 0;
let collapsed = false;

// ── Simulated spectrum shape (decorative only — see file header) ─────────────
// Used ONLY to shape the fake noise+signal curve shown before any real scope
// data has arrived. These are NOT shown as DX spot flags and are NOT
// clickable - getVisibleSpots() (below) returns ONLY real DX Cluster spots.
const _simStations = [
  {
    mode: 'CW', offsetKhz: -11, type: 'cw',
    morsePattern: [1,0,1,0,3,0,1,0, 0,0, 3,0,1,0,3,0, 0,0,0, 1,0,1,0,1],
    morseStep: 0, rfActive: true
  },
  {
    mode: 'CW', offsetKhz: -4.5, type: 'cw',
    morsePattern: [3,0,3,0,1,0,3,0, 0,0, 3,0,1,0,3],
    morseStep: 0, rfActive: true
  },
  {
    mode: 'SSB', offsetKhz: 1.2, type: 'ssb',
    speechTimer: 0, currentLevel: 0.8, rfActive: true
  },
  {
    mode: 'SSB', offsetKhz: 19, type: 'ssb',
    speechTimer: 10, currentLevel: 0.6, rfActive: true
  }
];

let _simAnchorFreq = 0;
function ensureSimAnchors(center) {
  if (!center) center = 14205000;
  // If center moved by more than 100 kHz (band change), re-anchor simulation stations
  if (!_simAnchorFreq || Math.abs(center - _simAnchorFreq) > 100000) {
    _simAnchorFreq = center;
    for (let st of _simStations) {
      st.fixedFreqHz = Math.round(center + st.offsetKhz * 1000);
    }
  }
}

// ── IC-7300 palette: deep dark navy → blue → cyan → yellow → red → white ────
const PALETTE = (() => {
  const p = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let r = 0, g = 0, b = 0;
    if (i < 30) {
      // Dark navy noise floor
      b = Math.round(i * 2.5);
    } else if (i < 75) {
      // Navy → cobalt blue
      r = 0;
      g = Math.round((i - 30) * 1.8);
      b = 100 + Math.round((i - 30) * 3);
    } else if (i < 130) {
      // Blue → electric cyan
      r = 0;
      g = 180 + Math.round((i - 75) * 1.3);
      b = 255;
    } else if (i < 185) {
      // Cyan → emerald green → yellow
      r = Math.round((i - 130) * 4.6);
      g = 255;
      b = Math.max(0, 255 - Math.round((i - 130) * 4.6));
    } else if (i < 235) {
      // Yellow → orange → bright red
      r = 255;
      g = Math.max(0, 255 - Math.round((i - 185) * 5.1));
      b = 0;
    } else {
      // Peak saturation: red → pure white
      r = 255;
      g = Math.round((i - 235) * 12);
      b = Math.round((i - 235) * 12);
    }
    p[i] = (255 << 24) | (b << 16) | (g << 8) | r; // ABGR
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
    wfCanvas.parentElement?.insertBefore(axisCanvas, wfCanvas);
  }

  resize();
  specCtx = specCanvas.getContext('2d');
  wfCtx   = wfCanvas.getContext('2d');
  axisCtx = axisCanvas.getContext('2d');

  attachClickToTune();
  window.addEventListener('resize', resize);

  // ResizeObserver on .tile-wf guarantees instantaneous responsive sizing
  // on initial page load and whenever layout changes (e.g. fullscreen)
  const roTarget = document.querySelector('.tile-wf');
  if (roTarget && window.ResizeObserver) {
    const ro = new ResizeObserver(() => {
      resize();
    });
    ro.observe(roTarget);
  }

  _updateSoftkeys();
  drawAxis();
  start();
}

// FIX (2026-10-17, live report: "wodospad dochodzi do konca okna,
// mrygniecie, zaczyna od gory, w kolko" - caught live via diagnostic
// logging). Root cause confirmed via console capture: a fractional
// devicePixelRatio (1.75 - 175% Windows display scaling) combined with
// the fractional CSS transform:scale() this app uses for its responsive
// layout (#app-scale) makes the browser's sub-pixel layout measurements
// unstable across repaints - clientHeight of the SAME, unchanged element
// flip-flopped by +-2px (618/620) every few seconds with no real resize
// happening (confirmed via a ResizeObserver stack trace + logged
// devicePixelRatio). Can't fix the browser's rounding, but setting
// canvas.width/height unconditionally CLEARS the canvas (HTML spec) - so
// every one of those +-2px jitters was wiping the waterfall, which reads
// exactly like "reached the bottom and restarted" ("bardziej widoczne
// przy zmniejszonym oknie" - more visible on a short canvas because the
// same lost scroll history is a bigger fraction of what's visible).
// Fix: a tolerance band - don't touch (and don't clear) the canvas for a
// sub-pixel-jitter-sized change. A REAL resize (window drag, tab switch,
// fullscreen) is always tens/hundreds of px, so this never masks one.
const _RESIZE_JITTER_TOLERANCE_PX = 4;

function resize() {
  [specCanvas, axisCanvas, wfCanvas].forEach(c => {
    if (!c) return;
    const W = c.clientWidth || c.offsetWidth || c.parentElement?.clientWidth || 600;
    const H = c.clientHeight || c.offsetHeight ||
              (c.id === 'wf-spectrum' ? 80 : c.id === 'wf-axis' ? 20 : 120);
    if (Math.abs(c.width - W) > _RESIZE_JITTER_TOLERANCE_PX ||
        Math.abs(c.height - H) > _RESIZE_JITTER_TOLERANCE_PX) {
      c.width = W;
      c.height = H;
    }
  });
  drawAxis();
}

// ── Click-to-tune & Spot Snapping ─────────────────────────────────────────────
function freqFromX(x, W) {
  const { loHz, hiHz } = lastMeta;
  if (loHz == null || hiHz == null) return null;
  return Math.round(loHz + (x / W) * (hiHz - loHz));
}

// Find if coordinates (x, y) hit a DX spot badge or its frequency needle
function findSpotAt(x, y, W, canvas) {
  if (!_spotsEnabled) return null;
  const spots = getVisibleSpots(W);
  if (!spots || !spots.length) return null;

  let bestSpot = null;
  let bestDist = Infinity;

  for (let s of spots) {
    const boxLeft = s.x - _FLAG_HALF_W;
    const boxRight = s.x + _FLAG_HALF_W;
    const dist = Math.abs(x - s.x);
    // Badge's own row-adjusted y-range (see _assignFlagRows/drawSpotFlags) -
    // without this, a click in row 0's vertical slot could "hit" a row-1
    // flag sitting at a nearby x instead, since both used to share one
    // blanket y<=34 test regardless of which row was actually drawn there.
    const sy = 18 + (s.row || 0) * _FLAG_ROW_H;

    // 1. Clicked inside the flag box at the top of the spectrum
    if (canvas === specCanvas && y >= sy - 16 && y <= sy + 5 && x >= boxLeft && x <= boxRight) {
      if (dist < bestDist) {
        bestDist = dist;
        bestSpot = s;
      }
    } else if (dist <= 8) {
      // 2. Clicked on or near the vertical frequency needle line (spectrum, axis, or waterfall)
      if (dist < bestDist) {
        bestDist = dist;
        bestSpot = s;
      }
    }
  }
  return bestSpot;
}

function tuneToClick(e, canvas) {
  const rect = canvas.getBoundingClientRect();
  const x = (e.clientX - rect.left) * (canvas.width / rect.width);
  const y = (e.clientY - rect.top) * (canvas.height / rect.height);

  // If clicked on a REAL DX-cluster spot's badge or needle, QSY exactly to
  // that station's frequency AND mode - same path as the DX CLUSTER tab's
  // own "QSY" button (DXCluster.qsy), not a bare freq-only WS message, so
  // the sideband/CW mode switch and the "can this user control the radio
  // right now" messaging match everywhere QSY happens in the app.
  const spot = findSpotAt(x, y, canvas.width, canvas);
  if (spot && spot.freqHz) {
    if (typeof window.DXCluster?.qsy === 'function') {
      window.DXCluster.qsy(spot.freqHz, spot.mode, spot.call);
    } else {
      if (S) S.freq = spot.freqHz;
      window.UI?.updateFreqDisplay?.();
      window.VFO?.updateVFODisplay?.();
      window.WS?.send({ type: 'freq', freq: spot.freqHz });
    }
    window.UI?.showToast?.(`QSY: ${spot.call} (${(spot.freqHz / 1000).toFixed(1)} kHz)`, 'info');
    return;
  }

  const freq = freqFromX(x, canvas.width);
  if (freq == null) return;
  if (S) S.freq = freq;
  window.UI?.updateFreqDisplay?.();
  window.VFO?.updateVFODisplay?.();
  window.WS?.send({ type: 'freq', freq });
}

function attachClickToTune() {
  [specCanvas, axisCanvas, wfCanvas].forEach(c => {
    if (!c) return;
    c.style.cursor = 'crosshair';
    let lastPointerTs = 0;

    c.addEventListener('mousemove', (e) => {
      const rect = c.getBoundingClientRect();
      const x = (e.clientX - rect.left) * (c.width / rect.width);
      const y = (e.clientY - rect.top) * (c.height / rect.height);
      const spot = findSpotAt(x, y, c.width, c);
      if (spot && spot.freqHz) {
        c.style.cursor = 'pointer';
        c.title = `QSY → ${spot.call} [${spot.mode}] ${(spot.freqHz / 1000).toFixed(1)} kHz`;
      } else {
        c.style.cursor = 'crosshair';
        const freq = freqFromX(x, c.width);
        c.title = freq ? `VFO → ${(freq / 1000).toFixed(1)} kHz` : '';
      }
    });

    c.addEventListener('pointerdown', (e) => {
      if (e.button !== undefined && e.button !== 0) return;
      lastPointerTs = e.timeStamp;
      tuneToClick(e, c);
    });

    c.addEventListener('click', (e) => {
      if (e.timeStamp - lastPointerTs < 350) return;
      tuneToClick(e, c);
    });
  });
}

// ── Data from WS ──────────────────────────────────────────────────────────────
function handleScopeData(msg) {
  if (msg.type !== 'scope_frame' && msg.type !== 'scope_data') return;
  lastScopeData = msg.data;
  const newSource = msg.source === 'radio' ? 'radio' : 'sim';
  // FOUND 2026-10-17: real scope frames resuming after a power-on correctly
  // updated `source` back to 'radio', but nothing ever re-painted
  // #wf-source-badge for it - onPowerReset() is the only other place that
  // calls updateSourceBadge(), and that only fires on power OFF. Left the
  // badge stuck on "RADIO WYLACZONE" even once frames were flowing again.
  // Change-detected like scopeMode below, to avoid repainting the DOM on
  // every single frame (~15/s).
  if (source !== newSource) {
    source = newSource;
    updateSourceBadge();
  }
  if (msg.centerHz != null) lastMeta.centerHz = msg.centerHz;
  if (msg.spanHz   != null) lastMeta.spanHz   = msg.spanHz;
  if (msg.loHz     != null) lastMeta.loHz     = msg.loHz;
  if (msg.hiHz     != null) lastMeta.hiHz     = msg.hiHz;
  if (msg.mode     != null) lastMeta.mode     = msg.mode;
  if (msg.dataMode != null) lastMeta.dataMode = msg.dataMode;
  if (msg.filterHz != null) lastMeta.filterHz = msg.filterHz;
  if (msg.scopeMode!= null) {
    // Radio raportuje swoj REALNY tryb w kazdej ramce - synchronizujemy
    // przycisk CENT/FIX z tym (np. gdyby ktos przelaczyl tryb bezposrednio
    // na radiu), ale odswiezamy DOM tylko przy faktycznej zmianie, zeby
    // nie robic tego 20x/s na kazdej klatce wodospadu.
    if (lastMeta.scopeMode !== msg.scopeMode) {
      lastMeta.scopeMode = msg.scopeMode;
      _updateSoftkeys();
    }
  }
  if (msg.outOfRange!=null) lastMeta.outOfRange = msg.outOfRange;

  if (collapsed) return;
  drawFrame(msg.data);
  drawAxis();
}

function handleFilterWidth(msg) {
  if (msg.type !== 'filter_width') return;
  lastMeta.filterHz = msg.hz;
  if (msg.mode) lastMeta.mode = msg.mode;
  if (msg.dataMode != null) lastMeta.dataMode = msg.dataMode;
  if (collapsed) return;
  drawAxis();
}

// ── Get Visible Station Spots (REAL DX Cluster only — see file header) ───────
// Tryby cyfrowe - te NIGDY nie pokazuja flag na wodospadzie radia (maja
// wlasne flagi CQ na wodospadzie FT8, patrz wsjtx_scope.js). Ta sama lista
// trybow co w dxcluster.js (_spotModeToRigMode -> DIGI), zeby obie strony
// kodu zgadzaly sie co do tego co jest "cyfrowe".
const _SPOT_DIGI_MODES = new Set(['FT8', 'FT4', 'RTTY', 'PSK', 'PSK31', 'PSK63',
  'JT65', 'JT9', 'DIGI', 'DATA', 'MFSK', 'OLIVIA', 'JS8', 'WSPR', 'MSK144', 'Q65']);

// Flaga znika jesli korespondent nie zostal ponownie zaspotowany przez ten
// czas - zgloszone na zywo: "flagi z dx clustra pala sie zbyt krotko...
// jesli nie przyjdzie wiecej info z clustra to znikaja", chodzilo o to ze
// znikaly dużo szybciej niz oczekiwane kilka minut (patrz tez bump
// _max_history w dxcluster.py - powod byl czesciowo tam, wypychanie starych
// spotow z bufora przez zalew spotow cyfrowych).
const SPOT_FLAG_TTL_S = 180;

// Vertical stacking for flags that land close together in frequency (e.g. a
// pile-up) - without this, overlapping badges become unreadable and the
// hit-test in findSpotAt could only ever reach whichever one was "closest",
// making the others unclickable. _FLAG_HALF_W mirrors the generous hit-box
// half-width findSpotAt already used (real badge width varies with callsign
// length, but this is just for deciding when two flags are "too close",
// not for drawing - close enough). Past _FLAG_MAX_ROWS deep in one pile-up
// flags start overlapping again rather than growing the stack forever.
const _FLAG_HALF_W  = 42;
const _FLAG_ROW_H   = 20;
const _FLAG_MAX_ROWS = 3;

function _assignFlagRows(matched) {
  matched.sort((a, b) => a.x - b.x);
  const rowRightEdge = [];
  for (const sp of matched) {
    let row = 0;
    while (row < rowRightEdge.length && rowRightEdge[row] > sp.x - _FLAG_HALF_W) row++;
    if (row >= _FLAG_MAX_ROWS) row = _FLAG_MAX_ROWS - 1;
    rowRightEdge[row] = sp.x + _FLAG_HALF_W;
    sp.row = row;
  }
  return matched;
}

function getVisibleSpots(W) {
  const span   = lastMeta.spanHz || 20000;
  const center = lastMeta.centerHz || (S?.freq || 14205000);
  const lo     = lastMeta.loHz || (center - span / 2);
  const hi     = lastMeta.hiHz || (center + span / 2);

  const isAllowedMode = (m) => {
    // Brak informacji o trybie - wiekszosc spotow bez trybu to telefonia,
    // wiec nie ukrywamy (lepiej pokazac niepewny niz zgubic prawdziwy SSB).
    if (!m) return true;
    const up = String(m).toUpperCase().trim();
    if (_SPOT_DIGI_MODES.has(up)) return false;
    for (const d of _SPOT_DIGI_MODES) { if (up.includes(d)) return false; }
    return true; // CW, SSB, USB, LSB, PHONE, AM, FM...
  };

  // Real DX Cluster spots only - CW i SSB (telefonia), bez trybow cyfrowych
  // (FT8 ma wlasne flagi CQ na wodospadzie FT8, patrz wsjtx_scope.js), i
  // tylko z ostatnich SPOT_FLAG_TTL_S sekund. No fallback to anything fake:
  // an empty result here means no flags are drawn, which is correct - every
  // flag on screen must be a real, clickable, QSY-able station.
  const realSpots = window.DXCluster?.getSpots?.() || [];
  const nowS = Date.now() / 1000;
  const matched = [];

  for (let s of realSpots) {
    if (!isAllowedMode(s.mode)) continue;
    if (s.ts && (nowS - s.ts) > SPOT_FLAG_TTL_S) continue;
    if (s.freq_hz >= lo && s.freq_hz <= hi) {
      const xNorm = (s.freq_hz - lo) / span;
      matched.push({
        call: s.dx_call || s.call,
        mode: s.mode || 'SSB',
        freqHz: s.freq_hz,
        x: Math.round(xNorm * W),
        badgeColor: (String(s.mode).toUpperCase() === 'CW') ? '#fbbf24' : '#38bdf8',
        active: true,
        row: 0,
      });
    }
  }
  return _assignFlagRows(matched);
}

// ── Draw Spectrum Scope Frame ────────────────────────────────────────────────
function drawFrame(data) {
  if (!specCtx || !wfCtx || !data?.length) return;
  const W  = specCanvas.width, Hs = specCanvas.height;
  const Ww = wfCanvas.width,   Hw = wfCanvas.height;

  // Update peak-hold
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
  specCtx.fillStyle = '#020407';
  specCtx.fillRect(0, 0, W, Hs);

  // Horizontal dBm grid (Icom IC-7300 style)
  drawSpecGrid(specCtx, W, Hs);

  // Filter passband overlay (USB / LSB / CW)
  drawFilterOverlay(specCtx, W, Hs);

  // Gradient under spectrum curve
  const grad = specCtx.createLinearGradient(0, 0, 0, Hs);
  grad.addColorStop(0,   'rgba(34, 197, 94, 0.42)');
  grad.addColorStop(0.5, 'rgba(34, 197, 94, 0.12)');
  grad.addColorStop(1,   'rgba(5, 30, 15, 0.01)');

  specCtx.beginPath();
  specCtx.moveTo(0, Hs);
  for (let i = 0; i < W; i++) {
    const v = interpolate(data, i) / 255;
    const y = Hs - v * (Hs - 8) - 4;
    specCtx.lineTo(i, y);
  }
  specCtx.lineTo(W, Hs);
  specCtx.closePath();
  specCtx.fillStyle = grad;
  specCtx.fill();

  // Spectrum line — bright Icom emerald green
  specCtx.beginPath();
  specCtx.strokeStyle = '#22c55e';
  specCtx.lineWidth   = 1.8;
  for (let i = 0; i < W; i++) {
    const v = interpolate(data, i) / 255;
    const y = Hs - v * (Hs - 8) - 4;
    i === 0 ? specCtx.moveTo(i, y) : specCtx.lineTo(i, y);
  }
  specCtx.stroke();

  // Peak-hold line (amber/gold)
  if (_peakEnabled && _peakHold) {
    specCtx.beginPath();
    specCtx.strokeStyle = 'rgba(251, 191, 36, 0.75)';
    specCtx.lineWidth   = 1.2;
    for (let i = 0; i < W; i++) {
      const v = interpolate(_peakHold, i) / 255;
      const y = Hs - v * (Hs - 8) - 4;
      i === 0 ? specCtx.moveTo(i, y) : specCtx.lineTo(i, y);
    }
    specCtx.stroke();
  }

  // ── Persistent Station Flags (DX Spots) ──────────────────────────────────
  if (_spotsEnabled) {
    drawSpotFlags(specCtx, W, Hs);
  }

  // Red center carrier VFO line
  const centerPx = W / 2;
  specCtx.strokeStyle = '#f43f5e';
  specCtx.lineWidth   = 1.5;
  specCtx.beginPath();
  specCtx.moveTo(centerPx, 0);
  specCtx.lineTo(centerPx, Hs);
  specCtx.stroke();

  // Source badge
  specCtx.font = '9px "JetBrains Mono", monospace';
  specCtx.fillStyle = source === 'radio' ? 'rgba(34,197,94,0.7)' : 'rgba(200,160,60,0.45)';
  specCtx.textAlign = 'right';
  specCtx.fillText(source === 'radio' ? '● IC-7300 SCOPE' : '● SIM', W - 6, 12);

  if (lastMeta.outOfRange) {
    specCtx.fillStyle = 'rgba(244,63,94,0.85)';
    specCtx.textAlign = 'left';
    specCtx.fillText('OUT OF RANGE', 6, 12);
  }

  // ── WATERFALL ───────────────────────────────────────────────────────────────
  // Fractional scroll accumulator: _speedMultiplier can be < 1 (SLOW), and a
  // real waterfall can't shift by a fractional pixel - accumulate it across
  // frames instead, and on a frame where the accumulated amount is still
  // under 1px, skip the scroll+new-row draw entirely rather than forcing a
  // minimum 1px/frame (which used to make SLOW indistinguishable from MID).
  _shiftAccum += _speedMultiplier;
  let shift = Math.floor(_shiftAccum);
  if (shift > 0) {
    _shiftAccum -= shift;
    // SAFETY: if shift ever reached/exceeded the canvas height, Hw-shift
    // would be <= 0 and this drawImage call corrupts/throws. Shouldn't
    // happen at the intended ~66ms frame pacing (max ~2px/frame even at
    // FAST), but clamp defensively instead of silently corrupting the
    // canvas if it ever does (e.g. a tab backgrounded/throttled for a
    // while then catching up on a backlog of frames at once).
    if (shift >= Hw) shift = Hw - 1;
    wfCtx.drawImage(wfCanvas, 0, 0, Ww, Hw - shift, 0, shift, Ww, Hw - shift);

    const lineImg = wfCtx.createImageData(Ww, 1);
    const d32     = new Uint32Array(lineImg.data.buffer);
    const scale   = data.length / Ww;

    for (let i = 0; i < Ww; i++) {
      const src = i * scale;
      const i0 = Math.floor(src);
      const i1 = Math.min(data.length - 1, i0 + 1);
      const frac = src - i0;
      let rawVal = (data[i0] || 0) * (1 - frac) + (data[i1] || 0) * frac;

      // Apply REF LEVEL scaling for signal contrast. FIX (2026-10-17, live
      // report: "bardzo silne sygnaly widac ale slabe juz gina w tle"):
      // the old formula was pure linear ((v-floor)*1.5), which gave a weak
      // signal just above the noise floor almost no separation from it -
      // e.g. a signal only 15% above floor mapped to palette index ~7,
      // still deep in PALETTE's own dark-navy "noise floor" band (0-29) -
      // while anything reasonably strong already saturated to white by
      // ~77% raw. A gamma/sqrt-like curve (exponent <1) applied to the
      // above-floor portion boosts WEAK signals much more than strong ones
      // (standard "waterfall gain" technique in real SDR software), so a
      // signal barely above the floor now lands solidly in the visible
      // blue zone instead of disappearing into the background, without
      // blowing out strong signals sooner than before.
      const above = Math.max(0, rawVal / 255.0 - (0.10 + (_refLevelDb + 15) * 0.005));
      const norm = Math.pow(above, 0.6) * 1.05;
      const mapped = Math.min(255, Math.max(0, Math.round(norm * 255)));
      d32[i] = PALETTE[mapped];
    }
    wfCtx.putImageData(lineImg, 0, 0);
  }

  // VFO center marker tick on waterfall
  wfCtx.strokeStyle = 'rgba(244,63,94,0.6)';
  wfCtx.lineWidth   = 1;
  wfCtx.beginPath();
  wfCtx.moveTo(Ww/2, 0); wfCtx.lineTo(Ww/2, 5);
  wfCtx.stroke();
}

// ── Draw Persistent DX Spot Flags ────────────────────────────────────────────
function drawSpotFlags(ctx, W, Hs) {
  const spots = getVisibleSpots(W);

  for (let st of spots) {
    const sx = st.x;
    const sy = 18 + (st.row || 0) * _FLAG_ROW_H;

    // Vertical dashed frequency needle
    ctx.strokeStyle = st.badgeColor;
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 2]);
    ctx.beginPath();
    ctx.moveTo(sx, sy + 7);
    ctx.lineTo(sx, Hs);
    ctx.stroke();
    ctx.setLineDash([]);

    // Flag badge
    const labelText = st.call;
    const subText   = st.mode;
    ctx.font = 'bold 9.5px "JetBrains Mono", monospace';
    const txtWidth = ctx.measureText(labelText).width;
    const boxW = txtWidth + 28;
    const boxH = 17;
    const boxX = sx - (boxW / 2);
    const boxY = sy - 14;

    // Badge background & border
    ctx.fillStyle = '#090d16';
    ctx.strokeStyle = st.badgeColor;
    ctx.lineWidth = 1.4;
    ctx.beginPath();
    ctx.roundRect(boxX, boxY, boxW, boxH, 3);
    ctx.fill();
    ctx.stroke();

    // RF Activity Dot
    ctx.fillStyle = st.active ? '#22c55e' : 'rgba(148,163,184,0.35)';
    ctx.beginPath();
    ctx.arc(boxX + 7, boxY + 8.5, 3, 0, Math.PI * 2);
    ctx.fill();

    // Callsign text
    ctx.fillStyle = '#ffffff';
    ctx.font = 'bold 9.5px "JetBrains Mono", monospace';
    ctx.textAlign = 'left';
    ctx.fillText(labelText, boxX + 13, boxY + 12);

    // Mode text
    ctx.fillStyle = st.badgeColor;
    ctx.font = '8px "JetBrains Mono", monospace';
    ctx.fillText(subText, boxX + txtWidth + 14, boxY + 12);
  }
}

// ── Horizontal dBm grid & vertical kHz offset lines ───────────────────────────
function drawSpecGrid(ctx, W, H) {
  ctx.strokeStyle = 'rgba(255,255,255,0.07)';
  ctx.lineWidth   = 1;
  ctx.setLineDash([3, 3]);

  // Horizontal dBm lines
  for (let db = 28; db < H; db += 26) {
    ctx.beginPath();
    ctx.moveTo(40, db);
    ctx.lineTo(W, db);
    ctx.stroke();

    // dB label on the left
    ctx.fillStyle = 'rgba(148, 163, 184, 0.45)';
    ctx.font = '9px "JetBrains Mono", monospace';
    const label = '-' + Math.round((db / H) * 70 - _refLevelDb) + 'dB';
    ctx.fillText(label, 6, db + 3);
  }

  // Vertical kHz grid lines
  const span = lastMeta.spanHz || 20000;
  const step = span <= 10000 ? 1000 :
               span <= 20000 ? 5000 :
               span <= 50000 ? 10000 :
               span <= 100000 ? 25000 :
               span <= 200000 ? 50000 :
               span <= 500000 ? 100000 : 200000;
  const center = lastMeta.centerHz || (S?.freq || 14205000);
  const lo     = lastMeta.loHz || (center - span/2);

  for (let f = Math.ceil(lo/step)*step; f <= (lo+span); f += step) {
    const x = Math.round((f - lo) / span * W) + 0.5;
    if (Math.abs(f - center) < step * 0.1) continue;
    ctx.beginPath();
    ctx.moveTo(x, 0); ctx.lineTo(x, H);
    ctx.stroke();
  }

  ctx.setLineDash([]);
}

// ── Mode-aware Filter Overlay (USB / LSB / CW) ────────────────────────────────
function drawFilterOverlay(ctx, W, H) {
  const span = lastMeta.spanHz || 25000;
  const filt = lastMeta.filterHz || 2400;
  const mode = (lastMeta.mode || S?.mode || 'USB').toUpperCase();
  const cx   = W / 2;
  const filtPx = Math.max(3, (filt / span) * W);

  ctx.fillStyle   = 'rgba(56, 189, 248, 0.12)';
  ctx.strokeStyle = 'rgba(56, 189, 248, 0.35)';
  ctx.lineWidth   = 1;

  if (mode.includes('USB')) {
    // USB passband to the right of carrier
    ctx.fillRect(cx, 0, filtPx, H);
    ctx.strokeRect(cx, 0, filtPx, H);
  } else if (mode.includes('LSB')) {
    // LSB passband to the left of carrier
    ctx.fillRect(cx - filtPx, 0, filtPx, H);
    ctx.strokeRect(cx - filtPx, 0, filtPx, H);
  } else {
    // CW / AM / other: centered around carrier
    ctx.fillRect(cx - filtPx/2, 0, filtPx, H);
    ctx.strokeRect(cx - filtPx/2, 0, filtPx, H);
  }
}

// ── Frequency axis — between spectrum and waterfall (Icom IC-7300 layout) ─────
function drawAxis() {
  if (!axisCtx) return;
  const W = axisCanvas.width, H = axisCanvas.height;

  axisCtx.fillStyle = '#060a12';
  axisCtx.fillRect(0, 0, W, H);

  const span   = lastMeta.spanHz || 20000;
  const center = lastMeta.centerHz || (S?.freq || 14205000);
  const lo     = lastMeta.loHz || (center - span/2);

  axisCtx.font = '10px "JetBrains Mono", monospace';
  axisCtx.lineWidth = 1;

  const step = span <= 10000 ? 1000 :
               span <= 20000 ? 5000 :
               span <= 50000 ? 10000 :
               span <= 100000 ? 25000 :
               span <= 200000 ? 50000 :
               span <= 500000 ? 100000 : 200000;

  for (let f = Math.ceil(lo/step)*step; f <= (lo+span); f += step) {
    const x = Math.round((f - lo) / span * W);
    const offsetKhz = Math.round((f - center) / 1000);
    const isCtr = Math.abs(f - center) < step * 0.1;

    // Tick
    axisCtx.strokeStyle = isCtr ? '#f43f5e' : 'rgba(255,255,255,0.2)';
    axisCtx.beginPath();
    axisCtx.moveTo(x + 0.5, 0); axisCtx.lineTo(x + 0.5, isCtr ? 7 : 5);
    axisCtx.stroke();

    // Label
    axisCtx.textAlign = 'center';
    if (isCtr) {
      axisCtx.fillStyle = '#f43f5e';
      axisCtx.font = 'bold 10px "JetBrains Mono", monospace';
      const mhz = (center / 1000000).toFixed(3);
      axisCtx.fillText(`▼ ${mhz}`, x, H - 4);
    } else {
      axisCtx.fillStyle = '#94a3b8';
      axisCtx.font = '9px "JetBrains Mono", monospace';
      const label = offsetKhz > 0 ? `+${offsetKhz}k` : `${offsetKhz}k`;
      axisCtx.fillText(label, x, H - 5);
    }
  }

  // Active mode and filter width in top left
  if (lastMeta.filterHz) {
    axisCtx.textAlign = 'left';
    axisCtx.fillStyle = 'rgba(56, 189, 248, 0.6)';
    axisCtx.font = '9px "JetBrains Mono", monospace';
    const modeLabel = (lastMeta.mode || S?.mode || 'USB') + (lastMeta.dataMode ? '-D' : '');
    axisCtx.fillText(`${modeLabel} ${lastMeta.filterHz}Hz`, 4, H - 5);
  }
}

// ── Simulated spectrum shape (decorative only, see file header) ──────────────
function updateSimStations() {
  _simTick++;

  for (let st of _simStations) {
    if (st.type === 'cw') {
      if (_simTick % 7 === 0 && st.morsePattern) {
        st.morseStep = (st.morseStep + 1) % st.morsePattern.length;
        st.rfActive = st.morsePattern[st.morseStep] > 0;
      }
    } else if (st.type === 'ssb') {
      st.speechTimer = (st.speechTimer || 0) + 0.05;
      const sentenceMod = Math.sin(st.speechTimer * 0.8) > -0.3;
      const syllableMod = Math.sin(st.speechTimer * 7.5) * 0.5 + 0.5;
      st.currentLevel = sentenceMod ? (0.35 + 0.65 * syllableMod) : 0.05;
      st.rfActive = st.currentLevel > 0.2;
    }
  }
}

function computeSimSpectrum(W) {
  const data = new Float32Array(W);
  const span = lastMeta.spanHz || 25000;
  const center = lastMeta.centerHz || (S?.freq || 14205000);
  ensureSimAnchors(center);
  const lo = lastMeta.loHz || (center - span/2);
  const noiseBase = 0.10 + (_refLevelDb + 15) * 0.005;

  for (let x = 0; x < W; x++) {
    let val = noiseBase + (Math.random() * 0.06);
    const fAtX = lo + (x / W) * span;

    for (let st of _simStations) {
      if (!st.rfActive) continue;
      const fSt = st.fixedFreqHz || (center + st.offsetKhz * 1000);
      if (st.type === 'cw') {
        const dHz = Math.abs(fAtX - fSt);
        if (dHz < 350) {
          const p = 1 - (dHz / 350);
          val += Math.pow(p, 2) * 0.88 * (0.9 + Math.random() * 0.1);
        }
      } else if (st.type === 'ssb') {
        const dHz = fAtX - fSt;
        if (dHz >= 0 && dHz < 2400) {
          const p = dHz / 2400;
          const formant = Math.sin(p * Math.PI) * (0.8 + 0.2 * Math.sin(x * 0.5 + _simTick * 0.3));
          val += formant * 0.82 * (st.currentLevel || 0.7);
        }
      }
    }

    data[x] = Math.max(0.0, Math.min(1.0, val)) * 255;
  }
  return data;
}

function start() {
  let lastFrame = Date.now();
  drawAxis();

  const tick = () => {
    animFrame = requestAnimationFrame(tick);
    if (Date.now() - lastFrame < 66) return; // ~15 FPS
    lastFrame = Date.now();

    if (!lastScopeData) {
      updateSimStations();
      const W = specCanvas?.width || 600;
      const simData = computeSimSpectrum(W);
      drawFrame(simData);
      drawAxis();
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
                   source === 'audio' ? '● AUDIO FFT' : '● SIM';
  el.style.color = source === 'radio' ? 'var(--green)' :
                   source === 'audio' ? 'var(--amber)' : 'rgba(200,200,200,0.35)';
}

function onPowerReset() {
  source = 'off';
  updateSourceBadge();
  const canvas = document.getElementById('wf-waterfall');
  if (canvas) {
    const ctx = canvas.getContext('2d');
    if (ctx) { ctx.fillStyle = '#010204'; ctx.fillRect(0, 0, canvas.width, canvas.height); }
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

// ── Softkeys & Controls API ───────────────────────────────────────────────────
async function setSpan(spanHz) {
  localStorage.setItem('wf_span_hz', String(spanHz));
  lastMeta.spanHz = spanHz;
  const center = lastMeta.centerHz || (S?.freq || 14205000);
  lastMeta.loHz = center - spanHz / 2;
  lastMeta.hiHz = center + spanHz / 2;

  const sel = document.getElementById('wf-span-select');
  if (sel) sel.value = String(spanHz);
  _updateSoftkeys();
  drawAxis();

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
      _peakHold = null;
    }
  } catch(e) {
    console.warn('[wf] setSpan error:', e);
  }
}

function setPeakHold(enabled) {
  _peakEnabled = !!enabled;
  if (!_peakEnabled) _peakHold = null;
  const chk = document.getElementById('wf-peak-toggle');
  if (chk) chk.checked = _peakEnabled;
  _updateSoftkeys();
}

// FIX (2026-10-17, live report: CENT/FIX "do konca i tak nie wiem do czego
// sluzy i czy dziala"): przycisk wczesniej TYLKO zmienial wlasny napis -
// zadna wartosc nim sterowana nie byla nigdzie czytana, wiec realnie nic
// nie robil. Teraz woła prawdziwa komende CI-V (27 14, patrz
// civ.py::set_scope_mode) i czeka na ACK/NG radia, dokladnie jak setSpan().
// Center (0x00): okno wodospadu podaza za VFO. Fixed (0x01): okno zostaje
// na stalym zakresie czestotliwosci niezaleznie od strojenia.
// Rozwijane menu (jak SPAN/SPEED/REF ponizej) zamiast przycisku-cyklu -
// wybor od razu zamiast klikania przez stany.
async function setScopeMode(target) {
  try {
    const token = localStorage.getItem('token');
    const r = await fetch('/api/scope/mode', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' },
        token ? { 'Authorization': `Bearer ${token}` } : {}),
      body: JSON.stringify({ mode: target }),
    });
    const data = await r.json();
    if (data.ok) {
      lastMeta.scopeMode = target;
      _updateSoftkeys();
      window.UI?.showToast?.(target === 0x00
        ? 'Wodospad: CENTER (podąża za VFO)'
        : 'Wodospad: FIXED (stałe okno)', 'info');
    } else {
      window.UI?.showToast?.('✗ Radio nie potwierdziło zmiany trybu wodospadu', 'error');
    }
  } catch(e) {
    console.warn('[wf] setScopeMode error:', e);
  }
}

function togglePeak() {
  setPeakHold(!_peakEnabled);
}

function toggleSpots() {
  _spotsEnabled = !_spotsEnabled;
  _updateSoftkeys();
}

function setSpeed(mult) {
  _speedMultiplier = mult;
  _updateSoftkeys();
}

function setRefLevel(db) {
  _refLevelDb = db;
  _updateSoftkeys();
  window.UI?.showToast?.(`REF LEVEL: ${db} dB`, 'info');
}

function _updateSoftkeys() {
  const btnCent = document.getElementById('wf-btn-cent');
  if (btnCent) {
    // <select> - scopeMode: 0=Center, 1=Fixed, 2/3=SCROLL (nie sterowane
    // z tego menu - traktujemy jak Fixed przy wyswietlaniu).
    btnCent.value = lastMeta.scopeMode !== 0 ? '1' : '0';
  }
  const btnSpan = document.getElementById('wf-btn-span');
  if (btnSpan) {
    // <select> teraz, nie <button> - synchronizujemy wartosc wybrana w
    // menu z aktualnym spanem (np. po przywroceniu z localStorage albo
    // po zmianie z poziomu cycleSpan/klawiatury), zamiast tekstu przycisku.
    btnSpan.value = String(lastMeta.spanHz || 20000);
  }
  const btnPeak = document.getElementById('wf-btn-peak');
  if (btnPeak) {
    btnPeak.classList.toggle('active', _peakEnabled);
    btnPeak.textContent = _peakEnabled ? 'PEAK: ON' : 'PEAK: OFF';
  }
  const btnSpots = document.getElementById('wf-btn-spots');
  if (btnSpots) {
    btnSpots.classList.toggle('active', _spotsEnabled);
    btnSpots.textContent = _spotsEnabled ? 'SPOTS: ON' : 'SPOTS: OFF';
  }
  const btnSpeed = document.getElementById('wf-btn-speed');
  if (btnSpeed) btnSpeed.value = String(_speedMultiplier);
  const btnRef = document.getElementById('wf-btn-ref');
  if (btnRef) btnRef.value = String(_refLevelDb);
}

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

document.addEventListener('fullscreenchange', () => {
  const el = document.querySelector('.tile-wf');
  if (!document.fullscreenElement && el) el.classList.remove('wf-fullscreen');
  if (typeof resize === 'function') resize();
});

function applyCollapsed() {
  const el  = document.querySelector('.tile-wf');
  const btn = document.getElementById('wf-collapse-btn');
  if (el) el.classList.toggle('wf-collapsed', collapsed);
  if (btn) btn.textContent = collapsed ? '+' : '−';
  if (collapsed) {
    stop();
  } else {
    if (!animFrame) start();
    resize();
  }
}

function toggleCollapse() {
  collapsed = !collapsed;
  localStorage.setItem('wf_collapsed', collapsed ? '1' : '0');
  applyCollapsed();
}

// ── Window Exports ────────────────────────────────────────────────────────────
window.Waterfall = {
  init, handleScopeData, handleFilterWidth, startScope, updateSourceBadge, stop,
  setSpan, setPeakHold, toggleFullscreen, toggleCollapse, onPowerReset, resize,
  setScopeMode, togglePeak, toggleSpots, setSpeed, setRefLevel
};

window.addEventListener('app:ready', () => {
  if (localStorage.getItem('wf_collapsed') === '1') {
    collapsed = true;
    applyCollapsed();
  }
});
document.addEventListener('DOMContentLoaded', () => setTimeout(init, 300));

window.addEventListener('app:ready', () => {
  const saved = parseInt(localStorage.getItem('wf_span_hz'), 10);
  if (!saved) return;
  const sel = document.getElementById('wf-span-select');
  if (sel) sel.value = String(saved);
  setSpan(saved);
});

})();
