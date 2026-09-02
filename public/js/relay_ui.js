/*
 * relay_ui.js - Relay buttons in the Radio tab (next to the radio actions).
 *
 * Uses /api/relay/state (what the user sees) and /api/relay/action (clicks).
 * WS listener: 'relay_state' updates the buttons in real time.
 */

window.RelayUI = (function() {
  let _relays = [];    // visible to this user
  let _connected = false;

  async function refresh() {
    try {
      const r = await fetch('/api/relay/state', { credentials: 'include' });
      if (!r.ok) return;
      const data = await r.json();
      if (!data.ok) return;
      _relays = data.relays || [];
      _connected = !!data.connected;
      render();
    } catch(e) { console.warn('[relay-ui] refresh error:', e); }
  }

  function render() {
    const container = document.getElementById('relay-buttons');
    if (!container) return;

    if (!_relays.length) {
      container.style.display = 'none';
      return;
    }
    container.style.display = 'flex';

    container.innerHTML = _relays.map(r => {
      const isOn = !!r.state;
      const isMomentary = r.mode === 'momentary';
      const color = isOn ? 'var(--red)' : 'var(--green)';
      const bgColor = isOn ? 'rgba(217,119,106,0.15)' : 'rgba(184,201,143,0.1)';
      const label = isMomentary
        ? `⚡ ${_escapeHtml(r.name)}`
        : (isOn ? `● ${_escapeHtml(r.name)}` : `○ ${_escapeHtml(r.name)}`);
      const title = isMomentary
        ? `Impuls ${r.pulse_s}s`
        : (isOn ? 'Kliknij żeby wyłączyć' : 'Kliknij żeby włączyć');
      return `
        <button class="relay-btn" data-relay-id="${r.id}" onclick="RelayUI.action(${r.id})"
          title="${title}"
          style="font-family:var(--mono);font-size:10px;padding:5px 10px;background:${bgColor};
          border:1px solid ${color};color:${color};border-radius:3px;cursor:pointer;
          letter-spacing:1px;white-space:nowrap;transition:all 0.15s;">
          ${label}
        </button>
      `;
    }).join('');
  }

  async function action(relayId) {
    if (!_connected) {
      window.UI?.showToast?.('⛔ Kontroler przekaźników niepodłączony', 'error');
      return;
    }
    // Optimistic feedback — the button dims
    const btn = document.querySelector(`.relay-btn[data-relay-id="${relayId}"]`);
    if (btn) btn.style.opacity = '0.5';
    try {
      const r = await fetch('/api/relay/action', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: relayId }),
      });
      const data = await r.json();
      if (!r.ok || !data.ok) {
        window.UI?.showToast?.('⛔ ' + (data.error || 'Błąd'), 'error');
      }
    } catch(e) {
      window.UI?.showToast?.('⛔ ' + e.message, 'error');
    } finally {
      if (btn) btn.style.opacity = '';
    }
  }

  function onWSMessage(msg) {
    if (msg.type !== 'relay_state') return;
    const r = _relays.find(x => x.id === msg.id);
    if (r) {
      r.state = !!msg.state;
      render();
    }
  }

  function _escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, m => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[m]));
  }

  // Auto-refresh after login
  window.addEventListener('app:ready', () => {
    refresh();
    // Refresh every 30s (in case the admin changes the config)
    setInterval(refresh, 30000);
  });

  return { refresh, action, render, onWSMessage };
})();
