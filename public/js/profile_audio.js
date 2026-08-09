/*
 * profile_audio.js — Wybor urzadzen audio uzytkownika (przegladarka).
 *
 * RX output (glosnik/sluchawki gdzie user slyszy odbior) — setSinkId na
 * globalnym audioCtx, zapisywane w localStorage['ham_audio_sinkId'].
 * TX input (mikrofon) — zapisywane w localStorage['ham_audio_micId'],
 * uzywane przez ws.js/_txMic i tx_eq.js monitor.
 */
window.ProfileAudio = (function() {
  let _testCtx = null;
  let _meterStream = null;
  let _meterRaf = null;

  // Filtr: odrzuc wirtualne/kablowe urzadzenia z domyslnej sugestii
  function _isVirtual(label) {
    const l = (label || '').toLowerCase();
    return l.includes('virtual') || l.includes('cable') ||
           l.includes('vb-audio') || l.includes('voicemeeter');
  }

  async function load() {
    try {
      // Poproś o dostep do mikrofonu zeby dostac etykiety urzadzen
      // (bez permission enumerateDevices zwraca puste labelki)
      try {
        const s = await navigator.mediaDevices.getUserMedia({ audio: true });
        s.getTracks().forEach(t => t.stop());
      } catch(e) { /* user odmowil — pokazemy co sie da */ }

      const devs = await navigator.mediaDevices.enumerateDevices();
      const outputs = devs.filter(d => d.kind === 'audiooutput');
      const inputs  = devs.filter(d => d.kind === 'audioinput');

      const outSel = document.getElementById('profile-audio-out');
      const inSel  = document.getElementById('profile-audio-in');

      const savedOut = localStorage.getItem('ham_audio_sinkId') || '';
      const savedIn  = localStorage.getItem('ham_audio_micId') || '';

      if (outSel) {
        outSel.innerHTML = outputs.map(d => {
          const virt = _isVirtual(d.label) ? ' ⚠' : '';
          const sel = d.deviceId === savedOut ? 'selected' : '';
          return `<option value="${d.deviceId}" ${sel}>${_escape(d.label || 'Urządzenie ' + d.deviceId.slice(0,6))}${virt}</option>`;
        }).join('') || '<option value="">brak urządzeń</option>';
      }
      if (inSel) {
        inSel.innerHTML = inputs.map(d => {
          const virt = _isVirtual(d.label) ? ' ⚠' : '';
          const sel = d.deviceId === savedIn ? 'selected' : '';
          return `<option value="${d.deviceId}" ${sel}>${_escape(d.label || 'Mikrofon ' + d.deviceId.slice(0,6))}${virt}</option>`;
        }).join('') || '<option value="">brak urządzeń</option>';
      }
    } catch(e) {
      console.warn('[profile-audio] load blad:', e);
    }
  }

  function setOutput(deviceId) {
    if (!deviceId) return;
    localStorage.setItem('ham_audio_sinkId', deviceId);
    // Zastosuj od razu na globalnym audioCtx jesli istnieje
    const ctx = window.audioCtx || window.AppAudio?.ctx;
    if (ctx && ctx.setSinkId) {
      ctx.setSinkId(deviceId).catch(e => console.warn('[profile-audio] setSinkId:', e));
    }
    window.UI?.showToast?.('✓ Głośnik RX zapisany', 'info');
  }

  function setInput(deviceId) {
    if (!deviceId) return;
    localStorage.setItem('ham_audio_micId', deviceId);
    window.UI?.showToast?.('✓ Mikrofon TX zapisany (aktywny przy następnym TX)', 'info');
  }

  // Test glosnika — odtworz krotki ton (1 kHz, 0.5s) na wybranym wyjsciu
  async function testOutput() {
    try {
      if (_testCtx) { try { _testCtx.close(); } catch(e){} }
      _testCtx = new (window.AudioContext || window.webkitAudioContext)();
      const sinkId = localStorage.getItem('ham_audio_sinkId');
      if (sinkId && _testCtx.setSinkId) {
        try { await _testCtx.setSinkId(sinkId); } catch(e) {}
      }
      const osc = _testCtx.createOscillator();
      const gain = _testCtx.createGain();
      osc.frequency.value = 1000;
      osc.type = 'sine';
      gain.gain.setValueAtTime(0.0001, _testCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.3, _testCtx.currentTime + 0.05);
      gain.gain.exponentialRampToValueAtTime(0.0001, _testCtx.currentTime + 0.5);
      osc.connect(gain); gain.connect(_testCtx.destination);
      osc.start();
      osc.stop(_testCtx.currentTime + 0.5);
      window.UI?.showToast?.('🔉 Ton testowy 1 kHz', 'info');
    } catch(e) {
      window.UI?.showToast?.('✕ Błąd testu głośnika: ' + e.message, 'error');
    }
  }

  // Test mikrofonu — pokaz poziom przez 5s na wybranym wejsciu
  async function testInput() {
    const meter = document.getElementById('profile-audio-meter');
    const level = document.getElementById('profile-audio-level');
    const hint  = document.getElementById('profile-audio-hint');
    if (!meter || !level) return;

    // Zatrzymaj poprzedni test jesli trwa
    stopMeter();

    const micId = localStorage.getItem('ham_audio_micId');
    try {
      const constraints = { audio: micId ? { deviceId: { exact: micId } } : true };
      _meterStream = await navigator.mediaDevices.getUserMedia(constraints);
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const src = ctx.createMediaStreamSource(_meterStream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      src.connect(analyser);
      const data = new Uint8Array(analyser.fftSize);

      meter.style.display = 'block';
      let peak = 0;
      const startTs = Date.now();

      function tick() {
        analyser.getByteTimeDomainData(data);
        // Oblicz RMS
        let sum = 0;
        for (let i = 0; i < data.length; i++) {
          const v = (data[i] - 128) / 128;
          sum += v * v;
        }
        const rms = Math.sqrt(sum / data.length);
        const pct = Math.min(100, rms * 300);
        level.style.width = pct + '%';
        if (pct > peak) peak = pct;

        const elapsed = (Date.now() - startTs) / 1000;
        if (elapsed < 5) {
          if (hint) hint.textContent = `Mów do mikrofonu... (${(5-elapsed).toFixed(0)}s) — szczyt: ${peak.toFixed(0)}%`;
          _meterRaf = requestAnimationFrame(tick);
        } else {
          // Podsumowanie
          if (hint) {
            if (peak < 5) {
              hint.textContent = `⚠ Szczyt tylko ${peak.toFixed(0)}% — mikrofon za cichy lub zły wybór urządzenia`;
              hint.style.color = 'var(--red)';
            } else if (peak > 90) {
              hint.textContent = `⚠ Szczyt ${peak.toFixed(0)}% — za głośno, może przesterować`;
              hint.style.color = 'var(--amber)';
            } else {
              hint.textContent = `✓ Szczyt ${peak.toFixed(0)}% — poziom OK`;
              hint.style.color = 'var(--green)';
            }
          }
          level.style.width = '0%';
          stopMeter();
          ctx.close();
        }
      }
      tick();
    } catch(e) {
      if (hint) { hint.textContent = '✕ Błąd: ' + e.message; hint.style.color = 'var(--red)'; }
      window.UI?.showToast?.('✕ Nie można otworzyć mikrofonu: ' + e.message, 'error');
    }
  }

  function stopMeter() {
    if (_meterRaf) { cancelAnimationFrame(_meterRaf); _meterRaf = null; }
    if (_meterStream) {
      _meterStream.getTracks().forEach(t => t.stop());
      _meterStream = null;
    }
  }

  function _escape(s) {
    return String(s || '').replace(/[&<>"']/g, m => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[m]));
  }

  return { load, setOutput, setInput, testOutput, testInput, stopMeter };
})();
