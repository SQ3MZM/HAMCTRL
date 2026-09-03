
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

// Mirrors admin.js's DEFAULT_PERMS and webapp.py's _has_perm fallback -
// used below ONLY for a key entirely ABSENT from the user's stored
// permissions (never for one explicitly set to false).
const _DEFAULT_PERMS = {
  ptt:true, rfPower:true, rfGain:true, mode:true, band:true,
  freq:true, cw:true, rotator:true, log:true, settings:false, admin:false,
};

// Keys with NO per-user permission at all (2026-09-03, explicit decision):
// general radio capabilities governed ONLY by the per-rig static feature
// whitelist (KONFIGURACJA -> FUNKCJE RADIA) - same for every operator, no
// per-user override. "split" was the first (needed for PTT to work
// correctly with a split-frequency setup); "tuner"/"autotune" followed
// the same reasoning (they used to piggyback on the unrelated freq/ptt
// permissions). Used below to skip the permsSet check entirely for these.
const FEATURE_ONLY_KEYS = new Set(['split', 'tuner', 'autotune']);

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
    // FIX (2026-09-03, live report: "zwykly user nie widzi buttona
    // splitu"): a key absent from permsRaw entirely used to be treated
    // identically to one explicitly set to false - wrong for a key like
    // "split" that only started actually being enforced this session
    // (the SPLIT button used to be gated by "freq" instead, so most
    // existing accounts never got an explicit permissions.split value
    // saved at all). Missing now falls back to the same default a
    // brand-new user gets - see the matching fix in webapp.py's
    // _has_perm for the full story (both sides must agree, or the UI
    // and the server enforcement disagree on what's allowed).
    const merged = { ..._DEFAULT_PERMS, ...permsRaw };
    permsSet = new Set(Object.entries(merged).filter(([,v]) => v).map(([k]) => k));
  }

  // Get the active static features (from RadioFunctions' last refresh)
  const activeFeatures = new Set((window._activeStaticFeatures || []).map(f => f.id || f));
  const featuresLoaded = window._activeStaticFeatures !== undefined && activeFeatures.size > 0;

  // FIX (2026-09-03, live report: "admin zaznaczyl split w KONFIGURACJI,
  // zwykly user dalej go nie widzi"): activeFeatures/featuresLoaded above
  // were computed but NEVER actually consulted anywhere in this
  // function - data-perm only ever checked the per-USER permsSet, so the
  // admin's per-RIG whitelist (KONFIGURACJA -> FUNKCJE RADIA, features.py's
  // FEATURES, saved as enabledFeatures) had ZERO effect on whether a
  // hardcoded button like #split-btn was shown, even though the backend
  // (webapp.py) already correctly requires BOTH gates for the actual
  // action (_has_perm for the per-user permission AND _feature_allowed
  // for the per-rig whitelist) - only the frontend's VISIBILITY check was
  // missing half of that. Maps a data-perm value to the matching FEATURES
  // id where one exists (not every data-perm key is a rig capability -
  // cw/rotator/settings/view/admin/band have no FEATURES entry and stay
  // governed by the per-user permission alone).
  const FEATURE_ID_FOR_PERM = {
    freq: 'freq_set', mode: 'mode_set', split: 'split', ptt: 'ptt',
    tuner: 'tuner', autotune: 'autotune',
  };

  // data-perm elements — show/hide
  document.querySelectorAll('[data-perm]').forEach(el => {
    const required = el.dataset.perm;
    if (required === 'admin') {
      el.style.display = isAdmin ? '' : 'none';
      return;
    }
    // Admin sees everything
    if (isAdmin) { el.style.display = ''; return; }
    // Check the user's permissions from the backend - skipped entirely
    // for FEATURE_ONLY_KEYS (e.g. "split", see above), which have no
    // per-user permission at all.
    const permAllowed = FEATURE_ONLY_KEYS.has(required) || permsSet.has(required) ||
      !['ptt','cw','band','mode','freq','rotator','settings','log'].includes(required);
    // Check the admin's per-rig static-feature whitelist. Fails OPEN
    // while features haven't loaded yet (avoids a flash-of-hidden-content
    // before RadioFunctions' first fetch completes) - reapplyPermissions()
    // re-runs this once they do (see radiofunctions.js), so any brief
    // over-showing self-corrects a moment later; the backend enforces the
    // real gate regardless of what's momentarily displayed.
    const featId = FEATURE_ID_FOR_PERM[required];
    const featureAllowed = !featId || !featuresLoaded || activeFeatures.has(featId);
    el.style.display = (permAllowed && featureAllowed) ? '' : 'none';
  });

  // data-perm-disable — grayed-out controls (rotator/cw/ptt/freq/vfo/split).
  // Same two-gate fix as data-perm above: some of these keys (freq, ptt)
  // also correspond to a per-rig static feature the admin can toggle in
  // KONFIGURACJA -> FUNKCJE RADIA - both gates must allow it. FEATURE_ONLY_KEYS
  // (split) skip the permission check entirely, same as above.
  document.querySelectorAll('[data-perm-disable]').forEach(el => {
    const required = el.dataset.permDisable;
    const permOk = isAdmin || FEATURE_ONLY_KEYS.has(required) || permsSet.has(required);
    const featId = FEATURE_ID_FOR_PERM[required];
    const featureOk = isAdmin || !featId || !featuresLoaded || activeFeatures.has(featId);
    const has = permOk && featureOk;
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
