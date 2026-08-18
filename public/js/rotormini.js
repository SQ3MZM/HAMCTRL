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

// Local interpolation
let _azDisp  = 0;      // currently displayed angle
let _azPrev  = 0;      // previous position from the server
let _tPrev   = 0;      // time of the previous update from the server
let _tNext   = 0;      // time of the next expected update
let _speed   = 0;      // °/s speed computed from previous updates

// ── Compass ───────────────────────────────────────────────────────────────────
function drawCompass(az, taz, moving) {
  const cv = document.getElementById('rot-compass');
  if (!cv) return;
  const ctx = cv.getContext('2d');
  const W = cv.width, H = cv.height;
  const cx = W/2, cy = H/2, R = W/2 - 3;

  ctx.clearRect(0, 0, W, H);

  // Background
  const g = ctx.createRadialGradient(cx,cy,0,cx,cy,R);
  g.addColorStop(0, '#111811'); g.addColorStop(1, '#0a0c0a');
  ctx.beginPath(); ctx.arc(cx,cy,R,0,Math.PI*2);
  ctx.fillStyle = g; ctx.fill();
  ctx.strokeStyle = moving ? 'rgba(240,180,41,0.4)' : 'rgba(76,219,106,0.25)';
  ctx.lineWidth = moving ? 2 : 1; ctx.stroke();

  // Tick marks
  for (let d=0; d<360; d+=5) {
    const r  = (d-90)*Math.PI/180;
    const big = d%30===0, med = d%10===0;
    const r1 = R-(big?13:med?8:5), r2 = R-2;
    ctx.beginPath();
    ctx.moveTo(cx+Math.cos(r)*r1, cy+Math.sin(r)*r1);
    ctx.lineTo(cx+Math.cos(r)*r2, cy+Math.sin(r)*r2);
    ctx.strokeStyle = big?'rgba(76,219,106,0.6)':med?'rgba(76,219,106,0.3)':'rgba(76,219,106,0.1)';
    ctx.lineWidth = big?1.5:0.8; ctx.stroke();
  }

  // N E S W
  ctx.font='bold 9px monospace'; ctx.textAlign='center'; ctx.textBaseline='middle';
  for (const [l,d,red] of [['N',0,true],['E',90,false],['S',180,false],['W',270,false]]) {
    const r=(d-90)*Math.PI/180, rr=R-21;
    ctx.fillStyle = red?'rgba(224,82,82,0.9)':'rgba(76,219,106,0.55)';
    ctx.fillText(l, cx+Math.cos(r)*rr, cy+Math.sin(r)*rr);
  }

  // Target line (dashed)
  if (moving && taz!=null) {
    const tr=(taz-90)*Math.PI/180;
    ctx.save(); ctx.setLineDash([3,4]);
    ctx.beginPath(); ctx.moveTo(cx,cy);
    ctx.lineTo(cx+Math.cos(tr)*(R-16), cy+Math.sin(tr)*(R-16));
    ctx.strokeStyle='rgba(240,180,41,0.55)'; ctx.lineWidth=1.5; ctx.stroke();
    ctx.restore();
  }

  // Pointer
  const col = moving ? '#f0b429' : '#4cdb6a';
  const aLen = R-14;
  ctx.save();
  ctx.translate(cx,cy);
  ctx.rotate((az-90)*Math.PI/180);
  ctx.shadowBlur = moving?8:4; ctx.shadowColor = col;
  ctx.beginPath(); ctx.moveTo(0,0); ctx.lineTo(aLen-10,0);
  ctx.strokeStyle=col; ctx.lineWidth=2; ctx.stroke();
  ctx.beginPath(); ctx.moveTo(aLen,0); ctx.lineTo(aLen-10,-3.5); ctx.lineTo(aLen-10,3.5);
  ctx.closePath(); ctx.fillStyle=col; ctx.fill();
  ctx.restore();

  // Center
  ctx.beginPath(); ctx.arc(cx,cy,4,0,Math.PI*2);
  ctx.fillStyle=col; ctx.shadowBlur=6; ctx.shadowColor=col; ctx.fill();
  ctx.shadowBlur=0;
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
  try {
    const r=await fetch('/api/rotator');
    const rots=await r.json();
    if (Array.isArray(rots)&&rots.length>0) updateUI(rots[0]);
    else { const p=document.getElementById('rot-side-panel'); if(p) p.style.display='none'; }
  } catch(e) { const p=document.getElementById('rot-side-panel'); if(p) p.style.display='none'; }
}

window.RotW      = {init,handleWS,go,stop,onInput};
window.RotorMini = window.RotW;

window.addEventListener('app:ready', () => setTimeout(init,300));

})();
