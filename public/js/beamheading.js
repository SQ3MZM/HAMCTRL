/**
 * beamheading.js — rotator heading to the correspondent
 *
 * After typing a callsign into the QSO log, shows the azimuth (and
 * distance) to point the antenna at. Everything is computed IN THE
 * BROWSER — the server does nothing extra.
 *
 * The correspondent's locator is looked up in order:
 *   1. the "grid" field in the form (if the operator already filled it in / it came from FT8)
 *   2. the call->grid cache from FT8 decodes (endpoint /api/qsolog/calls + FT8)
 *   3. our own QSO log (the station was already worked - we know its locator)
 *   4. the callsign prefix -> approximate country coordinates (table below)
 *
 * Point 4 gives a "rough" heading, but at the beamwidth of a typical
 * antenna a few degrees of error doesn't matter — and for DX it's still
 * better than nothing.
 */
(function () {
'use strict';

// ── Maidenhead -> latitude/longitude (field center) ───────────────────────────
function gridToLatLon(grid) {
  const g = (grid || '').trim().toUpperCase();
  if (!/^[A-R]{2}\d{2}([A-X]{2})?$/.test(g)) return null;
  // RR73 formally matches the locator pattern, but it signals QSO END
  // (the protocol picked a grid in Antarctica precisely because no one
  // transmits from there). Without this exception the antenna would swing
  // to "say goodbye".
  if (g === 'RR73') return null;
  const A = 'A'.charCodeAt(0);
  let lon = (g.charCodeAt(0) - A) * 20 - 180;
  let lat = (g.charCodeAt(1) - A) * 10 - 90;
  lon += parseInt(g[2], 10) * 2;
  lat += parseInt(g[3], 10) * 1;
  if (g.length >= 6) {
    // Subsquare: 24 divisions per square (2° longitude, 1° latitude)
    lon += (g.charCodeAt(4) - A) * (2 / 24) + (1 / 24);   // +subsquare center
    lat += (g.charCodeAt(5) - A) * (1 / 24) + (0.5 / 24);
  } else {
    lon += 1;           // square center
    lat += 0.5;
  }
  return { lat, lon };
}

// ── Azimuth and distance (great-circle formula) ───────────────────────────────
function beamAndDistance(from, to) {
  const R = 6371.0;                    // Earth radius [km]
  const rad = Math.PI / 180, deg = 180 / Math.PI;
  const la1 = from.lat * rad, lo1 = from.lon * rad;
  const la2 = to.lat   * rad, lo2 = to.lon   * rad;
  const dLon = lo2 - lo1;

  const y = Math.sin(dLon) * Math.cos(la2);
  const x = Math.cos(la1) * Math.sin(la2) -
            Math.sin(la1) * Math.cos(la2) * Math.cos(dLon);
  let az = Math.atan2(y, x) * deg;
  if (az < 0) az += 360;

  const dSig = Math.acos(Math.min(1, Math.max(-1,
      Math.sin(la1) * Math.sin(la2) +
      Math.cos(la1) * Math.cos(la2) * Math.cos(dLon))));
  const dist = R * dSig;

  return {
    azimuth:  Math.round(az),
    azLong:   Math.round((az + 180) % 360),   // long path
    distance: Math.round(dist),
    distLong: Math.round(40075 - dist),       // Earth's circumference minus the short path
  };
}

// ── Approximate coordinates by callsign prefix ────────────────────────────────
// Only used when the locator is unknown. The center of the country/region
// is good enough for pointing the antenna. List trimmed to the most common
// in European traffic + major DX.
const PREFIX_LOC = {
  'SP':[52.0,19.0], 'SQ':[52.0,19.0], 'SN':[52.0,19.0], 'SO':[52.0,19.0],
  'DL':[51.0,10.0], 'DK':[51.0,10.0], 'DJ':[51.0,10.0], 'DF':[51.0,10.0], 'DB':[51.0,10.0],
  'G':[52.5,-1.5],  'M':[52.5,-1.5],  '2E':[52.5,-1.5], 'GW':[52.3,-3.6], 'GM':[56.8,-4.2],
  'GI':[54.6,-6.5], 'GD':[54.2,-4.5], 'GJ':[49.2,-2.1], 'GU':[49.5,-2.5],
  'F':[46.6,2.4],   'ON':[50.6,4.6],  'PA':[52.2,5.5],  'LX':[49.8,6.1],
  'EA':[40.3,-3.7], 'EB':[40.3,-3.7], 'EC':[40.3,-3.7], 'CT':[39.6,-8.0],
  'I':[42.8,12.5],  'IK':[42.8,12.5], 'IZ':[42.8,12.5], 'IW':[42.8,12.5],
  'HB':[46.8,8.2],  'OE':[47.6,14.1], 'OK':[49.8,15.5], 'OM':[48.7,19.5],
  'HA':[47.2,19.5], 'HG':[47.2,19.5], 'YO':[45.9,25.0], 'LZ':[42.7,25.5],
  'YU':[44.0,20.9], 'YT':[44.0,20.9], 'S5':[46.1,14.8], '9A':[45.1,15.2],
  'Z3':[41.6,21.7], 'SV':[39.0,22.0], 'TA':[39.0,35.2], 'ZA':[41.1,20.1],
  'OH':[64.0,26.0], 'SM':[62.0,15.0], 'SA':[62.0,15.0], 'LA':[61.0,8.5],
  'OZ':[56.0,10.0], 'OY':[62.0,-6.8], 'TF':[64.9,-19.0], 'ES':[58.6,25.0],
  'YL':[56.9,24.6], 'LY':[55.2,23.9], 'EW':[53.7,27.9], 'UR':[48.4,31.2],
  'UT':[48.4,31.2], 'UX':[48.4,31.2], 'US':[48.4,31.2], 'ER':[47.0,28.9],
  'UA':[55.8,37.6], 'RA':[55.8,37.6], 'RU':[55.8,37.6], 'RK':[55.8,37.6],
  'RN':[55.8,37.6], 'RV':[55.8,37.6], 'RW':[55.8,37.6], 'RZ':[55.8,37.6],
  'UA9':[55.0,73.4],'RA9':[55.0,73.4],'UA0':[56.0,105.0],'RA0':[56.0,105.0],
  'EI':[53.4,-8.0], 'EJ':[53.4,-8.0], '4X':[31.5,34.8], '4Z':[31.5,34.8],
  'W':[39.8,-98.6], 'K':[39.8,-98.6], 'N':[39.8,-98.6], 'AA':[39.8,-98.6],
  'VE':[56.1,-106.3],'VA':[56.1,-106.3],
  'JA':[36.2,138.3],'JE':[36.2,138.3],'JF':[36.2,138.3],'JH':[36.2,138.3],
  'JR':[36.2,138.3],'7K':[35.7,139.7],
  'VK':[-25.3,133.8],'ZL':[-41.0,174.0],'ZS':[-30.6,22.9],
  'PY':[-14.2,-51.9],'LU':[-38.4,-63.6],'CE':[-35.7,-71.5],
  'BY':[35.9,104.2],'BG':[35.9,104.2],'BA':[35.9,104.2],
  'HL':[35.9,127.8],'VU':[20.6,79.0],  'YB':[-0.8,113.9],
  'EX':[41.2,74.8], 'UN':[48.0,66.9],  'EY':[38.9,71.3],
  'HS':[15.9,101.0],'9M':[4.2,101.9],  '9V':[1.35,103.8],
  'ZC':[35.1,33.4], '5B':[35.1,33.4],  'C3':[42.5,1.5],
  'CN':[31.8,-7.1], 'SU':[26.8,30.8],  '3V':[33.9,9.5],  '7X':[28.0,1.7],
  'EL':[6.4,-9.4],  '5N':[9.1,8.7],    'TR':[-0.8,11.6], '9J':[-13.1,27.8],
};

// ── Fallback: country-center coordinates, keyed by the SAME country names
// dxcc.js's DXCC_TABLE already resolves (2026-10-20, live report: tried
// "5W" (Samoa) and "7T" (Algeria), got no hint at all - PREFIX_LOC above
// is deliberately small, "most common in European traffic + major DX",
// so plenty of real prefixes just aren't in it). Rather than hand-typing
// another ~150 prefix->coords entries here (duplicating dxcc.js's own,
// already-maintained prefix->country table and its compound-callsign
// handling), this looks up window.DXCC.lookup(call)'s country NAME and
// maps THAT to approximate coordinates - one shared source of truth for
// "which prefix is which country" (dxcc.js), this table only has to cover
// "which country is roughly where" (never needs the prefix list itself).
const COUNTRY_CENTER = {
  // Europe
  poland:[52.0,19.0], germany:[51.0,10.0], england:[52.5,-1.5], scotland:[56.8,-4.2],
  wales:[52.3,-3.6], 'n. ireland':[54.6,-6.5], 'isle of man':[54.2,-4.5],
  jersey:[49.2,-2.1], guernsey:[49.5,-2.5], france:[46.6,2.4],
  guadeloupe:[16.3,-61.6], martinique:[14.6,-61.0], 'french guiana':[4.0,-53.0],
  reunion:[-21.1,55.5], italy:[42.8,12.5], sardinia:[40.1,9.1],
  pantelleria:[36.8,12.0], lampedusa:[35.5,12.6], spain:[40.3,-3.7],
  'balearic is.':[39.6,3.0], 'canary is.':[28.3,-16.6], 'ceuta & melilla':[35.9,-5.3],
  russia:[55.8,37.6], 'russia (as)':[56.0,90.0], ukraine:[48.4,31.2],
  belarus:[53.7,27.9], 'czech rep.':[49.8,15.5], slovakia:[48.7,19.5],
  hungary:[47.2,19.5], romania:[45.9,25.0], bulgaria:[42.7,25.5],
  slovenia:[46.1,14.8], croatia:[45.1,15.2], bosnia:[44.2,17.7],
  serbia:[44.0,20.9], 'n. macedonia':[41.6,21.7], kosovo:[42.6,21.0],
  montenegro:[42.7,19.3], albania:[41.1,20.1], greece:[39.0,22.0],
  turkey:[39.0,35.2], cyprus:[35.1,33.4], sweden:[62.0,15.0], norway:[61.0,8.5],
  svalbard:[78.2,15.6], 'jan mayen':[71.0,-8.5], finland:[64.0,26.0],
  denmark:[56.0,10.0], greenland:[71.7,-42.6], 'faroe is.':[62.0,-6.8],
  iceland:[64.9,-19.0], belgium:[50.6,4.6], netherlands:[52.2,5.5],
  luxembourg:[49.8,6.1], switzerland:[46.8,8.2], liechtenstein:[47.1,9.5],
  austria:[47.6,14.1], estonia:[58.6,25.0], latvia:[56.9,24.6],
  lithuania:[55.2,23.9], portugal:[39.6,-8.0], madeira:[32.7,-17.0],
  azores:[37.7,-25.7], ireland:[53.4,-8.0], malta:[35.9,14.5],
  'san marino':[43.9,12.4], monaco:[43.7,7.4], andorra:[42.5,1.5], vatican:[41.9,12.5],
  gibraltar:[36.1,-5.3], 'uk bases cyprus':[34.9,33.2],

  // North America / Caribbean
  usa:[39.8,-98.6], hawaii:[20.8,-156.3], alaska:[64.2,-149.5],
  'puerto rico':[18.2,-66.6], canada:[56.1,-106.3], mexico:[23.6,-102.5],
  cuba:[21.5,-79.5], 'dominican rep.':[18.7,-70.2], haiti:[19.0,-72.4],
  'us virgin is.':[18.3,-64.9], 'st. kitts':[17.3,-62.7], 'cayman is.':[19.5,-80.6],
  belize:[17.2,-88.5],

  // South America
  georgia:[42.3,43.4], brazil:[-14.2,-51.9], paraguay:[-23.4,-58.4],
  chile:[-35.7,-71.5], argentina:[-38.4,-63.6], uruguay:[-32.5,-55.8],
  peru:[-9.2,-75.0], colombia:[4.6,-74.1], venezuela:[6.4,-66.6], ecuador:[-1.8,-78.2],

  // Asia
  japan:[36.2,138.3], 's. korea':[35.9,127.8], china:[35.9,104.2],
  taiwan:[23.7,121.0], 'hong kong':[22.3,114.2], macao:[22.2,113.5],
  india:[20.6,79.0], thailand:[15.9,101.0], cambodia:[12.6,104.9],
  vietnam:[14.1,108.3], indonesia:[-0.8,113.9], malaysia:[4.2,101.9],
  singapore:[1.35,103.8], philippines:[12.9,121.8], mongolia:[46.9,103.8],

  // Middle East
  israel:[31.5,34.8], kuwait:[29.3,47.5], oman:[21.5,55.9], uae:[23.4,53.8],
  qatar:[25.3,51.2], bahrain:[26.0,50.5], 'saudi arabia':[23.9,45.1],
  iraq:[33.2,43.7], armenia:[40.1,45.0], azerbaijan:[40.1,47.6],
  jordan:[30.6,36.2], lebanon:[33.9,35.9],

  // Africa
  's. africa':[-30.6,22.9], morocco:[31.8,-7.1], algeria:[28.0,1.7],
  tunisia:[33.9,9.5], libya:[26.3,17.2], egypt:[26.8,30.8], senegal:[14.5,-14.5],
  uganda:[1.4,32.3], kenya:[-0.0,37.9], namibia:[-22.6,17.1],

  // Oceania
  australia:[-25.3,133.8], 'aus islands':[-10.5,105.6], 'new zealand':[-41.0,174.0],
  'new caledonia':[-20.9,165.6], 'french polynesia':[-17.7,-149.4],
  marianas:[15.2,145.7], guam:[13.4,144.8], 'american samoa':[-14.3,-170.7],
  samoa:[-13.8,-172.0], micronesia:[7.4,150.6], 'marshall is.':[7.1,171.4],
  tuvalu:[-7.1,177.6], kiribati:[1.4,173.0], vanuatu:[-15.4,166.9],
  fiji:[-17.7,178.1], tonga:[-21.2,-175.2], 'solomon is.':[-9.6,160.2],
};

function prefixToLatLon(call) {
  const c = (call || '').trim().toUpperCase();
  if (!c) return null;
  // Compound callsign — take the prefix segment (e.g. F/SQ3MZM -> F)
  const base = c.includes('/')
      ? c.split('/').reduce((a, b) => (a.length <= b.length ? a : b))
      : c;
  // Try the longest prefix first (UA9 before UA)
  for (let len = 3; len >= 1; len--) {
    const p = base.slice(0, len);
    if (PREFIX_LOC[p]) {
      const [lat, lon] = PREFIX_LOC[p];
      return { lat, lon, approx: true };
    }
  }
  // Fall back to dxcc.js's broader prefix->country table (reuses its own
  // compound-callsign handling too - pass the ORIGINAL call, not `base`
  // above, which only does OUR simpler split) + the country-center table
  // above, instead of this file never recognizing anything dxcc.js itself
  // already knows.
  const info = window.DXCC?.lookup(c);
  if (info && info.name) {
    const coords = COUNTRY_CENTER[info.name.toLowerCase()];
    if (coords) return { lat: coords[0], lon: coords[1], approx: true };
  }
  return null;
}

// ── Public API ─────────────────────────────────────────────────────────────
// Returns {azimuth, azLong, distance, source} or null.
//   source: 'grid' (exact locator) | 'prefix' (approximate country)
function headingFor(call, grid) {
  // The azimuth is computed FROM THE ANTENNA, i.e. from the STATION's
  // locator (set by the admin as STATION_LOCATOR) — not from the logged-in
  // operator's locator. In remote operation each user sits somewhere
  // different, but the rotator turns in one place; the heading must be the same for everyone.
  const myGrid = (window.S?.stationLocator
                  || window.AppState?.stationLocator || '').toUpperCase();
  const me = gridToLatLon(myGrid);
  if (!me) return null;                       // station position unknown

  let dx = gridToLatLon(grid);
  let source = 'grid';
  if (!dx) {
    dx = prefixToLatLon(call);
    source = 'prefix';
  }
  if (!dx) return null;

  const r = beamAndDistance(me, dx);
  r.source = source;
  return r;
}

window.BeamHeading = { headingFor, gridToLatLon, beamAndDistance, prefixToLatLon };
})();
