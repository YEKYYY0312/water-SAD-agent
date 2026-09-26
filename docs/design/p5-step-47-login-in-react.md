# Spec: P5-S47 登录并入 React 工作台

> 状态：待用户确认。本 Step 只交付前端登录闭环 + 拆除 8001 临时脚手架的最小切片，不改动任何认证语义（密码校验、会话、CSRF、限流、审计全部复用现有 `webapp/auth/`）。

## Objective

新部署用户打开 `http://127.0.0.1:8000/` 即可登录并进入工作台。消灭「独立 login.html + serve_login.sh + 8001 端口 + CORSMiddleware 开发例外」这套临时脚手架。

## Problem（实测证据）

- React 前端无任何登录界面：`main.tsx` 直接渲染 `Workbench`，全部 `.tsx/.ts` 中 grep `login|登录` 零命中。
- 未登录打开工作台：`GET /api/projects` 等返回 `401 AUTH_SESSION_INVALID`，`useWorkspaceController` 的 catch 把它渲染成一句通用 `pageError`，**没有任何"请先登录"的提示或入口**。
- 设置面板同样：`GET /api/settings/credentials` 401 → `SettingsPanel` 的 rows 为空 → 一个输入框都不渲染，只剩一块红色错误。
- 登录要走 `bash serve_login.sh` 起的 8001 端口独立 `login.html`，跨端口 cookie 还要求两端都用 `127.0.0.1`（一个 localhost 一个 127.0.0.1 就失败）。这对新部署用户是不可发现的死路。

## Decisions

### D1 认证判定：新增 `/api/auth/status`，不复用 `/api/auth/me`

`/api/auth/me` 挂在 `require_password_changed` 上：`must_change_password=true` 的用户拿到 **403**，无法区分「未登录」与「已登录但须改密」。前端路由需要一个无条件的三态判定。

新增 `GET /api/auth/status`（不依赖任何 guard，只读会话 cookie）：

```json
// 未登录
{"ok": true, "data": {"authenticated": false}}
// 已登录
{"ok": true, "data": {"authenticated": true, "user": {...safe_dict}, "must_change_password": false}}
```

- 始终 200：`authenticated` 字段承载三态，错误语义不借用 HTTP 状态码（401/403 已被占用且含义不同）。
- 不泄漏：未登录不返回任何用户字段；已登录只返回 `safe_dict()`。
- 不计入登录限流：这是只读探针，`limiter` 只在 POST /login 路径工作。

### D2 前端登录态门：Workbench 之前插一层 AuthGate

`main.tsx` 渲染 `AuthGate` 而非直接渲染 `Workbench`：

```
AuthGate
  ├─ status loading      → 全屏 loading（不打断现有视觉）
  ├─ authenticated=false → <LoginPanel />（登录成功后再拉一次 status，直接进工作台，不刷新页面）
  ├─ must_change_password=true → <ChangePasswordPanel />（强制改密后才进工作台）
  └─ authenticated=true  → <Workbench />
```

- 不引入 react-router：两个面板是模态状态，不是 URL 路由（单页工作台哲学一致）。
- 登录/改密成功后调用 `onAuthed()` 重新拉 `/api/auth/status`，状态提升在 AuthGate 内，`Workbench` 的既有数据流（projects/sessions/capabilities）完全不动。
- `Workbench` 顶部 topbar 新增「退出」按钮（调 `POST /api/auth/logout` + 清本地选中态 + 回登录面板）。

### D3 LoginPanel 行为契约

- 字段：用户名（默认 `admin`）、密码（placeholder 提示 `见 secrets/admin_password`）。
- 提交 → `POST /api/auth/login`（同源，`credentials: "same-origin"`，既有 `workspaceApi.request` 已带 CSRF header 逻辑，login 是 POST 且无 cookie 时无 token 可带——登录本就不需要 CSRF token，因为尚无会话）。
- 失败渲染后端 `error.code + error.message`（`AUTH_INVALID_CREDENTIALS` / `AUTH_RATE_LIMITED` / `AUTH_USER_INACTIVE`），不做前端重写文案。
- 成功但 `must_change_password=true` → 切到改密面板（见 D4）。
- 加载中禁用按钮（沿用 login.html 既有交互）。

### D4 ChangePasswordPanel 行为契约（must_change_password 分支）

- 三个字段：当前密码、新密码、确认新密码（前端只做「两次一致」与「非空」检查；强度由后端 `AUTH_WEAK_PASSWORD` 裁定）。
- 提交 → `POST /api/auth/change-pass`（需要 CSRF：登录响应已种下 `wa_csrf` cookie，`workspaceApi.request` 自动带上）。
- 成功后：后端不改会话（现有实现只更新 hash），前端直接视为可进入工作台（再拉一次 status，`must_change_password` 已 false）。
- 失败渲染后端 code/message。

### D5 设置面板与全局 pageError 的 401 文案

`SettingsPanel` 与 `useWorkspaceController` 的 catch 分支：当 `WorkspaceApiError.status === 401` 时，文案从通用「请求未完成」改为「会话已失效，请重新登录」+ 一个「返回登录」按钮（点击后清空本地状态并回 AuthGate 登录面板）。

这是兜底：正常流程下 AuthGate 已经挡住了未登录用户；这里只覆盖「会话中途过期」的场景。

### D6 拆除临时脚手架

- 删除 `login.html`、`serve_login.sh`、`LOGIN_QUICKSTART.md`、`WEB_LOGIN_GUIDE.md`（用户已拍板直接删，信息由 BEGINNER_GUIDE 覆盖）。
- `app.py` **整体删除 CORSMiddleware**（用户已拍板）：同源部署下浏览器根本不发 CORS 预检，四条 origin 全是历史脚手架残留。vite dev 走 proxy 转发同样不需要。
- 更新文档：`BEGINNER_GUIDE.md`、`STARTUP_GUIDE.md` 中所有「8001 / serve_login.sh」路径改为「直接打开 8000」。`diagnose_web.sh` 中 8001 检查项删除。
- `AGENTS.md` 增补一条完成记录（沿用既有格式：✅ + 日期 + 实测证据 + 残余）。

## Explicit non-goals

- 不改 `webapp/auth/` 任何后端逻辑（密码哈希、会话存储、限流、审计、CSRF 中间件原样保留）。
- 不做注册界面（公网注册本就 404 禁用，管理员创建用户走 `/api/admin/users`）。
- 不做「记住我」、token 刷新、多 tab 会话同步。
- 不动 `compose.yml` / Dockerfile（前端构建产物已是镜像内 COPY 的 dist，本 Step 改源码后经既有 build 链路生效）。

## Testing

### 前端单测（`web/app/tests/auth.test.mjs`，node --test，沿用现有风格）

- `AuthGate` 状态机：loading → login / change-password / workbench 三态迁移（mock fetch）。
- `LoginPanel`：失败渲染后端 code；成功后 `must_change_password` 两分支。
- `ChangePasswordPanel`：两次输入不一致的前端校验；`AUTH_WEAK_PASSWORD` 渲染。
- 设置面板 401 分支文案与「返回登录」回调。

### 后端 pytest（`tests/test_web_auth.py` 增补）

- `GET /api/auth/status`：无 cookie → 200 `authenticated=false`；有效 cookie → 200 `authenticated=true` + safe_dict；`must_change_password=true` 用户 → 200 且字段为 true（**不被 403 拦**）。
- 篡改 cookie → 200 `authenticated=false`（不是 401/500）。
- status 不消耗登录限流额度（连续 N 次 status 后 login 仍不受限）。

### 冒烟

- `webapp/smoke.py` 新增一项：`GET /` 返回的 index.html 引用的 bundle 内含登录面板标记（字符串断言，与 settings-toggle 同法）。
- 手工实测路径（进完成报告）：无 cookie 打开 8000 → 见登录面板 → 错误密码 → AUTH_INVALID_CREDENTIALS → 正确密码 → 进工作台 → 设置面板可填密钥 → 退出 → 回登录面板。
- `grep -rn "8001" web/app/src src/water_agent/webapp` 零命中（除历史注释）。

### 回归

- `bash scripts/ci_gate.sh` 全绿（含 web pytest 全套 + 8 个 harness + baseline 对比）。
- `web/app` 下 `npm run test` 与 `npm run typecheck` 全绿。

## Files

新增：
- `web/app/src/AuthGate.tsx`（门 + 状态机）
- `web/app/src/LoginPanel.tsx`
- `web/app/src/ChangePasswordPanel.tsx`
- `web/app/src/authApi.ts`（status/login/logout/change-pass 四个调用，复用 workspaceApi 的 envelope/CSRF 工具）
- `web/app/src/authState.ts`（纯函数：文案映射、表单校验，供 node --test 单测）
- `web/app/tests/auth.test.mjs`

修改：
- `web/app/src/main.tsx`（AuthGate 替换直接渲染 Workbench）
- `web/app/src/Workbench.tsx`（topbar 加退出按钮 + 接收 onLogout 回调）
- `web/app/src/useWorkspaceController.ts`（401 catch 分支暴露 `authExpired` 信号）
- `web/app/src/SettingsPanel.tsx`（401 分支文案 + 返回登录按钮）
- `src/water_agent/webapp/auth/routes.py`（新增 `/api/auth/status`）
- `src/water_agent/webapp/app.py`（删 8001 两条 CORS origin）
- `src/water_agent/webapp/smoke.py`（新增 bundle 标记断言）
- `tests/test_web_auth.py`（status 契约用例）
- `BEGINNER_GUIDE.md` / `STARTUP_GUIDE.md` / `LOGIN_QUICKSTART.md` / `WEB_LOGIN_GUIDE.md` / `diagnose_web.sh`（路径更正）
- `AGENTS.md`（完成记录）

删除：
- `login.html`
- `serve_login.sh`

## Risks

- **R1 会话中途过期体验**：D5 只改文案 + 手动返回按钮，不做自动跳转（自动跳转会把用户正在输入的内容弄丢）。接受。
- **R2 must_change_password 用户的 status 契约**：新端点必须绕过 `require_password_changed`，否则该分支永远进不了改密面板。有专门测试锁死。
- **R3 删 login.html 是破坏兼容**：任何还指着 8001 的肌肉记忆/外部文档会 404。缓解：README 与两份指南同步改；`diagnose_web.sh` 对 8001 的检查改为「确认 8001 不再被需要」的说明。
