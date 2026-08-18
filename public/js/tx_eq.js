/*
 * tx_eq.js — Per-user TX microphone EQ with presets and local monitoring.
 *
 * Features:
 *  - Presets: default/dark/bright/dx/ragchew/flat/custom
 *  - Custom EQ: 5 bands (bass/mud/clarity/punch/air) with sliders
 *  - Local monitoring — runs the microphone through the EQ and plays it
 *    back in headphones, without transmitting via the radio
 *  - Per-user settings saved in localStorage (key: txEq_<user_id>)
 *
 * Integration with _txMic in ws.js:
 *  - _txMic uses TxEq.getCurrentBands() to configure the filters in the peer connection
 *  - Changing the preset/slider updates gain.value live on all filters
 *    (both in the TX peer connection and in the monitor)
 */
window.TxEq = (() => {
  // Default presets - based on Heil/Yaesu recommendations for SSB
  // Values are deliberately strongly differentiated so the effect is audible in the monitor
  const PRESETS = {
    default: { bass: -3, mud: -5, clarity: 6, punch: 8, air: 3 },
    dark:    { bass: -10, mud: -8, clarity: 10, punch: 12, air: 6 }, // for a dark/heavy voice - MAX clarity
    bright:  { bass: 2,  mud: -1, clarity: 0, punch: 2,  air: -3 }, // for a bright voice - DARKER
    dx:      { bass: -10, mud: -8, clarity: 8, punch: 12, air: 5 }, // DX/contest - hard punch
    ragchew: { bass: 0,  mud: -2, clarity: 2, punch: 3,  air: 1 }, // natural, flat
    flat:    { bass: 0,  mud: 0,  clarity: 0, punch: 0,  air: 0 }, // no EQ - for comparison
    custom:  { bass: -3, mud: -5, clarity: 6, punch: 8, air: 3 },
  };

  let currentBands = { ...PRESETS.default };
  let currentPreset = 'default';

  // Filters used by the TX peer connection (set by _txMic)
  let txFilters = null;
  // Filters used by the monitor (local listening)
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
    } catch(e) { console.warn('[txeq] load error:', e); }
  }

  // Load from the backend — overwrites localStorage if the server has newer data
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
        // Cache locally after loading from the server
        localStorage.setItem(storageKey(), JSON.stringify({
          preset: currentPreset, bands: currentBands,
        }));
        _applyToFilters(txFilters);
        _applyToFilters(monitorFilters);
        _refreshUiSliders();
        console.log('[txeq] Loaded settings from server:', currentPreset);
      }
    } catch(e) { console.warn('[txeq] loadFromServer error:', e); }
  }

  let _saveTimeout = null;
  function save() {
    try {
      const payload = { preset: currentPreset, bands: currentBands };
      localStorage.setItem(storageKey(), JSON.stringify(payload));
      // Debounced save to the server (don't spam while dragging a slider)
      if (_saveTimeout) clearTimeout(_saveTimeout);
      _saveTimeout = setTimeout(() => {
        fetch('/api/user/tx_eq', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        }).catch(e => console.warn('[txeq] server save error:', e));
      }, 800);
    } catch(e) { console.warn('[txeq] save error:', e); }
  }

  // ── Update filters (called when settings change) ─────────────────────────
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
    currentPreset = 'custom'; // manual change -> switch to custom
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

  // Builds the filter chain (used by _txMic and the monitor)
  // Returns { input, output, filters: {bass, mud, clarity, punch, air} }
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

  // Called by _txMic when it builds the peer connection
  function registerTxFilters(filters) { txFilters = filters; }
  function unregisterTxFilters() { txFilters = null; }

  // ── Monitor (local listening) ────────────────────────────────────────────
  async function startMonitor() {
    console.log('[txeq] startMonitor called, monitorActive=', monitorActive);
    if (monitorActive) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      window.UI?.showToast(I18n.t('profile_toast_mic_unavailable'), 'error');
      return;
    }
    console.log('[txeq] requesting microphone...');
    try {
      // First show all available microphones
      const devs = await navigator.mediaDevices.enumerateDevices();
      const inputs = devs.filter(d => d.kind === 'audioinput');
      console.log('[txeq] === Available microphones ===');
      inputs.forEach(d => {
        console.log(`[txeq]   ${d.label || '(no name)'} [${d.deviceId.substring(0,16)}...]`);
      });

      const savedMic = localStorage.getItem('ham_monitor_micId');

      // Auto-select: avoid virtual cables (VB-Cable), prefer a Realtek/USB
      // mic over an Intel Smart Sound Array (a built-in, very quiet laptop mic)
      let preferredMicId = savedMic;
      if (!preferredMicId) {
        const isBad = (label) => {
          const l = (label || '').toLowerCase();
          return l.includes('virtual') || l.includes('cable') ||
                 l.includes('line ') || l.includes('smart sound') ||
                 l.includes('array') || l.includes('intel');
        };
        // First look for Realtek/USB (real microphones)
        const isGood = (label) => {
          const l = (label || '').toLowerCase();
          return l.includes('realtek') || l.includes('usb');
        };
        const realMic = inputs.find(d =>
          d.deviceId !== 'default' && d.deviceId !== 'communications' &&
          isGood(d.label) && !isBad(d.label));
        // Fallback: anything non-virtual
        const anyRealMic = inputs.find(d =>
          d.deviceId !== 'default' && d.deviceId !== 'communications' &&
          !isBad(d.label));
        const chosen = realMic || anyRealMic;
        if (chosen) {
          preferredMicId = chosen.deviceId;
          console.log('[txeq] Auto-selected microphone:', chosen.label);
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
      console.log('[txeq] microphone OK, label=', track?.label, 'settings=', track?.getSettings());
    } catch (e) {
      console.error('[txeq] getUserMedia error:', e);
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

    // Create a DEDICATED AudioContext for monitoring (don't mix with RX
    // audio, since that one has a masterGain/compressor chain for RX radio audio)
    let monitorCtx;
    try {
      monitorCtx = new (window.AudioContext || window.webkitAudioContext)({
        sampleRate: 48000,
        latencyHint: 'interactive',
      });
      if (monitorCtx.state === 'suspended') await monitorCtx.resume();
      // Set sinkId to the same device as RX audio (that's where the headphones are)
      if (monitorCtx.setSinkId && ctx.sinkId) {
        try {
          await monitorCtx.setSinkId(ctx.sinkId);
        } catch (e) { console.warn('[txeq] monitor setSinkId:', e.message); }
      }
      console.log('[txeq] monitorCtx created, state=', monitorCtx.state, 'sinkId=', monitorCtx.sinkId);
    } catch(e) {
      console.error('[txeq] error creating monitorCtx:', e);
      monitorCtx = ctx; // fallback
    }
    window._monitorCtx = monitorCtx;

    const src = monitorCtx.createMediaStreamSource(monitorStream);

    // The monitor should show EXACTLY what goes out on TX - the same
    // filter chain as _txMic in ws.js (buildFilterChain + registerTxFilters).
    // This used to have "TEST: skip the EQ chain entirely" (mic straight
    // to gain -> destination) - a leftover from debugging a mic-detection
    // issue, never restored. Effect: the EQ sliders didn't change what you
    // heard in the monitor, even though the label next to it said "the
    // monitor runs the mic through the EQ".
    const chain = buildFilterChain(monitorCtx);
    monitorFilters = chain.filters;

    monitorGain = monitorCtx.createGain();
    monitorGain.gain.value = monitorVol;

    src.connect(chain.input);
    chain.output.connect(monitorGain);
    monitorGain.connect(monitorCtx.destination);

    // Check whether the analyser sees any signal from the microphone
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

    console.log('[txeq] Monitor: mic -> gain(2.0) -> destination, no EQ, sinkId=', monitorCtx.sinkId);

    // Audio output selection - the AudioContext already has the correct
    // sinkId, but we log it for diagnostics
    try {
      const devs = await navigator.mediaDevices.enumerateDevices();
      const outputs = devs.filter(d => d.kind === 'audiooutput');
      console.log('[txeq] === Available audio outputs ===');
      outputs.forEach(d => {
        const isSink = d.deviceId === ctx.sinkId;
        console.log(`[txeq]   ${isSink ? '>>>' : '   '} ${d.label || '(no name)'} [${d.deviceId.substring(0,16)}...]`);
      });
    } catch(e) { console.warn('[txeq] enumerate error:', e); }

    monitorActive = true;
    const btn = document.getElementById('eq-monitor-btn');
    if (btn) {
      btn.removeAttribute('data-i18n');  // see the note at rot-status-badge (rotormini.js)
      btn.textContent = I18n.t('profile_eq_monitor_stop_btn');
      btn.style.background = 'var(--red)';
      btn.style.color = 'white';
    }
    console.log('[txeq] Monitor active');
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
    console.log('[txeq] Monitor stopped');
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

  // Init after the page loads
  document.addEventListener('DOMContentLoaded', () => {
    load();
    setTimeout(_refreshUiSliders, 200);
  });

  // After login — fetch settings from the server (may be newer than the local cache)
  window.addEventListener('app:ready', () => {
    load(); // reload localStorage with the correct uid
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
