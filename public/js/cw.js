
/**
 * cw.js (frontend) — CW keying panel
 * Handles all TRX methods: auto/CAT/DTR/RTS
 *
 * MACROS: editable by every user, in their own scope — saved in the
 * browser's localStorage (per-user/per-device, no effect on other
 * operators or server config). Double-click on a macro card opens editing
 * (label + CW text); F1-F15 sends the macro.
 */
(function () {
'use strict';

const S = window.AppState;
const MACROS_KEY = 'cwMacros';
let macros      = [];
let cwActive    = false;
let cwMethod    = 'auto';
let catCapable  = null; // null=unknown

// Default macros (used on first run / when nothing is saved in localStorage)
const DEFAULT_MACROS = [
  { id: 1, label: 'CQ',   text: 'CQ CQ DE {MYCALL} {MYCALL} K' },
  { id: 2, label: 'RST',  text: 'UR 599 599 BK' },
  { id: 3, label: '73',   text: '73 DE {MYCALL} SK' },
  { id: 4, label: 'QRZ?', text: 'QRZ? DE {MYCALL}' },
  { id: 5, label: 'AGN?', text: 'PSE AGN?' },
  { id: 6, label: 'QSL',  text: 'TU QSL 73' },
];

// ── Load macros (localStorage, per-user/per-browser) and status ─────────────
async function loadMacros() {
  try {
    const token = localStorage.getItem('token') || '';
    const r = await fetch('/api/cw/macros', {
      headers: token ? {'Authorization': `Bearer ${token}`} : {}
    });
    if (r.ok) {
      const data = await r.json();
      macros = data.macros || DEFAULT_MACROS.map(m => ({...m}));
    } else {
      macros = DEFAULT_MACROS.map(m => ({...m}));
    }
  } catch(e) {
    macros = DEFAULT_MACROS.map(m => ({...m}));
  }
  // Fill in missing ids (1-6) with defaults
  for (const def of DEFAULT_MACROS) {
    if (!macros.find(m => m.id === def.id)) macros.push({...def});
  }
  renderMacros();
}

async function loadStatus() {
  try {
    const r   = await fetch('/api/cw/status');
    const st  = await r.json();
    cwMethod  = st.method    || 'auto';
    catCapable= st.capabilities?.catMorse ?? null;
    updateMethodBadge();
    updateCapsBadge(st.capabilities);
  } catch(e) {}
}

function updateMethodBadge() {
  const el = document.getElementById('cw-method-badge');
  if (!el) return;
  const labels = { auto:'AUTO', cat:'CAT', dtr:'DTR', rts:'RTS' };
  const colors = { auto:'var(--dim)', cat:'var(--green)', dtr:'var(--amber)', rts:'var(--amber)' };
  el.textContent = labels[cwMethod] || cwMethod.toUpperCase();
  el.style.color = colors[cwMethod] || 'var(--dim)';
}

function updateCapsBadge(caps) {
  const el = document.getElementById('cw-caps-badge');
  if (!el || !caps) return;
  if (caps.error) { el.textContent = '? nieznany'; el.style.color = 'var(--dim)'; return; }
  if (caps.catMorse) {
    el.textContent = `✓ CAT OK (${caps.rigMfr || ''})`;
    el.style.color = 'var(--green)';
  } else {
    el.textContent = `⚠ CAT N/A → DTR/RTS`;
    el.style.color = 'var(--amber)';
  }
}

// ── Render macros ────────────────────────────────────────────────────────────
function renderMacros() {
  const el = document.getElementById('cw-macros-grid');
  if (!el) return;
  // Only update the labels/text of existing cards (the 2×3 grid is static in the HTML)
  const display = macros.slice(0, 6);
  display.forEach(m => {
    const lbl = document.getElementById('cwm-lbl-' + m.id);
    const txt = document.getElementById('cwm-txt-' + m.id);
    const card = document.getElementById('cwm-' + m.id);
    if (lbl)  lbl.textContent = m.label || ('F' + m.id);
    if (txt)  txt.textContent = m.text  || '—';
    if (card) {
      card.title = m.text || '';
      card.classList.toggle('empty', !m.text);
    }
  });
}

function startEdit(id) {
  const m = macros.find(x => x.id === id);
  if (!m) return;
  const modal = document.getElementById('cw-edit-modal');
  if (!modal) return;
  document.getElementById('cw-edit-id').value    = id;
  document.getElementById('cw-edit-label').value = m.label;
  document.getElementById('cw-edit-text').value  = m.text;
  document.getElementById('cw-edit-title').textContent = `Makro F${id}`;
  modal.style.display = 'flex';
  document.getElementById('cw-edit-text').focus();
}

function saveEditMacro() {
  const id    = parseInt(document.getElementById('cw-edit-id')?.value);
  const label = document.getElementById('cw-edit-label')?.value?.trim() || `F${id}`;
  const text  = document.getElementById('cw-edit-text')?.value?.trim();
  const idx   = macros.findIndex(m => m.id === id);
  if (idx >= 0) { macros[idx].label = label; macros[idx].text = text; }
  closeEdit();
  _saveMacros();
  renderMacros();
}

async function _saveMacros() {
  try {
    const token = localStorage.getItem('token') || '';
    await fetch('/api/cw/macros', {
      method: 'POST',
      headers: {'Content-Type': 'application/json',
        ...(token ? {'Authorization': `Bearer ${token}`} : {})},
      body: JSON.stringify({ macros }),
    });
  } catch(e) {
    console.warn('[cw] macro save failed:', e);
    window.UI?.showToast('✗ Nie udało się zapisać makra', 'error');
  }
}

function closeEdit() {
  const m = document.getElementById('cw-edit-modal');
  if (m) m.style.display = 'none';
}

// ── Sending ───────────────────────────────────────────────────────────────────
async function sendMacro(id) {
  const m = macros.find(x => x.id === id);
  if (m?.text) await sendText(m.text);
}

async function sendText(text, extra = {}) {
  if (cwActive) { stopCW(); return; }
  // FIX (reported live 2026-08-24): cwActive used to flip true only once
  // the 'cw_sending' WS broadcast echoed back from the server. A repeat
  // tap/keypress fired BEFORE that echo arrived (any real network RTT -
  // worse on mobile/LTE) still saw cwActive===false, fell through to a
  // brand-new /api/cw/send instead of the stop-toggle above, and the
  // server's own overlap guard (_cw_tx_busy in webapp.py) silently
  // rejected it - looked exactly like "the macro just does nothing" on
  // 3 out of 4 rapid presses, with no obvious sign why. Setting it here,
  // optimistically, closes that window: any repeat tap while our own
  // request is in flight now correctly stops it instead of racing a
  // second send.
  cwActive = true;
  // FIX (reported live 2026-08-24: "cw makro sie zwiesilo, w ogole nie
  // reaguje" - every tap after this just re-sent /api/cw/stop with no
  // effect). The optimistic cwActive=true above has no recovery path if
  // the fetch itself throws (dropped connection, server restart mid-
  // request) - the old code had no try/catch here, so an exception left
  // cwActive stuck true FOREVER: every later tap saw cwActive===true and
  // called stopCW() instead of sending, forever, even though nothing was
  // actually transmitting server-side. A safety timeout (below) also
  // guards the case where the request itself succeeds but the
  // 'cw_done'/'cw_stopped'/'cw_error' WS confirmation never arrives (a
  // dropped WS reconnect mid-transmission) - self-heals either way
  // instead of requiring a page reload.
  const _cwSafetyTimer = setTimeout(() => {
    if (cwActive) {
      cwActive = false;
      console.warn('[cw] cwActive force-cleared by safety timeout (no cw_done/error arrived)');
    }
  }, 20000); // generous: even a long macro at low WPM finishes well under this
  try {
    // {CALL} and {RST} used to be filled in from the "Quick QSO log" form
    // (removed). They remain as placeholders for the user to type manually
    // into the macro text (e.g. "UR 599 OP JAN") — {MYCALL} still works
    // automatically from the station settings.
    const vars = {
      myCall: S.callsign || '',
      rst:    '599',
      ...extra,
    };
    const token = localStorage.getItem('token') || sessionStorage.getItem('ham_token') || '';
    const r   = await fetch('/api/cw/send', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? {'Authorization': `Bearer ${token}`} : {}),
      },
      body: JSON.stringify({ text, vars }),
    });
    if (r.status === 401) {
      cwActive = false;
      clearTimeout(_cwSafetyTimer);
      window.UI?.showToast('✗ CW: brak autoryzacji — zaloguj się ponownie', 'error');
      return;
    }
    const res = await r.json();
    if (!res.ok) {
      // The send never actually started (rejected before any PTT/keying) -
      // undo the optimistic flag so the button/Escape-to-stop don't get
      // stuck thinking a transmission is running when none is.
      cwActive = false;
      clearTimeout(_cwSafetyTimer);
      window.UI?.showToast('✗ CW: ' + (res.error || 'błąd'), 'error');
    }
    // res.ok === true: DELIBERATELY leave cwActive=true and the safety
    // timer RUNNING here - the fetch resolving successfully only means
    // the server accepted and finished the transmission (it blocks until
    // done), not that we've received the 'cw_done' WS confirmation that
    // normally clears cwActive (handleWS below). If that WS message gets
    // lost (a reconnect mid-transmission), the timer is the only thing
    // that will ever clear it - clearing it here unconditionally would
    // remove that backstop in exactly the case it exists for.
  } catch (e) {
    cwActive = false;
    clearTimeout(_cwSafetyTimer);
    window.UI?.showToast('✗ CW: błąd połączenia — ' + e.message, 'error');
  }
}

async function sendCustom() {
  const inp = document.getElementById('cw-custom-input');
  if (!inp?.value.trim()) return;
  await sendText(inp.value);
  inp.value = '';
}

async function stopCW() {
  const token = localStorage.getItem('token') || sessionStorage.getItem('ham_token') || '';
  await fetch('/api/cw/stop', {
    method: 'POST',
    headers: token ? {'Authorization': `Bearer ${token}`} : {},
  });
}

// The WPM slider in the CW KEYER panel (#cw-wpm-slider) controls CI-V
// KEYSPD (14 0C) — the same mechanism as the sliders in the left column
// (rig_slider). Step of 1 WPM. The initial value and live-sync (change on
// the radio's own panel) are handled by RadioFunctions (level_keyspd in
// _sliderEls, see radiofunctions.js).
function setWPM(wpm) {
  const val = parseInt(wpm);
  const el = document.getElementById('cw-wpm-val');
  if (el) el.textContent = val;
  WS.send({ type: 'rig_slider', id: 'level_keyspd', value: val });
}

async function setMethodUI(method) {
  cwMethod = method;
  updateMethodBadge();
  await fetch('/api/cw/method', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ method }),
  });
  window.UI?.showToast(`CW metoda: ${method.toUpperCase()}`);
}

async function openDTRPort(port) {
  const r   = await fetch('/api/cw/dtr-port', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ port }),
  });
  const res = await r.json();
  window.UI?.showToast(res.ok ? `✓ Port DTR/RTS: ${port}` : `✗ ${res.error}`, res.ok ? 'info' : 'error');
}

// ── WS handler ────────────────────────────────────────────────────────────────
function handleWS(msg) {
  if (msg.type === 'cw_sending') {
    cwActive = true;
    const st = document.getElementById('cw-status');
    if (st) {
      st.innerHTML = `<span style="color:var(--red)">▶ TX</span> <span style="font-size:10px;opacity:0.8">[${msg.method||''}]</span> ${esc(msg.text||'')}`;
    }
    document.querySelectorAll('.cw-send-btn').forEach(b => b.classList.add('cw-tx'));
  }
  if (msg.type === 'cw_done' || msg.type === 'cw_stopped') {
    cwActive = false;
    const st = document.getElementById('cw-status');
    if (st) { st.innerHTML = '● GOTOWY'; st.style.color = 'var(--dim)'; }
    document.querySelectorAll('.cw-send-btn').forEach(b => b.classList.remove('cw-tx'));
  }
  if (msg.type === 'cw_error') {
    cwActive = false;
    const st = document.getElementById('cw-status');
    if (st) { st.textContent = '✗ ' + (msg.error||''); st.style.color = 'var(--red)'; }
    document.querySelectorAll('.cw-send-btn').forEach(b => b.classList.remove('cw-tx'));
  }
}

// ── F1-F15 keys ───────────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  const tag = document.activeElement?.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
  const f = e.key?.match(/^F(\d+)$/);
  if (!f) return;
  const id = parseInt(f[1]);
  if (id >= 1 && id <= 15) {
    e.preventDefault();
    const m = macros.find(x => x.id === id);
    if (m?.text) sendMacro(id);
  }
  // Escape = stop
  if (e.key === 'Escape' && cwActive) { e.preventDefault(); stopCW(); }
});

function esc(s){ return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

window.CW = {
  loadMacros, loadStatus, renderMacros,
  sendMacro, sendText, sendCustom, stopCW, setWPM, setMethodUI, openDTRPort,
  handleWS, startEdit, saveEditMacro, closeEdit,
};

window.CW._loaded = false;

document.addEventListener('DOMContentLoaded', () => {
  loadStatus();
  window.CW._loaded = true;
});

// Load macros after login — that's when we finally have the token and uid
window.addEventListener('app:ready', () => { loadMacros(); });

})();

