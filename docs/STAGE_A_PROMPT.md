# 阶段 A 执行 Prompt（可直接粘贴给执行 Agent）

> 用法：整段复制给 Cursor / WorkBuddy / 任何执行 Agent。已按「未参与前期对话」的前提写全，
> 所有路径、命令、验收标准均显式给出。**遇到 STOP 条款必须停下来问人，不要自行推断。**

---

你是「给排水 Agent」项目的执行者。现在执行**阶段 A：给已完成的工作兜底**。

## 零、项目背景（执行前必读）

项目位置：`/Users/yaerken/Desktop/给排水agent`

这是一个面向给排水设计院的 AI Agent，最终覆盖规范问答、水力计算、计算书生成、图纸审查、材料统计。
设计文档在 `docs/design/`（9 份），工程原则在 `docs/CURSOR_BUILD_BRIEF.md`，现状盘点在 `docs/09-next-steps.md`。

**三条不可妥协的原则：**
1. 计算类永远走程序化校验，绝不让 LLM 口算
2. 引用可追溯 —— 每个回答必须指回「规范号+条款号」，禁止编造
3. 评测先行 —— 任何改动不得让关键指标下降，且指标必须可度量、可回归

**当前状态：P0 完成，P1 的「规范问答」已打通，「水力计算」尚未开始。**
阶段 A 只做止损和固化，**严格执行阶段范围，不要顺手去做水力计算、文档生成或图纸审查。**

### 运行环境（重要）

- 必须用项目自带虚拟环境：`./_venv/bin/python`（**不要用系统 python 或 `python3`**）
- 所有命令的工作目录：`/Users/yaerken/Desktop/给排水agent`
- 测试基线：`./_venv/bin/python -m pytest -q` → **214 passed，退出码 0**。
  阶段 A 全程必须保持 214 全绿。任何一步导致测试变红，先修测试再继续。
- `DASHSCOPE_API_KEY` 已在环境中配置；`DEEPSEEK_API_KEY` 未配置（见 A4）
- `data/indexes/` 下已有两套索引：`qwen3.7__512__curated.db`、`qwen3.7__256__curated.db`

---

## A1. 修复 `semantic_audit.py` 的中文乱码

文件：`src/water_agent/ingestion/semantic_audit.py`

该文件在早前的一次写入中，所有非 ASCII 字符被替换成了 `?`，共 19 行受影响。
乱码已随报告写入 `data/quality/reports/*_semantic_audit.json` 和 `.md`，导致
「正式入库策略」「待人工决策事项」两节不可读。

已全量确认：**乱码只存在于这一个文件**，`src/`、`scripts/`、`tests/` 其余文件干净。

### 🛑 STOP 条款（最高优先级）

**第 19 行是正则表达式，不是文案，禁止猜测修复。**

```python
OCR_SYMBOL_RE = re.compile(r"[#|]{3,}|[??]{1,}")
```

`[??]` 原本是两个非 ASCII 字符（大概率是两个 OCR 常见的占位/乱码符号，如 `□■`、`○●` 之类），
**但具体是什么无法从上下文推断，猜错会直接改变 OCR 疑似检测的行为**，
进而影响 397 条隔离记录的划分，属于静默改变数据口径。

处理方式：
1. 这一行**原样保留，不要修改**
2. 在文件该行上方加一行 TODO 注释：
   ```python
   # TODO(需人工确认): 原字符在编码事故中丢失，当前 [??] 是占位，待用户确认真实字符集后替换
   ```
3. 在最终汇报里**单独列出这一项**，交给用户确认

### 其余 18 行：按上下文恢复

下表给出每行所在的逻辑分支，据此恢复文案。恢复后的中文要**忠实反映该分支的判定逻辑**，
不要添加原文没有的信息，也不要写成空话。

| 行号 | 所在分支 | 应表达的语义 |
|---|---|---|
| 1 | 模块 docstring | 语义质检模块：对规范条款语料做分类、重复组识别、给出入库策略建议 |
| 127 | `quarantine` 分支 | 命中 OCR 疑似 / 条款号非法 / 低信息量，整条隔离待人工复核 |
| 131 | `table_fragment` 分支 | 识别为表格片段，按 table 块保留原表格结构 |
| 135 | `normal_article` 分支 | 条款格式规范，按父子级结构正常入库 |
| 179 | `_group_recommendation` 含隔离项 | 重复组内存在隔离项，整组隔离 |
| 181 | `_group_recommendation` 含表格 | 组内为同一 article 的表格片段，按原文顺序保留 |
| 182 | `_group_recommendation` 人工复核 | 同一 spec_id + article 的重复组，需人工判定是否为合法片段 |
| 226 | 策略表 `normal_article` | 正常父子级入库 |
| 227 | 策略表 `article_fragment` | 按序保留片段，作为 child 挂到同一 article |
| 228 | 策略表 `table_fragment` | 保留为 table chunk，沿用所属 article 引用 |
| 229 | 策略表 `ocr_suspect` | 隔离（OCR 质量存疑） |
| 230 | 策略表 `invalid_article_format` | 隔离（条款号格式不合规） |
| 231 | 策略表 `empty_or_low_signal` | 隔离（内容为空或信息量过低） |
| 232 | 策略表 `duplicate_article_variant` | 保留有序片段或转人工复核 |
| 258 | 待人工决策 1 | 同一 spec_id + article 的多条记录，如何区分合法片段与 OCR 重复 |
| 259 | 待人工决策 2 | GB18918-2002 是否整本排除（限值表为主，条款文本有限） |
| 260 | 待人工决策 3 | GB5749-2022 是否只保留限值 table |
| 261 | 待人工决策 4 | 458 组需人工复核的重复组，复核优先级与判定标准 |

### A1 完成后

重跑质检，覆盖被污染的报告：

```bash
./_venv/bin/python -m water_agent.ingestion.semantic_audit_cli \
  --data-root knowledge --output-dir data/quality/reports
```

（参数名以 `src/water_agent/ingestion/semantic_audit_cli.py` 的实际 argparse 定义为准，
若不一致请先读该文件确认。）

**验收**：新生成的 `data/quality/reports/*_semantic_audit.json` 与 `.md` 中，
「正式入库策略」「待人工决策事项」两节中文可读、无 `?` 占位；`pytest -q` 仍 214 通过。
在汇报中附上你恢复的 18 行中文原文清单，供用户过目。

---

## A2. 给检索评测脚本加报告落盘

文件：`scripts/evaluate_retrieval.py`

**问题**：该脚本只把结果 `print` 到 stdout，不写文件。后果是——做了 70 条金标准、
两套索引、三套 rerank，但**没有任何数字能证明检索质量是多少**，后续改动也无法判断变好变坏。
这直接违反项目的「评测先行」原则。

**要求**：复用 `src/water_agent/harness/runner.py:124-130` 里 `HarnessRunner._write_report` 的写法，保持一致风格：

- 新增 `--report-dir` 参数，默认 `data/eval/reports`
- 报告文件名格式：`retrieval_<dimensions>_<rerank>_<UTC时间戳>.json`
- 落盘内容：在现有 `out` 字典基础上，补上 `created_at`（ISO 8601 UTC）
- 用 `path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")`
- 保留 stdout 打印，并在其中增加 `report_path` 字段
- 目录不存在时 `mkdir(parents=True, exist_ok=True)`

注意：`data/eval/reports/` 已被 `.gitignore` 忽略，无需额外处理。

**验收**：跑一次后 `data/eval/reports/` 下出现新报告，`cat` 出来字段完整、中文正常。

---

## A3. 跑基线矩阵并产出对比表

用 A2 改好的脚本，跑 **6 组**（2 维度 × 3 种 rerank），固定其余参数：

```bash
GOLD=data/eval/retrieval_gold.jsonl
for DIM in 512 256; do
  for RK in none local bailian; do
    ./_venv/bin/python scripts/evaluate_retrieval.py \
      --db-path data/indexes/qwen3.7__${DIM}__curated.db \
      --gold "$GOLD" \
      --dimensions "$DIM" \
      --top-k 5 --recall-top-k 20 \
      --rerank "$RK" \
      --rerank-api-key "$DASHSCOPE_API_KEY"
  done
done
```

`bailian` 组会真实调用百炼 gte-rerank，70 条 × 20 候选，注意可能较慢或触发限流；
如遇 429/超时，按脚本现有的瞬时错误重试逻辑处理，仍失败则记录错误并继续下一组，
不要把失败组静默跳过。

**产出**：一张对比表，行是 512 / 256，列是 none / local / bailian，
单元格填 `recall_at_5`（主指标）与 `mrr`（副指标），例如 `0.71 / 0.58`。
同时列出 `queries` 确认 70 条全部参与，没有静默丢用例。

把这张表：
1. 写进 `docs/09-next-steps.md` 的新章节「检索基线（2026-08-31）」
2. 在最终汇报中原样贴出

**这张表是本项目第一份可回归的检索基线，是阶段 A 最重要的产出。**
如果某组数字异常（比如 recall 低于 0.3 或某组直接报错），不要自己调参掩盖，
如实记录并标注为待查项。

---

## A4. 真实问答端到端核验

**前置**：需要 `DEEPSEEK_API_KEY`。当前未配置。

```bash
export DEEPSEEK_API_KEY=<用户提供>
```

若用户暂不提供：**跳过 A4，在汇报中明确标注「端到端未验证」**，不要用 mock 生成器冒充真实链路。

拿到 Key 后执行：

```bash
./_venv/bin/python -m water_agent.cli "给水管道设计秒流量如何确定" \
  --db-path data/indexes/qwen3.7__512__curated.db --dimensions 512 \
  --rerank bailian --rerank-api-key "$DASHSCOPE_API_KEY" --json
```

再自选 **3–5 个覆盖不同规范的问题**（建议至少覆盖 GB50013、GB50014、GB50318 各一个），
问题要来自 `data/eval/retrieval_gold.jsonl` 里的真实 query，便于对照已知正确条款。

**人工核验清单（逐条记录结论）：**
- [ ] 回答中的每个引用，是否都出现在当次检索的 context 里？（有无编造条号）
- [ ] 引用的条款号，是否与 `retrieval_gold.jsonl` 中该 query 的 `gold_articles_any` 一致？
- [ ] 引用粒度是否精确到「条」（如 `GB50014-2021 4.4.2`），而非只到章/节？
- [ ] trace 文件 `data/traces/<trace_id>.json` 中，检索/生成/引用检查事件是否完整？
- [ ] 是否存在「检索到了但 LLM 没引用」或「引用了但检索没召回」的情况？

发现编造引用 → **这是 P1 红线，立即停止并上报**，不要自行改 prompt 掩盖。

---

## A5. 版本控制初始化

**前置**：需用户明确授权（设计文档规定不得自动执行）。

```bash
git init
```

提交前先补 `.gitignore`（现有文件已覆盖大部分，但缺几项）：

```
# 索引产物（3.3MB 的 db 与 manifest 均属生成物）
data/indexes/

# 根目录残留杂物
:memory:.*
knowledge的替身*
```

然后首次提交：

```bash
git add -A
git status    # 确认没有把 *_venv/*、data/indexes/、*.db 加进来
git commit -m "chore: 初始化仓库，固化 P0 完成态与 P1 规范问答链路"
```

**不要打 tag，不要 push。** 打 tag 属于阶段门禁，等阶段 A 全部验收通过后再说。

如用户明确说不 git init → 跳过，并在汇报中保留「版本控制缺失」这条风险。

---

## A6. 重写 README

现有 README 只描述 P0（fixture 检索、mock 生成器），与当前代码严重脱节——
现在已经有真实语料、真实 embedding、真实索引、rerank 和 LLM 生成。

重写要点：
- 项目定位与三条工程原则
- 当前进度：P0 完成 / P1 规范问答已通 / P1 水力计算未开始
- 数据管线与关键产物路径
- 快速验证命令（pytest / 检索评测 / 真实问答），**注明需要哪些 API Key**
- 指向 `docs/design/`、`docs/09-next-steps.md`
- 保留「已知约束」章节，更新为真实状态

**不要夸大进度。** 水力计算没做就是没做，端到端没验证就是没验证。

---

## 执行顺序与门禁

**严格按 A1 → A2 → A3 → A4 → A5 → A6 顺序执行。** A2 是 A3 的前置。

每一步完成后自检：
- [ ] `./_venv/bin/python -m pytest -q` 仍是 214 passed
- [ ] 没有引入新的硬编码绝对路径
- [ ] 没有静默 fallback（真实 DB 检索失败必须抛错，不得回退 fixture）
- [ ] 没有跨越阶段范围去做 P2/P3 的活

## 最终汇报格式

1. **检索基线表**（A3 产出，最重要）
2. **端到端核验结论**（A4，或明确写「因缺 Key 未验证」）
3. **A1 恢复的 18 行中文清单**
4. **🛑 待用户确认项**：`semantic_audit.py:19` 的 OCR 字符类
5. **执行中遇到的异常**，尤其是没有自行消化的问题
6. **测试状态**：214 passed 是否保持

---

## 已知背景（供判断用，不必复述）

- 587 组重复条款中 458 组待人工复核，GB50013-2018 独占 336 组 → 若 A3 基线偏低，这是首要嫌疑
- `knowledge/` 下 221 个 md（40MB，标准图集与教材）尚未入库，当前索引只有 4 本规范的条款 JSONL
- `GB50015-2019.jsonl` 只有 4.7KB（其余三本 770KB/714KB/33KB），该规范基本是空的
- 阶段 A 之后是阶段 B（水力计算，P1 验收硬门槛）与阶段 C（语料扩容）
