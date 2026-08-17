/*
 * tx_eq.js — Per-user TX microphone EQ z presetami i lokalnym odsluchem.
 *
 * Funkcjonalnosc:
 *  - Presety: default/dark/bright/dx/ragchew/flat/custom
 *  - Custom EQ: 5 pasm (bass/mud/clarity/punch/air) z suwakami
 *  - Lokalny odsluch (monitor) — przepuszcza mikrofon przez EQ i odtwarza
 *    w sluchawkach, bez nadawania przez radio
 *  - Zapis ustawien per-user w localStorage (klucz: txEq_<user_id>)
 *
 * Integracja z _txMic w ws.js:
 *  - _txMic uzywa TxEq.getCurrentBands() do skonfigurowania filtrow w peer connection
 *  - Zmiana presetu/suwaka aktualizuje na zywo gain.value wszystkich filtrow
 *    (zarowno w TX peer connection jak i w monitorze)
 */
window.TxEq = (() => {
  // Domyslne presety - oparte na rekomendacjach Heil/Yaesu dla SSB
  // Wartości celowo mocno zroznicowane zeby uslyszec efekt w odsluchu
  const PRESETS = {
    default: { bass: -3, mud: -5, clarity: 6, punch: 8, air: 3 },
    dark:    { bass: -10, mud: -8, clarity: 10, punch: 12, air: 6 }, // dla ciemnego/grubego glosu - MAX klarownosc
    bright:  { bass: 2,  mud: -1, clarity: 0, punch: 2,  air: -3 }, // dla jasnego glosu - CIEMNIEJ
    dx:      { bass: -10, mud: -8, clarity: 8, punch: 12, air: 5 }, // DX/contest - hard punch
    ragchew: { bass: 0,  mud: -2, clarity: 2, punch: 3,  air: 1 }, // naturalny, plaski
    flat:    { bass: 0,  mud: 0,  clarity: 0, punch: 0,  air: 0 }, // bez EQ - do porownania
    custom:  { bass: -3, mud: -5, clarity: 6, punch: 8, air: 3 },
  };

  let currentBands = { ...PRESETS.default };
  let currentPreset = 'default';

  // Filtry uzywane przez TX peer connection (ustawiane przez _txMic)
  let txFilters = null;
  // Filtry uzywane przez monitor (lokalny odsluch)
  let monitorFilters = null;
  let monitorStream = null;
  let monitorActive = false;
  let monitorVol = 0.3;
  let monitorGain = null;

  // ── Persistence ──────────────────────────────────────────────────────────
  function storageKey() {
    const uid = window.CurrentUser?.id || window.AppState?.my_uid || 'default';
    return `txEq_${uid}`;
  }

  function load() {
    try {
      const raw = localStorage.getItem(storageKey());
      if (!raw) return;
      const obj = JSON.parse(raw);
      if (obj.preset && PRESETS[obj.preset]) currentPreset = obj.preset;
      if (obj.bands) currentBands = { ...currentBands, ...obj.bands };
    } catch(e) { console.warn('[txeq] load blad:', e); }
  }

  // Ladowanie z backendu — nadpisuje localStorage jesli serwer ma nowsze dane
  async function loadFromServer() {
    try {
      const r = await fetch('/api/user/tx_eq', { credentials: 'include' });
      if (!r.ok) return;
      const data = await r.json();
      if (data.ok && data.tx_eq) {
        if (data.tx_eq.preset && PRESETS[data.tx_eq.preset]) {
          currentPreset = data.tx_eq.preset;
        }
        if (data.tx_eq.bands) {
          currentBands = { ...currentBands, ...data.tx_eq.bands };
        }
        // Cache lokalnie po zaladowaniu z serwera
        localStorage.setItem(storageKey(), JSON.stringify({
          preset: currentPreset, bands: currentBands,
        }));
        _applyToFilters(txFilters);
        _applyToFilters(monitorFilters);
        _refreshUiSliders();
        console.log('[txeq] Zaladowano ustawienia z serwera:', currentPreset);
      }
    } catch(e) { console.warn('[txeq] loadFromServer blad:', e); }
  }

  let _saveTimeout = null;
  function save() {
    try {
      const payload = { preset: currentPreset, bands: currentBands };
      localStorage.setItem(storageKey(), JSON.stringify(payload));
      // Debounced save do serwera (nie spamuj przy przesuwaniu suwaka)
      if (_saveTimeout) clearTimeout(_saveTimeout);
      _saveTimeout = setTimeout(() => {
        fetch('/api/user/tx_eq', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }).catch(e => console.warn('[txeq] serwer save blad:', e));
      }, 800);
    } catch(e) { console.warn('[txeq] save blad:', e); }
  }

  // ── Aktualizacja filtrow (wywolywane przy zmianie ustawien) ──────────────
  function _applyToFilters(filters) {
    if (!filters) return;
    if (filters.bass)    filters.bass.gain.value    = currentBands.bass;
    if (filters.mud)     filters.mud.gain.value     = currentBands.mud;
    if (filters.clarity) filters.clarity.gain.value = currentBands.clarity;
    if (filters.punch)   filters.punch.gain.value   = currentBands.punch;
    if (filters.air)     filters.air.gain.value     = currentBands.air;
  }

  function _refreshUiSliders() {
    const map = {bass:'eq-bass', mud:'eq-mud', clarity:'eq-clarity', punch:'eq-punch', air:'eq-air'};
    for (const [band, id] of Object.entries(map)) {
      const sl = document.getElementById(id);
      const val = document.getElementById(id + '-val');
      if (sl) sl.value = currentBands[band];
      if (val) val.textContent = currentBands[band];
    }
    const sel = document.getElementById('profile-eq-preset');
    if (sel) sel.value = currentPreset;
    const custom = document.getElementById('profile-eq-custom');
    if (custom) custom.style.display = currentPreset === 'custom' ? 'flex' : 'none';
  }

  // ── Public API ───────────────────────────────────────────────────────────
  function applyPreset(name) {
    if (!PRESETS[name]) return;
    currentPreset = name;
    if (name !== 'custom') {
      currentBands = { ...PRESETS[name] };
    }
    _applyToFilters(txFilters);
    _applyToFilters(monitorFilters);
    _refreshUiSliders();
    save();
  }

  function setBand(band, value) {
    if (!(band in currentBands)) return;
    currentBands[band] = value;
    currentPreset = 'custom'; // recznie -> przelacz na custom
    PRESETS.custom = { ...currentBands };
    _applyToFilters(txFilters);
    _applyToFilters(monitorFilters);
    const val = document.getElementById('eq-' + band + '-val');
    if (val) val.textContent = value;
    const sel = document.getElementById('profile-eq-preset');
    if (sel) sel.value = 'custom';
    const custom = document.getElementById('profile-eq-custom');
    if (custom) custom.style.display = 'flex';
    save();
  }

  // Tworzy lancuch filtrow (uzywany przez _txMic i monitor)
  // Zwraca { input, output, filters: {bass, mud, clarity, punch, air} }
  function buildFilterChain(ctx) {
    const rumble = ctx.createBiquadFilter();
    rumble.type = 'highpass';
    rumble.frequency.value = 150;
    rumble.Q.value = 0.7;

    const bass = ctx.createBiquadFilter();
    bass.type = 'peaking';
    bass.frequency.value = 200;
    bass.Q.value = 0.7;
    bass.gain.value = currentBands.bass;

    const mud = ctx.createBiquadFilter();
    mud.type = 'peaking';
    mud.frequency.value = 700;
    mud.Q.value = 1.2;
    mud.gain.value = currentBands.mud;

    const clarity = ctx.createBiquadFilter();
    clarity.type = 'peaking';
    clarity.frequency.value = 1800;
    clarity.Q.value = 1.0;
    clarity.gain.value = currentBands.clarity;

    const punch = ctx.createBiquadFilter();
    punch.type = 'peaking';
    punch.frequency.value = 2400;
    punch.Q.value = 1.2;
    punch.gain.value = currentBands.punch;

    const air = ctx.createBiquadFilter();
    air.type = 'peaking';
    air.frequency.value = 2700;
    air.Q.value = 1.5;
    air.gain.value = currentBands.air;

    const ssb_lp = ctx.createBiquadFilter();
    ssb_lp.type = 'lowpass';
    ssb_lp.frequency.value = 3000;
    ssb_lp.Q.value = 0.7;

    const limiter = ctx.createDynamicsCompressor();
    limiter.threshold.value = -3;
    limiter.knee.value = 6;
    limiter.ratio.value = 3;
    limiter.attack.value = 0.003;
    limiter.release.value = 0.05;

    rumble.connect(bass);
    bass.connect(mud);
    mud.connect(clarity);
    clarity.connect(punch);
    punch.connect(air);
    air.connect(ssb_lp);
    ssb_lp.connect(limiter);

    return {
      input: rumble,
      output: limiter,
      filters: { bass, mud, clarity, punch, air },
    };
  }

  // Wywolywane przez _txMic gdy buduje peer connection
  function registerTxFilters(filters) { txFilters = filters; }
  function unregisterTxFilters() { txFilters = null; }

  // ── Monitor (lokalny odsluch) ────────────────────────────────────────────
  async function startMonitor() {
    console.log('[txeq] startMonitor called, monitorActive=', monitorActive);
    if (monitorActive) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      window.UI?.showToast(I18n.t('profile_toast_mic_unavailable'), 'error');
      return;
    }
    console.log('[txeq] proszę o mikrofon...');
    try {
      // Najpierw pokaż wszystkie dostępne mikrofony
      const devs = await navigator.mediaDevices.enumerateDevices();
      const inputs = devs.filter(d => d.kind === 'audioinput');
      console.log('[txeq] === Dostepne mikrofony ===');
      inputs.forEach(d => {
        console.log(`[txeq]   ${d.label || '(brak nazwy)'} [${d.deviceId.substring(0,16)}...]`);
      });

      const savedMic = localStorage.getItem('ham_monitor_micId');

      // Auto-wybor: unikaj wirtualnych kabli (VB-Cable), preferuj Realtek/USB mic
      // nad Intel Smart Sound Array (wbudowany bardzo cichy laptop mic)
      let preferredMicId = savedMic;
      if (!preferredMicId) {
        const isBad = (label) => {
          const l = (label || '').toLowerCase();
          return l.includes('virtual') || l.includes('cable') ||
                 l.includes('line ') || l.includes('smart sound') ||
                 l.includes('array') || l.includes('intel');
        };
        // Najpierw szukaj Realtek/USB (prawdziwe mikrofony)
        const isGood = (label) => {
          const l = (label || '').toLowerCase();
          return l.includes('realtek') || l.includes('usb');
        };
        const realMic = inputs.find(d =>
          d.deviceId !== 'default' && d.deviceId !== 'communications' &&
          isGood(d.label) && !isBad(d.label));
        // Fallback: cokolwiek nie-wirtualne
        const anyRealMic = inputs.find(d =>
          d.deviceId !== 'default' && d.deviceId !== 'communications' &&
          !isBad(d.label));
        const chosen = realMic || anyRealMic;
        if (chosen) {
          preferredMicId = chosen.deviceId;
          console.log('[txeq] Auto-wybrany mikrofon:', chosen.label);
        }
      }

      const audioConstraint = {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl:  false,
        sampleRate: 48000,
      };
      if (preferredMicId) {
        audioConstraint.deviceId = { exact: preferredMicId };
      }
      monitorStream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraint });
      const track = monitorStream.getAudioTracks()[0];
      console.log('[txeq] mikrofon OK, label=', track?.label, 'settings=', track?.getSettings());
    } catch (e) {
      console.error('[txeq] getUserMedia blad:', e);
      window.UI?.showToast(I18n.t('profile_toast_mic_no_access') + e.message, 'error');
      return;
    }

    if (!window._audioCtx && typeof window.initAudioContext === 'function') {
      console.log('[txeq] initAudioContext...');
      window.initAudioContext();
    }
    const ctx = window._audioCtx || window.audioCtx;
    console.log('[txeq] AudioContext:', ctx, 'state=', ctx?.state);
    if (!ctx) {
      window.UI?.showToast(I18n.t('profile_toast_audioctx_unavailable'), 'error');
      monitorStream.getTracks().forEach(t => t.stop());
      monitorStream = null;
      return;
    }
    if (ctx.state === 'suspended') {
      console.log('[txeq] AudioContext suspended, resume()...');
      await ctx.resume();
    }

    // Utworz DEDYKOWANY AudioContext dla odsluchu (nie mieszaj z RX audio,
    // bo tamten ma masterGain/compressor chain dla RX radio)
    let monitorCtx;
    try {
      monitorCtx = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: 48000,
        latencyHint: 'interactive',
      });
      if (monitorCtx.state === 'suspended') await monitorCtx.resume();
      // Ustaw sinkId na to samo urzadzenie co RX audio (tam sluchawki)
      if (monitorCtx.setSinkId && ctx.sinkId) {
        try {
          await monitorCtx.setSinkId(ctx.sinkId);
        } catch (e) { console.warn('[txeq] monitor setSinkId:', e.message); }
      }
      console.log('[txeq] monitorCtx utworzony, state=', monitorCtx.state, 'sinkId=', monitorCtx.sinkId);
    } catch(e) {
      console.error('[txeq] blad tworzenia monitorCtx:', e);
      monitorCtx = ctx; // fallback
    }
    window._monitorCtx = monitorCtx;

    const src = monitorCtx.createMediaStreamSource(monitorStream);

    // Odsluch ma pokazywac DOKLADNIE to co leci przy nadawaniu - ten sam
    // lancuch filtrow co _txMic w ws.js (buildFilterChain + registerTxFilters).
    // Wczesniej tu bylo "TEST: pomijamy EQ chain calkowicie" (mikrofon prosto
    // do gain -> destination) - resztka po debugowaniu problemu z wykrywaniem
    // mikrofonu, nigdy nie przywrocona. Efekt: suwaki EQ nie zmienialy tego
    // co slychac w odsluchu, mimo ze opis obok mowil "Odsluch przepuszcza
    // mikrofon przez EQ".
    const chain = buildFilterChain(monitorCtx);
    monitorFilters = chain.filters;

    monitorGain = monitorCtx.createGain();
    monitorGain.gain.value = monitorVol;

    src.connect(chain.input);
    chain.output.connect(monitorGain);
    monitorGain.connect(monitorCtx.destination);

    // Sprawdz czy analyser widzi jakikolwiek sygnal z mikrofonu
    const analyser = monitorCtx.createAnalyser();
    analyser.fftSize = 512;
    src.connect(analyser);
    const buf = new Uint8Array(analyser.frequencyBinCount);
    let checkCount = 0;
    const levelCheck = setInterval(() => {
      if (!monitorActive || checkCount++ > 20) { clearInterval(levelCheck); return; }
      analyser.getByteTimeDomainData(buf);
      let peak = 0;
      for (let i = 0; i < buf.length; i++) {
        const v = Math.abs(buf[i] - 128);
        if (v > peak) peak = v;
      }
      console.log(`[txeq] mic peak: ${peak}/128 (${(peak/128*100).toFixed(0)}%)`);
    }, 500);

    console.log('[txeq] Monitor: mic -> gain(2.0) -> destination, bez EQ, sinkId=', monitorCtx.sinkId);

    // Wybor wyjscia audio - AudioContext juz ma poprawny sinkId, ale
    // logujemy dla diagnostyki
    try {
      const devs = await navigator.mediaDevices.enumerateDevices();
      const outputs = devs.filter(d => d.kind === 'audiooutput');
      console.log('[txeq] === Dostepne wyjscia audio ===');
      outputs.forEach(d => {
        const isSink = d.deviceId === ctx.sinkId;
        console.log(`[txeq]   ${isSink ? '>>>' : '   '} ${d.label || '(brak nazwy)'} [${d.deviceId.substring(0,16)}...]`);
      });
    } catch(e) { console.warn('[txeq] enumeruj blad:', e); }

    monitorActive = true;
    const btn = document.getElementById('eq-monitor-btn');
    if (btn) {
      btn.textContent = I18n.t('profile_eq_monitor_stop_btn');
      btn.style.background = 'var(--red)';
      btn.style.color = 'white';
    }
    console.log('[txeq] Monitor aktywny');
  }

  function stopMonitor() {
    if (!monitorActive) return;
    if (monitorStream) {
      monitorStream.getTracks().forEach(t => t.stop());
      monitorStream = null;
    }
    if (monitorGain) {
      try { monitorGain.disconnect(); } catch(e) {}
      monitorGain = null;
    }
    if (window._monitorCtx && window._monitorCtx !== window._audioCtx) {
      try { window._monitorCtx.close(); } catch(e) {}
      window._monitorCtx = null;
    }
    monitorFilters = null;
    monitorActive = false;
    const btn = document.getElementById('eq-monitor-btn');
    if (btn) {
      btn.textContent = I18n.t('profile_eq_monitor_start_btn');
      btn.style.background = 'var(--panel3)';
      btn.style.color = 'var(--green)';
    }
    console.log('[txeq] Monitor zatrzymany');
  }

  function toggleMonitor() {
    if (monitorActive) stopMonitor();
    else startMonitor();
  }

  function setMonitorVol(val) {
    monitorVol = Math.max(0, Math.min(1, val));
    if (monitorGain) monitorGain.gain.value = monitorVol;
    const el = document.getElementById('eq-monitor-vol-val');
    if (el) el.textContent = monitorVol.toFixed(2);
  }

  function getCurrentBands() { return { ...currentBands }; }

  // Inicjalizacja po zaladowaniu strony
  document.addEventListener('DOMContentLoaded', () => {
    load();
    setTimeout(_refreshUiSliders, 200);
  });

  // Po zalogowaniu — pobierz ustawienia z serwera (moga byc nowsze niz lokalny cache)
  window.addEventListener('app:ready', () => {
    load(); // reload localStorage z prawidlowym uid
    loadFromServer();
  });

  return {
    applyPreset, setBand, buildFilterChain,
    registerTxFilters, unregisterTxFilters,
    toggleMonitor, startMonitor, stopMonitor, setMonitorVol,
    getCurrentBands,
    load, save, loadFromServer,
  };
})();
