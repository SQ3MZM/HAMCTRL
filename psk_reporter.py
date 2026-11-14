"""
psk_reporter.py - upload of received FT8/FT4 stations to PSK Reporter
(https://pskreporter.info), so the map there shows who this station hears
and - for the stations calling us - who hears them.

Protocol: IPFIX (RFC 7011) over UDP to report.pskreporter.info:4739, with
PSK Reporter's own information elements (enterprise number 30351), as
described in their "Sending reception reports" developer page. Every
packet carries the two templates (receiver + sender) followed by one
receiver record and the sender records.

Rules kept here (the service's request to uploaders):
  - at most one upload every 5 minutes (plus a little random jitter so
    many stations don't hit the server at the same second),
  - one report per station + band per upload (the best SNR is kept),
  - packets kept well under a typical UDP MTU (split if needed).

Off unless the admin enables it (config "pskReporter") - it publishes the
station's receptions on an external website.
"""
import random
import socket
import struct
import time

HOST = "report.pskreporter.info"
PORT = 4739
PEN = 30351                      # PSK Reporter's IANA enterprise number
INTERVAL_S = 300                 # upload at most every 5 minutes
MAX_PACKET = 1300                # bytes - keep under the UDP MTU

_TPL_RECEIVER = 0x9992
_TPL_SENDER = 0x9993

# (element id, length) - 0xFFFF = variable length; ids with the high bit
# set are enterprise-specific (followed by PEN in the template).
_RECEIVER_FIELDS = [(0x8002, 0xFFFF),   # receiverCallsign
                    (0x8004, 0xFFFF),   # receiverLocator
                    (0x8008, 0xFFFF)]   # decodingSoftware
_SENDER_FIELDS = [(0x8001, 0xFFFF),     # senderCallsign
                  (0x8003, 0xFFFF),     # senderLocator
                  (0x8005, 4),          # frequency (Hz)
                  (0x8006, 1),          # sNR (dB, signed)
                  (0x800A, 0xFFFF),     # mode
                  (0x800B, 1),          # informationSource (1 = automatic)
                  (0x0096, 4)]          # flowStartSeconds (standard IPFIX)


def _pad4(b: bytes) -> bytes:
    return b + b"\x00" * (-len(b) % 4)


def _field_specs(fields) -> bytes:
    out = b""
    for fid, flen in fields:
        out += struct.pack("!HH", fid, flen)
        if fid & 0x8000:
            out += struct.pack("!I", PEN)
    return out


def _templates() -> bytes:
    # Options template set (id 3) for the receiver, template set (id 2) for senders
    rec = struct.pack("!HHH", _TPL_RECEIVER, len(_RECEIVER_FIELDS), 0) + _field_specs(_RECEIVER_FIELDS)
    rec = _pad4(struct.pack("!HH", 3, 4 + len(rec) + (-(4 + len(rec)) % 4)) + rec)
    snd = struct.pack("!HH", _TPL_SENDER, len(_SENDER_FIELDS)) + _field_specs(_SENDER_FIELDS)
    snd = _pad4(struct.pack("!HH", 2, 4 + len(snd) + (-(4 + len(snd)) % 4)) + snd)
    return rec + snd


def _vstr(s: str) -> bytes:
    b = (s or "").encode("ascii", "ignore")[:254]
    return bytes([len(b)]) + b


def _data_set(template_id: int, records: bytes) -> bytes:
    body = records
    length = 4 + len(body)
    pad = -length % 4
    return struct.pack("!HH", template_id, length + pad) + body + b"\x00" * pad


def _sender_record(s: dict) -> bytes:
    return (_vstr(s["call"]) + _vstr(s.get("grid", "")) +
            struct.pack("!I", int(s["freq_hz"]) & 0xFFFFFFFF) +
            struct.pack("!b", max(-128, min(127, int(s["snr"])))) +
            _vstr(s["mode"]) + b"\x01" +
            struct.pack("!I", int(s["epoch"]) & 0xFFFFFFFF))


def build_packets(receiver_call: str, receiver_grid: str, software: str,
                  spots: list, seq_start: int, domain_id: int) -> list:
    """IPFIX packets (bytes) for the given spots. Pure function - tested
    offline (test_psk_reporter / scratch tests)."""
    tpl = _templates()
    rec_set = _data_set(_TPL_RECEIVER, _vstr(receiver_call) + _vstr(receiver_grid) + _vstr(software))
    packets = []
    seq = seq_start
    pending = list(spots)
    while pending:
        records = b""
        count = 0
        while pending:
            r = _sender_record(pending[0])
            if 16 + len(tpl) + len(rec_set) + 4 + len(records) + len(r) + 3 > MAX_PACKET and count:
                break
            records += r
            pending.pop(0)
            count += 1
        body = tpl + rec_set + _data_set(_TPL_SENDER, records)
        header = struct.pack("!HHIII", 10, 16 + len(body), int(time.time()), seq, domain_id)
        packets.append(header + body)
        seq += count
    return packets


class PskReporter:
    """Collects spots between uploads; webapp calls add() for every decode
    and runs loop() as a background task."""

    def __init__(self, get_cfg):
        self._get_cfg = get_cfg       # -> (enabled, call, grid, software)
        self._spots = {}              # (call, band_mhz) -> spot
        self._seq = 0
        self._domain = random.getrandbits(32)
        self.last_upload = None       # (epoch, n_spots, error_or_None)

    def add(self, call: str, grid: str, freq_hz: int, snr: int, mode: str, epoch: float):
        enabled, rx_call, rx_grid, _sw = self._get_cfg()
        if not enabled or not call or not freq_hz or call.startswith("<") or not rx_call:
            return
        # The receiver (operator holding the TRX) is taken NOW, at reception,
        # not at upload time - if another operator takes the radio within
        # the 5-minute window, what the first one heard still goes out
        # under the first one's callsign.
        rx_call = rx_call.upper()
        key = (rx_call, call.upper(), int(freq_hz) // 1_000_000)
        old = self._spots.get(key)
        if old is None or snr > old["snr"]:
            self._spots[key] = {"call": call.upper(), "grid": (grid or "")[:6],
                                "freq_hz": int(freq_hz), "snr": int(snr),
                                "mode": mode, "epoch": int(epoch),
                                "rx_call": rx_call, "rx_grid": (rx_grid or "")[:6]}
        if len(self._spots) > 2000:          # safety cap between uploads
            self._spots.pop(next(iter(self._spots)))

    def _send(self) -> None:
        enabled, _call, _grid, software = self._get_cfg()
        spots = list(self._spots.values())
        self._spots = {}
        if not enabled or not spots:
            return
        try:
            # One group per receiver (operator) - each sent under its own callsign.
            groups = {}
            for sp in spots:
                if sp.get("rx_call") and sp.get("rx_grid"):
                    groups.setdefault((sp["rx_call"], sp["rx_grid"]), []).append(sp)
            addr = socket.gethostbyname(HOST)
            n_pk = n_sp = 0
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                for (rcall, rgrid), grp in groups.items():
                    packets = build_packets(rcall, rgrid, software, grp, self._seq, self._domain)
                    for p in packets:
                        s.sendto(p, (addr, PORT))
                    self._seq += len(grp)
                    n_pk += len(packets); n_sp += len(grp)
            self.last_upload = (time.time(), n_sp, None)
            print(f"[pskreporter] sent {n_sp} spot(s) in {n_pk} packet(s) "
                  f"for {len(groups)} operator(s): {', '.join(c for c, _ in groups)}", flush=True)
        except Exception as e:
            self.last_upload = (time.time(), len(spots), str(e))
            print(f"[pskreporter] upload error: {e}", flush=True)

    async def loop(self):
        import asyncio
        while True:
            await asyncio.sleep(INTERVAL_S + random.uniform(0, 30))
            try:
                await asyncio.to_thread(self._send)
            except Exception as e:
                print(f"[pskreporter] loop error: {e}", flush=True)
