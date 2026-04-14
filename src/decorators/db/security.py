"""
Password helpers for registry-managed models.

The framework intentionally uses only the Python standard library here so
password hashing stays available without pulling in another runtime
dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Any

PASSWORD_HASH_SCHEME = "pbkdf2_sha256"
PASSWORD_HASH_ITERATIONS = 120_000
_PASSWORD_PARTS = 4


def hash_password(password: str, *, iterations: int = PASSWORD_HASH_ITERATIONS) -> str:
    """Return a salted PBKDF2-SHA256 password hash."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        iterations,
    ).hex()
    return f"{PASSWORD_HASH_SCHEME}${iterations}${salt}${digest}"


def is_password_hash(value: Any) -> bool:
    """Return True when *value* matches this module's hash format."""
    if not isinstance(value, str):
        return False

    parts = value.split("$")
    if len(parts) != _PASSWORD_PARTS:
        return False
    scheme, iterations, salt, digest = parts
    if scheme != PASSWORD_HASH_SCHEME:
        return False
    if not iterations.isdigit():
        return False
    return bool(salt) and bool(digest)


def verify_password(password: str, stored_hash: str) -> bool:
    """Return True when *password* matches *stored_hash*."""
    if not is_password_hash(stored_hash):
        return False

    _, iterations, salt, digest = stored_hash.split("$", maxsplit=3)
    candidate = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        int(iterations),
    ).hex()
    return hmac.compare_digest(candidate, digest)
