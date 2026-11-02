"""下载 skill 的严格解析（P7-S13，规格 §6.3 第 4 步）。

输入是**原始字节**，不是 ``read_text`` 的结果：``read_text`` 会把 ``\\r\\n`` 翻译成 ``\\n``，于是字节摘要与
``Skill.digest``（``sha256(read_text)``）对同一个文件给出不同的值——批准时绑定的是字节，加载期重算的也必须是字节。

检查顺序（便宜的、最致命的在前；任一步失败即停，错误码稳定）：

1. 大小 ≤ 64 KiB，非空；不含 NUL、不含 ``\\r``、不含 BOM；严格 UTF-8；                      → ``SKILL_INVALID``
2. 字符正向白名单（正文与 frontmatter 的**每个字符**，见 :mod:`water_agent.skills.charset`）；   → ``SKILL_REMOTE_CHARSET``
3. frontmatter 形状（复用 ``loader._split_frontmatter``，**不复制第二份解析**）；               → ``SKILL_INVALID``
4. YAML 事件扫描——**先于** ``safe_load``：anchor / alias / 显式 tag / 重复键 / 合并键 / 超深嵌套；  → ``SKILL_REMOTE_YAML_UNSAFE``
5. ``safe_load`` + ``SkillManifest``（复用既有模型，``extra="forbid"``）+ 请求一致性 + 正文长度。 → ``SKILL_INVALID`` /
   ``SKILL_REGISTRY_MISMATCH``（frontmatter 的 name / version 与请求不一致：注册表按 ``name@version`` 返回了别的 skill）

为什么 YAML 要先扫事件：实测 213 字节的 header 经 6 层别名展开成 67 182 个节点，``safe_load`` 先展开再校验；
``yaml.parse`` 0.3 ms 就能标出来。重复键 ``safe_load`` 静默取后者。这些在**下载路径**一律拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from water_agent.skills.charset import disallowed_characters
from water_agent.skills.errors import (
    SKILL_INVALID,
    SKILL_REGISTRY_MISMATCH,
    SKILL_REMOTE_CHARSET,
    SKILL_REMOTE_YAML_UNSAFE,
)
from water_agent.skills.loader import _split_frontmatter
from water_agent.skills.models import (
    MAX_BODY_CHARACTERS,
    MAX_FILE_BYTES,
    Skill,
    SkillManifest,
)
from water_agent.skills.naming import (
    NAME_PATTERN,
    VERSION_PATTERN,
    is_valid_name,
    is_valid_version,
    version_tuple,
)

#: frontmatter 的 YAML 嵌套深度上限（合法 header 最深两层：映射 → 序列）。
MAX_YAML_DEPTH = 4
#: frontmatter 字节上限（合法 header 远小于此；防止用一个巨大的 header 吃时间）。
MAX_HEADER_BYTES = 4096


class SkillParseError(Exception):
    """下载包被拒。``code`` 是稳定码，``detail`` 只含规则 id / 码点 / 字段名，**不含**包内文本。"""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}：{detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ParsedPackage:
    """通过严格解析的下载包（还没过安检）。"""

    manifest: SkillManifest
    body: str
    text: str
    sha256: str
    size: int

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def version(self) -> str:
        return self.manifest.version


# ---------------------------------------------------------------------------
# YAML 事件扫描
# ---------------------------------------------------------------------------


def yaml_hazards(header: str) -> list[str]:
    """frontmatter 里的 YAML 危险构造（规则 id 列表，去重、有序）。空列表 = 干净。

    - ``yaml.parse`` 的事件流能标出 anchor / alias / 显式 tag；
    - 重复键与合并键 ``<<`` 在事件流里只是普通标量，必须用 ``yaml.compose`` 的节点树才看得见；
    - 解析失败**不是**危险构造：返回 ``["yaml_error"]`` 交给调用方按 ``SKILL_INVALID`` 处理。
    """

    hazards: list[str] = []

    def flag(rule: str) -> None:
        if rule not in hazards:
            hazards.append(rule)

    depth = 0
    try:
        for event in yaml.parse(header):
            if isinstance(event, yaml.AliasEvent):
                flag("alias")
            if getattr(event, "anchor", None) is not None:
                flag("anchor")
            tag = getattr(event, "tag", None)
            if tag:
                # 隐式解析出的标量 tag（``name: ab-cd`` 的 ``!!str``）在事件里 ``implicit[0]`` 为真，不是显式 tag
                implicit = getattr(event, "implicit", None)
                if not (isinstance(event, yaml.ScalarEvent) and implicit and (implicit[0] or implicit[1])):
                    flag("tag")
            if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
                depth += 1
                if depth > MAX_YAML_DEPTH:
                    flag("depth")
            elif isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
                depth -= 1
        # 已命中的危险构造直接拒绝：compose 会递归构图，别名还可形成环或共享子图。
        # 只有无别名且深度有界的 header 才需要继续查重复键与合并键。
        if hazards:
            return hazards
        node = yaml.compose(header)
    except yaml.YAMLError:
        return ["yaml_error"]

    def walk(current: object) -> None:
        if isinstance(current, yaml.MappingNode):
            seen: set[str] = set()
            for key, value in current.value:
                if isinstance(key, yaml.ScalarNode):
                    if key.value == "<<":
                        flag("merge_key")
                    if key.value in seen:
                        flag("duplicate_key")
                    seen.add(key.value)
                walk(key)
                walk(value)
        elif isinstance(current, yaml.SequenceNode):
            for child in current.value:
                walk(child)

    if node is not None:
        walk(node)
    return hazards


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def parse_package(raw: bytes, *, expected_name: str, expected_version: str, sha256: str) -> ParsedPackage:
    """严格解析下载包。任一问题抛 :class:`SkillParseError`（稳定码 + 不含包内文本的说明）。

    ``sha256`` 由调用方对**同一份字节**算好传入（本函数不算第二遍，避免两处口径）。
    """

    if not raw:
        raise SkillParseError(SKILL_INVALID, "文件为空")
    if len(raw) > MAX_FILE_BYTES:
        raise SkillParseError(SKILL_INVALID, f"文件超过 {MAX_FILE_BYTES // 1024} KiB")
    if b"\x00" in raw:
        raise SkillParseError(SKILL_INVALID, "含 NUL 字节")
    if b"\r" in raw:
        raise SkillParseError(SKILL_INVALID, "含回车符（只接受 \\n 换行）")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise SkillParseError(SKILL_INVALID, "含 UTF-8 BOM")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise SkillParseError(SKILL_INVALID, "不是合法的 UTF-8") from None

    bad = disallowed_characters(text)
    if bad:
        shown = ", ".join(f"U+{codepoint:04X}" for _index, codepoint in bad)
        raise SkillParseError(SKILL_REMOTE_CHARSET, f"含白名单之外的字符：{shown}")

    parts = _split_frontmatter(text)
    if parts is None:
        raise SkillParseError(SKILL_INVALID, "缺少 --- 包裹的 YAML frontmatter")
    header, body = parts
    if len(header.encode("utf-8")) > MAX_HEADER_BYTES:
        raise SkillParseError(SKILL_INVALID, f"frontmatter 超过 {MAX_HEADER_BYTES} 字节")

    hazards = yaml_hazards(header)
    if hazards == ["yaml_error"]:
        raise SkillParseError(SKILL_INVALID, "frontmatter 不是合法 YAML")
    if hazards:
        raise SkillParseError(SKILL_REMOTE_YAML_UNSAFE, f"frontmatter 含不允许的 YAML 构造：{', '.join(hazards)}")

    try:
        meta = yaml.safe_load(header)
    except yaml.YAMLError:
        raise SkillParseError(SKILL_INVALID, "frontmatter 不是合法 YAML") from None
    if not isinstance(meta, dict):
        raise SkillParseError(SKILL_INVALID, "frontmatter 必须是键值对")
    try:
        manifest = SkillManifest.model_validate(meta)
    except ValidationError as exc:
        # 只报**已知字段名**：未知键本身是包里的攻击者可控文本，不能进错误说明（它会进 CLI 输出与审计）
        known = set(SkillManifest.model_fields)
        fields = sorted({str(item["loc"][0]) for item in exc.errors() if item.get("loc") and item["loc"][0] in known})
        unknown = any(not item.get("loc") or item["loc"][0] not in known for item in exc.errors())
        detail = ", ".join(fields) if fields else ""
        if unknown:
            detail = f"{detail}{'，' if detail else ''}含未知字段"
        raise SkillParseError(SKILL_INVALID, f"frontmatter 校验失败：{detail or 'frontmatter'}") from None

    if not is_valid_name(manifest.name):
        raise SkillParseError(SKILL_INVALID, "frontmatter 的 name 不是合法的 ASCII kebab-case 名字")
    if not is_valid_version(manifest.version):
        raise SkillParseError(SKILL_INVALID, "frontmatter 的 version 必须是 主.次.修订 三段整数")
    if manifest.name != expected_name or manifest.version != expected_version:
        raise SkillParseError(SKILL_REGISTRY_MISMATCH, "文件的 name 或 version 与请求的 name@version 不一致")

    stripped = body.strip()
    if not stripped:
        raise SkillParseError(SKILL_INVALID, "正文为空")
    if len(stripped) > MAX_BODY_CHARACTERS:
        raise SkillParseError(SKILL_INVALID, f"正文超过 {MAX_BODY_CHARACTERS} 字")
    return ParsedPackage(manifest=manifest, body=stripped, text=text, sha256=sha256, size=len(raw))


def to_skill(package: ParsedPackage, *, source: str = "installed") -> Skill:
    """已解析包 → ``Skill``（供复用 ``check_skill``）。``digest`` 沿用既有口径 ``sha256(text)[:16]``。"""

    return Skill(
        manifest=package.manifest,
        body=package.body,
        source=source,  # type: ignore[arg-type]
        digest=Skill.digest_of(package.text),
    )


__all__ = [
    "MAX_HEADER_BYTES",
    "MAX_YAML_DEPTH",
    "NAME_PATTERN",
    "VERSION_PATTERN",
    "ParsedPackage",
    "SkillParseError",
    "is_valid_name",
    "is_valid_version",
    "parse_package",
    "to_skill",
    "version_tuple",
    "yaml_hazards",
]
