/*
 * audio_autodetect.js — Auto-detekcja karty audio radia (admin, zakladka
 * Konfiguracja).
 *
 * Domyslnie serwer automatycznie wykrywa karte "USB Audio CODEC" (IC-7300,
 * IC-705) czy "SCU-17" (Yaesu) po nazwie. Ten modul pokazuje status
 * detekcji i pozwala:
 *   - odswiezyc detekcje (np. po podpięciu radia po starcie serwera)
 *   - przelaczyc w tryb "recznej" konfiguracji (ekspert) dla nietypowych radi
 */

window.AudioAutoDetect = (function() {
  let _expertMode = false;

  async function load() {
    try {
      const r = await fetch('/api/audio/detect', { credentials: 'include' });
      if (!r.ok) return;
      const data = await r.json();
      if (!data.ok) return;
      _render(data);
    } catch(e) { console.warn('[audio-detect] load blad:', e); }
  }

  function _render(data) {
    const info = document.getElementById('audio-detect-info');
    const badge = document.getElementById('audio-detect-badge');
    if (!info || !badge) return;
    badge.removeAttribute('data-i18n');  // patrz uwaga przy rot-status-badge (rotormini.js)

    const det = data.detection || {};
    const cur = data.current || {};

    if (det.detected) {
      badge.textContent = I18n.t('cfg_detected_badge');
      badge.style.color = 'var(--green)';
      badge.style.background = 'rgba(184,201,143,0.15)';
      info.style.borderLeftColor = 'var(--green2)';
      info.innerHTML = `
        <div style="color:var(--green);font-weight:600;margin-bottom:6px;">
          ${I18n.t('cfg_card_detected_msg').replace('{pattern}', '<code>' + _esc(det.pattern) + '</code>')}
        </div>
        <div style="color:var(--dim);font-size:10px;line-height:1.7;">
          <b style="color:var(--fg);">${I18n.t('cfg_rx_arrow_lbl')}</b> <code>${_esc(det.rx || '—')}</code><br>
          <b style="color:var(--fg);">${I18n.t('cfg_tx_arrow_lbl')}</b> <code>${_esc(det.tx || '—')}</code>
        </div>
        ${(cur.rxDevice && cur.rxDevice !== det.rx) || (cur.txDevice && cur.txDevice !== det.tx) ? `
          <div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border);color:var(--amber);font-size:10px;">
            ${I18n.t('cfg_other_cards_active')}
            RX=<code>${_esc(cur.rxDevice || '—')}</code>, TX=<code>${_esc(cur.txDevice || '—')}</code>
          </div>
        ` : ''}
      `;
    } else {
      badge.textContent = I18n.t('cfg_not_detected_badge');
      badge.style.color = 'var(--amber)';
      badge.style.background = 'rgba(212,168,87,0.15)';
      info.style.borderLeftColor = 'var(--amber)';
      const rxList = (det.all_rx || []).slice(0, 5);
      const txList = (det.all_tx || []).slice(0, 5);
      info.innerHTML = `
        <div style="color:var(--amber);font-weight:600;margin-bottom:6px;">
          ${I18n.t('cfg_not_detected_msg')}
        </div>
        <div style="color:var(--dim);font-size:10px;line-height:1.6;">
          ${I18n.t('cfg_not_detected_desc')}
        </div>
        <div style="margin-top:8px;padding-top:8px;border-top:1px solid var(--border);color:var(--dim);font-size:9px;">
          <b>${I18n.t('cfg_avail_capture_lbl')}</b> ${rxList.length ? rxList.map(x => `<code>${_esc(x)}</code>`).join(', ') : I18n.t('cfg_none_paren')}<br>
          <b>${I18n.t('cfg_avail_playback_lbl')}</b> ${txList.length ? txList.map(x => `<code>${_esc(x)}</code>`).join(', ') : I18n.t('cfg_none_paren')}
        </div>
      `;
    }
  }

  async function rescan() {
    const badge = document.getElementById('audio-detect-badge');
    if (badge) { badge.textContent = I18n.t('cfg_scanning_badge'); badge.style.color = 'var(--amber)'; }
    try {
      const r = await fetch('/api/audio/detect', {
        method: 'POST', credentials: 'include'
      });
      const data = await r.json();
      if (data.ok) {
        window.UI?.showToast?.(
          data.detection?.detected
            ? I18n.t('cfg_toast_detected').replace('{pattern}', data.detection.pattern)
            : I18n.t('cfg_toast_not_detected'),
          data.detection?.detected ? 'info' : 'warning'
        );
        await load();
      } else {
        window.UI?.showToast?.(I18n.t('cfg_toast_scan_error_prefix') + (data.error || ''), 'error');
      }
    } catch(e) {
      window.UI?.showToast?.(I18n.t('cfg_toast_network_error_prefix') + e.message, 'error');
    }
  }

  function toggleExpert() {
    _expertMode = !_expertMode;
    console.log('[audio-cfg] toggleExpert() ->', _expertMode);
    const el = document.getElementById('audio-manual-config');
    if (el) el.style.display = _expertMode ? '' : 'none';
    // Gdy pokazujemy tryb ekspert, zaladuj karty ORAZ zapisane wartosci
    // (rx/tx/bitrate/txVolume). Wczesniej wolano _audioDeviceRefresh, ktory
    // laduje TYLKO liste kart — nie ustawial zapisanych wartosci ani txVolume,
    // wiec suwak zostawal na HTML-owym 4x a karty na domyslnych. _audioDeviceLoad
    // robi jedno i drugie (w srodku i tak wola refresh).
    if (_expertMode && typeof window._audioDeviceLoad === 'function') {
      window._audioDeviceLoad();
    } else if (_expertMode && typeof window._audioDeviceRefresh === 'function') {
      window._audioDeviceRefresh();
    }
  }

  function _esc(s) {
    return String(s || '').replace(/[<>&"']/g, m => ({
      '<':'&lt;','>':'&gt;','&':'&amp;','"':'&quot;',"'":'&#39;'
    }[m]));
  }

  return { load, rescan, toggleExpert };
})();
