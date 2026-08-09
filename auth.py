"""
auth.py — autoryzacja i kryptografia dla Ham Radio Control Server.

Funkcje:
  jwt_sign(payload)       -> token string
  jwt_verify(token)       -> payload dict | None
  hash_pw(password)       -> hex string (SHA-256, kompatybilny z oryginalna implementacja)
  make_reset_token()      -> token_str
  consume_reset_token()   -> payload | None
  RESET_TOKENS            -> dict {token: {user_id, expires}}

hash_pw uzywa prostego SHA-256 (bez HMAC) — tak jak oryginalna implementacja,
zeby istniejace hasla w users.json dzialaly bez zmian po aktualizacji.
JWT podpisywane HMAC-SHA256 z SECRET z config.py (rowniez bez zmian).
"""
import json, time, hmac, hashlib, base64, secrets
from typing import Optional

# SECRET pochodzi z config.py (tak jak w oryginalnej implementacji)
# — nie generujemy wlasnego klucza, zeby JWT z poprzednich sesji
# nadal byly wazne po aktualizacji.
try:
    from config import SECRET
except ImportError:
    # Fallback if config.py has no SECRET (older version). We must NOT use a
    # hardcoded value here: the source is public, so a known fallback secret
    # would let anyone forge tokens and log in as admin. Instead generate a
    # random per-process secret. This invalidates existing tokens (everyone
    # re-logs in once), which is the safe failure mode — far better than a
    # predictable key. In practice config.SECRET always exists, so this branch
    # is a last-resort guard, not a normal path.
    import sys as _sys
    SECRET = secrets.token_hex(32)
    print("[auth] OSTRZEZENIE: brak SECRET z config.py — uzyto losowego klucza "
          "na czas dzialania procesu (stare tokeny wygasly). Sprawdz .env / config.py.",
          file=_sys.stderr, flush=True)

# ── JWT (HMAC-SHA256) — identyczny z oryginalna implementacja ────────────────
def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64ud(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)

def jwt_sign(payload: dict, expires_in: int = 86400 * 7) -> str:
    payload["exp"] = int(time.time()) + expires_in
    h = _b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    b = _b64u(json.dumps(payload).encode())
    sig = _b64u(hmac.new(SECRET.encode(), f"{h}.{b}".encode(), hashlib.sha256).digest())
    return f"{h}.{b}.{sig}"

def jwt_verify(token: str) -> Optional[dict]:
    try:
        h, b, s = token.strip().split(".")
        expected = _b64u(hmac.new(SECRET.encode(), f"{h}.{b}".encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(s, expected):
            return None
        payload = json.loads(_b64ud(b))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None

# ── Hashowanie hasel (SHA-256 — kompatybilne ze starymi hasłami w users.json) ─
def hash_pw(pw: str) -> str:
    """Legacy SHA-256 hash — kept ONLY so old users.json hashes still verify.
    New passwords use hash_pw_secure(); verify_pw() understands both and
    transparently upgrades on next login. Do not use this for new hashes."""
    return hashlib.sha256(pw.encode()).hexdigest()

# ── Secure password hashing (scrypt + per-user salt) ─────────────────────────
# scrypt is in the stdlib (hashlib), memory-hard and deliberately slow, so a
# stolen users.json can't be brute-forced the way bare SHA-256 can. Format:
#   scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>
# The parameters are stored inline so we can raise cost later without breaking
# old hashes.
_SCRYPT_N = 16384   # CPU/memory cost (2^14)
_SCRYPT_R = 8
_SCRYPT_P = 1

def hash_pw_secure(pw: str) -> str:
    """Hash a password with scrypt + a fresh random salt. Use this for all new
    and changed passwords."""
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt,
                        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${dk.hex()}"

def verify_pw(pw: str, stored: str) -> bool:
    """Verify a password against a stored hash. Understands both the new scrypt
    format and legacy bare SHA-256, using constant-time comparison. Returns
    True/False; callers should upgrade legacy hashes on success (see
    needs_rehash)."""
    if not stored:
        return False
    if stored.startswith("scrypt$"):
        try:
            _tag, n, r, p, salt_hex, hash_hex = stored.split("$")
            dk = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=32)
            return hmac.compare_digest(dk.hex(), hash_hex)
        except Exception:
            return False
    # Legacy SHA-256 (hex). Constant-time compare.
    return hmac.compare_digest(hashlib.sha256(pw.encode()).hexdigest(), stored)

def needs_rehash(stored: str) -> bool:
    """True if the stored hash is in the legacy format and should be upgraded to
    scrypt the next time we have the plaintext (i.e. on successful login)."""
    return not (stored or "").startswith("scrypt$")

# ── Tokeny resetowania hasla (jednorazowe, wygasajace) ───────────────────────
RESET_TOKENS: dict[str, dict] = {}
RESET_TOKEN_TTL = 3600  # 1 godzina

def make_reset_token(user_id: str, username: str, email: str) -> str:
    """Wygeneruj jednorazowy token resetowania hasla (wazny 1h)."""
    # Usun stare tokeny dla tego usera i wygasle
    stale = [t for t, v in RESET_TOKENS.items()
             if v["user_id"] == user_id or v["expires"] < time.time()]
    for t in stale:
        RESET_TOKENS.pop(t, None)
    token = secrets.token_urlsafe(32)
    RESET_TOKENS[token] = {
        "user_id":  user_id,
        "username": username,
        "email":    email,
        "expires":  time.time() + RESET_TOKEN_TTL,
    }
    return token

def consume_reset_token(token: str) -> Optional[dict]:
    """Wez i usun token (jednorazowy). Zwraca payload lub None jesli wygas/nieznany."""
    entry = RESET_TOKENS.pop(token, None)
    if not entry or entry["expires"] < time.time():
        return None
    return entry

