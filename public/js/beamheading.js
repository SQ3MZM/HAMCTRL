/**
 * beamheading.js — kierunek rotora na korespondenta
 *
 * Po wpisaniu znaku w logu QSO pokazuje azymut (i dystans), na ktory ustawic
 * antene. Wszystko liczone W PRZEGLADARCE — serwer nie robi nic dodatkowego.
 *
 * Lokator korespondenta szukamy po kolei:
 *   1. pole "grid" w formularzu (jesli operator juz je wypelnil / przyszlo z FT8)
 *   2. cache call->grid z dekodow FT8 (endpoint /api/qsolog/calls + FT8)
 *   3. wlasny log QSO (stacja juz przepracowana - znamy jej lokator)
 *   4. prefiks znaku -> przyblizone wspolrzedne kraju (tablica ponizej)
 *
 * Punkt 4 daje kierunek "z grubsza", ale przy szerokosci wiazki typowej anteny
 * kilka stopni bledu nie ma znaczenia — a dla DX-u to i tak lepsze niz nic.
 */
(function () {
'use strict';

// ── Maidenhead -> szerokosc/dlugosc (srodek pola) ─────────────────────────────
function gridToLatLon(grid) {
  const g = (grid || '').trim().toUpperCase();
  if (!/^[A-R]{2}\d{2}([A-X]{2})?$/.test(g)) return null;
  // RR73 formalnie pasuje do wzorca lokatora, ale to ZAKONCZENIE QSO
  // (protokol wybral grid z Antarktydy wlasnie dlatego, ze nikt stamtad nie
  // nadaje). Bez tego wyjatku antena jechalaby "na pozegnanie".
  if (g === 'RR73') return null;
  const A = 'A'.charCodeAt(0);
  let lon = (g.charCodeAt(0) - A) * 20 - 180;
  let lat = (g.charCodeAt(1) - A) * 10 - 90;
  lon += parseInt(g[2], 10) * 2;
  lat += parseInt(g[3], 10) * 1;
  if (g.length >= 6) {
    // Podkwadrat: 24 podzialy na kwadrat (2° dlugosci, 1° szerokosci)
    lon += (g.charCodeAt(4) - A) * (2 / 24) + (1 / 24);   // +srodek podkwadratu
    lat += (g.charCodeAt(5) - A) * (1 / 24) + (0.5 / 24);
  } else {
    lon += 1;           // srodek kwadratu
    lat += 0.5;
  }
  return { lat, lon };
}

// ── Azymut i dystans (wzor great-circle) ──────────────────────────────────────
function beamAndDistance(from, to) {
  const R = 6371.0;                    // promien Ziemi [km]
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
    azLong:   Math.round((az + 180) % 360),   // dluga droga (long path)
    distance: Math.round(dist),
    distLong: Math.round(40075 - dist),       // obwod Ziemi - krotka droga
  };
}

// ── Przyblizone wspolrzedne wg prefiksu znaku ─────────────────────────────────
// Tylko gdy nie znamy lokatora. Srodek kraju/regionu wystarcza do ustawienia
// anteny. Lista skrocona do najczestszych w ruchu europejskim + glowne DX.
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

function prefixToLatLon(call) {
  const c = (call || '').trim().toUpperCase();
  if (!c) return null;
  // Znak lamany — wez czlon prefiksowy (np. F/SQ3MZM -> F)
  const base = c.includes('/')
      ? c.split('/').reduce((a, b) => (a.length <= b.length ? a : b))
      : c;
  // Probuj od najdluzszego prefiksu (UA9 przed UA)
  for (let len = 3; len >= 1; len--) {
    const p = base.slice(0, len);
    if (PREFIX_LOC[p]) {
      const [lat, lon] = PREFIX_LOC[p];
      return { lat, lon, approx: true };
    }
  }
  return null;
}

// ── Publiczne API ─────────────────────────────────────────────────────────────
// Zwraca {azimuth, azLong, distance, source} albo null.
//   source: 'grid' (dokladny lokator) | 'prefix' (przyblizony kraj)
function headingFor(call, grid) {
  // Azymut liczymy OD ANTENY, czyli od lokatora STACJI (ustawianego przez
  // admina jako STATION_LOCATOR) — nie od lokatora zalogowanego operatora.
  // Przy pracy zdalnej kazdy user siedzi gdzie indziej, a rotor obraca sie
  // w jednym miejscu; kierunek musi byc ten sam dla wszystkich.
  const myGrid = (window.S?.stationLocator
                  || window.AppState?.stationLocator || '').toUpperCase();
  const me = gridToLatLon(myGrid);
  if (!me) return null;                       // nie znamy pozycji stacji

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
