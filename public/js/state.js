/**
 * state.js — shared frontend state
 */
window.AppState = {
  freq:      14200000,
  freqB:     14200000,
  mode:      'USB',
  bandwidth: 2400,
  ptt:       false,
  rfPower:   100,
  afGain:    50,
  squelch:   0,
  sMeter:    0,
  split:     false,
  vfo:       'VFOA',
  connected: false,
  sim:       false,
  models:    {},
  bands:     {},
  modes:     [],
  rigs:      [],
  callsign:  '',
  stationLocator: 'KO02',  // Maidenhead locator of the STATION (where the
                           // antenna is) — for rotator azimuth calculation
  operatorLocator: '',     // locator of the LOGGED-IN OPERATOR — for the QSO/FT8 log
  memories:  JSON.parse(localStorage.getItem('ham_memories') || '[]'),

  saveMemories() {
    localStorage.setItem('ham_memories', JSON.stringify(this.memories));
  },
};
