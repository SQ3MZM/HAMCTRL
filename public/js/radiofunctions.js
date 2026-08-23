/* radiofunctions.js — Dynamic radio features detected via dump_caps (Hamlib)
 *
 * Renders:
 *  - BUTTONS (VFO A / VFO B, NB/VOX/COMP/... function toggles)
 *    -> panel #rig-features-panel (between PTT and the waterfall)
 *  - SLIDERS (RFPOWER, AF, MICGAIN, SQL, ...)
 *    -> section #rig-dynamic-sliders in the left column (tile-left, below BAND)
 *
 * Data comes from GET /api/rig/features:
 *   user:  {active:[...], actions:[...], sliders:[...]}
 *   admin: {features:[...], dynamic:{actions:[...], sliders:[...]}}
 *
 * Live updates via the WS broadcast 'rig_features' (after a save in the admin panel).
 *
 * LAYOUT: all sliders are rendered as individual tiles in a single column,
 * one below the other (icon + label + value + range). No core/advanced
 * split — everything is visible right away.
 */

// Tabler icons for the sliders (by id). Fallback: ti-adjustments-horizontal
const SLIDER_ICONS = {
  level_af:        'ti-volume',
  level_rf:        'ti-antenna',
  level_sql:       'ti-circle-dot',
  level_nr:        'ti-wave-square',
  level_rfpower:   'ti-bolt',
  level_micgain:   'ti-microphone',
  level_pbt_in:    'ti-arrows-horizontal',
  level_pbt_out:   'ti-arrows-horizontal',
  level_cwpitch:   'ti-music',
  level_keyspd:    'ti-keyboard',
  level_notchf:    'ti-filter',
  level_comp:      'ti-adjustments',
  level_bkindl:    'ti-clock-pause',
  level_nb_level:  'ti-wave-sine',
};
const SLIDER_ICON_FALLBACK = 'ti-adjustments-horizontal';

// Custom formatting of the value shown on the slider (rf-slider-tile-val)
// — independent of the CI-V value actually sent (input.value, 0-100 for
// float01 ranges). 'display(pct)' -> string to show; 'step' overrides the
// input's default step (e.g. CWPITCH every 10Hz instead of every 1Hz).
const SLIDER_DISPLAY = {
  // Mic compression: UI 0-100% -> displayed scale 1.0-10.0 (decimal)
  level_comp: {
    display: (pct) => (1 + (pct / 100) * 9).toFixed(1),
  },
  // Noise reduction (NR): UI 0-100% -> displayed scale 0-7
  // (pct value divided by 100/7, rounded to a whole number)
  level_nr: {
    display: (pct) => String(Math.round(pct / (100 / 7))),
  },
  // CW tone (Pitch, 300-900Hz): step of 10Hz instead of 1Hz
  level_cwpitch: {
    step: 10,
  },
};

const RadioFunctions = (() => {

  let _actions = [];
  let _sliders = [];
  // Map id -> {input, val, isFloat01, min, max} for live slider value
  // updates (WS 'level_value', when someone changes a setting on the
  // radio's own panel) without re-rendering the whole list.
  let _sliderEls = {};
  // Map id -> button for the function-toggle buttons (NB/NR/ANF/.../BKIN)
  // — lets the server (e.g. auto BK-IN on entering CW) update a button's
  // visual state without re-rendering the whole row.
  let _funcBtnEls = {};

  // ── Buttons (VFO select + func toggle) ───────────────────────────────────
  function renderActions(actions) {
    _actions = actions || [];
    const wrap = document.getElementById('rig-features-buttons');
    if (!wrap) return;

    wrap.innerHTML = '';

    if (!_actions.length) {
      wrap.style.display = 'none';
      return;
    }
    wrap.style.display = '';

    // VFO select — A/B buttons side by side, highlight the active one
    const vfoActions = _actions.filter(a => a.group === 'vfo');
    if (vfoActions.length) {
      const vfoGroup = document.createElement('div');
      vfoGroup.className = 'rig-action-group';
      vfoGroup.style.cssText = 'display:flex;gap:4px;margin-right:8px;';
      for (const a of vfoActions) {
        const btn = document.createElement('button');
        btn.className = 'rf-btn';
        btn.dataset.actionId = a.id;
        btn.textContent = a.label;
        btn.onclick = () => {
          WS.send({ type: 'rig_action', id: a.id });
          // Highlight the selected one
          vfoGroup.querySelectorAll('.rf-btn').forEach(b =>
            b.classList.toggle('active', b === btn));
        };
        vfoGroup.appendChild(btn);
      }
      wrap.appendChild(vfoGroup);
    }

    // Power toggle — a highlighted button (red accent).
    // STATE is held on btn.dataset.powerOn (NOT a local variable) - because
    // it must stay in sync with the server: when another user turns off
    // the radio, we get power_state and update it here, so a click sends
    // the CORRECT value. Previously a local 'let state=true' drifted out
    // of sync with reality - it took several clicks to land right.
    const powerAction = _actions.find(a => a.id === 'power_toggle');
    if (powerAction) {
      const btn = document.createElement('button');
      btn.className = 'rf-btn rf-btn-power';
      btn.dataset.actionId = powerAction.id;
      btn.textContent = '⏻ ' + powerAction.label;
      // Assume ON by default, but the server state will override it right
      // away (handlePowerState called after get_status / power_state broadcast).
      if (btn.dataset.powerOn === undefined) btn.dataset.powerOn = '1';
      btn.onclick = () => {
        // Negate the CURRENT (actual) state, not a local counter.
        const currentlyOn = btn.dataset.powerOn === '1';
        const newState = !currentlyOn;
        // Optimistically update the appearance; the server confirms via power_state
        btn.dataset.powerOn = newState ? '1' : '0';
        btn.classList.toggle('active', !newState); // active = OFF (red)
        WS.send({ type: 'rig_action', id: powerAction.id, value: newState });
      };
      wrap.appendChild(btn);
      _funcBtnEls['power_toggle'] = btn;
      // Apply the state if we already know it (from an earlier get_status)
      if (window.AppState && typeof window.AppState.rigPowerOn === 'boolean') {
        handlePowerState(window.AppState.rigPowerOn);
      }
    }

    // Func toggle — small buttons with an active state (visual toggle).
    // All functions are rendered in a single row (the bar scrolls
    // horizontally on overflow, see .rf-buttons-row).
    const funcActions = _actions.filter(a => a.group === 'func');

    const makeFuncBtn = (a) => {
      const btn = document.createElement('button');
      btn.className = 'rf-btn';
      btn.dataset.actionId = a.id;
      btn.textContent = a.label.split('(')[0].trim(); // shorter label
      btn.title = a.label;
      // FIX: MON used to send the radio's own CI-V MONI toggle - reported
      // live as "seems like it doesn't work". Root cause: our own RX duck
      // (setTxAudioDuck, see ws.js) mutes RX audio for the ENTIRE PTT
      // period specifically to hide MONI squeal during FT8 - so even a
      // correctly-toggled MONI could never actually be heard, since the
      // one moment it would matter (while transmitting) is exactly when
      // we silence RX. Repurposed: MON now toggles a LOCAL/server-side
      // monitor of what's actually being sent (SSB mic via TxEq, CW via
      // CwSidetone below) instead of touching the radio at all - doesn't
      // depend on MONI or fight the duck.
      if (a.id === 'func_mon') {
        btn.id = 'radio-mon-btn';
        btn.onclick = async () => {
          // MUST await - toggleMonitor() is async (getUserMedia + AudioContext
          // setup), and isMonitorActive() right after an un-awaited call
          // read the state from BEFORE the toggle finished - see the FIX
          // comment on toggleMonitor() in tx_eq.js.
          await window.TxEq?.toggleMonitor();
          window.CwSidetone?.setEnabled(window.TxEq ? window.TxEq.isMonitorActive() : false);
        };
        _funcBtnEls[a.id] = btn;
        return btn;
      }
      btn.onclick = () => {
        const state = !btn.classList.contains('active');
        btn.classList.toggle('active', state);
        WS.send({ type: 'rig_action', id: a.id, value: state });
      };
      _funcBtnEls[a.id] = btn;
      return btn;
    };

    for (const a of funcActions) wrap.appendChild(makeFuncBtn(a));

    // The buttons were just created — highlight them by the ACTUAL radio
    // state (init may have arrived earlier; without this a new user sees
    // everything dimmed even though, say, VFO A is active / split is on).
    syncStates({});
  }

  // ── Sliders (Set level) ────────────────────────────────────────────────────
  function renderSliders(sliders) {
    _sliders = sliders || [];
    const section = document.getElementById('rig-dynamic-sliders');
    if (!section) return;

    // Remove old elements (keep the section title)
    section.querySelectorAll('.rf-slider-tile, .rf-adv-sliders, .rf-adv-toggle').forEach(el => el.remove());
    _sliderEls = {};

    if (!_sliders.length) {
      section.style.display = 'none';
      return;
    }
    section.style.display = '';

    // KEYSPD (CW speed) -> the WPM slider in the CW KEYER panel
    // (#cw-wpm-slider), not a tile here. Set the initial value (the
    // radio's actual setting) and register it in _sliderEls, so
    // handleLevelValue (WS 'level_value') can update it live (KEYSPD
    // changed on the radio's own panel).
    const keyspd = _sliders.find(s => s.id === 'level_keyspd');
    if (keyspd) {
      const wpmInput = document.getElementById('cw-wpm-slider');
      const wpmVal   = document.getElementById('cw-wpm-val');
      if (wpmInput && wpmVal) {
        const initial = (typeof keyspd.value === 'number') ? keyspd.value : keyspd.min;
        wpmInput.min = keyspd.min; wpmInput.max = keyspd.max; wpmInput.step = 1;
        wpmInput.value = Math.round(initial);
        wpmVal.textContent = wpmInput.value;
        _sliderEls['level_keyspd'] = {
          input: wpmInput, val: wpmVal, isFloat01: false,
          min: keyspd.min, max: keyspd.max,
        };
      }
    }

    const makeTile = (s) => {
      const tile = document.createElement('div');
      tile.className = 'rf-slider-tile';

      const head = document.createElement('div');
      head.className = 'rf-slider-tile-head';

      const left = document.createElement('span');
      left.className = 'rf-slider-tile-left';
      const icon = document.createElement('i');
      icon.className = 'ti ' + (SLIDER_ICONS[s.id] || SLIDER_ICON_FALLBACK);
      icon.setAttribute('aria-hidden', 'true');
      const lbl = document.createElement('span');
      lbl.className = 'rf-slider-tile-lbl';
      lbl.textContent = s.label.length > 14 ? s.label.slice(0, 14) : s.label;
      left.appendChild(icon);
      left.appendChild(lbl);
      head.title = s.label;

      const val = document.createElement('span');
      val.className = 'rf-slider-tile-val';

      head.appendChild(left);
      head.appendChild(val);

      const input = document.createElement('input');
      input.type = 'range';
      input.className = 'rf-range';
      // Hamlib returns float ranges (0.0-1.0) or int ranges (6-48). Scale
      // to 0-100 for the UI if the range is <=1, otherwise use the actual
      // values. s.value (if present) is the actual setting read from the
      // radio (civ.py _read_all_levels) — we use it as the starting
      // position instead of always 0/min, so the slider reflects the radio's state.
      const isFloat01 = s.max <= 1.0 && s.max > 0;
      const initial = (typeof s.value === 'number') ? s.value : s.min;
      const disp = SLIDER_DISPLAY[s.id];
      if (isFloat01) {
        input.min = 0; input.max = 100; input.step = disp?.step ?? 1;
        input.value = Math.round(((initial - s.min) / (s.max - s.min)) * 100);
      } else {
        input.min = s.min; input.max = s.max;
        input.step = disp?.step ?? (s.step > 0 ? s.step : 1);
        input.value = Math.round(initial);
      }
      val.textContent = disp?.display ? disp.display(Number(input.value)) : input.value;

      // Throttling: don't spam WS/CI-V while dragging quickly.
      // Send a new value at most every 100ms, but always send the final
      // value once movement stops (change event).
      let _lastSend = 0;
      let _pendingTimer = null;
      const doSend = () => {
        let sendVal = parseFloat(input.value);
        if (isFloat01) sendVal = sendVal / 100.0;
        WS.send({ type: 'rig_slider', id: s.id, value: sendVal });
        _lastSend = Date.now();
        _pendingTimer = null;
      };
      input.oninput = () => {
        val.textContent = disp?.display ? disp.display(Number(input.value)) : input.value;
        const dt = Date.now() - _lastSend;
        if (dt >= 100) {
          doSend();
        } else if (!_pendingTimer) {
          _pendingTimer = setTimeout(doSend, 100 - dt);
        }
      };
      // Always send the final value on release (mouseup)
      input.onchange = () => {
        if (_pendingTimer) { clearTimeout(_pendingTimer); _pendingTimer = null; }
        doSend();
      };

      // Save the reference + scaling metadata, so handleLevelValue can
      // update the slider after a WS 'level_value' broadcast (changed on
      // the radio's own panel) without re-rendering the whole list.
      _sliderEls[s.id] = { input, val, isFloat01, min: s.min, max: s.max, display: disp?.display };

      tile.appendChild(head);
      tile.appendChild(input);
      return tile;
    };

    // level_keyspd (CW speed, WPM) is controlled by the WPM slider in the
    // CW KEYER panel (the same CI-V 14 0C) — skip the duplicate here.
    for (const s of _sliders) {
      if (s.id === 'level_keyspd') continue;
      section.appendChild(makeTile(s));
    }
  }

  // ── Main refresh ──────────────────────────────────────────────────────────
  async function refresh() {
    try {
      const token = localStorage.getItem('token');
      const res = await fetch('/api/rig/features', {
        headers: token ? { 'Authorization': `Bearer ${token}` } : {}
      });
      const data = await res.json();
      if (!data.ok) return;

      if (data.active) {
        // User view
        renderActions(data.actions || []);
        renderSliders(data.sliders || []);
      } else if (data.dynamic) {
        // Admin view — show all detected ones (for testing), even disabled ones
        renderActions((data.dynamic.actions || []));
        renderSliders((data.dynamic.sliders || []));
      }

      // Static features (informational badge — optionally used elsewhere)
      if (data.active) {
        window._activeStaticFeatures = data.active;
        // Recompute UI permissions after loading the features
        window.Auth?.reapplyPermissions?.();
      }
    } catch (e) {
      console.warn('[radiofunctions] refresh error:', e);
    }
  }

  function handleWsMessage(data) {
    if (data.type !== 'rig_features') return;
    // FIX (found live 2026-08-24): this used to render straight from the
    // broadcast payload (data.active/.actions/.sliders). But the server's
    // POST /api/rig/features handler (_set_rig_features in webapp.py)
    // broadcasts ONE payload to every client — the OPERATOR-shaped
    // effective/filtered list — not the role-aware shape each client's
    // own GET /api/rig/features would return. An admin's own www tab
    // receiving that broadcast silently swapped its "see everything,
    // even disabled" admin slider list for the same reduced list an
    // operator sees, with no error and no visual sign anything changed —
    // it just looked like sliders stopped syncing live. Re-fetching here
    // instead goes through the same role-aware endpoint the initial page
    // load uses, so admin stays admin and operator stays operator no
    // matter who saved a feature toggle.
    refresh();
  }

  // ── Live slider update after WS 'level_value' ────────────────────────────
  // (e.g. the user changed AF/RF/SQL on the radio's own panel — the
  // poller in civ.py detected the change and broadcast the new value; we
  // update the slider without re-rendering the whole list, so as not to
  // interrupt any in-progress drag).
  function handleLevelValue(msg) {
    const el = _sliderEls[msg.id];
    if (!el) return;
    const { input, val, isFloat01, min, max, display: fmt } = el;
    let pct;
    if (isFloat01) {
      pct = Math.round(((msg.value - min) / (max - min)) * 100);
    } else {
      pct = Math.round(msg.value);
    }
    input.value = pct;
    val.textContent = fmt ? fmt(pct) : pct;
  }

  // Updates a function toggle button's visual state (e.g. after auto
  // BK-IN on entering CW, WS 'func_state': {id:'func_bkin', value:true}).
  function handleFuncState(msg) {
    const btn = _funcBtnEls[msg.id];
    if (btn) btn.classList.toggle('active', !!msg.value);
  }

  // Updates the visual state for legacy types: preamp, attenuator, tuner.
  // Maps to the dynamic ids: func_preamp, func_attenuator, func_tuner.
  function handleLegacyFunc(type, msg) {
    // preamp: value 0=OFF, 1=P1, 2=P2 — treat > 0 as active
    const isActive = typeof msg.value === 'boolean' ? msg.value : (msg.value > 0);
    const idMap = {
      preamp: 'func_preamp',
      attenuator: 'func_att',
      tuner: 'func_tuner',
    };
    const btnId = idMap[type];
    if (btnId && _funcBtnEls[btnId]) {
      _funcBtnEls[btnId].classList.toggle('active', isActive);
    }
    // Alternative ids (can vary depending on dump_caps)
    const altMap = {
      preamp: ['func_pamp', 'func_preamp'],
      attenuator: ['func_att', 'func_attenuator', 'func_atten'],
      tuner: ['func_tuner', 'func_atu'],
    };
    for (const altId of (altMap[type] || [])) {
      if (_funcBtnEls[altId]) {
        _funcBtnEls[altId].classList.toggle('active', isActive);
      }
    }
  }

  // Updates the POWER button (a separate handler since it has different
  // semantics — "active" means OFF).
  // IMPORTANT: updates BOTH the appearance AND dataset.powerOn (the
  // logical state), so the next click sends the correct value. Called from:
  //   - the power_state broadcast (another user toggled the radio)
  //   - get_status after login (a new user learns the current state)
  function handlePowerState(isOn) {
    if (window.AppState) window.AppState.rigPowerOn = !!isOn;
    const btn = _funcBtnEls['power_toggle'];
    if (btn) {
      btn.dataset.powerOn = isOn ? '1' : '0';
      btn.classList.toggle('active', !isOn); // active = off (red)
    }
  }

  // Syncs button highlighting to the RADIO'S STATE (not a click). Called
  // after init (a new user sees the truth from the start), after
  // vfo/split broadcasts, and after building the buttons (renderActions).
  function syncStates(st) {
    st = st || {};
    const vfo = st.vfo || (window.S && window.S.vfo) || 'VFOA';
    // VFO A/B buttons (data-action-id = vfo_a / vfo_b)
    document.querySelectorAll('.rf-btn[data-action-id="vfo_a"]').forEach(b =>
      b.classList.toggle('active', vfo === 'VFOA'));
    document.querySelectorAll('.rf-btn[data-action-id="vfo_b"]').forEach(b =>
      b.classList.toggle('active', vfo === 'VFOB'));
    // Split — if a func button exists with an id containing 'split'
    const split = (st.split !== undefined) ? !!st.split
                : !!(window.S && window.S.split);
    for (const id in _funcBtnEls) {
      if (id.toLowerCase().includes('split')) {
        _funcBtnEls[id].classList.toggle('active', split);
      }
    }
    const sb = document.getElementById('split-btn');
    if (sb) sb.classList.toggle('active', split);
  }

  return { refresh, renderActions, renderSliders, handleWsMessage, handleLevelValue,
           handleFuncState, handleLegacyFunc, handlePowerState, syncStates };
})();

document.addEventListener('DOMContentLoaded', () => {
  setTimeout(() => RadioFunctions.refresh(), 600);
});
