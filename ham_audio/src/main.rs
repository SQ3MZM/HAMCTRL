//! ham_audio v0.2 — Audio server + FT8/FT4 decoder
//! WSS bezposrednio z przegladarki + FT8 decode przez TCP do Pythona

use std::sync::Arc;
use std::path::Path;
use tokio::sync::{broadcast, RwLock};
use tokio::net::{TcpListener, TcpStream};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::{accept_async, tungstenite::Message};
use tokio_rustls::rustls;
use serde::{Deserialize, Serialize};
use bytes::Bytes;
use tracing::{info, warn};

mod audio;
mod config;
mod decode;
mod mixer;

use config::Config;
use decode::buffer::Ft8Buffer;
use decode::rx_loop::run_decode_loop;
use mixer::Mixer;

#[derive(Clone)]
pub struct AudioFrame {
    pub opus: Bytes,
    pub seq:  u32,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "cmd")]
pub enum CtrlCmd {
    SetRxDevice  { name: String },
    SetTxDevice  { name: String },
    SetBitrate   { bps: u32 },
    SetVolume    { vol: f32 },
    SetFt8Mode   { mode: String },   // "FT8" or "FT4"
    SetFt8Rx     { enabled: bool },  // enable/disable FT8 decode
    GetStatus,
    ListDevices,
    Shutdown,
}

#[derive(Debug, Serialize)]
pub struct Status {
    pub rx_device:      String,
    pub tx_device:      String,
    pub rx_running:     bool,
    pub tx_running:     bool,
    pub subscribers:    usize,
    pub opus_bitrate:   u32,
    pub latency_ms:     f32,
    pub ft8_mode:       String,
    pub ft8_rx_enabled: bool,
    pub decode_port:    u16,
}

pub type SharedConfig = Arc<RwLock<Config>>;
pub type SharedMixer  = Arc<Mixer>;
pub type AudioTx      = broadcast::Sender<AudioFrame>;
pub type AudioRx      = broadcast::Receiver<AudioFrame>;

fn main() {
    // Manual runtime build instead of #[tokio::main] to raise
    // thread_keep_alive above tokio's default 10s. The FT8 decode window is
    // 15s (FT4: 7.5s) and each decode's spawn_blocking call (rx_loop.rs)
    // only occupies its blocking-pool thread for ~1s, so with the 10s
    // default that thread sits idle for ~14s between windows - LONGER than
    // the keep-alive - and gets reaped. The next window then has to spin up
    // a brand new OS thread from scratch before decoding can even start.
    // Live diagnostics (pass_elapsed_s) showed a suspiciously constant
    // ~1.08-1.09s cost on EVERY window regardless of candidate count (even
    // n=0, zero work) - a fixed per-window overhead independent of
    // workload points at exactly this kind of "always pay thread-creation
    // cost" pattern rather than anything in the decode math itself (which
    // offline profiling, run without spawn_blocking/tokio at all, showed
    // taking ~130-200ms of actual work). 60s keep-alive comfortably covers
    // both window periods so the SAME blocking thread gets reused window
    // after window instead of being torn down and recreated every time.
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .thread_keep_alive(std::time::Duration::from_secs(60))
        .build()
        .expect("tokio runtime build");
    rt.block_on(async_main());
}

async fn async_main() {
    tracing_subscriber::fmt()
        .with_env_filter("ham_audio=info")
        .init();

    // Znacznik wersji — potwierdza ktora wersja kodu jest w EXE, analogicznie
    // do "[build] webapp.py wersja BUILD-..." w webapp.py. Bez tego, po
    // rebuildzie, nie ma jak z logu stwierdzic czy zmiana w Rust faktycznie
    // dotarla do dzialajacego procesu (ham_audio.exe biegnie jako osobny
    // proces w OSOBNEJ konsoli od hamctrl.exe - latwo przetestowac stara
    // binarke myslac ze to nowa). Podbijac przy kazdej istotnej zmianie w
    // decode/*, zwlaszcza timingowej (patrz FT8_TIME_BUDGET_S w decode/mod.rs).
    println!("[build] ham_audio.exe wersja BUILD-2026-08-14-LDPC-ITER-30");

    let cfg: SharedConfig = Arc::new(RwLock::new(Config::from_env()));
    let (audio_tx, _)     = broadcast::channel::<AudioFrame>(256);
    let audio_tx          = Arc::new(audio_tx);
    let mixer             = Arc::new(Mixer::new());

    // FT8 audio ring buffer
    let ft8_buf = Ft8Buffer::new();

    // Kanał PCM 48kHz dla FT8 buffer (osobny od Opus encoder).
    // WIEKSZY bufor (256 = ~5s) — ten kanal NIE wplywa na latencje odsluchu
    // (dekoder pracuje na oknach 15s). Duzy zapas chroni dekoder przed dziurami
    // w audio gdy buffer dekodera chwilowo przystanie przy obciazeniu I/O.
    let (ft8_pcm_tx, ft8_pcm_rx) = std::sync::mpsc::sync_channel::<Vec<f32>>(256);

    // Audio RX loop — oryginalne audio bez zmian + nowy kanał PCM dla FT8
    {
        let cfg2     = cfg.clone();
        let atx2     = audio_tx.clone();
        let mix2     = mixer.clone();
        tokio::spawn(audio::run_audio_loop(cfg2, atx2, mix2, ft8_pcm_tx));
    }

    // Osobny task: czyta PCM z kanału i zasila FT8 ring buffer
    {
        let ft8_buf2 = ft8_buf.clone();
        tokio::task::spawn_blocking(move || {
            while let Ok(pcm) = ft8_pcm_rx.recv() {
                ft8_buf2.push_pcm_f32(&pcm);
            }
        });
    }

    // FT8 decode loop — connects to Python on decode_port
    {
        let ft8_buf3 = ft8_buf.clone();
        let cfg3     = cfg.clone();
        let dport    = { cfg.read().await.decode_port() };
        tokio::spawn(async move {
            run_decode_loop(ft8_buf3, cfg3, dport).await;
        });
    }

    let (ctrl_addr, ws_addr, wss_addr) = {
        let c = cfg.read().await;
        (
            format!("0.0.0.0:{}", c.ctrl_port),
            format!("0.0.0.0:{}", c.ws_port),
            format!("0.0.0.0:{}", c.wss_port()),
        )
    };

    let ctrl_listener = TcpListener::bind(&ctrl_addr).await.expect("bind ctrl");
    let ws_listener   = TcpListener::bind(&ws_addr).await.expect("bind ws");

    info!("Control TCP  : {}", ctrl_addr);
    info!("Audio WS     : {}", ws_addr);
    info!("FT8 decode   → Python port {}", { cfg.read().await.decode_port() });

    // WSS (SSL) listener
    let ssl_ctx = load_ssl_ctx(&cfg).await;
    if let Some(ref _tls) = ssl_ctx {
        info!("Audio WSS    : {} (SSL)", wss_addr);
    }

    if let Some(tls_acceptor) = ssl_ctx {
        let wss_listener = TcpListener::bind(&wss_addr).await.expect("bind wss");
        let atx3 = audio_tx.clone();
        let mix3 = mixer.clone();
        let cfg3 = cfg.clone();
        tokio::spawn(async move {
            loop {
                if let Ok((stream, addr)) = wss_listener.accept().await {
                    info!("WSS: {}", addr);
                    let arx  = atx3.subscribe();
                    let mix4 = mix3.clone();
                    let cfg4 = cfg3.clone();
                    let acc  = tls_acceptor.clone();
                    tokio::spawn(async move {
                        match tokio_rustls::TlsAcceptor::from(acc).accept(stream).await {
                            Ok(tls_stream) => handle_ws_stream(tls_stream, arx, mix4, cfg4).await,
                            Err(e) => warn!("TLS accept: {}", e),
                        }
                    });
                }
            }
        });
    }

    loop {
        tokio::select! {
            Ok((stream, addr)) = ctrl_listener.accept() => {
                info!("Ctrl: {}", addr);
                let cfg2  = cfg.clone();
                let atx2  = audio_tx.clone();
                let mix2  = mixer.clone();
                tokio::spawn(handle_ctrl(stream, cfg2, atx2, mix2));
            }
            Ok((stream, addr)) = ws_listener.accept() => {
                info!("WS: {}", addr);
                let arx  = audio_tx.subscribe();
                let mix2 = mixer.clone();
                let cfg2 = cfg.clone();
                tokio::spawn(handle_ws_plain(stream, arx, mix2, cfg2));
            }
        }
    }
}

async fn load_ssl_ctx(cfg: &SharedConfig) -> Option<Arc<rustls::ServerConfig>> {
    let (cert_path, key_path) = {
        let c = cfg.read().await;
        (c.ssl_cert().to_string(), c.ssl_key().to_string())
    };
    if !Path::new(&cert_path).exists() || !Path::new(&key_path).exists() {
        info!("SSL cert not found — WSS disabled");
        return None;
    }
    use rustls_pemfile::{certs, pkcs8_private_keys};
    let cert_data = std::fs::read(&cert_path).ok()?;
    let key_data  = std::fs::read(&key_path).ok()?;
    let certs: Vec<_> = certs(&mut cert_data.as_slice())
        .filter_map(|c| c.ok())
        .map(|c| rustls::pki_types::CertificateDer::from(c.to_vec()))
        .collect();
    let keys: Vec<_> = pkcs8_private_keys(&mut key_data.as_slice())
        .filter_map(|k| k.ok())
        .collect();
    if certs.is_empty() || keys.is_empty() { return None; }
    let key = rustls::pki_types::PrivateKeyDer::Pkcs8(
        rustls::pki_types::PrivatePkcs8KeyDer::from(keys[0].secret_pkcs8_der().to_vec())
    );
    let config = rustls::ServerConfig::builder()
        .with_no_client_auth()
        .with_single_cert(certs, key)
        .ok()?;
    info!("SSL loaded: {}", cert_path);
    Some(Arc::new(config))
}

async fn handle_ctrl(
    mut stream: TcpStream,
    cfg:   SharedConfig,
    atx:   Arc<AudioTx>,
    mixer: SharedMixer,
) {
    let mut buf = [0u8; 4096];
    loop {
        let n = match stream.read(&mut buf).await {
            Ok(0) | Err(_) => break,
            Ok(n) => n,
        };
        let line = match std::str::from_utf8(&buf[..n]) {
            Ok(s) => s.trim().to_string(),
            Err(_) => continue,
        };
        let cmd: CtrlCmd = match serde_json::from_str(&line) {
            Ok(c)  => c,
            Err(e) => {
                let _ = stream.write_all(format!("{{\"error\":\"{}\"}}\n", e).as_bytes()).await;
                continue;
            }
        };
        let resp = match cmd {
            CtrlCmd::GetStatus => {
                let c = cfg.read().await;
                serde_json::to_string(&Status {
                    rx_device:      c.rx_device.clone(),
                    tx_device:      c.tx_device.clone(),
                    rx_running:     true,
                    tx_running:     mixer.has_clients(),
                    subscribers:    atx.receiver_count(),
                    opus_bitrate:   c.bitrate,
                    latency_ms:     c.latency_ms,
                    ft8_mode:       c.ft8_decode_mode.clone(),
                    ft8_rx_enabled: c.ft8_rx_enabled,
                    decode_port:    c.decode_port(),
                }).unwrap()
            }
            CtrlCmd::SetRxDevice { name } => {
                // HOT-SWAP karty RX bez restartu procesu: zapisz nazwe i obudz
                // watek RX (bump generacji). Watek zamknie stary stream (czyste
                // WASAPI, bez fantomow) a run_audio_loop przeladuje z nowa karta.
                {
                    let mut c = cfg.write().await;
                    if c.rx_device != name {
                        c.rx_device = name;
                        drop(c);
                        crate::audio::bump_rx_device_gen();
                        info!("[audio] SetRxDevice: hot-swap karty RX (bez restartu)");
                    }
                }
                r#"{"ok":true}"#.into()
            }
            CtrlCmd::SetTxDevice { name } => { cfg.write().await.tx_device = name; r#"{"ok":true}"#.into() }
            CtrlCmd::SetBitrate  { bps  } => { cfg.write().await.bitrate   = bps;  r#"{"ok":true}"#.into() }
            CtrlCmd::SetVolume   { vol  } => { cfg.write().await.rx_volume = vol;  r#"{"ok":true}"#.into() }
            CtrlCmd::SetFt8Mode  { mode } => {
                info!("[ft8dec] Mode: {}", mode);
                cfg.write().await.ft8_decode_mode = mode;
                r#"{"ok":true}"#.into()
            }
            CtrlCmd::SetFt8Rx    { enabled } => {
                info!("[ft8dec] RX enabled: {}", enabled);
                cfg.write().await.ft8_rx_enabled = enabled;
                r#"{"ok":true}"#.into()
            }
            CtrlCmd::ListDevices => serde_json::to_string(&audio::list_devices()).unwrap(),
            CtrlCmd::Shutdown    => { std::process::exit(0); }
        };
        let _ = stream.write_all(format!("{}\n", resp).as_bytes()).await;
    }
}

async fn handle_ws_plain(stream: TcpStream, arx: AudioRx, mixer: SharedMixer, cfg: SharedConfig) {
    match accept_async(stream).await {
        Ok(ws) => handle_ws(ws, arx, mixer, cfg).await,
        Err(e) => warn!("WS plain handshake: {}", e),
    }
}

async fn handle_ws_stream<S>(stream: S, arx: AudioRx, mixer: SharedMixer, cfg: SharedConfig)
where S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static
{
    match accept_async(stream).await {
        Ok(ws) => handle_ws(ws, arx, mixer, cfg).await,
        Err(e) => warn!("WS TLS handshake: {}", e),
    }
}

async fn handle_ws<S>(
    ws:      tokio_tungstenite::WebSocketStream<S>,
    mut arx: AudioRx,
    mixer:   SharedMixer,
    _cfg:    SharedConfig,
)
where S: tokio::io::AsyncRead + tokio::io::AsyncWrite + Unpin + Send + 'static
{
    let (mut ws_tx, mut ws_rx) = ws.split();
    let (local_tx, mut local_rx) = tokio::sync::mpsc::channel::<Vec<u8>>(64);

    tokio::spawn(async move {
        while let Some(data) = local_rx.recv().await {
            if ws_tx.send(Message::Binary(data)).await.is_err() { break; }
        }
    });

    let client_id = mixer.add_client();
    loop {
        tokio::select! {
            frame = arx.recv() => {
                match frame {
                    Ok(f) => {
                        let mut pkt = Vec::with_capacity(5 + f.opus.len());
                        pkt.push(0xA1);
                        pkt.extend_from_slice(&f.seq.to_le_bytes());
                        pkt.extend_from_slice(&f.opus);
                        let _ = local_tx.send(pkt).await;
                    }
                    Err(broadcast::error::RecvError::Lagged(n)) => warn!("WS lagged {}", n),
                    Err(_) => break,
                }
            }
            msg = ws_rx.next() => {
                match msg {
                    Some(Ok(Message::Binary(data))) if !data.is_empty() => {
                        if data[0] == 0xA2 {
                            mixer.push_tx(client_id, Bytes::from(data[1..].to_vec()));
                        }
                    }
                    Some(Ok(Message::Close(_))) | None => break,
                    _ => {}
                }
            }
        }
    }
    mixer.remove_client(client_id);
}
