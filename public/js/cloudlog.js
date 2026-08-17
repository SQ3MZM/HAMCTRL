/**
 * cloudlog.js — integracja z CloudLog / WaveLog
 *
 * Dwa osobne API keys:
 *   1. cl-api-key-qso   → POST /index.php/api/qso  (logowanie QSO)
 *   2. cl-api-key-radio → POST /index.php/api/radio (live freq+mode co 30s)
 */
(function () {
'use strict';

const S = window.AppState;
let _liveInterval = null;
let _cfg = {};

// ── Naglowki z tokenem JWT ────────────────────────────────────────────────────
// WAZNE: wszystkie endpointy /api/cloudlog/* wymagaja autoryzacji w backendzie
// (if not user: return 401). Bez tokenu KAZDE wywolanie konczylo sie 401/404 —
// to byl powod dlaczego integracja z Cloudlogiem w ogole nie dzialala.
function _hdr(json = false) {
  const token = localStorage.getItem('token') || sessionStorage.getItem('ham_token') || '';
  const h = {};
  if (token) h['Authorization'] = `Bearer ${token}`;
  if (json)  h['Content-Type']  = 'application/json';
  return h;
}

// ── Laduj zapisana konfiguracje ───────────────────────────────────────────────
async function load() {
  try {
    const r = await fetch('/api/cloudlog/config', { headers: _hdr() });
    if (!r.ok) return;
    _cfg = await r.json();
    _applyToUI(_cfg);
    if (_cfg.liveEnabled) _startLive();
  } catch(e) {
    console.warn('[cloudlog] load error:', e);
  }
}

function _applyToUI(cfg) {
  const set = (id, val) => { const el = document.getElementById(id); if (el) el.value = val || ''; };
  set('cl-url',          cfg.url);
  set('cl-api-key-qso',  cfg.apiKeyQso);
  set('cl-api-key-radio',cfg.apiKeyRadio);
  set('cl-station-id',   cfg.stationId);
  const cb = document.getElementById('cl-live-enabled');
  if (cb) cb.checked = !!cfg.liveEnabled;
}

// ── Zapisz konfiguracje ───────────────────────────────────────────────────────
async function save() {
  _cfg = {
    url:          document.getElementById('cl-url')?.value.trim(),
    apiKeyQso:    document.getElementById('cl-api-key-qso')?.value.trim(),
    apiKeyRadio:  document.getElementById('cl-api-key-radio')?.value.trim(),
    stationId:    parseInt(document.getElementById('cl-station-id')?.value) || 1,
    liveEnabled:  document.getElementById('cl-live-enabled')?.checked || false,
  };
  try {
    await fetch('/api/cloudlog/config', {
      method: 'POST',
      headers: _hdr(true),
      body: JSON.stringify(_cfg),
    });
    window.UI?.showToast(I18n.t('toast_cloudlog_saved'));
    if (_cfg.liveEnabled) _startLive(); else _stopLive();
  } catch(e) {
    window.UI?.showToast(I18n.t('toast_cloudlog_save_error'), 'error');
  }
}

// ── Test polaczenia ───────────────────────────────────────────────────────────
async function test() {
  const url    = document.getElementById('cl-url')?.value.trim();
  const apiKey = document.getElementById('cl-api-key-qso')?.value.trim();
  if (!url || !apiKey) {
    _setStatus('error', I18n.t('cloudlog_fill_url_key'));
    return;
  }
  _setStatus('pending', I18n.t('status_checking'));
  try {
    const r = await fetch('/api/cloudlog/test', {
      method: 'POST',
      headers: _hdr(true),
      body: JSON.stringify({ url, apiKeyQso: apiKey }),
    });
    const res = await r.json();
    if (res.ok) {
      _setStatus('ok', res.message || I18n.t('cloudlog_connected_default'));
    } else {
      _setStatus('error', res.error || I18n.t('status_error_generic'));
    }
  } catch(e) {
    _setStatus('error', I18n.t('status_no_response'));
  }
}

// ── Status indicator (dwukolorowa kropka) ─────────────────────────────────────
function _setStatus(state, text) {
  const dot  = document.getElementById('cloudlog-status');
  const label = document.getElementById('cloudlog-status-text');
  if (dot) {
    dot.style.background  = state === 'ok'      ? 'var(--green)'
                          : state === 'error'   ? 'var(--red)'
                          : state === 'pending' ? 'var(--amber)'
                          : 'var(--dim)';
    dot.style.boxShadow   = state === 'ok'    ? '0 0 6px var(--green)'
                          : state === 'error' ? '0 0 6px var(--red)'
                          : 'none';
  }
  if (label) label.textContent = text || '';
}

// ── Live freq/mode (co 30s podczas nadawania lub zawsze jesli wlaczone) ──────
function _startLive() {
  _stopLive();
  _sendLive(); // natychmiast
  _liveInterval = setInterval(_sendLive, 5000);
}

function _stopLive() {
  if (_liveInterval) { clearInterval(_liveInterval); _liveInterval = null; }
}

async function _sendLive() {
  if (!_cfg.url || !_cfg.apiKeyRadio) return;
  const freq = S?.freq || 0;
  const mode = S?.mode || 'USB';
  try {
    await fetch('/api/cloudlog/radio', {
      method: 'POST',
      headers: _hdr(true),
      body: JSON.stringify({
        url:       _cfg.url,
        apiKey:    _cfg.apiKeyRadio,
        stationId: _cfg.stationId || 1,
        freq, mode,
      }),
    });
  } catch(e) {
    // cicho — nie przeszkadzaj uzytkownikowi bledem live update
  }
}

function setLive(enabled) {
  _cfg.liveEnabled = enabled;
  if (enabled) _startLive(); else _stopLive();
}

// ── Wyslij QSO do CloudLog ─────────────────────────────────────────────────────
async function logQso(qso) {
  if (!_cfg.url || !_cfg.apiKeyQso) return false;
  try {
    const r = await fetch('/api/cloudlog/qso', {
      method: 'POST',
      headers: _hdr(true),
      body: JSON.stringify({
        url:       _cfg.url,
        apiKey:    _cfg.apiKeyQso,
        stationId: _cfg.stationId || 1,
        qso,
      }),
    });
    const res = await r.json();
    if (res.ok) {
      window.UI?.showToast(I18n.t('toast_cloudlog_qso_sent'));
      return true;
    } else {
      window.UI?.showToast('✗ CloudLog: ' + (res.error || I18n.t('status_error_generic')), 'error');
      return false;
    }
  } catch(e) {
    window.UI?.showToast(I18n.t('toast_cloudlog_no_connection'), 'error');
    return false;
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
window.addEventListener('app:ready', () => { load(); });

window.CloudLog = { load, save, test, setLive, logQso };

})();
