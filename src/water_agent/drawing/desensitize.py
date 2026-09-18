"""DXF 脱敏：移除可识别信息，保留技术内容（P2 真实样本采集 Step 3）。

为什么需要这个模块
------------------
`docs/design/13-p3-drawing-review.md` §4.6 要求送模型前对图纸标注脱敏，而
`dxf_reader._guard_text` 的运行时脱敏只覆盖**单条标注的 PII 正则**。样本集是
另一回事：它要进仓库、进评测集、可能被分享，所以必须**在落盘前**把可识别信息
清干净，而不是指望读取时的运行时护栏。

本模块的边界（**刻意不做的事**）
--------------------------------
* **不做数值改写。** 坐标、半径、图层名、几何图元一律不碰 —— 它们既是技术内容，
  也是缺陷定位的依据。脱敏只作用于**字符串值**，且只在明确的白名单组码上。
* **不猜人名。** 实测 18 份真实样本：用「2–4 个汉字」当人名特征，命中前 30 名
  是「参见 / 钢短管 / 数量 / 给排水 / 序号 / 构筑物 / 异径管 / 剖面图」——
  **几乎全是技术词汇**。任何自动人名识别都会大面积误伤技术内容，因此人名只在
  **两种高置信场景**下替换：显式替换表、或「标签+值同串」的锚定形态
  （如 ``设计：张三``）。其余候选**只进报告的待确认清单，不自动改写**。
* **不做「已完全脱敏」的声明。** 报告区分「已处理」与「未覆盖」，并把启发式候选
  原样带出。静默的过度自信比漏脱敏更危险。

DXF 的三个硬约束（实测得出，非推测）
------------------------------------
1. **中文在 DXF 里不是明文**，而是 AutoCAD 的 MIF 转义：``\\M+5D2BB`` =
   ``\\M+5`` + GBK 字节对十六进制。实测 18 份样本的中文**几乎全部**是这种形态，
   只有极少数（如 ``上海环境节能工程有限公司``）是明文。**直接对原始行跑中文正则
   等于没跑** —— 必须先解码再匹配。

2. **同一批样本里存在三种互不兼容的编码形态**，实测均出现：

   ===================  ==========================================  ==========
   形态                  样子                                         实例
   ===================  ==========================================  ==========
   MIF 转义              ``\\M+5D2BB\\M+5B5C0``                         多数样本
   明文 UTF-8 中文       ``上海环境节能工程有限公司``                        s03
   Latin-1 伪编码        ``¹øÂˉ·¿``（= GBK ``B9F8C2CBB7BF`` = 锅炉房）  p01
   ===================  ==========================================  ==========

   第三种最阴险：文件字节是**合法 UTF-8**（``C2B9``），但字符本身是错的 ——
   要还原得先 ``latin-1`` 编码回字节再按 ``gbk`` 解码。因此本模块**不改动编码形态**：
   只在原串本来就是 MIF 时才回写成 MIF，否则原样保留（见 `_scrub_string` 的
   ``had_mif``）。把非 MIF 的纯文本强行转成 MIF 属于无谓改写，还会让下游的
   Latin-1 还原路径失效。

3. **MTEXT 长文本按 250 字符分块**：组码 ``3`` 重复若干次 + 组码 ``1`` 收尾。
   跨块的 PII（如被切断的手机号）单块匹配不到，因此必须**先拼块、再脱敏、再分块**。

验证纪律
--------
**必须做字节级比对，不能只做语义比对。** 踩过的坑：第一版验证把两侧都
`mif_decode` 后比较，于是「纯文本被转成 MIF 转义」这种编码形态改变**完全看不出来**
（语义相同），漏掉了 p01 的整段改写。正确的做法是同时比对**原始字节**与**语义**。

处理的组码（白名单，不做全量字符串扫描）
----------------------------------------
============================  ====================================
组码 / 位置                    处理
============================  ====================================
TEXT/MTEXT 的 ``1`` ``3``      标注文本（含 ATTRIB/ATTDEF 的 ``1`` 值）
ATTRIB/ATTDEF 的 ``4``         属性提示串
HEADER 敏感变量                ``$LASTSAVEDBY`` 等，见 `DEFAULT_HEADER_SCRUB`
THUMBNAILIMAGE 段              整段删除（缩略图会渲染出图签）
============================  ====================================

用法::

    from water_agent.drawing.desensitize import desensitize_file, DesensitizeConfig

    report = desensitize_file(src, dst, DesensitizeConfig())

    # 命令行批量
    python -m water_agent.drawing.desensitize --in DIR --out DIR --report r.json

退出码：0 全部成功 / 1 有文件失败 / 2 参数或前置条件错误。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# MIF 编解码的实现在 `water_agent.drawing.mif`（读图与脱敏共用，读图不该反向依赖
# 脱敏模块）。此处**保留同名再导出**，`desensitize.mif_decode` 等旧路径继续可用
# —— `tests/test_desensitize.py` 与 `scripts/scan_drawing_pii.py` 都依赖它。
from water_agent.drawing.mif import mif_decode, mif_encode, mif_encoded_chars

# ---------------------------------------------------------------------------
# 规则 ID：报告只带 ID 与计数，**不带命中文本**
# （与 `GuardrailPipeline._summarize` 同一纪律：摘要不泄漏原文）
# ---------------------------------------------------------------------------

RULE_PHONE = "pii.phone"
RULE_TEL = "pii.tel"
RULE_EMAIL = "pii.email"
RULE_ID_CARD = "pii.id_card"
RULE_URL = "pii.url"
RULE_ADDRESS = "pii.address"
RULE_LICENCE = "pii.licence"
RULE_ORG_KEYWORD = "org.keyword"
RULE_ORG_EXPLICIT = "org.explicit"
RULE_PERSON_EXPLICIT = "person.explicit"
RULE_PERSON_ANCHORED = "person.anchored"
RULE_META_LAST_SAVED_BY = "meta.last_saved_by"
RULE_META_GUID = "meta.guid"
RULE_META_HEADER_VAR = "meta.header_var"
RULE_STRUCT_THUMBNAIL = "struct.thumbnail"

#: HEADER 中默认清洗的变量。取值必须与变量类型相容，否则 DXF 会读坏：
#: 字符串型用空串，GUID 型用全零 GUID（**不能**用空串，AutoCAD 会拒绝）。
DEFAULT_HEADER_SCRUB: dict[str, str] = {
    "$LASTSAVEDBY": "",
    "$PROJECTNAME": "",
    "$HYPERLINKBASE": "",
    "$FINGERPRINTGUID": "{00000000-0000-0000-0000-000000000000}",
    "$VERSIONGUID": "{00000000-0000-0000-0000-000000000000}",
}

#: 带人员语义的属性标签。命中时该标签对应的值走锚定替换。
PERSON_LABELS: tuple[str, ...] = (
    "姓名", "设计人", "设计", "制图", "绘图", "描图", "审核", "审核人",
    "校对", "校核", "校核人", "审定", "审定人", "批准", "会签", "修改人",
    "专业负责人", "项目负责人", "设计负责人", "工程负责人", "负责人",
    "designer", "checker", "drawn", "approved", "name",
)

#: 单位名的**后缀锚点**。用后缀而非前缀识别，因为「北京建工金源环保工程有限公司」
#: 这类名称的前缀千变万化，后缀才是稳定特征。
ORG_SUFFIXES: tuple[str, ...] = (
    "设计研究院", "设计研究院有限公司", "勘察设计研究院", "建筑设计研究院",
    "环境工程设计院", "设计院", "研究院", "设计研究院",
    "股份有限公司", "有限责任公司", "工程有限公司", "设计有限公司",
    "环保工程有限公司", "科技有限公司", "有限公司", "集团公司", "集团",
    "设计事务所", "事务所", "设计中心", "设计咨询公司", "环保工程公司",
)

#: 「疑似人名」候选的技术词黑名单。**这是启发式，不是判定** ——
#: 目的只是把报告里的待确认清单从数百条噪声压到可人工过一遍的量级。
_TECH_STOPWORDS: frozenset[str] = frozenset({
    "参见", "数量", "给排水", "序号", "单位", "构筑物", "异径管", "剖面图",
    "铸铁管", "单重", "总重", "比例", "日期", "图号", "备注", "道路", "修改人",
    "钢盖板", "施工图", "浓缩池", "配水井", "调节池", "编号", "建筑物", "铸铁",
    "材料", "重量", "投标", "隔油池", "平面图", "系统图", "大样图", "标准图",
    "设计阶段", "设计项目", "初步设计", "施工图设计", "设计专业", "设计号",
    "设计证号", "污水", "废水", "雨水", "污泥", "清水", "中水", "给水", "排水",
    "管道", "阀门", "水泵", "风机", "水池", "水箱", "井盖", "栏杆", "踏步",
    "标高", "管径", "坡度", "流向", "示意", "说明", "注", "总计", "小计",
    "名称", "规格", "型号", "材质", "长度", "面积", "体积", "流量", "扬程",
    # 以下为**实测踩坑补录**：分隔符可选时这些词被误判为人名（详见 `_PERSON_ANCHOR_RE`
    # 的注释）。现在分隔符已强制，它们不会再被命中；仍留在守卫里作纵深防御，
    # 防止将来有人放松正则时重演同一批误伤。
    "阶段", "项目", "专业", "图库", "题目", "规模为", "证号", "负责", "依据",
    "负责人", "工程名称", "子项名称", "图纸编号", "材料名称", "工艺机械",
    "钢制支架", "可调支架", "蜗轮蜗杆", "星欣设计图库",
})

# ---------------------------------------------------------------------------
# MIF 转义（AutoCAD 的 ``\M+<codepage><4位十六进制>``）
# ---------------------------------------------------------------------------
# 实现已抽到 `water_agent.drawing.mif`，模块顶部已导入并再导出（见那里的注释）。

# ---------------------------------------------------------------------------
# 配置与报告
# ---------------------------------------------------------------------------


class DesensitizeConfig(BaseModel):
    """脱敏策略。默认值即「保守但覆盖实测到的全部已知泄漏面」。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: 显式单位名替换表（**优先级最高**，逐字替换，不做分词）。
    org_terms: tuple[str, ...] = ()
    #: 显式人名替换表。
    person_terms: tuple[str, ...] = ()
    #: 额外自定义规则：``(rule_id, pattern, replacement)``。
    extra_rules: tuple[tuple[str, str, str], ...] = ()
    #: 要清洗的 HEADER 变量 → 替换值。``None`` 表示用 `DEFAULT_HEADER_SCRUB`；
    #: 传空 dict 表示不清洗 HEADER。
    header_scrub: dict[str, str] | None = None
    #: 是否删除 THUMBNAILIMAGE 段（缩略图会渲染出图签，默认删）。
    drop_thumbnail: bool = True
    #: 是否识别 URL（水印站域名也是可识别信息）。
    redact_urls: bool = True
    #: 是否识别固话。默认**关** —— 固话正则与工程编号、图号高度混淆，
    #: 实测 18 份样本 0 命中，开着只有误伤风险没有收益。
    redact_tel: bool = False
    #: 伪名作用域：``run``（同一批次内稳定，便于跨文件追溯）或 ``file``。
    pseudonym_scope: str = "run"
    #: 报告里待确认人名候选的上限（防止报告本身变成一份敏感词清单）。
    max_person_candidates: int = 40


class DesensitizeReport(BaseModel):
    """单份文件的脱敏记录。

    ``hits`` 只记 ``规则ID -> 命中次数``，**不含命中文本**。``unresolved_person_candidates``
    是启发式候选，明确标注为「未自动处理、需人工确认」—— 它的存在是为了**避免**
    「脱敏已完成」的虚假结论，而不是为了好看。
    """

    model_config = ConfigDict(extra="forbid")

    source_name: str
    ok: bool = True
    error: str = ""
    pairs_in: int = 0
    pairs_out: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    hits: dict[str, int] = Field(default_factory=dict)
    header_vars_scrubbed: tuple[str, ...] = ()
    thumbnail_dropped: bool = False
    pseudonyms: dict[str, str] = Field(default_factory=dict)
    unresolved_person_candidates: tuple[str, ...] = ()
    #: 姓氏开头的疑似人名 —— `unresolved_person_candidates` 的**高优先级子集**。
    #: 实测「汪华兵」这类独立成条的真名靠它浮出；**仍然只是候选，不做自动替换**。
    person_name_candidates: tuple[str, ...] = ()
    #: 未覆盖的说明（人可读，用于如实交代边界）。
    coverage_notes: tuple[str, ...] = ()

    @property
    def total_hits(self) -> int:
        return sum(self.hits.values())


class PseudonymAllocator:
    """稳定伪名分配器。同一原文恒得同一伪名，跨文件可追溯。"""

    def __init__(self, scope: str = "run") -> None:
        self.scope = scope
        self._map: dict[tuple[str, str], str] = {}
        self._counters: Counter[str] = Counter()

    def get(self, kind: str, original: str, *, file_key: str = "") -> str:
        key = (kind, original if self.scope == "run" else f"{file_key}\x00{original}")
        existing = self._map.get(key)
        if existing is not None:
            return existing
        self._counters[kind] += 1
        label = {"org": "单位", "person": "人员", "site": "站点"}.get(kind, kind)
        alias = f"[{label}-{self._counters[kind]}]"
        self._map[key] = alias
        return alias

    def as_dict(self) -> dict[str, str]:
        """``伪名 -> 原文`` 的反查表。**只在报告里给人工核对用**，不进日志/ trace。"""
        return {v: k[1] for k, v in sorted(self._map.items(), key=lambda kv: kv[1])}


# ---------------------------------------------------------------------------
# 正则规则
# ---------------------------------------------------------------------------

#: 手机号：11 位，1 开头，第二位 3–9。前后不得接数字（避免切掉长数字的中段）。
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")
#: 固话：区号 + 分隔符 + 号码。**要求分隔符**，否则与图号/编号混淆。
_TEL_RE = re.compile(r"(?<!\d)0\d{2,3}[-‐\s]\d{7,8}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ID_CARD_RE = re.compile(
    r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
)
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s\"'<>，。；]+", re.IGNORECASE)

#: 通信地址：``<省/市/区/县>…<路/街/道/号/里/园/村/大厦/楼>``。
#: 图签里常印设计院地址 —— 实测 p05 的 ``中国·北京朝阳区安慧北里安园甲8号`` 属于可识别
#: 信息（配合单位名可直接定位到具体设计院）。
#:
#: **踩坑记录（2026-09-15，Step 5 复查发现）**：本规则的第一版写成
#: ``[\u4e00-\u9fa5]{2,8}(?:省|自治区|市)`` 开头，对 p05 这条**完全没生效** ——
#: 直辖市的写法是「北京朝阳区」，「北京」后面直接跟「区」，**没有「市」字**，整条规则空转，
#: 地址原样留在样本里；而报告里那唯一 1 处 ``pii.address`` 其实是 d01 的证书号串被
#: 地址规则先截走造成的**误归因**。两个后果都不报错、也不影响其它指标，只在人工核对
#: 原始字节时才暴露。**教训**：注释里写了某个案例，不等于正则覆盖了那个案例；
#: 每条规则都要拿真实字符串跑一遍（见 ``tests/test_desensitize.py`` 的回归用例）。
#: 现改为两个分支：省/地级市带「市」字；直辖市与特别行政区可省略「市」字。
#: 两端锚定（行政区划词开头 + 门牌尾词收尾），避免误伤 ``A区/B路`` 这类图内索引。
_ADDRESS_RE = re.compile(
    r"(?:中国[·•.])?"  # 可选国名前缀（实测样本为「中国·北京朝阳区…」）
    r"(?:[\u4e00-\u9fa5]{2,8}(?:省|自治区|市)"  # 分支 1：广东省 / 深圳市
    r"|(?:北京|天津|上海|重庆|香港|澳门)(?:市)?)"  # 分支 2：直辖市可省「市」
    r"[\u4e00-\u9fa5A-Za-z0-9]{1,24}?"  # 区 / 县 / 街道 / 路名
    r"(?:路|街|街道|道|里|园|村|大厦|大楼|楼|院|区|镇|乡|号)"
    r"[\u4e00-\u9fa5A-Za-z0-9]{0,16}?"  # 门牌细节（弄 / 甲 / 号楼…）
    r"(?:号|室|栋|座|层|楼|园|里|院|区)(?![\u4e00-\u9fa5])"
)

#: 资质/证照编号：``<…>字第<编号>号`` / ``<…>证书<编号>号``。
#: 实测 d01 的 ``深圳市环境保护局颁环保工程技术资格证书001号`` —— 证号可反查持证单位。
#: **必须排在 `_ADDRESS_RE` 之前**：该串同时满足地址规则的「号」结尾形态，先跑地址会把它
#: 记成 ``pii.address``，PII 虽然照样删掉，但报告口径失实（读者会以为命中的是地址）。
_LICENCE_RE = re.compile(
    r"[\u4e00-\u9fa5]{2,20}(?:证书|资格证书|资质证书|执照)"
    r"(?:第)?\s*[A-Za-z0-9\-—]{1,20}\s*号"
)

#: 单位名：``<2–24 个汉字/字母数字><后缀锚点>``。后缀是强特征，前缀宽松即可。
_ORG_RE = re.compile(
    r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,24}?(?:"
    + "|".join(re.escape(s) for s in sorted(ORG_SUFFIXES, key=len, reverse=True))
    + r")"
)

#: 锚定人名：``标签 + 分隔符 + 2–4 个汉字``。
#:
#: **分隔符是强制的，不是可选的。** 这是踩过坑的结论：把分隔符设成可选时，实测
#: 18 份样本命中 10 种「人名」，**全部是技术词** —— ``设计阶段``→``设计``+``阶段``、
#: ``设计项目``→``设计``+``项目``、``设计专业``→``设计``+``专业``、
#: ``设计证号``→``设计``+``证号``、``星欣设计图库``→``设计``+``图库``、
#: ``设计规模为``→``设计``+``规模为``、``设计题目``→``设计``+``题目``。
#: 假阳性率 100%、真阳性 0。要求显式分隔符后，``设计阶段`` 这类无分隔符的词
#: 天然不再命中，而真实的图签写法（``设计：张三``）仍然覆盖。
_PERSON_ANCHOR_RE = re.compile(
    r"(?P<label>"
    + "|".join(re.escape(s) for s in sorted(PERSON_LABELS, key=len, reverse=True))
    + r")(?P<sep>\s*[:：]\s*)(?P<name>[\u4e00-\u9fa5]{2,4})(?![\u4e00-\u9fa5])",
    re.IGNORECASE,
)

#: 纯中文短词 —— 只用于生成「待确认」候选，**不用于替换**。
_CN_SHORT_RE = re.compile(r"^[\u4e00-\u9fa5]{2,4}$")

#: 常见汉语姓氏（百家姓高频段）。**只用于把候选浮出来，不用于自动替换。**
#:
#: 为什么需要它：实测 P-03 里藏着一个独立成条的人名「汪华兵」—— 没有标签、没有
#: 分隔符，靠 `_PERSON_ANCHOR_RE` 抓不到，而「2–4 个汉字」这个特征一次能捞 250 条
#: 技术词，人工过不完。姓氏是唯一能把候选量从 250 压到个位数的**低成本**特征。
#: 但它**不足以**支撑自动替换：``王铜``/``石棉``/``马力`` 这类词以姓氏字开头却是
#: 技术名词。所以这里的产出只进报告的 `person_name_candidates`，等人确认。
_SURNAMES: frozenset[str] = frozenset(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄"
    "和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁"
    "杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍"
    "虞万支柯昝管卢莫经房裘缪干解应宗丁宣贲邓郁单杭洪包诸左石崔吉钮龚"
    "程嵇邢滑裴陆荣翁荀羊於惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓"
    "牧隗山谷车侯宓蓬全郗班仰秋仲伊宫宁仇栾暴甘钭厉戎祖武符刘景詹束龙"
    "叶幸司韶郜黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻"
    "莘党翟谭贡劳逄姬申扶堵冉宰郦雍卻璩桑桂濮牛寿通边扈燕冀郏浦尚农温"
    "别庄晏柴瞿阎充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡"
    "国文寇广禄阙东欧殳沃利蔚越夔隆师巩厍聂晁勾敖融冷訾辛阚那简饶空曾"
    "毋沙乜养鞠须丰巢关蒯相查后荆红游竺权逯盖益桓公"
)

#: MTEXT 分块长度上限（DXF 规范：组码 3 每块 ≤ 250 字符）。
_MTEXT_CHUNK = 250


# ---------------------------------------------------------------------------
# 文本级脱敏
# ---------------------------------------------------------------------------


def _scrub_string(
    raw: str,
    *,
    config: DesensitizeConfig,
    alloc: PseudonymAllocator,
    file_key: str,
    hits: Counter[str],
    candidates: list[str],
    surname_candidates: list[str],
    allow_person_anchor: bool,
) -> str:
    """对**单个字符串值**脱敏：解码 MIF → 依次应用规则 → 编码回 MIF。

    只在白名单组码上被调用，因此可以放心地做「解码 + 全量替换」。
    """
    decoded = mif_decode(raw)
    #: 原串里**本来就是 MIF** 的字符。回写时只还原这部分，其余非 ASCII 字符
    #: 保持原形态（实测存在 MIF 与 Latin-1 伪编码**混在同一串**的情况）。
    mif_chars = mif_encoded_chars(raw)

    # 1) 显式替换表（优先级最高，用户说了算）
    for term in config.org_terms:
        if term and term in decoded:
            alias = alloc.get("org", term, file_key=file_key)
            n = decoded.count(term)
            decoded = decoded.replace(term, alias)
            hits[RULE_ORG_EXPLICIT] += n
    for term in config.person_terms:
        if term and term in decoded:
            alias = alloc.get("person", term, file_key=file_key)
            n = decoded.count(term)
            decoded = decoded.replace(term, alias)
            hits[RULE_PERSON_EXPLICIT] += n

    # 2) 自定义规则
    for rule_id, pattern, repl in config.extra_rules:
        try:
            rx = re.compile(pattern)
        except re.error:
            hits[f"{rule_id}.INVALID_PATTERN"] += 1
            continue
        decoded, n = rx.subn(repl, decoded)
        if n:
            hits[rule_id] += n

    # 3) 单位名（后缀锚定）
    def _org_sub(m: re.Match[str]) -> str:
        alias = alloc.get("org", m.group(0), file_key=file_key)
        hits[RULE_ORG_KEYWORD] += 1
        return alias

    decoded = _ORG_RE.sub(_org_sub, decoded)

    # 4) 锚定人名（仅同串「标签+值」形态）
    if allow_person_anchor:
        def _person_sub(m: re.Match[str]) -> str:
            name = m.group("name")
            if name in _TECH_STOPWORDS:
                return m.group(0)
            alias = alloc.get("person", name, file_key=file_key)
            hits[RULE_PERSON_ANCHORED] += 1
            return f"{m.group('label')}{m.group('sep')}{alias}"

        decoded = _PERSON_ANCHOR_RE.sub(_person_sub, decoded)

    # 5) 确定性 PII
    decoded, n = _PHONE_RE.subn("[PHONE]", decoded)
    if n:
        hits[RULE_PHONE] += n
    if config.redact_tel:
        decoded, n = _TEL_RE.subn("[TEL]", decoded)
        if n:
            hits[RULE_TEL] += n
    decoded, n = _EMAIL_RE.subn("[EMAIL]", decoded)
    if n:
        hits[RULE_EMAIL] += n
    decoded, n = _ID_CARD_RE.subn("[ID_CARD]", decoded)
    if n:
        hits[RULE_ID_CARD] += n
    if config.redact_urls:
        decoded, n = _URL_RE.subn("[URL]", decoded)
        if n:
            hits[RULE_URL] += n

    # 7) 证号与地址（图签常见）。顺序**不可交换**：证号串同时满足地址规则的
    #    「…号」结尾形态，先跑地址会把它误归因成 pii.address（PII 照样删掉，
    #    但报告口径失实）。两者都在单位名替换之后跑 —— 单位名已换成 [单位-N]，
    #    剩下的地址串仍能被两端锚点识别。
    decoded, n = _LICENCE_RE.subn("[LICENCE]", decoded)
    if n:
        hits[RULE_LICENCE] += n
    decoded, n = _ADDRESS_RE.subn("[ADDRESS]", decoded)
    if n:
        hits[RULE_ADDRESS] += n

    # 6) 收集「疑似人名」候选 —— 只报告，不替换
    stripped = decoded.strip()
    if _CN_SHORT_RE.match(stripped) and stripped not in _TECH_STOPWORDS:
        candidates.append(stripped)
        # 姓氏开头 → 单独归档到高优先级候选（这类最可能是真名，人工优先看）。
        # 实测：P-03 的「汪华兵」就是靠这一步浮出来的。
        if stripped[0] in _SURNAMES and stripped not in config.person_terms:
            surname_candidates.append(stripped)

    return mif_encode(decoded, only=mif_chars) if mif_chars else decoded


# ---------------------------------------------------------------------------
# 文件级脱敏（组码流处理）
# ---------------------------------------------------------------------------


def _to_pairs(lines: Sequence[str]) -> list[list[str]]:
    """DXF 行对 → ``[组码, 值]`` 列表。奇数行尾（不完整对）原样保留为单元素。"""
    pairs: list[list[str]] = []
    i = 0
    while i + 1 < len(lines):
        pairs.append([lines[i].strip(), lines[i + 1]])
        i += 2
    if i < len(lines):
        pairs.append([lines[i], ""])
    return pairs


def _from_pairs(pairs: Iterable[Sequence[str]], newline: str) -> str:
    out: list[str] = []
    for p in pairs:
        if len(p) == 1:
            out.append(p[0])
        else:
            out.append(p[0])
            out.append(p[1])
    return newline.join(out) + newline


def _find_thumbnail_span(pairs: Sequence[Sequence[str]]) -> tuple[int, int] | None:
    """定位 THUMBNAILIMAGE 段 ``[start, end]``（含端点，覆盖 ``ENDSEC``）。

    不依赖段在文件中的位置：先找 ``0/SECTION``，再看紧随的 ``2/<name>``。
    """
    for i, p in enumerate(pairs):
        if len(p) >= 2 and p[0] == "0" and p[1].strip() == "SECTION":
            name = ""
            for j in range(i + 1, min(i + 5, len(pairs))):
                if pairs[j][0] == "2":
                    name = pairs[j][1].strip()
                    break
            if name == "THUMBNAILIMAGE":
                for k in range(i + 1, len(pairs)):
                    if pairs[k][0] == "0" and pairs[k][1].strip() == "ENDSEC":
                        return i, k
                return i, len(pairs) - 1
    return None


def desensitize_dxf_text(
    text: str,
    config: DesensitizeConfig | None = None,
    *,
    alloc: PseudonymAllocator | None = None,
    file_key: str = "",
    source_name: str = "",
) -> tuple[str, DesensitizeReport]:
    """对 DXF 文本脱敏。返回 ``(脱敏后文本, 报告)``。

    ``alloc`` 可在多文件间复用，使伪名在同一批次内稳定（``pseudonym_scope="run"``）。
    """
    cfg = config or DesensitizeConfig()
    allocator = alloc or PseudonymAllocator(cfg.pseudonym_scope)
    hits: Counter[str] = Counter()
    candidates: list[str] = []
    surname_candidates: list[str] = []

    newline = "\r\n" if "\r\n" in text else "\n"
    pairs = _to_pairs(text.splitlines())
    pairs_in = len(pairs)

    # --- THUMBNAILIMAGE：整段删除 ---
    thumb_dropped = False
    if cfg.drop_thumbnail:
        span = _find_thumbnail_span(pairs)
        if span is not None:
            start, end = span
            del pairs[start:end + 1]
            thumb_dropped = True
            hits[RULE_STRUCT_THUMBNAIL] += 1

    scrub_map = DEFAULT_HEADER_SCRUB if cfg.header_scrub is None else cfg.header_scrub
    header_scrubbed: list[str] = []

    # --- 主遍历：按段/实体状态机处理 ---
    section = ""
    entity = ""
    in_header_var: str | None = None
    # MTEXT 分块缓冲：连续 ``3`` 组码 + 收尾 ``1``
    chunk_idx: list[int] = []

    def flush_chunks() -> None:
        """把缓冲的 MTEXT 分块拼起来脱敏，再按 250 字符分回去。"""
        if not chunk_idx:
            return
        joined = "".join(pairs[i][1] for i in chunk_idx)
        scrubbed = _scrub_string(
            joined, config=cfg, alloc=allocator, file_key=file_key,
            hits=hits, candidates=candidates,
            surname_candidates=surname_candidates, allow_person_anchor=True,
        )
        if len(chunk_idx) == 1:
            pairs[chunk_idx[0]][1] = scrubbed
        else:
            pieces = [scrubbed[i:i + _MTEXT_CHUNK] for i in range(0, len(scrubbed), _MTEXT_CHUNK)]
            if not pieces:
                pieces = [""]
            # 前 n-1 块用组码 3，最后一块用组码 1（DXF 规范）
            for k, idx in enumerate(chunk_idx):
                pairs[idx][0] = "1" if k == len(chunk_idx) - 1 else "3"
                pairs[idx][1] = pieces[k] if k < len(pieces) else ""
            for extra in pieces[len(chunk_idx):]:
                pairs[chunk_idx[-1]][1] += extra
        chunk_idx.clear()

    i = 0
    while i < len(pairs):
        code, value = pairs[i][0], pairs[i][1]
        vstrip = value.strip()

        if code == "0":
            flush_chunks()
            if vstrip == "SECTION":
                # 段名在紧随其后的 ``2`` 组码上，下一轮赋值。
                section = ""
                entity = ""
            elif vstrip == "ENDSEC":
                section = ""
                entity = ""
            else:
                entity = vstrip
            in_header_var = None
            i += 1
            continue

        # ``0/SECTION`` 之后的 ``2/<段名>`` —— 唯一可信的段名来源。
        if (
            code == "2"
            and i > 0
            and pairs[i - 1][0] == "0"
            and pairs[i - 1][1].strip() == "SECTION"
        ):
            section = vstrip

        # --- HEADER 敏感变量 ---
        if section == "HEADER" and code == "9" and vstrip in scrub_map:
            in_header_var = vstrip
            i += 1
            continue
        if section == "HEADER" and in_header_var and code in ("1", "2", "3"):
            pairs[i][1] = scrub_map[in_header_var]
            header_scrubbed.append(in_header_var)
            hits[RULE_META_LAST_SAVED_BY if in_header_var == "$LASTSAVEDBY"
                 else RULE_META_GUID if in_header_var in ("$FINGERPRINTGUID", "$VERSIONGUID")
                 else RULE_META_HEADER_VAR] += 1
            in_header_var = None
            i += 1
            continue
        if section == "HEADER" and in_header_var and code != "9":
            # 变量的值组码不是字符串型 → 该变量本就不含文本，跳过清洗
            in_header_var = None

        # --- 实体字符串值 ---
        if entity in ("TEXT", "MTEXT", "ATTRIB", "ATTDEF"):
            if entity in ("MTEXT", "TEXT") and code == "3":
                chunk_idx.append(i)
                i += 1
                continue
            if entity in ("MTEXT", "TEXT") and code == "1" and chunk_idx:
                chunk_idx.append(i)
                flush_chunks()
                i += 1
                continue
            if code in ("1", "3", "4"):
                flush_chunks()
                pairs[i][1] = _scrub_string(
                    value, config=cfg, alloc=allocator, file_key=file_key,
                    hits=hits, candidates=candidates,
                    surname_candidates=surname_candidates,
                    allow_person_anchor=(entity in ("ATTRIB", "ATTDEF") or code == "1"),
                )
        i += 1

    flush_chunks()

    out_text = _from_pairs(pairs, newline)

    # 待确认候选：去重 + 限量（报告不应变成一份敏感词清单）
    seen: dict[str, None] = {}
    for c in candidates:
        seen.setdefault(c, None)
    cand_list = tuple(list(seen)[: cfg.max_person_candidates])

    # 姓氏开头的候选单独成列 —— 量小、命中率高，人工优先过一遍
    surname_seen: dict[str, None] = {}
    for c in surname_candidates:
        surname_seen.setdefault(c, None)
    surname_list = tuple(surname_seen)

    notes: list[str] = []
    if cand_list:
        notes.append(
            f"{len(seen)} 个 2–4 字纯中文短词未自动处理（可能是人名，也可能是技术术语），"
            "已列入 unresolved_person_candidates 供人工确认；系统不做自动判定"
        )
    if surname_list:
        notes.append(
            f"{len(surname_list)} 个候选以常见姓氏开头（{', '.join(surname_list[:6])}"
            f"{'…' if len(surname_list) > 6 else ''}），已列入 person_name_candidates —— "
            "**这类最可能是真实姓名，请人工确认后加入 person_terms**"
        )
    notes.append(
        "人名只在「显式替换表」与「标签+值同串」两种高置信形态下替换；"
        "独立成条的人名（图签里常见）无法在不误伤技术词的前提下自动识别"
    )
    if not cfg.person_terms:
        notes.append("未提供 person_terms —— 若样本含真实姓名，建议补充显式替换表")

    report = DesensitizeReport(
        source_name=source_name,
        ok=True,
        pairs_in=pairs_in,
        pairs_out=len(pairs),
        bytes_in=len(text.encode("utf-8", "replace")),
        bytes_out=len(out_text.encode("utf-8", "replace")),
        hits=dict(sorted(hits.items())),
        header_vars_scrubbed=tuple(dict.fromkeys(header_scrubbed)),
        thumbnail_dropped=thumb_dropped,
        pseudonyms=allocator.as_dict(),
        unresolved_person_candidates=cand_list,
        person_name_candidates=surname_list,
        coverage_notes=tuple(notes),
    )
    return out_text, report


def desensitize_file(
    src: Path,
    dst: Path,
    config: DesensitizeConfig | None = None,
    *,
    alloc: PseudonymAllocator | None = None,
) -> DesensitizeReport:
    """读 ``src`` → 脱敏 → 写 ``dst``。失败以 ``ok=False`` 返回，不抛异常。"""
    cfg = config or DesensitizeConfig()
    try:
        raw = src.read_bytes()
    except OSError as exc:
        return DesensitizeReport(source_name=src.name, ok=False, error=f"读取失败: {exc}")

    text = raw.decode("utf-8", "replace")
    out_text, report = desensitize_dxf_text(
        text, cfg, alloc=alloc, file_key=src.name, source_name=src.name
    )
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(out_text, encoding="utf-8", newline="")
    except OSError as exc:
        report.ok = False
        report.error = f"写入失败: {exc}"
    return report


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _load_config(path: str | None) -> DesensitizeConfig:
    if not path:
        return DesensitizeConfig()
    data = json.loads(Path(path).read_text("utf-8"))
    if "extra_rules" in data:
        data["extra_rules"] = tuple(tuple(r) for r in data["extra_rules"])
    return DesensitizeConfig(**data)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m water_agent.drawing.desensitize",
        description="DXF 脱敏：移除可识别信息，保留管径/高程/图层/几何图元等技术内容。",
    )
    ap.add_argument("--in", dest="src", required=True, help="输入目录或单个 .dxf")
    ap.add_argument("--out", dest="dst", required=True, help="输出目录")
    ap.add_argument("--config", default=None, help="JSON 配置（替换表等）")
    ap.add_argument("--report", default=None, help="报告输出路径（JSON）")
    args = ap.parse_args(argv)

    src = Path(args.src)
    dst = Path(args.dst)
    if not src.exists():
        print(f"前置条件缺失：输入不存在 {src}", file=sys.stderr)
        return 2

    try:
        cfg = _load_config(args.config)
    except Exception as exc:  # noqa: BLE001 — 配置错就是参数错
        print(f"配置读取失败：{exc}", file=sys.stderr)
        return 2

    files = [src] if src.is_file() else sorted(src.glob("*.dxf"))
    if not files:
        print(f"输入目录内没有 .dxf：{src}", file=sys.stderr)
        return 2

    dst.mkdir(parents=True, exist_ok=True)
    alloc = PseudonymAllocator(cfg.pseudonym_scope)
    reports: list[DesensitizeReport] = []
    for f in files:
        reports.append(desensitize_file(f, dst / f.name, cfg, alloc=alloc))

    n_fail = sum(1 for r in reports if not r.ok)
    print(f"{'文件':<12}{'状态':<8}{'组码对':>9}{'命中':>7}{'缩略图':>8}  规则明细")
    print("-" * 92)
    for r in reports:
        print(f"{r.source_name[:11]:<12}{'OK' if r.ok else 'FAILED':<8}{r.pairs_out:>9}"
              f"{r.total_hits:>7}"
              f"{'已删' if r.thumbnail_dropped else '-':>8}  "
              f"{', '.join(f'{k}×{v}' for k, v in r.hits.items()) or '-'}")
        if not r.ok:
            print(f"    <- {r.error}")

    total: Counter[str] = Counter()
    for r in reports:
        total.update(r.hits)
    print(f"\n成功 {len(reports) - n_fail} / 失败 {n_fail}")
    print("规则合计: " + (", ".join(f"{k}×{v}" for k, v in sorted(total.items())) or "无命中"))

    if args.report:
        Path(args.report).write_text(
            json.dumps([r.model_dump() for r in reports], ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"报告: {args.report}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
