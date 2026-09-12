"""P5 Step 37: Argon2id password service.

Wraps argon2-cffi so the rest of the app never imports it directly.
All tunables are constants here; change them only with a migration that
re-hashes all stored passwords.
"""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError, InvalidHashError

# OWASP-recommended Argon2id settings (2024):
#   time_cost=2, memory_cost=65536 (64 MiB), parallelism=1
_PH = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16)

# Minimum password length enforced at the service boundary.
MIN_PASSWORD_LENGTH = 12


class PasswordTooShortError(ValueError):
    """Raised when a candidate password is shorter than MIN_PASSWORD_LENGTH."""


def hash_password(plaintext: str) -> str:
    """Return an Argon2id hash string for *plaintext*.

    Raises PasswordTooShortError if the password is too short.
    Never logs or returns the plaintext.
    """
    if len(plaintext) < MIN_PASSWORD_LENGTH:
        raise PasswordTooShortError(
            f"密码长度不足 {MIN_PASSWORD_LENGTH} 个字符"
        )
    return _PH.hash(plaintext)


def verify_password(plaintext: str, stored_hash: str) -> bool:
    """Return True iff *plaintext* matches *stored_hash*.

    Always returns False on any error — never raises to the caller so
    that login failure paths stay uniform regardless of hash validity.
    """
    try:
        return _PH.verify(stored_hash, plaintext)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True if the stored hash was created with outdated parameters."""
    return _PH.check_needs_rehash(stored_hash)
