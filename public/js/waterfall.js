/**
 * waterfall.js — panel waterfall/spektrum
 * Styl zbliżony do IC-7300: ciemne tło, zielona linia spektrum,
 * paleta niebieski→cyan→żółty→czerwony, siatka offsetów kHz,
 * wyraźny overlay filtra, oś z offsetami relative do VFO.
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

// ── Paleta IC-7300: czarny → granatowy → niebieski → cyan → żółty → czerwony ──
const PALETTE = (() => {
  const p = new Uint32Array(256);
  for (let i = 0; i < 256; i++) {
    let r, g, b;
    if (i < 40) {
      // Czarny → bardzo ciemny granatowy (szum tła)
      r = 0; g = 0; b = Math.round(i * 3.5);
    } else if (i < 90) {
      // Granatowy → niebieski
      r = 0; g = Math.round((i-40)*1.5); b = Math.min(255, 140 + Math.round((i-40)*2));
    } else if (i < 140) {
      // Niebieski → cyan
      r = 0; g = Math.min(255, 75 + Math.round((i-90)*3.6)); b = 255 - Math.round((i-90)*2);
    } else if (i < 190) {
      // Cyan → żółty
      r = Math.round((i-140)*5.1); g = 255; b = Math.max(0, 155 - Math.round((i-140)*3.1));
    } else {
      // Żółty → czerwony
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

// ── Dane z WS ─────────────────────────────────────────────────────────────────
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

// ── Rysuj ramkę ───────────────────────────────────────────────────────────────
// Peak-hold state — najwyzsza wartosc w kazdym binie ze zdefiniowanym decay.
// Reset gdy zmienia sie ilosc bins albo szerokosc canvasu.
let _peakHold = null;
let _peakDecay = 0.985; // per frame — im blizsze 1, tym wolniejszy fade

function drawFrame(data) {
  if (!specCtx || !wfCtx || !data?.length) return;
  const W  = specCanvas.width, Hs = specCanvas.height;
  const Ww = wfCanvas.width,   Hw = wfCanvas.height;

  // Aktualizuj peak-hold (dopasuj do dlugosci danych)
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

  // Interpolacja liniowa pomiedzy binami — zamiast Math.floor(i * data.length / W)
  // ktora daje "schodkowanie" gdy W < data.length. Interpolacja daje gladki
  // wykres nawet gdy piksele mapowane sa na frakcje binu.
  const interpolate = (arr, x) => {
    const scale = arr.length / W;
    const src = x * scale;
    const i0 = Math.floor(src);
    const i1 = Math.min(arr.length - 1, i0 + 1);
    const frac = src - i0;
    return (arr[i0] || 0) * (1 - frac) + (arr[i1] || 0) * frac;
  };

  // ── SPEKTRUM ──────────────────────────────────────────────────────────────
  // Tło
  specCtx.fillStyle = '#050708';
  specCtx.fillRect(0, 0, W, Hs);

  // Siatka pozioma (dB) — jak IC-7300
  drawSpecGrid(specCtx, W, Hs);

  // Overlay filtra (przed krzywą)
  drawFilterOverlay(specCtx, W, Hs);

  // Gradient pod krzywą spektrum
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

  // Linia spektrum — jasna zielona jak IC-7300
  specCtx.beginPath();
  specCtx.strokeStyle = source === 'radio' ? '#00e040' : 'rgba(0,200,60,0.5)';
  specCtx.lineWidth   = 1.5;
  specCtx.shadowBlur  = source === 'radio' ? 3 : 0;
  specCtx.shadowColor = '#00ff60';
  for (let i = 0; i < W; i++) {
    const v  = interpolate(data, i) / 255;
    const y  = Hs - v * (Hs - 4) - 2;
    i === 0 ? specCtx.moveTo(i, y) : specCtx.lineTo(i, y);
  }
  specCtx.stroke();
  specCtx.shadowBlur = 0;

  // Peak-hold — cienka pomarańczowa linia (jesli wlaczony)
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

  // Marker centrum VFO — pionowa żółta linia jak IC-7300
  specCtx.strokeStyle = 'rgba(255,220,0,0.85)';
  specCtx.lineWidth   = 1;
  specCtx.setLineDash([]);
  specCtx.beginPath();
  specCtx.moveTo(W/2, 0); specCtx.lineTo(W/2, Hs);
  specCtx.stroke();

  // Badge źródła
  specCtx.font = '9px Share Tech Mono,monospace';
  specCtx.fillStyle = source === 'radio' ? 'rgba(0,230,60,0.7)' : 'rgba(200,160,60,0.45)';
  specCtx.textAlign = 'right';
  specCtx.fillText(source === 'radio' ? '● RADIO' : '● SIM', W - 4, 11);

  if (lastMeta.outOfRange) {
    specCtx.fillStyle = 'rgba(255,80,80,0.8)';
    specCtx.textAlign = 'left';
    specCtx.fillText('OUT OF RANGE', 4, 11);
  }

  // ── WATERFALL ─────────────────────────────────────────────────────────────
  // Przesun caly waterfall o 1 piksel w dol (najstarsze linie schodza).
  // KRYTYCZNE dla latency: to leci 15x/s (scope_frame z backendu) w tym samym
  // watku JS co ping/pong (badge WS) i dekodowanie audio (_audioWs). Poprzednio
  // getImageData/putImageData alokowaly nowa tablice ~kilkaset KB PRZY KAZDEJ
  // ramce (15x/s) - okresowe pauzy GC blokowaly watek na tyle dlugo, ze pong
  // przychodzil z opoznieniem (falszywy skok "WS") a audio realnie sie
  // przycinalo (opoznione playOpusFrame/planowanie bufora). drawImage kopiuje
  // canvas na siebie samego bez zadnej alokacji widocznej z JS.
  wfCtx.drawImage(wfCanvas, 0, 0, Ww, Hw - 1, 0, 1, Ww, Hw - 1);

  // Narysuj nowa linie z INTERPOLACJA — kazdy piksel canvasa dostaje
  // interpolowana wartosc, zamiast dyskretnego "step" na Math.floor
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

  // Pionowa linia centrum na waterfalllu (żółta, krótka)
  wfCtx.strokeStyle = 'rgba(255,220,0,0.5)';
  wfCtx.lineWidth   = 1;
  wfCtx.beginPath();
  wfCtx.moveTo(Ww/2, 0); wfCtx.lineTo(Ww/2, 4);
  wfCtx.stroke();
}

// ── Siatka spektrum (poziome linie dB, pionowe linie offsetów kHz) ────────────
function drawSpecGrid(ctx, W, H) {
  ctx.strokeStyle = 'rgba(80,120,100,0.18)';
  ctx.lineWidth   = 0.5;
  ctx.setLineDash([2, 4]);
  ctx.beginPath();

  // Poziome — co 25% wysokości (4 linie)
  for (let i = 1; i < 4; i++) {
    const y = Math.round(H * i / 4) + 0.5;
    ctx.moveTo(0, y); ctx.lineTo(W, y);
  }

  // Pionowe — siatka offsetów kHz (co 5kHz przy 25kHz span)
  const span = lastMeta.spanHz || 25000;
  const step = span <= 10000 ? 1000 :
               span <= 25000 ? 5000 :
               span <= 50000 ? 10000 : 25000;
  const center = lastMeta.centerHz || 0;
  const lo     = lastMeta.loHz || (center - span/2);
  for (let f = Math.ceil(lo/step)*step; f <= (lo+span); f += step) {
    const x = Math.round((f - lo) / span * W) + 0.5;
    if (Math.abs(f - center) < step * 0.1) continue; // pomiń centrum (ma własny marker)
    ctx.moveTo(x, 0); ctx.lineTo(x, H);
  }

  ctx.stroke();
  ctx.setLineDash([]);
}

// ── Overlay filtra ─────────────────────────────────────────────────────────────
function drawFilterOverlay(ctx, W, H) {
  const span = lastMeta.spanHz || 25000;
  const filt = lastMeta.filterHz || 0;
  if (!filt || !span) return;

  const filtPx = Math.max(2, (filt / span) * W);
  const cx = W / 2;

  // Jaśniejszy niż poprzednio, bardziej jak IC-7300
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

// ── Oś częstotliwości — offsety kHz relative do VFO (jak IC-7300) ────────────
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

  // Siatka pionowa — te same linie co w spektrum
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

  // Tryb + filtr w lewym rogu
  if (lastMeta.filterHz) {
    axisCtx.textAlign = 'left';
    axisCtx.fillStyle = 'rgba(100,180,130,0.5)';
    axisCtx.font = '11px Share Tech Mono,monospace';
    const modeLabel = (lastMeta.mode || '') + (lastMeta.dataMode ? '-D' : '');
    axisCtx.fillText(`${modeLabel} ${lastMeta.filterHz}Hz`, 2, H - 1);
  }
}

// ── Symulacja / audio FFT ─────────────────────────────────────────────────────
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
      // Symulacja: noise floor + losowe "sygnały"
      const sim = new Array(W).fill(0).map((_, i) => {
        let v = Math.floor(Math.random() * 18 + 8); // szum tła
        // kilka "sygnałów" na stałych pozycjach
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

// Radio wylaczone przez usera (scope_reset broadcast). Czyscimy waterfall i
// pokazujemy stan OFF zamiast mylacej "SYMULACJI" (user mial wrazenie ze
// waterfall "w stanie testu"). Gdy radio wroci (power ON + potwierdzenie),
// backend zacznie znowu slac scope_frame source=radio -> badge wroci.
function onPowerReset() {
  source = 'off';
  updateSourceBadge();
  // Wyczysc canvas (czarny)
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

// Zmien span waterfallu (Hz) — wysyla do serwera, ktory ustawia w radiu.
// IC-7300 wspiera: 2500, 5000, 10000, 25000, 50000, 100000, 250000, 500000 Hz.
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
      window.UI?.showToast?.(`Span waterfall: ±${spanHz/1000}kHz`, 'info');
      // Reset peak-hold przy zmianie spanu (skala inna)
      _peakHold = null;
    } else {
      window.UI?.showToast?.('Blad zmiany spanu: ' + (data.error || ''), 'error');
    }
  } catch(e) {
    console.warn('[wf] setSpan blad:', e);
  }
}

// Wlacz/wylacz peak-hold
function setPeakHold(enabled) {
  if (!enabled) {
    _peakHold = null; // wylaczone - dalej beda rysowac
    _peakEnabled = false;
  } else {
    _peakEnabled = true;
  }
}

let _peakEnabled = true;

// Fullscreen waterfall — natywne Fullscreen API na tile-wf.
// F na klawiaturze rowniez toggluje (obsluga w ui.js listener).
function toggleFullscreen() {
  const el = document.querySelector('.tile-wf');
  if (!el) return;
  if (!document.fullscreenElement) {
    el.classList.add('wf-fullscreen');
    if (el.requestFullscreen) el.requestFullscreen().catch(e => console.warn('[wf] fs blad:', e));
    else if (el.webkitRequestFullscreen) el.webkitRequestFullscreen();
  } else {
    if (document.exitFullscreen) document.exitFullscreen();
    else if (document.webkitExitFullscreen) document.webkitExitFullscreen();
    el.classList.remove('wf-fullscreen');
  }
}

// Nasluchuj na zmiane fullscreen zeby wyczyscic klase gdy user wciska Esc
document.addEventListener('fullscreenchange', () => {
  const el = document.querySelector('.tile-wf');
  if (!document.fullscreenElement && el) el.classList.remove('wf-fullscreen');
  // Wymus resize canvas przy zmianie fullscreen
  if (typeof resize === 'function') resize();
});

window.Waterfall = { init, handleScopeData, handleFilterWidth, startScope, updateSourceBadge, stop,
                     setSpan, setPeakHold, toggleFullscreen, onPowerReset };
document.addEventListener('DOMContentLoaded', () => setTimeout(init, 300));

})();
