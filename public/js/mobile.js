/**
 * mobile.js — HAMCTRL Mobile: own minimal WS client + rendering.
 * Deliberately does NOT include ws.js (1418 lines, auto-enables Opus
 * audio on every connect regardless of subscribed channels, and is
 * wired to lots of desktop-only panels this page doesn't have).
 */
(function () {
'use strict';

const S = {
  connected: false,
  freq: 0, mode: '', bandwidth: 0,
  sMeter: 0, pwr: 0, swr: 0,
  ptt: false,
  lock: { locked: false, user_id: null, username: '', callsign: '' },
  allBands: {}, enabledBands: [],
  allModes: [], enabledModes: [],
};

let ws = null;
let reconnectTimer = null;
let toastTimer = null;

function myUid() { return window.CurrentUser?.id ?? null; }
function iHaveLock() { return S.lock.locked && S.lock.user_id === myUid(); }
function isAdmin() { return window.CurrentUser?.role === 'admin'; }
function canControl() { return iHaveLock() || isAdmin(); }

function fmtFreq(hz) {
  if (!hz) return '-.--- MHz';
  return (hz / 1e6).toFixed(3) + ' MHz';
}

function showToast(msg, level) {
  const el = document.getElementById('m-toast');
  el.textContent = msg;
  el.className = 'm-toast' + (level === 'error' ? ' error' : '');
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}

// ── WebSocket ────────────────────────────────────────────────────────────────
function connect() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const token = localStorage.getItem('token') || sessionStorage.getItem('ham_token') || '';
  const wsUrl = `${proto}://${location.host}/ws` + (token ? `?token=${encodeURIComponent(token)}` : '');
  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    S.connected = true;
    updateConnUI();
    ws.send(JSON.stringify({ type: 'subscribe', channels: ['ft8'] }));
  };
  ws.onclose = () => {
    S.connected = false;
    updateConnUI();
    reconnectTimer = setTimeout(connect, 3000);
  };
  ws.onerror = () => {};
  ws.onmessage = (e) => {
    if (typeof e.data !== 'string') return; // ignore any binary (audio) frames — never enabled, but be defensive
    let msg;
    try { msg = JSON.parse(e.data); } catch (err) { return; }
    handleMessage(msg);
  };
}

function wsSend(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}

function handleMessage(msg) {
  switch (msg.type) {
    case 'init':
      if (msg.freq) S.freq = msg.freq;
      if (msg.mode) S.mode = msg.mode;
      if (msg.bandwidth) S.bandwidth = msg.bandwidth;
      if (typeof msg.ptt === 'boolean') S.ptt = msg.ptt;
      renderFreq(); renderModeChips(); renderPTT();
      break;
    case 'freq':
      S.freq = msg.freq; renderFreq();
      break;
    case 'mode':
      S.mode = msg.mode; if (msg.bandwidth) S.bandwidth = msg.bandwidth;
      renderModeChips();
      break;
    case 'smeter':
      S.sMeter = msg.value ?? 0; renderMeters();
      break;
    case 'txmeter':
      if (msg.meter === 'PWR') S.pwr = msg.value;
      else if (msg.meter === 'SWR') S.swr = msg.value;
      else break;
      renderMeters();
      break;
    case 'ptt':
      S.ptt = !!msg.ptt; renderPTT();
      break;
    case 'radio_lock_state':
      S.lock = { locked: !!msg.locked, user_id: msg.user_id, username: msg.username, callsign: msg.callsign };
      renderLock(); renderLockedControls();
      break;
    case 'toast':
      showToast(msg.msg || msg.message || '', msg.level);
      break;
    case 'deepcw_text':
      renderCwText(msg);
      break;
    case 'qso_logged':
      if (msg.qso) prependLog(msg.qso);
      break;
    case 'auto_qso_status':
      renderFt8Status(msg);
      break;
    case 'auto_seq_status':
      renderFt8Status(msg);
      break;
  }
}

function updateConnUI() {
  const dot = document.getElementById('m-conn-dot');
  const label = document.getElementById('m-conn-label');
  dot.className = 'm-dot' + (S.connected ? ' ok' : '');
  label.textContent = S.connected ? 'połączono' : 'łączenie...';
}

// ── Radio lock ───────────────────────────────────────────────────────────────
function renderLock() {
  const statusEl = document.getElementById('m-lock-status');
  const btn = document.getElementById('m-lock-btn');
  if (!S.lock.locked) {
    statusEl.textContent = 'Radio wolne';
    btn.textContent = 'Zajmij radio';
    btn.className = 'm-btn m-btn-amber';
  } else if (iHaveLock()) {
    statusEl.textContent = 'Radio zajęte przez Ciebie';
    btn.textContent = 'Zwolnij';
    btn.className = 'm-btn m-btn-red';
  } else {
    statusEl.textContent = `Radio zajęte: ${S.lock.callsign || S.lock.username}`;
    btn.textContent = 'Poproś';
    btn.className = 'm-btn m-btn-amber';
  }
}

async function toggleLock() {
  try {
    if (iHaveLock()) {
      await fetch('/api/radio/release', { method: 'POST' });
    } else {
      const r = await fetch('/api/radio/request', { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.granted === false) showToast(d.message || 'Prośba wysłana');
      else if (!r.ok) showToast(d.error || 'Błąd', 'error');
    }
  } catch (e) { showToast('Błąd sieci', 'error'); }
}

function renderLockedControls() {
  const enabled = canControl();
  document.getElementById('m-ptt-btn').disabled = !enabled;
  const strip = document.getElementById('m-freq-strip');
  strip.dataset.disabled = enabled ? '0' : '1';
  document.querySelectorAll('#m-mode-row .m-chip, #m-band-row .m-chip').forEach(c => { c.disabled = !enabled; });
}

// ── Frequency / mode / bands ────────────────────────────────────────────────
function renderFreq() {
  document.getElementById('m-freq').textContent = fmtFreq(S.freq);
}

function renderModeChips() {
  const row = document.getElementById('m-mode-row');
  const modes = S.enabledModes.length ? S.enabledModes : ['USB', 'LSB', 'AM', 'FM', 'CW'];
  row.innerHTML = modes.map(m =>
    `<button class="m-chip ${m === S.mode ? 'active' : ''}" data-mode="${m}">${m}</button>`
  ).join('');
  row.querySelectorAll('.m-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      if (!canControl()) { showToast('Zajmij radio, żeby zmieniać tryb', 'error'); return; }
      wsSend({ type: 'mode', mode: btn.dataset.mode });
    });
  });
  renderLockedControls();
}

function renderBandChips() {
  const row = document.getElementById('m-band-row');
  const bands = S.enabledBands.length ? S.enabledBands : Object.keys(S.allBands);
  const curBand = findBand(S.freq);
  row.innerHTML = bands.map(b => {
    const r = S.allBands[b];
    if (!r) return '';
    return `<button class="m-chip ${b === curBand ? 'active' : ''}" data-freq="${r.def}" data-mode="${S.mode || 'USB'}">${b}</button>`;
  }).join('');
  row.querySelectorAll('.m-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      if (!canControl()) { showToast('Zajmij radio, żeby zmieniać pasmo', 'error'); return; }
      const freq = parseInt(btn.dataset.freq, 10);
      wsSend({ type: 'freq', freq });
      if (btn.dataset.mode && btn.dataset.mode !== S.mode) wsSend({ type: 'mode', mode: btn.dataset.mode });
    });
  });
  renderLockedControls();
}

function findBand(hz) {
  for (const [name, r] of Object.entries(S.allBands)) {
    if (hz >= r.min && hz <= r.max) return name;
  }
  return null;
}

async function loadBandsConfig() {
  try {
    const [rb, rm] = await Promise.all([fetch('/api/config/bands'), fetch('/api/config/modes')]);
    const db = await rb.json(), dm = await rm.json();
    S.allBands = db.allBands || {};
    S.enabledBands = db.enabledBands || [];
    S.allModes = dm.allModes || [];
    S.enabledModes = dm.enabledModes || [];
    renderModeChips();
    renderBandChips();
  } catch (e) { console.warn('[mobile] loadBandsConfig error:', e); }
}

// ── Frequency drag strip ─────────────────────────────────────────────────────
function initFreqStrip() {
  const strip = document.getElementById('m-freq-strip');
  let dragging = false, lastX = 0, lastT = 0, dragBand = null, lastSentAt = 0, pendingFreq = null;

  function clamp(hz) {
    if (dragBand) return Math.min(Math.max(hz, dragBand.min - 5000), dragBand.max + 5000);
    return Math.min(Math.max(hz, 100000), 470000000);
  }

  function sendMaybe(force) {
    if (pendingFreq == null) return;
    const now = performance.now();
    if (!force && now - lastSentAt < 50) return;
    lastSentAt = now;
    wsSend({ type: 'freq', freq: Math.round(pendingFreq / 10) * 10 });
  }

  strip.addEventListener('pointerdown', (e) => {
    if (strip.dataset.disabled === '1') return;
    if (!canControl()) { showToast('Zajmij radio, żeby stroić', 'error'); return; }
    dragging = true;
    strip.classList.add('dragging');
    strip.setPointerCapture(e.pointerId);
    lastX = e.clientX; lastT = performance.now();
    pendingFreq = S.freq;
    const bn = findBand(S.freq);
    dragBand = bn ? S.allBands[bn] : null;
  });

  strip.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    const now = performance.now();
    const dx = e.clientX - lastX;
    const dt = Math.max(1, now - lastT);
    const velocity = Math.abs(dx) / dt; // px/ms
    const hzPerPixel = Math.min(1500, Math.max(15, velocity * 500));
    pendingFreq = clamp(pendingFreq + dx * hzPerPixel);
    S.freq = pendingFreq;
    renderFreq();
    lastX = e.clientX; lastT = now;
    sendMaybe(false);
  });

  function endDrag(e) {
    if (!dragging) return;
    dragging = false;
    strip.classList.remove('dragging');
    try { strip.releasePointerCapture(e.pointerId); } catch (err) {}
    sendMaybe(true);
    pendingFreq = null; dragBand = null;
  }
  strip.addEventListener('pointerup', endDrag);
  strip.addEventListener('pointercancel', endDrag);
  strip.addEventListener('pointerleave', (e) => { if (dragging && e.pointerType === 'mouse') endDrag(e); });
}

// ── Meters ───────────────────────────────────────────────────────────────────
function renderMeters() {
  document.getElementById('m-smeter').textContent = S.sMeter || S.sMeter === 0 ? S.sMeter : '--';
  document.getElementById('m-pwr').textContent = S.pwr != null ? S.pwr : '--';
  document.getElementById('m-swr').textContent = S.swr != null ? S.swr : '--';
}

// ── PTT (press-and-hold — see plan for why not a toggle) ────────────────────
function initPTT() {
  const btn = document.getElementById('m-ptt-btn');
  function down(e) {
    if (btn.disabled) return;
    if (!canControl()) { showToast('Zajmij radio, żeby nadawać', 'error'); return; }
    btn.setPointerCapture(e.pointerId);
    wsSend({ type: 'ptt', ptt: true });
  }
  function up(e) {
    try { btn.releasePointerCapture(e.pointerId); } catch (err) {}
    wsSend({ type: 'ptt', ptt: false });
  }
  btn.addEventListener('pointerdown', down);
  btn.addEventListener('pointerup', up);
  btn.addEventListener('pointercancel', up);
  btn.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse' && S.ptt) up(e); });
}

function renderPTT() {
  const btn = document.getElementById('m-ptt-btn');
  btn.classList.toggle('active', S.ptt);
  btn.textContent = S.ptt ? 'NADAJE' : 'PTT';
}

// ── FT8 / CW status ──────────────────────────────────────────────────────────
function renderFt8Status(msg) {
  const el = document.getElementById('m-ft8-status');
  if (msg.state && msg.state !== 'idle' && msg.state !== 'off') {
    el.textContent = `${msg.state}${msg.partner ? ' — ' + msg.partner : ''}`;
  } else if (msg.enabled === false) {
    el.textContent = 'automatyka wyłączona';
  } else {
    el.textContent = 'brak aktywnego QSO';
  }
}

let cwLine = '';
function renderCwText(msg) {
  if (msg.close) { cwLine = ''; }
  else if (msg.block) { cwLine = ((cwLine + ' ' + msg.block).trim().slice(-120)); }
  const el = document.getElementById('m-cw-text');
  el.textContent = cwLine + (msg.preview || '');
}

// ── QSO log ──────────────────────────────────────────────────────────────────
function renderLogRow(q) {
  const time = (q.time_on || '').slice(0, 5);
  return `<div class="m-log-item">
    <span class="m-log-call">${q.call || ''}</span>
    <span class="m-log-meta">${q.band || ''} ${q.mode || ''} ${time}</span>
  </div>`;
}

function prependLog(q) {
  const list = document.getElementById('m-log-list');
  const empty = list.querySelector('.m-log-empty');
  if (empty) empty.remove();
  list.insertAdjacentHTML('afterbegin', renderLogRow(q));
}

async function loadLog() {
  const list = document.getElementById('m-log-list');
  try {
    const r = await fetch('/api/qsolog?per=25');
    const d = await r.json();
    const qsos = d.qsos || [];
    list.innerHTML = qsos.length
      ? qsos.map(renderLogRow).join('')
      : '<div class="m-log-empty">brak wpisów</div>';
  } catch (e) {
    list.innerHTML = '<div class="m-log-empty">błąd ładowania</div>';
  }
}

// ── Boot ─────────────────────────────────────────────────────────────────────
window.addEventListener('app:ready', () => {
  loadBandsConfig();
  loadLog();
  fetch('/api/radio/state').then(r => r.json()).then(d => {
    S.lock = { locked: !!d.locked, user_id: d.user_id, username: d.username, callsign: d.callsign };
    renderLock(); renderLockedControls();
  }).catch(() => {});
});

initFreqStrip();
initPTT();
renderLock();
renderMeters();
renderPTT();
connect();

window.Mobile = { toggleLock };

})();
