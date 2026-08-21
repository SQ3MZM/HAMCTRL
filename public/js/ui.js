/**
 * ui.js — UI rendering, event handling
 */
(function() {
'use strict';

const S = window.AppState;

// ── Formatting ────────────────────────────────────────────────────────────────
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

// ── Connection ────────────────────────────────────────────────────────────────
function updateConnectionStatus(on) {
  const dot   = document.getElementById('conn-dot');
  const label = document.getElementById('conn-label');
  const sim   = document.getElementById('sim-badge');
  if (dot)   dot.className   = 'dot ' + (on ? 'green' : 'red');
  if (label) label.textContent = on ? 'ONLINE' : 'OFFLINE';
  if (sim)   sim.style.display = S.sim ? 'inline' : 'none';
}

// ── Frequency ─────────────────────────────────────────────────────────────────
function updateFreqDisplay() {
  const el = document.getElementById('freq-display');
  if (el) el.textContent = fmtFreq(S.freq);
  const elB = document.getElementById('freq-b-display');
  if (elB) elB.textContent = fmtFreq(S.freqB);
  updateBandButtons();
  // Refresh the digital VFO display (rendered by vfo.js)
  window.VFO?.updateVFODisplay?.();
}

// Color theme — saved in localStorage per user.
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

// Load the saved theme on startup
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

// Grays out all radio controls when the user doesn't hold the TRX. The
// user sees everything (freq, waterfall, S-meter) but can't click or send
// commands. The admin's UI stays fully enabled.
function applyRadioLockUI() {
  const canControl = _canControlRadio();
  const page = document.getElementById('page-radio');
  if (!page) return;
  // The radio_lock block goes EXCLUSIVELY through this class (CSS
  // pointer-events in style.css, .radio-readonly .ptt-btn and
  // .radio-readonly button:not(...)) — this is the only place that
  // decides who can click. updatePTT() below knows NOTHING about
  // radio_lock (see its definition) — it only refreshes the cross-band
  // split guard, so it must be called here in order to run before the UI returns.
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
  updateVFOBadges();   // update the band badge immediately (don't wait for the server)
  updatePTT();         // cross-band guard
  scheduleBandMemorySave();  // save to band history/memory (debounced 2s)
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

// ── Mode ──────────────────────────────────────────────────────────────────────
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
  updateVFOBadges();   // update the mode badge immediately

  // Default filter for the mode — can be '1'/'2'/'3'
  const filterRaw = S.modeFilters?.[mode];
  let filterNum = null;
  if (filterRaw) {
    const n = parseInt(String(filterRaw).replace(/[^0-9]/g, ''));
    if (n >= 1 && n <= 3) filterNum = n;
  }

  // Update the filter select in the UI
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
  // Suggest RST in "LOG QSO" (CW->599, phone->59) — called from here
  // because updateModeButtons() already runs on EVERY mode change (click,
  // telemetry, WS 'mode'), so it's the only place that needs hooking up.
  window.QSOLog?.updateRstDefaults?.(S.mode);
}

// ── Bands ─────────────────────────────────────────────────────────────────────
// S.bands used to ALWAYS be empty on the client (never populated) — hence
// the band buttons never highlighted, and the fallback in buildBandGrid()
// ('20m': {def:...} WITHOUT min/max) made updateBandButtons() compare
// f>=undefined, which is always false. The backend HAS the correct data
// (min/max/def for 14 bands) at /api/config/bands, but nothing used to
// fetch it into S.bands. Called once on startup (app:ready) and on every
// 'config_update' from the backend (when the admin changes the list of
// enabled bands in settings).
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

    // Modes and filters
    S.modes       = dm.enabledModes || dm.allModes || ['USB','LSB','AM','FM','CW'];
    S.modeFilters = dm.modeFilters  || {};

    buildBandGrid();
    buildModeGrid();
    updateBandButtons();
  } catch (e) {
    console.warn('[ui] loadBandsConfig error:', e);
  }
}

// Per-user localStorage frequency memory per band. The key is the band
// name (e.g. '20m'), the value is an object { freq, mode, ts }.
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
  catch(e) { console.warn('[band-mem] save error:', e); }
}
// Save the current freq+mode to the band memory (called after every freq change)
function saveBandMemory() {
  const band = getBandName(S.freq);
  if (!band || band.includes('MHz') || band.includes('Hz')) return; // don't save when out of band
  const mem = _loadBandMemory();
  mem[band] = { freq: S.freq, mode: S.mode, ts: Date.now() };
  _saveBandMemory(mem);
}

function buildBandGrid() {
  const bands = Object.keys(S.bands).length ? S.bands : { '20m': { def: 14200000 } };
  const mem = _loadBandMemory();
  const bandHtml = Object.entries(bands)
    .map(([b, r]) => {
      // Use the remembered freq if present, otherwise the default from the config
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
  updateBandButtons();  // highlight the correct button right away based on current S.freq
}

function updateBandButtons() {
  // Highlight the band by the ACTIVE VFO — freqB when VFO B is active,
  // freq when VFO A is active (default). It used to always use S.freq,
  // which meant that after switching to VFO B the band buttons showed
  // VFO A's band instead of VFO B's current band.
  const f = (S.vfo === 'VFOB') ? (S.freqB || S.freq) : S.freq;
  document.querySelectorAll('.band-btn').forEach(btn => {
    const band = S.bands[btn.textContent];
    btn.classList.toggle('active', !!band && f >= band.min && f <= band.max);
  });
}

// Fetch the bands once on app startup (auth.js emits 'app:ready' after
// login — the same pattern settings.js uses to fetch rig models).
window.addEventListener('app:ready', () => { loadBandsConfig(); loadTheme(); });


// ── PTT ───────────────────────────────────────────────────────────────────────
let _pttOwnsMic = false;
function setPTT(on) {
  S.ptt = !!on;
  updatePTT();
  WS.send({ type: 'ptt', ptt: on });

  // TX mic tied directly to PTT (SSB/AM/FM only - CW is keyed via CI-V
  // text in civ.py, never touches the microphone at all). Used to
  // auto-start once at login instead - live test showed that keeps the
  // mic (getUserMedia) open for the WHOLE session, and Windows'
  // "Communications" audio ducking (Control Panel > Sound) then mutes/
  // reduces ALL other system sound - including our own RX - for as long
  // as ANY app holds the mic open, not just while actually transmitting.
  // Opening it only for the PTT window keeps that ducking limited to
  // exactly when RX is already intentionally muted anyway (setTxAudioDuck,
  // above in updatePTT) - no NEW loss of RX audio versus today's normal PTT.
  if (S.mode !== 'CW') {
    if (on) {
      // Only start (and remember we own it) if it wasn't already running -
      // an operator may have manually enabled the "Nadawanie TX -
      // mikrofon" button in RADIO to keep it on continuously; in that
      // case leave it alone entirely, don't stop it out from under them
      // on the next PTT release.
      if (window.WS?.isTxActive?.()) {
        _pttOwnsMic = false;
      } else {
        _pttOwnsMic = true;
        window.WS?.startTX?.().then(ok => { if (ok) _syncTxMicBtn(true); });
      }
    } else if (_pttOwnsMic) {
      _pttOwnsMic = false;
      window.WS?.stopTX?.();
      _syncTxMicBtn(false);
    }
  }
}

// Keeps the manual "Nadawanie TX - mikrofon" button (RADIO tab) in sync
// when PTT starts/stops it automatically, so its label/color don't go
// stale relative to what's actually running.
function _syncTxMicBtn(active) {
  const btn = document.getElementById('tx-mic-btn');
  if (!btn) return;
  btn.removeAttribute('data-i18n');
  btn.textContent = I18n.t(active ? 'tx_mic_stop' : 'tx_mic_start');
  btn.style.color = active ? 'var(--red)' : 'var(--dim)';
  btn.style.borderColor = active ? 'var(--red)' : 'rgba(217,119,106,0.3)';
}

function updatePTT() {
  const btn = document.getElementById('ptt-btn');
  const dot = document.getElementById('ptt-dot');
  const lbl = document.getElementById('ptt-label');
  if (btn) btn.classList.toggle('active', S.ptt);
  if (dot) dot.className = 'dot ' + (S.ptt ? 'red' : 'green');
  if (lbl) lbl.textContent = S.ptt ? 'TX' : 'RX';

  // Mute RX during EVERY transmission (phone and digital). With MONI on,
  // the radio puts a monitor of its own TX on the USB-out -> on phone you
  // hear your own ECHO, on FT8 squealing tones. During TX there's nothing
  // on RX anyway, so the duck doesn't drop anything. Idempotent with the FT8 duck (ft8_tx_status).
  window.setTxAudioDuck?.(!!S.ptt);

  // Cross-band split guard: disable PTT when VFO-A and VFO-B are on
  // different bands (protects the radio/antenna from transmitting on the wrong band)
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

// Checks whether split is active and VFO-A/B are on different bands.
// Called from updatePTT and after every freq/freqB/split change.
function isCrossBandSplit() {
  if (!S.split) return false;
  const bandA = getBandName(S.freq);
  const bandB = getBandName(S.freqB || S.freq);
  return bandA !== bandB;
}

// ── S-meter ───────────────────────────────────────────────────────────────────
function updateSMeter(val) {
  S.sMeter = val;
  // The bargraph maps the REAL IC-7300 S-meter scale, matched to the tick
  // marks in the HTML (8 labels: 1,3,5,7,9,+20,+40,+60 spread evenly via
  // space-between, so "9" sits at 4/7 = ~57% of the width):
  //   S0..S9      -> 0..57% (S9 under the "9" label)
  //   S9..S9+60dB -> 57..100% (+20@71%, +40@86%, +60@100%)
  // It used to hit 100% already at S9 and never showed S9+ at all.
  // val: 0..9 = S0..S9, 9..15 = S9+0..+60dB (each unit of val>9 is +10dB).
  let pct;
  if (val <= 9) {
    pct = (val / 9) * 57;              // S0..S9 -> 0..57% (under the "9" label)
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

// ── TRX meter (ALC/PWR/SWR/VOLT) ─────────────────────────────────────────────
// Stores the last known value of EACH of the 4 meters (the backend sends
// them all cyclically, regardless of which one a given user is currently
// watching) — this way switching the select shows the data immediately,
// without waiting for the next CI-V polling cycle (up to 2s with PTT off, see civ.py n%8==0).
const _txMeterValues = { ALC: null, PWR: null, SWR: null, VOLT: null };
let _txMeterSelected = 'ALC';

const _TXMETER_UNITS = { ALC: '%', PWR: '%', SWR: '', VOLT: 'V' };

// Tick marks below the bar — matched to the REAL reading range from the
// radio (civ.py), not a plain 0-100 scale. Each point: {at: position in %
// of the bar's width (0-100), label: text}. For ALC/PWR, pct is linear
// relative to the value, so the ticks are linear too. For SWR, pct =
// min(1.0, raw/120) while value = 1.0 + (raw/241)*49 — these two scales
// are NOT linearly related, so the 1.5/2.0/3.0 positions have to be
// converted back to pct (see the civ.py comment: 0=1.0, 48=1.5, 80=2.0,
// 120=3.0, where pct=raw/120 — e.g. 1.5 is at pct=48/120=40%).
const _TXMETER_SCALES = {
  ALC:  [{at:0,label:'0'}, {at:25,label:'25'}, {at:50,label:'50'}, {at:75,label:'75'}, {at:100,label:'100%'}],
  PWR:  [{at:0,label:'0'}, {at:25,label:'25'}, {at:50,label:'50'}, {at:75,label:'75'}, {at:100,label:'100%'}],
  SWR:  [{at:0,label:'1.0'}, {at:40,label:'1.5'}, {at:66.7,label:'2.0'}, {at:100,label:'3.0'}],
  VOLT: [],  // no ticks — VOLT is a stable number, not a moving scale (see below)
};

function updateTxMeter(msg) {
  if (!msg.meter || !(msg.meter in _txMeterValues)) return;
  _txMeterValues[msg.meter] = msg;
  if (msg.meter === _txMeterSelected) _renderTxMeter();
}

function setTxMeter(meter) {
  if (!(meter in _txMeterValues)) return;
  _txMeterSelected = meter;
  // The only select driving the bargraph now lives in the left column
  // (TRX FUNCTIONS section below BAND) — we sync it in case setTxMeter()
  // is called from somewhere other than the dropdown itself.
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
    // VOLT: the supply voltage changes slowly and slightly (CI-V read
    // noise of around ±0.1-0.2V) — without this the bar would "jitter" on
    // every polling cycle even though nothing is really changing. We
    // quantize to 5% steps, so the bar only moves on an actual, visible
    // voltage change, not measurement noise.
    if (m === 'VOLT' && data) pct = Math.round(pct / 5) * 5;
    fill.style.width = pct + '%';
  }
  if (scale) {
    const ticks = _TXMETER_SCALES[m] || [];
    scale.innerHTML = ticks.map(t => `<span style="left:${t.at}%">${t.label}</span>`).join('');
  }
  if (value) {
    // ALC/PWR peak-hold (civ.py resets it on every PTT-on): live SSB
    // voice makes the instantaneous reading jump around a lot (loud
    // syllable vs. a gap between words, sampled a few tens of ms apart) -
    // the PEAK seen across the whole transmission, like a real meter's
    // needle memory, is what actually tells you whether drive is set
    // right. Shown only for meters that carry a peak (ALC/PWR).
    const unit = _TXMETER_UNITS[m];
    const peakTxt = (data && typeof data.peak === 'number') ? ` (szczyt ${data.peak}${unit})` : '';
    value.textContent = data ? `${data.value}${unit}${peakTxt}` : '--';
  }
}

// ── Sliders ───────────────────────────────────────────────────────────────────
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

// ── Custom modal instead of prompt() ──────────────────────────────────────────
// prompt()/confirm()/alert() are SYNCHRONOUS - they block the entire main
// JS thread until the user dismisses them, which live froze audio
// streaming (WebAudio/WebRTC) until the dialog was closed (the same root
// cause as the rotator fix - see WSJTX.rotorGoManual in wsjtx.js).
// #text-prompt-modal in index.html is shared by every place that used to
// use prompt() with a single text field.
let _textPromptResolve = null;

function textPrompt(title, defaultValue) {
  return new Promise((resolve) => {
    const modal   = document.getElementById('text-prompt-modal');
    const input   = document.getElementById('text-prompt-input');
    const titleEl = document.getElementById('text-prompt-title');
    if (!modal || !input) { resolve(null); return; }
    if (titleEl) titleEl.textContent = title || 'WPISZ WARTOŚĆ';
    input.value = defaultValue || '';
    _textPromptResolve = resolve;
    modal.style.display = 'flex';
    input.focus();
    input.select();
  });
}

function _textPromptSubmit() {
  const input = document.getElementById('text-prompt-input');
  const val = input ? input.value : '';
  _textPromptClose();
  _textPromptResolve?.(val);
  _textPromptResolve = null;
}

function _textPromptCancel() {
  _textPromptClose();
  _textPromptResolve?.(null);
  _textPromptResolve = null;
}

function _textPromptClose() {
  const modal = document.getElementById('text-prompt-modal');
  if (modal) modal.style.display = 'none';
}

let _confirmModalResolve = null;

// A confirm() replacement — non-blocking, but still requires an explicit
// OK/CANCEL click before the caller gets an answer (unlike alerts turned
// into showToast() below, where there's no decision to make).
// danger=true colors OK red for destructive actions.
function confirmModal(message, { title = I18n.t('common_confirm_title'), okLabel = 'OK', danger = false } = {}) {
  return new Promise((resolve) => {
    const modal   = document.getElementById('confirm-modal');
    const msgEl   = document.getElementById('confirm-modal-msg');
    const titleEl = document.getElementById('confirm-modal-title');
    const okBtn   = document.getElementById('confirm-modal-ok');
    if (!modal || !msgEl) { resolve(false); return; }
    if (titleEl) { titleEl.removeAttribute('data-i18n'); titleEl.textContent = title; }
    msgEl.textContent = message;
    if (okBtn) {
      okBtn.textContent = okLabel;
      okBtn.style.background = danger ? 'var(--red)' : '';
      okBtn.style.borderColor = danger ? 'var(--red)' : '';
    }
    _confirmModalResolve = resolve;
    modal.style.display = 'flex';
    okBtn?.focus();
  });
}

function _confirmModalSubmit() {
  _confirmModalClose();
  _confirmModalResolve?.(true);
  _confirmModalResolve = null;
}

function _confirmModalCancel() {
  _confirmModalClose();
  _confirmModalResolve?.(false);
  _confirmModalResolve = null;
}

function _confirmModalClose() {
  const modal = document.getElementById('confirm-modal');
  if (modal) modal.style.display = 'none';
}

// ── Memories ──────────────────────────────────────────────────────────────────
async function saveMemory() {
  const name = (await textPrompt('NAZWA CZĘSTOTLIWOŚCI (opcjonalnie)', '')) ?? '';
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

// ── Pages ─────────────────────────────────────────────────────────────────────
function setPage(name) {
  // Highlight the active tab
  document.querySelectorAll('.tab-btn').forEach(b => {
    const onclick = b.getAttribute('onclick') || '';
    b.classList.toggle('active',
      onclick.includes("'" + name + "'") || onclick.includes('"' + name + '"'));
  });

  // Switch the page (inline pages)
  document.querySelectorAll('.page-content').forEach(p => p.classList.remove('active'));
  const pg = document.getElementById('page-' + name);
  if (pg) pg.classList.add('active');
  // Block body scrolling on the FT8 page
  document.body.classList.toggle('page-wsjtx-active', name === 'wsjtx');

  // Reset scroll to the top - every tab starts from the beginning.
  // Without this, scrolling the Radio page causes "shifted" views in
  // other tabs (window.scrollY is shared across all page-content elements).
  // We use 'instant' so the change isn't animated (annoying when clicking through tabs).
  try {
    window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
  } catch(e) {
    // Fallback for older browsers that don't support the options object
    window.scrollTo(0, 0);
  }

  // WebSocket channel subscription per tab. The server only sends
  // messages to clients subscribed to a given channel - this way a
  // client on Log QSO doesn't get scope_frame or ft8_waterfall (~50KB/s
  // of unnecessary data + JSON-parsing overhead in JS).
  //
  // 'control' always stays subscribed (freq, mode, ptt, radio_lock, chat, presence).
  // Extra channels depending on the tab's needs:
  //   Radio    -> scope     (CI-V waterfall)
  //   WSJT-X   -> scope+ft8 (waterfall + decodes + auto QSO + tune status)
  //   DXCluster -> dxcluster
  //   other    -> control only
  const channelsForPage = {
    radio:     ['control', 'scope'],
    wsjtx:     ['control', 'scope', 'ft8'],  // scope too, for the VFO badge/freq
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

  // Per-page actions
  if (name === 'log')      { window.QSOLog?.load?.(); window.QSOLog?.loadAdminUsers?.(); }
  if (name === 'internet') { window.Tunnel?.load?.(); window.Tunnel?.checkCF?.(); window.Tunnel?.startAutoRefresh?.(); }
  else { window.Tunnel?.stopAutoRefresh?.(); }
  if (name === 'wsjtx')    {
    window.FT8Timer?.init?.();
    // setTimeout lets the browser render page-content.active before
    // init() checks the canvas dimensions (display:none = width:0)
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
    window.HamlibUI?.load?.();
    window.Callbook?.load?.();
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


// ── Full refresh ──────────────────────────────────────────────────────────────
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
  // Callsign in the header. Fallback to window.CurrentUser?.callsign
  // (same pattern already used for my_gridsquare in qsolog.js) - reported
  // live stuck at "--" while CurrentUser was already populated, so relying
  // on S.callsign (AppState.callsign, set separately/async in auth.js)
  // alone isn't reliable enough for this to always show correctly.
  const cs = document.getElementById('callsign-display');
  if (cs) cs.textContent = S.callsign || window.CurrentUser?.callsign || '--';
  // SIM badge
  const sim = document.getElementById('sim-badge');
  if (sim) sim.style.display = S.sim ? 'inline' : 'none';
  // Rig names in the select
  const rigSel = document.getElementById('active-rig-select');
  if (rigSel) {
    if (S.rigs.length > 1) {
      rigSel.innerHTML = S.rigs.map(r => `<option value="${r.id}">${r.name}</option>`).join('');
      rigSel.style.display = '';
    } else {
      // Only 1 radio — hide the selector, no point choosing
      rigSel.style.display = 'none';
    }
  }
}

// Freq history — automatic, the last 20 values the user visited (skips
// minor tuning, only bigger jumps >5kHz).
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
  catch(e) { console.warn('[freq-hist] save error:', e); }
}
let _lastHistoryFreq = 0;
function addFreqHistory(hz, mode) {
  if (!hz || hz < 100000) return;
  // Skip minor tuning (< 5kHz difference from the last saved value)
  if (Math.abs(hz - _lastHistoryFreq) < 5000) return;
  _lastHistoryFreq = hz;
  const hist = _loadFreqHistory();
  // Deduplication — if the same freq is already there, remove the old entry
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
    // Refresh the tooltips on the buttons (to show the newly remembered freq)
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

  // VFO A/B — the buttons live in radiofunctions.js (dynamic), highlighted
  // by vfoGroup's internal logic. badge-vfoa/b are hidden — nothing to do here.

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

  // Split label on VFO B
  const bspl = document.getElementById('vfo-b-split-label');
  if (bspl) bspl.style.display = isSplit ? 'inline' : 'none';

  // Mode badge
  const mb = document.getElementById('vfo-mode-badge');
  if (mb) mb.textContent = S.mode || 'USB';

  // Band badge
  const band = getBandName(S.freq);
  const bbd = document.getElementById('vfo-band-badge');
  if (bbd) bbd.textContent = band;

  // VFO A color - changes on PTT
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
  // VFO B color - brighter when split (TX on B)
  const isActive = S.vfo === 'VFOB';
  const isSplit  = !!S.split;
  el.style.color = isActive ? 'var(--green)'
    : (isSplit ? 'var(--amber)' : 'var(--dim)');
}

// Manually type in the VFO-B frequency (modal opened by right-click)
async function editVfoB() {
  const currentMHz = (S.freqB || S.freq) / 1e6;
  const input = await textPrompt('CZĘSTOTLIWOŚĆ VFO-B (MHz)', currentMHz.toFixed(6));
  if (input === null) return;
  // Accept the format: 14.074000 / 14074000 / 14074 kHz
  let hz;
  const val = input.trim().replace(',', '.');
  if (val.includes('.')) {
    hz = Math.round(parseFloat(val) * 1e6);
  } else {
    // Guess: <1000 = MHz*1000 (e.g. 14 = 14000000), otherwise Hz
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

// Scroll on VFO-B - changes the frequency with a step depending on
// whether the Shift key is held (1kHz vs 100Hz).
function wheelVfoB(e) {
  e.preventDefault();
  const step = e.shiftKey ? 100 : 1000;
  const delta = e.deltaY < 0 ? step : -step;
  const cur = S.freqB || S.freq;
  const newHz = Math.max(100000, cur + delta);
  S.freqB = newHz;
  updateFreqB();
  updatePTT();  // cross-band guard
  // Debounce so as not to flood CI-V - send at most every 100ms
  if (window._vfoBTimer) clearTimeout(window._vfoBTimer);
  window._vfoBTimer = setTimeout(() => {
    WS.send({ type: 'freqB', freqB: S.freqB });
  }, 100);
}

function getBandName(hz) {
  // 160m/60m/6m: the same (narrower, real PL/EU allocation) bounds as
  // webapp.py::_BAND_RANGES and dxcluster.py::_BAND_RANGES - this used to
  // be a separate, drifted copy (the same bug, a different file).
  const bands = {
    '160m':[1810000,2000000],'80m':[3500000,3800000],'60m':[5351500,5366500],
    '40m':[7000000,7200000],'30m':[10100000,10150000],'20m':[14000000,14350000],
    '17m':[18068000,18168000],'15m':[21000000,21450000],'12m':[24890000,24990000],
    '10m':[28000000,29700000],'6m':[50000000,52000000],'4m':[70000000,70500000],
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
  // Send the VFO-change command via Hamlib
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
  // Autotune requires PTT — the radio generates its own TX signal while tuning
  // We send the tuner_autotune command, the backend (civ.py) runs the sequence:
  // 1C 01 01 (tuner ON) + 1C 01 02 (START autotune)
  WS.send({ type: 'tuner_autotune' });
  // Temporarily highlight the button to indicate the command was sent
  const btn = document.querySelector('.rf-btn[onclick="UI.startAutotune()"]');
  if (btn) {
    btn.classList.add('active');
    setTimeout(() => btn.classList.remove('active'), 3000);
  }
}

// ── Keyboard ──────────────────────────────────────────────────────────────────
// Keyboard shortcuts in the Radio tab:
//   Space       — PTT (hold to transmit)
//   Escape      — PTT OFF (abort TX)
//   ArrowUp/Dn  — tune +/- (step from the STEP dropdown)
//   ArrowLeft/R — same as Up/Down (handy on keyboards without an arrow cluster)
//   PageUp/Dn   — +/- 1 kHz (coarse tuning)
//   Home/End    — +/- 10 kHz (very coarse tuning)
//   +/- (num)   — switch to the next band up/down (if S.bands is defined)
//   F1-F6       — CW macros (handled in CW.sendMacroKey)
//   Ctrl+Space  — toggle PTT lock (click instead of hold)
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
  // Shortcuts are only active in the Radio tab (don't interfere on other pages)
  const radioActive = document.getElementById('page-radio')?.classList.contains('active');
  if (!radioActive) return;

  // Check whether the user can control it (readonly mode)
  const canCtrl = _canControlRadio();

  // PTT with a modifier (Ctrl+Space) — toggle
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
  // F1-F6 - CW macros (if CW.sendMacro exists)
  else if (e.key === 'F1') { e.preventDefault(); if (canCtrl) window.CW?.sendMacro?.(1); }
  else if (e.key === 'F2') { e.preventDefault(); if (canCtrl) window.CW?.sendMacro?.(2); }
  else if (e.key === 'F3') { e.preventDefault(); if (canCtrl) window.CW?.sendMacro?.(3); }
  else if (e.key === 'F4') { e.preventDefault(); if (canCtrl) window.CW?.sendMacro?.(4); }
  else if (e.key === 'F5') { e.preventDefault(); if (canCtrl) window.CW?.sendMacro?.(5); }
  else if (e.key === 'F6') { e.preventDefault(); if (canCtrl) window.CW?.sendMacro?.(6); }
});

// Space released = PTT OFF (hold-to-transmit)
document.addEventListener('keyup', (e) => {
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === ' ') { e.preventDefault(); setPTT(false); }
});

// Scroll over the VFO — NOTE: individual frequency digits (.vfo-digit)
// have THEIR OWN per-digit scroll handling (inline
// onwheel="VFO.wheelDigit(...)" in vfo.js, with the correct step for each
// position). This listener (older, using the global step from
// #step-select) should NOT also react to scrolling over the digits — two
// parallel handlers on the same event caused a conflict: tune() read the
// step from #step-select, which NO LONGER EXISTS in the current HTML
// (replaced by the VFO.updateStep() button system), so it always silently
// fell back to the default 1000Hz step instead of the digit's actual
// step — and both calls tried to send a frequency change to the server at
// the same time, losing/overwriting the correct command. We exclude
// .vfo-digit from this listener; VFO.init() has its own listener on
// #vfo-box that correctly skips the digits (see vfo.js, the same
// e.target.closest('.vfo-digit') pattern).
// VFO scrolling is handled entirely by vfo.js.
// This listener is kept only as a fallback but does NOT call tune(), to
// avoid conflicting with the handler in vfo.js.
document.addEventListener('wheel', (e) => {
  if (e.target.closest('#vfo-box')) {
    e.preventDefault();
    // Don't call tune() — vfo.js handles all scrolling in #vfo-box
  }
}, { passive: false });

// ── Export ────────────────────────────────────────────────────────────────────
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
  textPrompt, _textPromptSubmit, _textPromptCancel,
  confirmModal, _confirmModalSubmit, _confirmModalCancel,
  buildModeGrid, buildBandGrid, updateBandButtons,
  updateTxMeter, setTxMeter,
  getBandName,
};

// Global alias
window.fmtFreq = fmtFreq;

})();