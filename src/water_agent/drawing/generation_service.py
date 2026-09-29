"""参数化出图服务（P6-S1 Step 5）。

流程（顺序即纪律）：

    校验 → 规划（三张图）→ 渲染 → 逐张读回核对 → 逐张 S50 复审 → 可选实验性 DWG
    → 写入 ``export_root/drawings/{generation_id}/`` → 打包 ZIP（含 README.txt）

- **读回核对不过不交付**：生成图必须能被本仓库 ``read_drawing`` 直接打开，且每条管段的
  管径都能被 ``map_pipes`` 按 handle 读出——做不到就是生成器的缺陷，报 ``GEN_RENDER_FAILED``。
- **复审结果如实呈现**：逐张调用 S50 的 ``review_uploaded_dxf``，报告原样附上。现有规则集
  只覆盖排水，给水图会落进"未检查"；这里**不写"审查通过"或"0 缺陷"**，只转述报告。
- **落盘是原子的**：先写 ``drawings/.partial-{id}/``，齐了再改名为 ``drawings/{id}/``；
  ``meta.json`` 记录生成者（下载路由据此校验归属）。``generation_id`` 由本模块生成，
  从不来自用户输入，目录名天然无法承载路径穿越。
- 整个服务**不调用任何模型**，也不 import 模型或 HTTP 客户端（AST 静态断言）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from water_agent.documents.drawing_upload import DrawingReviewText, review_uploaded_dxf
from water_agent.documents.errors import UploadError
from water_agent.documents.storage import DIR_MODE, FILE_MODE
from water_agent.drawing.building_rules import (
    BuildingRules,
    BuildingRulesError,
    load_building_rules,
)
from water_agent.drawing.detail_drawings import plan_details
from water_agent.drawing.generation_models import (
    DIAMETER_BASIS_NOTE,
    GEN_RENDER_FAILED,
    RENDER_FAILED_MESSAGE,
    GenerationError,
    GenerationPlan,
    GenerationRequest,
    PipeMaterial,
    SheetPlan,
    check_velocities,
    parse_generation_request,
    resolve_riser_position,
    validate_sheet,
)
from water_agent.drawing.generator import (
    DwgExport,
    ReadbackReport,
    export_dwg,
    render_sheet,
    verify_readback,
)
from water_agent.drawing.layout_engine import DISCLAIMER, plan_floor, unit_count
from water_agent.drawing.system_schematic import plan_schematic

logger = logging.getLogger(__name__)

GENERATIONS_DIRNAME = "drawings"
META_NAME = "meta.json"
ZIP_NAME = "drawing_set.zip"
README_NAME = "README.txt"
META_VERSION = 1
#: 前端与 README 都要出现的原话（执行 prompt Step 6）。
DRAFT_NOTICE = "程序生成的初稿，需设计人复核"
SHEET_FILENAMES: dict[str, str] = {
    "plan": "01-给水平面图.dxf",
    "schematic": "02-给水系统图.dxf",
    "detail": "03-节点大样图.dxf",
}


@dataclass(frozen=True, slots=True)
class SheetResult:
    sheet_id: str
    title: str
    filename: str
    scale: int
    readback: ReadbackReport
    review: DrawingReviewText
    dwg: DwgExport | None = None

    @property
    def dwg_filename(self) -> str | None:
        if self.dwg is None or self.dwg.status != "ok":
            return None
        return self.filename.removesuffix(".dxf") + "（实验性）.dwg"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet_id": self.sheet_id,
            "title": self.title,
            "filename": self.filename,
            "scale": f"1:{self.scale}",
            # 生成器自己画的管段（WS-PIPE-* 图层）与读回核对的结果。
            "pipe_segments": self.readback.planned_segments,
            "pipe_segments_read_back": self.readback.pipe_segments,
            "dn_matched": self.readback.dn_matched,
            "entity_count": self.readback.entity_count,
            "review": {
                # 下面的计数与文本都是 S50 复审的原样输出。"已审查管段"按审查器口径，
                # 把轴网、符号、轮廓的线段也算作管段，因此比 pipe_segments 多。
                "pipes_reviewed": self.review.pipes_reviewed,
                "findings": self.review.findings,
                "not_checked": self.review.not_checked,
                "not_checked_items": list(self.review.not_checked_items),
                "suspicious_entities": self.review.suspicious_entities,
                "truncated": self.review.truncated,
                "summary": self.review.summary,
                "report_text": self.review.text,
            },
            "dwg": None
            if self.dwg is None
            else {
                "status": self.dwg.status,
                "experimental": True,
                "reason": self.dwg.reason,
                "filename": self.dwg_filename,
                "converter_warnings": self.dwg.converter_warnings,
            },
        }


@dataclass(frozen=True, slots=True)
class GenerationResult:
    generation_id: str
    created_at: str
    owner_user_id: str | None
    plan: GenerationPlan
    sheets: tuple[SheetResult, ...]
    directory: Path
    zip_name: str
    zip_bytes: int
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def zip_path(self) -> Path:
        return self.directory / ZIP_NAME

    def to_response(self) -> dict[str, Any]:
        plan = self.plan
        return {
            "generation_id": self.generation_id,
            "created_at": self.created_at,
            "notice": DRAFT_NOTICE,
            "disclaimer": DISCLAIMER,
            "request": plan.request.model_dump(mode="json"),
            "riser_position": plan.riser_position,
            "riser_position_defaulted": plan.riser_position_defaulted,
            "rules_version": plan.rules_version,
            "calculations": [record.model_dump(mode="json") for record in plan.calculations],
            "warnings": list(plan.warnings) + self._dwg_warnings(),
            "missing_inputs": list(plan.missing_inputs),
            "assumptions": list(plan.assumptions),
            "sheets": [sheet.to_dict() for sheet in self.sheets],
            "dwg_requested": plan.request.include_dwg,
            "download": {
                "url": f"/api/drawings/{self.generation_id}/download",
                "filename": self.zip_name,
                "size_bytes": self.zip_bytes,
            },
            "timings_ms": dict(self.timings_ms),
        }

    def _dwg_warnings(self) -> list[str]:
        warnings: list[str] = []
        for sheet in self.sheets:
            if sheet.dwg is not None and sheet.dwg.status != "ok":
                warnings.append(f"{sheet.filename}：DWG（实验性）未交付——{sheet.dwg.reason}")
        return warnings


# ---------------------------------------------------------------------------
# 规划
# ---------------------------------------------------------------------------


def plan_generation(request: GenerationRequest, rules: BuildingRules) -> GenerationPlan:
    """三张图的完整规划（尚未渲染）。同一请求两次调用逐字节相同。"""

    riser_position, defaulted = resolve_riser_position(request, rules)
    check = check_velocities(request, rules)
    typed = rules.for_type(request.building_type)
    assumptions = [DIAMETER_BASIS_NOTE]
    if defaulted:
        assumptions.append(f"立管位置未指定，按规则表 {typed.riser.rule_id} 默认取「{riser_position}」")
    assumptions += [
        f"立管全高按主管 {request.main_pipe.dn.value}，未做逐层变径（缺逐层设计秒流量，不猜）",
        "图面管线旁只注管径（DN），管材写在说明里",
        (
            "复审报告的「已审查管段」按审查器口径计数：轴网、图例符号、大样轮廓的线段也算作管段，"
            "因此多于生成器画的管段数（见每张图的 pipe_segments）"
        ),
    ]
    if PipeMaterial.STEEL_PLASTIC_COMPOSITE in (request.main_pipe.material, request.branch_pipe.material):
        assumptions.append("审查器的材质词表不识别「钢塑复合」，复审里这类管段会显示缺材质（审查器已知缺口）")
    return GenerationPlan(
        request=request,
        rules_version=rules.rules_version,
        riser_position=riser_position,
        riser_position_defaulted=defaulted,
        calculations=check.calculations,
        warnings=check.warnings,
        missing_inputs=check.missing_inputs,
        assumptions=tuple(assumptions),
        floor_plan=plan_floor(request, rules),
        schematic=plan_schematic(request, rules),
        details=tuple(plan_details(request, rules)),
    )


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


def _readme(result: GenerationResult, rules: BuildingRules) -> str:
    plan = result.plan
    request = plan.request
    typed_unit = rules.for_type(request.building_type).unit_label
    lines = [
        f"给水系统参数化图纸——{DRAFT_NOTICE}",
        "",
        f"生成编号：{result.generation_id}",
        f"生成时间（UTC）：{result.created_at}",
        f"规则表版本：{plan.rules_version}",
        "",
        "一、设计参数（用户输入）",
        f"  系统：{request.system_type}；建筑类型：{request.building_type.value}",
        f"  层数：{request.floors}；每层：{unit_count(request.units_per_floor, typed_unit)}；层高：{request.floor_height_m:.2f} m",
        f"  立管位置：{plan.riser_position}" + ("（未指定，按规则表默认）" if plan.riser_position_defaulted else ""),
        f"  主管（含立管）：{request.main_pipe.dn.value} {request.main_pipe.material.value}",
        f"  支管：{request.branch_pipe.dn.value} {request.branch_pipe.material.value}",
        f"  设计流量：{request.flow_rate_lps:.2f} L/s"
        + (f"；支管设计流量：{request.branch_flow_rate_lps:.2f} L/s" if request.branch_flow_rate_lps is not None else ""),
        f"  供水压力：{request.pressure_mpa:.2f} MPa（只作为设计参数写入说明，未做分区与减压）",
        "",
        "二、计算结果（tools/hydraulics.py::calc_velocity，程序化计算，不经模型）",
    ]
    for record in plan.calculations:
        if record.velocity_mps is not None:
            lines.append(f"  {record.label}：流速 {record.velocity_mps:.4f} m/s（输入 {json.dumps(record.inputs, ensure_ascii=False)}）")
        else:
            lines.append(f"  {record.label}：未计算，缺输入 {list(record.missing_inputs)}（不补默认值）")
        lines.extend(f"    calc_velocity 标注：{item}" for item in record.assumptions)
    lines += ["", "三、提示、缺输入与假设"]
    lines += [f"  [提示] {item}" for item in [*plan.warnings, *result._dwg_warnings()]]
    lines += [f"  [缺输入] {item}" for item in plan.missing_inputs]
    lines += [f"  [假设] {item}" for item in plan.assumptions]
    lines += ["", "四、图纸清单"]
    for sheet in result.sheets:
        lines.append(
            f"  {sheet.filename}：{sheet.title}；生成器画的管段 {sheet.readback.planned_segments} 条，"
            f"读回核对管径一致 {sheet.readback.dn_matched} 条"
        )
        if sheet.dwg is not None:
            lines.append(f"    DWG（实验性）：{sheet.dwg.status}——{sheet.dwg.reason}")
    lines += [
        "",
        "五、程序化复审报告（S50 review_uploaded_dxf 原样输出，含未检查项）",
        "  现有审查规则只覆盖排水管段；给水管段不在规则作用域内，报告里的「未检查」不代表合规。",
    ]
    for sheet in result.sheets:
        lines += ["", f"—— {sheet.filename} ——", sheet.review.text]
    lines += ["", "六、声明", f"  {DISCLAIMER}", f"  {DRAFT_NOTICE}；未在 AutoCAD / 天正中打开验证，只验证过 ezdxf 与本仓库读取器可正常读取。"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# 生成
# ---------------------------------------------------------------------------


def generations_root(export_root: Path) -> Path:
    return Path(export_root) / GENERATIONS_DIRNAME


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)
    os.chmod(path, FILE_MODE)


def _render_failed(stage: str, exc: BaseException) -> GenerationError:
    # 内部异常只进服务端日志；对外只有稳定码与固定说法（不回显异常文本）。
    logger.error("drawing generation failed at %s: %s", stage, type(exc).__name__, exc_info=exc)
    return GenerationError(GEN_RENDER_FAILED, RENDER_FAILED_MESSAGE)


def generate_drawing_set(
    request: GenerationRequest | dict[str, Any],
    *,
    export_root: Path,
    owner_user_id: str | None = None,
    rules: BuildingRules | None = None,
) -> GenerationResult:
    """出一套图并落盘。输入不合法抛 ``GEN_INVALID_INPUT`` / ``GEN_UNSUPPORTED_SYSTEM``；
    规划、渲染、读回、复审的内部失败抛 ``GEN_RENDER_FAILED``；导出目录写不进去抛 ``OSError``。
    """

    timings: dict[str, float] = {}
    started = time.perf_counter()
    parsed = parse_generation_request(request)
    try:
        table = rules if rules is not None else load_building_rules()
    except BuildingRulesError as exc:
        raise _render_failed("rules", exc) from None
    resolve_riser_position(parsed, table)  # 立管位置不属于该建筑类型 → GEN_INVALID_INPUT

    try:
        mark = time.perf_counter()
        plan = plan_generation(parsed, table)
        timings["plan"] = (time.perf_counter() - mark) * 1000
        sheets: list[SheetResult] = []
        payloads: dict[str, bytes] = {}
        for sheet_plan in plan.sheets():
            sheets.append(_produce_sheet(sheet_plan, parsed, payloads, timings))
    except GenerationError:
        raise
    except Exception as exc:  # noqa: BLE001 - 任何内部失败都收口为 GEN_RENDER_FAILED
        raise _render_failed("render", exc) from None

    generation_id = uuid.uuid4().hex
    created_at = datetime.now(UTC).isoformat(timespec="seconds")
    root = generations_root(export_root)
    root.mkdir(parents=True, exist_ok=True)
    partial = root / f".partial-{generation_id}"
    final = root / generation_id
    zip_name = f"给水图纸-{parsed.building_type.value}-{parsed.floors}层-{generation_id[:8]}.zip"
    mark = time.perf_counter()
    try:
        partial.mkdir(mode=DIR_MODE)
        os.chmod(partial, DIR_MODE)
        for filename, data in payloads.items():
            _write(partial / filename, data)
        draft = GenerationResult(
            generation_id=generation_id,
            created_at=created_at,
            owner_user_id=owner_user_id,
            plan=plan,
            sheets=tuple(sheets),
            directory=final,
            zip_name=zip_name,
            zip_bytes=0,
            timings_ms=timings,
        )
        readme = _readme(draft, table).encode("utf-8-sig")
        _write(partial / README_NAME, readme)
        with zipfile.ZipFile(partial / ZIP_NAME, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for filename, data in payloads.items():
                archive.writestr(filename, data)
            archive.writestr(README_NAME, readme)
        os.chmod(partial / ZIP_NAME, FILE_MODE)
        zip_bytes = (partial / ZIP_NAME).stat().st_size
        meta = {
            "meta_version": META_VERSION,
            "generation_id": generation_id,
            "owner_user_id": owner_user_id,
            "created_at": created_at,
            "zip_name": zip_name,
            "zip_bytes": zip_bytes,
            "files": sorted(payloads),
            "request": parsed.model_dump(mode="json"),
            "rules_version": plan.rules_version,
        }
        _write(partial / META_NAME, json.dumps(meta, ensure_ascii=False, indent=2).encode("utf-8"))
        os.replace(partial, final)
    except OSError:
        shutil.rmtree(partial, ignore_errors=True)
        raise
    timings["write"] = (time.perf_counter() - mark) * 1000
    timings["total"] = (time.perf_counter() - started) * 1000
    return GenerationResult(
        generation_id=generation_id,
        created_at=created_at,
        owner_user_id=owner_user_id,
        plan=plan,
        sheets=tuple(sheets),
        directory=final,
        zip_name=zip_name,
        zip_bytes=zip_bytes,
        timings_ms={key: round(value, 1) for key, value in timings.items()},
    )


def _produce_sheet(
    sheet_plan: SheetPlan,
    request: GenerationRequest,
    payloads: dict[str, bytes],
    timings: dict[str, float],
) -> SheetResult:
    problems = validate_sheet(sheet_plan)
    if problems:
        raise RuntimeError(f"{sheet_plan.sheet_id} 规划不变量不成立：{problems[:3]}")
    filename = SHEET_FILENAMES[sheet_plan.sheet_id]

    mark = time.perf_counter()
    rendered = render_sheet(sheet_plan)
    timings[f"render.{sheet_plan.sheet_id}"] = (time.perf_counter() - mark) * 1000

    mark = time.perf_counter()
    readback = verify_readback(rendered, sheet_plan)
    timings[f"readback.{sheet_plan.sheet_id}"] = (time.perf_counter() - mark) * 1000
    if not readback.verified:
        raise RuntimeError(
            f"{sheet_plan.sheet_id} 读回核对未通过：ok={readback.ok} truncated={readback.truncated} "
            f"pipes={readback.pipe_segments}/{readback.planned_segments} dn={readback.dn_matched}"
        )

    mark = time.perf_counter()
    try:
        review = review_uploaded_dxf(rendered.data, display_name=filename)
    except UploadError as exc:
        raise RuntimeError(f"{sheet_plan.sheet_id} 复审失败：{exc.code}") from exc
    timings[f"review.{sheet_plan.sheet_id}"] = (time.perf_counter() - mark) * 1000
    payloads[filename] = rendered.data

    dwg: DwgExport | None = None
    if request.include_dwg:
        mark = time.perf_counter()
        try:
            dwg = export_dwg(sheet_plan)
        except Exception as exc:  # 实验性输出的故障不得连累 DXF 交付
            logger.error("experimental DWG export failed: %s", type(exc).__name__, exc_info=exc)
            dwg = DwgExport("failed", "DWG 输出内部错误，只交付 DXF")
        timings[f"dwg.{sheet_plan.sheet_id}"] = (time.perf_counter() - mark) * 1000
    result = SheetResult(
        sheet_id=sheet_plan.sheet_id,
        title=sheet_plan.title,
        filename=filename,
        scale=sheet_plan.scale,
        readback=readback,
        review=review,
        dwg=dwg,
    )
    if dwg is not None and dwg.data is not None and result.dwg_filename:
        payloads[result.dwg_filename] = dwg.data
    return result


# ---------------------------------------------------------------------------
# 下载侧：读回元数据并校验归属
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredGeneration:
    generation_id: str
    owner_user_id: str | None
    zip_path: Path
    zip_name: str


def find_generation(export_root: Path, generation_id: str, *, owner_user_id: str) -> StoredGeneration | None:
    """按 id 找回一次生成；id 形态不对、不存在、或不属于该用户，一律 None（路由统一 404）。"""

    try:
        normalized = uuid.UUID(hex=str(generation_id)).hex
    except ValueError:
        return None
    directory = generations_root(export_root) / normalized
    try:
        meta = json.loads((directory / META_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict) or meta.get("owner_user_id") != owner_user_id:
        return None
    zip_path = directory / ZIP_NAME
    if not zip_path.is_file():
        return None
    return StoredGeneration(
        generation_id=normalized,
        owner_user_id=owner_user_id,
        zip_path=zip_path,
        zip_name=str(meta.get("zip_name") or ZIP_NAME),
    )


__all__ = [
    "DRAFT_NOTICE",
    "GENERATIONS_DIRNAME",
    "META_NAME",
    "README_NAME",
    "SHEET_FILENAMES",
    "ZIP_NAME",
    "GenerationResult",
    "SheetResult",
    "StoredGeneration",
    "find_generation",
    "generate_drawing_set",
    "generations_root",
    "plan_generation",
]
