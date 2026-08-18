/* rigfeatures.js — Radio features panel (below PTT, above the waterfall)
 *
 * For a regular user: shows buttons only for features the admin enabled
 * AND that the radio technically supports (effective_features from the backend).
 *
 * Updates:
 *  - after the WS connects (init)
 *  - after the radio changes (/api/rig/connect)
 *  - after the admin saves the whitelist (broadcast 'rig_features')
 *
 * Each button:
 *  - "freq_set"/"mode_set"/"split"/"rit" — doesn't generate its own button
 *    (these features already have separate controls in the VFO UI) — the
 *    panel only shows them as info/toggling the visibility of the related controls
 *  - "ptt" — already has its own PTT button (the panel doesn't duplicate it)
 *  - "tx_power", "memory", "dstar", "scope", "smeter" — generate a
 *    dedicated button/link in the panel
 *
 * The feature_id -> button-action mapping is in FEATURE_ACTIONS below.
 * Features without an entry in FEATURE_ACTIONS are shown as a passive
 * badge indicating the feature is available, but controlled via existing
 * controls elsewhere in the UI.
 */

const RigFeatures = (() => {

  // Actions for the panel's buttons — feature_id -> {onClick, toggles}
  // 'toggles' = list of CSS selectors for elements to show/hide
  //             depending on effective (e.g. the TX power slider)
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

    // Features that have their own controls elsewhere — don't generate a
    // button, just show/hide the related elements
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

    // Generate buttons only for features with onClick (navigation/action)
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

    // Show the panel only if there are buttons to display
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
        // The admin endpoint returns {features:[...]} with
        // supported/enabled/effective; the user endpoint returns
        // {active:[...]}. Handle both.
        if (data.active) {
          render(data.active);
        } else if (data.features) {
          const active = data.features.filter(f => f.effective)
            .map(f => ({ id: f.id, label: f.label, icon: f.icon, group: f.group }));
          render(active);
        }
      }
    } catch (e) {
      console.warn('[rigfeatures] refresh error:', e);
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

// Refresh after the page loads (after auth)
document.addEventListener('DOMContentLoaded', () => {
  // Small delay so AUTH/token is already available
  setTimeout(() => RigFeatures.refresh(), 500);
});
