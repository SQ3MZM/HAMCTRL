/*
 * serial_ports.js — shared COM port dropdown helper.
 *
 * Backs every serial-port <select> in the app (radio, CW DTR/RTS keyer,
 * rotator) with what Windows actually reports via /api/system/serial-ports,
 * instead of a free-typed text field. One client-side cache shared by all
 * callers so opening several dropdowns in a row doesn't re-enumerate ports
 * (list_serial_ports() can be slow on Windows) — same 30s window as the
 * server-side cache behind the endpoint.
 */
(function() {
  let _cache = null;
  let _cacheTime = 0;
  const TTL_MS = 30000;

  async function fetchPorts(force) {
    const now = Date.now();
    if (!force && _cache && (now - _cacheTime) < TTL_MS) return _cache;
    try {
      const r = await fetch('/api/system/serial-ports', { credentials: 'include' });
      const d = await r.json();
      _cache = (d.ok && d.ports) || [];
      _cacheTime = now;
    } catch (e) {
      _cache = _cache || [];
    }
    return _cache;
  }

  // Fill a <select> with detected COM ports, keeping `current` selected even
  // if it isn't currently detected (radio/keyer/rotor may be powered off or
  // mid-reconnect) so a save never silently drops a valid-but-offline port.
  // `emptyLabel`, when given, adds a blank option first (used by the CW
  // keyer port, where empty means "reuse the CI-V port").
  async function populate(sel, current, emptyLabel, force) {
    if (!sel) return;
    const ports = await fetchPorts(force);
    sel.innerHTML = '';
    if (emptyLabel !== undefined) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = emptyLabel;
      sel.appendChild(opt);
    } else if (!current) {
      const opt = document.createElement('option');
      opt.value = '';
      opt.textContent = window.I18n ? I18n.t('adm_choose_port') : '(wybierz port)';
      sel.appendChild(opt);
    }
    ports.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.device;
      opt.textContent = p.description ? `${p.device} — ${p.description}` : p.device;
      sel.appendChild(opt);
    });
    if (current && !ports.find(p => p.device === current)) {
      const opt = document.createElement('option');
      opt.value = current;
      opt.textContent = current + (window.I18n ? I18n.t('adm_port_unavailable') : ' (niedostepny)');
      sel.appendChild(opt);
    }
    sel.value = current || '';
  }

  window.SerialPorts = { fetchPorts, populate };
})();
