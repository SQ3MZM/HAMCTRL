
/**
 * auth.js (frontend) — sprawdzenie sesji, logout, guard
 * Ładowany jako pierwszy skrypt na każdej stronie (oprócz login.html)
 */
(function() {
'use strict';

// ── Sprawdź sesję ─────────────────────────────────────────────────────────────
async function checkSession() {
  try {
    const token = localStorage.getItem('token') || sessionStorage.getItem('ham_token');
    if (!token) {
      location.replace('/login.html');
      return null;
    }
    const r = await fetch('/api/auth/me', {
      headers: { 'Authorization': 'Bearer ' + token }
    });
    if (!r.ok) {
      localStorage.removeItem('token');
      sessionStorage.removeItem('ham_token');
      location.replace('/login.html');
      return null;
    }
    const data = await r.json();
    window.CurrentUser = data.user;
    if (window.AppState) {
      window.AppState.role     = data.user.role;
      window.AppState.callsign = data.user.callsign || data.user.username;
      window.AppState.my_uid   = data.user.id;
    }
    applyPermissions(data.user);
    window.dispatchEvent(new CustomEvent('app:ready', {detail: data.user}));
    return data.user;
  } catch(e) {
    location.replace('/login.html');
    return null;
  }
}

// ── Zastosuj uprawnienia do UI ────────────────────────────────────────────────
function applyPermissions(user) {
  if (!user) return;

  const role    = user.role || 'viewer';
  const isAdmin = role === 'admin';

  // Uprawnienia z backendu (tablica lub obiekt)
  let permsRaw = user.permissions || [];
  let permsSet;
  if (Array.isArray(permsRaw)) {
    permsSet = new Set(permsRaw);
  } else {
    permsSet = new Set(Object.entries(permsRaw).filter(([,v]) => v).map(([k]) => k));
  }

  // Pobierz aktywne statyczne features (z ostatniego refresh RadioFunctions)
  const activeFeatures = new Set((window._activeStaticFeatures || []).map(f => f.id || f));
  const featuresLoaded = window._activeStaticFeatures !== undefined && activeFeatures.size > 0;

  // Elementy data-perm — show/hide
  document.querySelectorAll('[data-perm]').forEach(el => {
    const required = el.dataset.perm;
    if (required === 'admin') {
      el.style.display = isAdmin ? '' : 'none';
      return;
    }
    // Admin widzi wszystko
    if (isAdmin) { el.style.display = ''; return; }
    // Sprawdz uprawnienia usera z backendu
    const permAllowed = permsSet.has(required) ||
      !['ptt','cw','band','mode','freq','split','rotator','settings','log'].includes(required);
    el.style.display = permAllowed ? '' : 'none';
  });

  // data-perm-disable — tylko PTT/CW dla nie-operatorow
  document.querySelectorAll('[data-perm-disable]').forEach(el => {
    const required = el.dataset.permDisable;
    const has = isAdmin || permsSet.has(required);
    el.disabled      = !has;
    el.style.opacity = has ? '' : '0.4';
    el.style.cursor  = has ? '' : 'not-allowed';
    el.title         = has ? '' : `Brak uprawnienia: ${required}`;
  });

  // Callsign
  const csEl = document.getElementById('callsign-display');
  if (csEl) csEl.textContent = user.callsign || user.username || '--';

  // Rola badge
  const roleEl = document.getElementById('user-role');
  if (roleEl) {
    roleEl.textContent = role.toUpperCase();
    roleEl.className   = `role-badge role-${role}`;
  }

  // Zakładki tylko dla admina
  const adminTab = document.getElementById('tab-admin');
  if (adminTab) adminTab.style.display = isAdmin ? '' : 'none';

  const internetTab = document.getElementById('tab-internet');
  if (internetTab) internetTab.style.display = isAdmin ? '' : 'none';
}

// Pozwól na ponowne zastosowanie uprawnień (np. po zmianie przez admina)
function reapplyPermissions() {
  if (window.CurrentUser) applyPermissions(window.CurrentUser);
}

// ── Logout ────────────────────────────────────────────────────────────────────
async function logout() {
  await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {});
  localStorage.removeItem('token');
  sessionStorage.removeItem('ham_token');
  location.replace('/login.html');
}

// ── Dodaj nagłówek auth do fetch ──────────────────────────────────────────────
// Patch globalny fetch — automatycznie doda Bearer token do wszystkich /api/*
const _origFetch = window.fetch;
window.fetch = function(url, opts = {}) {
  if (typeof url === 'string' && url.startsWith('/api')) {
    const token = localStorage.getItem('token') || sessionStorage.getItem('ham_token');
    if (token && !(opts.headers || {})['Authorization']) {
      opts.headers = Object.assign({}, opts.headers || {}, {
        'Authorization': `Bearer ${token}`
      });
    }
  }
  return _origFetch.call(this, url, opts);
};

// ── Eksport ───────────────────────────────────────────────────────────────────
window.Auth = { checkSession, logout, applyPermissions, reapplyPermissions };

// Auto-sprawdź sesję przy ładowaniu
document.addEventListener('DOMContentLoaded', () => checkSession());

})();
