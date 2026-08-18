"""
Stage 3: LDPC(174,91) belief propagation decoder (min-sum algorithm).

Uses the parity-check matrix Nm (83 parity checks x 174 bits, each bit
appears in exactly 3 parity checks) which already exists in ft8_encoder.py
as _NM (used there only for the _ldpc_check self-test). Indices in _NM are
1-based with 0 as padding (no connection).

Algorithm: min-sum belief propagation (a simplified, numerically stable
variant of sum-product), iteratively refining soft LLRs by passing messages
between bit nodes and check nodes until a consistent (all parity=0)
codeword is found or iterations are exhausted.
"""
import numpy as np
import ft8_encoder as fe

# Build (check_idx -> [bit_idx,...]) and (bit_idx -> [check_idx,...])
# from _NM (1-based, 0=padding), converting to 0-based.
_CHECKS = []  # list of bit-index lists (0-based) for each parity check
for row in fe._NM:
    bits = [i - 1 for i in row if i != 0]
    _CHECKS.append(bits)

N_CHECKS = len(_CHECKS)   # 83
N_BITS = 174

_BIT_TO_CHECKS = [[] for _ in range(N_BITS)]
for c_idx, bits in enumerate(_CHECKS):
    for b in bits:
        _BIT_TO_CHECKS[b].append(c_idx)

# All bits should have degree=3 (verified earlier)
_DEGREES = [len(_BIT_TO_CHECKS[b]) for b in range(N_BITS)]


def ldpc_check_llr(bits174):
    """Checks whether the given hard bits (0/1, list of 174) satisfy all parity checks."""
    for bits in _CHECKS:
        x = 0
        for i in bits:
            x ^= bits174[i]
        if x != 0:
            return False
    return True


def bp_decode(llr_channel, max_iters=50):
    """
    Min-sum belief propagation.
    llr_channel: 174 channel LLR values (positive = bit=0 more likely,
        matching demod.extract_llr174's convention).
    Returns: (bits174_hard, success, n_iters)
        success=True if a codeword satisfying all parity checks was found.
    """
    llr_channel = np.asarray(llr_channel, dtype=np.float64)

    # Messages bit->check (Q) and check->bit (R), indexed (check_idx, bit_idx)
    # Initialization: Q = channel LLR
    Q = {}  # (check_idx, bit_idx) -> value
    for c_idx, bits in enumerate(_CHECKS):
        for b in bits:
            Q[(c_idx, b)] = llr_channel[b]

    R = {}

    for it in range(max_iters):
        # --- Check node update (min-sum) ---
        for c_idx, bits in enumerate(_CHECKS):
            vals = [Q[(c_idx, b)] for b in bits]
            signs = [1.0 if v >= 0 else -1.0 for v in vals]
            mags = [abs(v) for v in vals]
            total_sign = 1.0
            for s in signs:
                total_sign *= s
            for j, b in enumerate(bits):
                # min magnitude excluding position j
                other_mags = mags[:j] + mags[j+1:]
                other_signs = signs[:j] + signs[j+1:]
                min_mag = min(other_mags) if other_mags else 0.0
                sign_prod = 1.0
                for s in other_signs:
                    sign_prod *= s
                R[(c_idx, b)] = sign_prod * min_mag

        # --- Bit node update ---
        bit_total = np.zeros(N_BITS, dtype=np.float64)
        for b in range(N_BITS):
            total = llr_channel[b]
            for c_idx in _BIT_TO_CHECKS[b]:
                total += R[(c_idx, b)]
            bit_total[b] = total

        # Hard decision + early termination check
        hard = (bit_total < 0).astype(np.int32)  # LLR<0 -> bit=1 (convention: positive=bit0)
        if ldpc_check_llr(hard.tolist()):
            return hard.tolist(), True, it + 1

        # Update Q for the next iteration: total - R(this check) (extrinsic)
        for c_idx, bits in enumerate(_CHECKS):
            for b in bits:
                Q[(c_idx, b)] = bit_total[b] - R[(c_idx, b)]

    # Did not converge - return the last hard decision
    bit_total = np.zeros(N_BITS, dtype=np.float64)
    for b in range(N_BITS):
        total = llr_channel[b]
        for c_idx in _BIT_TO_CHECKS[b]:
            total += R.get((c_idx, b), 0.0)
        bit_total[b] = total
    hard = (bit_total < 0).astype(np.int32)
    return hard.tolist(), False, max_iters
