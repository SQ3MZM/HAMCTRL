
/**
 * cw.js (frontend) — panel kluczowania CW
 * Obsługuje wszystkie TRX: auto/CAT/DTR/RTS
 *
 * MAKRA: edytowalne przez kazdego usera, we wlasnym zakresie — zapisywane
 * w localStorage przegladarki (per-user/per-urzadzenie, bez wplywu na
 * innych operatorow czy konfiguracje serwera). Dwuklik na karcie makra
 * otwiera edycje (etykieta + tekst CW); F1-F15 wysyla makro.
 */
(function () {
'use strict';

const S = window.AppState;
const MACROS_KEY = 'cwMacros';
let macros      = [];
let cwActive    = false;
let cwMethod    = 'auto';
let catCapable  = null; // null=nieznany

// Domyslne makra (uzyte przy pierwszym uruchomieniu / braku zapisu w localStorage)
const DEFAULT_MACROS = [
  { id: 1, label: 'CQ',   text: 'CQ CQ DE {MYCALL} {MYCALL} K' },
  { id: 2, label: 'RST',  text: 'UR 599 599 BK' },
  { id: 3, label: '73',   text: '73 DE {MYCALL} SK' },
  { id: 4, label: 'QRZ?', text: 'QRZ? DE {MYCALL}' },
  { id: 5, label: 'AGN?', text: 'PSE AGN?' },
  { id: 6, label: 'QSL',  text: 'TU QSL 73' },
];

// ── Ładuj makra (localStorage, per-user/per-przegladarka) i status ───────────
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
  // Dopelnij brakujace id (1-6) defaultami
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

// ── Render makr ───────────────────────────────────────────────────────────────
function renderMacros() {
  const el = document.getElementById('cw-macros-grid');
  if (!el) return;
  // Aktualizuj tylko etykiety i teksty istniejących kart (siatka 2×3 jest statyczna w HTML)
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
    console.warn('[cw] zapis makr nie powiodl sie:', e);
    window.UI?.showToast('✗ Nie udało się zapisać makra', 'error');
  }
}

function closeEdit() {
  const m = document.getElementById('cw-edit-modal');
  if (m) m.style.display = 'none';
}

// ── Wysyłanie ─────────────────────────────────────────────────────────────────
async function sendMacro(id) {
  const m = macros.find(x => x.id === id);
  if (m?.text) await sendText(m.text);
}

async function sendText(text, extra = {}) {
  if (cwActive) { stopCW(); return; }
  // {CALL} i {RST} byly wczesniej wypelniane z formularza "Szybki log QSO"
  // (usuniety). Pozostaja jako placeholdery do recznego wpisania w tresc
  // makra przez uzytkownika (np. "UR 599 OP JAN") — {MYCALL} dziala
  // automatycznie z ustawien stacji.
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
  if (r.status === 401) { window.UI?.showToast('✗ CW: brak autoryzacji — zaloguj się ponownie', 'error'); return; }
  const res = await r.json();
  if (!res.ok) window.UI?.showToast('✗ CW: ' + (res.error || 'błąd'), 'error');
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

// Slider WPM w panelu CW KEYER (#cw-wpm-slider) steruje CI-V KEYSPD (14 0C)
// — tym samym mechanizmem co slidery w lewej kolumnie (rig_slider). Krok 1
// WPM. Wartosc startowa i live-sync (zmiana na panelu radia) obslugiwane
// przez RadioFunctions (level_keyspd w _sliderEls, patrz radiofunctions.js).
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

// ── Klawisze F1-F15 ───────────────────────────────────────────────────────────
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

// Zaladuj makra po zalogowaniu — dopiero wtedy mamy token i uid
window.addEventListener('app:ready', () => { loadMacros(); });

})();

