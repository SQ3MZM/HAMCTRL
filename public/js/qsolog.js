/**
 * qsolog.js — Log QSO per użytkownik
 * Szkielet — integracja z backendem w kolejnym kroku
 */
(function() {
'use strict';

const S = window.AppState;

let _page     = 1;
let _perPage  = 50;
let _total    = 0;
let _sortCol  = 'date';
let _sortDir  = 'desc';
let _editId   = null;  // null = nowe QSO, string = edycja

// ── Filtry ────────────────────────────────────────────────────────────────────
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
    // /api/users zwraca tablice bezposrednio
    const users = Array.isArray(data) ? data : (data.users || []);
    sel.innerHTML = '<option value="">Wszyscy</option>' +
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

// ── Sortowanie ────────────────────────────────────────────────────────────────
function sort(col) {
  if (_sortCol === col) _sortDir = _sortDir === 'asc' ? 'desc' : 'asc';
  else { _sortCol = col; _sortDir = 'desc'; }
  _page = 1;
  load();
}

// ── Ładowanie danych ──────────────────────────────────────────────────────────
async function load() {
  const tbody = document.getElementById('log-table-body');
  if (!tbody) return;
  tbody.innerHTML = '<tr><td colspan="12" style="text-align:center;padding:20px;color:var(--dim);">Ładowanie...</td></tr>';

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
    tbody.innerHTML = `<tr><td colspan="12" style="text-align:center;padding:20px;color:var(--red);">Błąd: ${e.message}</td></tr>`;
  }
}

// ── Renderowanie tabeli ───────────────────────────────────────────────────────
function _renderTable(qsos) {
  const tbody = document.getElementById('log-table-body');
  if (!tbody) return;
  if (!qsos.length) {
    tbody.innerHTML = '<tr><td colspan="12" style="text-align:center;padding:20px;color:var(--dim);">Brak QSO</td></tr>';
    return;
  }
  tbody.innerHTML = qsos.map(q => {
    const modeClass = q.mode === 'CW' ? 'log-mode-cw' : q.mode === 'FT8' ? 'log-mode-ft8' : q.mode === 'FT4' ? 'log-mode-ft4' : '';
    const date = q.qso_date ? `${q.qso_date.slice(6,8)}.${q.qso_date.slice(4,6)}.${q.qso_date.slice(0,4)}` : '';
    const time = q.time_on ? `${q.time_on.slice(0,2)}:${q.time_on.slice(2,4)}` : '';
    return `<tr data-id="${q.id}">
      <td style="text-align:center;padding:0 4px;">
        <input type="checkbox" class="qso-chk" data-id="${q.id}"
          style="width:13px;height:13px;cursor:pointer;accent-color:var(--red);">
      </td>
      <td>${date}</td>
      <td>${time}</td>
      <td class="log-call">${q.call || ''}${q.country
        ? ` <span title="${q.country}${q.name ? ' — ' + q.name : ''}${q.qth ? ', ' + q.qth : ''}">${window.DXCC?.lookup?.(q.call)?.flag || ''}</span>`
        : ''}</td>
      <td>${q.band || ''}</td>
      <td class="${modeClass}">${q.mode || ''}${q.sat_name
        ? ` <span title="Satelita: ${q.sat_name}${q.sat_mode ? ' (' + q.sat_mode + ')' : ''}${q.band_rx ? ', downlink ' + q.band_rx : ''}">🛰</span>`
        : ''}</td>
      <td>${q.freq ? parseFloat(q.freq).toFixed(4) : ''}</td>
      <td>${q.rst_sent || ''}</td>
      <td>${q.rst_rcvd || ''}</td>
      <td>${q.gridsquare || ''}</td>
      <td style="max-width:130px;overflow:hidden;text-overflow:ellipsis;">${q.comment || ''}</td>
      <td style="display:flex;gap:4px;">
        <button class="log-action-btn" onclick="QSOLog.openEdit('${q.id}')">EDYTUJ</button>
        <button class="log-action-btn del" onclick="QSOLog.deleteQSO('${q.id}')">USUŃ</button>
      </td>
    </tr>`;
  }).join('');
}

// ── Paginacja ─────────────────────────────────────────────────────────────────
function _renderPagination() {
  const totalPages = Math.max(1, Math.ceil(_total / _perPage));
  const el = document.getElementById('log-page-info');
  if (el) el.textContent = `Strona ${_page} z ${totalPages} (${_total} QSO)`;
}

function prevPage() { if (_page > 1) { _page--; load(); } }
function nextPage() {
  const totalPages = Math.ceil(_total / _perPage);
  if (_page < totalPages) { _page++; load(); }
}

// ── Modal: nowe / edytuj QSO ─────────────────────────────────────────────────
// Sekcja satelitarna (SAT_NAME/SAT_MODE/FREQ_RX/BAND_RX) chowana pod
// checkboxem - wiekszosc QSO nie jest satelitarna, nie ma sensu zajmowac
// miejsca na stale. toggleSatFields steruje widocznoscia (grid<->none).
function toggleSatFields(show) {
  const box = document.getElementById('qso-sat-fields');
  if (box) box.style.display = show ? 'grid' : 'none';
}

// Podpowiedz KRAJ (i kontynent, w tle) z lokalnej tabeli prefiksow (dxcc.js) —
// dziala od razu, bez lookupu QRZ/HamQTH (ten jeszcze nie podpiety). Nie
// nadpisuje pola jesli user juz cos tam wpisal recznie (np. skorygowal
// pomylke tabeli prefiksow) — patrz ten sam wzorzec co updateRstDefaults.
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
  if (title) title.textContent = 'NOWE QSO';
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
    if (countryEl) countryEl.dataset.cont = q.cont || '';
    const flagEl = document.getElementById('qso-country-flag');
    if (flagEl) flagEl.textContent = q.country ? (window.DXCC?.lookup?.(q.call)?.flag || '') : '';
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
    if (title) title.textContent = 'EDYTUJ QSO';
    const modal = document.getElementById('log-modal');
    if (modal) modal.style.display = 'flex';
  } catch(e) {
    window.UI?.showToast('Błąd ładowania QSO: ' + e.message, 'error');
  }
}

function closeModal() {
  const modal = document.getElementById('log-modal');
  if (modal) modal.style.display = 'none';
}

// ── Zapis QSO ─────────────────────────────────────────────────────────────────
async function saveQSO() {
  const call = document.getElementById('qso-call')?.value?.trim().toUpperCase();
  if (!call) { window.UI?.showToast('Brak znaku wywoławczego', 'error'); return; }

  const dateVal = document.getElementById('qso-date')?.value || '';
  const timeVal = document.getElementById('qso-time')?.value || '';
  const qso_date = dateVal.replace(/-/g, '');  // YYYYMMDD
  const time_on  = timeVal.replace(':', '') + '00'; // HHMMSS

  const qso = {
    call,
    gridsquare: document.getElementById('qso-gridsquare')?.value?.trim().toUpperCase() || '',
    qso_date,
    time_on,
    time_off: time_on,  // ten sam czas
    band:     document.getElementById('qso-band')?.value || '',
    mode:     document.getElementById('qso-mode')?.value || '',
    freq:     document.getElementById('qso-freq')?.value || '',
    power:    document.getElementById('qso-power')?.value || '',
    rst_sent: document.getElementById('qso-rst-sent')?.value || '',
    rst_rcvd: document.getElementById('qso-rst-rcvd')?.value || '',
    comment:  document.getElementById('qso-comment')?.value || '',
    my_call:  S?.callsign || '',
    my_gridsquare: (window.CurrentUser?.locator || S?.operatorLocator
                   || S?.stationLocator || ''),  // lokator OPERATORA
    name:     document.getElementById('qso-name')?.value?.trim() || '',
    qth:      document.getElementById('qso-qth')?.value?.trim() || '',
    country:  document.getElementById('qso-country')?.value?.trim() || '',
    cont:     document.getElementById('qso-country')?.dataset.cont || '',
  };

  // Lacznosc satelitarna — tylko gdy checkbox zaznaczony. PASMO/FREQ wyzej
  // to uplink, band_rx/freq_rx to downlink (patrz komentarz przy polu w HTML).
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
      window.UI?.showToast(_editId ? '✓ QSO zaktualizowane' : '✓ QSO zapisane');
      closeModal();
      load();
    } else {
      window.UI?.showToast('✗ Błąd: ' + (res.error || 'nieznany'), 'error');
    }
  } catch(e) {
    window.UI?.showToast('✗ Błąd zapisu: ' + e.message, 'error');
  }
}

// ── Usuń QSO ──────────────────────────────────────────────────────────────────
async function deleteQSO(id) {
  if (!confirm('Usunąć to QSO?')) return;
  const token = localStorage.getItem('token') || '';
  try {
    const r = await fetch(`/api/qsolog/${id}`, {
      method: 'DELETE',
      headers: token ? { 'Authorization': `Bearer ${token}` } : {},
    });
    const res = await r.json();
    if (res.ok) { window.UI?.showToast('✓ QSO usunięte'); load(); }
    else window.UI?.showToast('✗ ' + (res.error || 'Błąd'), 'error');
  } catch(e) {
    window.UI?.showToast('✗ ' + e.message, 'error');
  }
}

// ── Export ────────────────────────────────────────────────────────────────────
async function _exportFetch(format) {
  const token = localStorage.getItem('token') || '';
  // Priorytet 1: jesli operator ZAZNACZYL konkretne QSO — eksportuj tylko te.
  const selectedIds = [...document.querySelectorAll('.qso-chk:checked')]
    .map(el => el.dataset.id).filter(Boolean);

  const params = new URLSearchParams({ format });
  if (selectedIds.length) {
    // Eksport wybranych wpisow po ID
    params.set('ids', selectedIds.join(','));
  } else {
    // Priorytet 2: brak zaznaczenia — eksportuj wg filtrow (w tym zakres dat
    // od-do). Bez filtrow: caly log. NIE ograniczamy do widocznej strony.
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
      window.UI?.showToast(`✓ Wyeksportowano ${selectedIds.length} zaznaczonych QSO`, 'success');
    }
  } catch(e) {
    window.UI?.showToast('✗ Export błąd: ' + e.message, 'error');
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
  // Zakresy CELOWO SZEROKIE — suma alokacji amatorskich ze wszystkich 3
  // regionow ITU (Europa/Afryka/pln.Azja = R1, Ameryki = R2, reszta Azji/
  // Pacyfik = R3), nie tylko Europy. Ten soft moze trafic gdziekolwiek na
  // swiecie, wiec lepiej objac szerszy, prawdziwy zakres uzywany przez
  // ktoregokolwiek hama niz zawezac pod jeden kraj/region. Gorne (mikrofalowe)
  // pasma dopisane m.in. pod QO-100 (13cm uplink / 3cm downlink) i lacznosci
  // satelitarne w ogole (patrz pola SAT_NAME/SAT_MODE/FREQ_RX/BAND_RX w
  // qso_db.py).
  if (mhz >= 1.8    && mhz <= 2.0)    return '160m';
  if (mhz >= 3.5    && mhz <= 4.0)    return '80m';   // R2 siega do 4.0
  if (mhz >= 5.06   && mhz <= 5.45)   return '60m';   // rozne kanaly/zakresy wg kraju
  if (mhz >= 7.0    && mhz <= 7.3)    return '40m';   // R2/R3 siegaja do 7.3
  if (mhz >= 10.1   && mhz <= 10.15)  return '30m';
  if (mhz >= 14.0   && mhz <= 14.35)  return '20m';
  if (mhz >= 18.0   && mhz <= 18.17)  return '17m';
  if (mhz >= 21.0   && mhz <= 21.45)  return '15m';
  if (mhz >= 24.8   && mhz <= 24.99)  return '12m';
  if (mhz >= 28.0   && mhz <= 29.7)   return '10m';
  if (mhz >= 50.0   && mhz <= 54.0)   return '6m';
  if (mhz >= 70.0   && mhz <= 70.5)   return '4m';    // gl. Europa/Afryka, nieszkodliwe gdzie indziej
  if (mhz >= 144    && mhz <= 148)    return '2m';
  if (mhz >= 220    && mhz <= 225)    return '1.25m'; // R2 (USA/Kanada)
  if (mhz >= 420    && mhz <= 450)    return '70cm';  // R2 siega do 420-450, nie tylko 430-440
  if (mhz >= 902     && mhz <= 928)    return '33cm';  // R2 (USA)
  if (mhz >= 1240   && mhz <= 1300)   return '23cm';
  if (mhz >= 2300   && mhz <= 2450)   return '13cm';
  if (mhz >= 3400   && mhz <= 3410)   return '9cm';
  if (mhz >= 5650   && mhz <= 5925)   return '6cm';   // R2 siega do 5925
  if (mhz >= 10000  && mhz <= 10500)  return '3cm';
  if (mhz >= 24000  && mhz <= 24250)  return '1.2cm'; // R2 siega do 24250
  return '20m';
}

// ── Szybkie logowanie z zakładki RADIO ───────────────────────────────────────
// Podpowiedz raportu (RST) w polach SENT/RCVD zalezna od trybu: CW/CW-R -> 599,
// telefonia (USB/LSB/AM/FM/...) -> 59 — automatycznie po przelaczeniu modulacji
// (wolane z UI.updateModeButtons(), ktore i tak biegnie przy kazdej zmianie
// trybu: klik, telemetria, WS 'mode'). Nadpisuje pole TYLKO gdy wciaz trzyma
// jeden z dwoch znanych domyslnych raportow (albo jest puste) — recznie
// wpisany prawdziwy raport QSO nigdy nie jest nadpisywany.
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

async function quickLog() {
  const call = document.getElementById('qlog-call')?.value?.trim().toUpperCase();
  if (!call) {
    _setStatus('Wpisz znak!', 'red');
    document.getElementById('qlog-call')?.focus();
    return;
  }

  const now  = new Date();
  const pad  = n => String(n).padStart(2, '0');
  const qso  = {
    call,
    qso_date:  `${now.getUTCFullYear()}${pad(now.getUTCMonth()+1)}${pad(now.getUTCDate())}`,
    time_on:   `${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}`,
    time_off:  `${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}`,
    band:      _freqToBand(S?.freq || 0),
    mode:      S?.mode || 'SSB',
    freq:      S?.freq ? (S.freq / 1e6).toFixed(4) : '',
    rst_sent:  document.getElementById('qlog-rst-s')?.value || (String(S?.mode||'').toUpperCase().startsWith('CW') ? _RST_CW : _RST_PHONE),
    rst_rcvd:  document.getElementById('qlog-rst-r')?.value || (String(S?.mode||'').toUpperCase().startsWith('CW') ? _RST_CW : _RST_PHONE),
    gridsquare: document.getElementById('qlog-grid')?.value?.trim().toUpperCase() || '',
    my_call:   S?.callsign || '',
    my_gridsquare: (window.CurrentUser?.locator || S?.operatorLocator
                   || S?.stationLocator || ''),  // lokator OPERATORA
    comment:   '',
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
      // Wyczysc pole CALL, RST zostaje na 599
      document.getElementById('qlog-call').value = '';
      document.getElementById('qlog-grid').value = '';
      document.getElementById('qlog-call').focus();
      // Jesli jestesmy na stronie LOG — odswiez tabele
      if (document.getElementById('page-log')?.classList.contains('active')) load();
    } else {
      _setStatus('✗ ' + (res.error || 'błąd'), 'red');
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
  // Wygaś po 4s
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.textContent = ''; }, 4000);
}

// ── Import ADIF ──────────────────────────────────────────────────────────────
async function importADIF(input) {
  const file = input.files?.[0];
  if (!file) return;
  input.value = '';

  let text;
  try { text = await file.text(); }
  catch(e) { window.UI?.showToast('✗ Nie można odczytać pliku: ' + e.message, 'error'); return; }

  const qsos = _parseADIF(text);
  if (!qsos.length) { window.UI?.showToast('✗ Brak QSO w pliku ADIF', 'error'); return; }

  window.UI?.showToast(`Importuję ${qsos.length} QSO — proszę czekać...`);

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
    // Aktualizuj toast postępu
    const done = Math.min(i + CHUNK, qsos.length);
    window.UI?.showToast(`Importuję... ${done}/${qsos.length} QSO`);
  }

  // Komunikat: wczytane + osobno duplikaty (pominiete jako juz w logu)
  // i ewentualne bledne wpisy.
  let msg = `✓ Import ADIF: ${inserted} QSO wczytano`;
  if (duplicates) msg += `, ${duplicates} duplikatów pominięto`;
  if (skipped)    msg += `, ${skipped} błędnych`;
  window.UI?.showToast(msg, (skipped > 0) ? 'error' : 'info');
  load();
}

function _parseADIF(text) {
  const qsos = [];
  // Pomiń nagłówek (wszystko przed <EOH>)
  const eohIdx = text.toUpperCase().indexOf('<EOH>');
  const body   = eohIdx >= 0 ? text.slice(eohIdx + 5) : text;

  // Podziel na rekordy po <EOR>
  const records = body.split(/<EOR>/i);

  for (const rec of records) {
    if (!rec.trim()) continue;
    const fields = {};

    // Parsuj pola: <FIELD:długość>wartość
    const re = /<([A-Z0-9_]+):(\d+)(?::[A-Z])?>/gi;
    let match;
    while ((match = re.exec(rec)) !== null) {
      const tag = match[1].toUpperCase();
      const len = parseInt(match[2]);
      const val = rec.slice(match.index + match[0].length, match.index + match[0].length + len);
      fields[tag] = val.trim();
    }

    if (!fields.CALL) continue;  // pomijaj rekordy bez znaku

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

// ── Zaznaczanie i usuwanie zbiorcze ──────────────────────────────────────────
function selectAll(chk) {
  document.querySelectorAll('.qso-chk').forEach(el => el.checked = chk.checked);
}

async function deleteSelected() {
  const ids = [...document.querySelectorAll('.qso-chk:checked')].map(el => el.dataset.id);
  if (!ids.length) { window.UI?.showToast('Zaznacz QSO do usunięcia', 'error'); return; }
  if (!confirm(`Usunąć ${ids.length} zaznaczonych QSO?`)) return;
  const token = localStorage.getItem('token') || '';
  const h = {'Authorization': `Bearer ${token}`};
  let ok = 0;
  for (const id of ids) {
    try {
      const r = await fetch(`/api/qsolog/${id}`, {method:'DELETE', headers:h});
      if ((await r.json()).ok) ok++;
    } catch(e) {}
  }
  window.UI?.showToast(`✓ Usunięto ${ok} QSO`);
  load();
}

async function deleteAll() {
  const sel      = document.getElementById('log-filter-user');
  const userId   = sel?.value || '';
  const userName = sel?.selectedOptions[0]?.text || '';
  const who      = userId ? `użytkownika ${userName}` : 'WSZYSTKICH użytkowników';
  if (!confirm(`Usunąć WSZYSTKIE QSO ${who}?\nTej operacji nie można cofnąć!`)) return;
  if (!confirm(`Jesteś PEWIEN? Log ${who} zostanie skasowany!`)) return;
  const token = localStorage.getItem('token') || '';
  const h = {'Authorization': `Bearer ${token}`};
  const url = userId ? `/api/qsolog/all?user_id=${userId}` : '/api/qsolog/all';
  try {
    const r = await fetch(url, {method:'DELETE', headers:h});
    const res = await r.json();
    if (res.ok) { window.UI?.showToast(`✓ Usunięto ${res.count || ''} QSO`); load(); }
    else window.UI?.showToast('✗ ' + (res.error||'Błąd'), 'error');
  } catch(e) { window.UI?.showToast('✗ ' + e.message, 'error'); }
}

// ── Eksport modułu ────────────────────────────────────────────────────────────

// Sprawdz czy dany call byl juz worked (debounced 500ms po ostatnim wpisaniu)
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
      // Priorytet: identyczne QSO > tylko band > tylko mode > tylko call > new
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
    } catch(e) { console.warn('[qso] workedBefore blad:', e); }
  }, 500);
}

window.QSOLog = {
  load, sort, clearFilters, quickLog, updateRstDefaults, importADIF, selectAll, deleteSelected, deleteAll,
  prevPage, nextPage,
  openNew, openEdit, closeModal, saveQSO, deleteQSO, toggleSatFields, autoFillCountry,
  exportADI, exportCSV,
  loadAdminUsers: _loadAdminUsers,
  checkWorkedBefore,
};

})();
