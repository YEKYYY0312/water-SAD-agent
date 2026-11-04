# 中转站 embedding 切换与独立检索实测（2026-11-04）

用户选定沿用现有对话 API，并使用 `text-embedding-3-large`。本地 8000 实例已完成配置切换；真实混合检索与一条 Agentic 问答闭环通过。配对指标、索引摘要与运行证据见 [JSON 记录](embedding-proxy-2026-11-04.json)。这是新依赖组合的实测，不替代原百炼索引基线，也不放行 P7-S16。

## 依赖与索引

- 对话与 embedding Base URL 均为 `https://llm-proxy.learnings.ai/v1`；对话模型保持 `deepseek-v4.1-flash`，embedding 模型为 `text-embedding-3-large`。
- 小样本请求实际返回 3072 维有限值向量。该模型的官方缺省维度也是 3072，见 [OpenAI embeddings 指南](https://developers.openai.com/api/docs/guides/embeddings)。中转站返回的模型名称只证明其声明，不能据此验证底层模型权重。
- 精选语料经现有 ingestion / chunk / TriviumDB 构建链写入 3313 个子块、147 个父块。3306 个不同文本经 104 次批量请求取得真实向量，重复文本复用同一向量；每批校验返回索引、维度、有限值与非零向量，不以伪向量补失败。建库请求已知输入用量为 371521 token，不是本次全部请求总用量；价格未知，不估算费用。
- 语料摘要为 `d56c602596332df3e738bd407fb8d39ea276fbd3fa1c27267aed16912edce587`；10 个索引文件合计 47784883 字节，逐文件 SHA256 见 JSON。发布目录 `data/indexes/text-embedding-3-large__3072__d56c60259633__20261104/`，原索引保留。模型／维度绑定写入 `clauses.db.index_meta.json`，不能把旧向量库改个模型名继续用。
- 上述建库摘要按排序后的“文件名＋原始字节”计算；harness 在文件名后额外加入 NUL 分隔，故同一份语料得到 `033061fbb30540f97602085661b888ebae0d119e055fce6780285be56d4feec9`。两种口径均已从当前语料逐文件重算，文件清单及算法保存在 JSON 的 `corpus_identity`，摘要不同不表示语料不同。
- 批量向量缓存与探针脚本保留在受忽略的 `work/embedding-proxy/`，索引二进制不入 Git；入仓证据不含密钥、项目私有会话或 reasoning 正文。

## 54 条配对检索

使用 main `304b85e` 的 `water_agent.harness.retrieval_mode_eval`，数据集为 `data/eval/retrieval_colloquial_p0.jsonl`；两种模式均显式读取本次新索引，`top_k=5`、`recall_top_k=20`、hybrid alpha 为 0.7。text 只使用同一索引的关键词分量，embedding 调用数为 0；hybrid 使用真实向量与关键词分量，发生 108 次真实查询 embedding 调用。两次报告均无 harness error。

指标沿用 `harness/retrieval_metrics.py`：先对来源 ID 保序去重，再计算 Recall@k／MRR。JSON 保留原始返回列表与位置，便于按同一口径复算；原始位置包含重复来源，不能直接当作指标排名。

| 模式／阶段 | Recall@1 | Recall@3 | Recall@5 | MRR |
|---|---:|---:|---:|---:|
| text / retriever | 25.93% | 40.74% | 46.30% | 0.3278 |
| hybrid / retriever | 48.15% | 66.67% | 70.37% | 0.5731 |
| text / local rerank | 31.48% | 44.44% | 51.85% | 0.3870 |
| hybrid / local rerank | 46.30% | 66.67% | 70.37% | 0.5586 |

retriever Recall@5 提升 24.07 个百分点；local rerank 后提升 18.52 个百分点。仍有 16/54 条在前五名未命中 gold。hybrid 的 local rerank 降低了自身 Recall@1 与 MRR，尚无依据声称它改善了这一组合的排序。以上是检索命中指标，不是最终回答正确率，不能与其他数据集的历史百分比混用。

## 本地切换与验证

8000 实例采用配置和只读索引挂载切换：保持原镜像 `sha256:ac9567ae2364d8dfa733926bd944092b843a64b50ec51275aaa36f6c9f763cf2`，数据库仍为 `0007_step51_dwg_uploads`；没有部署新 main 代码或执行数据库迁移。8001 对照实例保持原状。

运行配置保存在宿主机 `.claude/compose.embedding-overlay.yml`，叠加顺序为 `compose.yml` → `.claude/compose.mcp-overlay.yml` → embedding overlay。继续使用完整叠加命令，单独运行基础 Compose 可能覆盖本次配置：

```bash
docker compose -p water-agent -f compose.yml -f .claude/compose.mcp-overlay.yml -f .claude/compose.embedding-overlay.yml up -d --no-deps --no-build app
```

overlay 固定当前镜像，只添加四个非秘密环境变量及只读挂载：

```yaml
WATER_AGENT_EMBEDDING_BASE_URL: https://llm-proxy.learnings.ai/v1
WATER_AGENT_EMBEDDING_MODEL: text-embedding-3-large
WATER_AGENT_RETRIEVAL_MODE: hybrid
WATER_AGENT_INDEX_PATH: /opt/water-agent-indexes/clauses.db
```

凭据复用同站点现有对话 Key，经 `write_credential` 写入受保护的 `WATER_AGENT_EMBEDDING_API_KEY` 槽位，来源为 `volume`，不写入 overlay。为避免旧 embedding 地址短暂取得新 Key，先确认无运行中的请求，停止 app 后写凭据，再以新配置启动。

验证结果：

- `/health/live`、`/health/ready` 均为 HTTP 200，镜像 ID 与数据库版本未变。
- 在实际运行容器内读取配置并真实检索：模型为 `text-embedding-3-large`，3072 维，`retrieval_mode=hybrid`、`is_fallback=false`、`access_mode=read_only`，命中 `GB50013-2018 7.1.8` 等来源。
- 同一运行容器执行真实 CLI Agentic 问答，问题为“根据入库规范，给水管网的环状布置有什么要求？请检索并给出可追溯引用。”23.686 秒收口为 `succeeded`，声明 5 个可追溯条款，errors／warnings 为空，无引用回流。会话和 trace 使用 `/tmp/wa-embedding-acceptance/`，没有读写用户 Web 会话。
- 此单样本验证生产装配、检索、模型工具调用与引用校验，不证明回答已通过专家审查，不覆盖 Web SSE 或代表性 p95 时延。
- 首次切换的挂载校验断言未通过，触发自动回滚；旧 embedding 凭据状态、旧索引配置与服务就绪均恢复。校验补充 Docker Desktop 的 `/host_mnt` 路径映射后再次切换成功。失败时未保留挂载 Source 的实际值，不能仅凭后续成功唯一确定失败原因；首次阶段输出、恢复后的检查边界与部署摘要哈希已保存在 JSON 的 `rollback_observation` 和 `raw_evidence_sha256`。

回滚材料位于宿主机 `.claude/embedding-rollback-20261104/`；原 embedding 槽位状态保存在独立凭据卷的 `operator-rollback-20261104-embedding/`，权限为目录 0700／文件 0600。回滚时先停止 app，恢复该槽位（本次切换前不存在，故应清除），再使用原两层配置加 `compose.rollback.yml` 启动 app，确认健康与原检索模式。保持先停服务再切 Key／地址，避免向不同服务商发送凭据。原基础 Compose 与 MCP overlay 字节摘要已保存并核对。

## 后续边界

现用 embedding 基础设施已可用，原百炼 401 不再阻塞这套新组合的运行。原基线的同条件质量对比、官方默认对话路由（D7）、代表性思考负载时延（D8）仍待补齐；不因一次检索改进或问答 smoke 把 S16 标为可开工。下一步应对选定组合独立进行真实 RAG 质量评测，并保留模式、语料、模型及索引标识。

## 工程回归

`bash scripts/ci_gate.sh` 本次退出码为 0：pytest、ruff、mypy、全部机制层 harness 与入仓基线对比通过。54 条配对报告另按生产指标函数复算一致，数据集摘要与 8 份原始证据摘要核对通过。独立审查未发现阻塞问题，回滚证据留存建议已补齐。本次入仓变更只有证据文档和交叉链接；运行时源码、评测 gold 与门禁基线未改动。
