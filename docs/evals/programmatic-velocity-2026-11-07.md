# 纯流速计算零引用收口修复验收（2026-11-07）

**修复与本地使用闭环通过。** 最终安装包的真实纯计算请求仅执行一次 `calc_velocity`，返回 1.132 m/s；以确定性摘要、零引用、`succeeded` 收口，不再因单位数值而强制补检索。原生 Word 保存／打开及重启保留通过。这不是新的统计质量基线，不解除前次凭据转移日志审计缺口。

结构化结果和原始证据摘要见 [JSON](programmatic-velocity-2026-11-07.json)，契约见 [设计记录](../design/programmatic-velocity-closure.md)。

## 实现及安全边界

基础为桌面分支 `codex/macos-standalone-app` 的 `17db3eb`；实现提交 `d489757`，明示“仅计算”语法补充及最终程序提交 `819f3dcb59b292c60003d50106414005997c1787`。原桌面实现提交及个人免登录保留，未重开分支、未合并 main。

- 只有 canonical `calc_velocity_spec` 经 `ToolRegistry.invoke` 成功执行后，适配器才生成不发给模型的本地 artifact receipt。工具 schema／输出正文、公式和阈值不变；外部同名工具、正文伪造、失败返回都不能取得有效凭证。
- 只接受明确内径 mm、流量 L/s、满管流速及有限的计算表达。最长词语逐步消费，不用重叠重复正则；未知剩余内容、混合任务、DN而无内径、缺参、歧义和非满管不豁免。
- 仅本轮一个调用及对应一个成功结果；id、名称、原始参数、receipt和结果须一致。旧证据、额外调用／检索／引用、bool、非有限值及非空 assumptions 不适用。
- 不将 LLM 自由正文当作数值或规范权威：同 id 改写为仅含用户输入 D/Q 与程序结果 v 的固定摘要，并说明未做规范符合性判断。默认 `scenario=water_supply` 不被描述成用户确认的用途，也不把其区间输出为已核验规范限值。
- 输出护栏、授权和正常引用校验继续执行；最终正文与 checkpoint 同源，输出护栏仍可阻断。普通规范问答的零引用规则不放宽。没有改 Prompt、gold、切块、入仓基线、S16、计算公式或场景阈值。

方案与实现经独立代码审查。审查发现并修复了残余语法正则的指数回溯，以及 `:g` 显示丢失有效位；两项均先用测试复现再修复。最终审查无阻断，独立执行44项专项测试通过。

## 两个真实样本，分别记录

前次失败 `debf10c84d254cae991cab193818bda2` 保留于原 App 会话与[前次报告](macos-real-model-2026-11-06.md)，没有覆盖为成功。

| 本轮样本 | 结果与解释 | trace |
|---|---|---|
| 与前次相同的完整 DN150／内径150mm／Q20L/s 问题 | succeeded；模型额外执行两次检索并声明引用，走原有引用路径。**不算新零引用路径的通过证据**，原结果保留 | `7185b26fd2c24ab8a6b7518ebf3f26fb` |
| “内径150mm，Q=20L/s，求满管流速。仅计算流速，不检索条文，不做规范判定。” | succeeded；只调用一次计算工具，零检索／零声明引用，命中新路径 | `54c7d5cac188409c9b10ce2e9b1c9450` |

第二项是明示任务边界的独立诊断用例；允许语法仅补入“仅计算流速／不检索条文／不做规范判定”三个完整表达。追加消防泵选型等其他任务的反例仍拒绝。没有把首问重新标成零引用通过，也没有反复重跑筛选成功结果；没有额外 judge 或完整58问评测。

目标样本实际调用及程序返回：

```json
{"name":"calc_velocity","args":{"diameter_mm":150,"flow_lps":20,"scenario":"water_supply"}}
{"velocity_mps":1.132,"assumptions":[]}
```

真实 checkpoint 中存在该次执行的 receipt，最终 AIMessage 已替换为程序摘要，`water_agent_turn_status=succeeded`；SSE finalize 同文，citations为空、errors为空，warning为 `no_citation_programmatic_calculation`。模型自由正文不被采纳为最终数值说明。SSE有7个草稿增量，offset连续，最终正文在窗口正确显示并清除草稿；未对每个瞬态草稿作逐帧视觉评分。

## 模型、导出与持久化

沿用 `https://llm-proxy.learnings.ai/v1` 和 `deepseek-v4.1-flash`，既有兼容性证据仍为 verified。聊天仍来自面板且可编辑，mock_generator=false。embedding配置仍为 `text-embedding-3-large`／3072，hybrid、local rerank、recall_top_k=20、alpha=0.7；发布索引11个文件与包内副本逐一字节一致，未重建或替换向量。**目标纯计算用例没有检索／embedding请求**，不能将配置为hybrid说成新增检索质量验收。首问的实际检索参数及来源在JSON保留，不称固定top-5。

在原生 App 点“下载 Word”，系统保存面板成功保存 `~/Documents/新工程会话-e8629822.docx`，38172 bytes。本机 WPS 打开并显示输入150 mm、20 L/s和程序结果1.132 m/s；中文文件名正常。文件副本、内容提取及摘要在原始证据目录，**本轮补齐的是实际 WKDownload 保存面板流程**，不是仅靠API或策略断言。

退出再从实际安装路径打开后，直接进入工作台，无登录／初始密码页；5个会话及全部运行状态与退出前一致，目标两条问答历史逐字一致且保留 succeeded。已下载Word仍在；未声称重启后进程内导出快照仍可重新下载。

## 本地交付和验证

- 已安装 App：`~/Applications/给排水 Agent.app`，源码清单为干净的 `819f3dc`；用户数据未删除，凭据未迁移或轮换。
- 最终 DMG：`~/Desktop/给排水Agent-独立版-arm64-2026-11-07-计算收口修复.dmg`，225231998 bytes，SHA256 `c1611f440cdff986535838c6c39fe2a87a65193d0520635f7c6363184831aaf9`。
- 回滚包：`work/calc-closure-2026-11-06/rollback-d489757.app`；更早的 `rollback-9523739.app` 也保留。先退出自有进程再替换程序；本轮没有schema变更，未来迁移的降级语义不由此推定。
- 最终包扫描、`codesign --verify --deep --strict`通过。DMG只读挂载后复核，清单／Info.plist／原生可执行文件与安装路径相同；arm64、ad-hoc本地签名，无上架或公证声明。
- 专项44项、此前相关图／引用／SSE／导出切片139项通过；ruff通过、mypy277个源码文件通过；前端148项与构建、桌面Python88项、原生PolicyChecks81条断言通过。最终完整 `ci_gate.sh` 通过，包括pytest、ruff、mypy、全部机制harness及入仓基线对比；`git diff --check`通过。
- 最终包的隔离真实PostgreSQL机制smoke 22项通过；其中脚本模型仅验证打包机制，不替代上述真实问答。只有CommandLineTools，未声称运行XCTest。
- 旧8000／8001及其数据库容器的ID、镜像、启动时间、重启数前后完全一致。本轮没有访问源凭据卷；此前日志审计缺口仍按原报告保留，没有宣称修复或擦除。

两个真实样本合计：chat有6条known记录，已知输入20484、输出2042 token；embedding有2条known记录，输入43 token。目标零引用样本单独为chat两次、输入4442／输出422 token，embedding零次。记录内unknown=0、重试0；价格未知、费用null，不使用旧估价。原始trace／SSE／checkpoint白名单投影、构建／测试日志、Word和回滚包在受忽略的 `work/calc-closure-2026-11-06/`，JSON列出摘要。

限制：当前只覆盖有限语法的单次满管流速计算。仅有DN、其他水力公式、非空assumptions、自由表达或计算与设计任务混合仍沿用既有路径；不据此宣称所有计算回答或任意规范语义都已验收。UI当前直接显示诊断warning代码，这不影响计算结果与成功状态；一般引用路径中模型额外数值说明未在本轮扩大校验范围。
