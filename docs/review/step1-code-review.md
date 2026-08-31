# Step 1 代码评审报告

评审人：资深开发工程师（外部视角）
评审对象：公式口径表 / 水力计算工具 / calc 评测集 / harness 扩展
评审日期：2026-08-31
评审方法：口径一致性核对 + gold 独立复算 + **变异测试** + 静态走查

---

## 0. 结论先行

| 交付物 | 评级 | 判断 |
|---|---|---|
| `docs/design/09-hydraulics-formulas.md` | **B+** | 可合入。口径写得扎实，是本项目目前质量最高的资产 |
| `src/water_agent/tools/hydraulics.py` | **B** | 可合入。公式实现正确，但有 3 处健壮性缺陷 |
| `data/eval/calc_p0.jsonl` | **C+** | ⚠️ **不得单独作为 Step 1 达标依据** |
| `tests/test_hydraulics.py` / `test_calc_harness.py` | **C** | ⚠️ 存在 1 处假测试；缺陷检出率 36% |

**一句话判断：方向对、方法论对、代码对，但"验证"这一层做虚了。**

22/22 = 100% 是真实的，这个数字没有造假。但它证明的是"**现有测试和你写的代码互相一致**"，
而不是"**这批代码在真实出错时会被拦住**"。这两件事的差别，是这次评审最想让团队记住的东西。

> 已核验属实的部分：全量 `pytest` 基线为绿（含 `tests/` 全目录）；harness 跑 `calc_p0.jsonl`
> 实测 22/22；17 条 gold 经**独立复算**（不 import 被实现，按口径表公式另写一份）
> 全部吻合，最大相对误差 4.0e-4。**gold 不是从实现输出抄来的，这一点站得住。**
> 未能核验：`ruff` / `mypy` 结论（评审环境 shell 中断），请团队自行复跑确认。

---

## 1. 我做了一件你们没做的事：变异测试

评审不只看"测试是否通过"，要问一个更狠的问题：

> **如果我故意把代码写错，你的测试能抓到吗？**

做法是把源码里的系数/指数/边界条件逐个做微小篡改，植入明知的错误，看测试是否变红。
能被测试抓到的变异体占比，叫**变异得分（mutation score）**，它是唯一能区分
"测试多" 和 "测试有效" 的指标。

### 1.1 评测集 `calc_p0.jsonl` 的检出能力（19 个有效变异体）

| 变异体（故意植入的错误） | 评测集结果 | 检出 |
|---|---|---|
| Hazen-Williams 系数 `10.67 → 10.7`（+0.28%） | 22/22 通过 | ❌ 漏网 |
| Hazen-Williams 指数 `4.8704 → 4.87`（+0.08%） | 22/22 通过 | ❌ 漏网 |
| Darcy-Weisbach 重力加速度 `9.81 → 9.8`（+0.10%） | 22/22 通过 | ❌ 漏网 |
| 舍维列夫分段点 `1.2 → 1.5 m/s` | 22/22 通过 | ❌ 漏网 |
| 给水流速上限 `1.5 → 2.0 m/s` | 22/22 通过 | ❌ 漏网 |
| 排水流速上限 `5.0 → 10.0 m/s` | 22/22 通过 | ❌ 漏网 |
| 缺参检测去掉空字符串判定 | 22/22 通过 | ❌ 漏网 |
| 删除越界 `assumptions` 标注 | 22/22 通过 | ❌ 漏网 |
| Darcy 漏除以 2（`v²/g`） | 20/22 | ✅ |
| 舍维列夫高/低速系数、`(1+0.867/v)^0.3` 漏指数 | 21/22 | ✅ |
| 曼宁 `R^(2/3)`、漏平方、湿周 `θ→θ/2` | 19/22 | ✅ |
| 管径 `4Q/πv → 2Q/πv`、流速面积翻倍 | 18–20/22 | ✅ |
| 泵扬程漏加剩余水头、`fill_ratio` 边界放宽 | 21/22 | ✅ |

**变异得分：11 / 19 = 58%**

### 1.2 全量单元测试的检出能力（11 个有效变异体）

**变异得分：4 / 11 = 36%**

漏网的和上面高度重合：Hazen-Williams 系数/指数、达西 `g`、舍维列夫分段点、
给水流速上限、缺参空串判定、越界标注。**也就是说单元测试并没有补上评测集的洞。**

### 1.3 这张表说明什么

1. **Hazen-Williams 公式的系数写错，全项目 251 个测试一个都不会红。**
   这是最刺眼的一条——口径表里你专门写了"SI 制与 C 值来源"，结果这个公式最脆弱。
2. **"流速约束按给水/排水场景区分"是任务书点名的关键约束，也是总结里的卖点，
   但给水上/下限、排水下限全部零覆盖。** 把上限从 1.5 改成 2.0，测试全绿。
3. **越界 `assumptions` 标注（口径表着墨最多的一节）完全不参与判分。**
   `score_calc_case` 从头到尾没读过 `calc_result.assumptions`。
4. **舍维列夫分段点没有任何用例落在 1.2–1.5 之间。**
   现有两条：v=1.5（改分段点后仍走高速支）、v=0.8（仍走低速支）——
   分段点怎么改都测不出来。这是评测集设计的典型盲区：**分支边界必须有跨越边界的用例**。

---

## 2. 缺陷清单

### P0 — 合入前必须修

#### P0-1 假测试：`assert ... or True`

`tests/test_hydraulics.py:184`

```python
def test_all_formulas_are_dispatchable(self) -> None:
    for formula in FORMULA_SET:
        assert formula in run_calc.__globals__.get("_CALC_FUNCTIONS", {}) or True
```

**`or True` 让这个断言永远为真。** 它是空转测试。

为什么严重：这是全项目**唯一**守护 `FORMULA_SET` 与 `_CALC_FUNCTIONS` 一致性的测试。
一旦有人在注册表里漏登一个公式，这个测试照样绿。它给出的是假的安全感，
比没有测试更糟——因为有人会指着它说"覆盖到了"。

**修法**：

```python
def test_all_formulas_are_dispatchable(self) -> None:
    assert FORMULA_SET == set(_CALC_FUNCTIONS), (
        f"公式清单与分发表不一致：{FORMULA_SET ^ set(_CALC_FUNCTIONS)}"
    )
```

**纪律**：代码里禁止出现 `assert ... or True`、`except: pass`、
`pytest.skip()` 无条件跳过、`== True` 这类恒真表达式。建议加一条 CI grep 门禁。

#### P0-2 评测集不得单独作为达标依据

`22/22 ≥ 95%` 形式上达标了，但变异得分 58% 说明这套评测集
**对"系数写错"这一类最该防的错误几乎没有免疫力**。

在补齐 §3 的用例之前，不建议对外宣称"计算能力已通过评测验证"。

**这不是说 22 条白写了**——17 条数值 gold 经独立复算完全正确，这是真金白银。
问题在于**用例的"覆盖位置"不对**：数量够，但没打在要害上。

---

### P1 — 本迭代内修

#### P1-1 `_within_tolerance` 与口径表语义相反（这是漏网的根因）

`src/water_agent/harness/scorer.py:91-93`

```python
def _within_tolerance(actual: float, gold: float, tolerances: CalcTolerances) -> bool:
    threshold = max(tolerances.absolute, tolerances.relative * abs(gold))
    return abs(actual - gold) <= threshold
```

口径表 5 处（§1/§2.3/§3/§4/§5）写的是：

> 判分 | 相对误差 ≤1% **且** 绝对误差 ≤0.001

**文档说"且"（AND，两者都要满足），实现写的是"或"（OR，取宽松者）。**

后果量化（以 `gold=1.132`，`relative=0.01`，`absolute=0.001` 为例）：

- `max(0.001, 0.01132) = 0.01132` → 实际生效容差是 **1% 相对**
- 那个 `absolute=0.001` **完全失效，是死参数**

除了 `slope_manning`（数值本身小，`max(0.0001, 3.85e-5)=0.0001`，绝对阈值才生效），
**其余 6 个公式的绝对阈值全是摆设**。

这正是 `10.67 → 10.7`（偏差 0.28%）能大摇大摆通过的原因：0.28% < 1%。

**修法**（推荐对齐文档，改实现不改文档）：

```python
def _within_tolerance(actual: float, gold: float, tol: CalcTolerances) -> bool:
    abs_err = abs(actual - gold)
    rel_err = abs_err / abs(gold) if gold else 0.0
    return abs_err <= tol.absolute and rel_err <= tol.relative
```

> 如果团队讨论后认为 OR 更合理（小数值场景下 AND 会过严），那就**改口径表**，
> 把"且"改成"或（取宽松者）"，并说明取值逻辑。**口径表和实现必须有一个改，
> 不能有分歧。** 口径表自称"唯一口径来源"，那就以实现服从文档为默认。

#### P1-2 `calc_pump_head` 无异常保护，非数值输入直接崩溃

`src/water_agent/tools/hydraulics.py:229-233`

```python
invalid = [name for name in [...] if float(inputs[name]) < 0]   # 无 try/except
...
total = sum(_numeric(inputs, name) for name in [...])            # 无 try/except
```

`float("abc")` 抛 `ValueError`，`float(None)` 抛 `TypeError`——**两者都会穿透工具层，
直接崩掉调用方**。

这是 7 个计算函数里**唯一**不走 `_positive_numbers()` 的（那个函数里有 try/except 兜底）。
也就是说这条防御是有的，只是这一个函数漏掉了。

上层接了 LLM 之后，模型吐出 `{"static_head_m": "约10米"}` 这种值是常态，
这属于**必现的线上崩溃路径**，不是理论风险。

**修法**：把 pump_head 也收进 `_positive_numbers` 体系，另外补一个
统一的 `_number()` 安全取值助手，让所有函数走同一条防御路径。

同类隐患：`calc_head_loss_darcy_weisbach:135` 的 `float(friction)` 也在裸奔，
`friction_factor="abc"` 会崩。

#### P1-3 Darcy-Weisbach 错误信息张冠李戴

`src/water_agent/tools/hydraulics.py:133-136`

```python
invalid = _positive_numbers(inputs, ["length_m", "diameter_mm", "velocity_mps"])
friction = inputs.get("friction_factor")
if invalid or friction is None or float(friction) <= 0:
    return CalcResult(ok=False, errors=[f"{name} 必须为正数" for name in invalid]
                                       + ["friction_factor 必须为正数"])
```

当 `length_m = -100` 而 `friction_factor = 0.02`（合法）时，条件成立，返回：

```
errors = ["length_m 必须为正数", "friction_factor 必须为正数"]
```

**后半句是假的**——摩阻系数明明是合法的。

这类错误信息的危害被严重低估：工具输出要回填给 LLM 做 observation，
一条错误的诊断会把模型往完全错误的方向带（它会去追问用户"摩阻系数是不是填错了"）。

**修法**：拆成独立判断，只对真的有问题的参数报错。

---

### P2 — 记录并排期

| # | 位置 | 问题 |
|---|---|---|
| P2-1 | `hydraulics.py` | **公式清单有 5 份副本**：`HydraulicsFormula` Literal、`FORMULA_SET`、`_CALC_FUNCTIONS`、口径表 §6、测试里的字面量集合。任何一处漏改就漂移。应只保留 1 份真源，其余全部 `get_args()` 派生；口径表 §6 用脚本解析后断言一致 |
| P2-2 | `models.py:113-119` | `CalcTolerances` 默认 `relative=0.0, absolute=0.0`。**新用例一旦漏写 `tolerances`，就变成要求浮点精确相等**，报错信息还完全看不出原因。这是个等着坑下一个人的地雷，建议改为必填字段或给一个明确的宽松默认 |
| P2-3 | `models.py:122-136` | `CalcCase.formula: str` 无枚举约束，`input: dict[str, Any]` 无 per-formula schema。口径表写错一个参数名（如 `diameter_m`），结果是 `missing_inputs`——看起来像正常通过，实则是用例写错了。Step 4 做工具注册表时应一并补上 |
| P2-4 | `runner.py:121-122` vs `129-136` | `output_path` 在**写文件之后**才赋值，导致落盘的 JSON 里恒为 `"output_path": ""`，报告无法自证来源。应先赋值再 dump |
| P2-5 | `hydraulics.py:176-179` | 舍维列夫在 v=1.2 处两支不连续：高速支 `i=0.0015408/d^1.3`，低速支 `i=0.0015460/d^1.3`，**跳变约 0.34%**。属经验公式固有特性，非实现错误，但应在口径表注明，并让上层知道 v≈1.2 附近结果有 ±0.4% 的不确定性 |
| P2-6 | 口径表 §4 | `slope_manning` 列出可选输入 `gravity_mps2`（默认 9.81），但曼宁公式不含 `g`，实现也确实没用到。文档列了一个不存在的输入，会误导调用方。建议删除该行 |
| P2-7 | `scorer.py:138-163` | `score_case()` 未被 runner 使用（runner 直接调 `score_calc_case`/`score_eval_case`），且内部会重复执行一次 `run_calc`。疑似死代码，确认后删除或补测试 |

---

## 3. 修复建议：评测集该怎么补

### 3.1 先把 gold 精度提上去（这是当前最大的瓶颈）

现状：gold 只给 3 位有效数字（`1.132`、`0.871`、`9.537`）。
独立复算显示，光是**舍入本身就引入了最高 0.04% 的误差**（calc-0002：1.019 vs 1.018592）。

这就锁死了容差的下限——容差不能比 gold 自身的舍入误差还小，否则用例会自己红。
**gold 精度不够 → 容差只能放松 → 系数错误抓不到。这是个连环套。**

| 项 | 现状 | 建议 |
|---|---|---|
| gold 精度 | 3 位有效数字 | **≥6 位有效数字** |
| `relative` | `0.01`（1%） | **`1e-4`（0.01%）** |
| `absolute` | `0.001` | `1e-6` |
| 判分语义 | OR（`max`，取宽松） | **AND（两者都满足，与口径表一致）** |

收紧后的检出效果（实测推算）：

- 达西 `g 9.81 → 9.8`（偏差 0.102%）→ **抓到**（当前漏网）
- Hazen-Williams `10.67 → 10.7`（0.281%）→ **抓到**（当前漏网）
- Hazen-Williams 指数 `4.8704 → 4.87`（0.076%）→ **抓到**（当前漏网）
- 指数 `1.852 → 1.85`（单项约 0.79%~0.98%）→ **抓到**（当前漏网，且卡在 1% 边缘极其危险）

### 3.2 补 6 条打在要害上的用例

按优先级：

1. **`head_loss_shevelev`，v = 1.3 m/s** — 落在分段点 1.2 与变异值 1.5 之间，
   专治"分段点被改"。**这类"跨越分支边界"的用例是每个分段公式的标配。**
2. **`velocity` + `scenario=water_supply`，v > 1.5** — 例如 DN50、Q=5 L/s（v≈2.55 m/s），
   期望 `succeeded` 且 `assumptions` 含"高于"。
3. **`velocity` + `scenario=water_supply`，v < 0.6** — 例如 DN300、Q=10 L/s（v≈0.14 m/s），
   期望 `assumptions` 含"低于"。
4. **`velocity` + `scenario=drainage`，v < 0.6** — 排水最小流速 0.6 m/s 是规范硬约束，
   目前同样零覆盖。
5. **`velocity` + `scenario` 传空字符串** — 治"缺参检测去掉空串判定"的漏网。
6. **`velocity` + 未知 `scenario`** — 当前实现会写一条 assumption 但仍 `ok=True`，
   行为需要用例固化下来（这是设计决策，不是 bug，但必须有测试锁住）。

> 第 2–4 条要求 `CalcCase` 增加 `expected_assumptions` 字段，
> 并让 `score_calc_case` 校验 `assumptions`。
> **"越界不拦截但要标注"是口径表的核心策略，不参与判分就等于没写。**

### 3.3 单元侧补一条口径文档一致性测试

```python
def test_formula_set_matches_spec_doc():
    """口径表 §6 是唯一真源：从 markdown 解析出来比对，避免第 5 份副本漂移。"""
    doc = Path("docs/design/09-hydraulics-formulas.md").read_text(encoding="utf-8")
    block = doc.split("## 6. 公式清单")[1]
    listed = {ln.strip() for ln in block.split("```")[1].splitlines() if ln.strip()}
    assert listed == FORMULA_SET == set(_CALC_FUNCTIONS)
```

这一条同时解决 P2-1 和 P0-1：把口径表变成机器可校验的，而不只是给人看的。

---

## 4. 给团队的三个方法论

这次评审的具体缺陷修完就没了，但这三条留下来才真的提升团队水平。

### 4.1 测试绿 ≠ 代码对；要用变异测试度量测试本身

行业经验值：**变异得分 70%+ 才算及格，85%+ 才算良好。**
你们现在是 36%（单元）/ 58%（评测集）。

不必一步到位。建议先把 §3.2 那 6 条用例补上，跑一遍变异测试看得分变化——
**让团队亲眼看到"加了 6 条用例，得分从 36% 跳到 70%"**，
这个体感比任何规范宣讲都有效。

变异测试不用上重型框架。这次评审用的就是一个 60 行的脚本：
读源码 → 字符串替换 → 写临时模块 → 跑 pytest → 统计退出码。
建议做成 `scripts/mutation_check.py` 挂进 CI，每次 PR 跑一轮。

### 4.2 gold 值必须比实现更精确

金标准数据的铁律：**gold 的精度必须高于被测实现的精度要求，且来源独立于实现。**

你们"gold 独立参考计算"这条做对了（我复算过，可信），
但**精度只给 3 位，等于自己给自己设了天花板**。
下次写 gold 时问一句：**"如果某个系数错了 0.1%，这条 gold 能看出来吗？"**
看不出来就说明精度不够。

### 4.3 容差是"你能容忍的错误"，不是"让测试变绿的旋钮"

容差放宽是让测试变绿最快的手段，也是最容易掩盖问题的手段。
每次想放宽容差之前，先问：

1. 我能接受多大的工程误差？——这是**需求**，不是调试参数
2. 这个容差下，"某个系数打错一位"能被抓到吗？——这是**校验**
3. 如果抓不到，是 gold 精度不够，还是用例位置不对？——这是**定位**

这次的 `relative=0.01` 就是典型的"为了通过而设"：
1% 相对误差在给排水工程里意味着 DN300 管道流量偏差 10% 以上，
这个精度根本不够做设计依据。

### 4.4 一条纪律建议

`assert ... or True` 这种恒真断言，本质上是**为了凑测试数量而写的测试**。
建议在 Code Review checklist 里加一条硬规则：

> 任何断言，如果把它删掉测试依然通过，它就不该存在。

用变异测试可以自动发现这类问题——恒真断言守护的代码，被变异后测试一定还是绿的。

---

## 5. 值得肯定的部分

评审不能只挑刺。以下几点是真做对了，建议保留并发扬：

1. **口径表先于代码** —— `docs/design/09-hydraulics-formulas.md` 写得非常扎实。
   7 个计算项逐项写清适用场景/单位/公式/系数来源/默认值策略/缺参策略/越界策略，
   尤其"**默认值是否允许**"单独成列——这是很多团队会漏掉的。
   这份文档是全项目目前质量最高的资产。
2. **公式显式指定、禁止静默切换** —— 这条约束在这个领域是对的。
   水力计算里"自动选个公式"是灾难，宁可报错。落实得到位。
3. **缺参/越界结构化区分** —— `missing_inputs` / `errors` / `assumptions` 三分法设计得清楚，
   "越界计算照常返回但标注"这个取舍也符合工程实际。
4. **gold 值来源独立** —— 我独立复算了 17 条，全部吻合，最大误差 4.0e-4。
   没有拿实现输出当 gold，这是有职业素养的做法。
5. **`.gitignore` 维护得干净** —— 连 `:memory:.lock`、`knowledge的替身*` 这类
   临时产物都单独列了规则。这种细节能看出团队的工程习惯不差。

**缺的不是能力，是"验证自己"的习惯。** 这次的差距不在写代码，
在写完代码之后有没有问一句"我怎么证明它是对的"。

---

## 附录：评审执行记录

| 项 | 命令/方法 | 结果 |
|---|---|---|
| gold 独立复算 | 不 import 实现，按口径表另写一份算 17 条 | 17/17 吻合，最大相对误差 4.0e-4 |
| 评测集变异测试 | 19 个有效变异体（源码级篡改） | 检出 11 → **58%** |
| 单元测试变异测试 | 11 个有效变异体，跑全量 `pytest` | 检出 4 → **36%** |
| 全量测试基线 | `pytest -q tests/` | 绿 |
| harness 基线 | `calc_p0.jsonl` | 22/22 |
| `ruff` / `mypy` | — | 评审环境 shell 中断，**未核验**，需团队复跑 |

> 评审过程中对 `src/water_agent/tools/hydraulics.py` 做过变异注入，
> 已通过 `diff` 校验确认**源文件完整还原，无任何残留改动**。
> 本次评审**未修改任何源码或测试用例**——修改与验证需在同一轮完成，
> 评审环境不具备验证条件，故只出结论不改代码。
