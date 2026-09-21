# 登录与模型配置快速指南

## 启动

```bash
# 终端 1：后端
cd "/Users/yaerken/Desktop/给排水agent"
docker compose up -d

# 终端 2：登录页（临时，见文末「遗留项」）
bash serve_login.sh
```

## 登录

浏览器打开 **http://127.0.0.1:8001/login.html**

- 用户名已填好：`admin`
- 密码在 `secrets/admin_password`，取用：`cat secrets/admin_password | pbcopy`

登录成功会跳到 http://127.0.0.1:8000/ 工作台。

## 配置模型密钥

不配密钥时提问会被诚实拒绝（424 `WEB_MODEL_NOT_CONFIGURED`），不会拿假答案糊过去。

在工作台点右上角 **⚙** → 设置面板：

| 凭据 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | 对话生成 |
| `DASHSCOPE_API_KEY` | 向量检索与重排 |

填入后保存，**立即生效、无需重启**；重启后依然有效。保存成功的标志是卡片状态
转绿、来源显示「本面板写入」，且 topbar 的 `model` 由 `mock / 未配置` 变为
`real model`。

密钥只写不读：面板与所有接口都只返回「是否已配置、来自哪一层、何时更新」，
不回传密钥内容。想确认填对了，回会话发一条消息。

写入需要管理员账号；普通成员能看到配置状态，以免撞上 424 却不知缘由。

其他配置途径（环境变量、`secrets/` 文件）及三层优先级见 `secrets/README.md`。

## 常见错误

### 网络错误 `Failed to fetch`

直接用 `file://` 打开了 login.html。改用 `bash serve_login.sh` 后经
http://127.0.0.1:8001/login.html 访问。

### 401 `AUTH_SESSION_INVALID`

未登录或会话过期，重新登录。

### 403 `AUTH_CSRF_INVALID`

前端已自动处理 CSRF（`workspaceApi.ts` 的 `csrfToken()` 读 `wa_csrf` cookie 并
回传 `X-CSRF-Token`），正常使用界面不会遇到。若自行用 curl 调写接口才需手动带上。

### 403 `AUTH_FORBIDDEN`（保存密钥时）

当前账号不是管理员。

### 503 `WEB_CREDENTIAL_STORE_UNAVAILABLE`（保存密钥时）

凭据卷没挂上，见 `WEB_LOGIN_GUIDE.md` 的「问题 5」。

## 技术细节

- **CSRF**：double-submit cookie。`wa_session` 为 HttpOnly（浏览器自动发送）；
  `wa_csrf` 非 HttpOnly，供前端读取后放进 `X-CSRF-Token`。GET 不需要。
- **密钥解析**：环境变量 > `{ENV}_FILE`（Compose secret） > 凭据卷（设置面板写入）。
  面板写入后会注入进程环境，因此 `embedding.py` 与 `retrieval/rerank.py` 无需改动
  即可读到；重启由 `create_app()` 回填恢复。

## 遗留项

登录页目前还是独立的 `login.html`，靠 8001 端口提供，样式也与深色工作台不一致
（浅色卡片）。下一轮会把它并入 React 应用，届时同源访问，`serve_login.sh`、
8001 端口与后端的 `CORSMiddleware` 一并删除。
