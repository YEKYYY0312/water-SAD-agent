# P5 Step 41 决策记录：全文召回、确定性排序与 Prompt 预算

## 结论

Step 41 使用 PostgreSQL 内建全文检索，不调用远程 embedding、pgvector 或第三方 Memory MCP。
生产查询使用 `to_tsvector('simple', coalesce(content, ''))` 与 `plainto_tsquery('simple', :query)`，
并由 `0003_step41_memory_fts` 创建 `memory_records` 的 GIN 表达式索引。query 只经过 NFKC 和空白
折叠，始终作为绑定参数传入；调用方文本不会被当作可执行 tsquery。

召回 SQL 在全文匹配前硬过滤 tenant、服务端传入的已认证用户、活动用户、当前项目 membership、
`active`、非 tombstone 和 `expires_at`。user scope 只允许本人；project scope 只允许当前项目且调用者
有 membership；未提供当前项目时不召回项目记录。排序固定为 user/project scope、全文/字面相关度、
kind 优先级、`updated_at` 倒序、`memory_id` 升序。

## 中文分词边界

PostgreSQL `simple` 配置不是中文语义分词器。实现保留 FTS 作为生产主路径，并对包含 CJK 字符的 query
增加绑定参数驱动的字面关键词匹配，保证连续中文短语和空白分隔关键词有可复现的机制层行为。这个
补充策略不是同义词、向量或语义召回；中文的自然语言切词、同义表达和跨词改写仍不作能力承诺。
真实 PostgreSQL 中文效果由 `tests/test_memory_recall.py::test_postgres_chinese_recall_is_verified_when_postgres_is_configured`
验证；没有 `P5_TEST_DATABASE_URL` 时按项目约定结构化跳过。

## 上下文与观测

每条命中通过 `ContentEnvelope(source_tag=SourceTag.SESSION_MEMORY, redacted=True)` 进入独立的
`memory_context` 段。段落明确声明记忆是用户保存的数据而非系统指令，并位于系统规则之后、当前用户
消息之前；最多 8 条、1200 字符，按完整记忆边界保留，省略数写入结果和 trace。记忆正文不进入
`ROLE_AND_PRINCIPLES`，也不改变工具、权限、引用、计算或审批。

recall trace 只写 query/content hash、memory ID、kind、scope、score、注入标记和预算计数；正文、
semantic key 和 query 原文不写入 trace。SQLite 分支仅是现有测试替身，不代表 SQLite 提供生产 FTS。
