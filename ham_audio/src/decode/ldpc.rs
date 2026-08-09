/// LDPC(174,91) belief propagation decoder — min-sum algorithm.
/// Direct port of ldpc_decode.py, optimized for Rust.

use super::params::NM;

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

fn parity_ok(bits: &[u8], g: &LdpcGraph) -> bool {
    for c in 0..N_CHECKS {
        let deg = g.check_deg[c];
        let mut xor = 0u8;
        for i in 0..deg {
            xor ^= bits[g.checks[c][i]];
        }
        if xor != 0 { return false; }
    }
    true
}

/// Decode LLRs (positive = bit likely 0, negative = bit likely 1).
/// Returns (bits, success, iterations).
pub fn bp_decode(llr_channel: &[f32; N_BITS], max_iters: usize) -> ([u8; N_BITS], bool, usize) {
    let g = LdpcGraph::build();

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

    for it in 0..max_iters {
        // Check node update (min-sum)
        for c in 0..N_CHECKS {
            let deg = g.check_deg[c];
            // Collect values
            let mut vals = [0f32; 7];
            for i in 0..deg { vals[i] = q[c][i]; }

            // Total sign product
            let total_sign: f32 = vals[..deg].iter().map(|v| if *v >= 0.0 { 1.0 } else { -1.0 }).product();

            for j in 0..deg {
                let sign_j = if vals[j] >= 0.0 { 1.0f32 } else { -1.0 };
                let sign_others = total_sign * sign_j; // remove j-th sign

                // Min magnitude excluding j
                let min_mag = vals[..deg]
                    .iter()
                    .enumerate()
                    .filter(|&(k, _)| k != j)
                    .map(|(_, v)| v.abs())
                    .fold(f32::INFINITY, f32::min);

                r[c][j] = sign_others * min_mag;
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

        // Hard decision
        let mut bits = [0u8; N_BITS];
        for b in 0..N_BITS {
            bits[b] = if bit_total[b] < 0.0 { 1 } else { 0 };
        }

        if parity_ok(&bits, &g) {
            return (bits, true, it + 1);
        }

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

    // Non-convergence: return last hard decision
    let mut bits = [0u8; N_BITS];
    for b in 0..N_BITS { bits[b] = if bit_total[b] < 0.0 { 1 } else { 0 }; }
    (bits, false, max_iters)
}
