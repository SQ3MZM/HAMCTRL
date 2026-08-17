/**
 * i18n.js — minimal PL/EN string-switching layer.
 *
 * Static text: mark an element with data-i18n="key" (replaces textContent),
 * data-i18n-title="key" (replaces title attribute), or
 * data-i18n-placeholder="key" (replaces placeholder attribute). Call
 * I18n.apply() after injecting new HTML dynamically (e.g. after building a
 * list of rows) to translate the new nodes too.
 *
 * Dynamic text (toasts, JS-built strings): call I18n.t('key') directly.
 *
 * Language source of truth: localStorage 'ham_lang'. Falls back to the
 * value the installer wrote at first run (window.__HAM_INITIAL_LANG__, set
 * by a small inline script in index.html reading it from /api/config), then
 * to 'pl' if neither is present (matches this app's original language).
 */
window.I18N_STRINGS = {
  pl: {
    tab_radio: "RADIO",
    tab_settings: "USTAWIENIA",
    tab_profile: "👤 PROFIL",
    tab_log: "LOG QSO",
    tab_config: "KONFIGURACJA",
    tab_admin: "ADMIN",
    logout_btn: "⏻ WYLOGUJ",
    logout_title: "Wyloguj",

    // ── RADIO tab ──
    radio_mode_hdr: "TRYB",
    radio_band_hdr: "PASMO",
    radio_freq_history: "HISTORIA FREQ",
    radio_trx_functions: "TRX FUNKCJE",
    radio_meter_indicator: "WSKAŹNIK (bargraf pod S-metrem)",
    radio_filter_lbl: "FILTR (FIL1/2/3 — ustawienia z menu radia)",
    vfo_b_tooltip: "Klik: przełącz na VFO-B | Prawy klik: wpisz częstotliwość | Scroll: zmieniaj co 1kHz",
    ws_badge_title: "Opóźnienie połączenia sterującego (ping/pong) — NIE opóźnienie samego audio. Kliknij aby odświeżyć.",
    audio_badge_title: "Rzeczywiste opóźnienie odsłuchu RX: ile audio jest zbuforowane między odbiorem z serwera a odtworzeniem. Cel adaptacyjny 180-300ms (rośnie po niedoborach, opada gdy łącze spokojne), twardy limit 400ms.",
    vfoab_title: "Zamień VFO A i B (CI-V 07 B0)",
    vfoa2b_title: "Skopiuj A do B (CI-V 07 A0)",
    autotune_title: "Autotune — cykl dopasowania (CI-V 1C 01 02, generuje TX)",
    peak_hold_title: "Peak-hold: pokazuje najwyższą wartość w każdym paśmie",
    rotator_target_lbl: "CEL (stopnie lub lokator)",
    cw_custom_placeholder: "Wpisz tekst CW…",
    operators_hdr: "OPERATORZY",
    sent_title: "RST wysłane",
    rcvd_title: "RST odebrane",
    rotator_target_placeholder: "272 lub KO02",
    no_connections: "Brak połączeń",

    // RadioLock (TRX lock panel — buttons + dynamic status text)
    radio_free: "🔓 RADIO WOLNE",
    you_have_trx: "🎙 MASZ TRX",
    take_trx_btn: "▶ PRZEJMIJ TRX",
    release_trx_btn: "✕ ODDAJ TRX",
    request_trx_btn: "🙋 POPROŚ O TRX",
    request_trx_again_btn: "🙋 POPROŚ PONOWNIE",
    request_sending: "✋ Wysyłanie…",
    request_sent_short: "✋ Prośba wysłana…",
    force_release_btn: "⚡ WYMUŚ (admin)",
    toast_trx_granted: "✓ Masz teraz dostęp do TRX!",
    toast_request_sent: "✋ Prośba wysłana — operator zostanie powiadomiony",
    toast_request_error: "✗ Błąd wysyłania prośby",
    toast_release_error: "✗ Błąd zwalniania radia",
    toast_reject_error: "✗ Błąd odrzucania prośby",
    toast_request_rejected: "✗ Prośba o TRX odrzucona przez {by}",
    tx_mic_start: "🎤 Nadawanie TX — mikrofon",
    tx_mic_stop: "⏹ Zatrzymaj TX mikrofon",
    cw_edit_macro_title: "Edytuj makro",
    rotator_none: "✗ brak",
    rotator_moving: "↻ ruch",
  },
  en: {
    tab_radio: "RADIO",
    tab_settings: "SETTINGS",
    tab_profile: "👤 PROFILE",
    tab_log: "QSO LOG",
    tab_config: "CONFIGURATION",
    tab_admin: "ADMIN",
    logout_btn: "⏻ LOG OUT",
    logout_title: "Log out",

    // ── RADIO tab ──
    radio_mode_hdr: "MODE",
    radio_band_hdr: "BAND",
    radio_freq_history: "FREQ HISTORY",
    radio_trx_functions: "TRX FUNCTIONS",
    radio_meter_indicator: "METER (bargraph below S-meter)",
    radio_filter_lbl: "FILTER (FIL1/2/3 — radio menu settings)",
    vfo_b_tooltip: "Click: switch to VFO-B | Right-click: type frequency | Scroll: step 1kHz",
    ws_badge_title: "Control connection latency (ping/pong) — NOT audio latency itself. Click to refresh.",
    audio_badge_title: "Real RX listening latency: how much audio is buffered between receiving it from the server and playing it. Adaptive target 180-300ms (grows after underruns, decays when the link is quiet), hard limit 400ms.",
    vfoab_title: "Swap VFO A and B (CI-V 07 B0)",
    vfoa2b_title: "Copy A to B (CI-V 07 A0)",
    autotune_title: "Autotune — matching cycle (CI-V 1C 01 02, triggers TX)",
    peak_hold_title: "Peak-hold: shows the highest value in each band",
    rotator_target_lbl: "TARGET (degrees or locator)",
    cw_custom_placeholder: "Type CW text…",
    operators_hdr: "OPERATORS",
    sent_title: "RST sent",
    rcvd_title: "RST received",
    rotator_target_placeholder: "272 or KO02",
    no_connections: "No connections",

    // RadioLock (TRX lock panel — buttons + dynamic status text)
    radio_free: "🔓 RADIO FREE",
    you_have_trx: "🎙 YOU HAVE TRX",
    take_trx_btn: "▶ TAKE OVER TRX",
    release_trx_btn: "✕ RELEASE TRX",
    request_trx_btn: "🙋 REQUEST TRX",
    request_trx_again_btn: "🙋 REQUEST AGAIN",
    request_sending: "✋ Sending…",
    request_sent_short: "✋ Request sent…",
    force_release_btn: "⚡ FORCE (admin)",
    toast_trx_granted: "✓ You now have TRX access!",
    toast_request_sent: "✋ Request sent — the operator will be notified",
    toast_request_error: "✗ Error sending request",
    toast_release_error: "✗ Error releasing radio",
    toast_reject_error: "✗ Error rejecting request",
    toast_request_rejected: "✗ TRX request rejected by {by}",
    tx_mic_start: "🎤 Start TX — microphone",
    tx_mic_stop: "⏹ Stop TX microphone",
    cw_edit_macro_title: "Edit macro",
    rotator_none: "✗ none",
    rotator_moving: "↻ moving",
  },
};

window.I18n = (function () {
  const SUPPORTED = ["pl", "en"];
  let current = null;

  function detectInitial() {
    const saved = localStorage.getItem("ham_lang");
    if (saved && SUPPORTED.includes(saved)) return saved;
    if (window.__HAM_INITIAL_LANG__ && SUPPORTED.includes(window.__HAM_INITIAL_LANG__)) {
      return window.__HAM_INITIAL_LANG__;
    }
    return "pl";
  }

  function t(key) {
    const dict = window.I18N_STRINGS[current] || {};
    if (key in dict) return dict[key];
    // Fall back to Polish (the app's original/complete language) rather
    // than showing a raw key, then to the key itself as a last resort so a
    // missing translation is still visibly wrong instead of silently blank.
    if (key in window.I18N_STRINGS.pl) return window.I18N_STRINGS.pl[key];
    return key;
  }

  function apply(root) {
    root = root || document;
    root.querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    root.querySelectorAll("[data-i18n-title]").forEach((el) => {
      el.title = t(el.getAttribute("data-i18n-title"));
    });
    root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
      el.placeholder = t(el.getAttribute("data-i18n-placeholder"));
    });
    const btn = document.getElementById("lang-switch");
    if (btn) btn.textContent = current.toUpperCase();
    document.documentElement.lang = current;
  }

  function setLang(lang) {
    if (!SUPPORTED.includes(lang)) return;
    current = lang;
    localStorage.setItem("ham_lang", lang);
    apply();
  }

  function toggle() {
    setLang(current === "pl" ? "en" : "pl");
  }

  function init() {
    current = detectInitial();
    apply();
  }

  document.addEventListener("DOMContentLoaded", init);

  return { t, apply, setLang, toggle, current: () => current };
})();
