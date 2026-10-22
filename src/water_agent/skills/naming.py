"""skill 名字与版本的严格校验（P7-S13）。

放在 ``skills/`` 顶层而不是 ``skills/remote/``：已安装层（库与面板会读，P7-I12 允许）和下载路径（只被运维 CLI 导入）
都要用同一套规则判定"这个名字能不能拿去拼路径"，两处各写一份就是两个真源。

**一律 ``fullmatch``，不用 ``match``**：既有 ``SKILL_NAME_RE.match("ab-cd\\n")`` 实测为真（``$`` 容许结尾换行），拿它判路径
用的名字会让 ``ab-cd\\n`` 通过。既有 ``SKILL_NAME_RE`` 的 ``.match`` 用法**不改**（``skills/models.py`` 只许改 ``SkillSource``
一行），本模块只服务于新代码。
"""
from __future__ import annotations

import re

from water_agent.skills.models import MAX_NAME_LENGTH

NAME_PATTERN = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
MIN_NAME_LENGTH = 2
#: 版本：三段整数，各 1–4 位 ASCII 数字（``fullmatch``）。
VERSION_PATTERN = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}")


def is_valid_name(name: object) -> bool:
    return (
        isinstance(name, str)
        and name.isascii()
        and MIN_NAME_LENGTH <= len(name) <= MAX_NAME_LENGTH
        and NAME_PATTERN.fullmatch(name) is not None
    )


def is_valid_version(version: object) -> bool:
    return isinstance(version, str) and version.isascii() and VERSION_PATTERN.fullmatch(version) is not None


def version_tuple(version: str) -> tuple[int, int, int]:
    """``"1.10.0"`` → ``(1, 10, 0)``。比较版本只能用整数元组（字符串比较会得出 ``"1.10.0" < "1.9.0"``）。"""

    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)


__all__ = [
    "MIN_NAME_LENGTH",
    "NAME_PATTERN",
    "VERSION_PATTERN",
    "is_valid_name",
    "is_valid_version",
    "version_tuple",
]
