#!/usr/bin/env python3
"""
tunnel_manager.py — manages the cloudflared process
Modes:
  quick  — Cloudflare Quick Tunnel (random address, no account)
  named  — own domain via a Cloudflare Tunnel token
  off    — disabled

NOTE ON TRANSLATION SCOPE: every literal string assigned to self._error /
self._last_cert_error, and every string passed to _broadcast_msg(), reaches
the browser unmodified — self._error via the "tunnel_update" WS broadcast's
"error" field (public/js/tunnel.js's renderError() sets it as raw
el.textContent, no I18n), and _broadcast_msg() via the "toast" WS message
(public/js/ws.js's case 'toast' calls UI.showToast(msg.message) directly).
Those strings are deliberately left in Polish throughout this file; only
comments, docstrings, and print() console-log text are translated.
"""
import asyncio
import json
import os
import pathlib
import re
import subprocess
import sys

# DATA = writable data directory (next to the EXE or in APPDATA). Tunnel
# files (config, certs) must go there, not into read-only Program Files.
try:
    from config import DATA as _DATA
except Exception:
    _DATA = pathlib.Path(".")

try:
    from crypto_secrets import encrypt_secret, decrypt_secret, PREFIX as _ENC_PREFIX
except Exception:
    def encrypt_secret(v): return v
    def decrypt_secret(v): return v
    _ENC_PREFIX = "enc1:"

_SECRET_KEYS = ("token", "duckToken")

CFG_FILE = _DATA / "tunnel_config.json"
CF_EXE   = (_DATA / "cloudflared.exe") if sys.platform == "win32" else (_DATA / "cloudflared")

DEFAULT_CFG = {
    "mode":      "off",
    "token":     "",
    "hostname":  "your-server.example.com",
    "autoStart": False,
}


def _safe_exists(p: pathlib.Path) -> bool:
    """
    pathlib.Path.exists() raises PermissionError instead of returning False
    when the containing directory has restrictive ACLs the current token
    can't even stat() (e.g. a leftover certbot config/live/ tree created
    under an elevated run, pre-win-acme). Treat "can't tell" as "not there"
    instead of crashing the caller.
    """
    try:
        return p.exists()
    except PermissionError:
        print(f"[tunnel] WARNING: no permission to access {p} "
              f"(leftover from an earlier elevated cert-gen run?) - treating as missing", flush=True)
        return False


def load_cfg() -> dict:
    """Load the config and decrypt tokens — in memory (self._cfg) we always
    keep plaintext, since cloudflared/win-acme use it directly; only the
    on-disk file is encrypted (see save_cfg)."""
    try:
        raw = {**DEFAULT_CFG, **json.loads(CFG_FILE.read_text())}
    except Exception:
        return dict(DEFAULT_CFG)
    cfg = dict(raw)
    needs_migration = False
    for key in _SECRET_KEYS:
        val = cfg.get(key)
        if val:
            if not val.startswith(_ENC_PREFIX):
                needs_migration = True
            cfg[key] = decrypt_secret(val)
    if needs_migration:
        # The old file had plaintext tokens (from before encryption at
        # rest) — write them back encrypted right away, without waiting
        # for the next save.
        save_cfg(cfg)
        print("[tunnel] encrypted tokens in tunnel_config.json", flush=True)
    return cfg


def save_cfg(cfg: dict):
    """Save the config to disk with encrypted tokens (Cloudflare Tunnel,
    DuckDNS) — does not mutate the passed-in dict (that's the live
    self._cfg used directly to launch processes, it must stay plaintext)."""
    on_disk = dict(cfg)
    for key in _SECRET_KEYS:
        if on_disk.get(key):
            on_disk[key] = encrypt_secret(on_disk[key])
    try:
        CFG_FILE.write_text(json.dumps(on_disk, indent=2))
    except Exception as e:
        print(f"[tunnel] Config write error: {e}")


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

    # ── State ──────────────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        cert_days = None
        cert = self._cfg.get("certPath", "")
        if cert and _safe_exists(pathlib.Path(cert)):
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
        """Find cloudflared in the project folder or PATH."""
        if CF_EXE.exists():
            return CF_EXE
        # Search PATH
        import shutil
        found = shutil.which("cloudflared")
        if found:
            return pathlib.Path(found)
        return None

    async def _download_cloudflared(self) -> bool:
        """Download cloudflared if missing."""
        if self._find_cloudflared():
            return True
        print("[tunnel] Downloading cloudflared...")
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
            print(f"[tunnel] cloudflared downloaded: {dest}")
            return True
        except Exception as e:
            self._error = f"Nie można pobrać cloudflared: {e}"
            print(f"[tunnel] {self._error}")
            return False

    def check_available(self) -> dict:
        """Check whether cloudflared is available + info on stale processes."""
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
        """Count stale cloudflared processes on the system."""
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
        """Check whether cloudflared is installed as a Windows service."""
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
        """Clean up stale processes and remove the cloudflared Windows service."""
        killed = 0
        svc_removed = False
        msgs = []

        # 1. Stop the current tunnel
        await self.stop()

        # 2. Kill all cloudflared processes
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

        # 3. Uninstall the Windows service
        if sys.platform == "win32":
            cf = self._find_cloudflared()
            if cf:
                try:
                    # First uninstall via cloudflared service uninstall
                    subprocess.run([str(cf), "service", "uninstall"],
                        capture_output=True, timeout=10)
                    # Then remove via sc delete
                    r = subprocess.run(["sc", "delete", "cloudflared"],
                        capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10)
                    if "SUCCESS" in r.stdout or "[SC]" in r.stdout:
                        svc_removed = True
                        msgs.append("Usługa Windows cloudflared usunięta")
                except Exception as e:
                    msgs.append(f"Usługa: {e}")

        # 4. Remove the cloudflared.exe file if it's local
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
        """Start the tunnel."""
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

        # Modes that don't use cloudflared
        if mode == "duckdns":
            await self._start_duckdns()
            return
        if mode == "staticip":
            await self._start_staticip()
            return
        if mode == "customcert":
            await self._start_customcert()
            return

        # Cloudflare modes — download cloudflared if missing
        if not await self._download_cloudflared():
            self._status = "error"
            await self._broadcast()
            return

        cf = self._find_cloudflared()
        if mode == "quick":
            cmd = [str(cf), "tunnel", "--url", self._local_url]
        else:
            # Named tunnel via token
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
        # Strip .duckdns.org if the user pasted the full domain
        domain = domain.replace(".duckdns.org", "").strip()
        if not domain or not token:
            self._error = "Brak domeny lub tokenu DuckDNS"
            return False
        try:
            import urllib.request, urllib.parse, ssl
            # urllib.urlopen is BLOCKING (DNS + HTTP + up to a 10s timeout).
            # Even though this function is async, the urlopen call itself
            # was freezing the whole event loop for the duration of the
            # request — caught via looplag (stack: getaddrinfo in the
            # DuckDNS tunnel update). Offloaded to a thread so the loop
            # keeps running.
            def _do_update():
                # FULL certificate verification - duckdns.org has a normal,
                # publicly trusted cert, no reason to disable it. This used
                # to be disabled (check_hostname=False, verify_mode=
                # CERT_NONE), which made the DuckDNS tunnel vulnerable to
                # MITM - since the TOKEN travels in the URL, that meant it
                # could be intercepted. No comment in the code explained
                # why verification was supposed to be off - removed,
                # DuckDNS doesn't need this exception.
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

        # Update the IP
        ok = await self._duckdns_update_ip()
        if not ok:
            self._status = "error"
            await self._broadcast()
            return

        fqdn = f"{domain}.duckdns.org"
        port = self._cfg.get("staticPort", "8001")

        # Check for an existing certificate
        cert = self._cfg.get("certPath", "")
        key  = self._cfg.get("keyPath", "")

        # PRIORITY: if a real Let's Encrypt cert exists in pem/, use it -
        # even if the config points to a self-signed one. Otherwise a
        # once-saved self-signed cert would block using LE forever (since
        # "cert exists" = the condition below would be false).
        #
        # NOTE: an OLDER C:/.../letsencrypt/config/live/<fqdn>/ tree may
        # still exist on disk from before the win-acme switch (real
        # certbot set restrictive ACLs on it that only an elevated token
        # can even .exists()-check) - _safe_exists() below treats a
        # PermissionError as "not found" instead of crashing this whole
        # autostart task, which is what happened live before this fix.
        le_cert = _DATA / "letsencrypt" / "pem" / f"{fqdn}-chain.pem"
        le_key  = _DATA / "letsencrypt" / "pem" / f"{fqdn}-key.pem"
        _is_selfsigned = "selfsigned" in str(cert).lower()
        if _safe_exists(le_cert) and _safe_exists(le_key) and (not cert or _is_selfsigned or not _safe_exists(pathlib.Path(cert))):
            print(f"[tunnel] Using existing Let's Encrypt cert: {le_cert}", flush=True)
            cert = str(le_cert)
            key  = str(le_key)
            self._cfg["certPath"] = cert
            self._cfg["keyPath"]  = key
            save_cfg(self._cfg)

        if not cert or not _safe_exists(pathlib.Path(cert)):
            # Try Let's Encrypt
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

        # Check whether the cert needs renewal (< 30 days to expiry)
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
                print(f"[tunnel] cert valid for {days_left} more days", flush=True)
                if days_left < 30:
                    print("[tunnel] Renewing certificate...", flush=True)
                    new_cert, new_key = await self._get_letsencrypt_cert(fqdn, domain)
                    if new_cert:
                        cert, key = new_cert, new_key
                        self._cfg["certPath"] = cert
                        self._cfg["keyPath"]  = key
                        save_cfg(self._cfg)
            except Exception as e:
                print(f"[tunnel] cert check: {e}", flush=True)

        self._public_url = f"https://{fqdn}:{port}"
        self._status     = "connected"
        if not cert:
            self._error      = "Brak certyfikatu SSL"
            self._public_url = f"http://{fqdn}:{port}"
        await self._broadcast()
        print(f"[tunnel] DuckDNS running: {self._public_url}", flush=True)

        # Refresh the IP every 5 minutes
        self._task = asyncio.create_task(self._duckdns_loop(domain))

    async def _get_letsencrypt_cert(self, fqdn: str, duck_domain: str) -> tuple:
        """Get a Let's Encrypt certificate via an ACME DNS-01 challenge through DuckDNS."""
        duck_token = self._cfg.get("duckToken", "").strip()
        if not duck_token:
            return "", ""

        print(f"[tunnel] Let's Encrypt: attempting for {fqdn}", flush=True)

        # Check for an existing certificate in the standard locations.
        # C:/Certbot/live is kept for backward compat with installs that
        # already have a certbot-issued cert on disk from before the
        # win-acme switch (certbot discontinued Windows support Feb 2024).
        for base in [
            pathlib.Path(f"C:/Certbot/live/{fqdn}"),
            pathlib.Path(f"/etc/letsencrypt/live/{fqdn}"),
            (_DATA / "letsencrypt" / fqdn),
        ]:
            cp = base / "fullchain.pem"
            kp = base / "privkey.pem"
            if _safe_exists(cp) and _safe_exists(kp):
                print(f"[tunnel] Found cert: {cp}", flush=True)
                return str(cp), str(kp)
        _pem_dir = _DATA / "letsencrypt" / "pem"
        _wacs_cp = _pem_dir / f"{fqdn}-chain.pem"
        _wacs_kp = _pem_dir / f"{fqdn}-key.pem"
        if _safe_exists(_wacs_cp) and _safe_exists(_wacs_kp):
            print(f"[tunnel] Found cert: {_wacs_cp}", flush=True)
            return str(_wacs_cp), str(_wacs_kp)

        # Running win-acme requires Administrator rights on Windows. The server
        # normally runs WITHOUT admin, so an automatic attempt on every startup
        # would always fail and spam the log. If we have no existing cert and
        # no admin rights, skip win-acme entirely and fall back to self-signed
        # — the user generates the real cert once via the elevated
        # "Wygeneruj certyfikat (jako admin)" shortcut.
        if sys.platform == "win32":
            try:
                import ctypes
                _is_admin = ctypes.windll.shell32.IsUserAnAdmin()
            except Exception:
                _is_admin = 0
            if not _is_admin:
                print("[tunnel] No Let's Encrypt cert and no admin rights "
                      "- using self-signed. To get a real certificate, "
                      "run the 'Generate certificate (as admin)' shortcut from the Start menu.",
                      flush=True)
                return "", ""

        # Install win-acme if missing
        wacs = await self._ensure_wacs()
        print(f"[tunnel] win-acme path: {wacs!r}", flush=True)
        if not wacs:
            print("[tunnel] win-acme unavailable - using self-signed", flush=True)
            return "", ""

        # Create the DuckDNS create/delete scripts win-acme calls for DNS-01
        print(f"[tunnel] creating DuckDNS scripts for {duck_domain}", flush=True)
        create_script, delete_script = self._create_duckdns_dns_scripts(duck_domain, duck_token)
        print(f"[tunnel] scripts: {create_script} / {delete_script}", flush=True)

        # Run win-acme. --store pemfiles writes plain PEM files (like
        # certbot's certonly) instead of touching the Windows cert store /
        # IIS bindings, which we don't use.
        cmd = [
            wacs,
            "--source", "manual",
            "--host", fqdn,
            "--validationmode", "dns-01",
            "--validation", "script",
            "--dnscreatescript", create_script,
            "--dnscreatescriptarguments", "{RecordName} {Token}",
            "--dnsdeletescript", delete_script,
            "--dnsdeletescriptarguments", "{RecordName} {Token}",
            "--store", "pemfiles",
            "--pemfilespath", str(_DATA / "letsencrypt" / "pem"),
            "--pemfilesname", fqdn,
            "--accepttos", "--emailaddress", f"admin@{fqdn}",
            "--closeonfinish", "--notaskscheduler",
        ]
        print(f"[tunnel] running win-acme: {cmd[0]}", flush=True)
        # Run win-acme in a separate thread (Windows subprocess issue)
        def _run_wacs():
            import subprocess
            try:
                (_DATA / "letsencrypt" / "pem").mkdir(parents=True, exist_ok=True)
                # Remove stuck wacs instances from previous attempts.
                if sys.platform == "win32":
                    try:
                        subprocess.run(["taskkill", "/F", "/IM", "wacs.exe"],
                                       capture_output=True, timeout=10)
                    except Exception:
                        pass
                print(f"[tunnel] win-acme subprocess starting", flush=True)
                # stdin=DEVNULL: without an explicit valid stdin handle, this
                # process (and everything it spawns down the chain - cmd.exe,
                # our .bat scripts, curl.exe) can inherit a broken one and
                # fail with "Input redirection is not supported" (seen live
                # from the 'timeout' command inside duckdns_create.bat).
                r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                                    errors='replace', timeout=300, stdin=subprocess.DEVNULL)
                print(f"[tunnel] win-acme exit={r.returncode}", flush=True)
                if r.stdout: print(f"[tunnel] win-acme out: {r.stdout[-500:]}", flush=True)
                if r.stderr: print(f"[tunnel] win-acme err: {r.stderr[-500:]}", flush=True)
                # Recognize the most common first-run failure: needs elevated
                # (Administrator) rights. The raw "exit=1" tells a new club
                # nothing — capture this case so gen_cert_task can show a
                # clear, actionable message.
                _err = (r.stderr or "") + (r.stdout or "")
                if r.returncode != 0 and ("administrator" in _err.lower() or "elevat" in _err.lower()):
                    self._last_cert_error = (
                        "win-acme wymaga uprawnien administratora. Uruchom skrot "
                        "'Wygeneruj certyfikat (jako admin)' z menu Start "
                        "(prawy przycisk → Uruchom jako administrator), albo uruchom "
                        "serwer jako administrator tylko na czas generowania certyfikatu. "
                        "Na co dzien serwer NIE potrzebuje admina.")
                elif r.returncode != 0:
                    self._last_cert_error = (r.stderr or r.stdout or "").strip()[-300:] or \
                        "win-acme zwrocil blad — sprawdz logi konsoli."
                else:
                    self._last_cert_error = ""
                return r.returncode == 0
            except Exception as e:
                print(f"[tunnel] win-acme exception: {e}", flush=True)
                return False

        print("[tunnel] run_in_executor START", flush=True)
        try:
            ok = await asyncio.get_event_loop().run_in_executor(None, _run_wacs)
            print(f"[tunnel] run_in_executor DONE ok={ok}", flush=True)
            if ok:
                cp = (_DATA / "letsencrypt" / "pem" / f"{fqdn}-chain.pem")
                kp = (_DATA / "letsencrypt" / "pem" / f"{fqdn}-key.pem")
                if cp.exists():
                    return str(cp), str(kp)
        except Exception as e:
            print(f"[tunnel] win-acme exception: {e}", flush=True)
        return "", ""

    async def _ensure_wacs(self) -> str:
        """
        Find win-acme (wacs.exe). Certbot discontinued Windows support in
        Feb 2024, hence win-acme. It has no installer and no pip equivalent
        — it's a portable, self-contained exe the admin downloads once from
        win-acme.com and extracts. We check PATH + the typical extraction
        locations on Windows.
        """
        import shutil
        import pathlib

        # 1. PATH (most common)
        for name in ("wacs", "wacs.exe"):
            cb = shutil.which(name)
            if cb:
                print(f"[tunnel] wacs found: {cb}", flush=True)
                return cb

        # 2. Typical extraction locations on Windows (no installer, admin
        #    just unzips the portable release)
        win_paths = [
            pathlib.Path(r"C:\win-acme\wacs.exe"),
            pathlib.Path(r"C:\Program Files\win-acme\wacs.exe"),
            pathlib.Path(r"C:\Program Files (x86)\win-acme\wacs.exe"),
        ]
        for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")):
            if base:
                win_paths.append(pathlib.Path(base) / "win-acme" / "wacs.exe")
        for p in win_paths:
            if p.exists():
                print(f"[tunnel] wacs found: {p}", flush=True)
                return str(p)

        # 3. Not found. There's nothing to auto-install (no pip package, no
        #    silent installer) — give the admin clear instructions.
        print("[tunnel] ============================================", flush=True)
        print("[tunnel] WIN-ACME (wacs.exe) NOT FOUND", flush=True)
        print("[tunnel] Let's Encrypt requires win-acme to be installed.", flush=True)
        print("[tunnel] Admin: download the portable zip from https://www.win-acme.com/", flush=True)
        print("[tunnel]   (no installer - extract wacs.exe to C:\\win-acme\\)", flush=True)
        print("[tunnel] Restart the server after extracting.", flush=True)
        print("[tunnel] Alternative: use a Cloudflare tunnel (no win-acme needed).", flush=True)
        print("[tunnel] ============================================", flush=True)
        self._error = ("win-acme nie zainstalowany. Pobierz portable zip z win-acme.com "
                       "(bez instalatora, rozpakuj do C:\\win-acme\\) i zrestartuj "
                       "serwer, albo uzyj Cloudflare tunnel.")
        return ""

    def _create_duckdns_dns_scripts(self, domain: str, token: str) -> tuple:
        """
        Create the create/delete scripts win-acme calls for the DNS-01
        challenge via DuckDNS (--dnscreatescript / --dnsdeletescript). Unlike
        certbot's single reused hook, win-acme calls create during
        validation setup and delete automatically afterwards.

        IMPORTANT: same path-with-spaces problem as certbot before it — a
        path like 'C:\\Program Files (x86)\\HAM RADIO CTRL\\' breaks script
        invocation, so scripts MUST live in a directory WITHOUT spaces.
        We use C:\\HAMCTRL (or %TEMP% as fallback).
        """
        import tempfile
        hook_dir = None
        for cand in (pathlib.Path(r"C:\HAMCTRL"),
                     pathlib.Path(tempfile.gettempdir())):
            try:
                cand.mkdir(parents=True, exist_ok=True)
                if " " not in str(cand):
                    hook_dir = cand
                    break
            except Exception:
                continue
        if hook_dir is None:
            hook_dir = _DATA  # last resort (may have spaces)

        # Called as: duckdns_create.bat {RecordName} {Token} -> %1 %2
        # WITHOUT -k: curl verifies the duckdns.org cert by default
        # (publicly trusted, no reason to disable it - see the same
        # comment at _duckdns_update_ip, which had the identical issue).
        create = hook_dir / "duckdns_create.bat"
        create.write_text(
            f"@echo off\n"
            f"curl \"https://www.duckdns.org/update?domains={domain}&token={token}&txt=%~2&verbose=true\" <nul\n"
            f"echo Waiting 120 seconds for DNS propagation...\n"
            # 'timeout' needs a real console and fails with "Input redirection
            # is not supported" when launched without one (as here: win-acme
            # -> cmd.exe -> this script, no console) - use ping as a
            # console-free sleep instead (standard Windows batch workaround).
            f"ping -n 121 127.0.0.1 >nul\n",
            encoding="utf-8",
        )
        # DuckDNS has no real "delete a TXT record" - the next update just
        # overwrites it. The delete script just needs to exist and exit 0.
        delete = hook_dir / "duckdns_delete.bat"
        delete.write_text(
            "@echo off\nrem no cleanup needed for DuckDNS TXT records\n",
            encoding="utf-8",
        )
        return str(create.absolute()), str(delete.absolute())

    async def _duckdns_loop(self, domain: str):
        while True:
            await asyncio.sleep(300)
            try:
                await self._duckdns_update_ip()
            except Exception:
                pass

    # ── Static IP ─────────────────────────────────────────────────────────────

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
        if not cert or not _safe_exists(pathlib.Path(cert)):
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
        print(f"[tunnel] Static IP: {self._public_url}", flush=True)

    # ── Own domain + own certificate ────────────────────────────────────

    async def _start_customcert(self):
        """
        The admin already has THEIR OWN domain (DNS pointing at their
        static IP/router with a forwarded port) and their OWN SSL
        certificate obtained/issued elsewhere - unlike _start_staticip
        (always generates self-signed) and _start_duckdns (always Let's
        Encrypt), here we generate NOTHING. We just point at existing PEM
        files.

        server.py (the main HTTP/HTTPS server) already knows how to load
        certPath/keyPath from tunnel_config.json - this is EXACTLY the same
        mechanism as the Let's Encrypt certificate from DuckDNS mode (see
        "check the tunnel config for a certificate first" in server.py), so
        here it's enough to validate that the files exist and set those
        same two fields.
        """
        hostname = self._cfg.get("customHostname", "").strip()
        port     = (self._cfg.get("customPort", "8001") or "8001").strip()
        if not hostname:
            self._error  = "Brak domeny"
            self._status = "error"
            await self._broadcast()
            return

        # Empty fields = the default location server.py already looks for
        # as a fallback (cert.pem/key.pem in the data directory) - the user
        # can just drop files there and not fill in anything else.
        cert = (self._cfg.get("customCertPath", "") or "").strip() or str(_DATA / "cert.pem")
        key  = (self._cfg.get("customKeyPath", "")  or "").strip() or str(_DATA / "key.pem")

        if not _safe_exists(pathlib.Path(cert)) or not _safe_exists(pathlib.Path(key)):
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
        print(f"[tunnel] Own certificate: {self._public_url} (cert={cert})", flush=True)
        # server.py loads the cert AT STARTUP; if HTTPS was already working
        # then (even if self-signed), the hot-reload watcher (every 6h)
        # will swap it in live once the files on disk change. If HTTPS
        # never came up at all (e.g. no "cryptography" module, or this is
        # the first start with no cert at all) - a restart is needed. There
        # is no reliable way to tell which case it is from here, so we
        # inform the user every time, just in case.
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
            print("[tunnel] cryptography module missing - pip install cryptography", flush=True)
            return "", ""
        except Exception as e:
            print(f"[tunnel] Cert error: {e}", flush=True)
            return "", ""

    async def gen_cert_task(self):
        """Generate a Let's Encrypt certificate for DuckDNS."""
        domain = self._cfg.get("duckDomain", "").strip().replace(".duckdns.org", "")
        if not domain:
            await self._broadcast_msg("✗ Brak domeny DuckDNS w konfiguracji")
            return
        fqdn = f"{domain}.duckdns.org"
        await self._broadcast_msg(f"Generuję certyfikat dla {fqdn}...")
        print(f"[tunnel] gen_cert_task: {fqdn}", flush=True)
        # Clear the old cert to force a new one
        self._cfg["certPath"] = ""
        self._cfg["keyPath"]  = ""
        cert, key = await self._get_letsencrypt_cert(fqdn, domain)
        if cert and key:
            self._cfg["certPath"] = cert
            self._cfg["keyPath"]  = key
            save_cfg(self._cfg)
            # IMPORTANT: the HTTPS server loaded its certificate AT STARTUP
            # and holds it in memory. The new Let's Encrypt cert is saved to
            # the config, but for the server to use it - a restart is
            # REQUIRED. Without this it keeps serving the old self-signed
            # cert (the browser shows a warning).
            await self._broadcast_msg(
                "✓ Certyfikat Let's Encrypt gotowy! ZRESTARTUJ serwer, "
                "aby zaczal go uzywac (teraz dziala jeszcze stary certyfikat).")
            print("[tunnel] ============================================", flush=True)
            print("[tunnel] LET'S ENCRYPT CERTIFICATE READY", flush=True)
            print(f"[tunnel] cert: {cert}", flush=True)
            print("[tunnel] RESTART THE SERVER to start using the new cert.", flush=True)
            print("[tunnel] (the server loads the cert at startup - it's still holding the old one)", flush=True)
            print("[tunnel] ============================================", flush=True)
        else:
            # Show the specific reason if we captured one (e.g. win-acme needs
            # Administrator rights) — a generic "check the logs" leaves a new
            # club stuck. _last_cert_error is set in the win-acme runner above.
            _reason = getattr(self, "_last_cert_error", "") or \
                "Generowanie certyfikatu nie powiodlo sie — sprawdz logi serwera."
            await self._broadcast_msg("✗ " + _reason)

    async def _broadcast_msg(self, msg: str):
        """Send a toast to the UI."""
        try:
            await self.hub.broadcast({"type": "toast", "message": msg})
        except Exception:
            print(f"[tunnel] {msg}", flush=True)

    async def stop(self):
        """Stop the tunnel."""
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
        print("[tunnel] Stopped")

    async def _run(self, cmd: list):
        """Run cloudflared and parse the URL from its output."""
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            print(f"[tunnel] Started PID={self._proc.pid}")

            # Read the output and look for the public URL
            async for raw in self._proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue

                # Quick tunnel URL: https://xxx.trycloudflare.com
                m = re.search(r'https://[a-z0-9-]+\.trycloudflare\.com', line)
                if m and not self._public_url:
                    self._public_url = m.group()
                    self._status     = "connected"
                    print(f"[tunnel] Public URL: {self._public_url}")
                    await self._broadcast()

                # Named tunnel — look for "Connected" or a hostname
                if "Connected" in line or "connection registered" in line.lower():
                    if not self._public_url:
                        host = self._cfg.get("hostname", "")
                        if host:
                            self._public_url = f"https://{host}"
                    self._status = "connected"
                    await self._broadcast()

                # Named tunnel URL from the log
                m2 = re.search(r'https://[a-zA-Z0-9.-]+\.[a-z]{2,}', line)
                if m2 and not self._public_url and self._status == "starting":
                    url = m2.group()
                    if "cloudflare" not in url and "github" not in url:
                        self._public_url = url
                        self._status     = "connected"
                        await self._broadcast()

                # Errors
                if "error" in line.lower() and self._status != "connected":
                    self._error = line[:200]

            await self._proc.wait()
            rc = self._proc.returncode
            print(f"[tunnel] Exited, code={rc}")
            if self._status != "stopped":
                self._status = "stopped"
                self._public_url = ""
                await self._broadcast()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._error  = str(e)
            self._status = "error"
            print(f"[tunnel] Error: {e}")
            await self._broadcast()

    # ── Autostart ─────────────────────────────────────────────────────────────

    async def autostart(self):
        """Start automatically at server startup if configured."""
        cfg = load_cfg()
        if cfg.get("autoStart") and cfg.get("mode", "off") != "off":
            print(f"[tunnel] Autostart: mode={cfg['mode']}")
            await asyncio.sleep(2)  # wait for the server to fully start
            await self.start()

    # ── Broadcast ─────────────────────────────────────────────────────────────

    async def _broadcast(self):
        await self.hub.broadcast({
            "type":   "tunnel_update",
            "tunnel": self.get_status(),
        })
