"""
reset_admin.py — Awaryjne resetowanie hasla admin (uruchamiane z konsoli).

Uzycie:
    cd C:\\Users\\sp3gsk\\ham
    py reset_admin.py

Interaktywnie zapyta o nowe haslo, zaktualizuje users.json.
Jesli haslo zapomniane albo user 'admin' nie moze sie zalogowac, uruchamiasz
ten skrypt na serwerze (NIE zdalnie) zeby przywrocic dostep.

Wymaga:
- Python 3.9+
- users.json w tej samej sciezce co skrypt.py (albo `ham/` obok)
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


def find_users_json() -> Path:
    """Znajdz users.json - w BASE (ham/) albo w cwd."""
    candidates = [
        Path(__file__).parent / "ham" / "users.json",  # C:\Users\sp3gsk\ham\ham\users.json
        Path(__file__).parent / "users.json",          # C:\Users\sp3gsk\ham\users.json
        Path.cwd() / "ham" / "users.json",
        Path.cwd() / "users.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def main():
    print("=" * 60)
    print(" RESET HASLA ADMIN")
    print("=" * 60)

    usr_f = find_users_json()
    if not usr_f:
        print("BLAD: nie moge znalezc users.json")
        print("Sprawdzone lokalizacje:")
        for c in [
            Path(__file__).parent / "ham" / "users.json",
            Path(__file__).parent / "users.json",
            Path.cwd() / "ham" / "users.json",
            Path.cwd() / "users.json",
        ]:
            print(f"  {c}")
        sys.exit(1)

    print(f"Wczytuje: {usr_f}")

    # Wczytaj
    try:
        with open(usr_f, "r", encoding="utf-8") as f:
            users = json.load(f)
    except Exception as e:
        print(f"BLAD wczytania: {e}")
        sys.exit(2)

    if not isinstance(users, list):
        # Format moze byc {"users": [...]}
        if isinstance(users, dict) and "users" in users:
            users_list = users["users"]
        else:
            print(f"BLAD: users.json ma nieoczekiwany format")
            sys.exit(3)
    else:
        users_list = users

    # Pokaz aktualnych userow
    print(f"\nZnaleziono {len(users_list)} uzytkownikow:")
    for i, u in enumerate(users_list, 1):
        active = "aktywny" if u.get("active", True) else "NIEAKTYWNY"
        print(f"  {i}. {u.get('username', '?'):20s} role={u.get('role', '?'):10s} [{active}]")

    # Znajdz admina (username == 'admin', case-insensitive)
    admin = next((u for u in users_list if u.get("username", "").lower() == "admin"), None)
    if not admin:
        # Sprobuj po roli
        admins = [u for u in users_list if u.get("role") == "admin"]
        if not admins:
            print("\nBLAD: nie ma zadnego uzytkownika z rola admin")
            sys.exit(4)
        if len(admins) == 1:
            admin = admins[0]
            print(f"\nUwaga: nie ma usera o username='admin', ale jest jeden admin: {admin['username']}")
        else:
            print("\nZnaleziono wielu adminow, wybierz numer:")
            for i, u in enumerate(admins, 1):
                print(f"  {i}. {u['username']}")
            try:
                idx = int(input("> ")) - 1
                admin = admins[idx]
            except (ValueError, IndexError):
                print("BLAD: nieprawidlowy wybor")
                sys.exit(5)

    print(f"\nResetuje haslo dla: {admin.get('username')} (role={admin.get('role')})")

    # Wpisz nowe haslo
    print()
    print("Wpisz nowe haslo (Enter aby uzyc domyslnego 'Admin1234!'):")
    new_pw = input("> ").strip()
    if not new_pw:
        new_pw = "Admin1234!"
        print(f"Uzywam domyslnego: {new_pw}")

    # Zapisz zmiany
    admin["password"] = hash_pw(new_pw)
    admin["active"] = True  # upewnij sie ze konto jest aktywne
    # Usun ewentualny lock/reset token jesli tam byl
    admin.pop("reset_token", None)

    # Backup
    backup = usr_f.with_suffix(".json.bak")
    try:
        with open(backup, "w", encoding="utf-8") as f:
            json.dump(users if isinstance(users, list) else {"users": users_list},
                       f, indent=2, ensure_ascii=False)
        print(f"Kopia zapasowa: {backup}")
    except Exception as e:
        print(f"UWAGA: nie moge zrobic backupu: {e}")

    # Zapisz users.json
    try:
        with open(usr_f, "w", encoding="utf-8") as f:
            if isinstance(users, list):
                json.dump(users_list, f, indent=2, ensure_ascii=False)
            else:
                users["users"] = users_list
                json.dump(users, f, indent=2, ensure_ascii=False)
        print(f"\n✓ Haslo admin zresetowane: {admin.get('username')} / {new_pw}")
        print(f"✓ Zapisano: {usr_f}")
        print("\nRESTART Python (webapp.py) zeby zmiany zadzialaly!")
    except Exception as e:
        print(f"BLAD zapisu: {e}")
        sys.exit(6)


if __name__ == "__main__":
    main()
