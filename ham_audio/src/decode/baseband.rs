/// FT8: demodulacja w pasmie podstawowym (baseband) - sposob z opisu
/// protokolu FT8 (K1JT/K9AN/G4WJS).
///
/// Zamiast liczyc FFT symbol-po-symbolu z calego pasma audio (stary tor
/// refine_candidate_ft8 + extract_tone_power), sygnal kazdego kandydata jest:
///   1. wycinany z widma CALEGO okna (jedna duza FFT na przebieg) w waskim
///      pasmie f0-1.5..f0+8.5 tonu i przenoszony do pasma podstawowego
///      200 S/s (32 probki/symbol) - filtr odcina sasiadow i szum spoza
///      sygnalu,
///   2. dosynchronizowany w czasie co 5 ms i w czestotliwosci co 0.5 Hz
///      zespolona korelacja z wzorcem Costas,
///   3. demodulowany 32-punktowa FFT na symbol (okno prostokatne - tony
///      ortogonalne, bez przeciekow miedzy sasiednimi tonami),
///   4. zamieniany na 4 zestawy LLR: metryka 1-, 2- i 3-symbolowa
///      (koherentne sumowanie sasiednich symboli) oraz 1-symbolowa
///      normalizowana. Kazdy zestaw jest znormalizowany do stalej wariancji,
///      co jest kluczowe dla dekodera LDPC.
///
/// Zmierzone na plikach testowych (ft8sim, pojedynczy sygnal): stary tor
/// gubil praktycznie wszystko ponizej -18 dB.

use rustfft::num_complex::Complex;
use super::fft_cache::{cached_fft_forward, cached_fft_inverse};
use super::params::{FT8_COSTAS, FT8_GRAYMAP, FT4_COSTAS, FT4_GRAYMAP};
use super::sync::Candidate;

const NFFT1: usize = 192_000;      // FFT calego okna (16 s przy 12 kHz)
const NFFT2: usize = 3_200;        // dlugosc sygnalu w pasmie podstawowym
const NSPS2: usize = 32;           // probek na symbol przy 200 S/s
const FS2: f32 = 200.0;
const BAUD: f32 = 6.25;
const SCALEFAC: f32 = 2.83;

// FT4: 12000/18 = 666.7 S/s, tez 32 probki/symbol (symbol 48 ms).
const NFFT1_FT4: usize = 96_768;   // 8.064 s przy 12 kHz
const NFFT2_FT4: usize = 5_376;    // NFFT1_FT4 / 18
const FS4: f32 = 12000.0 / 18.0;
const BAUD4: f32 = 12000.0 / 576.0;

/// Widmo calego okna - liczone raz na przebieg, wspolne dla wszystkich
/// kandydatow (tylko do odczytu, wiec bezpieczne dla rayon).
pub struct WindowSpectrum {
    cx: Vec<Complex<f32>>,
    nfft1: usize,
}

fn spectrum_n(audio: &[f32], nfft1: usize) -> WindowSpectrum {
    let fft = cached_fft_forward(nfft1);
    let mut buf = vec![Complex::new(0f32, 0f32); nfft1];
    for (i, &s) in audio.iter().take(nfft1).enumerate() {
        buf[i] = Complex::new(s, 0.0);
    }
    fft.process(&mut buf);
    buf.truncate(nfft1 / 2 + 1);
    WindowSpectrum { cx: buf, nfft1 }
}

/// Widmo okna FT8 (16 s).
pub fn window_spectrum(audio: &[f32]) -> WindowSpectrum { spectrum_n(audio, NFFT1) }

/// Widmo okna FT4 (8.064 s).
pub fn window_spectrum_ft4(audio: &[f32]) -> WindowSpectrum { spectrum_n(audio, NFFT1_FT4) }

/// Wycina pasmo sygnalu o tonie 0 na f0 i przenosi je do pasma
/// podstawowego 200 S/s (ton 0 -> 0 Hz, ton k -> k*6.25 Hz).
fn downsample(ws: &WindowSpectrum, f0: f32) -> Vec<Complex<f32>> {
    downsample_gen(ws, f0, NFFT2, BAUD, 8.5)
}

/// Wspolny rdzen dla FT8/FT4: pasmo f0-1.5 tonu .. f0+`hi_tones` tonu,
/// wynik `nfft2` probek zespolonych (ws.nfft1/nfft2 = wspolczynnik decymacji).
fn downsample_gen(ws: &WindowSpectrum, f0: f32, nfft2: usize, baud: f32, hi_tones: f32) -> Vec<Complex<f32>> {
    let nfft1 = ws.nfft1;
    let df = 12000.0 / nfft1 as f32;
    let half = nfft1 / 2;
    let i0 = (f0 / df).round() as isize;
    let it = (((f0 + hi_tones * baud) / df).round() as isize).min(half as isize);
    let ib = (((f0 - 1.5 * baud) / df).round() as isize).max(1);
    let mut c1 = vec![Complex::new(0f32, 0f32); nfft2];
    let mut k = 0usize;
    let mut i = ib;
    while i <= it && k < nfft2 {
        c1[k] = ws.cx[i as usize];
        k += 1;
        i += 1;
    }
    // Lagodne zbocza filtra (pol-kosinus na 101 binach z kazdej strony).
    if k > 202 {
        for j in 0..=100usize {
            let taper = |m: usize| 0.5 * (1.0 + (m as f32 * std::f32::consts::PI / 100.0).cos());
            c1[j] *= taper(100 - j);
            c1[k - 1 - 100 + j] *= taper(j);
        }
    }
    // Przesuniecie cykliczne: bin f0 na indeks 0.
    let shift = (i0 - ib).rem_euclid(nfft2 as isize) as usize;
    c1.rotate_left(shift);
    cached_fft_inverse(nfft2).process(&mut c1);
    let fac = 1.0 / ((nfft1 as f32) * (nfft2 as f32)).sqrt();
    for v in c1.iter_mut() { *v *= fac; }
    c1
}

fn costas_templates() -> [[Complex<f32>; NSPS2]; 7] {
    let mut t = [[Complex::new(0f32, 0f32); NSPS2]; 7];
    for (i, &tone) in FT8_COSTAS.iter().enumerate() {
        let dphi = 2.0 * std::f32::consts::PI * tone as f32 / NSPS2 as f32;
        let mut phi = 0f32;
        for j in 0..NSPS2 {
            t[i][j] = Complex::new(phi.cos(), phi.sin());
            phi = (phi + dphi) % (2.0 * std::f32::consts::PI);
        }
    }
    t
}

/// Moc korelacji z trzema blokami Costas przy starcie symbolu 0 w probce i0,
/// opcjonalnie z korekta czestotliwosci `twk` (mnoznik fazowy na probke).
fn sync8d(cd: &[Complex<f32>], i0: isize, tmpl: &[[Complex<f32>; NSPS2]; 7], twk: Option<&[Complex<f32>; NSPS2]>) -> f32 {
    let mut sync = 0f32;
    for i in 0..7 {
        let mut csync = tmpl[i];
        if let Some(tw) = twk {
            for j in 0..NSPS2 { csync[j] *= tw[j]; }
        }
        for block in [0isize, 36, 72] {
            let i1 = i0 + (i as isize + block) * NSPS2 as isize;
            if i1 < 0 || i1 + NSPS2 as isize > cd.len() as isize { continue; }
            let s = i1 as usize;
            let mut z = Complex::new(0f32, 0f32);
            for j in 0..NSPS2 { z += cd[s + j] * csync[j].conj(); }
            sync += z.norm_sqr();
        }
    }
    sync
}

/// Wynik demodulacji kandydata: dokladna pozycja (do odejmowania/wyswietlania)
/// i 4 zestawy LLR do probowania po kolei (konwencja bp_decode: dodatni = 0).
pub struct Demod {
    pub cand: Candidate,
    pub llrs: Vec<[f32; 174]>,
    /// Ile tonow Costas trafilo (FT8: z 21, FT4: z 16).
    pub nsync: usize,
    /// Amplitudy tonow w kazdym symbolu (FT8: 79x8, FT4: 103x4, wierszami) -
    /// do SNR liczonego z tonow zdekodowanego komunikatu (snr_from_tones).
    pub mag: Vec<f32>,
}

/// SNR (dB, skala WSJT-X): srednia moc tonow zdekodowanego komunikatu
/// (z demodulacji - bez sasiednich stacji) wzgledem poziomu tla szumu z
/// widma calego okna (noise_floor_from_spec). Pierwsza wersja liczyla szum z
/// tonu "przeciwleglego" w tym samym symbolu, ale na zatloczonym pasmie
/// siedza tam tony sasiadow i silne stacje wychodzily o 10-15 dB za nisko.
/// `offset_db` dopasowany do jt9 na probkach referencyjnych.
pub fn snr_from_tones(mag: &[f32], tones: &[usize], n_tones: usize, noise_floor: f32, offset_db: f32) -> f32 {
    let mut xsig = 0f32;
    let mut n = 0usize;
    for (k, &t) in tones.iter().enumerate() {
        let row = &mag[k * n_tones..(k + 1) * n_tones];
        // Symbole poza oknem (same zera) nie wnosza informacji o mocy.
        if row.iter().all(|&v| v == 0.0) { continue; }
        xsig += row[t] * row[t];
        n += 1;
    }
    if n == 0 || noise_floor <= 0.0 || xsig <= 0.0 { return -24.0; }
    (10.0 * (xsig / n as f32 / noise_floor).log10() + offset_db).clamp(-24.0, 30.0)
}

pub fn demod_ft8(ws: &WindowSpectrum, coarse: &Candidate) -> Option<Demod> {
    let tmpl = costas_templates();
    let mut f1 = coarse.freq_hz;
    let mut cd = downsample(ws, f1);

    // Czas: +-10 probek (+-50 ms) wokol pozycji zgrubnej, krok 5 ms.
    let i0 = (coarse.time_offset_s * FS2).round() as isize;
    let mut ibest = i0;
    let mut smax = f32::MIN;
    for idt in (i0 - 10)..=(i0 + 10) {
        let s = sync8d(&cd, idt, &tmpl, None);
        if s > smax { smax = s; ibest = idt; }
    }

    // Czestotliwosc i czas dokladnie: koherentna korelacja calych blokow
    // Costas (patrz refine_coherent).
    let blocks: [(usize, &[usize]); 3] = [(0, &FT8_COSTAS), (36, &FT8_COSTAS), (72, &FT8_COSTAS)];
    let (df, ib2) = refine_coherent(&cd, ibest, &blocks, FS2, 2.5, 4);
    if df != 0.0 {
        f1 += df;
        cd = downsample(ws, f1);
    }
    let ibest = ib2;

    // Symbole: 32-punktowa FFT, pierwsze 8 binow = tony 0..7.
    let fft32 = cached_fft_forward(NSPS2);
    let mut cs = [[Complex::new(0f32, 0f32); 8]; 79];
    let mut s8 = [[0f32; 8]; 79];
    for k in 0..79 {
        let i1 = ibest + (k * NSPS2) as isize;
        let mut buf = [Complex::new(0f32, 0f32); NSPS2];
        if i1 >= 0 && i1 + NSPS2 as isize <= cd.len() as isize {
            buf.copy_from_slice(&cd[i1 as usize..i1 as usize + NSPS2]);
        }
        fft32.process(&mut buf);
        for t in 0..8 {
            cs[k][t] = buf[t];
            s8[k][t] = buf[t].norm();
        }
    }

    // Twarda kontrola synchronizacji: ile z 21 tonow Costas jest najmocniejszych
    // w swoim symbolu. <=6 to szum - szkoda czasu LDPC (i ryzyko fantomow).
    let mut nsync = 0;
    for block in [0usize, 36, 72] {
        for k in 0..7 {
            let row = &s8[block + k];
            let mx = (0..8).max_by(|&a, &b| row[a].partial_cmp(&row[b]).unwrap()).unwrap();
            if mx == FT8_COSTAS[k] { nsync += 1; }
        }
    }
    if nsync <= 6 { return None; }
    let mag: Vec<f32> = s8.iter().flat_map(|r| r.iter().copied()).collect();

    // Metryki bitowe: 1-, 2- i 3-symbolowe (koherentnie) + 1-symbolowa
    // normalizowana. Konwencja metryki: dodatnia = bit 1.
    let mut bmeta = [0f32; 174];
    let mut bmetb = [0f32; 174];
    let mut bmetc = [0f32; 174];
    let mut bmetd = [0f32; 174];
    let gm = FT8_GRAYMAP;
    for nsym in 1..=3usize {
        let nt = 1usize << (3 * nsym);
        let ibmax = match nsym { 1 => 2, 2 => 5, _ => 8 };
        for ihalf in 0..2usize {
            let mut k = 0usize;
            while k < 29 {
                let ks = k + 7 + ihalf * 36;  // indeks symbolu (0-based)
                let mut s2 = [0f32; 512];
                for i in 0..nt {
                    let i1 = i / 64;
                    let i2 = (i & 63) / 8;
                    let i3 = i & 7;
                    s2[i] = match nsym {
                        1 => cs[ks][gm[i3]].norm(),
                        2 => (cs[ks][gm[i2]] + cs[ks + 1][gm[i3]]).norm(),
                        _ => (cs[ks][gm[i1]] + cs[ks + 1][gm[i2]] + cs[ks + 2][gm[i3]]).norm(),
                    };
                }
                let i32_ = k * 3 + ihalf * 87;
                for ib in 0..=ibmax {
                    let bit = ibmax - ib;
                    let mut max1 = f32::MIN;
                    let mut max0 = f32::MIN;
                    for i in 0..nt {
                        if (i >> bit) & 1 == 1 { max1 = max1.max(s2[i]); } else { max0 = max0.max(s2[i]); }
                    }
                    let idx = i32_ + ib;
                    if idx >= 174 { continue; }
                    let bm = max1 - max0;
                    match nsym {
                        1 => {
                            bmeta[idx] = bm;
                            let den = max1.max(max0);
                            bmetd[idx] = if den > 0.0 { bm / den } else { 0.0 };
                        }
                        2 => bmetb[idx] = bm,
                        _ => bmetc[idx] = bm,
                    }
                }
                k += nsym;
            }
        }
    }

    let llrs = normalize_llrs(&[&bmeta, &bmetb, &bmetc, &bmetd])?;

    Some(Demod {
        cand: Candidate { freq_hz: f1, time_offset_s: ibest as f32 / FS2, score: coarse.score },
        llrs,
        nsync,
        mag,
    })
}

/// Liczba bitow, w ktorych zdekodowane slowo kodowe nie zgadza sie z twarda
/// decyzja z LLR - zabezpieczenie przed falszywymi dekodami (duzo
/// niezgodnosci = LDPC+CRC "trafily" przypadkiem w zupelnie inne slowo).
pub fn hard_errors(llr: &[f32; 174], cw: &[u8; 174]) -> usize {
    (0..174).filter(|&i| ((llr[i] < 0.0) as u8) != cw[i]).count()
}

/// Metryki -> LLR: normalizacja do jednostkowego odchylenia, skala SCALEFAC,
/// znak odwrocony (metryka: dodatnia = bit 1; bp_decode: dodatni = bit 0).
fn normalize_llrs(sets: &[&[f32; 174]]) -> Option<Vec<[f32; 174]>> {
    let mut out = Vec::with_capacity(sets.len());
    for src in sets {
        let n = 174.0f32;
        let av: f32 = src.iter().sum::<f32>() / n;
        let av2: f32 = src.iter().map(|v| v * v).sum::<f32>() / n;
        let var = av2 - av * av;
        let sig = if var > 0.0 { var.sqrt() } else { av2.sqrt() };
        if !(sig > 0.0) { return None; }
        let mut dst = [0f32; 174];
        for i in 0..174 { dst[i] = -SCALEFAC * src[i] / sig; }
        out.push(dst);
    }
    Some(out)
}

const FT4_COSTAS_POS: [usize; 4] = [0, 33, 66, 99];

fn tone_template(tone: usize) -> [Complex<f32>; NSPS2] {
    let mut t = [Complex::new(0f32, 0f32); NSPS2];
    let dphi = 2.0 * std::f32::consts::PI * tone as f32 / NSPS2 as f32;
    let mut phi = 0f32;
    for j in 0..NSPS2 {
        t[j] = Complex::new(phi.cos(), phi.sin());
        phi = (phi + dphi) % (2.0 * std::f32::consts::PI);
    }
    t
}

/// FT4: moc korelacji z czterema (roznymi) blokami Costas po 4 symbole.
fn sync4d(cd: &[Complex<f32>], i0: isize, tmpl: &[[[Complex<f32>; NSPS2]; 4]; 4], twk: Option<&[Complex<f32>; NSPS2]>) -> f32 {
    let mut sync = 0f32;
    for (b, &pos) in FT4_COSTAS_POS.iter().enumerate() {
        for k in 0..4 {
            let i1 = i0 + ((pos + k) * NSPS2) as isize;
            if i1 < 0 || i1 + NSPS2 as isize > cd.len() as isize { continue; }
            let s = i1 as usize;
            let mut z = Complex::new(0f32, 0f32);
            for j in 0..NSPS2 {
                let mut c = tmpl[b][k][j];
                if let Some(tw) = twk { c *= tw[j]; }
                z += cd[s + j] * c.conj();
            }
            sync += z.norm_sqr();
        }
    }
    sync
}

/// FT4: ten sam schemat co demod_ft8 (pasmo podstawowe, dosynchronizowanie
/// zespolone, FFT 32 na symbol), metryki 1-, 2- i 4-symbolowe -> 3 zestawy
/// LLR. Bity w LLR sa w kolejnosci nadawania (scramblowane) - descrambling
/// po zdekodowaniu, jak w dotychczasowym torze FT4.
pub fn demod_ft4(ws: &WindowSpectrum, coarse: &Candidate) -> Option<Demod> {
    let mut tmpl = [[[Complex::new(0f32, 0f32); NSPS2]; 4]; 4];
    for b in 0..4 { for k in 0..4 { tmpl[b][k] = tone_template(FT4_COSTAS[b][k]); } }

    let mut f1 = coarse.freq_hz;
    let mut cd = downsample_gen(ws, f1, NFFT2_FT4, BAUD4, 4.5);

    // Czas: +-60 probek (+-90 ms), krok 1.5 ms. Zgrubny czas FT4 z
    // spektrogramu bywa przesuniety o 45-70 ms (zmierzone na pliku
    // referencyjnym ze slabymi CQ) - przy +-36 ms czesc sygnalow nigdy nie
    // trafiala we wlasciwe polozenie.
    let i0 = (coarse.time_offset_s * FS4).round() as isize;
    let mut ibest = i0;
    let mut smax = f32::MIN;
    for idt in (i0 - 60)..=(i0 + 60) {
        let s = sync4d(&cd, idt, &tmpl, None);
        if s > smax { smax = s; ibest = idt; }
    }
    // Czestotliwosc i czas dokladnie: koherentnie po blokach Costas
    // (siatka zgrubna FT4 to ~10 Hz, wiec +-6 Hz).
    let blocks: [(usize, &[usize]); 4] = [
        (FT4_COSTAS_POS[0], &FT4_COSTAS[0]), (FT4_COSTAS_POS[1], &FT4_COSTAS[1]),
        (FT4_COSTAS_POS[2], &FT4_COSTAS[2]), (FT4_COSTAS_POS[3], &FT4_COSTAS[3]),
    ];
    let (df, ib2) = refine_coherent(&cd, ibest, &blocks, FS4, 6.0, 4);
    if df != 0.0 {
        f1 += df;
        cd = downsample_gen(ws, f1, NFFT2_FT4, BAUD4, 4.5);
    }
    let ibest = ib2;

    let fft32 = cached_fft_forward(NSPS2);
    let mut cs = [[Complex::new(0f32, 0f32); 4]; 103];
    let mut s4 = [[0f32; 4]; 103];
    for k in 0..103 {
        let i1 = ibest + (k * NSPS2) as isize;
        let mut buf = [Complex::new(0f32, 0f32); NSPS2];
        if i1 >= 0 && i1 + NSPS2 as isize <= cd.len() as isize {
            buf.copy_from_slice(&cd[i1 as usize..i1 as usize + NSPS2]);
        }
        fft32.process(&mut buf);
        for t in 0..4 {
            cs[k][t] = buf[t];
            s4[k][t] = buf[t].norm();
        }
    }

    // Twarda kontrola synchronizacji: z 16 tonow Costas przypadkiem trafia
    // sie ok. 4 - ponizej 6 to szum.
    let mut nsync = 0;
    for (b, &pos) in FT4_COSTAS_POS.iter().enumerate() {
        for k in 0..4 {
            let row = &s4[pos + k];
            let mx = (0..4).max_by(|&a, &c| row[a].partial_cmp(&row[c]).unwrap()).unwrap();
            if mx == FT4_COSTAS[b][k] { nsync += 1; }
        }
    }
    if nsync < 8 { return None; }
    let mag: Vec<f32> = s4.iter().flat_map(|r| r.iter().copied()).collect();

    // Metryki po wszystkich 103 symbolach (206 bitow, razem z Costas),
    // grupy po 1, 2 i 4 symbole; potem wyciecie 3x58 bitow danych.
    let gm = FT4_GRAYMAP;
    let mut bm = [[0f32; 206]; 3];
    for (set, &nseq) in [1usize, 2, 4].iter().enumerate() {
        let nt = 1usize << (2 * nseq);
        let ibmax = 2 * nseq - 1;
        let mut ks = 0usize;
        while ks + nseq <= 103 {
            let mut s2 = [0f32; 256];
            for i in 0..nt {
                let mut z = Complex::new(0f32, 0f32);
                for j in 0..nseq {
                    let sym = (i >> (2 * (nseq - 1 - j))) & 3;
                    z += cs[ks + j][gm[sym]];
                }
                s2[i] = z.norm();
            }
            let ipt = 2 * ks;
            for ib in 0..=ibmax {
                let bit = ibmax - ib;
                let mut max1 = f32::MIN;
                let mut max0 = f32::MIN;
                for i in 0..nt {
                    if (i >> bit) & 1 == 1 { max1 = max1.max(s2[i]); } else { max0 = max0.max(s2[i]); }
                }
                if ipt + ib < 206 { bm[set][ipt + ib] = max1 - max0; }
            }
            ks += nseq;
        }
    }
    let mut sets = [[0f32; 174]; 3];
    for set in 0..3 {
        sets[set][0..58].copy_from_slice(&bm[set][8..66]);
        sets[set][58..116].copy_from_slice(&bm[set][74..132]);
        sets[set][116..174].copy_from_slice(&bm[set][140..198]);
    }
    let llrs = normalize_llrs(&[&sets[0], &sets[1], &sets[2]])?;

    Some(Demod {
        cand: Candidate { freq_hz: f1, time_offset_s: ibest as f32 / FS4, score: coarse.score },
        llrs,
        nsync,
        mag,
    })
}

/// Moc koherentnej korelacji calych blokow Costas przy starcie symbolu 0 w
/// probce i0 i odstrojeniu `df` Hz. Kazdy ton robi calkowita liczbe okresow
/// na symbol, wiec faza jest ciagla przez caly blok - mozna sumowac
/// koherentnie wszystkie jego symbole (FT8: 7, FT4: 4) zamiast kazdy osobno.
/// Daje to kilkukrotnie ostrzejsza rozdzielczosc czestotliwosci niz
/// korelacja symbol-po-symbolu (sync8d/sync4d). Blok niemieszczacy sie w
/// sygnale jest pomijany.
fn sync_coh(cd: &[Complex<f32>], i0: isize, blocks: &[(usize, &[usize])], df: f32, fs: f32) -> f32 {
    let rot = Complex::from_polar(1.0f32, -2.0 * std::f32::consts::PI * df / fs);
    let mut total = 0f32;
    for &(pos, tones) in blocks {
        let start = i0 + (pos * NSPS2) as isize;
        let len = (tones.len() * NSPS2) as isize;
        if start < 0 || start + len > cd.len() as isize { continue; }
        let start = start as usize;
        let mut z = Complex::new(0f32, 0f32);
        let mut tw = Complex::new(1f32, 0f32);
        for (k, &t) in tones.iter().enumerate() {
            let tmpl = tone_template(t);
            for j in 0..NSPS2 {
                z += cd[start + k * NSPS2 + j] * tmpl[j].conj() * tw;
                tw *= rot;
            }
        }
        total += z.norm_sqr();
    }
    total
}

/// Dokladna synchronizacja: siatka czas x czestotliwosc z koherentna
/// korelacja blokow (+-`dt_range` probek, +-`df_range` Hz co 0.25 Hz), potem
/// doprecyzowanie czestotliwosci co 0.05 Hz. Zwraca (odstrojenie Hz, start).
/// Zmierzone na plikach testowych FT4: przy dokladnie znanej czestotliwosci i
/// czasie dekodowalismy 39/40 plikow przy -16/-17 dB, z dotychczasowym
/// szukaniem (symbol po symbolu, krok 0.5 Hz, bledy do 2.5 Hz) tylko 22/40 -
/// metryki 2-, 3- i 4-symbolowe sa koherentne i odstrojenie je niszczy.
fn refine_coherent(cd: &[Complex<f32>], i0: isize, blocks: &[(usize, &[usize])], fs: f32, df_range: f32, dt_range: isize) -> (f32, isize) {
    let mut best = (f32::MIN, 0f32, i0);
    let n = (df_range / 0.25).round() as i32;
    for idt in -dt_range..=dt_range {
        for ifr in -n..=n {
            let df = ifr as f32 * 0.25;
            let s = sync_coh(cd, i0 + idt, blocks, df, fs);
            if s > best.0 { best = (s, df, i0 + idt); }
        }
    }
    let (_, df0, ib) = best;
    let mut bdf = (f32::MIN, df0);
    for k in -5..=5 {
        let df = df0 + k as f32 * 0.05;
        let s = sync_coh(cd, ib, blocks, df, fs);
        if s > bdf.0 { bdf = (s, df); }
    }
    (bdf.1, ib)
}
