/**
 * wf_palettes.js - selectable waterfall colour palettes (2026-11-14).
 *
 * Shared by both waterfalls: RADIO (waterfall.js, canvas) and FT8/WSJT-X
 * (wsjtx_scope.js, WebGL). "default" keeps each waterfall's own original
 * palette; the others are defined here as colour stops over 0..255 (weak
 * -> strong signal). The choice is per browser (localStorage) and changing
 * it in either place updates both waterfalls and both selectors at once.
 */
window.WfPalettes = (() => {
  'use strict';
  const KEY = 'ham_wf_palette';
  const STOPS = {
    // SDR-style: black -> deep blue -> purple -> red -> yellow
    sdr:  [[0, [0, 0, 0]], [50, [0, 0, 90]], [100, [60, 0, 140]], [150, [180, 0, 120]],
           [200, [255, 60, 0]], [235, [255, 200, 0]], [255, [255, 255, 200]]],
    // Greyscale - easiest on the eyes at night, weak signals stand out by brightness
    mono: [[0, [0, 0, 0]], [60, [40, 40, 40]], [255, [255, 255, 255]]],
    // "Iron": black -> dark red -> orange -> yellow -> white
    warm: [[0, [0, 0, 0]], [60, [60, 0, 0]], [130, [200, 30, 0]], [190, [255, 140, 0]],
           [235, [255, 230, 80]], [255, [255, 255, 255]]],
  };
  const listeners = [];

  function current() {
    try {
      const v = localStorage.getItem(KEY);
      return (v && (v === 'default' || STOPS[v])) ? v : 'default';
    } catch (e) { return 'default'; }
  }

  // 256 x RGBA for a named palette, or null for "default" (the caller then
  // uses its own built-in palette).
  function rgba(name) {
    const st = STOPS[name];
    if (!st) return null;
    const out = new Uint8Array(256 * 4);
    for (let i = 0; i < 256; i++) {
      let lo = st[0], hi = st[st.length - 1];
      for (let s = 0; s < st.length - 1; s++) {
        if (i >= st[s][0] && i <= st[s + 1][0]) { lo = st[s]; hi = st[s + 1]; break; }
      }
      const span = hi[0] - lo[0];
      const t = span > 0 ? (i - lo[0]) / span : 0;
      for (let c = 0; c < 3; c++) out[i * 4 + c] = Math.round(lo[1][c] + t * (hi[1][c] - lo[1][c]));
      out[i * 4 + 3] = 255;
    }
    return out;
  }

  function _syncSelects(name) {
    document.querySelectorAll('select.wf-palette-select').forEach(s => { s.value = name; });
  }

  function set(name) {
    if (name !== 'default' && !STOPS[name]) name = 'default';
    try { localStorage.setItem(KEY, name); } catch (e) {}
    _syncSelects(name);
    listeners.forEach(f => { try { f(name); } catch (e) { console.warn('[wf-palette]', e); } });
  }

  function onChange(f) { listeners.push(f); }

  document.addEventListener('DOMContentLoaded', () => _syncSelects(current()));

  return { current, rgba, set, onChange };
})();
