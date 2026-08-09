/**
 * state.js — współdzielony stan frontendu
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
  stationLocator: 'KO02',  // Maidenhead locator STACJI (gdzie stoi antena) —
                           // do przeliczania azymutu rotora
  operatorLocator: '',     // lokator ZALOGOWANEGO OPERATORA — do logu QSO/FT8
  memories:  JSON.parse(localStorage.getItem('ham_memories') || '[]'),

  saveMemories() {
    localStorage.setItem('ham_memories', JSON.stringify(this.memories));
  },
};
