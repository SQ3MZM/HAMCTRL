/**
 * ui.js — renderowanie UI, obsługa zdarzeń
 */
(function() {
'use strict';

const S = window.AppState;

// ── Formatowanie ──────────────────────────────────────────────────────────────
function fmtFreq(hz) {
  if (hz >= 1e6) {
    const s = (hz / 1e6).toFixed(5);
    const [i, d] = s.split('.');
    return `${i}.${d.slice(0,3)}.${d.slice(3)}`;
  }
  return (hz / 1000).toFixed(3);
}

function fmtBand(freq) {
  for (const [b, r] of Object.entries(S.bands)) {
    if (freq >= r.min && freq <= r.max) return b;
  }
  return '?';
}

// ── Połączenie ────────────────────────────────────────────────────────────────
function updateConnectionStatus(on) {
  const dot   = document.getElementById('conn-dot');
  const label = document.getElementById('conn-label');
  const sim   = document.getElementById('sim-badge');
  if (dot)   dot.className   = 'dot ' + (on ? 'green' : 'red');
  if (label) label.textContent = on ? 'ONLINE' : 'OFFLINE';
  if (sim)   sim.style.display = S.sim ? 'inline' : 'none';
}

// ── Częstotliwość ─────────────────────────────────────────────────────────────
function updateFreqDisplay() {
  const el = document.getElementById('freq-display');
  if (el) el.textContent = fmtFreq(S.freq);
  const elB = document.getElementById('freq-b-display');
  if (elB) elB.textContent = fmtFreq(S.freqB);
  updateBandButtons();
  // Odswież cyfrowy wyswietlacz VFO (renderowany przez vfo.js)
  window.VFO?.updateVFODisplay?.();
}

// Motyw kolorystyczny — zapisywany w localStorage per user.
function setTheme(theme) {
  const valid = ['default', 'blue', 'mono', 'amber-classic'];
  if (!valid.includes(theme)) theme = 'default';
  if (theme === 'default') {
    document.documentElement.removeAttribute('data-theme');
  } else {
    document.documentElement.setAttribute('data-theme', theme);
  }
  try {
    const uid = window.CurrentUser?.id || 'default';
    localStorage.setItem(`theme_${uid}`, theme);
  } catch(e) {}
  const sel = document.getElementById('profile-theme');
  if (sel) sel.value = theme;
}

// Zaladuj zapisany motyw przy starcie
function loadTheme() {
  try {
    const uid = window.CurrentUser?.id || 'default';
    const saved = localStorage.getItem(`theme_${uid}`) || 'default';
    setTheme(saved);
  } catch(e) {}
}

function _canControlRadio() {
  const lock  = window.AppState?.radio_lock;
  const myUid = String(window.AppState?.my_uid || window.CurrentUser?.id || '');
  const role  = window.CurrentUser?.role;
  if (role === 'admin') return true;
  if (!lock?.locked) return false;
  return String(lock.user_id) === myUid;
}

// Wyszarza wszystkie kontrolki radia gdy user nie trzyma TRX. Uzytkownik
// widzi wszystko (freq, waterfall, S-meter) ale nie moze klikac ani
// wysylac komend. Adminowi UI pozostaje pelne.
function applyRadioLockUI() {
  const canControl = _canControlRadio();
  const page = document.getElementById('page-radio');
  if (!page) return;
  // Blokada radio_lock idzie WYLACZNIE przez te klase (CSS pointer-events w
  // style.css, .radio-readonly .ptt-btn i .radio-readonly button:not(...)) —
  // to jedyne miejsce ktore decyduje kto moze klikac. updatePTT() ponizej NIE
  // wie nic o radio_lock (patrz jej definicja) — odswieza tylko cross-band
  // split guard, wiec musi byc wolana tutaj żeby zdazyc zanim zwroci UI.
  // Wczesniej istnial tu blednie opisany drugi mechanizm (_setUILocked w
  // index.html, usuniety w audycie zakladki RADIO 2026-08-15) ktory rowniez
  // ustawial .disabled — ten komentarz sie do niego odnosil.
  page.classList.toggle('radio-readonly', !canControl);
  updatePTT();
}

function sendFreq(freq) {
  freq = parseInt(freq);
  if (!freq || freq < 100000) return;
  if (!_canControlRadio()) {
    const holder = window.AppState?.radio_lock?.callsign ||
                   window.AppState?.radio_lock?.username || '?';
    showToast(`⛔ Radio zajęte przez ${holder} — przejmij TRX`, 'error');
    return;
  }
  S.freq = freq;
  S._localFreqSetAt = Date.now();
  updateFreqDisplay();
  updateVFOBadges();   // natychmiast zaktualizuj badge pasma (nie czekaj na serwer)
  updatePTT();         // cross-band guard
  scheduleBandMemorySave();  // zapisz do historii/pamieci pasma (debounced 2s)
  WS.sendFreqFast ? WS.sendFreqFast(freq) : WS.send({ type: 'freq', freq });
}

function tune(dir) {
  const step = parseInt(document.getElementById('step-select')?.value || 1000);
  const f    = Math.max(100000, S.freq + dir * step);
  sendFreq(f);
}

function gotoFreq(freq, mode) {
  sendFreq(freq);
  if (mode && mode !== S.mode) setMode(mode);
}

// ── Tryb ──────────────────────────────────────────────────────────────────────
function buildModeGrid() {
  const modes = (S.modes && S.modes.length) ? S.modes : ['USB','LSB','AM','FM','CW'];
  const modeHtml = modes.map(m =>
    `<button class="mode-btn ${m === S.mode ? 'active' : ''}" onclick="UI.setMode('${m}')">${m}</button>`
  ).join('');
  ['mode-grid','mode-grid-left'].forEach(id => {
    const el2 = document.getElementById(id);
    if (el2) el2.innerHTML = modeHtml;
  });
}

function setMode(mode, bw) {
  S.mode = mode;
  if (bw) S.bandwidth = bw;
  updateModeButtons();
  updateVFOBadges();   // natychmiast zaktualizuj badge trybu

  // Filtr domyslny dla trybu — moze byc '1'/'2'/'3'
  const filterRaw = S.modeFilters?.[mode];
  let filterNum = null;
  if (filterRaw) {
    const n = parseInt(String(filterRaw).replace(/[^0-9]/g, ''));
    if (n >= 1 && n <= 3) filterNum = n;
  }

  // Zaktualizuj select filtra w UI
  if (filterNum) {
    const sel = document.getElementById('bw-select');
    if (sel) sel.value = String(filterNum);
  }

  const msg = { type: 'mode', mode, bandwidth: S.bandwidth };
  if (filterNum) msg.filterNum = filterNum;
  WS.send(msg);
}

function updateModeButtons() {
  document.querySelectorAll('.mode-btn').forEach(b => {
    b.classList.toggle('active', b.textContent === S.mode);
  });
  // Podpowiedz RST w "LOG QSO" (CW->599, telefonia->59) — wolane stad bo
  // updateModeButtons() i tak biegnie przy KAZDEJ zmianie trybu (klik,
  // telemetria, WS 'mode'), wiec to jedyne miejsce ktore trzeba podpiac.
  window.QSOLog?.updateRstDefaults?.(S.mode);
}

// ── Pasma ─────────────────────────────────────────────────────────────────────
// S.bands byl dotychczas ZAWSZE pusty po stronie klienta (nigdy nie
// wypelniany) — stad przyciski pasm nigdy sie nie podswietlaly i fallback
// w buildBandGrid() ('20m': {def:...} BEZ min/max) sprawial ze
// updateBandButtons() porownywalo f>=undefined, co zawsze daje false.
// Backend MA poprawne dane (min/max/def dla 14 pasm) pod /api/config/bands,
// ale dotychczas nic ich stamtad nie pobieralo do S.bands. Wolane raz przy
// starcie (app:ready) i przy kazdym 'config_update' z backendu (gdy admin
// zmieni liste wlaczonych pasm w ustawieniach).
async function loadBandsConfig() {
  try {
    const [rb, rm] = await Promise.all([
      fetch('/api/config/bands'),
      fetch('/api/config/modes'),
    ]);
    const db = await rb.json();
    const dm = await rm.json();

    const allBands = db.allBands || {};
    const enabled  = new Set(db.enabledBands || Object.keys(allBands));
    const bands    = {};
    for (const [name, info] of Object.entries(allBands)) {
      if (enabled.has(name)) bands[name] = info;
    }
    S.bands = bands;

    // Tryby i filtry
    S.modes       = dm.enabledModes || dm.allModes || ['USB','LSB','AM','FM','CW'];
    S.modeFilters = dm.modeFilters  || {};

    buildBandGrid();
    buildModeGrid();
    updateBandButtons();
  } catch (e) {
    console.warn('[ui] loadBandsConfig blad:', e);
  }
}

// LocalStorage per-user pamiec czestotliwosci per pasmo. Kluczem jest
// nazwa pasma (np. '20m'), wartoscia obiekt { freq, mode, ts }.
function _bandMemoryKey() {
  const uid = window.CurrentUser?.id || 'default';
  return `bandFreq_${uid}`;
}
function _loadBandMemory() {
  try { return JSON.parse(localStorage.getItem(_bandMemoryKey()) || '{}'); }
  catch { return {}; }
}
function _saveBandMemory(mem) {
  try { localStorage.setItem(_bandMemoryKey(), JSON.stringify(mem)); }
  catch(e) { console.warn('[band-mem] save blad:', e); }
}
// Zapisz aktualna freq+mode do pamieci pasma (wywolywane po kazdej zmianie freq)
function saveBandMemory() {
  const band = getBandName(S.freq);
  if (!band || band.includes('MHz') || band.includes('Hz')) return; // nie zapisuj gdy poza pasmem
  const mem = _loadBandMemory();
  mem[band] = { freq: S.freq, mode: S.mode, ts: Date.now() };
  _saveBandMemory(mem);
}

function buildBandGrid() {
  const bands = Object.keys(S.bands).length ? S.bands : { '20m': { def: 14200000 } };
  const mem = _loadBandMemory();
  const bandHtml = Object.entries(bands)
    .map(([b, r]) => {
      // Uzyj zapamietanej freq jesli istnieje, inaczej domyslnej z konfiguracji
      const remembered = mem[b];
      const freq = remembered?.freq || r.def;
      const mode = remembered?.mode || S.mode;
      return `<button class="band-btn" onclick="UI.gotoFreq(${freq},'${mode}')" title="Zapamiętane: ${(freq/1e6).toFixed(3)} MHz ${mode}">${b}</button>`;
    })
    .join('');
  ['band-grid','band-grid-left'].forEach(id => {
    const el2 = document.getElementById(id);
    if (el2) el2.innerHTML = bandHtml;
  });
  const el = document.getElementById('band-grid') || document.getElementById('band-grid-left');
  if (!el) return;
  updateBandButtons();  // od razu podswietl wlasciwy przycisk wg aktualnej S.freq
}

function updateBandButtons() {
  // Podswietl pasmo wg AKTYWNEGO VFO — freqB gdy aktywny jest VFO B,
  // freq gdy aktywny jest VFO A (domyslnie). Poprzednio zawsze uzywal S.freq,
  // co powodowalo ze po przelaczeniu na VFO B przyciski pasm pokazywaly
  // pasmo VFO A zamiast aktualnego pasma VFO B.
  const f = (S.vfo === 'VFOB') ? (S.freqB || S.freq) : S.freq;
  document.querySelectorAll('.band-btn').forEach(btn => {
    const band = S.bands[btn.textContent];
    btn.classList.toggle('active', !!band && f >= band.min && f <= band.max);
  });
}

// Pobierz pasma raz po starcie aplikacji (auth.js emituje 'app:ready' po
// zalogowaniu — ten sam wzorzec uzywany przez settings.js do pobierania
// modeli rigow).
window.addEventListener('app:ready', () => { loadBandsConfig(); loadTheme(); });


// ── PTT ───────────────────────────────────────────────────────────────────────
function setPTT(on) {
  S.ptt = !!on;
  updatePTT();
  WS.send({ type: 'ptt', ptt: on });
}

function updatePTT() {
  const btn = document.getElementById('ptt-btn');
  const dot = document.getElementById('ptt-dot');
  const lbl = document.getElementById('ptt-label');
  if (btn) btn.classList.toggle('active', S.ptt);
  if (dot) dot.className = 'dot ' + (S.ptt ? 'red' : 'green');
  if (lbl) lbl.textContent = S.ptt ? 'TX' : 'RX';

  // Wycisz RX na czas KAZDEGO nadawania (fonia i cyfra). Radio z MONI podaje
  // na USB-out monitor wlasnego TX -> przy fonii slychac wlasne ECHO, przy
  // FT8 piszczace tony. Podczas TX pasma i tak nie ma na RX, wiec duck nic
  // nie odbiera. Idempotentne z duckiem FT8 (ft8_tx_status).
  window.setTxAudioDuck?.(!!S.ptt);

  // Cross-band split guard: wylacz PTT gdy VFO-A i VFO-B w roznych pasmach
  // (chroni radio/antene przed nadawaniem na niewlasciwym pasmie)
  const cross = isCrossBandSplit();
  if (btn) {
    if (cross && !S.ptt) {
      btn.disabled = true;
      btn.classList.add('crossband-blocked');
      btn.title = `⛔ Cross-band split: RX ${getBandName(S.freq)} / TX ${getBandName(S.freqB || S.freq)}. Wylacz split zeby nadawac.`;
    } else {
      btn.disabled = false;
      btn.classList.remove('crossband-blocked');
      btn.title = '';
    }
  }
}

// Sprawdza czy split jest aktywny i VFO-A/B sa w roznych pasmach.
// Wywolywane przy updatePTT i po kazdej zmianie freq/freqB/split.
function isCrossBandSplit() {
  if (!S.split) return false;
  const bandA = getBandName(S.freq);
  const bandB = getBandName(S.freqB || S.freq);
  return bandA !== bandB;
}

// ── S-meter ───────────────────────────────────────────────────────────────────
function updateSMeter(val) {
  S.sMeter = val;
  // Bargraf odwzorowuje REALNA skale S-metra IC-7300, dopasowana do podzialki
  // w HTML (8 etykiet: 1,3,5,7,9,+20,+40,+60 rozlozone rowno przez
  // space-between, wiec "9" jest na 4/7 = ~57% szerokosci):
  //   S0..S9      -> 0..57% (S9 pod etykieta "9")
  //   S9..S9+60dB -> 57..100% (+20@71%, +40@86%, +60@100%)
  // Poprzednio pasek stawal na 100% juz przy S9 i nie pokazywal S9+ wcale.
  // val: 0..9 = S0..S9, 9..15 = S9+0..+60dB (kazda jednostka val>9 to +10dB).
  let pct;
  if (val <= 9) {
    pct = (val / 9) * 57;              // S0..S9 -> 0..57% (pod etykieta "9")
  } else {
    pct = 57 + ((val - 9) / 6) * 43;   // S9..S9+60dB (val 9..15) -> 57..100%
  }
  pct = Math.min(100, Math.max(0, pct));
  const fill = document.getElementById('smeter-fill');
  const disp = document.getElementById('smeter-value');
  const lbl  = document.getElementById('smeter-text');
  if (fill) fill.style.clipPath = `inset(0 ${100 - pct}% 0 0)`;
  const txt = val <= 9 ? `S ${Math.round(val)}` : `S9+${Math.round((val - 9) * 10)}dB`;
  if (disp) disp.textContent = txt;
  if (lbl)  lbl.textContent  = txt;
}

// ── TRX meter (ALC/PWR/SWR/VOLT) ────────────────────────────────────────────
// Przechowuje ostatnia znana wartosc KAZDEGO z 4 miernikow (backend wysyla je
// wszystkie cyklicznie, niezaleznie od tego ktory user akurat oglada) — dzieki
// temu przelaczenie selecta pokazuje dane natychmiast, bez czekania na
// nastepny cykl odpytywania CI-V (do 2s przy PTT off, patrz civ.py n%8==0).
const _txMeterValues = { ALC: null, PWR: null, SWR: null, VOLT: null };
let _txMeterSelected = 'ALC';

const _TXMETER_UNITS = { ALC: '%', PWR: '%', SWR: '', VOLT: 'V' };

// Podzialka pod paskiem — dopasowana do REALNEGO zakresu odczytu z radia
// (civ.py), nie do prostej skali 0-100. Kazdy punkt: {at: pozycja w % szer.
// paska (0-100), label: tekst}. Dla ALC/PWR pct jest liniowe wzgledem
// wartosci, wiec podzialka tez liniowa. Dla SWR pct = min(1.0, raw/120)
// podczas gdy value = 1.0 + (raw/241)*49 — te dwie skale NIE sa liniowo
// zalezne od siebie, wiec pozycje 1.5/2.0/3.0 musza byc przeliczone z
// powrotem na pct (patrz civ.py komentarz: 0=1.0, 48=1.5, 80=2.0, 120=3.0,
// gdzie pct=raw/120 — czyli np. 1.5 jest przy pct=48/120=40%).
const _TXMETER_SCALES = {
  ALC:  [{at:0,label:'0'}, {at:25,label:'25'}, {at:50,label:'50'}, {at:75,label:'75'}, {at:100,label:'100%'}],
  PWR:  [{at:0,label:'0'}, {at:25,label:'25'}, {at:50,label:'50'}, {at:75,label:'75'}, {at:100,label:'100%'}],
  SWR:  [{at:0,label:'1.0'}, {at:40,label:'1.5'}, {at:66.7,label:'2.0'}, {at:100,label:'3.0'}],
  VOLT: [],  // brak podzialki — VOLT to stabilna liczba, nie ruchoma skala (patrz nizej)
};

function updateTxMeter(msg) {
  if (!msg.meter || !(msg.meter in _txMeterValues)) return;
  _txMeterValues[msg.meter] = msg;
  if (msg.meter === _txMeterSelected) _renderTxMeter();
}

function setTxMeter(meter) {
  if (!(meter in _txMeterValues)) return;
  _txMeterSelected = meter;
  // Jedyny select sterujacy bargrafem jest teraz w lewej kolumnie
  // (sekcja TRX FUNKCJE pod PASMO) — synchronizujemy go na wypadek
  // wywolania setTxMeter() z innego miejsca niz sam dropdown.
  const sel = document.getElementById('trx-funkcje-select');
  if (sel) sel.value = meter;
  _renderTxMeter();
}

function _renderTxMeter() {
  const label = document.getElementById('txmeter-label');
  const fill  = document.getElementById('txmeter-fill');
  const value = document.getElementById('txmeter-value');
  const scale = document.getElementById('txmeter-scale');
  const m     = _txMeterSelected;
  const data  = _txMeterValues[m];

  if (label) label.textContent = m;
  if (fill) {
    fill.className = 'txmeter-fill ' + m.toLowerCase();
    let pct = data ? Math.min(100, Math.max(0, data.pct * 100)) : 0;
    // VOLT: napiecie zasilania zmienia sie powoli i nieznacznie (szum
    // odczytu CI-V rzedu ±0.1-0.2V) — bez tego pasek "drgalby" przy kazdym
    // cyklu odpytywania mimo ze realnie nic sie nie zmienia. Kwantyzujemy
    // do krokow 5%, wiec pasek przesuwa sie TYLKO przy faktycznej, widocznej
    // zmianie napiecia, nie przy szumie pomiarowym.
    if (m === 'VOLT' && data) pct = Math.round(pct / 5) * 5;
    fill.style.width = pct + '%';
  }
  if (scale) {
    const ticks = _TXMETER_SCALES[m] || [];
    scale.innerHTML = ticks.map(t => `<span style="left:${t.at}%">${t.label}</span>`).join('');
  }
  if (value) {
    value.textContent = data ? `${data.value}${_TXMETER_UNITS[m]}` : '--';
  }
}

// ── Suwaki ────────────────────────────────────────────────────────────────────
function setLevel(param, value) {
  value = parseInt(value);
  if (param === 'RFPOWER') { S.rfPower = value; const el = document.getElementById('rf-power-val');  if(el) el.textContent = value; }
  if (param === 'AF')      { S.afGain  = value; const el = document.getElementById('af-gain-val');   if(el) el.textContent = value; }
  if (param === 'SQL')     { S.squelch = value; const el = document.getElementById('squelch-val');   if(el) el.textContent = value; }
  WS.send({ type: 'level', param, value });
}

// ── VFO ───────────────────────────────────────────────────────────────────────
function vfoSwap() {
  [S.freq, S.freqB] = [S.freqB, S.freq];
  WS.send({ type: 'freq', freq: S.freq });
  updateFreqDisplay();
}
function vfoCopy() {
  S.freqB = S.freq;
  updateFreqDisplay();
}
function toggleSplit() {
  S.split = !S.split;
  document.getElementById('split-btn')?.classList.toggle('active', S.split);
  WS.send({ type: 'split', split: S.split, freqB: S.freqB });
}

// ── Pamięci ───────────────────────────────────────────────────────────────────
function saveMemory() {
  const name = prompt('Nazwa częstotliwości (opcjonalnie):') ?? '';
  S.memories.push({ freq: S.freq, mode: S.mode, name });
  S.saveMemories();
  renderMemories();
}

function deleteMemory(i) {
  S.memories.splice(i, 1);
  S.saveMemories();
  renderMemories();
}

function renderMemories() {
  const el = document.getElementById('memory-list');
  if (!el) return;
  if (!S.memories.length) {
    el.innerHTML = '<li style="padding:10px 8px;font-family:var(--mono);font-size:11px;color:var(--dim)">Brak zapisanych</li>';
    return;
  }
  el.innerHTML = S.memories.map((m, i) => `
    <li class="memory-item" onclick="UI.gotoFreq(${m.freq},'${m.mode}')">
      <span class="memory-freq">${fmtFreq(m.freq)}</span>
      <span class="memory-mode">${m.mode}</span>
      <span class="memory-name">${m.name || ''}</span>
      <button class="memory-del" onclick="event.stopPropagation();UI.deleteMemory(${i})">×</button>
    </li>`).join('');
}

// ── Spektrum + Waterfall ──────────────────────────────────────────────────────
let specCtx = null, wfCtx = null;

function initCanvases() {
  const spec = document.getElementById('spectrum');
  const wf   = document.getElementById('waterfall');
  if (!spec || !wf) return;
  spec.width = spec.offsetWidth || 600; spec.height = 60;
  wf.width   = wf.offsetWidth   || 600; wf.height   = 100;
  specCtx = spec.getContext('2d');
  wfCtx   = wf.getContext('2d');
}

function drawSpectrum() {
  if (!specCtx) return;
  const W = specCtx.canvas.width, H = specCtx.canvas.height;
  specCtx.fillStyle = '#060806';
  specCtx.fillRect(0, 0, W, H);

  const sig = S.ptt ? 0.9 : (S.sMeter / 9) * 0.8;
  specCtx.beginPath();
  specCtx.strokeStyle = S.ptt ? '#e05252' : '#4cdb6a';
  specCtx.lineWidth = 1.5;
  for (let i = 0; i < W; i++) {
    const dist = Math.abs(i - W / 2) / (W / 2);
    const y    = H - (Math.exp(-dist * 12) * sig + Math.random() * 0.06) * H * 0.9;
    i === 0 ? specCtx.moveTo(i, y) : specCtx.lineTo(i, y);
  }
  specCtx.stroke();

  // Marker środka
  specCtx.strokeStyle = 'rgba(240,180,41,0.5)';
  specCtx.lineWidth = 1;
  specCtx.beginPath();
  specCtx.moveTo(W/2, 0); specCtx.lineTo(W/2, H);
  specCtx.stroke();

  // Waterfall — przesuń w dół o 1px i dorysuj nową linię
  if (wfCtx) {
    const Ww = wfCtx.canvas.width, Hw = wfCtx.canvas.height;
    const img = wfCtx.getImageData(0, 0, Ww, Hw - 1);
    wfCtx.putImageData(img, 0, 1);
    for (let i = 0; i < Ww; i++) {
      const x    = Math.floor(i * W / Ww);
      const dist = Math.abs(x - W / 2) / (W / 2);
      const v    = Math.min(1, Math.exp(-dist * 12) * sig + Math.random() * 0.05);
      wfCtx.fillStyle = `rgb(${Math.floor(v*100)},${Math.floor(v*200)},${Math.floor(30 + v*180)})`;
      wfCtx.fillRect(i, 0, 1, 1);
    }
  }
}

// ── Strony ────────────────────────────────────────────────────────────────────
function setPage(name) {
  // Podswietl aktywna zakladke
  document.querySelectorAll('.tab-btn').forEach(b => {
    const onclick = b.getAttribute('onclick') || '';
    b.classList.toggle('active',
      onclick.includes("'" + name + "'") || onclick.includes('"' + name + '"'));
  });

  // Przelacz strone (inline pages)
  document.querySelectorAll('.page-content').forEach(p => p.classList.remove('active'));
  const pg = document.getElementById('page-' + name);
  if (pg) pg.classList.add('active');
  // Blokuj przewijanie body na stronie FT8
  document.body.classList.toggle('page-wsjtx-active', name === 'wsjtx');

  // Reset scroll do gory - kazda zakladka zaczyna od poczatku.
  // Bez tego przewiniecie strony w Radio powoduje "przesuniete" widoki w
  // innych zakladkach (window.scrollY jest wspolne dla wszystkich page-content).
  // Uzywamy 'instant' zeby nie animowac zmiany (irytujace przy klikaniu tab-ow).
  try {
    window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
  } catch(e) {
    // Fallback dla starszych przegladarek ktore nie wspieraja opcji obiektu
    window.scrollTo(0, 0);
  }

  // Subskrypcja kanalow WebSocket wg zakladki. Serwer wysyla wiadomosci
  // tylko klientom ktorzy sa zasubskrybowani na dany kanal - dzieki temu
  // klient w Log QSO nie dostaje scope_frame ani ft8_waterfall (~50KB/s
  // zbednych danych + narzut na parsowanie JSON w JS).
  //
  // 'control' zawsze zostaje (freq, mode, ptt, radio_lock, chat, presence).
  // Dodatkowe kanały wg potrzeb zakladki:
  //   Radio    -> scope     (waterfall CI-V)
  //   WSJT-X   -> scope+ft8 (waterfall + decodes + auto QSO + tune status)
  //   DXCluster -> dxcluster
  //   inne     -> tylko control
  const channelsForPage = {
    radio:     ['control', 'scope'],
    wsjtx:     ['control', 'scope', 'ft8'],  // scope tez bo VFO badge/freq
    dxcluster: ['control', 'dxcluster'],
    log:       ['control'],
    profile:   ['control'],
    config:    ['control'],
    settings:  ['control'],
    internet:  ['control'],
    admin:     ['control'],
  };
  const channels = channelsForPage[name] || ['control'];
  if (window.WS?.send) {
    window.WS.send({ type: 'subscribe', channels });
  }

  // Akcje per strona
  if (name === 'log')      { window.QSOLog?.load?.(); window.QSOLog?.loadAdminUsers?.(); }
  if (name === 'internet') { window.Tunnel?.load?.(); window.Tunnel?.checkCF?.(); window.Tunnel?.startAutoRefresh?.(); }
  else { window.Tunnel?.stopAutoRefresh?.(); }
  if (name === 'wsjtx')    {
    window.FT8Timer?.init?.();
    // setTimeout pozwala przegladarce wyrenderowac page-content.active
    // zanim init() sprawdzi wymiary canvas (display:none = width:0)
    setTimeout(() => { window.WSJTX?.init?.(); window.WSJTX?.loadWorkedCalls?.(); }, 50);
  }
  if (name === 'admin')    { window.Admin?.loadUsers?.(); window.Admin?.loadFt8Timers?.(); window.AdminSmtp?.load?.(); window.AdminStatus?.refresh?.(); }
  if (name === 'config')   {
    window.Admin?.loadRotatorConfig?.(); window.Admin?.loadRigFeatures?.(); window.AdminBands?.load?.();
    window.Admin?.loadRelayConfig?.();
    window.AudioAutoDetect?.load?.();
    window.ComBridge?.load?.();
    var m = window.AppState?.models;
    if (m && Object.keys(m).length) { window.Settings?.renderRigs?.(m, window.AppState.rigs); }
    else { fetch('/api/config').then(r=>r.json()).then(d=>{ if(d.models) window.Settings?.renderRigs?.(d.models,d.rigs||[]); }).catch(()=>{}); }
  }
  if (name === 'profile')  { if (typeof loadProfile === 'function') loadProfile(); window.ProfileAudio?.load?.(); }
  else { window.ProfileAudio?.stopMeter?.(); }
  if (name === 'settings') {
    window.Settings?.loadStatus?.();
    window.ComBridge?.load?.();
    var m = window.AppState?.models;
    if (m && Object.keys(m).length) { window.Settings?.renderRigs?.(m, window.AppState.rigs); }
    if (typeof loadProfile === 'function') loadProfile();
    if (typeof deepcwAdminRefreshStatus === 'function') deepcwAdminRefreshStatus();
  }
  if (name === 'dxcluster') {
    window.DXCluster?.loadConfig?.();
    window.DXCluster?.renderSpots?.();
  }
}

// ── Toast ─────────────────────────────────────────────────────────────────────
function showToast(msg, type = 'info') {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className   = 'toast show ' + type;
  clearTimeout(el._timer);
  const duration = (type === 'error' || type === 'warning') ? 4500 : 2800;
  el._timer = setTimeout(() => el.classList.remove('show'), duration);
}


// ── Pełny refresh ─────────────────────────────────────────────────────────────
function fullRefresh() {
  updateConnectionStatus(S.connected);
  updateFreqDisplay();
  buildModeGrid();
  buildBandGrid();
  updateModeButtons();
  updatePTT();
  updateSMeter(S.sMeter);
  updateVFOBadges();
  updateFreqB();
  renderMemories();
  // Callsign w headerze
  const cs = document.getElementById('callsign-display');
  if (cs) cs.textContent = S.callsign || '--';
  // SIM badge
  const sim = document.getElementById('sim-badge');
  if (sim) sim.style.display = S.sim ? 'inline' : 'none';
  // Nazwy rig w selecie
  const rigSel = document.getElementById('active-rig-select');
  if (rigSel) {
    if (S.rigs.length > 1) {
      rigSel.innerHTML = S.rigs.map(r => `<option value="${r.id}">${r.name}</option>`).join('');
      rigSel.style.display = '';
    } else {
      // Tylko 1 radio — ukryj selektor, nie ma sensu wybierać
      rigSel.style.display = 'none';
    }
  }
}

// Historia freq — automatyczna, ostatnie 20 wartosci gdzie user byl
// (pomija drobne strojenie, tylko wieksze skoki >5kHz).
function _freqHistoryKey() {
  const uid = window.CurrentUser?.id || 'default';
  return `freqHistory_${uid}`;
}
function _loadFreqHistory() {
  try { return JSON.parse(localStorage.getItem(_freqHistoryKey()) || '[]'); }
  catch { return []; }
}
function _saveFreqHistory(list) {
  try { localStorage.setItem(_freqHistoryKey(), JSON.stringify(list)); }
  catch(e) { console.warn('[freq-hist] save blad:', e); }
}
let _lastHistoryFreq = 0;
function addFreqHistory(hz, mode) {
  if (!hz || hz < 100000) return;
  // Pomijaj drobne strojenie (< 5kHz roznica od ostatniego zapisanego)
  if (Math.abs(hz - _lastHistoryFreq) < 5000) return;
  _lastHistoryFreq = hz;
  const hist = _loadFreqHistory();
  // Deduplikacja — jesli te same freq juz jest, usun stara pozycje
  const idx = hist.findIndex(h => Math.abs(h.freq - hz) < 100);
  if (idx >= 0) hist.splice(idx, 1);
  hist.unshift({ freq: hz, mode: mode || S.mode, ts: Date.now() });
  if (hist.length > 20) hist.length = 20;
  _saveFreqHistory(hist);
  renderFreqHistory();
}
function renderFreqHistory() {
  const list = document.getElementById('freq-history-list');
  if (!list) return;
  const hist = _loadFreqHistory();
  if (!hist.length) {
    list.innerHTML = '<div style="padding:6px;color:var(--dim);font-size:9px;text-align:center;">Historia pusta</div>';
    return;
  }
  list.innerHTML = hist.map(h => {
    const mhz = (h.freq / 1e6).toFixed(3);
    const band = getBandName(h.freq);
    const ageS = Math.floor((Date.now() - h.ts) / 1000);
    const age = ageS < 60 ? `${ageS}s` : ageS < 3600 ? `${Math.floor(ageS/60)}m` : `${Math.floor(ageS/3600)}h`;
    return `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:3px 4px;border-bottom:1px solid var(--panel3);cursor:pointer;transition:background 0.1s;"
        onmouseover="this.style.background='rgba(76,219,106,0.05)'"
        onmouseout="this.style.background=''"
        onclick="UI.gotoFreq(${h.freq},'${h.mode}')">
        <span style="color:var(--amber);">${mhz}</span>
        <span style="color:var(--dim);">${band}</span>
        <span style="color:var(--fg);">${h.mode}</span>
        <span style="color:var(--dim);font-size:9px;">${age}</span>
      </div>`;
  }).join('');
}
function toggleFreqHistory() {
  const list = document.getElementById('freq-history-list');
  const tgl  = document.getElementById('freq-history-toggle');
  if (!list) return;
  const isVisible = list.style.display !== 'none';
  list.style.display = isVisible ? 'none' : 'block';
  if (tgl) tgl.textContent = isVisible ? '▶' : '▼';
  if (!isVisible) renderFreqHistory();
}

let _bandMemTimer = null;
function scheduleBandMemorySave() {
  if (_bandMemTimer) clearTimeout(_bandMemTimer);
  _bandMemTimer = setTimeout(() => {
    saveBandMemory();
    addFreqHistory(S.freq, S.mode);
    // Odswiez tooltipy na buttonach (zeby pokazywaly nowa zapamietana freq)
    buildBandGrid();
  }, 2000);
}

function updateTelemetry() {
  updateFreqDisplay();
  updatePTT();
  updateSMeter(S.sMeter);
  updateModeButtons();
  updateVFOBadges();
  updateFreqB();
  applyRadioLockUI();
  scheduleBandMemorySave();
}

function updateVFOBadges() {
  const isA    = !S.vfo || S.vfo === 'VFOA';
  const isSplit = !!S.split;
  const isPTT  = !!S.ptt;

  // VFO A/B — przyciski sa w radiofunctions.js (dynamiczne), podswietlane
  // przez wewnetrzna logike vfoGroup. Badge-vfoa/b sa ukryte — nic do zrobienia.

  // SPLIT badge
  const bs = document.getElementById('badge-split');
  if (bs) {
    bs.style.display    = isSplit ? 'inline' : 'none';
    bs.style.color      = 'var(--amber)';
    bs.style.background = 'rgba(240,180,41,0.12)';
    bs.style.borderColor= 'rgba(240,180,41,0.4)';
  }

  // RX / TX badges
  const brx = document.getElementById('badge-rx');
  const btx = document.getElementById('badge-tx');
  if (brx) {
    brx.style.display    = isPTT ? 'none'   : 'inline';
  }
  if (btx) {
    btx.style.display    = isPTT ? 'inline' : 'none';
    btx.style.color      = 'var(--red)';
    btx.style.background = 'rgba(224,82,82,0.15)';
    btx.style.borderColor= 'rgba(224,82,82,0.4)';
  }

  // Split label na VFO B
  const bspl = document.getElementById('vfo-b-split-label');
  if (bspl) bspl.style.display = isSplit ? 'inline' : 'none';

  // Mode badge
  const mb = document.getElementById('vfo-mode-badge');
  if (mb) mb.textContent = S.mode || 'USB';

  // Band badge
  const band = getBandName(S.freq);
  const bbd = document.getElementById('vfo-band-badge');
  if (bbd) bbd.textContent = band;

  // Kolor VFO A - zmień gdy PTT
  const vfoDigits = document.getElementById('vfo-digits');
  if (vfoDigits) {
    vfoDigits.style.color = isPTT
      ? 'var(--red)'
      : (!isA ? 'var(--amber)' : 'var(--green)');
    vfoDigits.style.textShadow = isPTT
      ? '0 0 30px rgba(224,82,82,0.5)'
      : (!isA ? '0 0 30px rgba(240,180,41,0.4)' : '0 0 30px rgba(76,219,106,0.4)');
  }
}

function updateFreqB() {
  const el = document.getElementById('freq-b-display');
  if (!el) return;
  const hz  = S.freqB || S.freq;
  const s   = String(hz).padStart(9,'0');
  el.textContent = `${s.slice(0,3)}.${s.slice(3,6)}.${s.slice(6)}`;
  // Kolor VFO B - jaśniejszy gdy split (TX na B)
  const isActive = S.vfo === 'VFOB';
  const isSplit  = !!S.split;
  el.style.color = isActive ? 'var(--green)'
    : (isSplit ? 'var(--amber)' : 'var(--dim)');
}

// Wpisz recznie czestotliwosc VFO-B (prompt otwierany przez prawy klik)
function editVfoB() {
  const currentMHz = (S.freqB || S.freq) / 1e6;
  const input = prompt('Częstotliwość VFO-B (MHz):', currentMHz.toFixed(6));
  if (input === null) return;
  // Zaakceptuj format: 14.074000 / 14074000 / 14074 kHz
  let hz;
  const val = input.trim().replace(',', '.');
  if (val.includes('.')) {
    hz = Math.round(parseFloat(val) * 1e6);
  } else {
    // Zgadnij: <1000 = MHz*1000 (np. 14 = 14000000), inaczej Hz
    const n = parseInt(val, 10);
    if (isNaN(n)) return;
    hz = n < 1000 ? n * 1e6 : (n < 100000 ? n * 1000 : n);
  }
  if (!hz || hz < 100000 || hz > 500000000) {
    showToast('Nieprawidlowa czestotliwosc', 'error');
    return;
  }
  S.freqB = hz;
  updateFreqB();
  updatePTT();  // cross-band guard
  WS.send({ type: 'freqB', freqB: hz });
}

// Scroll na VFO-B - zmiana czestotliwosci z krokiem zaleznym od tego czy
// jest wcisniety klawisz Shift (1kHz vs 100Hz).
function wheelVfoB(e) {
  e.preventDefault();
  const step = e.shiftKey ? 100 : 1000;
  const delta = e.deltaY < 0 ? step : -step;
  const cur = S.freqB || S.freq;
  const newHz = Math.max(100000, cur + delta);
  S.freqB = newHz;
  updateFreqB();
  updatePTT();  // cross-band guard
  // Debounce zeby nie zapchac CIV - wysylaj max co 100ms
  if (window._vfoBTimer) clearTimeout(window._vfoBTimer);
  window._vfoBTimer = setTimeout(() => {
    WS.send({ type: 'freqB', freqB: S.freqB });
  }, 100);
}

function getBandName(hz) {
  const bands = {
    '160m':[1800000,2000000],'80m':[3500000,3800000],'60m':[5300000,5410000],
    '40m':[7000000,7200000],'30m':[10100000,10150000],'20m':[14000000,14350000],
    '17m':[18068000,18168000],'15m':[21000000,21450000],'12m':[24890000,24990000],
    '10m':[28000000,29700000],'6m':[50000000,54000000],'4m':[70000000,70500000],
    '2m':[144000000,146000000],'70cm':[430000000,440000000],
  };
  for (const [b,[lo,hi]] of Object.entries(bands)) {
    if (hz >= lo && hz <= hi) return b;
  }
  return hz > 1000000 ? `${(hz/1e6).toFixed(3)}MHz` : `${hz}Hz`;
}

function vfoSelect(vfo) {
  const newVfo = vfo === 'B' ? 'VFOB' : 'VFOA';
  if (S.vfo === newVfo) return;
  S.vfo = newVfo;
  // Wyślij komendę zmiany VFO przez Hamlib
  window.WS?.send({ type:'vfo', vfo: newVfo });
  updateVFOBadges();
}

// ── Tuner ────────────────────────────────────────────────────────────────────
function toggleTuner() {
  const newVal = !S.tuner;
  S.tuner = newVal;
  document.getElementById('tuner-btn')?.classList.toggle('active', newVal);
  WS.send({ type: 'tuner', value: newVal });
}

function startAutotune() {
  // Autotune wymaga PTT — radio samo generuje sygnal TX podczas strojenia
  // Wysylamy komende tuner_autotune, backend (civ.py) wykona sekwencje:
  // 1C 01 01 (tuner ON) + 1C 01 02 (START autotune)
  WS.send({ type: 'tuner_autotune' });
  // Podswietl przycisk tymczasowo zeby oznaczyc ze komenda zostala wyslana
  const btn = document.querySelector('.rf-btn[onclick="UI.startAutotune()"]');
  if (btn) {
    btn.classList.add('active');
    setTimeout(() => btn.classList.remove('active'), 3000);
  }
}

// ── Animacja ──────────────────────────────────────────────────────────────────
let _specFrame = null;
(function _specLoop() {
  drawSpectrum();
  _specFrame = requestAnimationFrame(_specLoop);
})();

// ── Klawiatura ────────────────────────────────────────────────────────────────
// Skroty klawiszowe w zakladce Radio:
//   Spacja      — PTT (hold to transmit)
//   Escape      — PTT OFF (przerwanie TX)
//   ArrowUp/Dn  — stroj +/- (step z dropdown STEP)
//   ArrowLeft/R — jak Up/Down (przydatne przy klawiaturach bez kolumny arrow)
//   PageUp/Dn   — +/- 1 kHz (grube stroje)
//   Home/End    — +/- 10 kHz (bardzo grube stroje)
//   +/- (num)   — zmiana pasma na kolejne wyzej/nizej (jesli S.bands zdefiniowane)
//   F1-F6       — makra CW (obsluga w CW.sendMacroKey)
//   Ctrl+Space  — toggle PTT lock (klik zamiast hold)
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
  // Skroty aktywne tylko w zakladce Radio (na innych stronach nie przeszkadzaj)
  const radioActive = document.getElementById('page-radio')?.classList.contains('active');
  if (!radioActive) return;

  // Sprawdz czy user moze sterowac (readonly mode)
  const canCtrl = _canControlRadio();

  // PTT z modyfikatorem (Ctrl+Space) — toggle
  if (e.ctrlKey && e.code === 'Space' && !e.repeat) {
    e.preventDefault();
    if (canCtrl) setPTT(!S.ptt);
    return;
  }

  if (e.key === 'ArrowUp'    || e.key === 'ArrowRight') { e.preventDefault(); if (canCtrl) tune(1); }
  else if (e.key === 'ArrowDown' || e.key === 'ArrowLeft')  { e.preventDefault(); if (canCtrl) tune(-1); }
  else if (e.key === 'PageUp')    { e.preventDefault(); if (canCtrl) sendFreq(S.freq + 1000); }
  else if (e.key === 'PageDown')  { e.preventDefault(); if (canCtrl) sendFreq(S.freq - 1000); }
  else if (e.key === 'Home')      { e.preventDefault(); if (canCtrl) sendFreq(S.freq + 10000); }
  else if (e.key === 'End')       { e.preventDefault(); if (canCtrl) sendFreq(S.freq - 10000); }
  else if (e.key === ' ') { e.preventDefault(); if (!e.repeat && canCtrl) setPTT(true); }
  else if (e.key === 'Escape')    { if (S.ptt && canCtrl) setPTT(false); }
  // F1-F6 - makra CW (jesli CW.sendMacro istnieje)
  else if (e.key === 'F1') { e.preventDefault(); if (canCtrl) window.CW?.sendMacro?.(1); }
  else if (e.key === 'F2') { e.preventDefault(); if (canCtrl) window.CW?.sendMacro?.(2); }
  else if (e.key === 'F3') { e.preventDefault(); if (canCtrl) window.CW?.sendMacro?.(3); }
  else if (e.key === 'F4') { e.preventDefault(); if (canCtrl) window.CW?.sendMacro?.(4); }
  else if (e.key === 'F5') { e.preventDefault(); if (canCtrl) window.CW?.sendMacro?.(5); }
  else if (e.key === 'F6') { e.preventDefault(); if (canCtrl) window.CW?.sendMacro?.(6); }
});

// Spacja puszczona = PTT OFF (hold-to-transmit)
document.addEventListener('keyup', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === ' ') { e.preventDefault(); setPTT(false); }
});

// Scroll po VFO — UWAGA: poszczegolne cyfry czestotliwosci (.vfo-digit) maja
// WLASNA obsluge scroll per-cyfra (inline onwheel="VFO.wheelDigit(...)" w
// vfo.js, z poprawnym krokiem dla kazdej pozycji). Ten listener (starszy,
// uzywajacy globalnego kroku z #step-select) NIE powinien tez reagowac na
// scroll nad cyframi — dwa rownolegle handlery na tym samym zdarzeniu
// powodowaly konflikt: tune() czytal krok z #step-select, ktorego JUZ NIE MA
// w obecnym HTML (zastapiony przez system przyciskow VFO.updateStep()), wiec
// zawsze cicho uzywal domyslnego kroku 1000Hz zamiast wlasciwego kroku danej
// cyfry — a oba wywolania probowaly jednoczesnie wyslac zmiane czestotliwosci
// do serwera, gubiac/nadpisujac wlasciwa komende. Wykluczamy .vfo-digit z
// tego listenera; VFO.init() ma wlasny listener na #vfo-box ktory poprawnie
// pomija cyfry (patrz vfo.js, ten sam wzorzec e.target.closest('.vfo-digit')).
// Scroll VFO jest obsługiwany w całości przez vfo.js
// Ten listener jest zachowany tylko jako fallback ale NIE wywołuje tune()
// żeby uniknąć konfliktu z handlerem w vfo.js
document.addEventListener('wheel', (e) => {
  if (e.target.closest('#vfo-box')) {
    e.preventDefault();
    // Nie wywołuj tune() — vfo.js obsługuje cały scroll w #vfo-box
  }
}, { passive: false });

// Init canvases po załadowaniu
document.addEventListener('DOMContentLoaded', () => {
  setTimeout(initCanvases, 100);
});

// ── Eksport ───────────────────────────────────────────────────────────────────
window.UI = {
  updateConnectionStatus, updateFreqDisplay, updateModeButtons, updatePTT,
  updateSMeter, updateTelemetry, fullRefresh, updateFreqB, updateVFOBadges,
  setPage, showToast, tune, sendFreq, gotoFreq, vfoSelect,
  editVfoB, wheelVfoB, applyRadioLockUI,
  toggleFreqHistory, renderFreqHistory, addFreqHistory,
  setTheme, loadTheme,
  setMode, setPTT, setLevel,
  vfoSwap, vfoCopy, toggleSplit, toggleTuner, startAutotune,
  saveMemory, deleteMemory, renderMemories,
  buildModeGrid, buildBandGrid, updateBandButtons,
  updateTxMeter, setTxMeter,
  initCanvases,
  getBandName,
};

// Alias globalny
window.fmtFreq = fmtFreq;

})();