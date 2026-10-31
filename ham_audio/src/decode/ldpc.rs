/// LDPC(174,91) belief propagation decoder — sum-product (tanh).
/// Direct port of ldpc_decode.py, optimized for Rust.

use super::params::NM;
use std::sync::OnceLock;

const N_BITS: usize = 174;
const N_CHECKS: usize = 83;

/// Precomputed check-to-bits and bit-to-checks adjacency.
struct LdpcGraph {
    checks:    [[usize; 7]; N_CHECKS],
    check_deg: [usize; N_CHECKS],
    bit_checks: Vec<Vec<usize>>,
}

impl LdpcGraph {
    fn build() -> Self {
        let mut checks    = [[0usize; 7]; N_CHECKS];
        let mut check_deg = [0usize; N_CHECKS];
        let mut bit_checks: Vec<Vec<usize>> = vec![Vec::new(); N_BITS];

        for (c, row) in NM.iter().enumerate() {
            let mut deg = 0;
            for &idx in row.iter() {
                if idx == 0 { break; }
                let b = (idx as usize) - 1;
                checks[c][deg] = b;
                deg += 1;
                bit_checks[b].push(c);
            }
            check_deg[c] = deg;
        }
        LdpcGraph { checks, check_deg, bit_checks }
    }
}

fn unsatisfied_checks(bits: &[u8], g: &LdpcGraph) -> usize {
    let mut n = 0;
    for c in 0..N_CHECKS {
        let deg = g.check_deg[c];
        let mut xor = 0u8;
        for i in 0..deg {
            xor ^= bits[g.checks[c][i]];
        }
        if xor != 0 { n += 1; }
    }
    n
}

static GRAPH: OnceLock<LdpcGraph> = OnceLock::new();

/// atanh z obcieciem przy |x|->1 (inaczej nieskonczonosc psuje kolejne iteracje).
fn atanh_clamped(x: f32) -> f32 {
    let x = x.clamp(-0.999_999_9, 0.999_999_9);
    0.5 * ((1.0 + x) / (1.0 - x)).ln()
}

/// Decode LLRs (positive = bit likely 0, negative = bit likely 1).
/// Returns (bits, success, iterations).
pub fn bp_decode(llr_channel: &[f32; N_BITS], max_iters: usize) -> ([u8; N_BITS], bool, usize) {
    bp_decode_save(llr_channel, max_iters, &mut [])
}

/// Jak bp_decode, ale dodatkowo zapisuje do `save[i]` sumaryczne LLR bitow
/// (kanal + wiadomosci od checkow) po iteracji i+1 - wejscie dla pelnego OSD
/// (osd::osd_full), gdy BP nie zbiegnie. OSD na LLR "wygladzonych" 1-2
/// iteracjami BP trafia czesciej niz na surowych LLR z kanalu.
pub fn bp_decode_save(llr_channel: &[f32; N_BITS], max_iters: usize, save: &mut [[f32; N_BITS]]) -> ([u8; N_BITS], bool, usize) {
    // LdpcGraph::build() only depends on the constant NM matrix, never on
    // input data - it was previously rebuilt (with ~500 small heap
    // allocations for bit_checks) on EVERY call, i.e. up to 60x per decode
    // pass, every window, forever. Built once and shared read-only across
    // threads (rayon calls bp_decode concurrently per candidate) instead.
    let g = GRAPH.get_or_init(LdpcGraph::build);

    // Q[c][pos_in_check] = message from bit to check
    let mut q = [[0f32; 7]; N_CHECKS];
    let mut r = [[0f32; 7]; N_CHECKS];

    // Initialize Q from channel LLRs
    for c in 0..N_CHECKS {
        let deg = g.check_deg[c];
        for i in 0..deg {
            q[c][i] = llr_channel[g.checks[c][i]];
        }
    }

    let mut bit_total = [0f32; N_BITS];
    let mut nclast = usize::MAX;
    let mut ncnt = 0usize;

    for it in 0..max_iters {
        // Check node update: pelny sum-product (tanh), nie min-sum. Min-sum
        // zawyza wiarygodnosc wiadomosci i przy slabych sygnalach zbiega
        // gorzej - zmierzone na plikach ft8sim (patrz baseband.rs).
        for c in 0..N_CHECKS {
            let deg = g.check_deg[c];
            let mut th = [0f32; 7];
            for i in 0..deg { th[i] = (q[c][i] * 0.5).tanh(); }
            for j in 0..deg {
                let mut prod = 1.0f32;
                for k in 0..deg { if k != j { prod *= th[k]; } }
                r[c][j] = 2.0 * atanh_clamped(prod);
            }
        }

        // Bit node update: sum channel LLR + all incoming R messages
        for b in 0..N_BITS {
            let mut total = llr_channel[b];
            for &c in g.bit_checks[b].iter() {
                // Find position of b in check c
                let deg = g.check_deg[c];
                for i in 0..deg {
                    if g.checks[c][i] == b {
                        total += r[c][i];
                        break;
                    }
                }
            }
            bit_total[b] = total;
        }
        if it < save.len() { save[it] = bit_total; }

        // Hard decision
        let mut bits = [0u8; N_BITS];
        for b in 0..N_BITS {
            bits[b] = if bit_total[b] < 0.0 { 1 } else { 0 };
        }

        let ncheck = unsatisfied_checks(&bits, &g);
        if ncheck == 0 {
            return (bits, true, it + 1);
        }
        // Wczesne przerwanie beznadziejnych przypadkow: po 10 iteracjach,
        // jesli liczba niespelnionych rownan od 5 iteracji nie maleje i wciaz
        // jest > 15, BP juz nie zbiegnie (ta sama regula co w dekoderze
        // referencyjnym). Na zatloczonym pasmie wiekszosc kandydatow to takie
        // przypadki - wczesniej kazdy liczyl pelne 30 iteracji.
        if it > 0 {
            if ncheck < nclast { ncnt = 0; } else { ncnt += 1; }
            if ncnt >= 5 && it >= 10 && ncheck > 15 { break; }
        }
        nclast = ncheck;

        // Update Q (extrinsic): total - R from this check
        for c in 0..N_CHECKS {
            let deg = g.check_deg[c];
            for i in 0..deg {
                let b = g.checks[c][i];
                // Find position of this check in bit_total
                let mut extrinsic = bit_total[b];
                extrinsic -= r[c][i];
                q[c][i] = extrinsic;
            }
        }
    }

    // Non-convergence (or early stop): return last hard decision
    let mut bits = [0u8; N_BITS];
    for b in 0..N_BITS { bits[b] = if bit_total[b] < 0.0 { 1 } else { 0 }; }
    (bits, false, max_iters)
}
