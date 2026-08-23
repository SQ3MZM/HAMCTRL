// ── RadioLock — blokada radia (jeden operator naraz) ──────────────────────────
// Wydzielone z index.html (2026-08-24) do osobnego pliku, żeby telefon mógł
// reużyć DOKŁADNIE tę samą logikę (lista operatorów online, blokada TRX,
// prośby o radio) zamiast duplikować ją — ten sam wzorzec co wsjtx.js/cw.js/
// radiofunctions.js/qsolog.js/rotormini.js gdzie indziej w tym projekcie.
// Treść bez zmian względem oryginału inline w index.html — czysta ekstrakcja.
window.RadioLock = (function() {
'use strict';

let _state = { locked: false, user_id: null, username: null, callsign: null,
               timeout_min: 20, requests: [], online: [] };
const S = window.AppState;

// ── Pobierz/przetwórz stan z WS lub API ──────────────────────────────────────
function handleWS(msg) {
  if (msg.locked !== undefined)  _state.locked      = msg.locked;
  if (msg.user_id !== undefined) _state.user_id     = msg.user_id;
  if (msg.username !== undefined) _state.username   = msg.username;
  if (msg.callsign !== undefined) _state.callsign   = msg.callsign;
  if (msg.timeout_min !== undefined) _state.timeout_min = msg.timeout_min;
  if (msg.requests)  _state.requests  = msg.requests || [];
  if (msg.online)    _state.online    = msg.online   || [];
  _render();
}

// ── Powiadomienie o prośbie o radio ──────────────────────────────────────────
function handleRequest(msg) {
  const myUid    = window.AppState?.my_uid || window.CurrentUser?.id;
  const isHolder = _state.locked && _state.user_id === myUid;
  if (!isHolder) return;

  const callsign = msg.from_cs || msg.from_uid || '?';

  // Pokaż nieblokowalny toast z przyciskami zamiast confirm()
  _showRequestToast(callsign, msg.from_uid);
}

function _showRequestToast(callsign, fromUid) {
  // Usuń poprzednie powiadomienie jeśli istnieje
  document.getElementById('radio-request-toast')?.remove();

  const toast = document.createElement('div');
  toast.id = 'radio-request-toast';
  toast.style.cssText = `
    position:fixed; top:70px; right:20px; z-index:9999;
    background:var(--panel); border:1px solid var(--amber);
    border-radius:6px; padding:16px 20px; min-width:280px;
    font-family:var(--mono); box-shadow:0 4px 20px rgba(0,0,0,0.5);
  `;
  toast.innerHTML = `
    <div style="color:var(--amber);font-size:12px;margin-bottom:8px;letter-spacing:1px;">
      📻 PROŚBA O RADIO
    </div>
    <div style="color:var(--text);font-size:13px;margin-bottom:14px;">
      <b>${callsign}</b> prosi o dostęp do TRX
    </div>
    <div style="display:flex;gap:8px;">
      <button onclick="window.OpPanel.releaseRadio();document.getElementById('radio-request-toast')?.remove();"
        style="flex:1;padding:8px;background:rgba(184,201,143,0.15);border:1px solid var(--green2);
        border-radius:4px;color:var(--green);font-family:var(--mono);font-size:11px;cursor:pointer;">
        ✓ ODDAJ RADIO
      </button>
      <button onclick="window.OpPanel.rejectRequest('${fromUid}');document.getElementById('radio-request-toast')?.remove();"
        style="flex:1;padding:8px;background:rgba(217,119,106,0.1);border:1px solid rgba(217,119,106,0.3);
        border-radius:4px;color:var(--red);font-family:var(--mono);font-size:11px;cursor:pointer;">
        ✗ ODRZUĆ
      </button>
    </div>
  `;
  document.body.appendChild(toast);

  // Auto-zamknij po 30s
  setTimeout(() => toast.remove(), 30000);
}

// ── Akcje ─────────────────────────────────────────────────────────────────────
async function lockRadio() {
  try {
    const r = await _api('/api/radio/lock', 'POST', {});
    if (!r.ok) { window.UI?.showToast('✗ ' + r.error, 'error'); }
  } catch(e) { window.UI?.showToast('✗ Błąd blokady radia', 'error'); }
}

async function releaseRadio() {
  try {
    const r = await _api('/api/radio/release', 'POST', {});
    if (!r.ok) { window.UI?.showToast('✗ ' + r.error, 'error'); }
  } catch(e) { window.UI?.showToast(I18n.t('toast_release_error'), 'error'); }
}

async function requestRadio() {
  const btn = document.getElementById('op-btn-request');
  if (btn) { btn.disabled = true; btn.textContent = I18n.t('request_sending'); }
  try {
    const r = await _api('/api/radio/request', 'POST', {});
    if (r.granted) {
      window.UI?.showToast(I18n.t('toast_trx_granted'));
    } else {
      window.UI?.showToast(I18n.t('toast_request_sent'));
      // Po 10s odblokuj przycisk — user może wysłać ponownie jeśli odrzucono
      setTimeout(() => {
        if (btn) { btn.disabled = false; btn.textContent = I18n.t('request_trx_again_btn'); }
      }, 10000);
    }
  } catch(e) {
    window.UI?.showToast(I18n.t('toast_request_error'), 'error');
    if (btn) { btn.disabled = false; btn.textContent = I18n.t('request_trx_btn'); }
  }
}

async function rejectRequest(fromUid) {
  try {
    const r = await _api('/api/radio/reject-request', 'POST', { uid: fromUid });
    if (!r.ok) window.UI?.showToast('✗ ' + (r.error || 'Błąd'), 'error');
  } catch(e) { window.UI?.showToast(I18n.t('toast_reject_error'), 'error'); }
}

// Powiadomienie DLA PROSZĄCEGO gdy jego prośba zostanie odrzucona — bez tego
// przycisk "POPROŚ O TRX" milczy i user nie wie czy ktoś w ogóle odpowiedział.
function handleRejected(msg) {
  const myUid = window.AppState?.my_uid || window.CurrentUser?.id;
  if (String(msg.to_uid) !== String(myUid)) return;
  window.UI?.showToast(I18n.t('toast_request_rejected').replace('{by}', msg.by || '?'), 'error');
  const btn = document.getElementById('op-btn-request');
  if (btn) { btn.disabled = false; btn.textContent = I18n.t('request_trx_again_btn'); }
}

async function forceRelease() {
  if (!await window.UI?.confirmModal('Wymusić zwolnienie radia? Aktywny operator straci dostęp.', { danger: true, okLabel: 'ZWOLNIJ' })) return;
  await _api('/api/radio/force-release', 'POST', {});
}

async function setTimeout_(minutes) {
  await _api('/api/radio/timeout', 'POST', { minutes });
}

// ── Render UI ─────────────────────────────────────────────────────────────────
function _render() {
  _renderOpPanel();
}

function _renderOpPanel() {
  const myUid   = window.AppState?.my_uid || window.CurrentUser?.id;
  const isAdmin  = window.CurrentUser?.role === 'admin';
  const isHolder = _state.locked && _state.user_id === myUid;
  const isFree   = !_state.locked;
  const hasReq   = _state.requests?.some(r => r.user_id === myUid);

  // Lista operatorów online
  const list = document.getElementById('op-list');
  const count = document.getElementById('op-online-count');
  if (list) {
    list.innerHTML = _state.online.map(u => {
      const isMe     = u.user_id === myUid;
      const isActive = u.user_id === _state.user_id;
      return `<div style="display:flex;align-items:center;gap:5px;padding:3px 4px;
        border-radius:3px;background:${isActive ? 'rgba(184,201,143,0.08)' : 'transparent'};
        border:1px solid ${isActive ? 'var(--border2)' : 'transparent'};">
        <span style="font-size:14px;">${isActive ? '🎙' : '👤'}</span>
        <span style="font-family:var(--mono);font-size:10px;color:${isMe ? 'var(--green)' : 'var(--text)'};flex:1;">
          ${u.callsign || u.username}${isMe ? ' ◀' : ''}
        </span>
        ${isActive ? `<span style="font-family:var(--mono);font-size:8px;color:var(--green);letter-spacing:1px;">TRX</span>` : ''}
      </div>`;
    }).join('') || `<div style="font-family:var(--mono);font-size:9px;color:var(--dim);padding:8px;text-align:center;">${I18n.t('no_connections')}</div>`;
  }
  if (count) count.textContent = _state.online.length;

  // Status lock
  const status = document.getElementById('op-lock-status');
  if (status) {
    if (isFree) {
      status.innerHTML = `<span style="color:var(--green)">${I18n.t('radio_free')}</span>`;
    } else if (isHolder) {
      status.innerHTML = `<span style="color:var(--green)">${I18n.t('you_have_trx')}</span>`;
    } else {
      status.innerHTML = `<span style="color:var(--amber)">🔒 ${_state.callsign || _state.username || '?'}</span>`;
    }
  }

  // Przyciski — pokaż właściwy zestaw
  const btnTake    = document.getElementById('op-btn-take');
  const btnRelease = document.getElementById('op-btn-release');
  const btnRequest = document.getElementById('op-btn-request');
  const btnForce   = document.getElementById('op-btn-force');

  if (btnTake)    btnTake.style.display    = isFree ? '' : 'none';
  if (btnRelease) btnRelease.style.display = isHolder ? '' : 'none';
  if (btnRequest) {
    btnRequest.removeAttribute('data-i18n');  // patrz uwaga przy rot-status-badge (rotormini.js)
    btnRequest.style.display = (!isFree && !isHolder) ? '' : 'none';
    btnRequest.textContent   = hasReq ? I18n.t('request_sent_short') : I18n.t('request_trx_btn');
    btnRequest.disabled      = hasReq;
  }
  if (btnForce)   btnForce.style.display   = (isAdmin && _state.locked && !isHolder) ? '' : 'none';

  // Blokada UI gdy ktos inny ma radio zalatwia WYLACZNIE applyRadioLockUI()
  // (ui.js) przez klase .radio-readonly (CSS pointer-events — dziala
  // niezaleznie od atrybutu disabled). Byla tu KIEDYS druga, rownolegla
  // sciezka (_setUILocked wlasnym querySelectorAll ustawiajaca .disabled) —
  // usunieta w audycie zakladki RADIO 2026-08-15: mialo nieaktualna liste
  // selektorow (np. ".go-btn" nie pasowal do zadnego elementu — prawdziwe
  // przyciski rotora to .rot-go-btn/.rot-stop-btn) i dublowalo stan bez
  // realnej korzysci, bo klikniecia i tak byly juz zablokowane przez CSS.
}

// ── HTTP helper ───────────────────────────────────────────────────────────────
async function _api(path, method, body) {
  const token = localStorage.getItem('token') || sessionStorage.getItem('ham_token');
  const r = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json',
               ...(token ? {'Authorization': `Bearer ${token}`} : {}) },
    body: JSON.stringify(body),
  });
  return r.json();
}

window.OpPanel = { handleWS, handleRequest, handleRejected, lockRadio, releaseRadio, requestRadio, rejectRequest, forceRelease };
return window.OpPanel;
})();
