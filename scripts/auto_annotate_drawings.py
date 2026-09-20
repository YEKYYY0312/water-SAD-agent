#!/usr/bin/env python3
"""P2.5 真实图纸启发式标注（诚实版）。

与 docs/P2.5_CURSOR_AUTO.txt 初版的区别（必须先读）：
- 初版 import 的 read_dxf_file 在本仓不存在（真实 API 是
  read_drawing + ToolPolicy），且正则只认 TEXT、只认图层含
  给水/排水/消防 —— 真实 18 张图的图层多为 P_XF_DIM / 0 /
  $TD_AUDIT_GENERATED_*，初版会静默产出 0 缺陷。
- 初版引用的 GB50015-2019 3.5.2 / 4.3.2 / 8.2.2 / 3.5.4 /
  8.2.3 / 4.3.5 在本仓语料库不存在（实测 GB50015-2019 仅 9 条，
  且自述全部为非正式综述）。按铁律 2 禁止编造条款，本脚本
  只引用真实在库条文 GB50015-2019 4.7.1（坡度 0.005~0.01，
  INFORMAL_SUMMARY），其余一律标 engineering_practice，不配
  source_id、不使用“违反规范”表述。
- 输出是“待人工复核的候选标注”，不是 gold：每个缺陷带
  confidence + needs_manual_review=true；README 与 jsonl 明确
  标注 accepts_recall_gate=false，不可直接当召回率证据。

检测面（全部基于 read_drawing 已解码/MIF/护栏后的文本）：
  A. P3 管线 findings（map_pipes + review_drawing）：程序化阈值，
     如 drain.min_diameter（<DN300，工程惯例）。
  H1. 小管径文本观测：DN/De<=80（DN110/DN133 合规值豁免；
      D159×4.5/D219×6/DN133×4 等壁厚写法豁免：DNX 只取首值，
      次值壁厚不判定）或 D 系列 100<=x<300
      （去重到按“取值×图层”一条，附 occurrences 计数；
      含坡度标注的文本只走坡度检查，不重复记管径；
      小写 d 系列（d200/d150 合规室外管径）不记管径候选）。
  H2. 塑料管材文本观测：独立词 PVC/UPVC/PPR/PE/HDPE 出现即记一条
      （只说明“需确认是否用于热水/消防”，不定罪；
      PVC板构件、APPROVED/APPR. 图签英文子串豁免）。
  H3. 坡度文本观测：I=0.xxx / I=1:xxx，小于 0.005 即记一条，
      引用真实在库 GB50015-2019 4.7.1（非正式综述）。
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

# mypy 不检查 sys.path 动态插入的仓内模块（与 scripts/ 下其他脚本一致，
# ci_gate 的 mypy 范围也只是 src/water_agent）。
from water_agent.drawing.dxf_reader import read_drawing  # type: ignore[import-untyped]
from water_agent.drawing.mapping import map_pipes  # type: ignore[import-untyped]
from water_agent.drawing.rules import review_drawing  # type: ignore[import-untyped]
from water_agent.runtime.governance.pipeline import ToolPolicy  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).parent.parent
DRAWINGS_DIR = REPO_ROOT / "data" / "drawings" / "real_samples"
ANNOTATIONS_DIR = REPO_ROOT / "data" / "eval" / "drawing_p1" / "annotations"

DN_RE = re.compile(r"(?<![A-Za-z0-9])(?:DN|De|dn|de)\s*(\d{2,4})\b")
DNX_RE = re.compile(r"(?<![A-Za-z0-9])DN\s*(\d{2,4})\s*[x×]\s*(\d{1,4})", re.IGNORECASE)
#: 小写 d 系列（室外排水常用 d+数字，如“d 200 i=0.020”）：
#: 仅 100<=d<300 记为管径候选；d150/d200 的正常值只记坡度时
#: 的伴随信息，不单独记管径（由 _slope_text_has_diameter 判定）。
D_LOWER_RE = re.compile(r"(?<![A-Za-z])d\s*(\d{2,4})\b")
D_SERIES_RE = re.compile(r"(?<![A-Za-z])D\s*(\d{3,4})\b")
SLOPE_EQ_RE = re.compile(r"\b[iI]\s*[=＝]\s*([0-9]*\.?[0-9]+)")
SLOPE_RATIO_RE = re.compile(r"\b[iI]\s*[=＝]\s*1\s*[:：]\s*(\d+(?:\.\d+)?)")
PLASTIC_RE = re.compile(r"PVC-U|UPVC|PVC|PPR|HDPE|\bPE\b", re.IGNORECASE)

SLOPE_INFORMAL_MIN = 0.005
SMALL_DN_MAX = 80
#: D 系列（室外排水常用 D+数字）：仅 D<300 记为候选，对应 P3
#: drain.min_diameter 的惯例下限 DN300；D300/D400 合规不记。
#: 含坡度标注（I=）的文本只走坡度检查，不重复记管径，避免
#: “D200 I=0.005”被拆成一条管径待核。
D_SERIES_FLAG_MAX = 300

#: 标注日期（固定值，保证输出可复现；改日期须同步更新报告）。
ANNOTATION_DATE = datetime(2026, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai")).date().isoformat()

DRAWING_TYPE_BY_PREFIX = (("PUMP-", "泵房图"), ("P-", "平面图"), ("S-", "系统图"), ("D-", "大样图"))


def drawing_type_of(drawing_id: str) -> str:
    for prefix, label in DRAWING_TYPE_BY_PREFIX:
        if drawing_id.startswith(prefix):
            return label
    return "未知"


def parse_slope_value(text: str) -> float | None:
    m = SLOPE_RATIO_RE.search(text)
    if m:
        try:
            denom = float(m.group(1))
            if denom > 0:
                return 1.0 / denom
        except ValueError:
            return None
    m = SLOPE_EQ_RE.search(text)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def annotate_one(drawing_id: str, root: Path, policy: ToolPolicy) -> dict[str, Any]:
    extraction = read_drawing(drawing_id, root=root, policy=policy, max_entities=50000)
    if not extraction.ok:
        return {
            "drawing_id": drawing_id,
            "drawing_type": drawing_type_of(drawing_id),
            "annotation_date": ANNOTATION_DATE,
            "annotator": "启发式文本扫描（非规范判定）+ P3 程序化管线",
            "total_defects": 0,
            "extraction_ok": False,
            "extraction_errors": list(extraction.errors),
            "defects": [],
            "notes": "图纸解析失败，无可标注图元；先解决 extraction_errors。",
        }
    mapping = map_pipes(extraction)
    review = review_drawing(mapping)
    texts = [e for e in extraction.entities if e.kind.value in ("text", "mtext") and e.text.strip()]

    defects: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(defect: dict[str, Any]) -> None:
        key = (defect["category"], defect["actual"], defect["location"]["layer"])
        if key in seen:
            for d in defects:
                if (d["category"], d["actual"], d["location"]["layer"]) == key:
                    d["occurrences"] = int(d.get("occurrences", 1)) + int(defect.get("occurrences", 1))
                    break
            return
        seen.add(key)
        defects.append(defect)

    for f in review.findings:
        d = f.to_dict()
        add(
            {
                "category": "P3管线检查",
                "rule_id": d["rule_id"],
                "severity": "中" if d["severity"] == "medium" else d["severity"],
                "description": d["rendered"],
                "location": {
                    "layer": d["layer"],
                    "entity_type": "LINE/POLYLINE",
                    "entity_content": d["segment_id"],
                    "approx_position": f"图元 handle={d['handle']}，图层 {d['layer']}",
                    "handle": d["handle"],
                },
                "regulation": {
                    "spec": d["source_id"] or "工程惯例（无条文支撑）",
                    "evidence_level": d["evidence_level"],
                    "basis": d["basis"],
                },
                "expected": "见 rendered 中的阈值描述",
                "actual": d["detail"],
                "occurrences": 1,
                "confidence": "中",
                "needs_manual_review": True,
                "notes": "P3 程序化管线产出；drain.min_diameter 为工程惯例级，不得表述为违反规范。",
            }
        )

    small_dn_groups: dict[tuple[str, str], Counter[str]] = {}
    for e in texts:
        # 防护栏：D 系列是大口径钢制管外径写法（D159×4.5/D219×6/D325/D426），
        # 属于壁厚标注而非 DN 公称直径，不参与小管径判定（复核 D-02/P-02）。
        # 复核 P-02-D003：“2 DN133×4”是壁厚式写法：DN133 为公称直径
        # （排水常用管径，合规不记），“×4”为壁厚。DNX 只取首值判小管径，
        # 次值（壁厚）不参与判定；DN133/DN110 本身合规不记。
        for m in DN_RE.findall(e.text):
            if re.search(rf"DN\s*{m}\s*[×x]\s*\d", e.text):
                continue
            dn_v = int(m)
            if dn_v in (110, 133):
                continue
            if dn_v <= SMALL_DN_MAX:
                small_dn_groups.setdefault((f"DN{dn_v}", e.layer), Counter())[e.text.strip()] += 1
        for a, b in DNX_RE.findall(e.text):
            if int(a) in (110, 133):
                continue
            if int(a) <= SMALL_DN_MAX:
                small_dn_groups.setdefault((f"DN{a}", e.layer), Counter())[e.text.strip()] += 1
        # 含坡度标注的文本只走坡度检查，不重复记管径
        if SLOPE_EQ_RE.search(e.text) or SLOPE_RATIO_RE.search(e.text):
            continue
        for m in D_SERIES_RE.findall(e.text):
            d_v = int(m)
            # 复核 P-02：D159×4.5/D219×7 为壁厚写法，非管径违规，不记。
            if re.search(rf"D\s*{d_v}\s*[×x]\s*\d", e.text):
                continue
            if d_v < D_SERIES_FLAG_MAX:
                small_dn_groups.setdefault((f"D{d_v}", e.layer), Counter())[e.text.strip()] += 1
        for m in D_LOWER_RE.findall(e.text):
            d_v = int(m)
            # 复核 P-07：d200/d150 均为合规室外排水管径（常用 d200/d300），
            # 且多与 i=0.020/i=0.010 坡度标注同条出现；小写 d 系列不记管径候选，
            # 只在坡度检查里作为伴随信息（坡度样本原文已含管径）。
            continue
    for (norm, layer), samples in sorted(small_dn_groups.items()):
        sample = "; ".join(f"{k}×{c}" for k, c in samples.most_common(3))[:200]
        add(
            {
                "category": "管径待核",
                "severity": "中",
                "description": f"图面出现小管径标注 {norm}（图层 {layer}），需人工确认系统与立管/支管属性后再判是否合规",
                "location": {"layer": layer, "entity_type": "TEXT/MTEXT", "entity_content": sample, "approx_position": f"图层 {layer}"},
                "regulation": {"spec": "工程惯例（无条文支撑）", "evidence_level": "engineering_practice", "basis": "未引用规范条款；小管径是否违规取决于系统、立管/支管与建筑类型，文本扫描无法判定"},
                "expected": "人工核对",
                "actual": norm,
                "occurrences": sum(samples.values()),
                "confidence": "低",
                "needs_manual_review": True,
                "notes": "仅文本观测，不构成规范违反判定；DN/De/D 裸值不区分立管支管与室内外。",
            }
        )

    plastic_groups: dict[tuple[str, str], Counter[str]] = {}
    for e in texts:
        # 复核 D-01：PVC 板（出水堰构件）非管道材料，不记。
        if "PVC" in e.text.upper() and "PVC板" in e.text:
            continue
        # 复核 D-02/P-04/P-05：APPROVED/APPR. 图签栏英文含 PPR 子串，
        # 必须要求 PPR 为独立词（前后非字母），否则英文图签全误报。
        pm = re.search(r"(?<![A-Za-z])(PVC-U|UPVC|PVC|PPR|HDPE|PE)(?![A-Za-z])", e.text, re.IGNORECASE)
        if pm:
            plastic_groups.setdefault((pm.group(0).upper(), e.layer), Counter())[e.text.strip()[:120]] += 1
    for (mat, layer), samples in sorted(plastic_groups.items()):
        sample = "; ".join(f"{k}×{c}" for k, c in samples.most_common(3))[:200]
        add(
            {
                "category": "材料待核",
                "severity": "中",
                "description": f"图面出现塑料管材字样 {mat}（图层 {layer}），需人工确认是否用于热水或消防系统",
                "location": {"layer": layer, "entity_type": "TEXT/MTEXT", "entity_content": sample, "approx_position": f"图层 {layer}"},
                "regulation": {"spec": "工程惯例（无条文支撑）", "evidence_level": "engineering_practice", "basis": "未引用规范条款；塑料管能否使用取决于系统与温度压力条件"},
                "expected": "人工核对",
                "actual": mat,
                "occurrences": sum(samples.values()),
                "confidence": "低",
                "needs_manual_review": True,
                "notes": "仅文本观测；消防/热水材料判定需结合系统图与说明页，不单凭一词定罪。",
            }
        )

    slope_groups: dict[tuple[str, str, float], Counter[str]] = {}
    for e in texts:
        if "I" not in e.text and "i" not in e.text:
            continue
        slope_v: float | None = parse_slope_value(e.text)
        if slope_v is None:
            continue
        if slope_v < SLOPE_INFORMAL_MIN:
            slope_groups.setdefault((e.text.strip()[:60], e.layer, slope_v), Counter())[e.text.strip()[:120]] += 1
    for (shown, layer, slope_num), samples in sorted(slope_groups.items(), key=lambda kv: kv[0][2]):
        sample = "; ".join(f"{k}×{c}" for k, c in samples.most_common(3))[:200]
        add(
            {
                "category": "坡度待核",
                "severity": "中",
                "description": f"图面坡度标注 {shown}（≈{slope_num:.4f}，图层 {layer}）低于设计要点下限 0.005，需人工结合管径复核",
                "location": {"layer": layer, "entity_type": "TEXT/MTEXT", "entity_content": sample, "approx_position": f"图层 {layer}"},
                "regulation": {"spec": "GB50015-2019", "clause": "4.7.1", "evidence_level": "informal_summary", "requirement": "排水横管坡度宜 0.005~0.01（语料自述为设计要点综述，非正式条文）"},
                "expected": "i ≥ 0.005（设计要点，非正式条文）",
                "actual": shown,
                "occurrences": sum(samples.values()),
                "confidence": "低",
                "needs_manual_review": True,
                "notes": "唯一引用真实在库条文的一项，且为非正式综述级；不得表述为违反规范，具体管径对应坡度需人工核对。",
            }
        )

    for i, d in enumerate(defects, start=1):
        d["defect_id"] = f"{drawing_id}-D{i:03d}"
    return {
        "drawing_id": drawing_id,
        "drawing_type": drawing_type_of(drawing_id),
        "annotation_date": ANNOTATION_DATE,
        "annotator": "启发式文本扫描（非规范判定）+ P3 程序化管线",
        "total_defects": len(defects),
        "extraction_ok": True,
        "extraction_summary": {
            "entity_count": len(extraction.entities),
            "layer_count": len(extraction.layers),
            "unit_known": extraction.unit_known,
            "insunits": extraction.insunits,
            "truncated": extraction.truncated,
            "pipes_reviewed": len(mapping.pipes),
            "mapping_missing_inputs": list(mapping.missing_inputs),
            "review_not_checked": list(review.not_checked),
        },
        "defects": defects,
        "notes": "候选标注非 gold：管径/材料/坡度三类待核均须人工复核后才可转评测 gold；P3 findings 为程序化产出，其中 min_diameter 系工程惯例级。INSERT 块内文字不可见，实际缺陷可能更多。",
    }


def main() -> int:
    root = DRAWINGS_DIR.resolve()
    if not root.is_dir():
        print(f"图纸目录不存在: {root}")
        return 2
    policy = ToolPolicy(allowed_read_dirs=frozenset({root}))
    dxf_files = sorted(root.glob("*.dxf"))
    print(f"找到 {len(dxf_files)} 份图纸")
    ANNOTATIONS_DIR.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    for dxf in dxf_files:
        try:
            ann = annotate_one(dxf.stem, root, policy)
        except Exception as exc:  # noqa: BLE001 — 单张失败记日志继续，不中断批量
            print(f"  ✗ {dxf.name} 标注失败: {exc}")
            continue
        out = ANNOTATIONS_DIR / f"{dxf.stem}.json"
        out.write_text(json.dumps(ann, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        for d in ann["defects"]:
            counts[d["category"]] += 1
        conf = Counter(d.get("confidence", "?") for d in ann["defects"])
        print(f"  ✓ {dxf.name}: {ann['total_defects']} 条候选 (高:{conf.get('高',0)} 中:{conf.get('中',0)} 低:{conf.get('低',0)})")
    print(f"\n✅ 完成，输出: {ANNOTATIONS_DIR}")
    print(f"   分类合计: {dict(counts)}")
    print("   ⚠️ 全部为待复核候选，非 gold；转评测前须人工复核。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
