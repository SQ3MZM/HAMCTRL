/**
 * hamlib_ui.js — VIRTUAL RADIO panel (Hamlib NET rigctl emulation, SETTINGS)
 *
 * Visible to everyone (so the user knows which ports to connect their
 * program to), but enabling/disabling TCP ports and saving the config —
 * ADMIN ONLY. Toggling these servers affects all users at once (shared
 * port), so not everyone with access to the panel can do it — for other
 * roles it's a plain status view.
 */
(function () {
'use strict';

async function load() {
  const box = document.getElementById('hamlib-slots');
  if (!box) return;
  box.innerHTML = `<div style="font-family:var(--mono);font-size:10px;color:var(--dim);">${I18n.t('settings_loading')}</div>`;
  try {
    const r = await fetch('/api/hamlib/status');
    const d = await r.json();
    _render(d.servers || []);
  } catch (e) {
    box.innerHTML = `<div style="font-family:var(--mono);font-size:10px;color:var(--red);">${I18n.t('hamlib_load_error')}</div>`;
  }
}

function _render(servers) {
  const box = document.getElementById('hamlib-slots');
  if (!box) return;
  const isAdmin = window.AppState?.role === 'admin';
  const saveBtn = document.getElementById('hamlib-save-btn');
  if (saveBtn) saveBtn.style.display = isAdmin ? '' : 'none';

  if (!servers.length) {
    box.innerHTML = `<div style="font-family:var(--mono);font-size:10px;color:var(--dim);">${I18n.t('hamlib_no_ports')}</div>`;
    return;
  }

  box.innerHTML = servers.map((s, i) => {
    const clientWord = s.clients === 1 ? I18n.t('hamlib_client_singular') : I18n.t('hamlib_client_plural');
    const running = s.running
      ? `<span style="color:var(--green);">● ${I18n.t('hamlib_active')}</span> · ${s.clients || 0} ${clientWord}`
      : `<span style="color:var(--dim);">${I18n.t('hamlib_disabled')}</span>`;
    if (isAdmin) {
      return `
        <div style="display:flex;align-items:center;gap:8px;padding:8px;background:var(--panel2);border:1px solid var(--border);border-radius:6px;">
          <input type="checkbox" data-slot="${i}" class="hamlib-enabled" ${s.enabled ? 'checked' : ''}
            title="${I18n.t('hamlib_toggle_port_title')}">
          <input type="number" data-slot="${i}" class="hamlib-port" value="${s.port}" min="1024" max="65535"
            style="width:70px;font-family:var(--mono);font-size:11px;">
          <input type="text" data-slot="${i}" class="hamlib-label" value="${_esc(s.label || '')}"
            style="flex:1;font-family:var(--mono);font-size:11px;">
          <span style="font-family:var(--mono);font-size:9px;white-space:nowrap;">${running}</span>
        </div>`;
    }
    return `
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px;background:var(--panel2);border:1px solid var(--border);border-radius:6px;">
        <span style="font-family:var(--mono);font-size:11px;color:var(--fg);">
          <b>${s.port}</b> — ${_esc(s.label || '')}
        </span>
        <span style="font-family:var(--mono);font-size:9px;white-space:nowrap;">${running}</span>
      </div>`;
  }).join('');
}

function _esc(s) {
  return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function save() {
  // The panel hides this button for non-admins, but the backend enforces
  // it anyway (role != admin -> 403) — this is just a local guard against
  // an accidental call before the DOM has re-rendered.
  if (window.AppState?.role !== 'admin') return;
  const msg = document.getElementById('hamlib-msg');
  const rows = document.querySelectorAll('#hamlib-slots [data-slot]');
  const bySlot = {};
  rows.forEach(el => {
    const i = el.dataset.slot;
    bySlot[i] = bySlot[i] || {};
    if (el.classList.contains('hamlib-enabled')) bySlot[i].enabled = el.checked;
    if (el.classList.contains('hamlib-port'))    bySlot[i].port    = parseInt(el.value) || 0;
    if (el.classList.contains('hamlib-label'))   bySlot[i].label   = el.value;
  });
  const servers = Object.keys(bySlot).sort((a, b) => a - b).map(k => bySlot[k]);
  if (msg) msg.textContent = I18n.t('hamlib_saving');
  try {
    const r = await fetch('/api/hamlib/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ servers }),
    });
    const d = await r.json();
    if (msg) msg.textContent = d.message || (d.ok ? I18n.t('settings_saved_short') : (d.error || I18n.t('status_error_generic')));
    await load();
  } catch (e) {
    if (msg) msg.textContent = I18n.t('settings_save_error_plain');
  }
}

window.HamlibUI = { load, save };

})();
