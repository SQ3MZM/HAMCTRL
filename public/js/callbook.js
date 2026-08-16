/**
 * callbook.js — ustawienia lookupu QRZ.com / HamQTH (USTAWIENIA) + lookup
 * wywolywany z formularza QSO (qsolog.js woła Callbook.lookup(call)).
 */
(function () {
'use strict';

// ── Ustawienia (USTAWIENIA) ───────────────────────────────────────────────────
async function load() {
  try {
    const r = await fetch('/api/callbook/config');
    if (!r.ok) return;
    const cfg = await r.json();
    _set('cb-qrz-user', cfg.qrzUsername);
    _set('cb-qrz-pass', cfg.qrzPassword);
    _set('cb-hamqth-user', cfg.hamqthUsername);
    _set('cb-hamqth-pass', cfg.hamqthPassword);
  } catch (e) { console.warn('[callbook] load error:', e); }
}

function _set(id, val) { const el = document.getElementById(id); if (el) el.value = val || ''; }
function _get(id) { return document.getElementById(id)?.value?.trim() || ''; }

async function save() {
  const msg = document.getElementById('cb-save-msg');
  const cfg = {
    qrzUsername:    _get('cb-qrz-user'),
    qrzPassword:    _get('cb-qrz-pass'),
    hamqthUsername: _get('cb-hamqth-user'),
    hamqthPassword: _get('cb-hamqth-pass'),
  };
  try {
    await fetch('/api/callbook/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(cfg),
    });
    if (msg) msg.textContent = '✓ zapisano';
    window.UI?.showToast('✓ Ustawienia lookupu zapisane');
  } catch (e) {
    if (msg) msg.textContent = '✗ błąd zapisu';
  }
}

async function test(service) {
  const isQrz = service === 'qrz';
  const statusEl = document.getElementById(isQrz ? 'cb-qrz-status' : 'cb-hamqth-status');
  const username = _get(isQrz ? 'cb-qrz-user' : 'cb-hamqth-user');
  const password = _get(isQrz ? 'cb-qrz-pass' : 'cb-hamqth-pass');
  if (!username || !password) {
    if (statusEl) { statusEl.textContent = 'uzupełnij login/hasło'; statusEl.style.color = 'var(--red)'; }
    return;
  }
  if (statusEl) { statusEl.textContent = 'sprawdzanie...'; statusEl.style.color = 'var(--dim)'; }
  try {
    const r = await fetch('/api/callbook/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ service, username, password }),
    });
    const res = await r.json();
    if (statusEl) {
      if (res.ok) { statusEl.textContent = '✓ połączono'; statusEl.style.color = 'var(--green)'; }
      else { statusEl.textContent = '✗ ' + (res.error || 'błąd'); statusEl.style.color = 'var(--red)'; }
    }
  } catch (e) {
    if (statusEl) { statusEl.textContent = '✗ brak odpowiedzi'; statusEl.style.color = 'var(--red)'; }
  }
}

// ── Lookup (wolane z formularza QSO) ──────────────────────────────────────────
async function lookup(call) {
  if (!call) return null;
  try {
    const r = await fetch('/api/callbook/lookup?call=' + encodeURIComponent(call));
    const res = await r.json();
    return res.ok ? res : null;
  } catch (e) {
    console.warn('[callbook] lookup error:', e);
    return null;
  }
}

window.Callbook = { load, save, test, lookup };

})();
