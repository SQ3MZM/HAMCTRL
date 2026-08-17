/*
 * relay_admin.js - Panel konfiguracji przekaznikow Arduino w zakladce KONFIGURACJA
 *
 * Uzywa /api/relay/config GET/POST.
 * Renderuje 8 pol konfiguracji przekaznikow (nazwa, tryb, czas impulsu, widocznosc).
 */

(function() {
  const RELAY_COUNT = 8;
  let _config = { enabled: false, port: '', baudrate: 9600, relays: [] };
  let _ports = [];
  let _maxPulse = 10.0;

  async function load() {
    try {
      const r = await fetch('/api/relay/config', { credentials: 'include' });
      if (!r.ok) return;
      const data = await r.json();
      if (!data.ok) return;
      _config = data.config || { enabled: false, port: '', baudrate: 9600, relays: [] };
      _ports = data.ports || [];
      _maxPulse = data.max_pulse_s || 10.0;

      // Wypelnij UI
      document.getElementById('relay-enabled').checked = !!_config.enabled;
      const portSel = document.getElementById('relay-port');
      portSel.innerHTML = `<option value="">${I18n.t('adm_choose_port')}</option>`;
      _ports.forEach(p => {
        const opt = document.createElement('option');
        opt.value = p.device;
        opt.textContent = `${p.device}${p.description ? ' — ' + p.description : ''}`;
        if (p.device === _config.port) opt.selected = true;
        portSel.appendChild(opt);
      });
      // Jesli zapisany port nie ma na liscie, dodaj go
      if (_config.port && !_ports.find(p => p.device === _config.port)) {
        const opt = document.createElement('option');
        opt.value = _config.port;
        opt.textContent = _config.port + I18n.t('adm_port_unavailable');
        opt.selected = true;
        portSel.appendChild(opt);
      }
      document.getElementById('relay-baudrate').value = String(_config.baudrate || 9600);

      // Badge statusu
      const badge = document.getElementById('relay-status-badge');
      if (badge) {
        if (data.connected) {
          badge.textContent = I18n.t('adm_relay_connected_badge');
          badge.style.color = 'var(--green)';
          badge.style.background = 'rgba(184,201,143,0.15)';
        } else if (_config.enabled) {
          badge.textContent = I18n.t('adm_relay_error_badge');
          badge.style.color = 'var(--red)';
          badge.style.background = 'rgba(217,119,106,0.15)';
        } else {
          badge.textContent = I18n.t('in_stopped_badge');
          badge.style.color = 'var(--dim)';
        }
      }

      renderRelays();
    } catch(e) {
      console.warn('[relay-admin] load blad:', e);
    }
  }

  function renderRelays() {
    const list = document.getElementById('relay-list');
    if (!list) return;
    // Uzupelnij tablice do 8 elementow domyslnymi wartosciami
    const relays = [];
    for (let i = 0; i < RELAY_COUNT; i++) {
      const existing = (_config.relays || []).find(r => r.id === i);
      relays.push(existing || {
        id: i,
        name: I18n.t('perm_relay_n').replace('{n}', i),
        mode: 'manual',
        pulse_s: 1.0,
        visible: true,
      });
    }

    list.innerHTML = relays.map(r => `
      <div style="display:grid;grid-template-columns:24px 1fr 130px 90px 80px;gap:6px;align-items:center;padding:6px;border:1px solid var(--border);border-radius:3px;background:var(--panel2);">
        <div style="font-family:var(--mono);font-size:11px;color:var(--dim);text-align:center;">${r.id}</div>
        <input type="text" data-relay-id="${r.id}" data-field="name"
          value="${_escapeHtml(r.name)}" maxlength="30" placeholder="${I18n.t('adm_relay_name_ph')}"
          style="font-family:var(--mono);font-size:11px;padding:4px 6px;background:var(--panel3);border:1px solid var(--border);color:var(--fg);border-radius:3px;">
        <select data-relay-id="${r.id}" data-field="mode"
          style="font-family:var(--mono);font-size:10px;padding:4px;background:var(--panel3);border:1px solid var(--border);color:var(--fg);border-radius:3px;">
          <option value="manual" ${r.mode === 'manual' ? 'selected' : ''}>${I18n.t('adm_relay_manual_opt')}</option>
          <option value="momentary" ${r.mode === 'momentary' ? 'selected' : ''}>${I18n.t('adm_relay_momentary_opt')}</option>
        </select>
        <input type="number" data-relay-id="${r.id}" data-field="pulse_s"
          value="${r.pulse_s}" min="0.1" max="${_maxPulse}" step="0.1"
          ${r.mode === 'manual' ? 'disabled' : ''}
          style="font-family:var(--mono);font-size:11px;padding:4px 6px;background:var(--panel3);border:1px solid var(--border);color:var(--fg);border-radius:3px;">
        <label style="display:flex;align-items:center;gap:4px;font-family:var(--mono);font-size:10px;color:var(--dim);cursor:pointer;">
          <input type="checkbox" data-relay-id="${r.id}" data-field="visible" ${r.visible ? 'checked' : ''}
            style="width:12px;height:12px;">
          ${I18n.t('adm_visible_lbl')}
        </label>
      </div>
    `).join('');

    // Podepnij event: zmiana modu wyszarza/wlacza pole pulse_s
    list.querySelectorAll('select[data-field="mode"]').forEach(sel => {
      sel.addEventListener('change', (e) => {
        const rid = e.target.dataset.relayId;
        const pulseInput = list.querySelector(`input[data-relay-id="${rid}"][data-field="pulse_s"]`);
        if (pulseInput) pulseInput.disabled = e.target.value === 'manual';
      });
    });
  }

  function _escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, m => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[m]));
  }

  async function save() {
    const status = document.getElementById('relay-save-status');
    if (status) { status.textContent = I18n.t('dx_saving'); status.style.color = 'var(--dim)'; }

    // Zbierz dane
    const list = document.getElementById('relay-list');
    const relays = [];
    for (let i = 0; i < RELAY_COUNT; i++) {
      const nameEl = list.querySelector(`input[data-relay-id="${i}"][data-field="name"]`);
      const modeEl = list.querySelector(`select[data-relay-id="${i}"][data-field="mode"]`);
      const pulseEl = list.querySelector(`input[data-relay-id="${i}"][data-field="pulse_s"]`);
      const visEl = list.querySelector(`input[data-relay-id="${i}"][data-field="visible"]`);
      relays.push({
        id: i,
        name: (nameEl?.value || I18n.t('perm_relay_n').replace('{n}', i)).trim(),
        mode: modeEl?.value || 'manual',
        pulse_s: parseFloat(pulseEl?.value || '1.0'),
        visible: !!(visEl?.checked),
      });
    }

    const payload = {
      enabled: document.getElementById('relay-enabled').checked,
      port: document.getElementById('relay-port').value,
      baudrate: parseInt(document.getElementById('relay-baudrate').value, 10),
      relays,
    };

    try {
      const r = await fetch('/api/relay/config', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (r.ok && data.ok) {
        if (status) { status.textContent = I18n.t('cfg_saved_capital'); status.style.color = 'var(--green)'; }
        setTimeout(load, 1500); // odswiez status polaczenia
      } else {
        if (status) { status.textContent = '✕ ' + (data.error || I18n.t('profile_error_fallback')); status.style.color = 'var(--red)'; }
      }
    } catch(e) {
      if (status) { status.textContent = '✕ ' + e.message; status.style.color = 'var(--red)'; }
    }
  }

  // Podepnij do globalnego Admin objektu
  window.Admin = window.Admin || {};
  window.Admin.loadRelayConfig = load;
  window.Admin.renderRelays = renderRelays;
  window.Admin.saveRelayConfig = save;

  // Auto-load przy wejsciu na page-config
  document.addEventListener('DOMContentLoaded', () => {
    // Wywolaj gdy user przelaczy sie na zakladke konfiguracji
    document.getElementById('tab-config')?.addEventListener('click', () => {
      setTimeout(load, 300);
    });
    // Fallback: sprawdzaj co 500ms czy strona config jest widoczna i nie zaladowana
    let lastLoad = 0;
    setInterval(() => {
      const page = document.getElementById('page-config');
      if (page && page.classList.contains('active') && Date.now() - lastLoad > 5000) {
        // Zaladuj tylko jesli od ostatniego minelo 5s (nie spamuj przy odswiezaniu)
        const list = document.getElementById('relay-list');
        if (list && list.children.length === 0) {
          lastLoad = Date.now();
          load();
        }
      }
    }, 500);
  });
})();
