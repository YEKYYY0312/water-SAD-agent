"""docgen 质量层（P2 收口）：真实 LLM prose 润色 + 确定性自检 + 质量结论。

分层（与 ``docs/design/11-docgen-materials.md`` §6 一致）：

``RenderedDoc``（结构化底座，程序化渲染）
    → prose polish（真实 LLM，只允许改写章节行措辞；无 Key/不可达 → blocked）
    → quality self-check（确定性，离线）：数值保真 / 引用保真 / 结构合规
    → 质量结论（verdict：pass / fail / blocked，绝不把 blocked 写成 pass）

三条铁律在质量层的实例化：
- 数值程序化：润色不触碰 ``calc_values``；自检把润色后正文里的数字与
  「底座 ``calc_values`` ∪ 底座正文数字（备注/标题等程序化渲染的原文）」比对，
  凭空多出/改值 = 数值保真失败——底座正文的数字全部来自程序化渲染，可作白名单；
- 引用可追溯：润色只允许保留 ``citation_ids`` 里的引用；自检跑与模板同一套
  ``validate_citation_policy``，正文出现输入之外的"规范号+条款号" = 编造；
- 结构合规：canonical 必需章节与章节骨架不得因润色增删（``check_structure`` + 骨架比对）。

SKIP / blocked 语义（与 harness 口径一致）：
- 无 Key → ``blocked`` + ``skipped=True``（结构化 SKIP，不参与通过/失败统计）；
- 有 Key 但润色传输失败 / 润色产物结构破坏 → ``blocked`` + ``skipped=False``，
  计入真实运行失败并保留原因；
- 只有真实润色成功且自检零问题 → ``pass``；
- 自检有任何问题 → ``fail``（附问题清单）。
"""
from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from water_agent.docgen.templates import RenderedDoc, check_structure, validate_citation_policy
from water_agent.domain.models import TokenUsage
from water_agent.generation.cost import estimate_deepseek_cost
from water_agent.generation.endpoints import (
    DEFAULT_CHAT_API_KEY,
    DEFAULT_CHAT_BASE_URL,
    DEFAULT_CHAT_MODEL,
)
from water_agent.runtime.governance.contracts import CallCategory, RequestContext
from water_agent.runtime.governance.gateway import ProviderGateway

# 数值保真双阈值（与 CalcTolerances 缺省口径一致：精确原值）
NUMERIC_REL_TOL = 1e-6
NUMERIC_ABS_TOL = 1e-6

# 润色模型与真实链路同源：端点 / 模型 / Key 名没显式给出时按 resolve_chat_endpoint 解析（P7-S10，
# 缺省 DeepSeek）。常量只为历史引用保留。
POLISH_MODEL = DEFAULT_CHAT_MODEL
POLISH_BASE_URL = DEFAULT_CHAT_BASE_URL
POLISH_API_KEY_ENV = DEFAULT_CHAT_API_KEY


@dataclass(slots=True)
class PolishVerdict:
    """一次润色 + 自检的质量结论。

    - ``pass``：真实润色产物通过全部确定性自检；
    - ``fail``：自检发现结构化问题（``problems`` 非空）；
    - ``blocked``：无 Key / 调用失败 / 润色产物结构破坏，真实质量无法验证；
      只有无 Key 等前置条件缺失才设置 ``skipped=True``。
    """

    verdict: str                     # pass | fail | blocked
    problems: list[str] = field(default_factory=list)
    skipped: bool = False
    blocked_reason: str = ""
    polished: RenderedDoc | None = None
    used_real_model: bool = False    # 真实 LLM 已返回产物（即使产物后续被自检拒绝）
    polish_error: str = ""
    raw_output: str = ""             # 真实/脚本化传输原始文本，供质量报告审计

    @property
    def ok(self) -> bool:
        return self.verdict == "pass"


# ─── 数值保真（确定性） ───────────────────────────────────────────────

# 正文里的 "key = value [单位]" 行（模板参数行同构），如 "- velocity_mps = 1.13177 m/s"
_KEY_VALUE_LINE_RE = re.compile(r"([a-z_][a-z0-9_]*)\s*=\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)")
# 自由文本里的普通数字（标记/编号粘连的数字不算：DN150、S1、GB50015 的前缀部分）
_BARE_NUMBER_RE = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?![\w])")
# 行首的章节编号/列表序号（"1. xxx"）不算数值出现
_STRUCTURAL_PREFIX_RE = re.compile(r"^\s*\d+[.、)）]?\s+")
# 逐行数字保真使用更宽的扫描：DN150、GB50015-2019、1.1.1 都纳入原位比对。
_ALL_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)*(?:[eE][+-]?\d+)?")


def _within_tolerance(actual: float, gold: float) -> bool:
    abs_err = abs(actual - gold)
    rel_err = abs_err / abs(gold) if gold else abs_err
    return abs_err <= NUMERIC_ABS_TOL or rel_err <= NUMERIC_REL_TOL


def _fmt(value: float) -> str:
    """与模板 ``_fmt_number`` 同刻度（:g，≤6 位有效数字），供数值保真比对。"""
    return f"{value:g}"


def numeric_preservation_problems(
    polished_text: str,
    calc_values: dict[str, float],
    base_text: str = "",
) -> list[str]:
    """数值保真检查：润色后正文中的数字必须全部可溯源到程序化渲染的白名单。

    白名单 = 底座 ``calc_values``（浮点原值及其 :g 展示刻度）∪ 底座正文里的数字
    （备注/标题等由程序化渲染写入的原文数字）。底座里没有的数字 = 凭空出现。

    - ``key = value`` 行：key ∈ calc_values 且双阈值一致，否则 = 改值/凭空键；
    - 自由数字：字符串 ∈ 白名单刻度，或数值与白名单值双阈值一致；
    - 行首章节/列表编号（"1. xxx"）不算数值出现。
    """
    problems: list[str] = []
    allowed_strings: set[str] = set()
    for value in calc_values.values():
        allowed_strings.add(_fmt(value))
        allowed_strings.add(repr(value))
        allowed_strings.add(str(value))
    base_bare_numbers: set[str] = set()
    for line in base_text.splitlines():
        if _KEY_VALUE_LINE_RE.search(line):
            continue
        body = _STRUCTURAL_PREFIX_RE.sub("", line.lstrip(), count=1)
        base_bare_numbers.update(m.group(1) for m in _BARE_NUMBER_RE.finditer(body))
    allowed_strings |= base_bare_numbers
    allowed_values = list(calc_values.values()) + [
        float(token) for token in base_bare_numbers if _is_float(token)
    ]

    for line in polished_text.splitlines():
        stripped = line.lstrip()
        body = _STRUCTURAL_PREFIX_RE.sub("", stripped, count=1)
        key_value_matches = list(_KEY_VALUE_LINE_RE.finditer(body))
        for key_value_match in key_value_matches:
            key, raw_value = key_value_match.group(1), key_value_match.group(2)
            value = float(raw_value)
            if key not in calc_values:
                problems.append(f"出现底座之外的数值键：{key} = {raw_value}")
            # 正文只能看到模板 ``:g`` 后的展示值；精确原值另由 calc_values 映射一致性
            # 保护。这里与程序化展示刻度比较，避免把模板自身的合法格式化误判为改数。
            elif not _within_tolerance(value, float(_fmt(calc_values[key]))):
                problems.append(f"数值被改写：{key} = {raw_value}，底座原值 {calc_values[key]!r}")

        # A legal template assignment does not exempt the rest of the same
        # line. Mask only the validated ``key=value`` spans, then scan every
        # remaining number against the existing docgen whitelist.
        bare_body = list(body)
        for key_value_match in key_value_matches:
            bare_body[key_value_match.start() : key_value_match.end()] = " " * (
                key_value_match.end() - key_value_match.start()
            )
        for match in _BARE_NUMBER_RE.finditer("".join(bare_body)):
            raw = match.group(1)
            try:
                number = float(raw)
            except ValueError:  # pragma: no cover — 正则已保证可解析
                continue
            if not math.isfinite(number):
                problems.append(f"正文出现非有限数值：{raw}")
                continue
            if raw in allowed_strings:
                continue
            if any(_within_tolerance(number, gold) for gold in allowed_values):
                continue
            problems.append(f"正文出现底座之外的数值：{raw}")

    return problems


def _is_float(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


def _line_number_tokens(line: str) -> list[str]:
    return [match.group(0) for match in _ALL_NUMBER_RE.finditer(line)]


# 润色产物中的定位行（输入回显）：不计入内容行对齐。机器标记避免与真实内容行
# ``- 文档类型：计算书`` 混淆；旧标记仍兼容已有测试/缓存输出。
_POLISH_META_LINE_RE = re.compile(
    r"^\s*(?:\[\[(?:DOC|SECTION)_META\b.*\]\]|文档类型：|\[章节：)"
)


def strip_polish_meta_lines(text: str) -> list[str]:
    """从润色输出里剥掉定位行（「文档类型：…」/「[章节：…]」），只留内容行。

    润色按行对齐回填（``apply_polish_lines``），输入里的 ``[[..._META]]`` 定位行是给模型的上下文，
    不属于内容行；模型若按指令不输出、或原样回显，都能被同一过滤器归一。
    """
    return [line for line in text.splitlines() if not _POLISH_META_LINE_RE.match(line)]


# ─── prose 润色（真实 LLM，可选；受限回填） ────────────────────────────

POLISH_SYSTEM_PROMPT = (
    "你是给排水专业文档的文字编辑。你的唯一任务是改写措辞：把每行文字改得更专业、更通顺。"
    "必须遵守以下硬约束，违反任何一条即任务失败：\n"
    "1. 不得增删或改写任何数字（包括单位前的数值、备注里的限值区间）；\n"
    "2. 不得引入输入行中没有的规范号/条款号（如 GB50015 3.7.6 这类编号）；\n"
    "3. 不得增删、合并、拆分内容行；每条内容行对应一条输出行，顺序不变；\n"
    "4. 以 - 开头的行保持 - 开头；不得输出章节标题；\n"
    "5. 输入里的 [[DOC_META ...]] 与 [[SECTION_META ...]] 行只是定位信息，不要输出；"
    "以 - 开头的「文档类型」是正文，必须保留。\n"
    "输出格式：与输入内容行数完全一致的纯文本行，不要输出任何解释、标题或代码块围栏。"
)


def build_polish_prompt(doc: RenderedDoc) -> str:
    """构建润色用户消息：只暴露章节行文本，不暴露数值映射（数值只能原样保留在行里）。"""
    lines: list[str] = [f"[[DOC_META type={doc.doc_type} title={doc.title}]]"]
    for section in doc.sections:
        lines.append(f"[[SECTION_META heading={section.heading}]]")
        lines.extend(section.lines)
    return "\n".join(lines)


def apply_polish_lines(doc: RenderedDoc, polished_lines: list[str]) -> RenderedDoc | None:
    """把润色后的行回填到章节骨架（按行数对齐；行数不一致 = 拒绝润色产物）。

    返回 None 表示润色产物不可回填（行数不匹配，结构被破坏）——调用方按 blocked
    处理并保留底座原文，绝不把残缺产物当通过。
    """
    from water_agent.docgen.templates import DocSection

    cursor = 0
    paired: list[tuple[Any, list[str]]] = []
    for section in doc.sections:
        count = len(section.lines)
        chunk = polished_lines[cursor : cursor + count]
        cursor += count
        if len(chunk) != count:
            return None
        paired.append((section, chunk))
    if cursor != len(polished_lines):
        return None

    return RenderedDoc(
        doc_type=doc.doc_type,
        title=doc.title,
        sections=[
            DocSection(key=section.key, heading=section.heading, lines=list(chunk))
            for section, chunk in paired
        ],
        calc_values=dict(doc.calc_values),
        citation_ids=list(doc.citation_ids),
        notes=list(doc.notes),
    )


def _polish_lines_from_output(polished_text: str, doc: RenderedDoc) -> list[str] | None:
    """润色输出 → 内容行清单（先剥定位行；内容行数不匹配时回退原始行再试一次）。

    返回 None = 两种形态都对不上内容行数（结构破坏）。
    """
    stripped = strip_polish_meta_lines(polished_text)
    expected = sum(len(section.lines) for section in doc.sections)
    if len(stripped) == expected:
        return stripped
    raw = polished_text.splitlines()
    if len(raw) == expected:
        return raw
    return None


class RealPolishTransport:
    """真实 LLM 润色传输（DeepSeek OpenAI 兼容，与真实链路同源）。

    惰性建模型；调用失败抛异常（由 ``run_quality_polish`` 结构化为 blocked，
    绝不静默降级为原文输出——那会把"没润色"伪装成"润色通过"）。
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        base_url: str | None = None,
        api_key_env: str | None = None,
        gateway: ProviderGateway | None = None,
        request_context: RequestContext | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key_env = api_key_env
        self._gateway = gateway
        self._request_context = request_context
        self._llm: Any = None
        self.last_usage: TokenUsage | None = None

    def _ensure_llm(self) -> Any:
        if self._llm is not None:
            return self._llm
        # P2.5 Step 12：收敛到统一装配（build_chat_model），不再内联构造 ChatOpenAI。
        # 返回的是 GovernedChatModel：经 ProviderGateway 限流/预算/重试/熔断/对账。
        # 缺 Key 仍在此响亮失败（由 run_quality_polish 结构化为 blocked）。
        from water_agent.generation.chat_model import build_chat_model
        from water_agent.generation.compatibility import require_verified_endpoint
        from water_agent.generation.endpoints import resolve_chat_endpoint

        # 换对话模型的硬门槛（P7-S10）：润色的产物会进交付文档，非缺省端点同样要有通过的兼容性报告
        require_verified_endpoint(
            resolve_chat_endpoint(base_url=self.base_url, model=self.model, api_key_env=self.api_key_env)
        )
        self._llm = build_chat_model(
            model=self.model,
            base_url=self.base_url,
            api_key_env=self.api_key_env,
            temperature=0.0,
            category=CallCategory.DOCGEN_POLISH,
            gateway=self._gateway,
            request_context=self._request_context,
        )
        return self._llm

    def __call__(self, system: str, user: str) -> str:
        # transport 会跨用例复用；每次调用先清零，失败时不得沿用上一条 token/cost。
        self.last_usage = None
        llm = self._ensure_llm()
        response = llm.invoke(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]
        )
        # P2.5 review 修复：provider 未返回 usage → usage_unknown（token/cost 为 None），
        # 不填充 0、不用估算值冒充账单。
        usage = getattr(response, "usage_metadata", None)
        if not usage:
            self.last_usage = TokenUsage.unknown()
        else:
            def _get(name: str):
                return usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)

            input_tokens = _get("input_tokens")
            output_tokens = _get("output_tokens")
            if isinstance(input_tokens, int) and isinstance(output_tokens, int):
                self.last_usage = TokenUsage.known(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=estimate_deepseek_cost(input_tokens, output_tokens),
                )
            else:
                self.last_usage = TokenUsage.unknown()
        content = getattr(response, "content", "")
        if not content:
            raise ValueError("润色 LLM 返回空响应")
        return str(content)


PolishTransport = Callable[[str, str], str]
"""润色 LLM 传输函数：(system, user) → 回答文本；失败抛异常。"""


def run_quality_polish(
    doc: RenderedDoc,
    *,
    polish: PolishTransport | None = None,
    api_key_env: str = POLISH_API_KEY_ENV,
    used_real_model: bool = False,
) -> PolishVerdict:
    """对结构化底座做一次润色 + 确定性自检 + 质量结论。

    Args:
        doc: 结构化底座（``RenderedDoc``）。
        polish: 润色传输函数（真实 LLM）；``None`` = 未装配真实模型 → blocked（结构化）。
        api_key_env: 无传输函数时用环境变量判定 Key 是否可用（无 Key → blocked）。
        used_real_model: ``polish`` 是否为真实模型传输。真实模型返回产物后标为 True，
            即使产物随后因结构/自检失败被拒绝；离线脚本化传输保持 False。

    语义（绝不把 blocked 折算成 pass/fail）：
    - 无 polish 函数且无 Key → ``blocked``（skipped=True，原因可见）；
    - 有 Key 但未装配传输函数 → ``blocked``（计失败，不伪造通过）；
    - 润色调用抛异常 → ``blocked``（计失败；polish_error 可见）；
    - 润色产物行数不匹配 → 回退底座原文，``blocked``（结构破坏，不予采用）；
    - 回填成功但自检有问题 → ``fail``（problems 全部可见）；
        - 回填成功且自检零问题 → ``pass``。
    """
    if polish is None:
        if not os.environ.get(api_key_env, "").strip():
            return PolishVerdict(
                verdict="blocked",
                skipped=True,
                blocked_reason=(
                    f"SKIP: {api_key_env} 未配置，真实 LLM 润色未执行（质量结论 blocked，不是 pass）"
                ),
            )
        return PolishVerdict(
            verdict="blocked",
            skipped=False,
            blocked_reason=(
                f"润色传输函数未装配（有 {api_key_env} 但未提供 LLM transport），"
                "真实质量无法验证；不伪造通过"
            ),
        )

    try:
        polished_text = polish(POLISH_SYSTEM_PROMPT, build_polish_prompt(doc))
    except Exception as exc:  # noqa: BLE001 — 润色失败必须结构化，不得伪装通过
        return PolishVerdict(
            verdict="blocked",
            skipped=False,
            blocked_reason=f"润色调用失败：{type(exc).__name__}: {exc}",
            polish_error=f"{type(exc).__name__}: {exc}",
        )

    polished_lines = _polish_lines_from_output(polished_text, doc)
    if polished_lines is None:
        return PolishVerdict(
            verdict="blocked",
            skipped=False,
            blocked_reason="润色产物行数与底座不一致（结构被破坏），已拒绝采用；质量结论 blocked",
            polish_error="polish line count mismatch",
            used_real_model=used_real_model,
            raw_output=polished_text,
        )

    polished = apply_polish_lines(doc, polished_lines)
    if polished is None:
        return PolishVerdict(
            verdict="blocked",
            skipped=False,
            blocked_reason="润色产物无法回填章节骨架（结构被破坏），已拒绝采用；质量结论 blocked",
            polish_error="polish line count mismatch",
            used_real_model=used_real_model,
            raw_output=polished_text,
        )

    problems = run_quality_self_check(polished, base=doc)
    if problems:
        return PolishVerdict(
            verdict="fail",
            problems=problems,
            polished=polished,
            used_real_model=used_real_model,
            raw_output=polished_text,
        )
    return PolishVerdict(
        verdict="pass",
        polished=polished,
        used_real_model=used_real_model,
        raw_output=polished_text,
    )


# ─── 确定性自检（离线，对任意 RenderedDoc 可独立运行） ──────────────────


def run_quality_self_check(
    polished: RenderedDoc,
    *,
    base: RenderedDoc | None = None,
) -> list[str]:
    """对（润色后的）``RenderedDoc`` 跑全套确定性自检，返回问题清单（空 = 通过）。

    1. 结构合规：canonical 必需章节齐全（``check_structure``）；
    2. 引用保真：``validate_citation_policy``（正文条款 ⊆ citation_ids，防编造）；
    3. 数值保真：正文中所有数字可溯源到 calc_values ∪ 底座正文数字；
    4. 数值完整性：``calc_values`` 全有限；
    5. 底座一致性（提供 ``base`` 时）：``calc_values``/``citation_ids``/章节骨架与底座
       完全一致；每条内容行的数字序列原位不变——润色只许改措辞，不许删数、移数、造数。
    """
    problems: list[str] = []

    ok, missing = check_structure(polished)
    if not ok:
        problems.append(f"结构不合规，缺少必需章节：{'、'.join(missing)}")

    problems.extend(validate_citation_policy(polished))

    if base is not None:
        base_shape = [
            (section.key, section.heading, len(section.lines))
            for section in base.sections
        ]
        polished_shape = [
            (section.key, section.heading, len(section.lines))
            for section in polished.sections
        ]
        if base_shape != polished_shape:
            problems.append(f"章节骨架被改动：底座 {base_shape} → 润色后 {polished_shape}")
        if polished.calc_values != base.calc_values:
            problems.append("calc_values 与底座不一致（润色不得触碰数值映射）")
        if polished.citation_ids != base.citation_ids:
            problems.append("citation_ids 与底座不一致（润色不得增删引用）")
        if base_shape == polished_shape:
            for base_section, polished_section in zip(base.sections, polished.sections, strict=True):
                for index, (base_line, polished_line) in enumerate(
                    zip(base_section.lines, polished_section.lines, strict=True),
                    start=1,
                ):
                    base_numbers = _line_number_tokens(base_line)
                    polished_numbers = _line_number_tokens(polished_line)
                    if polished_numbers != base_numbers:
                        problems.append(
                            f"章节「{base_section.key}」第 {index} 行数字被增删或移位："
                            f"底座 {base_numbers} → 润色后 {polished_numbers}"
                        )

    problems.extend(
        numeric_preservation_problems(polished.text, polished.calc_values, base_text=base.text if base else "")
    )

    for key, value in polished.calc_values.items():
        if not math.isfinite(value):
            problems.append(f"calc_values[{key}]={value!r} 非有限（NaN/inf）")

    return problems


def summarize_verdicts(verdicts: Sequence[PolishVerdict]) -> dict[str, int]:
    """结论汇总：{'pass': n, 'fail': n, 'blocked': n}。blocked 只计数，绝不折算成 pass。"""
    summary = {"pass": 0, "fail": 0, "blocked": 0}
    for verdict in verdicts:
        summary[verdict.verdict] = summary.get(verdict.verdict, 0) + 1
    return summary
