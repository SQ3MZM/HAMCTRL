"""
clock_check.py — UTC clock sync check for FT8/FT4 (HAMCTRL).

WHY: FT8 relies STRICTLY on UTC time windows (15s for FT8, 7.5s for FT4).
If the machine's clock drifts by more than ~1s, ALL windows shift —
decoding degrades, QSOs don't complete, and the symptom looks EXACTLY like
a bug in the timing code. This check lets you tell "bad clock" apart from
"bad code": check the clock before hunting for a bug in the script.

Dependencies: NONE (raw NTP socket). Timeout + safe fallback — if NTP is
unavailable, returns status "unknown" and NEVER crashes the app.

Thresholds (for FT8, 15s window):
  < 0.5s   OK        — decoding reliable
  0.5-1.0s WARNING   — may still work, but marginal
  > 1.0s   ERROR     — windows misaligned, QSOs won't complete; fix NTP/clock
"""
import socket
import struct
import time

# NTP servers (several, for resilience — tried in order until one responds)
_NTP_SERVERS = ["pool.ntp.org", "time.google.com", "time.windows.com",
                "tempus1.gum.gov.pl"]  # last one: Polish GUM server (author is in PL)

# Offset thresholds in seconds (absolute value)
THRESH_OK = 0.5      # below = OK
THRESH_WARN = 1.0    # 0.5-1.0 = warning; above = error


def query_ntp_offset(host, timeout=3.0):
    """
    Returns the clock offset (NTP_time - local_time) in seconds, or raises
    if the server doesn't respond. Positive offset = our clock is BEHIND.
    Uses RTT/2 correction (like a real NTP client), so it's accurate.
    """
    # NTP request: LI=0, VN=3, Mode=3 (client) -> first byte 0x1b
    packet = b'\x1b' + 47 * b'\0'
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        t0 = time.time()
        s.sendto(packet, (host, 123))
        resp, _ = s.recvfrom(1024)
        t3 = time.time()
    finally:
        s.close()

    if len(resp) < 48:
        raise ValueError("NTP response too short")

    unpacked = struct.unpack("!12I", resp[:48])
    # Transmit timestamp: words 10 (seconds) and 11 (fraction), since 1900-01-01
    tx_secs = unpacked[10]
    tx_frac = unpacked[11]
    ntp_time = (tx_secs - 2208988800) + (tx_frac / 2**32)  # -> unix epoch
    # Offset with network delay correction (assuming symmetric RTT):
    # the server's time corresponds to roughly (t0+t3)/2 on our side.
    offset = ntp_time - (t0 + t3) / 2.0
    return offset


def check_clock(timeout=3.0):
    """
    Checks the clock offset against NTP. Returns a dict:
      {status, offset_s, level, message, server}
    status: 'ok' | 'warning' | 'error' | 'unknown'
    level:  mirrors status (for UI color-coding)
    NEVER raises — returns status='unknown' if NTP is unavailable.
    """
    last_err = None
    for host in _NTP_SERVERS:
        try:
            offset = query_ntp_offset(host, timeout=timeout)
            a = abs(offset)
            if a < THRESH_OK:
                status = "ok"
                msg = f"Clock synchronized (offset {offset:+.2f}s)."
            elif a < THRESH_WARN:
                status = "warning"
                msg = (f"Clock is marginal (offset {offset:+.2f}s). "
                       f"FT8 may work unreliably — consider syncing NTP.")
            else:
                status = "error"
                msg = (f"CLOCK OFF by {offset:+.2f}s! FT8/FT4 windows won't "
                       f"align — QSOs won't complete. Sync the clock (NTP/Windows "
                       f"internet time) BEFORE looking for a bug in the code.")
            return {"status": status, "offset_s": round(offset, 3),
                    "level": status, "message": msg, "server": host}
        except Exception as e:
            last_err = e
            continue
    # No server responded
    return {"status": "unknown", "offset_s": None, "level": "unknown",
            "message": (f"Could not check the clock (NTP unavailable: {last_err}). "
                        f"Manually verify the system time is accurate."),
            "server": None}


if __name__ == "__main__":
    # Quick command-line test
    print("Checking UTC clock synchronization...")
    result = check_clock()
    print(f"  status:  {result['status']}")
    print(f"  offset:  {result['offset_s']}s")
    print(f"  server:  {result['server']}")
    print(f"  message: {result['message']}")
