/**
 * voicekeyer.js - recorded SSB messages (2026-11-14).
 *
 * 4 slots per operator (F1-F4 in the RADIO tab while the radio is in a
 * voice mode; in CW the F keys keep sending CW macros - see cw.js).
 * Recording happens in the browser: 16 kHz mono 16-bit WAV, max 30 s
 * (plenty for SSB's 300-2700 Hz and small enough to upload), stored on the
 * server per operator. "Transmit" asks the server to play the recording
 * on air through the same TX path as the live microphone; the server keys
 * PTT itself and only the TRX holder may do it. Esc stops it.
 */
window.VoiceKeyer = (() => {
  'use strict';
  const SLOTS = 4, RATE = 16000;
  let maxS = 30;
  let rec = null;          // {slot, ctx, stream, node, chunks, len, timer}
  let playing = null;      // slot being transmitted (from the server)
  let preview = null;      // local <audio> preview
  const has = {};          // slot -> has a recording (for the empty-key message)

  function _el(id) { return document.getElementById(id); }

  async function load() {
    try {
      const d = await (await fetch('/api/voicekeyer')).json();
      maxS = d.max_s || 30;
      (d.slots || []).forEach(s => _setLen(s.slot, s.secs));
    } catch (e) {}
  }

  function _setLen(slot, secs) {
    has[slot] = !!secs;
    const l = _el(`vk-len-${slot}`);
    if (l) l.textContent = secs ? `${secs.toFixed(1)} s` : '—';
    const p = _el(`vk-play-${slot}`);
    if (p) p.disabled = !secs;
  }

  function _wav(samples) {
    const n = samples.length, buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
    const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    w(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); w(8, 'WAVE'); w(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, RATE, true); v.setUint32(28, RATE * 2, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, 'data'); v.setUint32(40, n * 2, true);
    for (let i = 0; i < n; i++) {
      const x = Math.max(-1, Math.min(1, samples[i]));
      v.setInt16(44 + i * 2, x < 0 ? x * 0x8000 : x * 0x7FFF, true);
    }
    return new Blob([buf], { type: 'audio/wav' });
  }

  async function toggleRecord(slot) {
    if (rec) { if (rec.slot === slot) await _stopRecord(); return; }
    let stream;
    try {
      const micId = localStorage.getItem('ham_audio_micId');
      const c = { echoCancellation: false, noiseSuppression: false, autoGainControl: false };
      if (micId) c.deviceId = { ideal: micId };
      stream = await navigator.mediaDevices.getUserMedia({ audio: c });
    } catch (e) {
      window.UI?.showToast(I18n.t('profile_toast_mic_no_access') + (e.message || e.name), 'error');
      return;
    }
    const ctx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: RATE });
    const src = ctx.createMediaStreamSource(stream);
    const node = ctx.createScriptProcessor(4096, 1, 1);
    rec = { slot, ctx, stream, node, chunks: [], len: 0 };
    node.onaudioprocess = (ev) => {
      if (!rec) return;
      const d = new Float32Array(ev.inputBuffer.getChannelData(0));
      rec.chunks.push(d); rec.len += d.length;
      const l = _el(`vk-len-${slot}`);
      if (l) l.textContent = `● ${(rec.len / RATE).toFixed(1)} s`;
      if (rec.len >= maxS * RATE) _stopRecord();
    };
    src.connect(node);
    node.connect(ctx.destination);   // ScriptProcessor only runs when connected; it outputs silence
    const b = _el(`vk-rec-${slot}`);
    if (b) { b.textContent = '■'; b.style.color = 'var(--red)'; }
  }

  async function _stopRecord() {
    const r = rec; rec = null;
    if (!r) return;
    try { r.node.disconnect(); } catch (e) {}
    r.stream.getTracks().forEach(t => t.stop());
    try { await r.ctx.close(); } catch (e) {}
    const b = _el(`vk-rec-${r.slot}`);
    if (b) { b.textContent = '●'; b.style.color = ''; }
    const all = new Float32Array(Math.min(r.len, maxS * RATE));
    let off = 0;
    for (const c of r.chunks) { const k = Math.min(c.length, all.length - off); all.set(c.subarray(0, k), off); off += k; if (off >= all.length) break; }
    if (all.length < RATE / 2) { window.UI?.showToast(I18n.t('vk_too_short'), 'error'); load(); return; }
    const resp = await fetch(`/api/voicekeyer/${r.slot}`, {
      method: 'POST', headers: { 'Content-Type': 'application/octet-stream' }, body: _wav(all) });
    let d = {}; try { d = await resp.json(); } catch (e) {}
    if (resp.ok) { _setLen(r.slot, d.secs); window.UI?.showToast(I18n.t('vk_saved').replace('{n}', r.slot)); }
    else { window.UI?.showToast('✕ ' + (d.error || resp.status), 'error'); load(); }
  }

  function play(slot) {
    if (rec) return;
    if (!has[slot]) { window.UI?.showToast(I18n.t('vk_empty').replace('{n}', slot), 'error'); return; }
    window.FT8Timer?.reset?.();
    window.WS?.send({ type: 'voice_play', slot });
  }

  function stop() { window.WS?.send({ type: 'voice_stop' }); }

  async function listen(slot) {
    if (preview) { preview.pause(); preview = null; return; }
    const r = await fetch(`/api/voicekeyer/${slot}`);
    if (!r.ok) return;
    const url = URL.createObjectURL(await r.blob());
    preview = new Audio(url);
    preview.onended = () => { URL.revokeObjectURL(url); preview = null; };
    preview.play();
  }

  function onStatus(msg) {
    playing = msg.playing || null;
    for (let s = 1; s <= SLOTS; s++) {
      const b = _el(`vk-play-${s}`);
      if (b) b.classList.toggle('active', playing === s);
    }
  }

  window.addEventListener('app:ready', load);
  return { load, toggleRecord, play, stop, listen, onStatus, isPlaying: () => !!playing };
})();
