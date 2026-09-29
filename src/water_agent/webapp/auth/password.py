"""P5 Step 37: Argon2id password service.

Wraps argon2-cffi so the rest of the app never imports it directly.
All tunables are constants here; change them only with a migration that
re-hashes all stored passwords.
"""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

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


#: 用户不存在时拿来空验一次的哈希。参数与真实哈希完全相同（同一个 _PH），
#: 所以 Argon2 的耗时一致。懒生成：模块导入时不白花 ~50 ms。
_DUMMY_HASH: str | None = None


def burn_password_check(plaintext: str) -> None:
    """对一个不存在的账号做一次与真实校验等价的 Argon2 计算，结果丢弃。

    登录时"用户不存在"若直接返回，耗时约 0.5 ms，而"存在但密码错"要跑一次
    Argon2（实测约 51 ms）——响应时间本身就告诉攻击者哪些用户名存在
    （review 09-28 #6）。两条失败路径都必须付出同样的哈希成本。
    """
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = _PH.hash("water-agent-timing-equalizer")
    verify_password(plaintext, _DUMMY_HASH)


def needs_rehash(stored_hash: str) -> bool:
    """True if the stored hash was created with outdated parameters."""
    return _PH.check_needs_rehash(stored_hash)
