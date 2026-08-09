"""
Etap 3: LDPC(174,91) belief propagation decoder (min-sum algorithm).

Uzywa macierzy parzystosci Nm (83 parity checks x 174 bity, kazdy bit w
dokladnie 3 parity-checkach) ktora jest juz obecna w ft8_encoder.py jako
_NM (tam uzywana tylko do self-testu _ldpc_check). Indeksy w _NM sa
1-based z 0 jako padding (brak polaczenia).

Algorytm: min-sum belief propagation (uproszczona, numerycznie stabilna
wersja sum-product), iteracyjnie poprawia miekkie LLR-y wymieniajac
wiadomosci miedzy wezlami bitowymi a wezlami parzystosci, az do
osiagniecia spojnego (wszystkie parity=0) codeworda lub wyczerpania
iteracji.
"""
import numpy as np
import ft8_encoder as fe

# Budujemy liste (check_idx -> [bit_idx,...]) i (bit_idx -> [check_idx,...])
# z _NM (1-based, 0=padding), konwertujac na 0-based.
_CHECKS = []  # lista list bit-indeksow (0-based) dla kazdego parity check
for row in fe._NM:
    bits = [i - 1 for i in row if i != 0]
    _CHECKS.append(bits)

N_CHECKS = len(_CHECKS)   # 83
N_BITS = 174

_BIT_TO_CHECKS = [[] for _ in range(N_BITS)]
for c_idx, bits in enumerate(_CHECKS):
    for b in bits:
        _BIT_TO_CHECKS[b].append(c_idx)

# Wszystkie bity powinny miec degree=3 (zweryfikowane wczesniej)
_DEGREES = [len(_BIT_TO_CHECKS[b]) for b in range(N_BITS)]


def ldpc_check_llr(bits174):
    """Sprawdza czy podane twarde bity (0/1, lista 174) spelniaja wszystkie parity checks."""
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
    llr_channel: 174 wartosci LLR z kanalu (dodatni = bit=0 bardziej
        prawdopodobny, zgodnie z konwencja demod.extract_llr174).
    Zwraca: (bits174_hard, success, n_iters)
        success=True jesli znaleziono codeword spelniajacy wszystkie parity checks.
    """
    llr_channel = np.asarray(llr_channel, dtype=np.float64)

    # Wiadomosci bit->check (Q) i check->bit (R), indeksowane (check_idx, pozycja_w_check)
    # Inicjalizacja: Q = llr kanalu
    Q = {}  # (check_idx, bit_idx) -> wartosc
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
        hard = (bit_total < 0).astype(np.int32)  # LLR<0 -> bit=1 (konwencja: dodatni=bit0)
        if ldpc_check_llr(hard.tolist()):
            return hard.tolist(), True, it + 1

        # Update Q dla nastepnej iteracji: total - R(tego checka) (extrinsic)
        for c_idx, bits in enumerate(_CHECKS):
            for b in bits:
                Q[(c_idx, b)] = bit_total[b] - R[(c_idx, b)]

    # Brak zbieznosci - zwroc ostatnia twarda decyzje
    bit_total = np.zeros(N_BITS, dtype=np.float64)
    for b in range(N_BITS):
        total = llr_channel[b]
        for c_idx in _BIT_TO_CHECKS[b]:
            total += R.get((c_idx, b), 0.0)
        bit_total[b] = total
    hard = (bit_total < 0).astype(np.int32)
    return hard.tolist(), False, max_iters
