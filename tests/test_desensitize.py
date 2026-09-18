"""P2 Step 3：DXF 脱敏模块的正反例。

本文件的断言**全部来自实测踩到的坑**，不是设计意图的复述：

1. ``test_mif_roundtrip_*`` —— 最初 `mif_encode` 按**字节**而不是**字节对**加
   ``\\M+5`` 前缀，``专``（GBK ``D7A8``）被写成 ``\\M+5D7\\M+5A8``，
   **整份图纸中文全部解不出来**。文件仍能被解析器打开，只在读文本时表现为乱码 ——
   属于静默损坏，必须用往返测试钉死。
2. ``test_person_anchor_*`` —— 人名锚定规则最初把分隔符设为可选，实测 18 份真实
   样本命中 10 种「人名」**全部是技术词**（``设计阶段``→``阶段``、``设计项目``→
   ``项目``、``星欣设计图库``→``图库``），假阳性率 100%、真阳性 0。要求显式分隔符
   后修复，这里锁住「必须带分隔符」与「技术词不入表」两条。
3. ``test_technical_content_untouched`` —— 脱敏的边界是**只动字符串值**，
   坐标/图层/几何必须零改动，否则缺陷定位会失效。
4. ``test_address_*`` / ``test_licence_runs_before_address`` —— 地址与证号规则的第一版
   **注释里写了案例但正则没覆盖那个案例**：``_ADDRESS_RE`` 要求「省/市」字样，而 p05 的
   真实地址是直辖市写法「北京朝阳区…」（无「市」字），整条规则空转，地址原样留在归档样本；
   同时证号串被地址规则抢先截走，``pii.licence`` 恒为 0。**这两件事都不报错、不影响任何
   其它指标**，是 Step 5 拿原始字节逐条核对时才发现的。
   教训：注释里写了某个案例 ≠ 正则覆盖了那个案例；每条规则都要拿**真实字符串**跑一遍。
"""
from __future__ import annotations

from water_agent.drawing.desensitize import (
    RULE_META_GUID,
    RULE_META_LAST_SAVED_BY,
    RULE_ORG_EXPLICIT,
    RULE_ORG_KEYWORD,
    RULE_PERSON_ANCHORED,
    RULE_PHONE,
    RULE_STRUCT_THUMBNAIL,
    DesensitizeConfig,
    desensitize_dxf_text,
    mif_decode,
    mif_encode,
    mif_encoded_chars,
)

# ---------------------------------------------------------------------------
# 1. MIF 编解码：字节对，不是字节
# ---------------------------------------------------------------------------


def test_mif_encode_uses_byte_pairs() -> None:
    """一个 ``\\M+5`` 前缀对应一个 **2 字节**单元。"""
    assert mif_encode("专") == r"\M+5D7A8"
    assert mif_encode("一道，面漆") == r"\M+5D2BB\M+5B5C0\M+5A3AC\M+5C3E6\M+5C6E1"
    # ASCII 原样通过 —— MTEXT 格式码 ``\A1;`` 不能被编码破坏
    assert mif_encode(r"\A1;45") == r"\A1;45"


def test_mif_roundtrip_various() -> None:
    """往返恒等，含四字节 GB18030 字符与工程符号。"""
    for original in ("专", "一道，面漆", "°", "施工图", "污水厂平面图",
                     "上海环境节能工程有限公司", "ABC123", "㎡³±", "𠀋"):
        assert mif_decode(mif_encode(original)) == original, original


def test_mif_decode_batches_consecutive_units() -> None:
    """四字节字符由两个连续单元组成，必须合并后再解码。

    ezdxf 1.4.4 的 `decode_mif_to_unicode` 逐单元独立解码，此例会失败 ——
    这是本模块保留自有实现的依据。
    """
    assert mif_decode(r"\M+59532\M+58337") == "𠀋"


def test_mif_decode_preserves_unknown_codepage() -> None:
    """未知码页原样保留，不猜。"""
    assert mif_decode(r"\M+7ABCD") == r"\M+7ABCD"
    assert mif_decode("plain ascii") == "plain ascii"


def test_mixed_encoding_not_rewritten() -> None:
    """同一串里 MIF 与 Latin-1 伪编码混存时，**只还原 MIF 那部分**。

    实测 p01 的文本长这样：``¹øÂ\\M+5A1A5\\M+5A1A4¿`` —— 既有 MIF 转义，又有
    Latin-1 伪编码字节（``¹øÂ`` 还原是 GBK 字节）。第一版按「整串含 MIF 就全部
    重新编码」处理，伪编码字符被改写成 MIF 转义，**编码形态被无谓改变**，
    下游的 Latin-1 还原路径随之失效。修复后逐字符判定。
    """
    src = _text_entity("¹øÂ" + r"\M+5A1A5\M+5A1A4" + "¿")
    out, _ = desensitize_dxf_text(src)
    # 伪编码部分必须原样保留（不转成 \M+5）
    assert "¹øÂ" in out
    assert "¿" in out
    # MIF 部分保持不变
    assert r"\M+5A1A5\M+5A1A4" in out
    # mif_encoded_chars 只收 MIF 解出的字符，伪编码字符不得入集
    chars = mif_encoded_chars("¹øÂ" + r"\M+5A1A5")
    assert chars == set(mif_decode(r"\M+5A1A5"))
    assert not (chars & {"¹", "ø", "Â"})


def test_plain_text_stays_plain() -> None:
    """本来就是明文的非 ASCII 文本，脱敏后仍是明文，不转 MIF。"""
    src = _text_entity("上海环境节能工程有限公司")
    out, _ = desensitize_dxf_text(src)
    assert "\\M+" not in out
    assert "[单位-" in out


# ---------------------------------------------------------------------------
# 2. 人名锚定：分隔符强制 + 技术词守卫
# ---------------------------------------------------------------------------


def _text_entity(value: str) -> str:
    """包一份最小可解析 DXF，实体里放一个 TEXT。"""
    return (
        "  0\r\nSECTION\r\n  2\r\nENTITIES\r\n"
        "  0\r\nTEXT\r\n  5\r\n1\r\n  1\r\n" + value + "\r\n"
        "  0\r\nENDSEC\r\n  0\r\nEOF\r\n"
    )


def test_person_anchor_requires_separator() -> None:
    """``设计：张三`` 命中；``设计阶段`` 这类无分隔符的技术词**不得**命中。"""
    _, hit = desensitize_dxf_text(_text_entity(mif_encode("设计：张三")))
    assert hit.hits.get(RULE_PERSON_ANCHORED) == 1

    _, miss = desensitize_dxf_text(_text_entity(mif_encode("设计阶段")))
    assert RULE_PERSON_ANCHORED not in miss.hits


def test_person_anchor_rejects_technical_words() -> None:
    """实测踩坑的那批词，即使带分隔符也不得被当成人名。"""
    for word in ("阶段", "项目", "专业", "图库", "题目", "证号", "负责", "依据"):
        _, rep = desensitize_dxf_text(_text_entity(mif_encode(f"设计：{word}")))
        assert RULE_PERSON_ANCHORED not in rep.hits, word


# ---------------------------------------------------------------------------
# 3. 确定性 PII 与元数据
# ---------------------------------------------------------------------------


def test_pii_and_metadata_scrubbed() -> None:
    src = (
        "  0\r\nSECTION\r\n  2\r\nHEADER\r\n"
        "  9\r\n$LASTSAVEDBY\r\n  1\r\nyaerken\r\n"
        "  9\r\n$FINGERPRINTGUID\r\n  2\r\n{AAAA-BBBB}\r\n"
        "  0\r\nENDSEC\r\n"
        "  0\r\nSECTION\r\n  2\r\nENTITIES\r\n"
        "  0\r\nTEXT\r\n  5\r\n1\r\n  1\r\n"
        + mif_encode("联系 13111542600 或 a@b.com")
        + "\r\n  0\r\nENDSEC\r\n  0\r\nEOF\r\n"
    )
    out, rep = desensitize_dxf_text(src)
    assert rep.hits[RULE_PHONE] == 1
    assert rep.hits[RULE_META_LAST_SAVED_BY] == 1
    assert rep.hits[RULE_META_GUID] == 1
    assert "yaerken" not in out
    assert "13111542600" not in mif_decode(out)
    assert "a@b.com" not in mif_decode(out)
    assert "$LASTSAVEDBY" in rep.header_vars_scrubbed


def test_thumbnail_section_dropped() -> None:
    src = (
        "  0\r\nSECTION\r\n  2\r\nENTITIES\r\n  0\r\nENDSEC\r\n"
        "  0\r\nSECTION\r\n  2\r\nTHUMBNAILIMAGE\r\n  90\r\n4\r\n  310\r\nDEADBEEF\r\n  0\r\nENDSEC\r\n"
        "  0\r\nEOF\r\n"
    )
    out, rep = desensitize_dxf_text(src)
    assert rep.thumbnail_dropped is True
    assert rep.hits[RULE_STRUCT_THUMBNAIL] == 1
    assert "THUMBNAILIMAGE" not in out
    assert "DEADBEEF" not in out


# ---------------------------------------------------------------------------
# 4. 显式替换表与单位名
# ---------------------------------------------------------------------------


def test_explicit_terms_take_priority() -> None:
    cfg = DesensitizeConfig(org_terms=("某某环保工程有限公司",))
    _, rep = desensitize_dxf_text(
        _text_entity(mif_encode("某某环保工程有限公司")), cfg
    )
    assert rep.hits.get(RULE_ORG_EXPLICIT) == 1
    # 显式表命中后不再走关键词规则，避免双重替换
    assert RULE_ORG_KEYWORD not in rep.hits


def test_org_keyword_suffix_anchor() -> None:
    _, rep = desensitize_dxf_text(_text_entity(mif_encode("北京建工金源环保工程有限公司")))
    assert rep.hits.get(RULE_ORG_KEYWORD) == 1


def test_pseudonym_stable_within_run() -> None:
    """同一原文在同一次运行内恒得同一伪名（跨文件可追溯）。"""
    cfg = DesensitizeConfig()
    _, r1 = desensitize_dxf_text(_text_entity(mif_encode("甲设计院")), cfg, file_key="a")
    _, r2 = desensitize_dxf_text(_text_entity(mif_encode("甲设计院")), cfg, file_key="b")
    assert r1.pseudonyms == r2.pseudonyms


# ---------------------------------------------------------------------------
# 5. 技术内容零改动（脱敏的边界）
# ---------------------------------------------------------------------------


def test_technical_content_untouched() -> None:
    """坐标、图层、几何组码一律不得被改写。

    组码 ``8``（图层名）不在白名单里，无论明文还是 MIF 形态都必须原样保留 ——
    图层名是给排水系统归属判定的依据（`dxf_reader._layer_system`），改了会改变审查结论。
    """
    src = (
        "  0\r\nSECTION\r\n  2\r\nENTITIES\r\n"
        "  0\r\nLINE\r\n  5\r\n2F\r\n  8\r\n给水管\r\n"
        " 10\r\n-3848.031497023709\r\n 20\r\n-7393.527513270681\r\n"
        " 11\r\n19912.14182223065\r\n 21\r\n9413.5461709398\r\n"
        "  0\r\nLINE\r\n  5\r\n30\r\n  8\r\n" + mif_encode("污水管") + "\r\n"
        " 10\r\n0.0\r\n 20\r\n0.0\r\n"
        "  0\r\nENDSEC\r\n  0\r\nEOF\r\n"
    )
    out, rep = desensitize_dxf_text(src)
    # 坐标原样保留
    assert "-3848.031497023709" in out
    assert "-7393.527513270681" in out
    assert "19912.14182223065" in out
    # 图层名原样保留（明文与 MIF 两种形态）
    assert "给水管" in out
    assert mif_encode("污水管") in out
    assert rep.total_hits == 0


def test_honest_coverage_notes_present() -> None:
    """报告必须如实交代未覆盖边界，不得暗示「已完全脱敏」。"""
    _, rep = desensitize_dxf_text(_text_entity(mif_encode("张伟")))
    assert rep.coverage_notes
    assert any("人名" in n for n in rep.coverage_notes)


# ---------------------------------------------------------------------------
# 7. 地址与证号：**Step 5 复查抓出的真实泄漏**
# ---------------------------------------------------------------------------
# 这两条规则的第一版都「看起来对了但没生效」，而且**不报错、不影响任何其它指标**，
# 只在拿原始字节逐条核对时才暴露。下面每个字符串都是真实样本里的原文。


def test_address_municipality_without_shi() -> None:
    """直辖市写法「北京朝阳区…」没有「市」字，必须命中。

    第一版 `_ADDRESS_RE` 要求 ``(?:省|自治区|市)``，对 p05 的真实地址
    ``中国·北京朝阳区安慧北里安园甲8号`` **完全空转** —— 地址原样留在归档样本里，
    而 Step 5 的复扫也没抓到（扫描正则同样要求「市」字），两边一起漏，
    于是「0 命中」这个结论是假的。
    """
    from water_agent.drawing.desensitize import _ADDRESS_RE, RULE_ADDRESS

    for raw in (
        "中国·北京朝阳区安慧北里安园甲8号",
        "北京朝阳区安慧北里安园甲8号",
        "上海市浦东新区世纪大道100号",
        "广东省深圳市南山区科技园",
        "深圳市南山区科技园",
    ):
        assert _ADDRESS_RE.search(raw), f"未命中: {raw}"

    src = _text_entity(mif_encode("中国·北京朝阳区安慧北里安园甲8号"))
    out, rep = desensitize_dxf_text(src)
    assert rep.hits.get(RULE_ADDRESS) == 1
    assert "安慧北里" not in mif_decode(out)
    assert "朝阳区" not in mif_decode(out)


def test_address_does_not_over_match_technical_text() -> None:
    """地址规则不得误伤图内索引/管段编号 —— 实测全量 9038 个文本单元零误伤。"""
    from water_agent.drawing.desensitize import _ADDRESS_RE

    for raw in (
        "接Rww-B1001号下水管",
        "1号泵房",
        "A区/B路",
        "DN300 焊接钢管",
        "标高 12.500",
        "深圳天马微电子股份有限公司",  # 单位名，归 _ORG_RE 管
    ):
        assert not _ADDRESS_RE.search(raw), f"误伤: {raw}"


def test_licence_runs_before_address() -> None:
    """证号串必须归到 ``pii.licence``，不能被地址规则截走。

    实测 d01 的 ``深圳市环境保护局颁环保工程技术资格证书001号`` 同时满足两条规则；
    先跑地址会把 PII 删掉但**记成 pii.address**，报告口径失实（读者会以为命中的是地址）。
    规则顺序是契约，所以这里直接锁住。
    """
    from water_agent.drawing.desensitize import RULE_ADDRESS, RULE_LICENCE

    src = _text_entity(mif_encode("深圳市环境保护局颁环保工程技术资格证书001号 甲级"))
    out, rep = desensitize_dxf_text(src)
    assert rep.hits.get(RULE_LICENCE) == 1
    assert RULE_ADDRESS not in rep.hits
    assert "资格证书001号" not in mif_decode(out)
    # 证号被删，同串里的技术后缀要留下
    assert "甲级" in mif_decode(out)
