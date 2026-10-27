"""
ft8_all_log.py - dziennik wszystkiego, co bylo odebrane i nadane w FT8/FT4,
w formacie pliku ALL.TXT z WSJT-X (jedna linia na komunikat):

    251027_183015    14.074 Rx FT8    -12  0.3 1500 K1ABC W9XYZ RR73
    251027_183030    14.074 Tx FT8      0  0.0 1500 W9XYZ K1ABC 73

Osobny plik dla kazdego operatora (wedlug id konta - zapisuje sie temu, kto
w danej chwili ma przejete radio), w DATA/all_txt/. Rozmiar pilnowany
rotacja: po przekroczeniu MAX_BYTES biezacy plik staje sie kopia ".old" (poprzednia
kopia jest kasowana), czyli na operatora najwyzej ~2 x MAX_BYTES.
"""
import re
import time
from pathlib import Path

MAX_BYTES = 5_000_000


class Ft8AllLog:
    def __init__(self, data_dir: Path):
        self.dir = Path(data_dir) / "all_txt"

    def path_for(self, uid) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(uid)) or "unknown"
        return self.dir / f"ALL_{safe}.TXT"

    def old_path_for(self, uid) -> Path:
        return self.path_for(uid).with_suffix(".old.TXT")

    def write(self, uid, rx: bool, dial_hz: float, mode: str, snr: int, dt: float,
              freq_hz: int, message: str, time_str: str = "", epoch: float | None = None):
        """time_str: HHMMSS okna (z dekodera); epoch: kiedy (do daty, albo
        do czasu gdy brak time_str)."""
        if not uid:
            return
        try:
            t = time.gmtime(epoch if epoch is not None else time.time())
            hhmmss = time_str if (time_str and len(time_str) == 6) else time.strftime("%H%M%S", t)
            line = (f"{time.strftime('%y%m%d', t)}_{hhmmss}"
                    f"{(dial_hz or 0) / 1e6:11.3f} {'Rx' if rx else 'Tx'} {mode:<4}"
                    f"{int(snr):6d}{float(dt):5.1f}{int(freq_hz):5d} {message}\n")
            self.dir.mkdir(parents=True, exist_ok=True)
            p = self.path_for(uid)
            if p.exists() and p.stat().st_size > MAX_BYTES:
                p.replace(self.old_path_for(uid))
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            # Dziennik nigdy nie moze przerwac odbioru/nadawania.
            print(f"[all.txt] blad zapisu: {e}", flush=True)

    def read(self, uid) -> bytes:
        """Starsza kopia (jesli jest) + biezacy plik, chronologicznie."""
        out = b""
        for p in (self.old_path_for(uid), self.path_for(uid)):
            if p.exists():
                out += p.read_bytes()
        return out
