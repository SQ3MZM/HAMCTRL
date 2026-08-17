/*
 * admin_status.js — Panel statusu serwera + backup/restore (zakladka Admin).
 */
window.AdminStatus = (function() {

  function _fmtUptime(s) {
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    const parts = [];
    if (d) parts.push(`${d}d`);
    if (h || d) parts.push(`${h}h`);
    parts.push(`${m}m`);
    return parts.join(' ');
  }

  function _dot(ok, labelOk, labelBad) {
    const color = ok ? 'var(--green)' : 'var(--red)';
    const label = ok ? labelOk : labelBad;
    return `<span style="color:${color};">● ${label}</span>`;
  }

  async function refresh() {
    const body = document.getElementById('admin-status-body');
    if (!body) return;
    try {
      const r = await fetch('/api/status', { credentials: 'include' });
      if (!r.ok) {
        body.innerHTML = `<div style="color:var(--red);">${I18n.t('log_error_prefix')}HTTP ${r.status}</div>`;
        return;
      }
      const d = await r.json();
      if (!d.ok) return;

      const rig = d.rig || {};
      const audio = d.audio || {};
      const sys = d.system || {};

      // Zbuduj grid statusu
      const rows = [
        [I18n.t('adm_stat_version'), `${d.version} (Python ${d.python}, ${d.platform})`],
        [I18n.t('adm_stat_uptime'), _fmtUptime(d.uptime_s)],
        [I18n.t('adm_stat_online'), I18n.t('adm_stat_users_count').replace('{n}', d.online_count)],
        [I18n.t('adm_stat_radio'), rig.sim
          ? _dot(false, '', I18n.t('adm_stat_sim'))
          : _dot(rig.connected, `${I18n.t('adm_stat_civ_connected')} (${rig.backend})`, I18n.t('adm_stat_disconnected'))],
        [I18n.t('adm_stat_model_port'), `${rig.model || '?'} @ ${rig.port || '?'}`
          + (rig.speed && rig.speed !== '?' ? ` (${rig.speed} bd)` : '')],
        [I18n.t('adm_stat_freq'), rig.freq ? `${(rig.freq/1e6).toFixed(3)} MHz` : '—'],
        [I18n.t('adm_stat_audio'), _dot(true, `${audio.backend}${audio.rust ? ' (Rust)' : ''}`, '')],
        [I18n.t('adm_stat_dxcluster'), _dot(d.dxcluster?.available, I18n.t('adm_stat_available'), I18n.t('adm_stat_unavailable'))],
        [I18n.t('adm_stat_relays'), d.relay?.available
          ? _dot(d.relay.connected, I18n.t('adm_stat_relays_connected'), I18n.t('adm_stat_relays_configured'))
          : `<span style="color:var(--dim);">${I18n.t('adm_stat_no_module')}</span>`],
      ];

      // CPU/RAM jesli dostepne
      if (sys.cpu_pct !== null && sys.cpu_pct !== undefined) {
        const cpuColor = sys.cpu_pct > 80 ? 'var(--red)' : sys.cpu_pct > 50 ? 'var(--amber)' : 'var(--green)';
        rows.push(['CPU', `<span style="color:${cpuColor};">${sys.cpu_pct.toFixed(0)}%</span>`]);
      }
      if (sys.ram_pct !== null && sys.ram_pct !== undefined) {
        const ramColor = sys.ram_pct > 85 ? 'var(--red)' : sys.ram_pct > 65 ? 'var(--amber)' : 'var(--green)';
        rows.push(['RAM', `<span style="color:${ramColor};">${sys.ram_pct.toFixed(0)}% (${sys.ram_used_mb} MB)</span>`]);
      }

      body.innerHTML = `
        <div style="display:grid;grid-template-columns:140px 1fr;gap:4px 12px;">
          ${rows.map(([k,v]) => `
            <div style="color:var(--dim);">${k}:</div>
            <div style="color:var(--fg);">${v}</div>
          `).join('')}
        </div>
        <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--border);">
          <a href="/perf" target="_blank" rel="noopener"
             style="color:var(--green);text-decoration:none;font-size:12px;">
            ${I18n.t('adm_stat_perf_link')}
          </a>
        </div>`;
    } catch(e) {
      body.innerHTML = `<div style="color:var(--red);">${I18n.t('log_error_prefix')}${e.message}</div>`;
    }
  }

  async function testCiv() {
    const res = document.getElementById('admin-civ-test-result');
    if (res) { res.textContent = I18n.t('adm_testing'); res.style.color = 'var(--dim)'; }
    try {
      const r = await fetch('/api/status/test_civ', { method: 'POST', credentials: 'include' });
      const d = await r.json();
      if (res) {
        res.textContent = (d.ok ? '✓ ' : '✕ ') + (d.message || '');
        res.style.color = d.ok ? 'var(--green)' : 'var(--red)';
      }
    } catch(e) {
      if (res) { res.textContent = '✕ ' + e.message; res.style.color = 'var(--red)'; }
    }
  }

  async function downloadBackup() {
    const status = document.getElementById('admin-backup-status');
    if (status) { status.textContent = I18n.t('adm_generating'); status.style.color = 'var(--dim)'; }
    try {
      const r = await fetch('/api/backup', { credentials: 'include' });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const data = await r.json();
      // Pobierz jako plik
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      const ts = new Date().toISOString().slice(0,19).replace(/[:T]/g, '-');
      a.href = url;
      a.download = `sp3gsk-backup-${ts}.json`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      if (status) { status.textContent = I18n.t('adm_downloaded'); status.style.color = 'var(--green)'; }
    } catch(e) {
      if (status) { status.textContent = '✕ ' + e.message; status.style.color = 'var(--red)'; }
    }
  }

  async function uploadBackup(input) {
    const status = document.getElementById('admin-backup-status');
    const file = input.files?.[0];
    if (!file) return;
    if (!await window.UI?.confirmModal(I18n.t('adm_confirm_restore').replace('{name}', file.name), { danger: true, okLabel: I18n.t('adm_restore_btn') })) {
      input.value = '';
      return;
    }
    if (status) { status.textContent = I18n.t('adm_loading_ellipsis'); status.style.color = 'var(--dim)'; }
    try {
      const text = await file.text();
      const backup = JSON.parse(text);
      const r = await fetch('/api/restore', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backup }),
      });
      const d = await r.json();
      if (r.ok && d.ok) {
        if (status) {
          status.textContent = I18n.t('adm_restored').replace('{list}', d.restored.join(', '));
          status.style.color = 'var(--green)';
        }
        window.UI?.showToast?.(I18n.t('adm_toast_restored'), 'info');
      } else {
        if (status) { status.textContent = '✕ ' + (d.error || 'Błąd'); status.style.color = 'var(--red)'; }
      }
    } catch(e) {
      if (status) { status.textContent = '✕ ' + e.message; status.style.color = 'var(--red)'; }
    } finally {
      input.value = '';
    }
  }

  return { refresh, testCiv, downloadBackup, uploadBackup };
})();
