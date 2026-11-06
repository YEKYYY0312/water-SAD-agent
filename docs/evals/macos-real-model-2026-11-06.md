# Mac 独立 App 真实模型接入与使用 smoke（2026-11-06）

**模型接入已落地并安装，业务验收部分通过，不能宣布三项闭环全部完成。** 规范问答与同会话追问 succeeded；计算工具返回 1.132 m/s，但该轮被既有零引用规则判为 failed。另有首次凭据转移未禁用 Docker 日志驱动的审计缺口，安全要求未完全验收。原结果、问题、工具参数、来源、SSE 和 trace 全部保留，没有重跑业务失败筛选成功结果。结构化记录见 [脱敏 JSON](macos-real-model-2026-11-06.json)。本记录不是统计质量基线，不引用历史质量百分比作为 App 成绩。

## 源码与本地安装

- 工作区：`~/.codex/worktrees/water-agent-ci-recovery/给排水agent`，继续使用 `codex/macos-standalone-app`，原有五个桌面提交全部保留，没有从 main 重开空分支，也没有纳入桌面原始检出的用户改动。
- 接线提交 `beca651081176a6198db8facfdb945529f0d3f95`；最终程序提交 `9523739e5a48113c8d4bbbe50dc298a418d9fd40`。包清单为干净源码快照，末尾证据文档提交不改变程序资源。
- 已安装：`~/Applications/给排水 Agent.app`，从此实际路径打开；数据继续位于 `~/Library/Application Support/WaterAgent/`。
- DMG：`~/Desktop/给排水Agent-独立版-arm64-2026-11-06.dmg`，235551258 bytes，SHA256 `b2f99f33a3fbd9dd312a1a20310d913e927eae1a9b49ddde7a0b739042b46395`。
- 原始程序回滚包：`work/macos-real-model-2026-11-06/rollback-e27440b.app`；问答时的中间程序另存 `rollback-beca651.app`。更新先退出本 App 自有进程，再替换程序目录，没有删除用户数据。回滚须先退出当前 App、保留当前包，再将旧程序复制回安装路径；本轮无 schema 变更，未来涉及迁移时不得据此承诺可直接降级。

真实三问执行于接线包 `beca651`；发现原生确认弹窗缺口后再次打包安装。最终包与三问执行包的 **314 个 backend 资源逐项摘要相同**，只增加原生确认接线及策略检查。最终包实测恢复原有问答历史，且通过设置页再次完成真实兼容性检查；没有重跑三问换取通过结果。

## 实际配置、凭据和索引

| 项目 | 本次配置与核验 |
|---|---|
| Chat Base URL | `https://llm-proxy.learnings.ai/v1` |
| Chat 模型 | `deepseek-v4.1-flash`；真实响应 metadata 与 usage 一致 |
| Embedding Base URL | `https://llm-proxy.learnings.ai/v1` |
| Embedding 模型／维度 | `text-embedding-3-large`／3072，维度从真实索引读取 |
| 检索／重排 | hybrid／local，recall_top_k=20，hybrid_alpha=0.7 |
| 实际 retrieve_clauses.top_k | 第一问 8、8；追问 8、6；计算未检索，不能称固定 top-5 |

桌面启动器原本会清空宿主环境，只透传 OS 必需项与 App 独立路径。本次聊天继续经设置页写入 `credentials/models/llm_endpoint.json`，生产解析器报告 `source=panel`、`editable=true`。没有给聊天端点注入运维环境变量锁死面板，也未继承宿主机 Key、代理或数据库地址。

embedding 没有现成地址／模型设置页，故仅增加 `config/embedding.json` 三字段适配：`base_url`、`model`、`retrieval_mode` 转为既有生产环境输入，重启本 App 生效；不接受 Key、维度或任意环境变量。生产端点解析器、凭据优先级和索引绑定仍是唯一来源。错误配置不静默回到默认地址；新增八项回归先红后绿。生产代码与接线说明见 `desktop/macos/runtime/launcher.py`、`desktop/macos/README.md`。

源凭据卷 `water-agent_app_credentials` 通过独立、无网络、只读根文件系统的临时容器 **只读挂载**，先经生产解析器核对聊天／embedding 都属于上述站点，再读取两个专用槽位，经生产 `write_credential` 写入 App 专用凭据目录。没有读取或发送官方 DeepSeek／百炼槽位；Key 未放入命令行、仓库、包或报告。复查目录 0700、凭据及配置文件 0600。没有修改或重启旧 8000／8001 服务。

**凭据转移审计缺口**：首次通过捕获 stdout 的内存管道转移时，没有显式设置 `--log-driver none`，而 Docker 默认是 `json-file`，因此存在把凭据写入短暂容器 daemon 日志的路径。该 `--rm` 容器已删除，没有留存日志可进一步核查；不能宣称“全程没有进入普通日志”。复核后使用 `--log-driver none`、无网络、只读源卷重新读取、核对并经生产写入器写入相同值；App／报告／原始留存证据的实际密钥值扫描为零命中。这不抹去首次缺口，也没有擅自轮换两套旧服务共用的 Key；需要轮换时应另行协调旧服务配置。凭据安全验收保持 incomplete。

索引沿用 `~/Desktop/给排水agent/data/indexes/text-embedding-3-large__3072__d56c60259633__20261104/`。发布目录、安装包内副本以及前次入仓记录的模型／维度与逐文件摘要核对；10 个索引文件加 `artifact.json` 共 11 个文件，字节一致，未重建、未改向量。索引元数据含 `embedding_model=text-embedding-3-large`、`dimensions=3072`、`embedding_host=llm-proxy.learnings.ai`。

实际 SSE 与 trace 均为 hybrid／forced；真实 usage 含四次该 embedding 模型请求。另用最终包的生产工厂在相同隔离环境中做**不发请求的装配诊断**，读到 `is_fallback=false`、`fallback_kind=none`、`db_opened=true`、`access_mode=read_only`、`text_index_source=persisted`。该诊断不是读取运行中私有对象；结合真实来源、请求用量及强制 hybrid 的生产实现核对无 fixture 回落。客户端构造诊断也确认同步／异步 chat client 的地址均为该中转站，embedding URL 为该站 `/embeddings`。未进行 TLS 解密或推断中转站底层模型权重。

## 兼容性门槛与必要集成修复

首次通过 App 原有管理员＋session＋CSRF API 发起生产兼容性探针，七项全部 passed，报告由原探针写入专用凭据目录，没有手写报告或扩大免检条件。

原生窗口随后复现设置页按钮无动作：React 的 `window.confirm` 没有对应的 WKUIDelegate 实现，因此没有发起探针。按 [Apple 的确认弹窗委托接口](https://developer.apple.com/documentation/webkit/wkuidelegate/webview%28_%3Arunjavascriptconfirmpanelwithmessage%3Ainitiatedbyframe%3Acompletionhandler%3A%29) 补接原生 sheet，仅当前服务同源主框架可发起，默认取消，仅明确点“继续”返回 true。五项新增策略断言覆盖主框架、子框架、旧端口、外站及未知来源。

最终安装包原生实测：点检查出现计费提示；取消后原报告摘要不变；再次明确继续后显示“检查进行中”，最终显示 `compatible`。第二份真实报告时间 `2026-11-06T10:09:44+00:00`，function_calling、structured_citations、programmatic_calculation、tool_schema_budget、streaming_usage、error_handling、cancellation_timeout 七项均 passed。第二次用于验证原生集成修复，不是业务失败重试。

## 三项实际业务结果

| case | 原结果 | trace |
|---|---|---|
| “根据入库规范，新建地区的排水系统应采用何种排水体制？请检索并给出可追溯引用。” | succeeded，回答除降雨量少的干旱地区外采用分流制 | `3314b6faaa5243069c0f03edbde5ab4b` |
| 同会话追问“你刚才提到的‘降雨量少的干旱地区’例外，条文说明是如何解释的？……” | succeeded，衔接上一轮并区分正文与条文说明 | `f25adea04f344db49551bf597fc185bf` |
| “DN150、Q=20L/s，求流速。此次验收明确按内径150mm、满管计算，请调用程序化计算工具并注明这个前提。” | 工具 succeeded，整轮 failed；原文保留且窗口标明未通过校验 | `debf10c84d254cae991cab193818bda2` |

前两问均声明 `GB50014-2021 3.1.2` 与 `GB50318-2017 3.3.2`，两条都属于本轮返回来源，也属于线程累计来源。原始检索查询、每次 top_k、全部返回 source_id 和声明列表保存在 JSON。追问再次检索，因此本样本证明历史连续与来源 membership，不证明“完全不检索复用上一轮引用”。membership 也不等于专家对每句语义的验收；第一问正文还提到 `5.4.2`、`5.1.14`，没有单独通过 cite 工具登记，不将其当作本次额外已校验声明。

计算实际调用：

```json
{"name":"calc_velocity","args":{"diameter_mm":150,"flow_lps":20,"scenario":"water_supply"}}
```

程序返回 `{"velocity_mps":1.132,"assumptions":[]}`，SSE 明确 `programmatic=true`。数值来自工具，没有用 LLM 手算替代。`scenario=water_supply` 是模型补入、用户没有提供用途，本次不接受其附加场景区间评论作为工程结论。原有校验器因带单位数值而要求引用，回流一次后仍无 cite，故整轮失败；不是鉴权／网络失败，不包装成 SKIP 或成功，也未改引用校验、计算验证或 Prompt。

另保留一次操作失误：native `typeText` 未输入中文，服务实际收到“，？。”，trace `4ec773a4ee59413db4f7ea2177bbf5d5`，failed。其问题、回答与用量独立记录；之后改用粘贴并在发送前核对完整文本。该失误不混入三项指定 case，也没有删除失败。

## 窗口、草稿、Word 与重启

- 原生窗口见到两份成功最终回答及计算失败标记。实际 SSE 中规范问答／追问有 23／29 个 draft_delta；计算有 38 个 draft_delta、两次 draft_retract。offset 连续，失败以 finalize.failed 收口，已关闭草稿通道。证据是实际 SSE 回放与前端测试，未逐帧独立给每个瞬态草稿做视觉评分。
- 追问的真实导出 API 返回 200，得到 `新工程会话-b5ff3a83.docx`（39801 bytes，SHA256 `071eb381437787c0408e69a0ccc14e11fb27719c7087a4c21c625c960429eedd`），本机 WPS 成功打开、中文可见。正文仍保留原 Markdown 标记，属于现有渲染器限制。本次下载使用正常鉴权 HTTP API；**没有独立完成该真实回答的 WKDownload 原生保存面板闭环**，不以策略断言冒充窗口实测。
- 退出并从实际安装路径重新打开后，无登录或初始密码界面，直接工作台；3 个会话和运行状态完全相同，目标线程6条问题／回答历史保留，计算失败仍显示“未通过校验”。聊天面板可编辑，模型已配置指示、embedding 配置与凭据保留。导出重启后重新生成仍受既有进程内快照限制，本次仅确认已下载文件保留且可打开。

## 用量与工程验证

三项指定 case 加输入失误：chat 有 11 条 usage known 记录，已知输入 48588、输出 5449 token；embedding 有 4 条 known，输入 96 token，输出0；这些记录 unknown=0、retry_count=0。**两次兼容性探针的逐请求用量没有被既有报告保留，单列未知，不能把上述数字称作全部计费用量。** 代理价格未知，费用为 null；不采用 trace 中旧 llm_call 估价。

已验证：前端148/148与构建；桌面 Python 88项；原生 PolicyChecks 81条断言与 release build；额外 Web／导出／授权测试98 passed、1 skipped（缺 P5_TEST_DATABASE_URL）；首次及最终源码完整 ci_gate 均 exit0（pytest、ruff、mypy、全部机制层 harness 与入仓基线比较）。`git diff --check` 通过。最终包扫描、签名及 DMG复核均通过，证据见 JSON 的 verification。本机缺 XCTest，不宣称 XCTest 通过。独立打包 smoke 使用隔离的真实 PostgreSQL 和脚本模型，22项通过，不替代上面的真实模型使用结果。

原始 trace、SSE、checkpoint 白名单投影、配置／索引核对、构建／测试日志、导出及回滚包位于受忽略的 `work/macos-real-model-2026-11-06/`。不含 Key 或 session cookie；正式 JSON 只记录专用验收问题、工具／来源、状态与证据摘要。DMG 按只读方式挂载后再做包扫描及 deep strict 签名检查，清单与安装目录相同；签名为 arm64 本机 ad-hoc，没有上架、公证或公开部署。

下一项最小修复：单独定义“有成功程序化计算证据、数值严格来自工具、无未引用规范结论”的零引用收口契约，先锁定本次失败回归，再评审校验器改动。本轮按范围约束不实施；同时保留原 top_k 偏差、Word原生保存面板与逐句引用质量边界，不以本次 smoke 解除其他阶段阻塞。
