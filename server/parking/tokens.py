# server/parking/tokens.py
"""Device tokens for workers.

Format: `apms_dev_` + 40 URL-safe characters from 30 random bytes (240 bits).
The prefix makes a leaked token recognisable to secret scanners and in logs.

Stored as SHA-256 hex. High-entropy tokens do not need a slow password hash,
and a deterministic hash makes token lookup one indexed read. The plaintext
exists only in the response or command output that issues it.
"""
from __future__ import annotations

import hashlib
import re
import secrets

TOKEN_PREFIX = "apms_dev_"
TOKEN_PATTERN = re.compile(r"apms_dev_[A-Za-z0-9_-]{40}")


def is_well_formed(token: str | None) -> bool:
    return bool(token) and TOKEN_PATTERN.fullmatch(token) is not None


def hash_device_token(token: str) -> str:
    if not is_well_formed(token):
        raise ValueError("malformed device token")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def generate_device_token() -> tuple[str, str]:
    """Returns (plaintext, sha256_hex). Store only the second."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(30)
    return token, hash_device_token(token)