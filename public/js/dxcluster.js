/*
 * dxcluster.js — DX Cluster client (UI + WS handlers).
 *
 * Receives from the server:
 *   {type: 'dx_spot', freq_hz, call, spotter, comment, utc, ts, band, mode}
 *   {type: 'dx_status', status: 'connecting'|'connected'|'disconnected'|'error', message}
 *
 * Sends to the server:
 *   POST /api/dxcluster/config  — save host/login/password
 *   POST /api/dxcluster/connect — connect
 *   POST /api/dxcluster/disconnect — disconnect
 *   POST /api/dxcluster/command — arbitrary telnet command
 *   GET  /api/dxcluster/config  — fetch the config
 *   GET  /api/dxcluster/history — fetch the recent-spots cache
 */

window.DXCluster = (function() {
  let _spots = [];             // all spots received in this session
  const _MAX_SPOTS = 500;      // in-memory limit
  let _connected = false;

  // Show/hide the config panel
  function toggleConfig() {
    const panel = document.getElementById('dx-config-panel');
    if (!panel) return;
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
  }

  // ── Config ────────────────────────────────────────────────────────────────
  async function loadConfig() {
    try {
      const r = await fetch('/api/dxcluster/config', { credentials: 'include' });
      if (!r.ok) return;
      const data = await r.json();
      if (!data.ok) return;
      const cfg = data.config || {};
      document.getElementById('dx-host').value = cfg.host || '';
      document.getElementById('dx-port').value = cfg.port || 7300;
      document.getElementById('dx-login').value = cfg.login || '';
      // We don't return the password - the placeholder shows it's saved
      const pwEl = document.getElementById('dx-password');
      if (pwEl) pwEl.placeholder = cfg.has_password ? I18n.t('dx_password_saved_ph') : I18n.t('dx_optional_ph');
      document.getElementById('dx-auto-connect').checked = !!cfg.auto_connect;

      // Auto-show the config if there's no host (first time)
      if (!cfg.host) {
        const panel = document.getElementById('dx-config-panel');
        if (panel) panel.style.display = 'block';
      }

      // Load history from the server cache (spots from this session even after a refresh)
      if (data.history && data.history.length) {
        _spots = data.history.slice();
        renderSpots();
      }

      // Update the status
      _connected = !!data.connected;
      updateStatusBadge(_connected ? 'connected' : 'disconnected', '');
    } catch(e) { console.warn('[dx] loadConfig error:', e); }
  }

  async function save() {
    const status = document.getElementById('dx-save-status');
    if (status) { status.textContent = I18n.t('dx_saving'); status.style.color = 'var(--dim)'; }
    const password = document.getElementById('dx-password').value;
    const payload = {
      host: document.getElementById('dx-host').value.trim(),
      port: parseInt(document.getElementById('dx-port').value, 10),
      login: document.getElementById('dx-login').value.trim(),
      // If the password field is empty, send null - the server keeps the previous one
      password: password ? password : null,
      auto_connect: document.getElementById('dx-auto-connect').checked,
    };
    try {
      const r = await fetch('/api/dxcluster/config', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (r.ok && data.ok) {
        if (status) { status.textContent = I18n.t('dx_saved'); status.style.color = 'var(--green)'; }
        // Clear the password field (we don't want to keep it in the DOM)
        document.getElementById('dx-password').value = '';
        setTimeout(loadConfig, 500);
      } else {
        if (status) { status.textContent = '✕ ' + (data.error || I18n.t('profile_error_fallback')); status.style.color = 'var(--red)'; }
      }
    } catch(e) {
      if (status) { status.textContent = '✕ ' + e.message; status.style.color = 'var(--red)'; }
    }
  }

  async function connect() {
    // Save the config before connecting, in case there are unsaved changes
    await save();
    try {
      const r = await fetch('/api/dxcluster/connect', {
        method: 'POST', credentials: 'include',
      });
      const data = await r.json();
      if (!r.ok || !data.ok) {
        window.UI?.showToast?.('⛔ ' + (data.error || I18n.t('cfg_conn_error')), 'error');
      }
    } catch(e) { window.UI?.showToast?.('⛔ ' + e.message, 'error'); }
  }

  async function disconnect() {
    try {
      await fetch('/api/dxcluster/disconnect', {
        method: 'POST', credentials: 'include',
      });
    } catch(e) {}
  }

  async function sendCommand() {
    const el = document.getElementById('dx-cmd');
    const cmd = (el?.value || '').trim();
    if (!cmd) return;
    try {
      const r = await fetch('/api/dxcluster/command', {
        method: 'POST', credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ cmd }),
      });
      const data = await r.json();
      if (data.ok) {
        el.value = '';
      } else {
        window.UI?.showToast?.(I18n.t('dx_cmd_send_fail'), 'error');
      }
    } catch(e) { window.UI?.showToast?.('⛔ ' + e.message, 'error'); }
  }

  // ── Rendering spots ───────────────────────────────────────────────────────
  function renderSpots() {
    const tbody = document.getElementById('dx-spot-list');
    if (!tbody) return;
    const fBand = document.getElementById('dx-filter-band')?.value || '';
    const fMode = document.getElementById('dx-filter-mode')?.value || '';
    const fCall = (document.getElementById('dx-filter-call')?.value || '').toUpperCase().trim();

    // Newest at the top
    const filtered = _spots.slice().reverse().filter(s => {
      if (fBand && s.band !== fBand) return false;
      if (fMode && s.mode !== fMode) return false;
      if (fCall && !s.call.includes(fCall) && !s.spotter.includes(fCall)) return false;
      return true;
    });

    // Update the counter - show "shown / total" since we only render 30
    const totalCount = filtered.length;
    const shownCount = Math.min(30, totalCount);
    const countEl = document.getElementById('dx-spot-count');
    if (countEl) {
      countEl.textContent = totalCount > 30 ? `${shownCount}/${totalCount}` : String(totalCount);
      countEl.title = totalCount > 30 ? I18n.t('dx_spot_count_title').replace('{shown}', shownCount).replace('{total}', totalCount) : '';
    }

    if (!filtered.length) {
      tbody.innerHTML = `<tr><td colspan="8" style="text-align:center;padding:20px;color:var(--dim);font-family:var(--mono);font-size:10px;">
        ${_spots.length ? I18n.t('dx_empty_filtered') : I18n.t('dx_empty_connect')}
      </td></tr>`;
      return;
    }

    // Band colors for better readability
    const bandColors = {
      '160m':'#B08D57','80m':'#c17a2f','60m':'#d99050',
      '40m':'#4C8F2F','30m':'#3a7fc7','20m':'#E5B84A',
      '17m':'#E5824A','15m':'#c7783a','12m':'#a86532',
      '10m':'#d97a5c','6m':'#c95040','2m':'#8f4fa8','70cm':'#6d3a90',
    };

    tbody.innerHTML = filtered.slice(0, 30).map(s => {
      const mhz = (s.freq_hz / 1e6).toFixed(3);
      const ageS = Math.floor((Date.now() - s.ts * 1000) / 1000);
      const ageStr = ageS < 60 ? `${ageS}s`
                   : ageS < 3600 ? `${Math.floor(ageS/60)}m`
                   : `${Math.floor(ageS/3600)}h`;
      const bColor = bandColors[s.band] || 'var(--dim)';
      const isNew = ageS < 60;
      return `
        <tr style="font-family:var(--mono);font-size:11px;${isNew?'background:rgba(184,201,143,0.05);':''}">
          <td style="color:var(--dim);font-size:9px;">${_escapeHtml(s.utc || ageStr)}</td>
          <td style="color:var(--amber);font-weight:600;">${mhz}</td>
          <td style="color:var(--green);font-weight:700;letter-spacing:1px;">${_escapeHtml(s.call)}</td>
          <td style="color:${bColor};font-weight:600;">${s.band}</td>
          <td style="color:var(--fg);">${s.mode}</td>
          <td style="color:var(--dim);">${_escapeHtml(s.comment || '')}</td>
          <td style="color:var(--dim);font-size:10px;">${_escapeHtml(s.spotter)}</td>
          <td>
            <button onclick="DXCluster.qsy(${s.freq_hz}, '${s.mode}')"
              style="font-family:var(--mono);font-size:10px;padding:2px 8px;background:rgba(184,201,143,0.1);border:1px solid var(--green2);color:var(--green);border-radius:3px;cursor:pointer;">
              QSY
            </button>
          </td>
        </tr>`;
    }).join('');
  }

  // QSY to a spot's frequency + set the mode (if known).
  // ── Convert a spot's mode to the radio's mode (IC-7300) ────────────────────
  //
  // A cluster spot gives the "operating" mode (SSB, FT8, CW, RTTY...), but
  // the radio only understands LSB/USB/CW/RTTY/AM/FM. It needs translating
  // — in particular picking the CORRECT SIDEBAND for SSB and digital modes.
  //
  // Amateur convention (IARU):
  //   - below 10 MHz  -> LSB  (160m, 80m, 40m)
  //   - above 10 MHz  -> USB  (20m, 17m, 15m, 12m, 10m, 6m and up)
  //   - 60m (5.3 MHz) -> USB  (exception! 60m channels are always USB)
  //   - digital modes -> always USB (regardless of band)
  //
  // Returns null when changing the mode doesn't make sense (unknown / '?').
  function _spotModeToRigMode(mode, freq_hz) {
    const m = String(mode || '').trim().toUpperCase();
    if (!m || m === '?') return null;

    // Sideband for phone: <10 MHz = LSB, >=10 MHz = USB.
    // Exception: 60m (5250-5450 kHz) - internationally always USB.
    const is60m = freq_hz >= 5_250_000 && freq_hz <= 5_450_000;
    const phoneSideband = (freq_hz < 10_000_000 && !is60m) ? 'LSB' : 'USB';

    // Digital modes: ALWAYS USB (that's the standard, even on 40m/80m).
    // The radio can be in USB or USB-D - both work, USB-D is better (filter).
    const DIGI = ['FT8', 'FT4', 'RTTY', 'PSK', 'PSK31', 'JT65', 'JT9',
                  'DIGI', 'DATA', 'MFSK', 'OLIVIA', 'JS8', 'WSPR',
                  'MSK144', 'Q65'];
    if (DIGI.includes(m)) return 'USB';

    // SAT - satellite operation. We don't auto-change the mode, since it
    // depends on the satellite (FM/SSB/CW) and the link direction
    // (uplink/downlink). The operator sets it themselves.
    if (m === 'SAT') return null;

    // Phone -> correct sideband by band
    if (m === 'SSB' || m === 'PHONE' || m === 'LSB' || m === 'USB') {
      // If the spot explicitly says LSB/USB - respect it (the spotter knew better).
      // But when it just says "SSB" generically - pick the sideband by frequency.
      if (m === 'LSB' || m === 'USB') return m;
      return phoneSideband;
    }

    // CW, AM, FM - unchanged (the radio knows these modes)
    if (m === 'CW' || m === 'CWR' || m === 'CW-R') return 'CW';
    if (m === 'AM') return 'AM';
    if (m === 'FM' || m === 'NFM' || m === 'WFM') return 'FM';

    // Unknown mode - don't touch the radio (better to leave it as is)
    return null;
  }

  // IMPORTANT: we use window.UI.sendFreq (the proven mechanism) instead of
  // calling WS.send directly - this way QSY behaves exactly like manual
  // tuning from the Radio panel (lock check, S.freq update,
  // updateFreqDisplay, etc.). It used to call WS.send({type:'freq',freq})
  // directly, which bypassed the S.freq/updateFreqDisplay logic and didn't
  // always produce a visible change.
  function qsy(freq_hz, mode) {
    // Guard against invalid arguments from the HTML onclick
    freq_hz = parseInt(freq_hz, 10);
    if (!freq_hz || freq_hz < 100000) {
      window.UI?.showToast?.(I18n.t('dx_invalid_freq'), 'error');
      return;
    }

    // Check whether the user can control the radio (an informational
    // toast before sending, since without this UI.sendFreq would also
    // show a toast but we can explain it more clearly from the DX Cluster tab).
    const lock  = window.AppState?.radio_lock;
    const myUid = String(window.AppState?.my_uid || window.CurrentUser?.id || '');
    const role  = window.CurrentUser?.role;
    const canControl = role === 'admin' || (lock?.locked && String(lock.user_id) === myUid);
    if (!canControl) {
      const holder = lock?.callsign || lock?.username || '';
      const reason = lock?.locked && holder
        ? I18n.t('dx_trx_busy').replace('{holder}', holder)
        : I18n.t('dx_take_trx_first');
      window.UI?.showToast?.(I18n.t('dx_cant_qsy').replace('{reason}', reason), 'error');
      return;
    }

    // Use the proven UI.sendFreq — calls _canControlRadio, updates S.freq,
    // updateFreqDisplay, updateVFOBadges, scheduleBandMemorySave, and
    // sends the WS message. This is EXACTLY what clicking the "Radio" tab
    // during tuning does.
    if (typeof window.UI?.sendFreq === 'function') {
      window.UI.sendFreq(freq_hz);
    } else {
      // Fallback if UI.sendFreq is unavailable (shouldn't happen)
      window.WS?.send?.({ type: 'freq', freq: freq_hz });
    }

    // Change the mode (if specific) — with a short delay so the freq
    // change arrives first (the backend processes messages sequentially,
    // but the radio may need a moment between the CI-V freq and mode commands).
    const rigMode = _spotModeToRigMode(mode, freq_hz);
    if (rigMode) {
      setTimeout(() => {
        window.WS?.send?.({ type: 'mode', mode: rigMode });
      }, 150);
    }

    const mhz = (freq_hz / 1e6).toFixed(3);
    window.UI?.showToast?.(`✓ QSY: ${mhz} MHz${mode && mode !== '?' ? ' ('+mode+')' : ''}`, 'info');

    // Switch to the Radio tab so the user sees the freq/mode change.
    // setTimeout(0) so the scroll reset (added in setPage) doesn't collide
    // with rendering the toast above.
    setTimeout(() => { window.UI?.setPage?.('radio'); }, 100);
  }

  function clearSpots() {
    _spots = [];
    renderSpots();
  }

  // ── WS handlers ───────────────────────────────────────────────────────────
  function handleSpot(msg) {
    _spots.push(msg);
    if (_spots.length > _MAX_SPOTS) _spots.splice(0, _spots.length - _MAX_SPOTS);
    // Update the UI only if the tab is active (saves CPU)
    const page = document.getElementById('page-dxcluster');
    if (page && page.classList.contains('active')) {
      renderSpots();
    }
  }

  function handleStatus(msg) {
    _connected = msg.status === 'connected';
    updateStatusBadge(msg.status, msg.message);
  }

  function updateStatusBadge(status, message) {
    const badge = document.getElementById('dx-status-badge');
    const connectBtn = document.getElementById('dx-connect-btn');
    const disconnectBtn = document.getElementById('dx-disconnect-btn');
    if (!badge) return;

    if (status === 'connected') {
      badge.textContent = I18n.t('dx_connected');
      badge.style.color = 'var(--green)';
      badge.style.background = 'rgba(184,201,143,0.15)';
      if (connectBtn) connectBtn.style.display = 'none';
      if (disconnectBtn) disconnectBtn.style.display = '';
      // Hide the config panel after a successful connection (so the spot
      // list has more room — we don't force scrolling)
      const cfgPanel = document.getElementById('dx-config-panel');
      if (cfgPanel) cfgPanel.style.display = 'none';
    } else if (status === 'connecting') {
      badge.textContent = I18n.t('dx_connecting');
      badge.style.color = 'var(--amber)';
      badge.style.background = 'rgba(212,168,87,0.15)';
    } else if (status === 'error') {
      badge.textContent = I18n.t('dx_error_prefix') + (message || I18n.t('dx_unknown'));
      badge.style.color = 'var(--red)';
      badge.style.background = 'rgba(217,119,106,0.15)';
      if (connectBtn) connectBtn.style.display = '';
      if (disconnectBtn) disconnectBtn.style.display = 'none';
    } else {
      badge.textContent = I18n.t('dx_disconnected') + (message ? ' — ' + message : '');
      badge.style.color = 'var(--dim)';
      badge.style.background = 'var(--panel3)';
      if (connectBtn) connectBtn.style.display = '';
      if (disconnectBtn) disconnectBtn.style.display = 'none';
    }
  }

  // ── Utility ───────────────────────────────────────────────────────────────
  function _escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, m => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[m]));
  }

  // ── Init ──────────────────────────────────────────────────────────────────
  window.addEventListener('app:ready', () => {
    loadConfig();
  });

  // Auto-refresh the age every 30s so "5s ago" -> "1m ago" transitions update
  setInterval(() => {
    const page = document.getElementById('page-dxcluster');
    if (page && page.classList.contains('active')) renderSpots();
  }, 30000);

  // ── Sending a spot to the cluster ──────────────────────────────────────────

  function _spotHdr() {
    const token = localStorage.getItem('token') || sessionStorage.getItem('ham_token') || '';
    const h = { 'Content-Type': 'application/json' };
    if (token) h['Authorization'] = `Bearer ${token}`;
    return h;
  }

  function _spotError(msg) {
    const el = document.getElementById('dx-spot-error');
    if (!el) return;
    if (msg) { el.textContent = msg; el.style.display = 'block'; }
    else     { el.style.display = 'none'; }
  }

  // Update the preview of the command that will be sent to the cluster
  function _updateSpotPreview() {
    const call = (document.getElementById('dx-spot-call')?.value || '').trim().toUpperCase();
    const freq = (document.getElementById('dx-spot-freq')?.value || '').trim();
    const cmt  = (document.getElementById('dx-spot-comment')?.value || '').trim();
    const prev = document.getElementById('dx-spot-preview');
    if (!prev) return;
    let cmd = `DX ${freq || '?'} ${call || '?'}`;
    if (cmt) cmd += ` ${cmt}`;
    prev.textContent = cmd;
  }

  // Convert the radio's mode to the label used in spots.
  // The radio reports USB/LSB/CW/RTTY... and USB-D/LSB-D for digital
  // work. For a spot it makes more sense to give the real operating mode
  // (FT8 when on an FT8 frequency).
  function _guessSpotMode(freqHz, rigMode) {
    const m = String(rigMode || '').toUpperCase();
    const khz = freqHz / 1000;
    // Exact FT8/FT4 frequencies (kHz) - HF + VHF/UHF + microwave
    const FT8 = [1840, 3573, 5357, 7074, 10136, 14074, 18100, 21074, 24915, 28074,
                 50313, 50323, 70100, 70154, 144174, 222065, 432174,
                 1296174, 2320174, 3400174, 5760174, 10368174, 24048174];
    const FT4 = [3575, 7047.5, 10140, 14080, 18104, 21140, 24919, 28180,
                 50318, 144170];
    const near = (list) => list.some(f => Math.abs(khz - f) <= 2);
    if (m.includes('-D') || m === 'DATA' || m === 'PKT') {
      if (near(FT8)) return 'FT8';
      if (near(FT4)) return 'FT4';
      return 'DIGI';
    }
    if (m.startsWith('CW'))   return 'CW';
    if (m.startsWith('RTTY')) return 'RTTY';
    if (m === 'USB' || m === 'LSB') {
      if (near(FT8)) return 'FT8';   // someone forgot to switch to -D
      if (near(FT4)) return 'FT4';
      return 'SSB';
    }
    return m || '';
  }

  function openSpotDialog() {
    const modal = document.getElementById('dx-spot-modal');
    if (!modal) return;

    // Fill in the current frequency and the radio's mode
    const S = window.AppState || {};
    const freqHz = parseInt(S.freq, 10) || 0;
    const khz = freqHz ? (freqHz / 1000).toFixed(1).replace(/\.0$/, '') : '';
    const mode = _guessSpotMode(freqHz, S.mode);

    const fEl = document.getElementById('dx-spot-freq');
    const cEl = document.getElementById('dx-spot-call');
    const mEl = document.getElementById('dx-spot-comment');
    if (fEl) fEl.value = khz;
    if (cEl) cEl.value = '';
    if (mEl) mEl.value = mode ? `${mode} ` : '';   // trailing space - the user types the rest

    _spotError('');
    _updateSpotPreview();

    // Hook up the live preview (once)
    if (!modal.dataset.wired) {
      ['dx-spot-call', 'dx-spot-freq', 'dx-spot-comment'].forEach(id => {
        document.getElementById(id)?.addEventListener('input', _updateSpotPreview);
      });
      // Enter in the call/comment field = send
      ['dx-spot-call', 'dx-spot-comment'].forEach(id => {
        document.getElementById(id)?.addEventListener('keydown', e => {
          if (e.key === 'Enter') { e.preventDefault(); sendSpot(); }
        });
      });
      modal.dataset.wired = '1';
    }

    modal.classList.add('active');   // CSS: .modal-overlay.active { display:flex }
    setTimeout(() => cEl?.focus(), 50);   // put the cursor straight into the call field
  }

  function closeSpotDialog() {
    const modal = document.getElementById('dx-spot-modal');
    if (!modal) return;
    modal.classList.remove('active');
  }

  async function sendSpot() {
    const call = (document.getElementById('dx-spot-call')?.value || '').trim().toUpperCase();
    const freqKhz = (document.getElementById('dx-spot-freq')?.value || '').trim();
    const comment = (document.getElementById('dx-spot-comment')?.value || '').trim();

    // Client-side validation (the backend re-checks it anyway)
    if (!call || !/^[A-Z0-9/]{3,16}$/.test(call)) {
      _spotError(I18n.t('dx_spot_call_invalid'));
      return;
    }
    const khz = parseFloat(freqKhz);
    if (!khz || khz < 1800 || khz > 1300000) {
      _spotError(I18n.t('dx_spot_freq_invalid'));
      return;
    }
    _spotError('');

    const btn = document.getElementById('dx-spot-send-btn');
    if (btn) { btn.disabled = true; btn.textContent = I18n.t('dx_sending'); }

    try {
      const r = await fetch('/api/dxcluster/spot', {
        method: 'POST',
        headers: _spotHdr(),
        body: JSON.stringify({
          call,
          freq_hz: Math.round(khz * 1000),
          comment,
        }),
      });
      const res = await r.json();
      if (res.ok) {
        window.UI?.showToast?.(I18n.t('dx_spot_sent').replace('{call}', call).replace('{freq}', freqKhz), 'info');
        closeSpotDialog();
      } else {
        _spotError(res.error || I18n.t('dx_spot_send_fail'));
      }
    } catch (e) {
      _spotError(I18n.t('dx_spot_conn_error').replace('{msg}', e.message));
    } finally {
      if (btn) { btn.disabled = false; btn.textContent = I18n.t('dx_send_spot_modal_btn'); }
    }
  }

  return {
    loadConfig, save, connect, disconnect, sendCommand,
    renderSpots, qsy, clearSpots, toggleConfig,
    handleSpot, handleStatus,
    openSpotDialog, closeSpotDialog, sendSpot,
  };
})();
