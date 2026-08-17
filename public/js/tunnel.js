
/**
 * tunnel.js — panel dostępu przez internet
 * Tryby:
 *   off    — tylko LAN
 *   quick  — Cloudflare Quick Tunnel (losowy adres, bez konta)
 *   custom — własna domena przez Cloudflare Tunnel token
 */
(function () {
'use strict';

let state = { mode:'off', status:'stopped', publicUrl:'', localUrl:'', error:'', certDaysLeft: null };
let _saveTimer = null;

let _refreshTimer = null;

// ── Ładowanie stanu i konfiguracji ───────────────────────────────────────────
async function load() {
  try {
    // UWAGA: auth.js patchuje globalny window.fetch dodajac Bearer token
    // do wszystkich /api/* zadan automatycznie. Nie potrzeba _auth() helpera.
    // Wczesniejsza wersja miala _auth() ktore NIE istnialo -> ReferenceError
    // -> load() cicho failowal -> UI nigdy nie dostawal aktualnego statusu.
    const [sr, cr] = await Promise.all([
      fetch('/api/tunnel/status'),
      fetch('/api/tunnel/config'),
    ]);
    if (!sr.ok || !cr.ok) {
      console.warn('[tunnel] load: HTTP', sr.status, cr.status);
      return;
    }
    const st  = await sr.json();
    state = { ...state, ...st };
    const cfg = await cr.json();
    applyConfig(cfg);
    render();
  } catch(e) { console.warn('[tunnel] load:', e); }
}

function startAutoRefresh() {
  stopAutoRefresh();
  _refreshTimer = setInterval(load, 10000);
}

function stopAutoRefresh() {
  if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
}

function applyConfig(cfg) {
  const _s = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ''; };
  const _c = (id, val) => { const el = document.getElementById(id); if (el) el.checked = !!val; };

  _s('tn-mode',        cfg.mode);
  _s('tn-token',       cfg.token);
  _s('tn-hostname',    cfg.hostname);
  _s('tn-duck-domain', cfg.duckDomain);
  _s('tn-duck-token',  cfg.duckToken);
  _s('tn-static-ip',   cfg.staticIp);
  _s('tn-static-port', cfg.staticPort || '8001');
  _s('tn-custom-hostname', cfg.customHostname);
  _s('tn-custom-port',     cfg.customPort || '8001');
  _s('tn-custom-certpath', cfg.customCertPath);
  _s('tn-custom-keypath',  cfg.customKeyPath);
  _c('tn-autostart',   cfg.autoStart);

  modeChanged(cfg.mode || 'off');
}

// ── WS handler ────────────────────────────────────────────────────────────────
function handleWS(msg) {
  if (msg.type !== 'tunnel_update') return;
  state = msg.tunnel;
  render();
  if (msg.tunnel.publicUrl && msg.tunnel.status === 'connected') {
    window.UI?.showToast(I18n.t('in_toast_tunnel_active').replace('{url}', msg.tunnel.publicUrl));
  }
}

// ── Render ────────────────────────────────────────────────────────────────────
function render() {
  renderBadge();
  renderUrl();
  renderError();
  renderDuckPanel();
}

function renderDuckPanel() {
  const days    = state.certDaysLeft;
  const daysEl  = document.getElementById('tn-cert-days');
  const stEl    = document.getElementById('tn-duck-tunnel-status');
  const addrEl  = document.getElementById('tn-duck-addr');

  if (daysEl) {
    if (days === null || days === undefined) {
      daysEl.textContent = I18n.t('in_no_cert');
      daysEl.style.color = 'var(--red)';
    } else if (days < 7) {
      daysEl.textContent = I18n.t('in_days_warn').replace('{n}', days);
      daysEl.style.color = 'var(--red)';
    } else if (days < 30) {
      daysEl.textContent = I18n.t('in_days_warn').replace('{n}', days);
      daysEl.style.color = 'var(--amber)';
    } else {
      daysEl.textContent = I18n.t('in_days_ok').replace('{n}', days);
      daysEl.style.color = 'var(--green)';
    }
  }

  if (stEl) {
    const map = {
      connected: { txt: I18n.t('in_active'),          color: 'var(--green)' },
      starting:  { txt: I18n.t('in_connecting_dots'),  color: 'var(--amber)' },
      error:     { txt: I18n.t('in_error_badge'),      color: 'var(--red)'   },
      stopped:   { txt: I18n.t('in_stopped_tunnel'),   color: 'var(--dim)'   },
    };
    const s = map[state.status] || map.stopped;
    stEl.textContent  = s.txt;
    stEl.style.color  = s.color;
  }

  if (addrEl) {
    if (state.publicUrl && state.status === 'connected') {
      addrEl.innerHTML = `<a href="${state.publicUrl}" target="_blank"
        style="color:var(--green);text-decoration:none;">${state.publicUrl}</a>`;
    } else {
      addrEl.textContent = '—';
    }
  }
}

function renderBadge() {
  const el  = document.getElementById('tn-status-badge');
  const dot = document.getElementById('tn-status-dot');
  if (!el) return;
  const map = {
    connected: { txt: I18n.t('in_active'),          color:'var(--green)',  bg:'rgba(184,201,143,0.1)' },
    starting:  { txt: I18n.t('in_connecting_dots'), color:'var(--amber)',  bg:'rgba(212,168,87,0.1)' },
    error:     { txt: I18n.t('in_error_badge'),     color:'var(--red)',    bg:'rgba(217,119,106,0.1)' },
    stopped:   { txt: I18n.t('in_stopped_badge'),   color:'var(--dim)',    bg:'transparent' },
  };
  const st = map[state.status] || map.stopped;
  el.textContent    = st.txt;
  el.style.color    = st.color;
  el.style.background = st.bg;
  if (dot) { dot.style.background = st.color; }
}

function renderUrl() {
  const pubEl   = document.getElementById('tn-public-url');
  const localEl = document.getElementById('tn-local-url');
  if (pubEl) {
    if (state.publicUrl && state.status === 'connected') {
      // Prosty link bez inline copy button - globalny KOPIUJ jest w headerze karty
      pubEl.innerHTML = `<a href="${state.publicUrl}" target="_blank"
        style="color:var(--green);text-decoration:none;font-weight:bold;">${state.publicUrl}</a>`;
    } else if (state.status === 'starting') {
      pubEl.innerHTML = `<span style="color:var(--amber);">${I18n.t('in_waiting_url')}</span>`;
    } else {
      pubEl.innerHTML = `<span style="color:var(--dim);">—</span>`;
    }
  }
  if (localEl) {
    if (state.localUrl) {
      localEl.innerHTML = `<a href="${state.localUrl}" target="_blank"
        style="color:var(--dim);text-decoration:none;">${state.localUrl}</a>`;
    } else {
      localEl.textContent = '—';
    }
  }
}

function renderError() {
  const el = document.getElementById('tn-error-msg');
  if (!el) return;
  el.textContent   = state.error || '';
  el.style.display = state.error ? 'block' : 'none';
}

function copyUrl(url) {
  navigator.clipboard?.writeText(url).then(() => {
    window.UI?.showToast(I18n.t('in_toast_addr_copied'));
  }).catch(() => {
    const inp = document.createElement('input');
    inp.value = url; document.body.appendChild(inp);
    inp.select(); document.execCommand('copy');
    document.body.removeChild(inp);
    window.UI?.showToast(I18n.t('in_toast_copied_plain'));
  });
}

// ── Zmiana trybu ─────────────────────────────────────────────────────────────
function modeChanged(mode) {
  // Ukryj wszystkie panele
  ['tn-named-fields','tn-duckdns-fields','tn-staticip-fields','tn-customcert-fields'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });

  // Pokaż odpowiedni panel
  if (mode === 'named')      { const el = document.getElementById('tn-named-fields');      if (el) el.style.display = 'block'; }
  if (mode === 'duckdns')    { const el = document.getElementById('tn-duckdns-fields');    if (el) el.style.display = 'block'; }
  if (mode === 'staticip')   { const el = document.getElementById('tn-staticip-fields');   if (el) el.style.display = 'block'; }
  if (mode === 'customcert') { const el = document.getElementById('tn-customcert-fields'); if (el) el.style.display = 'block'; }

  // Zaznacz aktywny tryb
  document.querySelectorAll('.tn-mode-btn').forEach(b => {
    const active = b.dataset.mode === mode;
    b.style.borderColor = active ? 'var(--green)' : 'var(--border)';
    b.style.color       = active ? 'var(--green)' : 'var(--dim)';
    b.style.background  = active ? 'rgba(184,201,143,0.08)' : 'var(--panel2)';
  });

  // Pokaż diagnostykę cloudflared tylko dla trybów Cloudflare
  const cfStatus = document.getElementById('tn-cf-status');
  if (cfStatus) {
    const showCf = (mode === 'quick' || mode === 'named');
    cfStatus.style.display = showCf ? 'flex' : 'none';
  }

  // Pokaż detail-pills wg trybu (cert dla DuckDNS/stałe IP/własny cert, tunel dla Cloudflare)
  const showCert   = (mode === 'duckdns' || mode === 'staticip' || mode === 'customcert');
  const showTunnel = (mode === 'quick'  || mode === 'named'   || mode === 'duckdns');
  const cert = document.getElementById('tn-detail-cert');
  const tun  = document.getElementById('tn-detail-tunnel');
  if (cert) cert.style.display = showCert   ? 'inline-block' : 'none';
  if (tun)  tun.style.display  = showTunnel ? 'inline-block' : 'none';

  const modeEl = document.getElementById('tn-mode');
  if (modeEl) modeEl.value = mode;
}

// Kopiowanie adresu publicznego do schowka
async function copyPublic() {
  const el = document.getElementById('tn-public-url');
  if (!el) return;
  // Wyciągnij URL - może być z <a> albo bezpośrednio tekst
  const link = el.querySelector('a');
  const url = (link ? link.href : el.textContent).trim();
  if (!url || url === '—') {
    window.UI?.showToast(I18n.t('in_toast_no_addr'), 'error');
    return;
  }
  try {
    await navigator.clipboard.writeText(url);
    window.UI?.showToast(I18n.t('in_toast_copied_prefix') + url);
  } catch (e) {
    // Fallback dla starszych przeglądarek
    const tmp = document.createElement('textarea');
    tmp.value = url;
    document.body.appendChild(tmp);
    tmp.select();
    try { document.execCommand('copy'); window.UI?.showToast(I18n.t('in_toast_copied_plain')); }
    catch { window.UI?.showToast(I18n.t('in_toast_copy_failed') + e.message, 'error'); }
    document.body.removeChild(tmp);
  }
}

// ── Akcje ─────────────────────────────────────────────────────────────────────
async function startTunnel() {
  const mode           = document.getElementById('tn-mode')?.value?.trim()        || 'quick';
  const token          = document.getElementById('tn-token')?.value?.trim()       || '';
  const duckDomain     = document.getElementById('tn-duck-domain')?.value?.trim() || '';
  const duckToken      = document.getElementById('tn-duck-token')?.value?.trim()  || '';
  const staticIp       = document.getElementById('tn-static-ip')?.value?.trim()   || '';
  const customHostname = document.getElementById('tn-custom-hostname')?.value?.trim() || '';

  // Walidacja per tryb
  if (mode === 'named' && !token) {
    window.UI?.showToast(I18n.t('in_toast_paste_token'), 'error'); return;
  }
  if (mode === 'duckdns' && (!duckDomain || !duckToken)) {
    window.UI?.showToast(I18n.t('in_toast_duckdns_fields'), 'error'); return;
  }
  if (mode === 'staticip' && !staticIp) {
    window.UI?.showToast(I18n.t('in_toast_enter_ip'), 'error'); return;
  }
  if (mode === 'customcert' && !customHostname) {
    window.UI?.showToast(I18n.t('in_toast_enter_domain'), 'error'); return;
  }

  // Zapisz konfigurację przed uruchomieniem
  await saveTunnelConfig(false);

  const btn = document.getElementById('tn-start-btn');
  // Usun data-i18n statycznego HTML - inaczej kolejny I18n.setLang() nadpisze
  // ten przycisk z powrotem na "URUCHOM" w trakcie faktycznego laczenia
  // (patrz ta sama uwaga przy rot-status-badge w rotormini.js).
  if (btn) { btn.removeAttribute('data-i18n'); btn.textContent = I18n.t('in_connecting_btn'); btn.disabled = true; }

  const tkn = localStorage.getItem('token') || '';
  try {
    const r   = await fetch('/api/tunnel/start', {
      method: 'POST',
      headers: {'Content-Type':'application/json',
        ...(tkn ? {'Authorization': `Bearer ${tkn}`} : {})},
      body: JSON.stringify({ mode }),
    });
    const res = await r.json();
    if (!res.ok) {
      window.UI?.showToast('✗ ' + (res.error||I18n.t('profile_error_fallback')), 'error');
    }
  } catch(e) {
    window.UI?.showToast('✗ ' + I18n.t('cfg_conn_error'), 'error');
  } finally {
    if (btn) { btn.textContent = I18n.t('in_start_btn'); btn.disabled = false; }
  }
}

async function stopTunnel() {
  await fetch('/api/tunnel/stop', { method:'POST' });
  state.status = 'stopped'; state.publicUrl = '';
  render();
  window.UI?.showToast(I18n.t('in_toast_tunnel_stopped'));
}

// Zapisz konfigurację tunelu
async function saveTunnelConfig(showToast = true) {
  const mode           = document.getElementById('tn-mode')?.value?.trim()        || 'off';
  const token          = document.getElementById('tn-token')?.value?.trim()       || '';
  const hostname       = document.getElementById('tn-hostname')?.value?.trim()    || '';
  const duckDomain     = document.getElementById('tn-duck-domain')?.value?.trim() || '';
  const duckToken      = document.getElementById('tn-duck-token')?.value?.trim()  || '';
  const staticIp       = document.getElementById('tn-static-ip')?.value?.trim()   || '';
  const staticPort     = document.getElementById('tn-static-port')?.value?.trim() || '8001';
  const customHostname = document.getElementById('tn-custom-hostname')?.value?.trim() || '';
  const customPort     = document.getElementById('tn-custom-port')?.value?.trim()     || '8001';
  const customCertPath = document.getElementById('tn-custom-certpath')?.value?.trim() || '';
  const customKeyPath  = document.getElementById('tn-custom-keypath')?.value?.trim()  || '';
  const autoStart  = document.getElementById('tn-autostart')?.checked         || false;

  // Podgląd adresu DuckDNS
  const duckPreview = document.getElementById('tn-duck-preview');
  if (duckPreview) duckPreview.textContent = duckDomain ? `${duckDomain}.duckdns.org` : 'mojradio.duckdns.org';

  const tkn = localStorage.getItem('token') || '';
  try {
    await fetch('/api/tunnel/config', {
      method: 'POST',
      headers: {'Content-Type':'application/json',
        ...(tkn ? {'Authorization': `Bearer ${tkn}`} : {})},
      body: JSON.stringify({ mode, token, hostname, duckDomain, duckToken, staticIp, staticPort,
        customHostname, customPort, customCertPath, customKeyPath, autoStart }),
    });
    if (showToast) window.UI?.showToast(I18n.t('in_toast_config_saved'));
  } catch(e) {
    if (showToast) window.UI?.showToast(I18n.t('in_toast_save_error'), 'error');
  }
}

async function checkCF() {
  const el      = document.getElementById('tn-cf-status');
  const verEl   = document.getElementById('tn-cf-version');
  const staleEl = document.getElementById('tn-cf-stale');
  const svcEl   = document.getElementById('tn-cf-svc');
  if (!el) return;
  el.style.display = 'flex';
  if (verEl) verEl.textContent = I18n.t('in_cf_checking');
  try {
    const token = localStorage.getItem('token') || '';
    const r   = await fetch('/api/tunnel/check', {
      headers: token ? {'Authorization': `Bearer ${token}`} : {}
    });
    const res = await r.json();
    if (verEl) {
      if (res.available) {
        verEl.innerHTML = `<span style="color:var(--green)">${I18n.t('in_cf_available').replace('{ver}', res.version||'')}</span>`;
      } else {
        verEl.innerHTML = `<span style="color:var(--amber)">${I18n.t('in_cf_missing')}</span>`;
      }
    }
    // Stale procs
    if (staleEl) {
      staleEl.style.display = (res.stale_procs > 0) ? 'inline' : 'none';
      if (res.stale_procs > 0) staleEl.textContent = I18n.t('in_stale_procs').replace('{n}', res.stale_procs);
    }
    // Windows service
    if (svcEl) svcEl.style.display = res.svc_installed ? 'inline' : 'none';
  } catch(e) {
    if (verEl) verEl.textContent = I18n.t('in_cf_check_error');
  }
}

async function installCertbot() {
  window.UI?.showToast(I18n.t('in_toast_installing_certbot'));
  const h = {'Authorization': `Bearer ${localStorage.getItem('token')||''}`};
  try {
    await fetch('/api/tunnel/install-certbot', {method:'POST', headers:h});
  } catch(e) { window.UI?.showToast('✗ ' + e.message, 'error'); }
}

async function genCert() {
  await saveTunnelConfig(false);
  window.UI?.showToast(I18n.t('in_toast_generating_cert'));
  const h = {'Authorization': `Bearer ${localStorage.getItem('token')||''}`};
  try {
    await fetch('/api/tunnel/gen-cert', {method:'POST', headers:h});
  } catch(e) { window.UI?.showToast('✗ ' + e.message, 'error'); }
}

async function cleanup() {
  if (!await window.UI?.confirmModal(I18n.t('in_confirm_cleanup'))) return;
  try {
    const r   = await fetch('/api/tunnel/cleanup', {method:'POST'});
    const res = await r.json();
    if (res.ok) {
      window.UI?.showToast(I18n.t('in_toast_cleaned') + (res.messages?.[0] || 'OK'));
      await checkCF();
    }
  } catch(e) {
    window.UI?.showToast(I18n.t('in_toast_cleanup_error') + e.message, 'error');
  }
}

window.Tunnel = { load, handleWS, startTunnel, stopTunnel, saveTunnelConfig, checkCF, modeChanged, cleanup, installCertbot, genCert, startAutoRefresh, stopAutoRefresh, copyPublic };
})();
