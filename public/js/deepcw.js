/**
 * deepcw.js — DeepCW client
 * Streamuje PCM Float32 przez WebSocket do serwera (Python+ONNX)
 */
(function () {
'use strict';

let _ctx     = null;
let _proc    = null;
let _src     = null;
let _stream  = null;
let _running = false;

async function startDecoding(source) {
  if (_running) return;
  // Dekoder pracuje na SERWERZE, na surowym audio prosto z karty radia.
  // Przegladarka juz NIE wysyla dzwieku: przechodzil przez kodek Opus, ktory
  // rozmywal krawedzie kluczowania (kontrast obwiedni 6.4x zamiast >20x)
  // i model dostawal rozmyty sygnal — stad kasza mimo mocnej stacji.
  try {
    window.WS?.send({ type: 'cw_rx_enable', enabled: true });
    _running = true;
    _updateBtn(true);
    _setLog('🎧 Aktywny (audio surowe z karty radia)');
  } catch(e) {
    _setLog('✗ ' + e.message);
    console.error('[DeepCW]', e);
  }
}

function stopDecoding() {
  _running = false;
  try { window.WS?.send({ type: 'cw_rx_enable', enabled: false }); } catch(e) {}
  _updateBtn(false);
  _setLog('⏹ Zatrzymano.');
}

async function _startDecodingOLD(source) {
  if (_running) return;
  try {
    if (source === 'radio') {
      // Użyj istniejącego AudioContext radia (przez window lub z _masterGain.context)
      const radioCtx = window._masterGain?.context || window.audioCtx;
      if (!radioCtx) { _setLog('✗ Brak audio radia — uruchom radio najpierw'); return; }
      _ctx = radioCtx;
      if (_ctx.state === 'suspended') { try { await _ctx.resume(); } catch(e){} }

      // Utwórz ScriptProcessor w kontekście radia
      _proc = _ctx.createScriptProcessor(4096, 1, 1);

      // Podepnij _masterGain → _proc → destination
      const gain = window._masterGain || _ctx.destination;
      gain.connect(_proc);
      _proc.connect(_ctx.destination);

      const sampleRate = _ctx.sampleRate;
      _proc.onaudioprocess = (e) => {
        if (!_running) return;
        const data = e.inputBuffer.getChannelData(0);
        _sendPCM(data, sampleRate);
        _updateVU(data);
      };
    } else {
      // Mikrofon — własny AudioContext
      _ctx = new AudioContext({ sampleRate: 8000 });
      _stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false }
      });
      _src  = _ctx.createMediaStreamSource(_stream);
      _proc = _ctx.createScriptProcessor(4096, 1, 1);
      _src.connect(_proc);
      _proc.connect(_ctx.destination);

      const sampleRate = _ctx.sampleRate;
      _proc.onaudioprocess = (e) => {
        if (!_running) return;
        const data = e.inputBuffer.getChannelData(0);
        _sendPCM(data, sampleRate);
        _updateVU(data);
      };
    }

    _running = true;
    _updateBtn(true);
    _setLog(`🎧 Aktywny (${source === 'radio' ? 'radio' : 'mikrofon'}, ${_ctx.sampleRate}Hz)`);
  } catch(e) {
    _setLog('✗ ' + e.message);
    console.error('[DeepCW]', e);
  }
}

function toggleStart() {
  if (_running) stopDecoding();
  else startDecoding(document.getElementById('deepcw-source')?.value || 'mic');
}

// ── Wyślij PCM przez główny WS ────────────────────────────────────────────────
function _sendPCM(f32data, srcRate) {
  const ws = window._mainWS;
  if (!ws || ws.readyState !== WebSocket.OPEN) return;

  const header = new ArrayBuffer(5);
  const hv = new DataView(header);
  hv.setUint8(0, 0xC1);
  hv.setUint32(1, srcRate, true);  // little-endian

  const pcm = f32data.buffer.slice(f32data.byteOffset, f32data.byteOffset + f32data.byteLength);
  const msg = new Uint8Array(5 + pcm.byteLength);
  msg.set(new Uint8Array(header), 0);
  msg.set(new Uint8Array(pcm),    5);
  ws.send(msg.buffer);
}

// ── Odbierz tekst z serwera ───────────────────────────────────────────────────
let _rxCount = 0;
let _liveLine = null;   // element biezacej linii (podmieniany, nie doklejany)
function handleText(msg1, msg2) {
  // Obsluga dwoch formatow wywolania:
  //   handleText({block, preview, close})  — nowy, z ws.js
  //   handleText(text, preview)            — stary, dla zgodnosci
  let block, preview, close;
  if (msg1 && typeof msg1 === 'object') {
    ({ block, preview, close } = msg1);
  } else {
    block = msg1; preview = msg2; close = false;
  }

  const el = document.getElementById('deepcw-output');
  if (!el) return;

  // Podglad "NA ZYWO" ma wlasna linie pod oknem.
  const pv = document.getElementById('deepcw-preview');
  if (pv) pv.textContent = preview || '';

  // Koniec transmisji — zamknij biezaca linie, nastepna stacja zacznie nowa.
  if (close) {
    _liveLine = null;
    return;
  }

  if (block) {
    _rxCount++;
    _setLog(`🎧 Dekodowanie… (odebrano ${_rxCount} fragm.)`);
    // PODMIANA calej linii zamiast doklejania fragmentow. Silnik przysyla
    // pelny biezacy odczyt, wiec nadpisujemy nim ostatnia linie — dzieki temu
    // tekst nie jest sklejany ze strzepow i nie ma powtorzen ani dziur.
    if (!_liveLine || !_liveLine.isConnected) {
      _liveLine = document.createElement('div');
      el.appendChild(_liveLine);
    }
    _liveLine.innerHTML = _colorize(block);
  }
  // Ogranicz do ostatnich ~500 znakow
  if (el.textContent.length > 800) {
    // Usun pierwsze dziecko az do 600 znakow
    while (el.textContent.length > 600 && el.firstChild
           && el.firstChild !== _liveLine) {
      el.removeChild(el.firstChild);
    }
  }
  el.scrollTop = el.scrollHeight;
}

// ── Baza znanych znakow (walidacja kolorowania) ───────────────────────────────
// Zrodla: dekody FT8 + log QSO (z serwera), spoty DX cluster (lokalne, kazdy
// user ma swoj cluster). Znak z bazy kolorujemy PEWNIE; przepracowany innym
// odcieniem — operator od razu widzi czy warto wolac.
let _knownCalls  = new Set();
let _workedCalls = new Set(); // klucze CALL|BAND — kazde pasmo to nowa lacznosc

function _workedKeyCW(call, band) {
  return `${(call||'').toUpperCase()}|${(band||'').toUpperCase()}`;
}

async function refreshKnownCalls() {
  try {
    const token = localStorage.getItem('token') || '';
    const hdr = token ? {'Authorization': `Bearer ${token}`} : {};
    const [k, w] = await Promise.all([
      fetch('/api/deepcw/known_calls', {headers: hdr}).then(r => r.json()).catch(() => ({})),
      fetch('/api/qsolog/calls',       {headers: hdr}).then(r => r.json()).catch(() => ({})),
    ]);
    if (k.calls) _knownCalls  = new Set(k.calls.map(c => c.toUpperCase()));
    // w.calls to lista {call, mode, band} (patrz qso_db.py::worked_calls) —
    // NIE lista stringow. c.toUpperCase() na obiekcie rzucalo TypeError,
    // polykane przez catch(e){} ponizej, wiec _workedCalls nigdy sie nie
    // wypelnial i szarzenie "juz w logu" bylo martwe. Band w kluczu, zeby
    // stacja zrobiona na jednym pasmie nie gasla jako dupe na innym.
    if (w.calls) _workedCalls = new Set(w.calls.map(c => _workedKeyCW(c.call, c.band)));
  } catch(e) {}
}

// Spoty z DX clustera usera — wolane z modulu clustera gdy przyjdzie spot.
function addClusterSpots(calls) {
  for (const c of calls || []) {
    const u = (c || '').trim().toUpperCase();
    if (u.length >= 3) _knownCalls.add(u);
  }
}

function _colorize(text) {
  // Koloruj slowa kluczowe CW
  const COLORS = {
    // Zakonczenia QSO
    '73':   '#fa0',  // pomaranczowy
    'TU':   '#fa0',
    'SK':   '#fa0',
    'AR':   '#fa0',
    // Raporty RST obsługuje regex poniżej
    // Wywolanie
    'CQ':   '#4cf',  // jasnoniebieski
    'DE':   '#8af',
    // Potwierdzenia
    'R':    '#aaf',
    'RR':   '#aaf',
    'RRR':  '#aaf',
    // Test
    'TEST': '#f4f',
    'K':    '#ff8',
    'KN':   '#ff8',
    'BK':   '#ff8',
  };

  // Podziel na tokeny zachowujac spacje i nowe linie
  let result = '';
  let i = 0;
  while (i < text.length) {
    // Nowa linia
    if (text[i] === '\n') {
      result += '<br>';
      i++;
      continue;
    }
    // Spacja
    if (text[i] === ' ') {
      result += ' ';
      i++;
      continue;
    }
    // Pobierz token (do spacji lub nowej linii)
    let j = i;
    while (j < text.length && text[j] !== ' ' && text[j] !== '\n') j++;
    const token = text.slice(i, j);
    const TU = token.toUpperCase();

    const color = COLORS[TU];
    if (color) {
      result += `<span style="color:${color};font-weight:bold;">${_esc(token)}</span>`;
    } else if (_workedCalls.has(_workedKeyCW(TU, window.UI?.getBandName?.(window.AppState?.freq)))) {
      // ZNAK JUZ PRZEPRACOWANY — szary, zeby nie kusil (dupe)
      result += `<span style="color:#888;font-weight:bold;" title="juz w logu">`
              + `${_esc(token)}</span>`;
    } else if (_knownCalls.has(TU)) {
      // ZNAK POTWIERDZONY baza (FT8/log/cluster) — pewne trafienie, mocny akcent
      result += `<span style="color:#0f8;font-weight:bold;text-shadow:0 0 4px rgba(0,255,136,.4);" `
              + `title="znak potwierdzony">${_esc(token)}</span>`;
    } else if (/^[A-R]{2}\d{2}([A-X]{2})?$/i.test(token)) {
      // Lokator QTH: JO82, KO02, IN77, JO82AA itp.
      result += `<span style="color:#f90;font-weight:bold;">${_esc(token)}</span>`;
    } else if (/^\d{3}$|^[5T][59NT][19NT]$|^T{1,2}[019NT]{1,3}$/.test(TU)) {
      // Raporty RST: 599, 5N9, 59N, 5NN, T001, T01, TT1 i podobne
      result += `<span style="color:#4f4;">${_esc(token)}</span>`;
    } else if (/^\d{1,4}$/.test(TU)) {
      // Numer kolejny w zawodach (po raporcie): 001, 14, 1234
      result += `<span style="color:#8f8;">${_esc(token)}</span>`;
    } else if (/^[A-Z0-9]{3,6}\/[A-Z0-9]/.test(TU) || /^[A-Z]{1,2}\d[A-Z]{1,4}$/.test(TU)) {
      // Znak wywoławczy (wzorzec, niepotwierdzony baza) — slabszy akcent
      result += `<span style="color:#4cf;font-weight:bold;">${_esc(token)}</span>`;
    } else {
      result += `<span style="color:var(--green);">${_esc(token)}</span>`;
    }
    i = j;
  }
  return result;
}

function _esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// Diagnostyka: nagraj audio tak, jak slyszy je model, i pobierz do odsluchu.
async function capture() {
  const token = localStorage.getItem('token') || '';
  const hdr = token ? {'Authorization': `Bearer ${token}`} : {};
  try {
    _setLog('🔴 Nagrywam 15 s audio (tak jak slyszy je model)...');
    const r = await fetch('/api/deepcw/capture', {
      method: 'POST',
      headers: {'Content-Type':'application/json', ...hdr},
      body: JSON.stringify({ seconds: 15 })
    });
    const d = await r.json();
    if (!d.ok) { _setLog('✗ ' + (d.error || 'Blad')); return; }
    // Poczekaj az nagranie sie skonczy, potem zaproponuj pobranie
    setTimeout(() => {
      _setLog('✓ Nagranie gotowe — pobieram...');
      window.open('/api/deepcw/capture_file' + (token ? `?token=${token}` : ''), '_blank');
    }, 16000);
  } catch(e) {
    _setLog('✗ ' + e.message);
  }
}

function clearOutput() {
  const el = document.getElementById('deepcw-output');
  if (el) el.innerHTML = '';
  _liveLine = null;
  const pv = document.getElementById('deepcw-preview');
  if (pv) pv.textContent = '';   // wyczysc takze linie "NA ZYWO"
}

// ── UI ────────────────────────────────────────────────────────────────────────
function _setLog(msg) {
  const el = document.getElementById('deepcw-log');
  if (el) el.textContent = msg;
}

function _updateBtn(running) {
  const btn = document.getElementById('deepcw-start-btn');
  if (!btn) return;
  btn.textContent      = running ? '⏹ STOP' : '▶ START';
  btn.style.color      = running ? 'var(--red)' : 'var(--green)';
  btn.style.borderColor = running ? 'var(--red)' : 'var(--green2)';
}

function _updateVU(data) {
  const canvas = document.getElementById('deepcw-vu');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  let rms = 0;
  for (const s of data) rms += s * s;
  rms = Math.sqrt(rms / data.length);
  const level = Math.min(1, rms * 10);
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = level > 0.7 ? '#f44' : level > 0.3 ? '#4f4' : '#4cf';
  ctx.fillRect(0, 0, W * level, H);
}

// Bargraf zasilany POZIOMEM Z SERWERA. Po przejsciu na audio z karty
// przegladarka nie ma juz wlasnego strumienia — poziom liczy serwer
// (z tego samego surowego audio, ktore trafia do modelu) i przysyla gotowy.
function handleVU(level) {
  const canvas = document.getElementById('deepcw-vu');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const lv = Math.max(0, Math.min(1, level || 0));
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = lv > 0.7 ? '#f44' : lv > 0.3 ? '#4f4' : '#4cf';
  ctx.fillRect(0, 0, W * lv, H);
}

// ── Przeciaganie okna ─────────────────────────────────────────────────────────
// Okno chwytamy za pasek tytulu. Pozycje pamietamy w pamieci sesji, zeby po
// zamknieciu i ponownym otwarciu zostalo tam, gdzie je operator postawil.
let _dragPos = null;   // {left, top} albo null = pozycja domyslna

function _initDrag() {
  const modal = document.getElementById('deepcw-modal');
  const bar   = document.getElementById('deepcw-drag-bar');
  if (!modal || !bar || bar._dragBound) return;
  bar._dragBound = true;
  bar.style.cursor = 'move';

  let sx = 0, sy = 0, sl = 0, st = 0, dragging = false;

  const onDown = (e) => {
    // Nie przechwytuj klikniec w przyciski na pasku (CLR / zamknij)
    if (e.target.closest('button')) return;
    dragging = true;
    const r = modal.getBoundingClientRect();
    // Przejdz z prawego/dolnego kotwiczenia na lewe/gorne, zeby dalo sie
    // swobodnie przesuwac w kazda strone.
    modal.style.left   = r.left + 'px';
    modal.style.top    = r.top  + 'px';
    modal.style.right  = 'auto';
    modal.style.bottom = 'auto';
    sx = e.clientX; sy = e.clientY; sl = r.left; st = r.top;
    e.preventDefault();
  };

  const onMove = (e) => {
    if (!dragging) return;
    // Trzymaj okno w obrebie ekranu (zostaw margines, zeby pasek byl chwytny)
    const w = modal.offsetWidth, h = modal.offsetHeight;
    let nl = Math.min(Math.max(0, sl + e.clientX - sx), window.innerWidth  - 60);
    let nt = Math.min(Math.max(0, st + e.clientY - sy), window.innerHeight - 30);
    modal.style.left = nl + 'px';
    modal.style.top  = nt + 'px';
    _dragPos = { left: nl, top: nt };
  };

  const onUp = () => { dragging = false; };

  bar.addEventListener('mousedown', onDown);
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup',   onUp);

  // Dotyk (tablet przy radiu)
  bar.addEventListener('touchstart', (e) => {
    if (e.target.closest('button')) return;
    const t = e.touches[0];
    onDown({ clientX: t.clientX, clientY: t.clientY,
             target: e.target, preventDefault: () => e.preventDefault() });
  }, { passive: false });
  document.addEventListener('touchmove', (e) => {
    if (!dragging) return;
    const t = e.touches[0];
    onMove({ clientX: t.clientX, clientY: t.clientY });
    e.preventDefault();
  }, { passive: false });
  document.addEventListener('touchend', onUp);
}

function openModal() {
  const m = document.getElementById('deepcw-modal');
  if (m) {
    m.style.display = 'block';
    // Przywroc pozycje z poprzedniego otwarcia
    if (_dragPos) {
      m.style.left = _dragPos.left + 'px';
      m.style.top  = _dragPos.top  + 'px';
      m.style.right = 'auto'; m.style.bottom = 'auto';
    }
  }
  _initDrag();
  refreshKnownCalls();   // swieza baza znakow do kolorowania
  fetch('/api/deepcw/engine_status').then(r => r.json()).then(d => {
    if (!d.hasModel)   _setLog('⚠ Model nie pobrany — USTAWIENIA → DeepCW → POBIERZ');
    else if (!d.ready) _setLog('⏳ Model ładuje się...');
    else               _setLog(`✓ Gotowy (${d.sizeMB} MB) — wybierz źródło i START`);
    // If the decoder is already running on the server (another operator has it
    // open, or we had it open before logging out), sync this window to that
    // state so it shows as active instead of stopped. We register as a viewer
    // so the decoder keeps running while our window is open, and stops only
    // when the last viewer leaves.
    if (d.running && !_running) {
      window.WS?.send({ type: 'cw_rx_enable', enabled: true });
      _running = true;
      _updateBtn(true);
      _setLog('🎧 Aktywny (dekoder już działał — dołączono)');
    }
  }).catch(() => {});
}

function closeModal() {
  const m = document.getElementById('deepcw-modal');
  if (m) m.style.display = 'none';
  // WAZNE: zamkniecie panelu ZATRZYMUJE dekoder. Inaczej przegladarka dalej
  // slalaby PCM, a serwer liczyl inferencje ONNX w tle — niepotrzebne
  // obciazenie procesora przy zamknietym oknie.
  if (_running) stopDecoding();
}

// ── SKALOWANIE OKNA DEKODERA CW ──────────────────────────────────────────────
// Okno CW mialo staly wymiar, czcionka bywala za mala. Tu: (1) uchwyt do
// rozciagania okna mysza (jak w prawdziwym oknie), (2) przyciski powiekszania
// /pomniejszania czcionki. Rozmiary zapamietane w configu przegladarki
// (localStorage NIE dziala w artifactach, ale to prawdziwa aplikacja — dziala).
let _cwFontPx = 14;

function _applyCwFont() {
  const el = document.getElementById('deepcw-output');
  if (el) el.style.fontSize = _cwFontPx + 'px';
  const pv = document.getElementById('deepcw-preview');
  if (pv) pv.style.fontSize = _cwFontPx + 'px';
  try { localStorage.setItem('deepcw_font_px', String(_cwFontPx)); } catch (e) {}
}

function cwFontBigger() { _cwFontPx = Math.min(40, _cwFontPx + 2); _applyCwFont(); }
function cwFontSmaller() { _cwFontPx = Math.max(8, _cwFontPx - 2); _applyCwFont(); }

function _initCwScaling() {
  const el = document.getElementById('deepcw-output');
  if (!el) return;

  // Przywroc zapamietany rozmiar czcionki i wysokosc okna.
  try {
    const f = parseInt(localStorage.getItem('deepcw_font_px'), 10);
    if (f) _cwFontPx = f;
    const h = localStorage.getItem('deepcw_output_h');
    if (h) el.style.height = h;
  } catch (e) {}
  _applyCwFont();

  // Uczyn okno rozciagalnym w PIONIE i POZIOMIE natywnie (CSS resize).
  el.style.resize = 'both';
  el.style.overflow = 'auto';
  el.style.minHeight = '80px';
  el.style.minWidth = '200px';

  // Zapamietaj rozmiar po zakonczeniu rozciagania (ResizeObserver).
  try {
    let _saveTimer = null;
    const ro = new ResizeObserver(() => {
      clearTimeout(_saveTimer);
      _saveTimer = setTimeout(() => {
        try { localStorage.setItem('deepcw_output_h', el.style.height || el.offsetHeight + 'px'); } catch (e) {}
      }, 400);
    });
    ro.observe(el);
  } catch (e) { /* ResizeObserver brak — trudno, resize dalej dziala */ }
}

// Inicjalizuj skalowanie gdy DOM gotowy (panel CW moze byc juz w HTML).
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _initCwScaling);
} else {
  _initCwScaling();
}

window.DeepCW = { startDecoding, stopDecoding, toggleStart,
                  clearOutput, handleText, handleVU, openModal, closeModal,
                  refreshKnownCalls, addClusterSpots, capture,
                  cwFontBigger, cwFontSmaller };
})();
