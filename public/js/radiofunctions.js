/* radiofunctions.js — Dynamiczne funkcje radia wykryte przez dump_caps (Hamlib)
 *
 * Renderuje:
 *  - PRZYCISKI (VFO A / VFO B, toggle funkcji NB/VOX/COMP/...)
 *    -> panel #rig-features-panel (miedzy PTT i waterfallem)
 *  - SLIDERY (RFPOWER, AF, MICGAIN, SQL, ...)
 *    -> sekcja #rig-dynamic-sliders w lewej kolumnie (tile-left, ponizej PASMO)
 *
 * Dane pochodza z GET /api/rig/features:
 *   user:  {active:[...], actions:[...], sliders:[...]}
 *   admin: {features:[...], dynamic:{actions:[...], sliders:[...]}}
 *
 * Aktualizacja live przez WS broadcast 'rig_features' (po zapisie w panelu admina).
 *
 * UKLAD: wszystkie slidery renderowane jako pojedyncze kafelki w jednej
 * kolumnie, jeden pod drugim (ikona + etykieta + wartosc + range).
 * Brak podzialu core/advanced — wszystkie widoczne od razu.
 */

// Ikony Tabler dla sliderow (po id). Fallback: ti-adjustments-horizontal
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

// Niestandardowe formatowanie wartosci wyswietlanej na sliderze (rf-slider-tile-val)
// — niezalezne od wartosci CI-V faktycznie wysylanej (input.value, 0-100 dla
// zakresow float01). 'display(pct)' -> string do pokazania; 'step' nadpisuje
// domyslny step inputu (np. CWPITCH co 10Hz zamiast co 1).
const SLIDER_DISPLAY = {
  // Kompresja mikrofonu: UI 0-100% -> wyswietlana skala 1.0-10.0 (dziesietne)
  level_comp: {
    display: (pct) => (1 + (pct / 100) * 9).toFixed(1),
  },
  // Redukcja szumow (NR): UI 0-100% -> wyswietlana skala 0-7
  // (wartosc_pct podzielona przez 100/7, zaokraglona do calej liczby)
  level_nr: {
    display: (pct) => String(Math.round(pct / (100 / 7))),
  },
  // Ton CW (Pitch, 300-900Hz): krok co 10Hz zamiast co 1Hz
  level_cwpitch: {
    step: 10,
  },
};

const RadioFunctions = (() => {

  let _actions = [];
  let _sliders = [];
  // Mapa id -> {input, val, isFloat01, min, max} dla zywych aktualizacji
  // wartosci sliderow (WS 'level_value', gdy ktos zmieni nastawe na panelu
  // radia) bez przerysowywania calej listy.
  let _sliderEls = {};
  // Mapa id -> button dla przyciskow toggle funkcji (NB/NR/ANF/.../BKIN) —
  // pozwala serwerowi (np. auto BK-IN przy wejsciu w CW) zaktualizowac stan
  // wizualny przycisku bez przerysowywania calego rzedu.
  let _funcBtnEls = {};

  // ── Przyciski (VFO select + func toggle) ────────────────────────────────
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

    // VFO select — przyciski A/B obok siebie, podswietlenie aktywnego
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
          // Podswietl wybrany
          vfoGroup.querySelectorAll('.rf-btn').forEach(b =>
            b.classList.toggle('active', b === btn));
        };
        vfoGroup.appendChild(btn);
      }
      wrap.appendChild(vfoGroup);
    }

    // Power toggle — wyrozniony przycisk (czerwony akcent).
    // STAN trzymany na btn.dataset.powerOn (NIE lokalna zmienna) - bo musi
    // synchronizowac sie z serwerem: gdy inny user wylaczy radio, my dostajemy
    // power_state i aktualizujemy tu, zeby klik wyslal WLASCIWA wartosc.
    // Wczesniej lokalne 'let state=true' rozjezdzalo sie z rzeczywistoscia -
    // trzeba bylo klikac kilka razy zeby trafic.
    const powerAction = _actions.find(a => a.id === 'power_toggle');
    if (powerAction) {
      const btn = document.createElement('button');
      btn.className = 'rf-btn rf-btn-power';
      btn.dataset.actionId = powerAction.id;
      btn.textContent = '⏻ ' + powerAction.label;
      // Domyslnie zakladamy ON, ale zaraz nadpisze to stan z serwera
      // (handlePowerState wywolane po get_status / power_state broadcast).
      if (btn.dataset.powerOn === undefined) btn.dataset.powerOn = '1';
      btn.onclick = () => {
        // Negujemy AKTUALNY (rzeczywisty) stan, nie lokalny licznik.
        const currentlyOn = btn.dataset.powerOn === '1';
        const newState = !currentlyOn;
        // Optymistycznie zaktualizuj wyglad; serwer potwierdzi przez power_state
        btn.dataset.powerOn = newState ? '1' : '0';
        btn.classList.toggle('active', !newState); // active = OFF (czerwony)
        WS.send({ type: 'rig_action', id: powerAction.id, value: newState });
      };
      wrap.appendChild(btn);
      _funcBtnEls['power_toggle'] = btn;
      // Zastosuj stan jesli juz go znamy (z wczesniejszego get_status)
      if (window.AppState && typeof window.AppState.rigPowerOn === 'boolean') {
        handlePowerState(window.AppState.rigPowerOn);
      }
    }

    // Func toggle — male przyciski z aktywnym stanem (toggle wizualny).
    // Wszystkie funkcje renderowane w jednym wierszu (pasek przewija sie
    // horyzontalnie przy przepelnieniu, patrz .rf-buttons-row).
    const funcActions = _actions.filter(a => a.group === 'func');

    const makeFuncBtn = (a) => {
      const btn = document.createElement('button');
      btn.className = 'rf-btn';
      btn.dataset.actionId = a.id;
      btn.textContent = a.label.split('(')[0].trim(); // krotsza etykieta
      btn.title = a.label;
      btn.onclick = () => {
        const state = !btn.classList.contains('active');
        btn.classList.toggle('active', state);
        WS.send({ type: 'rig_action', id: a.id, value: state });
      };
      _funcBtnEls[a.id] = btn;
      return btn;
    };

    for (const a of funcActions) wrap.appendChild(makeFuncBtn(a));

    // Przyciski wlasnie powstaly — podswietl wg AKTUALNEGO stanu radia
    // (init mogl przyjsc wczesniej; bez tego nowy user widzi wszystko
    // wygaszone mimo ze np. VFO A aktywne / split wlaczony).
    syncStates({});
  }

  // ── Slidery (Set level) ──────────────────────────────────────────────────
  function renderSliders(sliders) {
    _sliders = sliders || [];
    const section = document.getElementById('rig-dynamic-sliders');
    if (!section) return;

    // Usun stare elementy (zachowaj tytul sekcji)
    section.querySelectorAll('.rf-slider-tile, .rf-adv-sliders, .rf-adv-toggle').forEach(el => el.remove());
    _sliderEls = {};

    if (!_sliders.length) {
      section.style.display = 'none';
      return;
    }
    section.style.display = '';

    // KEYSPD (szybkosc CW) -> slider WPM w panelu CW KEYER (#cw-wpm-slider),
    // nie kafelek tutaj. Ustawiamy poczatkowa wartosc (realna nastawa radia)
    // i rejestrujemy w _sliderEls, zeby handleLevelValue (WS 'level_value')
    // mogl go zaktualizowac na zywo (zmiana KEYSPD na panelu radia).
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
      // Hamlib zwraca zakresy float (0.0-1.0) lub int (6-48). Skaluj do 0-100
      // dla UI jesli zakres jest <=1, inaczej uzyj realnych wartosci.
      // s.value (jesli obecne) to faktyczna nastawa odczytana z radia
      // (civ.py _read_all_levels) — uzywamy jej jako pozycji startowej
      // zamiast zawsze 0/min, zeby slider odzwierciedlal stan radia.
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

      // Throttling: nie spamuj WS/CI-V przy szybkim przesuwaniu.
      // Wysylamy nowa wartosc maksymalnie co 100ms, ale zawsze wysylamy
      // ostatnia wartosc po zakonczeniu ruchu (change event).
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
      // Zawsze wyslij ostatnia wartosc po zakonczeniu (release myszki)
      input.onchange = () => {
        if (_pendingTimer) { clearTimeout(_pendingTimer); _pendingTimer = null; }
        doSend();
      };

      // Zapisz referencje + metadane skalowania, zeby handleLevelValue moglo
      // zaktualizowac slider po WS broadcast 'level_value' (zmiana na panelu
      // radia) bez przerysowywania calej listy.
      _sliderEls[s.id] = { input, val, isFloat01, min: s.min, max: s.max, display: disp?.display };

      tile.appendChild(head);
      tile.appendChild(input);
      return tile;
    };

    // level_keyspd (szybkosc CW, WPM) jest sterowane przez slider WPM w
    // panelu CW KEYER (ten sam CI-V 14 0C) — pomijamy duplikat tutaj.
    for (const s of _sliders) {
      if (s.id === 'level_keyspd') continue;
      section.appendChild(makeTile(s));
    }
  }

  // ── Glowny refresh ────────────────────────────────────────────────────────
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
        // Admin view — pokaz wszystkie wykryte (do testow), nawet wylaczone
        renderActions((data.dynamic.actions || []));
        renderSliders((data.dynamic.sliders || []));
      }

      // Statyczne features (badge informacyjne — opcjonalnie uzyte gdzie indziej)
      if (data.active) {
        window._activeStaticFeatures = data.active;
        // Przelicz uprawnienia UI po zaladowaniu features
        window.Auth?.reapplyPermissions?.();
      }
    } catch (e) {
      console.warn('[radiofunctions] refresh blad:', e);
    }
  }

  function handleWsMessage(data) {
    if (data.type !== 'rig_features') return;
    if (data.active !== undefined) {
      renderActions(data.actions || []);
      renderSliders(data.sliders || []);
    }
  }

  // ── Live aktualizacja slidera po WS 'level_value' ────────────────────────
  // (np. uzytkownik zmienil AF/RF/SQL na panelu radia — poller w civ.py
  // wykryl zmiane i rozeslal nowa wartosc; aktualizujemy slider bez
  // przerysowywania calej listy, zeby nie przerywac ewentualnego drag'a).
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

  // Aktualizuje stan wizualny przycisku toggle funkcji (np. po auto BK-IN
  // przy wejsciu w CW, WS 'func_state': {id:'func_bkin', value:true}).
  function handleFuncState(msg) {
    const btn = _funcBtnEls[msg.id];
    if (btn) btn.classList.toggle('active', !!msg.value);
  }

  // Aktualizuje stan wizualny wg legacy typow: preamp, attenuator, tuner.
  // Mapuja sie na dynamiczne id: func_preamp, func_attenuator, func_tuner.
  function handleLegacyFunc(type, msg) {
    // preamp: value 0=OFF, 1=P1, 2=P2 — traktuj > 0 jako aktywny
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
    // Alternatywne id (moga sie roznic zaleznie od dump_caps)
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

  // Aktualizuje przycisk POWER (osobny handler bo ma inna semantyke — "aktywny" znaczy OFF).
  // WAZNE: aktualizuje ZAROWNO wyglad JAK I dataset.powerOn (stan logiczny),
  // zeby kolejne klikniecie wyslalo poprawna wartosc. Wolane z:
  //   - power_state broadcast (inny user przelaczyl radio)
  //   - get_status po zalogowaniu (nowy user poznaje aktualny stan)
  function handlePowerState(isOn) {
    if (window.AppState) window.AppState.rigPowerOn = !!isOn;
    const btn = _funcBtnEls['power_toggle'];
    if (btn) {
      btn.dataset.powerOn = isOn ? '1' : '0';
      btn.classList.toggle('active', !isOn); // active = wylaczony (czerwony)
    }
  }

  // Synchronizacja podswietlenia przyciskow ze STANEM RADIA (nie klikiem).
  // Wolane po init (nowy user widzi prawde od wejscia), po broadcastach
  // vfo/split, i po zbudowaniu przyciskow (renderActions).
  function syncStates(st) {
    st = st || {};
    const vfo = st.vfo || (window.S && window.S.vfo) || 'VFOA';
    // Przyciski VFO A/B (data-action-id = vfo_a / vfo_b)
    document.querySelectorAll('.rf-btn[data-action-id="vfo_a"]').forEach(b =>
      b.classList.toggle('active', vfo === 'VFOA'));
    document.querySelectorAll('.rf-btn[data-action-id="vfo_b"]').forEach(b =>
      b.classList.toggle('active', vfo === 'VFOB'));
    // Split — jesli istnieje przycisk func o id zawierajacym 'split'
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
