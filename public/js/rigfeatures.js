/* rigfeatures.js — Panel funkcji radia (pod PTT, nad waterfallem)
 *
 * Dla zwyklego usera: pokazuje przyciski tylko dla funkcji ktore admin
 * wlaczyl ORAZ radio technicznie wspiera (effective_features z backendu).
 *
 * Aktualizuje sie:
 *  - po polaczeniu WS (init)
 *  - po zmianie radia (/api/rig/connect)
 *  - po zapisaniu whitelisty przez admina (broadcast 'rig_features')
 *
 * Kazdy przycisk:
 *  - "freq_set"/"mode_set"/"split"/"rit" — nie generuje wlasnego przycisku
 *    (te funkcje sa juz osobnymi kontrolkami w UI VFO) — panel pokazuje je
 *    tylko jako informacje/toggle widocznosci powiazanych kontrolek
 *  - "ptt" — juz ma wlasny przycisk PTT (panel nie duplikuje)
 *  - "tx_power", "memory", "dstar", "scope", "smeter" — generuja
 *    dedykowany przycisk/link w panelu
 *
 * Mapowanie feature_id -> akcja przycisku jest w FEATURE_ACTIONS nizej.
 * Funkcje bez wpisu w FEATURE_ACTIONS sa pokazywane jako pasywny znacznik
 * (badge) informujacy ze funkcja jest dostepna, ale steruje sie nia
 * przez istniejace kontrolki gdzie indziej w UI.
 */

const RigFeatures = (() => {

  // Akcje dla przyciskow w panelu — feature_id -> {onClick, toggles}
  // 'toggles' = lista selektorow CSS elementow ktore pokazac/skryc
  //             w zaleznosci od effective (np. slider mocy TX)
  const FEATURE_ACTIONS = {
    tx_power: {
      toggles: ['#tx-power-row', '.tx-power-control'],
    },
    rit: {
      toggles: ['#rit-row', '.rit-control'],
    },
    memory: {
      onClick: () => UI.showTab && UI.showTab('memory'),
    },
    scope: {
      toggles: ['#wf-spectrum', '#wf-waterfall', '.tile-wf'],
    },
    dstar: {
      toggles: ['.dstar-control'],
    },
  };

  let _active = [];

  function render(active) {
    _active = active || [];
    const panel = document.getElementById('rig-features-panel');
    const wrap  = document.getElementById('rig-features-buttons');
    if (!panel || !wrap) return;

    wrap.innerHTML = '';

    // Funkcje ktore maja wlasne kontrolki gdzie indziej — nie generuj przycisku,
    // tylko pokaz/skryj powiazane elementy
    const activeIds = new Set(_active.map(f => f.id));

    for (const [fid, action] of Object.entries(FEATURE_ACTIONS)) {
      const isActive = activeIds.has(fid);
      if (action.toggles) {
        for (const sel of action.toggles) {
          document.querySelectorAll(sel).forEach(el => {
            el.style.display = isActive ? '' : 'none';
          });
        }
      }
    }

    // Generuj przyciski tylko dla funkcji z onClick (nawigacyjne/akcyjne)
    let anyButton = false;
    for (const f of _active) {
      const action = FEATURE_ACTIONS[f.id];
      if (!action || !action.onClick) continue;
      anyButton = true;
      const btn = document.createElement('button');
      btn.className = 'btn-feature';
      btn.dataset.feature = f.id;
      btn.innerHTML = `${f.icon || ''} ${f.label}`;
      btn.onclick = action.onClick;
      wrap.appendChild(btn);
    }

    // Pokaz panel tylko jesli sa jakies przyciski do wyswietlenia
    panel.style.display = anyButton ? '' : 'none';
  }

  async function refresh() {
    try {
      const token = (window.AUTH && AUTH.getToken) ? AUTH.getToken() : localStorage.getItem('token');
      const res = await fetch('/api/rig/features', {
        headers: token ? { 'Authorization': `Bearer ${token}` } : {}
      });
      const data = await res.json();
      if (data.ok) {
        // Admin endpoint zwraca {features:[...]} z supported/enabled/effective;
        // user endpoint zwraca {active:[...]}. Obsluz oba.
        if (data.active) {
          render(data.active);
        } else if (data.features) {
          const active = data.features.filter(f => f.effective)
            .map(f => ({ id: f.id, label: f.label, icon: f.icon, group: f.group }));
          render(active);
        }
      }
    } catch (e) {
      console.warn('[rigfeatures] refresh blad:', e);
    }
  }

  function handleWsMessage(data) {
    if (data.type === 'rig_features' && Array.isArray(data.active)) {
      render(data.active);
    }
  }

  function isActive(featureId) {
    return _active.some(f => f.id === featureId);
  }

  return { refresh, render, handleWsMessage, isActive };
})();

// Odswiez po zaladowaniu strony (po autoryzacji)
document.addEventListener('DOMContentLoaded', () => {
  // Malle opoznienie zeby AUTH/token byl juz dostepny
  setTimeout(() => RigFeatures.refresh(), 500);
});
