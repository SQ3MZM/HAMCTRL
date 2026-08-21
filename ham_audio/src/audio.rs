//! WASAPI audio — RX (karta → Opus → WS) + TX (WS Opus → karta)

use std::sync::Arc;
use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::StreamConfig;
use opus::{Encoder, Decoder, Application, Channels};
use serde::Serialize;
use tracing::{info, warn, error};
use bytes::Bytes;

use crate::{AudioFrame, AudioTx, SharedConfig, SharedMixer};

const OPUS_RATE:  u32   = 48000;
const OPUS_CH:    u32   = 1;
const FRAME_SAMP: usize = 960;  // 20ms @ 48kHz

/// Licznik "generacji" karty RX. Hot-swap bez restartu procesu: gdy user
/// zmienia karte (SetRxDevice), main.rs wola bump_rx_device_gen(), co zwieksza
/// ten licznik. Watek WASAPI porownuje zapamietana generacje z biezaca w petli
/// odczytu — gdy sie rozni, konczy stream (czyste zamkniecie, bez fantomow),
/// encoder loop sie konczy, a run_audio_loop przeladowuje z nowa karta.
static RX_DEVICE_GEN: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

/// Zwieksz generacje karty RX — sygnal dla watku WASAPI by przeladowal karte.
pub fn bump_rx_device_gen() {
    RX_DEVICE_GEN.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
}

/// Odczytaj biezaca generacje karty RX.
fn current_rx_device_gen() -> u64 {
    RX_DEVICE_GEN.load(std::sync::atomic::Ordering::SeqCst)
}

/// Liczniki zgubionych ramek (diagnostyka stabilnosci audio). Callback WASAPI
/// inkrementuje je gdy kanal pelny (odbiorca przystanal przy obciazeniu I/O).
/// Widoczne w logu — pozwala ZMIERZYC czy buffer skacze i jak czesto.
static DROPPED_OPUS: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
static DROPPED_FT8:  std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);

#[derive(Debug, Serialize)]
pub struct DeviceInfo {
    pub name:              String,
    pub is_input:          bool,
    pub is_default_input:  bool,
    pub is_default_output: bool,
}

pub fn list_devices() -> Vec<DeviceInfo> {
    let host    = cpal::default_host();
    let def_in  = host.default_input_device().map(|d| d.name().unwrap_or_default());
    let def_out = host.default_output_device().map(|d| d.name().unwrap_or_default());
    let mut result = Vec::new();

    if let Ok(devs) = host.input_devices() {
        for d in devs {
            let name = d.name().unwrap_or_default();
            result.push(DeviceInfo {
                is_default_input:  def_in.as_deref() == Some(&name),
                is_default_output: false,
                is_input: true, name,
            });
        }
    }
    if let Ok(devs) = host.output_devices() {
        for d in devs {
            let name = d.name().unwrap_or_default();
            result.push(DeviceInfo {
                is_default_input:  false,
                is_default_output: def_out.as_deref() == Some(&name),
                is_input: false, name,
            });
        }
    }
    result
}

pub async fn run_audio_loop(
    cfg:    SharedConfig,
    tx:     Arc<AudioTx>,
    tx_lo:  Arc<AudioTx>,
    _mixer: SharedMixer,
    ft8_pcm_tx: std::sync::mpsc::SyncSender<Vec<f32>>,
) {
    loop {
        let (rx_dev, bitrate, bitrate_lo, vol) = {
            let c = cfg.read().await;
            (c.rx_device.clone(), c.bitrate, c.bitrate_lo, c.rx_volume)
        };

        // std::sync::mpsc — działa poprawnie między std thread a tokio.
        // Kanal Opus (odsluch na zywo): MALY bufor = niska latencja. 12 ramek
        // x 20ms = 240ms max (limit Toma 200-300ms). Wiekszy bufor psulby
        // latencje; wolimy zgubic ramke (liczona) niz opoznic odsluch.
        let (pcm_tx, pcm_rx) = std::sync::mpsc::sync_channel::<Vec<f32>>(12);
        let tx2 = tx.clone();

        // Wątek WASAPI — std::thread bo cpal::Stream nie jest Send
        let ft8_pcm_tx2 = ft8_pcm_tx.clone();
        std::thread::spawn(move || {
            if let Err(e) = run_rx_thread(rx_dev, bitrate, vol, pcm_tx, ft8_pcm_tx2) {
                error!("[audio] RX thread error: {}", e);
            }
        });

        // Opus encoder w osobnym wątku blocking — odbiera z std channel.
        // DWA rownolegle kodery z TEGO SAMEGO PCM: "hi" (jakosc domyslna) i
        // "lo" (adaptacyjny bitrate dla slabszych lączy — patrz handle_ws w
        // main.rs, ktory przelacza subskrypcje per-klient po zdarzeniach
        // Lagged, bez wplywu na klientow z dobrym lączem).
        let tx_lo2 = tx_lo.clone();
        let enc_handle = tokio::task::spawn_blocking(move || {
            let mut enc = match Encoder::new(OPUS_RATE, Channels::Mono, Application::LowDelay) {
                Ok(e) => e,
                Err(e) => { error!("[audio] Opus encoder: {}", e); return; }
            };
            let _ = enc.set_bitrate(opus::Bitrate::Bits(bitrate as i32));
            let _ = enc.set_inband_fec(false);
            let _ = enc.set_dtx(false);
            let _ = enc.set_vbr(false);

            let mut enc_lo = match Encoder::new(OPUS_RATE, Channels::Mono, Application::LowDelay) {
                Ok(e) => e,
                Err(e) => { error!("[audio] Opus encoder (lo): {}", e); return; }
            };
            let _ = enc_lo.set_bitrate(opus::Bitrate::Bits(bitrate_lo as i32));
            let _ = enc_lo.set_inband_fec(false);
            let _ = enc_lo.set_dtx(false);
            let _ = enc_lo.set_vbr(false);

            let mut seq: u32 = 0;
            let mut opus_buf = vec![0u8; 4096];
            let mut opus_buf_lo = vec![0u8; 4096];
            let mut frame_count: u64 = 0;

            // Odbieraj PCM ze std channel — blokuje ale to OK bo spawn_blocking
            while let Ok(pcm) = pcm_rx.recv() {
                frame_count += 1;
                if frame_count <= 5 || frame_count % 500 == 0 {
                    println!("[audio] encode frame #{}", frame_count);
                }

                match enc.encode_float(&pcm, &mut opus_buf) {
                    Ok(n) if n > 0 => {
                        let frame = AudioFrame {
                            opus: Bytes::copy_from_slice(&opus_buf[..n]),
                            seq,
                        };
                    let receivers = tx2.receiver_count();
                    let send_result = tx2.send(frame);
                    if frame_count <= 5 || frame_count % 100 == 0 || receivers > 0 && frame_count <= 20 {
                        println!("[audio] frame #{} receivers={} ok={}", frame_count, receivers, send_result.is_ok());
                    }
                    let _ = send_result;
                    }
                    Ok(_) => {}
                    Err(e) => warn!("[audio] Opus encode: {}", e),
                }

                match enc_lo.encode_float(&pcm, &mut opus_buf_lo) {
                    Ok(n) if n > 0 => {
                        let frame_lo = AudioFrame {
                            opus: Bytes::copy_from_slice(&opus_buf_lo[..n]),
                            seq,
                        };
                        let _ = tx_lo2.send(frame_lo);
                    }
                    Ok(_) => {}
                    Err(e) => warn!("[audio] Opus encode (lo): {}", e),
                }

                seq = seq.wrapping_add(1);
            }
            println!("[audio] encoder loop ended");
        });

        let _ = enc_handle.await;
        info!("[audio] RX loop ended, restarting...");
        tokio::time::sleep(tokio::time::Duration::from_secs(2)).await;
    }
}

fn run_rx_thread(
    device_name: String,
    _bitrate:    u32,
    _vol:        f32,
    pcm_tx:      std::sync::mpsc::SyncSender<Vec<f32>>,
    ft8_pcm_tx:  std::sync::mpsc::SyncSender<Vec<f32>>,
) -> Result<(), Box<dyn std::error::Error>> {
    let host = cpal::default_host();

    let device = if device_name.is_empty() {
        host.default_input_device().ok_or("No default input device")?
    } else {
        host.input_devices()?
            .find(|d| d.name().map(|n| n.contains(&device_name)).unwrap_or(false))
            .unwrap_or_else(|| host.default_input_device().unwrap())
    };

    info!("[audio] RX: {}", device.name().unwrap_or_default());

    let supported = device.supported_input_configs()
        .map(|mut configs| configs.any(|c| {
            c.min_sample_rate().0 <= OPUS_RATE && c.max_sample_rate().0 >= OPUS_RATE
        }))
        .unwrap_or(false);

    // Jawny, maly bufor sprzetowy (20ms, ta sama granulacja co reszta toru:
    // FRAME_SAMP, ramki Opus, jitter-buffer front-endu) zamiast BufferSize::
    // Default — sterownik/WASAPI mial dowolnosc wyboru okresu, tu zadamy
    // wprost wartosci zgodnej z budzetem 200-300ms latencji.
    let (cfg, actual_rate) = if supported {
        (StreamConfig {
            channels:    1,
            sample_rate: cpal::SampleRate(OPUS_RATE),
            buffer_size: cpal::BufferSize::Fixed(FRAME_SAMP as u32),
        }, OPUS_RATE)
    } else {
        let default_cfg = device.default_input_config()?;
        let rate = default_cfg.sample_rate().0;
        println!("[audio] 48kHz niedostepne, uzyje {}Hz + resample", rate);
        let frame_native = ((rate as u64 * FRAME_SAMP as u64) / OPUS_RATE as u64) as u32;
        (StreamConfig {
            channels:    1,
            sample_rate: default_cfg.sample_rate(),
            buffer_size: cpal::BufferSize::Fixed(frame_native.max(1)),
        }, rate)
    };
    println!("[audio] Stream config: {}Hz", actual_rate);

    // Bufor do resamplera — akumuluje probki przed wyslaniem
    let resample_ratio = OPUS_RATE as f64 / actual_rate as f64;
    let pcm_tx2 = pcm_tx.clone();

    // FIX: reported live — after the IC-7300 loses power (its built-in USB
    // Audio CODEC vanishes with it) and comes back, CI-V and even the
    // Python-side audio_stream.py capture both self-heal correctly, but
    // this WASAPI RX stream stayed permanently silent until the whole
    // ham_audio.exe process was restarted. Root cause: the keep-alive loop
    // below only ever broke out (and let run_audio_loop reopen with a
    // fresh device handle) on a MANUAL device switch (RX_DEVICE_GEN bump)
    // — a device that silently disappears/reappears underneath an
    // already-open cpal::Stream was never detected at all, so the stream
    // sat there technically "running" but dead. stream_ok tracks whether
    // the WASAPI error callback fired; last_cb_tick tracks whether the
    // DATA callback is still actually being invoked (some drivers drop a
    // device without ever calling the error callback at all — silent
    // stall, not an explicit error). Either signal now breaks the loop the
    // same way a device-gen change does.
    let stream_ok     = Arc::new(std::sync::atomic::AtomicBool::new(true));
    let stream_ok_cb  = stream_ok.clone();
    let last_cb_tick   = Arc::new(std::sync::atomic::AtomicU64::new(0));
    let last_cb_tick_cb = last_cb_tick.clone();

    let mut resample_buf: Vec<f32> = Vec::new();
    let mut direct_buf:   Vec<f32> = Vec::new();
    let stream = device.build_input_stream(
        &cfg,
        move |data: &[f32], _| {
            last_cb_tick_cb.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            if (resample_ratio - 1.0).abs() < 0.001 {
                // Brak resamplera — akumuluj do pelnych ramek FRAME_SAMP
                direct_buf.extend_from_slice(data);
                while direct_buf.len() >= FRAME_SAMP {
                    let frame: Vec<f32> = direct_buf.drain(..FRAME_SAMP).collect();
                    // FT8 (dekoder) — gdy pelny licz drop (dziura w audio dekodera)
                    if ft8_pcm_tx.try_send(frame.clone()).is_err() {
                        DROPPED_FT8.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    }
                    // Opus (odsluch) — maly bufor dla latencji; drop = licz
                    if pcm_tx2.try_send(frame).is_err() {
                        DROPPED_OPUS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    }
                }
            } else {
                // Resample liniowy do 48kHz
                resample_buf.extend_from_slice(data);
                let needed = (FRAME_SAMP as f64 / resample_ratio) as usize + 1;
                while resample_buf.len() >= needed {
                    let out_len = FRAME_SAMP;
                    let mut out = vec![0f32; out_len];
                    for i in 0..out_len {
                        let src_pos = i as f64 / resample_ratio;
                        let left = src_pos as usize;
                        let right = (left + 1).min(resample_buf.len() - 1);
                        let frac = src_pos - left as f64;
                        out[i] = resample_buf[left] * (1.0 - frac as f32)
                                + resample_buf[right] * frac as f32;
                    }
                    let consumed = (out_len as f64 / resample_ratio) as usize;
                    resample_buf.drain(..consumed.min(resample_buf.len()));
                    if ft8_pcm_tx.try_send(out.clone()).is_err() {
                        DROPPED_FT8.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    }
                    if pcm_tx2.try_send(out).is_err() {
                        DROPPED_OPUS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    }
                }
            }
        },
        move |e| {
            error!("[audio] WASAPI error: {}", e);
            stream_ok_cb.store(false, std::sync::atomic::Ordering::Relaxed);
        },
        None,
    )?;

    stream.play()?;
    println!("[audio] WASAPI RX stream started, device={}", device.name().unwrap_or_default());

    // Trzymaj wątek przy życiu, ale reaguj na hot-swap karty ORAZ na
    // device, ktore znika/wraca pod spodem (patrz komentarz przy stream_ok
    // powyzej). Zapamietaj generacje z momentu startu; gdy user zmieni
    // karte (bump_rx_device_gen) LUB stream przestanie realnie dostarczac
    // dane (device zniknal), konczymy stream (drop na wyjsciu z funkcji
    // zamyka WASAPI czysto), co konczy encoder loop i pozwala
    // run_audio_loop przeladowac z nowym uchwytem urzadzenia.
    let my_gen = current_rx_device_gen();
    let mut ticks: u64 = 0;
    let mut last_opus: u64 = 0;
    let mut last_ft8:  u64 = 0;
    let mut last_seen_cb_tick: u64 = 0;
    let mut stale_ticks: u32 = 0;
    // Grace period before the staleness check kicks in — device open +
    // first WASAPI buffer can legitimately take a few hundred ms.
    const STALE_GRACE_TICKS: u32 = 15;  // 15 * 200ms = 3s
    const STALE_LIMIT_TICKS: u32 = 10;  // 10 * 200ms = 2s with zero new callbacks
    loop {
        std::thread::sleep(std::time::Duration::from_millis(200));
        if current_rx_device_gen() != my_gen {
            println!("[audio] hot-swap: generacja karty zmieniona, zamykam stream RX");
            break;
        }
        if !stream_ok.load(std::sync::atomic::Ordering::Relaxed) {
            println!("[audio] RX stream WASAPI error signalled — zamykam i przeladowuje");
            break;
        }
        if ticks >= STALE_GRACE_TICKS as u64 {
            let cur = last_cb_tick.load(std::sync::atomic::Ordering::Relaxed);
            if cur == last_seen_cb_tick {
                stale_ticks += 1;
                if stale_ticks >= STALE_LIMIT_TICKS {
                    println!("[audio] RX stream STALE — brak danych z urzadzenia od {}ms, zamykam i przeladowuje",
                             stale_ticks as u64 * 200);
                    break;
                }
            } else {
                stale_ticks = 0;
                last_seen_cb_tick = cur;
            }
        }
        // Raportuj zgubione ramki co ~5s, ale TYLKO gdy cos pada (inaczej cisza).
        // Pozwala Tomowi ZMIERZYC czy buffer skacze przy operacjach I/O.
        ticks += 1;
        if ticks % 25 == 0 {
            let opus = DROPPED_OPUS.load(std::sync::atomic::Ordering::Relaxed);
            let ft8  = DROPPED_FT8.load(std::sync::atomic::Ordering::Relaxed);
            let d_opus = opus - last_opus;
            let d_ft8  = ft8  - last_ft8;
            if d_opus > 0 || d_ft8 > 0 {
                println!("[audio] DROP w ost. 5s: opus(odsluch)={} ft8(dekoder)={} | lacznie opus={} ft8={}",
                         d_opus, d_ft8, opus, ft8);
            }
            last_opus = opus;
            last_ft8  = ft8;
        }
    }
    // stream zostanie zdropniety tutaj (StreamTrait) — czyste zamkniecie WASAPI
    drop(stream);
    Ok(())
}

#[allow(dead_code)]
pub fn run_tx_thread(
    device_name: String,
    opus_rx:     std::sync::mpsc::Receiver<Bytes>,
) {
    std::thread::spawn(move || {
        if let Err(e) = _run_tx_inner(device_name, opus_rx) {
            error!("[audio] TX thread: {}", e);
        }
    });
}

fn _run_tx_inner(
    device_name: String,
    opus_rx:     std::sync::mpsc::Receiver<Bytes>,
) -> Result<(), Box<dyn std::error::Error>> {
    let host = cpal::default_host();

    let device = if device_name.is_empty() {
        host.default_output_device().ok_or("No default output device")?
    } else {
        host.output_devices()?
            .find(|d| d.name().map(|n| n.contains(&device_name)).unwrap_or(false))
            .unwrap_or_else(|| host.default_output_device().unwrap())
    };

    info!("[audio] TX: {}", device.name().unwrap_or_default());

    let cfg = StreamConfig {
        channels:    OPUS_CH as u16,
        sample_rate: cpal::SampleRate(OPUS_RATE),
        buffer_size: cpal::BufferSize::Fixed(FRAME_SAMP as u32),
    };

    let mut dec = Decoder::new(OPUS_RATE, Channels::Mono)?;

    let pcm_queue: Arc<std::sync::Mutex<std::collections::VecDeque<f32>>> =
        Arc::new(std::sync::Mutex::new(std::collections::VecDeque::new()));
    let pq2 = pcm_queue.clone();

    let stream = device.build_output_stream(
        &cfg,
        move |out: &mut [f32], _| {
            let mut q = pq2.lock().unwrap();
            for s in out.iter_mut() {
                *s = q.pop_front().unwrap_or(0.0);
            }
        },
        |e| error!("[audio] TX WASAPI: {}", e),
        None,
    )?;
    stream.play()?;

    let mut pcm_buf = vec![0f32; FRAME_SAMP];
    for opus_data in opus_rx.iter() {
        match dec.decode_float(&opus_data, &mut pcm_buf, false) {
            Ok(n) => {
                let mut q = pcm_queue.lock().unwrap();
                if q.len() < FRAME_SAMP * 10 {
                    q.extend(pcm_buf[..n].iter().copied());
                }
            }
            Err(e) => warn!("[audio] Opus TX decode: {}", e),
        }
    }
    Ok(())
}
