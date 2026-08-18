/*
 * grid_math.js — Maidenhead grid math: conversion to lat/lon, distance
 * (great-circle) and azimuth (initial bearing).
 *
 * Maidenhead format:
 *   2 chars (Field):   A-R x A-R    - 20 degrees longitude x 10 degrees latitude
 *   4 chars (Square):  ...+ 0-9 x 0-9 - 2 x 1 degree
 *   6 chars (Subsq):   ...+ a-x x a-x - 5' x 2.5'
 *
 * Example: JO72 → SP3GSK QTH in Wielkopolska (Poland)
 *   J = 9 (longitude +18°..+20°)
 *   O = 14 (latitude +50°..+52°)
 *   7 = (16°..18°) → longitude ~+16°
 *   2 = (52°N center)
 */

(function() {
'use strict';

// Maidenhead → {lat, lon} (square center)
function gridToLatLon(grid) {
  if (!grid) return null;
  const g = grid.toUpperCase().trim();
  if (g.length < 4) return null;

  // Validate characters
  if (!/^[A-R]{2}\d{2}([A-X]{2})?$/.test(g)) return null;

  // Field (2 chars)
  const A = g.charCodeAt(0) - 65;  // 0..17
  const B = g.charCodeAt(1) - 65;  // 0..17 (0=AA-AR near Antarctica)
  let lon = A * 20 - 180;   // -180..+160
  let lat = B * 10 - 90;    // -90..+80

  // Square (2 digits)
  const C = parseInt(g[2], 10);
  const D = parseInt(g[3], 10);
  lon += C * 2;   // 0..18 -> +0..+18
  lat += D * 1;   // 0..9 -> +0..+9

  // Subsquare (2 optional chars)
  if (g.length >= 6) {
    const E = g.charCodeAt(4) - 65;
    const F = g.charCodeAt(5) - 65;
    lon += (E + 0.5) * (2/24);    // 5min ~ 2/24 degree
    lat += (F + 0.5) * (1/24);    // 2.5min ~ 1/24 degree
  } else {
    // Add half a square so the center falls in the middle
    lon += 1;      // +1 degree (half of 2°)
    lat += 0.5;    // +0.5 degree (half of 1°)
  }

  return { lat, lon };
}

// Great-circle distance (Haversine formula) in km
function distanceKm(grid1, grid2) {
  const p1 = gridToLatLon(grid1);
  const p2 = gridToLatLon(grid2);
  if (!p1 || !p2) return null;

  const R = 6371.0;  // Earth radius in km
  const toRad = Math.PI / 180;
  const dLat = (p2.lat - p1.lat) * toRad;
  const dLon = (p2.lon - p1.lon) * toRad;
  const lat1 = p1.lat * toRad;
  const lat2 = p2.lat * toRad;

  const a = Math.sin(dLat/2) ** 2 +
            Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon/2) ** 2;
  const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a));
  return R * c;
}

// Initial bearing (great-circle azimuth from p1 to p2) in degrees 0-360
// (0 = north, 90 = east, 180 = south, 270 = west)
function azimuthDeg(grid1, grid2) {
  const p1 = gridToLatLon(grid1);
  const p2 = gridToLatLon(grid2);
  if (!p1 || !p2) return null;

  const toRad = Math.PI / 180;
  const lat1 = p1.lat * toRad;
  const lat2 = p2.lat * toRad;
  const dLon = (p2.lon - p1.lon) * toRad;

  const y = Math.sin(dLon) * Math.cos(lat2);
  const x = Math.cos(lat1) * Math.sin(lat2) -
            Math.sin(lat1) * Math.cos(lat2) * Math.cos(dLon);
  let bearing = Math.atan2(y, x) / toRad;
  return ((bearing + 360) % 360);
}

// Long-path azimuth (opposite direction, long way around to the station)
function longPathAzimuthDeg(grid1, grid2) {
  const az = azimuthDeg(grid1, grid2);
  if (az === null) return null;
  return (az + 180) % 360;
}

// Format distance: "1234 km" or "12,345 km"
function formatKm(km) {
  if (km === null || km === undefined) return '';
  if (km >= 10000) return `${Math.round(km).toLocaleString('en-US')} km`;
  if (km >= 1000)  return `${Math.round(km)} km`;
  return `${Math.round(km)} km`;
}

// Azimuth as "045° NE" (heading + compass direction)
function formatAzimuth(deg) {
  if (deg === null || deg === undefined) return '';
  const dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE',
                 'S','SSW','SW','WSW','W','WNW','NW','NNW'];
  const idx = Math.round(deg / 22.5) % 16;
  return `${String(Math.round(deg)).padStart(3,'0')}° ${dirs[idx]}`;
}

window.GridMath = {
  gridToLatLon, distanceKm, azimuthDeg, longPathAzimuthDeg,
  formatKm, formatAzimuth,
};
})();
