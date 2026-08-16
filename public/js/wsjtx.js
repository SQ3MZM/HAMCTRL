/**
 * wsjtx.js — WSJT-X frontend (FT8/FT4/JT65 monitor + osobisty log QSO)
 * Każdy użytkownik widzi i loguje tylko swoje łączności.
 */
(function() {
'use strict';

const S = window.AppState;

// ── Stan ─────────────────────────────────────────────────────────────────────
let _decodes     = [];
let _allDecodes  = [];
let _myCall      = '';
let _workedCalls = new Set(); // klucze CALL|BAND|MODE z logu QSO — kazde pasmo/tryb to osobna lacznosc
let _hideWorked  = false;     // filtr — ukryj juz zaliczone

function _workedKey(call, band, mode) {
  return `${(call||'').toUpperCase()}|${(band||'').toUpperCase()}|${(mode||'').toUpperCase()}`;
}

// Zaladuj znaki z logu (co 60s i przy starcie). Uzywa /api/qsolog/calls
// (SELECT DISTINCT call+band+mode, bez capu na liczbe wpisow) zamiast
// /api/qsolog, ktore ma twardy limit per<=200 po stronie backendu — przy
// wiekszym logu starsze lacznosci znikaly z oznaczania jako zrobione.
// Band+mode w kluczu, bo "kazde pasmo (i FT8 vs FT4) to nowa lacznosc":
// stacja zrobiona na 40m nie powinna gasnac na 20m.
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

// Czy dany znak byl juz zrobiony na AKTUALNYM pasmie/trybie (nie globalnie).
function _isWorkedHere(call) {
  if (!call) return false;
  const band = window.UI?.getBandName ? window.UI.getBandName(S.freq) : '';
  return _workedCalls.has(_workedKey(call, band, _decodeMode));
}
let _myGrid      = '';
let _status      = { running: false, transmit: false, decoding: false };
let _clockTimer  = null;
let _decodeCount = 0;
let _miniLogEntries = [];   // ostatnie QSO z prawdziwego logu (qso_db, /api/qsolog) - podglad pod automatyka
const MINI_LOG_MAX = 8;
const MAX_DECODES = 300;

// ── Kraj wolajacego CQ (kolumna "Kraj" w Band Activity / RX Frequency) ────────
// Naglowek kolumny byl w HTML od dawna, ale nic jej nigdy nie wypelnialo -
// _decodeRowHtml renderowal <span class="wj-d-country"></span> zawsze pusty.
// Heurystyka prefiksowa (nie pelny/dokladny DXCC - to wymagaloby zewnetrznej
// bazy zakresow prefiksow z wyjatkami), analogiczna do prefixToLatLon() w
// beamheading.js (ta sama idea: probuj od najdluzszego prefiksu), ale osobna
// tabela - tu potrzebna nazwa+flaga, nie wspolrzedne.
const _PREFIX_COUNTRY = {
  // Europa
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
  // Afryka Płn./Bliski Wschod
  'CN':['Maroko','MA'], 'SU':['Egipt','EG'], '3V':['Tunezja','TN'], '7X':['Algieria','DZ'], '5A':['Libia','LY'],
  'A4':['Oman','OM'], 'A6':['ZEA','AE'], 'A7':['Katar','QA'], 'A9':['Bahrajn','BH'], '9K':['Kuwejt','KW'], 'HZ':['Arabia Saudyjska','SA'], '7Z':['Arabia Saudyjska','SA'],
  // Afryka Subsah.
  'EL':['Liberia','LR'], '5N':['Nigeria','NG'], 'TR':['Gabon','GA'], '9J':['Zambia','ZM'], 'ZS':['RPA','ZA'], 'ZR':['RPA','ZA'], 'ZT':['RPA','ZA'], 'ZU':['RPA','ZA'], '5H':['Tanzania','TZ'], '5Z':['Kenia','KE'], '5X':['Uganda','UG'], '7P':['Lesotho','LS'], 'V5':['Namibia','NA'], 'C9':['Mozambik','MZ'],
  // Ameryka Płn.
  'W':['USA','US'], 'K':['USA','US'], 'N':['USA','US'], 'AA':['USA','US'], 'AB':['USA','US'], 'AC':['USA','US'], 'AD':['USA','US'], 'AE':['USA','US'], 'AF':['USA','US'], 'AG':['USA','US'], 'AI':['USA','US'], 'AJ':['USA','US'], 'AK':['USA','US'],
  'KL':['Alaska','US'], 'KL7':['Alaska','US'], 'NL7':['Alaska','US'], 'KH6':['Hawaje','US'], 'NH6':['Hawaje','US'],
  'VE':['Kanada','CA'], 'VA':['Kanada','CA'], 'VO':['Kanada','CA'], 'VY':['Kanada','CA'], 'CF':['Kanada','CA'], 'CG':['Kanada','CA'], 'CJ':['Kanada','CA'], 'CK':['Kanada','CA'],
  'XE':['Meksyk','MX'], 'XF':['Meksyk','MX'],
  // Ameryka Środk./Karaiby
  'CO':['Kuba','CU'], 'CM':['Kuba','CU'], 'HI':['Dominikana','DO'], 'KP4':['Puerto Rico','PR'], 'NP4':['Puerto Rico','PR'], 'V2':['Antigua','AG'], '8P':['Barbados','BB'], 'J3':['Grenada','GD'], '6Y':['Jamajka','JM'], 'TG':['Gwatemala','GT'], 'TI':['Kostaryka','CR'], 'HP':['Panama','PA'], 'YN':['Nikaragua','NI'], 'HR':['Honduras','HN'], 'YS':['Salwador','SV'],
  // Ameryka Płd.
  'PY':['Brazylia','BR'], 'PP':['Brazylia','BR'], 'PQ':['Brazylia','BR'], 'PR':['Brazylia','BR'], 'PS':['Brazylia','BR'], 'PT':['Brazylia','BR'], 'PU':['Brazylia','BR'], 'PV':['Brazylia','BR'], 'PW':['Brazylia','BR'], 'ZV':['Brazylia','BR'], 'ZW':['Brazylia','BR'], 'ZX':['Brazylia','BR'], 'ZY':['Brazylia','BR'], 'ZZ':['Brazylia','BR'],
  'LU':['Argentyna','AR'], 'LO':['Argentyna','AR'], 'LP':['Argentyna','AR'], 'LQ':['Argentyna','AR'], 'LR':['Argentyna','AR'], 'LS':['Argentyna','AR'], 'LT':['Argentyna','AR'], 'LV':['Argentyna','AR'], 'LW':['Argentyna','AR'],
  'CE':['Chile','CL'], 'CA':['Chile','CL'], 'CB':['Chile','CL'], 'CC':['Chile','CL'], 'CD':['Chile','CL'], 'XQ':['Chile','CL'], 'XR':['Chile','CL'],
  'HK':['Kolumbia','CO'], 'HJ':['Kolumbia','CO'],
  'YV':['Wenezuela','VE'], 'YW':['Wenezuela','VE'], 'YX':['Wenezuela','VE'],
  'OA':['Peru','PE'], 'OB':['Peru','PE'], 'OC':['Peru','PE'],
  'CP':['Boliwia','BO'], 'HC':['Ekwador','EC'], 'HD':['Ekwador','EC'], 'CX':['Urugwaj','UY'], 'CV':['Urugwaj','UY'], 'ZP':['Paragwaj','PY'],
  // Azja
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
  // Afryka Płd./inne
  'ZS':['RPA','ZA'],
};

// Flaga z kodu ISO-3166 alpha-2 (regionalne wskazniki Unicode) - algorytm, nie
// baza obrazkow, wiec nie trzeba trzymac osobnych plikow/emoji per kraj.
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

// Tryb wyswietlania kolumny "Kraj" - CELOWO tylko jedna opcja naraz (flaga
// ALBO nazwa), nie obie jednoczesnie - przelacznik w naglowku Band Activity
// i RX Frequency (ten sam globalny stan, dwa przyciski .wj-country-mode-btn
// zeby dzialalo z obu paneli).
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

// Automatyka QSO (stan UI, zrodlo prawdy jest na backendzie — synchronizowane
// przez WS auto_seq_status/auto_qso_status/auto_qso_queue)
let _autoSeqEnabled = false;
let _autoCall1st = false;
let _autoQsoState = 'IDLE';
let _autoQsoPartner = null;
let _autoQsoQueue = [];

// ── Zegar 15s ─────────────────────────────────────────────────────────────────
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
    // Pokaż info w statusie
    const pill = document.getElementById('wj-status-pill');
    if (d.running) {
      if (pill) { pill.textContent = '● ONLINE'; pill.className = 'wsjtx-status-pill online'; }
      window.UI?.showToast(`✓ WSJT-X monitor aktywny (UDP :${d.port})`);
    } else {
      // Autostart (wsjtxAutostart w config, domyslnie wlaczony) normalnie
      // sam nasluchuje na porcie 2238 - OFFLINE tutaj oznacza ze autostart
      // sie nie udal (np. port zajety), nie ze trzeba cos recznie kliknac
      // (przycisk START usuniety 2026-08-15, nasluch juz zawsze auto-startuje).
      if (pill) { pill.textContent = '○ OFFLINE (autostart nieudany?)'; pill.className = 'wsjtx-status-pill'; }
    }
    // Pokaż liczniki
    if (d.packets_rx > 0 || d.decodes_rx > 0) {
      const countEl = document.getElementById('wj-decode-count');
      if (countEl) countEl.textContent = `${d.decodes_rx} odebranych dekodowań (sesja)`;
    }
  } catch(e) { console.warn('[wsjtx] init error', e); }
  await _loadMiniLog();
  try {
    const rr = await fetch('/api/rotator');
    const rots = await rr.json();
    if (Array.isArray(rots) && rots.length > 0) _onRotatorUpdate(rots[0]);
  } catch(e) { /* brak rotora - przyciski SP/LP zostaja wylaczone */ }
  // Init scope — ponawia az canvas bedzie mial wymiary (WebGL wymaga width>0)
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
  _tryScopeInit(20);  // max 2s prób co 100ms
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
      window.UI?.showToast(`✓ Monitor aktywny na porcie ${port}`);
    }
  } catch(e) { window.UI?.showToast('✗ ' + e.message, 'error'); }
}

async function stopWsjtx() {
  try {
    await fetch('/api/wsjtx/stop', {method:'POST'});
    _updateStatus({running:false});
    window.UI?.showToast('Monitor zatrzymany');
  } catch(e) {}
}

async function haltTx() {
  // Zatrzymaj TX FT8 — wyslij do serwera i wylacz lokalnie
  try {
    WS.send({ type: 'ft8_tx_stop' });
    await fetch('/api/ft8/halt', {method:'POST'});
  } catch(e) {}
  stopTx();
  window.UI?.showToast('⛔ TX zatrzymany');
}

function stopTx() {
  // Local TX abort UI reset. The actual abort is handled server-side
  // (ft8_tx_stop message -> self._ft8_tx_abort in webapp.py); this just
  // clears the highlighted macro button.
  document.querySelectorAll('.wj-tx-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById('wj-halt-tx-btn');
  if (btn) btn.style.background = '';
  window.UI?.showToast('TX wstrzymany');
}

// ── Wlasny dekoder FT8 RX (zamiast fizycznego WSJT-X/JTDX) ────────────────────
let _ownRxEnabled = false;
let _radioSyncTimer = null;
let _lastSyncedFreq = null;

// Synchronizacja wyswietlanej czestotliwosci (gorny pasek, wj-freq) z
// PRAWDZIWA czestotliwoscia glownego radia (window.AppState.freq), ktora
// jest aktualizowana globalnie przez ws.js (telemetry/freq) niezaleznie od
// tego czy WSJTX jest aktywna zakladka. UWAGA: nie synchronizujemy wj-mode
// z S.mode — S.mode to tryb PRACY RADIA (USB/LSB/CW/...), inne pojecie niz
// tryb CYFROWY (FT8/FT4) pokazywany w wj-mode, ktory jest kontrolowany przez
// osobny selektor trybu dekodowania.
function _startRadioSync() {
  if (_radioSyncTimer) clearInterval(_radioSyncTimer);
  _radioSyncTimer = setInterval(_syncFreqFromRadio, 500);
  _syncFreqFromRadio(); // natychmiastowa pierwsza aktualizacja, nie czekaj 500ms
}

function _syncFreqFromRadio() {
  if (!S || S.freq == null) return;
  if (S.freq === _lastSyncedFreq) return; // bez zmian — pomin DOM write
  _lastSyncedFreq = S.freq;
  const mhz = (S.freq/1e6).toFixed(6).replace(/(\d+)\.(\d{3})(\d{3})/, '$1.$2.$3');
  const el = document.getElementById('wj-freq');
  if (el) el.textContent = mhz;
}

let _decodeMode = 'FT8';
let _lastDxSnr = null;  // SNR ostatnio wybranej (klikietej) stacji DX — do makra F3 (R+raport)
// ZAMROZONY raport z backendu (partner_report_sent/recv). Gdy QSO aktywne,
// makra i UI uzywaja TEJ wartosci zamiast _lastDxSnr (surowy, zmienny SNR),
// zeby makro == eter == log. Ustawiane w _onAutoQsoStatus.
let _frozenRstSent = null;
let _frozenRstRcvd = null;

// Standardowe czestotliwosci robocze (dial freq, Hz) wg konwencji WSJT-X dla
// FT8 i FT4, dla wszystkich glownych pasm amatorskich. Zrodlo: domyslna
// tabela "Working Frequencies" WSJT-X (Settings | Frequencies), zgodna z
// powszechnie publikowanymi listami (m.in. sigidwiki.com, dxzone.com).
// Niektore pasma nie maja oficjalnej konwencji FT4 (np. gdzie FT4 rzadko
// uzywane) — wtedy pomijamy wpis zamiast zgadywac.
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
  { band: '2m',   ft8: 144174000, ft4: null     },
];

// Przelacznik trybu dekodowania FT8/FT4 (gorny pasek). Zmienia aktywny
// przycisk, synchronizuje wj-log-mode (selektor trybu w formularzu
// logowania QSO) i powiadamia backend (ft8_set_decode_mode), ktory wybiera
// odpowiedni enkoder/timing (ft8_encoder.py vs ft4_encoder.py, okno 15s vs
// 7.5s). UWAGA: to przelacza tylko TX (nadawanie). Strona dekodowania RX
// (rozpoznawanie sygnalow FT4 w eterze) NIE jest jeszcze zaimplementowana —
// to osobny, duzy etap (kolejna sesja), wymagajacy innego pipeline'u FFT/sync.
function setDecodeMode(mode) {
  _decodeMode = mode;
  const ft8Btn = document.getElementById('wj-mode-ft8-btn');
  const ft4Btn = document.getElementById('wj-mode-ft4-btn');
  if (ft8Btn) ft8Btn.classList.toggle('active', mode === 'FT8');
  if (ft4Btn) ft4Btn.classList.toggle('active', mode === 'FT4');

  // Synchronizuj selektor trybu w formularzu logowania QSO
  const logModeEl = document.getElementById('wj-log-mode');
  if (logModeEl) logModeEl.value = mode;

  _populateBandSelect(); // lista czestotliwosci zalezy od trybu (FT8 vs FT4)
  window.WSJTXScope?.setScopeDecodeMode(mode);
  window.WS?.send({ type: 'ft8_set_decode_mode', mode });
  window.UI?.showToast(`Tryb dekodowania: ${mode}`);
}

// Wypelnia <select> pasm aktualnymi czestotliwosciami dla biezacego trybu
// (FT8 lub FT4). Pasma bez zdefiniowanej konwencji dla danego trybu (np.
// 60m/2m nie maja standardowej czestotliwosci FT4) sa pomijane z listy
// zamiast pokazywac bledna/zgadywana wartosc.
function _populateBandSelect() {
  const sel = document.getElementById('wj-band-select');
  if (!sel) return;
  const prevValue = sel.value;
  sel.innerHTML = '<option value="">-- pasmo --</option>';
  for (const b of BAND_FREQUENCIES) {
    const hz = _decodeMode === 'FT4' ? b.ft4 : b.ft8;
    if (hz == null) continue;
    const opt = document.createElement('option');
    opt.value = String(hz);
    opt.textContent = `${b.band} (${(hz/1e6).toFixed(3)} MHz)`;
    sel.appendChild(opt);
  }
  // Zachowaj poprzedni wybor jesli nadal dostepny w nowej liscie (np. po
  // przelaczeniu trybu na pasmo ktore ma konwencje w obu trybach)
  if (prevValue && [...sel.options].some(o => o.value === prevValue)) {
    sel.value = prevValue;
  }
}

// Przestraja glowne radio na czestotliwosc wybrana z listy pasm i ustawia
// tryb USB-D — ATOMOWO, jedna komenda ft8_qsy do serwera. Wczesniej wysylalo
// sie freq (przez debounce 50ms) i mode (natychmiast) OSOBNO, co powodowalo
// wyscig na CI-V ("raz reaguje raz nie", trzeba bylo klikac kilka razy).
// Teraz serwer ustawia tryb+freq sekwencyjnie, niezawodnie. Uprawnienia i
// blokady (radio_lock, feature_allowed, pasmo) sprawdza backend.
function tuneToBand(hzStr) {
  if (!hzStr) return;
  const hz = parseInt(hzStr, 10);
  if (!hz) return;
  // Sprawdz blokade radia po stronie klienta (szybki feedback) — backend
  // i tak zweryfikuje ponownie.
  const lock  = window.AppState?.radio_lock;
  const myUid = String(window.AppState?.my_uid || window.CurrentUser?.id || '');
  const role  = window.CurrentUser?.role;
  if (role !== 'admin' && (!lock?.locked || String(lock.user_id) !== myUid)) {
    const holder = lock?.callsign || lock?.username || '?';
    window.UI?.showToast(`⛔ Radio zajęte przez ${holder} — przejmij TRX`, 'error');
    return;
  }
  // Jedna atomowa komenda — serwer ustawi USB-D + freq w dobrej kolejnosci.
  WS.send({ type:'ft8_qsy', freq: hz, mode: 'USB-D' });
  window.UI?.showToast(`${(hz/1e6).toFixed(3)} MHz — USB-D (${_decodeMode})`);
}

// Okres nadawania: dwie stacje w QSO nadaja na przemian w jednym z dwoch
// naprzemiennych okien 15s (period 1 = xx:00/xx:30, period 2 = xx:15/xx:45),
// zeby nigdy nie nadawac jednoczesnie. Backend uzywa tego do wyboru
// WLASCIWEGO okna w seconds_until_next_tx_window().
function setTxPeriod(period) {
  const btn1 = document.getElementById('wj-period-1-btn');
  const btn2 = document.getElementById('wj-period-2-btn');
  if (btn1) btn1.classList.toggle('active', period === 1);
  if (btn2) btn2.classList.toggle('active', period === 2);
  window.WS?.send({ type: 'ft8_set_tx_period', period });
  window.UI?.showToast(`Okres nadawania: ${period === 1 ? '1st (xx:00/30)' : '2nd (xx:15/45)'}`);
}

function _onTxPeriodUpdate(msg) {
  const btn1 = document.getElementById('wj-period-1-btn');
  const btn2 = document.getElementById('wj-period-2-btn');
  if (btn1) btn1.classList.toggle('active', msg.period === 1);
  if (btn2) btn2.classList.toggle('active', msg.period === 2);
}

// Synchronizuje UI przelacznika trybu gdy zmiana przyszla z innego
// podlaczonego klienta (broadcast od backendu) — NIE wysyla WS z powrotem,
// inaczej petla w nieskonczonosc z setDecodeMode().
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
  window.UI?.showToast(_ownRxEnabled ? '✓ Wlasny dekoder FT8 RX aktywny' : 'Wlasny dekoder FT8 RX zatrzymany');
  const btn = document.getElementById('wj-own-rx-btn');
  if (btn) {
    btn.textContent = _ownRxEnabled ? '⏹ STOP (własny RX)' : '▶ START (własny RX)';
    btn.classList.toggle('active', _ownRxEnabled);
  }
}

function clearDecodes() {
  _decodes = []; _decodeCount = 0;
  _renderDecodes(); _updateCount();
}

// Czyszczenie panelu RX Frequency NIE czysci samej tabeli _decodes (Band
// Activity ma wlasne dane) — tylko ukrywa biezacy widok RX Frequency, dopoki
// nie nadejdzie nowe dekodowanie/transmisja na czestotliwosci RX.
let _rxFreqPanelCleared = false;
function clearRxFreqPanel() {
  _rxFreqPanelCleared = true;
  const el = document.getElementById('wj-rx-freq-row');
  if (el) el.innerHTML = '<div class="wj-empty">Brak sygnału na czestotliwosci RX</div>';
}

// ── Resizer miedzy Band Activity i RX Frequency ────────────────────────────────
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

// Proxy do WSJTXScope (przyciski w HTML wywoluja WSJTX.*, logika jest w scope module)
function toggleTxFreeze() { window.WSJTXScope?.toggleTxFreeze(); }

// Przywroc suwaki Palette Adjust (REF/ZERO/GAIN) do wartosci domyslnych —
// zarowno w DOM (input + wyswietlana liczba), jak i w samym wodospadzie.
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

// FAKE SPLIT (Rig Split): wlacz/wylacz przesuwanie VFO by audio bylo ~1500Hz
// (pelna moc, brak splatterow przy krawedziach filtra). Steruje radiem podczas
// TX — wlaczaj swiadomie. Stan zapamietany w configu (przetrwa restart).
let _fakeSplitEnabled = false;
function toggleFakeSplit() {
  _fakeSplitEnabled = !_fakeSplitEnabled;
  WS.send({ type: 'ft8_toggle_fake_split', enabled: _fakeSplitEnabled });
}
function _onFakeSplitStatus(msg) {
  if (msg.enabled !== undefined) _fakeSplitEnabled = msg.enabled;
  const btn = document.getElementById('wj-fake-split-toggle');
  if (btn) {
    btn.textContent = _fakeSplitEnabled ? '🎯 FAKE SPLIT: ON' : '⭕ Fake Split: OFF';
    btn.classList.toggle('active', _fakeSplitEnabled);
  }
  const tgt = document.getElementById('wj-fake-split-target');
  if (tgt && msg.targetHz !== undefined) tgt.textContent = `${msg.targetHz|0} Hz`;
}

function toggleAutoSeq() {
  const cb = document.getElementById('wj-auto-seq-toggle');
  const enabled = cb ? cb.checked : !_autoSeqEnabled;
  window.WS?.send({ type: 'ft8_toggle_auto_seq', enabled });
}

function toggleCall1st() {
  const cb = document.getElementById('wj-call1st-toggle');
  const enabled = cb ? cb.checked : !_autoCall1st;
  window.WS?.send({ type: 'ft8_toggle_call_1st', enabled });
}

function _onAutoSeqStatus(msg) {
  if (msg.call1st !== undefined) {
    // Timer bezpieczenstwa FT8 (WSJT-X "Tx Watchdog") ARM/DISARM na
    // faktycznej zmianie stanu Call 1st (potwierdzonej przez backend, nie
    // optymistycznie w toggleCall1st()) - Call 1st ON to jedyny tryb w tej
    // aplikacji gdzie automat realnie odpowiada nieznanym wolajacym bez
    // udzialu operatora, wiec to on powinien uzbrajac zegar. Wczesniej
    // FT8Timer.start()/stop() byly wolane WYLACZNIE z toggleHound() -
    // dla zwyklej automatyki (Call 1st) zegar nigdy sie nie uzbrajal.
    if (msg.call1st && !_autoCall1st) window.FT8Timer?.start();
    else if (!msg.call1st && _autoCall1st) window.FT8Timer?.stop();
    _autoCall1st = msg.call1st;
  }
  if (msg.enabled !== undefined) _autoSeqEnabled = msg.enabled;
  if (msg.state !== undefined) _autoQsoState = msg.state;
  if (msg.partner !== undefined) _autoQsoPartner = msg.partner;
  if (msg.queue !== undefined) _autoQsoQueue = msg.queue;
  _renderAutoQsoPanel();
}

function _onAutoQsoStatus(msg) {
  if (msg.state !== undefined) _autoQsoState = msg.state;
  if (msg.partner !== undefined) _autoQsoPartner = msg.partner;
  // ZAMROZONY raport z backendu (partner_report_sent/recv). To JEST wartosc
  // ktora backend nadaje i loguje. UI MUSI pokazywac dokladnie ja — nie
  // przeliczac z _lastDxSnr (surowy SNR z dekodow, zmienia sie co okno =
  // "bzdury" w makrach). Spojnosc: makro == eter == log == to co widzi user.
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
  // Odswiez podglad tekstu pod przyciskami makr (makro 3 uzywa
  // _frozenRstSent) — bez tego przycisk pokazywal STARA wartosc az do
  // recznego kliknieca wiersza dekodu, mimo ze backend juz zamrozil raport.
  _updateMacroTexts();
}

// Po zakonczeniu automatycznego QSO (otrzymanie/wyslanie 73), wypelnia
// ISTNIEJACY formularz logowania (te same pola co reczne klikniecie wiersza
// w _selectRow) i zwraca na niego uwage uzytkownika. CELOWO NIE wywoluje
// addLog() — zapis do dziennika wymaga jawnego zatwierdzenia przyciskiem
// "+ LOG QSO", zgodnie z zyczeniem: "logowanie po zakonczeniu qso poprzez
// zatwierdzenie usera".
function _onAutoQsoComplete(msg) {
  // UWAGA: uzywamy bezposredniego przypisania .value zamiast _setField(),
  // bo _setField celowo NIE nadpisuje pola pustym/falsy val (przydatne przy
  // recznym wypelnianiu z listy dekodowan, gdzie brak danych = zostaw bez
  // zmian) — tutaj odwrotnie, chcemy ZAWSZE nadpisac, nawet pustym stringiem,
  // zeby nie zostawic "wyciekajacych" danych z poprzedniego QSO w lancuchu
  // wielu automatycznych QSO pod rzad (Call 1st).
  const callEl = document.getElementById('wj-log-call');
  if (callEl) callEl.value = msg.dxCall || '';
  const gridEl = document.getElementById('wj-log-grid');
  if (gridEl) gridEl.value = msg.dxGrid || '';
  const rstSentEl = document.getElementById('wj-log-rst-sent');
  if (rstSentEl) rstSentEl.value = msg.rstSent || '+00';
  const rstRcvdEl = document.getElementById('wj-log-rst-rcvd');
  if (rstRcvdEl) rstRcvdEl.value = msg.rstRcvd || '+00';
  const modeEl = document.getElementById('wj-log-mode');
  // Reset zamrozonych raportow po zakonczeniu QSO — inaczej wycieklyby
  // do nastepnego QSO w lancuchu Call 1st.
  _frozenRstSent = null; // reset po QSO
  _frozenRstRcvd = null;
  _updateMacroTexts(); // podglad makra 3 wraca do biezacego _lastDxSnr
  if (modeEl) modeEl.value = msg.mode || 'FT8';
  const commentEl = document.getElementById('wj-log-comment');
  if (commentEl) commentEl.value = '';

  window.UI?.showToast(`✓ QSO z ${msg.dxCall} zakonczone — sprawdz i zatwierdz w "MÓJ LOG QSO"`);

  // Dopisz OD RAZU do _workedCalls (zamiast czekac do 60s pollingu
  // _loadWorkedCalls) — inaczej ta sama stacja, gdyby zawolala CQ ponownie
  // za chwile, jeszcze przez do minuty wygladalaby jak NIEzrobiona w oknie
  // Band Activity (patrz _classify: CQ od juz-zrobionej stacji -> szary).
  if (msg.dxCall) {
    const band = window.UI?.getBandName ? window.UI.getBandName(S.freq) : '';
    _workedCalls.add(_workedKey(msg.dxCall, band, msg.mode || _decodeMode));
    _renderDecodes();
  }

  if (callEl) {
    // BEZ scrollIntoView: panel "MOJ LOG QSO" jest i tak zawsze widoczny w
    // tym layoucie (bez przewijania strony) - scrollIntoView({block:'center'})
    // na stronie ze skalowaniem transform (#app-scale) liczyl "wysrodkowanie"
    // zle i wywalal cale okno FT8/WSJTX wysoko w gore po kazdym zakonczonym
    // auto-QSO, zaslaniajac gorny pasek az do band-select. Samo podswietlenie
    // wystarcza do zwrocenia uwagi.
    callEl.classList.add('wj-pending-log-highlight');
    setTimeout(() => callEl.classList.remove('wj-pending-log-highlight'), 3000);
  }
}

function _onAutoQsoQueue(msg) {
  if (msg.queue !== undefined) _autoQsoQueue = msg.queue;
  if (msg.active !== undefined) _autoQsoPartner = msg.active;
  _renderAutoQsoPanel();
}

// Usuwa pojedyncza stacje z kolejki "Call 1st" (✕ na chipie).
function removeFromQueue(call) {
  window.WS?.send({ type: 'ft8_queue_remove', call });
}

// Oproznia cala kolejke "Call 1st" (przycisk "wyczysc" w naglowku panelu) —
// bez tego stare zgloszenia (stacje ktore odpowiedzialy na CQ dawno temu i
// moga juz nie sluchac) nie mialy zadnego sposobu opuszczenia kolejki poza
// usuwaniem pojedynczo, wiec po dluzszej sesji rosla i Call 1st w koncu
// wywolywal stary, nieaktualny znak.
function clearAutoQsoQueue() {
  window.WS?.send({ type: 'ft8_queue_clear' });
}

// Reczny "skip" biezacej stacji — porzuca aktywne QSO i od razu (bez
// czekania na 60s stall-timeout w backendzie) przechodzi do nastepnej
// stacji z kolejki Call 1st, jesli jakas czeka.
function skipAutoQso() {
  window.WS?.send({ type: 'ft8_abort_auto_qso' });
}

function _renderAutoQsoPanel() {
  const seqCb = document.getElementById('wj-auto-seq-toggle');
  if (seqCb) seqCb.checked = _autoSeqEnabled;
  const c1Cb = document.getElementById('wj-call1st-toggle');
  if (c1Cb) c1Cb.checked = _autoCall1st;

  const skipBtn = document.getElementById('wj-autoqso-skip');
  if (skipBtn) {
    const qsoActive = _autoQsoPartner && _autoQsoState !== 'IDLE' && _autoQsoState !== 'DONE';
    skipBtn.style.display = qsoActive ? '' : 'none';
  }

  const statusEl = document.getElementById('wj-autoqso-status');
  if (statusEl) {
    statusEl.classList.remove('active', 'done', 'error');
    if (!_autoSeqEnabled) {
      statusEl.textContent = 'Automatyka wylaczona — kliknij wiersz CQ aby odpowiedziec recznie';
    } else if (_autoQsoState === 'IDLE' || !_autoQsoPartner) {
      statusEl.textContent = _autoCall1st
        ? 'Auto-Sequencing aktywne — czekam na wywolanie (Call 1st wlaczone)'
        : 'Auto-Sequencing aktywne — kliknij wiersz CQ aby rozpoczac QSO';
    } else if (_autoQsoState === 'DONE') {
      statusEl.textContent = `✓ QSO z ${_autoQsoPartner} zakonczone`;
      statusEl.classList.add('done');
    } else {
      const stateLabels = {
        CALLING: 'wywoluje',
        REPORT_SENT: 'wyslano raport, czekam na potwierdzenie',
        RRR_SENT: 'wyslano RRR, czekam na 73',
      };
      statusEl.textContent = `QSO z ${_autoQsoPartner}: ${stateLabels[_autoQsoState] || _autoQsoState}`;
      statusEl.classList.add('active');
    }
  }

  const queueWrap = document.getElementById('wj-autoqso-queue-wrap');
  const queueEl = document.getElementById('wj-autoqso-queue');
  if (queueWrap && queueEl) {
    if (_autoQsoQueue.length > 0) {
      queueWrap.style.display = '';
      queueEl.innerHTML = _autoQsoQueue.map((call, i) =>
        `<span class="wj-queue-chip${i===0?' first':''}">${_esc(call)}` +
        `<span class="wj-queue-chip-x" title="Usuń ${_esc(call)} z kolejki" onclick="WSJTX.removeFromQueue('${_esc(call)}')">✕</span></span>`
      ).join('');
    } else {
      queueWrap.style.display = 'none';
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
    // UWAGA: NIE resetujemy tu FT8Timer.reset() na kazdy dekod - band
    // activity nie jest dowodem obecnosci OPERATORA (WSJT-X liczy brak
    // ruchu myszka/klawiatura, nie ruch na pasmie). Na zywym, zajetym
    // pasmie dekody przychodza co ~15s bez przerwy, wiec timer resetowany
    // TU nigdy realnie by nie doszedl do zera. reset() jest teraz wolane
    // z faktycznych akcji operatora - patrz _selectRow/sendTx.
    case 'wsjtx_decode':  _addDecode(msg); break;
    case 'wsjtx_clear':   _decodes = []; _renderDecodes(); break;
    case 'wsjtx_qso_logged': _onWsjtxQsoLogged(msg); break;
    case 'ft8_tx_status': _onFt8TxStatus(msg); break;
    case 'ft8_tx_error':
      // Wysylane gdy reczne TX FT8 (np. klik odpowiedzi na dekod) mialo
      // brakujace pola (callTo/callDe/report) - bez tego przycisk po prostu
      // nic nie robil, bez zadnego komunikatu bledu.
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
    case 'auto_qso_queue':   _onAutoQsoQueue(msg); break;
    case 'auto_qso_error':   window.UI?.showToast(`⚠ ${msg.error}`); break;
    case 'qso_logged':
      // Nowe QSO w prawdziwym logu (qso_db) — zarowno z automatyki jak i
      // recznego "+ LOG QSO" (patrz broadcast w /api/qsolog POST i w
      // _process_auto_qso, oba wysylaja ten sam typ). Broadcast idzie do
      // WSZYSTKICH klientow (nie tylko wlasciciela QSO) - filtrujemy tu,
      // bo user_id nie jest w tym konkretnym payloadzie z auto_qso, tylko
      // porownujemy przez to ze i tak liczy sie WYLACZNIE dla wlasnego
      // widoku (kazdy klient ma wlasny mini-log niezaleznie).
      _onQsoLogged(msg);
      break;
    case 'rotator_update':
      // Ten sam broadcast co duzy kompas w RADIO (rotormini.js) — zasila
      // tylko zywy odczyt ROTOR ---° i stan przyciskow SP/LP tutaj, nie
      // duplikuje calego widgetu. Wiele rotorow: bierzemy ten sam co
      // rotormini.js (pierwszy z listy / juz wybrany _rotorId).
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
    else if (_status.decoding)  { pill.textContent='⚡ DEKODUJE';  pill.className='wsjtx-status-pill decoding'; }
    else                        { pill.textContent='● ONLINE';     pill.className='wsjtx-status-pill online'; }
  }
  document.getElementById('wj-tx-indicator').style.display = _status.transmit ? '' : 'none';

  if (d.freq) {
    // UWAGA: wj-freq ma DWA niezalezne zrodla aktualizacji — to (z pakietow
    // zewnetrznego WSJT-X przez UDP, jesli jest uruchomiony) i _syncFreqFromRadio
    // (z AppState.freq, glownego radia, w tle co 500ms). Nie koliduja realnie,
    // bo oba ostatecznie odzwierciedlaja te sama fizyczna czestotliwosc radia —
    // ten kod tylko pokrywa przypadek gdy zewnetrzny WSJT-X faktycznie dziala
    // i moze byc nieznacznie szybszy/dokladniejszy zrodlem niz nasz polling.
    const mhz = (d.freq/1e6).toFixed(6).replace(/(\d+)\.(\d{3})(\d{3})/, '$1.$2.$3');
    const el = document.getElementById('wj-freq');
    if (el) el.textContent = mhz;
  }
  // UWAGA: 'wj-mode' (stary statyczny div) zastapiony przez przelacznik
  // FT8/FT4 (wj-mode-switch) sterowany przez setDecodeMode() — NIE
  // nadpisujemy go raportem trybu z zewnetrznego WSJT-X, bo to dwa rozne
  // zrodla prawdy (nasz wybor dekodowania vs to co zewnetrzny WSJT-X robi).
  // Pokazuj callsign/grid z WSJT-X w UI informacyjnie, ale NIE nadpisuj
  // _myCall/_myGrid jezeli user jest zalogowany z wlasnym profilem.
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

// ── Dekodowania ───────────────────────────────────────────────────────────────
function _classify(message) {
  const m  = message.toUpperCase();
  const mc = (_myCall||'').toUpperCase();
  if (mc && m.includes(mc)) return 'wj-mycall';
  if (m.startsWith('CQ ')) {
    // "juz zrobiona" ma sens pokazac TYLKO przy CQ — to jedyny moment gdy
    // operator faktycznie decyduje "klikac czy pomijac". Ta sama stacja
    // widziana w trakcie QSO Z KIMS INNYM (raport/73/RR73) nie niesie tej
    // informacji (nie ma czego klikac), wiec tam kolor CQ/73/DX zostaje
    // bez zmian - poprzednia wersja sprawdzala "worked" na samym koncu, PO
    // klasyfikacji CQ, wiec w praktyce PRAWIE NIGDY sie nie uruchamiala
    // (niemal kazdy dekod trafial w CQ/73/DX pierwszy) - stacja juz w logu
    // wygladala identycznie jak swieza.
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
    if (/^[A-R]{2}\d{2}([A-X]{2})?$/.test(p)) return p.slice(0,4);
  }
  return '';
}

// CQ MODIFIERS — musi byc w zgodzie z _CQ_MODIFIERS w qso_engine.py (backend
// tam ma pelniejsza/autorytatywna liste, to jest jej podzbior dla najczestszych
// przypadkow). Bez tego "CQ SOTA W1XYZ FN42"/"CQ POTA ..." byly parsowane jako
// call="SOTA"/"POTA" (dlugosc 4 > starego progu <=3 dla modifierow typu DX/NA),
// wiec klikniecie takiego CQ startowalo automatyczne QSO z fikcyjnym partnerem
// "SOTA" zamiast prawdziwym callsignem aktywatora.
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

// Czy `s` wyglada jak modifier CQ (POTA/DX/USA itp.), nie jak callsign.
// Ta sama heurystyka co is_cq_modifier() w qso_engine.py: whitelist LUB
// string do 6 znakow zlozony WYLACZNIE z liter (bez cyfr). Celowo ogolne,
// nie whitelist-only: prawdziwy znak amatorski zawsze ma cyfre, wiec kazdy
// czysto literowy modifier do 6 znakow (BOTA/GOTA/HOTA/... - caly rodzaj
// "*OTA" programow aktywacyjnych, nie tylko POTA/SOTA) jest bezpiecznie
// rozpoznawany bez wymieniania kazdego z osobna.
function _isCqModifier(s) {
  if (!s) return false;
  if (_CQ_MODIFIERS.has(s)) return true;
  return s.length <= 6 && /^[A-Z]+$/.test(s);
}

function _extractCall(msg) {
  // Format FT8: "CQ SP3GSK JO82" — wywolanie CQ — chcemy SP3GSK
  // Format FT8: "CQ SOTA SP3GSK/P JO82" — CQ z modifierem — chcemy SP3GSK/P (parts[2])
  // Format FT8: "SP3GSK SQ3MZM -05" — SQ3MZM wywoluje SP3GSK — chcemy SQ3MZM (parts[1])
  // Format FT8: "SQ3MZM SP3GSK R-12" — chcemy SQ3MZM (parts[0])
  const parts = msg.trim().toUpperCase().replace(/[<>]/g, '').split(/\s+/);
  if (!parts.length) return '';
  // CQ [MOD] CALL GRID
  if (parts[0] === 'CQ') {
    return parts.length >= 3 && _isCqModifier(parts[1]) ? parts[2] : parts[1];
  }
  // CALL_TO CALL_DE ... — zwroc CALL_DE (ten kto nadaje = nasz korespondent)
  if (parts.length >= 2) return parts[1];
  return parts[0];
}

function _addDecode(d) {
  // WLASNA TRANSMISJA (is_tx): dodaj do listy (zeby byla widoczna w oknie RX
  // obok odebranych), ale NIE przekazuj do Hound ani nie licz jako odebrany
  // dekod DX — to nasze TX, nie sygnal z pasma.
  if (!d.is_tx && _hound.active) _houndOnDecode(d);
  if (!d.is_tx) _decodeCount++;
  // Dodaj zawsze — isNew=false to replaye z poprzednich cykli (też warto pokazać)
  // Przy MSG_CLEAR WSJT-X czyści tabelę; my robimy to samo w handleWS('wsjtx_clear')
  _decodes.push(d);
  if (_decodes.length > MAX_DECODES) _decodes.shift();
  // Nowa aktywnosc odblokowuje panel RX FREQUENCY po recznym "wyczysc" (🗑) —
  // to mialo byc tymczasowe odgracenie widoku, nie trwale wylaczenie panelu
  // az do przeladowania strony.
  _rxFreqPanelCleared = false;
  _renderDecodes();
  _updateCount();
  _renderRxFreqPanel();
}

// Wylicza numer okna dekodowania (slot index) z timeStr (format HHMMSS, UTC)
// i trybu (FT8: okna 15s, FT4: okna 7.5s) — uzywane do wykrywania granicy
// miedzy kolejnymi okresami nadawania w Band Activity (linia przerywana).
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
  // WLASNA TRANSMISJA (is_tx): wyroznij wizualnie klasa wj-own-tx (inny kolor),
  // zeby user odroznil co NADAL od tego co ODEBRAL. Prefix ">>" oznacza nasze TX.
  const cls = d.is_tx ? 'wj-own-tx' : _classify(d.message);
  const snr = d.snr>=0 ? '+'+d.snr : String(d.snr);
  const dt  = d.deltaTime>=0 ? '+'+(d.deltaTime||0).toFixed(1) : (d.deltaTime||0).toFixed(1);
  const grid= _extractGrid(d.message);
  const txMark = d.is_tx ? '<span class="wj-tx-mark" style="color:#ff6; font-weight:bold;">▶ TX</span> ' : '';
  const txStyle = d.is_tx ? ' style="background:rgba(255,200,0,0.12); border-left:3px solid #fc0;"' : '';
  // Kraj TYLKO przy CQ — to jedyny moment gdy ma to praktyczne znaczenie
  // (kogo szukamy/wolamy), patrz komentarz przy _PREFIX_COUNTRY wyzej.
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
  // Filtr "tylko CQ" dotyczy WYLACZNIE Band Activity — RX Frequency
  // (_renderRxFreqPanel) przeszukuje _decodes bezposrednio, bez filtra,
  // bo ma pokazywac wszystko co jest na danej czestotliwosci niezaleznie
  // od tego czy to wolanie CQ czy odpowiedz w trwajacym QSO.
  const visible = _cqOnly
    ? _decodes.filter(d => (d.message||'').toUpperCase().startsWith('CQ '))
    : _decodes;
  if (!visible.length) {
    el.innerHTML = _cqOnly
      ? '<div class="wj-empty">Brak wywolan CQ</div>'
      : '<div class="wj-empty">Brak dekodowań — uruchom WSJT-X i kliknij ▶ START</div>';
    return;
  }
  const reversedVisible = [...visible].reverse();
  let prevSlot = null;
  el.innerHTML = reversedVisible.map((d) => {
    // Indeks w ORYGINALNEJ tablicy _decodes (nie w przefiltrowanej liscie) —
    // potrzebny zeby klikniecie wiersza (_selectRow) trafialo w poprawny
    // rekord nawet gdy filtr CQ jest aktywny.
    const idx = _decodes.indexOf(d);
    const slot = _windowSlot(d.timeStr, d.mode);
    let separator = '';
    if (slot !== null && prevSlot !== null && slot !== prevSlot) {
      // Granica miedzy okresami dekodowania (np. xx:15 -> xx:30 dla FT8) —
      // linia przerywana informujaca ze ponizej zaczyna sie NOWE okno.
      separator = '<div class="wj-period-sep"></div>';
    }
    prevSlot = slot;
    return separator + _decodeRowHtml(d, idx);
  }).join('');
}

// Rx Frequency panel: KOLEJKA (nie pojedynczy wiersz) dekodowan ktorych
// czestotliwosc (deltaFreq) jest blisko aktualnego znacznika RX (tolerancja
// +/- kilka Hz, zeby uwzglednic naturalny dryf/niedokladnosc dekodowania).
// Pokazuje jedno pod drugim, chronologicznie, zarowno to co ODEBRALISMY jak
// i to co SAMI NADALISMY (wpisy is_tx sa oznaczone "▶ TX" i innym tlem w
// _decodeRowHtml) — bez tego panel nadpisywal sie przy kazdym kolejnym
// dekodzie i nie dalo sie prosledzic co dokladnie dzieje sie na tej
// czestotliwosci (dostajemy vs nadajemy). Max RX_FREQ_QUEUE_MAX pozycji,
// najstarsze znikaja pierwsze (FIFO). Wlasna transmisja pojawia sie w tej
// kolejce naturalnie — backend broadcastuje ja jako wsjtx_decode (is_tx=true)
// juz w chwili PTT ON (_addDecode wywoluje ten render przy kazdym dekodzie),
// wiec nie trzeba osobnego "live preview" wiersza.
const RX_FREQ_TOLERANCE_HZ = 8;
const RX_FREQ_QUEUE_MAX = 20;

function _renderRxFreqPanel() {
  const el = document.getElementById('wj-rx-freq-row');
  if (!el) return;

  if (_rxFreqPanelCleared) {
    el.innerHTML = '<div class="wj-empty">Brak sygnału na czestotliwosci RX</div>';
    return;
  }

  const rxFreq = window.WSJTXScope?.getRxFreq?.();
  if (rxFreq == null) {
    el.innerHTML = '<div class="wj-empty">Brak sygnału na czestotliwosci RX</div>';
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
    el.innerHTML = '<div class="wj-empty">Brak sygnału na czestotliwosci RX</div>';
    return;
  }
  const queued = matches.slice(-RX_FREQ_QUEUE_MAX);
  el.innerHTML = [...queued].reverse().map((d) => {
    const idx = _decodes.indexOf(d);
    return _decodeRowHtml(d, idx);
  }).join('');
}

function _selectRow(el, idx) {
  // Klik operatora = dowod obecnosci dla timera bezpieczenstwa (WSJT-X Tx
  // Watchdog) - patrz komentarz przy 'wsjtx_decode' w handleWS.
  window.FT8Timer?.reset();
  document.querySelectorAll('.wj-decode-row.selected').forEach(r=>r.classList.remove('selected'));
  el.classList.add('selected');
  const d = _decodes[idx];
  if (!d) return;
  // Klik we WLASNA transmisje (is_tx) — nie rob nic (to nasz komunikat w
  // historii QSO, nie stacja do wolania). Tylko podswietl.
  if (d.is_tx) return;
  if (d.deltaFreq !== undefined) {
    // WOLANIE KOGOS: oba znaczniki (RX i TX) podazaja za korespondentem —
    // zgodnie ze specyfikacja WSJT-X. Wczesniej ustawialismy TYLKO RX, wiec
    // znaczniki pracowaly osobno (blad). TX podaza CHYBA ze zamrozony (Hold Tx
    // Freq) albo Hound — wtedy TX zostaje osobno (tez wg specyfikacji WSJT-X).
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
  // _setField() ustawia .value programowo, wiec NIE odpala oninput z HTML -
  // trzeba jawnie przeliczyc namiar anteny po kliknieciu wiersza.
  updateBeamRow();
  // Zaktualizuj tekst makr TX
  _updateMacroTexts();

  // Klikniecie na wywolanie CQ LUB na wiadomosc adresowana BEZPOSREDNIO do
  // nas (ktos juz nas zawolal - Tx1/raport/RRR/RR73/73 z naszym znakiem
  // jako call_to) przy wlaczonej automatyce startuje PELNE automatyczne
  // QSO (zamiast tylko wypelniac pola do recznego wyslania). Do 2026-08-15
  // dzialalo to WYLACZNIE dla "CQ ..." — stacja ktora zawolala nas wprost
  // (np. "SQ3MZM DL3MIB JN57", wchodzaca do kolejki Call 1st automatycznie
  // po stronie backendu) nie dawala sie recznie "przeskoczyc" klikniciem,
  // bo isCq bylo falszywe i klik tylko przestrajal RX/TX, bez wyslania
  // ft8_start_auto_qso w ogole — backend (handler "ft8_start_auto_qso" w
  // webapp.py) juz ODDAWNA poprawnie przyjmuje initial_decode DOWOLNEGO
  // typu wiadomosci, wiec to byla wylacznie blokada front-endu.
  // Guard call!=='CQ': wiadomosci niekompletne/skrocone (np. samo "CQ" bez
  // callsignu, blad dekodowania) daja call==='CQ' z _extractCall — to NIE
  // jest prawdziwy callsign partnera i nie powinno startowac automatyki.
  const upperMsg = (d.message||'').toUpperCase();
  const isCq = upperMsg.startsWith('CQ ');
  const isDirectToMe = _myCall && upperMsg.startsWith(_myCall.toUpperCase() + ' ');
  if ((isCq || isDirectToMe) && _autoSeqEnabled && call && call !== 'CQ') {
    // recvEpoch (dokladny czas odbioru TEGO dekodu od backendu, nie
    // "teraz") jest kluczowy — backend liczy z niego nasze okno TX. Bez
    // tego poprawny wybor okna dzialal tylko gdy klikniesz w ulamek
    // sekundy po pojawieniu sie dekodu — reczna reakcja czlowieka
    // (kilka-kilkanascie sekund) ladowala transmisje w zlym oknie
    // (kolizja z partnerem, QSO "nie startowalo").
    window.WS?.send({ type: 'ft8_start_auto_qso', callDe: call,
                       message: d.message, recvEpoch: d.recvEpoch, snr: d.snr });
  }
}

// Reczne wyszukanie stacji po znaku w polu DX (Enter/utrata fokusu) —
// odwrotny kierunek niz klik w wiersz: zamiast myszka wskazywac dekod,
// operator wpisuje znak, a jesli stacja jest AKTUALNIE widoczna w historii
// dekodow na pasmie, znacznik RX sam przestraja sie na jej czestotliwosc.
// Grid CELOWO nie jest wymagany do przestrojenia (uzupelniany tylko jesli
// akurat sie znajdzie w dopasowanym dekodzie) — to czysto informacyjne pole
// tutaj, nie warunek. TX marker NIE podaza (w odroznieniu od klikniecia
// wiersza) — wpisanie znaku to "szukam/nasluchuje", nie "zamierzam nadac
// teraz", te dwie intencje maja byc rozdzielone.
function searchDxCall(rawCall) {
  const call = (rawCall || '').trim().toUpperCase();
  if (!call) return;
  // Szukaj od NAJNOWSZEGO dekodu wstecz — jesli stacja pojawila sie
  // wielokrotnie, interesuje nas jej OSTATNIA znana czestotliwosc.
  for (let i = _decodes.length - 1; i >= 0; i--) {
    const d = _decodes[i];
    if (d.is_tx || d.deltaFreq === undefined) continue;
    const tokens = (d.message || '').toUpperCase().replace(/[<>]/g, '').split(/\s+/);
    if (tokens.includes(call)) {
      window.WSJTXScope?.setRxFreqManual(d.deltaFreq);
      const grid = _extractGrid(d.message);
      if (grid) _setField('wj-dx-grid', grid);
      return;
    }
  }
  window.UI?.showToast(`${call} nie widac aktualnie na pasmie`, 'error');
}

// ── Namiar anteny + rotor (wiersz ANTENA pod polem DX w SZYBKI ZAPIS QSO) ─────
// Naglowek/wiersz byl w HTML od dawna (beamheading.js liczyl azymut), ale
// nic nigdy nie wywolywalo BeamHeading.headingFor() ani nie laczylo tego z
// rotorem — czysto martwy fragment UI. Naprawione 2026-08-15: przelicza sie
// przy kazdej zmianie pola CALLSIGN DX/Grid DX (oninput w index.html + po
// programowym wypelnieniu przez _selectRow), a SP/LP wysylaja komende
// BEZPOSREDNIO do tego samego /api/rotator/<id>/position co duzy kompas w
// RADIO (rotormini.js) — bez osobnego potwierdzenia, klik = jedz. Zywa
// pozycja rotora aktualizuje sie przez TEN SAM broadcast "rotator_update"
// co duzy kompas (patrz case w handleWS), niezaleznie od tego czy operator
// ma akurat otwarta zakladke RADIO.
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
  // Wiersz jest STALE WIDOCZNY (patrz index.html) — tu tylko resetujemy do
  // placeholderow "---°" gdy nie ma jeszcze wybranej stacji, zamiast chowac
  // caly wiersz. SP/LP zostaja wylaczone naturalnie (_beamSpAz/_beamLpAz=null).
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
  if (!_rotorId) { window.UI?.showToast?.('⚠ Brak rotora', 'error'); return; }
  try {
    const r = await fetch(`/api/rotator/${_rotorId}/position`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({az, el: 0})
    });
    const d = await r.json();
    if (d.ok) window.UI?.showToast?.(`↻ Rotor → ${az}°${label ? ' ('+label+')' : ''}`);
    else window.UI?.showToast?.(`✗ ${d.error || 'Błąd'}`, 'error');
  } catch(e) { window.UI?.showToast?.(`✗ ${e.message}`, 'error'); }
}

function rotorGoBeam(which) {
  const az = which === 'lp' ? _beamLpAz : _beamSpAz;
  if (az == null) return;
  _rotorSetAz(az, which.toUpperCase());
}

// Reczne przesuniecie rotora na DOWOLNY azymut lub lokator — SP/LP daja
// tylko policzony kierunek na aktualnie wybrana stacje, brakowalo sposobu
// na wpisanie wlasnego celu (zglaszone na zywo). Wlasny modal (#rotor-manual-
// modal w index.html) zamiast prompt() — prompt() jest SYNCHRONICZNY i
// blokuje caly glowny watek JS dopoki user go nie zamknie, co na zywo
// zawieszalo streaming audio (WebAudio/WebRTC) do czasu zamkniecia okienka.
function rotorGoManual() {
  if (!_rotorId) { window.UI?.showToast?.('⚠ Brak rotora', 'error'); return; }
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
  if (az == null) { window.UI?.showToast?.('⚠ Nieprawidłowy format (stopnie lub lokator)', 'error'); return; }
  _rotorSetAz(az, raw.toUpperCase());
}

// ── TX makra ──────────────────────────────────────────────────────────────────
// Raport dla makra 3 (R+raport): potwierdzam odbior + moj ZMIERZONY raport
// sygnalu partnera (nie nasz grid!). JEDNO miejsce dla tej logiki, uzywane
// zarowno przez _txMacroParts (co faktycznie leci w eter) jak i
// _updateMacroTexts (podglad tekstu pod przyciskiem) — wczesniej to byly
// DWIE osobne kopie i tylko jedna z nich zamrazala raport, wiec podglad pod
// przyciskiem migotal/zmienial sie co dekod mimo ze faktyczna transmisja
// poprawnie trzymala jedna, zamrozona wartosc przez cale QSO.
// ZAMROZONY raport: gdy QSO aktywne, pokazuj WYLACZNIE potwierdzona przez
// backend zamrozona wartosc (albo neutralny placeholder do czasu az sie
// pojawi) - NIGDY _lastDxSnr w tej fazie. _lastDxSnr to SNR z OSTATNIO
// KLIKNIETEGO wiersza dekodu, ktory podczas pelnej automatyki (Call 1st,
// nikt nie klika recznie) jest zupelnie niepowiazany z biezacym partnerem
// - dawalo to wiarygodnie wygladajaca, ale przypadkowa liczbe zanim
// backend zdazyl zamrozic prawdziwy raport (np. w fazie wysylania
// wlasnego Tx1/grida, przed otrzymaniem raportu od partnera). Poza
// aktywnym QSO (reczne makro przed startem automatyki) — biezacy
// _lastDxSnr nadal ma sens, bo to jedyne dostepne zrodlo.
function _macro3Report() {
  if (_autoQsoState && _autoQsoState !== 'IDLE') {
    return _frozenRstSent || '+00';
  }
  const snr = _lastDxSnr != null ? _lastDxSnr : 0;
  const sign = snr >= 0 ? '+' : '-';
  return sign + String(Math.abs(snr)).padStart(2, '0');
}

// Strukturalna definicja makr F1-F7: [callTo, callDe, report].
// callTo/callDe='CQ' oznacza specjalne slowo CQ (nie callsign).
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
  window.FT8Timer?.reset();  // reczne TX = dowod obecnosci, patrz _selectRow
  const textEl = document.getElementById(`wj-tx${n}-text`);
  if (!textEl) return;
  const parts = _txMacroParts(n);
  if (!parts || !parts.callDe) {
    window.UI?.showToast('Ustaw najpierw swoj znak (MYCALL) w ustawieniach WSJT-X');
    return;
  }
  if (!parts.callTo && parts.callTo !== 'CQ') {
    window.UI?.showToast('Brak DX Call — wybierz stacje z listy dekodowan');
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

// Reaguj na statusy nadawania z backendu (PTT/audio sequence)
// Rozpoznaje ktory numer makra (1-5) odpowiada faktycznie wysylanej tresci
// `text` (np. "SP3GSK DL1ABC RRR"), na podstawie ostatniego "slowa"
// (raport/grid/RRR/73/RR73) — niezalezne od tego czy transmisja wystartowala
// z recznego klikniecia (sendTx) czy z automatyki QSO (backend generuje
// tresc bezposrednio, bez przechodzenia przez sendTx), wiec to JEDYNY
// niezawodny sposob podswietlenia "co realnie poleci w eter" w obu trybach.
function _macroNumberForText(text) {
  if (!text) return null;
  const upper = text.toUpperCase().trim();
  if (upper.startsWith('CQ ')) return 1;
  const lastWord = upper.split(/\s+/).pop();
  if (lastWord === 'RR73') return 6;
  if (lastWord === '73')   return 5;
  if (lastWord === 'RRR')  return 4;
  // Raport z prefixem "R" (np. "R-18") to potwierdzenie + zmierzony raport —
  // odrebne makro od pierwszego raportu/grida (ktory NIE ma prefixu R).
  if (/^R[+-]\d+$/.test(lastWord)) return 3;
  // Pozostale przypadki to pierwszy raport liczbowy (np. "-12") lub grid
  // (np. "JO72") wymiany — oba odpowiadaja makru 2.
  return 2;
}

// Licznik oczekiwania na okno TX. Wczesniej jedynym sygnalem byl znikajacy
// toast (parka sekund) — po jego zniknieciu przez reszte kilkunastu sekund
// oczekiwania UI nie dawalo ZADNEGO widocznego znaku ze cos sie dzieje, wiec
// wygladalo na zawieszone i operator recznie przerywal ("abort") zanim TX
// w ogole ruszyl. Trwaly, odliczajacy wskaznik (wj-tx-wait-status) zamiast
// tego, zeby bylo jasno widac ze to normalne odliczanie do granicy okna
// 15s/7.5s UTC, nie blad.
let _txWaitTimer = null;
let _txWaitTarget = 0; // Date.now() (ms) w ktorym TX ma faktycznie wystartowac

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
    el.textContent = `⏳ TX za ${remain.toFixed(1)}s — ${text}`;
    if (remain <= 0) _stopTxWaitCountdown();
  };
  tick();
  if (_txWaitTimer) clearInterval(_txWaitTimer);
  _txWaitTimer = setInterval(tick, 200);
}

function _onFt8TxStatus(d) {
  const btns = document.querySelectorAll('.wj-tx-btn');
  if (d.status === 'waiting') {
    window.UI?.showToast(`Czekam na okno 15s (${(d.waitSeconds||0).toFixed(1)}s) — ${d.text}`);
    _startTxWaitCountdown(d.waitSeconds||0, d.text);
    // Podswietl JUZ na etapie oczekiwania (nie dopiero przy starcie nadawania),
    // zeby bylo widac co poleci w eter zanim faktycznie zacznie sie PTT —
    // to wlasnie najbardziej przydaje sie w automatyce, gdzie oczekiwanie na
    // okno moze trwac kilka-kilkanascie sekund.
    const n = _macroNumberForText(d.text);
    btns.forEach(b=>b.classList.remove('active'));
    if (n) document.getElementById(`wj-tx${n}`)?.classList.add('active');
  } else if (d.status === 'starting') {
    window.UI?.showToast(`Nadaje: ${d.text}`);
    _stopTxWaitCountdown();
    const el = document.getElementById('wj-tx-wait-status');
    if (el) { el.style.display = ''; el.textContent = `📡 NADAJE — ${d.text}`; }
    const n = _macroNumberForText(d.text);
    btns.forEach(b=>b.classList.remove('active'));
    if (n) document.getElementById(`wj-tx${n}`)?.classList.add('active');
  } else if (d.status === 'error') {
    window.UI?.showToast(`Blad TX FT8: ${d.error}`);
    _stopTxWaitCountdown();
    btns.forEach(b=>b.classList.remove('active'));
  } else if (d.status === 'done') {
    _stopTxWaitCountdown();
    btns.forEach(b=>b.classList.remove('active'));
  }
}

// ── QSO Log ───────────────────────────────────────────────────────────────────
// Prawdziwy log to /api/qsolog (baza qso_db) - TA SAMA co pelna strona LOG QSO
// (qsolog.js), automatyczne zapisywanie QSO z FT8 i sprawdzanie "juz zrobione"
// (patrz _isWorkedHere/_loadWorkedCalls wyzej). Panel "SZYBKI ZAPIS QSO" tutaj
// dawniej pisal/czytal z ZUPELNIE INNEGO, osobnego magazynu (/api/log,
// self.log w webapp.py) - QSO dodane tym formularzem nigdy nie trafialy do
// prawdziwego logu, nie liczyly sie jako "juz zrobione", nie szly do
// CloudLog. Naprawione 2026-08-15 - patrz identyczna poprawka w webapp.py
// (usuniete /api/log*, self.log; dodany broadcast "qso_logged" do
// /api/qsolog POST tak samo jak juz mial auto-zapis z automatyki).

// Pobiera ostatnie MINI_LOG_MAX QSO do wstepnego wypelnienia mini-logu przy
// starcie strony - potem trzyma sie na biezaco przez broadcast "qso_logged"
// (_onQsoLogged), bez ponownego odpytywania.
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

// Nowe QSO w prawdziwym logu (auto-zapis z automatyki LUB reczne "+ LOG QSO"
// ponizej - oba wysylaja ten sam broadcast, patrz komentarz przy handleWS).
// Broadcast idzie do WSZYSTKICH polaczonych klientow (hub.broadcast nie jest
// per-user), wiec filtrujemy po user_id - inaczej QSO innego zalogowanego
// operatora wskoczyloby do naszego mini-logu.
function _onQsoLogged(msg) {
  const qso = msg.qso;
  if (!qso || qso.user_id !== window.CurrentUser?.id) return;
  _miniLogEntries.unshift(qso);
  if (_miniLogEntries.length > MINI_LOG_MAX) _miniLogEntries.length = MINI_LOG_MAX;
  _renderMiniLog();
}

// Mini-log pod "SZYBKI ZAPIS QSO" (panel automatyki) — czysto informacyjny
// podglad ostatnich QSO z prawdziwego logu. Zero edycji/usuwania tutaj
// CELOWO - do tego jest osobna strona LOG QSO (pelna edycja i zapis).
function _renderMiniLog() {
  const el = document.getElementById('wj-minilog-body');
  if (!el) return;
  if (!_miniLogEntries.length) {
    el.innerHTML = '<tr><td colspan="4" style="color:#333;text-align:center;padding:8px;">Brak zalogowanych QSO</td></tr>';
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
  if (!call) { window.UI?.showToast('✗ Wpisz callsign DX', 'error'); return; }

  const now    = new Date();
  const freq   = S?.freq || 0;
  const band   = _freqToBand(freq);
  const mode   = document.getElementById('wj-log-mode')?.value || 'FT8';
  const grid   = document.getElementById('wj-log-grid')?.value.trim().toUpperCase() || '';
  const rstS   = document.getElementById('wj-log-rst-sent')?.value || '+00';
  const rstR   = document.getElementById('wj-log-rst-rcvd')?.value || '+00';
  const comment= document.getElementById('wj-log-comment')?.value.trim() || '';

  // Format qso_db (qso_date=YYYYMMDD, time_on=HHMMSS, bez separatorow) -
  // patrz add_qso() w qso_db.py i identyczny sposob budowania w
  // _process_auto_qso (webapp.py, auto-zapis QSO).
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
      window.UI?.showToast(`✓ QSO zalogowane: ${call}`);
      // Dopisz OD RAZU do _workedCalls (zamiast czekac na 60s polling)
      // — inaczej ta sama stacja wygladalaby jak niezrobiona w Band
      // Activity jeszcze przez chwile (patrz identyczny komentarz w
      // _onAutoQsoComplete). Mini-log sam sie odswieży przez broadcast
      // "qso_logged" (_onQsoLogged) - nie trzeba go tu recznie dopisywac.
      _workedCalls.add(_workedKey(call, band, mode));
      _renderDecodes();
      // Wyczyść formularz
      ['wj-log-call','wj-log-grid','wj-log-rst-rcvd','wj-log-comment'].forEach(id=>{
        const el=document.getElementById(id); if(el) el.value='';
      });
    } else {
      window.UI?.showToast(`✗ ${d.error || 'Błąd logowania'}`, 'error');
    }
  } catch(e) { window.UI?.showToast('✗ Błąd logowania', 'error'); }
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
    window.UI?.showToast('✓ Wyeksportowano log (ADIF)');
  } catch(e) { window.UI?.showToast('✗ Błąd eksportu', 'error'); }
}

// QSO zalogowane przez WSJT-X automatycznie (z pakietu UDP)
function _onWsjtxQsoLogged(d) {
  // WSJT-X zalogowalo QSO → zapisz w /api/qsolog (per-user przez JWT)
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
  if (!qso.call) return;  // brak znaku — nie loguj
  const token = localStorage.getItem('token') || '';
  fetch('/api/qsolog', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json',
      ...(token ? {'Authorization': `Bearer ${token}`} : {}) },
    body: JSON.stringify(qso),
  }).then(r => r.json()).then(res => {
    if (res.ok) {
      window.UI?.showToast(`✓ QSO zalogowane: ${qso.call} ${qso.band} ${qso.mode}`);
      // Odswiez tabele jezeli jestesmy na stronie LOG
      if (document.getElementById('page-log')?.classList.contains('active')) {
        window.QSOLog?.load?.();
      }
    }
  }).catch(() => {});
}

// ── FT8 Timer bezpieczeństwa ─────────────────────────────────────────────────
window.FT8Timer = (() => {
  let _durationMs  = 6 * 60 * 1000;  // domyslnie 6 min
  let _remaining   = 0;
  let _interval    = null;
  let _active      = false;
  let _userCanEdit = false;
  let _warnShown   = false;
  let _expired     = false;  // wygasl, czeka na potwierdzenie (patrz confirm()/reset())

  async function init() {
    // Pobierz ustawienia timera dla aktualnego usera
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
    // Startuj licznik - Call 1st wlaczony (glowna automatyka) lub Hound
    // wlaczony, patrz _onAutoSeqStatus/toggleHound.
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
    // Zatrzymaj licznik (Call 1st/Hound wylaczony, albo wewnetrzne uzycie
    // przy wygasnieciu - patrz _tick). Explicit disarm (Call 1st off) NIE
    // zeruje _expired samo z siebie - o to dba wylacznie confirm()/start(),
    // zeby stan "czeka na potwierdzenie" nie zniknal po cichu bez realnego
    // potwierdzenia operatora.
    _active = false;
    clearInterval(_interval);
    _interval = null;
    _remaining = 0;
    const btn = document.getElementById('ft8-timer-confirm');
    if (btn) btn.style.display = 'none';
    _updateDisplay();
  }

  // Jedyne zrodlo prawdy dla "operator potwierdzil obecnosc" - wolane
  // zarowno z przycisku POTWIERDZ jak i z reset() gdy operator dziala mimo
  // wygasnietego timera (patrz reset() nizej). Powiadamia TEZ backend
  // (ft8_timer_confirm) - bez tego automat zostalby zablokowany na
  // zawsze, bo backend NIE widzi lokalnych klikniec w przegladarce.
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
    window.UI?.showToast('✓ Timer zresetowany');
  }

  function reset() {
    // Jesli timer wygasl i czeka na potwierdzenie - JAKAKOLWIEK akcja
    // operatora (klik wiersza, TX makro) JEST tym potwierdzeniem, taka
    // sama jak klikniecie POTWIERDZ. Bez tego operator musialby zawsze
    // trafic dokladnie w mala plaszczke przycisku, zamiast po prostu
    // wrocic do normalnej pracy.
    if (_expired) { confirm(); return; }
    // Reset bez zatrzymania — po kazdej akcji uzytkownika, TYLKO gdy timer
    // faktycznie aktywny (Call 1st wlaczony) - patrz _onAutoSeqStatus.
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

    // Ostrzezenie przy 1 min pozostalej
    if (_remaining <= 60000 && !_warnShown) {
      _warnShown = true;
      window.UI?.showToast('⚠️ FT8 Timer: 1 minuta do zatrzymania TX!', 'error');
      // Pokaz przycisk potwierdzenia
      const btn = document.getElementById('ft8-timer-confirm');
      if (btn) btn.style.display = 'inline-block';
    }

    if (_remaining <= 0) {
      // Czas minał — zatrzymaj TX i zablokuj automatyke do potwierdzenia
      stop();
      _expired = true;
      _stopTX();
      window.UI?.showToast('⛔ FT8 Timer: TX zatrzymany — potwierdź obecność!', 'error');
      return;
    }

    _updateDisplay();
  }

  function _stopTX() {
    // Zatrzymaj RZECZYWISTE nadawanie (PTT + silnik QSO) - poprzednia
    // wersja wolala tylko stopTx() (kosmetyczny reset podswietlenia
    // przycisku, NIE dotyka PTT ani silnika) i houndStop(), wiec dla
    // glownej automatyki (Call 1st) w ogole nic realnie nie przerywala.
    // haltTx() to ten sam pelny halt co przycisk HALT TX (PTT off +
    // abort_qso + uniewaznienie zaplanowanych w locie retransmisji).
    window.WSJTX?.haltTx();
    if (window.WSJTX?.houndStop) window.WSJTX.houndStop();
    // Poinformuj backend - dopoki nie przyjdzie ft8_timer_confirm, automat
    // ma PRZESTAC reagowac na nowych wolajacych mimo wlaczonego Call 1st
    // (patrz _ft8_operator_present w webapp.py). Samo haltTx() przerywa
    // TYLKO biezaca transmisje - bez tej dodatkowej blokady automat
    // złapałby kolejnego wolajacego juz za chwile, co czyni caly timer
    // bezuzytecznym (dokladnie ten problem zglosil uzytkownik: timer mial
    // pilnowac max czasu nadawania, a przerywal co najwyzej jedna wysylke).
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
    // Kolor: zielony → żółty (<2min) → czerwony (<1min)
    el.style.color = _remaining < 60000  ? 'var(--red)'
                   : _remaining < 120000 ? 'var(--amber)'
                   : 'var(--green)';
  }

  return { init, start, stop, confirm, reset };
})();

// ── Fox / Hound mode ─────────────────────────────────────────────────────────
// Zgodne z "FT8 DXpedition Mode User Guide" (K1JT, 2018) i "The FT4 and FT8
// Communication Protocols" (K9AN/G4WJS/K1JT, QEX Jul/Aug 2020). Fox mode
// (druga strona: DXpedycja) CELOWO nigdy nie bedzie implementowany w tym
// projekcie - to dziala wylacznie po stronie Hounda (stacji wolajacej).
const _hound = {
  active:         false,
  foxCall:        '',
  step:           0,        // 0=idle 1=wolanie/czekam na Fox 3=wysylam R+rpt 4=czekam na RR73
  txFreq:         1500,     // Hz — wolanie Foxa (spec: 1000-4000 Hz)
  reportBaseFreq: 0,        // Hz — freq na ktorej Fox NAS wolal (nominalnie 300-540 Hz)
  reportFreq:     0,        // Hz — aktualnie uzywana (moze byc przesunieta po retry)
  attempts:       0,        // ile razy wyslalismy R+rpt w tym QSO (tylko do UI/przesuniecia freq)
  foxReport:      '',       // raport od Foxa np. "-13"
  timer:          null,     // timer 2-minutowego potwierdzenia obecnosci operatora
  cycleTimer:     null,     // timer sprawdzajacy czy trzeba ponowic TX (brak odpowiedzi Foxa)
  lastConfirm:    0,        // czas ostatniego potwierdzenia operatora
  lastTxAt:       0,        // czas ostatniej wyslanej wiadomosci Hounda (do retry co cykl)
};

const HOUND_SHIFT_HZ = 300;   // spec: retry R+rpt przesuwa sie o 300Hz wyzej/nizej
const HOUND_CYCLE_MS = { FT8: 15000, FT4: 7500 };

function _houndCycleMs() {
  return HOUND_CYCLE_MS[_decodeMode] || 15000;
}

// Retry R+rpt: attempt 1 = base freq (bez przesuniecia), 2,3,4,... naprzemiennie
// +300/-300/+600/-600... (spec: "subsequent transmissions will be moved 300 Hz
// higher or lower" - kierunek nie jest narzucony, wiec naprzemiennie zeby
// pozostac blisko oryginalnego slotu Foxa zamiast uciekac w jedna strone).
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
      window.UI?.showToast('Wpisz znak Foxa w polu DX!', 'error');
      document.getElementById('wj-hound-toggle').checked = false;
      _hound.active = false;
      return;
    }
    _hound.foxCall    = foxCall;
    _hound.step       = 1;
    _hound.attempts   = 0;
    _hound.txFreq     = 1500;   // domyslna czestotliwosc wolania (w zakresie 1000-4000)
    _hound.lastConfirm = Date.now();
    _houndUpdateUI();
    _houndStartCalling();
    window.FT8Timer?.start();  // Uruchom timer bezpieczenstwa
    // Wewnetrzny timer Hound — sprawdza potwierdzenie co 30s
    clearInterval(_hound.timer);
    _hound.timer = setInterval(_houndCheckConfirm, 30000);
    // Ponawianie TX co cykl (15s/7.5s) jesli Fox nie odpowiedzial - spec:
    // Hound "may keep calling until he answers" (wolanie) i "will repeat his
    // transmission of Tx3" bez limitu prob (R+rpt). Bez tego Hound wolal
    // DOKLADNIE RAZ i milkl, jesli Fox nie zdazyl odpowiedziec za pierwszym
    // razem (w ruchliwym pile-upie niemal pewne) - zglosone na zywo jako
    // "tryb byl dzialajacym TX", wiec to realna regresja funkcjonalna.
    clearInterval(_hound.cycleTimer);
    _hound.cycleTimer = setInterval(_houndCycleCheck, 3000);
    window.UI?.showToast(`🦊 Hound mode: szukam ${foxCall} — TX ${_hound.txFreq} Hz`);
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
  window.FT8Timer?.stop();  // Zatrzymaj timer
  const _ht = document.getElementById('wj-hound-toggle');
  if (_ht) _ht.checked = false;
  _houndUpdateUI();
  window.UI?.showToast('Hound mode wyłączony');
}

// Potwierdzenie operatora co 2 min (wymóg protokołu)
function houndConfirm() {
  _hound.lastConfirm = Date.now();
}

function _houndCheckConfirm() {
  if (!_hound.active) return;
  const elapsed = Date.now() - _hound.lastConfirm;
  if (elapsed > 120000) {  // 2 minuty
    window.UI?.showToast('⚠️ Hound: potwierdź obecność (2 min) — TX wstrzymany', 'error');
    _hound.step = 0;
    _houndUpdateUI();
  }
}

// Sprawdzane co 3s: czy minal caly cykl TX (15s FT8 / 7.5s FT4) bez zadnej
// wyslanej wiadomosci Hounda w biezacym kroku? Jesli tak - ponow (krok 1:
// ta sama freq, krok 3: R+rpt z przesunieta freq, patrz _houndShiftedFreq).
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

// Główna logika — wywoływana po każdym odebranym dekodzie w trybie Hound
function _houndOnDecode(decoded) {
  if (!_hound.active || !_hound.foxCall) return;

  const fox = _hound.foxCall.toUpperCase();
  const my  = (_myCall || '').toUpperCase();
  // Hound uzywa BASE call Foxa, nie pelnego zlozonego znaku (spec: "Hounds
  // use Fox's base call, not his full compound callsign") - np. "KH7Z" w
  // wiadomosci vs "KH1/KH7Z" wpisane w polu DX. Zwykle zawieranie substringow
  // wystarcza (dziala w obie strony bez wzgledu ktora forma jest krotsza).
  const isFoxCall = (call) => call === fox || fox.includes(call) || call.includes(fox);

  // Wiadomosc typu 0.1 (i3=0, n3=1) - Fox JEDNOCZESNIE zamyka QSO jednego
  // Hounda (RR73) i zaprasza kolejnego z raportem, w JEDNEJ transmisji.
  // Pole "message" tu NIE jest wiarygodne do parsowania regexem (patrz
  // unpack_type0_1 w unpack.rs) - uzywamy ustrukturyzowanych pol wprost.
  if (decoded.isDxpedition) {
    const call1 = (decoded.call_to || '').toUpperCase(); // dostaje RR73
    const call2 = (decoded.call_de || '').toUpperCase(); // dostaje raport
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
      _houndUpdateUI();
      _houndSendReport();
      return;
    }
    return; // dotyczy innego Hounda - nas nie obchodzi
  }

  // Standardowa (i3=1) wiadomosc: "TO DE report" - parsowanie tokenowe,
  // nie luzny substring-check (ten dawal false-positive gdy inny fragment
  // tekstu przypadkiem zawieral "73"). Fox->Hound: TO=my call, DE=Fox
  // (base call) - wczesniejsza wersja tego kodu sprawdzala pozycje
  // ODWROTNIE (spodziewala sie "FOXCALL MYCALL ..."), przez co nigdy nie
  // wykrywala prawdziwej odpowiedzi Foxa.
  const parts = (decoded.message || '').toUpperCase().trim().split(/\s+/);
  if (parts.length < 3) return;
  const [callTo, callDeRaw, tail] = parts;
  const callDe = callDeRaw.replace(/[<>]/g, '');
  if (callTo !== my || !isFoxCall(callDe)) return;

  // Krok 1: Fox odpowiada na nasze CQ z raportem SNR -> wysylamy R+rpt
  if (_hound.step === 1 && /^[+-]\d{1,2}$/.test(tail)) {
    _hound.foxReport      = tail;
    _hound.reportBaseFreq = decoded.deltaFreq || 400; // freq na ktorej Fox NAS wolal
    _hound.reportFreq     = _hound.reportBaseFreq;
    _hound.step           = 3;
    _hound.attempts        = 0;
    _hound.lastConfirm     = Date.now();
    _houndUpdateUI();
    _houndSendReport();
    return;
  }

  if (_hound.step === 3 || _hound.step === 4) {
    // Krok 4: Fox potwierdza RR73/73/RRR (niepolaczona wiadomosc) - QSO zaliczone
    if (tail === 'RR73' || tail === '73' || tail === 'RRR') {
      _hound.step = 4;
      _houndUpdateUI();
      _houndQSOComplete();
      return;
    }
    // Fox ponownie daje ten sam raport (nie odebral naszego R+rpt) - powtorz
    if (_hound.step === 3 && /^[+-]\d{1,2}$/.test(tail)) {
      _hound.lastConfirm = Date.now();
      _houndSendReport();
    }
  }
}

function _houndStartCalling() {
  if (!_hound.active || _hound.step !== 1) return;
  // TX1: "KH1/KH7Z SP3GSK KO02" na freq 1000-4000 Hz. Spec nie nakazuje
  // zmiany freq przy braku odpowiedzi (to opcjonalna decyzja operatora w
  // prawdziwym WSJT-X) - ponawiamy na TEJ SAMEJ freq co poprzednio.
  _hound.lastTxAt = Date.now();
  _houndUpdateUI();
  _houndSendMsg(_hound.foxCall, _myCall, (_myGrid || '').trim(), false, _hound.txFreq);
}

function _houndSendReport() {
  if (!_hound.active) return;
  _hound.attempts++;
  // TX3: "KH1/KH7Z SP3GSK R-13" - pierwsza proba na freq Foxa (nominalnie
  // 300-540 Hz), kazda kolejna PRZESUNIETA o 300Hz (spec, wymagane -
  // "will be moved", nie opcjonalne). Ponawiamy BEZ LIMITU az do RR73 -
  // spec: "WSJT-X will send this message even if... you have not called Fox
  // for several Tx sequences" - to Fox ma wlasny limit (3 proby + timeout
  // 3 min), Hound nie poddaje sie sam.
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
  window.UI?.showToast(`✓ QSO z ${foxCall} zaliczone! RR73 odebrane.`);
}

function _houndAutoLog(foxCall) {
  // Ustaw CALL w quick-log i wywołaj zapis
  const callEl = document.getElementById('qlog-call');
  const rstEl  = document.getElementById('qlog-rst-s');
  const rstREl = document.getElementById('qlog-rst-r');
  if (callEl) callEl.value = foxCall;
  if (rstEl)  rstEl.value  = _hound.foxReport || '-99';
  if (rstREl) rstREl.value = _hound.foxReport || '-99';
  window.QSOLog?.quickLog?.();
}

function _houndSendMsg(callTo, callDe, report, rFlag, audioFreqHz) {
  // Ta sama sciezka WS co zwykle TX FT8 (patrz sendMacro() wyzej) — jedyna
  // faktycznie dzialajaca. Wczesniej Hound wysylal POST na /api/wsjtx/tx,
  // ktorego backend nigdy nie mial (0 wynikow w webapp.py) — TX Hounda bylo
  // wiec cichym no-opem (tylko console.warn), mimo ze panel wygladal na
  // aktywny. audioFreq nadpisuje freq TX przed enkodowaniem — patrz komentarz
  // przy "elif t == ft8_tx" w webapp.py, ktory explicite nazywa Hound mode.
  window.WS?.send({
    type: 'ft8_tx',
    callTo, callDe, report,
    rFlag: !!rFlag,
    audioFreq: audioFreqHz,
  });
}

function _houndUpdateUI() {
  const active  = _hound.active;
  const toggle  = document.getElementById('wj-hound-toggle');
  const statusEl = document.getElementById('wj-autoqso-status');

  if (toggle) toggle.checked = active;

  // Podswietl checkbox w topbarze gdy aktywny
  const label = toggle?.closest('label');
  if (label) {
    label.style.background = active ? 'rgba(255,140,0,0.2)' : '';
    label.style.borderColor = active ? '#f90' : 'rgba(255,140,0,0.3)';
  }

  if (!statusEl) return;
  if (!active) {
    statusEl.style.color = '';
    statusEl.textContent = 'Automatyka wylaczona — kliknij wiersz CQ aby odpowiedziec recznie';
    return;
  }

  const stepNames = ['', 'Wołanie Foxa', '', 'Wysyłam R+rpt', 'Czekam na RR73'];
  const freq = _hound.step === 3 ? _hound.reportFreq : _hound.txFreq;
  const step = stepNames[_hound.step] || '...';
  const attemptTxt = _hound.step === 3 ? ` | próba ${_hound.attempts}` : '';
  statusEl.style.color = '#f90';
  statusEl.textContent = `🦊 HOUND: ${_hound.foxCall} | ${step} | TX:${freq}Hz${attemptTxt}`;
}

// ── Pomocnicze ────────────────────────────────────────────────────────────────
function _freqToBand(hz) {
  if (!hz) return '';
  // 160m: wezsza (realna alokacja PL/EU) granica, ta sama co webapp.py/
  // dxcluster.py/ui.js::getBandName - byla tu rozjechana (1800000 zamiast
  // 1810000), ten sam blad co juz raz naprawiony gdzie indziej w projekcie.
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
  if (el) el.textContent = _decodeCount + ' dekodowań';
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
  toggleTxFreeze, toggleFakeSplit, toggleCqOnly, toggleAutoSeq, toggleCall1st, setDecodeMode,
  tuneToBand, setTxPeriod,
  setTxFreqManual, setRxFreqManual, rxEqTx, txEqRx,
  _selectRow, addLog, exportAdif,
  toggleHound, houndStop, houndConfirm,
  removeFromQueue, clearAutoQsoQueue, skipAutoQso,
  resetPaletteAdjust,
};

})();
