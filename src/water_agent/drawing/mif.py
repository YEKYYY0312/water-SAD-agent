"""AutoCAD MIF 转义编解码（``\\M+<codepage><4位十六进制>``）。

为什么单独一个模块
------------------
MIF 转义是 **DXF 的编码层**，不是脱敏的概念。读图（`dxf_reader`）要解码它、
脱敏（`desensitize`）要解码后再回写原形态、评测要统计它的占比 —— 三处都需要，
而读图模块**不应该反向依赖脱敏模块**（依赖方向会是"读文件"→"改文件"，
语义倒置，且脱敏模块迟早要引入替换表等与读图无关的重依赖）。

实测：真实图纸里 3,387 条文本图元有 **1,239 条（37%）** 是这种形态。
不解码就等于把 ``\\M+5D0C2\\M+5BDA8…`` 而不是「新建建(构)筑物」送进模型。

三种中文编码形态（**必须区分，混为一谈必然漏**）
------------------------------------------------
1. **MIF 转义** —— ``\\M+5D0C2\\M+5BDA8``，本模块处理。
   一个 ``\\M+5`` 前缀对应**一个 2 字节单元**，不是一字节一个。
2. **明文 UTF-8** —— 直接是可读中文，本模块原样返回。
3. **CP1252 重编码损坏**（旧称"Latin-1 伪编码"）—— 如 ``¹øÂˉ·¿`` 本应是「锅炉房」。
   **本模块只检测、不还原**（`looks_like_mojibake`）—— 该形态**不可逆**，
   硬解会得到"貌似通顺、实则错误"的假中文。完整论证见 `looks_like_mojibake`。
"""
from __future__ import annotations

import re
from collections.abc import Collection

#: 只处理 ``\M+5``（GBK/ANSI_936，本批样本全部使用）；其他码页原样保留 ——
#: 宁可留着不动，也不猜错码页把文本改坏。
_MIF_RE = re.compile(r"\\M\+(\d)([0-9A-Fa-f]{4})")


def mif_decode(text: str) -> str:
    """``\\M+5XXXX`` → 真实字符。未知码页与坏字节原样保留。

    **连续单元要合并后再解码**：一个 ``\\M+5`` 单元是 2 字节，而 GB18030 的四字节
    字符会拆成两个连续单元。逐单元单独解码会把这类字符解成两个乱码，因此这里把
    相邻单元先拼成字节串再统一解码。

    **为什么不直接用 ezdxf 的 `decode_mif_to_unicode`**：ezdxf 1.4.4 的
    ``lldxf/encoding.py`` 里确实有同名功能，但它把每个 ``\\M+5XXXX`` 单元**独立**
    解码，四字节字符（如 ``𠀋`` = ``\\M+59532\\M+58337``）会解码失败并原样返回。
    已实测：18 份样本 3409 条文本值上两者结果**完全一致**（无一差异），说明保留本
    实现不会引入行为差异，而在四字节字符上本实现更正确。

    **幂等**：对已解码的文本再次调用是安全的（无 ``\\M+`` 即原样返回）。
    这一点对 `dxf_reader` 很重要 —— 它可能在多处被调用，重复解码不得改变结果。
    """
    if "\\M+" not in text:
        return text

    out: list[str] = []
    pending = bytearray()
    pos = 0

    def flush() -> None:
        if not pending:
            return
        raw = bytes(pending)
        for enc in ("gbk", "gb18030"):
            try:
                out.append(raw.decode(enc))
                break
            except UnicodeDecodeError:
                continue
        else:
            out.append(raw.decode("gbk", errors="replace"))
        pending.clear()

    for m in _MIF_RE.finditer(text):
        if m.group(1) != "5":
            flush()
            out.append(text[pos:m.start()])
            out.append(m.group(0))
            pos = m.end()
            continue
        gap = text[pos:m.start()]
        if gap:
            flush()
            out.append(gap)
        pending.extend(bytes.fromhex(m.group(2)))
        pos = m.end()

    flush()
    out.append(text[pos:])
    return "".join(out)


def mif_encode(text: str, *, only: Collection[str] | None = None) -> str:
    """非 ASCII 字符 → ``\\M+5XXXX``（GBK），保持样本原有的编码约定。

    **一个 ``\\M+5`` 前缀对应一个 2 字节单元，不是一字节一个。** 这是踩过坑的
    结论：最初按字节输出，``专``（GBK ``D7A8``）被写成 ``\\M+5D7\\M+5A8`` 而不是
    ``\\M+5D7A8``，结果**整份图纸的中文全部解不出来** —— 编码被破坏，但文件仍能
    被解析器打开，所以只会在读文本时表现为乱码，属于"静默损坏"。
    四字节字符（GB18030）按 2 字节切成多个单元，与 AutoCAD 的写法一致。

    ``only`` —— **只对集合内的字符做 MIF 编码，其余非 ASCII 字符原样输出**。
    这是第二个坑的修复：实测 p01 的文本值是**混合形态**，同一串里既有 MIF 转义
    又有 Latin-1 伪编码字节（``¹øÂ\\M+5A1A5\\M+5A1A4¿``）。若按"整串含 MIF 就全部
    重新编码"处理，伪编码字符会被改写成 MIF 转义，**编码形态被无谓改变**，
    下游的 Latin-1 还原路径随之失效。传 ``only=`` 后逐字符判定，只还原本来
    就是 MIF 的那部分。``None`` 表示全部编码（构造新串时用）。

    ASCII 原样输出，因此 MTEXT 的 ``\\A1;`` / ``{\\fSimSun|...;}`` 等格式码不受影响。
    """
    if text.isascii():
        return text
    out: list[str] = []
    for ch in text:
        if ch.isascii():
            out.append(ch)
            continue
        if only is not None and ch not in only:
            out.append(ch)
            continue
        try:
            raw = ch.encode("gbk")
        except UnicodeEncodeError:
            raw = ch.encode("gb18030", errors="replace")
        if len(raw) % 2:
            raw += b"\x00"
        for k in range(0, len(raw), 2):
            out.append("\\M+5" + raw[k:k + 2].hex().upper())
    return "".join(out)


def mif_encoded_chars(text: str) -> set[str]:
    """返回 ``text`` 中**由 MIF 单元解出的**字符集合。

    用于 `mif_encode(only=...)`：只还原本来就是 MIF 的那部分字符。
    连续单元合并后解码，与 `mif_decode` 同一套批处理逻辑（四字节字符需要）。
    """
    if "\\M+" not in text:
        return set()
    chars: set[str] = set()
    pending = bytearray()

    def flush() -> None:
        if not pending:
            return
        raw = bytes(pending)
        for enc in ("gbk", "gb18030"):
            try:
                chars.update(raw.decode(enc))
                break
            except UnicodeDecodeError:
                continue
        pending.clear()

    pos = 0
    for m in _MIF_RE.finditer(text):
        if m.group(1) != "5":
            flush()
            pos = m.end()
            continue
        if text[pos:m.start()]:
            flush()
        pending.extend(bytes.fromhex(m.group(2)))
        pos = m.end()
    flush()
    return chars


#: 判定"疑似重编码损坏"所需的最短**连续** Latin-1 补充区字符数。
#:
#: 取 2 而不是 1：工程图上单个 ``°`` ``±`` ``Ø`` ``µ`` 是**正常符号**
#: （``±0.5``、``°C``、``Ø300``、``µg/L``），不该被当成乱码；而实测的损坏
#: 总是**成串**出现（``¹øÂ¯·¿``）。单字符门槛会把正常符号卷进来。
_MOJIBAKE_RUN = 2

#: Latin-1 补充区单字符（U+00A0–U+00FF）。
_LATIN1_SUPPLEMENT = re.compile(r"[\u00a0-\u00ff]")

#: 连续片段判定式。
_LATIN1_RUN = re.compile(r"[\u00a0-\u00ff]{" + str(_MOJIBAKE_RUN) + r",}")

#: 只会在重编码损坏里出现、正常工程图不会用的"标记字符"。
#:
#: 它们**不在 CP1252 里**，所以不可能是"字节 → 字符"那一步的直译结果，
#: 只能来自后面"最佳拟合"的那一步。实测来自 P-01：``￡``(U+FFE1) 9 次、
#: ``μ``(U+03BC) 5 次、``ˉ``(U+02C9) 1 次。
#:
#: **``′``(U+2032) 被排除**：它是角度的分符号（``30°15′``），收进来会误报；
#: 而实测 ``′`` 出现的两处（``°æ    ′Î``、``·ÏË®′¦àí…``）在规则 1 下已经命中，
#: 排除它不损失检出。**收不收它要按语料实测决定，不是凭直觉。**
#:
#: **单独出现也不算证据**：``μ`` 常被当成微克前缀打字。所以要求它**同时**与至少
#: 一个 Latin-1 补充区字符共现（``μà``）。
_MOJIBAKE_MARKERS = frozenset("\u02c9\u03bc\uffe1")


def looks_like_mojibake(text: str) -> bool:
    """判定文本是否**疑似 CP1252 重编码损坏**（旧称"Latin-1 伪编码"）。

    **本函数只判定、不还原。** 这不是保守，是结论 —— 该形态**不可逆**。

    形态
    ----
    原始 ``锅炉房`` → GBK 字节 ``B9 F8 C2 AF B7 BF``；按 CP1252 逐字节解释得到
    ``¹øÂ¯·¿``；回写时再经一次 **GB2312 最佳拟合**，``¯``(U+00AF) 落成 GB2312
    ``A1A5`` 的 ``ˉ``(U+02C9)，最终磁盘上是 ``¹øÂˉ·¿``。

    **为什么不可逆**（2026-09-15 实测；18 份样本中只有 P-01 命中，52/111 条文本）
    -------------------------------------------------------------------------
    最后那步"最佳拟合"是**多对一**的：GB2312 的 A8 行只有**全角小写**带音符号，
    于是 CP1252 的大小写对**坍缩成同一个字符**：

    ==========  ======  ======  ======
    字节        字符    字节    字符
    ==========  ======  ======  ======
    ``C0``      ``À``   ``E0``  ``à``
    ``C1``      ``Á``   ``E1``  ``á``
    ``C9``      ``É``   ``E9``  ``é``
    ``CD``      ``Í``   ``ED``  ``í``
    ``D3``      ``Ó``   ``F3``  ``ó``
    ``DA``      ``Ú``   ``FA``  ``ú``
    ``DC``      ``Ü``   ``FC``  ``ü``
    ==========  ======  ======  ======

    两组字节输出**完全相同**，逆推时无法区分。实测冲突（同一字符在不同串里
    必须还原成不同字节）：

    - ``í``：``沉淀池`` 要 ``ED``（淀 = ``B5ED``），``脱水间`` 要 ``CD``（脱 = ``CDD1``）
    - ``ó``：``加药泵`` 要 ``D3``（加 = ``BCD3``），``审定`` 要 ``F3``（审 = ``C9F3``）
    - ``¼``：``加药`` 要 ``BC``，``图`` 要 ``CD``

    两个候选**往往都是合法 GBK**，"解出来必须是汉字"筛不掉。用一张"最合理"的
    表硬解，会得到 ``沉淀池`` → ``翅雾池``、``加药`` → ``俭颟``、``管`` → ``裹``、
    ``设计`` → ``殍计`` —— **貌似通顺、实则错误**。这比留着乱码更危险：乱码一眼
    看得出，假中文会被当成事实引用（违反铁律 2）。

    所以本模块**只提供检测**，由调用方决定剔除样本还是人工核对。还原需要的是
    **领域知识**（知道这里该是"沉淀池"），不是算法。

    判定门槛（任一成立即算疑似）
    ---------------------------
    1. 存在 **≥2 个连续**的 Latin-1 补充区字符（``¹øÂ¯·¿``）。单个 ``°``/``±``/``Ø``
       是正常工程符号，成串才是损坏特征。
    2. 存在 `_MOJIBAKE_MARKERS` 里的标记字符，**且**至少与一个 Latin-1 补充区字符
       共现（``μà``）。单独一个 ``μ`` 不算 —— 它常被当成微克前缀打字。

    **宁可多报**：这是给人看的告警 —— 误报的代价是一次人工扫视，漏报的代价是
    **看不见的 PII 泄漏**（实测 P-01 的图签栏字段正好落在这一类里）。
    """
    if _LATIN1_RUN.search(text):
        return True
    return bool(_MOJIBAKE_MARKERS & set(text)) and bool(_LATIN1_SUPPLEMENT.search(text))
