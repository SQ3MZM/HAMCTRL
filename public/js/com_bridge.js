/**
 * com_bridge.js - COM Bridge (klient EXE Windows).
 *
 * Wersja informacyjna (2026-07-05): kazdy user ma STALE 2 porty CI-V.
 * Sekcja w web UI jest tylko informacyjna (bez konfiguracji per-user).
 * Ten modul obsluguje tylko:
 *   - Badge statusu (ilu klientow online)
 *   - Admin: statystyki podlaczonych klientow (/api/com/stats)
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
  // Viewer NIE ma dostepu do mostow (COM Bridge) - tylko web UI.
  // Ukrywamy obie sekcje COM Bridge (Konfiguracja + Ustawienia) dla viewera.
  // Operator i admin je widza. (fix 2026-07-05 SQ3MZM)
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
  list.innerHTML = `<div style="color:var(--dim);">Ładowanie...</div>`;

  try {
    const r = await fetch('/api/com/stats');
    if (!r.ok) {
      list.innerHTML = `<div style="color:var(--red);">Błąd: HTTP ${r.status} (tylko admin)</div>`;
      return;
    }
    const d = await r.json();
    _renderStats(d);
  } catch (e) {
    list.innerHTML = `<div style="color:var(--red);">Błąd: ${e.message}</div>`;
  }
}

function _renderStats(data) {
  const list = document.getElementById('cb-stats-list');
  if (!list) return;

  const n = data.connected_clients || 0;

  const badge = document.getElementById('cb-client-status');
  if (badge) {
    if (n > 0) {
      badge.textContent = `● ${n} klient${n > 1 ? 'ów' : ''} online`;
      badge.style.color = 'var(--green)';
    } else {
      badge.textContent = '— brak podłączonego klienta —';
      badge.style.color = 'var(--dim)';
    }
  }

  if (n === 0) {
    list.innerHTML = `<div style="padding:10px;color:var(--dim);">Brak podłączonych klientów.</div>`;
    return;
  }

  const clients = data.clients || [];
  list.innerHTML = `
    <div style="padding:8px 0;color:var(--green);font-size:12px;">
      ● ${n} klient${n > 1 ? 'ów' : ''} podłączony${n > 1 ? 'ch' : ''}
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
