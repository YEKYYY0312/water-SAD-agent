"""解析 ``SKILL.md``（P7-S9）。

只认单层目录：``<root>/<name>/SKILL.md``，``name`` 必须与目录名相同；不递归、不认平铺 ``<name>.md``、不跟随
逃出根目录的符号链接。frontmatter 用 PyYAML ``safe_load``（只构造基本类型）。

单个 skill 的问题只让这一个 skill 不加载（返回 ``SkillProblem``），不抛异常——一份写坏的运维 skill 不该
把整个装配拖垮；根目录本身不可读才是部署错误（由 ``library.py`` 判定并上抛）。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from water_agent.skills.errors import SKILL_INVALID
from water_agent.skills.models import (
    MAX_BODY_CHARACTERS,
    MAX_FILE_BYTES,
    SKILL_NAME_RE,
    Skill,
    SkillManifest,
    SkillSource,
)

SKILL_FILENAME = "SKILL.md"
_FENCE = "---"


@dataclass(frozen=True, slots=True)
class SkillProblem:
    """一份没能加载的 skill。``name`` 是目录名（目录名本身不合法时原样记下，不进任何提示）。"""

    name: str
    code: str
    detail: str


def _split_frontmatter(text: str) -> tuple[str, str] | None:
    """``---\\n<yaml>\\n---\\n<body>``；不是这个形状返回 ``None``。"""

    stripped = text.lstrip("﻿")
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FENCE:
            return "\n".join(lines[1:index]), "\n".join(lines[index + 1 :])
    return None


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError):
        return False
    return True


def parse_skill(path: Path, *, source: SkillSource, root: Path) -> Skill | SkillProblem:
    """解析一个 skill 目录（``path`` 是 ``<root>/<name>``）。"""

    name = path.name
    skill_file = path / SKILL_FILENAME
    if not SKILL_NAME_RE.match(name):
        return SkillProblem(name, SKILL_INVALID, "目录名不是 kebab-case")
    if not skill_file.is_file():
        return SkillProblem(name, SKILL_INVALID, f"目录里没有 {SKILL_FILENAME}")
    if not _inside(skill_file, root):
        return SkillProblem(name, SKILL_INVALID, f"{SKILL_FILENAME} 解析后不在 skill 根目录内（符号链接逃逸）")
    try:
        size = skill_file.stat().st_size
        if size > MAX_FILE_BYTES:
            return SkillProblem(name, SKILL_INVALID, f"{SKILL_FILENAME} 超过 {MAX_FILE_BYTES // 1024} KiB")
        raw = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return SkillProblem(name, SKILL_INVALID, f"读不了 {SKILL_FILENAME}：{type(exc).__name__}")
    parts = _split_frontmatter(raw)
    if parts is None:
        return SkillProblem(name, SKILL_INVALID, "缺少 --- 包裹的 YAML frontmatter")
    header, body = parts
    try:
        meta = yaml.safe_load(header)
    except yaml.YAMLError:
        return SkillProblem(name, SKILL_INVALID, "frontmatter 不是合法 YAML")
    if not isinstance(meta, dict):
        return SkillProblem(name, SKILL_INVALID, "frontmatter 必须是键值对")
    try:
        manifest = SkillManifest.model_validate(meta)
    except ValidationError as exc:
        fields = sorted({".".join(str(part) for part in item.get("loc", ())) or "frontmatter" for item in exc.errors()})
        return SkillProblem(name, SKILL_INVALID, f"frontmatter 校验失败：{', '.join(fields)}")
    if manifest.name != name:
        return SkillProblem(name, SKILL_INVALID, "frontmatter 的 name 与目录名不一致")
    body = body.strip()
    if not body:
        return SkillProblem(name, SKILL_INVALID, "正文为空")
    if len(body) > MAX_BODY_CHARACTERS:
        return SkillProblem(name, SKILL_INVALID, f"正文超过 {MAX_BODY_CHARACTERS} 字")
    return Skill(manifest=manifest, body=body, source=source, digest=Skill.digest_of(raw))


def scan_root(root: Path, *, source: SkillSource) -> list[Skill | SkillProblem]:
    """扫描一个 skill 根目录的**直接子目录**（按名字排序；隐藏目录与普通文件忽略）。"""

    results: list[Skill | SkillProblem] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name.startswith(".") or not entry.is_dir():
            continue
        results.append(parse_skill(entry, source=source, root=root))
    return results


__all__ = ["SKILL_FILENAME", "SkillProblem", "parse_skill", "scan_root"]
