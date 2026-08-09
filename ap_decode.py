"""
AP (a-priori) decoding dla FT8 — celowany, nie brute force.
Odzyskuje slabe sygnaly gdy kontekst (znaki) jest znany.
Trzy zrodla hipotez:
  A) aktywne QSO: para (nasz_znak, klikniety_partner)
  B) ktos nas wola: call_to = nasz_znak
  C) kontekst pasma: znaki widziane w poprzednich oknach
"""
import numpy as np
import re
from demod import extract_llr174
from ldpc_decode import bp_decode
from unpack import unpack77, format_message
import ft8_encoder as enc


def _bits174(call_to, call_de, rpt):
    """Wiadomosc -> 174 bity (przez pack77 + crc14 + ldpc_encode)."""
    b = enc.pack77(call_to, call_de, rpt)
    c = enc._crc14(b + [0] * 5)
    a91 = b + list(c[:14])
    return np.array(enc._ldpc_encode(np.array(a91[:91], dtype=np.int32))[:174]).astype(int)


def _check_crc(hard):
    c = enc._crc14(list(hard[:77]) + [0] * 5)
    return list(c[:14]) == list(hard[77:91])


# raporty najczestsze w FT8 QSO (kolejnosc = priorytet prob)
_COMMON_REPORTS = ['-01', '-05', '-10', '-15', 'R-01', 'R-05', 'R-10',
                   'RR73', '73', '+00', '-03', '-08', '-12', '-18']


def ap_try_candidate(power, hypotheses, prior_strength=6.0, max_iters=60):
    """
    Dla jednego kandydata (jego 'power' z extract_tone_power), probuje
    listy hipotez (call_to, call_de). Zwraca (msg, i3) pierwszej ktora
    zdekoduje z poprawnym CRC i pasuje do hipotezy, albo None.
    Raport iterowany z _COMMON_REPORTS (bo raportu zwykle nie znamy).
    """
    llr_ch = extract_llr174(power)
    for (to, de) in hypotheses:
        for rpt in _COMMON_REPORTS:
            try:
                full = _bits174(to, de, rpt)
            except Exception:
                continue
            prior = np.zeros(174)
            # prior tylko na bity ZNAKOW [0:57] (raport zostawiamy kanalowi)
            prior[:57] = np.where(full[:57] == 0, prior_strength, -prior_strength)
            hard, ok, it = bp_decode(llr_ch + prior, max_iters=max_iters)
            if not ok or not _check_crc(hard):
                continue
            msg = format_message(unpack77(list(hard[:77]))).strip()
            # WALIDACJA: dekod musi zawierac hipotezowane znaki
            if de in msg and (to in msg or to == 'CQ'):
                return msg
    return None


def build_band_context(decoded_messages):
    """
    Tryb C: z wiadomosci juz zdekodowanych wyciaga zbior znakow (kontekst pasma).
    """
    calls = set()
    for msg in decoded_messages:
        for tok in msg.split():
            if tok in ('CQ', 'DE', 'QRZ', 'RR73', 'RRR', '73'):
                continue
            if re.match(r'^R?[+-]?\d+$', tok):
                continue
            if re.match(r'^[A-R]{2}\d{2}([a-x]{2})?$', tok):
                continue
            if tok.startswith('<') or '...' in tok:
                continue
            calls.add(tok)
    return calls


def make_hypotheses_band(known_calls, max_pairs=40):
    """
    Tryb C: buduje CELOWANA liste hipotez par ze znanych znakow.
    NIE wszystkie pary (to timeout) — priorytetyzuje:
      1. CQ od znanego znaku (ktos moze odpowiadac)
      2. pary gdzie oba znaki znane (trwajace QSO)
    Ogranicza do max_pairs najsensowniejszych.
    """
    calls = list(known_calls)
    hyps = []
    # 1. CQ od kazdego znanego (ktos wola znany znak)
    for de in calls:
        hyps.append(('CQ', de))
    # 2. pary znanych (odpowiedz w QSO) — ograniczone
    # priorytet: pary ktore realnie moglyby rozmawiac (heurystyka: wszystkie,
    # ale limitowane). W produkcie: pary z faktycznie zaobserwowanych QSO.
    for i, de in enumerate(calls):
        for to in calls:
            if to == de:
                continue
            hyps.append((to, de))
            if len(hyps) >= max_pairs:
                return hyps
    return hyps


def make_hypotheses_qso(my_calls, partner):
    """
    Tryb A: aktywne QSO. Bardzo waski, najbezpieczniejszy.
    Para (nasz_znak, partner) w obu kierunkach.
    """
    hyps = []
    for my in my_calls:
        hyps.append((my, partner))   # my wolamy partnera / on nam odpowiada
        hyps.append((partner, my))   # on wola nas
    return hyps


def make_hypotheses_calling_us(my_calls):
    """
    Tryb B: ktos nas wola. call_to = nasz znak, call_de nieznany.
    Zwraca hipotezy z naszym znakiem jako call_to (de='CQ' placeholder
    nie zadziala — w tym trybie prior tylko na call_to [0:28]).
    """
    # ten tryb wymaga innego priora (tylko call_to) — obslugiwany osobno
    return [(my, None) for my in my_calls]
