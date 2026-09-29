# P5-S49 决策记录：对话与检查链路修复 + 显式附件读取

日期：2026-09-29　状态：已交付（本地 compose + 真实 DeepSeek 端到端实测）

## 触发

本地 compose 实测截图：第 1 轮问"能读 DXF 吗"，第 2 轮问"能读 CAD 设计图的 PDF 并提建议吗"。界面只剩一个回答，内容是写给校验器的话（"说明：本轮我**未引用任何规范条款**…结论重申：…我没有文件解析能力"），而且 `**` 原样显示。

## 根因（逐条对应修复）

| # | 根因 | 修复 |
|---|---|---|
| 1 | `_extract_validation_signal` 扫描整条 thread：上一轮的检索结果让本轮被判成"已检索却零引用"，回流文案也选错 | 新增 `current_turn_messages()`（graph 层唯一实现，`RunManager._current_turn` 改为调用它）。"本轮是否检索"和 `cited_ids` 只看本轮；引用合法性用的 `context_ids` 仍跨轮累积 |
| 2 | 零引用只有 `_declares_no_basis` 一条豁免，能力询问、寒暄一律强制回流 | `_is_non_normative_answer`：本轮未检索，且回答里没有规范号、条款号、带工程单位的数值，才豁免；同时写入 `warnings: no_citation_non_normative`，让豁免可见、可统计 |
| 3 | 回流后模型是在回复校验器，于是写出"本轮…结论重申" | 所有回流反馈追加 `_USER_FACING_SUFFIX`，要求直接给出面向用户的回答 |
| 4 | 消息体只有 `{message}`；召回只认字面重合，"总结这份文件"命中 0 条；系统提示从未提过上传渠道 | 消息新增 `attachments: [upload_id]`（≤5）；`documents/attachments.py` 按原文顺序读取点名的附件（先读开头，再补与问题相关的块，未读全时明确声明）；新增 `file_context` 提示段（文件能力说明 + 上传清单 + 附件正文） |
| 5 | 前端发第二问时 `setRun(emptyRunState())`，上一轮回答随之消失；切换会话会清空；没有历史接口 | `archiveRun` 先把上一轮归档进对话；新增 `GET /api/sessions/{id}/messages`（先校验归属再读 checkpointer，只投影问题和最终回答，并**重新过一遍输出护栏**） |
| 6 | 回答按纯文本渲染；有回答时隐藏 errors | `react-markdown` + `remark-gfm`，加 `skipHtml`，不渲染原始 HTML；失败的 run 即使有回答也显示"未通过校验"横幅，并加醒目边框 |

## 边界与纪律

- **豁免判定是确定性正则，不是意图分类器。** 它刻意保守：只要出现 `GB…`、`x.y.z`、`第…条`、`DN/De`，或"数字 + m/s、MPa、%、mm…"，就视为规范性结论，仍须引用（铁律 2）。模型凭记忆说"流速不大于 2 m/s"，仍然会被回流。
- `file_context` **只在 Web 路径渲染**：`RunManager` 只给声明了 `supports_attachment_context` 的会话传入。CLI、harness 和各评测集的提示词不变，基线不漂。
- 能力说明**由运行时推导**：DXF 一项取决于 `read_drawing` 是否已注册，S50 接通后文案会自动变化。
- 附件与召回使用同一条隔离线：`UploadRecallHit` 放进 `RETRIEVED_FILE` 信封，suspicious 照常标注；SSE 的 `attachment_read` 事件只带 id 和计数，不带正文。
- 点名的附件读不到时（不可见、数据库不可达），run 记为 **failed**（`attachment unavailable`），不静默降级为"不带附件作答"——那正是截图里的事故形态。
- 没有新增模型可调用工具（schema headroom 仍为 130）。
- `PROMPT_TEMPLATE_VERSION` 改为 `p5-step49-file-context-2026-09-29`。

## 实测（本地 compose，真实 DeepSeek，2026-09-29）

1. "能读 DXF 吗" → `succeeded`，warning 为 `no_citation_non_normative`。回答如实说明网页端读不了 DXF，列出能读的格式，并提示扫描件和矢量 PDF 没有文字层。
2. "能读 CAD 设计图的 PDF 吗" → `succeeded`。回答区分了有文字层和无文字层两种情况，并给出"试着复制一段文字"的自检方法。
3. 前两轮在历史接口和界面上都保留了下来；三轮全文中"本轮 / 结论重申 / 未引用任何规范"出现 **0 次**。
4. 附上 `design-note.txt`，问"总结给水分区情况" → 回答正确读出了附件内容（18 层、54.6 m、0.25 MPa、1–3 层直供、4 层以上变频加压），**但 run 为 `failed`**：模型检索后在正文里直接引用了 GB50013-2018 7.4.16 等条文，却两次都没有调用 `cite_clauses`。校验器按铁律 2 判为失败是对的；界面现在会把这条回答标为"未通过校验"，而不是当成普通回答显示。

## 残余

- **第 4 条是模型行为问题**：正文写了条款号，却不走 `cite_clauses` 声明。这不是本步引入的，但附件场景会更常触发，因为模型会主动检索做对照。后续可以考虑：发现正文里有条款号、但没有声明时，给出专门的回流文案。
- 历史接口只返回问题和最终回答，不回放每轮的工具卡片、引用、计算结果；只有最新一轮通过 SSE 完整回放。
- 显式附件在会话里只作用于"随它一起发出的那条消息"；后续追问需要再次附上，或者依靠关键词召回。
- 多标签页之间的历史不会实时同步。
