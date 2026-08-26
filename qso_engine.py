"""
qso_engine.py — automatic QSO engine following the standard FT8 protocol
sequence.

Pure logic (no asyncio/networking), easy to test in isolation.
Responsible for:
  1. Parsing received FT8 messages (who/to whom/what).
  2. The state machine of a single QSO (Tx1..Tx5, classic RRR->73 sequence).
  3. The queue of stations answering our CQ ("Call 1st" / FIFO).

Standard auto-QSO message sequence (when WE start, answering someone else's
CQ — i.e. Tx1 as the first step):
  Tx1: <THEIR_CALL> <OUR_CALL> <OUR_GRID>
  Tx2: <THEIR_CALL> <OUR_CALL> <SNR_REPORT>          (when they send us a report)
  Tx3: <THEIR_CALL> <OUR_CALL> R<SNR_REPORT>         (ack + our report)
  Tx4: <THEIR_CALL> <OUR_CALL> RRR                   (ack of received report)
  Tx5: <THEIR_CALL> <OUR_CALL> 73                    (sign-off, QSO end)

When WE call CQ and SOMEONE answers us, the sequence starts at Tx2 (Tx1 is
their first message to us, which we answer with a report).

This implementation uses the classic (not the shortened RR73) ending:
separate Tx4=RRR and Tx5=73, per the user's choice — RRR is sent immediately
upon receiving a report, 73 is sent only once the partner confirms (sends
RRR or 73 on their side). This mirrors the traditional auto-sequencing
sequence more closely than the newer RR73 shortcut.
"""

import re
import time


# ─────────────────────────────────────────────────────────────────────────────
# FT8 message parsing
# ─────────────────────────────────────────────────────────────────────────────

# CQ MODIFIERS - list of all known modifiers used on FT8.
# Format: "CQ <MODIFIER> <CALL> <GRID>" e.g. "CQ POTA W1XYZ FN42".
# Modifiers are 2-6 character words that aren't callsigns. Most are
# abbreviations of activation programs (POTA, SOTA), regions (EU, NA, AS)
# or countries (USA, JA, DL). Full set from common ham band practice.
_CQ_MODIFIERS = frozenset({
    # Directional / regional (continents and regions)
    "DX", "NA", "SA", "EU", "AS", "AF", "OC", "WW", "WWDX",
    # Countries (1-4 char prefixes) - most common
    "USA", "JA", "DL", "F", "G", "GM", "GI", "GW", "GD", "GJ",
    "PA", "OE", "OK", "OM", "SP", "SM", "OZ", "LA", "OH", "OY",
    "EA", "CT", "I", "IT", "IS", "S5", "9A", "T7", "T9", "E7",
    "YO", "YU", "LY", "YL", "ES", "UR", "R", "UA", "UN", "UK",
    "VE", "VK", "ZL", "ZS", "ZL7", "KH6", "KL7", "PY", "LU", "CE",
    "HK", "HC", "HI", "HP", "TI", "TG", "XE", "CO", "9Y", "8P",
    "VP", "VP2", "VP6", "VP8", "VP9",
    # Contests and events
    "TEST", "CONTEST", "SPRINT", "FD", "FIELD",
    # Activation programs
    "POTA", "SOTA", "IOTA", "WWFF", "COTA", "BUNKER", "REF",
    "USI", "USIS", "ILLW", "WCA", "WFF", "TQP",
    # Clubs and organizations
    "ARRL", "RSGB", "DARC", "IARU", "SP",
    # Power
    "QRP", "QRO", "QRPP",
    # Special
    "FF", "SKCC", "SOWP", "PODXS",
})

# Known callsign suffixes (after "/")
# Note: portable district /0..9 is handled separately as a regex.
_CALL_SUFFIXES = frozenset({
    "M",     # mobile
    "MM",    # maritime mobile
    "AM",    # aeronautical mobile
    "P",     # portable
    "QRP",   # low power
    "QRPP",  # very low power
    "LH",    # lighthouse
})

# Regex for portable district: /0 to /9
_PORTABLE_DISTRICT_RE = re.compile(r'^\d$')

# Amateur callsign format: 1-2 letters, 0-1 digit, 1-2 letters, digit, 1-4 letters
# Simplified: 3-8 chars, at least 1 digit and at least 1 letter
_CALLSIGN_RE = re.compile(r'^[A-Z0-9]{3,8}$')


def is_valid_callsign(s: str) -> bool:
    """Whether a string looks like an amateur callsign (without suffix/prefix)."""
    if not s or not _CALLSIGN_RE.fullmatch(s):
        return False
    has_digit  = any(c.isdigit() for c in s)
    has_letter = any(c.isalpha() for c in s)
    return has_digit and has_letter


def is_cq_modifier(s: str) -> bool:
    """Whether a string looks like a CQ modifier (POTA, DX, USA, etc.).

    Classification: (1) whitelist of known modifiers, OR
    (2) 2-6 characters, no digits (if it has digits it's more likely a
    callsign or prefix).
    Note: this is a heuristic — in rare cases something like 'K7RA' will be
    misclassified, but it works for 99% of on-air practice.

    (2) is DELIBERATELY generic rather than another whitelist entry: a real
    amateur callsign ALWAYS contains a digit (see is_valid_callsign above),
    so any purely alphabetic string up to 6 characters is safely classified
    as a modifier without having to enumerate every activation program by
    name. The whitelist previously had POTA/SOTA explicitly but not BOTA
    (Bunkers on the Air) — the code actually checked `len(s) <= 3`, even
    though this docstring always said "2-6": a code/description mismatch,
    not just a missing entry. Fixed by correcting the boundary to the
    actual 6 instead of hardcoding more 4-6-letter abbreviations
    (COTA/GOTA/HOTA/LOTA/MOTA/VOTA/WOTA/ZOTA/...) one by one — the same fix
    is mirrored in _isCqModifier (JS, wsjtx.js) on the frontend side, which
    has its own, smaller whitelist subset."""
    if not s:
        return False
    if s in _CQ_MODIFIERS:
        return True
    # Fallback: a short string with no digits may be a modifier (e.g. a
    # rare country or an activation program not in the whitelist) - MUST
    # have no digits, since a real amateur callsign always has at least one.
    if len(s) <= 6 and s.isalpha():
        return True
    return False


def strip_suffix(call: str) -> tuple:
    """Split a callsign into (base_call, suffix, prefix).

    Handles:
      - XX0XXX/M       -> base=XX0XXX, suffix=M,   prefix=None
      - XX0XXX/MM      -> base=XX0XXX, suffix=MM,  prefix=None
      - XX0XXX/P       -> base=XX0XXX, suffix=P,   prefix=None
      - XX0XXX/QRP     -> base=XX0XXX, suffix=QRP, prefix=None
      - W1AB/2         -> base=W1AB,   suffix='2', prefix=None (portable district)
      - W1/DL3ABC      -> base=DL3ABC, suffix=None, prefix=W1 (operating from another country)
      - W1/DL3ABC/P    -> base=DL3ABC, suffix=P,   prefix=W1 (portable, operating from another country)
      - XX0XXX         -> base=XX0XXX, suffix=None, prefix=None
      - Empty/missing  -> (call, None, None)

    Note: base_call is the callsign used for DXCC lookup and QSO comparison
    (same station regardless of /M vs fixed). suffix and prefix are kept
    for display and for use in TX messages (must be preserved so the
    station recognizes we're addressing it)."""
    if not call:
        return (call, None, None)
    parts = call.split('/')
    if len(parts) == 1:
        return (call, None, None)
    if len(parts) == 2:
        # Could be: BASE/SUFFIX or PREFIX/BASE
        a, b = parts
        # If b is a known suffix or a portable-district digit
        if b in _CALL_SUFFIXES or _PORTABLE_DISTRICT_RE.fullmatch(b):
            return (a, b, None)
        # If a is very short (1-3 chars) and b looks like a callsign
        # -> a is the prefix (country/region), b is the base
        if len(a) <= 3 and is_valid_callsign(b):
            return (b, None, a)
        # Fallback: treat as BASE/SUFFIX (even if unknown)
        return (a, b, None)
    if len(parts) >= 3:
        # PREFIX/BASE/SUFFIX e.g. W1/DL3ABC/P
        prefix, base, suffix = parts[0], parts[1], parts[2]
        return (base, suffix, prefix)
    return (call, None, None)


def base_call(call: str) -> str:
    """Extract the base callsign (without suffix/prefix). Used for DXCC
    lookup and to compare whether it's the same station."""
    b, _, _ = strip_suffix(call)
    return b


def parse_dxpedition_message(call_to: str, call_de: str, sender_call: str,
                              report: str, my_call: str):
    """
    Builds a dict in the SAME shape as parse_message(), but from the fields
    of a type 0.1 message (i3=0, n3=1 - "FT8 DXpedition"/Fox, see
    unpack_type0_1 in ham_audio/src/decode/unpack.rs). This message does
    NOT have normal TO/DE semantics: call_to/call_de are two DIFFERENT
    Hounds (one gets RR73, the other gets a new report) in a SINGLE Fox
    transmission; the actual sender is given separately as sender_call
    (the resolved 10-bit hash of its callsign, may be "..." if not yet
    resolved).

    Used NOT ONLY by Hound mode (which reads these fields directly in
    _houndOnDecode in wsjtx.js) - stations running MSHV in "Multi Answering"
    mode use THIS SAME message format even in ordinary, everyday QSOs (not
    just real DXpeditions - confirmed: newer MSHV Multistream versions are
    indistinguishable from Fox/Hound at the transmission level), so the
    MAIN engine (Call 1st / normal QSO) also has to understand this -
    otherwise a QSO with such a station would never close out in the log
    (their final RR73 or report invitation would simply be dropped).

    Returns None if the message doesn't concern us at all (neither call_to
    nor call_de is our own callsign).
    """
    my = (my_call or "").upper()
    call_to = (call_to or "").strip().upper()
    call_de = (call_de or "").strip().upper()
    sender = (sender_call or "").strip().upper()
    if sender in ("", "...", "<...>"):
        sender = ""
    report = (report or "").strip()

    if call_to == my:
        # Addressed to us: the sender (Fox or an MSHV Multistream station)
        # is saying RR73 - QSO complete. call_de is the OTHER Hound (not
        # us) - used only as a fallback when sender_call isn't resolved yet.
        de = sender or call_de
        de_base, de_suffix, de_prefix = strip_suffix(de) if de else (None, None, None)
        return {'call_to': my, 'call_de': de, 'extra': None,
                'is_cq': False, 'is_rrr': False, 'is_73': False, 'is_rr73': True,
                'report': None, 'cq_modifier': None,
                'de_base': de_base, 'de_suffix': de_suffix, 'de_prefix': de_prefix,
                'to_base': base_call(my)}

    if call_de == my:
        # Addressed to us: the sender invites us with a NEW report (raw, no
        # R- prefix - like Tx1/Tx2 in a normal sequence). call_to is the
        # OTHER Hound (the one that got RR73 in this SAME transmission),
        # not us.
        de = sender or call_to
        de_base, de_suffix, de_prefix = strip_suffix(de) if de else (None, None, None)
        return {'call_to': my, 'call_de': de, 'extra': None,
                'is_cq': False, 'is_rrr': False, 'is_73': False, 'is_rr73': False,
                'report': report or None, 'cq_modifier': None,
                'de_base': de_base, 'de_suffix': de_suffix, 'de_prefix': de_prefix,
                'to_base': base_call(my)}

    return None  # concerns two other Hounds - not our business


def parse_message(message: str):
    """
    Splits a decoded FT8 message into its components.
    Returns a dict: {call_to, call_de, extra, is_cq, is_rrr, is_73, is_rr73, report,
                  cq_modifier, de_base, de_suffix, de_prefix, to_base}
    or None if the message doesn't match any recognized pattern.

    New fields:
      cq_modifier - CQ modifier (POTA/SOTA/DX/USA etc.) or None
      de_base     - sender's base callsign (without suffix /M /P) for DXCC lookup
      de_suffix   - suffix (M/MM/P/QRP/0-9) or None
      de_prefix   - prefix (W1/, EA5/) or None (for stations operating from another country)
      to_base     - recipient's base callsign (without suffix)

    Supported formats:
      "CQ CALL GRID"                    -> plain CQ
      "CQ MOD CALL GRID"                -> CQ with a modifier (POTA/DX/USA/JA/EU/...)
      "CQ CALL/M GRID"                  -> mobile CQ
      "CQ MOD CALL/P GRID"              -> portable CQ with a modifier
      "CQ W1/DL3ABC GRID"               -> CQ with a prefix (operating from another country)
      "CQ MOD"                          -> broadcast with no specific addressee
      "TO DE GRID"                      -> Tx1
      "TO DE +NN"/"TO DE -NN"           -> Tx1/Tx2 SNR report
      "TO DE R+NN"/"TO DE R-NN"         -> Tx3 ack + report
      "TO DE RRR"                       -> Tx4
      "TO DE RR73"                      -> Tx4 shortened
      "TO DE 73"                        -> Tx5
    """
    if not message:
        return None
    parts = message.strip().upper().replace('<', '').replace('>', '').split()
    if not parts:
        return None

    if parts[0] == 'CQ':
        # No call - "CQ MOD" (2 words) - broadcast with no specific station,
        # e.g. "CQ FF" (Fox call in Fox/Hound), "CQ DX" with no callsign
        if len(parts) == 2 and is_cq_modifier(parts[1]):
            return {'call_to': None, 'call_de': None, 'extra': None,
                    'is_cq': True, 'is_rrr': False, 'is_73': False,
                    'is_rr73': False, 'report': None, 'cq_modifier': parts[1],
                    'de_base': None, 'de_suffix': None, 'de_prefix': None,
                    'to_base': None}

        # 3 words: "CQ CALL GRID" (no modifier)
        if len(parts) == 3:
            call_de = parts[1]
            de_base, de_suffix, de_prefix = strip_suffix(call_de)
            return {'call_to': None, 'call_de': call_de, 'extra': parts[2],
                    'is_cq': True, 'is_rrr': False, 'is_73': False,
                    'is_rr73': False, 'report': None, 'cq_modifier': None,
                    'de_base': de_base, 'de_suffix': de_suffix,
                    'de_prefix': de_prefix, 'to_base': None}

        # 4 words: "CQ MOD CALL GRID" (POTA/DX/USA/EU/...)
        # Decide whether parts[1] is a modifier or a callsign:
        #   - if it's in the whitelist -> definitely a modifier
        #   - if it looks like a callsign (has a digit) -> more likely a callsign
        #     (then this is an unusual 4-word structure with no modifier, rare)
        if len(parts) == 4:
            if is_cq_modifier(parts[1]):
                call_de = parts[2]
                de_base, de_suffix, de_prefix = strip_suffix(call_de)
                return {'call_to': None, 'call_de': call_de, 'extra': parts[3],
                        'is_cq': True, 'is_rrr': False, 'is_73': False,
                        'is_rr73': False, 'report': None, 'cq_modifier': parts[1],
                        'de_base': de_base, 'de_suffix': de_suffix,
                        'de_prefix': de_prefix, 'to_base': None}
            # Unusual: 4 words but parts[1] doesn't look like a modifier
            # Treat as "CQ CALL GRID EXTRA" (ignore EXTRA)
            call_de = parts[1]
            de_base, de_suffix, de_prefix = strip_suffix(call_de)
            return {'call_to': None, 'call_de': call_de, 'extra': parts[2],
                    'is_cq': True, 'is_rrr': False, 'is_73': False,
                    'is_rr73': False, 'report': None, 'cq_modifier': None,
                    'de_base': de_base, 'de_suffix': de_suffix,
                    'de_prefix': de_prefix, 'to_base': None}
        return None

    if len(parts) < 3:
        # "<TO> <DE> 73" is 3 words minimum; anything shorter doesn't match
        # the standard pattern (could be free text — we ignore it).
        return None

    call_to, call_de = parts[0], parts[1]
    to_base = base_call(call_to)
    de_base, de_suffix, de_prefix = strip_suffix(call_de)
    tail = parts[2]

    is_rrr = (tail == 'RRR')
    is_73 = (tail == '73')
    is_rr73 = (tail == 'RR73')
    report = None
    if not (is_rrr or is_73 or is_rr73):
        # SNR report: "+NN", "-NN", "R+NN", "R-NN" (R prefix = ack + report)
        if re.fullmatch(r'R?[+-]\d{1,2}', tail):
            report = tail
        # Otherwise tail is probably a grid square (Tx1) — leave it in
        # 'extra', don't treat it as a parse error.

    return {
        'call_to': call_to, 'call_de': call_de,
        'extra': tail if report is None and not (is_rrr or is_73 or is_rr73) else None,
        'is_cq': False, 'is_rrr': is_rrr, 'is_73': is_73, 'is_rr73': is_rr73,
        'report': report, 'cq_modifier': None,
        'de_base': de_base, 'de_suffix': de_suffix, 'de_prefix': de_prefix,
        'to_base': to_base,
    }


# ─────────────────────────────────────────────────────────────────────────────
# QSO states
# ─────────────────────────────────────────────────────────────────────────────

ST_IDLE = 'IDLE'                  # no active QSO
ST_CALLING = 'CALLING'            # we sent Tx1/Tx2, waiting for a reply
ST_REPORT_SENT = 'REPORT_SENT'    # we sent our report (Tx2/Tx3), waiting for R+report or RRR
ST_RRR_SENT = 'RRR_SENT'          # we sent RRR (Tx4), waiting for 73 or a repeat
ST_DONE = 'DONE'                  # QSO finished (received/sent 73), ready to log


class QsoEngine:
    """
    State machine for a single active QSO + the queue of waiting stations
    (answered our CQ but aren't being handled yet).

    Usage (from webapp.py):
      engine = QsoEngine(my_call='XX0XXX', my_grid='KO02')
      engine.start_qso('DL1ABC')                 # manually OR automatically after a CQ
      action = engine.on_decode(parsed_message)  # called for EVERY decode
      if action: send_tx(action.call_to, action.call_de, action.report_or_grid, action.r_flag)
    """

    def __init__(self, my_call: str, my_grid: str):
        self.my_call = my_call.upper()
        self.my_grid = my_grid.upper()
        self.state = ST_IDLE
        self.partner_call = None
        self.partner_grid = None          # partner's grid, if known (from their Tx1)
        self.partner_report_sent = None   # report WE sent to the partner
        self.partner_report_recv = None   # report WE received from the partner
        self.started_at = None
        self.last_activity_at = None
        self.last_tx_at = None    # when we LAST sent anything in this QSO
        self.retry_count = 0      # how many times we repeated the last message with no reply
        self.queue = []   # list of callsigns waiting in the queue (FIFO), filled by CQ answers
        self._queue_seen = set()  # prevents duplicates in the queue
        # call -> recvEpoch (the RECEIVE time of the decode that added this
        # station to the queue). Needed so that after pop_next_from_queue()
        # webapp.py can correctly compute the TX period (see
        # _period_from_epoch in webapp.py) - without this, auto-advance from
        # the queue would transmit with a RANDOM/stale period left over from
        # the previous QSO, randomly colliding with the partner.
        self._queue_recv_epoch = {}

    # ── QSO management ──────────────────────────────────────────────────────

    def record_sent_report(self, report: str):
        """
        Records the report we ACTUALLY sent to the partner — called by
        webapp.py AFTER substituting the real, measured SNR (the engine
        itself doesn't know this value at the moment it generates the
        'reply' action with needs_measured_report=True, since the SNR
        measurement comes from the audio decoder, outside the engine).
        Needed among other things to fill in the RST Sent field on the QSO
        logging form once the QSO ends.
        """
        self.partner_report_sent = report

    def start_qso(self, partner_call: str, initial_decode: dict = None):
        """
        Starts a QSO with partner_call. Two scenarios:

        1. WE initiate (e.g. clicking "reply" on someone else's CQ) —
           initial_decode is NOT given. State -> CALLING, the first message
           to send is Tx1 (our grid), fetched via next_tx_action().

        2. The partner has ALREADY replied to OUR CQ with a message
           containing useful information (their grid or a report) —
           initial_decode is the parsed message (from parse_message). In
           this case we do NOT send our own Tx1 (the partner already knows
           our grid, since WE were the one calling CQ with the grid in the
           message) — we immediately process their message through
           on_decode, which correctly jumps to sending a report
           (Tx2/Tx3-style). This matches the standard "skip Tx1" behavior
           when auto-sequencing is armed on the side that's answering its
           own CQ.
        """
        self.state = ST_CALLING
        self.partner_call = partner_call.upper()
        self.partner_grid = None
        self.partner_report_sent = None
        self.partner_report_recv = None
        self.started_at = time.time()
        self.last_activity_at = self.started_at
        self.last_tx_at = None
        self.retry_count = 0
        self._queue_seen.discard(self.partner_call)
        self.queue = [c for c in self.queue if c != self.partner_call]
        self._queue_recv_epoch.pop(self.partner_call, None)
        if initial_decode is not None:
            return self.on_decode(initial_decode)
        return None

    def abort_qso(self):
        """Aborts the current QSO without logging it (e.g. manually by the
        user, or when the partner doesn't respond for too long)."""
        self.state = ST_IDLE
        self.partner_call = None
        self.partner_grid = None
        self.partner_report_sent = None
        self.partner_report_recv = None
        self.last_tx_at = None
        self.retry_count = 0

    def enqueue_caller(self, callsign: str, recv_epoch: float = None):
        """Adds a station to the "Call 1st" queue (answered our CQ, but
        we're currently in another QSO or haven't started one yet).
        FIFO, no duplicates. recv_epoch (the receive time of the decode
        that added it) is remembered for later TX period computation in
        pop_next_from_queue()."""
        callsign = callsign.upper()
        # Compare by BASE call so that XX0XXX/M doesn't get duplicated when
        # XX0XXX is already the partner (or vice versa).
        if base_call(callsign) == base_call(self.partner_call or ''):
            return
        if callsign in self._queue_seen:
            return
        self._queue_seen.add(callsign)
        self.queue.append(callsign)
        if recv_epoch is not None:
            self._queue_recv_epoch[callsign] = recv_epoch

    def pop_next_from_queue(self):
        """Returns (callsign, recv_epoch) of the first station in the queue
        (FIFO) and removes it, or (None, None) if empty. recv_epoch is None
        when the station was queued without a timestamp (shouldn't happen
        in normal use, but the caller must handle it)."""
        if not self.queue:
            return None, None
        callsign = self.queue.pop(0)
        self._queue_seen.discard(callsign)
        recv_epoch = self._queue_recv_epoch.pop(callsign, None)
        return callsign, recv_epoch

    def remove_from_queue(self, callsign: str) -> bool:
        """Removes the given station from the queue (the ✕ button in the
        UI). Returns True if the station was in the queue. Also clears
        _queue_seen so the station can rejoin the queue if it answers a
        CQ again."""
        callsign = (callsign or "").upper()
        if callsign not in self.queue:
            return False
        self.queue = [c for c in self.queue if c != callsign]
        self._queue_seen.discard(callsign)
        self._queue_recv_epoch.pop(callsign, None)
        return True

    def clear_queue(self):
        """Empties the whole "Call 1st" queue (the "clear" button in the
        UI). Without this, old, long-stale entries (stations that answered
        a CQ minutes/hours earlier and may no longer be listening) had no
        way to leave the queue except manually clicking ✕ on each one
        individually — over a long session the queue would grow and
        Call 1st would eventually "call" a stale, no-longer-relevant
        callsign."""
        self.queue = []
        self._queue_seen = set()
        self._queue_recv_epoch = {}

    # ── Processing received messages ──────────────────────────────────

    def on_decode(self, parsed: dict, recv_epoch: float = None):
        """
        Called for EVERY parsed decode (see parse_message).
        Returns a dict {'action': 'reply'|'enqueue'|None, 'call_to', 'call_de',
        'report_or_grid', 'r_flag'} describing what to do, or None if this
        message requires no reaction.

        recv_epoch: the RECEIVE time of this decode (webapp.py's recvEpoch)
        — if the station is queued to Call 1st, it's remembered alongside
        it (see enqueue_caller) so that after pop_next_from_queue()
        webapp.py can correctly compute the TX period for that station
        instead of inheriting a random period left over from the previous
        QSO.

        Recognition logic:
        - If it's a CQ from someone: if IDLE -> nothing (the UI can show a
          list of CQs, but automatic calling requires an explicit
          start_qso, per the "click once, the rest is automatic" model —
          the mere fact of a CQ isn't enough, since dozens of stations
          could be calling CQ at once).
        - If the message is ADDRESSED TO US (call_to == my_call):
            - if call_de == the partner_call of the ACTIVE QSO -> process
              per the state machine
            - if call_de != partner_call but we're IDLE or CALLING someone
              else -> someone else is answering us (e.g. our CQ) ->
              enqueue (unless we're IDLE and this is the first such station
              — then the UI/webapp may decide to start_qso immediately; the
              engine only signals 'enqueue', the calling code decides on
              auto-start based on the Call 1st settings)
        """
        if parsed is None:
            return None

        if parsed['is_cq']:
            # FIX (found by systematic scenario review, 2026-08-24): a CQ
            # from our OWN CURRENT PARTNER, mid-QSO, is even stronger
            # evidence they've moved on than partner_busy (replying to
            # someone else) — restarting CQ means THEY consider the
            # exchange over/abandoned (didn't hear our last reply, gave
            # up, or simply reset). Previously this was silently ignored
            # like any other CQ, so the engine kept waiting/retrying a
            # partner who had visibly already walked away. Routed through
            # the same partner_busy handling (bounded retry/give-up timer
            # in webapp.py, not an instant abort) - see the partner_busy
            # comment below for why instant abort is avoided.
            if self.is_active() and self.partner_call and parsed.get('call_de'):
                de_base = parsed.get('de_base') or base_call(parsed['call_de'])
                if de_base == base_call(self.partner_call):
                    return {'action': 'partner_busy', 'call_de': self.partner_call}
            return None  # UI/webapp decides whether/when to start a QSO with the CQ caller

        if parsed['call_to'] != self.my_call:
            # Message not addressed to us — but if this is our CURRENT
            # PARTNER transmitting to SOMEONE ELSE, that's proof it has
            # already started a QSO with that station and calling it
            # further is pointless - without this, retry
            # (should_retransmit) would blindly keep trying despite clear
            # evidence the station is busy. This mirrors the real-world
            # requirement: if we're answering a station's CQ and it's
            # already transmitting to someone else, we must not keep
            # calling it. Only applies when we have an active partner (not
            # IDLE/DONE) — otherwise any random exchange between two
            # UNRELATED stations would trigger this needlessly.
            if self.is_active() and self.partner_call and parsed.get('call_de'):
                de_base = parsed.get('de_base') or base_call(parsed['call_de'])
                if de_base == base_call(self.partner_call):
                    return {'action': 'partner_busy', 'call_de': self.partner_call}
            return None  # message not addressed to us — ignore (Band Activity shows it anyway)

        call_de = parsed['call_de']

        # Message from someone OTHER than our current QSO partner.
        # Compared by BASE call (without suffix /M /P) so a mobile station
        # is treated as the same station throughout the QSO, even if it
        # sends with a suffix one time and without it another time.
        de_base = parsed.get('de_base') or base_call(call_de) if call_de else None
        partner_base = base_call(self.partner_call) if self.partner_call else None
        if de_base != partner_base:
            # FIX (reported live 2026-08-26): 73/RR73/RRR are QSO-ENDING
            # confirmations, never a valid QSO-starting message — ignore
            # them here instead of enqueueing/auto-starting. Without this,
            # the following loop was possible: partner sends RR73 -> we
            # reply 73, qso_complete=True -> webapp.py logs the QSO and
            # calls abort_qso() (state->IDLE, partner_call=None) ->
            # partner's own echoed "73" (a common FT8 courtesy repeat)
            # arrives a moment later -> since partner_call is now None,
            # de_base != partner_base is trivially true, so this branch
            # treated it as "a NEW station calling us" -> webapp.py called
            # start_qso(call_de, initial_decode=<the 73>) -> on_decode()
            # saw is_73 in the freshly-set state CALLING (not DONE) ->
            # replied with OUR OWN 73 again, qso_complete=True AGAIN ->
            # webapp.py logged a duplicate QSO and abort_qso()'d again ->
            # repeat forever on every one of the partner's closing "73"s.
            # A bare 73/RR73/RRR can only ever be a reply WITHIN an
            # already-active exchange (handled further below, matched by
            # de_base == partner_base) — never a fresh opener.
            if parsed['is_73'] or parsed['is_rr73'] or parsed['is_rrr']:
                return None
            # Always actually add to the queue (not just signal it) — so
            # that even if webapp.py ignores the returned 'enqueue' and
            # doesn't immediately fire start_qso, the station still isn't
            # lost.
            self.enqueue_caller(call_de, recv_epoch)
            if self.state == ST_IDLE:
                # No one is currently being handled — signal 'enqueue' so
                # webapp.py can (per the Call 1st settings) IMMEDIATELY
                # call start_qso(call_de, initial_decode=parsed) instead of
                # waiting in the queue.
                return {'action': 'enqueue', 'call_de': call_de}
            return None  # busy with another QSO — waits in the queue for later

        # Message FROM THE CURRENT QSO PARTNER — process per the state
        # machine. The partner is actually responding, so reset the retry
        # counter (see note_retry/should_give_up) — the next silence from
        # them gets a fresh full set of attempts.
        self.last_activity_at = time.time()
        self.retry_count = 0

        if parsed['is_73']:
            # Partner is ending the QSO — we send our 73 (if not sent yet)
            # and log it. Works from any state (the partner may skip RRR
            # and send 73 directly, or repeat 73 if our RRR got lost in
            # propagation).
            # FIX for "unnecessary QSO extension": if the QSO is ALREADY
            # DONE (we sent 73), don't answer the partner's echoed 73
            # again. The engine used to answer 73 with 73 in a loop. 73 is
            # sent ONCE; further echoes = silence.
            if self.state == ST_DONE:
                return None
            self.state = ST_DONE
            return {'action': 'reply', 'call_to': self.partner_call,
                    'call_de': self.my_call, 'report_or_grid': '73',
                    'r_flag': False, 'qso_complete': True}

        if parsed['is_rr73']:
            # Shortened form from the partner: ack + sign-off combined.
            # We reply with our 73 and finish.
            # FIX: same as 73 above — don't answer an echoed RR73 after DONE.
            if self.state == ST_DONE:
                return None
            self.state = ST_DONE
            return {'action': 'reply', 'call_to': self.partner_call,
                    'call_de': self.my_call, 'report_or_grid': '73',
                    'r_flag': False, 'qso_complete': True}

        if parsed['is_rrr']:
            # Partner confirmed receipt of our report (RRR = "Roger Roger
            # Roger"). We reply IMMEDIATELY with our 73 - the partner's
            # RRR already IS an acknowledgment, no need to echo-answer it
            # with OUR OWN RRR and wait another cycle. Works from any
            # non-DONE state, same as is_73/is_rr73 above.
            # FIX: the previous version here sent OUR OWN RRR back (state
            # RRR_SENT), and only the partner's echo of THAT RRR would
            # finish the QSO by sending 73 - an unnecessary extra cycle
            # (the invariant: if we get RRR, we send 73). No reaction to
            # an echoed RRR after DONE.
            if self.state == ST_DONE:
                return None
            self.state = ST_DONE
            return {'action': 'reply', 'call_to': self.partner_call,
                    'call_de': self.my_call, 'report_or_grid': '73',
                    'r_flag': False, 'qso_complete': True}

        if parsed['report'] is not None:
            report = parsed['report']
            if report.startswith('R'):
                # "R-12" — the partner CONFIRMS receipt of our report AND
                # sends its own (which we no longer need to send again).
                # We reply with RRR.
                self.partner_report_recv = report
                self.state = ST_RRR_SENT
                return {'action': 'reply', 'call_to': self.partner_call,
                        'call_de': self.my_call, 'report_or_grid': 'RRR',
                        'r_flag': False, 'qso_complete': False}
            else:
                # "-12" — the partner's first (raw) report, no R-prefix.
                # We MUST reply with OUR OWN, MEASURED report (R+our_SNR),
                # NOT the partner's report reflected back — that would be a
                # protocol error (the partner would get its own report
                # back instead of information on how WE hear it).
                # report_or_grid=None here is deliberate: webapp.py MUST
                # substitute the real SNR measurement before sending (see
                # needs_measured_report).
                self.partner_report_recv = report
                self.state = ST_REPORT_SENT
                return {'action': 'reply', 'call_to': self.partner_call,
                        'call_de': self.my_call,
                        'report_or_grid': None, 'r_flag': True,
                        'qso_complete': False, 'needs_measured_report': True}

        # Message with a grid (Tx1) — the partner is initiating, we reply
        # with a report. report_or_grid=None: webapp.py MUST substitute a
        # real SNR measurement before sending (see needs_measured_report),
        # same as above.
        #
        # HANDLING A REPEAT (fix for "the engine goes silent while the
        # partner keeps calling"): we accept a grid not only in CALLING but
        # also in REPORT_SENT. In FT8 the partner REPEATS its grid when it
        # hasn't heard our answer — we MUST then REPEAT the report (not
        # stay silent). Without this the engine sent its report ONCE and
        # went quiet, the partner kept calling and eventually went back to
        # CQ for someone else. The report is already frozen
        # (partner_report_sent), so webapp will supply the same value —
        # consistently.
        if parsed['extra'] and self.state in (ST_CALLING, ST_REPORT_SENT):
            # Validate the grid square format (e.g. KO02, JO80aa) — extra
            # may in theory contain arbitrary text (the parser doesn't
            # enforce a grid format), so we don't store obvious garbage in
            # partner_grid (used later to pre-fill the QSO logging form).
            if re.fullmatch(r'[A-R]{2}\d{2}([A-X]{2})?', parsed['extra']):
                self.partner_grid = parsed['extra']
            self.state = ST_REPORT_SENT
            return {'action': 'reply', 'call_to': self.partner_call,
                    'call_de': self.my_call, 'report_or_grid': None,
                    'r_flag': False, 'qso_complete': False,
                    'needs_measured_report': True}

        # Partner sent a report directly (no grid) while in state CALLING —
        # e.g. when WE called the station and it answered with a report.
        # We reply with R+report (confirming + giving our SNR).
        if parsed['report'] is not None and self.state == ST_CALLING:
            report = parsed['report']
            if not report.startswith('R'):
                self.partner_report_recv = report
                self.state = ST_REPORT_SENT
                return {'action': 'reply', 'call_to': self.partner_call,
                        'call_de': self.my_call, 'report_or_grid': None,
                        'r_flag': True, 'qso_complete': False,
                        'needs_measured_report': True}

        return None

    def next_tx_action(self):
        """
        Returns the message to send WHEN STARTING a new QSO (state
        CALLING, before the partner has answered anything) — i.e. our
        Tx1: their call, our call, our grid.
        """
        if self.state != ST_CALLING or not self.partner_call:
            return None
        return {'call_to': self.partner_call, 'call_de': self.my_call,
                'report_or_grid': self.my_grid, 'r_flag': False}

    def is_active(self):
        return self.state not in (ST_IDLE, ST_DONE)

    def record_tx_sent(self):
        """Called by webapp.py right after a transmission has actually been
        scheduled (first send OR a repeat) - sets the reference point for
        should_retransmit()."""
        self.last_tx_at = time.time()

    def should_retransmit(self, period_s: float) -> bool:
        """Whether at least one full TX window (period_s) has passed since
        our last transmission with no new reply from the partner in the
        meantime - i.e. whether it's time to repeat the last message.

        Conventional auto-QSO implementations don't have a separate
        "repeat" mechanism - they simply send the message matching the
        current state on EVERY TX window, so if the state hasn't changed
        (the partner didn't reply), the same message goes out again
        automatically. Our engine is event-driven (reacts only to a new
        decode), so it needs this explicit check instead of unconditional
        periodic transmission - without it, one lost transmission (normal
        under QSB) would result in silence instead of the expected
        automatic repeat."""
        if not self.is_active() or self.last_tx_at is None:
            return False
        return (time.time() - self.last_tx_at) >= period_s

    def note_retry(self):
        """Called RIGHT BEFORE an actual retransmission (not on the first
        send) - increments the attempt counter for should_give_up()."""
        self.retry_count += 1

    def should_give_up(self, max_retries: int) -> bool:
        """Whether we've exhausted the retry limit with no reply from the
        partner.

        Other auto-QSO implementations usually have this counter disabled
        by default (they retry indefinitely, since the operator is
        watching the screen and can decide when to give up). Our engine
        runs unsupervised (Call 1st needs to move on to the next station
        in the queue), so the limit is ALWAYS ENABLED here."""
        return self.retry_count >= max_retries
