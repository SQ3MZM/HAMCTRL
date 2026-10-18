/**
 * rotator.js (frontend) — rotator control panel for the big ROTOR tab.
 * - Clickable SVG compass
 * - Type a 4/6-char locator, degrees, or a callsign → compute azimuth → go
 * - Preset directions N/NE/E...
 * - Direction HINT: typing into this tab's own target field (without
 *   hitting go) draws a dotted suggestion line on THIS dial, separate from
 *   the real target/needle, cleared once the rotor actually starts moving
 *   (any command) - see updateCard. The RADIO tab's quick-log bar and the
 *   FT8 quick-log show the SAME kind of hint, but on the separate mini
 *   compass next to the VFO (rotormini.js's own setHint/clearHint, not
 *   this file's) - that's the dial actually visible alongside them.
 */
(function() {
'use strict';

let rotators = [];

// rot.name is admin-typed free text (KONFIGURACJA, already escaped on the
// edit side in admin.js) but was shown here, to every operator, unescaped -
// same injection class as ui.js's rig-name fix.
function _esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// ── Maidenhead locator → coordinates ─────────────────────────────────────────
function locatorToLatLon(loc) {
  loc = loc.toUpperCase().trim();
  if (loc.length < 4) return null;
  const A = loc.charCodeAt(0) - 65; // A=0
  const B = loc.charCodeAt(1) - 65;
  const C = parseInt(loc[2]);
  const D = parseInt(loc[3]);
  if (A<0||A>17||B<0||B>17||isNaN(C)||isNaN(D)) return null;
  let lon = A * 20 - 180 + C * 2 + 1;
  let lat = B * 10 - 90  + D * 1 + 0.5;
  if (loc.length >= 6) {
    const E = loc.charCodeAt(4) - 65;
    const F = loc.charCodeAt(5) - 65;
    if (!isNaN(E) && !isNaN(F)) {
      lon += E * (2/24) + (2/24)/2;
      lat += F * (1/24) + (1/24)/2;
    }
  }
  return { lat, lon };
}

// ── Compute the bearing between two points ────────────────────────────────────
function bearingTo(fromLat, fromLon, toLat, toLon) {
  const φ1 = fromLat * Math.PI / 180;
  const φ2 = toLat   * Math.PI / 180;
  const Δλ = (toLon - fromLon) * Math.PI / 180;
  const y   = Math.sin(Δλ) * Math.cos(φ2);
  const x   = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ);
  const θ   = Math.atan2(y, x);
  return ((θ * 180 / Math.PI) + 360) % 360;
}

// ── Get the station's location from the config ────────────────────────────────
function getMyLocator() {
  const cfgLoc = window.AppState?.stationLocator || '';
  return cfgLoc || 'KO02'; // fallback: central Poland
}

// ── Compute azimuth from a locator or degrees ─────────────────────────────────
function resolveTarget(input) {
  input = (input || '').trim();
  if (!input) return null;

  // Try as a number (degrees)
  const deg = parseFloat(input);
  if (!isNaN(deg) && /^-?\d/.test(input)) {
    return { az: ((deg % 360) + 360) % 360, label: `${deg}°` };
  }

  // Try as a Maidenhead locator
  if (/^[A-Ra-r]{2}\d{2}/.test(input)) {
    const myLoc = getMyLocator();
    const myPos = locatorToLatLon(myLoc);
    const dxPos = locatorToLatLon(input);
    if (!myPos || !dxPos) return null;
    const az = bearingTo(myPos.lat, myPos.lon, dxPos.lat, dxPos.lon);
    const dist = calcDist(myPos.lat, myPos.lon, dxPos.lat, dxPos.lon);
    return { az: Math.round(az * 10) / 10, label: `${input} → ${Math.round(az)}° (${Math.round(dist)} km)` };
  }

  // Otherwise treat it as a callsign - reuse beamheading.js's own
  // locator-or-prefix resolution (same logic the FT8 quick-log's SP/LP
  // already relies on) instead of duplicating a prefix table here.
  const h = window.BeamHeading?.headingFor(input, '');
  if (h) {
    return { az: h.azimuth, label: `${input.toUpperCase()} → ${h.azimuth}° (${h.distance}km, ${h.source === 'grid' ? 'grid' : 'prefix'})` };
  }
  return null;
}

// Distance in km (haversine)
function calcDist(lat1, lon1, lat2, lon2) {
  const R = 6371;
  const φ1 = lat1 * Math.PI/180, φ2 = lat2 * Math.PI/180;
  const Δφ = (lat2-lat1) * Math.PI/180, Δλ = (lon2-lon1) * Math.PI/180;
  const a = Math.sin(Δφ/2)**2 + Math.cos(φ1)*Math.cos(φ2)*Math.sin(Δλ/2)**2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
}

// ── Load state ───────────────────────────────────────────────────────────────
async function load() {
  try {
    const r = await fetch('/api/rotator');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    rotators = await r.json();
    render();
  } catch(e) {
    const el = document.getElementById('rotators-container');
    if (el) el.innerHTML = `<div style="text-align:center;padding:30px;font-family:var(--mono);font-size:12px;color:var(--dim)">Brak rotatorów lub błąd połączenia.<br>Skonfiguruj w Ustawienia → Admin.</div>`;
  }
}

// ── WS handler ────────────────────────────────────────────────────────────────
function handleWS(msg) {
  if (msg.type !== 'rotator_update') return;
  const idx = rotators.findIndex(r => r.id === msg.rotator.id);
  if (idx >= 0) { rotators[idx] = msg.rotator; updateCard(msg.rotator); }
}

// ── Render ────────────────────────────────────────────────────────────────────
function render() {
  const el = document.getElementById('rotators-container');
  if (!el) return;
  if (!rotators.length) {
    el.innerHTML = `<div style="text-align:center;padding:40px;font-family:var(--mono);font-size:12px;color:var(--dim)">Brak skonfigurowanych rotatorów.<br>Dodaj w Ustawienia → Admin.</div>`;
    return;
  }
  el.innerHTML = rotators.map(r => buildCard(r)).join('');
  rotators.forEach(r => drawCompass(r.id, r.azimuth, r.target_az, r.moving));
}

function buildCard(rot) {
  const info   = rot.model_info || {};
  const isAzEl = info.hasEl;
  const ok     = rot.connected;
  const sim    = rot.sim;

  return `
  <div class="rot-card box" id="rot-card-${rot.id}">
    <div class="box-header">
      <span>${_esc(rot.name || 'Rotator')}</span>
      <div style="display:flex;gap:6px;align-items:center;">
        <span style="font-family:var(--mono);font-size:10px;color:${ok?(sim?'var(--amber)':'var(--green)'):'var(--red)'}">
          ${ok ? (sim ? '⚠ SIM' : '✓ OK') : '✗ OFFLINE'}
        </span>
        <span style="font-family:var(--mono);font-size:10px;color:var(--dim)">${info.name||''}</span>
      </div>
    </div>

    <div class="rot-body">

      <!-- COMPASS -->
      <div class="compass-wrap">
        <canvas id="compass-${rot.id}" width="180" height="180"
          onclick="Rotator.compassClick(event,${rot.id})"
          title="Kliknij aby ustawić azymut"
          style="cursor:crosshair;border-radius:50%;display:block;"></canvas>
        <div style="text-align:center;margin-top:6px;">
          <div class="az-val" id="rot-az-disp-${rot.id}">${rot.azimuth.toFixed(1)}°</div>
          ${isAzEl ? `<div class="el-val" id="rot-el-disp-${rot.id}">EL: ${rot.elevation.toFixed(1)}°</div>` : ''}
          <div id="rot-moving-${rot.id}" style="font-family:var(--mono);font-size:10px;margin-top:3px;color:${rot.moving?'var(--amber)':'var(--dim)'}">
            ${rot.moving ? `● OBRACA → ${rot.target_az}°` : '● W POZYCJI'}
          </div>
        </div>
      </div>

      <!-- CONTROL PANEL -->
      <div class="rot-controls">

        <!-- Type a locator, degrees, or a callsign -->
        <div style="margin-bottom:10px;">
          <div class="form-label" style="margin-bottom:5px;">LOCATOR / ZNAK / STOPNIE</div>
          <div style="display:flex;gap:5px;">
            <input type="text" id="rot-target-${rot.id}"
              placeholder="KO02 / SP3MZM / 270"
              style="flex:1;padding:7px 10px;font-size:13px;font-family:var(--mono);"
              oninput="Rotator.previewTarget(${rot.id})"
              onkeydown="if(event.key==='Enter')Rotator.goTarget(${rot.id})">
            <button class="rot-btn" data-perm-disable="rotator_control"
              onclick="Rotator.goTarget(${rot.id})"
              style="background:rgba(76,219,106,0.15);border-color:var(--green2);color:var(--green);padding:7px 14px;letter-spacing:1px;">
              ▶ START
            </button>
          </div>
          <div id="rot-target-preview-${rot.id}"
            style="font-family:var(--mono);font-size:10px;color:var(--amber);margin-top:4px;min-height:14px;"></div>
        </div>

        <!-- Manual Az / El -->
        <div style="display:grid;grid-template-columns:1fr${isAzEl?' 1fr':''};gap:6px;margin-bottom:8px;">
          <div>
            <div class="form-label">AZYMUT °</div>
            <input type="number" id="rot-az-input-${rot.id}" min="0" max="450" step="1"
              value="${Math.round(rot.target_az)}"
              style="padding:6px 8px;font-size:13px;"
              onkeydown="if(event.key==='Enter')Rotator.setPos(${rot.id})">
          </div>
          ${isAzEl ? `
          <div>
            <div class="form-label">ELEWACJA °</div>
            <input type="number" id="rot-el-input-${rot.id}" min="0" max="180" step="1"
              value="${Math.round(rot.target_el)}"
              style="padding:6px 8px;font-size:13px;"
              onkeydown="if(event.key==='Enter')Rotator.setPos(${rot.id})">
          </div>` : ''}
        </div>

        <!-- Action buttons -->
        <div style="display:flex;gap:5px;margin-bottom:10px;">
          <button class="rot-btn" data-perm-disable="rotator_control"
            onclick="Rotator.setPos(${rot.id})"
            style="flex:2;background:rgba(76,219,106,0.1);">▶ IDZIE</button>
          <button class="rot-btn" data-perm-disable="rotator_control"
            onclick="Rotator.stop(${rot.id})"
            style="flex:1;color:var(--red);border-color:rgba(224,82,82,0.3);background:rgba(224,82,82,0.08);">■ STOP</button>
          <button class="rot-btn" data-perm-disable="rotator_control"
            onclick="Rotator.park(${rot.id})"
            title="Park (0°/0°)">⊙ PARK</button>
        </div>

        <!-- Preset directions -->
        <div>
          <div class="form-label" style="margin-bottom:5px;">PRESET KIERUNKI</div>
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:3px;">
            ${[['N','0'],['NE','45'],['E','90'],['SE','135'],['S','180'],['SW','225'],['W','270'],['NW','315']].map(([l,a]) =>
              `<button class="dir-btn" data-perm-disable="rotator_control"
                onclick="Rotator.goDir(${rot.id},${a})">${l}</button>`
            ).join('')}
          </div>
        </div>

      </div><!-- /rot-controls -->
    </div><!-- /rot-body -->
  </div>`;
}

// ── Preview the target while typing ───────────────────────────────────────────
function previewTarget(id) {
  const inp = document.getElementById(`rot-target-${id}`);
  const prv = document.getElementById(`rot-target-preview-${id}`);
  if (!inp || !prv) return;
  const result = resolveTarget(inp.value);
  prv.textContent = result ? `→ Azymut: ${result.az}° ${result.label || ''}` : (inp.value ? '⚠ Nieprawidłowy locator / kąt' : '');
  // Empty/invalid input -> result is null -> setHint(id, null) clears it,
  // same as deleting a callsign in the FT8 quick-log (see updateBeamRow).
  setHint(id, result ? result.az : null);
}

// ── Go to a locator/degrees ────────────────────────────────────────────────────
function goTarget(id) {
  const inp = document.getElementById(`rot-target-${id}`);
  if (!inp || !inp.value.trim()) return;
  const result = resolveTarget(inp.value);
  if (!result) { window.UI?.showToast('⚠ Nieprawidłowy locator lub kąt', 'error'); return; }

  // Fill in the az field and go
  const azInp = document.getElementById(`rot-az-input-${id}`);
  if (azInp) azInp.value = Math.round(result.az);
  setPos(id);
  window.UI?.showToast(`▶ Rotator #${id} → ${result.az}° ${result.label||''}`);
}

// ── Update the card ─────────────────────────────────────────────────────────
// Azimuth interpolation: the server sends the position every ~0.5s, we
// smoothly animate the needle between readings (60fps) so the motion
// looks continuous. We keep the currently displayed azimuth (_displayAz)
// and the target (_targetDisplayAz) per rotator.
const _displayAz = {};       // currently displayed angle (animated)
const _serverAz  = {};       // last angle from the server (animation target)
const _rotMeta   = {};       // {target_az, moving} per rotator
// FOUND 2026-10-18: the needle used to just ease 25%/frame toward the last
// server reading - at 60fps that closes the gap in ~0.25s, far faster than
// the ~0.5-1.5s between actual position updates from the rotor, so the
// needle visibly snapped-then-waited in lockstep with the jumpy numeric
// readout instead of looking continuous. Ported the speed-based
// extrapolation rotormini.js (the compact widget next to the VFO) already
// used successfully: estimate °/s from consecutive updates, then keep
// projecting the needle forward at that speed between updates (capped at
// the commanded target, so it can't overshoot).
const _azPrevSrv = {};       // previous angle from the server, per id
const _tPrevSrv  = {};       // time of the last server update, per id
const _speed     = {};       // smoothed °/s per id
let _animRaf = null;

// Direction HINT per rotator - a suggestion only (typed into this tab's own
// target input, or into the FT8 quick-log via Rotator.setHint from
// wsjtx.js's updateBeamRow), separate from _rotMeta's real target_az. Drawn
// on the dial as its own distinct marker (see drawCompass) and cleared as
// soon as the rotor actually starts moving (updateCard below) so it can
// never linger on the dial looking like a still-pending suggestion once
// the rotor is already headed somewhere (possibly from a DIFFERENT
// command - manual az, preset button, etc).
const _hintAz = {};

function setHint(id, az) {
  _hintAz[id] = (az == null || isNaN(az)) ? null : az;
  _redrawHint(id);
}

function clearHint(id) {
  _hintAz[id] = null;
  _redrawHint(id);
}

function _redrawHint(id) {
  const rot  = rotators.find(r => r.id === id);
  const az   = _displayAz[id] ?? rot?.azimuth ?? 0;
  const meta = _rotMeta[id] || {};
  drawCompass(id, az, meta.target_az ?? rot?.target_az ?? az, meta.moving ?? rot?.moving ?? false);
}

function _shortestAngleDiff(from, to) {
  // Shortest angular difference (-180..180), so the needle doesn't loop 350°->10°
  let diff = (to - from) % 360;
  if (diff > 180) diff -= 360;
  if (diff < -180) diff += 360;
  return diff;
}

function _animateNeedles() {
  const now = performance.now();
  let anyMoving = false;
  for (const id in _serverAz) {
    const meta = _rotMeta[id] || {};
    const cur  = _displayAz[id] ?? _serverAz[id];
    const base = _azPrevSrv[id] ?? _serverAz[id];
    if (meta.moving && (_speed[id] || 0) > 0.05) {
      // Actively turning - project forward from the last server reading at
      // the estimated speed, capped so it never runs past the commanded
      // target even if a status update is late.
      const dt = Math.min((now - (_tPrevSrv[id] || now)) / 1000, 2.0);
      const toTarget = _shortestAngleDiff(base, meta.target_az ?? base);
      if (Math.abs(toTarget) > 1) {
        const dir     = Math.sign(toTarget);
        const stepMag = Math.min(_speed[id] * dt, Math.abs(toTarget));
        _displayAz[id] = (base + dir * stepMag + 360) % 360;
      } else {
        _displayAz[id] = _serverAz[id];
      }
      anyMoving = true;
    } else {
      // Stopped - gentle ease to the exact position
      const diff = _shortestAngleDiff(cur, _serverAz[id]);
      if (Math.abs(diff) > 0.05) {
        _displayAz[id] = (cur + diff * 0.15 + 360) % 360;
        anyMoving = true;
      } else {
        _displayAz[id] = _serverAz[id];
      }
    }
    drawCompass(id, _displayAz[id], meta.target_az ?? _serverAz[id], meta.moving);
    // Also update the azimuth text smoothly
    const azD = document.getElementById(`rot-az-disp-${id}`);
    if (azD) azD.textContent = `${_displayAz[id].toFixed(1)}°`;
  }
  if (anyMoving) {
    _animRaf = requestAnimationFrame(_animateNeedles);
  } else {
    _animRaf = null;
  }
}

function _startNeedleAnim() {
  if (!_animRaf) _animRaf = requestAnimationFrame(_animateNeedles);
}

function updateCard(rot) {
  const elD  = document.getElementById(`rot-el-disp-${rot.id}`);
  const movD = document.getElementById(`rot-moving-${rot.id}`);
  if (elD)  elD.textContent  = `EL: ${rot.elevation.toFixed(1)}°`;
  if (movD) movD.innerHTML   = rot.moving
    ? `<span style="color:var(--amber)">● OBRACA → ${rot.target_az}°</span>`
    : `<span style="color:var(--dim)">● W POZYCJI</span>`;

  // The rotor is actually moving now (from ANY command - this update, a
  // preset button, another operator) - a lingering hint would read as "go
  // here next" when it's really just stale input. Clear it unconditionally
  // rather than only for the command that started this exact move.
  if (rot.moving && _hintAz[rot.id] != null) _hintAz[rot.id] = null;

  // Estimate rotation speed (°/s) from consecutive server readings while
  // moving - see _animateNeedles for why this matters. Smoothed (60/40)
  // like rotormini.js's own speed estimate, so one glitchy read doesn't
  // make the needle lurch.
  const now = performance.now();
  if (rot.moving && _tPrevSrv[rot.id] && now > _tPrevSrv[rot.id]) {
    const dt = (now - _tPrevSrv[rot.id]) / 1000;
    if (dt > 0.05 && dt < 3.0) {
      const rawSpeed = Math.abs(_shortestAngleDiff(_serverAz[rot.id] ?? rot.azimuth, rot.azimuth)) / dt;
      if (rawSpeed < 20) _speed[rot.id] = (_speed[rot.id] ?? 0) * 0.6 + rawSpeed * 0.4;
    }
  } else if (!rot.moving) {
    _speed[rot.id] = 0;
  }
  _azPrevSrv[rot.id] = _serverAz[rot.id] ?? rot.azimuth;
  _tPrevSrv[rot.id]  = now;

  // Save the target from the server and start a smooth needle animation to that position
  _serverAz[rot.id] = rot.azimuth;
  _rotMeta[rot.id]  = { target_az: rot.target_az, moving: rot.moving };
  if (_displayAz[rot.id] === undefined) {
    // First reading — set immediately, no animation
    _displayAz[rot.id] = rot.azimuth;
    drawCompass(rot.id, rot.azimuth, rot.target_az, rot.moving);
    const azD = document.getElementById(`rot-az-disp-${rot.id}`);
    if (azD) azD.textContent = `${rot.azimuth.toFixed(1)}°`;
  } else {
    _startNeedleAnim();
  }
}

// ── Compass canvas ───────────────────────────────────────────────────────────
function drawCompass(id, azimuth, target, moving) {
  const canvas = document.getElementById(`compass-${id}`);
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const cx = W/2, cy = H/2, R = W/2 - 5;

  ctx.clearRect(0, 0, W, H);

  // Circle background
  ctx.beginPath(); ctx.arc(cx,cy,R,0,Math.PI*2);
  ctx.fillStyle = '#090c09'; ctx.fill();
  ctx.strokeStyle = 'rgba(76,219,106,0.18)'; ctx.lineWidth = 1; ctx.stroke();

  // Inner rings
  [0.35,0.65,0.88].forEach(f => {
    ctx.beginPath(); ctx.arc(cx,cy,R*f,0,Math.PI*2);
    ctx.strokeStyle = 'rgba(76,219,106,0.07)'; ctx.lineWidth = 0.5; ctx.stroke();
  });

  // Lines every 10° — longer every 30°, cardinal every 90°
  for (let d = 0; d < 360; d += 10) {
    const rad = (d-90)*Math.PI/180;
    const inner = d % 90 === 0 ? 0.78 : (d % 30 === 0 ? 0.83 : 0.89);
    ctx.beginPath();
    ctx.moveTo(cx + R*inner*Math.cos(rad), cy + R*inner*Math.sin(rad));
    ctx.lineTo(cx + R*0.96*Math.cos(rad), cy + R*0.96*Math.sin(rad));
    ctx.strokeStyle = d%90===0 ? 'rgba(76,219,106,0.55)' : (d%30===0 ? 'rgba(76,219,106,0.22)' : 'rgba(76,219,106,0.1)');
    ctx.lineWidth   = d%90===0 ? 1.5 : 0.7;
    ctx.stroke();
  }

  // Labels every 30°
  const lbls = {0:'N',30:'30',60:'60',90:'E',120:'120',150:'150',180:'S',210:'210',240:'240',270:'W',300:'300',330:'330'};
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  Object.entries(lbls).forEach(([d,l]) => {
    const rad  = (parseInt(d)-90)*Math.PI/180;
    const dist = d==='0'||d==='90'||d==='180'||d==='270' ? 0.68 : 0.71;
    ctx.font      = ['0','90','180','270'].includes(d) ? 'bold 11px Share Tech Mono,monospace' : '9px Share Tech Mono,monospace';
    ctx.fillStyle = ['0','90','180','270'].includes(d) ? 'rgba(76,219,106,0.8)' : 'rgba(76,219,106,0.45)';
    ctx.fillText(l, cx + R*dist*Math.cos(rad), cy + R*dist*Math.sin(rad));
  });

  // Direction HINT (dotted blue) - a typed-in suggestion, not a real
  // command (see _hintAz above). Drawn before the target/needle so it
  // never visually competes with them - it's a suggestion, not a state.
  const hint = _hintAz[id];
  if (hint != null) {
    const hRad = (hint-90)*Math.PI/180;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + R*0.82*Math.cos(hRad), cy + R*0.82*Math.sin(hRad));
    ctx.strokeStyle = 'rgba(100,180,255,0.6)';
    ctx.lineWidth = 1.5; ctx.setLineDash([2,3]); ctx.stroke(); ctx.setLineDash([]);
    const hx = cx + R*0.82*Math.cos(hRad), hy = cy + R*0.82*Math.sin(hRad);
    ctx.beginPath();
    ctx.arc(hx, hy, 3.5, 0, Math.PI*2);
    ctx.fillStyle = 'rgba(100,180,255,0.8)'; ctx.fill();
  }

  // Target line (dashed yellow)
  if (Math.abs(target - azimuth) > 1) {
    const tRad = (target-90)*Math.PI/180;
    ctx.beginPath();
    ctx.moveTo(cx, cy);
    ctx.lineTo(cx + R*0.82*Math.cos(tRad), cy + R*0.82*Math.sin(tRad));
    ctx.strokeStyle = moving ? 'rgba(240,180,41,0.7)' : 'rgba(240,180,41,0.35)';
    ctx.lineWidth = 1.5; ctx.setLineDash([5,4]); ctx.stroke(); ctx.setLineDash([]);

    // Target dot
    const tx = cx + R*0.82*Math.cos(tRad), ty = cy + R*0.82*Math.sin(tRad);
    ctx.beginPath();
    ctx.arc(tx, ty, 4, 0, Math.PI*2);
    ctx.fillStyle = 'rgba(240,180,41,0.7)'; ctx.fill();
  }

  // Azimuth needle (green)
  const aRad = (azimuth-90)*Math.PI/180;
  // Needle shadow
  ctx.shadowBlur = 8; ctx.shadowColor = 'rgba(76,219,106,0.5)';
  ctx.beginPath();
  ctx.moveTo(cx - R*0.28*Math.cos(aRad), cy - R*0.28*Math.sin(aRad));
  ctx.lineTo(cx + R*0.80*Math.cos(aRad), cy + R*0.80*Math.sin(aRad));
  ctx.strokeStyle = '#4cdb6a'; ctx.lineWidth = 2.5; ctx.stroke();
  ctx.shadowBlur = 0;

  // Arrowhead
  const ax = cx+R*0.80*Math.cos(aRad), ay = cy+R*0.80*Math.sin(aRad);
  ctx.beginPath();
  ctx.moveTo(ax, ay);
  ctx.lineTo(ax - 11*Math.cos(aRad) + 5*Math.sin(aRad), ay - 11*Math.sin(aRad) - 5*Math.cos(aRad));
  ctx.lineTo(ax - 11*Math.cos(aRad) - 5*Math.sin(aRad), ay - 11*Math.sin(aRad) + 5*Math.cos(aRad));
  ctx.closePath();
  ctx.fillStyle = '#4cdb6a'; ctx.fill();

  // Tail
  ctx.beginPath();
  ctx.moveTo(cx - R*0.28*Math.cos(aRad), cy - R*0.28*Math.sin(aRad));
  ctx.lineTo(cx - R*0.28*Math.cos(aRad) + 6*Math.sin(aRad),  cy - R*0.28*Math.sin(aRad) - 6*Math.cos(aRad));
  ctx.lineTo(cx - R*0.28*Math.cos(aRad) - 6*Math.sin(aRad),  cy - R*0.28*Math.sin(aRad) + 6*Math.cos(aRad));
  ctx.closePath(); ctx.fillStyle = '#2a8a3a'; ctx.fill();

  // Center
  ctx.beginPath(); ctx.arc(cx,cy,5,0,Math.PI*2);
  ctx.fillStyle = '#4cdb6a'; ctx.fill();

  // Value in the center
  ctx.font = 'bold 14px Orbitron,sans-serif';
  ctx.fillStyle = 'rgba(76,219,106,0.85)';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(`${Math.round(azimuth)}°`, cx, cy + R*0.47);
}

// ── Compass click ────────────────────────────────────────────────────────────
function compassClick(event, id) {
  const canvas = event.target;
  const rect   = canvas.getBoundingClientRect();
  const cx = canvas.width/2, cy = canvas.height/2;
  const x  = (event.clientX - rect.left) * (canvas.width / rect.width)  - cx;
  const y  = (event.clientY - rect.top)  * (canvas.height / rect.height) - cy;
  const az = Math.round(((Math.atan2(y,x)*180/Math.PI) + 90 + 360) % 360);
  const azInp = document.getElementById(`rot-az-input-${id}`);
  if (azInp) azInp.value = az;
  // Show the preview
  const targetInp = document.getElementById(`rot-target-${id}`);
  if (targetInp) { targetInp.value = String(az); previewTarget(id); }
  setPos(id);
}

// ── Actions ───────────────────────────────────────────────────────────────────
async function setPos(id) {
  const az  = parseFloat(document.getElementById(`rot-az-input-${id}`)?.value) || 0;
  const elI = document.getElementById(`rot-el-input-${id}`);
  const el  = elI ? (parseFloat(elI.value)||0) : 0;
  await apiPost(`/api/rotator/${id}/position`, {az, el});
}

async function stop(id)  { await apiPost(`/api/rotator/${id}/stop`); }

// Was posting to /api/rotator/<id>/park - a backend route that never
// existed (only /position, /stop, /test are implemented), so PARK 404'd
// silently on every click. "Park" is just "go to 0/0" - reuse the real
// /position endpoint instead of inventing a dedicated backend route for it.
async function park(id) {
  const azInp = document.getElementById(`rot-az-input-${id}`);
  if (azInp) azInp.value = 0;
  const elInp = document.getElementById(`rot-el-input-${id}`);
  if (elInp) elInp.value = 0;
  const targetInp = document.getElementById(`rot-target-${id}`);
  if (targetInp) { targetInp.value = '0'; previewTarget(id); }
  await apiPost(`/api/rotator/${id}/position`, {az: 0, el: 0});
}

function goDir(id, az) {
  const azInp = document.getElementById(`rot-az-input-${id}`);
  if (azInp) azInp.value = az;
  const tInp  = document.getElementById(`rot-target-${id}`);
  if (tInp)  { tInp.value = String(az); previewTarget(id); }
  setPos(id);
}

async function apiPost(url, body) {
  try {
    const r = await fetch(url, { method:'POST', headers:{'Content-Type':'application/json'}, body: body ? JSON.stringify(body) : undefined });
    const res = await r.json();
    // The backend now rejects /position and /stop for a viewer or someone
    // without the radio claimed (403 {error}) — without this check a click
    // simply did nothing, with no feedback at all (same endpoint as rotormini.js).
    if (res && res.error) window.UI?.showToast?.('✗ ' + res.error, 'error');
    return res;
  } catch(e) {
    console.error('[rotator]', e);
    window.UI?.showToast?.('✗ ' + e.message, 'error');
  }
}

window.Rotator = { load, render, handleWS, compassClick, previewTarget, goTarget, setPos, stop, park, goDir, setHint, clearHint };

})();
