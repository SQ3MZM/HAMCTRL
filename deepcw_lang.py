"""
deepcw_lang.py — warstwa jezykowa dekodera CW

Poprawia wyjscie modelu tak, jak robi to CW Skimmer: wykorzystuje wiedze
o STRUKTURZE lacznosci CW. Model czyta znak po znaku bez kontekstu, wiec
myli podobne litery (KEITH -> KEHTHA). Warstwa jezykowa wie, ze:
  - istnieje skonczony zbior typowych zwrotow (UR, TU, TKS, QSO, RST...),
  - raporty maja ustalony format (599, 5NN),
  - po NAME/OP idzie imie,
  - znaki maja strukture prefiks+cyfra+sufiks.

Dziala WYLACZNIE na tekscie, zero kosztu audio. Poprawia tylko tam, gdzie
dopasowanie jest pewne — przy watpliwosci zostawia oryginal (lepiej surowy
odczyt niz bledna "poprawka").
"""
from __future__ import annotations

# ── Typowe zwroty CW (prosign, skroty Q, slowa robocze) ──────────────────────
# Zbior zamkniety — model czesto zwraca ich przekrecone wersje.
CW_WORDS = {
    # Wywolania i zakonczenia
    "CQ", "DE", "K", "KN", "AR", "SK", "BK", "R", "RR", "RRR", "AS",
    # Skroty Q
    "QRZ", "QTH", "QSL", "QRM", "QRN", "QSB", "QRP", "QRO", "QSY", "QRT",
    "QSO", "QRX", "QRL", "QRG", "QRQ", "QRS",
    # Slowa robocze
    "UR", "URS", "TU", "TKS", "TNX", "THX", "FB", "HR", "HW", "PSE", "PWR",
    "RST", "RIG", "ANT", "WX", "TEMP", "NAME", "OP", "QTH", "AGN", "CFM",
    "GM", "GA", "GE", "GN", "GD", "DR", "OM", "YL", "XYL", "ES", "NW",
    "BTU", "WID", "ABT", "VY", "GUD", "GL", "HPE", "CUL", "CU", "SRI",
    "WKD", "WKG", "RCVR", "RX", "TX", "TRX", "WATT", "WATTS", "MTR", "MTRS",
    "DPL", "VERT", "BEAM", "LOOP", "GND", "COAX", "KEY", "BUG", "PADDLE",
    "SIG", "SIGS", "RPT", "RPRT", "SED", "SND", "SENT", "RCVD", "CPY", "COPY",
    "SOLID", "PART", "OK", "NO", "YES", "TEST", "CONTEST", "DX", "SASE",
    "CARD", "BURO", "DIRECT", "VIA", "MNI", "TMW", "TDY", "MORN", "EVE",
    "NITE", "DAY", "SUN", "RAIN", "SNOW", "CLDY", "HOT", "CLD", "WARM",
    # Zwroty konwersacyjne i pogodowe (czeste w QSO, model je skleja)
    "FER", "FINE", "BIZ", "FB", "SUNNY", "CLOUDY", "OVERCAST", "COOL",
    "MILD", "WINDY", "FOGGY", "CLEAR", "WET", "DRY", "DEG", "DEGS", "DEGREES",
    "TEMP", "CELSIUS", "FAHR", "PSE", "CPI", "CPY", "AGN", "AGE", "YRS",
    "OLD", "YOUNG", "WIFE", "SON", "DTR", "FAMILY", "JOB", "WORK", "RETIRED",
    "STUDENT", "HAM", "LIC", "LICENSE", "FIRST", "SECOND", "YEAR", "MONTH",
    "WEEK", "HOUR", "MIN", "SEC", "TIME", "LOCAL", "UTC", "ANT", "TOWER",
    "HEIGHT", "FT", "MTRS", "ELEMENTS", "WATTS", "OUTPUT", "INPUT", "FINALS",
    "TUBE", "SOLID", "STATE", "HOMEBREW", "COMMERCIAL", "MADE",
    "73", "88", "72", "99",
}

# ── Typowe imiona (po NAME / OP) ─────────────────────────────────────────────
CW_NAMES = {
    "JOHN", "JIM", "TOM", "BOB", "BILL", "DAVE", "MIKE", "STEVE", "PAUL",
    "PETE", "DAN", "KEN", "KEITH", "CARL", "FRED", "GARY", "JACK", "JOE",
    "LARRY", "MARK", "RALPH", "RAY", "RICK", "RON", "ROY", "SAM", "TED",
    "WALT", "GENE", "AL", "ED", "HANK", "LOU", "MAX", "PHIL", "STAN",
    "HANS", "KURT", "FRITZ", "HANS", "PETER", "KLAUS", "WERNER", "DIETER",
    "ANDRE", "PIERRE", "JEAN", "LUC", "MARCO", "LUIGI", "ANTON", "PAVEL",
    "IVAN", "YURI", "SERGE", "OLEG", "NICK", "ALEX", "VIC", "WALLY",
    "ANDY", "CHRIS", "DENNIS", "DON", "DOUG", "FRANK", "GEORGE", "GREG",
    "HARRY", "JERRY", "LEO", "LES", "NORM", "RUSS", "SCOTT", "WAYNE",
}

# ── Cut numbers (skroty cyfr) ────────────────────────────────────────────────
CUT = {"T": "0", "N": "9", "E": "5", "A": "1", "U": "2", "V": "3",
       "4": "4", "G": "7", "D": "8", "B": "6"}


def _edit_dist(a: str, b: str) -> int:
    """Odleglosc Levenshteina (male ciagi, prosta implementacja DP)."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > 2:      # optymalizacja: za duza roznica dlugosci
        return 99
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i-1] == b[j-1] else 1
            cur[j] = min(prev[j] + 1, cur[j-1] + 1, prev[j-1] + cost)
        prev = cur
    return prev[lb]


def _best_match(token: str, vocab: set, max_dist: int = 1) -> str | None:
    """Znajdz slowo ze slownika najblizsze tokenowi (odleglosc <= max_dist).

    Zwraca dopasowanie tylko gdy JEDNOZNACZNE — przy remisie None.
    """
    t = token.upper()
    if t in vocab:
        return t
    best, best_d, ties = None, max_dist + 1, 0
    for w in vocab:
        # Szybkie odrzucenie: przy max_dist=1 slowa roznia sie dlugoscia o >1
        # albo pierwsza litera nie pasuje -> odleglosc na pewno > max_dist.
        # Pomija to kosztowne liczenie DP dla oczywiscie roznych slow.
        if abs(len(w) - len(t)) > max_dist:
            continue
        if max_dist == 1 and w[0] != t[0] and w[-1] != t[-1]:
            continue
        d = _edit_dist(t, w)
        if d < best_d:
            best, best_d, ties = w, d, 1
        elif d == best_d:
            ties += 1
    if best and best_d <= max_dist and ties == 1:
        return best
    return None


def _is_report(tok: str) -> str | None:
    """Rozpoznaj i znormalizuj raport RST. Zwraca poprawiona forme lub None."""
    t = tok.upper()
    # Rozwin cut numbers do postaci cyfrowej
    digits = "".join(CUT.get(c, c) for c in t)
    if not digits.isdigit():
        return None
    # Typowe raporty: 599, 579, 559, 339... (3 cyfry, srodkowa nieparzysta R-S-T)
    if len(digits) == 3 and digits[0] in "12345" and digits[2] in "9":
        return tok  # juz sensowny raport, zostaw jak nadano (moze byc 5NN)
    return None


def _segment(token: str) -> str | None:
    """Rozdziel sklejony ciag na znane slowa CW.

    Model przy szybkim CW nie wstawia spacji (widzi ciag znakow bez wyraznych
    przerw), wiec zwraca np. 'TKSFERFB' zamiast 'TKS FER FB'. Probujemy podzielic
    token na kolejne slowa ze slownika. Zwraca podzial tylko gdy CALY token da
    sie pokryc znanymi slowami — inaczej None (nie zgadujemy).
    """
    t = token.upper()
    if len(t) < 4 or t in CW_WORDS:
        return None
    # Programowanie dynamiczne: czy da sie pokryc t[i:] znanymi slowami
    n = len(t)
    # dp[i] = lista slow pokrywajacych t[i:], albo None
    dp: list = [None] * (n + 1)
    dp[n] = []
    for i in range(n - 1, -1, -1):
        for w in CW_WORDS:
            lw = len(w)
            if lw >= 2 and t[i:i+lw] == w and dp[i+lw] is not None:
                dp[i] = [w] + dp[i+lw]
                break
    if dp[0] is not None and len(dp[0]) >= 2:
        return " ".join(dp[0])
    return None


def correct(text: str, known_calls: set | None = None) -> str:
    """Popraw tekst z dekodera wykorzystujac strukture lacznosci CW.

    known_calls: pula realnych znakow (z FT8/logu/clustera) do walidacji.
    """
    if not text:
        return text
    known_calls = known_calls or set()
    tokens = text.split(" ")
    out = []
    prev_upper = ""

    for tok in tokens:
        if not tok:
            continue
        T = tok.upper()

        # 1. Raport RST — normalizacja formatu
        rep = _is_report(T)
        if rep is not None:
            out.append(rep)
            prev_upper = rep
            continue

        # 2. Po NAME / OP — sprobuj dopasowac imie
        if prev_upper in ("NAME", "OP", "OM") and len(T) >= 3:
            m = _best_match(T, CW_NAMES, max_dist=2)
            if m:
                out.append(m)
                prev_upper = m
                continue

        # 3. Znak wywolawczy — walidacja baza znanych (jesli podana)
        if known_calls and _looks_like_call(T):
            if T in known_calls:
                out.append(T)
                prev_upper = T
                continue
            # Dopasowanie do bazy tylko dla tokenow o SENSOWNEJ strukturze —
            # inaczej kazdy smiec (R3BDR, OKOK5I9) uruchamialby kosztowne
            # liczenie odleglosci do calej bazy, zapychajac procesor przy
            # dluzszych liniach.
            m = _best_match(T, known_calls, max_dist=1)
            if m:
                out.append(m)
                prev_upper = m
                continue

        # 4. Typowy zwrot CW — dopasuj do slownika
        if len(T) >= 2:
            m = _best_match(T, CW_WORDS, max_dist=1)
            if m:
                out.append(m)
                prev_upper = m
                continue

        # 5. Sklejone slowa — sprobuj rozdzielic wg slownika (TKSFER -> TKS FER)
        seg = _segment(T)
        if seg:
            out.append(seg)
            prev_upper = seg.split()[-1]
            continue

        # 6. Nic nie pasuje — zostaw oryginal
        out.append(tok)
        prev_upper = T

    return " ".join(out)


def _looks_like_call(t: str) -> bool:
    """Czy token wyglada jak znak wywolawczy (litera + cyfra + litery)."""
    if not (3 <= len(t) <= 10):
        return False
    return any(c.isdigit() for c in t) and any(c.isalpha() for c in t)
