# 设置页副栏分页与「获取模型」（P7-S10 之后的 UI 改造）

日期：2026-10-13 · 分支 `YEK/settings-subnav` · 不属于 P7 步骤序列（不适用 `p7-scope` 范围守卫）。

## 起因

S10 把对话端点（base_url + 模型）放进了设置页，但设置页仍是一个纵向长页面：凭据卡片、端点卡片、Skill 库、MCP 扩展依次堆叠。管理员找不到端点输入（且之前运行的栈是 S10 之前的旧镜像，根本没有这一块），Skill / MCP / 密钥没有各自的页面，填完地址与 Key 后模型名只能手敲。

## 做了什么

1. **副栏分页**（纯前端）：设置页左侧副栏 =「模型与密钥 / Skill 库 / MCP 扩展」，一次只渲染一页。`SettingsPanel.tsx` 仍是外壳（`Workbench` 不变）。非管理员不显示 MCP 项（以端点读接口的 `can_manage` 判定；角色未知时先显示，读到非管理员再收起）。窄屏副栏变顶部横排。
2. **模型页顺序**：接口地址 → API Key → 「获取模型」→ 模型下拉（含「其他（手动输入）」）→ 保存。Key 写进后端将要读取的槽位：地址仍是 `https://api.deepseek.com` 写 `DEEPSEEK_API_KEY`，否则写 `WATER_AGENT_LLM_API_KEY`（与 `generation/endpoints.py::_key_name` 一致）。先存端点再存 Key，端点校验失败不留孤立密钥。端点可编辑时，两把对话 Key 不再在下方「其他密钥」里重复出现。
3. **获取模型**：`POST /api/settings/llm-endpoint/models`（管理员 + CSRF），请求体 `{base_url, api_key?}`，用**未保存**的表单值；`api_key` 缺省则用服务端已存的有效 Key。响应只含模型 id。
4. **授权回调落点**：MCP 授权 303 回首页时地址栏带结果，`useWorkspaceController` 据此直接打开设置页、`SettingsPanel` 初始停在 MCP 页（否则结果提示没有地方显示）。
5. `SkillLibraryPanel` 的「查看 MCP 扩展」改为切换子页（`onOpenMcp`），不再 `scrollIntoView`。

## 「获取模型」的安全取舍

- **它是读取原语**：响应内容回到浏览器，所以不能像聊天调用那样只依赖 https 校验。出站走 `mcp/http_transport.open_exchange`——解析后整体分类全部应答（私有 / 回环 / 链路本地 / 云元数据 / CGNAT 任一命中即拒，零字节发出）、连接钉死校验过的 IP、不跟随重定向、响应字节上限 256 KiB。`PANEL_HTTP_HOSTS`（localhost / 127.0.0.1 / ::1 / local-model）是唯一允许内网目的地的例外，与面板本来就收的 http 主机一致。
- **不新增出站模块**：函数放在已登记的 `generation/chat_model.py`，经 `ProviderGateway.call`（新增 `CallCategory.MODEL_LIST`，provider 仍是 `DEEPSEEK_CHAT`，模型键 `model-list:<host>`，usage 记未知）。静态扫描的出站特征只命中 `http_transport.py` 与既有登记模块，`PROVIDER_CALL_SITES` 数量不变。HTTP 错误以不带状态属性的 `ModelListError` 抛出，所以不会被网关当作瞬时错误重试。
- **Key 处理**：表单里的 Key 只进 `Authorization` 头，不落盘、不进响应 / 审计 / 日志 / 异常文本。审计 `settings.llm_models_listed` 只记主机名、结果码与条数。
- **响应过滤**：只留符合 `validate_model_name` 字符集与长度的 id，去重保序，最多 200 条；兼容 OpenAI 形（`data[].id`）、Ollama 形（`models[].name`）与纯数组。
- **部署锁定时仍可用**：这是只读操作；保存仍被拒。
- 稳定错误码：`WEB_LLM_MODELS_UNAUTHORIZED` / `_UNSUPPORTED`（无 /models、非列表格式、空列表——提示手动输入）/ `_DESTINATION_FORBIDDEN` / `_UNREACHABLE`，输入非法沿用 `WEB_LLM_ENDPOINT_INVALID`。

## 验证

- 后端：`tests/test_web_llm_models.py` 21 条（权限 / CSRF、未保存 Key 与已存 Key、Key 不泄漏、上游 401 / 404 / 非 JSON、Ollama 形、部署锁定、非法 URL、五类内网目的地、DNS 应答整体分类、响应上限）；既有 `test_web_llm_endpoint` / `test_governance_contracts` / `test_vision_review` / `test_mcp_http_transport` 等 228 条无回退；ruff、mypy 干净。
- 前端：`npm test` 100/100（新增 `settings_nav.test.mjs`；`settings.test.mjs` 里"端点卡片没有密钥输入框"的断言按新设计改为"Key 只写、保存后清空、模型列表经服务端取"，`skills.test.mjs` 的滚动跳转断言改为子页切换），`npm run build` 通过。

## 残余

- 只与本地假 OpenAI 兼容服务器验证过（`fake_openai_server.py` 新增 `GET /models`）；**真实服务商的 `/models` 返回形态与鉴权未验证**（无 Key，blocked）。没有 `/models` 的端点退回手动输入。
- 浏览器手动验收未做（需要运行中的栈与管理员账号）。
- 仅覆盖对话端点；embedding 端点的地址 / 获取模型未做（结构相同，可照搬）。
- 管理员账号被盗即可让服务端向任意公网 https 地址发一次带 Key 的 GET（内网被拒）；与"面板能改对话端点"同级风险，已有审计。
