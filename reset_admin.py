"""
reset_admin.py — Emergency admin password reset (run from the console).

Usage:
    cd D:\\HAMCTRL          (or wherever this script lives)
    py reset_admin.py

Interactively asks for a new password and updates users.json.
If the password is forgotten or the 'admin' user can't log in, run this
script on the server (NOT remotely) to restore access.

Requires:
- Python 3.9+
- users.json reachable via config.py's data directory (dev: next to this
  script; installed EXE: %APPDATA%\\HAMCTRL - see config._writable_data_dir),
  or next to this script / in the current directory as a fallback.
"""

import hashlib
import json
import sys
from pathlib import Path


def hash_pw(pw: str) -> str:
    """Hash the admin password. Prefer scrypt (auth.hash_pw_secure); fall back to
    legacy SHA-256 only if auth can't be imported (standalone emergency use).
    A legacy hash still works — it upgrades to scrypt on next login."""
    try:
        from auth import hash_pw_secure
        return hash_pw_secure(pw)
    except Exception:
        return hashlib.sha256(pw.encode()).hexdigest()


def _candidate_paths() -> list[Path]:
    """users.json locations to try, in priority order. The first one uses
    config.py's own logic for finding the app's writable data directory
    (handles dev / portable-next-to-EXE / %APPDATA%\\HAMCTRL correctly) -
    that's the authoritative location the running app actually reads/writes.
    The rest are plain fallbacks in case config.py can't be imported."""
    candidates = []
    try:
        from config import USR_F
        candidates.append(USR_F)
    except Exception:
        pass
    candidates.append(Path(__file__).parent / "users.json")
    candidates.append(Path.cwd() / "users.json")
    return candidates


def find_users_json() -> Path:
    """Find users.json - see _candidate_paths() for the search order."""
    for c in _candidate_paths():
        if c.exists():
            return c
    return None


def main():
    print("=" * 60)
    print(" ADMIN PASSWORD RESET")
    print("=" * 60)

    usr_f = find_users_json()
    if not usr_f:
        print("ERROR: could not find users.json")
        print("Checked locations:")
        for c in _candidate_paths():
            print(f"  {c}")
        sys.exit(1)

    print(f"Loading: {usr_f}")

    # Load
    try:
        with open(usr_f, "r", encoding="utf-8") as f:
            users = json.load(f)
    except Exception as e:
        print(f"LOAD ERROR: {e}")
        sys.exit(2)

    if not isinstance(users, list):
        # The format may be {"users": [...]}
        if isinstance(users, dict) and "users" in users:
            users_list = users["users"]
        else:
            print(f"ERROR: users.json has an unexpected format")
            sys.exit(3)
    else:
        users_list = users

    # Show current users
    print(f"\nFound {len(users_list)} users:")
    for i, u in enumerate(users_list, 1):
        active = "active" if u.get("active", True) else "INACTIVE"
        print(f"  {i}. {u.get('username', '?'):20s} role={u.get('role', '?'):10s} [{active}]")

    # Find the admin (username == 'admin', case-insensitive)
    admin = next((u for u in users_list if u.get("username", "").lower() == "admin"), None)
    if not admin:
        # Try by role
        admins = [u for u in users_list if u.get("role") == "admin"]
        if not admins:
            print("\nERROR: no user with the admin role exists")
            sys.exit(4)
        if len(admins) == 1:
            admin = admins[0]
            print(f"\nNote: no user with username='admin', but there is one admin: {admin['username']}")
        else:
            print("\nFound multiple admins, pick a number:")
            for i, u in enumerate(admins, 1):
                print(f"  {i}. {u['username']}")
            try:
                idx = int(input("> ")) - 1
                admin = admins[idx]
            except (ValueError, IndexError):
                print("ERROR: invalid choice")
                sys.exit(5)

    print(f"\nResetting password for: {admin.get('username')} (role={admin.get('role')})")

    # Enter the new password
    print()
    print("Enter the new password (press Enter to use the default 'Admin1234!'):")
    new_pw = input("> ").strip()
    if not new_pw:
        new_pw = "Admin1234!"
        print(f"Using default: {new_pw}")

    # Save changes
    admin["password"] = hash_pw(new_pw)
    admin["active"] = True  # make sure the account is active
    # Remove any leftover lock/reset token
    admin.pop("reset_token", None)

    # Backup
    backup = usr_f.with_suffix(".json.bak")
    try:
        with open(backup, "w", encoding="utf-8") as f:
            json.dump(users if isinstance(users, list) else {"users": users_list},
                       f, indent=2, ensure_ascii=False)
        print(f"Backup: {backup}")
    except Exception as e:
        print(f"WARNING: could not create a backup: {e}")

    # Save users.json
    try:
        with open(usr_f, "w", encoding="utf-8") as f:
            if isinstance(users, list):
                json.dump(users_list, f, indent=2, ensure_ascii=False)
            else:
                users["users"] = users_list
                json.dump(users, f, indent=2, ensure_ascii=False)
        print(f"\n✓ Admin password reset: {admin.get('username')} / {new_pw}")
        print(f"✓ Saved: {usr_f}")
        print("\nRESTART Python (webapp.py) for the change to take effect!")
    except Exception as e:
        print(f"WRITE ERROR: {e}")
        sys.exit(6)


if __name__ == "__main__":
    main()
