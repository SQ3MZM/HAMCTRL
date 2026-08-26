/// FT8/FT4 RX decode loop.
/// Wakes at period boundaries, decodes audio, sends JSON results via TCP to Python.

use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::io::AsyncWriteExt;
use tokio::net::TcpStream;
use tokio::sync::RwLock;
use tracing::{info, warn};

use super::ap::ApHints;
use super::buffer::Ft8Buffer;
use super::{decode_ft8, decode_ft4, DecodeResult, PassTiming};
use crate::config::Config;

pub type SharedConfig = Arc<RwLock<Config>>;

pub async fn run_decode_loop(
    buffer:       Ft8Buffer,
    cfg:          SharedConfig,
    decode_port:  u16,
) {
    // Connect to Python decode results receiver with retry
    let addr = format!("127.0.0.1:{}", decode_port);
    println!("[ft8dec] Decode loop start, port={}", decode_port);
    loop {
        println!("[ft8dec] Laczenie z {}", addr);
        match TcpStream::connect(&addr).await {
            Ok(stream) => {
                println!("[ft8dec] Connected to Python decoder receiver at {}", addr);
                info!("[ft8dec] Connected to Python decoder receiver at {}", addr);
                run_loop(buffer.clone(), cfg.clone(), stream).await;
                warn!("[ft8dec] Python receiver disconnected, retrying in 3s...");
                println!("[ft8dec] Rozlaczono, retry za 3s...");
            }
            Err(e) => {
                println!("[ft8dec] Blad polaczenia: {}", e);
            }
        }
        tokio::time::sleep(tokio::time::Duration::from_secs(3)).await;
    }
}

async fn run_loop(
    buffer: Ft8Buffer,
    cfg:    SharedConfig,
    mut stream: TcpStream,
) {
    // Sent once per connection, into the SAME shared Python log the
    // operator pastes (ham_audio.exe's own println!s land in a SEPARATE
    // console - see the [build] marker's comment in main.rs for why that's
    // been a recurring blind spot). rayon::current_num_threads() also
    // lazily initializes rayon's global pool on first call, which is fine
    // here (decode_and_subtract's par_iter uses that same global pool).
    // Weak/unexpected parallelism (e.g. a hybrid-CPU machine defaulting to
    // far fewer usable threads than logical cores) is one of the remaining
    // unconfirmed explanations for why live decode time hasn't responded
    // to any of several targeted fixes - this makes it checkable directly
    // instead of inferred.
    let startup = serde_json::json!({
        "type": "startup_stats",
        "rayon_threads": rayon::current_num_threads(),
        "cpus": std::thread::available_parallelism().map(|n| n.get()).unwrap_or(0),
    });
    let _ = stream.write_all(format!("{}\n", startup).as_bytes()).await;

    // Align to the FT8/FT4 UTC grid. We wake shortly AFTER each window
    // boundary, then decode the window that just completed using an aligned
    // snapshot. Previously the loop woke 0.6s before the boundary and grabbed
    // "the last 16s", which was misaligned from the transmission and lost most
    // stations (measured: 4 decodes vs 18 on a busy band).
    let lead_s = 0.5f64;   // let a signal starting slightly early through

    loop {
        let (decode_mode, rx_enabled, ap_hints) = {
            let c = cfg.read().await;
            let hints = ApHints {
                own_call:     c.ap_own_call.clone(),
                partner_call: c.ap_partner_call.clone(),
                queue:        c.ap_queue.clone(),
            };
            (c.ft8_decode_mode.clone(), c.ft8_rx_enabled, hints)
        };
        let is_ft4 = decode_mode == "FT4";
        let (window_s, min_s) = if is_ft4 {
            (7.5f64, 4.5f64)
        } else {
            (15.0f64, 12.5f64)
        };

        // Wake shortly after the window boundary so the just-completed window
        // is fully in the buffer, then decode it from an aligned snapshot.
        let now_epoch = current_epoch();
        let pos = now_epoch % window_s;               // 0..window_s
        let settle = 0.3f64;                          // wait past boundary a touch
        let wait = (window_s - pos) + settle;
        // TRUE UTC start of the window we're about to decode, captured NOW
        // (before sleeping) - NOT recomputed after waking. Bug found live
        // 2026-08-26 (operator reported: "I can't tell who's replying to
        // whom, the displayed decode time is the same period as when
        // someone transmits to us"): the timeStr sent to Python/the UI used
        // to be computed via utc_time_str() AFTER this sleep, i.e. at
        // window_start + window_s + settle - the START of the FOLLOWING
        // window, not the window whose audio was actually just decoded.
        // Every RX decode was therefore labeled one full window (15s FT8 /
        // 7.5s FT4) too late, which made it collide in the UI with OUR OWN
        // next transmission instead of showing when the partner actually
        // sent it.
        let window_start_epoch = now_epoch - pos;
        tokio::time::sleep(tokio::time::Duration::from_secs_f64(wait.max(0.0))).await;

        if !rx_enabled {
            // RX off — heartbeat to keep the TCP link alive.
            let hb = b"{\"type\":\"heartbeat\"}\n";
            if stream.write_all(hb).await.is_err() { return; }
            continue;
        }

        let available = buffer.available_seconds() as f64;
        if available < min_s {
            println!("[ft8dec] skip: only {:.1}s buffered (need {:.1}s)", available, min_s);
            continue;   // not enough audio yet
        }

        let audio = buffer.snapshot_aligned(window_s, lead_s);
        match &audio {
            None => println!("[ft8dec] snapshot_aligned returned None (available={:.1}s)", available),
            Some(s) => println!("[ft8dec] decoding window: {} samples ({:.1}s)", s.len(), s.len() as f64 / 12000.0),
        }
        if let Some(samples) = audio {
            let t0 = std::time::Instant::now();
            let mode_str = decode_mode.clone();

            // Stream each pass's results out to Python AS SOON AS that pass's
            // decode+dedup is done, instead of waiting for decode_ft8/ft4 to
            // fully return (which also includes the expensive, SIC-subtract
            // cleanup + any later passes). Measured offline: pass 0 alone
            // (spectrogram+candidate-search+parallel-LDPC) takes ~150-200ms
            // on a busy window, vs ~500ms-1s+ for the full pipeline including
            // subtraction - and pass 0 already accounts for the large
            // majority of decodes. Subtraction only helps LATER passes find
            // a handful of extra masked signals, so there's no reason the
            // operator should have to wait for it before seeing/clicking a
            // decode. See decode_and_subtract's doc comment in mod.rs.
            //
            // blocking_send from inside spawn_blocking (sync context) into a
            // channel this async task drains concurrently - recv() yields
            // each batch as soon as it's sent, it does NOT wait for the
            // sender to be dropped/closed.
            let (tx_batch, mut rx_batch) = tokio::sync::mpsc::channel::<(f64, PassTiming, Vec<DecodeResult>)>(8);

            let decode_handle = tokio::task::spawn_blocking(move || {
                let mut on_pass = move |batch: &[DecodeResult], timing: &PassTiming| {
                    // Captured HERE, on the decode thread, right when this
                    // pass's results are ready - not after the channel
                    // hand-off - so this measures the thing the streaming
                    // change above is supposed to have sped up: time from
                    // "window audio ready" to "this pass's results exist".
                    let pass_elapsed = t0.elapsed().as_secs_f64();
                    let _ = tx_batch.blocking_send((pass_elapsed, *timing, batch.to_vec()));
                };
                if is_ft4 { decode_ft4(&samples, &ap_hints, &mut on_pass) } else { decode_ft8(&samples, &ap_hints, &mut on_pass) }
            });

            let time_str = utc_time_str_at(window_start_epoch);
            while let Some((pass_elapsed, timing, batch)) = rx_batch.recv().await {
                // Diagnostyka: pelny rozklad fazowy tego przebiegu - cztery
                // niezalezne poprawki (wczesniejsze dostarczanie wynikow,
                // cache planow FFT, cache grafu LDPC, dluzszy keep-alive puli
                // watkow tokio) NIE zmienily pass_elapsed_s na zywym sprzecie
                // ani trocha (stale ~1.08s, niezaleznie od n_results 0-26) -
                // zamiast zgadywac piaty raz, ten rozklad (spec_ms/
                // find_cand_ms/par_decode_ms/n_cand) pokazuje z NAZWY, ktora
                // faza faktycznie zjada czas na TEJ maszynie.
                let pstats = serde_json::json!({
                    "type": "pass_stats",
                    "pass_elapsed_s": pass_elapsed,
                    "n": batch.len(),
                    "spec_ms": timing.spec_ms,
                    "find_cand_ms": timing.find_cand_ms,
                    "par_decode_ms": timing.par_decode_ms,
                    "n_cand": timing.n_cand,
                    "demod_ms_sum": timing.demod_ms_sum,
                    "ldpc_ms_sum": timing.ldpc_ms_sum,
                    "osd_ms_sum": timing.osd_ms_sum,
                    "ap_ms_sum": timing.ap_ms_sum,
                });
                if stream.write_all(format!("{}\n", pstats).as_bytes()).await.is_err() {
                    return;
                }
                for r in &batch {
                    let json = serde_json::json!({
                        "type":           "wsjtx_decode",
                        "timeStr":        time_str,
                        "snr":            r.snr_db.round() as i32,
                        "deltaTime":      ((r.time_offset_s - 0.5) * 10.0).round() / 10.0,
                        "deltaFreq":      r.freq_hz.round() as i32,
                        "message":        r.message,
                        "call_to":        r.call_to,
                        "call_de":        r.call_de,
                        "report_or_grid": r.report_or_grid,
                        "mode":           mode_str,
                        "isDxpedition":   r.is_dxpedition,
                        "senderCall":     r.sender_call,
                    });
                    let line = format!("{}\n", json);
                    if stream.write_all(line.as_bytes()).await.is_err() {
                        return; // Connection lost
                    }
                }
            }

            let results = decode_handle.await.unwrap_or_default();
            let elapsed = t0.elapsed().as_secs_f32();
            println!("[ft8dec] decoded {} messages in {:.2}s", results.len(), elapsed);

            // Wyslij czas dekodowania do Pythona jako osobna linia JSON - to
            // idzie do TEGO SAMEGO loga co "[ft8rx] dekod -> UI: ...", ktory
            // operator faktycznie wkleja/sprawdza. Bez tego powyzszy println!
            // znika w OSOBNEJ konsoli ham_audio.exe (CREATE_NEW_CONSOLE w
            // audio_rust_bridge.py), niewidocznej operatorowi razem z reszta
            // logu. To CALKOWITY czas (wszystkie przebiegi + subtraction) -
            // od zmiany powyzej NIE odpowiada juz bezposrednio pos_in_win
            // pierwszych dekodow (te teraz docieraja duzo wczesniej, patrz
            // kanal tx_batch/rx_batch powyzej), ale wciaz uzyteczne jako
            // gorna granica calkowitego kosztu dekodowania danego okna.
            let stats = serde_json::json!({
                "type": "decode_stats",
                "decode_elapsed_s": elapsed,
                "n_results": results.len(),
            });
            let stats_line = format!("{}\n", stats);
            if stream.write_all(stats_line.as_bytes()).await.is_err() {
                return;
            }
        }
    }
}

fn current_epoch() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn utc_time_str_at(epoch_secs: f64) -> String {
    let secs = epoch_secs as u64;
    let h = (secs / 3600) % 24;
    let m = (secs / 60) % 60;
    let s = secs % 60;
    format!("{:02}{:02}{:02}", h, m, s)
}
