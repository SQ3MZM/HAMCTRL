"""
deepcw_lang.py — CW decoder language layer

Improves the model's output using knowledge of CW QSO STRUCTURE. The
model reads character by character with no context, so it confuses
similar letters (KEITH -> KEHTHA). The language layer knows that:
  - there's a finite set of typical phrases (UR, TU, TKS, QSO, RST...),
  - reports follow a fixed format (599, 5NN),
  - a name follows NAME/OP,
  - callsigns have a prefix+digit+suffix structure.

Operates PURELY on text, zero audio cost. Only corrects where the match
is certain — when in doubt it leaves the original (a raw reading is
better than a wrong "correction").
"""
from __future__ import annotations

# ── Common CW phrases (prosigns, Q-codes, working words) ─────────────────────
# A closed set — the model often returns garbled versions of these.
CW_WORDS = {
    # Calls and sign-offs
    "CQ", "DE", "K", "KN", "AR", "SK", "BK", "R", "RR", "RRR", "AS",
    # Q-codes
    "QRZ", "QTH", "QSL", "QRM", "QRN", "QSB", "QRP", "QRO", "QSY", "QRT",
    "QSO", "QRX", "QRL", "QRG", "QRQ", "QRS",
    # Q-codes — added (genuinely used in QSOs/contests, not just QRT/QSY)
    "QRV", "QSK", "QSP", "QRU", "QST", "QTC", "QSA", "QRA", "QRK",
    # BT prosign (a break/separator between message parts) — the model has
    # no "=" in its character set (see DEFAULT_META['chars'] in
    # deepcw_engine.py), so the prosign comes out as the letters "BT".
    "BT",
    # Common closing contractions/greetings in CW
    "CUAGN", "HNY", "MERRY", "XMAS", "ELMER",
    # Working words
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
    # Conversational/weather phrases (common in QSOs, the model glues them together)
    "FER", "FINE", "BIZ", "FB", "SUNNY", "CLOUDY", "OVERCAST", "COOL",
    "MILD", "WINDY", "FOGGY", "CLEAR", "WET", "DRY", "DEG", "DEGS", "DEGREES",
    "TEMP", "CELSIUS", "FAHR", "PSE", "CPI", "CPY", "AGN", "AGE", "YRS",
    "OLD", "YOUNG", "WIFE", "SON", "DTR", "FAMILY", "JOB", "WORK", "RETIRED",
    "STUDENT", "HAM", "LIC", "LICENSE", "FIRST", "SECOND", "YEAR", "MONTH",
    "WEEK", "HOUR", "MIN", "SEC", "TIME", "LOCAL", "UTC", "ANT", "TOWER",
    "HEIGHT", "FT", "MTRS", "ELEMENTS", "WATTS", "OUTPUT", "INPUT", "FINALS",
    "TUBE", "SOLID", "STATE", "HOMEBREW", "COMMERCIAL", "MADE",
    # Numeric greetings/sign-offs — 73/88/72/99 are standard, "44" is a
    # greeting/sign-off used in park activations (POTA/SOTA).
    "73", "88", "72", "99", "44",
}

# ── Common names (after NAME / OP) ───────────────────────────────────────────
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
    # Added — common operator names outside the original (mostly
    # Anglo/German) list, since the station works DX across many continents.
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

# ── Cut numbers ───────────────────────────────────────────────────────────────
CUT = {"T": "0", "N": "9", "E": "5", "A": "1", "U": "2", "V": "3",
       "4": "4", "G": "7", "D": "8", "B": "6"}

# ── Morse code — for weighting substitution cost in _edit_dist ───────────────
# The CTC model confuses letters/digits with a SIMILAR dot-dash pattern (E "."
# vs T "-", S "..." vs O "---" is NOT close, but E vs T is — 1 symbol
# difference). A flat Levenshtein distance (every substitution=1) treated
# every mistake the same, so at max_dist=1 it also "corrected" substitutions
# that are impossible in real CW (e.g. T->O, letters with a completely
# different pattern) - these false "corrections" broke an otherwise correct
# raw reading. The substitution cost is now the Levenshtein distance BETWEEN
# THE MORSE CODES THEMSELVES (length 1-5 characters, at most ~25 comparisons
# to compute) — cheap, computed once per token during text correction, zero
# audio/inference cost.
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
    """Plain Levenshtein distance between two dot/dash strings (Morse
    codes, length 1-5) — a helper for _sub_cost, NOT for text."""
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
    """Precompute the substitution cost for every character pair once at
    import time (36x36 pairs) — in _edit_dist this then becomes a simple
    O(1) dict lookup, faster than computing _morse_edit for every DP cell."""
    chars = list(MORSE_CODE.keys())
    table = {}
    for x in chars:
        for y in chars:
            table[(x, y)] = max(1, _morse_edit(MORSE_CODE[x], MORSE_CODE[y]))
    return table


_SUB_COST = _build_sub_cost_table()


def _sub_cost(a: str, b: str) -> int:
    """Cost of substituting character a->b. Morse-close characters (e.g.
    E<->T) cost 1 (like the old flat distance), Morse-far ones cost more —
    so at the same max_dist there are fewer false "corrections" between
    dissimilar characters, while real model mistakes (one Morse symbol of
    difference) still go through."""
    if a == b:
        return 0
    return _SUB_COST.get((a, b), 1)  # fallback for spaces/characters outside A-Z0-9


def _edit_dist(a: str, b: str) -> int:
    """Edit distance weighted by Morse cost for substitutions (small
    strings, simple DP implementation). Insertion/deletion of a character
    still costs 1 as before — that's not "a mistake between two
    characters", just a whole character too many/too few."""
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if abs(la - lb) > 2:      # optimization: length difference too large
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
    """Find the word in the vocabulary closest to the token (distance <= max_dist).

    Returns a match only when it's UNAMBIGUOUS — on a tie, returns None.
    """
    t = token.upper()
    if t in vocab:
        return t
    best, best_d, ties = None, max_dist + 1, 0
    for w in vocab:
        # Quick rejection: at max_dist=1, words differing in length by >1,
        # or whose first/last letter doesn't match, are guaranteed to have
        # a distance > max_dist. Skips the expensive DP computation for
        # obviously different words.
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
    """Recognize and normalize an RST report. Returns the corrected form or None."""
    t = tok.upper()
    # Expand cut numbers to digit form
    digits = "".join(CUT.get(c, c) for c in t)
    if not digits.isdigit():
        return None
    # Typical reports: 599, 579, 559, 339... (3 digits, odd middle R-S-T)
    if len(digits) == 3 and digits[0] in "12345" and digits[2] in "9":
        return tok  # already a sensible report, leave it as sent (may be 5NN)
    return None


def _longest_piece_at(t: str, i: int, known_calls: set) -> str | None:
    """The longest recognized piece starting at t[i:] — a CW word, a known
    callsign, or an RST report. None if nothing matches."""
    n = len(t)
    best = None
    # a) a word from CW_WORDS — the longest match wins (e.g. so "FB"
    # doesn't cut off a longer "FBQSO" if both were words).
    for w in CW_WORDS:
        lw = len(w)
        if lw >= 2 and t[i:i+lw] == w and (best is None or lw > len(best)):
            best = w
    # b) a known callsign glued to an adjacent word, e.g.
    # "DESQ3MZM" -> "DE SQ3MZM". Callsign length varies (3-10), so we try
    # successive lengths from longest to shortest instead of iterating the
    # (potentially large) known_calls set — this is usually O(10)
    # membership checks (O(1) each), not O(|known_calls|).
    if known_calls:
        for lw in range(min(10, n - i), 2, -1):
            cand = t[i:i+lw]
            if cand in known_calls:
                if best is None or lw > len(best):
                    best = cand
                break
    # c) an RST report glued on with no space, e.g. "5NNTU" -> "5NN TU" —
    # a very common pattern (the operator sends the report and immediately
    # "TU" with no gap).
    if best is None and i + 3 <= n and _is_report(t[i:i+3]):
        best = t[i:i+3]
    return best


def _segment(token: str, known_calls: set | None = None) -> str | None:
    """Split a glued-together string into known CW words, known callsigns,
    and RST reports.

    At fast CW speeds the model doesn't insert spaces (it sees a run of
    characters with no clear gaps), so it returns e.g. 'TKSFERFB' instead
    of 'TKS FER FB'. We walk the token from the start, taking the LONGEST
    recognized piece at each position (a word, a known callsign, a
    report); whatever doesn't match is left as a raw "island" between
    recognized pieces instead of ruining the whole result.
    Previously we required coverage of the WHOLE token, otherwise None —
    but one garbage tail (e.g. the signal cutting off at the end of a
    transmission) broke the WHOLE match and showed the entire, longer
    block with no spaces at all ('TNX FER QSO 73' glued during
    transmission into 'TNXFERQSO73TFE'). So we now return a split even
    when coverage is PARTIAL — but only when the MAJORITY of characters
    (>=50%) were recognized, to avoid inserting spaces into pure noise
    (a random 2-letter match inside a long garbage string).
    """
    t = token.upper()
    if len(t) < 4 or t in CW_WORDS:
        return None
    known_calls = known_calls or set()
    n = len(t)
    pieces: list[str] = []
    matched_chars = 0
    raw = ""
    i = 0
    while i < n:
        piece = _longest_piece_at(t, i, known_calls)
        if piece:
            if raw:
                pieces.append(raw)
                raw = ""
            pieces.append(piece)
            matched_chars += len(piece)
            i += len(piece)
        else:
            raw += t[i]
            i += 1
    if raw:
        pieces.append(raw)
    if matched_chars == 0 or len(pieces) < 2:
        return None
    if matched_chars / n < 0.5:
        return None
    return " ".join(pieces)


def _reglue_split_call(tokens: list[str], known_calls: set) -> list[str]:
    """Glue a run of short tokens broken up by false gaps back into one
    callsign, e.g. ['H','B','9','T','WX'] -> ['HB9TWX'].

    The inverse of the problem _segment fixes: there, the model failed to
    insert a gap between words (glued together with no space). Here it
    inserts a gap WHERE THERE ISN'T ONE — it confuses a short gap between
    the letters of a callsign with a gap between words (observed with
    clear, unhurried sending). The callsign comes out broken into single/
    two-three-letter pieces.

    We only merge a RUN of consecutive "candidates" (1-3 characters, NOT
    belonging to CW_WORDS — so we don't swallow real short words like
    TU/DE/K/BK/WX) and only when the merged result either matches
    known_calls directly, or has an unambiguous callsign shape (letters+
    digit, length 3-10). If a real word (e.g. "WX") shows up within the
    run, the run ends before it — this can cut the merge short too early
    (a callsign that happens to end in "WX"), but that's better than
    swallowing real words. When in doubt (the shape doesn't check out),
    the tokens are left as they were — we don't guess.
    """
    out: list[str] = []
    i, n = 0, len(tokens)
    while i < n:
        j = i
        while j < n:
            tk = tokens[j].upper()
            if 1 <= len(tk) <= 3 and tk.isalnum() and tk not in CW_WORDS:
                j += 1
            else:
                break
        merged, advance = None, i
        if known_calls and j > i:
            # First, solid evidence: the whole glued run (run + up to 2
            # more tokens, EVEN a real word like "WX") matches known_calls
            # directly. We check from the longest variant down — a solid
            # match takes priority over shape alone (below), otherwise a
            # shorter, "plausible-looking" prefix (e.g. "HB9T") would win
            # before we get a chance to check whether the longer variant
            # is a known callsign.
            for m in range(min(j + 2, n), i, -1):
                cand = "".join(t.upper() for t in tokens[i:m])
                if cand in known_calls:
                    merged, advance = cand, m
                    break
        if merged is None and j - i >= 2:
            cand = "".join(t.upper() for t in tokens[i:j])
            if len(cand) <= 10 and _looks_like_call(cand):
                merged, advance = cand, j
        if merged is not None:
            out.append(merged)
            i = advance
            continue
        out.append(tokens[i])
        i += 1
    return out


def correct(text: str, known_calls: set | None = None) -> str:
    """Correct decoder text using CW QSO structure.

    known_calls: a pool of real callsigns (from FT8/log/cluster) for validation.
    """
    if not text:
        return text
    known_calls = known_calls or set()
    tokens = [t for t in text.split(" ") if t]
    tokens = _reglue_split_call(tokens, known_calls)
    out = []
    prev_upper = ""

    for tok in tokens:
        if not tok:
            continue
        T = tok.upper()

        # 1. RST report — normalize the format
        rep = _is_report(T)
        if rep is not None:
            out.append(rep)
            prev_upper = rep
            continue

        # 2. After NAME / OP — try to match a name
        # max_dist=3 (was 2 under the flat distance) — the budget of 2 was
        # calibrated when EVERY substitution cost exactly 1. Now a
        # substitution between distant characters costs more (see
        # _sub_cost), so the same example as in the file header (KEITH ->
        # KEHTHA: I->H is 2 dots of difference plus an inserted A) needs a
        # budget of 3, otherwise even the author's own example wouldn't be
        # caught. _best_match still rejects ties either way, so a bigger
        # budget doesn't mean more false matches, just more CHANCES to find
        # an unambiguous one.
        if prev_upper in ("NAME", "OP", "OM") and len(T) >= 3:
            m = _best_match(T, CW_NAMES, max_dist=3)
            if m:
                out.append(m)
                prev_upper = m
                continue

        # 3. Callsign — validate against the known-callsign pool (if given)
        if known_calls and _looks_like_call(T):
            if T in known_calls:
                out.append(T)
                prev_upper = T
                continue
            # Only match against the pool for tokens with a SENSIBLE
            # structure — otherwise every piece of garbage (R3BDR, OKOK5I9)
            # would trigger an expensive distance computation against the
            # whole pool, bogging down the CPU on longer lines.
            m = _best_match(T, known_calls, max_dist=1)
            if m:
                out.append(m)
                prev_upper = m
                continue

        # 4. Common CW phrase — match against the vocabulary
        if len(T) >= 2:
            m = _best_match(T, CW_WORDS, max_dist=1)
            if m:
                out.append(m)
                prev_upper = m
                continue

        # 5. Glued words — try to split using the vocabulary (TKSFER -> TKS FER)
        seg = _segment(T, known_calls)
        if seg:
            out.append(seg)
            prev_upper = seg.split()[-1]
            continue

        # 6. Nothing matches — leave the original
        out.append(tok)
        prev_upper = T

    return " ".join(out)


def _looks_like_call(t: str) -> bool:
    """Whether a token looks like a callsign (letter + digit + letters)."""
    if not (3 <= len(t) <= 10):
        return False
    return any(c.isdigit() for c in t) and any(c.isalpha() for c in t)
