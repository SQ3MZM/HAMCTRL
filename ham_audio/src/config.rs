//! Konfiguracja ham_audio — z env lub defaults

#[derive(Debug, Clone)]
pub struct Config {
    pub rx_device:      String,
    pub tx_device:      String,
    pub bitrate:        u32,
    /// Bitrate strumienia "LOW" — dla klientow ze slabym lączem, ktorzy nie
    /// nadazaja za strumieniem glownym (patrz handle_ws w main.rs, przelaczanie
    /// po zdarzeniach Lagged). Niski, ale wciaz zrozumiały dla mowy/CW na
    /// waskim pasmie SSB (nie muzyka — tu nie trzeba wysokiej wiernosci).
    pub bitrate_lo:     u32,
    pub rx_volume:      f32,
    pub ctrl_port:      u16,
    pub ws_port:        u16,
    pub wss_port:       u16,
    pub ssl_cert:       String,
    pub ssl_key:        String,
    pub frame_ms:       u32,
    pub latency_ms:     f32,
    /// Port TCP do wysylania wynikow FT8 decode do Pythona
    pub decode_port:    u16,
    /// Tryb dekodowania: "FT8" lub "FT4"
    pub ft8_decode_mode: String,
    /// Czy wlaczone dekodowanie FT8/FT4
    pub ft8_rx_enabled: bool,
    /// AP (a priori) decode hints from the QSO engine (Python) - operator's
    /// own callsign, current QSO partner (if any), and the Call-1st queue.
    /// Empty ap_own_call means AP is effectively disabled (no valid
    /// own-call-as-TO hypothesis to try). Set via ctrl port (SetApHints),
    /// same pattern as ft8_decode_mode/ft8_rx_enabled - read fresh each
    /// decode cycle in rx_loop.rs.
    pub ap_own_call:     String,
    pub ap_partner_call: Option<String>,
    pub ap_queue:        Vec<String>,
}

impl Config {
    pub fn from_env() -> Self {
        Self {
            rx_device:       env("HAM_RX_DEVICE",   ""),
            tx_device:       env("HAM_TX_DEVICE",   ""),
            bitrate:         env_u32("HAM_BITRATE",  24000),
            bitrate_lo:      env_u32("HAM_BITRATE_LO", 9000),
            rx_volume:       env_f32("HAM_VOLUME",   1.0),
            ctrl_port:       env_u16("HAM_CTRL_PORT",  9400),
            ws_port:         env_u16("HAM_WS_PORT",    9401),
            wss_port:        env_u16("HAM_WSS_PORT",   9443),
            // Sciezki certu SSL: PUSTE domyslne (bez zaszytej sciezki do
            // konkretnego komputera!). Python (audio_rust_bridge.py) przekazuje
            // prawdziwa sciezke przez HAM_SSL_CERT/HAM_SSL_KEY — zna ja, bo sam
            // laduje cert z tunnel_config.json. Gdy zmiennej brak, sciezka jest
            // pusta -> plik nie istnieje -> Rust czysto wylacza WSS (zamiast
            // szukac martwej sciezki cudzego kompa). Dzieki temu exe dziala na
            // KAZDEJ maszynie, nie tylko tej gdzie byl kompilowany.
            ssl_cert:        env("HAM_SSL_CERT", ""),
            ssl_key:         env("HAM_SSL_KEY",  ""),
            frame_ms:        env_u32("HAM_FRAME_MS",  20),
            latency_ms:      0.0,
            decode_port:     env_u16("HAM_DECODE_PORT", 9444),
            ft8_decode_mode: env("HAM_FT8_MODE", "FT8"),
            ft8_rx_enabled:  false, // wlaczane przez Python przez ctrl port
            ap_own_call:     String::new(),
            ap_partner_call: None,
            ap_queue:        Vec::new(),
        }
    }
    pub fn wss_port(&self) -> u16     { self.wss_port }
    pub fn ssl_cert(&self) -> &str    { &self.ssl_cert }
    pub fn ssl_key(&self)  -> &str    { &self.ssl_key }
    pub fn decode_port(&self) -> u16  { self.decode_port }
}

fn env(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}
fn env_u16(key: &str, default: u16) -> u16 {
    std::env::var(key).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
}
fn env_u32(key: &str, default: u32) -> u32 {
    std::env::var(key).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
}
fn env_f32(key: &str, default: f32) -> f32 {
    std::env::var(key).ok().and_then(|v| v.parse().ok()).unwrap_or(default)
}
