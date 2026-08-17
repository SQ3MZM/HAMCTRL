/*
 * dxcluster.js — Klient DX Cluster (UI + WS handlers).
 *
 * Odbiera z serwera:
 *   {type: 'dx_spot', freq_hz, call, spotter, comment, utc, ts, band, mode}
 *   {type: 'dx_status', status: 'connecting'|'connected'|'disconnected'|'error', message}
 *
 * Wysyla do serwera:
 *   POST /api/dxcluster/config  — zapisz adres/login/haslo
 *   POST /api/dxcluster/connect — polacz
 *   POST /api/dxcluster/disconnect — rozlacz
 *   POST /api/dxcluster/command — dowolna komenda telnet
 *   GET  /api/dxcluster/config  — pobierz konfiguracje
 *   GET  /api/dxcluster/history — pobierz cache ostatnich spotow
 */

window.DXCluster = (function() {
  let _spots = [];             // wszystkie odebrane spoty w tej sesji
  const _MAX_SPOTS = 500;      // limit w pamieci
  let _connected = false;

  // Pokaz/ukryj panel konfiguracji
  function toggleConfig() {
    const panel = document.getElementById('dx-config-panel');
    if (!panel) return;
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
  }

  // ── Konfiguracja ─────────────────────────────────────────────────────────
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
      // Hasla nie zwracamy - placeholder pokazuje ze jest zapisane
      const pwEl = document.getElementById('dx-password');
      if (pwEl) pwEl.placeholder = cfg.has_password ? I18n.t('dx_password_saved_ph') : I18n.t('dx_optional_ph');
      document.getElementById('dx-auto-connect').checked = !!cfg.auto_connect;

      // Auto-pokaz konfiguracje jesli nie ma adresu (pierwszy raz)
      if (!cfg.host) {
        const panel = document.getElementById('dx-config-panel');
        if (panel) panel.style.display = 'block';
      }

      // Wczytaj historie z cache serwera (spoty z tej sesji nawet po odswiezeniu)
      if (data.history && data.history.length) {
        _spots = data.history.slice();
        renderSpots();
      }

      // Zaktualizuj status
      _connected = !!data.connected;
      updateStatusBadge(_connected ? 'connected' : 'disconnected', '');
    } catch(e) { console.warn('[dx] loadConfig blad:', e); }
  }

  async function save() {
    const status = document.getElementById('dx-save-status');
    if (status) { status.textContent = I18n.t('dx_saving'); status.style.color = 'var(--dim)'; }
    const password = document.getElementById('dx-password').value;
    const payload = {
      host: document.getElementById('dx-host').value.trim(),
      port: parseInt(document.getElementById('dx-port').value, 10),
      login: document.getElementById('dx-login').value.trim(),
      // Jesli pole hasla puste, wysylamy null - serwer zachowa poprzednie
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
        // Wyczyść pole hasła (nie chcemy go trzymać w DOM)
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
    // Zapisz konfiguracje przed polaczeniem, jesli sa niezapisane zmiany
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

  // ── Renderowanie spotow ──────────────────────────────────────────────────
  function renderSpots() {
    const tbody = document.getElementById('dx-spot-list');
    if (!tbody) return;
    const fBand = document.getElementById('dx-filter-band')?.value || '';
    const fMode = document.getElementById('dx-filter-mode')?.value || '';
    const fCall = (document.getElementById('dx-filter-call')?.value || '').toUpperCase().trim();

    // Najnowsze na gorze
    const filtered = _spots.slice().reverse().filter(s => {
      if (fBand && s.band !== fBand) return false;
      if (fMode && s.mode !== fMode) return false;
      if (fCall && !s.call.includes(fCall) && !s.spotter.includes(fCall)) return false;
      return true;
    });

    // Update licznika - pokaz "widoczne / lacznie" bo renderujemy tylko 30
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

    // Kolory pasm dla lepszej czytelnosci
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

  // QSY do czestotliwosci spota + ustaw tryb (jesli znany).
  // ── Konwersja trybu ze spota na tryb radia (IC-7300) ──────────────────────
  //
  // Spot z klastra podaje tryb "operacyjny" (SSB, FT8, CW, RTTY...), a radio
  // rozumie tylko LSB/USB/CW/RTTY/AM/FM. Trzeba przetlumaczyc — zwlaszcza
  // wybrac WLASCIWA WSTEGE dla SSB i trybow cyfrowych.
  //
  // Konwencja amatorska (IARU):
  //   - ponizej 10 MHz  -> LSB  (160m, 80m, 40m)
  //   - powyzej 10 MHz  -> USB  (20m, 17m, 15m, 12m, 10m, 6m i wyzej)
  //   - 60m (5.3 MHz)   -> USB  (wyjatek! kanaly 60m sa zawsze USB)
  //   - tryby cyfrowe   -> zawsze USB (niezaleznie od pasma)
  //
  // Zwraca null gdy nie ma sensu zmieniac trybu (nieznany / '?').
  function _spotModeToRigMode(mode, freq_hz) {
    const m = String(mode || '').trim().toUpperCase();
    if (!m || m === '?') return null;

    // Wstega dla fonii: <10 MHz = LSB, >=10 MHz = USB.
    // Wyjatek 60m (5250-5450 kHz) - miedzynarodowo zawsze USB.
    const is60m = freq_hz >= 5_250_000 && freq_hz <= 5_450_000;
    const phoneSideband = (freq_hz < 10_000_000 && !is60m) ? 'LSB' : 'USB';

    // Tryby cyfrowe: ZAWSZE USB (tak jest w standardzie, tez na 40m/80m).
    // Radio moze byc w USB albo USB-D - oba dzialaja, USB-D lepsze (filtr).
    const DIGI = ['FT8', 'FT4', 'RTTY', 'PSK', 'PSK31', 'JT65', 'JT9',
                  'DIGI', 'DATA', 'MFSK', 'OLIVIA', 'JS8', 'WSPR',
                  'MSK144', 'Q65'];
    if (DIGI.includes(m)) return 'USB';

    // SAT - praca satelitarna. Nie zmieniamy trybu automatycznie, bo zalezy
    // od satelity (FM/SSB/CW) i strony lacza (uplink/downlink). Operator
    // ustawia sam.
    if (m === 'SAT') return null;

    // Fonia -> wlasciwa wstega wg pasma
    if (m === 'SSB' || m === 'PHONE' || m === 'LSB' || m === 'USB') {
      // Jesli spot jawnie mowi LSB/USB - uszanuj to (spotter wiedzial lepiej).
      // Ale gdy mowi ogolnie "SSB" - wybierz wstege po czestotliwosci.
      if (m === 'LSB' || m === 'USB') return m;
      return phoneSideband;
    }

    // CW, AM, FM - bez zmian (radio zna te tryby)
    if (m === 'CW' || m === 'CWR' || m === 'CW-R') return 'CW';
    if (m === 'AM') return 'AM';
    if (m === 'FM' || m === 'NFM' || m === 'WFM') return 'FM';

    // Nieznany tryb - nie ruszaj radia (lepiej zostawic jak jest)
    return null;
  }

  // WAZNE: uzywamy window.UI.sendFreq (sprawdzony mechanizm) zamiast bezposredniego
  // WS.send - dzieki temu QSY zachowuje sie tak samo jak reczne strojenie z
  // panelu Radio (sprawdzanie locka, S.freq update, updateFreqDisplay itp.).
  // Wczesniej byl bezposredni WS.send({type:'freq',freq}) ktore omijalo
  // logike S.freq/updateFreqDisplay i nie zawsze dawalo widoczna zmiane.
  function qsy(freq_hz, mode) {
    // Zabezpieczenie przed nieprawidlowymi argumentami z HTML onclick
    freq_hz = parseInt(freq_hz, 10);
    if (!freq_hz || freq_hz < 100000) {
      window.UI?.showToast?.(I18n.t('dx_invalid_freq'), 'error');
      return;
    }

    // Sprawdz czy user moze sterowac radiem (informacyjny toast przed
    // wyslaniem, bo bez tego UI.sendFreq tez pokaze toast ale klarowniej
    // objasnimy z zakladki DX Cluster).
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

    // Uzyj sprawdzonego UI.sendFreq — wywoluje _canControlRadio, aktualizuje
    // S.freq, updateFreqDisplay, updateVFOBadges, scheduleBandMemorySave i
    // wysyla WS. To DOKLADNIE to co robi klik na "Radio" tab przy strojeniu.
    if (typeof window.UI?.sendFreq === 'function') {
      window.UI.sendFreq(freq_hz);
    } else {
      // Fallback jesli UI.sendFreq niedostepne (nie powinno sie zdarzyc)
      window.WS?.send?.({ type: 'freq', freq: freq_hz });
    }

    // Zmien tryb (jesli konkretny) — z krotkim opoznieniem zeby zmiana freq
    // dotarla najpierw (backend przetwarza wiadomosci sekwencyjnie ale radio
    // moze potrzebowac chwili miedzy CI-V freq i mode command).
    const rigMode = _spotModeToRigMode(mode, freq_hz);
    if (rigMode) {
      setTimeout(() => {
        window.WS?.send?.({ type: 'mode', mode: rigMode });
      }, 150);
    }

    const mhz = (freq_hz / 1e6).toFixed(3);
    window.UI?.showToast?.(`✓ QSY: ${mhz} MHz${mode && mode !== '?' ? ' ('+mode+')' : ''}`, 'info');

    // Przelacz na zakladke Radio zeby uzytkownik widzial zmianie freq/mode.
    // setTimeout(0) zeby scroll reset (dodany w setPage) nie skolidowal z
    // renderowaniem toasta powyzej.
    setTimeout(() => { window.UI?.setPage?.('radio'); }, 100);
  }

  function clearSpots() {
    _spots = [];
    renderSpots();
  }

  // ── WS handlery ─────────────────────────────────────────────────────────
  function handleSpot(msg) {
    _spots.push(msg);
    if (_spots.length > _MAX_SPOTS) _spots.splice(0, _spots.length - _MAX_SPOTS);
    // Aktualizuj UI tylko jesli zakladka aktywna (oszczedza CPU)
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
      // Ukryj panel konfiguracji po udanym polaczeniu (zeby lista spotow
      // miala wiecej miejsca — nie zmuszamy do scrollowania)
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

  // ── Utility ─────────────────────────────────────────────────────────────
  function _escapeHtml(s) {
    return String(s || '').replace(/[&<>"']/g, m => ({
      '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
    }[m]));
  }

  // ── Init ────────────────────────────────────────────────────────────────
  window.addEventListener('app:ready', () => {
    loadConfig();
  });

  // Auto-refresh age co 30s zeby stopnie "5s ago" -> "1m ago" aktualizowaly sie
  setInterval(() => {
    const page = document.getElementById('page-dxcluster');
    if (page && page.classList.contains('active')) renderSpots();
  }, 30000);

  // ── Wysylanie spota na klaster ─────────────────────────────────────────────

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

  // Aktualizuj podglad komendy jaka poleci na klaster
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

  // Zamien tryb radia na etykiete uzywana w spotach.
  // Radio raportuje USB/LSB/CW/RTTY... a przy pracy cyfrowej USB-D/LSB-D.
  // Dla spota sensowniej podac realny tryb pracy (FT8 gdy na czestotliwosci FT8).
  function _guessSpotMode(freqHz, rigMode) {
    const m = String(rigMode || '').toUpperCase();
    const khz = freqHz / 1000;
    // Dokladne czestotliwosci FT8/FT4 (kHz) - HF + VHF/UHF + mikrofale
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
      if (near(FT8)) return 'FT8';   // ktos zapomnial przelaczyc na -D
      if (near(FT4)) return 'FT4';
      return 'SSB';
    }
    return m || '';
  }

  function openSpotDialog() {
    const modal = document.getElementById('dx-spot-modal');
    if (!modal) return;

    // Wypelnij aktualna czestotliwoscia i trybem radia
    const S = window.AppState || {};
    const freqHz = parseInt(S.freq, 10) || 0;
    const khz = freqHz ? (freqHz / 1000).toFixed(1).replace(/\.0$/, '') : '';
    const mode = _guessSpotMode(freqHz, S.mode);

    const fEl = document.getElementById('dx-spot-freq');
    const cEl = document.getElementById('dx-spot-call');
    const mEl = document.getElementById('dx-spot-comment');
    if (fEl) fEl.value = khz;
    if (cEl) cEl.value = '';
    if (mEl) mEl.value = mode ? `${mode} ` : '';   // spacja - user dopisze resztę

    _spotError('');
    _updateSpotPreview();

    // Podepnij live-podglad (raz)
    if (!modal.dataset.wired) {
      ['dx-spot-call', 'dx-spot-freq', 'dx-spot-comment'].forEach(id => {
        document.getElementById(id)?.addEventListener('input', _updateSpotPreview);
      });
      // Enter w polu znaku/komentarza = wyslij
      ['dx-spot-call', 'dx-spot-comment'].forEach(id => {
        document.getElementById(id)?.addEventListener('keydown', e => {
          if (e.key === 'Enter') { e.preventDefault(); sendSpot(); }
        });
      });
      modal.dataset.wired = '1';
    }

    modal.classList.add('active');   // CSS: .modal-overlay.active { display:flex }
    setTimeout(() => cEl?.focus(), 50);   // kursor od razu w polu znaku
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

    // Walidacja po stronie klienta (backend i tak sprawdza ponownie)
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
