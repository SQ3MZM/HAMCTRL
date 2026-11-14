"""
audit_log.py - station log for the admin (2026-11-14).

Who logged in (or failed to) and from where, who took / released the TRX,
SWR protection trips, password / role / two-factor changes, backups. One
JSON line per event in DATA/audit.log; the file is rotated at MAX_BYTES
(the previous one is kept as audit.old.log), so it never grows without
bound. Shown in the ADMIN tab (/api/admin/audit).
"""
import json
import time
from pathlib import Path

MAX_BYTES = 2_000_000


class AuditLog:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "audit.log"
        self.old = Path(data_dir) / "audit.old.log"

    def add(self, event: str, who: str = "", ip: str = "", detail: str = ""):
        try:
            line = json.dumps({"t": int(time.time()), "event": event,
                               "who": str(who or "")[:60], "ip": str(ip or "")[:60],
                               "detail": str(detail or "")[:200]}, ensure_ascii=False)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                self.path.replace(self.old)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception as e:
            # The station log must never break logging in or the radio.
            print(f"[audit] write error: {e}", flush=True)

    def tail(self, n: int = 300) -> list:
        """Newest first."""
        out = []
        for p in (self.path, self.old):
            if len(out) >= n or not p.exists():
                continue
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            for ln in reversed(lines):
                try:
                    out.append(json.loads(ln))
                except Exception:
                    continue
                if len(out) >= n:
                    break
        return out
