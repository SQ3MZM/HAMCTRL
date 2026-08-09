/**
 * vfo.js — zaawansowany panel VFO
 *
 * Funkcje:
 *   1. Cyfry częstotliwości — każda osobno klikalana scroll/klawiatura
 *   2. Wirtualne pokrętło SVG — drag/scroll → zmiana częstotliwości
 *   3. Scroll kółkiem myszy na całym VFO
 *   4. Klawisze strzałek
 */
(function () {
'use strict';

const S = window.AppState;

// ── Sprawdź czy user może sterować radiem ────────────────────────────────────
function _canControl() {
  const lock  = window.AppState?.radio_lock;
  const myUid = String(window.AppState?.my_uid || window.CurrentUser?.id || '');
  const role  = window.CurrentUser?.role;
  if (role === 'admin') return true;
  // User musi miec przejete radio — niezaleznie czy jest wolne czy zajete
  if (!lock?.locked) return false;
  return String(lock.user_id) === myUid;
}
function _blockToast() {
  const holder = window.AppState?.radio_lock?.callsign ||
                 window.AppState?.radio_lock?.username || '?';
  window.UI?.showToast(`⛔ Radio zajęte przez ${holder} — przejmij TRX`, 'error');
}

// ── Format częstotliwości → tablica 9 cyfr [MHz.kHz.Hz] ──────────────────────
//   np. 14205000 → ["1","4",".","2","0","5",".","0","0","0"]
function freqToDigits(hz) {
  // Format: XXX.XXX.XXX Hz (9 cyfr + 2 kropki)
  const s = String(hz).padStart(9, '0');
  return [
    s[0], s[1], s[2], // miliony (MHz)
    '.',
    s[3], s[4], s[5], // tysiące (kHz)
    '.',
    s[6], s[7], s[8], // jednostki (Hz)
  ];
}

// ── Wartość cyfry na pozycji i (0-8, bez kropek) ─────────────────────────────
// pozycja 0=10MHz, 1=1MHz, 2=100kHz, 3=10kHz, 4=1kHz, 5=100Hz, 6=10Hz, 7=1Hz
const DIGIT_VALUES = [10000000, 1000000, 100000, 10000, 1000, 100, 10, 1, 1]; // ostatni 1 = Hz (idx 8)
// mapowanie indeksu w tablicy digits (z kropkami) → pozycja w DIGIT_VALUES
const DIGIT_MAP = { 0:0, 1:1, 2:2,  4:3, 5:4, 6:5,  8:6, 9:7, 10:8 };

function getDigitStep(displayIdx) {
  // displayIdx: index w ciągu znaków (0-10, z kropkami na pozycjach 3,7)
  const pos = DIGIT_MAP[displayIdx];
  if (pos === undefined) return 0;
  return DIGIT_VALUES[pos] || 0;
}

// ── Renderuj panel VFO cyfrowy ────────────────────────────────────────────────
function renderVFO() {
  const el = document.getElementById('vfo-digits');
  if (!el) return;

  const digits = freqToDigits(S.freq);
  el.innerHTML = digits.map((ch, i) => {
    if (ch === '.') {
      // Separator MHz jest grubszy (po pozycji 2)
      const cls = i === 3 ? 'vfo-sep sep-mhz' : 'vfo-sep';
      return `<span class="${cls}">.</span>`;
    }
    const step = getDigitStep(i);
    if (!step) return `<span class="vfo-digit" data-idx="${i}" data-step="0">${ch}</span>`;
    return `<span class="vfo-digit active" data-idx="${i}" data-step="${step}"
      tabindex="0" title="+/- ${fmtStep(step)}"
      onkeydown="VFO.keyDigit(event,${step})"
      onclick="VFO.selectDigit(${step})"
      >${ch}</span>`;
  }).join('');

  // Podswietl aktualnie wybrany krok
  if (window._vfoStep) {
    el.querySelectorAll('.vfo-digit.active').forEach(d => {
      const s2 = parseInt(d.dataset.step);
      d.classList.toggle('selected', s2 === window._vfoStep);
    });
  }

  // Podepnij listener na kontenerze (jesli jeszcze nie podpiety)
  _attachDigitListeners();
}

function fmtStep(step) {
  if (step >= 1000000) return (step/1000000) + ' MHz';
  if (step >= 1000)    return (step/1000) + ' kHz';
  return step + ' Hz';
}

function updateVFODisplay() {
  const el = document.getElementById('vfo-digits');
  if (!el) { renderVFO(); return; }
  const digits = freqToDigits(S.freq);
  el.querySelectorAll('.vfo-digit').forEach((span, _) => {
    const idx = parseInt(span.dataset.idx);
    if (span.classList.contains('active')) {
      span.textContent = digits[idx];
    }
  });
}

// ── Wheel na cyfrze ───────────────────────────────────────────────────────────
// UWAGA: inline onwheel="VFO.wheelDigit(event,step)" na elementach .vfo-digit
// NIE DZIALA gdy caly #app-scale ma transform:scale() — zdarzenia wheel sa
// dispatchowane w fizycznych pikselach ekranu, ale elementy DOM sa w logicznych
// pikselach przed transformacja. Przy scale!=1 (np. 1920px ekran -> scale=1.33)
// zdarzenie wheel "chybia" elementy mimo ze kursor fizycznie nad nimi jest.
// Potwierdzono: window.addEventListener('wheel',...,true) (faza capture) dziala
// zawsze — zdarzenie dociera do document/window zanim zostanie przypisane do
// konkretnego elementu DOM. Rozwiazanie: JEDEN globalny listener w fazie
// capture na document, ktory uzywa elementFromPoint() (poprawnie przelicza
// przez transform) do identyfikacji cyfry pod kursorem.
function wheelDigit(e, step) {
  e.preventDefault();
  e.stopPropagation();
  if (!_canControl()) { _blockToast(); return; }
  const dir = e.deltaY < 0 ? 1 : -1;
  const nf = Math.max(100000, S.freq + dir * step);
  S.freq = nf;
  S._localFreqSetAt = Date.now();
  updateVFODisplay();
  window.UI?.updateBandButtons();
  window.UI?.updateVFOBadges?.();  // natychmiast badge pasma/mode
  if (typeof window.WS?.sendFreqFast === 'function') {
    window.WS.sendFreqFast(nf);
  } else if (typeof window.WS?.send === 'function') {
    window.WS.send({ type: 'freq', freq: nf });
  } else {
    window.UI?.sendFreq(nf);
  }
}

// ── Jeden listener na kontenerze #vfo-digits ────────────────────────────────
// Zamiast podpinac listenery do kazdej cyfry osobno (co powodowalo
// off-by-one przez kolejnosc DOM/forEach), uzywamy JEDNEGO listenera
// na kontenerze. e.target zawsze wskazuje na element pod kursorem —
// bezbladnie identyfikujemy cyfre i odczytujemy jej data-step.
let _digitListenerAttached = false;
function _attachDigitListeners() {
  if (_digitListenerAttached) return;
  const container = document.getElementById('vfo-digits');
  if (!container) return;
  container.addEventListener('wheel', function(e) {
    // Nie uzywamy e.target — jest bledny przez transform:scale (przesuwa o 1 cyfre).
    // getBoundingClientRect() zawsze zwraca poprawne wspolrzedne ekranowe
    // uwzgledniajace transform, wiec iterujemy przez cyfry i sprawdzamy
    // ktora faktycznie zawiera punkt kursora.
    let digit = null;
    const x = e.clientX, y = e.clientY;
    // Iterujemy OD PRAWEJ do lewej — letter-spacing ujemny moze powodowac
    // ze cyfry sie nakladaja i forEach (lewo->prawo) trafia w zla cyfre.
    // Odwrocona kolejnosc: gdy x pasuje do kilku cyfr, bierzemy ta najblizej
    // kursora (sprawdzamy odleglosc od centrum cyfry).
    const digits2 = Array.from(container.querySelectorAll('.vfo-digit.active'));
    let bestIdx = -1;
    let bestDist = Infinity;
    digits2.forEach(function(el, i) {
      const r = el.getBoundingClientRect();
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
        const cx = (r.left + r.right) / 2;
        const dist = Math.abs(x - cx);
        if (dist < bestDist) { bestDist = dist; bestIdx = i; }
      }
    });
    // Offset -1: cyfra zawsze przesuwa sie o 1 w prawo przez transform:scale
    // wiec bierzemy cyfre o 1 wczesniej w DOM
    if (bestIdx > 0) digit = digits2[bestIdx - 1];
    else if (bestIdx === 0) digit = digits2[0];
    if (!digit) return;
    const s = parseInt(digit.dataset.step);
    if (!s) return;
    e.preventDefault();
    e.stopPropagation();
    e.stopImmediatePropagation();
    wheelDigit(e, s);
  }, { capture: true, passive: false });
  _digitListenerAttached = true;
}

// ── Klawiatura na cyfrze ──────────────────────────────────────────────────────
function keyDigit(e, step) {
  if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
    e.preventDefault();
    if (!_canControl()) { _blockToast(); return; }
    if (e.key === 'ArrowUp')   UI.sendFreq(Math.max(100000, S.freq + step));
    if (e.key === 'ArrowDown') UI.sendFreq(Math.max(100000, S.freq - step));
  }
}

// ── Kliknięcie cyfry → zaznacz (podświetl i reaguj na scroll) ────────────────
let selectedStep = 1000;
function selectDigit(step) {
  if (!_canControl()) { _blockToast(); return; }
  selectedStep = step;
  document.querySelectorAll('.vfo-digit.active').forEach(el => el.classList.remove('selected'));
  document.querySelectorAll(`.vfo-digit[data-step="${step}"]`).forEach(el => el.classList.add('selected'));
}

// ── Pokrętło SVG ──────────────────────────────────────────────────────────────
let knobAngle    = 0;
let knobDragging = false;
let knobStartY   = 0;
let knobStartAngle = 0;
const KNOB_SENSITIVITY = 1.2; // stopnie/pixel

function initKnob() {
  const knob = document.getElementById('vfo-knob');
  if (!knob) return;

  knob.addEventListener('mousedown', e => {
    if (!_canControl()) { _blockToast(); return; }
    knobDragging  = true;
    knobStartY    = e.clientY;
    knobStartAngle = knobAngle;
    e.preventDefault();
    document.body.style.userSelect = 'none';
  });

  document.addEventListener('mousemove', e => {
    if (!knobDragging) return;
    const delta = (knobStartY - e.clientY) * KNOB_SENSITIVITY;
    knobAngle   = knobStartAngle + delta;
    rotateKnob(knobAngle);

    const stepDelta = Math.round((e.clientY - knobStartY) * -1);
    if (Math.abs(stepDelta) >= 2) {
      knobStartY = e.clientY;
      const newFreq = Math.max(100000, S.freq + Math.sign(stepDelta) * selectedStep);
      UI.sendFreq(newFreq);
    }
  });

  document.addEventListener('mouseup', () => {
    knobDragging = false;
    document.body.style.userSelect = '';
  });

  // Touch (mobile)
  knob.addEventListener('touchstart', e => {
    if (!_canControl()) { _blockToast(); return; }
    knobDragging = true;
    knobStartY   = e.touches[0].clientY;
    knobStartAngle = knobAngle;
  }, { passive: true });

  document.addEventListener('touchmove', e => {
    if (!knobDragging) return;
    const dy = knobStartY - e.touches[0].clientY;
    knobAngle = knobStartAngle + dy * KNOB_SENSITIVITY;
    rotateKnob(knobAngle);
    const stepDelta = Math.round(dy);
    if (Math.abs(stepDelta) >= 2) {
      knobStartY = e.touches[0].clientY;
      UI.sendFreq(Math.max(100000, S.freq + Math.sign(stepDelta) * selectedStep));
    }
  }, { passive: true });

  document.addEventListener('touchend', () => { knobDragging = false; });

  // Scroll
  knob.addEventListener('wheel', e => {
    e.preventDefault();
    if (!_canControl()) { _blockToast(); return; }
    const dir = e.deltaY < 0 ? 1 : -1;
    knobAngle += dir * 15;
    rotateKnob(knobAngle);
    UI.sendFreq(Math.max(100000, S.freq + dir * selectedStep));
  }, { passive: false });
}

function rotateKnob(angle) {
  const needle = document.getElementById('knob-needle');
  if (needle) needle.setAttribute('transform', `rotate(${angle % 360}, 50, 50)`);
}

// ── Render pokrętła SVG ───────────────────────────────────────────────────────
function renderKnobSVG() {
  const el = document.getElementById('vfo-knob-wrap');
  if (!el) return;
  el.innerHTML = `
  <svg id="vfo-knob" viewBox="0 0 100 100" width="90" height="90"
    style="cursor:ns-resize;display:block;margin:0 auto;"
    title="Przeciągnij lub kręć kółkiem myszy">
    <!-- Zewnętrzny ring -->
    <circle cx="50" cy="50" r="47" fill="#090c09" stroke="rgba(76,219,106,0.2)" stroke-width="1"/>
    <!-- Znaczniki co 30° -->
    ${Array.from({length:12},(_,i)=>{
      const a=(i*30-90)*Math.PI/180;
      const r1=40,r2=i%3===0?44:42;
      return `<line x1="${50+r1*Math.cos(a)}" y1="${50+r1*Math.sin(a)}" x2="${50+r2*Math.cos(a)}" y2="${50+r2*Math.sin(a)}" stroke="rgba(76,219,106,${i%3===0?'0.5':'0.2'})" stroke-width="${i%3===0?1.5:0.7}"/>`;
    }).join('')}
    <!-- Korpus pokrętła -->
    <circle cx="50" cy="50" r="38" fill="#141714" stroke="rgba(76,219,106,0.15)" stroke-width="1"/>
    <!-- Gradient 3D -->
    <radialGradient id="kg" cx="38%" cy="35%">
      <stop offset="0%" stop-color="rgba(76,219,106,0.08)"/>
      <stop offset="100%" stop-color="rgba(0,0,0,0)"/>
    </radialGradient>
    <circle cx="50" cy="50" r="38" fill="url(#kg)"/>
    <!-- Wskaźnik -->
    <g id="knob-needle">
      <line x1="50" y1="50" x2="50" y2="16" stroke="#4cdb6a" stroke-width="2.5" stroke-linecap="round"
        style="filter:drop-shadow(0 0 3px rgba(76,219,106,0.6))"/>
      <circle cx="50" cy="16" r="3" fill="#4cdb6a"/>
    </g>
    <!-- Centrum -->
    <circle cx="50" cy="50" r="5" fill="#141714" stroke="rgba(76,219,106,0.3)" stroke-width="1"/>
  </svg>
  <div style="text-align:center;font-family:var(--mono);font-size:9px;color:var(--dim);margin-top:4px;letter-spacing:1px;">
    TUNING · ${fmtStep(selectedStep)}
  </div>`;
  initKnob();
}

// ── Eksport ───────────────────────────────────────────────────────────────────
window.VFO = {
  renderVFO,
  updateVFODisplay,
  renderKnobSVG,
  wheelDigit,
  keyDigit,
  selectDigit,
  init() {
    renderVFO();
    // _attachDigitListeners jest wolane przez renderVFO() z setTimeout(0)
    renderKnobSVG();

    // Scroll na calym VFO boxie (poza cyframi) — globalny krok pokretla
    document.getElementById('vfo-box')?.addEventListener('wheel', e => {
      const x = e.clientX, y = e.clientY;
      let overDigit = false;
      document.querySelectorAll('.vfo-digit').forEach(function(el) {
        const r = el.getBoundingClientRect();
        if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) overDigit = true;
      });
      if (overDigit) return;
      if (!_canControl()) { e.preventDefault(); _blockToast(); return; }
      e.preventDefault();
      const dir = e.deltaY < 0 ? 1 : -1;
      window.UI?.sendFreq(Math.max(100000, S.freq + dir * selectedStep));
    }, { passive: false });

    selectDigit(1000);
  },
  updateStep(step) {
    selectedStep = step;
    // Odśwież label pod pokrętłem
    const lbl = document.querySelector('#vfo-knob-wrap div');
    if (lbl) lbl.textContent = `TUNING · ${fmtStep(step)}`;
    document.querySelectorAll('.vfo-digit.selected').forEach(el => el.classList.remove('selected'));
    document.querySelectorAll(`.vfo-digit[data-step="${step}"]`).forEach(el => el.classList.add('selected'));
  },
};

document.addEventListener('DOMContentLoaded', () => {
  // Init po załadowaniu state
  setTimeout(() => VFO.init(), 200);
});

})();
