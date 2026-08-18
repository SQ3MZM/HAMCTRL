"""
auth.py — authentication and cryptography for Ham Radio Control Server.

Functions:
  jwt_sign(payload)       -> token string
  jwt_verify(token)       -> payload dict | None
  hash_pw(password)       -> hex string (SHA-256, compatible with the original implementation)
  make_reset_token()      -> token_str
  consume_reset_token()   -> payload | None
  RESET_TOKENS            -> dict {token: {user_id, expires}}

hash_pw uses plain SHA-256 (no HMAC) — same as the original implementation,
so existing password hashes in users.json keep working unchanged after an update.
JWT is signed with HMAC-SHA256 using SECRET from config.py (also unchanged).
"""
import json, time, hmac, hashlib, base64, secrets
from typing import Optional

# SECRET comes from config.py (same as the original implementation)
# — we don't generate our own key, so JWTs from previous sessions
# stay valid after an update.
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
    print("[auth] WARNING: no SECRET from config.py — using a random key "
          "for this process's lifetime (old tokens have expired). Check .env / config.py.",
          file=_sys.stderr, flush=True)

# ── JWT (HMAC-SHA256) — identical to the original implementation ────────────
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

# ── Password hashing (SHA-256 — compatible with old hashes in users.json) ───
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

# ── Password reset tokens (one-time, expiring) ───────────────────────────────
RESET_TOKENS: dict[str, dict] = {}
RESET_TOKEN_TTL = 3600  # 1 hour

def make_reset_token(user_id: str, username: str, email: str) -> str:
    """Generate a one-time password reset token (valid for 1h)."""
    # Remove stale tokens for this user, and expired ones
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
    """Fetch and remove a token (one-time use). Returns the payload, or None if expired/unknown."""
    entry = RESET_TOKENS.pop(token, None)
    if not entry or entry["expires"] < time.time():
        return None
    return entry

