/* advradio.js — "Deep" CI-V radio settings, admin-only (approved 2026-10-15):
 * modulation input source/level, AGC mode, VOX fine-tuning, NB depth/width,
 * APF, TX delay per band (amplifier/antenna-relay PTT sequencing), and
 * RIT/ΔTX (clarifier).
 *
 * Driven generically off rigs/civ_profiles.py's "adv" table via
 * GET/POST /api/rig/adv (same "settings" permission gate as KONFIGURACJA,
 * see auth.js's data-perm handling) — one renderer per "kind" instead of a
 * hand-written control per setting. RIT uses its own small GET/POST
 * /api/rig/rit (unique signed 4-digit format, doesn't fit the generic table).
 *
 * Containers (see index.html):
 *   #adv-mod      (KONFIGURACJA) — MOD_USB/ACC/LAN, DATA_OFF_MOD, DATA_MOD
 *   #adv-nb       (KONFIGURACJA) — NB_DEPTH, NB_WIDTH
 *   #adv-txdelay  (KONFIGURACJA) — TX_DELAY_HF/50M/70M
 *   #adv-agc      (RADIO tab)    — AGC_MODE
 *   #adv-vox      (RADIO tab)    — VOX_GAIN, VOX_DELAY, ANTI_VOX
 *   #adv-apf      (CW tab)       — APF_MODE, APF_TYPE, APF_LEVEL
 *   #rit-row      (RADIO tab, near VFO) — RIT/ΔTX, handled separately below.
 */
const AdvRadio = (() => {

  const GROUPS = {
    'adv-mod':     ['MOD_USB', 'MOD_ACC', 'MOD_LAN', 'DATA_OFF_MOD', 'DATA_MOD'],
    'adv-nb':      ['NB_DEPTH', 'NB_WIDTH'],
    'adv-txdelay': ['TX_DELAY_HF', 'TX_DELAY_50M', 'TX_DELAY_70M'],
    'adv-agc':     ['AGC_MODE'],
    'adv-vox':     ['VOX_GAIN', 'VOX_DELAY', 'ANTI_VOX'],
    'adv-apf':     ['APF_MODE', 'APF_TYPE', 'APF_LEVEL'],
  };

  function authHeaders() {
    const token = (window.AUTH && AUTH.getToken) ? AUTH.getToken() : localStorage.getItem('token');
    return token ? { 'Authorization': `Bearer ${token}` } : {};
  }

  function labelFor(key) {
    const k = 'adv_' + key.toLowerCase();
    return (window.I18n && I18n.t(k) !== k) ? I18n.t(k) : key;
  }

  async function send(key, value) {
    try {
      const res = await fetch('/api/rig/adv', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ key, value }),
      });
      const data = await res.json();
      if (!data.ok) window.UI?.showToast?.('✗ ' + (data.error || key), 'error');
    } catch (e) {
      console.warn('[advradio] send error:', key, e);
    }
  }

  function buildRow(key, spec, value) {
    const row = document.createElement('div');
    row.className = 'adv-row';
    row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:6px 0;flex-wrap:wrap;';

    const lbl = document.createElement('span');
    lbl.style.cssText = 'font-family:var(--mono);font-size:11px;color:var(--dim);min-width:150px;';
    lbl.textContent = labelFor(key);
    row.appendChild(lbl);

    if (spec.kind === 'enum') {
      const sel = document.createElement('select');
      sel.style.cssText = 'font-family:var(--mono);font-size:11px;background:var(--panel3);' +
        'border:1px solid var(--border2);color:var(--text);padding:3px 6px;border-radius:3px;';
      (spec.labels || []).forEach((lab, i) => {
        const opt = document.createElement('option');
        opt.value = i; opt.textContent = lab;
        if (i === value) opt.selected = true;
        sel.appendChild(opt);
      });
      sel.onchange = () => send(key, parseInt(sel.value, 10));
      row.appendChild(sel);
      return row;
    }

    // Numeric kinds: bcd2pct (0-100%), bcd2range (min..max), range (raw+offset, scaled)
    let min = 0, max = 100, step = 1, initial = 0, display;
    if (spec.kind === 'bcd2pct') {
      min = 0; max = 100; step = 1;
      initial = Math.round((value ?? 0) * 100);
      display = v => `${v}%`;
    } else if (spec.kind === 'bcd2range') {
      min = spec.min; max = spec.max; step = 1;
      initial = value ?? spec.min;
      display = v => `${v}`;
    } else { // "range"
      const scale = spec.scale || 1;
      const offset = spec.offset || 0;
      min = offset;
      max = (spec.rawmax ?? 255) * scale + offset;
      step = scale < 1 ? scale : 1;
      initial = value ?? min;
      display = v => (scale < 1 ? `${(+v).toFixed(1)}s` : `${v}`);
    }

    const input = document.createElement('input');
    input.type = 'range';
    input.min = min; input.max = max; input.step = step;
    input.value = initial;
    input.style.cssText = 'flex:1;min-width:120px;max-width:220px;';

    const valSpan = document.createElement('span');
    valSpan.style.cssText = 'font-family:var(--mono);font-size:11px;color:var(--fg);min-width:42px;text-align:right;';
    valSpan.textContent = display(input.value);

    let lastSend = 0, pendingTimer = null;
    const doSend = () => {
      let v = parseFloat(input.value);
      if (spec.kind === 'bcd2pct') v = v / 100;
      send(key, v);
      lastSend = Date.now();
      pendingTimer = null;
    };
    input.oninput = () => {
      valSpan.textContent = display(input.value);
      const dt = Date.now() - lastSend;
      if (dt >= 150) doSend();
      else if (!pendingTimer) pendingTimer = setTimeout(doSend, 150 - dt);
    };
    input.onchange = () => {
      if (pendingTimer) { clearTimeout(pendingTimer); pendingTimer = null; }
      doSend();
    };

    row.appendChild(input);
    row.appendChild(valSpan);
    return row;
  }

  async function refresh() {
    let data;
    try {
      const res = await fetch('/api/rig/adv', { headers: authHeaders() });
      data = await res.json();
    } catch (e) {
      console.warn('[advradio] refresh error:', e);
      return;
    }
    if (!data || !data.ok) return; // 403 for non-settings users — containers stay hidden via data-perm anyway

    const { values, spec } = data;
    for (const [containerId, keys] of Object.entries(GROUPS)) {
      const container = document.getElementById(containerId);
      if (!container) continue;
      container.innerHTML = '';
      for (const key of keys) {
        const s = spec[key];
        if (!s) continue;
        container.appendChild(buildRow(key, s, values[key]));
      }
    }
  }

  // ── RIT / ΔTX ────────────────────────────────────────────────────────────
  async function ritRefresh() {
    let data;
    try {
      const res = await fetch('/api/rig/rit', { headers: authHeaders() });
      data = await res.json();
    } catch (e) {
      console.warn('[advradio] rit refresh error:', e);
      return;
    }
    if (!data || !data.ok) return;
    ritApply(data);
  }

  function ritApply(data) {
    const hzEl = document.getElementById('rit-hz-input');
    const onBtn = document.getElementById('rit-on-btn');
    const dtxBtn = document.getElementById('dtx-on-btn');
    if (hzEl) hzEl.value = data.hz ?? 0;
    if (onBtn) onBtn.classList.toggle('active', !!data.ritOn);
    if (dtxBtn) dtxBtn.classList.toggle('active', !!data.dtxOn);
  }

  async function ritSetHz(hz) {
    const res = await fetch('/api/rig/rit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ hz: parseInt(hz, 10) || 0 }),
    });
    const data = await res.json();
    if (data.ok) ritApply(data);
  }

  async function ritToggle(field) {
    const current = document.getElementById(field === 'ritOn' ? 'rit-on-btn' : 'dtx-on-btn')?.classList.contains('active');
    const body = {}; body[field] = !current;
    const res = await fetch('/api/rig/rit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.ok) ritApply(data);
  }

  function init() {
    refresh();
    ritRefresh();
  }

  return { init, refresh, ritRefresh, ritSetHz, ritToggle };
})();

document.addEventListener('DOMContentLoaded', () => {
  setTimeout(() => AdvRadio.init(), 600);
});
