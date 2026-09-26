"""图纸审查规则引擎（P3 Step 21）：管段语义对象 → 分级缺陷清单。

规格 `docs/design/13-p3-drawing-review.md` §6。

阈值来源分级是本模块的核心约束
--------------------------------
每条规则必须声明 `EvidenceLevel`，它决定**允许的表述**：

- `FORMAL_CLAUSE`：阈值有正式条文，`source_id` 可指回 `data/curated/clauses/`，
  可声称"按 GB xxx 第 x.x.x 条校验"；
- `INFORMAL_SUMMARY`：只有非正式综述（语料里自述"非正式规范条文"），
  只能说"按设计要点提示"；
- `ENGINEERING_PRACTICE`：无条文支撑，只能说"工程惯例提示"。

**硬性禁止**（铁律②在 P3 的实例化）：后两档不得使用"符合/违反规范""按规范校验"
等表述，也不得伪造 `source_id`。这不是文案偏好 —— 设计院拿着"违反 GB50014 第
5.2.10 条"的报告去改图，而那条条文我们**根本没有**，那是凭空造出的规范依据。
`rendered_finding()` 在渲染时强制这条纪律，`source_id` 也由 `EvidenceLevel` 门控。

语料现实（2026-09-07 实测，非引述规格）
----------------------------------------
| 阈值 | 条文 | 是否在库 | 定级 |
|---|---|---|---|
| 压力流设计流速 0.7–2.0 m/s | `GB50014-2021 5.2.9` | **在库**（原文含此区间） | `FORMAL_CLAUSE` |
| 排水横管坡度 0.005–0.01 | `GB50015-2019 4.7.1` | 在库但**自述非正式综述** | `INFORMAL_SUMMARY` |
| 最小管径 / 最小设计坡度 | `GB50014-2021 5.2.10` | **不在库** | `ENGINEERING_PRACTICE` |

`5.2.10` 的表格文本因 OCR 串行**混进了 `5.2.9` 记录**，但库里没有
`article == "5.2.10"` 的条目，故其 `source_id` 无法通过 `validate_citations`
（引用必须在检索结果中）。**不得**因"文本看起来在库里"就标 `formal_clause` ——
能否指回一个真实可检索的 `source_id` 才是分级标准。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from water_agent.drawing.mapping import MappedPipe, MappingResult

# ---------------------------------------------------------------------------
# 分级与严重度
# ---------------------------------------------------------------------------


class EvidenceLevel(str, Enum):
    """阈值来源等级（规格 §6.2）。**顺序即可信度**，不可重排。"""

    FORMAL_CLAUSE = "formal_clause"
    INFORMAL_SUMMARY = "informal_summary"
    ENGINEERING_PRACTICE = "engineering_practice"


#: 允许携带 `source_id` 的等级。其余等级**必须**为 None ——
#: 给工程惯例配一个 source_id 就是伪造条文依据。
_SOURCE_ID_ALLOWED: frozenset[EvidenceLevel] = frozenset(
    {EvidenceLevel.FORMAL_CLAUSE, EvidenceLevel.INFORMAL_SUMMARY}
)

#: 各等级的**表述前缀**。渲染时强制加上，让读者一眼看出依据强度。
_LEVEL_PREFIX: dict[EvidenceLevel, str] = {
    EvidenceLevel.FORMAL_CLAUSE: "按规范校验",
    EvidenceLevel.INFORMAL_SUMMARY: "按设计要点提示（非正式条文，需人工确认）",
    EvidenceLevel.ENGINEERING_PRACTICE: "工程惯例提示（无条文支撑，需人工确认）",
}

#: 禁止出现在非正式等级输出里的表述。有测试逐条断言。
FORBIDDEN_IN_INFORMAL: tuple[str, ...] = (
    "违反规范",
    "符合规范",
    "不符合规范",
    "按规范校验",
    "规范要求",
)


class Severity(str, Enum):
    """缺陷严重度。仅用于排序与人工分流，**不代表法律效力**。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class RuleSpec:
    """一条审查规则的自描述。阈值与来源等级在此绑定，不散落在实现里。"""

    rule_id: str
    title: str
    level: EvidenceLevel
    severity: Severity
    #: 正式/非正式条文的 source_id；`ENGINEERING_PRACTICE` **必须**为 None。
    source_id: str | None = None
    #: 阈值出处的人类可读说明（工程惯例等级尤其重要：得说清"惯例从哪来"）。
    basis: str = ""

    def __post_init__(self) -> None:
        if self.source_id is not None and self.level not in _SOURCE_ID_ALLOWED:
            raise ValueError(
                f"规则 {self.rule_id}：等级 {self.level.value} 不得携带 source_id "
                f"（得到 {self.source_id!r}）—— 给无条文支撑的阈值配 source_id 就是伪造依据"
            )
        if self.level in _SOURCE_ID_ALLOWED and not self.source_id:
            raise ValueError(
                f"规则 {self.rule_id}：等级 {self.level.value} 必须给出 source_id"
            )


@dataclass(frozen=True, slots=True)
class Finding:
    """一条缺陷发现。`handle` + `layer` 承担"能定位缺陷对象"的验收要求。"""

    rule: RuleSpec
    segment_id: str
    handle: str
    layer: str
    #: 实测值与阈值的人类可读描述（数值由程序算出，不经 LLM）。
    detail: str
    #: 支撑本条判定的图元 handle（标注等）。
    evidence_handles: tuple[str, ...] = ()

    def rendered_finding(self) -> str:
        """渲染为可直接进报告的一行，**强制**加上等级前缀。

        渲染在此集中，是为了让"表述纪律"成为唯一出口而不是每个调用方的自觉。
        """
        prefix = _LEVEL_PREFIX[self.rule.level]
        cite = f"（{self.rule.source_id}）" if self.rule.source_id else ""
        return f"[{prefix}{cite}] {self.rule.title}：{self.detail}（图元 {self.handle}，图层 {self.layer}）"

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule.rule_id,
            "title": self.rule.title,
            "evidence_level": self.rule.level.value,
            "severity": self.rule.severity.value,
            "source_id": self.rule.source_id,
            "basis": self.rule.basis,
            "segment_id": self.segment_id,
            "handle": self.handle,
            "layer": self.layer,
            "detail": self.detail,
            "evidence_handles": list(self.evidence_handles),
            "rendered": self.rendered_finding(),
        }


@dataclass(slots=True)
class ReviewResult:
    """审查结论：缺陷 + 未检查项 + 缺参。

    ``not_checked`` 是**与 findings 同等重要**的输出：一条规则因缺输入没跑，
    与它跑了且通过，在工程上是完全不同的结论。只报 findings 会让读者
    把"没检查"读成"没问题"（Step 15 `success_hallucination` 检测器管的正是这类）。
    """

    findings: list[Finding] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=list)
    missing_inputs: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    #: 参与审查的管段数。**0 与"检查过且干净"必须分开表述** ——
    #: 一条规则都没跑过却说"未发现问题"，正是 success_hallucination。
    pipes_reviewed: int = 0

    @property
    def checked_any(self) -> bool:
        return bool(self.findings) or not self.not_checked

    def to_dict(self) -> dict:
        return {
            "ok": True,
            "findings": [f.to_dict() for f in self.findings],
            "not_checked": list(self.not_checked),
            "missing_inputs": list(self.missing_inputs),
            "assumptions": list(self.assumptions),
            "pipes_reviewed": self.pipes_reviewed,
            "summary": self.summary(),
        }

    def summary(self) -> str:
        """一句话结论。**未检查项非空时不得说"未发现问题"**。"""
        if self.findings:
            by_level: dict[str, int] = {}
            for finding in self.findings:
                key = finding.rule.level.value
                by_level[key] = by_level.get(key, 0) + 1
            parts = "、".join(f"{level} {count} 条" for level, count in sorted(by_level.items()))
            tail = f"；另有 {len(self.not_checked)} 项因缺输入未检查" if self.not_checked else ""
            return f"发现 {len(self.findings)} 条待核问题（{parts}）{tail}"
        if self.pipes_reviewed == 0:
            # 没有可审查的管段：任何"未发现问题"的说法都是误导 ——
            # 规则一条都没跑过（图纸解析失败、或图上没有可识别的管线图元）。
            return (
                "**未执行任何管段审查**：无可审查的管段（图纸解析失败或图上无可识别管线图元），"
                "本结论不含任何合规判断"
            )
        if self.not_checked:
            return (
                f"已跑的规则未发现问题，但有 {len(self.not_checked)} 项因缺输入**未检查**"
                "——不等于图纸无问题"
            )
        return "已检查的规则均未发现问题"


# ---------------------------------------------------------------------------
# 规则定义（阈值 + 来源等级绑定在一起）
# ---------------------------------------------------------------------------

#: 排水横管最小/最大坡度。来源是 `GB50015-2019 4.7.1`，语料原文自述
#: "本条目为联网整理的设计要点综述，非正式规范条文" —— 故只能 INFORMAL_SUMMARY。
RULE_DRAIN_SLOPE = RuleSpec(
    rule_id="drain.horizontal_slope_range",
    title="排水横管坡度宜在 0.005~0.01",
    level=EvidenceLevel.INFORMAL_SUMMARY,
    severity=Severity.MEDIUM,
    source_id="GB50015-2019 4.7.1",
    basis="GB50015-2019 4.7.1（语料自述为设计要点综述，非正式条文）",
)

#: 排水管最小管径。`GB50014-2021 5.2.10`（表 5.2.10：污水管/合流管 300mm）
#: **不在语料库** —— 实测 `article == "5.2.10"` 无记录，其表格文本因 OCR 串行
#: 混进了 5.2.9 的 text 里。无法指回可检索的 source_id，故只能 ENGINEERING_PRACTICE。
RULE_DRAIN_MIN_DN = RuleSpec(
    rule_id="drain.min_diameter",
    title="室外排水管（污水/合流）公称管径不宜小于 300mm",
    level=EvidenceLevel.ENGINEERING_PRACTICE,
    severity=Severity.MEDIUM,
    basis=(
        "阈值取自 GB50014-2021 表 5.2.10 的常见取值，但该条文**不在本仓语料库**"
        "（实测无 article=5.2.10 记录），故不得声称按规范校验，须人工核对现行条文"
    ),
)

#: 压力流设计流速区间。来源 `GB50014-2021 5.2.9`，语料原文即含
#: "压力管道的设计流速宜采用 0.7m/s~2.0m/s" —— 在库且是正式条文，故 FORMAL_CLAUSE。
RULE_PRESSURE_VELOCITY = RuleSpec(
    rule_id="drain.pressure_flow_velocity",
    title="排水压力管道设计流速宜为 0.7~2.0 m/s",
    level=EvidenceLevel.FORMAL_CLAUSE,
    severity=Severity.HIGH,
    source_id="GB50014-2021 5.2.9",
    basis="GB50014-2021 5.2.9（语料含原文区间 0.7~2.0 m/s）",
)

#: 按管材分类的最大流速（GB50014-2021 5.2.9）
_MAX_VELOCITY_BY_MATERIAL: dict[str, float] = {
    # 塑料管类
    "HDPE": 5.0,
    "PVC-U": 5.0,
    "UPVC": 5.0,
    "PPR": 5.0,
    "PE": 5.0,
    # 铸铁管类
    "铸铁": 5.0,
    "球墨铸铁": 5.0,
    "灰口铸铁": 5.0,
    # 混凝土管类
    "混凝土": 10.0,
    "钢筋混凝土": 10.0,
    # 钢管类
    "钢管": 10.0,
    "镀锌钢管": 10.0,
    "不锈钢": 10.0,
}


def _get_max_velocity_for_material(material: str | None) -> float:
    """获取指定管材的最大流速限值。未识别管材返回保守值 5.0。"""
    if material is None:
        return 5.0  # 未知管材用最严格限值
    return _MAX_VELOCITY_BY_MATERIAL.get(material, 5.0)


#: 公开别名（`hydraulics.calc_velocity` 等外部调用方使用，避免引用私有函数）。
get_max_velocity_for_material = _get_max_velocity_for_material

#: 排水管按管材分类的最大流速。来源 `GB50014-2021 5.2.9`，与
#: `RULE_PRESSURE_VELOCITY` 同一条文（语料含原文），故同为 FORMAL_CLAUSE。
RULE_DRAIN_MAX_VELOCITY = RuleSpec(
    rule_id="drain.max_velocity_by_material",
    title="排水管最大流速按管材区分：塑料管/铸铁管 5.0 m/s，混凝土管/钢管 10.0 m/s",
    level=EvidenceLevel.FORMAL_CLAUSE,
    severity=Severity.HIGH,
    source_id="GB50014-2021 5.2.9",
    basis="GB50014-2021 5.2.9（塑料管、铸铁管 5.0 m/s；混凝土管、钢管 10.0 m/s；未知管材取保守值 5.0）",
)

#: 全部规则的机器可读清单。新增规则必须进这里（供覆盖率断言与文档引用）。
ALL_RULES: tuple[RuleSpec, ...] = (
    RULE_PRESSURE_VELOCITY,
    RULE_DRAIN_MAX_VELOCITY,
    RULE_DRAIN_SLOPE,
    RULE_DRAIN_MIN_DN,
)

#: 排水最小管径阈值 mm（见 RULE_DRAIN_MIN_DN.basis 的来源说明）。
MIN_DRAIN_DN_MM = 300
#: 排水横管坡度区间（见 RULE_DRAIN_SLOPE）。
DRAIN_SLOPE_MIN = 0.005
DRAIN_SLOPE_MAX = 0.01


def _suspicious_note(pipe: MappedPipe) -> str:
    """可疑标注的提示后缀 —— 让读者知道这条判定旁边有被忽略的标注。"""
    if not pipe.ignored_suspicious_handles:
        return ""
    return f"；附近有 {len(pipe.ignored_suspicious_handles)} 条可疑标注未采信"


def check_min_diameter(result: MappingResult) -> tuple[list[Finding], list[str]]:
    """排水管最小管径检查。返回 (findings, 未检查说明)。

    检查策略（P5.1 恢复严格模式）：
    - **只检查 system=drainage 的管段**
    - system=None 或 system=water_supply 的管段不检查
    - 降低误报率：避免将室内立管（DN100-150）误判为室外排水主管

    P5.1 改动说明：
    P4 为提高召回率放宽了检查范围（system=None 且 50-800mm 也检查），
    但导致 P5 误报率 100%（67 个室内立管被误判）。本次恢复严格模式，
    依赖 P5.1 增强的系统类型识别（从标注文本推断）来保持召回率。
    """
    findings: list[Finding] = []
    missing_dn = [p for p in result.pipes if p.system == "drainage" and p.dn_mm is None]
    # 系统归属未知（图层名/标注都没能表达给水或排水）的管段：不是"确认不是
    # 排水管所以跳过"，是"不知道是不是"。真实图纸里这类管段常见（图层命名
    # 不规范），静默跳过会让本规则一条都没跑却在 summary 里显示"未发现问题"
    # ——这正是 B2：得如实计入 not_checked，而不是当作"不适用"悄悄放过。
    unknown_system = [p for p in result.pipes if p.system is None]

    for pipe in result.pipes:
        # 只检查明确标识为排水系统的管段
        if pipe.system != "drainage" or pipe.dn_mm is None:
            continue

        if pipe.dn_mm < MIN_DRAIN_DN_MM:
            detail = f"标注管径 DN{pipe.dn_mm}，小于惯例最小值 DN{MIN_DRAIN_DN_MM}"
            detail += _suspicious_note(pipe)

            findings.append(
                Finding(
                    rule=RULE_DRAIN_MIN_DN,
                    segment_id=pipe.segment_id,
                    handle=pipe.handle,
                    layer=pipe.layer,
                    detail=detail,
                    evidence_handles=pipe.evidence_handles,
                )
            )

    not_checked: list[str] = []
    if missing_dn:
        not_checked.append(
            f"{RULE_DRAIN_MIN_DN.rule_id}：{len(missing_dn)} 条排水管段无管径标注，未检查"
        )
    if unknown_system:
        not_checked.append(
            f"{RULE_DRAIN_MIN_DN.rule_id}：{len(unknown_system)} 条管段系统归属未知"
            "（图层名/标注均未表达给水或排水），无法判定是否属于排水管，**未检查**"
        )
    return findings, not_checked


def check_slope(result: MappingResult) -> tuple[list[Finding], list[str]]:
    """排水横管坡度检查。

    **当前恒为"未检查"**：坡度需要两端标高，而 `DrawingEntity.points` 是二维
    模型空间坐标，标高只存在于标注文本里（`井底标高 -1.850` 这类），
    Step 21 不做标高文本的结构化解析 —— 从自由文本推标高再算坡度，
    错一个小数点就是几十厘米的埋深偏差。

    保留这条规则并**如实报告未检查**，而不是删掉它：删掉会让报告读起来
    "坡度没问题"，而事实是"坡度根本没查"。这正是 `success_hallucination`
    检测器盯的那类失败。

    ``system`` 全为 ``None``（归属未知，不是"确认为给水"）时同样要报未检查
    ——B2：否则一张系统归属全部识别失败的真实图纸会因为"没有 drainage 管段"
    而直接 ``return [], []``，规则表现为"跑过且没发现问题"。
    """
    drainage_pipes = [p for p in result.pipes if p.system == "drainage"]
    unknown_system = sum(1 for p in result.pipes if p.system is None)
    if not drainage_pipes:
        if unknown_system:
            return [], [
                (
                    f"{RULE_DRAIN_SLOPE.rule_id}：{unknown_system} 条管段系统归属未知"
                    "（图层名/标注均未表达给水或排水），坡度规则的适用范围无法判定，"
                    "**未检查**"
                )
            ]
        return [], []
    return [], [
        (
            f"{RULE_DRAIN_SLOPE.rule_id}：坡度需两端标高，当前图元仅有二维坐标，"
            "标高未做结构化解析，**未检查**（不代表坡度合规）"
        )
    ]


def check_pressure_velocity(result: MappingResult) -> tuple[list[Finding], list[str]]:
    """压力流设计流速与最大流速检查。

    **当前恒为"未检查"**：流速 = 流量 / 过水断面，而流量不在图纸上 ——
    它来自水力计算（`tools/hydraulics.py`）或设计条件。图纸只给管径。
    规则保留在清单里是因为 Step 22 的审查子图可以在**有计算结果时**喂进来；
    此时如实报告未检查，不假装校验过。

    本函数同时负责两条流速规则：
    - RULE_PRESSURE_VELOCITY：压力流设计流速 0.7-2.0 m/s（GB50014-2021 5.2.9）
    - RULE_DRAIN_MAX_VELOCITY：按管材分类的最大流速（同一条文，塑料管/铸铁管 5.0，混凝土管/钢管 10.0）
    两者都需要流量数据，当前都报未检查。

    同 `check_slope`：``system`` 全为 ``None`` 时也要报未检查，不能因为
    "没有识别到 drainage 管段"就当作规则不适用而静默放过（B2）。
    """
    drainage_pipes = [p for p in result.pipes if p.system == "drainage"]
    unknown_system = sum(1 for p in result.pipes if p.system is None)
    if not drainage_pipes:
        if unknown_system:
            return [], [
                (
                    f"{RULE_PRESSURE_VELOCITY.rule_id}：{unknown_system} 条管段系统归属未知"
                    "（图层名/标注均未表达给水或排水），压力流速规则的适用范围无法判定，"
                    "**未检查**"
                ),
                f"{RULE_DRAIN_MAX_VELOCITY.rule_id}：同上，系统归属未知，**未检查**",
            ]
        return [], []
    return [], [
        (
            f"{RULE_PRESSURE_VELOCITY.rule_id}：压力流设计流速需设计流量，"
            "图纸不含流量信息，**未检查**（需配合水力计算结果）"
        ),
        (
            f"{RULE_DRAIN_MAX_VELOCITY.rule_id}：按管材分类的最大流速需设计流量，"
            "图纸不含流量信息，**未检查**（塑料管/铸铁管限值 5.0 m/s，混凝土管/钢管 10.0 m/s）"
        ),
    ]


#: 检查函数清单。顺序 = 报告里的规则顺序（确定性输出）。
_CHECKS = (check_min_diameter, check_slope, check_pressure_velocity)


def review_drawing(result: MappingResult) -> ReviewResult:
    """跑全部规则，产出分级缺陷清单。

    **数值全部程序化**（铁律①）：本函数不调用任何模型。
    `missing_inputs` 从映射层原样透传 —— 单位未知时下游必须看到它，
    而不是收到一份看起来完整的审查报告。
    """
    review = ReviewResult(
        missing_inputs=list(result.missing_inputs),
        assumptions=list(result.assumptions),
        pipes_reviewed=len(result.pipes),
    )
    for check in _CHECKS:
        findings, not_checked = check(result)
        review.findings.extend(findings)
        review.not_checked.extend(not_checked)

    # 严重度排序（HIGH 先），同级按 handle 保证确定性
    order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
    review.findings.sort(key=lambda f: (order[f.rule.severity], f.handle))

    if not result.unit_known:
        review.not_checked.append(
            "长度类规则：图纸单位未声明，长度未换算，**任何依赖长度的检查都未执行**"
        )
    elif result.unit_suspicious:
        # unit_known=True 但 unit_suspicious=True（B3）：字段"有值"不等于
        # "可信"，长度依然没换算，这条披露不能只看 unit_known。
        review.not_checked.append(
            "长度类规则：图纸声明的单位不可信（英制，中文市政图纸中极罕见），"
            "长度未换算，**任何依赖长度的检查都未执行**"
        )
    return review
