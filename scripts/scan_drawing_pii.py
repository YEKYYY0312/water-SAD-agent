#!/usr/bin/env python3
"""PII 泄漏复扫（组码流口径）—— 样本归档的验收闸门。

用法::

    _venv/bin/python scripts/scan_drawing_pii.py data/drawings/real_samples
    _venv/bin/python scripts/scan_drawing_pii.py /tmp/dxf_converted --expect-hits

退出码：0 干净 / 1 发现泄漏**或存在无法判定的文本** / 2 前置条件缺失。

为什么是**组码流**而不是 ezdxf 对象树
------------------------------------
1. **无盲区**。对象树只走 ``doc.layouts`` 与 ``doc.blocks``，够不到 CLASSES / TABLES /
   XDATA / 自定义节；脱敏模块改的却是整条组码流。两侧口径必须一致，否则"扫不到"
   可能只是扫描器没走到。
2. **不重复计数**。``doc.blocks`` 里含 ``*Model_Space`` / ``*Paper_Space`` 块定义，
   其内容与 ``doc.layouts`` 的模型/图纸空间是**同一批实体**。按对象树遍历会把模型空间
   的每个文本数两遍 —— 本脚本第一版就踩了这个坑，把 p05 的 4 处地址报成 5 处，
   进而让"命中数"与脱敏报告对不上。**计数对不上时，先怀疑自己的口径。**
3. **与模型实际看到的文本一致**。组码流里的每个字符串值都可能进提示词。

判据独立于 ``desensitize.py``
-----------------------------
本脚本的正则**故意不复用** ``desensitize.py`` 的任何规则对象。复用等于自己验自己：
规则漏了案例，两侧会一起漏（这正是 p05 地址第一版「修复」空转却没被发现的原因）。
两边各写一份，互为交叉验证。

三类必须分开的东西（踩坑记录）
------------------------------
1. **用户 PII** —— 必须删。邮箱/网址/手机/身份证/地址/证号/单位名/人名。
2. **厂商样板** —— 在 CLASSES 节里由 CAD 软件自己写入的代理类注册记录，例如
   ``WipeOut|AutoCAD Express Tool|expresstools@autodesk.com``（Autodesk）与
   ``TCH_KERNAL|缺少解释器天正图形看不见…|网址: www.tangent.com.cn``（天正）。
   这不是用户 PII，而且 CLASSES 节定义**代理实体类**，改写描述串有破坏代理实体
   解析的风险。本脚本按「**所在节 == CLASSES** 且值含厂商特征」双重条件判定为
   非泄漏 —— 单看值会误判，单看节会漏判。
   第一版没做这个区分，于是把 46 处厂商样板报成泄漏，噪声淹没了真实结论。
3. **编码形态** —— 同一个中文串可能是 MIF 转义、明文 UTF-8、或 Latin-1 伪编码。
   只认一种必然漏：第一版只做 ``mif_decode``，p03 里以明文 UTF-8 存放的
   ``杨兆华``/``汪华兵`` 完全扫不出来（``pii.person`` 恒为 0）。见 ``candidate_decodings``。

厂商样板**不被静默忽略**：它们在报告里单列一节「已判定非用户 PII」，写明理由。
静默放行等于给验收结论留一个看不见的缺口。

「无法判定」也要单列，而且**要阻断**
------------------------------------
还有第四种情况：文本是 **CP1252 重编码损坏**（``¹øÂˉ·¿`` 本应是「锅炉房」）。
它不是 PII、也不是厂商样板，而是**扫描器的盲区** —— 所有中文/ASCII 判据都看不见
里面有什么。实测 P-01 的图签栏（含人名与日期字段）正好落在这一类里。

这种串**不能算通过**：拿一个看不见目标的扫描器去证明目标不存在，等于没证明。
但它也**不能还原** —— 该变换多对一、不可逆（``À``/``à``、``Í``/``í`` 坍缩成同一
字符），硬解会产出「沉淀池」→「翅雾池」这类**貌似通顺的假中文**，比乱码更危险
（假中文会被当成事实引用）。完整论证见 `water_agent.drawing.mif.looks_like_mojibake`。

因此本脚本把它报成 `undecodable` 并让退出码为 1，把「看不见」变成「看得见」。
`--tolerate-undecodable` 可降为警告，但报告里仍然单列 —— 放行不等于通过。

阴性对照
--------
扫描器"什么都没找到"也可能是扫描器坏了。改动判据后请务必跑一次对照::

    _venv/bin/python scripts/scan_drawing_pii.py /tmp/dxf_converted --expect-hits

在**脱敏前**的目录上必须报出命中；报 0 说明判据失效，此时"脱敏后 0 命中"毫无意义。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from water_agent.drawing.desensitize import mif_decode
from water_agent.drawing.mif import looks_like_mojibake

#: 独立判据集。每条都对应一次**实测踩坑**，不是照着 desensitize.py 抄的。
CHECKS: dict[str, re.Pattern[str]] = {
    "pii.phone": re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    "pii.tel": re.compile(r"(?<!\d)0\d{2,3}[-\s]?\d{7,8}(?!\d)"),
    "pii.email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "pii.id_card": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "pii.url": re.compile(r"(?:https?://|www\.)[^\s\"'<>，。；]+", re.IGNORECASE),
    # 地址：直辖市可省「市」字（p05 的「中国·北京朝阳区安慧北里安园甲8号」）。
    # 独立写法：不要求两端锚点，只要「行政区划 + 门牌号」同时出现即可 —— 与脱敏侧的
    # 两端锚定写法不同，属于有意为之的交叉验证。
    "pii.address": re.compile(
        r"(?:中国[·•.]?)?"
        r"(?:[\u4e00-\u9fa5]{2,8}(?:省|自治区|市)"
        r"|(?:北京|天津|上海|重庆|香港|澳门)(?:市)?)"
        r"[\u4e00-\u9fa5A-Za-z0-9]{1,24}?"
        r"(?:路|街|街道|道|里|园|村|大厦|大楼|楼|院|区|镇|乡)"
        r"[\u4e00-\u9fa5A-Za-z0-9]{0,16}?号"
    ),
    "pii.licence": re.compile(
        r"[\u4e00-\u9fa5]{2,20}(?:证书|资格证书|资质证书|执照)\s*第?\s*[A-Za-z0-9\-—]{1,20}\s*号"
    ),
    # 单位名：后缀锚定。与脱敏侧 ORG_SUFFIXES 不同的一份后缀表（交叉验证）。
    "org.name": re.compile(
        r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,24}?"
        r"(?:设计院|设计研究院|研究院|勘测设计院|市政设计院|设计有限公司|工程有限公司"
        r"|环保工程有限公司|科技有限公司|有限责任公司|股份有限公司|设计事务所)"
    ),
}

#: 已知真实人名（来自本批样本实测）。**只对本批样本有意义**，换批样本须重录。
KNOWN_PERSON_NAMES: tuple[str, ...] = ("杨兆华", "汪华兵")

#: 表头变量：值非空且非全零 GUID 即视为残留（脱敏应把它们清空）。
HEADER_VARS: tuple[str, ...] = (
    "$LASTSAVEDBY",
    "$PROJECTNAME",
    "$HYPERLINKBASE",
    "$FINGERPRINTGUID",
    "$VERSIONGUID",
)

#: 脱敏后表头变量的**合法残留值**：全零 GUID 表示「已清空但保留格式」。
BENIGN_HEADER_VALUES: tuple[str, ...] = (
    "{00000000-0000-0000-0000-000000000000}",
)

#: 厂商样板特征词。**必须配合 ``section == "CLASSES"`` 使用**，单看特征词会误放行。
VENDOR_MARKERS: tuple[str, ...] = (
    "autodesk",
    "expresstools",
    "tangent.com.cn",
    "天正",
    "AcDbDimAssoc",
)

#: 厂商样板允许出现在哪个节。CLASSES 节定义代理实体类，改写有破坏解析的风险。
VENDOR_SECTIONS: frozenset[str] = frozenset({"CLASSES"})


class Hit(NamedTuple):
    """一处命中：值、规则、所在节、样例。"""

    value: str
    rule: str
    section: str
    sample: str

    @property
    def is_vendor(self) -> bool:
        """厂商样板：所在节是 CLASSES **且** 值含厂商特征词。两个条件都要满足。"""
        if self.section not in VENDOR_SECTIONS:
            return False
        low = self.value.lower()
        return any(m.lower() in low for m in VENDOR_MARKERS)


def stream_values(path: Path) -> list[tuple[int, str]]:
    """按 DXF 组码流取 ``(组码, 值)``。偶数行组码、奇数行值；不做对象树重建。

    用 ``latin-1`` 读，保证任意字节都能 1:1 映射成字符而不抛异常 —— 中文的三种
    编码形态都要能原样取出，交给 ``candidate_decodings`` 处理。
    """
    raw = path.read_bytes().decode("latin-1")
    lines = raw.replace("\r\n", "\n").split("\n")
    out: list[tuple[int, str]] = []
    i = 0
    while i + 1 < len(lines):
        code = lines[i].strip()
        if code.lstrip("-").isdigit():
            out.append((int(code), lines[i + 1]))
            i += 2
        else:
            i += 1
    return out


def candidate_decodings(value: str) -> list[str]:
    """把单个组码值展开成**所有可能的解码结果**，逐个扫描。

    1. MIF 转义（``\\M+5XXXX``）→ ``mif_decode``
    2. 明文 UTF-8 字节被 ``latin-1`` 读成乱码 → ``encode('latin-1').decode('utf-8')``
    3. GBK/GB18030 字节被 ``latin-1`` 读成乱码 → 同上换编码

    **踩坑记录（2026-09-15）**：本脚本第一版只做 ``mif_decode``，于是 p03.dxf 里
    以**明文 UTF-8 字节**存放的 ``杨兆华``/``汪华兵`` 完全扫不出来 —— 按 ``latin-1``
    读进来是乱码，``mif_decode`` 又不负责这种形态，``pii.person`` 恒为 0。
    当时脱敏模块其实**已经**把它们删掉了（``person.explicit×3``），是扫描器自己瞎。
    若不复核就报「验收通过」，等于用一个看不见目标的扫描器去证明目标不存在。

    宁可多试几种（多试只会多报，不会漏报），扫不到才是真问题。
    """
    out = [mif_decode(value)]
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            alt = value.encode("latin-1").decode(enc)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if alt not in out:
            out.append(alt)
    return out


#: 只扫文本值：1 = 字符串, 3 = MTEXT 分块。
_TEXT_CODES: tuple[int, ...] = (1, 3)


def transport_decoded(value: str) -> str:
    """把组码值从"latin-1 运输形态"还原成**读者会看到的文本**（最可能的那个）。

    与 `candidate_decodings` 的区别：那个给"所有可能"，这个给"最可能的那一个"。
    **检测类判据必须用后者。**

    为什么（2026-09-15 踩坑）：`stream_values` 用 latin-1 读文件以保证字节无损，
    于是**明文 UTF-8 中文本身**长这样 —— ``单位`` → ``å\\x8d\\x95ä½\\x8d``。
    这串里 ``ä½`` 就是两个连续 Latin-1 字符，**天然满足 `looks_like_mojibake`
    的形态门槛**。第一版把检测跑在 `any()` 所有候选上，结果 18 份样本报出
    **361 处**"疑似损坏"，而真实损坏只有 P-01 的 58 处 —— 全是明文中文的误报。

    **教训**：判据的输入口径要和"读者实际看到的文本"对齐。对 PII 判据
    （`candidate_decodings` 全试）宁多勿少是对的，因为多试只会多报；但对
    **形态类判据**，多试会把运输形态本身当成损坏特征，噪声直接淹没结论。
    """
    for enc in ("utf-8", "gbk", "gb18030"):
        try:
            return value.encode("latin-1").decode(enc)
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
    return value


def scan_file(path: Path) -> tuple[list[Hit], dict[str, str], list[str]]:
    """扫描单份 DXF，返回 ``(命中列表, 表头残留, 无法判定的文本)``。

    第三项是**编码层盲区**：文本疑似 CP1252 重编码损坏时，任何基于中文/ASCII
    的判据都看不见它 —— 里面可能藏着 PII（实测 P-01 的图签栏就是）。这种串
    **不能算通过**，也不能"还原"（不可逆，见 `looks_like_mojibake`），只能报出来。
    """
    hits: list[Hit] = []
    header_left: dict[str, str] = {}
    undecodable: list[str] = []
    section = ""
    expect_section_name = False
    pending_header: str | None = None

    for code, value in stream_values(path):
        # 跟踪所在节：0/SECTION -> 2/<节名> -> … -> 0/ENDSEC
        if code == 0:
            name = value.strip()
            if name == "SECTION":
                expect_section_name = True
            elif name == "ENDSEC":
                section = ""
                expect_section_name = False
            continue
        if expect_section_name and code == 2:
            section = value.strip()
            expect_section_name = False
            continue

        # 表头变量：组码 9 是变量名，其后的组码 1/2/3 是值
        if code == 9 and value.strip() in HEADER_VARS:
            pending_header = value.strip()
            continue
        if pending_header is not None:
            if code in _TEXT_CODES and value.strip() and value.strip() not in BENIGN_HEADER_VALUES:
                header_left[pending_header] = value.strip()[:60]
            pending_header = None

        if code not in _TEXT_CODES:
            continue

        candidates = candidate_decodings(value)
        for rule, rx in CHECKS.items():
            # 同一个值在多种解码下可能都命中（ASCII 的邮箱在 MIF 形态与 latin-1 乱码
            # 形态里都看得见）。取**各候选中的最大命中数**，而不是把候选逐个累加 ——
            # 累加会把一处邮箱记成 2–3 处，报告虚高同样不可信。
            best = 0
            best_sample = ""
            for decoded in candidates:
                found = rx.findall(decoded)
                if len(found) > best:
                    best = len(found)
                    best_sample = found[0][:60]
            for _ in range(best):
                hits.append(Hit(value=value, rule=rule, section=section, sample=best_sample))

        if any(name in d for d in candidates for name in KNOWN_PERSON_NAMES):
            found_name = next(n for d in candidates for n in KNOWN_PERSON_NAMES if n in d)
            hits.append(Hit(value=value, rule="pii.person", section=section, sample=found_name))

        # 形态判据只喂"读者会看到的那一个"解码结果，不喂全部候选 ——
        # 否则 latin-1 运输形态本身就会被当成损坏特征（见 `transport_decoded`）。
        shown = transport_decoded(value)
        if looks_like_mojibake(shown) or looks_like_mojibake(mif_decode(shown)):
            undecodable.append(mif_decode(shown)[:60])

    return hits, header_left, undecodable


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PII 泄漏复扫（组码流口径）")
    ap.add_argument("root", nargs="?", default="data/drawings/real_samples",
                    help="待扫目录（含 .dxf）")
    ap.add_argument("--report", default=None, help="JSON 报告输出路径")
    ap.add_argument("--expect-hits", action="store_true",
                    help="阴性对照模式：期望**扫出**命中（用于验证判据本身有效）")
    ap.add_argument("--tolerate-undecodable", action="store_true",
                    help="把「无法判定的文本」从阻断降为警告。仅在已知并已记录该缺口时使用，"
                         "不得用来把验收结论包装成通过。")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"前置条件缺失：目录不存在 {root}", file=sys.stderr)
        return 2
    files = sorted(root.glob("*.dxf"))
    if not files:
        print(f"前置条件缺失：目录内没有 .dxf {root}", file=sys.stderr)
        return 2

    leak_total: Counter[str] = Counter()
    vendor_total: Counter[str] = Counter()
    per_file: list[dict[str, object]] = []
    n_values = 0
    undecodable_total = 0

    for fp in files:
        hits, header_left, undecodable = scan_file(fp)
        n_values += sum(1 for c, _ in stream_values(fp) if c in _TEXT_CODES)
        leaks = [h for h in hits if not h.is_vendor]
        vendors = [h for h in hits if h.is_vendor]
        leak_rules = Counter(h.rule for h in leaks)
        vendor_rules = Counter(h.rule for h in vendors)
        leak_total.update(leak_rules)
        vendor_total.update(vendor_rules)
        undecodable_total += len(undecodable)

        samples: dict[str, list[str]] = {}
        for h in leaks:
            samples.setdefault(h.rule, [])
            if len(samples[h.rule]) < 3:
                samples[h.rule].append(h.sample)

        per_file.append({
            "file": fp.name,
            "leaks": dict(leak_rules),
            "leak_samples": samples,
            "vendor_boilerplate": dict(vendor_rules),
            "header_left": header_left,
            "undecodable_count": len(undecodable),
            "undecodable_samples": undecodable[:3],
            "clean": not leaks and not header_left,
        })

    n_clean = sum(1 for r in per_file if r["clean"])
    print(f"扫描目录 : {root}")
    print(f"文件数   : {len(files)}（文本值 {n_values} 个，组码流口径）")
    print(f"干净文件 : {n_clean}/{len(files)}\n")

    if any(not r["clean"] for r in per_file):
        print("--- 命中明细（仅列非干净文件）---")
        for r in per_file:
            if r["clean"]:
                continue
            print(f"  [{r['file']}]")
            for rule, n in sorted((r["leaks"] or {}).items()):  # type: ignore[union-attr]
                print(f"     {rule:<14} {n:>3} 处   例: {r['leak_samples'][rule]}")  # type: ignore[index]
            if r["header_left"]:
                print(f"     header 残留    {r['header_left']}")
        print("\n--- 泄漏按规则合计 ---")
        for rule, n in leak_total.most_common():
            print(f"  {rule:<14} {n:>4}")
    else:
        print("未发现泄漏。")

    if vendor_total:
        print("\n--- 已判定非用户 PII：厂商样板（CLASSES 节代理类注册记录）---")
        for rule, n in vendor_total.most_common():
            print(f"  {rule:<14} {n:>4}")
        print("  理由：这些值位于 CLASSES 节，由 CAD 软件自身写入，描述的是**代理实体类**"
              "（Autodesk / 天正 等厂商信息），不含用户可识别信息；")
        print("        且改写该节描述串有破坏代理实体解析的风险。**不做静默忽略** —— "
              "单列在此供人工复核。")

    if undecodable_total:
        print("\n--- ⚠ 无法判定的文本：疑似 CP1252 重编码损坏 ---")
        for r in per_file:
            if r["undecodable_count"]:
                print(f"  [{r['file']}]  {r['undecodable_count']} 处   例: "
                      f"{r['undecodable_samples']}")
        print(f"  合计 {undecodable_total} 处。")
        print("  含义：这些串的字节先被当成 CP1252 单字节解、回写时又经 GB2312 最佳拟合，")
        print("        **该变换多对一、不可逆**（CP1252 的大小写对如 À/à、Í/í 坍缩成同一字符），")
        print("        所以既不能还原（硬解会得到「沉淀池」→「翅雾池」这类**貌似通顺的假中文**，")
        print("        比乱码更危险），也不能靠中文/ASCII 判据看出里面有没有 PII。")
        print("  ⇒ 本扫描器**无法为这些文本出具「无 PII」结论**。"
              "处置见 data/drawings/real_samples/MANIFEST.md 已知限制 #5。")

    if args.report:
        rp = Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(
            json.dumps({
                "root": str(root),
                "files": len(files),
                "text_values": n_values,
                "clean_files": n_clean,
                "leak_totals": dict(leak_total),
                "vendor_boilerplate_totals": dict(vendor_total),
                "undecodable_total": undecodable_total,
                "per_file": per_file,
            }, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"\n报告: {rp}")

    dirty = bool(leak_total) or any(r["header_left"] for r in per_file)
    if args.expect_hits:
        if dirty:
            print("\n阴性对照通过：判据有效（在预期有命中的目录上确实扫出了命中）。")
            return 0
        print("\n!! 阴性对照失败：在预期有命中的目录上扫出 0 —— 判据已失效，"
              "此时「脱敏后 0 命中」不构成任何证据。", file=sys.stderr)
        return 1
    if dirty:
        print("\n!! 发现 PII 残留，验收不通过。", file=sys.stderr)
        return 1
    if undecodable_total and not args.tolerate_undecodable:
        print(f"\n!! 有 {undecodable_total} 处文本**无法判定**（疑似重编码损坏）："
              "无法判定 ≠ 通过，验收不通过。", file=sys.stderr)
        print("   要么把这些文本从样本里去掉，要么确认后加 --tolerate-undecodable 放行"
              "（放行即表示已知并接受这个缺口，报告里仍会单列）。", file=sys.stderr)
        return 1
    if undecodable_total:
        print(f"\n警告：{undecodable_total} 处文本无法判定，已按 --tolerate-undecodable 放行 —— "
              "**这不算「无 PII」的证据**。")
    print("\n验收通过：未发现用户 PII 残留。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
