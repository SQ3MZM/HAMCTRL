#!/usr/bin/env python3
"""crypto_secrets.py - at-rest encryption for user-supplied credentials
(CloudLog API keys, QRZ.com/HamQTH.com passwords) stored in users.json.

Key derivation: SHA-256 of this installation's JWT_SECRET (config.SECRET,
a random value already generated per-install into .env by config.ensure_env),
base64-urlsafe encoded for Fernet. Reuses the existing per-installation
secret instead of introducing a second key file to manage/lose/back up.
"""
import base64
import hashlib

try:
    from cryptography.fernet import Fernet, InvalidToken
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

PREFIX = "enc1:"

_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is None:
        from config import SECRET
        key = base64.urlsafe_b64encode(hashlib.sha256(SECRET.encode()).digest())
        _fernet = Fernet(key)
    return _fernet


def encrypt_secret(value: str) -> str:
    """Encrypt a credential for storage. Empty values, and values already
    starting with PREFIX, pass through unchanged. If the cryptography
    package is unavailable, degrades to plaintext (same behavior as before
    this module existed) rather than failing the save."""
    if not value or value.startswith(PREFIX) or not HAS_CRYPTO:
        return value
    return PREFIX + _get_fernet().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    """Decrypt a stored credential. Values without the PREFIX are treated
    as legacy plaintext (pre-encryption installs, or crypto unavailable)
    and returned as-is - they get encrypted the next time they're saved."""
    if not value or not value.startswith(PREFIX) or not HAS_CRYPTO:
        return value
    try:
        return _get_fernet().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        return value
