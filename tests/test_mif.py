"""P2 Step 8：AutoCAD MIF 转义编解码的单元测试。

本文件锁的是**真实样本上踩出来的行为**，不是接口的复述：

1. ``test_decode_batches_consecutive_units`` —— 一个 ``\\M+5`` 前缀对应一个 **2 字节
   单元**，而 GB18030 的四字节字符会拆成两个连续单元。逐单元独立解码会把
   ``𠀋`` 解成两个乱码。ezdxf 1.4.4 的 `decode_mif_to_unicode` 正是逐单元解码，
   在这类字符上失败并原样返回 —— 所以这里保留自研实现。
2. ``test_decode_is_idempotent`` —— `dxf_reader` 可能在多处调用解码，
   重复解码不得改变结果，否则会静默损坏文本。
3. ``test_decode_leaves_unknown_codepage`` —— 只处理 ``\\M+5``（GBK/ANSI_936）。
   其他码页宁可原样留着，也不猜错码页把文本改坏。
4. ``test_decode_does_not_touch_plain_text`` —— 明文中文（UTF-8）与 **CP1252 重编码
   损坏**都不是本模块的职责。后者（``¹øÂˉ·¿`` 本应是「锅炉房」）不靠 ``\\M+`` 标记，
   且**不可逆**（大小写对坍缩），硬解会产出假中文，所以**刻意不还原**，
   只由 `looks_like_mojibake` 判定（见 ``TestMojibakeDetection``）。
5. ``test_encode_uses_byte_pairs`` —— 最初按**字节**而不是**字节对**加前缀，
   ``专``（GBK ``D7A8``）被写成 ``\\M+5D7\\M+5A8``，**整份图纸中文全部解不出来**；
   文件仍能被打开，只在读文本时表现为乱码，属静默损坏。
6. ``test_encode_only_preserves_other_non_ascii`` —— 实测样本里存在**混合形态**，
   同一串既有 MIF 转义又有重编码损坏字节。按"整串含 MIF 就全部重编码"会把后者
   也改写成 MIF，**编码形态被无谓改变**，下游判定路径随之失效。
"""
from __future__ import annotations

from water_agent.drawing.mif import (
    looks_like_mojibake,
    mif_decode,
    mif_encode,
    mif_encoded_chars,
)


class TestDecode:
    def test_decode_basic_gbk(self) -> None:
        assert mif_decode(r"\M+5D0C2\M+5BDA8") == "新建"

    def test_decode_batches_consecutive_units(self) -> None:
        """四字节字符拆成两个连续单元，必须合并后统一解码。"""
        assert mif_encode("𠀋") == r"\M+59532\M+58337"
        assert mif_decode(r"\M+59532\M+58337") == "𠀋"
        # 逐单元独立解码会得到两个乱码 —— 这就是不用 ezdxf 实现的原因
        assert mif_decode(r"\M+59532") + mif_decode(r"\M+58337") != "𠀋"

    def test_decode_is_idempotent(self) -> None:
        once = mif_decode(r"\M+5D0C2\M+5BDA8")
        assert mif_decode(once) == once == "新建"

    def test_decode_leaves_unknown_codepage(self) -> None:
        assert mif_decode(r"\M+3ABCD") == r"\M+3ABCD"

    def test_decode_does_not_touch_plain_text(self) -> None:
        # 明文 UTF-8 中文：原样返回
        assert mif_decode("DN200 球墨铸铁") == "DN200 球墨铸铁"
        # Latin-1 伪编码：**刻意不处理**，猜错会改坏正常文本
        assert mif_decode("¹øÂˉ·¿") == "¹øÂˉ·¿"

    def test_decode_handles_mixed_forms(self) -> None:
        """同串内 MIF 与伪编码混存 —— 只解 MIF 部分。"""
        assert mif_decode("¹øÂ" + r"\M+5A1A5\M+5A1A4" + "¿") == "¹øÂˉ·¿"

    def test_decode_bad_bytes_does_not_raise(self) -> None:
        """坏字节替换而非抛异常 —— 一条坏标注不该让整张图读不出来。"""
        assert isinstance(mif_decode(r"\M+5FFFF"), str)

    def test_decode_empty_and_ascii(self) -> None:
        assert mif_decode("") == ""
        assert mif_decode("DN200") == "DN200"


class TestEncode:
    def test_encode_uses_byte_pairs(self) -> None:
        """一个 ``\\M+5`` 前缀对应一个 **2 字节**单元。"""
        assert mif_encode("专") == r"\M+5D7A8"

    def test_encode_roundtrip_various(self) -> None:
        for s in ("新建建(构)筑物", "管径DN200", "标高12.5", "𠀋", "给排水"):
            assert mif_decode(mif_encode(s)) == s

    def test_encode_ascii_untouched(self) -> None:
        """ASCII 原样输出，MTEXT 格式码不受影响。"""
        assert mif_encode(r"\A1;{\fSimSun|b0|i0;}DN200") == r"\A1;{\fSimSun|b0|i0;}DN200"

    def test_encode_only_preserves_other_non_ascii(self) -> None:
        """``only=`` 只编码本来就是 MIF 的那部分字符。"""
        original = "¹øÂ" + r"\M+5A1A5\M+5A1A4" + "¿"
        only = mif_encoded_chars(original)
        assert only == {"ˉ", "·"}
        # 只还原 MIF 部分：伪编码字符保持原样
        assert mif_encode("¹øÂˉ·¿", only=only) == original

    def test_mif_encoded_chars_empty_when_no_mif(self) -> None:
        assert mif_encoded_chars("给水管") == set()
        assert mif_encoded_chars("DN200") == set()


#: P-01 实测的 CP1252 重编码损坏串（**硬编码**，不依赖语料 ——
#: 样本在 .gitignore 里，测试读不到它们，依赖语料的测试会在干净克隆上失败）。
#: 每条后面是**据领域知识判断**的正确内容，仅作注释；**不参与断言** ——
#: 因为还原不可逆（见 `looks_like_mojibake`），断言"应该还原成什么"本身就是错的。
_P01_MOJIBAKE = (
    "¹øÂˉ·¿",                       # 锅炉房
    "¸ñÕ¤¾®",                       # 格栅井
    "»îÐÔÎÛÄà³Ø",                   # 活性污泥池
    "¹Ä·ç»ú·¿",                     # 鼓风机房
    "³áμí³Ø",                       # 沉淀池
    "ÎÛÄàÅ¨Ëõ³Ø1",                  # 污泥浓缩池1
    "·ÏË®′¦àíÕ¾×üÆ½Ãæ¹üÂ·²¼ÖÃí¼",   # 污水处理站总平面管路布置图
    "μ÷½ú³Ø",                       # 调节池
    "μà",                            # 只有 1 个 Latin-1 字符 + 标记字符 μ
    "Ð￡   ¶Ô",                       # 校   对（图签栏）
    "¹²    ÕÅ ±è    àý",             # 共    张 彪    勇（图签栏，含人名）
    "μú    ÕÅ ³öí¼èÕÆú",            # 制    张 出图日期（图签栏）
    "¹ü¾¶￡¨±ê¸ß￡©",                 # 管径（标高）
    "DN80￡¨0.20￡©",                 # DN80（0.20）
)

#: **正常工程文本**，一个都不许命中。这些是误报的代价：命中即告警，
#: 会把工程师推去逐条人工核对，噪声一大就没人看了。
_LEGIT_ENGINEERING = (
    "±0.5", "±0.000", "°C", "25°", "Ø300×500", "Ø25", "µg/L", "Δh=0.5",
    "DN200 球墨铸铁", "标高±0.000", "坡度i=0.003", "1×10⁴", "Φ500",
    "30°15′",          # 角度的分符号 —— 单独出现**不算**损坏（故 ′ 未入标记集）
    "5μm", "μ",        # 微克前缀常被直接打希腊 μ —— 无 Latin-1 字符时不算
    "α=90°",
    "", " ", "DN200", "P-01", "管径DN200 标高12.5", "新建建(构)筑物",
)


class TestMojibakeDetection:
    """`looks_like_mojibake` —— **只判定，不还原**。

    为什么没有 `recover_*`：该变换多对一、不可逆。CP1252 的大小写对
    （``À``/``à`` = C0/E0、``Í``/``í`` = CD/ED、``Ó``/``ó`` = D3/F3）经 GB2312
    最佳拟合后输出**同一个字符**，逆推时无法区分，且两个候选往往都是合法 GBK。
    硬解会产出「沉淀池」→「翅雾池」这类**貌似通顺的假中文** —— 比乱码更危险。
    所以本模块只提供检测，还原交给领域知识。详见 `looks_like_mojibake` 的 docstring。
    """

    def test_detects_real_sample_forms(self) -> None:
        missed = [s for s in _P01_MOJIBAKE if not looks_like_mojibake(s)]
        assert not missed, f"漏检: {missed}"

    def test_does_not_flag_legitimate_engineering_text(self) -> None:
        flagged = [s for s in _LEGIT_ENGINEERING if looks_like_mojibake(s)]
        assert not flagged, f"误报: {flagged}"

    def test_angle_prime_alone_is_not_flagged(self) -> None:
        """``′`` 是角度的分符号，刻意不收进标记集；收进来会误报 ``30°15′``。"""
        assert not looks_like_mojibake("30°15′")
        # 但它在损坏串里出现时，那些串本来就被规则 1（连续 Latin-1）命中
        assert looks_like_mojibake("°æ    ′Î")          # 版    次

    def test_marker_needs_latin1_companion(self) -> None:
        """``μ`` 单独出现不算 —— 它常被当成微克前缀打字。"""
        assert not looks_like_mojibake("μ")
        assert not looks_like_mojibake("5μm")
        assert looks_like_mojibake("μà")               # μ + Latin-1 字符共现

    def test_detects_mixed_form_with_mif_escapes(self) -> None:
        """实测样本是**混合形态**：同一串既有 MIF 转义又有损坏字节。

        检测对两种形态都成立 —— 未解码时前缀 ``¹øÂ`` 已构成连续片段，
        解码后 ``¹øÂˉ·¿`` 同样命中。扫描器对**所有解码候选**都跑判据，
        所以不依赖"先解码还是先检测"的顺序。
        """
        raw = "¹øÂ" + r"\M+5A1A5\M+5A1A4" + "¿"
        assert looks_like_mojibake(raw)
        assert looks_like_mojibake(mif_decode(raw))

    def test_single_symbol_run_of_one_is_not_flagged(self) -> None:
        """门槛是**连续 ≥2** 个 Latin-1 字符：单个 ``Ø``/``±``/``°`` 是正常符号。"""
        for s in ("Ø", "±", "°", "Ø300", "±0.5", "25°"):
            assert not looks_like_mojibake(s), s
