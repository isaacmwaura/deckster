"""One-time pairing code.

A 6-digit code is shown on the PC (console/tray) and typed on the phone. Seeing it
requires physical access to the PC, which is the out-of-band proof that binds the
phone to a machine the user controls. The code expires after a TTL, and repeated
wrong guesses burn the current code (rate limiting) so it cannot be brute-forced.
"""
from __future__ import annotations

import hmac
import secrets
import time


class PairingManager:
    def __init__(self, ttl_s: int = 180, max_attempts: int = 5) -> None:
        self._ttl = ttl_s
        self._max_attempts = max_attempts
        self._code: str | None = None
        self._expires: float = 0.0
        self._attempts = 0

    def _generate(self) -> None:
        self._code = f"{secrets.randbelow(1_000_000):06d}"
        self._expires = time.monotonic() + self._ttl
        self._attempts = 0

    def current_code(self) -> str:
        """Return the active code, generating a fresh one if none/expired."""
        if self._code is None or time.monotonic() >= self._expires:
            self._generate()
        return self._code  # type: ignore[return-value]

    def refresh(self) -> str:
        """Force a new code (e.g. user clicked 'new code')."""
        self._generate()
        return self._code  # type: ignore[return-value]

    def verify(self, code: str) -> bool:
        """Check a submitted code. Expired or too-many-attempts burns the code."""
        if self._code is None or time.monotonic() >= self._expires:
            self._generate()  # old code no longer valid
            return False

        self._attempts += 1
        if self._attempts > self._max_attempts:
            self._generate()  # burn the code; force a new one to be read off the PC
            return False

        if hmac.compare_digest(str(code), self._code):
            self._generate()  # single-use: a fresh code after success
            return True
        return False
