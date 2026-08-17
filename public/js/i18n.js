/**
 * i18n.js — minimal PL/EN string-switching layer.
 *
 * Static text: mark an element with data-i18n="key" (replaces textContent),
 * data-i18n-title="key" (replaces title attribute), or
 * data-i18n-placeholder="key" (replaces placeholder attribute). For strings
 * with inline HTML formatting (e.g. <b>) use data-i18n-html="key" instead of
 * data-i18n — sets innerHTML. Only for trusted, hardcoded dictionary strings,
 * never for anything derived from user input. Call I18n.apply() after
 * injecting new HTML dynamically (e.g. after building a list of rows) to
 * translate the new nodes too.
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

    // ── USTAWIENIA tab ──
    settings_save_btn: "ZAPISZ",
    settings_loading: "Ładowanie…",
    status_checking: "sprawdzanie…",
    status_error_generic: "błąd",
    status_no_response: "brak odpowiedzi",
    settings_saved_short: "✓ zapisano",
    settings_save_error_plain: "błąd zapisu",

    // COM BRIDGE
    settings_combridge_hdr: "🔌 ZDALNY DOSTĘP CAT — CW Skimmer, HRD, Logger32",
    settings_combridge_desc: "Pobierz aplikację <b style=\"color:var(--fg);\">HAM RADIO CTRL</b> i używaj zdalnego radia w programach CAT tak jakby było podłączone lokalnie. Masz <b style=\"color:var(--green);\">2 niezależne porty</b> — np. CW Skimmer i HRD jednocześnie.",
    settings_combridge_warn: "<b style=\"color:var(--amber);\">Uwaga:</b> zmiana częstotliwości/trybu przez CAT wymaga przejęcia radia (blokady). Bez blokady możesz tylko <b>odczytywać</b> stan radia (podgląd freq w programie). Numery COM na Twoim PC mogą się różnić — sprawdź w oknie aplikacji.",
    settings_combridge_download: "⬇️ POBIERZ MOST COM (Windows)",
    settings_combridge_winreq: "Windows 10/11 · ~25 MB · zawiera com0com",

    // CLOUDLOG / WAVELOG
    settings_cloudlog_status_title: "Kliknij aby przetestować połączenie",
    settings_cloudlog_status_default: "nie sprawdzono",
    settings_cl_url_lbl: "Adres serwera",
    settings_cl_apikey_qso_lbl: "API Key — logowanie QSO",
    settings_cl_apikey_qso_ph: "klucz API dla QSO",
    settings_cl_stationid_ph: "ID stacji",
    settings_cl_apikey_radio_lbl: "API Key — częstotliwość live",
    settings_cl_apikey_radio_ph: "klucz API dla live freq/mode",
    settings_cl_live_checkbox: "Wysyłaj freq+mode automatycznie (co 5s)",
    settings_test_connection_btn: "TEST POŁĄCZENIA",
    toast_cloudlog_saved: "✓ CloudLog: ustawienia zapisane",
    toast_cloudlog_save_error: "✗ Błąd zapisu ustawień",
    cloudlog_fill_url_key: "Uzupełnij adres i API Key QSO",
    cloudlog_connected_default: "połączono",
    toast_cloudlog_qso_sent: "✓ QSO wysłane do CloudLog",
    toast_cloudlog_no_connection: "✗ CloudLog: brak połączenia",

    // WYSZUKIWANIE ZNAKÓW (QRZ.com / HamQTH)
    settings_lookup_hdr: "🔍 WYSZUKIWANIE ZNAKÓW — QRZ.com / HamQTH",
    settings_lookup_desc: "Przy logowaniu QSO ikonka 🔍 przy polu ZNAK spróbuje pobrać imię/QTH/kraj drugiej stacji — najpierw z QRZ.com (jeśli skonfigurowane), potem HamQTH. Wymaga <b style=\"color:var(--fg);\">Twojego własnego</b> konta w tych serwisach (QRZ: płatna subskrypcja \"XML Data\" · HamQTH: darmowe konto).",
    settings_qrz_login_lbl: "QRZ.com — login",
    settings_qrz_pass_lbl: "QRZ.com — hasło",
    settings_hamqth_login_lbl: "HamQTH — login",
    settings_hamqth_pass_lbl: "HamQTH — hasło",
    ph_qrz_login: "login QRZ.com",
    ph_qrz_pass: "hasło QRZ.com",
    ph_hamqth_login: "login HamQTH",
    ph_hamqth_pass: "hasło HamQTH",
    cb_fill_login_pass: "uzupełnij login/hasło",
    cb_connected: "✓ połączono",
    cb_no_response: "✗ brak odpowiedzi",
    toast_lookup_saved: "✓ Ustawienia lookupu zapisane",

    // WIRTUALNE RADIO (Hamlib NET rigctl)
    settings_virtual_radio_hdr: "WIRTUALNE RADIO — ZEWNĘTRZNE PROGRAMY",
    settings_virtual_radio_desc: "Emulacja <b>Hamlib NET rigctl</b> (rigctld) — 3 niezależne serwery TCP.<br>Każdy port to osobne \"wirtualne radio\" do którego podłączasz zewnętrzny program.<br><span style=\"color:var(--green);\">WSJT-X / Log4OM / N1MM / CW Skimmer / DXKeeper / HRD:</span> Rig → <b>Hamlib NET rigctl</b> → host: <b>&lt;IP serwera&gt;</b> → port: <b>4532</b>",
    settings_virtual_radio_save_btn: "ZAPISZ KONFIGURACJĘ",
    settings_virtual_radio_refresh_btn: "↻ ODŚWIEŻ STATUS",
    settings_virtual_radio_howto_hdr: "Jak podłączyć program zewnętrzny:",
    hamlib_load_error: "Błąd ładowania statusu",
    hamlib_no_ports: "Brak skonfigurowanych portów.",
    hamlib_active: "aktywny",
    hamlib_client_singular: "klient",
    hamlib_client_plural: "klientów",
    hamlib_disabled: "— wyłączony —",
    hamlib_toggle_port_title: "Włącz/wyłącz ten port",
    hamlib_saving: "zapisywanie…",

    // ── PROFIL tab ──
    profile_myprofile_hdr: "👤 MÓJ PROFIL",
    profile_name_lbl: "IMIĘ",
    profile_name_ph: "Jan",
    profile_callsign_lbl: "ZNAK WYWOŁAWCZY",
    profile_locator_lbl: "LOKATOR QTH (np. JO82)",
    profile_save_btn: "💾 ZAPISZ PROFIL",
    profile_logged_in_as: "Zalogowany jako: {user} ({role})",

    profile_changepwd_hdr: "🔑 ZMIANA HASŁA",
    profile_old_pwd_lbl: "STARE HASŁO",
    profile_new_pwd_lbl: "NOWE HASŁO (min. 8 znaków)",
    profile_new_pwd2_lbl: "POWTÓRZ NOWE HASŁO",
    profile_changepwd_btn: "🔑 ZMIEŃ HASŁO",
    profile_fill_fields: "Uzupełnij pola",
    profile_pwd_mismatch: "Hasła nie pasują",
    profile_pwd_min_len: "Min. 8 znaków",
    profile_pwd_changed_short: "✓ Hasło zmienione",
    profile_pwd_changed_toast: "✓ Hasło zmienione (inne urządzenia wylogowane)",
    profile_saved_short: "✓ Profil zapisany",
    profile_error_fallback: "Błąd",

    profile_audio_hdr: "🔊 AUDIO — MOJE URZĄDZENIA (przeglądarka)",
    profile_audio_desc: "Wybierz gdzie słyszysz odbiór (RX) i skąd nadajesz głos (TX mikrofon). Ustawienia zapisywane lokalnie w Twojej przeglądarce.",
    profile_audio_out_lbl: "GŁOŚNIK RX (odbiór radia → Twoje słuchawki)",
    profile_loading_option: "— ładowanie… —",
    profile_audio_test_out_title: "Odtwórz ton testowy",
    profile_audio_in_lbl: "MIKROFON TX (Twój głos → radio)",
    profile_audio_test_in_title: "Zmierz poziom mikrofonu (5s)",
    profile_mic_level_lbl: "POZIOM MIKROFONU:",
    profile_audio_footnote: "💡 Unikaj urządzeń \"CABLE\"/\"Virtual\" — to wirtualne kable dla programów (WSJT-X), nie Twój sprzęt.",
    profile_no_devices: "brak urządzeń",
    profile_device_fallback: "Urządzenie",
    profile_mic_fallback: "Mikrofon",
    profile_toast_speaker_saved: "✓ Głośnik RX zapisany",
    profile_toast_mic_saved: "✓ Mikrofon TX zapisany (aktywny przy następnym TX)",
    profile_toast_test_tone: "🔉 Ton testowy 1 kHz",
    profile_toast_speaker_test_err: "✕ Błąd testu głośnika: ",
    profile_mic_hint_speaking: "Mów do mikrofonu… ({s}s) — szczyt: {peak}%",
    profile_mic_hint_low: "⚠ Szczyt tylko {peak}% — mikrofon za cichy lub zły wybór urządzenia",
    profile_mic_hint_high: "⚠ Szczyt {peak}% — za głośno, może przesterować",
    profile_mic_hint_ok: "✓ Szczyt {peak}% — poziom OK",
    profile_mic_hint_error: "✕ Błąd: ",
    profile_toast_mic_open_err: "✕ Nie można otworzyć mikrofonu: ",

    profile_theme_hdr: "🎨 WYGLĄD (motyw kolorów)",
    profile_theme_desc: "Motyw zmienia kolory akcentów w interfejsie. Zapisywany lokalnie w przeglądarce.",
    profile_theme_lbl: "MOTYW",
    profile_theme_opt_default: "Zielony + bursztyn (klasyczny IC-7300)",
    profile_theme_opt_blue: "Niebieski (nowoczesny)",
    profile_theme_opt_mono: "Monochromatyczny (dyskretny)",
    profile_theme_opt_amber: "Bursztyn klasyczny (retro CRT)",

    profile_eq_hdr: "🎤 TX MIKROFON — KOREKCJA AUDIO (EQ)",
    profile_eq_desc: "Korekcja audio dopasowuje sygnał mikrofonu do pasma SSB (300-2700 Hz). Wybierz preset pasujący do Twojego głosu i sprawdź odsłuchem jak będzie brzmieć w eterze. Ustawienia zapisują się lokalnie w przeglądarce.",
    profile_eq_preset_lbl: "PRESET EQ",
    profile_eq_opt_default: "Standardowy (zbalansowany)",
    profile_eq_opt_dark: "Ciemny głos (męski/gruby) — więcej klarowności",
    profile_eq_opt_bright: "Jasny głos (kobiecy/dziecięcy) — mniej wysokich",
    profile_eq_opt_dx: "DX/Contest — maksymalny punch",
    profile_eq_opt_ragchew: "Ragchew — naturalny, mniej agresywny",
    profile_eq_opt_flat: "Flat (bez EQ, tylko limiter)",
    profile_eq_opt_custom: "Custom (ręcznie)",
    profile_eq_custom_hdr: "CUSTOM EQ — ręczna regulacja",
    profile_eq_bass_lbl: "BAS (200 Hz):",
    profile_eq_mud_lbl: "MUD (700 Hz):",
    profile_eq_clarity_lbl: "KLAROWNOŚĆ (1800 Hz):",
    profile_eq_punch_lbl: "PUNCH (2400 Hz):",
    profile_eq_air_lbl: "POWIETRZE (2700 Hz):",
    profile_eq_monitor_hdr: "🎧 ODSŁUCH (Monitor) — usłysz jak EQ zmienia Twój głos",
    profile_eq_monitor_start_btn: "▶ START ODSŁUCH",
    profile_eq_monitor_stop_btn: "⏹ STOP ODSŁUCH",
    profile_eq_monitor_desc: "Odsłuch przepuszcza mikrofon przez EQ i odtwarza w słuchawkach lokalnie (nie nadaje przez radio). Używaj słuchawek żeby uniknąć sprzężenia!",
    profile_eq_monitor_vol_lbl: "Głośność:",
    profile_toast_mic_unavailable: "Mikrofon niedostępny w tej przeglądarce",
    profile_toast_mic_no_access: "Brak dostępu do mikrofonu: ",
    profile_toast_audioctx_unavailable: "AudioContext niedostępny - kliknij cokolwiek w UI żeby aktywować audio, potem spróbuj ponownie",
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

    // ── SETTINGS tab ──
    settings_save_btn: "SAVE",
    settings_loading: "Loading…",
    status_checking: "checking…",
    status_error_generic: "error",
    status_no_response: "no response",
    settings_saved_short: "✓ saved",
    settings_save_error_plain: "save error",

    // COM BRIDGE
    settings_combridge_hdr: "🔌 REMOTE CAT ACCESS — CW Skimmer, HRD, Logger32",
    settings_combridge_desc: "Download the <b style=\"color:var(--fg);\">HAM RADIO CTRL</b> app and use the remote radio in CAT programs as if it were connected locally. You get <b style=\"color:var(--green);\">2 independent ports</b> — e.g. CW Skimmer and HRD at the same time.",
    settings_combridge_warn: "<b style=\"color:var(--amber);\">Note:</b> changing frequency/mode via CAT requires taking over the radio (lock). Without the lock you can only <b>read</b> the radio state (freq preview in the program). COM numbers on your PC may differ — check the app window.",
    settings_combridge_download: "⬇️ DOWNLOAD COM BRIDGE (Windows)",
    settings_combridge_winreq: "Windows 10/11 · ~25 MB · includes com0com",

    // CLOUDLOG / WAVELOG
    settings_cloudlog_status_title: "Click to test the connection",
    settings_cloudlog_status_default: "not checked",
    settings_cl_url_lbl: "Server address",
    settings_cl_apikey_qso_lbl: "API Key — QSO logging",
    settings_cl_apikey_qso_ph: "API key for QSO",
    settings_cl_stationid_ph: "Station ID",
    settings_cl_apikey_radio_lbl: "API Key — live frequency",
    settings_cl_apikey_radio_ph: "API key for live freq/mode",
    settings_cl_live_checkbox: "Send freq+mode automatically (every 5s)",
    settings_test_connection_btn: "TEST CONNECTION",
    toast_cloudlog_saved: "✓ CloudLog: settings saved",
    toast_cloudlog_save_error: "✗ Error saving settings",
    cloudlog_fill_url_key: "Fill in the address and QSO API Key",
    cloudlog_connected_default: "connected",
    toast_cloudlog_qso_sent: "✓ QSO sent to CloudLog",
    toast_cloudlog_no_connection: "✗ CloudLog: no connection",

    // CALLSIGN LOOKUP (QRZ.com / HamQTH)
    settings_lookup_hdr: "🔍 CALLSIGN LOOKUP — QRZ.com / HamQTH",
    settings_lookup_desc: "When logging a QSO, the 🔍 icon next to the CALL field will try to fetch the other station's name/QTH/country — first from QRZ.com (if configured), then HamQTH. Requires <b style=\"color:var(--fg);\">your own</b> account on these services (QRZ: paid \"XML Data\" subscription · HamQTH: free account).",
    settings_qrz_login_lbl: "QRZ.com — login",
    settings_qrz_pass_lbl: "QRZ.com — password",
    settings_hamqth_login_lbl: "HamQTH — login",
    settings_hamqth_pass_lbl: "HamQTH — password",
    ph_qrz_login: "QRZ.com login",
    ph_qrz_pass: "QRZ.com password",
    ph_hamqth_login: "HamQTH login",
    ph_hamqth_pass: "HamQTH password",
    cb_fill_login_pass: "fill in login/password",
    cb_connected: "✓ connected",
    cb_no_response: "✗ no response",
    toast_lookup_saved: "✓ Lookup settings saved",

    // VIRTUAL RADIO (Hamlib NET rigctl)
    settings_virtual_radio_hdr: "VIRTUAL RADIO — EXTERNAL PROGRAMS",
    settings_virtual_radio_desc: "Emulates <b>Hamlib NET rigctl</b> (rigctld) — 3 independent TCP servers.<br>Each port is a separate \"virtual radio\" you connect an external program to.<br><span style=\"color:var(--green);\">WSJT-X / Log4OM / N1MM / CW Skimmer / DXKeeper / HRD:</span> Rig → <b>Hamlib NET rigctl</b> → host: <b>&lt;server IP&gt;</b> → port: <b>4532</b>",
    settings_virtual_radio_save_btn: "SAVE CONFIGURATION",
    settings_virtual_radio_refresh_btn: "↻ REFRESH STATUS",
    settings_virtual_radio_howto_hdr: "How to connect an external program:",
    hamlib_load_error: "Error loading status",
    hamlib_no_ports: "No configured ports.",
    hamlib_active: "active",
    hamlib_client_singular: "client",
    hamlib_client_plural: "clients",
    hamlib_disabled: "— disabled —",
    hamlib_toggle_port_title: "Enable/disable this port",
    hamlib_saving: "saving…",

    // ── PROFILE tab ──
    profile_myprofile_hdr: "👤 MY PROFILE",
    profile_name_lbl: "NAME",
    profile_name_ph: "John",
    profile_callsign_lbl: "CALLSIGN",
    profile_locator_lbl: "QTH LOCATOR (e.g. JO82)",
    profile_save_btn: "💾 SAVE PROFILE",
    profile_logged_in_as: "Logged in as: {user} ({role})",

    profile_changepwd_hdr: "🔑 CHANGE PASSWORD",
    profile_old_pwd_lbl: "OLD PASSWORD",
    profile_new_pwd_lbl: "NEW PASSWORD (min. 8 characters)",
    profile_new_pwd2_lbl: "REPEAT NEW PASSWORD",
    profile_changepwd_btn: "🔑 CHANGE PASSWORD",
    profile_fill_fields: "Fill in the fields",
    profile_pwd_mismatch: "Passwords don't match",
    profile_pwd_min_len: "Min. 8 characters",
    profile_pwd_changed_short: "✓ Password changed",
    profile_pwd_changed_toast: "✓ Password changed (other devices logged out)",
    profile_saved_short: "✓ Profile saved",
    profile_error_fallback: "Error",

    profile_audio_hdr: "🔊 AUDIO — MY DEVICES (browser)",
    profile_audio_desc: "Choose where you hear the radio (RX) and where your voice is sent from (TX microphone). Settings are saved locally in your browser.",
    profile_audio_out_lbl: "RX SPEAKER (radio reception → your headphones)",
    profile_loading_option: "— loading… —",
    profile_audio_test_out_title: "Play a test tone",
    profile_audio_in_lbl: "TX MICROPHONE (your voice → radio)",
    profile_audio_test_in_title: "Measure microphone level (5s)",
    profile_mic_level_lbl: "MICROPHONE LEVEL:",
    profile_audio_footnote: "💡 Avoid \"CABLE\"/\"Virtual\" devices — those are virtual cables for programs (WSJT-X), not your actual hardware.",
    profile_no_devices: "no devices",
    profile_device_fallback: "Device",
    profile_mic_fallback: "Microphone",
    profile_toast_speaker_saved: "✓ RX speaker saved",
    profile_toast_mic_saved: "✓ TX microphone saved (active on next TX)",
    profile_toast_test_tone: "🔉 1 kHz test tone",
    profile_toast_speaker_test_err: "✕ Speaker test error: ",
    profile_mic_hint_speaking: "Speak into the microphone… ({s}s) — peak: {peak}%",
    profile_mic_hint_low: "⚠ Peak only {peak}% — microphone too quiet or wrong device selected",
    profile_mic_hint_high: "⚠ Peak {peak}% — too loud, may clip",
    profile_mic_hint_ok: "✓ Peak {peak}% — level OK",
    profile_mic_hint_error: "✕ Error: ",
    profile_toast_mic_open_err: "✕ Can't open the microphone: ",

    profile_theme_hdr: "🎨 APPEARANCE (color theme)",
    profile_theme_desc: "The theme changes the accent colors in the interface. Saved locally in the browser.",
    profile_theme_lbl: "THEME",
    profile_theme_opt_default: "Green + amber (classic IC-7300)",
    profile_theme_opt_blue: "Blue (modern)",
    profile_theme_opt_mono: "Monochrome (discreet)",
    profile_theme_opt_amber: "Classic amber (retro CRT)",

    profile_eq_hdr: "🎤 TX MICROPHONE — AUDIO EQ",
    profile_eq_desc: "Audio EQ shapes the microphone signal to the SSB passband (300-2700 Hz). Pick a preset matching your voice and check the monitor to hear how it'll sound on the air. Settings are saved locally in the browser.",
    profile_eq_preset_lbl: "EQ PRESET",
    profile_eq_opt_default: "Standard (balanced)",
    profile_eq_opt_dark: "Dark voice (male/deep) — more clarity",
    profile_eq_opt_bright: "Bright voice (female/child) — less treble",
    profile_eq_opt_dx: "DX/Contest — maximum punch",
    profile_eq_opt_ragchew: "Ragchew — natural, less aggressive",
    profile_eq_opt_flat: "Flat (no EQ, limiter only)",
    profile_eq_opt_custom: "Custom (manual)",
    profile_eq_custom_hdr: "CUSTOM EQ — manual adjustment",
    profile_eq_bass_lbl: "BASS (200 Hz):",
    profile_eq_mud_lbl: "MUD (700 Hz):",
    profile_eq_clarity_lbl: "CLARITY (1800 Hz):",
    profile_eq_punch_lbl: "PUNCH (2400 Hz):",
    profile_eq_air_lbl: "AIR (2700 Hz):",
    profile_eq_monitor_hdr: "🎧 MONITOR — hear how EQ changes your voice",
    profile_eq_monitor_start_btn: "▶ START MONITOR",
    profile_eq_monitor_stop_btn: "⏹ STOP MONITOR",
    profile_eq_monitor_desc: "The monitor runs your microphone through the EQ and plays it back locally in your headphones (not transmitted over the radio). Use headphones to avoid feedback!",
    profile_eq_monitor_vol_lbl: "Volume:",
    profile_toast_mic_unavailable: "Microphone unavailable in this browser",
    profile_toast_mic_no_access: "No microphone access: ",
    profile_toast_audioctx_unavailable: "AudioContext unavailable - click anywhere in the UI to activate audio, then try again",
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
    root.querySelectorAll("[data-i18n-html]").forEach((el) => {
      el.innerHTML = t(el.getAttribute("data-i18n-html"));
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
