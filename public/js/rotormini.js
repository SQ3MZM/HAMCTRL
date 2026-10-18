/**
 * rotormini.js — Rotator widget next to the VFO
 * Smooth animation: local interpolation between updates from the server
 */
'use strict';
(function () {

// ── State ─────────────────────────────────────────────────────────────────────
let _rotId   = null;
let _az      = 0;      // last position from the server
let _taz     = 0;      // target
let _moving  = false;
let _myLoc   = 'KO02';

// Direction HINT - a suggestion only, typed into #rot-target-inp (this
// widget's own input, right next to the VFO in the RADIO tab) or into the
// RADIO tab's quick-log bar (#qlog-call/#qlog-grid, via qsolog.js's
// _updateQuickLogHint -> RotorMini.setHint). Separate from _taz (the real
// commanded target) - drawn as its own dotted marker in drawCompass, and
// cleared the moment the rotor actually starts moving (see updateUI) so it
// can never linger looking like a still-pending suggestion.
let _hintAz  = null;

function setHint(az) {
  _hintAz = (az == null || isNaN(az)) ? null : az;
}
function clearHint() { _hintAz = null; }

// Local interpolation
let _azDisp  = 0;      // currently displayed angle
let _azPrev  = 0;      // previous position from the server
let _tPrev   = 0;      // time of the previous update from the server
let _tNext   = 0;      // time of the next expected update
let _speed   = 0;      // °/s speed computed from previous updates

// ── Compass ───────────────────────────────────────────────────────────────────
// Theme-aware (2026-10-16, live report "rotor... sa na ciemno a powinny byc
// na jasno... tylko waterfall mial zostac na ciemno"): unlike the waterfall/
// scope (an RF data plot, kept dark in every SDR app regardless of theme),
// the user explicitly wants this compass to follow the day/night toggle.
// Reads data-mode directly (not a CSS var — this is a <canvas>, drawn in
// JS, not styled by CSS) once per frame; cheap enough at this size/rate.
function _compassPalette() {
  const light = document.documentElement.getAttribute('data-mode') === 'light';
  return light ? {
    bezel: '#94a3b8',
    bgInner: '#f8fafc', bgOuter: '#e2e8f0',
    hubBg: 'rgba(255, 255, 255, 0.95)',
    hubBorder: 'rgba(148, 163, 184, 0.6)',
    ringMoving: 'rgba(217, 119, 6, 0.65)', ringStill: 'rgba(2, 132, 199, 0.35)',
    radarRing: 'rgba(148, 163, 184, 0.25)',
    tickBig: 'rgba(15, 23, 42, 0.85)', tickMed: 'rgba(71, 85, 105, 0.6)', tickSmall: 'rgba(148, 163, 184, 0.35)',
    degText: 'rgba(71, 85, 105, 0.8)',
    labelN: '#dc2626', labelOther: '#0284c7',
    targetLine: '#d97706', targetBug: '#d97706',
    pointerMoving: '#d97706', pointerStill: '#0284c7',
  } : {
    bezel: '#1e293b',
    bgInner: '#0b1329', bgOuter: '#060a14',
    hubBg: 'rgba(7, 12, 22, 0.92)',
    hubBorder: 'rgba(56, 189, 248, 0.35)',
    ringMoving: 'rgba(251, 191, 36, 0.7)', ringStill: 'rgba(56, 189, 248, 0.35)',
    radarRing: 'rgba(56, 189, 248, 0.12)',
    tickBig: 'rgba(56, 189, 248, 0.95)', tickMed: 'rgba(56, 189, 248, 0.55)', tickSmall: 'rgba(56, 189, 248, 0.22)',
    degText: 'rgba(148, 163, 184, 0.65)',
    labelN: '#f43f5e', labelOther: '#38bdf8',
    targetLine: 'rgba(251, 191, 36, 0.75)', targetBug: '#fbbf24',
    pointerMoving: '#fbbf24', pointerStill: '#38bdf8',
  };
}

function drawCompass(az, taz, moving) {
  const cv = document.getElementById('rot-compass');
  if (!cv) return;
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  const cx = W/2, cy = H/2, R = W/2 - 2;
  const P = _compassPalette();
  const hubR = 38; // Radius of central digital readout hub

  ctx.clearRect(0, 0, W, H);

  // 1. Dial background plate
  const g = ctx.createRadialGradient(cx, cy, hubR, cx, cy, R);
  g.addColorStop(0, P.bgInner);
  g.addColorStop(1, P.bgOuter);
  ctx.beginPath();
  ctx.arc(cx, cy, R, 0, Math.PI * 2);
  ctx.fillStyle = g;
  ctx.fill();

  // Outer bezel & moving glow
  ctx.strokeStyle = moving ? P.ringMoving : P.bezel;
  ctx.lineWidth = moving ? 2.5 : 1.5;
  if (moving) {
    ctx.shadowBlur = 8;
    ctx.shadowColor = P.pointerMoving;
  }
  ctx.stroke();
  ctx.shadowBlur = 0;

  // Thin outer track ring
  ctx.beginPath();
  ctx.arc(cx, cy, R - 3, 0, Math.PI * 2);
  ctx.strokeStyle = moving ? P.ringMoving : P.ringStill;
  ctx.lineWidth = 1;
  ctx.stroke();

  // Subtle radar range ring
  ctx.beginPath();
  ctx.arc(cx, cy, R * 0.72, 0, Math.PI * 2);
  ctx.strokeStyle = P.radarRing;
  ctx.lineWidth = 0.8;
  ctx.stroke();

  // 2. Tick marks (avionics graduations)
  for (let d = 0; d < 360; d += 5) {
    const rad = (d - 90) * Math.PI / 180;
    const isMajor = d % 30 === 0;
    const isMed = d % 10 === 0;
    const r1 = R - (isMajor ? 12 : isMed ? 8 : 5);
    const r2 = R - 3;
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(rad) * r1, cy + Math.sin(rad) * r1);
    ctx.lineTo(cx + Math.cos(rad) * r2, cy + Math.sin(rad) * r2);
    ctx.strokeStyle = isMajor ? P.tickBig : (isMed ? P.tickMed : P.tickSmall);
    ctx.lineWidth = isMajor ? 1.6 : 0.8;
    ctx.stroke();
  }

  // 3. Labels (N, E, S, W and degree markers every 30°)
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  const headings = [
    { d: 0,   text: 'N',   cardinal: true, north: true },
    { d: 30,  text: '30',  cardinal: false },
    { d: 60,  text: '60',  cardinal: false },
    { d: 90,  text: 'E',   cardinal: true },
    { d: 120, text: '120', cardinal: false },
    { d: 150, text: '150', cardinal: false },
    { d: 180, text: 'S',   cardinal: true },
    { d: 210, text: '210', cardinal: false },
    { d: 240, text: '240', cardinal: false },
    { d: 270, text: 'W',   cardinal: true },
    { d: 300, text: '300', cardinal: false },
    { d: 330, text: '330', cardinal: false },
  ];

  for (const h of headings) {
    const rad = (h.d - 90) * Math.PI / 180;
    const textR = R - 20;
    const tx = cx + Math.cos(rad) * textR;
    const ty = cy + Math.sin(rad) * textR;

    if (h.north) {
      // Crimson North Triangle Marker at the outer track
      const nx = cx + Math.cos(rad) * (R - 3);
      const ny = cy + Math.sin(rad) * (R - 3);
      ctx.beginPath();
      ctx.moveTo(nx, ny);
      ctx.lineTo(nx - 4, ny + 7);
      ctx.lineTo(nx + 4, ny + 7);
      ctx.closePath();
      ctx.fillStyle = P.labelN;
      ctx.fill();

      ctx.font = 'bold 11px "JetBrains Mono", monospace';
      ctx.fillStyle = P.labelN;
      ctx.fillText(h.text, tx, ty);
    } else if (h.cardinal) {
      ctx.font = 'bold 10px "JetBrains Mono", monospace';
      ctx.fillStyle = P.labelOther;
      ctx.fillText(h.text, tx, ty);
    } else {
      ctx.font = '8px "JetBrains Mono", monospace';
      ctx.fillStyle = P.degText;
      ctx.fillText(h.text, tx, ty);
    }
  }

  // 4. Direction HINT (dotted cyan line & beacon tag) - a typed-in
  // suggestion, not a real command (see _hintAz above). Drawn before the
  // real target/pointer so it never visually competes with them.
  if (_hintAz != null) {
    const hr = (_hintAz - 90) * Math.PI / 180;
    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(hr) * (hubR + 2), cy + Math.sin(hr) * (hubR + 2));
    ctx.lineTo(cx + Math.cos(hr) * (R - 15), cy + Math.sin(hr) * (R - 15));
    ctx.strokeStyle = 'rgba(56, 189, 248, 0.9)';
    ctx.lineWidth = 2;
    ctx.stroke();
    ctx.restore();

    const hx = cx + Math.cos(hr) * (R - 14);
    const hy = cy + Math.sin(hr) * (R - 14);
    ctx.beginPath();
    ctx.arc(hx, hy, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = '#38bdf8';
    ctx.shadowBlur = 8;
    ctx.shadowColor = '#38bdf8';
    ctx.fill();
    ctx.shadowBlur = 0;

    const lx = cx + Math.cos(hr) * (R - 28);
    const ly = cy + Math.sin(hr) * (R - 28);
    ctx.font = 'bold 10px "JetBrains Mono", monospace';
    ctx.fillStyle = '#38bdf8';
    ctx.fillText(Math.round(_hintAz) + '°', lx, ly);
  }

  // 5. Target Line & Target Bug (when moving or taz specified)
  if (moving && taz != null) {
    const tr = (taz - 90) * Math.PI / 180;
    ctx.save();
    ctx.setLineDash([3, 4]);
    ctx.beginPath();
    ctx.moveTo(cx + Math.cos(tr) * (hubR + 2), cy + Math.sin(tr) * (hubR + 2));
    ctx.lineTo(cx + Math.cos(tr) * (R - 14), cy + Math.sin(tr) * (R - 14));
    ctx.strokeStyle = P.targetLine;
    ctx.lineWidth = 1.5;
    ctx.stroke();
    ctx.restore();

    // Target Bug chevron / marker at rim
    ctx.save();
    ctx.translate(cx, cy);
    ctx.rotate(tr);
    ctx.beginPath();
    ctx.moveTo(R - 3, 0);
    ctx.lineTo(R - 11, -4.5);
    ctx.lineTo(R - 9, 0);
    ctx.lineTo(R - 11, 4.5);
    ctx.closePath();
    ctx.fillStyle = P.targetBug;
    ctx.shadowBlur = 6;
    ctx.shadowColor = P.targetBug;
    ctx.fill();
    ctx.restore();
  }

  // 6. Azimuth Pointer (Precision avionics needle)
  const col = moving ? P.pointerMoving : P.pointerStill;
  const pRad = (az - 90) * Math.PI / 180;

  ctx.save();
  ctx.translate(cx, cy);
  ctx.rotate(pRad);

  // Sleek tapered needle from hub to rim
  ctx.beginPath();
  ctx.moveTo(hubR + 1, -1.8);
  ctx.lineTo(R - 15, -1.2);
  ctx.lineTo(R - 15, 1.2);
  ctx.lineTo(hubR + 1, 1.8);
  ctx.closePath();
  ctx.fillStyle = col;
  ctx.shadowBlur = moving ? 10 : 5;
  ctx.shadowColor = col;
  ctx.fill();

  // Aerodynamic arrow head
  ctx.beginPath();
  ctx.moveTo(R - 5, 0);
  ctx.lineTo(R - 16, -4.5);
  ctx.lineTo(R - 13, 0);
  ctx.lineTo(R - 16, 4.5);
  ctx.closePath();
  ctx.fillStyle = col;
  ctx.fill();

  // Counter pip on opposite side of hub
  ctx.beginPath();
  ctx.arc(-hubR - 2, 0, 2, 0, Math.PI * 2);
  ctx.fillStyle = col;
  ctx.fill();

  ctx.restore();

  // 7. Center Hub (Instrument Gauge Pod)
  // Keeps center clear for digital readout: <span id="rot-az-display">
  ctx.beginPath();
  ctx.arc(cx, cy, hubR, 0, Math.PI * 2);
  ctx.fillStyle = P.hubBg;
  ctx.fill();

  ctx.strokeStyle = moving ? P.ringMoving : P.hubBorder;
  ctx.lineWidth = 1.4;
  ctx.stroke();

  // Fine inner accent ring
  ctx.beginPath();
  ctx.arc(cx, cy, hubR - 2.5, 0, Math.PI * 2);
  ctx.strokeStyle = moving ? 'rgba(251, 191, 36, 0.25)' : 'rgba(56, 189, 248, 0.15)';
  ctx.lineWidth = 0.8;
  ctx.stroke();
}

// ── Animation loop (60fps) ─────────────────────────────────────────────────────
(function loop(now) {
  if (_moving && _speed > 0.05) {
    // Compute how much time has passed since the last server update
    const dt = Math.min((now - _tPrev) / 1000, 2.0);  // max 2s of extrapolation
    // Extrapolate the position linearly based on speed
    const extrapolated = _azPrev + _speed * dt;
    // But don't overshoot the target
    const toTarget = _taz - _azPrev;
    if (Math.abs(toTarget) > 1) {
      const limited = _azPrev + Math.sign(toTarget) * Math.min(Math.abs(extrapolated - _azPrev), Math.abs(toTarget));
      _azDisp = limited;
    } else {
      _azDisp = _az;
    }
  } else {
    // Stopped — smooth interpolation to the actual position
    const diff = _az - _azDisp;
    _azDisp += diff * 0.15;
    if (Math.abs(diff) < 0.05) _azDisp = _az;
  }

  drawCompass(_azDisp, _taz, _moving);

  // Digits
  const azEl = document.getElementById('rot-az-display');
  if (azEl) azEl.textContent = Math.round(_azDisp).toString().padStart(3,'0');

  requestAnimationFrame(loop);
})(0);

// ── Maidenhead ────────────────────────────────────────────────────────────────
function maidenToLL(loc) {
  loc=(loc||''). toUpperCase().trim();
  if (!/^[A-R]{2}\d{2}([A-X]{2})?$/.test(loc)) return null;
  const lon=(loc.charCodeAt(0)-65)*20+parseInt(loc[2])*2+(loc.length>=6?(loc.charCodeAt(4)-65)/12+1/24:1)-180;
  const lat=(loc.charCodeAt(1)-65)*10+parseInt(loc[3])+(loc.length>=6?(loc.charCodeAt(5)-65)/24+1/48:0.5)-90;
  return {lat,lon};
}
function locToAz(tgt, my) {
  const m=maidenToLL(my), t=maidenToLL(tgt);
  if (!m||!t) return null;
  const dL=(t.lon-m.lon)*Math.PI/180;
  const y=Math.sin(dL)*Math.cos(t.lat*Math.PI/180);
  const x=Math.cos(m.lat*Math.PI/180)*Math.sin(t.lat*Math.PI/180)-Math.sin(m.lat*Math.PI/180)*Math.cos(t.lat*Math.PI/180)*Math.cos(dL);
  return Math.round(((Math.atan2(y,x)*180/Math.PI)+360)%360);
}
function parseTarget(input) {
  input=(input||''). trim().toUpperCase();
  if (!input) return null;
  const n=parseFloat(input.replace(',','.'));
  if (!isNaN(n)&&input.match(/^\d+([.,]\d+)?°?$/)) return {az:Math.round(((n%360)+360)%360),label:Math.round(((n%360)+360)%360)+'°'};
  if (/^[A-R]{2}\d{2}([A-X]{2})?$/.test(input)) {
    const az=locToAz(input,_myLoc);
    if (az!=null) return {az,label:input+' → '+az+'°'};
  }
  // Otherwise treat it as a callsign - same BeamHeading lookup rotator.js's
  // own target field and the FT8 quick-log use.
  const h = window.BeamHeading?.headingFor(input, '');
  if (h) return {az:h.azimuth,label:input+' → '+h.azimuth+'°'};
  return null;
}

// ── updateUI ──────────────────────────────────────────────────────────────────
function updateUI(rot) {
  const panel = document.getElementById('rot-side-panel');
  if (!panel) return;
  if (!rot) { panel.style.display='none'; return; }
  panel.style.display='flex';

  _rotId = rot.id;

  // Compute speed from the difference between updates
  const now   = performance.now();
  const newAz = parseFloat(rot.azimuth ?? rot.az ?? 0);
  if (_moving && now > _tPrev) {
    const dt = (now - _tPrev) / 1000;
    if (dt > 0.05 && dt < 3.0) {
      const rawSpeed = Math.abs(newAz - _az) / dt;
      // Smooth the speed (don't jump on read glitches)
      if (rawSpeed < 20) _speed = _speed * 0.6 + rawSpeed * 0.4;
    }
  }

  _azPrev  = _az;
  _az      = newAz;
  _tPrev   = now;
  _taz     = parseFloat(rot.target_az ?? _taz ?? 0);
  const wasMoving = _moving;
  _moving  = !!rot.moving;

  // The rotor is actually moving now (from ANY command) - a lingering
  // hint would read as "go here next" when it's really just stale input.
  if (_moving) _hintAz = null;

  // When the rotator stops — reset the speed and jump to the exact position
  if (wasMoving && !_moving) {
    _speed  = 0;
    _azDisp = _az;
  }

  // Digit color
  const azEl = document.getElementById('rot-az-display');
  if (azEl) azEl.style.color = _moving ? 'var(--amber,#f0b429)' : (rot.connected ? 'var(--green)' : 'var(--dim)');

  // Badge
  const badge = document.getElementById('rot-status-badge');
  if (badge) {
    // Remove the static HTML's data-i18n - otherwise the next
    // I18n.setLang() would overwrite this badge back to "none" regardless
    // of the actual state (SIM/OK/moving), since apply() runs over the
    // whole document by attribute.
    badge.removeAttribute('data-i18n');
    if (!rot.connected&&!rot.sim) { badge.textContent=I18n.t('rotator_none'); badge.style.color='var(--red)'; }
    else if (rot.sim)             { badge.textContent='● SIM';  badge.style.color='var(--amber,#f0b429)'; }
    else if (_moving)             { badge.textContent=I18n.t('rotator_moving'); badge.style.color='var(--amber,#f0b429)'; }
    else                          { badge.textContent='● OK';   badge.style.color='var(--green)'; }
  }

  const nm=document.getElementById('rot-name-label');
  if (nm) nm.textContent=rot.name||'';
}

// ── Actions ───────────────────────────────────────────────────────────────────
function onInput() {
  const inp=document.getElementById('rot-target-inp');
  const prv=document.getElementById('rot-target-preview');
  if (!inp||!prv) return;
  const t=parseTarget(inp.value);
  if (t) { prv.textContent='→ '+t.label; prv.style.color='var(--green)'; }
  else   { prv.textContent=inp.value?'⚠ nieprawidłowy format':''; prv.style.color='var(--dim)'; }
  setHint(t ? t.az : null);
}

async function go() {
  const inp=document.getElementById('rot-target-inp');
  const t=parseTarget(inp?inp.value:'');
  if (!t) { window.UI?.showToast?.('⚠ Podaj stopnie (270) lub lokator (KO02)','error'); return; }
  if (!_rotId) { window.UI?.showToast?.('⚠ Brak rotora','error'); return; }
  try {
    const r=await fetch('/api/rotator/'+_rotId+'/position',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({az:t.az,el:0})
    });
    const res=await r.json();
    if (res.ok) {
      _taz=t.az; window.UI?.showToast?.('↻ Cel: '+t.label);
      if (inp) inp.value='';
      const prv=document.getElementById('rot-target-preview');
      if (prv) prv.textContent='';
    } else window.UI?.showToast?.('✗ '+(res.error||'Błąd'),'error');
  } catch(e) { window.UI?.showToast?.('✗ '+e.message,'error'); }
}

async function stop() {
  if (!_rotId) return;
  try {
    const r=await fetch('/api/rotator/'+_rotId+'/stop',{method:'POST'});
    const res=await r.json();
    if (res.ok) window.UI?.showToast?.('■ Zatrzymano');
    else window.UI?.showToast?.('✗ '+(res.error||'Błąd'),'error');
  } catch(e) { window.UI?.showToast?.('✗ '+e.message,'error'); }
}

// ── WS ────────────────────────────────────────────────────────────────────────
function handleWS(msg) {
  if (msg.type==='rotator_update'&&msg.rotator) {
    if (!_rotId||msg.rotator.id===_rotId) updateUI(msg.rotator);
  }
  if (msg.type==='init'&&msg.rotators?.length>0) {
    if (window.AppState?.stationLocator) _myLoc=window.AppState.stationLocator;
    updateUI(msg.rotators[0]);
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
async function init() {
  if (window.AppState?.stationLocator) _myLoc=window.AppState.stationLocator;

  // Click directly on the dial to set the target azimuth input (does NOT
  // move the rotor by itself - onInput() only sets the preview/hint, same
  // as typing a value; a separate GO/Enter still confirms the move).
  const cv = document.getElementById('rot-compass');
  if (cv && !cv._hasClickListener) {
    cv._hasClickListener = true;
    cv.style.cursor = 'crosshair';
    cv.title = 'Kliknij na tarczę, aby wybrać kąt azymutu';
    cv.addEventListener('click', (e) => {
      // FIX (found reviewing an independent fork, 2026-10-16): the fork's
      // version of this handler computed x/y directly from
      // getBoundingClientRect() (real, on-screen/CSS pixels) but then
      // subtracted cv.width/2 (the canvas's internal pixel count,
      // width="205") - correct only when the page happens to render at
      // exactly 1:1 scale. Everywhere else (any #app-scale zoom other
      // than 100%, which is almost always, see index.html's updateScale)
      // the two coordinate spaces don't match, so the computed angle was
      // simply wrong - click "due north" on a scaled-down window, get a
      // noticeably different degree value. Same fix tuneToClick() in
      // waterfall.js already applies: scale the click position into
      // canvas-internal pixels first via rect.width/rect.height, matching
      // what drawCompass() actually used to lay out the dial.
      const rect = cv.getBoundingClientRect();
      const scaleX = cv.width / rect.width;
      const scaleY = cv.height / rect.height;
      const x = (e.clientX - rect.left) * scaleX - cv.width / 2;
      const y = (e.clientY - rect.top) * scaleY - cv.height / 2;
      const dist = Math.sqrt(x*x + y*y);
      if (dist < 38) return; // click on the inner hub - ignored
      const deg = Math.round((Math.atan2(y, x) * 180 / Math.PI + 90 + 360) % 360);
      const inp = document.getElementById('rot-target-inp');
      if (inp) {
        inp.value = deg + '°';
        onInput();
      }
    });
  }

  try {
    const r=await fetch('/api/rotator');
    const rots=await r.json();
    if (Array.isArray(rots)&&rots.length>0) updateUI(rots[0]);
    else { const p=document.getElementById('rot-side-panel'); if(p) p.style.display='none'; }
  } catch(e) { const p=document.getElementById('rot-side-panel'); if(p) p.style.display='none'; }
}

window.RotW      = {init,handleWS,go,stop,onInput,setHint,clearHint};
window.RotorMini = window.RotW;

window.addEventListener('app:ready', () => setTimeout(init,300));

})();
