/**
 * com_bridge.js - COM Bridge (Windows EXE client).
 *
 * Informational version: every user has FIXED 2 CI-V ports. The section in
 * the web UI is informational only (no per-user configuration). This
 * module only handles:
 *   - Status badge (how many clients online)
 *   - Admin: connected-client stats (/api/com/stats)
 */
(function () {
'use strict';

async function load() {
  _applyRoleVisibility();
  _checkAdmin();
  if (window.AppState?.role === 'admin') {
    loadStats();
  }
}

function _applyRoleVisibility() {
  // Viewer has NO access to the bridges (COM Bridge) - web UI only.
  // Hide both COM Bridge sections (Configuration + Settings) for viewers.
  // Operator and admin can see them.
  const role = window.AppState?.role || 'viewer';
  const isViewer = (role === 'viewer');
  ['com-bridge-config-section', 'com-bridge-settings-card'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = isViewer ? 'none' : '';
  });
}

function _checkAdmin() {
  const isAdmin = (window.AppState?.role === 'admin');
  const box = document.getElementById('cb-admin-stats-box');
  if (box) box.style.display = isAdmin ? '' : 'none';
}

async function loadStats() {
  const list = document.getElementById('cb-stats-list');
  if (!list) return;
  list.innerHTML = `<div style="color:var(--dim);">${I18n.t('settings_loading')}</div>`;

  try {
    const r = await fetch('/api/com/stats');
    if (!r.ok) {
      list.innerHTML = `<div style="color:var(--red);">${I18n.t('log_error_prefix')}HTTP ${r.status} ${I18n.t('cfg_admin_only')}</div>`;
      return;
    }
    const d = await r.json();
    _renderStats(d);
  } catch (e) {
    list.innerHTML = `<div style="color:var(--red);">${I18n.t('log_error_prefix')}${e.message}</div>`;
  }
}

function _renderStats(data) {
  const list = document.getElementById('cb-stats-list');
  if (!list) return;

  const n = data.connected_clients || 0;

  const badge = document.getElementById('cb-client-status');
  if (badge) {
    badge.removeAttribute('data-i18n');  // see the note at rot-status-badge (rotormini.js)
    if (n > 0) {
      badge.textContent = I18n.t(n > 1 ? 'cfg_client_online_n' : 'cfg_client_online_1').replace('{n}', n);
      badge.style.color = 'var(--green)';
    } else {
      badge.textContent = I18n.t('cfg_combridge_no_client');
      badge.style.color = 'var(--dim)';
    }
  }

  if (n === 0) {
    list.innerHTML = `<div style="padding:10px;color:var(--dim);">${I18n.t('cfg_no_client_connected')}</div>`;
    return;
  }

  const clients = data.clients || [];
  list.innerHTML = `
    <div style="padding:8px 0;color:var(--green);font-size:12px;">
      ${I18n.t(n > 1 ? 'cfg_clients_connected_n' : 'cfg_clients_connected_1').replace('{n}', n)}
    </div>
    <table style="width:100%;border-collapse:collapse;font-size:10px;">
      <thead>
        <tr style="background:var(--panel3);color:var(--dim);">
          <th style="text-align:left;padding:6px;">IP klienta</th>
          <th style="text-align:left;padding:6px;">Użytkownik</th>
          <th style="text-align:left;padding:6px;">COM porty</th>
          <th style="text-align:left;padding:6px;">Przypisania</th>
          <th style="text-align:right;padding:6px;">Ruch (B/s)</th>
          <th style="text-align:right;padding:6px;">Uptime</th>
        </tr>
      </thead>
      <tbody>
        ${clients.map(c => `
          <tr style="border-top:1px solid var(--border);">
            <td style="padding:6px;color:var(--fg);">${c.peer || '?'}</td>
            <td style="padding:6px;color:var(--fg);">${c.user_id || '?'}</td>
            <td style="padding:6px;color:var(--dim);">${(c.ports || []).join(', ') || '—'}</td>
            <td style="padding:6px;color:var(--dim);">
              ${(c.assignments || []).map(a =>
                `<span style="color:${a.com ? 'var(--green)' : 'var(--amber)'};">${a.com || '?'}=${a.service}</span>`
              ).join(' ') || '—'}
            </td>
            <td style="padding:6px;text-align:right;color:${c.rate_bytes > 0 ? 'var(--green)' : 'var(--dim)'};">
              ${c.rate_bytes || 0}
            </td>
            <td style="padding:6px;text-align:right;color:var(--dim);">
              ${_formatUptime(c.connected_s || 0)}
            </td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

function _formatUptime(seconds) {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}min`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}min`;
}

window.ComBridge = { load, loadStats };

})();
