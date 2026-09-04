"""Opaque device tokens.

A token is a long random secret handed to a paired device. We never persist the
raw token — only a salted hash — so a leaked allowlist.json cannot be replayed.
Verification is a constant-time hash comparison.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets


def new_token() -> str:
    """Return a fresh URL-safe opaque token (~43 chars, 256 bits)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str, salt: str) -> str:
    """Return the salted SHA-256 hex digest stored in the allow-list."""
    return hashlib.sha256(f"{salt}:{token}".encode("utf-8")).hexdigest()


def verify_token(token: str, token_hash: str, salt: str) -> bool:
    """Constant-time check of a presented token against a stored hash."""
    return hmac.compare_digest(hash_token(token, salt), token_hash)
