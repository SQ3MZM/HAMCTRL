/**
 * wsjtx_scope.js — wodospad (waterfall) dla zakladki WSJT-X / wlasnego dekodera FT8.
 * Wersja WebGL: dane widmowe trzymane jako tekstura GPU (LUMINANCE, nBins x MAX_ROWS),
 * aktualizowana co nowa kolumne przez texSubImage2D (bez przesylania calej
 * tekstury na nowo). Paleta kolorow to osobna 1D lookup-texture, mapowanie
 * wartosc->kolor odbywa sie w fragment shaderze. Sprzetowa interpolacja
 * biliniowa (gl.LINEAR) daje plynne przejscia bez dodatkowego kosztu CPU —
 * w przeciwienstwie do poprzedniej wersji (canvas 2D + offscreen + drawImage),
 * cala praca skalowania/interpolacji dzieje sie na GPU.
 *
 * Odrebny od js/waterfall.js (ktory obsluguje szeroki scope CI-V calego radia).
 *
 * Funkcje (API identyczne jak wersja canvas 2D, zeby wsjtx.js/index.html
 * nie wymagaly zmian):
 *   init, onWaterfallData, onTxFreqUpdate, onSplitStatus, setRxFreq,
 *   toggleTxFreeze
 */
(function () {
'use strict';

let canvas = null, gl = null;
let fMin = 200, fMax = 3000, nBins = 200;
const MAX_ROWS = 300;

// Tekstura danych: LUMINANCE, szerokosc=nBins, wysokosc=MAX_ROWS. Kazdy nowy
// wiersz nadpisuje kolejna linie w buforze CPU-side, a writeRow wskazuje na
// ktora linie tekstury (cyklicznie) zapisac nastepna kolumne — dzieki temu
// NIE musimy przesuwac calej historii w pamieci ani re-uploadowac calej
// tekstury przy kazdej nowej kolumnie.
let dataTex = null;
let dataBuf = null;        // Uint8Array(nBins * MAX_ROWS), CPU-side mirror
let writeRow = 0;           // ktory wiersz (0..MAX_ROWS-1) zapisac nastepny
// Pozycja (writeRow) w ktorej zaczela sie OSTATNIA granica okresu dekodowania
// (xx:00/15/30/45 dla FT8, co 7.5s dla FT4) — uzywana do narysowania cienkiej
// linii separatora na wodospadzie, w tym samym miejscu co linia w Band
// Activity. null dopoki nie wykryto pierwszej granicy po starcie.
let periodBoundaryRow = null;
let _lastWindowSlot = null;   // do wykrywania PRZEJSCIA miedzy oknami (nie tylko stanu)
let _scopeDecodeMode = 'FT8'; // synchronizowane z WSJTX._decodeMode przez setDecodeMode poniżej
let rowsFilled = 0;         // ile wierszy ma juz prawdziwe dane (rosnie do MAX_ROWS)

let paletteTex = null;

let txFreqHz = 1000; // domyslna wartosc poczatkowa, nadpisana realnym stanem z backendu;
                      // NIE 1500Hz — patrz komentarz w webapp.py o notchu IC-7300 USB-D
let txFrozen = false;
let rxFreqHz = 1000; // znacznik RX, NIEZALEZNY od TX — startuje na tej samej
                      // wartosci, ale moze byc przesuwany osobno (przeciagniecie
                      // lub recznie wpisana wartosc)

// Stan przeciagania: ktory znacznik jest aktualnie chwycony ('tx'|'rx'|null).
// Wykrywane przy mousedown na podstawie odleglosci od kazdego znacznika —
// jesli klik jest blisko istniejacej linii, przeciagamy TYLKO ten znacznik;
// w przeciwnym razie traktujemy to jako nowe miejsce i przesuwamy OBA naraz.
let _dragging = null;
const DRAG_HIT_PX = 8; // promien (w px logicznych CSS, nie urzadzenia) wykrywania kliku na znaczniku

let _needsRender = false;
let _rafStarted = false;

// Palette Adjust (REF/ZERO/GAIN) — patrz komentarz przy uPaletteRef w
// fragment shaderze. Wartosci domyslne = no-op wzgledem bazowej palety.
let paletteRef  = 0.15;
let paletteZero = 0.0;
let paletteGain = 1.0;

function setPaletteReference(v) { paletteRef  = v; _requestRender(); }
function setPaletteZero(v)      { paletteZero = v; _requestRender(); }
function setPaletteGain(v)      { paletteGain = v; _requestRender(); }

// Paleta wodospadu: ciemny granat (cisza/szum) -> niebieski -> cyjan ->
// zielony -> zolty -> bialy (silne sygnaly). Wieksza czesc zakresu (szum
// tla) zostaje ciemna i stonowana, tylko silne sygnaly robia sie jasne.
// Typowe tlo/szum to NASYCONY niebieski [1,12,144], nie prawie-czarny jak
// w poprzedniej wersji. Krzywa: czern tylko dla absolutnej ciszy, szybko
// przechodzi w nasycony niebieski (typowy szum), potem cyjan/zielony/
// zolty/bialy dla coraz silniejszych sygnalow.
const PALETTE_STOPS = [
  [0,   [0,   0,   10]],
  [15,  [0,   5,   60]],
  [40,  [1,   12,  144]],  // "typowy szum"
  [90,  [0,   60,  190]],
  [140, [0,   150, 210]],
  [180, [90,  215, 150]],
  [210, [235, 225, 65]],
  [235, [255, 165, 40]],
  [255, [255, 255, 255]],
];

function _buildPaletteRGBA() {
  const out = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    let lo = PALETTE_STOPS[0], hi = PALETTE_STOPS[PALETTE_STOPS.length - 1];
    for (let s = 0; s < PALETTE_STOPS.length - 1; s++) {
      if (i >= PALETTE_STOPS[s][0] && i <= PALETTE_STOPS[s + 1][0]) { lo = PALETTE_STOPS[s]; hi = PALETTE_STOPS[s + 1]; break; }
    }
    const span = hi[0] - lo[0];
    const t = span > 0 ? (i - lo[0]) / span : 0;
    out[i * 4 + 0] = Math.round(lo[1][0] + t * (hi[1][0] - lo[1][0]));
    out[i * 4 + 1] = Math.round(lo[1][1] + t * (hi[1][1] - lo[1][1]));
    out[i * 4 + 2] = Math.round(lo[1][2] + t * (hi[1][2] - lo[1][2]));
    out[i * 4 + 3] = 255;
  }
  return out;
}

// ── Shadery ──────────────────────────────────────────────────────────────────
const VERT_SRC = `
attribute vec2 aPos;
varying vec2 vUv;
void main() {
  vUv = aPos * 0.5 + 0.5;
  gl_Position = vec4(aPos, 0.0, 1.0);
}`;

const FRAG_SRC = `
precision mediump float;
varying vec2 vUv;
uniform sampler2D uData;
uniform sampler2D uPalette;
uniform float uWriteRow;
uniform float uRowsFilled;
uniform float uMaxRows;
uniform float uTxX;
uniform float uRxX;
uniform float uHasRx;
uniform float uTxFrozen;
uniform float uAspectPx;
uniform float uAspectPxY;
uniform float uPeriodBoundaryRow;
uniform float uHasPeriodBoundary;
uniform float uPaletteRef;
uniform float uPaletteZero;
uniform float uPaletteGain;

void main() {
  float rowsFilled = min(uRowsFilled, uMaxRows);
  if (rowsFilled < 1.0) { gl_FragColor = vec4(0.0,0.0,0.0,1.0); return; }

  // KRYTYCZNE: skala odwzorowania piksel-ekranu -> wiersz-danych musi byc
  // STALA (uMaxRows), NIE zmieniac sie wraz z rowsFilled. Wczesniejsza wersja
  // uzywala rowsVisible=min(rowsFilled,uMaxRows) jako mnoznika — to oznaczalo
  // ze dopoki bufor sie nie zapelnil (pierwsze ~90s pracy), KAZDY piksel
  // ekranu byl przemapowywany na inny wiersz danych przy kazdej nowej
  // kolumnie (bo mnoznik rosl z kazda klatka), co dawalo widoczne "skakanie"/
  // "duchy" — juz narysowane sygnaly przesuwaly sie wizualnie mimo ze same
  // dane sie nie zmienialy. Teraz: stala skala (uMaxRows wierszy zawsze
  // odpowiada calej wysokosci ekranu), a niewypelnione jeszcze wiersze po
  // prostu pozostaja czarne, dopoki realne dane tam nie dotra.
  float rowFromTopF = (1.0 - vUv.y) * uMaxRows;
  rowFromTopF = min(rowFromTopF, uMaxRows - 1.0);
  if (rowFromTopF >= rowsFilled) { gl_FragColor = vec4(0.0,0.0,0.0,1.0); return; }

  // Probkowanie RECZNE w przestrzeni LOGICZNEJ (nie bezposrednio w przestrzeni
  // tekstury), zeby uniknac automatycznej interpolacji GPU na granicy
  // zawijania bufora cyklicznego (gdzie texRow=writeRow-1 (najnowsze) i
  // texRow=writeRow (najstarsze) sa fizycznie sasiednie w teksturze, mimo
  // ze logicznie to przeciwne konce historii — interpolacja miedzy nimi
  // dawala widoczny "szew"/artefakt raz na pelny cykl bufora).
  float rowFromTop0 = floor(rowFromTopF);
  float rowFromTop1 = min(rowFromTop0 + 1.0, rowsFilled - 1.0);
  float frac = rowFromTopF - rowFromTop0;

  float texRow0 = mod(uWriteRow - 1.0 - rowFromTop0 + uMaxRows * 4.0, uMaxRows);
  float texRow1 = mod(uWriteRow - 1.0 - rowFromTop1 + uMaxRows * 4.0, uMaxRows);

  float v0 = texture2D(uData, vec2(vUv.x, (texRow0 + 0.5) / uMaxRows)).r;
  float v1 = texture2D(uData, vec2(vUv.x, (texRow1 + 0.5) / uMaxRows)).r;
  float v = mix(v0, v1, frac);

  // Palette Adjust (REF/ZERO/GAIN). Przy domyslnych
  // wartosciach (ref=0.15, zero=0, gain=1.0) to no-op — v przechodzi bez
  // zmian, wiec baseline wyglada dokladnie tak jak przed dodaniem tych
  // suwakow. REF: przesuniecie jasnosci wzgledem wbudowanego punktu
  // odniesienia palety (0.15, tam gdzie PALETTE_STOPS ma "typowy szum").
  // ZERO: odcina dol (przycina slabe sygnaly do czerni). GAIN: kontrast
  // wzgledem srodka zakresu.
  float vAdj = v + (uPaletteRef - 0.15);
  vAdj = max(0.0, vAdj - uPaletteZero);
  vAdj = (vAdj - 0.5) * uPaletteGain + 0.5;
  vAdj = clamp(vAdj, 0.0, 1.0);

  vec3 color = texture2D(uPalette, vec2(vAdj, 0.5)).rgb;

  // Cienka linia pozioma na granicy okresu dekodowania (xx:00/15/30/45 dla
  // FT8, co 7.5s dla FT4) — informuje ze ponizej tej linii zaczyna sie NOWE
  // okno. Synchronizowana z analogiczna linia w Band Activity (ta sama
  // logika wykrywania granic, patrz _windowSlot w wsjtx.js i kod w
  // onWaterfallData powyzej). Pozycja przeliczana TA SAMA transformacja
  // (mod z zawijaniem bufora) co odczyt danych tekstury, zeby linia
  // przewijala sie idealnie razem z danymi, nie "plywala" wzgledem nich.
  if (uHasPeriodBoundary > 0.5) {
    float boundaryRowFromTop = mod(uWriteRow - 1.0 - uPeriodBoundaryRow + uMaxRows * 4.0, uMaxRows);
    if (boundaryRowFromTop < rowsFilled) {
      float rowPx = boundaryRowFromTop / uMaxRows;
      float curPx = 1.0 - vUv.y;
      float lineHalfHeight = 0.75 / uAspectPxY;
      if (abs(curPx - rowPx) < lineHalfHeight) {
        color = mix(color, vec3(1.0, 1.0, 1.0), 0.55);
      }
    }
  }

  float lineHalfWidth = 1.0 / uAspectPx;
  // Linia TX: czerwona gdy wolna, pomaranczowa gdy zamrozona w miejscu
  if (abs(vUv.x - uTxX) < lineHalfWidth * 1.5) {
    color = uTxFrozen > 0.5 ? vec3(1.0, 0.55, 0.0) : vec3(1.0, 0.27, 0.27);
  }
  // Linia RX: zielona (tylko gdy rozna od TX)
  if (uHasRx > 0.5 && abs(vUv.x - uRxX) < lineHalfWidth * 1.5) {
    color = vec3(0.27, 0.85, 0.33);
  }

  gl_FragColor = vec4(color, 1.0);
}`;

function _compileShader(type, src) {
  const sh = gl.createShader(type);
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    console.error('[wsjtx_scope] shader compile error:', gl.getShaderInfoLog(sh));
    gl.deleteShader(sh);
    return null;
  }
  return sh;
}

let prog = null, uLoc = {};

function _initGL() {
  gl = canvas.getContext('webgl', { antialias: true, preserveDrawingBuffer: false })
    || canvas.getContext('experimental-webgl');
  if (!gl) {
    console.warn('[wsjtx_scope] WebGL niedostepny, scope nie bedzie dzialac');
    return false;
  }

  const vs = _compileShader(gl.VERTEX_SHADER, VERT_SRC);
  const fs = _compileShader(gl.FRAGMENT_SHADER, FRAG_SRC);
  if (!vs || !fs) return false;

  prog = gl.createProgram();
  gl.attachShader(prog, vs);
  gl.attachShader(prog, fs);
  gl.linkProgram(prog);
  if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
    console.error('[wsjtx_scope] program link error:', gl.getProgramInfoLog(prog));
    return false;
  }
  gl.useProgram(prog);

  const quad = new Float32Array([-1,-1, 1,-1, -1,1, -1,1, 1,-1, 1,1]);
  const quadBuf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, quadBuf);
  gl.bufferData(gl.ARRAY_BUFFER, quad, gl.STATIC_DRAW);
  const aPos = gl.getAttribLocation(prog, 'aPos');
  gl.enableVertexAttribArray(aPos);
  gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

  uLoc = {
    uData: gl.getUniformLocation(prog, 'uData'),
    uPalette: gl.getUniformLocation(prog, 'uPalette'),
    uWriteRow: gl.getUniformLocation(prog, 'uWriteRow'),
    uRowsFilled: gl.getUniformLocation(prog, 'uRowsFilled'),
    uMaxRows: gl.getUniformLocation(prog, 'uMaxRows'),
    uTxX: gl.getUniformLocation(prog, 'uTxX'),
    uRxX: gl.getUniformLocation(prog, 'uRxX'),
    uHasRx: gl.getUniformLocation(prog, 'uHasRx'),
    uSplitX: gl.getUniformLocation(prog, 'uSplitX'),
    uSplitOn: gl.getUniformLocation(prog, 'uSplitOn'),
    uTxFrozen: gl.getUniformLocation(prog, 'uTxFrozen'),
    uAspectPx: gl.getUniformLocation(prog, 'uAspectPx'),
    uAspectPxY: gl.getUniformLocation(prog, 'uAspectPxY'),
    uPeriodBoundaryRow: gl.getUniformLocation(prog, 'uPeriodBoundaryRow'),
    uHasPeriodBoundary: gl.getUniformLocation(prog, 'uHasPeriodBoundary'),
    uPaletteRef: gl.getUniformLocation(prog, 'uPaletteRef'),
    uPaletteZero: gl.getUniformLocation(prog, 'uPaletteZero'),
    uPaletteGain: gl.getUniformLocation(prog, 'uPaletteGain'),
  };

  dataBuf = new Uint8Array(nBins * MAX_ROWS);
  dataTex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, dataTex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, nBins, MAX_ROWS, 0, gl.LUMINANCE, gl.UNSIGNED_BYTE, dataBuf);

  paletteTex = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, paletteTex);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, _buildPaletteRGBA());

  return true;
}

function _reallocDataTextureIfNeeded() {
  if (dataBuf && dataBuf.length === nBins * MAX_ROWS) return;
  dataBuf = new Uint8Array(nBins * MAX_ROWS);
  writeRow = 0;
  rowsFilled = 0;
  periodBoundaryRow = null;
  _lastWindowSlot = null;
  if (gl && dataTex) {
    gl.bindTexture(gl.TEXTURE_2D, dataTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, nBins, MAX_ROWS, 0, gl.LUMINANCE, gl.UNSIGNED_BYTE, dataBuf);
  }
}

function init() {
  canvas = document.getElementById('wj-scope-canvas');
  if (!canvas) { console.warn('[scope] brak canvas'); return; }

  // Jesli GL juz zainicjowany — tylko resize i render
  if (gl) { _resizeCanvas(); _renderAxis(); _requestRender(); return; }

  canvas.addEventListener('mousedown', _onMouseDown);
  window.addEventListener('mousemove', _onMouseMove);
  window.addEventListener('mouseup', _onMouseUp);

  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => { _resizeCanvas(); _renderAxis(); _requestRender(); });
    ro.observe(canvas);
  } else {
    window.addEventListener('resize', () => { _resizeCanvas(); _renderAxis(); _requestRender(); });
  }

  // Poczekaj az canvas bedzie mial wymiary przed inicjalizacja WebGL
  function _waitAndInit(tries) {
    const rect = canvas.getBoundingClientRect();
    console.log('[scope] init attempt', tries, 'rect:', rect.width, 'x', rect.height);
    if (rect.width < 1 || rect.height < 1) {
      if (tries > 0) setTimeout(() => _waitAndInit(tries - 1), 100);
      else console.warn('[scope] canvas ma zerowe wymiary po 30 probach');
      return;
    }
    _resizeCanvas();
    if (!_initGL()) { console.warn('[scope] _initGL zwrocil false'); return; }
    _renderAxis();
    _requestRender();
    _updateLabels();
    _startRenderLoop();
    console.log('[scope] zainicjalizowany, canvas:', canvas.width, 'x', canvas.height);
  }
  _waitAndInit(30);
}

// Podzialka czestotliwosci nad wodospadem (np. "500", "1000", "1500"...),
// w stylu referencyjnego JTDX. Renderowana jako lekkie elementy HTML
// pozycjonowane procentowo wzgledem szerokosci kontenera — prostsze i
// ostrzejsze (natywny font przegladarki) niz rysowanie tekstu w WebGL/Canvas.
function _renderAxis() {
  const axisEl = document.getElementById('wj-scope-axis');
  if (!axisEl || !canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 1) return;

  // Krok podzialki dobrany tak, zeby etykiety nie zachodzily na siebie:
  // ~500Hz przy typowej szerokosci panelu, gesciej gdy panel jest szerszy.
  const range = fMax - fMin;
  let step = 500;
  if (rect.width > 1400) step = 250;
  else if (rect.width < 700) step = 1000;

  const firstTick = Math.ceil(fMin / step) * step;
  let html = '';
  for (let f = firstTick; f < fMax; f += step) {
    const pct = ((f - fMin) / range) * 100;
    html += `<span class="wj-axis-tick" style="left:${pct.toFixed(2)}%">${f}</span>`;
  }
  axisEl.innerHTML = html;
}

function _startRenderLoop() {
  if (_rafStarted) return;
  _rafStarted = true;
  function tick() {
    if (_needsRender) {
      _needsRender = false;
      _render();
    }
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

function _requestRender() { _needsRender = true; }

function _resizeCanvas() {
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  if (rect.width < 1 || rect.height < 1) {
    return;
  }
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  if (gl) gl.viewport(0, 0, canvas.width, canvas.height);
}

function onWaterfallData(msg) {
  const prevFMin = fMin, prevFMax = fMax;
  if (msg.fMin !== undefined) fMin = msg.fMin;
  if (msg.fMax !== undefined) fMax = msg.fMax;
  if (fMin !== prevFMin || fMax !== prevFMax) _renderAxis();
  if (msg.nBins !== undefined && msg.nBins !== nBins) {
    nBins = msg.nBins;
    _reallocDataTextureIfNeeded();
  }
  if (!Array.isArray(msg.data) || !gl) return;

  // Wykryj granice okresu dekodowania (UTC, 15s dla FT8 / 7.5s dla FT4) —
  // ta sama logika "numeru slotu" co _windowSlot() w wsjtx.js, zeby obie
  // linie (Band Activity i wodospad) byly spojne. Sprawdzane PRZED zapisem
  // nowego wiersza, bo periodBoundaryRow ma wskazywac wiersz w ktorym
  // zaczela sie nowa transmisja.
  const nowUtc = new Date();
  const totalSec = nowUtc.getUTCHours()*3600 + nowUtc.getUTCMinutes()*60 + nowUtc.getUTCSeconds()
                    + nowUtc.getUTCMilliseconds()/1000;
  const windowS = _scopeDecodeMode === 'FT4' ? 7.5 : 15.0;
  const slot = Math.floor(totalSec / windowS);
  if (_lastWindowSlot !== null && slot !== _lastWindowSlot) {
    periodBoundaryRow = writeRow;
  }
  _lastWindowSlot = slot;

  const row = new Uint8Array(msg.data);
  dataBuf.set(row.subarray(0, nBins), writeRow * nBins);
  gl.bindTexture(gl.TEXTURE_2D, dataTex);
  gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, writeRow, nBins, 1, gl.LUMINANCE, gl.UNSIGNED_BYTE, row);

  writeRow = (writeRow + 1) % MAX_ROWS;
  rowsFilled = Math.min(rowsFilled + 1, MAX_ROWS);
  _requestRender();
}

// Wywolywane z wsjtx.js::setDecodeMode, zeby wodospad uzywal tej samej
// dlugosci okna (15s/7.5s) co reszta UI przy wykrywaniu granic periodow.
function setScopeDecodeMode(mode) {
  _scopeDecodeMode = mode;
}

function onTxFreqUpdate(msg) {
  if (msg.freqHz !== undefined) txFreqHz = msg.freqHz;
  if (msg.frozen !== undefined) txFrozen = msg.frozen;
  _updateLabels();
  _requestRender();
}


function setRxFreq(freqHz) {
  rxFreqHz = freqHz;
  _updateLabels();
  _requestRender();
}

function _pxToFreq(clientX) {
  const rect = canvas.getBoundingClientRect();
  const xFrac = (clientX - rect.left) / rect.width;
  return fMin + xFrac * (fMax - fMin);
}

function _freqToPx(freqHz) {
  const rect = canvas.getBoundingClientRect();
  return rect.left + _freqToFrac(freqHz) * rect.width;
}

function _onMouseDown(ev) {
  if (!canvas) return;
  const distTx = Math.abs(ev.clientX - _freqToPx(txFreqHz));
  const distRx = Math.abs(ev.clientX - _freqToPx(rxFreqHz));

  // Jesli klik jest blisko ISTNIEJACEGO znacznika, chwytamy TYLKO ten jeden
  // (przeciaganie niezalezne). W przeciwnym razie to "nowe miejsce" — od razu
  // przesuwamy OBA znaczniki tam (ft8_set_both_freq), a uzytkownik moze
  // kontynuowac przeciaganie jako nowy punkt startowy dla obu.
  if (distTx <= DRAG_HIT_PX && distTx <= distRx) {
    if (txFrozen) {
      window.UI?.showToast('🧊 TX zamrozone — odmrozy zeby recznie zmienic czestotliwosc');
      return;
    }
    _dragging = 'tx';
  } else if (distRx <= DRAG_HIT_PX) {
    _dragging = 'rx';
  } else {
    // Klik na pustym miejscu wodospadu: przesun OBA znaczniki tutaj.
    const freq = _pxToFreq(ev.clientX);
    window.WS?.send({ type: 'ft8_set_both_freq', freqHz: Math.round(freq) });
    _dragging = 'both'; // pozwala kontynuowac jako przeciaganie obu, jesli user nie puscil przycisku
  }
  ev.preventDefault();
}

function _onMouseMove(ev) {
  if (!_dragging || !canvas) return;
  const freq = Math.round(_pxToFreq(ev.clientX));
  if (_dragging === 'tx') {
    if (txFrozen) return;
    window.WS?.send({ type: 'ft8_set_tx_freq', freqHz: freq });
  } else if (_dragging === 'rx') {
    window.WS?.send({ type: 'ft8_set_rx_freq', freqHz: freq });
  } else if (_dragging === 'both') {
    window.WS?.send({ type: 'ft8_set_both_freq', freqHz: freq });
  }
}

function _onMouseUp() {
  _dragging = null;
}

function toggleTxFreeze() {
  window.WS?.send({ type: 'ft8_toggle_tx_freeze', frozen: !txFrozen });
}


function setTxFreqManual(val) {
  const freq = parseFloat(val);
  if (Number.isNaN(freq)) return;
  if (txFrozen) {
    window.UI?.showToast('🧊 TX zamrozone — odmrozy zeby recznie zmienic czestotliwosc');
    return;
  }
  window.WS?.send({ type: 'ft8_set_tx_freq', freqHz: Math.round(freq) });
}

function setRxFreqManual(val) {
  const freq = parseFloat(val);
  if (Number.isNaN(freq)) return;
  window.WS?.send({ type: 'ft8_set_rx_freq', freqHz: Math.round(freq) });
}

function rxEqTx() {
  window.WS?.send({ type: 'ft8_rx_eq_tx' });
}

function txEqRx() {
  window.WS?.send({ type: 'ft8_tx_eq_rx' });
}

function getRxFreq() {
  return rxFreqHz;
}

// Czy TX zamrozony (Hold Tx Freq)? Uzywane przez _selectRow w wsjtx.js
// zeby zdecydowac czy TX ma podazac za korespondentem przy wolaniu.
function isTxFrozen() { return txFrozen; }

function getTxFreq() {
  return txFreqHz;
}

function onRxFreqUpdate(msg) {
  if (msg.freqHz !== undefined) rxFreqHz = msg.freqHz;
  _updateLabels();
  _requestRender();
}

function _updateLabels() {
  const txInput = document.getElementById('wj-tx-freq-input');
  if (txInput && document.activeElement !== txInput) {
    txInput.value = Math.round(txFreqHz);
  }
  const rxInput = document.getElementById('wj-rx-freq-input');
  if (rxInput && document.activeElement !== rxInput) {
    rxInput.value = Math.round(rxFreqHz);
  }
  const freezeBtn = document.getElementById('wj-tx-freeze-btn');
  if (freezeBtn) {
    freezeBtn.textContent = txFrozen ? '📌 TX ZABLOKOWANY' : '🔓 TX WOLNY';
    freezeBtn.classList.toggle('active', txFrozen);
  }
}

function _freqToFrac(freqHz) {
  return (freqHz - fMin) / (fMax - fMin);
}

function _render() {
  if (!gl || !canvas) return;
  gl.useProgram(prog);

  gl.activeTexture(gl.TEXTURE0);
  gl.bindTexture(gl.TEXTURE_2D, dataTex);
  gl.uniform1i(uLoc.uData, 0);

  gl.activeTexture(gl.TEXTURE1);
  gl.bindTexture(gl.TEXTURE_2D, paletteTex);
  gl.uniform1i(uLoc.uPalette, 1);

  gl.uniform1f(uLoc.uWriteRow, writeRow);
  gl.uniform1f(uLoc.uRowsFilled, rowsFilled);
  gl.uniform1f(uLoc.uMaxRows, MAX_ROWS);
  gl.uniform1f(uLoc.uTxX, _freqToFrac(txFreqHz));
  gl.uniform1f(uLoc.uHasRx, 1.0);
  gl.uniform1f(uLoc.uRxX, _freqToFrac(rxFreqHz));
  gl.uniform1f(uLoc.uTxFrozen, txFrozen ? 1.0 : 0.0);
  gl.uniform1f(uLoc.uAspectPx, canvas.width);
  gl.uniform1f(uLoc.uAspectPxY, canvas.height);
  gl.uniform1f(uLoc.uHasPeriodBoundary, periodBoundaryRow !== null ? 1.0 : 0.0);
  gl.uniform1f(uLoc.uPeriodBoundaryRow, periodBoundaryRow !== null ? periodBoundaryRow : 0.0);
  gl.uniform1f(uLoc.uPaletteRef, paletteRef);
  gl.uniform1f(uLoc.uPaletteZero, paletteZero);
  gl.uniform1f(uLoc.uPaletteGain, paletteGain);

  gl.drawArrays(gl.TRIANGLES, 0, 6);
}

function onSplitStatus(msg) { /* split status - obsluzone przez wsjtx.js */ }

window.WSJTXScope = { init, onWaterfallData, onTxFreqUpdate, onRxFreqUpdate, onSplitStatus,
                        setRxFreq, setTxFreqManual, setRxFreqManual, rxEqTx, txEqRx, getRxFreq, getTxFreq, isTxFrozen,
                        toggleTxFreeze, setScopeDecodeMode,
                        setPaletteReference, setPaletteZero, setPaletteGain };
})();
