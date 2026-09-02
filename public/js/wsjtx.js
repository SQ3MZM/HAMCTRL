/**
 * wsjtx.js — WSJT-X frontend (FT8/FT4/JT65 monitor + personal QSO log)
 * Each user sees and logs only their own contacts.
 */
(function() {
'use strict';

const S = window.AppState;

// ── State ─────────────────────────────────────────────────────────────────────
let _decodes     = [];
let _allDecodes  = [];
let _myCall      = '';
let _workedCalls = new Set(); // CALL|BAND|MODE keys from the QSO log — each band/mode is a separate QSO
let _hideWorked  = false;     // filter — hide already-worked ones

function _workedKey(call, band, mode) {
  return `${(call||'').toUpperCase()}|${(band||'').toUpperCase()}|${(mode||'').toUpperCase()}`;
}

// Load callsigns from the log (every 60s and on startup). Uses
// /api/qsolog/calls (SELECT DISTINCT call+band+mode, no cap on the
// number of entries) instead of /api/qsolog, which has a hard per<=200
// limit on the backend — with a larger log, older QSOs used to
// disappear from the worked-marking. Band+mode are in the key, because
// "every band (and FT8 vs FT4) is a new QSO": a station worked on 40m
// shouldn't gray out on 20m.
async function _loadWorkedCalls() {
  try {
    const token = localStorage.getItem('token') || '';
    const r = await fetch('/api/qsolog/calls', {
      headers: token ? {'Authorization': `Bearer ${token}`} : {}
    });
    const d = await r.json();
    _workedCalls = new Set((d.calls || []).map(q => _workedKey(q.call, q.band, q.mode)));
  } catch(e) {}
}
setInterval(_loadWorkedCalls, 60000);

// Whether a callsign has already been worked on the CURRENT band/mode (not globally).
function _isWorkedHere(call) {
  if (!call) return false;
  const band = window.UI?.getBandName ? window.UI.getBandName(S.freq) : '';
  return _workedCalls.has(_workedKey(call, band, _decodeMode));
}
let _myGrid      = '';
let _status      = { running: false, transmit: false, decoding: false };
let _clockTimer  = null;
let _decodeCount = 0;
let _miniLogEntries = [];   // recent QSOs from the real log (qso_db, /api/qsolog) - preview under the automation
const MINI_LOG_MAX = 8;
const MAX_DECODES = 300;

// ── Country of the calling station (the "Country" column in Band Activity / RX Frequency) ─
// The column header had been in the HTML for a while, but nothing ever
// filled it in - _decodeRowHtml rendered <span class="wj-d-country"></span>
// always empty. A prefix heuristic (not the full/exact DXCC - that would
// need an external database of prefix ranges with exceptions), similar to
// prefixToLatLon() in beamheading.js (same idea: try the longest prefix
// first), but a separate table - here we need a name+flag, not coordinates.
const _PREFIX_COUNTRY = {
  // Europe
  'SP':['Polska','PL'], 'SQ':['Polska','PL'], 'SN':['Polska','PL'], 'SO':['Polska','PL'], 'SR':['Polska','PL'], 'HF':['Polska','PL'], '3Z':['Polska','PL'],
  'DL':['Niemcy','DE'], 'DK':['Niemcy','DE'], 'DJ':['Niemcy','DE'], 'DF':['Niemcy','DE'], 'DB':['Niemcy','DE'], 'DA':['Niemcy','DE'], 'DC':['Niemcy','DE'], 'DD':['Niemcy','DE'], 'DG':['Niemcy','DE'], 'DH':['Niemcy','DE'], 'DM':['Niemcy','DE'], 'DO':['Niemcy','DE'],
  'G':['Anglia','GB'], 'M':['Anglia','GB'], '2E':['Anglia','GB'],
  'GW':['Walia','GB'], 'MW':['Walia','GB'], 'GM':['Szkocja','GB'], 'MM':['Szkocja','GB'],
  'GI':['Irlandia Płn.','GB'], 'MI':['Irlandia Płn.','GB'], 'GD':['Wyspa Man','GB'], 'GJ':['Jersey','GB'], 'GU':['Guernsey','GB'],
  'F':['Francja','FR'], 'TM':['Francja','FR'], 'TO':['Francja','FR'],
  'ON':['Belgia','BE'], 'OO':['Belgia','BE'], 'OP':['Belgia','BE'], 'OQ':['Belgia','BE'], 'OR':['Belgia','BE'], 'OS':['Belgia','BE'], 'OT':['Belgia','BE'],
  'PA':['Holandia','NL'], 'PB':['Holandia','NL'], 'PC':['Holandia','NL'], 'PD':['Holandia','NL'], 'PE':['Holandia','NL'], 'PF':['Holandia','NL'], 'PG':['Holandia','NL'], 'PH':['Holandia','NL'], 'PI':['Holandia','NL'],
  'LX':['Luksemburg','LU'],
  'EA':['Hiszpania','ES'], 'EB':['Hiszpania','ES'], 'EC':['Hiszpania','ES'], 'ED':['Hiszpania','ES'], 'EE':['Hiszpania','ES'], 'EF':['Hiszpania','ES'], 'EG':['Hiszpania','ES'], 'EH':['Hiszpania','ES'],
  'CT':['Portugalia','PT'], 'CQ':['Portugalia','PT'], 'CS':['Portugalia','PT'], 'CR':['Portugalia','PT'],
  'I':['Włochy','IT'], 'IK':['Włochy','IT'], 'IZ':['Włochy','IT'], 'IW':['Włochy','IT'], 'IU':['Włochy','IT'], 'IN':['Włochy','IT'], 'IQ':['Włochy','IT'], 'IR':['Włochy','IT'], 'IT':['Włochy','IT'], 'IS':['Sardynia','IT'], 'IB':['Baleary','ES'],
  'HB':['Szwajcaria','CH'], 'HE':['Szwajcaria','CH'],
  'OE':['Austria','AT'],
  'OK':['Czechy','CZ'], 'OL':['Czechy','CZ'],
  'OM':['Słowacja','SK'],
  'HA':['Węgry','HU'], 'HG':['Węgry','HU'],
  'YO':['Rumunia','RO'], 'YP':['Rumunia','RO'], 'YQ':['Rumunia','RO'], 'YR':['Rumunia','RO'],
  'LZ':['Bułgaria','BG'],
  'YU':['Serbia','RS'], 'YT':['Serbia','RS'],
  'S5':['Słowenia','SI'], '9A':['Chorwacja','HR'], 'E7':['Bośnia i Hercegowina','BA'],
  'Z3':['Macedonia Płn.','MK'], 'Z6':['Kosowo','XK'], '4O':['Czarnogóra','ME'],
  'SV':['Grecja','GR'], 'SY':['Grecja','GR'], 'SZ':['Grecja','GR'], 'SW':['Grecja','GR'], 'SX':['Grecja','GR'],
  'TA':['Turcja','TR'], 'TB':['Turcja','TR'], 'TC':['Turcja','TR'],
  'ZA':['Albania','AL'],
  'OH':['Finlandia','FI'], 'OF':['Finlandia','FI'], 'OG':['Finlandia','FI'], 'OI':['Finlandia','FI'], 'OJ':['Wyspy Alandzkie','AX'],
  'SM':['Szwecja','SE'], 'SA':['Szwecja','SE'], 'SB':['Szwecja','SE'], 'SC':['Szwecja','SE'], 'SD':['Szwecja','SE'], 'SE':['Szwecja','SE'], 'SF':['Szwecja','SE'], 'SG':['Szwecja','SE'], 'SH':['Szwecja','SE'], 'SI':['Szwecja','SE'], 'SJ':['Szwecja','SE'], 'SK':['Szwecja','SE'], 'SL':['Szwecja','SE'],
  'LA':['Norwegia','NO'], 'LB':['Norwegia','NO'], 'LJ':['Norwegia','NO'], 'LN':['Norwegia','NO'],
  'OZ':['Dania','DK'], '5P':['Dania','DK'], '5Q':['Dania','DK'], 'OU':['Dania','DK'], 'OV':['Dania','DK'],
  'OY':['Wyspy Owcze','FO'], 'TF':['Islandia','IS'],
  'ES':['Estonia','EE'], 'YL':['Łotwa','LV'], 'LY':['Litwa','LT'],
  'EW':['Białoruś','BY'], 'EU':['Białoruś','BY'], 'EV':['Białoruś','BY'],
  'UR':['Ukraina','UA'], 'UT':['Ukraina','UA'], 'UX':['Ukraina','UA'], 'US':['Ukraina','UA'], 'UY':['Ukraina','UA'], 'UZ':['Ukraina','UA'], 'EM':['Ukraina','UA'], 'EN':['Ukraina','UA'], 'EO':['Ukraina','UA'],
  'ER':['Mołdawia','MD'],
  'EI':['Irlandia','IE'], 'EJ':['Irlandia','IE'],
  'C3':['Andora','AD'], '3A':['Monako','MC'], '4U':['Watykan/UN','VA'], 'ZB':['Gibraltar','GI'], '9H':['Malta','MT'], '5B':['Cypr','CY'], 'ZC':['Cypr (bryt.)','CY'],
  'R':['Rosja (Europ.)','RU'], 'UA':['Rosja (Europ.)','RU'], 'RA':['Rosja (Europ.)','RU'], 'RK':['Rosja (Europ.)','RU'], 'RN':['Rosja (Europ.)','RU'], 'RV':['Rosja (Europ.)','RU'], 'RW':['Rosja (Europ.)','RU'], 'RX':['Rosja (Europ.)','RU'], 'RZ':['Rosja (Europ.)','RU'], 'RC':['Rosja (Europ.)','RU'], 'RD':['Rosja (Europ.)','RU'], 'RG':['Rosja (Europ.)','RU'], 'RJ':['Rosja (Europ.)','RU'], 'RL':['Rosja (Europ.)','RU'], 'RM':['Rosja (Europ.)','RU'], 'RO':['Rosja (Europ.)','RU'], 'RP':['Rosja (Europ.)','RU'], 'RQ':['Rosja (Europ.)','RU'], 'RT':['Rosja (Europ.)','RU'], 'RU':['Rosja (Europ.)','RU'],
  'UA9':['Rosja (Azja)','RU'], 'RA9':['Rosja (Azja)','RU'], 'UA0':['Rosja (Azja)','RU'], 'RA0':['Rosja (Azja)','RU'], 'R9':['Rosja (Azja)','RU'], 'R0':['Rosja (Azja)','RU'],
  '4X':['Izrael','IL'], '4Z':['Izrael','IL'],
  // N. Africa/Middle East
  'CN':['Maroko','MA'], 'SU':['Egipt','EG'], '3V':['Tunezja','TN'], '7X':['Algieria','DZ'], '5A':['Libia','LY'],
  'A4':['Oman','OM'], 'A6':['ZEA','AE'], 'A7':['Katar','QA'], 'A9':['Bahrajn','BH'], '9K':['Kuwejt','KW'], 'HZ':['Arabia Saudyjska','SA'], '7Z':['Arabia Saudyjska','SA'],
  // Sub-Saharan Africa
  'EL':['Liberia','LR'], '5N':['Nigeria','NG'], 'TR':['Gabon','GA'], '9J':['Zambia','ZM'], 'ZS':['RPA','ZA'], 'ZR':['RPA','ZA'], 'ZT':['RPA','ZA'], 'ZU':['RPA','ZA'], '5H':['Tanzania','TZ'], '5Z':['Kenia','KE'], '5X':['Uganda','UG'], '7P':['Lesotho','LS'], 'V5':['Namibia','NA'], 'C9':['Mozambik','MZ'],
  // North America
  'W':['USA','US'], 'K':['USA','US'], 'N':['USA','US'], 'AA':['USA','US'], 'AB':['USA','US'], 'AC':['USA','US'], 'AD':['USA','US'], 'AE':['USA','US'], 'AF':['USA','US'], 'AG':['USA','US'], 'AI':['USA','US'], 'AJ':['USA','US'], 'AK':['USA','US'],
  'KL':['Alaska','US'], 'KL7':['Alaska','US'], 'NL7':['Alaska','US'], 'KH6':['Hawaje','US'], 'NH6':['Hawaje','US'],
  'VE':['Kanada','CA'], 'VA':['Kanada','CA'], 'VO':['Kanada','CA'], 'VY':['Kanada','CA'], 'CF':['Kanada','CA'], 'CG':['Kanada','CA'], 'CJ':['Kanada','CA'], 'CK':['Kanada','CA'],
  'XE':['Meksyk','MX'], 'XF':['Meksyk','MX'],
  // Central America/Caribbean
  'CO':['Kuba','CU'], 'CM':['Kuba','CU'], 'HI':['Dominikana','DO'], 'KP4':['Puerto Rico','PR'], 'NP4':['Puerto Rico','PR'], 'V2':['Antigua','AG'], '8P':['Barbados','BB'], 'J3':['Grenada','GD'], '6Y':['Jamajka','JM'], 'TG':['Gwatemala','GT'], 'TI':['Kostaryka','CR'], 'HP':['Panama','PA'], 'YN':['Nikaragua','NI'], 'HR':['Honduras','HN'], 'YS':['Salwador','SV'],
  // South America
  'PY':['Brazylia','BR'], 'PP':['Brazylia','BR'], 'PQ':['Brazylia','BR'], 'PR':['Brazylia','BR'], 'PS':['Brazylia','BR'], 'PT':['Brazylia','BR'], 'PU':['Brazylia','BR'], 'PV':['Brazylia','BR'], 'PW':['Brazylia','BR'], 'ZV':['Brazylia','BR'], 'ZW':['Brazylia','BR'], 'ZX':['Brazylia','BR'], 'ZY':['Brazylia','BR'], 'ZZ':['Brazylia','BR'],
  'LU':['Argentyna','AR'], 'LO':['Argentyna','AR'], 'LP':['Argentyna','AR'], 'LQ':['Argentyna','AR'], 'LR':['Argentyna','AR'], 'LS':['Argentyna','AR'], 'LT':['Argentyna','AR'], 'LV':['Argentyna','AR'], 'LW':['Argentyna','AR'],
  'CE':['Chile','CL'], 'CA':['Chile','CL'], 'CB':['Chile','CL'], 'CC':['Chile','CL'], 'CD':['Chile','CL'], 'XQ':['Chile','CL'], 'XR':['Chile','CL'],
  'HK':['Kolumbia','CO'], 'HJ':['Kolumbia','CO'],
  'YV':['Wenezuela','VE'], 'YW':['Wenezuela','VE'], 'YX':['Wenezuela','VE'],
  'OA':['Peru','PE'], 'OB':['Peru','PE'], 'OC':['Peru','PE'],
  'CP':['Boliwia','BO'], 'HC':['Ekwador','EC'], 'HD':['Ekwador','EC'], 'CX':['Urugwaj','UY'], 'CV':['Urugwaj','UY'], 'ZP':['Paragwaj','PY'],
  // Asia
  'JA':['Japonia','JP'], 'JE':['Japonia','JP'], 'JF':['Japonia','JP'], 'JG':['Japonia','JP'], 'JH':['Japonia','JP'], 'JI':['Japonia','JP'], 'JJ':['Japonia','JP'], 'JK':['Japonia','JP'], 'JL':['Japonia','JP'], 'JM':['Japonia','JP'], 'JN':['Japonia','JP'], 'JO':['Japonia','JP'], 'JP':['Japonia','JP'], 'JQ':['Japonia','JP'], 'JR':['Japonia','JP'], 'JS':['Japonia','JP'], '7J':['Japonia','JP'], '7K':['Japonia','JP'], '7L':['Japonia','JP'], '7M':['Japonia','JP'], '7N':['Japonia','JP'], '8J':['Japonia','JP'],
  'HL':['Korea Płd.','KR'], 'DS':['Korea Płd.','KR'], '6K':['Korea Płd.','KR'], '6L':['Korea Płd.','KR'], '6M':['Korea Płd.','KR'], '6N':['Korea Płd.','KR'],
  'BY':['Chiny','CN'], 'BA':['Chiny','CN'], 'BD':['Chiny','CN'], 'BG':['Chiny','CN'], 'BH':['Chiny','CN'], 'BI':['Chiny','CN'],
  'BV':['Tajwan','TW'],
  'VU':['Indie','IN'], 'AT':['Indie','IN'], 'AU':['Indie','IN'], 'AV':['Indie','IN'], 'AW':['Indie','IN'],
  'YB':['Indonezja','ID'], 'YC':['Indonezja','ID'], 'YD':['Indonezja','ID'], 'YE':['Indonezja','ID'], 'YF':['Indonezja','ID'], 'YG':['Indonezja','ID'], 'YH':['Indonezja','ID'],
  'EX':['Kirgistan','KG'], 'UN':['Kazachstan','KZ'], 'EY':['Tadżykistan','TJ'], 'EZ':['Turkmenistan','TM'], 'UK':['Uzbekistan','UZ'], 'EK':['Armenia','AM'], '4J':['Azerbejdżan','AZ'], '4L':['Gruzja','GE'],
  'HS':['Tajlandia','TH'], 'E2':['Tajlandia','TH'], '9M':['Malezja','MY'], '9V':['Singapur','SG'], 'XV':['Wietnam','VN'], '3W':['Wietnam','VN'], 'XU':['Kambodża','KH'], 'XW':['Laos','LA'], 'XZ':['Mjanma','MM'],
  'DU':['Filipiny','PH'], 'DV':['Filipiny','PH'], 'DW':['Filipiny','PH'], 'DX':['Filipiny','PH'], 'DY':['Filipiny','PH'], 'DZ':['Filipiny','PH'], '4D':['Filipiny','PH'],
  '9M2':['Malezja Zach.','MY'], '9M6':['Malezja Wsch.','MY'],
  // Oceania
  'VK':['Australia','AU'], 'VH':['Australia','AU'], 'VI':['Australia','AU'], 'VJ':['Australia','AU'], 'VL':['Australia','AU'], 'VN':['Australia','AU'], 'VZ':['Australia','AU'],
  'ZL':['Nowa Zelandia','NZ'], 'ZK':['Nowa Zelandia','NZ'], 'ZM':['Nowa Zelandia','NZ'],
  'KH0':['Mariany Płn.','MP'], 'KH2':['Guam','GU'],
  // S. Africa/other
  'ZS':['RPA','ZA'],
};

// Flag from an ISO-3166 alpha-2 code (Unicode regional indicators) - an
// algorithm, not an image database, so no need to keep separate files/emoji per country.
function _isoToFlag(iso2) {
  if (!iso2 || iso2.length !== 2) return '';
  const codePoints = [...iso2.toUpperCase()].map(c => 127397 + c.charCodeAt(0));
  return String.fromCodePoint(...codePoints);
}

function _countryForCall(call) {
  const c = (call || '').trim().toUpperCase();
  if (!c) return null;
  const base = c.includes('/')
      ? c.split('/').reduce((a, b) => (a.length <= b.length ? a : b))
      : c;
  for (let len = 3; len >= 1; len--) {
    const p = base.slice(0, len);
    if (_PREFIX_COUNTRY[p]) {
      const [name, iso2] = _PREFIX_COUNTRY[p];
      return { name, flag: _isoToFlag(iso2) };
    }
  }
  return null;
}

// "Country" column display mode - DELIBERATELY only one option at a time
// (flag OR name), never both at once - a toggle in the Band Activity and
// RX Frequency headers (the same global state, two .wj-country-mode-btn
// buttons so it works from both panels).
let _countryMode = 'flag'; // 'flag' | 'name'

function toggleCountryMode() {
  _countryMode = _countryMode === 'flag' ? 'name' : 'flag';
  document.querySelectorAll('.wj-country-mode-btn').forEach(b => {
    b.textContent = _countryMode === 'flag' ? '🏳️' : 'Aa';
    b.title = _countryMode === 'flag'
      ? 'Kolumna Kraj: flaga (klik: przelacz na nazwe)'
      : 'Kolumna Kraj: nazwa (klik: przelacz na flage)';
  });
  _renderDecodes();
  _renderRxFreqPanel();
}

// QSO automation (UI state, the source of truth is on the backend —
// synced via WS auto_seq_status/auto_qso_status)
let _autoSeqEnabled = false;
let _autoQsoState = 'IDLE';
let _autoQsoPartner = null;

// ── 15s clock ─────────────────────────────────────────────────────────────────
function _startClock() {
  if (_clockTimer) clearInterval(_clockTimer);
  _clockTimer = setInterval(_updateClock, 200);
}

function _updateClock() {
  const now  = new Date();
  const s    = now.getUTCSeconds() % 15;
  const ms   = now.getUTCMilliseconds();
  const frac = (s * 1000 + ms) / 15000;
  const canvas = document.getElementById('wj-clock');
  if (canvas) {
    const ctx = canvas.getContext('2d');
    const cx = 18, cy = 18, r = 14;
    ctx.clearRect(0, 0, 36, 36);
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI*2);
    ctx.fillStyle = '#111'; ctx.fill();
    ctx.strokeStyle = '#222'; ctx.lineWidth = 1; ctx.stroke();
    ctx.beginPath(); ctx.moveTo(cx, cy);
    ctx.arc(cx, cy, r, -Math.PI/2, -Math.PI/2 + frac * 2 * Math.PI);
    ctx.closePath();
    ctx.fillStyle = _status.transmit ? '#c33' : (s < 8 ? '#1a4a2a' : '#1a2a4a');
    ctx.fill();
    ctx.beginPath(); ctx.arc(cx, cy, 2, 0, Math.PI*2);
    ctx.fillStyle = '#fff'; ctx.fill();
  }
  const el = document.getElementById('wj-clock-s');
  if (el) el.textContent = s;
}

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  _myCall = S?.callsign || window.CurrentUser?.callsign || '';
  _myGrid = window.CurrentUser?.locator || S?.stationLocator || '';
  if (!_myCall && window.CurrentUser) {
    _myCall = window.CurrentUser.callsign || window.CurrentUser.username || '';
  }
  _updateMacroTexts();
  _startClock();
  try {
    const r = await fetch('/api/wsjtx/status');
    const d = await r.json();
    _updateStatus({
      running:   d.running,
      decoding:  false,
      transmit:  false,
    });
    // Show info in the status
    const pill = document.getElementById('wj-status-pill');
    if (d.running) {
      if (pill) { pill.textContent = '● ONLINE'; pill.className = 'wsjtx-status-pill online'; }
      window.UI?.showToast(I18n.t('wj_toast_monitor_active').replace('{port}', d.port));
    } else {
      // Autostart (wsjtxAutostart in the config, enabled by default)
      // normally listens on port 2238 by itself - OFFLINE here means
      // autostart failed (e.g. the port is taken), not that something
      // needs to be manually clicked (the START button was removed,
      // listening now always auto-starts).
      if (pill) { pill.textContent = I18n.t('wj_offline_autostart_failed'); pill.className = 'wsjtx-status-pill'; }
    }
    // Show the counters
    if (d.packets_rx > 0 || d.decodes_rx > 0) {
      const countEl = document.getElementById('wj-decode-count');
      if (countEl) { countEl.removeAttribute('data-i18n'); countEl.textContent = I18n.t('wj_decode_count_session').replace('{n}', d.decodes_rx); }
    }
  } catch(e) { console.warn('[wsjtx] init error', e); }
  await _loadMiniLog();
  try {
    const rr = await fetch('/api/rotator');
    const rots = await rr.json();
    if (Array.isArray(rots) && rots.length > 0) _onRotatorUpdate(rots[0]);
  } catch(e) { /* no rotator - the SP/LP buttons stay disabled */ }
  // Init the scope — retries until the canvas has dimensions (WebGL needs width>0)
  function _tryScopeInit(attempts) {
    const c = document.getElementById('wj-scope-canvas');
    if (!c) return;
    const rect = c.getBoundingClientRect();
    if (rect.width > 0) {
      window.WSJTXScope?.init();
    } else if (attempts > 0) {
      setTimeout(() => _tryScopeInit(attempts - 1), 100);
    }
  }
  _tryScopeInit(20);  // max 2s of attempts every 100ms
  _initSplitResizer();
  _startRadioSync();
  _populateBandSelect();
}

// ── API ───────────────────────────────────────────────────────────────────────
async function startWsjtx() {
  const port = parseInt(document.getElementById('wsjtx-udp-port')?.value || 2237);
  try {
    const r = await fetch('/api/wsjtx/start', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({port})
    });
    const d = await r.json();
    if (d.ok) {
      _updateStatus({running:true, text:`Nasłuchuję UDP :${port}`});
      window.UI?.showToast(I18n.t('wj_toast_monitor_on_port').replace('{port}', port));
    }
  } catch(e) { window.UI?.showToast('✗ ' + e.message, 'error'); }
}

async function stopWsjtx() {
  try {
    await fetch('/api/wsjtx/stop', {method:'POST'});
    _updateStatus({running:false});
    window.UI?.showToast(I18n.t('wj_toast_monitor_stopped'));
  } catch(e) {}
}

async function haltTx() {
  // Stop FT8 TX — send it to the server and disable it locally
  try {
    WS.send({ type: 'ft8_tx_stop' });
    await fetch('/api/ft8/halt', {method:'POST'});
  } catch(e) {}
  stopTx();
  window.UI?.showToast(I18n.t('wj_toast_tx_stopped'));
}

function stopTx() {
  // Local TX abort UI reset. The actual abort is handled server-side
  // (ft8_tx_stop message -> self._ft8_tx_abort in webapp.py); this just
  // clears the highlighted macro button.
  document.querySelectorAll('.wj-tx-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('wj-halt-tx-btn');
  if (btn) btn.style.background = '';
  window.UI?.showToast(I18n.t('wj_toast_tx_paused'));
}

// ── Our own FT8 RX decoder (instead of a physical WSJT-X/JTDX) ────────────────
let _ownRxEnabled = false;
let _radioSyncTimer = null;
let _lastSyncedFreq = null;

// Syncs the displayed frequency (top bar, wj-freq) with the main radio's
// ACTUAL frequency (window.AppState.freq), which is updated globally by
// ws.js (telemetry/freq) regardless of whether WSJTX is the active tab.
// NOTE: we don't sync wj-mode with S.mode — S.mode is the radio's
// OPERATING mode (USB/LSB/CW/...), a different concept from the DIGITAL
// mode (FT8/FT4) shown in wj-mode, which is controlled by a separate
// decode-mode selector.
function _startRadioSync() {
  if (_radioSyncTimer) clearInterval(_radioSyncTimer);
  _radioSyncTimer = setInterval(_syncFreqFromRadio, 500);
  _syncFreqFromRadio(); // an immediate first update, don't wait 500ms
}

function _syncFreqFromRadio() {
  if (!S || S.freq == null) return;
  if (S.freq === _lastSyncedFreq) return; // no change — skip the DOM write
  _lastSyncedFreq = S.freq;
  const mhz = (S.freq/1e6).toFixed(6).replace(/(\d+)\.(\d{3})(\d{3})/, '$1.$2.$3');
  const el = document.getElementById('wj-freq');
  if (el) el.textContent = mhz;
}

let _decodeMode = 'FT8';
let _lastDxSnr = null;  // SNR of the last selected (clicked) DX station — for macro F3 (R+report)
// FROZEN report from the backend (partner_report_sent/recv). While a QSO
// is active, macros and the UI use THIS value instead of _lastDxSnr (a
// raw, changing SNR), so macro == on-air == log. Set in _onAutoQsoStatus.
let _frozenRstSent = null;
let _frozenRstRcvd = null;

// Standard working frequencies (dial freq, Hz) per WSJT-X convention for
// FT8 and FT4, for all major amateur bands. Source: WSJT-X's default
// "Working Frequencies" table (Settings | Frequencies), matching commonly
// published lists (e.g. sigidwiki.com, dxzone.com). Some bands have no
// official FT4 convention (e.g. where FT4 is rarely used) — those entries
// are omitted instead of guessed.
const BAND_FREQUENCIES = [
  { band: '160m', ft8: 1840000,   ft4: 1840000  },
  { band: '80m',  ft8: 3573000,   ft4: 3575000  },
  { band: '60m',  ft8: 5357000,   ft4: null     },
  { band: '40m',  ft8: 7074000,   ft4: 7047500  },
  { band: '30m',  ft8: 10136000,  ft4: 10140000 },
  { band: '20m',  ft8: 14074000,  ft4: 14080000 },
  { band: '17m',  ft8: 18100000,  ft4: 18104000 },
  { band: '15m',  ft8: 21074000,  ft4: 21140000 },
  { band: '12m',  ft8: 24915000,  ft4: 24919000 },
  { band: '10m',  ft8: 28074000,  ft4: 28180000 },
  { band: '6m',   ft8: 50313000,  ft4: 50318000 },
  { band: '4m',   ft8: 70100000,  ft4: null     },
  { band: '2m',   ft8: 144174000, ft4: null     },
];

// FT8/FT4 decode-mode toggle (top bar). Switches the active button,
// syncs wj-log-mode (the mode selector in the QSO logging form), and
// notifies the backend (ft8_set_decode_mode), which picks the right
// encoder/timing (ft8_encoder.py vs ft4_encoder.py, 15s vs 7.5s window).
// NOTE: this only switches TX (transmitting). The RX decoding side
// (recognizing FT4 signals on air) is NOT implemented yet — that's a
// separate, large undertaking requiring a different FFT/sync pipeline.
function setDecodeMode(mode) {
  _decodeMode = mode;
  const ft8Btn = document.getElementById('wj-mode-ft8-btn');
  const ft4Btn = document.getElementById('wj-mode-ft4-btn');
  if (ft8Btn) ft8Btn.classList.toggle('active', mode === 'FT8');
  if (ft4Btn) ft4Btn.classList.toggle('active', mode === 'FT4');

  // Sync the mode selector in the QSO logging form
  const logModeEl = document.getElementById('wj-log-mode');
  if (logModeEl) logModeEl.value = mode;

  _populateBandSelect(); // the frequency list depends on the mode (FT8 vs FT4)
  window.WSJTXScope?.setScopeDecodeMode(mode);
  window.WS?.send({ type: 'ft8_set_decode_mode', mode });
  window.UI?.showToast(I18n.t('wj_toast_decode_mode').replace('{mode}', mode));
}

// Fills the band <select> with the current frequencies for the active
// mode (FT8 or FT4). Bands without a defined convention for that mode
// (e.g. 60m/2m have no standard FT4 frequency) are omitted from the list
// instead of showing a wrong/guessed value.
function _populateBandSelect() {
  const sel = document.getElementById('wj-band-select');
  if (!sel) return;
  const prevValue = sel.value;
  sel.innerHTML = `<option value="">${I18n.t('wj_band_placeholder')}</option>`;
  for (const b of BAND_FREQUENCIES) {
    const hz = _decodeMode === 'FT4' ? b.ft4 : b.ft8;
    if (hz == null) continue;
    const opt = document.createElement('option');
    opt.value = String(hz);
    opt.textContent = `${b.band} (${(hz/1e6).toFixed(3)} MHz)`;
    sel.appendChild(opt);
  }
  // Keep the previous selection if it's still available in the new list
  // (e.g. after switching mode, for a band with a convention in both modes)
  if (prevValue && [...sel.options].some(o => o.value === prevValue)) {
    sel.value = prevValue;
  }
}

// Retunes the main radio to the frequency chosen from the band list and
// sets digital-USB mode — ATOMICALLY, a single ft8_qsy command to the server.
// It used to send freq (debounced 50ms) and mode (immediately)
// SEPARATELY, which raced on CI-V ("works sometimes, not other times",
// had to click a few times). Now the server sets mode+freq sequentially,
// reliably. Permissions and locks (radio_lock, feature_allowed, band) are
// checked on the backend.
//
// mode:'USB-D' below is a REQUEST, not a guarantee - the backend maps it
// to plain 'USB' for a Hamlib/RigCAT-driven rig (rigctld doesn't know the
// CI-V-only 'USB-D' token, and some older Icoms like the IC-746 have no
// digital-mode variant at all). The mode/bandwidth broadcast that follows
// reflects what was ACTUALLY set - don't assume USB-D in this toast.
function tuneToBand(hzStr) {
  if (!hzStr) return;
  const hz = parseInt(hzStr, 10);
  if (!hz) return;
  // Check the radio lock client-side (fast feedback) — the backend
  // re-verifies it anyway.
  const lock  = window.AppState?.radio_lock;
  const myUid = String(window.AppState?.my_uid || window.CurrentUser?.id || '');
  const role  = window.CurrentUser?.role;
  if (role !== 'admin' && (!lock?.locked || String(lock.user_id) !== myUid)) {
    const holder = lock?.callsign || lock?.username || '?';
    window.UI?.showToast(I18n.t('wj_toast_radio_busy').replace('{holder}', holder), 'error');
    return;
  }
  // A single atomic command — the server sets digital-USB + freq in the right order.
  WS.send({ type:'ft8_qsy', freq: hz, mode: 'USB-D' });
  window.UI?.showToast(`${(hz/1e6).toFixed(3)} MHz (${_decodeMode})`);
}

// TX period: the two stations in a QSO transmit alternately in one of two
// alternating 15s windows (period 1 = xx:00/xx:30, period 2 =
// xx:15/xx:45), so they never transmit at the same time. The backend uses
// this to pick the CORRECT window in seconds_until_next_tx_window().
function setTxPeriod(period) {
  const btn1 = document.getElementById('wj-period-1-btn');
  const btn2 = document.getElementById('wj-period-2-btn');
  if (btn1) btn1.classList.toggle('active', period === 1);
  if (btn2) btn2.classList.toggle('active', period === 2);
  window.WS?.send({ type: 'ft8_set_tx_period', period });
  window.UI?.showToast(I18n.t('wj_toast_tx_period_prefix') + (period === 1 ? '1st (xx:00/30)' : '2nd (xx:15/45)'));
}

function _onTxPeriodUpdate(msg) {
  const btn1 = document.getElementById('wj-period-1-btn');
  const btn2 = document.getElementById('wj-period-2-btn');
  if (btn1) btn1.classList.toggle('active', msg.period === 1);
  if (btn2) btn2.classList.toggle('active', msg.period === 2);
}

// Syncs the mode-toggle UI when the change came from another connected
// client (backend broadcast) — does NOT send WS back, otherwise it would
// loop forever with setDecodeMode().
function _onDecodeModeUpdate(msg) {
  _decodeMode = msg.mode;
  const ft8Btn = document.getElementById('wj-mode-ft8-btn');
  const ft4Btn = document.getElementById('wj-mode-ft4-btn');
  if (ft8Btn) ft8Btn.classList.toggle('active', msg.mode === 'FT8');
  if (ft4Btn) ft4Btn.classList.toggle('active', msg.mode === 'FT4');
  const logModeEl = document.getElementById('wj-log-mode');
  if (logModeEl) logModeEl.value = msg.mode;
  _populateBandSelect();
  window.WSJTXScope?.setScopeDecodeMode(msg.mode);
}

function toggleOwnRx() {
  _ownRxEnabled = !_ownRxEnabled;
  window.WS?.send({ type: 'ft8_rx_enable', enabled: _ownRxEnabled });
  window.UI?.showToast(_ownRxEnabled ? I18n.t('wj_toast_own_rx_on') : I18n.t('wj_toast_own_rx_off'));
  const btn = document.getElementById('wj-own-rx-btn');
  if (btn) {
    btn.textContent = _ownRxEnabled ? I18n.t('wj_own_rx_stop') : I18n.t('wj_own_rx_start');
    btn.classList.toggle('active', _ownRxEnabled);
  }
  // FIX (reported live 2026-08-26: "timer ft8 leci nawet jak zatrzymam
  // odbior i pojde sobie np na cw"): the FT8 safety timer (Tx Watchdog)
  // guards against UNATTENDED automated TX - with RX stopped there are no
  // decodes, so the automation literally cannot answer anyone or
  // transmit, and the timer has nothing to guard against. It used to arm
  // once (on the first auto_seq_status after connect) and just run
  // forever regardless of RX state - this is the actual on/off switch:
  // stop the countdown the moment RX stops, (re)start it the moment RX
  // starts, tracking the ONLY thing that determines whether unattended TX
  // is even possible. Hound layers its own start() on top when armed
  // while RX is running (see toggleHound); its stop no longer touches
  // this shared timer (see houndStop) since RX may still be running the
  // main automation after Hound turns off.
  if (_ownRxEnabled) {
    window.FT8Timer?.start();
  } else {
    window.FT8Timer?.stop();
  }
}

function clearDecodes() {
  _decodes = []; _decodeCount = 0;
  _renderDecodes(); _updateCount();
}

// Clearing the RX Frequency panel does NOT clear the _decodes table
// itself (Band Activity has its own data) — it only hides the current RX
// Frequency view until a new decode/transmission arrives on the RX frequency.
let _rxFreqPanelCleared = false;
function clearRxFreqPanel() {
  _rxFreqPanelCleared = true;
  const el = document.getElementById('wj-rx-freq-row');
  if (el) el.innerHTML = `<div class="wj-empty">${I18n.t('wj_no_rxfreq_signal')}</div>`;
}

// ── Resizer between Band Activity and RX Frequency ────────────────────────────
function _initSplitResizer() {
  const resizer = document.getElementById('wj-split-resizer');
  const wrap = document.getElementById('wj-decodes-wrap');
  const paneLeft = document.getElementById('wj-pane-activity');
  const paneRight = document.getElementById('wj-pane-rxfreq');
  if (!resizer || !wrap || !paneLeft || !paneRight) return;

  let dragging = false, startX = 0, startLeftWidth = 0;
  const MIN_PANE_PX = 120;

  resizer.addEventListener('mousedown', (ev) => {
    dragging = true;
    startX = ev.clientX;
    startLeftWidth = paneLeft.getBoundingClientRect().width;
    resizer.classList.add('dragging');
    ev.preventDefault();
  });

  window.addEventListener('mousemove', (ev) => {
    if (!dragging) return;
    const wrapW = wrap.getBoundingClientRect().width;
    const resizerW = resizer.getBoundingClientRect().width;
    const available = wrapW - resizerW;
    let newLeftW = startLeftWidth + (ev.clientX - startX);
    newLeftW = Math.max(MIN_PANE_PX, Math.min(available - MIN_PANE_PX, newLeftW));
    const leftPct = (newLeftW / available) * 100;
    paneLeft.style.flexBasis = `${leftPct}%`;
    paneRight.style.flexBasis = `${100 - leftPct}%`;
  });

  window.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    resizer.classList.remove('dragging');
  });
}

// Proxy to WSJTXScope (buttons in the HTML call WSJTX.*, the logic lives in the scope module)
function toggleTxFreeze() { window.WSJTXScope?.toggleTxFreeze(); }

// Reset the Palette Adjust sliders (REF/ZERO/GAIN) to their default
// values — both in the DOM (input + displayed number) and in the waterfall itself.
function resetPaletteAdjust() {
  const defaults = [
    ['wj-palette-ref',  15,  '15',  v => window.WSJTXScope?.setPaletteReference(v / 100)],
    ['wj-palette-zero', 0,   '0',   v => window.WSJTXScope?.setPaletteZero(v / 100)],
    ['wj-palette-gain', 100, '1.0', v => window.WSJTXScope?.setPaletteGain(v / 100)],
  ];
  for (const [inputId, rawVal, label, apply] of defaults) {
    const input = document.getElementById(inputId);
    if (input) input.value = rawVal;
    const valEl = document.getElementById(inputId + '-val');
    if (valEl) valEl.textContent = label;
    apply(rawVal);
  }
}

// FAKE SPLIT (Rig Split): enable/disable shifting the VFO so the audio
// tone is ~1500Hz (full power, no splatter at the filter edges). Controls
// the radio during TX — enable it knowingly. State is saved in the config
// (survives a restart).
let _fakeSplitEnabled = false;
function toggleFakeSplit() {
  _fakeSplitEnabled = !_fakeSplitEnabled;
  WS.send({ type: 'ft8_toggle_fake_split', enabled: _fakeSplitEnabled });
}
function _onFakeSplitStatus(msg) {
  if (msg.enabled !== undefined) _fakeSplitEnabled = msg.enabled;
  const btn = document.getElementById('wj-fake-split-toggle');
  if (btn) {
    btn.textContent = _fakeSplitEnabled ? I18n.t('wj_fake_split_on') : I18n.t('wj_fake_split_off');
    btn.classList.toggle('active', _fakeSplitEnabled);
  }
  const tgt = document.getElementById('wj-fake-split-target');
  if (tgt && msg.targetHz !== undefined) tgt.textContent = `${msg.targetHz|0} Hz`;
}

// TUNE button (🎵 TUNE, id="wj-tune-btn") - transmits a steady tone for
// ATU antenna tuning. The backend (_start_tune in webapp.py) has always
// been fully implemented (real PTT on, timed tone, PTT off, radio_lock +
// cross-band-split checks, 30s hard cap) - the button just called a
// WSJTX.startTune() that didn't exist anywhere in this file, so clicking
// it threw silently and never sent anything. "Click again to interrupt"
// (per the button's title) - toggles ft8_tune / ft8_tune_stop.
let _tuneActive = false;
function startTune() {
  if (_tuneActive) {
    window.WS?.send({ type: 'ft8_tune_stop' });
    return;
  }
  // duration is a REQUEST - the backend clamps it to max 30s regardless;
  // sent at that cap so the tone keeps running until the operator
  // explicitly clicks again, rather than cutting off mid-adjustment.
  window.WS?.send({ type: 'ft8_tune', duration: 30, tone: 1500 });
}
function _onTuneStatus(msg) {
  _tuneActive = !!msg.active;
  const btn = document.getElementById('wj-tune-btn');
  if (btn) {
    // .wj-btn has no .active style of its own (unlike .rf-btn) - set the
    // red highlight inline so "currently transmitting a tone" is visually
    // obvious, not just a text-label change.
    btn.textContent = _tuneActive ? '⏹ STOP' : '🎵 TUNE';
    btn.style.background = _tuneActive ? 'var(--red)' : '';
    btn.style.color      = _tuneActive ? 'white' : '';
  }
}

function toggleAutoSeq() {
  const cb = document.getElementById('wj-auto-seq-toggle');
  const enabled = cb ? cb.checked : !_autoSeqEnabled;
  window.WS?.send({ type: 'ft8_toggle_auto_seq', enabled });
}

function _onAutoSeqStatus(msg) {
  if (msg.enabled !== undefined) _autoSeqEnabled = msg.enabled;
  if (msg.state !== undefined) _autoQsoState = msg.state;
  if (msg.partner !== undefined) _autoQsoPartner = msg.partner;
  _renderAutoQsoPanel();
}

function _onAutoQsoStatus(msg) {
  if (msg.state !== undefined) _autoQsoState = msg.state;
  if (msg.partner !== undefined) {
    _autoQsoPartner = msg.partner;
    // Reported live: the automation auto-starting a QSO on its own (a
    // direct call while idle, see "Auto-starting QSO" in webapp.py - no
    // click involved at all) left the DX field/macro previews/RX+TX
    // waterfall markers completely untouched, so the operator had no
    // visible sign of who it was even talking to or where - only
    // _selectRow (a manually clicked row) ever did this. auto_qso_status
    // fires at every step of ANY auto-QSO (clicked or self-started), so
    // this is the one place that covers both: fill the DX field (feeds
    // the macro-preview refresh below) and follow RX+TX to wherever this
    // partner was last actually heard, same as clicking their row would -
    // re-checked on every step (not just QSO start) so it keeps tracking
    // even if the partner drifts frequency mid-QSO.
    if (msg.partner) {
      _setField('wj-dx-call', msg.partner);
      const d = _findLatestDecodeFrom(msg.partner.toUpperCase());
      if (d) {
        window.WSJTXScope?.setRxFreqManual(d.deltaFreq);
        const txHeld = window.WSJTXScope?.isTxFrozen?.() || _hound?.active;
        if (!txHeld) window.WSJTXScope?.setTxFreqManual(d.deltaFreq);
      }
    }
  }
  // FROZEN report from the backend (partner_report_sent/recv). This IS
  // the value the backend transmits and logs. The UI MUST show exactly
  // this — not recompute from _lastDxSnr (a raw SNR from decodes, which
  // changes every window = "nonsense" in the macros). Consistency: macro
  // == on-air == log == what the user sees.
  if (msg.rstSent !== undefined && msg.rstSent !== "") {
    _frozenRstSent = msg.rstSent;
    const el = document.getElementById('wj-log-rst-sent');
    if (el) el.value = msg.rstSent;
  }
  if (msg.rstRcvd !== undefined && msg.rstRcvd !== "") {
    _frozenRstRcvd = msg.rstRcvd;
    const el = document.getElementById('wj-log-rst-rcvd');
    if (el) el.value = msg.rstRcvd;
  }
  _renderAutoQsoPanel();
  // Refresh the text preview under the macro buttons (macro 3 uses
  // _frozenRstSent) — without this the button showed the OLD value until
  // manually clicking a decode row, even though the backend had already
  // frozen the report.
  _updateMacroTexts();
}

// After an automatic QSO ends (73 received/sent), fills in the EXISTING
// logging form (the same fields as manually clicking a row in
// _selectRow) and draws the user's attention to it. DELIBERATELY does
// NOT call addLog() — writing to the journal requires explicit
// confirmation via the "+ LOG QSO" button, per the request: "log after
// QSO completion via user confirmation".
function _onAutoQsoComplete(msg) {
  // NOTE: we use direct .value assignment instead of _setField(), because
  // _setField deliberately does NOT overwrite a field with an empty/falsy
  // val (useful when manually filling in from the decode list, where no
  // data = leave unchanged) — here it's the opposite, we want to ALWAYS
  // overwrite, even with an empty string, so as not to leave "leaking"
  // data from the previous QSO in a chain of multiple automatic QSOs in a row.
  const callEl = document.getElementById('wj-log-call');
  if (callEl) callEl.value = msg.dxCall || '';
  const gridEl = document.getElementById('wj-log-grid');
  if (gridEl) gridEl.value = msg.dxGrid || '';
  // FIX (reported live 2026-08-24): this used to fall back to the literal
  // string '+00' whenever the completion broadcast carried an empty
  // report (e.g. a QSO that ended via 73/RR73 before any report was ever
  // exchanged - a real, if abnormal, sequence a partner can send). '+00'
  // LOOKS like a real measured SNR, so it silently logged a fake report
  // instead of visibly nothing - the operator had no way to tell it
  // wasn't real. Falls back to the already-tracked frozen value first
  // (should normally already be there), then to a genuinely empty field
  // - honest "we don't know" beats a plausible-looking fake number.
  const rstSentEl = document.getElementById('wj-log-rst-sent');
  if (rstSentEl) rstSentEl.value = msg.rstSent || _frozenRstSent || '';
  const rstRcvdEl = document.getElementById('wj-log-rst-rcvd');
  if (rstRcvdEl) rstRcvdEl.value = msg.rstRcvd || _frozenRstRcvd || '';
  const modeEl = document.getElementById('wj-log-mode');
  // Reset the frozen reports after the QSO ends — otherwise they'd leak
  // into the next automatic QSO.
  _frozenRstSent = null; // reset after the QSO
  _frozenRstRcvd = null;
  _updateMacroTexts(); // the macro-3 preview reverts to the current _lastDxSnr
  if (modeEl) modeEl.value = msg.mode || 'FT8';
  const commentEl = document.getElementById('wj-log-comment');
  if (commentEl) commentEl.value = '';

  window.UI?.showToast(I18n.t('wj_toast_qso_complete').replace('{call}', msg.dxCall));

  // Add IMMEDIATELY to _workedCalls (instead of waiting for the 60s
  // _loadWorkedCalls poll) — otherwise if the same station called CQ
  // again shortly after, it would look UNworked for up to a minute in the
  // Band Activity window (see _classify: CQ from an already-worked station -> gray).
  if (msg.dxCall) {
    const band = window.UI?.getBandName ? window.UI.getBandName(S.freq) : '';
    _workedCalls.add(_workedKey(msg.dxCall, band, msg.mode || _decodeMode));
    _renderDecodes();
  }

  if (callEl) {
    // NO scrollIntoView: the "MY QSO LOG" panel is always visible in this
    // layout anyway (no page scrolling) - scrollIntoView({block:'center'})
    // on a page with a transform scale (#app-scale) computed
    // "centering" wrong and threw the whole FT8/WSJTX window way up after
    // every completed auto-QSO, covering the top bar down to band-select.
    // Just the highlight is enough to draw attention.
    callEl.classList.add('wj-pending-log-highlight');
    setTimeout(() => callEl.classList.remove('wj-pending-log-highlight'), 3000);
  }
}

// Manual "skip" of the current station — abandons the active QSO and
// immediately (without waiting for the backend's 60s stall-timeout) goes
// back to answering whoever calls next (no queue).
function skipAutoQso() {
  window.WS?.send({ type: 'ft8_abort_auto_qso' });
}

function _renderAutoQsoPanel() {
  const seqCb = document.getElementById('wj-auto-seq-toggle');
  if (seqCb) seqCb.checked = _autoSeqEnabled;

  const skipBtn = document.getElementById('wj-autoqso-skip');
  if (skipBtn) {
    const qsoActive = _autoQsoPartner && _autoQsoState !== 'IDLE' && _autoQsoState !== 'DONE';
    skipBtn.style.display = qsoActive ? '' : 'none';
  }

  const statusEl = document.getElementById('wj-autoqso-status');
  if (statusEl) {
    statusEl.removeAttribute('data-i18n');  // see the note at rot-status-badge (rotormini.js)
    statusEl.classList.remove('active', 'done', 'error');
    if (!_autoSeqEnabled) {
      statusEl.textContent = I18n.t('wj_status_no_decoding');
    } else if (_autoQsoState === 'IDLE' || !_autoQsoPartner) {
      statusEl.textContent = I18n.t('wj_status_waiting_call1st');
    } else if (_autoQsoState === 'DONE') {
      statusEl.textContent = I18n.t('wj_status_qso_done').replace('{partner}', _autoQsoPartner);
      statusEl.classList.add('done');
    } else {
      const stateLabels = {
        CALLING: I18n.t('wj_status_calling'),
        REPORT_SENT: I18n.t('wj_status_report_sent'),
        RRR_SENT: I18n.t('wj_status_rrr_sent'),
      };
      statusEl.textContent = I18n.t('wj_status_qso_with').replace('{partner}', _autoQsoPartner).replace('{state}', stateLabels[_autoQsoState] || _autoQsoState);
      statusEl.classList.add('active');
    }
  }
}
function setTxFreqManual(val) { window.WSJTXScope?.setTxFreqManual(val); }
function setRxFreqManual(val) { window.WSJTXScope?.setRxFreqManual(val); }
function rxEqTx() { window.WSJTXScope?.rxEqTx(); }
function txEqRx() { window.WSJTXScope?.txEqRx(); }

// ── WS dispatch ───────────────────────────────────────────────────────────────
function handleWS(msg) {
  switch(msg.type) {
    case 'wsjtx_status':  _updateStatus(msg); break;
    // NOTE: we do NOT call FT8Timer.reset() here on every decode - band
    // activity is not proof the OPERATOR is present (WSJT-X counts a lack
    // of mouse/keyboard activity, not band activity). On a live, busy
    // band decodes arrive every ~15s nonstop, so a timer reset HERE would
    // never actually reach zero. reset() is now called from actual
    // operator actions - see _selectRow/sendTx.
    case 'wsjtx_decode':  _addDecode(msg); break;
    case 'wsjtx_clear':   _decodes = []; _renderDecodes(); break;
    case 'wsjtx_qso_logged': _onWsjtxQsoLogged(msg); break;
    case 'ft8_tx_status': _onFt8TxStatus(msg); break;
    case 'ft8_tx_error':
      // Sent when a manual FT8 TX (e.g. clicking a reply to a decode) had
      // missing fields (callTo/callDe/report) - without this the button
      // simply did nothing, with no error message at all.
      window.UI?.showToast(`✗ FT8 TX: ${msg.error || 'blad'}`, 'error');
      break;
    case 'ft8_waterfall': window.WSJTXScope?.onWaterfallData(msg); break;
    case 'ft8_tx_freq':   window.WSJTXScope?.onTxFreqUpdate(msg); break;
    case 'ft8_fake_split_status': _onFakeSplitStatus(msg); break;
    case 'ft8_rx_freq':   window.WSJTXScope?.onRxFreqUpdate(msg); _renderRxFreqPanel(); break;
    case 'ft8_tx_period':    _onTxPeriodUpdate(msg); break;
    case 'ft8_decode_mode':  _onDecodeModeUpdate(msg); break;
    case 'auto_seq_status':  _onAutoSeqStatus(msg); break;
    case 'auto_qso_status':  _onAutoQsoStatus(msg); break;
    case 'auto_qso_complete': _onAutoQsoComplete(msg); break;
    case 'auto_qso_error':   window.UI?.showToast(`⚠ ${msg.error}`); break;
    case 'qso_logged':
      // A new QSO in the real log (qso_db) — from both the automation and
      // a manual "+ LOG QSO" (see the broadcast in /api/qsolog POST and
      // in _process_auto_qso, both send the same type). The broadcast
      // goes to ALL clients (not just the QSO's owner) - we filter here,
      // since user_id isn't in this specific auto_qso payload, we just
      // compare knowing it only ever matters for our OWN view anyway
      // (each client has its own independent mini-log).
      _onQsoLogged(msg);
      break;
    case 'tune_status': _onTuneStatus(msg); break;
    case 'rotator_update':
      // The same broadcast as the big compass in RADIO (rotormini.js) —
      // only feeds the live ROTOR ---° reading and the SP/LP button state
      // here, doesn't duplicate the whole widget. Multiple rotators: we
      // use the same one as rotormini.js (first in the list / the already-selected _rotorId).
      if (msg.rotator && (!_rotorId || msg.rotator.id === _rotorId)) _onRotatorUpdate(msg.rotator);
      break;
  }
}

// ── Status ────────────────────────────────────────────────────────────────────
function _updateStatus(d) {
  if (d.running   !== undefined) _status.running   = d.running;
  if (d.transmit  !== undefined) _status.transmit  = d.transmit;
  if (d.decoding  !== undefined) _status.decoding  = d.decoding;

  const pill = document.getElementById('wj-status-pill');
  if (pill) {
    if (!_status.running)       { pill.textContent='○ OFFLINE';    pill.className='wsjtx-status-pill'; }
    else if (_status.transmit)  { pill.textContent='📡 TX';        pill.className='wsjtx-status-pill tx'; }
    else if (_status.decoding)  { pill.textContent=I18n.t('wj_decoding_status');  pill.className='wsjtx-status-pill decoding'; }
    else                        { pill.textContent='● ONLINE';     pill.className='wsjtx-status-pill online'; }
  }
  document.getElementById('wj-tx-indicator').style.display = _status.transmit ? '' : 'none';

  if (d.freq) {
    // NOTE: wj-freq has TWO independent update sources — this (from an
    // external WSJT-X's UDP packets, if it's running) and
    // _syncFreqFromRadio (from AppState.freq, the main radio, in the
    // background every 500ms). They don't really collide, since both
    // ultimately reflect the same physical radio frequency — this code
    // just covers the case where an external WSJT-X is actually running
    // and may be a slightly faster/more accurate source than our polling.
    const mhz = (d.freq/1e6).toFixed(6).replace(/(\d+)\.(\d{3})(\d{3})/, '$1.$2.$3');
    const el = document.getElementById('wj-freq');
    if (el) el.textContent = mhz;
  }
  // NOTE: 'wj-mode' (the old static div) was replaced by the FT8/FT4
  // toggle (wj-mode-switch) controlled by setDecodeMode() — we do NOT
  // overwrite it with the mode reported by an external WSJT-X, since
  // these are two different sources of truth (our decode-mode choice vs
  // what an external WSJT-X is doing).
  // Show the callsign/grid from WSJT-X in the UI informationally, but do
  // NOT overwrite _myCall/_myGrid if the user is logged in with their own profile.
  if (d.deCall) {
    if (!window.CurrentUser?.callsign) _myCall = d.deCall;
    const el = document.getElementById('wj-de-call'); if(el) el.textContent='DE: '+(window.CurrentUser?.callsign||d.deCall);
  }
  if (d.deGrid) {
    if (!window.CurrentUser?.locator) _myGrid = d.deGrid;
    const el = document.getElementById('wj-de-grid'); if(el) el.textContent=(window.CurrentUser?.locator||d.deGrid);
  }
  if (d.rxDF !== undefined) { const el=document.getElementById('wj-rx-df'); if(el) el.textContent=d.rxDF+' Hz'; }
  if (d.txDF !== undefined) { const el=document.getElementById('wj-tx-df'); if(el) el.textContent=d.txDF+' Hz'; }
  if (d.version) { const el=document.getElementById('wj-version'); if(el) el.textContent='WSJT-X '+d.version; }
}

// ── Decodes ───────────────────────────────────────────────────────────────────
function _classify(message) {
  const m  = message.toUpperCase();
  const mc = (_myCall||'').toUpperCase();
  if (mc && m.includes(mc)) return 'wj-mycall';
  if (m.startsWith('CQ ')) {
    // "already worked" only makes sense to show for a CQ — it's the only
    // moment when the operator actually decides "click or skip". The same
    // station seen mid-QSO WITH SOMEONE ELSE (report/73/RR73) doesn't
    // carry that information (nothing to click), so the CQ/73/DX color
    // stays unchanged there - the previous version checked "worked" at
    // the very end, AFTER CQ classification, so in practice it almost
    // NEVER fired (almost every decode hit CQ/73/DX first) - a station
    // already in the log looked identical to a fresh one.
    const call = _extractCall(m);
    if (_isWorkedHere(call)) return 'wj-worked';
    return 'wj-cq';
  }
  if (/\bRR73\b|\b73\b/.test(m)) return 'wj-73';
  const dx = (document.getElementById('wj-dx-call')?.value||'').toUpperCase();
  if (dx && m.startsWith(dx)) return 'wj-dx';
  return '';
}

function _isHidden(message) {
  if (!_hideWorked) return false;
  const call = _extractCall(message.toUpperCase());
  return _isWorkedHere(call);
}

function _extractGrid(msg) {
  for (const p of msg.trim().toUpperCase().split(/\s+/)) {
    // RR73 formally matches the locator pattern (letters A-R + digits) -
    // the protocol deliberately chose an Antarctica grid to signal QSO
    // end, so a sign-off message ("SP3GSK DL1ABC RR73") got "RR73"
    // inserted as if it were the correspondent's real grid. Same
    // exclusion as the backend's _call_grid_cache (webapp.py) already
    // has - that one was fixed, this frontend copy never was.
    if (p === 'RR73') continue;
    if (/^[A-R]{2}\d{2}([A-X]{2})?$/.test(p)) return p.slice(0,4);
  }
  return '';
}

// CQ MODIFIERS — must stay in sync with _CQ_MODIFIERS in qso_engine.py
// (the backend has the fuller/authoritative list there, this is a subset
// for the most common cases). Without this, "CQ SOTA W1XYZ FN42"/"CQ POTA
// ..." got parsed as call="SOTA"/"POTA" (length 4 > the old <=3 threshold
// for modifiers like DX/NA), so clicking such a CQ started an automatic
// QSO with a fictitious partner "SOTA" instead of the activator's real callsign.
const _CQ_MODIFIERS = new Set([
  'DX','NA','SA','EU','AS','AF','OC','WW','WWDX',
  'USA','JA','DL','PA','OE','OK','OM','SP','SM','OZ','LA','OH','OY',
  'EA','CT','IT','IS','YO','YU','LY','YL','ES','UR','UA','UN','UK',
  'VE','VK','ZL','ZS','PY','LU','CE','HK','HC','HI','HP','TI','TG',
  'XE','CO',
  'TEST','CONTEST','SPRINT','FD','FIELD',
  'POTA','SOTA','IOTA','WWFF','COTA','BUNKER','REF','USI','USIS',
  'ILLW','WCA','WFF','TQP',
  'ARRL','RSGB','DARC','IARU',
  'QRP','QRO','QRPP',
  'FF','SKCC','SOWP','PODXS',
]);

// Whether `s` looks like a CQ modifier (POTA/DX/USA etc.), not a callsign.
// The same heuristic as is_cq_modifier() in qso_engine.py: whitelist OR a
// string up to 6 chars made ENTIRELY of letters (no digits). Deliberately
// general, not whitelist-only: a real amateur callsign always has a
// digit, so any purely alphabetic modifier up to 6 chars (BOTA/GOTA/
// HOTA/... - the whole "*OTA" family of activation programs, not just
// POTA/SOTA) is safely recognized without listing each one individually.
function _isCqModifier(s) {
  if (!s) return false;
  if (_CQ_MODIFIERS.has(s)) return true;
  return s.length <= 6 && /^[A-Z]+$/.test(s);
}

function _extractCall(msg) {
  // FT8 format: "CQ SP3GSK JO82" — a CQ call — we want SP3GSK
  // FT8 format: "CQ SOTA SP3GSK/P JO82" — CQ with a modifier — we want SP3GSK/P (parts[2])
  // FT8 format: "SP3GSK SQ3MZM -05" — SQ3MZM calling SP3GSK — we want SQ3MZM (parts[1])
  // FT8 format: "SQ3MZM SP3GSK R-12" — we want SQ3MZM (parts[0])
  const parts = msg.trim().toUpperCase().replace(/[<>]/g, '').split(/\s+/);
  if (!parts.length) return '';
  // CQ [MOD] CALL GRID
  if (parts[0] === 'CQ') {
    return parts.length >= 3 && _isCqModifier(parts[1]) ? parts[2] : parts[1];
  }
  // CALL_TO CALL_DE ... — return CALL_DE (whoever is transmitting = our correspondent)
  if (parts.length >= 2) return parts[1];
  return parts[0];
}

function _addDecode(d) {
  // OUR OWN TRANSMISSION (is_tx): add it to the list (so it's visible in
  // the RX window alongside received ones), but DON'T pass it to Hound or
  // count it as a received DX decode — it's our TX, not a signal from the band.
  if (!d.is_tx && _hound.active) _houndOnDecode(d);
  if (!d.is_tx) { _decodeCount++; _watchDxCall(d); }
  // Always add — isNew=false is replays from previous cycles (worth
  // showing too). On MSG_CLEAR, WSJT-X clears the table; we do the same in handleWS('wsjtx_clear')
  _decodes.push(d);
  if (_decodes.length > MAX_DECODES) _decodes.shift();
  // New activity unlocks the RX FREQUENCY panel after a manual "clear"
  // (🗑) — that was meant to be a temporary decluttering of the view, not
  // permanently disabling the panel until the page reloads.
  _rxFreqPanelCleared = false;
  _renderDecodes();
  _updateCount();
  _renderRxFreqPanel();
}

// Computes the decode-window number (slot index) from timeStr (format
// HHMMSS, UTC) and the mode (FT8: 15s windows, FT4: 7.5s windows) — used
// to detect the boundary between consecutive transmit periods in Band
// Activity (the dashed line).
function _windowSlot(timeStr, mode) {
  if (!timeStr || timeStr.length < 6) return null;
  const hh = parseInt(timeStr.slice(0, 2), 10);
  const mm = parseInt(timeStr.slice(2, 4), 10);
  const ss = parseInt(timeStr.slice(4, 6), 10);
  const totalSec = hh * 3600 + mm * 60 + ss;
  const windowS = mode === 'FT4' ? 7.5 : 15.0;
  return Math.floor(totalSec / windowS);
}

function _decodeRowHtml(d, idx) {
  if (_isHidden(d.message)) return '';
  // OUR OWN TRANSMISSION (is_tx): visually highlight with the wj-own-tx
  // class (a different color), so the user can tell what we TRANSMITTED
  // from what we RECEIVED. The ">>" prefix marks our own TX.
  const cls = d.is_tx ? 'wj-own-tx' : _classify(d.message);
  const snr = d.snr>=0 ? '+'+d.snr : String(d.snr);
  const dt  = d.deltaTime>=0 ? '+'+(d.deltaTime||0).toFixed(1) : (d.deltaTime||0).toFixed(1);
  const grid= _extractGrid(d.message);
  const txMark = d.is_tx ? '<span class="wj-tx-mark" style="color:#ff6; font-weight:bold;">▶ TX</span> ' : '';
  const txStyle = d.is_tx ? ' style="background:rgba(255,200,0,0.12); border-left:3px solid #fc0;"' : '';
  // Country ONLY for a CQ — the only moment it has practical relevance
  // (who we're looking for/calling), see the comment at _PREFIX_COUNTRY above.
  let country = '';
  if (!d.is_tx && (d.message||'').toUpperCase().startsWith('CQ ')) {
    const info = _countryForCall(_extractCall(d.message));
    if (info) country = _countryMode === 'flag' ? info.flag : _esc(info.name);
  }
  return `<div class="wj-decode-row ${cls}"${txStyle} data-idx="${idx}"
    onclick="WSJTX._selectRow(this,${idx})">
    <span class="wj-d-time">${d.timeStr||'--'}</span>
    <span class="wj-d-snr">${snr}</span>
    <span class="wj-d-dt">${dt}</span>
    <span class="wj-d-freq">${d.deltaFreq}</span>
    <span class="wj-d-msg">${txMark}${d.mode&&!d.is_tx?d.mode+' ':''}${_esc(d.message)}</span>
    <span class="wj-d-grid">${grid}</span>
    <span class="wj-d-country">${country}</span>
  </div>`;
}

let _cqOnly = false;

function toggleCqOnly() {
  const cb = document.getElementById('wj-cq-only-toggle');
  _cqOnly = cb ? cb.checked : !_cqOnly;
  _renderDecodes();
}

function _renderDecodes() {
  const el = document.getElementById('wj-decodes');
  if (!el) return;
  // The "CQ only" filter applies EXCLUSIVELY to Band Activity — RX
  // Frequency (_renderRxFreqPanel) searches _decodes directly, without
  // the filter, since it's meant to show everything on that frequency
  // regardless of whether it's a CQ call or a reply within an ongoing QSO.
  const visible = _cqOnly
    ? _decodes.filter(d => (d.message||'').toUpperCase().startsWith('CQ '))
    : _decodes;
  if (!visible.length) {
    el.innerHTML = _cqOnly
      ? `<div class="wj-empty">${I18n.t('wj_no_cq')}</div>`
      : `<div class="wj-empty">${I18n.t('wj_no_decodes')}</div>`;
    return;
  }
  const reversedVisible = [...visible].reverse();
  let prevSlot = null;
  el.innerHTML = reversedVisible.map((d) => {
    // Index in the ORIGINAL _decodes array (not the filtered list) —
    // needed so clicking a row (_selectRow) lands on the correct record
    // even when the CQ filter is active.
    const idx = _decodes.indexOf(d);
    const slot = _windowSlot(d.timeStr, d.mode);
    let separator = '';
    if (slot !== null && prevSlot !== null && slot !== prevSlot) {
      // Boundary between decode periods (e.g. xx:15 -> xx:30 for FT8) —
      // a dashed line indicating a NEW window starts below.
      separator = '<div class="wj-period-sep"></div>';
    }
    prevSlot = slot;
    return separator + _decodeRowHtml(d, idx);
  }).join('');
}

// Rx Frequency panel: a QUEUE (not a single row) of decodes whose
// frequency (deltaFreq) is close to the current RX marker (tolerance +/-
// a few Hz, to account for natural drift/decode inaccuracy). Shows them
// one below another, chronologically, both what we RECEIVED and what we
// TRANSMITTED ourselves (is_tx entries are marked "▶ TX" and a different
// background in _decodeRowHtml) — without this, the panel overwrote
// itself on every new decode and it was impossible to follow what
// exactly was happening on this frequency (receiving vs transmitting).
// Max RX_FREQ_QUEUE_MAX entries, the oldest disappear first (FIFO). Our
// own transmission appears in this queue naturally — the backend
// broadcasts it as wsjtx_decode (is_tx=true) already at the moment of
// PTT ON (_addDecode triggers this render on every decode), so no
// separate "live preview" row is needed.
const RX_FREQ_TOLERANCE_HZ = 8;
const RX_FREQ_QUEUE_MAX = 20;

function _renderRxFreqPanel() {
  const el = document.getElementById('wj-rx-freq-row');
  if (!el) return;

  if (_rxFreqPanelCleared) {
    el.innerHTML = `<div class="wj-empty">${I18n.t('wj_no_rxfreq_signal')}</div>`;
    return;
  }

  const rxFreq = window.WSJTXScope?.getRxFreq?.();
  if (rxFreq == null) {
    el.innerHTML = `<div class="wj-empty">${I18n.t('wj_no_rxfreq_signal')}</div>`;
    return;
  }
  // Hold TX (isTxFrozen) intentionally keeps the TX frequency apart from
  // the RX marker (RX follows the correspondent, TX stays put) — so own
  // transmissions always pass the filter while Hold TX is active, even
  // when their deltaFreq is outside RX_FREQ_TOLERANCE_HZ of the RX marker.
  const txHeld = window.WSJTXScope?.isTxFrozen?.();
  const matches = _decodes.filter(d => {
    if (d.deltaFreq === undefined) return false;
    if (Math.abs(d.deltaFreq - rxFreq) <= RX_FREQ_TOLERANCE_HZ) return true;
    return d.is_tx && txHeld;
  });
  if (!matches.length) {
    el.innerHTML = `<div class="wj-empty">${I18n.t('wj_no_rxfreq_signal')}</div>`;
    return;
  }
  const queued = matches.slice(-RX_FREQ_QUEUE_MAX);
  el.innerHTML = [...queued].reverse().map((d) => {
    const idx = _decodes.indexOf(d);
    return _decodeRowHtml(d, idx);
  }).join('');
}

function _selectRow(el, idx) {
  // An operator click = proof of presence for the safety timer (WSJT-X Tx
  // Watchdog) - see the comment at 'wsjtx_decode' in handleWS.
  window.FT8Timer?.reset();
  document.querySelectorAll('.wj-decode-row.selected').forEach(r=>r.classList.remove('selected'));
  el.classList.add('selected');
  const d = _decodes[idx];
  if (!d) return;
  // Clicking OUR OWN transmission (is_tx) — do nothing (it's our own
  // message in the QSO history, not a station to call). Just highlight it.
  if (d.is_tx) return;
  if (d.deltaFreq !== undefined) {
    // CALLING SOMEONE: both markers (RX and TX) follow the correspondent
    // — per the WSJT-X spec. We used to set ONLY RX, so the markers
    // worked independently (a bug). TX follows UNLESS it's frozen (Hold
    // Tx Freq) or Hound — then TX stays separate (also per the WSJT-X spec).
    window.WSJTXScope?.setRxFreqManual(d.deltaFreq);
    const txHeld = window.WSJTXScope?.isTxFrozen?.() || _hound?.active;
    if (!txHeld) {
      window.WSJTXScope?.setTxFreqManual(d.deltaFreq);
    }
  }
  const call  = _extractCall(d.message);
  const grid  = _extractGrid(d.message);
  const snr   = (d.snr>=0?'+':'')+d.snr+' dB';
  _setField('wj-dx-call', call);
  _setField('wj-dx-grid', grid);
  _lastDxSnr = d.snr;
  const snrEl = document.getElementById('wj-dx-snr');
  if (snrEl) snrEl.textContent = snr;
  // Prefill log form
  _setField('wj-log-call',    call);
  _setField('wj-log-grid',    grid);
  _setField('wj-log-rst-rcvd', d.snr>=0?'+'+d.snr:String(d.snr));
  _setField('wj-log-mode', d.mode || 'FT8');
  // _setField() sets .value programmatically, so it does NOT fire the
  // HTML's oninput - the antenna heading has to be explicitly recomputed
  // after clicking a row.
  updateBeamRow();
  // Update the TX macro text
  _updateMacroTexts();

  // Clicking a CQ call OR a message addressed DIRECTLY to us (someone
  // already called us - Tx1/report/RRR/RR73/73 with our callsign as
  // call_to), with automation enabled, starts a FULL automatic QSO
  // (instead of just filling in the fields for manual sending). This used
  // to work ONLY for "CQ ..." — a station that called us directly (e.g.
  // "SQ3MZM DL3MIB JN57") couldn't be manually "jumped to" by clicking, because
  // isCq was false and the click only retuned RX/TX, without sending
  // ft8_start_auto_qso at all — the backend (the "ft8_start_auto_qso"
  // handler in webapp.py) had long since correctly accepted an
  // initial_decode of ANY message type, so this was purely a frontend limitation.
  // Guard call!=='CQ': incomplete/truncated messages (e.g. just "CQ" with
  // no callsign, a decode error) give call==='CQ' from _extractCall —
  // that's NOT a real partner callsign and shouldn't start the automation.
  const upperMsg = (d.message||'').toUpperCase();
  const isCq = upperMsg.startsWith('CQ ');
  const isDirectToMe = _myCall && upperMsg.startsWith(_myCall.toUpperCase() + ' ');
  if ((isCq || isDirectToMe) && _autoSeqEnabled && call && call !== 'CQ') {
    // recvEpoch (the exact receive timestamp of THIS decode from the
    // backend, not "now") is crucial — the backend computes our TX window
    // from it. Without this, correct window selection only worked if you
    // clicked within a fraction of a second of the decode appearing — a
    // human's manual reaction (several-odd seconds) landed the
    // transmission in the wrong window (collision with the partner, the
    // QSO "wouldn't start").
    window.WS?.send({ type: 'ft8_start_auto_qso', callDe: call,
                       message: d.message, recvEpoch: d.recvEpoch, snr: d.snr });
  }
}

// Manual station search by callsign in the DX field (Enter/blur) — the
// opposite direction from clicking a row: instead of the mouse pointing
// at a decode, the operator types a callsign, and if the station is
// CURRENTLY visible in the band's decode history, the RX marker retunes
// to its frequency on its own. The grid is DELIBERATELY not required for
// retuning (only filled in if it happens to be present in the matched
// decode) — it's a purely informational field here, not a condition. The
// TX marker does NOT follow (unlike clicking a row) — typing a callsign
// means "I'm looking/listening", not "I intend to transmit right now",
// these two intents are meant to be kept separate.
// Shared by searchDxCall (one-shot, searches decode HISTORY) and
// _watchDxCall (live, called for every new decode as it arrives) - retunes
// RX and fills grid/SNR/macro-preview the same way _selectRow does for a
// clicked row, minus the TX marker (typing/tracking a callsign means "I'm
// listening", not "I intend to transmit right now" - see the comment above
// searchDxCall).
function _applyDxMatch(d) {
  window.WSJTXScope?.setRxFreqManual(d.deltaFreq);
  const grid = _extractGrid(d.message);
  if (grid) _setField('wj-dx-grid', grid);
  _lastDxSnr = d.snr;
  const snrEl = document.getElementById('wj-dx-snr');
  if (snrEl) snrEl.textContent = (d.snr >= 0 ? '+' : '') + d.snr + ' dB';
  _updateMacroTexts();
}

// Finds the most recent (newest first) RX decode where `call` is the
// station actually TRANSMITTING (calling CQ, or calling/replying to
// someone) - not merely mentioned as the addressee in someone else's
// transmission. Shared by searchDxCall, _watchDxCall and
// _onAutoQsoStatus - one search implementation instead of three copies.
//
// FIX (reported live 2026-08-25): this used to match `call` against ANY
// token in the message ("XX0XXX XX1XXX -10" matched "XX0XXX" too, even
// though XX1XXX is the one transmitting there and XX0XXX is just being
// called BY them) - so typing/tracking a callsign in the DX field
// followed it around the waterfall based on OTHER stations calling it,
// not its own activity. _extractCall() already correctly picks out
// whoever is actually sending a given decode (CQ caller, or the DE
// field of a CALL_TO CALL_DE exchange) - use that instead of a raw
// token search.
function _findLatestDecodeFrom(call) {
  for (let i = _decodes.length - 1; i >= 0; i--) {
    const d = _decodes[i];
    if (d.is_tx || d.deltaFreq === undefined) continue;
    if (_extractCall(d.message) === call) return d;
  }
  return null;
}

function searchDxCall(rawCall) {
  const call = (rawCall || '').trim().toUpperCase();
  if (!call) return;
  // Search from the NEWEST decode backward — if the station appeared
  // multiple times, we care about its MOST RECENT known frequency.
  const d = _findLatestDecodeFrom(call);
  if (d) { _applyDxMatch(d); return; }
  window.UI?.showToast(I18n.t('wj_toast_station_not_visible').replace('{call}', call), 'error');
}

// Live tracking: as long as the DX field still contains the callsign the
// operator typed/selected, keep retuning RX to it whenever it's heard
// again (e.g. searchDxCall found nothing yet because the station hadn't
// called since, or it moves frequency between transmissions). Stops the
// moment the DX field is cleared or changed to something else - it never
// tracks anything beyond what's literally showing in that field right now.
function _watchDxCall(d) {
  if (d.is_tx || d.deltaFreq === undefined) return;
  const call = (document.getElementById('wj-dx-call')?.value || '').trim().toUpperCase();
  if (!call) return;
  // See the FIX note on _findLatestDecodeFrom above - must match the
  // actual transmitting station, not just any mention of the callsign.
  if (_extractCall(d.message) === call) _applyDxMatch(d);
}

// ── Antenna heading + rotator (the ANTENNA row below the DX field in QUICK QSO LOG) ─
// The header/row had been in the HTML for a while (beamheading.js
// computed the azimuth), but nothing ever called BeamHeading.headingFor()
// or connected it to the rotator — a purely dead piece of UI. Fixed:
// recomputed on every change to the CALLSIGN DX/Grid DX fields (oninput
// in index.html + after being filled in programmatically by
// _selectRow), and SP/LP send the command DIRECTLY to the same
// /api/rotator/<id>/position as the big compass in RADIO (rotormini.js)
// — no separate confirmation, click = go. The live rotator position
// updates via the SAME "rotator_update" broadcast as the big compass
// (see the case in handleWS), regardless of whether the operator
// currently has the RADIO tab open.
let _rotorId  = null;
let _beamSpAz = null;
let _beamLpAz = null;

function updateBeamRow() {
  const azEl   = document.getElementById('wj-beam-az');
  const distEl = document.getElementById('wj-beam-dist');
  const srcEl  = document.getElementById('wj-beam-src');
  const longEl = document.getElementById('wj-beam-long');
  const call = document.getElementById('wj-log-call')?.value.trim().toUpperCase() || '';
  const grid = document.getElementById('wj-log-grid')?.value.trim().toUpperCase() || '';
  const h = call ? window.BeamHeading?.headingFor(call, grid) : null;
  // The row is ALWAYS VISIBLE (see index.html) — here we just reset it to
  // the "---°" placeholders when no station is selected yet, instead of
  // hiding the whole row. SP/LP end up disabled naturally (_beamSpAz/_beamLpAz=null).
  if (!h) {
    if (azEl)   azEl.textContent   = '---°';
    if (distEl) distEl.textContent = '';
    if (srcEl)  srcEl.textContent  = '';
    if (longEl) longEl.textContent = '';
    _beamSpAz = null; _beamLpAz = null;
    _updateRotorButtons();
    return;
  }
  if (azEl)   azEl.textContent   = h.azimuth + '°';
  if (distEl) distEl.textContent = h.distance + 'km';
  if (srcEl)  srcEl.textContent  = h.source === 'grid' ? '(grid)' : '(prefix)';
  if (longEl) longEl.textContent = `LP:${h.azLong}°`;
  _beamSpAz = h.azimuth;
  _beamLpAz = h.azLong;
  _updateRotorButtons();
}

function _updateRotorButtons() {
  const spBtn = document.getElementById('wj-rotor-sp-btn');
  const lpBtn = document.getElementById('wj-rotor-lp-btn');
  const manBtn = document.getElementById('wj-rotor-manual-btn');
  const has = _rotorId != null;
  if (spBtn) spBtn.disabled = !has || _beamSpAz == null;
  if (lpBtn) lpBtn.disabled = !has || _beamLpAz == null;
  if (manBtn) manBtn.disabled = !has;
}

function _onRotatorUpdate(rot) {
  if (!rot) return;
  _rotorId = rot.id;
  const az = Math.round(parseFloat(rot.azimuth ?? rot.az ?? 0));
  const el = document.getElementById('wj-rotor-az');
  if (el) el.textContent = `ROTOR ${az}°`;
  _updateRotorButtons();
}

async function _rotorSetAz(az, label) {
  if (!_rotorId) { window.UI?.showToast?.(I18n.t('wj_toast_no_rotor'), 'error'); return; }
  try {
    const r = await fetch(`/api/rotator/${_rotorId}/position`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({az, el: 0})
    });
    const d = await r.json();
    if (d.ok) window.UI?.showToast?.(I18n.t('wj_toast_rotor_moved').replace('{az}', az).replace('{labelPart}', label ? ' ('+label+')' : ''));
    else window.UI?.showToast?.(`✗ ${d.error || I18n.t('profile_error_fallback')}`, 'error');
  } catch(e) { window.UI?.showToast?.(`✗ ${e.message}`, 'error'); }
}

function rotorGoBeam(which) {
  const az = which === 'lp' ? _beamLpAz : _beamSpAz;
  if (az == null) return;
  _rotorSetAz(az, which.toUpperCase());
}

// Manually move the rotator to ANY azimuth or locator — SP/LP only give
// the computed heading to the currently selected station, there was no
// way to type in a custom target. A custom modal (#rotor-manual-modal in
// index.html) instead of prompt() — prompt() is SYNCHRONOUS and blocks
// the entire main JS thread until the user closes it, which live froze
// audio streaming (WebAudio/WebRTC) until the dialog was closed.
function rotorGoManual() {
  if (!_rotorId) { window.UI?.showToast?.(I18n.t('wj_toast_no_rotor'), 'error'); return; }
  const modal = document.getElementById('rotor-manual-modal');
  const input = document.getElementById('rotor-manual-input');
  if (!modal || !input) return;
  input.value = '';
  modal.style.display = 'flex';
  input.focus();
}

function rotorManualClose() {
  const modal = document.getElementById('rotor-manual-modal');
  if (modal) modal.style.display = 'none';
}

function rotorManualSubmit() {
  const input = document.getElementById('rotor-manual-input');
  const raw = (input?.value || '').trim();
  rotorManualClose();
  if (!raw) return;
  let az = null;
  const n = parseFloat(raw.replace(',', '.'));
  if (!isNaN(n) && /^\d+([.,]\d+)?°?$/.test(raw)) {
    az = Math.round(((n % 360) + 360) % 360);
  } else if (/^[A-R]{2}\d{2}([A-X]{2})?$/i.test(raw)) {
    const h = window.BeamHeading?.headingFor('', raw.toUpperCase());
    if (h) az = h.azimuth;
  }
  if (az == null) { window.UI?.showToast?.(I18n.t('wj_toast_bad_format'), 'error'); return; }
  _rotorSetAz(az, raw.toUpperCase());
}

// ── TX macros ─────────────────────────────────────────────────────────────────
// Report for macro 3 (R+report): acknowledging receipt + my MEASURED
// report of the partner's signal (not our grid!). ONE place for this
// logic, used both by _txMacroParts (what actually goes on air) and
// _updateMacroTexts (the text preview under the button) — these used to
// be TWO separate copies and only one of them froze the report, so the
// preview under the button flickered/changed with every decode even
// though the actual transmission correctly held one, frozen value for
// the whole QSO.
// FROZEN report: while a QSO is active, show EXCLUSIVELY the
// backend-confirmed frozen value (or a neutral placeholder until it
// appears) - NEVER _lastDxSnr during this phase. _lastDxSnr is the SNR of
// the LAST CLICKED decode row, which during full automation (nobody
// clicking manually) is completely unrelated to the current
// partner - it gave a plausible-looking but random number before the
// backend got around to freezing the real report (e.g. during the phase
// of sending our own Tx1/grid, before receiving a report from the
// partner). Outside an active QSO (a manual macro before automation
// starts) — the current _lastDxSnr still makes sense, since it's the only
// source available.
function _macro3Report() {
  if (_autoQsoState && _autoQsoState !== 'IDLE') {
    return _frozenRstSent || '+00';
  }
  const snr = _lastDxSnr != null ? _lastDxSnr : 0;
  const sign = snr >= 0 ? '+' : '-';
  return sign + String(Math.abs(snr)).padStart(2, '0');
}

// Structured definition of macros F1-F7: [callTo, callDe, report].
// callTo/callDe='CQ' means the special word CQ (not a callsign).
function _txMacroParts(n) {
  const myCall = _myCall || '';
  const myGrid = _myGrid || window.CurrentUser?.locator || '';
  const dxCall = document.getElementById('wj-dx-call')?.value || '';
  switch (n) {
    case 1:         return { callTo: 'CQ',   callDe: myCall, report: myGrid };
    case 2:         return { callTo: dxCall, callDe: myCall, report: myGrid };
    case 3:         return { callTo: dxCall, callDe: myCall, report: _macro3Report(), rFlag: true };
    case 4:         return { callTo: dxCall, callDe: myCall, report: 'RRR' };
    case 5:         return { callTo: dxCall, callDe: myCall, report: '73'  };
    case 6:         return { callTo: dxCall, callDe: myCall, report: 'RR73' };
    default:        return null;
  }
}

function _updateMacroTexts() {
  const myCall = _myCall || '?';
  const myGrid = _myGrid || '??';
  const dxCall = document.getElementById('wj-dx-call')?.value || '?';

  const macros = {
    1: `CQ ${myCall} ${myGrid}`,
    2: `${dxCall} ${myCall} ${myGrid}`,
    3: `${dxCall} ${myCall} R${_macro3Report()}`,
    4: `${dxCall} ${myCall} RRR`,
    5: `${dxCall} ${myCall} 73`,
    6: `${dxCall} ${myCall} RR73`,
  };
  for (const [n, text] of Object.entries(macros)) {
    const el = document.getElementById(`wj-tx${n}-text`);
    if (el) el.textContent = text;
  }
}

function sendTx(n) {
  window.FT8Timer?.reset();  // manual TX = proof of presence, see _selectRow
  const textEl = document.getElementById(`wj-tx${n}-text`);
  if (!textEl) return;
  const parts = _txMacroParts(n);
  if (!parts || !parts.callDe) {
    window.UI?.showToast(I18n.t('wj_toast_no_mycall'));
    return;
  }
  if (!parts.callTo && parts.callTo !== 'CQ') {
    window.UI?.showToast(I18n.t('wj_toast_no_dxcall'));
    return;
  }
  let text = textEl.textContent
    .replace('{MYCALL}', _myCall||'MYCALL')
    .replace('{DXCALL}', document.getElementById('wj-dx-call')?.value||'DXCALL')
    .replace('{GRID}',   _myGrid || window.CurrentUser?.locator || '')
    .replace('{REPORT}', parts.report||'');

  document.querySelectorAll('.wj-tx-btn').forEach(b=>b.classList.remove('active'));
  document.getElementById(`wj-tx${n}`)?.classList.add('active');
  window.UI?.showToast(`TX F${n}: ${text}`);

  window.WS?.send({
    type: 'ft8_tx',
    callTo: parts.callTo,
    callDe: parts.callDe,
    report: parts.report,
    rFlag: parts.rFlag || false
  });
}

// React to TX status updates from the backend (PTT/audio sequence)
// Determines which macro number (1-5) corresponds to the `text` actually
// being sent (e.g. "SP3GSK DL1ABC RRR"), based on the last "word"
// (report/grid/RRR/73/RR73) — regardless of whether the transmission
// started from a manual click (sendTx) or from QSO automation (the
// backend generates the text directly, without going through sendTx), so
// this is the ONLY reliable way to highlight "what's actually going on
// air" in both modes.
function _macroNumberForText(text) {
  if (!text) return null;
  const upper = text.toUpperCase().trim();
  if (upper.startsWith('CQ ')) return 1;
  const lastWord = upper.split(/\s+/).pop();
  if (lastWord === 'RR73') return 6;
  if (lastWord === '73')   return 5;
  if (lastWord === 'RRR')  return 4;
  // A report with an "R" prefix (e.g. "R-18") is an acknowledgment +
  // measured report — a different macro from the first report/grid
  // (which has NO R prefix).
  if (/^R[+-]\d+$/.test(lastWord)) return 3;
  // The remaining cases are the first numeric report (e.g. "-12") or a
  // grid (e.g. "JO72") exchange — both correspond to macro 2.
  return 2;
}

// TX-window wait countdown. It used to be that the only signal was a
// disappearing toast (a couple of seconds) — after it disappeared, for
// the rest of the up-to-a-dozen-odd seconds of waiting the UI gave NO
// visible sign anything was happening, so it looked stuck and the
// operator would manually abort before TX even started. A persistent,
// counting-down indicator (wj-tx-wait-status) instead, so it's clearly
// visible this is a normal countdown to the 15s/7.5s UTC window boundary, not a bug.
let _txWaitTimer = null;
let _txWaitTarget = 0; // Date.now() (ms) at which TX should actually start

function _stopTxWaitCountdown() {
  if (_txWaitTimer) { clearInterval(_txWaitTimer); _txWaitTimer = null; }
  const el = document.getElementById('wj-tx-wait-status');
  if (el) el.style.display = 'none';
}

function _startTxWaitCountdown(waitSeconds, text) {
  _txWaitTarget = Date.now() + waitSeconds * 1000;
  const el = document.getElementById('wj-tx-wait-status');
  if (!el) return;
  el.style.display = '';
  const tick = () => {
    const remain = Math.max(0, (_txWaitTarget - Date.now()) / 1000);
    el.textContent = I18n.t('wj_tx_wait_countdown').replace('{s}', remain.toFixed(1)).replace('{text}', text);
    if (remain <= 0) _stopTxWaitCountdown();
  };
  tick();
  if (_txWaitTimer) clearInterval(_txWaitTimer);
  _txWaitTimer = setInterval(tick, 200);
}

function _onFt8TxStatus(d) {
  const btns = document.querySelectorAll('.wj-tx-btn');
  if (d.status === 'waiting') {
    window.UI?.showToast(I18n.t('wj_toast_waiting_window').replace('{s}', (d.waitSeconds||0).toFixed(1)).replace('{text}', d.text));
    _startTxWaitCountdown(d.waitSeconds||0, d.text);
    // Highlight ALREADY at the waiting stage (not only once transmitting
    // starts), so it's visible what will go on air before PTT actually
    // fires — this is most useful in automation, where waiting for the
    // window can take several-odd seconds.
    const n = _macroNumberForText(d.text);
    btns.forEach(b=>b.classList.remove('active'));
    if (n) document.getElementById(`wj-tx${n}`)?.classList.add('active');
  } else if (d.status === 'starting') {
    window.UI?.showToast(I18n.t('wj_toast_transmitting').replace('{text}', d.text));
    _stopTxWaitCountdown();
    const el = document.getElementById('wj-tx-wait-status');
    if (el) { el.style.display = ''; el.textContent = I18n.t('wj_tx_transmitting_status').replace('{text}', d.text); }
    const n = _macroNumberForText(d.text);
    btns.forEach(b=>b.classList.remove('active'));
    if (n) document.getElementById(`wj-tx${n}`)?.classList.add('active');
  } else if (d.status === 'error') {
    window.UI?.showToast(I18n.t('wj_toast_tx_error').replace('{error}', d.error));
    _stopTxWaitCountdown();
    btns.forEach(b=>b.classList.remove('active'));
  } else if (d.status === 'done') {
    _stopTxWaitCountdown();
    btns.forEach(b=>b.classList.remove('active'));
  }
}

// ── QSO Log ───────────────────────────────────────────────────────────────────
// The real log is /api/qsolog (the qso_db database) - the SAME one as the
// full LOG QSO page (qsolog.js), automatic QSO saving from FT8, and the
// "already worked" check (see _isWorkedHere/_loadWorkedCalls above). The
// "QUICK QSO LOG" panel here used to write/read from a COMPLETELY
// DIFFERENT, separate store (/api/log, self.log in webapp.py) - QSOs
// added through this form never made it into the real log, weren't
// counted as "already worked", and didn't go to CloudLog. Fixed - see the
// identical fix in webapp.py (removed /api/log*, self.log; added a
// "qso_logged" broadcast to /api/qsolog POST, the same as the automation's
// auto-save already had).

// Fetches the last MINI_LOG_MAX QSOs to pre-fill the mini-log on page
// load - kept up to date afterward via the "qso_logged" broadcast
// (_onQsoLogged), with no re-fetching.
async function _loadMiniLog() {
  try {
    const token = localStorage.getItem('token') || '';
    const r = await fetch(`/api/qsolog?page=1&per=${MINI_LOG_MAX}`, {
      headers: token ? {'Authorization': `Bearer ${token}`} : {}
    });
    const d = await r.json();
    _miniLogEntries = d.qsos || [];
    _renderMiniLog();
  } catch(e) { console.warn('[wsjtx] mini-log load error', e); }
}

// A new QSO in the real log (auto-save from the automation OR a manual
// "+ LOG QSO" below - both send the same broadcast, see the comment at
// handleWS). The broadcast goes to ALL connected clients (hub.broadcast
// isn't per-user), so we filter by user_id - otherwise another logged-in
// operator's QSO would jump into our mini-log.
function _onQsoLogged(msg) {
  const qso = msg.qso;
  if (!qso || qso.user_id !== window.CurrentUser?.id) return;
  _miniLogEntries.unshift(qso);
  if (_miniLogEntries.length > MINI_LOG_MAX) _miniLogEntries.length = MINI_LOG_MAX;
  _renderMiniLog();
}

// Mini-log below "QUICK QSO LOG" (the automation panel) — a purely
// informational preview of recent QSOs from the real log. No editing/
// deleting here, DELIBERATELY - there's a separate LOG QSO page for that
// (full editing and saving).
function _renderMiniLog() {
  const el = document.getElementById('wj-minilog-body');
  if (!el) return;
  if (!_miniLogEntries.length) {
    el.innerHTML = `<tr><td colspan="4" style="color:#333;text-align:center;padding:8px;">${I18n.t('wj_no_logged_qso')}</td></tr>`;
    return;
  }
  el.innerHTML = _miniLogEntries.slice(0, MINI_LOG_MAX).map(e => `<tr>
    <td style="padding:2px 4px;color:#888;">${_esc(e.band||'')}</td>
    <td style="padding:2px 4px;color:#4cf;font-weight:bold;">${_esc(e.call||'')}</td>
    <td style="padding:2px 4px;color:#888;white-space:nowrap;">R: ${_esc(e.rst_rcvd||'')}</td>
    <td style="padding:2px 4px;color:#888;white-space:nowrap;">S: ${_esc(e.rst_sent||'')}</td>
  </tr>`).join('');
}

async function addLog() {
  const call = document.getElementById('wj-log-call')?.value.trim().toUpperCase();
  if (!call) { window.UI?.showToast(I18n.t('wj_toast_enter_callsign_dx'), 'error'); return; }

  const now    = new Date();
  const freq   = S?.freq || 0;
  const band   = _freqToBand(freq);
  const mode   = document.getElementById('wj-log-mode')?.value || 'FT8';
  const grid   = document.getElementById('wj-log-grid')?.value.trim().toUpperCase() || '';
  // FIX (reported live 2026-08-24, matches the _onAutoQsoComplete fix
  // above): '+00' looked like a real measured SNR, so an empty field
  // (report genuinely unknown) silently saved a fake-looking value into
  // the actual log entry instead of an honestly empty one.
  const rstS   = document.getElementById('wj-log-rst-sent')?.value || '';
  const rstR   = document.getElementById('wj-log-rst-rcvd')?.value || '';
  const comment= document.getElementById('wj-log-comment')?.value.trim() || '';

  // qso_db format (qso_date=YYYYMMDD, time_on=HHMMSS, no separators) -
  // see add_qso() in qso_db.py and the identical construction in
  // _process_auto_qso (webapp.py, QSO auto-save).
  const pad = n => String(n).padStart(2,'0');
  const qso_date = `${now.getUTCFullYear()}${pad(now.getUTCMonth()+1)}${pad(now.getUTCDate())}`;
  const time_on  = `${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}`;

  const entry = {
    call, gridsquare: grid, band, mode, freq,
    qso_date, time_on,
    rst_sent: rstS, rst_rcvd: rstR, comment,
    my_call: _myCall, my_gridsquare: _myGrid,
    source: 'manual',
  };

  try {
    const token = localStorage.getItem('token') || '';
    const r = await fetch('/api/qsolog', {
      method:'POST',
      headers: {'Content-Type':'application/json', ...(token ? {'Authorization': `Bearer ${token}`} : {})},
      body: JSON.stringify(entry)
    });
    const d = await r.json();
    if (d.ok) {
      window.UI?.showToast(I18n.t('wj_toast_qso_logged').replace('{call}', call));
      // Add IMMEDIATELY to _workedCalls (instead of waiting for the 60s
      // poll) — otherwise the same station would look unworked in Band
      // Activity for a while longer (see the identical comment in
      // _onAutoQsoComplete). The mini-log refreshes itself via the
      // "qso_logged" broadcast (_onQsoLogged) - no need to add it here manually.
      _workedCalls.add(_workedKey(call, band, mode));
      _renderDecodes();
      // Clear the form
      ['wj-log-call','wj-log-grid','wj-log-rst-rcvd','wj-log-comment'].forEach(id=>{
        const el=document.getElementById(id); if(el) el.value='';
      });
    } else {
      window.UI?.showToast(`✗ ${d.error || I18n.t('wj_log_error_fallback')}`, 'error');
    }
  } catch(e) { window.UI?.showToast(I18n.t('wj_toast_log_error_generic'), 'error'); }
}

async function exportAdif() {
  try {
    const token = localStorage.getItem('token') || '';
    const r = await fetch('/api/qsolog/export?format=adi', {
      headers: token ? {'Authorization': `Bearer ${token}`} : {}
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const blob = await r.blob();
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = `log_${(_myCall||'unknown').replace('/','_')}_${new Date().toISOString().slice(0,10)}.adi`;
    a.click();
    URL.revokeObjectURL(url);
    window.UI?.showToast(I18n.t('wj_toast_export_success'));
  } catch(e) { window.UI?.showToast(I18n.t('wj_toast_export_error'), 'error'); }
}

// A QSO logged automatically by WSJT-X (from a UDP packet)
function _onWsjtxQsoLogged(d) {
  // WSJT-X logged a QSO → save it to /api/qsolog (per-user via JWT)
  const now  = new Date();
  const pad  = n => String(n).padStart(2,'0');
  const qso  = {
    call:          (d.dxCall || '').toUpperCase(),
    gridsquare:    (d.dxGrid || '').toUpperCase(),
    qso_date:      `${now.getUTCFullYear()}${pad(now.getUTCMonth()+1)}${pad(now.getUTCDate())}`,
    time_on:       `${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}`,
    time_off:      `${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}`,
    band:          _freqToBand(d.freq || window.AppState?.freq || 0),
    mode:          d.mode || 'FT8',
    freq:          d.freq ? (d.freq / 1e6).toFixed(4) : '',
    rst_sent:      d.rstSent || '-99',
    rst_rcvd:      d.rstRcvd || '-99',
    my_call:       (d.myCall || _myCall || window.AppState?.callsign || '').toUpperCase(),
    my_gridsquare: (d.myGrid || _myGrid || window.AppState?.stationLocator || '').toUpperCase(),
    comment:       'Auto-logged by WSJT-X',
    source:        d.mode === 'FT4' ? 'ft4' : 'ft8',
  };
  if (!qso.call) return;  // no callsign — don't log
  const token = localStorage.getItem('token') || '';
  fetch('/api/qsolog', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json',
      ...(token ? {'Authorization': `Bearer ${token}`} : {}) },
    body: JSON.stringify(qso),
  }).then(r => r.json()).then(res => {
    if (res.ok) {
      window.UI?.showToast(I18n.t('wj_toast_qso_logged_full').replace('{call}', qso.call).replace('{band}', qso.band).replace('{mode}', qso.mode));
      // Refresh the table if we're on the LOG page
      if (document.getElementById('page-log')?.classList.contains('active')) {
        window.QSOLog?.load?.();
      }
    }
  }).catch(() => {});
}

// ── FT8 safety timer ──────────────────────────────────────────────────────────
window.FT8Timer = (() => {
  let _durationMs  = 6 * 60 * 1000;  // 6 min by default
  let _remaining   = 0;
  let _interval    = null;
  let _active      = false;
  let _userCanEdit = false;
  let _warnShown   = false;
  let _expired     = false;  // expired, waiting for confirmation (see confirm()/reset())

  async function init() {
    // Fetch the timer settings for the current user
    const token = localStorage.getItem('token') || '';
    try {
      const r = await fetch('/api/ft8timer/global', {
        headers: token ? {'Authorization': `Bearer ${token}`} : {}
      });
      if (!r.ok) return;
      const cfg = await r.json();
      _durationMs  = (cfg.duration_min || 6) * 60 * 1000;
      _userCanEdit = cfg.user_can_edit || false;
    } catch(e) {}
  }

  function start() {
    // Start the countdown - armed whenever FT8 RX is actually running (see
    // toggleOwnRx: RX is the only thing that determines whether unattended
    // auto-TX is even possible), or when Hound is enabled (toggleHound).
    _remaining  = _durationMs;
    _active     = true;
    _warnShown  = false;
    _expired    = false;
    _tick();
    clearInterval(_interval);
    _interval = setInterval(_tick, 1000);
    _updateDisplay();
  }

  function stop() {
    // Stop the countdown - RX turned off (toggleOwnRx, nothing left to
    // guard against with no decodes coming in) or internal use on expiry
    // (see _tick). Does NOT clear _expired on its own - only
    // confirm()/start() do that, so the "waiting for confirmation" state
    // doesn't silently vanish without an actual operator confirmation.
    _active = false;
    clearInterval(_interval);
    _interval = null;
    _remaining = 0;
    const btn = document.getElementById('ft8-timer-confirm');
    if (btn) btn.style.display = 'none';
    _updateDisplay();
  }

  // The single source of truth for "the operator confirmed presence" -
  // called both from the CONFIRM button and from reset() when the
  // operator takes an action despite an expired timer (see reset()
  // below). ALSO notifies the backend (ft8_timer_confirm) - without this
  // the automation would stay blocked forever, since the backend does
  // NOT see local clicks in the browser.
  function confirm() {
    _expired    = false;
    _remaining  = _durationMs;
    _warnShown  = false;
    _active     = true;
    clearInterval(_interval);
    _interval = setInterval(_tick, 1000);
    const btn = document.getElementById('ft8-timer-confirm');
    if (btn) btn.style.display = 'none';
    _updateDisplay();
    window.WS?.send({ type: 'ft8_timer_confirm' });
    window.UI?.showToast(I18n.t('wj_toast_timer_reset'));
  }

  function reset() {
    // If the timer expired and is waiting for confirmation - ANY operator
    // action (clicking a row, a TX macro) IS that confirmation, the same
    // as clicking CONFIRM. Without this the operator would always have
    // to land exactly on the small button, instead of simply returning to normal work.
    if (_expired) { confirm(); return; }
    // Reset without stopping — after every user action, ONLY when the
    // timer is actually active (armed while FT8 RX is running) - see
    // toggleOwnRx.
    if (_active) {
      _remaining = _durationMs;
      _warnShown = false;
      const btn = document.getElementById('ft8-timer-confirm');
      if (btn) btn.style.display = 'none';
    }
  }

  function _tick() {
    if (!_active) return;
    _remaining -= 1000;

    // Warning at 1 min remaining
    if (_remaining <= 60000 && !_warnShown) {
      _warnShown = true;
      window.UI?.showToast(I18n.t('wj_toast_timer_warning'), 'error');
      // Show the confirm button
      const btn = document.getElementById('ft8-timer-confirm');
      if (btn) btn.style.display = 'inline-block';
    }

    if (_remaining <= 0) {
      // Time's up — stop TX and block the automation until confirmed
      stop();
      _expired = true;
      _stopTX();
      window.UI?.showToast(I18n.t('wj_toast_timer_expired'), 'error');
      return;
    }

    _updateDisplay();
  }

  function _stopTX() {
    // Stop ACTUAL transmitting (PTT + QSO engine) - the previous version
    // only called stopTx() (a cosmetic reset of the button highlight,
    // doesn't touch PTT or the engine) and houndStop(), so for the main
    // automation it didn't actually stop anything at all.
    // haltTx() is the same full halt as the HALT TX button (PTT off +
    // abort_qso + invalidating any in-flight scheduled retransmits).
    window.WSJTX?.haltTx();
    if (window.WSJTX?.houndStop) window.WSJTX.houndStop();
    // Notify the backend - until ft8_timer_confirm arrives, the
    // automation must STOP responding to new callers
    // (see _ft8_operator_present in webapp.py). haltTx() alone
    // only stops the CURRENT transmission - without this extra block the
    // automation would catch the next caller right away, making the
    // whole timer useless (exactly the problem reported: the timer was
    // meant to cap TX time, but it interrupted at most one send).
    window.WS?.send({ type: 'ft8_timer_expired' });
  }

  function _updateDisplay() {
    const el = document.getElementById('ft8-timer-display');
    if (!el) return;
    if (!_active || _remaining <= 0) {
      el.textContent = '--:--';
      el.style.color = 'var(--dim)';
      return;
    }
    const mins = Math.floor(_remaining / 60000);
    const secs = Math.floor((_remaining % 60000) / 1000);
    el.textContent = `${String(mins).padStart(2,'0')}:${String(secs).padStart(2,'0')}`;
    // Color: green → yellow (<2min) → red (<1min)
    el.style.color = _remaining < 60000  ? 'var(--red)'
                   : _remaining < 120000 ? 'var(--amber)'
                   : 'var(--green)';
  }

  return { init, start, stop, confirm, reset };
})();

// ── Fox / Hound mode ─────────────────────────────────────────────────────────
// Compliant with the "FT8 DXpedition Mode User Guide" (K1JT, 2018) and
// "The FT4 and FT8 Communication Protocols" (K9AN/G4WJS/K1JT, QEX Jul/Aug
// 2020). Fox mode (the other side: the DXpedition) will DELIBERATELY
// never be implemented in this project - this only handles the Hound side
// (the calling station).
const _hound = {
  active:         false,
  foxCall:        '',
  step:           0,        // 0=idle 1=calling/waiting for Fox 3=sending R+rpt 4=waiting for RR73
  txFreq:         1500,     // Hz — calling the Fox (spec: 1000-4000 Hz)
  reportBaseFreq: 0,        // Hz — the freq the Fox called US on (nominally 300-540 Hz)
  reportFreq:     0,        // Hz — currently in use (may be shifted after a retry)
  attempts:       0,        // how many times we sent R+rpt in this QSO (UI/freq-shift only)
  foxReport:      '',       // report from the Fox, e.g. "-13"
  timer:          null,     // 2-minute operator-presence confirmation timer
  cycleTimer:     null,     // timer checking whether TX needs to be retried (no reply from the Fox)
  lastConfirm:    0,        // time of the last operator confirmation
  lastTxAt:       0,        // time of the last Hound message sent (for per-cycle retry)
};

const HOUND_SHIFT_HZ = 300;   // spec: an R+rpt retry shifts 300Hz higher/lower
const HOUND_CYCLE_MS = { FT8: 15000, FT4: 7500 };

function _houndCycleMs() {
  return HOUND_CYCLE_MS[_decodeMode] || 15000;
}

// R+rpt retry: attempt 1 = base freq (no shift), 2,3,4,... alternating
// +300/-300/+600/-600... (spec: "subsequent transmissions will be moved
// 300 Hz higher or lower" - the direction isn't mandated, so we alternate
// to stay close to the Fox's original slot instead of drifting one way).
function _houndShiftedFreq(base, attemptNum) {
  if (attemptNum <= 1) return base;
  const n = attemptNum - 1;
  const magnitude = Math.ceil(n / 2) * HOUND_SHIFT_HZ;
  const sign = (n % 2 === 1) ? 1 : -1;
  return Math.max(100, base + sign * magnitude);
}

function toggleHound(enabled) {
  _hound.active = enabled;
  const foxCall = (document.getElementById('wj-dx-call')?.value || '').trim().toUpperCase();

  if (enabled) {
    if (!foxCall) {
      window.UI?.showToast(I18n.t('wj_toast_no_foxcall'), 'error');
      document.getElementById('wj-hound-toggle').checked = false;
      _hound.active = false;
      return;
    }
    _hound.foxCall    = foxCall;
    _hound.step       = 1;
    _hound.attempts   = 0;
    // Set when the Fox FIRST actually replies to us (step 1->3 below) -
    // used as TIME_ON for the auto-logged QSO, matching the main auto-QSO
    // engine's convention (qso_engine.py's first_contact_at: anchor to the
    // partner's first reply, not to whenever the completing RR73 happens
    // to arrive - see _houndAutoLog).
    _hound.firstContactAt = null;
    // Calling frequency (1000-4000 range per spec) - FIX (live-seen
    // 2026-09-02): this used to be hardcoded to 1500 and never touched
    // again, so dragging the TX marker on the waterfall (setTxFreqManual
    // -> ft8_set_tx_freq -> WSJTXScope's txFreqHz) had zero effect on
    // Hound - it kept calling on 1500 regardless. Seed it from whatever
    // TX frequency is already selected instead of a fixed default.
    _hound.txFreq     = window.WSJTXScope?.getTxFreq() || 1500;
    _hound.lastConfirm = Date.now();
    _houndUpdateUI();
    _houndStartCalling();
    window.FT8Timer?.start();  // Start the safety timer
    // Internal Hound timer — checks confirmation every 30s
    clearInterval(_hound.timer);
    _hound.timer = setInterval(_houndCheckConfirm, 30000);
    // Retry TX every cycle (15s/7.5s) if the Fox hasn't replied - spec:
    // the Hound "may keep calling until he answers" (calling) and "will
    // repeat his transmission of Tx3" with no attempt limit (R+rpt).
    // Without this the Hound called EXACTLY ONCE and went silent if the
    // Fox didn't manage to reply the first time (near-certain in a busy
    // pile-up) - reported live as "the mode was a working TX", so this
    // was a real functional regression.
    clearInterval(_hound.cycleTimer);
    _hound.cycleTimer = setInterval(_houndCycleCheck, 3000);
    window.UI?.showToast(I18n.t('wj_toast_hound_searching').replace('{call}', foxCall).replace('{freq}', _hound.txFreq));
  } else {
    houndStop();
  }
}

function houndStop() {
  _hound.active  = false;
  _hound.step    = 0;
  _hound.attempts = 0;
  clearInterval(_hound.timer);
  clearInterval(_hound.cycleTimer);
  // Deliberately does NOT stop FT8Timer: the watchdog is a single shared
  // timer for ALL automated TX (see _onAutoSeqStatus) - regular
  // auto-answer keeps running after Hound turns off, so stopping it here
  // would leave that automation transmitting unattended with no cap.
  // Watchdog EXPIRY calls stop() itself (in _tick) before calling this
  // function, so that path is unaffected.
  const _ht = document.getElementById('wj-hound-toggle');
  if (_ht) _ht.checked = false;
  const _hcb = document.getElementById('wj-hound-confirm-btn');
  if (_hcb) _hcb.style.display = 'none';
  _houndUpdateUI();
  window.UI?.showToast(I18n.t('wj_toast_hound_off'));
}

// Operator confirmation every 2 min (protocol requirement)
function houndConfirm() {
  _hound.lastConfirm = Date.now();
  const btn = document.getElementById('wj-hound-confirm-btn');
  if (btn) btn.style.display = 'none';
}

function _houndCheckConfirm() {
  if (!_hound.active) return;
  const elapsed = Date.now() - _hound.lastConfirm;
  if (elapsed > 120000) {  // 2 minutes
    window.UI?.showToast(I18n.t('wj_toast_hound_confirm'), 'error');
    _hound.step = 0;
    _houndUpdateUI();
    // FIX (live-seen 2026-09-02): this toast used to fire every 30s with
    // NO way to actually confirm - houndConfirm() existed and was
    // exported, but nothing in the UI ever called it, so Hound stayed
    // stuck at step=0 forever once this fired.
    const btn = document.getElementById('wj-hound-confirm-btn');
    if (btn) btn.style.display = 'inline-block';
  }
}

// Checked every 3s: has a full TX cycle (15s FT8 / 7.5s FT4) passed
// without any Hound message sent in the current step? If so - retry
// (step 1: same freq, step 3: R+rpt with a shifted freq, see _houndShiftedFreq).
function _houndCycleCheck() {
  if (!_hound.active) return;
  const elapsed = Date.now() - _hound.lastTxAt;
  if (elapsed < _houndCycleMs() - 500) return;
  if (_hound.step === 1) {
    _houndStartCalling();
  } else if (_hound.step === 3) {
    _houndSendReport();
  }
}

// Main logic — called after every decode received in Hound mode
function _houndOnDecode(decoded) {
  if (!_hound.active || !_hound.foxCall) return;

  const fox = _hound.foxCall.toUpperCase();
  const my  = (_myCall || '').toUpperCase();
  // The Hound uses the Fox's BASE call, not the full compound callsign
  // (spec: "Hounds use Fox's base call, not his full compound callsign")
  // - e.g. "KH7Z" in the message vs "KH1/KH7Z" typed into the DX field.
  // A simple substring check is usually enough (works both ways
  // regardless of which form is shorter).
  const isFoxCall = (call) => call === fox || fox.includes(call) || call.includes(fox);

  // Type 0.1 message (i3=0, n3=1) - the Fox SIMULTANEOUSLY closes one
  // Hound's QSO (RR73) and invites the next one with a report, in ONE
  // transmission. The "message" field here is NOT reliable for regex
  // parsing (see unpack_type0_1 in unpack.rs) - we use the structured
  // fields directly instead.
  if (decoded.isDxpedition) {
    const call1 = (decoded.call_to || '').toUpperCase(); // gets RR73
    const call2 = (decoded.call_de || '').toUpperCase(); // gets a report
    if (call1 === my && (_hound.step === 3 || _hound.step === 4)) {
      _hound.step = 4;
      _houndUpdateUI();
      _houndQSOComplete();
      return;
    }
    if (call2 === my && _hound.step === 1) {
      _hound.foxReport      = decoded.report_or_grid || '';
      _hound.reportBaseFreq = decoded.deltaFreq || 400;
      _hound.reportFreq     = _hound.reportBaseFreq;
      _hound.step           = 3;
      _hound.attempts        = 0;
      _hound.lastConfirm     = Date.now();
      _hound.firstContactAt  = _hound.firstContactAt || Date.now();
      _houndUpdateUI();
      _houndSendReport();
      return;
    }
    return; // concerns a different Hound - not our business
  }

  // Standard (i3=1) message: "TO DE report" - token-based parsing, not a
  // loose substring check (that produced a false positive when some
  // other part of the text happened to contain "73"). Fox->Hound: TO=my
  // call, DE=Fox (base call) - an earlier version of this code checked
  // the positions BACKWARDS (expected "FOXCALL MYCALL ..."), so it never
  // detected a real reply from the Fox.
  const parts = (decoded.message || '').toUpperCase().trim().split(/\s+/);
  if (parts.length < 3) return;
  const [callTo, callDeRaw, tail] = parts;
  const callDe = callDeRaw.replace(/[<>]/g, '');
  if (callTo !== my || !isFoxCall(callDe)) return;

  // Step 1: the Fox replies to our CQ with an SNR report -> we send R+rpt
  if (_hound.step === 1 && /^[+-]\d{1,2}$/.test(tail)) {
    _hound.foxReport      = tail;
    _hound.reportBaseFreq = decoded.deltaFreq || 400; // the freq the Fox called US on
    _hound.reportFreq     = _hound.reportBaseFreq;
    _hound.step           = 3;
    _hound.attempts        = 0;
    _hound.lastConfirm     = Date.now();
    _hound.firstContactAt  = _hound.firstContactAt || Date.now();
    _houndUpdateUI();
    _houndSendReport();
    return;
  }

  if (_hound.step === 3 || _hound.step === 4) {
    // Step 4: the Fox confirms with RR73/73/RRR (a non-combined message) - QSO complete
    if (tail === 'RR73' || tail === '73' || tail === 'RRR') {
      _hound.step = 4;
      _houndUpdateUI();
      _houndQSOComplete();
      return;
    }
    // The Fox gives the same report again (didn't receive our R+rpt) - repeat
    if (_hound.step === 3 && /^[+-]\d{1,2}$/.test(tail)) {
      _hound.lastConfirm = Date.now();
      _houndSendReport();
    }
  }
}

function _houndStartCalling() {
  if (!_hound.active || _hound.step !== 1) return;
  // TX1: "KH1/KH7Z SP3GSK KO02" on freq 1000-4000 Hz. The spec doesn't
  // mandate AUTO-changing freq between retries when there's no reply -
  // but the OPERATOR dragging the TX marker mid-session is exactly the
  // "optional operator decision" the spec means, so pick up whatever is
  // currently selected right before each call instead of freezing
  // whatever was set when Hound started.
  _hound.txFreq = window.WSJTXScope?.getTxFreq() || _hound.txFreq;
  _hound.lastTxAt = Date.now();
  _houndUpdateUI();
  _houndSendMsg(_hound.foxCall, _myCall, (_myGrid || '').trim(), false, _hound.txFreq);
}

function _houndSendReport() {
  if (!_hound.active) return;
  _hound.attempts++;
  // TX3: "KH1/KH7Z SP3GSK R-13" - the first attempt on the Fox's freq
  // (nominally 300-540 Hz), each subsequent one SHIFTED by 300Hz (spec,
  // required - "will be moved", not optional). We retry WITHOUT LIMIT
  // until RR73 - spec: "WSJT-X will send this message even if... you have
  // not called Fox for several Tx sequences" - the Fox has its own limit
  // (3 attempts + a 3 min timeout), the Hound doesn't give up on its own.
  _hound.reportFreq = _houndShiftedFreq(_hound.reportBaseFreq, _hound.attempts);
  _hound.lastTxAt = Date.now();
  _houndUpdateUI();
  _houndSendMsg(_hound.foxCall, _myCall, `R${_hound.foxReport}`, false, _hound.reportFreq);
}

function _houndQSOComplete() {
  const foxCall = _hound.foxCall;
  houndStop();
  // Auto-log QSO
  window.QSOLog?.quickLog && _houndAutoLog(foxCall);
  window.UI?.showToast(I18n.t('wj_toast_qso_complete_fox').replace('{call}', foxCall));
}

function _houndAutoLog(foxCall) {
  // Set CALL in the quick-log and trigger the save
  const callEl = document.getElementById('qlog-call');
  const rstEl  = document.getElementById('qlog-rst-s');
  const rstREl = document.getElementById('qlog-rst-r');
  if (callEl) callEl.value = foxCall;
  if (rstEl)  rstEl.value  = _hound.foxReport || '-99';
  if (rstREl) rstREl.value = _hound.foxReport || '-99';
  // TIME_ON anchored to the Fox's FIRST reply (_hound.firstContactAt),
  // not to right now (when RR73 completes the QSO) - matches the main
  // auto-QSO engine's logging convention (qso_engine.py's
  // first_contact_at). Falls back to "now" only if that was somehow
  // never set (shouldn't happen - QSO completion implies step 3 already
  // ran, which always sets it).
  let overrides = {};
  if (_hound.firstContactAt) {
    const d = new Date(_hound.firstContactAt);
    const pad = n => String(n).padStart(2, '0');
    overrides = { time_on: `${pad(d.getUTCHours())}${pad(d.getUTCMinutes())}${pad(d.getUTCSeconds())}` };
  }
  window.QSOLog?.quickLog?.(overrides);
}

function _houndSendMsg(callTo, callDe, report, rFlag, audioFreqHz) {
  // The same WS path as regular FT8 TX (see sendMacro() above) — the only
  // one that actually works. The Hound used to POST to /api/wsjtx/tx,
  // which the backend never had (0 matches in webapp.py) — so Hound TX
  // was a silent no-op (just a console.warn), even though the panel
  // looked active. audioFreq overrides the TX freq before encoding — see
  // the comment at "elif t == ft8_tx" in webapp.py, which explicitly names Hound mode.
  window.WS?.send({
    type: 'ft8_tx',
    callTo, callDe, report,
    rFlag: !!rFlag,
    audioFreq: audioFreqHz,
    // Tells the backend NOT to feed this into the main auto-QSO engine
    // (self._qso_engine) - see the comment at "elif t == ft8_tx" /
    // isHound check in webapp.py. Hound's own Tx1 ("FOXCALL MYCALL
    // MYGRID") looks structurally identical to a normal manual grid-call
    // to that generic handler, which used to auto-start a regular tracked
    // QSO with the Fox's callsign - so when the Fox later sent a
    // (non-combined) RR73, the MAIN engine independently replied with its
    // own extra "73", on top of Hound's own (correct, silent) completion.
    isHound: true,
  });
}

function _houndUpdateUI() {
  const active  = _hound.active;
  const toggle  = document.getElementById('wj-hound-toggle');
  const statusEl = document.getElementById('wj-autoqso-status');

  if (toggle) toggle.checked = active;

  // Highlight the checkbox in the top bar when active
  const label = toggle?.closest('label');
  if (label) {
    label.style.background = active ? 'rgba(255,140,0,0.2)' : '';
    label.style.borderColor = active ? '#f90' : 'rgba(255,140,0,0.3)';
  }

  if (!statusEl) return;
  statusEl.removeAttribute('data-i18n');  // see the note at rot-status-badge (rotormini.js)
  if (!active) {
    statusEl.style.color = '';
    statusEl.textContent = I18n.t('wj_status_no_decoding');
    return;
  }

  const stepNames = ['', I18n.t('wj_hound_step_calling'), '', I18n.t('wj_hound_step_report'), I18n.t('wj_hound_step_waiting')];
  const freq = _hound.step === 3 ? _hound.reportFreq : _hound.txFreq;
  const step = stepNames[_hound.step] || '...';
  const attemptTxt = _hound.step === 3 ? I18n.t('wj_hound_attempt').replace('{n}', _hound.attempts) : '';
  statusEl.style.color = '#f90';
  statusEl.textContent = `🦊 HOUND: ${_hound.foxCall} | ${step} | TX:${freq}Hz${attemptTxt}`;
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function _freqToBand(hz) {
  if (!hz) return '';
  // 160m: the narrower (real PL/EU allocation) bound, the same as
  // webapp.py/dxcluster.py/ui.js::getBandName - it had drifted here
  // (1800000 instead of 1810000), the same bug already fixed once elsewhere in the project.
  if (hz >= 1810000  && hz <= 2000000)  return '160m';
  if (hz >= 3500000  && hz <= 3800000)  return '80m';
  if (hz >= 7000000  && hz <= 7200000)  return '40m';
  if (hz >= 10100000 && hz <= 10150000) return '30m';
  if (hz >= 14000000 && hz <= 14350000) return '20m';
  if (hz >= 18068000 && hz <= 18168000) return '17m';
  if (hz >= 21000000 && hz <= 21450000) return '15m';
  if (hz >= 24890000 && hz <= 24990000) return '12m';
  if (hz >= 28000000 && hz <= 29700000) return '10m';
  if (hz >= 50000000 && hz <= 52000000) return '6m';
  if (hz >= 70000000 && hz <= 70500000) return '4m';
  return '';
}

function _setField(id, val) {
  const el = document.getElementById(id); if(el && val) el.value = val;
}
function _esc(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function _updateCount() {
  const el = document.getElementById('wj-decode-count');
  if (el) { el.removeAttribute('data-i18n'); el.textContent = I18n.t('wj_decode_count_n').replace('{n}', _decodeCount); }
}

// ── Export ────────────────────────────────────────────────────────────────────
function toggleHideWorked(chk) {
  _hideWorked = chk.checked;
  _renderDecodes();
}

window.WSJTX = {
  init, startWsjtx, stopWsjtx, haltTx, stopTx, clearDecodes, clearRxFreqPanel, handleWS, sendTx, toggleOwnRx,
  tuneToBand, rxEqTx, txEqRx, toggleTxFreeze, _selectRow, searchDxCall, addLog, exportAdif,
  toggleHideWorked, loadWorkedCalls: _loadWorkedCalls, toggleCountryMode,
  updateBeamRow, rotorGoBeam, rotorGoManual, rotorManualClose, rotorManualSubmit,
  toggleTxFreeze, toggleFakeSplit, toggleCqOnly, toggleAutoSeq, setDecodeMode,
  tuneToBand, setTxPeriod,
  setTxFreqManual, setRxFreqManual, rxEqTx, txEqRx,
  _selectRow, addLog, exportAdif,
  toggleHound, houndStop, houndConfirm,
  skipAutoQso,
  resetPaletteAdjust, startTune,
};

})();
