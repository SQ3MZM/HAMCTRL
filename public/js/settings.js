
/**
 * settings.js — panel ustawień (frontend)
 */
(function() {
'use strict';

let _brands = {};   // cache modeli Hamlib
let _rigs   = [];   // cache konfiguracji radia
const BAUDS = ['1200','2400','4800','9600','19200','38400','57600','115200'];

function buildModelOptions(sel, brands, current) {
  sel.innerHTML = '';
  Object.entries(brands).forEach(([brand, models]) => {
    const og = document.createElement('optgroup');
    og.label = brand;
    models.forEach(m => {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = m.name;
      if (String(m.id) === String(current)) opt.selected = true;
      og.appendChild(opt);
    });
    sel.appendChild(og);
  });
}

function renderRigs(brands, rigs) {
  _brands = brands || _brands;
  _rigs   = rigs   || _rigs;

  const container = document.getElementById('rigs-settings-container');
  const addBtn    = document.getElementById('btn-add-rig');
  const isAdmin   = window.AppState?.role === 'admin';

  if (!container) return;
  // Przycisk "DODAJ RADIO" ukryty — obecna architektura obsluguje jedno radio.
  // (patrz addRig() ponizej). Zostaje w DOM, ale niewidoczny.
  if (addBtn) addBtn.style.display = 'none';

  container.innerHTML = '';
  (_rigs.length ? _rigs : [{}]).forEach((rig, idx) => {
    const id = rig.id || (idx + 1);
    container.appendChild(buildRigPanel(id, rig, isAdmin));
  });

  // Ładuj karty dźwiękowe po wyrenderowaniu DOM (krótkie opóźnienie)
  setTimeout(() => loadAudioCards(), 100);
}

function buildRigPanel(id, rig, isAdmin) {
  const div = document.createElement('div');
  div.style.cssText = 'border-bottom:1px solid var(--border);padding:14px 12px;';

  const speedOpts = BAUDS.map(b =>
    `<option value="${b}" ${(rig.speed||'19200')===b?'selected':''}>${b}</option>`
  ).join('');

  const modelOptsFn = (current) => {
    let html = '';
    Object.entries(_brands).forEach(([brand, models]) => {
      html += `<optgroup label="${brand}">`;
      models.forEach(m => {
        html += `<option value="${m.id}" ${String(m.id)===String(current)?'selected':''}>${m.name}</option>`;
      });
      html += '</optgroup>';
    });
    return html;
  };

  div.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
      <span style="font-family:var(--mono);font-size:11px;color:var(--green);letter-spacing:1px;">
        ${I18n.t('cfg_radio_n_lbl').replace('{id}', id)}${id===1?I18n.t('cfg_default_paren'):''}
      </span>
      ${isAdmin && id > 1 ? `<button onclick="Settings.removeRig(${id})" style="background:none;border:1px solid rgba(217,119,106,0.3);border-radius:3px;color:var(--red);font-family:var(--mono);font-size:10px;padding:2px 8px;cursor:pointer;">✕ ${I18n.t('common_delete_btn')}</button>` : ''}
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
      <div class="sg"><label>${I18n.t('cfg_name_lbl')}</label>
        <input type="text" id="cfg-name-${id}" value="${rig.name||''}" placeholder="np. IC-7300">
      </div>
      <div class="sg" style="grid-column:1/-1"><label>${I18n.t('cfg_model_hamlib_lbl')}</label>
        <select id="cfg-model-${id}">${modelOptsFn(rig.model||'3073')}</select>
      </div>
      <div class="sg"><label>${I18n.t('cfg_port_com_lbl')}</label>
        <input type="text" id="cfg-port-${id}" value="${rig.port||'COM'+(2+id)}" placeholder="COM3">
      </div>
      <div class="sg"><label>${I18n.t('cfg_baud_lbl')}</label>
        <select id="cfg-speed-${id}">${speedOpts}</select>
      </div>
      <div class="sg"><label>${I18n.t('cfg_civ_lbl')}</label>
        <input type="text" id="cfg-civ-${id}" value="${rig.civ||rig.civAddr||'0x94'}" placeholder="np. 0x94 / 0x56">
      </div>
      <div class="sg" style="grid-column:1/-1;margin-top:6px;padding-top:8px;border-top:1px solid var(--border);">
        <label style="color:var(--amber);">${I18n.t('cfg_card_rx_full_lbl')}</label>
        <div style="display:flex;gap:5px;">
          <select id="cfg-audio-rx-${id}" data-saved="${rig.audioRx||''}" style="flex:1;font-family:var(--mono);font-size:11px;">
            <option value="">${I18n.t('cfg_choose_rx_card')}</option>
            ${rig.audioRx ? `<option value="${rig.audioRx}" selected>${rig.audioRx}</option>` : ''}
          </select>
          <button onclick="Settings.loadAudioCards(${id})" style="background:none;border:1px solid var(--border);border-radius:4px;color:var(--dim);font-family:var(--mono);font-size:10px;padding:0 8px;cursor:pointer;" title="${I18n.t('cfg_refresh_title')}">⟳</button>
        </div>
        <div style="font-family:var(--mono);font-size:9px;color:var(--dim);margin-top:2px;">
          ${I18n.t('cfg_card_rx_desc')}
        </div>
      </div>
      <div class="sg" style="grid-column:1/-1;">
        <label style="color:var(--amber);">${I18n.t('cfg_card_tx_full_lbl')}</label>
        <select id="cfg-audio-tx-${id}" style="font-family:var(--mono);font-size:11px;">
          <option value="">${I18n.t('cfg_choose_tx_card')}</option>
          ${rig.audioTx ? `<option value="${rig.audioTx}" selected>${rig.audioTx}</option>` : ''}
        </select>
        <div style="font-family:var(--mono);font-size:9px;color:var(--dim);margin-top:2px;">
          ${I18n.t('cfg_card_tx_desc')}
        </div>
      </div>
    </div>

    <!-- CW KEYER — metoda i port DTR/RTS -->
    <div class="sg" style="grid-column:1/-1;margin-top:8px;padding-top:8px;border-top:1px solid var(--border);">
      <label style="color:var(--amber);">${I18n.t('cfg_cw_keyer_hdr')}</label>
      <select id="cfg-cw-method-${id}" style="font-family:var(--mono);font-size:11px;margin-bottom:8px;"
        onchange="Settings._cwMethodChange(${id})">
        <option value="auto">${I18n.t('cfg_cw_auto_opt')}</option>
        <option value="cat">${I18n.t('cfg_cw_cat_opt')}</option>
        <option value="dtr">${I18n.t('cfg_cw_dtr_opt')}</option>
        <option value="rts">${I18n.t('cfg_cw_rts_opt')}</option>
      </select>
      <div id="cfg-cw-dtr-section-${id}" style="display:none;">
        <div style="font-family:var(--mono);font-size:9px;color:var(--dim);margin-bottom:6px;line-height:1.8;">
          ${I18n.t('cfg_dtr_desc')}
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;">
          <div style="flex:1;min-width:120px;">
            <div style="font-family:var(--mono);font-size:9px;color:var(--dim);margin-bottom:3px;">${I18n.t('cfg_dtr_port_lbl')}</div>
            <input type="text" id="cfg-cw-dtr-port-${id}" placeholder="${I18n.t('cfg_dtr_port_ph')}"
              style="font-family:var(--mono);font-size:11px;" value="${rig.cwDtrPort||''}">
          </div>
          <div style="flex:1;min-width:100px;">
            <div style="font-family:var(--mono);font-size:9px;color:var(--dim);margin-bottom:3px;">${I18n.t('cfg_line_lbl')}</div>
            <select id="cfg-cw-dtr-line-${id}" style="font-family:var(--mono);font-size:11px;">
              <option value="DTR" ${(rig.cwDtrLine||'DTR')==='DTR'?'selected':''}>DTR</option>
              <option value="RTS" ${(rig.cwDtrLine||'DTR')==='RTS'?'selected':''}>RTS</option>
            </select>
          </div>
        </div>
        <button onclick="Settings._cwDtrSave(${id})" class="save-btn" style="margin-top:8px;">
          ${I18n.t('cfg_save_dtr_btn')}
        </button>
        <span id="cfg-cw-dtr-status-${id}" style="font-family:var(--mono);font-size:10px;color:var(--dim);margin-left:8px;"></span>
      </div>
    </div>

    <div style="display:flex;gap:6px;margin-top:10px;align-items:center;flex-wrap:wrap;">
      <button onclick="Settings.saveRig(${id})" class="save-btn">${I18n.t('settings_save_btn')}</button>
      <button onclick="Settings.connectRig(${id})" class="save-btn" style="background:rgba(184,201,143,0.05);">${I18n.t('cfg_connect_btn')}</button>
      <span id="rig-connect-status-${id}" style="font-family:var(--mono);font-size:10px;color:var(--dim);"></span>
    </div>
    ${id===1?`<div style="font-family:var(--mono);font-size:9px;color:var(--dim);margin-top:6px;padding:6px;background:var(--panel2);border-radius:3px;">
      ${I18n.t('cfg_rig1_note')}
    </div>`:''}
  `;
  return div;
}

// Admin: dodaj nowe radio
//
// WYLACZONE: obecna architektura obsluguje TYLKO JEDNO radio naraz — jeden
// strumien audio, jeden radio_lock, jedna instancja CI-V, jeden zestaw portow
// Rusta. Dodanie drugiego radia zapisywaloby sie do listy, ale serwer i tak
// laczy sie tylko z rigs[0], wiec operator dostawalby ciche, niedzialajace
// radio. Zeby nie wprowadzac w blad, blokujemy dodawanie. Pelna obsluga wielu
// radiow (rownolegla praca) to osobny, duzy etap — patrz notatki projektu.
function addRig() {
  window.UI?.showToast?.(I18n.t('cfg_single_rig_notice'), 'info');
}

// Admin: usuń radio (nie można usunąć radio 1)
function removeRig(id) {
  if (!window.AppState || window.AppState.role !== 'admin' || id === 1) return;
  _rigs = _rigs.filter(r => r.id !== id);
  renderRigs(_brands, _rigs);
}

// Alias dla kompatybilności wstecz
function populateModels(brands, rigs) {
  renderRigs(brands, rigs);
}

// ── Karty dźwiękowe ──────────────────────────────────────────────────────────
let _audioDevices = { rx:[], tx:[] };   // cache

async function loadAudioCards(rigId) {
  try {
    const r = await fetch('/api/audio/enumerate');
    if (!r.ok) return;
    const data = await r.json();
    _audioDevices = data.devices || { rx:[], tx:[] };

    // Wypełnij wszystkie widoczne radia
    const ids = rigId
      ? [rigId]
      : _rigs.map(r => r.id).filter(Boolean);

    if (!ids.length) ids.push(1);  // fallback do radio 1

    for (const id of ids) {
      fillAudioSelect(`cfg-audio-rx-${id}`, _audioDevices.rx);
      fillAudioSelect(`cfg-audio-tx-${id}`, _audioDevices.tx);
    }

    // Przywróć zapisane wartości z _rigs. Dopasowanie dokladne, z fallbackiem
    // na dopasowanie czesciowe (zawiera sie w / zawiera) - Windows potrafi
    // zwrocic nazwe karty USB troche inaczej zapisana miedzy kolejnymi
    // odpytaniami, a select.value przy braku dokladnego dopasowania cicho
    // wraca na pierwsza opcje mimo ze karta jest nadal zapisana poprawnie.
    const _selectFuzzy = (sel, wanted) => {
      if (!sel || !wanted) return;
      for (const opt of sel.options) if (opt.value === wanted) { sel.value = wanted; return; }
      for (const opt of sel.options) {
        if (opt.value && (opt.value.includes(wanted) || wanted.includes(opt.value))) { sel.value = opt.value; return; }
      }
    };
    _rigs.forEach(rig => {
      if (rig.audioRx) _selectFuzzy(document.getElementById(`cfg-audio-rx-${rig.id}`), rig.audioRx);
      if (rig.audioTx) _selectFuzzy(document.getElementById(`cfg-audio-tx-${rig.id}`), rig.audioTx);
    });

    const cnt = _audioDevices.rx?.length || 0;
    if (cnt > 0) {
      console.log(`[audio] ${cnt} kart audio załadowanych`);
    } else {
      console.log('[audio] Brak kart (Windows: wymagany FFmpeg lub PowerShell WMI)');
    }
  } catch(e) {
    console.warn('[audio] loadAudioCards:', e.message);
  }
}

function fillAudioSelect(elId, devices) {
  const sel = document.getElementById(elId);
  if (!sel) return;
  // Zachowaj obecną wartość (może być z zapisanej konfiguracji)
  const current = sel.value || sel.getAttribute('data-saved') || '';
  // Zachowaj opcję "zapisana karta" jeśli nie ma jej w liście
  const savedOpt = current && !(devices||[]).includes(current) ? current : null;

  sel.innerHTML = `<option value="">${I18n.t('cfg_choose_card_generic')}</option>`;
  if (savedOpt) {
    const opt = document.createElement('option');
    opt.value = opt.textContent = savedOpt;
    opt.selected = true;
    sel.appendChild(opt);
  }
  // Urzadzenia "USB Audio CODEC" (wbudowany kodek radia IC-7300/7610/9700/705
  // itp.) sortujemy na gore i oznaczamy ⭐ — to jest poprawny wybor, bo audio
  // do/z radia idzie przez USB, nie przez zewnetrzne gniazda ACC/AF Out/Mic.
  const sorted = [...(devices || [])].sort((a, b) => {
    const au = /usb audio/i.test(a) ? 0 : 1;
    const bu = /usb audio/i.test(b) ? 0 : 1;
    return au - bu;
  });
  sorted.forEach(d => {
    const opt = document.createElement('option');
    opt.value = d;
    opt.textContent = /usb audio/i.test(d) ? `⭐ ${d}` : d;
    if (d === current) opt.selected = true;
    sel.appendChild(opt);
  });
  if (current) sel.value = current;
}

function setAudioSelects(rigId, audioRx, audioTx) {
  const rx = document.getElementById(`cfg-audio-rx-${rigId}`);
  const tx = document.getElementById(`cfg-audio-tx-${rigId}`);
  if (rx) {
    // Jeśli wartość nie ma opcji jeszcze — dodaj ją tymczasowo
    if (audioRx && !rx.querySelector(`option[value="${audioRx}"]`)) {
      const opt = document.createElement('option');
      opt.value = opt.textContent = audioRx;
      rx.appendChild(opt);
    }
    rx.value = audioRx || '';
  }
  if (tx) {
    if (audioTx && !tx.querySelector(`option[value="${audioTx}"]`)) {
      const opt = document.createElement('option');
      opt.value = opt.textContent = audioTx;
      tx.appendChild(opt);
    }
    tx.value = audioTx || '';
  }
}

async function saveRig(id) {
  const get = el => document.getElementById(el)?.value || '';
  const body = {
    id,
    name:    get(`cfg-name-${id}`)    || `Radio ${id}`,
    model:   get(`cfg-model-${id}`)   || '3073',
    port:    get(`cfg-port-${id}`)    || `COM${id+2}`,
    speed:   get(`cfg-speed-${id}`)   || '19200',
    civ:     get(`cfg-civ-${id}`)     || '0x94',
    audioRx:    get(`cfg-audio-rx-${id}`),
    audioTx:    get(`cfg-audio-tx-${id}`),
    cwDtrPort:  get(`cfg-cw-dtr-port-${id}`) || '',
    cwDtrLine:  get(`cfg-cw-dtr-line-${id}`) || 'DTR',
    active:  id === 1,
  };
  // Uaktualnij cache
  const idx = _rigs.findIndex(r => r.id === id);
  if (idx >= 0) _rigs[idx] = { ..._rigs[idx], ...body };

  try {
    const r   = await fetch(`/api/config/rig/${id}`, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body),
    });
    const res = await r.json();
    if (res.ok) {
      window.UI?.showToast(I18n.t('cfg_toast_rig_saved').replace('{id}', id).replace('{name}', body.name));
    } else {
      window.UI?.showToast('✗ ' + (res.error||I18n.t('profile_error_fallback')), 'error');
    }
  } catch(e) { window.UI?.showToast('✗ ' + I18n.t('cfg_conn_error'), 'error'); }
}

async function connectRig(id) {
  const statusEl = document.getElementById(`rig-connect-status-${id}`);
  if (statusEl) statusEl.textContent = I18n.t('cfg_connecting');
  UI.showToast(I18n.t('cfg_toast_connecting_radio'));
  try {
    // Pobierz aktualne ustawienia z formularza
    const model = document.getElementById(`cfg-model-${id}`)?.value;
    const port  = document.getElementById(`cfg-port-${id}`)?.value;
    const speed = document.getElementById(`cfg-speed-${id}`)?.value;
    const civ   = document.getElementById(`cfg-civ-${id}`)?.value;
    const r   = await fetch('/api/rig/connect', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ rigId: id, model, port, speed, civ }),
    });
    const res = await r.json();
    if (res.ok) {
      const msg = res.sim
        ? I18n.t('cfg_sim_mode_msg').replace('{msg}', res.message||I18n.t('cfg_not_found'))
        : `✓ ${res.message||I18n.t('cfg_connected_fallback')}`;
      UI.showToast(msg, res.sim ? 'warn' : 'ok');
      if (statusEl) {
        statusEl.textContent = res.sim
          ? I18n.t('cfg_sim_no_rigctld').replace('{path}', res.rigctldPath||I18n.t('cfg_not_found'))
          : I18n.t('cfg_connected_status');
        statusEl.style.color = res.sim ? 'var(--amber)' : 'var(--green)';
      }
      // Odswiez panel funkcji radia (VFO A/B, sliders, func toggle) —
      // nowe radio moze miec inny zestaw capabilities
      window.RadioFunctions?.refresh();
      window.Admin?.loadRigFeatures?.();
    } else {
      const err = res.error || I18n.t('cfg_conn_error');
      UI.showToast('✗ ' + err, 'error');
      if (statusEl) {
        statusEl.textContent = '✗ ' + err;
        statusEl.style.color = 'var(--red)';
        statusEl.style.whiteSpace = 'normal';
      }
    }
  } catch(e) { UI.showToast('✗ ' + I18n.t('cfg_conn_error') + ': ' + e.message, 'error'); }
}

async function loadStatus() {
  try {
    const r   = await fetch('/api/health');
    const res = await r.json();
    const el  = document.getElementById('health-status');
    if (el) el.innerHTML = `
      <div>Uptime: <b>${res.uptime}s</b></div>
      <div>Node.js: <b>${res.node}</b></div>
      <div>Platforma: <b>${res.platform}</b></div>
      <div>Hamlib: <b style="color:${res.hamlib?'var(--green)':'var(--amber)'}">${res.hamlib ? 'POŁĄCZONO' : 'SYMULACJA'}</b></div>
      <div>Audio: <b style="color:${res.audio?'var(--green)':'var(--dim)'}">${res.audio ? 'AKTYWNE' : 'NIEAKTYWNE'}</b></div>
      <div>Klientów WS: <b>${res.listeners}</b></div>
    `;
  } catch(e) {}
}

async function toggleAudio(on) {
  const rxDevice = document.getElementById('cfg-audio-rx-1')?.value || null;
  // KRYTYCZNE: WS.enableAudio() tylko rejestruje tego klienta jako
  // subskrybenta strumienia audio (WS 'audio_start') — to NIE uruchamia
  // fizycznego odczytu z karty dzwiekowej/mikrofonu po stronie serwera.
  // To osobna operacja: audio_stream.py::start_rx() jest wolane WYLACZNIE
  // przez HTTP POST /api/audio/rx/start, ktorego nic w froncie nigdy nie
  // wywolywalo — efekt: kliknij "Wlacz audio RX", subskrypcja sie rejestruje,
  // OpusDecoder dziala poprawnie, ale serwer nigdy nie otwiera PyAudio
  // input stream (brak "[audio] RX START" w logach) wiec nie ma zadnych
  // danych do wyslania — totalna cisza bez zadnego bledu. Wywolujemy teraz
  // obie sciezki rownolegle przy wlaczaniu/wylaczaniu.
  if (on) {
    try {
      const r = await fetch('/api/audio/rx/start', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device: rxDevice })
      });
      const res = await r.json();
      if (!res.ok) {
        UI.showToast('✗ Nie udalo sie uruchomic audio RX na serwerze', 'error');
        console.error('[audio] /api/audio/rx/start nieudane:', res);
      }
    } catch (e) {
      UI.showToast('✗ Blad uruchamiania audio RX', 'error');
      console.error('[audio] /api/audio/rx/start blad:', e);
    }
  } else {
    try { await fetch('/api/audio/rx/stop', { method: 'POST' }); } catch (e) {}
  }
  await WS.enableAudio(on, rxDevice);
  const btn = document.getElementById('audio-toggle-btn');
  if (on) {
    UI.showToast('Włączono audio RX' + (rxDevice ? ': ' + rxDevice.slice(0,25) : ''));
    if (btn) { btn.textContent = '🔇 Wyłącz audio'; btn.style.color='var(--green)'; }
  } else {
    UI.showToast('Audio wyłączone');
    if (btn) { btn.textContent = '🔊 Włącz audio RX'; btn.style.color=''; }
  }
}

// ── CW Keyer method helpers ───────────────────────────────────────────────────
async function _cwMethodChange(id) {
  const method = document.getElementById(`cfg-cw-method-${id}`)?.value;
  const dtrSection = document.getElementById(`cfg-cw-dtr-section-${id}`);
  if (dtrSection) dtrSection.style.display = (method === 'dtr' || method === 'rts') ? '' : 'none';
  // Zapisz metode od razu
  const token = localStorage.getItem('token') || sessionStorage.getItem('ham_token');
  await fetch('/api/cw/method', {
    method: 'POST',
    headers: {'Content-Type':'application/json','Authorization':`Bearer ${token}`},
    body: JSON.stringify({method}),
  }).then(r => r.json())
    .then(d => UI.showToast(d.ok ? I18n.t('cfg_cw_method_toast').replace('{method}', method.toUpperCase()) : `✗ ${d.error||d.message||I18n.t('profile_error_fallback')}`))
    .catch(() => {});
}

async function _cwDtrSave(id) {
  const port   = document.getElementById(`cfg-cw-dtr-port-${id}`)?.value.trim() || '';
  const line   = document.getElementById(`cfg-cw-dtr-line-${id}`)?.value || 'DTR';
  const status = document.getElementById(`cfg-cw-dtr-status-${id}`);
  const token  = localStorage.getItem('token') || sessionStorage.getItem('ham_token');
  if (status) status.textContent = I18n.t('cfg_configuring');
  const r = await fetch('/api/cw/dtr-port', {
    method: 'POST',
    headers: {'Content-Type':'application/json','Authorization':`Bearer ${token}`},
    body: JSON.stringify({port, line}),
  }).then(r => r.json()).catch(e => ({ok:false,error:e.message}));
  if (status) {
    status.textContent = r.ok ? `✓ ${r.message}` : `✗ ${r.error}`;
    status.style.color = r.ok ? 'var(--green)' : 'var(--red)';
  }
}

// ── Inicjalizacja stanu CW method przy ladowaniu rig settings ─────────────────
async function _initCwStatus() {
  const token = localStorage.getItem('token') || sessionStorage.getItem('ham_token');
  try {
    const d = await fetch('/api/cw/status', {
      headers: {'Authorization': `Bearer ${token}`}
    }).then(r => r.json());
    // Ustaw select i widocznosc sekcji DTR dla kazdego zaladowanego radu
    document.querySelectorAll('[id^="cfg-cw-method-"]').forEach(sel => {
      const id = sel.id.replace('cfg-cw-method-','');
      sel.value = d.method || 'auto';
      const dtrSection = document.getElementById(`cfg-cw-dtr-section-${id}`);
      if (dtrSection) {
        dtrSection.style.display = (d.method==='dtr'||d.method==='rts') ? '' : 'none';
      }
      const portEl = document.getElementById(`cfg-cw-dtr-port-${id}`);
      if (portEl && d.dtrPort) portEl.value = d.dtrPort;
      const lineEl = document.getElementById(`cfg-cw-dtr-line-${id}`);
      if (lineEl && d.dtrLine) lineEl.value = d.dtrLine;
    });
  } catch(e) {}
}

window.Settings = { populateModels, renderRigs, saveRig, connectRig, loadStatus, toggleAudio, loadAudioCards, addRig, removeRig,
  loadAudioDevices: loadAudioCards,
  _cwMethodChange, _cwDtrSave, _initCwStatus,
};


})();
