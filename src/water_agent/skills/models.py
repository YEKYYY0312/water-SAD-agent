"""Skill 的数据模型（P7-S9）。

一份 skill 是 ``<root>/<name>/SKILL.md``：YAML frontmatter + Markdown 正文。frontmatter 只认下面这几个键，
其余一律拒绝（拼错的键不能被当成"没写"放过去）。
"""
from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: name：kebab-case，2–48 字符，与目录名相同。
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
MAX_NAME_LENGTH = 48
#: 描述进目录（工具描述里每个 skill 一行），要短。
MAX_DESCRIPTION_LENGTH = 300
#: 正文进系统提示的 ``skill_context`` 段，一份不能挤掉检索条文。
MAX_BODY_CHARACTERS = 6000
#: ``SKILL.md`` 文件本身的字节上限（frontmatter + 正文），读之前就判。
MAX_FILE_BYTES = 64 * 1024
MAX_TRIGGERS = 12
MAX_TRIGGER_LENGTH = 24
MAX_REQUIRED_TOOLS = 8

SkillSource = Literal["builtin", "operator", "installed"]


class SkillManifest(BaseModel):
    """frontmatter。``extra="forbid"``：未知键即 ``SKILL_INVALID``。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=2, max_length=MAX_NAME_LENGTH)
    description: str = Field(min_length=1, max_length=MAX_DESCRIPTION_LENGTH)
    version: str = Field(default="0.0.0", min_length=1, max_length=32)
    #: 暴露策略的关键词：问题里出现任一个，本轮就开放 ``load_skill``。
    triggers: tuple[str, ...] = Field(default=(), max_length=MAX_TRIGGERS)
    #: 正文里点名要用的工具；装配里缺任何一个，这份 skill 就不加载（``SKILL_TOOL_UNAVAILABLE``）。
    requires_tools: tuple[str, ...] = Field(default=(), max_length=MAX_REQUIRED_TOOLS)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not SKILL_NAME_RE.match(value):
            raise ValueError(f"name {value!r} 必须是 kebab-case（小写字母、数字、连字符）")
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("description 不能为空")
        return cleaned

    @field_validator("triggers")
    @classmethod
    def _validate_triggers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned: list[str] = []
        for item in value:
            word = str(item).strip()
            if not word or len(word) > MAX_TRIGGER_LENGTH:
                raise ValueError(f"trigger {item!r} 必须是 1–{MAX_TRIGGER_LENGTH} 个字符")
            if word not in cleaned:
                cleaned.append(word)
        return tuple(cleaned)

    @field_validator("requires_tools")
    @classmethod
    def _validate_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not re.match(r"^[a-z][a-z0-9_]{1,40}$", str(item)):
                raise ValueError(f"requires_tools 项 {item!r} 不是合法的工具名")
        return tuple(dict.fromkeys(value))


class Skill(BaseModel):
    """一份已通过解析的 skill（还没过安检）。``digest`` 是 frontmatter + 正文的摘要。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest: SkillManifest
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARACTERS)
    source: SkillSource
    digest: str = Field(min_length=16, max_length=16)

    @property
    def name(self) -> str:
        return self.manifest.name

    @staticmethod
    def digest_of(raw_text: str) -> str:
        return hashlib.sha256(raw_text.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "MAX_BODY_CHARACTERS",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_FILE_BYTES",
    "MAX_NAME_LENGTH",
    "SKILL_NAME_RE",
    "Skill",
    "SkillManifest",
    "SkillSource",
]
