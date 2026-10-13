# Ingestion —— 结构化父子级切块与入库

> P1 基线设计：结构化父子级切块为主 + 递归兜底 + 子块检索 + 父块回填。已落地于 `src/water_agent/ingestion/` 与 `src/water_agent/domain/models.py:Chunk`。

## 1. 设计目标

- 保留规范引用精度：最终引用必须指向具体条款 `GB50014-2021 4.4.2`，不允许用 `GB50014-2021 4` 替代
- 利用上下文提升回答质量：命中子块后回填父块（章/节）上下文
- 适配给排水语料多样性：GB 规范条款 + 标准图集 + 教材 + 限值表格

## 2. 整体管线

```
JSONL (knowledge/clauses/*.jsonl) / Markdown (knowledge/*.md)
  → 结构化章/节/条款识别
  → 构建父块(章/节/小节) + 子块(条/款/项/表)
  → 表格专项处理（单块）
  → 递归切块兜底（段落 / token window + overlap）
  → 生成 chunk_id / parent_id / content_hash（稳定 ID）
  → 子块 mock_embed/真实 embed + index_text → TriviumDB
  → 检索命中子块 → parent_id 回填父块上下文
```

仅子块进向量与文本索引；父块持久化为 `<db_path>.parents.json`，由 `TriviumRetriever` 初始化时自动加载，避免父块稀释检索精度并支持跨进程回填。缺失父块文件不阻断子块检索，但会通过 `db_error` 暴露可观测状态。

## 3. 切块层级

| 层级 | chunk_type | 粒度 | 入库 | 备注 |
|---|---|---|---|---|
| 父块 | chapter | 章 | 不入库 | `第4章` |
| 父块 | section | 节 | 不入库 | `4.4` |
| 父块 | subsection | 小节 | 不入库 | `4.4.1`（三级 section 归为小节） |
| 子块 | article | 条 | 入库 | 主检索单元 |
| 子块 | clause | 款 | 入库 | 条下细分 |
| 子块 | item | 项 | 入库 | 列表项 |
| 子块 | table | 表 | 入库单块 | 保留 `| 管径 | 坡度 |` 结构化 |
| 兜底 | paragraph | 段落 | 入库 | 无条号文本 |
| 兜底 | text_window | 窗口 | 入库 | 超长段落 token 窗口 |

## 4. Chunk Schema

```json
{
  "chunk_id": "sha256(spec_id|chapter|section|article|chunk_type|content_hash)[:16]",
  "parent_id": "父块chunk_id | null",
  "chunk_type": "article | clause | item | table | paragraph | text_window",
  "spec_id": "GB50014-2021",
  "chapter": "4",
  "section": "4.4",
  "article": "4.4.2",
  "title": "污水管道水力计算",
  "text": "污水管道的最大设计充满度...",
  "source_path": "knowledge/GB 50014-2021 室外排水设计标准.md",
  "page": 42,
  "content_hash": "sha256(text)[:16]"
}
```

- `content_hash = sha256(text)[:16]`，去重
- `chunk_id = sha256(spec_id|chapter|section|article|chunk_type|content_hash)[:16]`，重复索引不产生随机 ID
- 同一条款拆出的多个子块共享 `article=4.4.2`，引用统一为 `GB50014-2021 4.4.2`

父块示例：

```json
{"chunk_id":"chap_4","chunk_type":"chapter","spec_id":"GB50014-2021","chapter":"4","title":"第4章","text":"第4章"}
```

## 5. 递归兜底 6 规则

| 规则 | 条件 | 动作 |
|---|---|---|
| 1 | 能识别章/节/条款 | 结构化父子切块：`_build_parents()` + `article` 子块 |
| 2 | 条款过长 `> article_max_tokens(800)` | 按段落递归拆分，保留原 `article` 与 `parent_id` |
| 3 | 无条号但有标题 | 按标题层级建父子（`title` 视为节），子块按段落 |
| 4 | 无结构文本 | 按段落递归切块 |
| 5 | 段落仍过长 `> target_tokens(600)` | 按 token 窗口切分 `window=600 overlap=80`，生成 `text_window` |
| 6 | 表格 | `_extract_tables()` 抽 `\| ... \|` 表格，单块 `chunk_type=table`，保留表头-表体 |

关键约束：
- 子块不跨两条；父块不跨无关章节；同一 article 的子块共享逻辑引用
- `table` 永不拼接，`text` 保留 `| 管径 | 最大充满度 |` 原结构

## 6. 默认参数

| 语料 | 目标块大小 | overlap |
|---|---|---|
| 普通教材 | 400～800 tokens | 50～100 tokens |
| 规范条款 | 优先整条款 `article_max_tokens=800`，仅超长才拆段落 | 同上 |
| 表格 | 单块 | 无 overlap |

代码默认：`ChunkConfig(target_tokens=600, overlap_tokens=80, article_max_tokens=800)`。

## 7. 检索流程

```
用户问题: "污水管道DN300最大充满度是多少？"
  → mock_embed/真实 embed + query_text
  → TriviumDB.search_hybrid(子块 only, top_k=5, hybrid_alpha=0.7)
  → 命中 child: {chunk_id, parent_id, article=4.4.2}
  → parent_store[parent_id] 回填父块: "4 排水管渠 / 4.4 水力计算 ..."
  → context = 子块.text + "【父块上下文】\n" + 父块标题链
  → Generator 生成，citations 绑定子块 article
  → 引用: GB50014-2021 4.4.2
```

实现：`src/water_agent/retrieval/trivium.py:TriviumRetriever._enrich_with_parent()` 在 `retrieve()` 后拼前缀 `【chapter / section / title】`，不覆盖子块 `article`，确保 `citation_check` 仍以子块 `article` 为准。真实 DB 链路若检索/嵌入/payload 解析失败，必须抛出 `TriviumRetrievalError`，不得静默回退 fixture。

## 8. 入库 API

```python
from water_agent.ingestion.pipeline import ingest_jsonl_to_chunks, ingest_to_trivium

chunks, parents = ingest_jsonl_to_chunks("knowledge/clauses/GB50014-2021.jsonl")
stats = ingest_to_trivium("knowledge/clauses/GB50014-2021.jsonl", "data/p1.db")
# 批量
_venv/bin/python scripts/ingest.py "knowledge/clauses/*.jsonl" --db-path data/p1.db --overwrite
```

CLI 与 Harness 侧：

```bash
python -m water_agent.cli --db-path data/p1.db "污水管道的充满度要求"
python -m water_agent.harness.runner data/eval/rag_p0.jsonl --fixture-path data/fixtures/rag_fixture.jsonl
# variant=trivium 的用例自动走 TriviumRetriever + parent_store 回填
```

### 9. TriviumDB 0.8.2 幂等依据

经实际 API 探查，TriviumDB 0.8.2 提供：

- `insert_with_id(id, vector, payload)`：固定数字 node id 插入，重复 id 抛 `Node already exists`
- `upsert_with_id(id, vector, payload)`：固定数字 node id 更新或插入
- `all_node_ids()`：读取现有 node id
- `index_text(id, text)`：建立文本索引；重复调用实测不产生重复 hit
- `flush()` / `close()`：持久化与释放资源
- `search_hybrid(query_vector, query_text, top_k, hybrid_alpha)`：混合检索

因此 `chunk_id` 的十六进制值转为整数 node id，使用 `<db_path>.manifest.json` 记录本管线已成功处理过的 id：

- 首次出现：`inserted`
- manifest 已存在：`skipped_existing`
- DB 存在但 manifest 不存在：`updated`
- 任意 `insert/upsert/index_text/flush` 失败：`failed`，并抛出含 `source_file/chunk_id/operation/error` 的 `IngestError`

### 9.1 索引元数据 `<db_path>.index_meta.json`（P7-S10）

embedding 端点可配置之后（`WATER_AGENT_EMBEDDING_BASE_URL` / `WATER_AGENT_EMBEDDING_MODEL` / `WATER_AGENT_EMBEDDING_API_KEY`，
缺省百炼 `qwen3.7-text-embedding`），"换了模型还用旧向量"会让检索**静默**变差、没有任何报错。所以 `scripts/build_index.py`
建库时把这份索引的 embedding 来源写在旁边：

```json
{
  "format": 1,
  "embedding_model": "qwen3.7-text-embedding",
  "embedding_host": "dashscope.aliyuncs.com",
  "dimensions": 512,
  "index_version": "qwen3.7-text-embedding__512__curated-v1",
  "retrieval_modes": ["hybrid", "text"]
}
```

- 只记主机名，不记完整 base_url；判定只用模型与维度（`retrieval/trivium.py::embedding_mismatch`）。
- 检索侧打开索引时比对当前 embedding 配置：模型或维度不一致、`retrieval_modes` 不含 `hybrid`（纯关键词索引）、元数据读不出
  → 拒绝向量检索（`RETRIEVAL_EMBEDDING_MISMATCH`）。
- **没有这个文件的旧索引**按百炼缺省模型（`qwen3.7-text-embedding`、库的实际维度）对待，运行状态写 `index_meta=absent`（向后兼容）。
- 往已有索引里追加时，元数据与本次模型 / 维度不一致即拒绝（一份索引混两种模型的向量就是损坏）；元数据在入库**之前**写出，
  中途失败的库同样记着它的向量来源。
- `data/indexes/` 整目录不入库（`.gitignore`），元数据随索引一起由 `build_index.py` 产出、随镜像分发（`.dockerignore` 整目录放行）。

## 10. 验证门禁

- `test_chunker.py`: 不跨条/不跨章/超长拆分保留 article/表格单块/窗口 overlap/ID 稳定/ fixture 父子链完整
- `TriviumRetriever` 实测：fixture 切块后 22 chunks(12 parents + 10 children) → `search_hybrid` 命中后 `source_id` 仍为 `FIX 1.1.1` 等条款级引用
- `pytest tests/test_review_fixes.py tests/test_ingestion.py tests/test_chunker.py -q` 通过后再跑全量 `pytest -q`
