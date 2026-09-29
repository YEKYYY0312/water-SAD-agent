"""DXF 解析：文件 → 图元中间格式，含路径三层防御、块展开与四道护栏。

模型空间中的 INSERT 会保留为可查询的块引用，并通过 ``virtual_entities()``
展开其可见内容；展开后的图元带有 ``source_block`` 来源链，嵌套块使用 ``/``
连接块名。块展开失败只记录错误，不影响同一文件中其他图元的解析。

规格 `docs/design/13-p3-drawing-review.md` §2/§4/§5。

路径三层纵深防御（决策记录 D3）
--------------------------------
护栏的 `_looks_like_path` **不识别相对路径**（已实测：`plan1.dxf` 走
`validate_tool_call` 返回 `allowed=True`），且它刻意保守以免误伤 `DN200/DN150`，
不能靠收紧正则修复。本模块因此不依赖它作为唯一防线：

1. `_DRAWING_ID_RE` 让**模型无法表达路径** —— 任何分隔符、``..``、绝对路径形态
   在登记号校验就被拒。护栏的相对路径缺口对本模块**不适用**（不是"已修复"）。
2. 拼接后 ``resolve()`` 并 ``relative_to(root)``，防符号链接逃逸。
3. ``ToolPolicy.read_dir_allowed`` 最终裁定（护栏层白名单）。

四道护栏
---------
类型（只认 ``.dxf``）、大小（``MAX_DRAWING_BYTES``）、路径（上述三层）、
内容（逐图元 ``check_retrieved_content``，**REDACT 语义**：脱敏 + 标记，
不丢弃图元、不中断整张图 —— 否则一张被投毒的图纸会把读图能力整体废掉）。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from water_agent.drawing.drawing_root import DRAWING_SUFFIX, UNSUPPORTED_SUFFIXES
from water_agent.drawing.mif import mif_decode
from water_agent.drawing.models import (
    DRAWING_DEPENDENCY_MISSING,
    DRAWING_NOT_FOUND,
    DRAWING_PATH_DENIED,
    DRAWING_TOO_LARGE,
    DRAWING_UNREADABLE,
    DRAWING_UNSUPPORTED_FORMAT,
    DrawingEntity,
    DrawingEntityKind,
    DrawingExtraction,
)
from water_agent.runtime.governance.contracts import ContentEnvelope, SourceTag
from water_agent.runtime.governance.pipeline import (
    DEFAULT_TOOL_POLICY,
    GuardrailPipeline,
    ToolPolicy,
    default_guardrail_pipeline,
)
from water_agent.runtime.governance.rules import redact_text

#: 图纸登记号：字母数字开头，只允许字母数字与 ``.`` ``_`` ``-``。
#: **刻意不含 ``/`` ``\`` 与 ``..``** —— 这是路径防御第一层，让模型无法表达路径。
_DRAWING_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: 单张图纸字节上限（防资源耗尽 / prompt stuffing）。
MAX_DRAWING_BYTES = 20 * 1024 * 1024

#: 单条标注文本进 envelope 前的截断长度（`MAX_CONTENT_BYTES` 是 200KB，
#: 但单条标注没有正常理由超过这个长度，早截省得护栏跑长文本）。
_SINGLE_TEXT_CAP = 2000

_DXFTYPE_TO_KIND: dict[str, DrawingEntityKind] = {
    "LINE": DrawingEntityKind.LINE,
    "LWPOLYLINE": DrawingEntityKind.POLYLINE,
    "POLYLINE": DrawingEntityKind.POLYLINE,
    "CIRCLE": DrawingEntityKind.CIRCLE,
    "ARC": DrawingEntityKind.ARC,
    "TEXT": DrawingEntityKind.TEXT,
    "MTEXT": DrawingEntityKind.MTEXT,
    "INSERT": DrawingEntityKind.INSERT,
}

#: 图层名前缀 → 系统归属。给排水图纸的常见约定（W/WS = 给水，D/DR/S = 排水）。
#: 匹配不上的图元归 ``None``（既非给水也非排水），``system="all"`` 时仍返回。
_WATER_SUPPLY_HINTS = ("w-", "ws-", "给水", "冷水", "热水")
_DRAINAGE_HINTS = ("d-", "dr-", "s-", "排水", "污水", "废水", "雨水", "通气")


def _err(code: str, detail: str) -> str:
    """错误项格式 ``<CODE>: <detail>``，与 `DrawingExtraction.error_codes` 对应。"""
    return f"{code}: {detail}"


def _oda_installed() -> bool:
    """ODA File Converter 是否可用 —— **运行时探测，不硬编码**。

    P3 不因它存在而改变行为（转换仍需 `EXECUTE_SHELL`，构造期即被拒绝），
    只用于让 `.dwg` 的错误提示说实话：该不该让用户去手动转（规格 §2.2）。

    **为何走 `getattr` 而非直接调用**：上游 `ezdxf/addons/odafc.py` 第 3 行有文件级
    `# type: ignore`，mypy 因此把该模块整体视为无类型，`odafc.is_installed` 会报
    `attr-defined`，换任何 import 形式都绕不开。规格 §8 验收 2 要求 mypy 0 告警
    且**不得引入 `type: ignore`**，所以这里用动态查找 —— 顺带让「上游哪天挪掉这个
    函数」变成结构化的"不可用"而不是 AttributeError 崩栈。
    """
    try:
        from ezdxf.addons import odafc

        probe = getattr(odafc, "is_installed", None)
        if probe is None:
            return False
        return bool(probe())
    except Exception:  # noqa: BLE001 — 探测失败一律视为不可用，不影响主流程
        return False


def _failed(drawing_id: str, code: str, detail: str) -> DrawingExtraction:
    return DrawingExtraction(ok=False, drawing_id=drawing_id, errors=(_err(code, detail),))


def _layer_system(layer: str) -> str | None:
    """按图层名猜系统归属。**只用于过滤，不用于任何数值结论。**"""
    low = layer.strip().lower()
    if any(h in low for h in _WATER_SUPPLY_HINTS):
        return "water_supply"
    if any(h in low for h in _DRAINAGE_HINTS):
        return "drainage"
    return None


def resolve_drawing_path(drawing_id: str, root: Path) -> tuple[Path | None, str]:
    """登记号 → 绝对路径。返回 ``(path, error_detail)``，失败时 path 为 None。

    实现路径防御的第 1、2 层。第 3 层（读白名单）由 `read_drawing` 调用护栏完成。
    """
    if not _DRAWING_ID_RE.fullmatch(drawing_id or ""):
        return None, (
            f"图纸登记号非法: {drawing_id!r}（只允许字母数字与 . _ -，"
            "不得含路径分隔符或 ..）"
        )
    try:
        root_resolved = root.resolve()
        candidate = (root_resolved / f"{drawing_id}{DRAWING_SUFFIX}").resolve()
    except (OSError, RuntimeError) as exc:
        return None, f"路径解析失败: {exc}"
    try:
        candidate.relative_to(root_resolved)
    except ValueError:
        # 登记号正则已排除分隔符，走到这里说明符号链接逃逸出了 root。
        return None, "解析后的路径逃逸出 DRAWING_ROOT（疑似符号链接）"
    return candidate, ""


def _entity_points(entity: Any, dxftype: str) -> tuple[tuple[float, float], ...]:
    """抽取二维顶点。**显式 float() 强制** —— ezdxf 的 LWPOLYLINE 返回 np.float64，
    它恰好是 float 子类所以能 json 序列化，但依赖子类关系是脆的，且 numpy 标量
    会污染下游类型判断（规格 §5.3）。
    """
    dxf = entity.dxf
    if dxftype == "LINE":
        return (
            (float(dxf.start[0]), float(dxf.start[1])),
            (float(dxf.end[0]), float(dxf.end[1])),
        )
    if dxftype == "LWPOLYLINE":
        return tuple((float(p[0]), float(p[1])) for p in entity.get_points("xy"))
    if dxftype == "POLYLINE":
        return tuple(
            (float(v.dxf.location[0]), float(v.dxf.location[1])) for v in entity.vertices
        )
    if dxftype in ("CIRCLE", "ARC"):
        return ((float(dxf.center[0]), float(dxf.center[1])),)
    if dxftype == "TEXT":
        return ((float(dxf.insert[0]), float(dxf.insert[1])),)
    if dxftype in ("MTEXT", "INSERT"):
        return ((float(dxf.insert[0]), float(dxf.insert[1])),)
    return ()


def _entity_text(entity: Any, dxftype: str) -> str:
    """抽取标注文本，并**解码 AutoCAD 的 MIF 转义**。

    MTEXT 先用 `plain_mtext` 归一 ``\\P`` 段落符（规格 §5.4），再解码。

    **为什么必须先 plain_mtext、后 mif_decode**：MIF 转义在 ``plain_mtext`` 里被
    当作未知转义序列**原样保留**（已实测），而反过来先解码会让解出的 ``{`` ``}``
    ``\\`` 暴露给 MTEXT 的格式解析器，可能被当成格式组吞掉。两种顺序在实测样本上
    结果一致，取"改动最小 + 不让转义载荷进入解析器"的那个。

    **为什么这个解码是必需的（不是锦上添花）**：真实图纸里 3,387 条文本图元有
    **1,239 条（37%）** 是 ``\\M+5XXXX`` 形态。不解码，送进模型的是
    ``\\M+5D0C2\\M+5BDA8…`` 而不是「新建建(构)筑物」—— 标注全部不可读，
    审查链拿不到管径/高程/构筑物名。合成样本 `drawing_p0` 用明文，
    **永远暴露不出这个缺口**，所以只能靠真实样本发现（P2 Step 5）。

    **同时它也是安全边界**：解码放在 `_guard_text` 之前，注入检测与 PII 脱敏
    才能看到真实文本。否则把注入指令写成 MIF 转义即可绕过
    `check_retrieved_content` 的内容护栏 —— 转义形态此前是一条约定的绕过路径。
    """
    if dxftype == "TEXT":
        raw = str(entity.dxf.text or "")
    elif dxftype == "MTEXT":
        from ezdxf.tools.text import plain_mtext

        raw = str(plain_mtext(entity.text or ""))
    else:
        return ""
    # `mif_decode` 幂等：无 `\M+` 即原样返回，重复调用不改变结果。
    return mif_decode(raw)


def _guard_text(
    text: str, *, pipeline: GuardrailPipeline, file_hash: str, handle: str
) -> tuple[str, tuple[str, ...]]:
    """内容护栏：REDACT 语义（脱敏 + 标记），不丢弃图元。

    **按图元逐条包 envelope** —— `MAX_CONTENT_BYTES` 是单条 200KB 上限，
    整图标注拼成一条很容易破限，那样会让一张正常的大图整体失败。

    两道处理，顺序与理由：

    1. `check_retrieved_content` —— 间接注入检测。它**只标记不改写**
       （已实测），因为隔离由 `ContentEnvelope` + `RETRIEVED_FILE` 不在
       `INSTRUCTION_BEARING_SOURCES` 内保证，改写反而会破坏正常的规范引用标注。
    2. **PII 脱敏** —— `GuardrailPipeline` 的 `pii_mode` 只覆盖 input/output/
       持久化三面，**不覆盖检索面**（对公开条文语料合理：那里没有用户 PII）。
       但图纸标注是 `p2.5-threat-model.md` 登记的 **A6 用户 PII 资产**
       （业主联系人、电话写进图签是真实情况），且规格 §4.6 要求送模型前脱敏，
       所以这里必须显式补上，不能指望检索面。
       `redact_text` 的 PII 正则已带工程单位护栏（`m/s`、`kPa`、`DN` 等不误伤），
       已实测 7 个工程样本零误伤。
    """
    if not text:
        return "", ()
    # 孤立代理项（\udcXX）：ezdxf 以 surrogateescape 读入编码不符的字节时产生，LibreDWG
    # 转出的 DXF 实测会有（S51）。它不是合法字符，下游任何 .encode("utf-8") 都会抛
    # UnicodeEncodeError 打死整张图；换成 U+FFFD，文字其余部分照常可读。
    text = text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
    clipped = text[:_SINGLE_TEXT_CAP]
    envelope = ContentEnvelope(
        source_tag=SourceTag.RETRIEVED_FILE,
        text=clipped,
        reference_id=f"{file_hash}#{handle}",
    )
    outcome = pipeline.check_retrieved_content(envelope)
    rule_ids = {h.rule_id for h in outcome.hits}

    redacted, pii_hits = redact_text(outcome.content)
    rule_ids.update(h.rule_id for h in pii_hits)
    return redacted, tuple(sorted(rule_ids))


def read_drawing(
    drawing_id: str,
    *,
    root: Path,
    policy: ToolPolicy | None = None,
    pipeline: GuardrailPipeline | None = None,
    system: str = "all",
    max_entities: int = 500,
) -> DrawingExtraction:
    """读取一张已登记的 DXF 图纸，返回结构化图元。

    失败一律以 ``ok=False`` + 结构化错误码返回，**不抛异常给调用方**，
    也不静默返回空结果冒充成功。
    """
    effective_policy = policy if policy is not None else DEFAULT_TOOL_POLICY
    guard = pipeline if pipeline is not None else default_guardrail_pipeline()

    # --- 护栏 1/4：路径（三层防御的前两层）---
    path, detail = resolve_drawing_path(drawing_id, root)
    if path is None:
        return _failed(drawing_id, DRAWING_PATH_DENIED, detail)

    # --- 路径防御第三层：护栏读白名单 ---
    if not effective_policy.read_dir_allowed(path):
        return _failed(
            drawing_id,
            DRAWING_PATH_DENIED,
            "图纸不在允许读取的目录内（ToolPolicy.allowed_read_dirs 未授权）",
        )

    # --- 护栏 2/4：类型 ---
    # 登记号不含扩展名，拼接后恒为 .dxf，所以"检查 path.suffix"是死代码。
    # 真正要区分的是：**同名的不支持格式确实存在**（用户放了 plan.dwg 却没有
    # plan.dxf）—— 此时报"格式不支持 + 需人工预转"，而不是含糊的"文件不存在"，
    # 否则用户会以为图纸放错了目录，而实际问题是 P3 读不了 DWG（规格 §2.2）。
    if not path.is_file():
        for suffix in UNSUPPORTED_SUFFIXES:
            sibling = path.with_suffix(suffix)
            if sibling.is_file():
                return _failed(
                    drawing_id,
                    DRAWING_UNSUPPORTED_FORMAT,
                    f"存在 {sibling.name} 但 P3 只读 {DRAWING_SUFFIX}"
                    f"（DWG 转换需 EXECUTE_SHELL，该能力构造期即被拒绝；"
                    f"ODA File Converter {'已' if _oda_installed() else '未'}安装，"
                    "请人工转为 DXF 后重试）",
                )
        return _failed(drawing_id, DRAWING_NOT_FOUND, f"图纸不存在: {drawing_id}{DRAWING_SUFFIX}")

    # --- 护栏 3/4：大小 ---
    try:
        size = path.stat().st_size
    except OSError as exc:
        return _failed(drawing_id, DRAWING_NOT_FOUND, f"无法读取图纸属性: {exc}")
    if size > MAX_DRAWING_BYTES:
        return _failed(
            drawing_id,
            DRAWING_TOO_LARGE,
            f"图纸 {size} 字节，超过上限 {MAX_DRAWING_BYTES}",
        )

    try:
        raw = path.read_bytes()
    except OSError as exc:
        return _failed(drawing_id, DRAWING_UNREADABLE, f"读取失败: {exc}")
    file_hash = hashlib.sha256(raw).hexdigest()[:16]

    # ezdxf 是可选前置依赖：缺失 → 结构化前置缺失，不是"文件坏了"（照
    # materials/export.py 的先例，局部 import + 明确安装提示）。
    try:
        import ezdxf
    except ImportError:
        return _failed(
            drawing_id,
            DRAWING_DEPENDENCY_MISSING,
            "缺少 ezdxf，无法解析 DXF（安装：uv pip install 'ezdxf>=1.4,<2'）",
        )

    # ezdxf 对非 DXF 内容抛 OSError（实测，**不是** DXFError），故两者都捕获。
    # 截断的 DXF（头部合法、缺 EOF）在 dxf_info 里抛 StopIteration（P5-S50 上传实测）；
    # 本函数的契约是"永不抛异常"，逃出去的 StopIteration 在协程里还会变成
    # RuntimeError，所以一并收进 DRAWING_UNREADABLE。
    try:
        doc = ezdxf.readfile(str(path))
    except (OSError, ezdxf.DXFError, StopIteration, UnicodeDecodeError, ValueError) as exc:
        detail = str(exc) or type(exc).__name__
        return _failed(drawing_id, DRAWING_UNREADABLE, f"DXF 解析失败: {detail}")

    insunits_raw = doc.header.get("$INSUNITS")
    insunits = int(insunits_raw) if isinstance(insunits_raw, int) else None
    # 单位未声明或 unitless → 不猜（规格 §5.1）。
    unit_known = insunits is not None and insunits != 0
    # $INSUNITS 存在但取值是英寸/英尺（1/2）：实测（2026-09-22 对 18 份真实样本）
    # 中文市政给排水图纸几乎不用英制，这两个值更像 ODA/AutoCAD 模板残留而非
    # 真实图纸单位。不能直接当可信换算依据，但也不是"未声明"——是"声明了但
    # 不可信"，两者要分开表述，见 mapping.map_pipes 的处理。
    unit_suspicious = unit_known and insunits in (1, 2)

    assumptions: list[str] = []
    if not unit_known:
        assumptions.append(
            "图纸未声明单位（$INSUNITS 缺失或为 0），长度类结论需人工核对，"
            "解析器不做单位假定"
        )
    elif unit_suspicious:
        assumptions.append(
            f"$INSUNITS={insunits}（英寸/英尺）在中文市政图纸中极罕见，"
            "视为不可信声明而非真实图纸单位，长度类结论需人工核对，"
            "解析器不据此换算"
        )

    entities: list[DrawingEntity] = []
    errors: list[str] = []
    truncated = False
    expansion_limit = 32

    def append_entity(
        entity: Any,
        *,
        source_block: str | None,
        handle_prefix: str,
        depth: int = 0,
    ) -> None:
        nonlocal truncated
        if len(entities) >= max_entities:
            truncated = True
            return
        dxftype = entity.dxftype()
        layer = str(getattr(entity.dxf, "layer", ""))
        if system != "all" and _layer_system(layer) != system:
            return
        raw_handle = str(getattr(entity.dxf, "handle", "") or "")
        handle = raw_handle or handle_prefix
        text_raw = _entity_text(entity, dxftype)
        text, hits = _guard_text(
            text_raw, pipeline=guard, file_hash=file_hash, handle=handle
        )
        block_name = str(getattr(entity.dxf, "name", "")) if dxftype == "INSERT" else ""
        entities.append(
            DrawingEntity(
                handle=handle,
                kind=_DXFTYPE_TO_KIND.get(dxftype, DrawingEntityKind.OTHER),
                dxftype=dxftype,
                layer=layer,
                points=_entity_points(entity, dxftype),
                radius=(
                    float(entity.dxf.radius) if dxftype in ("CIRCLE", "ARC") else None
                ),
                text=text,
                suspicious=hits,
                block_name=block_name,
                source_block=source_block,
            )
        )

        if dxftype != "INSERT" or depth >= expansion_limit:
            if dxftype == "INSERT" and depth >= expansion_limit:
                errors.append(f"INSERT {handle} 嵌套深度超过 {expansion_limit}，停止展开")
            return
        nested_source = block_name if source_block is None else f"{source_block}/{block_name}"
        try:
            for index, virtual_entity in enumerate(entity.virtual_entities()):
                if len(entities) >= max_entities:
                    truncated = True
                    return
                append_entity(
                    virtual_entity,
                    source_block=nested_source,
                    handle_prefix=f"{handle}:virtual-{index}",
                    depth=depth + 1,
                )
        except Exception as exc:  # noqa: BLE001 — 块损坏不能阻断整张图
            errors.append(f"INSERT {handle} 展开失败: {exc}")

    for index, raw_entity in enumerate(doc.modelspace()):
        append_entity(
            raw_entity,
            source_block=None,
            handle_prefix=f"modelspace-{index}",
        )
        if truncated:
            break

    if truncated:
        assumptions.append(
            f"图元数超过 max_entities={max_entities}，结果已截断，不代表整图"
        )

    return DrawingExtraction(
        ok=True,
        drawing_id=drawing_id,
        file_hash=file_hash,
        dxf_version=str(doc.dxfversion),
        insunits=insunits,
        unit_known=unit_known,
        unit_suspicious=unit_suspicious,
        layers=tuple(sorted(str(layer.dxf.name) for layer in doc.layers)),
        entities=tuple(entities),
        truncated=truncated,
        errors=tuple(errors),
        assumptions=tuple(assumptions),
    )
