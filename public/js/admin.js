
/**
 * admin.js — panel administratora
 * Zarządzanie użytkownikami (+ uprawnienia granularne) + konfiguracja rotatorów
 */
(function() {
'use strict';

// ── Definicja uprawnień ───────────────────────────────────────────────────────
const PERM_DEFS = [
  { key: 'ptt',      label: 'PTT — nadawanie',          group: 'radio'  },
  { key: 'rfPower',  label: 'RF Power — moc TX',         group: 'radio'  },
  { key: 'rfGain',   label: 'RF/AF Gain, SQL',           group: 'radio'  },
  { key: 'mode',     label: 'Zmiana trybu (USB/CW…)',    group: 'radio'  },
  { key: 'band',     label: 'Zmiana pasma',              group: 'radio'  },
  { key: 'freq',     label: 'Strojenie częstotliwości',  group: 'radio'  },
  { key: 'split',    label: 'Split VFO A/B',             group: 'radio'  },
  { key: 'cw',       label: 'CW Keyer — wysyłanie',      group: 'radio'  },
  { key: 'rotator',  label: 'Rotator — sterowanie',      group: 'sprzet' },
  { key: 'log',      label: 'Log QSO — zapis',           group: 'system' },
  { key: 'settings', label: 'Ustawienia serwera',        group: 'system' },
  { key: 'admin',    label: 'Panel administratora',      group: 'system' },
  // Przekazniki Arduino (0-7) - kazdy przydzielany osobno
  { key: 'relay_0',  label: 'Przekaźnik 0',              group: 'relay'  },
  { key: 'relay_1',  label: 'Przekaźnik 1',              group: 'relay'  },
  { key: 'relay_2',  label: 'Przekaźnik 2',              group: 'relay'  },
  { key: 'relay_3',  label: 'Przekaźnik 3',              group: 'relay'  },
  { key: 'relay_4',  label: 'Przekaźnik 4',              group: 'relay'  },
  { key: 'relay_5',  label: 'Przekaźnik 5',              group: 'relay'  },
  { key: 'relay_6',  label: 'Przekaźnik 6',              group: 'relay'  },
  { key: 'relay_7',  label: 'Przekaźnik 7',              group: 'relay'  },
];

const DEFAULT_PERMS = {
  ptt:true, rfPower:true, rfGain:true, mode:true, band:true,
  freq:true, split:true, cw:true, rotator:true, log:true, settings:false, admin:false,
};

// ── Użytkownicy ───────────────────────────────────────────────────────────────
async function loadUsers() {
  const token = localStorage.getItem('token') || '';
  const r = await fetch('/api/users', {
    headers: token ? {'Authorization': `Bearer ${token}`} : {}
  });
  if (!r.ok) { console.warn('[admin] loadUsers HTTP', r.status); return; }
  const data = await r.json();
  renderUsers(data.users || data);
}

function renderUsers(users) {
  const tbody = document.getElementById('users-tbody');
  if (!tbody) return;
  const ROLE_COLORS = { admin:'var(--amber)', operator:'var(--green)', viewer:'var(--dim)' };

  tbody.innerHTML = users.map(u => {
    const perms = u.permissions || DEFAULT_PERMS;
    const permBadges = PERM_DEFS
      .filter(p => perms[p.key])
      .map(p => `<span style="font-family:var(--mono);font-size:9px;background:rgba(184,201,143,0.08);
        border:1px solid rgba(184,201,143,0.2);border-radius:2px;padding:1px 5px;color:var(--dim);
        white-space:nowrap;">${p.label.split('—')[0].split(' (')[0].trim()}</span>`).join('');

    // Escapuj dane od userow przed wstawieniem do HTML - user z nazwa typu
    // <img onerror=...> wykonalby skrypt w przegladarce ADMINA (kradziez
    // tokenu). Fix XSS 2026-07-05.
    const _u   = _escapeHtmlAdmin(u.username || '');
    const _nm  = _escapeHtmlAdmin(u.name || '');
    const _cs  = _escapeHtmlAdmin(u.callsign || '—');
    const _uid = String(u.id).replace(/'/g, "\\'");
    const _uUn = String(u.username || '').replace(/[\\'"]/g, '\\$&');

    return `
    <tr>
      <td>
        <span style="font-family:var(--mono);font-weight:bold;color:var(--green)">${_u}</span>
        <span style="font-size:10px;color:var(--dim);display:block">${_nm}</span>
      </td>
      <td>
        <span style="font-family:var(--mono);font-size:11px;color:${ROLE_COLORS[u.role]||'var(--dim)'}">
          ${(u.role||'').toUpperCase()}
        </span>
      </td>
      <td style="font-family:var(--mono);font-size:11px;color:var(--amber)">${_cs}</td>
      <td>
        <div style="display:flex;flex-wrap:wrap;gap:2px;max-width:260px;">${permBadges || '<span style="font-size:10px;color:var(--dim)">brak</span>'}</div>
      </td>
      <td>
        <span style="font-size:10px;color:${u.active?'var(--green)':'var(--red)'}">
          ${u.active ? '● AKTYWNY' : '○ WYŁĄCZONY'}
        </span>
      </td>
      <td>
        <div style="display:flex;gap:4px;">
          <button class="admin-btn" onclick="Admin.editUser('${_uid}')" title="Edytuj">✏</button>
          <button class="admin-btn" onclick="Admin.resetPwdDialog('${_uid}','${_uUn}')" title="Reset hasła">🔑</button>
          <button class="admin-btn danger" onclick="Admin.toggleActive('${_uid}',${u.active})" title="${u.active?'Dezaktywuj':'Aktywuj'}">
            ${u.active ? '⏸' : '▶'}
          </button>
          ${u.id !== window.CurrentUser?.id ? `<button class="admin-btn danger" onclick="Admin.deleteUser('${_uid}','${_uUn}')" title="Usuń">✕</button>` : ''}
        </div>
      </td>
    </tr>`;
  }).join('');
}

// ── Modal: dodaj użytkownika ──────────────────────────────────────────────────
function showAddUser() {
  _openModal(null);
}

async function editUser(id) {
  const token = localStorage.getItem('token') || '';
  const r = await fetch('/api/users', {
    headers: token ? {'Authorization': `Bearer ${token}`} : {}
  });
  if (!r.ok) { UI.showToast('✗ Brak dostępu', 'error'); return; }
  const data  = await r.json();
  const users = data.users || data;
  const u = users.find(x => String(x.id) === String(id));
  if (!u) { UI.showToast('✗ Nie znaleziono użytkownika', 'error'); return; }
  _openModal(u);
}

function _openModal(u) {
  const modal = document.getElementById('user-modal');
  if (!modal) return;

  const isNew = !u;
  document.getElementById('modal-title').textContent = isNew ? 'Nowy użytkownik' : `Edytuj: ${u.username}`;
  document.getElementById('user-id').value       = u?.id    || '';
  document.getElementById('uname').value         = u?.username || '';
  document.getElementById('upass').value         = '';
  document.getElementById('urole').value         = u?.role  || 'operator';
  document.getElementById('uname-full').value    = u?.name  || '';
  document.getElementById('ucallsign').value     = u?.callsign || '';
  document.getElementById('ulocator').value      = u?.locator || '';
  document.getElementById('uemail').value        = u?.email || '';
  document.getElementById('upass-row').style.display = isNew ? '' : 'none';

  // Zbuduj sekcje przekaznikow (dynamicznie, z realnymi nazwami z konfiguracji).
  // Robimy to PRZED ustawianiem checkboxow, zeby perm-relay_N juz istnialy.
  _renderRelayPerms(u?.permissions || {});

  // Ustaw checkboxy uprawnień
  const perms = u?.permissions || DEFAULT_PERMS;
  PERM_DEFS.forEach(p => {
    const cb = document.getElementById('perm-' + p.key);
    if (cb) cb.checked = !!perms[p.key];
  });

  // Admin ma zawsze wszystkie uprawnienia — zablokuj checkboxy
  _updatePermsByRole(document.getElementById('urole').value);

  modal.style.display = 'flex';
}

// Zbuduj checkboxy przekaznikow z aktualnej konfiguracji relay.
// Pokazuje TYLKO skonfigurowane przekazniki z ich realnymi nazwami
// (np. "Antena 40m" zamiast "Przekaznik 3"). Admin zaznacza ktore user widzi.
async function _renderRelayPerms(userPerms) {
  const section = document.getElementById('perm-relay-section');
  const list    = document.getElementById('perm-relay-list');
  if (!section || !list) return;

  let relays = [];
  try {
    const token = localStorage.getItem('token') || '';
    const r = await fetch('/api/relay/config', {
      headers: token ? {'Authorization': `Bearer ${token}`} : {}
    });
    if (r.ok) {
      const data = await r.json();
      relays = (data.config?.relays) || [];
    }
  } catch(e) { console.warn('[admin] relay config fetch:', e); }

  // Pokaz tylko wlaczone przekazniki (maja nazwe). Jesli brak - ukryj sekcje.
  const active = relays.filter(r => r && r.name);
  if (!active.length) {
    section.style.display = 'none';
    list.innerHTML = '';
    return;
  }
  section.style.display = '';
  list.innerHTML = active.map(r => {
    const key = 'relay_' + r.id;
    const checked = userPerms[key] ? 'checked' : '';
    const nm = _escapeHtmlAdmin(r.name);
    return `<label style="display:flex;align-items:center;gap:8px;padding:5px 0;cursor:pointer;border-bottom:1px solid rgba(255,255,255,0.04);">
      <input type="checkbox" id="perm-${key}" ${checked} style="width:14px;height:14px;accent-color:var(--green);cursor:pointer;">
      <span style="font-family:var(--mono);font-size:11px;color:var(--text);">🔌 ${nm}</span>
    </label>`;
  }).join('');

  // Zablokuj jesli admin (admin widzi wszystkie zawsze)
  const role = document.getElementById('urole')?.value;
  if (role === 'admin') {
    active.forEach(r => {
      const cb = document.getElementById('perm-relay_' + r.id);
      if (cb) { cb.checked = true; cb.disabled = true; }
    });
  }
}

function _escapeHtmlAdmin(s) {
  return String(s).replace(/[&<>"']/g, c => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]
  ));
}

function _updatePermsByRole(role) {
  const isAdmin = role === 'admin';
  PERM_DEFS.forEach(p => {
    const cb = document.getElementById('perm-' + p.key);
    if (!cb) return;
    if (isAdmin) { cb.checked = true; cb.disabled = true; }
    else { cb.disabled = false; }
  });
  // Relay checkboxy tez (generowane dynamicznie) - admin widzi wszystkie
  for (let i = 0; i < 8; i++) {
    const cb = document.getElementById('perm-relay_' + i);
    if (!cb) continue;
    if (isAdmin) { cb.checked = true; cb.disabled = true; }
    else { cb.disabled = false; }
  }
}

async function saveUser() {
  const token    = localStorage.getItem('token') || '';
  const id       = document.getElementById('user-id')?.value?.trim();
  const username = document.getElementById('uname')?.value?.trim();
  const password = document.getElementById('upass')?.value;
  const role     = document.getElementById('urole')?.value;
  const name     = document.getElementById('uname-full')?.value?.trim();
  const callsign = document.getElementById('ucallsign')?.value?.trim().toUpperCase();
  const locator  = document.getElementById('ulocator')?.value?.trim().toUpperCase();
  const email    = document.getElementById('uemail')?.value?.trim();

  if (!username) { UI.showToast('Podaj login', 'error'); return; }

  // Zbierz uprawnienia z checkboxów — admin dostaje wszystko
  const permissions = {};
  PERM_DEFS.forEach(p => {
    const cb = document.getElementById('perm-' + p.key);
    // relay_N moga nie istniec jesli przekaznik nieskonfigurowany - to OK
    // (brak checkboxa = brak dostepu). Ostrzegaj tylko dla stalych uprawnien.
    if (!cb && !p.key.startsWith('relay_')) {
      console.warn('[admin] brak checkboxa:', 'perm-' + p.key);
    }
    permissions[p.key] = (role === 'admin') ? true : !!(cb?.checked);
  });

  console.log('[admin] saveUser id=', id, 'permissions=', permissions);

  let r;
  try {
    if (id) {
      r = await fetch(`/api/users/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json',
          ...(token ? {'Authorization': `Bearer ${token}`} : {}) },
        body: JSON.stringify({ role, name, callsign, locator, email, permissions }),
      });
    } else {
      if (!password) { UI.showToast('Podaj hasło', 'error'); return; }
      r = await fetch('/api/users', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json',
          ...(token ? {'Authorization': `Bearer ${token}`} : {}) },
        body: JSON.stringify({ username, password, role, name, callsign, locator, email, permissions }),
      });
    }
  } catch(e) {
    UI.showToast('✗ Błąd sieci: ' + e.message, 'error');
    return;
  }

  let res = {};
  try { res = await r.json(); } catch(e) { /* puste body */ }

  console.log('[admin] saveUser response status=', r.status, 'body=', res);

  if (!r.ok) {
    UI.showToast('✗ ' + (res.error || `HTTP ${r.status}`), 'error');
    return;
  }

  closeModal();
  loadUsers();
  UI.showToast(id ? '✓ Użytkownik zaktualizowany' : '✓ Użytkownik dodany');

  // Jeśli admin edytował samego siebie — odśwież uprawnienia w UI natychmiast
  if (id && window.CurrentUser && String(window.CurrentUser.id) === String(id)) {
    fetch('/api/auth/me').then(r => r.json()).then(data => {
      if (data.user) {
        window.CurrentUser = data.user;
        Auth.applyPermissions(data.user);
      }
    });
  }
}

async function toggleActive(id, isActive) {
  const r = await fetch(`/api/users/${id}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ active: !isActive }),
  });
  const res = await r.json();
  if (res.ok) { loadUsers(); UI.showToast(isActive ? 'Konto dezaktywowane' : 'Konto aktywowane'); }
  else UI.showToast('✗ ' + res.error, 'error');
}

async function deleteUser(id, username) {
  if (!await UI.confirmModal(`Usunąć użytkownika ${username}?`, { danger: true, okLabel: 'USUŃ' })) return;
  const r   = await fetch(`/api/users/${id}`, { method: 'DELETE' });
  const res = await r.json();
  if (res.ok) { loadUsers(); UI.showToast(`✓ Usunięto ${username}`); }
  else UI.showToast('✗ ' + res.error, 'error');
}

async function resetPwdDialog(id, username) {
  const pwd = await UI.textPrompt(`NOWE HASŁO DLA ${username} (min. 8 znaków)`, '');
  if (!pwd) return;
  if (pwd.length < 8) { UI.showToast('✗ Min. 8 znaków', 'error'); return; }
  fetch(`/api/users/${id}/reset-password`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ newPassword: pwd }),
  }).then(r => r.json()).then(res => {
    if (res.ok) UI.showToast(`✓ Hasło ${username} zresetowane`);
    else UI.showToast('✗ ' + res.error, 'error');
  });
}

function closeModal() {
  const m = document.getElementById('user-modal');
  if (m) m.style.display = 'none';
}

// ── Konfiguracja rotatorów ────────────────────────────────────────────────────
async function loadRotatorConfig() {
  loadStationLocator();   // lokator stacji — w tej samej sekcji ustawien
  try {
    const r    = await fetch('/api/config');
    const cfg  = await r.json();
    _rotatorsCfg = cfg.rotators || [];
    renderRotatorConfig(_rotatorsCfg);
  } catch(e) {
    renderRotatorConfig([]);
  }
}

function renderRotatorConfig(rots) {
  const el = document.getElementById('rotator-config-list');
  if (!el) return;
  // Usun znacznik i18n statycznego placeholdera "Ladowanie..." - inaczej
  // kolejne I18n.setLang() (apply() dziala na calym dokumencie) nadpisze
  // ten kontener z powrotem na "Ladowanie..." i skasuje wyrenderowane rotatory.
  el.removeAttribute('data-i18n');
  el.innerHTML = rots.map((rot, i) => `
    <div class="rot-cfg-row">
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr 1fr auto;gap:8px;align-items:end;">
        <div class="sg"><label>${I18n.t('cfg_name_lbl')}</label><input type="text" value="${_escapeHtmlAdmin(rot.name||'')}" id="rcfg-name-${i}" placeholder="Rotator AZ"></div>
        <div class="sg"><label>${I18n.t('cfg_model_hamlib_lbl')}</label>
          <select id="rcfg-model-${i}">
            <optgroup label="Alfaspid SPID">
              <option value="901" ${rot.model==='901'?'selected':''}>RAK / BIG-RAK</option>
              <option value="902" ${rot.model==='902'?'selected':''}>RAS / BIG-RAS (AZ+EL)</option>
            </optgroup>
            <optgroup label="Yaesu">
              <option value="601" ${rot.model==='601'?'selected':''}>GS-232A</option>
              <option value="603" ${rot.model==='603'?'selected':''}>GS-232B</option>
            </optgroup>
            <option value="1" ${rot.model==='1'?'selected':''}>Dummy (test)</option>
          </select>
        </div>
        <div class="sg"><label>${I18n.t('cfg_port_com_lbl')}</label><input type="text" value="${_escapeHtmlAdmin(rot.port||'COM5')}" id="rcfg-port-${i}" placeholder="COM5"></div>
        <div class="sg"><label>${I18n.t('cfg_baud_lbl')}</label>
          <select id="rcfg-speed-${i}">
            <option value="600"  ${(rot.speed||'1200')==='600' ?'selected':''}>600</option>
            <option value="1200" ${(rot.speed||'1200')==='1200'?'selected':''}>1200</option>
            <option value="9600" ${(rot.speed||'1200')==='9600'?'selected':''}>9600</option>
          </select>
        </div>
        <div class="sg"><label>${I18n.t('cfg_rot_ph_lbl')}</label>
          <select id="rcfg-ph-${i}">
            <option value="2" ${(rot.ph||2)===2?'selected':''}>PH=2 (0.50°)</option>
            <option value="1" ${(rot.ph||2)===1?'selected':''}>PH=1 (1.00°)</option>
            <option value="4" ${(rot.ph||2)===4?'selected':''}>PH=4 (0.25°)</option>
          </select>
        </div>
        <div class="sg"><label>${I18n.t('cfg_rot_enabled_lbl')}</label>
          <select id="rcfg-en-${i}">
            <option value="1" ${rot.enabled?'selected':''}>${I18n.t('cfg_yes')}</option>
            <option value="0" ${!rot.enabled?'selected':''}>${I18n.t('cfg_no')}</option>
          </select>
        </div>
        <button class="admin-btn danger" onclick="Admin.removeRotator(${i})" style="height:33px;">✕</button>
        <button onclick="Admin.testRotator(${i})" style="height:33px;background:rgba(184,201,143,0.1);border:1px solid var(--green2);color:var(--green);font-family:var(--mono);font-size:10px;padding:0 8px;border-radius:3px;cursor:pointer;">TEST</button>
      </div>
      <div id="rot-test-result-${i}" style="font-family:var(--mono);font-size:10px;color:var(--dim);padding:3px 2px 0;min-height:14px;"></div>
    </div>`).join('') || `<div style="font-family:var(--mono);font-size:11px;color:var(--dim);padding:12px;">${I18n.t('cfg_no_rotators')}</div>`;
}

let _rotatorsCfg = [];

function addRotator() {
  const rows = _rotatorsCfg.length;
  _rotatorsCfg.push({ id:rows+1, name:`Rotator ${rows+1}`, model:'901', port:`COM${5+rows}`, speed:'1200', enabled:true });
  renderRotatorConfig(_rotatorsCfg);
}

function removeRotator(idx) {
  _rotatorsCfg.splice(idx, 1);
  _rotatorsCfg.forEach((r, i) => r.id = i + 1);
  renderRotatorConfig(_rotatorsCfg);
}

async function saveRotatorConfig() {
  const rows = document.querySelectorAll('.rot-cfg-row');
  const rots = [];
  rows.forEach((_, i) => {
    rots.push({
      id:      i+1,
      name:    document.getElementById(`rcfg-name-${i}`)?.value  || `Rotator ${i+1}`,
      model:   document.getElementById(`rcfg-model-${i}`)?.value || '901',
      port:    document.getElementById(`rcfg-port-${i}`)?.value  || 'COM5',
      speed:   document.getElementById(`rcfg-speed-${i}`)?.value || '1200',
      ph:      parseInt(document.getElementById(`rcfg-ph-${i}`)?.value||'2')||2,
      enabled: document.getElementById(`rcfg-en-${i}`)?.value === '1',
      hamlibPort: 4533 + i,
    });
  });
  _rotatorsCfg = rots;
  try {
    const r   = await fetch('/api/rotator/config', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ rotators: rots }),
    });
    const res = await r.json();
    if (res.ok) {
      window.UI?.showToast(I18n.t('cfg_toast_rotators_saved'));
    } else window.UI?.showToast('✗ ' + (res.error||I18n.t('profile_error_fallback')), 'error');
  } catch(e) { window.UI?.showToast('✗ ' + I18n.t('cfg_conn_error'), 'error'); }
}

async function testRotator(idx) {
  const cfg = _rotatorsCfg[idx];
  if (!cfg?.id) { window.UI?.showToast(I18n.t('cfg_save_config_first'), 'error'); return; }
  const resultEl = document.getElementById(`rot-test-result-${idx}`);
  if (resultEl) resultEl.textContent = I18n.t('cfg_testing');
  try {
    const r = await fetch(`/api/rotator/${cfg.id}/test`, {method:'POST'});
    const d = await r.json();
    const msg = d.testOk
      ? I18n.t('cfg_rot_test_ok').replace('{az}', d.testPos?.az??'?').replace('{driver}', d.driverType)
      : `✗ ${d.testMsg||d.error}`;
    if (resultEl) { resultEl.textContent = msg; resultEl.style.color = d.testOk ? 'var(--green)' : 'var(--red)'; }
    window.UI?.showToast(msg, d.testOk ? 'ok' : 'error');
  } catch(e) {
    if (resultEl) { resultEl.textContent = '✗ ' + I18n.t('log_error_prefix') + e.message; resultEl.style.color = 'var(--red)'; }
  }
}

// ── Funkcje radia (whitelist dla userow) ────────────────────────────────────
let _rigFeaturesData = null;

async function loadRigFeatures() {
  const el = document.getElementById('rig-features-config-list');
  if (!el) return;
  el.innerHTML = I18n.t('settings_loading');
  try {
    const token = localStorage.getItem('token');
    const r = await fetch('/api/rig/features', {
      headers: token ? { 'Authorization': `Bearer ${token}` } : {}
    });
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || 'blad');
    _rigFeaturesData = data;
    renderRigFeaturesConfig(data.features || [], data.dynamic || {actions:[],sliders:[]});
  } catch(e) {
    el.innerHTML = `<span style="color:var(--red)">${I18n.t('log_error_prefix')}${e.message}</span>`;
  }
}

function renderRigFeaturesConfig(features, dynamic) {
  const el = document.getElementById('rig-features-config-list');
  if (!el) return;
  el.removeAttribute('data-i18n');  // patrz komentarz w renderRotatorConfig()

  const actions = dynamic.actions || [];
  const sliders = dynamic.sliders || [];

  el.innerHTML = `
    <!-- TABELKI WYBORU: PRZYCISKI i SLIDERY -->
    <div style="display:flex;flex-direction:column;gap:20px;">

      ${_renderTransferWidget({
        title:     I18n.t('cfg_static_features_title'),
        items:     features,
        dataAttr:  'data-feature-id',
        widgetId:  'feat-transfer',
        labelFn:   f => (f.icon ? f.icon + ' ' : '') + f.label,
        sublabelFn: f => f.group?.toUpperCase() || '',
      })}

      ${_renderTransferWidget({
        title:     I18n.t('cfg_buttons_title'),
        items:     actions,
        dataAttr:  'data-dynamic-id',
        widgetId:  'btn-transfer',
        labelFn:   a => a.label.split('(')[0].trim(),
        sublabelFn: a => a.group.toUpperCase(),
      })}

      ${_renderTransferWidget({
        title:     I18n.t('cfg_sliders_title'),
        items:     sliders,
        dataAttr:  'data-dynamic-id',
        widgetId:  'sld-transfer',
        labelFn:   s => s.label,
        sublabelFn: s => `${s.min}…${s.max}`,
      })}

    </div>`;

  // Podpięcie zdarzeń (po wstawieniu do DOM)
  ['feat-transfer', 'btn-transfer', 'sld-transfer'].forEach(id => _attachTransferEvents(id));
}

// ── Transfer widget renderer ───────────────────────────────────────────────────
function _renderTransferWidget({ title, items, dataAttr, widgetId, labelFn, sublabelFn }) {
  const selected  = items.filter(i => i.enabled !== false);
  const available = items.filter(i => i.enabled === false);

  const renderList = (list, side) => list.map(i => `
    <div class="tw-item" ${dataAttr}="${i.id}" data-side="${side}"
         draggable="true" title="${sublabelFn(i)}">
      ${labelFn(i)}
      <span class="tw-sub">${sublabelFn(i)}</span>
    </div>`).join('');

  return `
  <div class="tw-widget" id="${widgetId}">
    <div style="font-family:var(--mono);font-size:9px;color:var(--dim);letter-spacing:2px;margin-bottom:8px;">${title}</div>
    <div class="tw-body">
      <div class="tw-col">
        <div class="tw-col-header">${I18n.t('tw_visible_lbl')} <span class="tw-count" id="${widgetId}-sel-count">${selected.length}</span></div>
        <div class="tw-list" id="${widgetId}-selected" data-side="selected">
          ${renderList(selected, 'selected')}
        </div>
        <div class="tw-hint">${I18n.t('tw_hint_remove')}</div>
      </div>
      <div class="tw-buttons">
        <button class="tw-btn" onclick="_twMoveAll('${widgetId}','selected')" title="${I18n.t('tw_title_add_all')}">${I18n.t('tw_btn_all_left')}</button>
        <button class="tw-btn" onclick="_twMoveSelected('${widgetId}','selected')" title="${I18n.t('tw_title_add_sel')}">${I18n.t('tw_btn_add')}</button>
        <button class="tw-btn" onclick="_twMoveSelected('${widgetId}','available')" title="${I18n.t('tw_title_remove_sel')}">${I18n.t('tw_btn_remove')}</button>
        <button class="tw-btn" onclick="_twMoveAll('${widgetId}','available')" title="${I18n.t('tw_title_remove_all')}">${I18n.t('tw_btn_all_right')}</button>
        <div style="margin-top:6px;border-top:1px solid var(--border);padding-top:6px;">
          <button class="tw-btn" onclick="_twMoveUp('${widgetId}')" title="${I18n.t('tw_title_move_up')}">${I18n.t('tw_btn_up')}</button>
          <button class="tw-btn" onclick="_twMoveDown('${widgetId}')" title="${I18n.t('tw_title_move_down')}">${I18n.t('tw_btn_down')}</button>
        </div>
      </div>
      <div class="tw-col">
        <div class="tw-col-header">${I18n.t('tw_available_lbl')} <span class="tw-count" id="${widgetId}-avl-count">${available.length}</span></div>
        <div class="tw-list" id="${widgetId}-available" data-side="available">
          ${renderList(available, 'available')}
        </div>
        <div class="tw-hint">${I18n.t('tw_hint_add')}</div>
      </div>
    </div>
  </div>`;
}

// ── Transfer widget events ─────────────────────────────────────────────────────
function _attachTransferEvents(widgetId) {
  const selList = document.getElementById(`${widgetId}-selected`);
  const avlList = document.getElementById(`${widgetId}-available`);
  if (!selList || !avlList) return;

  [selList, avlList].forEach(list => {
    // Podświetlenie przez klik
    list.addEventListener('click', e => {
      const item = e.target.closest('.tw-item');
      if (!item) return;
      // Toggle selection (ctrl = multi-select)
      if (!e.ctrlKey && !e.metaKey) {
        list.querySelectorAll('.tw-item.tw-selected').forEach(i => i.classList.remove('tw-selected'));
      }
      item.classList.toggle('tw-selected');
      _twUpdateCounts(widgetId);
    });

    // Podwójny klik = przenieś od razu
    list.addEventListener('dblclick', e => {
      const item = e.target.closest('.tw-item');
      if (!item) return;
      const target = item.dataset.side === 'selected' ? avlList : selList;
      item.dataset.side = target.dataset.side;
      target.appendChild(item);
      _twUpdateCounts(widgetId);
    });

    // Drag & drop
    list.addEventListener('dragover', e => { e.preventDefault(); list.classList.add('tw-drag-over'); });
    list.addEventListener('dragleave', () => list.classList.remove('tw-drag-over'));
    list.addEventListener('drop', e => {
      e.preventDefault();
      list.classList.remove('tw-drag-over');
      const id = e.dataTransfer.getData('tw-item-id');
      const item = document.querySelector(`.tw-item[data-drag-id="${id}"]`);
      if (item) {
        item.dataset.side = list.dataset.side;
        list.appendChild(item);
        _twUpdateCounts(widgetId);
      }
    });
  });

  // Drag start na elementach (delegacja — działa też na dynamicznie dodanych)
  document.getElementById(widgetId).addEventListener('dragstart', e => {
    const item = e.target.closest('.tw-item');
    if (!item) return;
    const uid = Math.random().toString(36).slice(2);
    item.setAttribute('data-drag-id', uid);
    e.dataTransfer.setData('tw-item-id', uid);
  });
}

// ── Helper functions dla przycisków transferu ──────────────────────────────────
function _twMoveSelected(widgetId, toSide) {
  const fromSide = toSide === 'selected' ? 'available' : 'selected';
  const fromList = document.getElementById(`${widgetId}-${fromSide}`);
  const toList   = document.getElementById(`${widgetId}-${toSide}`);
  fromList?.querySelectorAll('.tw-item.tw-selected').forEach(item => {
    item.classList.remove('tw-selected');
    item.dataset.side = toSide;
    toList.appendChild(item);
  });
  _twUpdateCounts(widgetId);
}
function _twMoveAll(widgetId, toSide) {
  const fromSide = toSide === 'selected' ? 'available' : 'selected';
  const fromList = document.getElementById(`${widgetId}-${fromSide}`);
  const toList   = document.getElementById(`${widgetId}-${toSide}`);
  fromList?.querySelectorAll('.tw-item').forEach(item => {
    item.dataset.side = toSide;
    toList.appendChild(item);
  });
  _twUpdateCounts(widgetId);
}
function _twMoveUp(widgetId) {
  const list = document.getElementById(`${widgetId}-selected`);
  list?.querySelectorAll('.tw-item.tw-selected').forEach(item => {
    if (item.previousElementSibling) list.insertBefore(item, item.previousElementSibling);
  });
}
function _twMoveDown(widgetId) {
  const list = document.getElementById(`${widgetId}-selected`);
  const items = [...(list?.querySelectorAll('.tw-item.tw-selected') || [])].reverse();
  items.forEach(item => {
    if (item.nextElementSibling) list.insertBefore(item.nextElementSibling, item);
  });
}
function _twUpdateCounts(widgetId) {
  const sel = document.getElementById(`${widgetId}-selected`);
  const avl = document.getElementById(`${widgetId}-available`);
  const sc  = document.getElementById(`${widgetId}-sel-count`);
  const ac  = document.getElementById(`${widgetId}-avl-count`);
  if (sc) sc.textContent = sel?.children.length ?? 0;
  if (ac) ac.textContent = avl?.children.length ?? 0;
}

// ── Globalne helpery (wywoływane inline z onclick) ─────────────────────────────
window._twMoveSelected = _twMoveSelected;
window._twMoveAll      = _twMoveAll;
window._twMoveUp       = _twMoveUp;
window._twMoveDown     = _twMoveDown;


async function saveRigFeatures() {
  const enabledDynamic  = {};
  const dynamicOrder    = {};
  const enabledFeatures = {};

  // Statyczne features (split, ptt, freq itd.)
  const featSel = document.getElementById('feat-transfer-selected');
  const featAvl = document.getElementById('feat-transfer-available');
  featSel?.querySelectorAll('.tw-item[data-feature-id]').forEach(item => {
    enabledFeatures[item.dataset.featureId] = true;
  });
  featAvl?.querySelectorAll('.tw-item[data-feature-id]').forEach(item => {
    enabledFeatures[item.dataset.featureId] = false;
  });

  // Przyciski
  const btnSel = document.getElementById('btn-transfer-selected');
  const btnAvl = document.getElementById('btn-transfer-available');
  btnSel?.querySelectorAll('.tw-item[data-dynamic-id]').forEach((item, idx) => {
    enabledDynamic[item.dataset.dynamicId] = true;
    dynamicOrder[item.dataset.dynamicId]   = idx;
  });
  btnAvl?.querySelectorAll('.tw-item[data-dynamic-id]').forEach(item => {
    enabledDynamic[item.dataset.dynamicId] = false;
  });

  // Slidery
  const sldSel = document.getElementById('sld-transfer-selected');
  const sldAvl = document.getElementById('sld-transfer-available');
  sldSel?.querySelectorAll('.tw-item[data-dynamic-id]').forEach((item, idx) => {
    enabledDynamic[item.dataset.dynamicId] = true;
    dynamicOrder[item.dataset.dynamicId]   = idx;
  });
  sldAvl?.querySelectorAll('.tw-item[data-dynamic-id]').forEach(item => {
    enabledDynamic[item.dataset.dynamicId] = false;
  });

  try {
    const token = localStorage.getItem('token');
    const r = await fetch('/api/rig/features', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        ...(token ? { 'Authorization': `Bearer ${token}` } : {})
      },
      body: JSON.stringify({
        rigId:          _rigFeaturesData?.rigId,
        enabledFeatures,
        enabledDynamic,
        dynamicOrder,
      }),
    });
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || 'blad');
    window.UI?.showToast(I18n.t('cfg_toast_config_saved'), 'success');
    await loadRigFeatures();
  } catch(e) {
    window.UI?.showToast('✗ ' + I18n.t('log_error_prefix') + e.message, 'error');
  }
}


// ── FT8 Timer — admin zarządzanie per user ────────────────────────────────────
async function loadFt8Timers() {
  // Zaladuj globalny timer
  const token = localStorage.getItem('token') || '';
  const h = token ? {'Authorization': `Bearer ${token}`} : {};
  try {
    const r = await fetch('/api/ft8timer/global', {headers: h});
    const d = await r.json();
    const el = document.getElementById('ft8t-global-dur');
    if (el) el.value = d.duration_min || 6;
    const st = document.getElementById('ft8t-global-status');
    if (st) st.textContent = `✓ Aktywny — ${d.duration_min || 6} min dla wszystkich użytkowników`;
  } catch(e) { console.warn('[ft8timer] load:', e); }
}

async function saveGlobalFt8Timer() {
  const dur   = parseInt(document.getElementById('ft8t-global-dur')?.value || '6');
  const token = localStorage.getItem('token') || '';
  const h = {'Content-Type':'application/json', ...(token ? {'Authorization': `Bearer ${token}`} : {})};
  try {
    const r = await fetch('/api/ft8timer/global', {
      method: 'POST', headers: h,
      body: JSON.stringify({ duration_min: dur }),
    });
    const d = await r.json();
    if (d.ok) {
      window.UI?.showToast(`✓ Timer FT8: ${dur} min dla wszystkich`);
      const st = document.getElementById('ft8t-global-status');
      if (st) st.textContent = `✓ Aktywny — ${dur} min dla wszystkich użytkowników`;
    }
  } catch(e) { window.UI?.showToast('✗ Błąd zapisu timera', 'error'); }
}

// UWAGA: byla tu wczesniej DRUGA deklaracja "function saveFt8Timer(userId)"
// (legacy stub przekierowujacy na saveGlobalFt8Timer) NAD tą - w JS druga
// deklaracja funkcji o tej samej nazwie w tym samym zasiegu CICHO
// PODMIENIA pierwsza, wiec ten legacy stub nigdy sie nie wykonywal. Zero
// realnego wplywu (UI per-user timera i tak nie istnieje w index.html od
// czasu uproszczenia do jednego globalnego timera - ani jedna, ani druga
// wersja nie miala zadnego wywolujacego), ale usunieta zeby ktos kiedys
// nie edytowal tej martwej kopii myslac ze to ona dziala.
async function saveFt8Timer(userId) {
  const durEl  = document.getElementById(`ft8t-dur-${userId}`);
  const editEl = document.getElementById(`ft8t-edit-${userId}`);
  if (!durEl) return;
  const token = localStorage.getItem('token') || '';
  try {
    const r = await fetch('/api/ft8timer/admin', {
      method: 'POST',
      headers: {'Content-Type':'application/json',
        ...(token ? {'Authorization':`Bearer ${token}`} : {})},
      body: JSON.stringify({
        user_id:       userId,
        duration_min:  parseInt(durEl.value) || 6,
        user_can_edit: editEl?.checked || false,
      }),
    });
    const res = await r.json();
    if (res.ok) window.UI?.showToast('✓ Timer zapisany');
    else window.UI?.showToast('✗ ' + (res.error || 'Błąd'), 'error');
  } catch(e) {
    window.UI?.showToast('✗ ' + e.message, 'error');
  }
}

// ── AdminBands — pasma i tryby ────────────────────────────────────────────────
window.AdminBands = (() => {
  const token = () => localStorage.getItem('token') || '';

  async function load() {
    await Promise.all([_loadBands(), _loadModes()]);
  }

  // ── PASMA ────────────────────────────────────────────────────────────────────
  async function _loadBands() {
    const sel = document.getElementById('bands-transfer-selected');
    const avl = document.getElementById('bands-transfer-available');
    if (!sel || !avl) return;
    try {
      const r    = await fetch('/api/config/bands', { headers: {'Authorization': `Bearer ${token()}`} });
      const data = await r.json();
      const enabled = data.enabledBands || [];
      const all     = Object.keys(data.allBands || {});

      sel.innerHTML = enabled.map(b =>
        `<div class="tw-item" data-band="${b}" data-side="selected" draggable="true">${b}</div>`
      ).join('');
      avl.innerHTML = all.filter(b => !enabled.includes(b)).map(b =>
        `<div class="tw-item" data-band="${b}" data-side="available" draggable="true">${b}</div>`
      ).join('');

      _updateCount('bands-transfer');
      _attachBandEvents();
    } catch(e) {
      console.warn('[AdminBands] bands error:', e);
    }
  }

  async function save() {
    const sel = document.getElementById('bands-transfer-selected');
    if (!sel) return;
    const enabled = [...sel.querySelectorAll('.tw-item')].map(el => el.dataset.band);
    try {
      const r = await fetch('/api/config/bands', {
        method: 'POST',
        headers: {'Content-Type':'application/json', 'Authorization': `Bearer ${token()}`},
        body: JSON.stringify({ enabledBands: enabled }),
      });
      const res = await r.json();
      if (res.ok) window.UI?.showToast(I18n.t('cfg_toast_bands_saved'));
      else window.UI?.showToast('✗ ' + (res.error||I18n.t('profile_error_fallback')), 'error');
    } catch(e) { window.UI?.showToast('✗ ' + e.message, 'error'); }
  }

  // ── TRYBY ────────────────────────────────────────────────────────────────────
  let _allModes = [];
  let _modeFilters = {};

  async function _loadModes() {
    const sel = document.getElementById('modes-transfer-selected');
    const avl = document.getElementById('modes-transfer-available');
    if (!sel || !avl) return;
    try {
      const r    = await fetch('/api/config/modes', { headers: {'Authorization': `Bearer ${token()}`} });
      const data = await r.json();
      _allModes    = data.allModes || [];
      _modeFilters = data.modeFilters || {};
      const enabled = data.enabledModes || [];

      sel.innerHTML = enabled.map(m =>
        `<div class="tw-item" data-mode="${m}" data-side="selected" draggable="true">${m}</div>`
      ).join('');
      avl.innerHTML = _allModes.filter(m => !enabled.includes(m)).map(m =>
        `<div class="tw-item" data-mode="${m}" data-side="available" draggable="true">${m}</div>`
      ).join('');

      _updateCount('modes-transfer');
      _attachModeEvents();
      _renderModeFilters(enabled);
    } catch(e) {
      console.warn('[AdminBands] modes error:', e);
    }
  }

  function _renderModeFilters(enabled) {
    const grid = document.getElementById('mode-filter-grid');
    if (!grid) return;
    const FILTERS = ['1','2','3'];
    grid.innerHTML = enabled.map(m => `
      <div style="display:flex;align-items:center;gap:6px;">
        <span style="font-family:var(--mono);font-size:10px;color:var(--text);min-width:60px;">${m}</span>
        <select data-mode-filter="${m}"
          style="font-family:var(--mono);font-size:10px;background:var(--panel3);
          border:1px solid var(--border2);border-radius:3px;color:var(--text);padding:2px 4px;">
          <option value="">${I18n.t('cfg_none_filter')}</option>
          ${['1','2','3'].map(f => `<option value="${f}" ${_modeFilters[m]==f?'selected':''}>FIL${f}</option>`).join('')}
        </select>
      </div>`).join('');
  }

  async function saveModes() {
    const sel = document.getElementById('modes-transfer-selected');
    if (!sel) return;
    const enabled = [...sel.querySelectorAll('.tw-item')].map(el => el.dataset.mode);
    const filters = {};
    document.querySelectorAll('[data-mode-filter]').forEach(el => {
      if (el.value) filters[el.dataset.modeFilter] = el.value;
    });
    try {
      const r = await fetch('/api/config/modes', {
        method: 'POST',
        headers: {'Content-Type':'application/json', 'Authorization': `Bearer ${token()}`},
        body: JSON.stringify({ enabledModes: enabled, modeFilters: filters }),
      });
      const res = await r.json();
      if (res.ok) window.UI?.showToast(I18n.t('cfg_toast_modes_saved'));
      else window.UI?.showToast('✗ ' + (res.error||I18n.t('profile_error_fallback')), 'error');
    } catch(e) { window.UI?.showToast('✗ ' + e.message, 'error'); }
  }

  // ── Drag & drop i eventy ─────────────────────────────────────────────────────
  function _attachBandEvents()  { _attachDnD('bands-transfer', 'band');  }
  function _attachModeEvents()  { _attachDnD('modes-transfer', 'mode');  }

  function _attachDnD(widgetId, dataKey) {
    ['selected','available'].forEach(side => {
      const list = document.getElementById(`${widgetId}-${side}`);
      if (!list) return;
      // _loadBands/_loadModes wolane przy KAZDYM wejsciu na zakladke
      // KONFIGURACJA, ale to sa STALE wezly DOM (tylko ich .innerHTML jest
      // podmieniane) - bez tej strazniczki addEventListener stackowalby sie
      // przy kazdej wizycie (delegacja na rodzicu, wiec przezywa podmiane
      // dzieci), a klik/dblclick zaczynalby dzialac losowo po paru wizytach
      // (parzysta/nieparzysta liczba nasluchiwaczy). Wystarczy podpiac raz.
      if (list.dataset.dndAttached === '1') return;
      list.dataset.dndAttached = '1';
      const other = document.getElementById(`${widgetId}-${side==='selected'?'available':'selected'}`);

      // Podswietlenie przez klik (ctrl/cmd = multi-select) - bez tego przyciski
      // "« Dodaj"/"Usun »" (ktore przenosza tylko .tw-item.tw-selected, patrz
      // _twMoveSelected) nie mialy jak trafic w cokolwiek i byly martwe -
      // dzialalo tylko podwojne kliknieciem i przeciaganie. _attachTransferEvents
      // (uzywany przez FUNKCJE RADIA) mial ten handler, ten (PASMA/TRYBY) nie.
      list.addEventListener('click', e => {
        const item = e.target.closest('.tw-item');
        if (!item) return;
        if (!e.ctrlKey && !e.metaKey) {
          list.querySelectorAll('.tw-item.tw-selected').forEach(i => i.classList.remove('tw-selected'));
        }
        item.classList.toggle('tw-selected');
      });

      list.addEventListener('dblclick', e => {
        const item = e.target.closest('.tw-item');
        if (!item) return;
        item.dataset.side = other.dataset.side;
        other.appendChild(item);
        _updateCount(widgetId);
        if (dataKey === 'mode') {
          const enabled = [...document.getElementById(`${widgetId}-selected`).querySelectorAll('.tw-item')].map(el => el.dataset.mode);
          _renderModeFilters(enabled);
        }
      });

      list.addEventListener('dragover', e => { e.preventDefault(); list.classList.add('tw-drag-over'); });
      list.addEventListener('dragleave', () => list.classList.remove('tw-drag-over'));
      list.addEventListener('drop', e => {
        e.preventDefault(); list.classList.remove('tw-drag-over');
        const id = e.dataTransfer.getData('tw-item-id');
        const item = document.querySelector(`.tw-item[data-drag-id="${id}"]`);
        if (item) { item.dataset.side = list.dataset.side; list.appendChild(item); _updateCount(widgetId); }
      });
    });

    const widget = document.getElementById(widgetId);
    if (widget && widget.dataset.dndAttached !== '1') {
      widget.dataset.dndAttached = '1';
      widget.addEventListener('dragstart', e => {
        const item = e.target.closest('.tw-item');
        if (!item) return;
        const uid = Math.random().toString(36).slice(2);
        item.setAttribute('data-drag-id', uid);
        e.dataTransfer.setData('tw-item-id', uid);
      });
    }
  }

  function _updateCount(widgetId) {
    const s = document.getElementById(`${widgetId}-selected`)?.children.length || 0;
    const a = document.getElementById(`${widgetId}-available`)?.children.length || 0;
    const sc = document.getElementById(`${widgetId}-sel-count`);
    const ac = document.getElementById(`${widgetId}-avl-count`);
    if (sc) sc.textContent = s;
    if (ac) ac.textContent = a;
  }

  return { load, save, saveModes };
})();

window._adminAttachTransferEvents = _attachTransferEvents;
window._adminTwUpdateCounts       = _twUpdateCounts;

// ── Lokator stacji (pozycja anteny — baza dla azymutu rotora) ────────────────
async function loadStationLocator() {
  const el = document.getElementById('cfg-station-locator');
  if (!el) return;
  try {
    const token = localStorage.getItem('token') || '';
    const r = await fetch('/api/config/station', {
      headers: token ? {'Authorization': `Bearer ${token}`} : {}
    });
    const d = await r.json();
    el.value = d.stationLocator || '';
  } catch(e) {}
}

async function saveStationLocator() {
  const el  = document.getElementById('cfg-station-locator');
  const msg = document.getElementById('cfg-station-locator-msg');
  if (!el) return;
  const loc = (el.value || '').trim().toUpperCase();
  if (loc && !/^[A-R]{2}\d{2}([A-X]{2})?$/.test(loc)) {
    if (msg) { msg.textContent = I18n.t('cfg_bad_locator_format'); msg.style.color = 'var(--red)'; }
    return;
  }
  try {
    const token = localStorage.getItem('token') || '';
    const r = await fetch('/api/config/station', {
      method: 'POST',
      headers: {'Content-Type':'application/json',
                ...(token ? {'Authorization': `Bearer ${token}`} : {})},
      body: JSON.stringify({ stationLocator: loc })
    });
    const d = await r.json();
    if (d.ok) {
      if (msg) { msg.textContent = I18n.t('cfg_saved_capital'); msg.style.color = 'var(--green)'; }
      setTimeout(() => { if (msg) msg.textContent = ''; }, 3000);
    } else {
      if (msg) { msg.textContent = '✗ ' + (d.error || I18n.t('profile_error_fallback')); msg.style.color = 'var(--red)'; }
    }
  } catch(e) {
    if (msg) { msg.textContent = '✗ ' + e.message; msg.style.color = 'var(--red)'; }
  }
}

// ── DeepCW — zarzadzanie modelem ─────────────────────────────────────────────
async function deepcwStatus() {
  const el = document.getElementById('deepcw-admin-status');
  if (!el) return;
  try {
    const token = localStorage.getItem('token') || '';
    const r = await fetch('/api/deepcw/engine_status', {
      headers: token ? {'Authorization': `Bearer ${token}`} : {}
    });
    const d = await r.json();
    if (d.error)          { el.textContent = '✗ ' + d.error;        el.style.color = 'var(--red)'; }
    else if (!d.hasModel) { el.textContent = I18n.t('cfg_deepcw_not_downloaded'); el.style.color = '#fa0'; }
    else if (!d.ready)    { el.textContent = I18n.t('cfg_deepcw_loading').replace('{mb}', d.sizeMB); el.style.color = '#fa0'; }
    else                  { el.textContent = I18n.t('cfg_deepcw_ready').replace('{mb}', d.sizeMB); el.style.color = 'var(--green)'; }
  } catch(e) {
    el.textContent = '✗ ' + e.message; el.style.color = 'var(--red)';
  }
}

async function deepcwDownload() {
  const bar = document.getElementById('deepcw-admin-bar');
  const log = document.getElementById('deepcw-admin-log');
  if (bar) bar.style.display = 'block';
  if (log) log.textContent = I18n.t('cfg_deepcw_starting_download');
  try {
    const token = localStorage.getItem('token') || '';
    const r = await fetch('/api/deepcw/download', {
      method: 'POST',
      headers: token ? {'Authorization': `Bearer ${token}`} : {}
    });
    const d = await r.json();
    if (d.ok) {
      if (log) { log.textContent = I18n.t('cfg_deepcw_downloaded').replace('{mb}', (d.sizeBytes/1e6).toFixed(1));
                 log.style.color = 'var(--green)'; }
      deepcwStatus();
    } else {
      if (log) { log.textContent = d.error ? ('✗ ' + d.error) : I18n.t('cfg_deepcw_download_error');
                 log.style.color = 'var(--red)'; }
    }
  } catch(e) {
    if (log) { log.textContent = '✗ ' + e.message; log.style.color = 'var(--red)'; }
  }
}

window.Admin = {
  loadUsers, showAddUser, editUser, saveUser,
  toggleActive, deleteUser, resetPwdDialog, closeModal,
  loadRotatorConfig, addRotator, removeRotator, saveRotatorConfig, testRotator,
  loadStationLocator, saveStationLocator,
  deepcwStatus, deepcwDownload,
  PERM_DEFS, _updatePermsByRole,
  loadRigFeatures, saveRigFeatures,
  loadFt8Timers, saveFt8Timer, saveGlobalFt8Timer,
};

})();
