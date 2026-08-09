"""
clock_check.py — Kontrola synchronizacji zegara UTC dla FT8/FT4 (HAMCTRL).

DLACZEGO: FT8 opiera sie SCISLE na oknach czasowych UTC (15s dla FT8, 7.5s
FT4). Jesli zegar maszyny odjedzie o >~1s, WSZYSTKIE okna sie rozjezdzaja —
dekodowanie slabnie, QSO nie wchodza, a objaw wyglada DOKLADNIE jak blad w
kodzie timingu. Ta kontrola pozwala odroznic "zly zegar" od "zly kod":
zanim zaczniesz szukac buga w skrypcie, sprawdz czy to nie zegar.

Zaleznosci: BRAK (surowy socket NTP). Timeout + bezpieczny fallback —
jesli NTP niedostepny, zwraca status "unknown" i NIGDY nie wywala aplikacji.

Progi (dla FT8, okno 15s):
  < 0.5s   OK        — dekodowanie pewne
  0.5-1.0s WARNING   — moze dzialac, ale na granicy
  > 1.0s   ERROR     — okna rozjechane, QSO nie wejda; ustaw NTP/zegar
"""
import socket
import struct
import time

# Serwery NTP (kilka, dla odpornosci — probujemy po kolei az ktorys odpowie)
_NTP_SERVERS = ["pool.ntp.org", "time.google.com", "time.windows.com",
                "tempus1.gum.gov.pl"]  # ostatni: polski serwer GUM (Tom w PL)

# Progi offsetu w sekundach (wartosc bezwzgledna)
THRESH_OK = 0.5      # ponizej = OK
THRESH_WARN = 1.0    # 0.5-1.0 = warning; powyzej = error


def query_ntp_offset(host, timeout=3.0):
    """
    Zwraca offset zegara (czas_NTP - czas_lokalny) w sekundach, lub rzuca
    wyjatek jesli serwer nie odpowie. Offset dodatni = nasz zegar SPOZNIONY.
    Uzywa korekcji RTT/2 (jak prawdziwy klient NTP), wiec jest dokladny.
    """
    # NTP request: LI=0, VN=3, Mode=3 (client) -> pierwszy bajt 0x1b
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
        raise ValueError("odpowiedz NTP za krotka")

    unpacked = struct.unpack("!12I", resp[:48])
    # Transmit timestamp: slowa 10 (sekundy) i 11 (ulamek), od 1900-01-01
    tx_secs = unpacked[10]
    tx_frac = unpacked[11]
    ntp_time = (tx_secs - 2208988800) + (tx_frac / 2**32)  # -> unix epoch
    # Offset z korekcja opoznienia sieci (zakladamy symetryczne RTT):
    # czas serwera odpowiada momentowi ~ (t0+t3)/2 po naszej stronie.
    offset = ntp_time - (t0 + t3) / 2.0
    return offset


def check_clock(timeout=3.0):
    """
    Sprawdza offset zegara wzgledem NTP. Zwraca dict:
      {status, offset_s, level, message, server}
    status: 'ok' | 'warning' | 'error' | 'unknown'
    level:  odpowiada status (do koloryzacji UI)
    NIGDY nie rzuca wyjatku — przy braku NTP zwraca status='unknown'.
    """
    last_err = None
    for host in _NTP_SERVERS:
        try:
            offset = query_ntp_offset(host, timeout=timeout)
            a = abs(offset)
            if a < THRESH_OK:
                status = "ok"
                msg = f"Zegar zsynchronizowany (offset {offset:+.2f}s)."
            elif a < THRESH_WARN:
                status = "warning"
                msg = (f"Zegar na granicy (offset {offset:+.2f}s). "
                       f"FT8 moze dzialac niepewnie — rozwaz synchronizacje NTP.")
            else:
                status = "error"
                msg = (f"ZEGAR ROZJECHANY o {offset:+.2f}s! Okna FT8/FT4 nie beda "
                       f"pasowac — QSO nie wejda. Zsynchronizuj zegar (NTP/Windows "
                       f"czas internetowy) PRZED szukaniem bledu w kodzie.")
            return {"status": status, "offset_s": round(offset, 3),
                    "level": status, "message": msg, "server": host}
        except Exception as e:
            last_err = e
            continue
    # Zaden serwer nie odpowiedzial
    return {"status": "unknown", "offset_s": None, "level": "unknown",
            "message": (f"Nie mozna sprawdzic zegara (NTP niedostepny: {last_err}). "
                        f"Upewnij sie recznie, ze czas systemowy jest dokladny."),
            "server": None}


if __name__ == "__main__":
    # Szybki test z linii polecen
    print("Sprawdzam synchronizacje zegara UTC...")
    result = check_clock()
    print(f"  status:  {result['status']}")
    print(f"  offset:  {result['offset_s']}s")
    print(f"  serwer:  {result['server']}")
    print(f"  komunikat: {result['message']}")
