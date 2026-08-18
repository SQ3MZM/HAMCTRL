/**
 * deepcw.js — DeepCW client
 * Streams Float32 PCM over WebSocket to the server (Python+ONNX)
 */
(function () {
'use strict';

let _ctx     = null;
let _proc    = null;
let _src     = null;
let _stream  = null;
let _running = false;

async function startDecoding(source) {
  if (_running) return;
  // The decoder runs on the SERVER, on raw audio straight from the
  // radio's sound card. The browser no longer sends audio: it used to go
  // through the Opus codec, which blurred the keying edges (envelope
  // contrast 6.4x instead of >20x) and the model got a smeared signal —
  // hence garbled output despite a strong station.
  try {
    window.WS?.send({ type: 'cw_rx_enable', enabled: true });
    _running = true;
    _updateBtn(true);
    _setLog('🎧 Aktywny (audio surowe z karty radia)');
  } catch(e) {
    _setLog('✗ ' + e.message);
    console.error('[DeepCW]', e);
  }
}

function stopDecoding() {
  _running = false;
  try { window.WS?.send({ type: 'cw_rx_enable', enabled: false }); } catch(e) {}
  _updateBtn(false);
  _setLog('⏹ Zatrzymano.');
}

async function _startDecodingOLD(source) {
  if (_running) return;
  try {
    if (source === 'radio') {
      // Use the radio's existing AudioContext (via window or via _masterGain.context)
      const radioCtx = window._masterGain?.context || window.audioCtx;
      if (!radioCtx) { _setLog('✗ Brak audio radia — uruchom radio najpierw'); return; }
      _ctx = radioCtx;
      if (_ctx.state === 'suspended') { try { await _ctx.resume(); } catch(e){} }

      // Create a ScriptProcessor in the radio's context
      _proc = _ctx.createScriptProcessor(4096, 1, 1);

      // Chain _masterGain → _proc → destination
      const gain = window._masterGain || _ctx.destination;
      gain.connect(_proc);
      _proc.connect(_ctx.destination);

      const sampleRate = _ctx.sampleRate;
      _proc.onaudioprocess = (e) => {
        if (!_running) return;
        const data = e.inputBuffer.getChannelData(0);
        _sendPCM(data, sampleRate);
        _updateVU(data);
      };
    } else {
      // Microphone — its own AudioContext
      _ctx = new AudioContext({ sampleRate: 8000 });
      _stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
      });
      _src  = _ctx.createMediaStreamSource(_stream);
      _proc = _ctx.createScriptProcessor(4096, 1, 1);
      _src.connect(_proc);
      _proc.connect(_ctx.destination);

      const sampleRate = _ctx.sampleRate;
      _proc.onaudioprocess = (e) => {
        if (!_running) return;
        const data = e.inputBuffer.getChannelData(0);
        _sendPCM(data, sampleRate);
        _updateVU(data);
      };
    }

    _running = true;
    _updateBtn(true);
    _setLog(`🎧 Aktywny (${source === 'radio' ? 'radio' : 'mikrofon'}, ${_ctx.sampleRate}Hz)`);
  } catch(e) {
    _setLog('✗ ' + e.message);
    console.error('[DeepCW]', e);
  }
}

function toggleStart() {
  if (_running) stopDecoding();
  else startDecoding(document.getElementById('deepcw-source')?.value || 'mic');
}

// ── Send PCM over the main WS ──────────────────────────────────────────────
function _sendPCM(f32data, srcRate) {
  const ws = window._mainWS;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;

  const header = new ArrayBuffer(5);
  const hv = new DataView(header);
  hv.setUint8(0, 0xC1);
  hv.setUint32(1, srcRate, true);  // little-endian

  const pcm = f32data.buffer.slice(f32data.byteOffset, f32data.byteOffset + f32data.byteLength);
  const msg = new Uint8Array(5 + pcm.byteLength);
  msg.set(new Uint8Array(header), 0);
  msg.set(new Uint8Array(pcm),    5);
  ws.send(msg.buffer);
}

// ── Receive text from the server ──────────────────────────────────────────────
let _rxCount = 0;
let _liveLine = null;   // the current line's element (replaced, not appended)
function handleText(msg1, msg2) {
  // Handles two call formats:
  //   handleText({block, preview, close})  — new, from ws.js
  //   handleText(text, preview)            — old, for compatibility
  let block, preview, close;
  if (msg1 && typeof msg1 === 'object') {
    ({ block, preview, close } = msg1);
  } else {
    block = msg1; preview = msg2; close = false;
  }

  const el = document.getElementById('deepcw-output');
  if (!el) return;

  // The "LIVE" preview has its own line below the window.
  const pv = document.getElementById('deepcw-preview');
  if (pv) pv.textContent = preview || '';

  // End of transmission — close the current line, the next station starts a new one.
  if (close) {
    _liveLine = null;
    return;
  }

  if (block) {
    _rxCount++;
    _setLog(`🎧 Dekodowanie… (odebrano ${_rxCount} fragm.)`);
    // REPLACE the whole line instead of appending fragments. The engine
    // sends the full current reading, so we overwrite the last line with
    // it — this way the text isn't stitched together from scraps and has
    // no repeats or gaps.
    if (!_liveLine || !_liveLine.isConnected) {
      _liveLine = document.createElement('div');
      el.appendChild(_liveLine);
    }
    _liveLine.innerHTML = _colorize(block);
  }
  // Cap at the last ~500 characters
  if (el.textContent.length > 800) {
    // Remove the first child until we're down to 600 characters
    while (el.textContent.length > 600 && el.firstChild
           && el.firstChild !== _liveLine) {
      el.removeChild(el.firstChild);
    }
  }
  el.scrollTop = el.scrollHeight;
}

// ── Known-callsign database (for coloring) ────────────────────────────────────
// Sources: FT8 decodes + QSO log (from the server), DX cluster spots
// (local, each user has their own cluster). A callsign from the database
// is colored as a CONFIRMED match; one already worked gets a different
// shade — the operator immediately sees whether it's worth calling.
let _knownCalls  = new Set();
let _workedCalls = new Set(); // CALL|BAND keys — each band is a new QSO

function _workedKeyCW(call, band) {
  return `${(call||'').toUpperCase()}|${(band||'').toUpperCase()}`;
}

async function refreshKnownCalls() {
  try {
    const token = localStorage.getItem('token') || '';
    const hdr = token ? {'Authorization': `Bearer ${token}`} : {};
    const [k, w] = await Promise.all([
      fetch('/api/deepcw/known_calls', {headers: hdr}).then(r => r.json()).catch(() => ({})),
      fetch('/api/qsolog/calls',       {headers: hdr}).then(r => r.json()).catch(() => ({})),
    ]);
    if (k.calls) _knownCalls  = new Set(k.calls.map(c => c.toUpperCase()));
    // w.calls is a list of {call, mode, band} (see qso_db.py::worked_calls)
    // — NOT a list of strings. c.toUpperCase() on an object threw a
    // TypeError, swallowed by the catch(e){} below, so _workedCalls never
    // got populated and the "already in log" graying was dead. Band is in
    // the key, so a station worked on one band doesn't gray out as a dupe on another.
    if (w.calls) _workedCalls = new Set(w.calls.map(c => _workedKeyCW(c.call, c.band)));
  } catch(e) {}
}

// Spots from the user's DX cluster — called from the cluster module when a spot arrives.
function addClusterSpots(calls) {
  for (const c of calls || []) {
    const u = (c || '').trim().toUpperCase();
    if (u.length >= 3) _knownCalls.add(u);
  }
}

function _colorize(text) {
  // Color CW keywords
  const COLORS = {
    // QSO endings
    '73':   '#fa0',  // orange
    'TU':   '#fa0',
    'SK':   '#fa0',
    'AR':   '#fa0',
    // RST reports handled by the regex below
    // Calling
    'CQ':   '#4cf',  // light blue
    'DE':   '#8af',
    // Acknowledgments
    'R':    '#aaf',
    'RR':   '#aaf',
    'RRR':  '#aaf',
    // Test
    'TEST': '#f4f',
    'K':    '#ff8',
    'KN':   '#ff8',
    'BK':   '#ff8',
  };

  // Split into tokens, keeping spaces and newlines
  let result = '';
  let i = 0;
  while (i < text.length) {
    // Newline
    if (text[i] === '\n') {
      result += '<br>';
      i++;
      continue;
    }
    // Space
    if (text[i] === ' ') {
      result += ' ';
      i++;
      continue;
    }
    // Read a token (up to a space or newline)
    let j = i;
    while (j < text.length && text[j] !== ' ' && text[j] !== '\n') j++;
    const token = text.slice(i, j);
    const TU = token.toUpperCase();

    const color = COLORS[TU];
    if (color) {
      result += `<span style="color:${color};font-weight:bold;">${_esc(token)}</span>`;
    } else if (_workedCalls.has(_workedKeyCW(TU, window.UI?.getBandName?.(window.AppState?.freq)))) {
      // ALREADY WORKED — gray, so it doesn't tempt the operator (dupe)
      result += `<span style="color:#888;font-weight:bold;" title="juz w logu">`
              + `${_esc(token)}</span>`;
    } else if (_knownCalls.has(TU)) {
      // CONFIRMED by the database (FT8/log/cluster) — a sure match, strong accent
      result += `<span style="color:#0f8;font-weight:bold;text-shadow:0 0 4px rgba(0,255,136,.4);" `
              + `title="znak potwierdzony">${_esc(token)}</span>`;
    } else if (/^[A-R]{2}\d{2}([A-X]{2})?$/i.test(token)) {
      // QTH locator: JO82, KO02, IN77, JO82AA etc.
      result += `<span style="color:#f90;font-weight:bold;">${_esc(token)}</span>`;
    } else if (/^\d{3}$|^[5T][59NT][19NT]$|^T{1,2}[019NT]{1,3}$/.test(TU)) {
      // RST reports: 599, 5N9, 59N, 5NN, T001, T01, TT1 and similar
      result += `<span style="color:#4f4;">${_esc(token)}</span>`;
    } else if (/^\d{1,4}$/.test(TU)) {
      // Contest serial number (after the report): 001, 14, 1234
      result += `<span style="color:#8f8;">${_esc(token)}</span>`;
    } else if (/^[A-Z0-9]{3,6}\/[A-Z0-9]/.test(TU) || /^[A-Z]{1,2}\d[A-Z]{1,4}$/.test(TU)) {
      // Callsign (pattern match, not confirmed by the database) — weaker accent
      result += `<span style="color:#4cf;font-weight:bold;">${_esc(token)}</span>`;
    } else {
      result += `<span style="color:var(--green);">${_esc(token)}</span>`;
    }
    i = j;
  }
  return result;
}

function _esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// Diagnostics: record the audio exactly as the model hears it, and download it for listening.
async function capture() {
  const token = localStorage.getItem('token') || '';
  const hdr = token ? {'Authorization': `Bearer ${token}`} : {};
  try {
    _setLog('🔴 Nagrywam 15 s audio (tak jak slyszy je model)...');
    const r = await fetch('/api/deepcw/capture', {
      method: 'POST',
      headers: {'Content-Type':'application/json', ...hdr},
      body: JSON.stringify({ seconds: 15 })
    });
    const d = await r.json();
    if (!d.ok) { _setLog('✗ ' + (d.error || 'Blad')); return; }
    // Wait for the recording to finish, then offer the download
    setTimeout(() => {
      _setLog('✓ Nagranie gotowe — pobieram...');
      window.open('/api/deepcw/capture_file' + (token ? `?token=${token}` : ''), '_blank');
    }, 16000);
  } catch(e) {
    _setLog('✗ ' + e.message);
  }
}

function clearOutput() {
  const el = document.getElementById('deepcw-output');
  if (el) el.innerHTML = '';
  _liveLine = null;
  const pv = document.getElementById('deepcw-preview');
  if (pv) pv.textContent = '';   // also clear the "LIVE" line
}

// ── UI ────────────────────────────────────────────────────────────────────────
function _setLog(msg) {
  const el = document.getElementById('deepcw-log');
  if (el) el.textContent = msg;
}

function _updateBtn(running) {
  const btn = document.getElementById('deepcw-start-btn');
  if (!btn) return;
  btn.textContent      = running ? '⏹ STOP' : '▶ START';
  btn.style.color      = running ? 'var(--red)' : 'var(--green)';
  btn.style.borderColor = running ? 'var(--red)' : 'var(--green2)';
}

function _updateVU(data) {
  const canvas = document.getElementById('deepcw-vu');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  let rms = 0;
  for (const s of data) rms += s * s;
  rms = Math.sqrt(rms / data.length);
  const level = Math.min(1, rms * 10);
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = level > 0.7 ? '#f44' : level > 0.3 ? '#4f4' : '#4cf';
  ctx.fillRect(0, 0, W * level, H);
}

// The bargraph is driven by the LEVEL FROM THE SERVER. After switching to
// card audio the browser no longer has its own stream — the level is
// computed by the server (from the same raw audio that feeds the model)
// and sent ready-made.
function handleVU(level) {
  const canvas = document.getElementById('deepcw-vu');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const lv = Math.max(0, Math.min(1, level || 0));
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = lv > 0.7 ? '#f44' : lv > 0.3 ? '#4f4' : '#4cf';
  ctx.fillRect(0, 0, W * lv, H);
}

// ── Window dragging ────────────────────────────────────────────────────────
// The window is grabbed by its title bar. The position is remembered in
// session memory, so after closing and reopening it stays where the operator put it.
let _dragPos = null;   // {left, top} or null = default position

function _initDrag() {
  const modal = document.getElementById('deepcw-modal');
  const bar   = document.getElementById('deepcw-drag-bar');
  if (!modal || !bar || bar._dragBound) return;
  bar._dragBound = true;
  bar.style.cursor = 'move';

  let sx = 0, sy = 0, sl = 0, st = 0, dragging = false;

  const onDown = (e) => {
    // Don't capture clicks on buttons in the bar (CLR / close)
    if (e.target.closest('button')) return;
    dragging = true;
    const r = modal.getBoundingClientRect();
    // Switch from right/bottom anchoring to left/top, so it can be
    // dragged freely in any direction.
    modal.style.left   = r.left + 'px';
    modal.style.top    = r.top  + 'px';
    modal.style.right  = 'auto';
    modal.style.bottom = 'auto';
    sx = e.clientX; sy = e.clientY; sl = r.left; st = r.top;
    e.preventDefault();
  };

  const onMove = (e) => {
    if (!dragging) return;
    // Keep the window within the screen (leave a margin so the bar stays grabbable)
    const w = modal.offsetWidth, h = modal.offsetHeight;
    let nl = Math.min(Math.max(0, sl + e.clientX - sx), window.innerWidth  - 60);
    let nt = Math.min(Math.max(0, st + e.clientY - sy), window.innerHeight - 30);
    modal.style.left = nl + 'px';
    modal.style.top  = nt + 'px';
    _dragPos = { left: nl, top: nt };
  };

  const onUp = () => { dragging = false; };

  bar.addEventListener('mousedown', onDown);
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup',   onUp);

  // Touch (a tablet next to the radio)
  bar.addEventListener('touchstart', (e) => {
    if (e.target.closest('button')) return;
    const t = e.touches[0];
    onDown({ clientX: t.clientX, clientY: t.clientY,
             target: e.target, preventDefault: () => e.preventDefault() });
  }, { passive: false });
  document.addEventListener('touchmove', (e) => {
    if (!dragging) return;
    const t = e.touches[0];
    onMove({ clientX: t.clientX, clientY: t.clientY });
    e.preventDefault();
  }, { passive: false });
  document.addEventListener('touchend', onUp);
}

function openModal() {
  const m = document.getElementById('deepcw-modal');
  if (m) {
    m.style.display = 'block';
    // Restore the position from the previous time it was opened
    if (_dragPos) {
      m.style.left = _dragPos.left + 'px';
      m.style.top  = _dragPos.top  + 'px';
      m.style.right = 'auto'; m.style.bottom = 'auto';
    }
  }
  _initDrag();
  refreshKnownCalls();   // a fresh callsign database for coloring
  fetch('/api/deepcw/engine_status').then(r => r.json()).then(d => {
    if (!d.hasModel)   _setLog('⚠ Model nie pobrany — USTAWIENIA → DeepCW → POBIERZ');
    else if (!d.ready) _setLog('⏳ Model ładuje się...');
    else               _setLog(`✓ Gotowy (${d.sizeMB} MB) — wybierz źródło i START`);
    // If the decoder is already running on the server (another operator has it
    // open, or we had it open before logging out), sync this window to that
    // state so it shows as active instead of stopped. We register as a viewer
    // so the decoder keeps running while our window is open, and stops only
    // when the last viewer leaves.
    if (d.running && !_running) {
      window.WS?.send({ type: 'cw_rx_enable', enabled: true });
      _running = true;
      _updateBtn(true);
      _setLog('🎧 Aktywny (dekoder już działał — dołączono)');
    }
  }).catch(() => {});
}

function closeModal() {
  const m = document.getElementById('deepcw-modal');
  if (m) m.style.display = 'none';
  // IMPORTANT: closing the panel STOPS the decoder. Otherwise the browser
  // would keep streaming PCM and the server would keep running ONNX
  // inference in the background — unnecessary CPU load with the window closed.
  if (_running) stopDecoding();
}

// ── CW DECODER WINDOW SCALING ─────────────────────────────────────────────────
// The CW window used to have a fixed size, and the font was sometimes too
// small. Here: (1) a handle to resize the window with the mouse (like a
// real window), (2) buttons to increase/decrease the font size. Sizes
// remembered in the browser's storage (localStorage doesn't work in
// artifacts, but this is a real app — it works).
let _cwFontPx = 14;

function _applyCwFont() {
  const el = document.getElementById('deepcw-output');
  if (el) el.style.fontSize = _cwFontPx + 'px';
  const pv = document.getElementById('deepcw-preview');
  if (pv) pv.style.fontSize = _cwFontPx + 'px';
  try { localStorage.setItem('deepcw_font_px', String(_cwFontPx)); } catch (e) {}
}

function cwFontBigger() { _cwFontPx = Math.min(40, _cwFontPx + 2); _applyCwFont(); }
function cwFontSmaller() { _cwFontPx = Math.max(8, _cwFontPx - 2); _applyCwFont(); }

function _initCwScaling() {
  const el = document.getElementById('deepcw-output');
  if (!el) return;

  // Restore the remembered font size and window height.
  try {
    const f = parseInt(localStorage.getItem('deepcw_font_px'), 10);
    if (f) _cwFontPx = f;
    const h = localStorage.getItem('deepcw_output_h');
    if (h) el.style.height = h;
  } catch (e) {}
  _applyCwFont();

  // Make the window natively resizable VERTICALLY and HORIZONTALLY (CSS resize).
  el.style.resize = 'both';
  el.style.overflow = 'auto';
  el.style.minHeight = '80px';
  el.style.minWidth = '200px';

  // Remember the size after resizing finishes (ResizeObserver).
  try {
    let _saveTimer = null;
    const ro = new ResizeObserver(() => {
      clearTimeout(_saveTimer);
      _saveTimer = setTimeout(() => {
        try { localStorage.setItem('deepcw_output_h', el.style.height || el.offsetHeight + 'px'); } catch (e) {}
      }, 400);
    });
    ro.observe(el);
  } catch (e) { /* No ResizeObserver — oh well, resize still works */ }
}

// Initialize scaling once the DOM is ready (the CW panel may already be in the HTML).
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _initCwScaling);
} else {
  _initCwScaling();
}

window.DeepCW = { startDecoding, stopDecoding, toggleStart,
                  clearOutput, handleText, handleVU, openModal, closeModal,
                  refreshKnownCalls, addClusterSpots, capture,
                  cwFontBigger, cwFontSmaller };
})();
