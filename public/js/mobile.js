/**
 * mobile.js — HAMCTRL Mobile.
 *
 * REUSE, DON'T REINVENT: VFO/freq/mode/band/PTT/meters/lock/log below are
 * this page's OWN small implementation (same as v1) — simple enough that
 * duplicating a few lines is cheaper than pulling in ui.js's much larger,
 * desktop-DOM-coupled module for them.
 *
 * FT8/FT4 and CW, by contrast, are NOT reimplemented here at all. Those
 * panels reuse the real public/js/wsjtx.js and public/js/cw.js verbatim —
 * same safety-critical logic (FT8 "operator presence" watchdog, auto-QSO
 * state machine, TX macro templating, CW macro save/edit) as the desktop,
 * not a parallel hand-rolled copy that could silently diverge or drop a
 * safety check. mobile.html gives those panels the SAME element IDs
 * wsjtx.js/cw.js already target (wj-*, cw-*) — mobile.css just restyles
 * those same IDs/classes for touch. See the <script> boot order in
 * mobile.html: window.AppState/window.WS placeholders, then i18n.js (both
 * modules call I18n.t()), then cw.js/wsjtx.js, then this file — which
 * installs the REAL window.WS.send and forwards incoming WS messages to
 * CW.handleWS/WSJTX.handleWS.
 *
 * Deliberately does NOT include ws.js (auto-enables continuous Opus RX
 * audio on every connect regardless of subscribed channels — wrong for
 * mobile data — and derives its channel subscription from desktop-only
 * '.tab-btn.active' DOM).
 */
(function () {
'use strict';

const S = {
  connected: false,
  freq: 0, mode: '', bandwidth: 0,
  freqB: 0, vfo: 'VFOA', split: false,
  sMeter: 0, pwr: 0, swr: 0, alc: 0,
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

// Same precision/grouping as desktop's fmtFreq (ui.js) — 5 MHz decimals =
// 10 Hz resolution, matching the drag strip's own send granularity below.
function fmtFreq(hz) {
  if (!hz) return '-.---.-- MHz';
  const s = (hz / 1e6).toFixed(5);
  const [i, d] = s.split('.');
  return `${i}.${d.slice(0, 3)}.${d.slice(3)} MHz`;
}

function showToast(msg, level) {
  const el = document.getElementById('m-toast');
  if (!el) return;
  el.textContent = msg;
  el.className = 'm-toast' + (level === 'error' ? ' error' : '');
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}
// wsjtx.js/cw.js report errors via window.UI?.showToast(...) — without
// this shim those calls silently no-op (optional-chained) and FT8/CW
// errors (e.g. "no CW authorization", radio-busy toasts) would vanish
// on mobile instead of reaching the operator.
window.UI = window.UI || {};
window.UI.showToast = showToast;

// Same contract as desktop's UI.confirmModal (ui.js): a Promise-based
// yes/no modal, needed so radiolock.js's RadioLock.forceRelease() (reused
// verbatim, see mobile.html "Operatorzy" card) has something to await —
// without this shim window.UI?.confirmModal(...) silently resolves to
// undefined (optional chaining on a missing method), "!await undefined"
// is true, and the function returns before ever confirming — the admin
// "WYMUŚ" button would look clickable but do nothing.
let _confirmModalResolve = null;
function confirmModal(message, { title = I18n.t('common_confirm_title'), okLabel = 'OK', danger = false } = {}) {
  return new Promise((resolve) => {
    const modal = document.getElementById('confirm-modal');
    const msgEl = document.getElementById('confirm-modal-msg');
    const titleEl = document.getElementById('confirm-modal-title');
    const okBtn = document.getElementById('confirm-modal-ok');
    if (!modal || !msgEl) { resolve(false); return; }
    if (titleEl) titleEl.textContent = title;
    msgEl.textContent = message;
    if (okBtn) {
      okBtn.textContent = okLabel;
      okBtn.className = 'm-btn m-btn-sm' + (danger ? ' m-btn-red' : ' m-btn-amber');
    }
    _confirmModalResolve = resolve;
    modal.style.display = 'flex';
  });
}
function _confirmModalClose() {
  const modal = document.getElementById('confirm-modal');
  if (modal) modal.style.display = 'none';
}
function _confirmModalSubmit() {
  _confirmModalClose();
  _confirmModalResolve?.(true);
  _confirmModalResolve = null;
}
function _confirmModalCancel() {
  _confirmModalClose();
  _confirmModalResolve?.(false);
  _confirmModalResolve = null;
}
window.UI.confirmModal = confirmModal;
window.UI._confirmModalSubmit = _confirmModalSubmit;
window.UI._confirmModalCancel = _confirmModalCancel;

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
    // Reuse desktop's own FT8/CW logic verbatim (see file header) instead
    // of a parallel implementation here.
    try { window.WSJTX?.handleWS?.(msg); } catch (err) { console.warn('[mobile] WSJTX.handleWS error:', err); }
    try { window.CW?.handleWS?.(msg); } catch (err) { console.warn('[mobile] CW.handleWS error:', err); }
    try { window.RotW?.handleWS?.(msg); } catch (err) { console.warn('[mobile] RotW.handleWS error:', err); }
    try { forwardToRadioFunctions(msg); } catch (err) { console.warn('[mobile] RadioFunctions forward error:', err); }
  };
}

// RadioFunctions (func-toggle buttons, RFPOWER/AF/.../WPM sliders) is
// reused the same way as WSJTX/CW above, but ws.js's per-type routing
// (the desktop dispatcher) isn't loaded here, so this mirrors just the
// handful of cases ws.js forwards to RadioFunctions.
function forwardToRadioFunctions(msg) {
  const RF = window.RadioFunctions;
  if (!RF) return;
  switch (msg.type) {
    case 'level_value':
    case 'rig_slider_ack':
      // Diagnostic (reported live 2026-08-23 as "sliders don't stay in
      // sync with www") — confirms whether the broadcast is even
      // arriving here, to tell a routing bug apart from the silent
      // set_level-failure case already fixed server-side (webapp.py
      // _handle_rig_slider).
      console.log(`[mobile] ${msg.type} id=${msg.id} value=${msg.value}`);
      RF.handleLevelValue?.(msg); break;
    case 'func_state': RF.handleFuncState?.(msg); break;
    case 'rig_features': RF.handleWsMessage?.(msg); break;
    case 'tuner': RF.handleLegacyFunc?.('tuner', msg); break;
    case 'preamp': RF.handleLegacyFunc?.('preamp', msg); break;
    case 'attenuator': RF.handleLegacyFunc?.('attenuator', msg); break;
    case 'power_state': RF.handlePowerState?.(!!msg.value); break;
  }
}

function wsSend(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
}
// Installs the REAL sender for wsjtx.js/cw.js's bare `WS.send(...)` calls —
// mobile.html pre-declares a no-op placeholder so nothing throws before
// this runs (both modules capture `const S = window.AppState` and call
// `WS.send` at times that predate this script).
window.WS = { send: wsSend, ping: function () {} };

function handleMessage(msg) {
  switch (msg.type) {
    case 'init':
      if (msg.freq) S.freq = msg.freq;
      if (msg.freqB) S.freqB = msg.freqB;
      if (msg.mode) S.mode = msg.mode;
      if (msg.bandwidth) S.bandwidth = msg.bandwidth;
      if (typeof msg.split === 'boolean') S.split = msg.split;
      if (msg.vfo) S.vfo = msg.vfo;
      if (typeof msg.ptt === 'boolean') S.ptt = msg.ptt;
      renderFreq(); updateModeActive(); updateBandActive(); updateVfoActive(); renderSplit(); renderPTT();
      window.RadioFunctions?.syncStates?.({ vfo: S.vfo, split: S.split });
      if (typeof msg.rigPowerOn === 'boolean') {
        window.AppState.rigPowerOn = msg.rigPowerOn;
        window.RadioFunctions?.handlePowerState?.(msg.rigPowerOn);
      }
      // init carries the same online/requests/locked fields RadioLock's
      // own handleWS expects (see ws.js on desktop, "Pass the operator
      // state to OpPanel") - without this the "Operatorzy" card stayed
      // empty until the NEXT unrelated online_update/radio_lock_state
      // broadcast happened to arrive.
      window.OpPanel?.handleWS?.(msg);
      break;
    case 'freq':
      S.freq = msg.freq; renderFreq(); updateBandActive();
      break;
    case 'freqB':
      S.freqB = msg.freqB; renderFreq();
      break;
    case 'split':
      S.split = !!msg.split; if (msg.freqB != null) S.freqB = msg.freqB;
      renderFreq(); renderSplit();
      window.RadioFunctions?.syncStates?.({ split: S.split });
      break;
    case 'vfo':
      S.vfo = msg.vfo; updateVfoActive();
      window.RadioFunctions?.syncStates?.({ vfo: S.vfo });
      break;
    case 'mode':
      S.mode = msg.mode; if (msg.bandwidth) S.bandwidth = msg.bandwidth;
      updateModeActive(); // also mirrors S.mode into window.AppState
      if (msg.filterNum) {
        const sel = document.getElementById('bw-select');
        if (sel) sel.value = String(msg.filterNum);
      }
      break;
    case 'smeter':
      S.sMeter = msg.value ?? 0; renderMeters();
      break;
    case 'txmeter':
      if (msg.meter === 'PWR') S.pwr = msg.value;
      else if (msg.meter === 'SWR') S.swr = msg.value;
      else if (msg.meter === 'ALC') S.alc = msg.value;
      else break;
      renderMeters();
      break;
    case 'ptt':
      S.ptt = !!msg.ptt; renderPTT();
      break;
    case 'radio_lock_state':
      S.lock = { locked: !!msg.locked, user_id: msg.user_id, username: msg.username, callsign: msg.callsign };
      renderLock(); renderLockedControls();
      window.OpPanel?.handleWS?.(msg);
      break;
    case 'online_update':
      // Reported live 2026-08-24: "nie widac userow na serwerze i nie
      // widac prosb o radio" - the "Operatorzy" card (op-list/op-btn-*,
      // reused from radiolock.js) needs BOTH this (who's online) and
      // radio_lock_state above (who holds the radio) to render correctly;
      // desktop's ws.js handles them with the same single case, mirrored here.
      window.OpPanel?.handleWS?.(msg);
      break;
    case 'radio_request':
    case 'radio_request_received':
      // Shows the non-blocking "ktoś prosi o radio" toast (radiolock.js's
      // own _showRequestToast, works anywhere on the page - no dependency
      // on which tab is open or the Operatorzy card being visible).
      window.OpPanel?.handleRequest?.(msg);
      break;
    case 'radio_request_rejected':
      window.OpPanel?.handleRejected?.(msg);
      break;
    case 'toast':
      showToast(msg.msg || msg.message || '', msg.level);
      break;
    case 'radio_locked':
      // Generic radio_lock rejection for anything in the backend's
      // _CONTROL_TYPES set (webapp.py ~5150) — webrtc_offer (mic TX) is
      // one of them. Normal PTT already pre-checks canControl() before
      // ever sending, so this mostly matters as a defense-in-depth path.
      showToast(msg.message || 'Radio zablokowane', 'error');
      break;
    case 'webrtc_answer':
      window.MobileAudio?.onAnswer?.(msg);
      break;
    case 'webrtc_ice':
      window.MobileAudio?.onRemoteIce?.(msg);
      break;
    case 'webrtc_error':
      window.MobileAudio?.onWebrtcError?.(msg);
      break;
    case 'webrtc_rx_offer':
      window.MobileAudio?.onRxOffer?.(msg);
      break;
    case 'webrtc_rx_error':
      window.MobileAudio?.onRxWebrtcError?.(msg);
      break;
    case 'qso_logged':
      if (msg.qso) prependLog(msg.qso);
      break;
    // auto_qso_status / auto_seq_status / wsjtx_decode /
    // ft8_tx_status / cw_sending / ... are NOT handled here — they're
    // rendered by the real WSJTX.handleWS / CW.handleWS forwarded above.
  }
}

function updateConnUI() {
  const dot = document.getElementById('m-conn-dot');
  const label = document.getElementById('m-conn-label');
  dot.className = 'm-dot' + (S.connected ? ' ok' : '');
  label.textContent = S.connected ? I18n.t('m_conn_connected') : I18n.t('m_conn_connecting');
}

// ── Tabs ─────────────────────────────────────────────────────────────────────
let _ft8Inited = false;
function initTabs() {
  document.querySelectorAll('.m-tab').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });
}
function switchTab(name) {
  document.querySelectorAll('.m-tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  document.querySelectorAll('.m-tabpanel').forEach(p => p.classList.toggle('active', p.id === 'tab-' + name));
  if (name === 'ft8') {
    // WSJTX.init() (below) runs once, eagerly, at boot — while this tab is
    // still hidden behind Radio (display:none). The waterfall needs the
    // canvas to have real pixel dimensions to init WebGL, and a
    // display:none canvas has none, so the scope silently gives up after
    // ~2s of retries and never gets another chance -> permanently blank
    // waterfall. Re-trigger just the scope init here, every time this tab
    // is opened, now that the canvas is actually visible and sized —
    // WSJTXScope.init() is cheap/safe to call again (no-op once WebGL is
    // already running).
    window.WSJTXScope?.init?.();
  }
  if (name === 'ft8' && !_ft8Inited) {
    _ft8Inited = true;
    try { window.FT8Timer?.init?.(); } catch (e) {}
    try { window.WSJTX?.init?.(); window.WSJTX?.loadWorkedCalls?.(); } catch (e) { console.warn('[mobile] WSJTX.init error:', e); }
  }
}

// ── Radio lock ───────────────────────────────────────────────────────────────
function renderLock() {
  const statusEl = document.getElementById('m-lock-status');
  const btn = document.getElementById('m-lock-btn');
  if (!S.lock.locked) {
    statusEl.textContent = I18n.t('m_radio_free_short');
    btn.textContent = I18n.t('m_take_radio_btn');
    btn.className = 'm-btn m-btn-amber m-btn-sm';
  } else if (iHaveLock()) {
    statusEl.textContent = I18n.t('m_radio_busy_you');
    btn.textContent = I18n.t('m_release_btn');
    btn.className = 'm-btn m-btn-red m-btn-sm';
  } else {
    statusEl.textContent = I18n.t('m_radio_busy_by').replace('{name}', S.lock.callsign || S.lock.username);
    btn.textContent = I18n.t('m_request_btn');
    btn.className = 'm-btn m-btn-amber m-btn-sm';
  }
}

async function toggleLock() {
  try {
    if (iHaveLock()) {
      await fetch('/api/radio/release', { method: 'POST' });
    } else {
      const r = await fetch('/api/radio/request', { method: 'POST' });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.granted === false) showToast(d.message || I18n.t('m_request_sent_short'));
      else if (!r.ok) showToast(d.error || I18n.t('status_error_generic'), 'error');
    }
  } catch (e) { showToast(I18n.t('m_network_error'), 'error'); }
}

function renderLockedControls() {
  const enabled = canControl();
  document.querySelectorAll('button[data-perm-disable], input[data-perm-disable]').forEach(el => { el.disabled = !enabled; });
  document.querySelectorAll('#m-mode-row .m-chip, #m-band-row .m-chip').forEach(c => { c.disabled = !enabled; });
  const strip = document.getElementById('m-freq-strip');
  if (strip) strip.dataset.disabled = enabled ? '0' : '1';
}

// ── Frequency / mode / bands (VFO A only — matches desktop's own
// sendFreq()/tuneToBand(), which always target VFO A too; VFO B is a
// separate, simpler read+swap+equalize control below, not independently
// drag-tunable on mobile). ─────────────────────────────────────────────────
function renderFreq() {
  document.getElementById('m-freq').textContent = fmtFreq(S.freq);
  document.getElementById('m-freq-other').textContent = fmtFreq(S.freqB);
  // qsolog.js::quickLog() reads window.AppState.freq for the logged QSO's
  // band/frequency (const S = window.AppState there, a DIFFERENT object
  // than this file's own S) — keep it mirrored.
  window.AppState.freq = S.freq;
}

function buildModeChips() {
  const row = document.getElementById('m-mode-row');
  const modes = S.enabledModes.length ? S.enabledModes : ['USB', 'LSB', 'AM', 'FM', 'CW'];
  row.innerHTML = modes.map(m => `<button class="m-chip" data-mode="${m}">${m}</button>`).join('');
  row.querySelectorAll('.m-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      if (!canControl()) { showToast(I18n.t('m_need_radio_mode'), 'error'); return; }
      S.mode = btn.dataset.mode;
      updateModeActive();
      wsSend({ type: 'mode', mode: S.mode });
    });
  });
  updateModeActive();
  renderLockedControls();
}

function updateModeActive() {
  window.AppState.mode = S.mode; // see renderFreq() note on window.AppState mirroring
  document.querySelectorAll('#m-mode-row .m-chip').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === S.mode);
  });
}

function buildBandChips() {
  const row = document.getElementById('m-band-row');
  const bands = S.enabledBands.length ? S.enabledBands : Object.keys(S.allBands);
  row.innerHTML = bands.map(b => {
    const r = S.allBands[b];
    if (!r) return '';
    return `<button class="m-chip" data-band="${b}" data-freq="${r.def}" data-mode="${S.mode || 'USB'}">${b}</button>`;
  }).join('');
  row.querySelectorAll('.m-chip').forEach(btn => {
    btn.addEventListener('click', () => {
      if (!canControl()) { showToast(I18n.t('m_need_radio_band'), 'error'); return; }
      S.freq = parseInt(btn.dataset.freq, 10);
      if (btn.dataset.mode) S.mode = btn.dataset.mode;
      renderFreq(); updateModeActive(); updateBandActive();
      wsSend({ type: 'freq', freq: S.freq });
      if (btn.dataset.mode) wsSend({ type: 'mode', mode: btn.dataset.mode });
    });
  });
  updateBandActive();
  renderLockedControls();
}

function updateBandActive() {
  const cur = findBand(S.freq);
  document.querySelectorAll('#m-band-row .m-chip').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.band === cur);
  });
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
    buildModeChips();
    buildBandChips();
  } catch (e) { console.warn('[mobile] loadBandsConfig error:', e); }
}

// ── VFO A/B, split — own small implementation (ui.js's vfoSwap/vfoCopy/
// toggleSplit/vfoSelect are each only a few lines; pulling in the whole
// desktop-DOM-coupled ui.js module for them isn't worth it, unlike
// FT8/CW above). Same optimistic-update pattern as freq/mode: 'freqB' is
// broadcast with skip=ws (see webapp.py ~5458), so the sender needs a
// local update before sending, same as mode/band chips in v1.
function updateVfoActive() {
  document.getElementById('m-vfoa-btn')?.classList.toggle('active', S.vfo !== 'VFOB');
  document.getElementById('m-vfob-btn')?.classList.toggle('active', S.vfo === 'VFOB');
}

function renderSplit() {
  document.getElementById('m-split-btn')?.classList.toggle('active', S.split);
}

function vfoSelect(vfo) {
  if (!canControl()) { showToast(I18n.t('m_take_radio_btn'), 'error'); return; }
  if (S.vfo === vfo) return;
  S.vfo = vfo;
  updateVfoActive();
  wsSend({ type: 'vfo', vfo });
}

// swap/equalize use the native CI-V 07 B0/A0 commands (civ.py::vfo_swap/
// vfo_equalize) via {type:'vfo_op'} - the radio does the copy/swap
// atomically, and the server broadcasts back the real post-op freq/freqB.
// Previously these sent {type:'freqB'} (CI-V 25 01, unselected-VFO
// frequency set) fire-and-forget with no ACK check - looked right in the
// UI (optimistic local update below), but per Hamlib's own icom.c this
// command isn't reliably supported/ACKed on every rig (it defaults to
// ASSUMING it fails until a live probe proves otherwise), which matches
// exactly what was reported live: display updates, but the radio's real
// VFO B still has the old frequency once you actually switch to it or
// engage split.
function vfoSwap() {
  if (!canControl()) { showToast(I18n.t('m_take_radio_btn'), 'error'); return; }
  [S.freq, S.freqB] = [S.freqB, S.freq];
  renderFreq(); updateBandActive();
  wsSend({ type: 'vfo_op', op: 'swap' });
}

function vfoEqualize() {
  if (!canControl()) { showToast(I18n.t('m_take_radio_btn'), 'error'); return; }
  S.freqB = S.freq;
  renderFreq();
  wsSend({ type: 'vfo_op', op: 'equalize' });
}

function toggleSplit() {
  if (!canControl()) { showToast(I18n.t('m_take_radio_btn'), 'error'); return; }
  S.split = !S.split;
  renderSplit();
  wsSend({ type: 'split', split: S.split, freqB: S.freqB });
}

// ── Frequency drag strip (VFO A) ─────────────────────────────────────────────
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
    if (!canControl()) { showToast(I18n.t('m_need_radio_tune'), 'error'); return; }
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
  document.getElementById('m-smeter').textContent = S.sMeter != null ? S.sMeter : '--';
  document.getElementById('m-pwr').textContent = S.pwr != null ? S.pwr : '--';
  document.getElementById('m-swr').textContent = S.swr != null ? S.swr : '--';
  document.getElementById('m-alc').textContent = S.alc != null ? S.alc : '--';
}

// ── PTT (press-and-hold — see plan for why not a toggle) ────────────────────
function initPTT() {
  const btn = document.getElementById('m-ptt-btn');
  function down(e) {
    if (btn.disabled) return;
    if (!canControl()) { showToast(I18n.t('m_need_radio_tx'), 'error'); return; }
    btn.setPointerCapture(e.pointerId);
    wsSend({ type: 'ptt', ptt: true });
    // Arms the microphone (fresh getUserMedia + WebRTC offer) in parallel
    // with keying the radio — see mobile_audio.js header for why this is
    // per-press rather than a separate long-lived toggle like desktop.
    window.MobileAudio?.startMicTx?.();
  }
  function up(e) {
    try { btn.releasePointerCapture(e.pointerId); } catch (err) {}
    wsSend({ type: 'ptt', ptt: false });
    window.MobileAudio?.stopMicTx?.();
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

// ── CW macro grid — tap = CW.sendMacro(id), long-press = CW.startEdit(id).
// The grid/status/edit-modal DOM all reuse cw.js's own IDs (see
// mobile.html) — cw.js itself owns loading/saving/rendering/sending; this
// is only the touch-gesture glue tap vs. long-press requires (a plain
// onclick can't tell those apart, desktop uses dblclick instead).
function initCwMacroGestures() {
  document.querySelectorAll('#cw-macros-grid .m-macro-card').forEach(card => {
    const id = parseInt(card.dataset.id, 10);
    let pressTimer = null, longPressed = false;
    card.addEventListener('pointerdown', () => {
      longPressed = false;
      pressTimer = setTimeout(() => { longPressed = true; window.CW?.startEdit?.(id); }, 550);
    });
    const cancelTimer = () => clearTimeout(pressTimer);
    card.addEventListener('pointerup', () => {
      cancelTimer();
      if (!longPressed) {
        if (!canControl()) { showToast(I18n.t('m_need_radio_cw'), 'error'); return; }
        window.CW?.sendMacro?.(id);
      }
    });
    card.addEventListener('pointerleave', cancelTimer);
    card.addEventListener('pointercancel', cancelTimer);
  });
}

// ── Manual frequency entry (long-press #m-freq / #m-freq-other) ──────────
// Same tap-vs-hold gesture pattern as initCwMacroGestures above, but
// there's no competing short-tap action here (the freq display isn't
// clickable otherwise), so any hold just opens the edit modal.
let _freqEditVfo = null; // 'A' | 'B' — which display was long-pressed

function initFreqEditGestures() {
  [['m-freq', 'A'], ['m-freq-other', 'B']].forEach(([id, vfo]) => {
    const el = document.getElementById(id);
    if (!el) return;
    let pressTimer = null;
    el.addEventListener('pointerdown', () => {
      pressTimer = setTimeout(() => openFreqEdit(vfo), 550);
    });
    const cancelTimer = () => clearTimeout(pressTimer);
    el.addEventListener('pointerup', cancelTimer);
    el.addEventListener('pointerleave', cancelTimer);
    el.addEventListener('pointercancel', cancelTimer);
  });
}

function openFreqEdit(vfo) {
  if (!canControl()) { showToast(I18n.t('m_need_radio_freq'), 'error'); return; }
  _freqEditVfo = vfo;
  const title = document.getElementById('m-freq-edit-title');
  if (title) title.textContent = I18n.t('m_freq_vfo_title').replace('{vfo}', vfo);
  const input = document.getElementById('m-freq-edit-input');
  if (input) {
    const hz = vfo === 'B' ? S.freqB : S.freq;
    input.value = hz ? String((hz / 1e6).toFixed(6)).replace(/0+$/, '').replace(/\.$/, '') : '';
  }
  const modal = document.getElementById('m-freq-edit-modal');
  if (modal) modal.style.display = 'flex';
  input?.focus();
}

function closeFreqEdit() {
  const modal = document.getElementById('m-freq-edit-modal');
  if (modal) modal.style.display = 'none';
  _freqEditVfo = null;
}

function saveFreqEdit() {
  const input = document.getElementById('m-freq-edit-input');
  const raw = (input?.value || '').trim().replace(',', '.');
  const v = parseFloat(raw);
  if (!raw || isNaN(v) || v <= 0) { showToast(I18n.t('m_invalid_freq'), 'error'); return; }
  // Accept either MHz ("14.074") or raw Hz — any real ham frequency in
  // MHz is well under 1000, so a bigger typed number must already be Hz
  // (same heuristic as qso_db.py's _normalize_freq_mhz).
  const hz = Math.round(v < 1000 ? v * 1e6 : v);
  if (_freqEditVfo === 'B') {
    S.freqB = hz;
    renderFreq();
    wsSend({ type: 'freqB', freqB: hz });
  } else {
    S.freq = hz;
    renderFreq();
    wsSend({ type: 'freq', freq: hz });
  }
  closeFreqEdit();
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
      : `<div class="m-log-empty">${I18n.t('m_log_empty')}</div>`;
  } catch (e) {
    list.innerHTML = `<div class="m-log-empty">${I18n.t('m_load_error')}</div>`;
  }
}

// ── Settings ("⋮") — audio toggle + mic device picker ───────────────────────
function esc(s) { return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

function toggleRxAudio(forceOn) {
  const wantOn = forceOn !== undefined ? !!forceOn : !window.MobileAudio?.isRxEnabled?.();
  const on = window.MobileAudio?.enableRx?.(wantOn) ?? false;
  const btn = document.getElementById('m-audio-btn');
  if (btn) { btn.textContent = on ? '🔊' : '🔇'; btn.classList.toggle('active', on); }
  const chk = document.getElementById('m-settings-audio-toggle');
  if (chk) chk.checked = on;
}

async function loadMicList() {
  const sel = document.getElementById('m-mic-select');
  if (!sel) return;
  try {
    // Unlock real device labels — same throwaway-getUserMedia trick as
    // profile_audio.js (without it, enumerateDevices() returns blank labels).
    const tmp = await navigator.mediaDevices.getUserMedia({ audio: true });
    tmp.getTracks().forEach(t => t.stop());
  } catch (e) { /* permission denied — list still shows, just with generic labels */ }
  try {
    const devs = await navigator.mediaDevices.enumerateDevices();
    const inputs = devs.filter(d => d.kind === 'audioinput');
    const saved = localStorage.getItem('ham_audio_micId') || '';
    sel.innerHTML = `<option value="">${I18n.t('m_mic_default_opt')}</option>` +
      inputs.map(d => `<option value="${esc(d.deviceId)}"${d.deviceId === saved ? ' selected' : ''}>${esc(d.label || I18n.t('profile_mic_fallback'))}</option>`).join('');
  } catch (e) { showToast(I18n.t('m_mic_list_error'), 'error'); }
}

function setMicDevice(id) {
  localStorage.setItem('ham_audio_micId', id || '');
}

function openSettings() {
  const modal = document.getElementById('m-settings-modal');
  if (!modal) return;
  const chk = document.getElementById('m-settings-audio-toggle');
  if (chk) chk.checked = !!window.MobileAudio?.isRxEnabled?.();
  modal.style.display = 'flex';
  loadMicList();
}

function closeSettings() {
  const modal = document.getElementById('m-settings-modal');
  if (modal) modal.style.display = 'none';
}

// ── Boot ─────────────────────────────────────────────────────────────────────
window.addEventListener('app:ready', () => {
  // cw.js's sendText() fills {MYCALL} from window.AppState.callsign —
  // wsjtx.js falls back to window.CurrentUser?.callsign on its own, but
  // cw.js does not, so this needs setting explicitly.
  window.AppState.callsign = window.CurrentUser?.callsign || window.CurrentUser?.username || '';
  window.AppState.stationLocator = window.CurrentUser?.locator || '';

  loadBandsConfig();
  loadLog();
  initCwMacroGestures();
  initFreqEditGestures();
  fetch('/api/radio/state').then(r => r.json()).then(d => {
    S.lock = { locked: !!d.locked, user_id: d.user_id, username: d.username, callsign: d.callsign };
    renderLock(); renderLockedControls();
  }).catch(() => {});
  // CW.loadMacros() runs on its own (cw.js has its own 'app:ready' listener).
  // FT8 (FT8Timer.init/WSJTX.init) boots eagerly here too rather than
  // lazily on first tab-open like desktop — mobile has far fewer tabs and
  // everything WSJTX.init() touches is already guarded for missing DOM.
  try { window.FT8Timer?.init?.(); } catch (e) {}
  try { window.WSJTX?.init?.(); window.WSJTX?.loadWorkedCalls?.(); } catch (e) { console.warn('[mobile] WSJTX.init error:', e); }
  _ft8Inited = true;
});

initTabs();
initFreqStrip();
initPTT();
renderLock();
renderMeters();
renderPTT();
updateVfoActive();
renderSplit();
connect();

window.Mobile = {
  toggleLock, vfoSelect, vfoSwap, vfoEqualize, toggleSplit,
  toggleRxAudio, openSettings, closeSettings, setMicDevice,
  closeFreqEdit, saveFreqEdit,
  showToast, // mobile_audio.js reuses this for its own error toasts
};

// VFO A/B select buttons (declarative, matches the mode/band chip pattern)
document.getElementById('m-vfoa-btn')?.addEventListener('click', () => vfoSelect('VFOA'));
document.getElementById('m-vfob-btn')?.addEventListener('click', () => vfoSelect('VFOB'));

})();
