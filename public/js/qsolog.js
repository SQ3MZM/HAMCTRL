/**
 * qsolog.js — per-user QSO log
 */
(function() {
'use strict';

const S = window.AppState;

let _page     = 1;
let _perPage  = 50;
let _total    = 0;
let _sortCol  = 'date';
let _sortDir  = 'desc';
let _editId   = null;  // null = new QSO, string = editing

// Prefer the flag for the COUNTRY NAME already resolved by QRZ.com/
// HamQTH (far more complete/accurate than dxcc.js's simplified ~150-
// entry prefix table, and correct even for special-event prefixes the
// local table was never going to cover) - fall back to guessing from
// the callsign prefix only when there's no stored country (e.g. a QSO
// that was never run through a lookup).
// HTML-escapes free-text QSO fields before they go into innerHTML/attribute
// templates below - operator-typed fields (comment/name/qth/country/call/
// gridsquare/...) went in RAW, so a comment like <img src=x onerror=...>
// executed for anyone viewing that log (including an admin browsing
// another operator's log - a privilege-escalation path), and a stray
// quote in country/name/qth broke out of the title="..." attribute on the
// flag span. Covers both cases (also escapes quotes, needed for the
// attribute context, not just for text content).
function _esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _flagFor(call, country) {
  if (country) {
    const byName = window.DXCC?.lookupByName?.(country);
    if (byName?.flag) return byName.flag;
  }
  return window.DXCC?.lookup?.(call)?.flag || '';
}

// ── Filters ───────────────────────────────────────────────────────────────────
function _getFilters() {
  return {
    from:    document.getElementById('log-filter-from')?.value  || '',
    to:      document.getElementById('log-filter-to')?.value    || '',
    call:    document.getElementById('log-filter-call')?.value?.trim().toUpperCase() || '',
    band:    document.getElementById('log-filter-band')?.value  || '',
    mode:    document.getElementById('log-filter-mode')?.value  || '',
    user_id: document.getElementById('log-filter-user')?.value  || '',
  };
}

async function _loadAdminUsers() {
  const wrap = document.getElementById('log-filter-user-wrap');
  const sel  = document.getElementById('log-filter-user');
  if (!wrap || !sel || window.CurrentUser?.role !== 'admin') return;
  wrap.style.display = 'flex';
  try {
    const token = localStorage.getItem('token') || '';
    const r = await fetch('/api/users', {headers: token ? {'Authorization': `Bearer ${token}`} : {}});
    const data = await r.json();
    // /api/users returns the array directly
    const users = Array.isArray(data) ? data : (data.users || []);
    sel.innerHTML = `<option value="">${I18n.t('log_filter_all_users')}</option>` +
      users.map(u => `<option value="${u.id}">${u.callsign || u.username}</option>`).join('');
  } catch(e) { console.warn('[qsolog] loadAdminUsers:', e); }
}

function clearFilters() {
  ['log-filter-from','log-filter-to','log-filter-call',
   'log-filter-band','log-filter-mode','log-filter-user'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  _page = 1;
  load();
}

// ── Sorting ───────────────────────────────────────────────────────────────────
function sort(col) {
  if (_sortCol === col) _sortDir = _sortDir === 'asc' ? 'desc' : 'asc';
  else { _sortCol = col; _sortDir = 'desc'; }
  _page = 1;
  load();
}

// ── Loading data ──────────────────────────────────────────────────────────────
async function load() {
  const tbody = document.getElementById('log-table-body');
  if (!tbody) return;
  tbody.innerHTML = `<tr><td colspan="12" style="text-align:center;padding:20px;color:var(--dim);">${I18n.t('settings_loading')}</td></tr>`;

  const filters = _getFilters();
  const token   = localStorage.getItem('token') || '';
  const params  = new URLSearchParams({
    page:    _page,
    per:     _perPage,
    sort:    _sortCol,
    dir:     _sortDir,
    ...Object.fromEntries(Object.entries(filters).filter(([,v]) => v)),
  });

  try {
    const r = await fetch(`/api/qsolog?${params}`, {
      headers: token ? { 'Authorization': `Bearer ${token}` } : {},
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    _total = data.total || 0;
    _renderTable(data.qsos || []);
    _renderPagination();
    const cnt = document.getElementById('log-count');
    if (cnt) cnt.textContent = `${_total} QSO`;
  } catch(e) {
    tbody.innerHTML = `<tr><td colspan="12" style="text-align:center;padding:20px;color:var(--red);">${I18n.t('log_error_prefix')}${e.message}</td></tr>`;
  }
}

// ── Rendering the table ────────────────────────────────────────────────────────
function _renderTable(qsos) {
  const tbody = document.getElementById('log-table-body');
  if (!tbody) return;
  if (!qsos.length) {
    tbody.innerHTML = `<tr><td colspan="12" style="text-align:center;padding:20px;color:var(--dim);">${I18n.t('log_no_qso')}</td></tr>`;
    return;
  }
  tbody.innerHTML = qsos.map(q => {
    const modeClass = q.mode === 'CW' ? 'log-mode-cw' : q.mode === 'FT8' ? 'log-mode-ft8' : q.mode === 'FT4' ? 'log-mode-ft4' : '';
    const date = q.qso_date ? `${q.qso_date.slice(6,8)}.${q.qso_date.slice(4,6)}.${q.qso_date.slice(0,4)}` : '';
    const time = q.time_on ? `${q.time_on.slice(0,2)}:${q.time_on.slice(2,4)}` : '';
    return `<tr data-id="${_esc(q.id)}">
      <td style="text-align:center;padding:0 4px;">
        <input type="checkbox" class="qso-chk" data-id="${_esc(q.id)}"
          style="width:13px;height:13px;cursor:pointer;accent-color:var(--red);">
      </td>
      <td>${_esc(date)}</td>
      <td>${_esc(time)}</td>
      <td class="log-call">${_esc(q.call)}${q.country
        ? ` <span title="${_esc(q.country)}${q.name ? ' — ' + _esc(q.name) : ''}${q.qth ? ', ' + _esc(q.qth) : ''}">${_flagFor(q.call, q.country)}</span>`
        : ''}</td>
      <td>${_esc(q.band)}</td>
      <td class="${modeClass}">${_esc(q.mode)}${q.sat_name
        ? ` <span title="${_esc(I18n.t('log_sat_tooltip_prefix'))}${_esc(q.sat_name)}${q.sat_mode ? ' (' + _esc(q.sat_mode) + ')' : ''}${q.band_rx ? ', downlink ' + _esc(q.band_rx) : ''}">🛰</span>`
        : ''}</td>
      <td>${q.freq ? parseFloat(q.freq).toFixed(4) : ''}</td>
      <td>${_esc(q.rst_sent)}</td>
      <td>${_esc(q.rst_rcvd)}</td>
      <td>${_esc(q.gridsquare)}</td>
      <td style="max-width:130px;overflow:hidden;text-overflow:ellipsis;">${_esc(q.comment)}</td>
      <td style="display:flex;gap:4px;">
        <button class="log-action-btn" onclick="QSOLog.openEdit('${_esc(q.id)}')">${I18n.t('log_row_edit_btn')}</button>
        <button class="log-action-btn del" onclick="QSOLog.deleteQSO('${_esc(q.id)}')">${I18n.t('common_delete_btn')}</button>
      </td>
    </tr>`;
  }).join('');
}

// ── Pagination ────────────────────────────────────────────────────────────────
function _renderPagination() {
  const totalPages = Math.max(1, Math.ceil(_total / _perPage));
  const el = document.getElementById('log-page-info');
  if (el) el.textContent = I18n.t('log_page_info_full').replace('{page}', _page).replace('{total}', totalPages).replace('{count}', _total);
}

function prevPage() { if (_page > 1) { _page--; load(); } }
function nextPage() {
  const totalPages = Math.ceil(_total / _perPage);
  if (_page < totalPages) { _page++; load(); }
}

// ── Modal: new / edit QSO ─────────────────────────────────────────────────────
// The satellite section (SAT_NAME/SAT_MODE/FREQ_RX/BAND_RX) is hidden
// behind a checkbox - most QSOs aren't satellite ones, no point taking up
// permanent space. toggleSatFields controls visibility (grid<->none).
function toggleSatFields(show) {
  const box = document.getElementById('qso-sat-fields');
  if (box) box.style.display = show ? 'grid' : 'none';
}

// Suggest the COUNTRY (and continent, in the background) from the local
// prefix table (dxcc.js) — works instantly as the callsign is typed,
// before the user even gets to click the QRZ/HamQTH lookup (see
// lookupCall() below - the real lookup OVERWRITES this, since it's more
// reliable than guessing from the prefix). This auto-fill does NOT
// overwrite if the user already typed something there manually — same
// pattern as updateRstDefaults.
function autoFillCountry() {
  const callEl    = document.getElementById('qso-call');
  const countryEl = document.getElementById('qso-country');
  const flagEl    = document.getElementById('qso-country-flag');
  if (!callEl || !countryEl) return;
  const call = callEl.value.trim();
  const info = window.DXCC?.lookup ? window.DXCC.lookup(call) : null;
  if (!info || !info.name) { if (flagEl) flagEl.textContent = ''; return; }
  if (!countryEl.value.trim()) countryEl.value = info.name;
  if (flagEl) flagEl.textContent = info.flag || '';
  countryEl.dataset.cont = info.continent || '';
}

// The real lookup (QRZ.com / HamQTH, per the user's config in SETTINGS) —
// triggered manually by clicking the 🔍 icon, not automatically on every
// keystroke (both services have query limits, no point burning them on
// every keypress). Unlike autoFillCountry(), this one OVERWRITES
// NAME/QTH/COUNTRY/LOCATOR - data from a real lookup is more reliable
// than whatever was already there (manually typed or guessed from the prefix).
// DXCC/CQZ/ITUZ/STATE/IOTA don't have their own form fields yet - kept
// quietly in the #qso-country dataset, they make it into the save (see
// saveQSO) and ADIF export for other programs, but don't clutter our log view.
async function lookupCall() {
  const callEl = document.getElementById('qso-call');
  const btn    = document.getElementById('qso-lookup-btn');
  const call   = callEl?.value?.trim().toUpperCase();
  if (!call) { window.UI?.showToast(I18n.t('log_enter_callsign'), 'error'); return; }
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  try {
    const res = await window.Callbook?.lookup?.(call);
    if (!res) {
      window.UI?.showToast(I18n.t('log_lookup_not_found'), 'error');
      return;
    }
    if (res.name)       _setField('qso-name', res.name);
    if (res.qth)         _setField('qso-qth', res.qth);
    if (res.country)     _setField('qso-country', res.country);
    if (res.gridsquare) _setField('qso-gridsquare', res.gridsquare.toUpperCase());
    const countryEl = document.getElementById('qso-country');
    const flagEl    = document.getElementById('qso-country-flag');
    if (countryEl) {
      countryEl.dataset.dxcc  = res.dxcc  || '';
      countryEl.dataset.cqz   = res.cqz   || '';
      countryEl.dataset.ituz  = res.ituz  || '';
      countryEl.dataset.state = res.state || '';
      countryEl.dataset.iota  = res.iota  || '';
      countryEl.dataset.cont  = window.DXCC?.lookupByName?.(res.country)?.continent
                                 || window.DXCC?.lookup?.(call)?.continent
                                 || countryEl.dataset.cont || '';
    }
    if (flagEl) flagEl.textContent = _flagFor(call, res.country);
    window.UI?.showToast(I18n.t('log_data_fetched_from').replace('{source}', res.source));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🔍'; }
  }
}

function openNew() {
  _editId = null;
  const now = new Date();
  const pad = n => String(n).padStart(2, '0');
  const dateStr = `${now.getUTCFullYear()}-${pad(now.getUTCMonth()+1)}-${pad(now.getUTCDate())}`;
  const timeStr = `${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}`;

  _setField('qso-call', '');
  _setField('qso-gridsquare', '');
  _setField('qso-name', '');
  _setField('qso-qth', '');
  _setField('qso-country', '');
  const countryEl0 = document.getElementById('qso-country');
  if (countryEl0) {
    countryEl0.dataset.cont = countryEl0.dataset.dxcc = countryEl0.dataset.cqz =
      countryEl0.dataset.ituz = countryEl0.dataset.state = countryEl0.dataset.iota = '';
  }
  const flagEl0 = document.getElementById('qso-country-flag');
  if (flagEl0) flagEl0.textContent = '';
  _setField('qso-date', dateStr);
  _setField('qso-time', timeStr);
  _setField('qso-band', S?.mode ? _freqToBand(S.freq) : '20m');
  _setField('qso-mode', S?.mode || 'SSB');
  _setField('qso-freq', S?.freq ? (S.freq / 1e6).toFixed(4) : '');
  _setField('qso-power', '100');
  _setField('qso-rst-sent', '599');
  _setField('qso-rst-rcvd', '599');
  _setField('qso-comment', '');
  _setField('qso-sat-name', '');
  _setField('qso-sat-mode', '');
  _setField('qso-band-rx', '');
  _setField('qso-freq-rx', '');
  const satChk = document.getElementById('qso-is-sat');
  if (satChk) satChk.checked = false;
  toggleSatFields(false);

  const title = document.getElementById('log-modal-title');
  if (title) { title.removeAttribute('data-i18n'); title.textContent = I18n.t('log_modal_new_title'); }
  const modal = document.getElementById('log-modal');
  if (modal) modal.style.display = 'flex';
  document.getElementById('qso-call')?.focus();
}

async function openEdit(id) {
  _editId = id;
  const token = localStorage.getItem('token') || '';
  try {
    const r = await fetch(`/api/qsolog/${id}`, {
      headers: token ? { 'Authorization': `Bearer ${token}` } : {},
    });
    const q = await r.json();
    _setField('qso-call', q.call || '');
    _setField('qso-gridsquare', q.gridsquare || '');
    _setField('qso-name', q.name || '');
    _setField('qso-qth', q.qth || '');
    _setField('qso-country', q.country || '');
    const countryEl = document.getElementById('qso-country');
    if (countryEl) {
      countryEl.dataset.cont  = q.cont  || '';
      countryEl.dataset.dxcc  = q.dxcc  || '';
      countryEl.dataset.cqz   = q.cqz   || '';
      countryEl.dataset.ituz  = q.ituz  || '';
      countryEl.dataset.state = q.state || '';
      countryEl.dataset.iota  = q.iota  || '';
    }
    const flagEl = document.getElementById('qso-country-flag');
    if (flagEl) flagEl.textContent = q.country ? _flagFor(q.call, q.country) : '';
    _setField('qso-date', q.qso_date ? `${q.qso_date.slice(0,4)}-${q.qso_date.slice(4,6)}-${q.qso_date.slice(6,8)}` : '');
    _setField('qso-time', q.time_on ? `${q.time_on.slice(0,2)}:${q.time_on.slice(2,4)}` : '');
    _setField('qso-band', q.band || '20m');
    _setField('qso-mode', q.mode || 'SSB');
    _setField('qso-freq', q.freq || '');
    _setField('qso-power', q.power || '');
    _setField('qso-rst-sent', q.rst_sent || '');
    _setField('qso-rst-rcvd', q.rst_rcvd || '');
    _setField('qso-comment', q.comment || '');
    _setField('qso-sat-name', q.sat_name || '');
    _setField('qso-sat-mode', q.sat_mode || '');
    _setField('qso-band-rx', q.band_rx || '');
    _setField('qso-freq-rx', q.freq_rx || '');
    const isSat = !!(q.sat_name || q.prop_mode === 'SAT');
    const satChk = document.getElementById('qso-is-sat');
    if (satChk) satChk.checked = isSat;
    toggleSatFields(isSat);
    const title = document.getElementById('log-modal-title');
    if (title) { title.removeAttribute('data-i18n'); title.textContent = I18n.t('log_modal_edit_title'); }
    const modal = document.getElementById('log-modal');
    if (modal) modal.style.display = 'flex';
  } catch(e) {
    window.UI?.showToast(I18n.t('log_load_qso_error') + e.message, 'error');
  }
}

function closeModal() {
  const modal = document.getElementById('log-modal');
  if (modal) modal.style.display = 'none';
}

// ── Save QSO ──────────────────────────────────────────────────────────────────
async function saveQSO() {
  const call = document.getElementById('qso-call')?.value?.trim().toUpperCase();
  if (!call) { window.UI?.showToast(I18n.t('log_missing_callsign'), 'error'); return; }

  const dateVal = document.getElementById('qso-date')?.value || '';
  const timeVal = document.getElementById('qso-time')?.value || '';
  const qso_date = dateVal.replace(/-/g, '');  // YYYYMMDD
  const time_on  = timeVal.replace(':', '') + '00'; // HHMMSS

  const qso = {
    call,
    gridsquare: document.getElementById('qso-gridsquare')?.value?.trim().toUpperCase() || '',
    qso_date,
    time_on,
    time_off: time_on,  // same time
    band:     document.getElementById('qso-band')?.value || '',
    mode:     document.getElementById('qso-mode')?.value || '',
    freq:     document.getElementById('qso-freq')?.value || '',
    power:    document.getElementById('qso-power')?.value || '',
    rst_sent: document.getElementById('qso-rst-sent')?.value || '',
    rst_rcvd: document.getElementById('qso-rst-rcvd')?.value || '',
    comment:  document.getElementById('qso-comment')?.value || '',
    my_call:  S?.callsign || window.CurrentUser?.callsign || '',
    my_gridsquare: (window.CurrentUser?.locator || S?.operatorLocator
                   || S?.stationLocator || ''),  // OPERATOR's locator
    name:     document.getElementById('qso-name')?.value?.trim() || '',
    qth:      document.getElementById('qso-qth')?.value?.trim() || '',
    country:  document.getElementById('qso-country')?.value?.trim() || '',
    // DXCC/CQZ/ITUZ/STATE/IOTA — no dedicated form field, but if they came
    // from lookupCall() (QRZ/HamQTH) they're kept in the dataset and go
    // into the save, so the ADIF export has complete data for other programs.
    cont:     document.getElementById('qso-country')?.dataset.cont  || '',
    dxcc:     document.getElementById('qso-country')?.dataset.dxcc  || '',
    cqz:      document.getElementById('qso-country')?.dataset.cqz   || '',
    ituz:     document.getElementById('qso-country')?.dataset.ituz  || '',
    state:    document.getElementById('qso-country')?.dataset.state || '',
    iota:     document.getElementById('qso-country')?.dataset.iota  || '',
  };

  // Satellite QSO — only when the checkbox is checked. BAND/FREQ above is
  // the uplink, band_rx/freq_rx is the downlink (see the comment on the field in the HTML).
  if (document.getElementById('qso-is-sat')?.checked) {
    qso.prop_mode = 'SAT';
    qso.sat_name  = document.getElementById('qso-sat-name')?.value?.trim().toUpperCase() || '';
    qso.sat_mode  = document.getElementById('qso-sat-mode')?.value?.trim().toUpperCase() || '';
    qso.band_rx   = document.getElementById('qso-band-rx')?.value || '';
    qso.freq_rx   = document.getElementById('qso-freq-rx')?.value?.trim() || '';
  } else {
    qso.prop_mode = '';
    qso.sat_name = qso.sat_mode = qso.band_rx = qso.freq_rx = '';
  }

  const token  = localStorage.getItem('token') || '';
  const url    = _editId ? `/api/qsolog/${_editId}` : '/api/qsolog';
  const method = _editId ? 'PUT' : 'POST';

  try {
    const r = await fetch(url, {
      method,
      headers: { 'Content-Type': 'application/json', ...(token ? {'Authorization': `Bearer ${token}`} : {}) },
      body: JSON.stringify(qso),
    });
    const res = await r.json();
    if (res.ok) {
      window.UI?.showToast(_editId ? I18n.t('log_qso_updated') : I18n.t('log_qso_saved'));
      closeModal();
      load();
    } else {
      window.UI?.showToast('✗ ' + I18n.t('log_error_prefix') + (res.error || I18n.t('log_unknown_fallback')), 'error');
    }
  } catch(e) {
    window.UI?.showToast(I18n.t('log_save_error_prefix') + e.message, 'error');
  }
}

// ── Delete QSO ────────────────────────────────────────────────────────────────
async function deleteQSO(id) {
  if (!await window.UI?.confirmModal(I18n.t('log_confirm_delete_one'), { danger: true, okLabel: I18n.t('common_delete_btn') })) return;
  const token = localStorage.getItem('token') || '';
  try {
    const r = await fetch(`/api/qsolog/${id}`, {
      method: 'DELETE',
      headers: token ? { 'Authorization': `Bearer ${token}` } : {},
    });
    const res = await r.json();
    if (res.ok) { window.UI?.showToast(I18n.t('log_qso_deleted')); load(); }
    else window.UI?.showToast('✗ ' + (res.error || I18n.t('profile_error_fallback')), 'error');
  } catch(e) {
    window.UI?.showToast('✗ ' + e.message, 'error');
  }
}

// ── Export ────────────────────────────────────────────────────────────────────
async function _exportFetch(format) {
  const token = localStorage.getItem('token') || '';
  // Priority 1: if the operator SELECTED specific QSOs — export only those.
  const selectedIds = [...document.querySelectorAll('.qso-chk:checked')]
    .map(el => el.dataset.id).filter(Boolean);

  const params = new URLSearchParams({ format });
  if (selectedIds.length) {
    // Export the selected entries by ID
    params.set('ids', selectedIds.join(','));
  } else {
    // Priority 2: nothing selected — export by filters (including a
    // from-to date range). No filters: the whole log. We do NOT limit to the visible page.
    const filters = _getFilters();
    for (const [k, v] of Object.entries(filters)) {
      if (v) params.set(k, v);
    }
  }

  try {
    const r = await fetch(`/api/qsolog/export?${params}`, {
      headers: token ? { 'Authorization': `Bearer ${token}` } : {},
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const blob = await r.blob();
    const ext  = format === 'csv' ? 'csv' : 'adi';
    const scope = selectedIds.length ? `_wybrane_${selectedIds.length}` : '';
    const name = `qso_log${scope}_${new Date().toISOString().slice(0,10)}.${ext}`;
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);
    if (selectedIds.length) {
      window.UI?.showToast(I18n.t('log_exported_selected').replace('{n}', selectedIds.length), 'success');
    }
  } catch(e) {
    window.UI?.showToast(I18n.t('log_export_error_prefix') + e.message, 'error');
  }
}

function exportADI() { _exportFetch('adi'); }
function exportCSV() { _exportFetch('csv'); }

// ── Helpers ───────────────────────────────────────────────────────────────────
function _setField(id, val) {
  const el = document.getElementById(id);
  if (el) el.value = val;
}

function _freqToBand(hz) {
  const mhz = hz / 1e6;
  // Ranges are DELIBERATELY WIDE — the union of amateur allocations across
  // all 3 ITU regions (Europe/Africa/N.Asia = R1, the Americas = R2, the
  // rest of Asia/Pacific = R3), not just Europe. This soft can be worked
  // from anywhere in the world, so it's better to cover the wider, real
  // range used by any ham than to narrow it to one country/region. The
  // upper (microwave) bands are added in part for QO-100 (13cm uplink /
  // 3cm downlink) and satellite QSOs in general (see the SAT_NAME/
  // SAT_MODE/FREQ_RX/BAND_RX fields in qso_db.py).
  if (mhz >= 1.8    && mhz <= 2.0)    return '160m';
  if (mhz >= 3.5    && mhz <= 4.0)    return '80m';   // R2 goes up to 4.0
  if (mhz >= 5.06   && mhz <= 5.45)   return '60m';   // various channels/ranges by country
  if (mhz >= 7.0    && mhz <= 7.3)    return '40m';   // R2/R3 go up to 7.3
  if (mhz >= 10.1   && mhz <= 10.15)  return '30m';
  if (mhz >= 14.0   && mhz <= 14.35)  return '20m';
  if (mhz >= 18.0   && mhz <= 18.17)  return '17m';
  if (mhz >= 21.0   && mhz <= 21.45)  return '15m';
  if (mhz >= 24.8   && mhz <= 24.99)  return '12m';
  if (mhz >= 28.0   && mhz <= 29.7)   return '10m';
  if (mhz >= 50.0   && mhz <= 54.0)   return '6m';
  if (mhz >= 70.0   && mhz <= 70.5)   return '4m';    // mainly Europe/Africa, harmless elsewhere
  if (mhz >= 144    && mhz <= 148)    return '2m';
  if (mhz >= 220    && mhz <= 225)    return '1.25m'; // R2 (USA/Canada)
  if (mhz >= 420    && mhz <= 450)    return '70cm';  // R2 goes up to 420-450, not just 430-440
  if (mhz >= 902     && mhz <= 928)    return '33cm';  // R2 (USA)
  if (mhz >= 1240   && mhz <= 1300)   return '23cm';
  if (mhz >= 2300   && mhz <= 2450)   return '13cm';
  if (mhz >= 3400   && mhz <= 3410)   return '9cm';
  if (mhz >= 5650   && mhz <= 5925)   return '6cm';   // R2 goes up to 5925
  if (mhz >= 10000  && mhz <= 10500)  return '3cm';
  if (mhz >= 24000  && mhz <= 24250)  return '1.2cm'; // R2 goes up to 24250
  return '20m';
}

// ── Quick log from the RADIO tab ──────────────────────────────────────────────
// Suggest a report (RST) in the SENT/RCVD fields depending on the mode:
// CW/CW-R -> 599, phone (USB/LSB/AM/FM/...) -> 59 — automatically after
// switching modulation (called from UI.updateModeButtons(), which already
// runs on every mode change: click, telemetry, WS 'mode'). Overwrites the
// field ONLY while it still holds one of the two known defaults (or is
// empty) — a manually typed real QSO report is never overwritten.
const _RST_CW = '599';
const _RST_PHONE = '59';
function updateRstDefaults(mode) {
  const def = String(mode || '').toUpperCase().startsWith('CW') ? _RST_CW : _RST_PHONE;
  const other = def === _RST_CW ? _RST_PHONE : _RST_CW;
  ['qlog-rst-s', 'qlog-rst-r'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    if (el.value === '' || el.value === other || el.value === def) el.value = def;
    el.placeholder = def;
  });
}

async function quickLog(overrides = {}) {
  const call = document.getElementById('qlog-call')?.value?.trim().toUpperCase();
  if (!call) {
    _setStatus(I18n.t('log_enter_callsign_excl'), 'red');
    document.getElementById('qlog-call')?.focus();
    return;
  }

  const now  = new Date();
  const pad  = n => String(n).padStart(2, '0');
  const qso  = {
    call,
    qso_date:  `${now.getUTCFullYear()}${pad(now.getUTCMonth()+1)}${pad(now.getUTCDate())}`,
    // time_on defaults to "now" (correct for a genuinely manual/quick log
    // - there's no earlier "first contact" moment to anchor to). Callers
    // that DO know one (e.g. Hound's auto-log - see _houndAutoLog in
    // wsjtx.js) pass it via `overrides`, matching the main auto-QSO
    // engine's convention of anchoring TIME_ON to the partner's first
    // reply, not to whenever the log call happens to fire.
    time_on:   `${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}`,
    time_off:  `${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}`,
    band:      _freqToBand(S?.freq || 0),
    mode:      S?.mode || 'SSB',
    freq:      S?.freq ? (S.freq / 1e6).toFixed(4) : '',
    rst_sent:  document.getElementById('qlog-rst-s')?.value || (String(S?.mode||'').toUpperCase().startsWith('CW') ? _RST_CW : _RST_PHONE),
    rst_rcvd:  document.getElementById('qlog-rst-r')?.value || (String(S?.mode||'').toUpperCase().startsWith('CW') ? _RST_CW : _RST_PHONE),
    gridsquare: document.getElementById('qlog-grid')?.value?.trim().toUpperCase() || '',
    my_call:   S?.callsign || window.CurrentUser?.callsign || '',
    my_gridsquare: (window.CurrentUser?.locator || S?.operatorLocator
                   || S?.stationLocator || ''),  // OPERATOR's locator
    comment:   '',
    ...overrides,
  };

  const token = localStorage.getItem('token') || '';
  try {
    _setStatus('...', 'dim');
    const r = await fetch('/api/qsolog', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json',
        ...(token ? {'Authorization': `Bearer ${token}`} : {}) },
      body: JSON.stringify(qso),
    });
    const res = await r.json();
    if (res.ok) {
      _setStatus(`✓ ${call} ${qso.band} ${qso.mode}`, 'green');
      // Clear the CALL field, RST stays at 599
      const callEl = document.getElementById('qlog-call');
      callEl.value = '';
      callEl.style.color = '';
      callEl.style.borderColor = '';
      document.getElementById('qlog-grid').value = '';
      callEl.focus();
      // If we're on the LOG page — refresh the table
      if (document.getElementById('page-log')?.classList.contains('active')) load();
    } else {
      _setStatus('✗ ' + (res.error || I18n.t('status_error_generic')), 'red');
    }
  } catch(e) {
    _setStatus('✗ ' + e.message, 'red');
  }
}

function _setStatus(msg, color) {
  const el = document.getElementById('qlog-status');
  if (!el) return;
  el.textContent = msg;
  el.style.color = color === 'green' ? 'var(--green)'
                 : color === 'red'   ? 'var(--red)'
                 : 'var(--dim)';
  // Fade out after 4s
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.textContent = ''; }, 4000);
}

// ── ADIF Import ───────────────────────────────────────────────────────────────
async function importADIF(input) {
  const file = input.files?.[0];
  if (!file) return;
  input.value = '';

  let text;
  try { text = await file.text(); }
  catch(e) { window.UI?.showToast(I18n.t('log_cant_read_file') + e.message, 'error'); return; }

  const qsos = _parseADIF(text);
  if (!qsos.length) { window.UI?.showToast(I18n.t('log_no_qso_in_adif'), 'error'); return; }

  window.UI?.showToast(I18n.t('log_importing').replace('{n}', qsos.length));

  const token   = localStorage.getItem('token') || '';
  const headers = {'Content-Type':'application/json',
                   ...(token ? {'Authorization': `Bearer ${token}`} : {})};
  const CHUNK   = 500;
  let inserted  = 0;
  let skipped   = 0;
  let duplicates = 0;

  for (let i = 0; i < qsos.length; i += CHUNK) {
    const chunk = qsos.slice(i, i + CHUNK);
    try {
      const r   = await fetch('/api/qsolog/bulk', {
        method: 'POST', headers,
        body: JSON.stringify({ qsos: chunk }),
      });
      const res = await r.json();
      if (res.ok) {
        inserted   += res.inserted   || 0;
        skipped    += res.skipped    || 0;
        duplicates += res.duplicates || 0;
      } else {
        skipped += chunk.length;
      }
    } catch(e) {
      skipped += chunk.length;
    }
    // Update the progress toast
    const done = Math.min(i + CHUNK, qsos.length);
    window.UI?.showToast(I18n.t('log_importing_progress').replace('{done}', done).replace('{total}', qsos.length));
  }

  // Message: imported + separately duplicates (skipped as already in the
  // log) and any invalid entries.
  let msg = I18n.t('log_import_done').replace('{n}', inserted);
  if (duplicates) msg += I18n.t('log_import_duplicates_skipped').replace('{n}', duplicates);
  if (skipped)    msg += I18n.t('log_import_errors').replace('{n}', skipped);
  window.UI?.showToast(msg, (skipped > 0) ? 'error' : 'info');
  load();
}

function _parseADIF(text) {
  const qsos = [];
  // Skip the header (everything before <EOH>)
  const eohIdx = text.toUpperCase().indexOf('<EOH>');
  const body   = eohIdx >= 0 ? text.slice(eohIdx + 5) : text;

  // Split into records by <EOR>
  const records = body.split(/<EOR>/i);

  for (const rec of records) {
    if (!rec.trim()) continue;
    const fields = {};

    // Parse fields: <FIELD:length>value
    const re = /<([A-Z0-9_]+):(\d+)(?::[A-Z])?>/gi;
    let match;
    while ((match = re.exec(rec)) !== null) {
      const tag = match[1].toUpperCase();
      const len = parseInt(match[2]);
      const val = rec.slice(match.index + match[0].length, match.index + match[0].length + len);
      fields[tag] = val.trim();
    }

    if (!fields.CALL) continue;  // skip records without a callsign

    qsos.push({
      call:          fields.CALL || '',
      qso_date:      fields.QSO_DATE || '',
      time_on:       fields.TIME_ON || '000000',
      time_off:      fields.TIME_OFF || fields.TIME_ON || '000000',
      band:          fields.BAND || '',
      mode:          fields.MODE || '',
      freq:          fields.FREQ || '',
      rst_sent:      fields.RST_SENT || '',
      rst_rcvd:      fields.RST_RCVD || '',
      gridsquare:    fields.GRIDSQUARE || '',
      my_call:       fields.MY_CALL || fields.STATION_CALLSIGN || '',
      my_gridsquare: fields.MY_GRIDSQUARE || '',
      power:         fields.TX_PWR || '',
      comment:       fields.COMMENT || fields.NOTES || '',
      prop_mode:     fields.PROP_MODE || '',
      sat_name:      fields.SAT_NAME || '',
      sat_mode:      fields.SAT_MODE || '',
      freq_rx:       fields.FREQ_RX || '',
      band_rx:       fields.BAND_RX || '',
      name:          fields.NAME || '',
      qth:           fields.QTH || '',
      dxcc:          fields.DXCC || '',
      country:       fields.COUNTRY || '',
      cont:          fields.CONT || '',
      cqz:           fields.CQZ || '',
      ituz:          fields.ITUZ || '',
      state:         fields.STATE || '',
      iota:          fields.IOTA || '',
      qsl_sent:      fields.QSL_SENT || '',
      qsl_rcvd:      fields.QSL_RCVD || '',
      lotw_qsl_sent: fields.LOTW_QSL_SENT || '',
      lotw_qsl_rcvd: fields.LOTW_QSL_RCVD || '',
      lotw_qslsdate: fields.LOTW_QSLSDATE || '',
      lotw_qslrdate: fields.LOTW_QSLRDATE || '',
      eqsl_qsl_sent: fields.EQSL_QSL_SENT || '',
      eqsl_qsl_rcvd: fields.EQSL_QSL_RCVD || '',
      pota_ref:      fields.POTA_REF || '',
      sota_ref:      fields.SOTA_REF || '',
      wwff_ref:      fields.WWFF_REF || '',
    });
  }

  return qsos;
}

// ── Bulk selection and deletion ────────────────────────────────────────────
function selectAll(chk) {
  document.querySelectorAll('.qso-chk').forEach(el => el.checked = chk.checked);
}

async function deleteSelected() {
  const ids = [...document.querySelectorAll('.qso-chk:checked')].map(el => el.dataset.id);
  if (!ids.length) { window.UI?.showToast(I18n.t('log_select_qso_to_delete'), 'error'); return; }
  if (!await window.UI?.confirmModal(I18n.t('log_confirm_delete_selected').replace('{n}', ids.length), { danger: true, okLabel: I18n.t('common_delete_btn') })) return;
  const token = localStorage.getItem('token') || '';
  const h = {'Authorization': `Bearer ${token}`};
  let ok = 0;
  for (const id of ids) {
    try {
      const r = await fetch(`/api/qsolog/${id}`, {method:'DELETE', headers:h});
      if ((await r.json()).ok) ok++;
    } catch(e) {}
  }
  window.UI?.showToast(I18n.t('log_deleted_count').replace('{n}', ok));
  load();
}

async function deleteAll() {
  const sel      = document.getElementById('log-filter-user');
  const userId   = sel?.value || '';
  const userName = sel?.selectedOptions[0]?.text || '';
  const who      = userId ? I18n.t('log_delete_all_specific_user').replace('{name}', userName) : I18n.t('log_delete_all_everyone');
  if (!await window.UI?.confirmModal(I18n.t('log_confirm_delete_all_1').replace('{who}', who), { danger: true, okLabel: I18n.t('common_delete_btn') })) return;
  if (!await window.UI?.confirmModal(I18n.t('log_confirm_delete_all_2').replace('{who}', who), { danger: true, okLabel: I18n.t('log_yes_delete_btn') })) return;
  const token = localStorage.getItem('token') || '';
  const h = {'Authorization': `Bearer ${token}`};
  const url = userId ? `/api/qsolog/all?user_id=${userId}` : '/api/qsolog/all';
  try {
    const r = await fetch(url, {method:'DELETE', headers:h});
    const res = await r.json();
    if (res.ok) { window.UI?.showToast(I18n.t('log_deleted_count').replace('{n}', res.count || '')); load(); }
    else window.UI?.showToast('✗ ' + (res.error||I18n.t('profile_error_fallback')), 'error');
  } catch(e) { window.UI?.showToast('✗ ' + e.message, 'error'); }
}

// ── Module export ─────────────────────────────────────────────────────────────

// Check whether a callsign was already worked (debounced 500ms after the last keystroke)
let _workedCheckTimer = null;
async function checkWorkedBefore() {
  if (_workedCheckTimer) clearTimeout(_workedCheckTimer);
  _workedCheckTimer = setTimeout(async () => {
    const callEl = document.getElementById('qso-call');
    const badge = document.getElementById('qso-worked-badge');
    if (!callEl || !badge) return;
    const call = callEl.value.toUpperCase().trim();
    if (!call || call.length < 2) {
      badge.style.display = 'none';
      return;
    }
    const band = document.getElementById('qso-band')?.value || null;
    const mode = document.getElementById('qso-mode')?.value || null;
    try {
      const params = new URLSearchParams();
      params.set('call', call);
      if (band) params.set('band', band);
      if (mode) params.set('mode', mode);
      const r = await fetch('/api/qsolog/worked_before?' + params, { credentials: 'include' });
      if (!r.ok) return;
      const d = await r.json();
      if (!d.ok) return;
      // Priority: identical QSO > band only > mode only > call only > new
      if (d.worked_all) {
        badge.textContent = `⚠ DUPE (${d.count}× worked, ${d.last_qso.qso_date})`;
        badge.style.color = 'var(--red)';
        badge.style.background = 'rgba(217,119,106,0.15)';
      } else if (d.worked_band) {
        badge.textContent = `⚠ WORKED NA ${band}`;
        badge.style.color = 'var(--amber)';
        badge.style.background = 'rgba(212,168,87,0.15)';
      } else if (d.worked) {
        badge.textContent = `✓ NOWY BAND (${d.count}× wcześniej)`;
        badge.style.color = 'var(--green)';
        badge.style.background = 'rgba(184,201,143,0.15)';
      } else {
        badge.textContent = '★ NEW ONE';
        badge.style.color = '#66d0ff';
        badge.style.background = 'rgba(102,208,255,0.15)';
      }
      badge.style.display = 'inline-block';
    } catch(e) { console.warn('[qso] workedBefore error:', e); }
  }, 500);
}

// Same idea as checkWorkedBefore() above, but for the always-visible
// quick-log bar (#qlog-call): highlights the CALL text itself in red
// instead of a separate badge (there's no room for one in that single
// row), band comes from the current VFO freq (same as quickLog() uses
// when it actually saves), and mode is bucketed to SSB (USB+LSB together)
// vs CW rather than an exact CI-V mode match - the request was
// specifically "already worked on this band, SSB vs CW", not "on this
// exact sideband".
let _qlogWorkedTimer = null;
async function checkQuickLogWorkedBefore(val) {
  const el = document.getElementById('qlog-call');
  if (!el) return;
  if (_qlogWorkedTimer) clearTimeout(_qlogWorkedTimer);
  const call = (val || '').toUpperCase().trim();
  if (!call || call.length < 3) {
    el.style.color = '';
    el.style.borderColor = '';
    return;
  }
  _qlogWorkedTimer = setTimeout(async () => {
    // Stale-response guard: bail if the field changed while we were debouncing/fetching.
    if (el.value.toUpperCase().trim() !== call) return;
    const band = _freqToBand(S?.freq || 0);
    const modeCat = String(S?.mode || '').toUpperCase().startsWith('CW') ? 'CW' : 'SSB';
    try {
      const params = new URLSearchParams({ call, band, mode: modeCat });
      const token = localStorage.getItem('token') || '';
      const r = await fetch('/api/qsolog/worked_before?' + params, {
        headers: token ? { 'Authorization': `Bearer ${token}` } : {},
      });
      if (!r.ok) return;
      const d = await r.json();
      if (el.value.toUpperCase().trim() !== call) return;  // stale by the time the response arrived
      if (d.ok && d.worked_all) {
        el.style.color = 'var(--red)';
        el.style.borderColor = 'var(--red)';
      } else {
        el.style.color = '';
        el.style.borderColor = '';
      }
    } catch(e) { console.warn('[qso] quickLog workedBefore error:', e); }
  }, 300);
}

window.QSOLog = {
  load, sort, clearFilters, quickLog, updateRstDefaults, importADIF, selectAll, deleteSelected, deleteAll,
  prevPage, nextPage,
  openNew, openEdit, closeModal, saveQSO, deleteQSO, toggleSatFields, autoFillCountry, lookupCall,
  exportADI, exportCSV,
  loadAdminUsers: _loadAdminUsers,
  checkWorkedBefore,
  checkQuickLogWorkedBefore,
};

})();
