
/**
 * auth.js (frontend) — session check, logout, guard
 * Loaded as the first script on every page (except login.html)
 */
(function() {
'use strict';

// ── Check session ───────────────────────────────────────────────────────────
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

// ── Apply permissions to the UI ───────────────────────────────────────────────
function applyPermissions(user) {
  if (!user) return;

  const role    = user.role || 'viewer';
  const isAdmin = role === 'admin';

  // Permissions from the backend (array or object)
  let permsRaw = user.permissions || [];
  let permsSet;
  if (Array.isArray(permsRaw)) {
    permsSet = new Set(permsRaw);
  } else {
    permsSet = new Set(Object.entries(permsRaw).filter(([,v]) => v).map(([k]) => k));
  }

  // Get the active static features (from RadioFunctions' last refresh)
  const activeFeatures = new Set((window._activeStaticFeatures || []).map(f => f.id || f));
  const featuresLoaded = window._activeStaticFeatures !== undefined && activeFeatures.size > 0;

  // data-perm elements — show/hide
  document.querySelectorAll('[data-perm]').forEach(el => {
    const required = el.dataset.perm;
    if (required === 'admin') {
      el.style.display = isAdmin ? '' : 'none';
      return;
    }
    // Admin sees everything
    if (isAdmin) { el.style.display = ''; return; }
    // Check the user's permissions from the backend
    const permAllowed = permsSet.has(required) ||
      !['ptt','cw','band','mode','freq','split','rotator','settings','log'].includes(required);
    el.style.display = permAllowed ? '' : 'none';
  });

  // data-perm-disable — PTT/CW only, for non-operators
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

  // Role badge
  const roleEl = document.getElementById('user-role');
  if (roleEl) {
    roleEl.textContent = role.toUpperCase();
    roleEl.className   = `role-badge role-${role}`;
  }

  // Tabs visible to admin only
  const adminTab = document.getElementById('tab-admin');
  if (adminTab) adminTab.style.display = isAdmin ? '' : 'none';

  const internetTab = document.getElementById('tab-internet');
  if (internetTab) internetTab.style.display = isAdmin ? '' : 'none';
}

// Allow re-applying permissions (e.g. after an admin change)
function reapplyPermissions() {
  if (window.CurrentUser) applyPermissions(window.CurrentUser);
}

// ── Logout ───────────────────────────────────────────────────────────────────
async function logout() {
  await fetch('/api/auth/logout', { method: 'POST', credentials: 'include' }).catch(() => {});
  localStorage.removeItem('token');
  sessionStorage.removeItem('ham_token');
  location.replace('/login.html');
}

// ── Add the auth header to fetch ──────────────────────────────────────────────
// Patch the global fetch — automatically adds the Bearer token to every /api/*
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

// ── Export ───────────────────────────────────────────────────────────────────
window.Auth = { checkSession, logout, applyPermissions, reapplyPermissions };

// Auto-check the session on load
document.addEventListener('DOMContentLoaded', () => checkSession());

})();
