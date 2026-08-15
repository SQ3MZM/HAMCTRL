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
    # Skroty Q — dorzucone (realnie uzywane w QSO/kontestach, nie tylko QRT/QSY)
    "QRV", "QSK", "QSP", "QRU", "QST", "QTC", "QSA", "QRA", "QRK",
    # Prosign BT (przerwa/separator czesci wiadomosci) — model nie ma "="
    # w zestawie znakow (patrz DEFAULT_META['chars'] w deepcw_engine.py),
    # wiec prosign wychodzi jako litery "BT".
    "BT",
    # Czeste kontrakcje/pozdrowienia koncowe CW
    "CUAGN", "HNY", "MERRY", "XMAS", "ELMER",
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
    # Dorzucone — czeste imiona operatorow spoza pierwotnej (glownie
    # anglo/niemieckiej) listy, stacja pracuje DX na wielu kontynentach.
    "JOSE", "JUAN", "CARLOS", "LUIS", "MIGUEL", "PABLO", "PEDRO", "RAUL",
    "MARIO", "GIANNI", "GIORGIO", "FRANCO", "ROBERTO", "RENATO",
    "PIOTR", "MAREK", "JUREK", "JANEK", "TOMEK", "KRZYSZTOF", "ANDRZEJ",
    "WOJCIECH", "GRZEGORZ", "PAWEL", "MIROSLAW", "ZBIGNIEW", "STANISLAW",
    "DMITRY", "VLADIMIR", "IGOR", "BORIS", "VIKTOR", "ANATOLY", "MIKHAIL",
    "TAKASHI", "HIROSHI", "KENJI", "AKIRA", "YOSHI",
    "AHMED", "MOHAMMED", "ALI", "HASSAN", "OMAR",
    "ERIK", "LARS", "SVEN", "OLE", "NIELS", "ANDERS", "BJORN",
    "WILLEM", "HENK", "PIET", "KEES", "JAN", "GERRIT",
}

# ── Cut numbers (skroty cyfr) ────────────────────────────────────────────────
CUT = {"T": "0", "N": "9", "E": "5", "A": "1", "U": "2", "V": "3",
       "4": "4", "G": "7", "D": "8", "B": "6"}

# ── Kod Morse'a — do wazenia kosztu podstawienia w _edit_dist ────────────────
# Model CTC myli litery/cyfry o PODOBNYM zapisie kropka-kreska (E "." vs T "-",
# S "..." vs O "---" to NIE jest bliskie, ale E vs T juz tak — 1 symbol roznicy).
# Plaska odleglosc Levenshteina (kazde podstawienie=1) traktowala kazda pomylke
# tak samo, wiec przy max_dist=1 poprawiala tez podstawienia ktore w realnym
# CW sa niemozliwe (np. T->O, litery o kompletnie roznym zapisie) - falszywe
# "poprawki" psuly poprawny, surowy odczyt. Kosztem podstawienia jest teraz
# odleglosc Levenshteina MIEDZY SAMYMI kodami Morse'a (dlugosc 1-5 znakow,
# maks. koszt do policzenia to ~25 porownan) — tanie, liczone raz na token
# przy korekcie tekstu, zero kosztu audio/inferencji.
MORSE_CODE = {
    "A": ".-",    "B": "-...",  "C": "-.-.",  "D": "-..",   "E": ".",
    "F": "..-.",  "G": "--.",   "H": "....",  "I": "..",    "J": ".---",
    "K": "-.-",   "L": ".-..",  "M": "--",    "N": "-.",    "O": "---",
    "P": ".--.",  "Q": "--.-",  "R": ".-.",   "S": "...",   "T": "-",
    "U": "..-",   "V": "...-",  "W": ".--",   "X": "-..-",  "Y": "-.--",
    "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
}


def _morse_edit(a: str, b: str) -> int:
    """Zwykla odleglosc Levenshteina miedzy dwoma ciagami kropek/kresek
    (kody Morse'a, dlugosc 1-5) — pomocnicza dla _sub_cost, NIE dla tekstu."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i-1] == b[j-1] else 1
            cur[j] = min(prev[j] + 1, cur[j-1] + 1, prev[j-1] + cost)
        prev = cur
    return prev[lb]


def _build_sub_cost_table() -> dict:
    """Prekomputuj koszt podstawienia dla kazdej pary znakow raz przy imporcie
    (36x36 par) — w _edit_dist jest to potem zwykle O(1) odczyt ze slownika,
    szybsze niz liczenie _morse_edit na kazdej komorce DP."""
    chars = list(MORSE_CODE.keys())
    table = {}
    for x in chars:
        for y in chars:
            table[(x, y)] = max(1, _morse_edit(MORSE_CODE[x], MORSE_CODE[y]))
    return table


_SUB_COST = _build_sub_cost_table()


def _sub_cost(a: str, b: str) -> int:
    """Koszt podstawienia znaku a->b. Morse-bliskie znaki (np. E<->T) kosztuja
    1 (jak dawniej plaska odleglosc), Morse-dalekie wiecej — wiec przy tym
    samym max_dist mniej falszywych "poprawek" miedzy niepodobnymi znakami,
    a prawdziwe pomylki modelu (jeden symbol Morse'a roznicy) nadal przechodza."""
    if a == b:
        return 0
    return _SUB_COST.get((a, b), 1)  # fallback dla spacji/znakow spoza A-Z0-9


def _edit_dist(a: str, b: str) -> int:
    """Odleglosc edycyjna wazona kosztem Morse'a dla podstawien (male ciagi,
    prosta implementacja DP). Insercja/usuniecie znaku kosztuje 1 jak dawniej —
    to nie jest "pomylka miedzy dwoma znakami", tylko caly znak za duzo/za malo."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > 2:      # optymalizacja: za duza roznica dlugosci
        return 99
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = _sub_cost(a[i-1], b[j-1])
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


def _segment(token: str, known_calls: set | None = None) -> str | None:
    """Rozdziel sklejony ciag na znane slowa CW, zaznane znaki i raporty RST.

    Model przy szybkim CW nie wstawia spacji (widzi ciag znakow bez wyraznych
    przerw), wiec zwraca np. 'TKSFERFB' zamiast 'TKS FER FB'. Probujemy podzielic
    token na kolejne slowa ze slownika (albo znany znak wywolawczy, albo raport
    RST doklejony bez spacji — patrz nizej). Zwraca podzial tylko gdy CALY token
    da sie pokryc rozpoznanymi kawalkami — inaczej None (nie zgadujemy).
    """
    t = token.upper()
    if len(t) < 4 or t in CW_WORDS:
        return None
    known_calls = known_calls or set()
    # Programowanie dynamiczne: czy da sie pokryc t[i:] rozpoznanymi kawalkami
    n = len(t)
    # dp[i] = lista kawalkow pokrywajacych t[i:], albo None
    dp: list = [None] * (n + 1)
    dp[n] = []
    for i in range(n - 1, -1, -1):
        # a) slowo ze slownika CW_WORDS
        for w in CW_WORDS:
            lw = len(w)
            if lw >= 2 and t[i:i+lw] == w and dp[i+lw] is not None:
                dp[i] = [w] + dp[i+lw]
                break
        if dp[i] is not None:
            continue
        # b) znany znak wywolawczy doklejony do sasiedniego slowa, np.
        # "DESQ3MZM" -> "DE SQ3MZM". Dlugosc znaku jest zmienna (3-10), wiec
        # probujemy kolejne dlugosci od najdluzszej zamiast iterowac caly
        # (potencjalnie duzy) zbior known_calls — to zwykle O(10) sprawdzen
        # przynaleznosci do zbioru (O(1) kazde), nie O(|known_calls|).
        if known_calls:
            for lw in range(min(10, n - i), 2, -1):
                cand = t[i:i+lw]
                if cand in known_calls and dp[i+lw] is not None:
                    dp[i] = [cand] + dp[i+lw]
                    break
        if dp[i] is not None:
            continue
        # c) raport RST doklejony bez spacji, np. "5NNTU" -> "5NN TU" —
        # bardzo czesty wzorzec (operator wysyla raport i od razu "TU" bez
        # przerwy). _is_report jest juz konserwatywny (tylko rozpoznaje
        # sensowny ksztalt raportu, niczego nie wymysla), wiec bezpiecznie
        # uzyc go tu jako zrodla "znanego slowa" dlugosci 3.
        if i + 3 <= n and _is_report(t[i:i+3]) and dp[i+3] is not None:
            dp[i] = [t[i:i+3]] + dp[i+3]
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
        # max_dist=3 (bylo 2 pod plaska odleglosc) — budzet 2 byl skalibrowany
        # gdy KAZDE podstawienie kosztowalo dokladnie 1. Teraz podstawienie
        # miedzy odleglymi znakami kosztuje wiecej (patrz _sub_cost), wiec ten
        # sam przyklad co w naglowku pliku (KEITH -> KEHTHA: I->H to 2 kropki
        # roznicy + wstawione A) potrzebuje budzetu 3, inaczej NIE zlapalby
        # sie wlasny przyklad autora. _best_match i tak odrzuca remisy, wiec
        # wiekszy budzet nie oznacza wiecej falszywych trafien, tylko wiecej
        # SZANS na jednoznaczne.
        if prev_upper in ("NAME", "OP", "OM") and len(T) >= 3:
            m = _best_match(T, CW_NAMES, max_dist=3)
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
        seg = _segment(T, known_calls)
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
