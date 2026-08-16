#!/usr/bin/env python3
"""
tunnel_manager.py — zarządza procesem cloudflared
Tryby:
  quick  — Cloudflare Quick Tunnel (losowy adres, bez konta)
  named  — własna domena przez token Cloudflare Tunnel
  off    — wyłączony
"""
import asyncio
import json
import os
import pathlib
import re
import subprocess
import sys

# DATA = zapisywalny katalog danych (obok EXE albo APPDATA). Pliki tunelu
# (config, certy) musza tam trafiac, nie do read-only Program Files.
try:
    from config import DATA as _DATA
except Exception:
    _DATA = pathlib.Path(".")

CFG_FILE = _DATA / "tunnel_config.json"
CF_EXE   = (_DATA / "cloudflared.exe") if sys.platform == "win32" else (_DATA / "cloudflared")

DEFAULT_CFG = {
    "mode":      "off",
    "token":     "",
    "hostname":  "your-server.example.com",
    "autoStart": False,
}


def load_cfg() -> dict:
    try:
        return {**DEFAULT_CFG, **json.loads(CFG_FILE.read_text())}
    except Exception:
        return dict(DEFAULT_CFG)


def save_cfg(cfg: dict):
    try:
        CFG_FILE.write_text(json.dumps(cfg, indent=2))
    except Exception as e:
        print(f"[tunnel] Błąd zapisu konfiguracji: {e}")


class TunnelManager:
    def __init__(self, hub):
        self.hub        = hub
        self._proc      = None
        self._task      = None
        self._status    = "stopped"
        self._public_url = ""
        self._error     = ""
        self._local_url  = "http://localhost:8000"
        self._cfg        = load_cfg()

    # ── Stan ──────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        cert_days = None
        cert = self._cfg.get("certPath", "")
        if cert and pathlib.Path(cert).exists():
            try:
                from cryptography import x509 as _x509
                import datetime as _dt
                c = _x509.load_pem_x509_certificate(open(cert, 'rb').read())
                cert_days = (c.not_valid_after_utc.replace(tzinfo=None) - _dt.datetime.utcnow()).days
            except Exception:
                pass
        return {
            "status":      self._status,
            "publicUrl":   self._public_url,
            "localUrl":    self._local_url,
            "error":       self._error,
            "mode":        self._cfg.get("mode", "off"),
            "certDaysLeft": cert_days,
        }

    def get_config(self) -> dict:
        return dict(self._cfg)

    def save_config(self, cfg: dict):
        self._cfg.update(cfg)
        save_cfg(self._cfg)

    # ── cloudflared binary ────────────────────────────────────────────────────

    def _find_cloudflared(self) -> pathlib.Path | None:
        """Znajdź cloudflared w folderze projektu lub PATH."""
        if CF_EXE.exists():
            return CF_EXE
        # Szukaj w PATH
        import shutil
        found = shutil.which("cloudflared")
        if found:
            return pathlib.Path(found)
        return None

    async def _download_cloudflared(self) -> bool:
        """Pobierz cloudflared jeśli brak."""
        if self._find_cloudflared():
            return True
        print("[tunnel] Pobieranie cloudflared...")
        self._status = "starting"
        await self._broadcast()
        try:
            import urllib.request
            if sys.platform == "win32":
                url  = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
                dest = CF_EXE
            elif sys.platform == "darwin":
                url  = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
                dest = _DATA / "cloudflared"
            else:
                url  = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
                dest = _DATA / "cloudflared"

            urllib.request.urlretrieve(url, dest)
            if sys.platform != "win32":
                dest.chmod(0o755)
            print(f"[tunnel] cloudflared pobrany: {dest}")
            return True
        except Exception as e:
            self._error = f"Nie można pobrać cloudflared: {e}"
            print(f"[tunnel] {self._error}")
            return False

    def check_available(self) -> dict:
        """Sprawdź czy cloudflared jest dostępny + informacje o śmieciach."""
        cf = self._find_cloudflared()
        result = {
            "available":    cf is not None,
            "version":      "",
            "path":         str(cf) if cf else "",
            "stale_procs":  self._count_stale_processes(),
            "svc_installed": self._is_service_installed(),
        }
        if cf:
            try:
                r = subprocess.run([str(cf), "--version"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                ver = r.stdout.strip() or r.stderr.strip()
                m = re.search(r'[\d.]+', ver)
                result["version"] = m.group() if m else ver
            except Exception:
                pass
        return result

    def _count_stale_processes(self) -> int:
        """Policz stare procesy cloudflared w systemie."""
        try:
            if sys.platform == "win32":
                r = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq cloudflared.exe", "/NH"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                return r.stdout.count("cloudflared.exe")
            else:
                r = subprocess.run(["pgrep", "-c", "cloudflared"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
                return int(r.stdout.strip() or 0)
        except Exception:
            return 0

    def _is_service_installed(self) -> bool:
        """Sprawdź czy cloudflared jest zainstalowany jako usługa Windows."""
        if sys.platform != "win32":
            return False
        try:
            r = subprocess.run(
                ["sc", "query", "cloudflared"],
                capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=5)
            return "cloudflared" in r.stdout
        except Exception:
            return False

    async def cleanup(self) -> dict:
        """Wyczyść stare procesy i usuń usługę Windows cloudflared."""
        killed = 0
        svc_removed = False
        msgs = []

        # 1. Zatrzymaj aktualny tunel
        await self.stop()

        # 2. Zabij wszystkie procesy cloudflared
        try:
            if sys.platform == "win32":
                r = subprocess.run(
                    ["taskkill", "/F", "/IM", "cloudflared.exe"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
                if "SUCCESS" in r.stdout or "terminated" in r.stdout.lower():
                    killed = r.stdout.count("SUCCESS")
                    msgs.append(f"Zabito {killed} proces(ów) cloudflared")
            else:
                subprocess.run(["pkill", "-f", "cloudflared"],
                    capture_output=True, timeout=10)
                msgs.append("Procesy cloudflared zatrzymane")
        except Exception as e:
            msgs.append(f"Procesy: {e}")

        # 3. Odinstaluj usługę Windows
        if sys.platform == "win32":
            cf = self._find_cloudflared()
            if cf:
                try:
                    # Najpierw odinstaluj przez cloudflared service uninstall
                    subprocess.run([str(cf), "service", "uninstall"],
                        capture_output=True, timeout=10)
                    # Potem usuń przez sc delete
                    r = subprocess.run(["sc", "delete", "cloudflared"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
                    if "SUCCESS" in r.stdout or "[SC]" in r.stdout:
                        svc_removed = True
                        msgs.append("Usługa Windows cloudflared usunięta")
                except Exception as e:
                    msgs.append(f"Usługa: {e}")

        # 4. Usuń plik cloudflared.exe jeśli lokalny
        local_exe = CF_EXE
        removed_exe = False
        if local_exe.exists() and local_exe.is_file():
            try:
                local_exe.unlink()
                removed_exe = True
                msgs.append(f"Usunięto {local_exe}")
            except Exception as e:
                msgs.append(f"Plik: {e}")

        print(f"[tunnel] Cleanup: {'; '.join(msgs)}")
        return {
            "ok": True,
            "killed": killed,
            "svc_removed": svc_removed,
            "removed_exe": removed_exe,
            "messages": msgs,
        }

    # ── Start / Stop ──────────────────────────────────────────────────────────

    async def start(self, mode: str = None, token: str = None, hostname: str = None):
        """Uruchom tunel."""
        await self.stop()

        if mode:     self._cfg["mode"]     = mode
        if token:    self._cfg["token"]    = token
        if hostname: self._cfg["hostname"] = hostname
        save_cfg(self._cfg)

        mode = self._cfg.get("mode", "quick")
        if mode == "off":
            return

        self._status     = "starting"
        self._public_url = ""
        self._error      = ""
        await self._broadcast()

        # Tryby bez cloudflared
        if mode == "duckdns":
            await self._start_duckdns()
            return
        if mode == "staticip":
            await self._start_staticip()
            return
        if mode == "customcert":
            await self._start_customcert()
            return

        # Tryby Cloudflare — pobierz cloudflared jeśli brak
        if not await self._download_cloudflared():
            self._status = "error"
            await self._broadcast()
            return

        cf = self._find_cloudflared()
        if mode == "quick":
            cmd = [str(cf), "tunnel", "--url", self._local_url]
        else:
            # Named tunnel przez token
            tok = self._cfg.get("token", "").strip()
            if not tok:
                self._error  = "Brak tokenu Cloudflare Tunnel"
                self._status = "error"
                await self._broadcast()
                return
            cmd = [str(cf), "tunnel", "--no-autoupdate", "run", "--token", tok]

        print(f"[tunnel] Start: {mode} | cmd: {' '.join(cmd[:3])}...")
        self._task = asyncio.create_task(self._run(cmd))

    # ── DuckDNS ───────────────────────────────────────────────────────────────

    async def _duckdns_update_ip(self) -> bool:
        domain = self._cfg.get("duckDomain", "").strip()
        token  = self._cfg.get("duckToken", "").strip()
        # Usun .duckdns.org jesli user wkleił pełną domenę
        domain = domain.replace(".duckdns.org", "").strip()
        if not domain or not token:
            self._error = "Brak domeny lub tokenu DuckDNS"
            return False
        try:
            import urllib.request, urllib.parse, ssl
            # urllib.urlopen jest BLOKUJACE (DNS + HTTP + timeout do 10s). Mimo
            # ze ta funkcja jest async, samo urlopen zamrazalo cala petle zdarzen
            # na czas zapytania — wykryte przez looplag (stos: getaddrinfo w
            # tunnel DuckDNS update). Wynosimy do watku, zeby petla plynela.
            def _do_update():
                # PELNA weryfikacja certyfikatu - duckdns.org ma normalny,
                # publicznie zaufany cert, nie ma powodu jej wylaczac.
                # Wczesniej (check_hostname=False, verify_mode=CERT_NONE) tunel
                # do DuckDNS byl podatny na MITM, co przy przesylaniu TOKENU w
                # URL oznaczalo mozliwosc jego przechwycenia. Bez komentarza w
                # kodzie tlumaczacego dlaczego weryfikacja miala byc wylaczona -
                # usunieta, DuckDNS nie wymaga tego wyjatku.
                ctx = ssl.create_default_context()
                _d = urllib.parse.quote(domain, safe='')
                _t = urllib.parse.quote(token, safe='')
                url  = f"https://www.duckdns.org/update?domains={_d}&token={_t}&ip="
                return urllib.request.urlopen(url, timeout=10, context=ctx).read().decode().strip()
            resp = await asyncio.to_thread(_do_update)
            print(f"[tunnel] DuckDNS update: {resp}", flush=True)
            return resp == "OK"
        except Exception as e:
            self._error = f"DuckDNS błąd: {e}"
            print(f"[tunnel] DuckDNS error: {e}", flush=True)
            return False

    async def _start_duckdns(self):
        domain = self._cfg.get("duckDomain", "").strip()
        domain = domain.replace(".duckdns.org", "").strip()
        if not domain:
            self._error  = "Brak nazwy domeny DuckDNS"
            self._status = "error"
            await self._broadcast()
            return

        print(f"[tunnel] DuckDNS start: {domain}.duckdns.org", flush=True)
        self._status = "starting"
        await self._broadcast()

        # Zaktualizuj IP
        ok = await self._duckdns_update_ip()
        if not ok:
            self._status = "error"
            await self._broadcast()
            return

        fqdn = f"{domain}.duckdns.org"
        port = self._cfg.get("staticPort", "8001")

        # Sprawdź istniejący certyfikat
        cert = self._cfg.get("certPath", "")
        key  = self._cfg.get("keyPath", "")

        # PIERWSZENSTWO: jesli istnieje prawdziwy cert Let's Encrypt w live/,
        # uzyj go - nawet jesli config wskazuje na self-signed. Inaczej raz
        # zapisany self-signed blokowalby uzycie LE na zawsze (bo "cert
        # istnieje" = warunek nizej falszywy).
        le_cert = _DATA / "letsencrypt" / "config" / "live" / fqdn / "fullchain.pem"
        le_key  = _DATA / "letsencrypt" / "config" / "live" / fqdn / "privkey.pem"
        _is_selfsigned = "selfsigned" in str(cert).lower()
        if le_cert.exists() and le_key.exists() and (not cert or _is_selfsigned or not pathlib.Path(cert).exists()):
            print(f"[tunnel] Uzywam istniejacego certu Let's Encrypt: {le_cert}", flush=True)
            cert = str(le_cert)
            key  = str(le_key)
            self._cfg["certPath"] = cert
            self._cfg["keyPath"]  = key
            save_cfg(self._cfg)

        if not cert or not pathlib.Path(cert).exists():
            # Spróbuj Let's Encrypt
            cert, key = await self._get_letsencrypt_cert(fqdn, domain)
            if cert and key:
                self._cfg["certPath"] = cert
                self._cfg["keyPath"]  = key
                save_cfg(self._cfg)
            else:
                # Fallback: self-signed. RSA key generation is slow and blocks
                # the event loop (looplag stack pointed at rsa.generate_private_key)
                # — run it in a thread so audio/pings keep flowing meanwhile.
                cert, key = await asyncio.to_thread(self._generate_selfsigned, fqdn)
                if cert and key:
                    self._cfg["certPath"] = cert
                    self._cfg["keyPath"]  = key
                    save_cfg(self._cfg)

        # Sprawdź czy cert wymaga odnowienia (< 30 dni do wygaśnięcia)
        if cert and key:
            try:
                import ssl as _ssl, datetime as _dt
                ctx = _ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
                cert_data = open(cert, 'rb').read()
                from cryptography import x509 as _x509
                c = _x509.load_pem_x509_certificate(cert_data)
                days_left = (c.not_valid_after_utc.replace(tzinfo=None) - _dt.datetime.utcnow()).days
                print(f"[tunnel] cert ważny jeszcze {days_left} dni", flush=True)
                if days_left < 30:
                    print("[tunnel] Odnawiam certyfikat...", flush=True)
                    new_cert, new_key = await self._get_letsencrypt_cert(fqdn, domain)
                    if new_cert:
                        cert, key = new_cert, new_key
                        self._cfg["certPath"] = cert
                        self._cfg["keyPath"]  = key
                        save_cfg(self._cfg)
            except Exception as e:
                print(f"[tunnel] sprawdzenie cert: {e}", flush=True)

        self._public_url = f"https://{fqdn}:{port}"
        self._status     = "connected"
        if not cert:
            self._error      = "Brak certyfikatu SSL"
            self._public_url = f"http://{fqdn}:{port}"
        await self._broadcast()
        print(f"[tunnel] DuckDNS running: {self._public_url}", flush=True)

        # Odświeżaj IP co 5 minut
        self._task = asyncio.create_task(self._duckdns_loop(domain))

    async def _get_letsencrypt_cert(self, fqdn: str, duck_domain: str) -> tuple:
        """Pobierz certyfikat Let's Encrypt przez ACME DNS-01 challenge z DuckDNS."""
        duck_token = self._cfg.get("duckToken", "").strip()
        if not duck_token:
            return "", ""

        print(f"[tunnel] Let's Encrypt: próba dla {fqdn}", flush=True)

        # Sprawdź istniejący certyfikat w standardowych lokalizacjach
        for base in [
            pathlib.Path(f"C:/Certbot/live/{fqdn}"),
            pathlib.Path(f"/etc/letsencrypt/live/{fqdn}"),
            (_DATA / "letsencrypt" / fqdn),
        ]:
            cp = base / "fullchain.pem"
            kp = base / "privkey.pem"
            if cp.exists() and kp.exists():
                print(f"[tunnel] Znaleziono cert: {cp}", flush=True)
                return str(cp), str(kp)

        # Running certbot requires Administrator rights on Windows. The server
        # normally runs WITHOUT admin, so an automatic attempt on every startup
        # would always fail with "administrative rights" and spam the log. If we
        # have no existing cert and no admin rights, skip certbot entirely and
        # fall back to self-signed — the user generates the real cert once via
        # the elevated "Wygeneruj certyfikat (jako admin)" shortcut.
        if sys.platform == "win32":
            try:
                import ctypes
                _is_admin = ctypes.windll.shell32.IsUserAnAdmin()
            except Exception:
                _is_admin = 0
            if not _is_admin:
                print("[tunnel] Brak certu Let's Encrypt i brak uprawnien admina "
                      "— uzywam self-signed. Aby uzyskac prawdziwy certyfikat, "
                      "uruchom skrot 'Wygeneruj certyfikat (jako admin)' z menu Start.",
                      flush=True)
                return "", ""

        # Instaluj certbot jeśli brak
        certbot = await self._ensure_certbot()
        print(f"[tunnel] certbot path: {certbot!r}", flush=True)
        if not certbot:
            print("[tunnel] certbot niedostępny — używam self-signed", flush=True)
            return "", ""

        # Utwórz hook DuckDNS
        print(f"[tunnel] tworzę hook DuckDNS dla {duck_domain}", flush=True)
        hook = self._create_duckdns_hook(duck_domain, duck_token)
        print(f"[tunnel] hook: {hook}", flush=True)

        # Uruchom certbot
        cmd = [
            certbot, "certonly",
            "--manual", "--preferred-challenges", "dns",
            "--manual-auth-hook", hook,
            "--manual-cleanup-hook", hook,
            "-d", fqdn,
            "--non-interactive", "--agree-tos",
            "--email", f"admin@{fqdn}",
            # --disable-hook-validation: certbot domyslnie waliduje hook przed
            # uzyciem, co na Windows ze sciezkami ze spacjami (Program Files)
            # daje falszywy blad. Hook i tak dziala - wylaczamy walidacje.
            "--disable-hook-validation",
            # nie probuj bindowac uprawnien do standardowych lokalizacji
            "--work-dir", str(_DATA / "letsencrypt" / "work"),
            "--logs-dir", str(_DATA / "letsencrypt" / "logs"),
            "--config-dir", str(_DATA / "letsencrypt" / "config"),
        ]
        print(f"[tunnel] uruchamiam certbot: {cmd[0]}", flush=True)
        # Uruchom certbot w osobnym watku (Windows subprocess issue)
        def _run_certbot():
            import subprocess
            try:
                (_DATA / "letsencrypt" / "work").mkdir(parents=True, exist_ok=True)
                (_DATA / "letsencrypt" / "logs").mkdir(parents=True, exist_ok=True)
                (_DATA / "letsencrypt" / "config").mkdir(parents=True, exist_ok=True)
                # Usun zablokowane instancje certbota z poprzednich prob
                # ("Another instance of Certbot is already running").
                if sys.platform == "win32":
                    try:
                        subprocess.run(["taskkill", "/F", "/IM", "certbot.exe"],
                                       capture_output=True, timeout=10)
                    except Exception:
                        pass
                # Usun pliki blokady (.certbot.lock) z katalogow certbota
                for _ld in (_DATA / "letsencrypt" / "work",
                            _DATA / "letsencrypt" / "config",
                            _DATA / "letsencrypt" / "logs"):
                    for _lock in _ld.glob(".certbot.lock"):
                        try: _lock.unlink()
                        except Exception: pass
                print(f"[tunnel] certbot subprocess start", flush=True)
                r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300)
                print(f"[tunnel] certbot exit={r.returncode}", flush=True)
                if r.stdout: print(f"[tunnel] certbot out: {r.stdout[-500:]}", flush=True)
                if r.stderr: print(f"[tunnel] certbot err: {r.stderr[-500:]}", flush=True)
                # Recognize the most common first-run failure: certbot on Windows
                # needs elevated (Administrator) rights to write the system cert
                # store. The raw "exit=1" tells a new club nothing — capture this
                # case so gen_cert_task can show a clear, actionable message.
                _err = (r.stderr or "") + (r.stdout or "")
                if r.returncode != 0 and "administrative rights" in _err.lower():
                    self._last_cert_error = (
                        "Certbot wymaga uprawnien administratora. Uruchom skrot "
                        "'Wygeneruj certyfikat (jako admin)' z menu Start "
                        "(prawy przycisk → Uruchom jako administrator), albo uruchom "
                        "serwer jako administrator tylko na czas generowania certyfikatu. "
                        "Na co dzien serwer NIE potrzebuje admina.")
                elif r.returncode != 0:
                    self._last_cert_error = (r.stderr or "").strip()[-300:] or \
                        "Certbot zwrocil blad — sprawdz logi konsoli."
                else:
                    self._last_cert_error = ""
                return r.returncode == 0
            except Exception as e:
                print(f"[tunnel] certbot wyjątek: {e}", flush=True)
                return False

        print("[tunnel] run_in_executor START", flush=True)
        try:
            ok = await asyncio.get_event_loop().run_in_executor(None, _run_certbot)
            print(f"[tunnel] run_in_executor DONE ok={ok}", flush=True)
            if ok:
                cp = (_DATA / "letsencrypt" / "config" / "live" / fqdn / "fullchain.pem")
                kp = (_DATA / "letsencrypt" / "config" / "live" / fqdn / "privkey.pem")
                if cp.exists():
                    return str(cp), str(kp)
        except Exception as e:
            print(f"[tunnel] certbot wyjątek: {e}", flush=True)
        return "", ""

    async def _ensure_certbot(self) -> str:
        """
        Znajdz certbot. W produkcie EXE certbot instaluje admin RECZNIE
        (pip w EXE nie dziala). Sprawdzamy PATH + typowe lokalizacje instalacji
        certbota na Windows.
        """
        import shutil
        import pathlib

        # 1. PATH (najczestsze)
        cb = shutil.which("certbot")
        if cb:
            print(f"[tunnel] certbot znaleziony: {cb}", flush=True)
            return cb

        # 2. Typowe lokalizacje instalacji certbota na Windows
        #    (instalator z certbot.eff.org / EFF Windows installer)
        win_paths = [
            pathlib.Path(r"C:\Program Files\Certbot\bin\certbot.exe"),
            pathlib.Path(r"C:\Program Files (x86)\Certbot\bin\certbot.exe"),
        ]
        # Certbot moze byc tez w Scripts obok jakiegos Pythona systemowego
        for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")):
            if base:
                win_paths.append(pathlib.Path(base) / "Programs" / "Certbot" / "bin" / "certbot.exe")
        for p in win_paths:
            if p.exists():
                print(f"[tunnel] certbot znaleziony: {p}", flush=True)
                return str(p)

        # 3. Nie znaleziono. W EXE nie probujemy pip (nie zadziala) - dajemy
        #    adminowi jasna instrukcje.
        if getattr(sys, "frozen", False):
            print("[tunnel] ============================================", flush=True)
            print("[tunnel] CERTBOT NIE ZNALEZIONY", flush=True)
            print("[tunnel] Let's Encrypt wymaga zainstalowania certbota.", flush=True)
            print("[tunnel] Admin: pobierz instalator z https://certbot.eff.org", flush=True)
            print("[tunnel]   (wybierz: Software=None, System=Windows)", flush=True)
            print("[tunnel] Po instalacji zrestartuj serwer.", flush=True)
            print("[tunnel] Alternatywa: uzyj Cloudflare tunnel (bez certbota).", flush=True)
            print("[tunnel] ============================================", flush=True)
            self._error = ("Certbot nie zainstalowany. Pobierz z certbot.eff.org "
                           "(Software=None, System=Windows) i zrestartuj serwer, "
                           "albo uzyj Cloudflare tunnel.")
            return ""

        # 4. Tryb dev (nie-frozen): probuj pip
        print("[tunnel] Instaluję certbot przez pip...", flush=True)
        try:
            def _pip_install():
                import subprocess
                r = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "certbot"],
                    capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
                print(f"[tunnel] pip exit={r.returncode}", flush=True)
                if r.stdout: print(f"[tunnel] pip: {r.stdout[-300:]}", flush=True)
                if r.stderr: print(f"[tunnel] pip err: {r.stderr[-300:]}", flush=True)
                return r.returncode == 0

            ok = await asyncio.get_event_loop().run_in_executor(None, _pip_install)
            if ok:
                cb = shutil.which("certbot")
                if cb:
                    print(f"[tunnel] certbot zainstalowany: {cb}", flush=True)
                    return cb
                # Windows: certbot może być w Scripts
                import pathlib
                scripts = pathlib.Path(sys.executable).parent / "Scripts" / "certbot.exe"
                if scripts.exists():
                    print(f"[tunnel] certbot Scripts: {scripts}", flush=True)
                    return str(scripts)
        except Exception as e:
            print(f"[tunnel] pip błąd: {e}", flush=True)
        return ""

    def _create_duckdns_hook(self, domain: str, token: str) -> str:
        """
        Utwórz skrypt hook dla certbot DNS challenge przez DuckDNS.

        WAZNE: certbot na Windows uruchamia hook przez PowerShell. Sciezka ze
        SPACJAMI (np. 'C:\\Program Files (x86)\\HAM RADIO CTRL\\') rozwala sie -
        PowerShell traktuje 'C:\\Program' jako komende. Dlatego hook MUSI byc w
        katalogu BEZ spacji. Uzywamy %TEMP% (albo dedykowanego C:\\HAMCTRL).
        """
        import tempfile
        # Katalog bez spacji: preferuj C:\HAMCTRL-tmp, fallback TEMP
        hook_dir = None
        for cand in (pathlib.Path(r"C:\HAMCTRL"),
                     pathlib.Path(tempfile.gettempdir())):
            try:
                cand.mkdir(parents=True, exist_ok=True)
                # Test braku spacji w sciezce
                if " " not in str(cand):
                    hook_dir = cand
                    break
            except Exception:
                continue
        if hook_dir is None:
            hook_dir = _DATA  # ostatecznosc (moze miec spacje)

        if sys.platform == "win32":
            hook = hook_dir / "duckdns_hook.bat"
            # BEZ -k: curl domyslnie weryfikuje cert duckdns.org (publicznie
            # zaufany, nie ma powodu tego wylaczac - patrz ten sam komentarz
            # przy _duckdns_update_ip, ktory mial identyczny problem).
            hook.write_text(
                f"@echo off\n"
                f"curl \"https://www.duckdns.org/update?domains={domain}&token={token}&txt=%CERTBOT_VALIDATION%&verbose=true\"\n"
                f"echo Czekam 120 sekund na propagacje DNS...\n"
                f"timeout /t 120 /nobreak >nul\n",
                encoding="utf-8",
            )
        else:
            hook = hook_dir / "duckdns_hook.sh"
            hook.write_text(
                f"#!/bin/bash\n"
                f"curl -s \"https://www.duckdns.org/update?domains={domain}&token={token}&txt=$CERTBOT_VALIDATION\"\n"
                f"echo 'Czekam 120s na propagacje DNS...'\n"
                f"sleep 120\n",
                encoding="utf-8",
            )
            hook.chmod(0o755)
        return str(hook.absolute())

    async def _duckdns_loop(self, domain: str):
        while True:
            await asyncio.sleep(300)
            try:
                await self._duckdns_update_ip()
            except Exception:
                pass

    # ── Stałe IP ─────────────────────────────────────────────────────────────

    async def _start_staticip(self):
        ip   = self._cfg.get("staticIp", "").strip()
        port = self._cfg.get("staticPort", "8001").strip()
        if not ip:
            self._error  = "Brak adresu IP"
            self._status = "error"
            await self._broadcast()
            return

        cert = self._cfg.get("certPath", "")
        key  = self._cfg.get("keyPath", "")
        if not cert or not pathlib.Path(cert).exists():
            cert, key = self._generate_selfsigned(ip)
            if cert and key:
                self._cfg["certPath"] = cert
                self._cfg["keyPath"]  = key
                save_cfg(self._cfg)

        self._public_url = f"https://{ip}:{port}"
        self._status     = "connected"
        if not cert:
            self._error      = "Brak certyfikatu SSL"
            self._public_url = f"http://{ip}:{port}"
        await self._broadcast()
        print(f"[tunnel] Stałe IP: {self._public_url}", flush=True)

    # ── Własna domena + własny certyfikat ────────────────────────────────────

    async def _start_customcert(self):
        """
        Admin ma juz WLASNA domene (DNS wskazujacy na jego stale IP/router z
        przekierowanym portem) i WLASNY, gdzies wykupiony/wystawiony
        certyfikat SSL - w odroznieniu od _start_staticip (zawsze generuje
        self-signed) i _start_duckdns (zawsze Let's Encrypt), tu NIC nie
        generujemy. Tylko wskazujemy istniejace pliki PEM.

        server.py (glowny serwer HTTP/HTTPS) juz umie zaladowac certPath/
        keyPath z tunnel_config.json - to DOKLADNIE ten sam mechanizm co
        certyfikat Let's Encrypt z trybu DuckDNS (patrz "Najpierw sprawdz
        certyfikat z konfiguracji tunelu" w server.py), wiec tu wystarczy
        zwalidowac ze pliki istnieja i ustawic te same dwa pola.
        """
        hostname = self._cfg.get("customHostname", "").strip()
        port     = (self._cfg.get("customPort", "8001") or "8001").strip()
        if not hostname:
            self._error  = "Brak domeny"
            self._status = "error"
            await self._broadcast()
            return

        # Puste pola = domyslna lokalizacja ktorej server.py i tak szuka jako
        # fallback (cert.pem/key.pem w katalogu danych) - user moze tam po
        # prostu podmienic pliki i nic wiecej nie wpisywac.
        cert = (self._cfg.get("customCertPath", "") or "").strip() or str(_DATA / "cert.pem")
        key  = (self._cfg.get("customKeyPath", "")  or "").strip() or str(_DATA / "key.pem")

        if not pathlib.Path(cert).exists() or not pathlib.Path(key).exists():
            self._error  = (f"Nie znaleziono plikow certyfikatu — umiesc wlasny "
                             f"cert.pem/key.pem pod: {cert}  /  {key}")
            self._status = "error"
            await self._broadcast()
            return

        self._cfg["certPath"] = cert
        self._cfg["keyPath"]  = key
        save_cfg(self._cfg)

        self._public_url = f"https://{hostname}:{port}"
        self._status     = "connected"
        await self._broadcast()
        print(f"[tunnel] Wlasny certyfikat: {self._public_url} (cert={cert})", flush=True)
        # server.py laduje cert PRZY STARCIE; jesli HTTPS juz wtedy dzialal
        # (choc self-signed), hot-reload watcher (co 6h) sam podmieni go na
        # zywo po podmianie plikow na dysku. Jesli HTTPS w ogole nie wstal
        # (np. brak "cryptography" albo to pierwszy start bez zadnego certu)
        # - potrzebny restart. Nie da sie stad wiarygodnie odroznic ktory to
        # przypadek, wiec informujemy zawsze, na wszelki wypadek.
        await self._broadcast_msg(
            "✓ Wskazano własny certyfikat. Jeśli HTTPS jeszcze nie działał "
            "(pierwsze uruchomienie), zrestartuj serwer — inaczej wystarczy "
            "poczekać do 6h na automatyczne przeładowanie na żywo.")

    def _generate_selfsigned(self, cn: str) -> tuple:
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            import datetime, ipaddress

            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            subject = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, cn),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Ham Radio CTRL"),
            ])
            try:
                san = x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(cn))])
            except Exception:
                san = x509.SubjectAlternativeName([x509.DNSName(cn)])

            cert = (x509.CertificateBuilder()
                .subject_name(subject).issuer_name(subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime.utcnow())
                .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
                .add_extension(san, critical=False)
                .sign(key, hashes.SHA256()))

            cp = _DATA / "selfsigned.crt"
            kp = _DATA / "selfsigned.key"
            cp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            kp.write_bytes(key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
            print(f"[tunnel] Self-signed cert: {cp}", flush=True)
            return str(cp), str(kp)
        except ImportError:
            print("[tunnel] Brak cryptography — pip install cryptography", flush=True)
            return "", ""
        except Exception as e:
            print(f"[tunnel] Błąd cert: {e}", flush=True)
            return "", ""

    async def install_certbot_task(self):
        """Zainstaluj certbot przez pip i powiadom UI."""
        print("[tunnel] install_certbot_task START", flush=True)
        await self._broadcast_msg("Instaluję certbot...")
        cb = await self._ensure_certbot()
        if cb:
            await self._broadcast_msg(f"✓ certbot zainstalowany: {cb}")
        else:
            await self._broadcast_msg("✗ Instalacja certbot nie powiodła się — spróbuj ręcznie: pip install certbot")

    async def gen_cert_task(self):
        """Wygeneruj certyfikat Let's Encrypt dla DuckDNS."""
        domain = self._cfg.get("duckDomain", "").strip().replace(".duckdns.org", "")
        if not domain:
            await self._broadcast_msg("✗ Brak domeny DuckDNS w konfiguracji")
            return
        fqdn = f"{domain}.duckdns.org"
        await self._broadcast_msg(f"Generuję certyfikat dla {fqdn}...")
        print(f"[tunnel] gen_cert_task: {fqdn}", flush=True)
        # Wyczysc stary cert zeby wymusic nowy
        self._cfg["certPath"] = ""
        self._cfg["keyPath"]  = ""
        cert, key = await self._get_letsencrypt_cert(fqdn, domain)
        if cert and key:
            self._cfg["certPath"] = cert
            self._cfg["keyPath"]  = key
            save_cfg(self._cfg)
            # WAZNE: serwer HTTPS zaladowal certyfikat przy STARCIE i trzyma go
            # w pamieci. Nowy cert Let's Encrypt zapisany do configu, ale zeby
            # serwer go uzyl - MUSI byc restart. Bez tego dalej serwuje stary
            # self-signed (przegladarka pokazuje ostrzezenie).
            await self._broadcast_msg(
                "✓ Certyfikat Let's Encrypt gotowy! ZRESTARTUJ serwer, "
                "aby zaczal go uzywac (teraz dziala jeszcze stary certyfikat).")
            print("[tunnel] ============================================", flush=True)
            print("[tunnel] CERTYFIKAT LET'S ENCRYPT GOTOWY", flush=True)
            print(f"[tunnel] cert: {cert}", flush=True)
            print("[tunnel] ZRESTARTUJ SERWER aby zaczal uzywac nowego certu.", flush=True)
            print("[tunnel] (serwer laduje cert przy starcie - teraz trzyma stary)", flush=True)
            print("[tunnel] ============================================", flush=True)
        else:
            # Show the specific reason if we captured one (e.g. certbot needs
            # Administrator rights) — a generic "check the logs" leaves a new
            # club stuck. _last_cert_error is set in the certbot runner above.
            _reason = getattr(self, "_last_cert_error", "") or \
                "Generowanie certyfikatu nie powiodlo sie — sprawdz logi serwera."
            await self._broadcast_msg("✗ " + _reason)

    async def _broadcast_msg(self, msg: str):
        """Wyślij toast do UI."""
        try:
            await self.hub.broadcast({"type": "toast", "message": msg})
        except Exception:
            print(f"[tunnel] {msg}", flush=True)

    async def stop(self):
        """Zatrzymaj tunel."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._proc:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except Exception:
                try: self._proc.kill()
                except Exception: pass
            self._proc = None
        self._status     = "stopped"
        self._public_url = ""
        await self._broadcast()
        print("[tunnel] Zatrzymany")

    async def _run(self, cmd: list):
        """Uruchom cloudflared i parsuj URL z output."""
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            print(f"[tunnel] Uruchomiony PID={self._proc.pid}")

            # Czytaj output i szukaj publicznego URL
            async for raw in self._proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue

                # Quick tunnel URL: https://xxx.trycloudflare.com
                m = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                if m and not self._public_url:
                    self._public_url = m.group()
                    self._status     = "connected"
                    print(f"[tunnel] Publiczny URL: {self._public_url}")
                    await self._broadcast()

                # Named tunnel — szukaj "Connected" lub hostname
                if "Connected" in line or "connection registered" in line.lower():
                    if not self._public_url:
                        host = self._cfg.get("hostname", "")
                        if host:
                            self._public_url = f"https://{host}"
                    self._status = "connected"
                    await self._broadcast()

                # Named tunnel URL z loga
                m2 = re.search(r'https://[a-zA-Z0-9.-]+\.[a-z]{2,}', line)
                if m2 and not self._public_url and self._status == "starting":
                    url = m2.group()
                    if "cloudflare" not in url and "github" not in url:
                        self._public_url = url
                        self._status     = "connected"
                        await self._broadcast()

                # Błędy
                if "error" in line.lower() and self._status != "connected":
                    self._error = line[:200]

            await self._proc.wait()
            rc = self._proc.returncode
            print(f"[tunnel] Zakończony, kod={rc}")
            if self._status != "stopped":
                self._status = "stopped"
                self._public_url = ""
                await self._broadcast()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._error  = str(e)
            self._status = "error"
            print(f"[tunnel] Błąd: {e}")
            await self._broadcast()

    # ── Autostart ─────────────────────────────────────────────────────────────

    async def autostart(self):
        """Uruchom automatycznie przy starcie serwera jeśli skonfigurowane."""
        cfg = load_cfg()
        if cfg.get("autoStart") and cfg.get("mode", "off") != "off":
            print(f"[tunnel] Autostart: tryb={cfg['mode']}")
            await asyncio.sleep(2)  # poczekaj na pełny start serwera
            await self.start()

    # ── Broadcast ─────────────────────────────────────────────────────────────

    async def _broadcast(self):
        await self.hub.broadcast({
            "type":   "tunnel_update",
            "tunnel": self.get_status(),
        })
