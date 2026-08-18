#!/usr/bin/env python3
"""
launcher.py — entry point for the bundled EXE (HAM RADIO CTRL).

Responsibilities:
  1. Start the server (server.main()) in a background thread.
  2. Wait until the server starts responding on the HTTPS port.
  3. Open a browser at https://localhost:<port>.
  4. Keep the process alive (the server runs in the background); Ctrl+C /
     closing the window ends the run.

Behaves the same in dev mode (python launcher.py) as in the bundled EXE.
"""
import sys
import time
import socket
import threading
import webbrowser
import os

# ── BLAS/numpy thread limit — CRITICAL for audio smoothness ──────────────────
# numpy/scipy launch BLAS on ALL cores by default. A single FFT operation
# (CW filter, waterfall, resampling) could briefly pin the whole CPU (100%
# in /perf) and stall the audio path — causing momentary audio glitches.
# Capping it at 2 threads: FFT stays fast, but CPU is left for audio/network.
# MUST run before numpy's first import (otherwise BLAS has already initialized).
for _v in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
           "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_v, "2")


def _setup_dll_path():
    """
    In the bundled EXE (PyInstaller), native DLLs (libopus, opus) end up in
    _MEIPASS, which is NOT on the system PATH. opuslib loads libopus via
    ctypes, searching PATH - so without this it won't find the DLL and
    audio will fail. Add the DLL directory to the search path (Windows).
    """
    if not getattr(sys, "frozen", False):
        return  # dev mode - DLLs come from the system/venv
    base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    try:
        # Python 3.8+ on Windows: the official way to add a DLL search path
        if hasattr(os, "add_dll_directory"):
            os.add_dll_directory(base)
        # Also add to PATH (for ctypes.util.find_library and older mechanisms)
        os.environ["PATH"] = base + os.pathsep + os.environ.get("PATH", "")
    except Exception as e:
        print(f"[launcher] Warning: could not set the DLL path: {e}",
              flush=True)


_setup_dll_path()


def _wait_for_port(host: str, port: int, timeout: float = 30.0) -> bool:
    """Wait until the port starts accepting connections (the server is up)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(0.3)
    return False


def _open_browser_when_ready(https_port: int, http_port: int):
    """
    In a separate thread: wait until the server is up, then open a browser.
    Prefer HTTPS (the TX microphone requires a secure context), falling
    back to HTTP if HTTPS doesn't start.
    """
    # Try HTTPS first
    if _wait_for_port("127.0.0.1", https_port, timeout=30.0):
        url = f"https://localhost:{https_port}"
    elif _wait_for_port("127.0.0.1", http_port, timeout=5.0):
        url = f"http://localhost:{http_port}"
    else:
        print("[launcher] The server didn't come up in the expected time — "
              "open the browser manually.", flush=True)
        return

    print(f"[launcher] Server ready — opening the browser: {url}", flush=True)
    print(f"[launcher] If it didn't open, go here manually: {url}", flush=True)
    # Brief pause so the server fully initializes its handlers
    time.sleep(0.5)
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"[launcher] Could not open the browser: {e}", flush=True)


def _reset_admin():
    """
    Emergency reset of the admin password to the default (Admin1234!). Uses
    paths from config (so it hits the right users.json - next to the EXE
    or in APPDATA). After the reset, log in with Admin1234! and the wizard
    will force a password change again.

    Invocation: HAM-RADIO-CTRL.exe --reset-admin
    """
    import json
    import hashlib
    try:
        from config import USR_F, ADMIN_PW
    except Exception as e:
        print(f"[reset] Could not load config: {e}", flush=True)
        return 1

    def hash_pw(pw):
        return hashlib.sha256(pw.encode()).hexdigest()

    print("=" * 56, flush=True)
    print("  ADMIN PASSWORD RESET", flush=True)
    print("=" * 56, flush=True)
    print(f"Users file: {USR_F}", flush=True)

    try:
        if USR_F.exists():
            users = json.loads(USR_F.read_text(encoding="utf-8"))
        else:
            users = []
    except Exception as e:
        print(f"[reset] Error reading users.json: {e}", flush=True)
        users = []

    # Find the admin, reset the password + flags
    found = False
    for u in users:
        if u.get("role") == "admin" or u.get("username") == "admin":
            u["password"] = hash_pw(ADMIN_PW)
            u["active"] = True
            u["pw_changed"] = False           # the wizard will force a change again
            u["pw_ver"] = int(u.get("pw_ver", 0)) + 1  # invalidate old tokens
            found = True
            print(f"[reset] Reset admin '{u.get('username')}'", flush=True)

    if not found:
        # No admin - create a new one
        users.append({"id": "1", "username": "admin",
                      "password": hash_pw(ADMIN_PW), "role": "admin",
                      "active": True, "pw_changed": False, "pw_ver": 1})
        print("[reset] Created a new admin account", flush=True)

    try:
        USR_F.write_text(json.dumps(users, indent=2, ensure_ascii=False),
                         encoding="utf-8")
        print(f"\n[reset] DONE. Log in:", flush=True)
        print(f"        username: admin", flush=True)
        print(f"        password: {ADMIN_PW}", flush=True)
        print(f"\n        The wizard will ask for a new password at login.", flush=True)
    except Exception as e:
        print(f"[reset] WRITE ERROR: {e}", flush=True)
        return 1
    return 0


def _gen_cert():
    """
    Generate/renew a Let's Encrypt certificate - STANDALONE mode (as admin).
    Kept separate from the normal server startup: certbot requires admin
    rights, but the server itself does NOT. This way the server runs
    normally (no admin, no Defender warnings), and the cert is generated
    separately roughly every ~90 days with this shortcut.

    Invocation: HAM-RADIO-CTRL.exe --gen-cert   (run as administrator)
    """
    import asyncio
    print("=" * 56, flush=True)
    print("  GENERATING LET'S ENCRYPT CERTIFICATE", flush=True)
    print("=" * 56, flush=True)

    # certbot on Windows must run elevated (writes the system cert store).
    # If we are NOT admin, re-launch this same command through UAC so the user
    # just clicks "Yes" instead of hitting a cryptic certbot error. This makes
    # the Start-menu shortcut work even without an elevation flag.
    if sys.platform == "win32":
        try:
            import ctypes
            _is_admin = ctypes.windll.shell32.IsUserAnAdmin()
        except Exception:
            _is_admin = 0
        if not _is_admin:
            print("No administrator rights — requesting elevation (UAC)...",
                  flush=True)
            try:
                import ctypes
                if getattr(sys, "frozen", False):
                    _exe, _params = sys.executable, "--gen-cert"
                else:
                    _exe, _params = sys.executable, f'"{os.path.abspath(sys.argv[0])}" --gen-cert'
                # ShellExecute with "runas" = UAC prompt; relaunches as admin
                _rc = ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", _exe, _params, None, 1)
                if _rc <= 32:
                    print(f"Could not elevate (code {_rc}). "
                          "Right-click the shortcut → Run as administrator.",
                          flush=True)
                    if getattr(sys, "frozen", False):
                        input("\nPress Enter to close...")
                    return 1
                # The elevated instance takes over — this one exits quietly.
                return 0
            except Exception as e:
                print(f"Elevation error: {e}", flush=True)
                print("Right-click the shortcut → Run as administrator.",
                      flush=True)
                if getattr(sys, "frozen", False):
                    input("\nPress Enter to close...")
                return 1

    print("Administrator mode OK — generating the certificate.", flush=True)
    print("", flush=True)

    class _DummyHub:
        """Minimal hub - gen_cert_task only broadcasts status."""
        async def broadcast(self, msg):
            t = msg.get("type", "")
            if t == "tunnel_msg" or "msg" in msg:
                print(f"  {msg.get('msg', msg)}", flush=True)

    async def _run():
        try:
            from tunnel_manager import TunnelManager
            tm = TunnelManager(_DummyHub())
            await tm.gen_cert_task()
        except Exception as e:
            print(f"[gen-cert] ERROR: {e}", flush=True)
            return 1
        return 0

    code = asyncio.run(_run())
    print("", flush=True)
    if code == 0:
        print("DONE. Now start the server NORMALLY (without admin) -", flush=True)
        print("it will automatically use the new certificate.", flush=True)
    if getattr(sys, "frozen", False):
        input("\nPress Enter to close...")
    return code


def main():
    # Emergency admin password reset: HAM-RADIO-CTRL.exe --reset-admin
    if "--reset-admin" in sys.argv:
        code = _reset_admin()
        if getattr(sys, "frozen", False):
            input("\nPress Enter to close...")
        sys.exit(code)

    # Generate a Let's Encrypt cert (as admin): HAM-RADIO-CTRL.exe --gen-cert
    if "--gen-cert" in sys.argv:
        sys.exit(_gen_cert())

    # Ports: defaults from config (HTTP 8000, HTTPS 8001). Imported AFTER
    # so config detects the paths (frozen/not frozen) and generates .env on
    # the first run.
    try:
        from config import PORT
    except Exception:
        PORT = 8000
    http_port  = PORT
    https_port = PORT + 1  # server.py: HTTPS = PORT+1 (8001 when PORT=8000)

    print("=" * 56, flush=True)
    print("  HAM RADIO CTRL — starting the server...", flush=True)
    print("=" * 56, flush=True)

    # Thread that opens the browser once the server is up
    t = threading.Thread(
        target=_open_browser_when_ready,
        args=(https_port, http_port),
        daemon=True,
    )
    t.start()

    # Start the server (blocks until shutdown). server.main() runs its own asyncio.run.
    try:
        import server
        server.main()
    except KeyboardInterrupt:
        print("\n[launcher] Stopped (Ctrl+C).", flush=True)
    except Exception as e:
        print(f"[launcher] SERVER ERROR: {e}", flush=True)
        import traceback
        traceback.print_exc()
        # In EXE mode the console window would close immediately - let the user see the error
        if getattr(sys, "frozen", False):
            input("\nPress Enter to close...")
        sys.exit(1)


if __name__ == "__main__":
    main()
