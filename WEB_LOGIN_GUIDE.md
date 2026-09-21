# 给排水 Agent Web 界面使用指南

## 🌐 访问地址

**本地开发**: 
- 主界面（工作台 / 长期记忆 / 设置）：http://127.0.0.1:8000/
- 登录页：http://127.0.0.1:8001/login.html，需先 `bash serve_login.sh`

> 💡 登录页尚未并入 React 应用，仍是独立的 `login.html`，由 `serve_login.sh`
> 起一个只暴露该文件的临时服务器提供（**不要**直接 `python3 -m http.server`
> 于项目根目录——那会把 `secrets/`、`.env`、`.git/` 一并暴露在 HTTP 上）。
> 下一轮并入后即同源访问，8001 端口与该脚本一并删除。

## 🔐 登录凭据

### 管理员账号

- **用户名**: `admin`
- **密码**: 见 `secrets/admin_password`

```bash
# 复制密码到剪贴板
cat secrets/admin_password | pbcopy
```

> ⚠️  **安全提示**: 
> - 密码存储在 `secrets/admin_password` 文件中
> - 首次登录后建议修改密码
> - 该文件已被 `.gitignore` 排除，不会提交到仓库
> - **不要把密码抄进任何会进仓库的文件**（文档、HTML、脚本）

## 📋 功能说明

### 当前可用功能

1. **会话管理**
   - 创建新会话
   - 查看历史会话
   - 恢复中断的会话

2. **项目管理**
   - 创建工程项目
   - 关联会话到项目
   - 查看项目历史

3. **问答交互**
   - 规范问答（RAG）
   - 水力计算
   - 图纸审查（需要上传 DXF 文件）

4. **长期记忆（P5）**
   - 写入确认机制
   - 跨会话召回
   - 记忆管理（激活/停用/遗忘）

### API 密钥配置

不配置真实密钥时，提问会被诚实拒绝（424 `WEB_MODEL_NOT_CONFIGURED`），
而不是用假答案糊过去。两种配置方式：

#### 方式一：设置面板（推荐，立即生效）

登录后点右上角 **⚙** → 在「对话生成（DeepSeek）」或「向量检索与重排（百炼）」
里填入密钥 → 保存。**无需重启**，下一次提问即使用新密钥；重启后依然生效。

密钥只写不读：面板与所有接口都只返回「是否已配置、来自哪一层、何时更新」，
不会回传密钥内容。要确认填对了，回到会话发一条消息。

需要管理员账号才能写入；普通成员可以看到配置状态，以免撞上 424 却不知缘由。

#### 方式二：Compose secret 文件（需重建容器）

```bash
printf 'sk-your-deepseek-key' > secrets/deepseek_api_key
printf 'sk-your-dashscope-key' > secrets/dashscope_api_key

# 必须 up -d 重建容器，restart 不会重新读取挂载内容
docker compose up -d app
```

两个文件**必须存在**（可以是空文件）才能 `docker compose up`，因为 Compose
解析 `compose.yml` 时就会校验每个 `file:`。空文件等同于未配置。

解析优先级：同名环境变量 > `secrets/` 文件 > 设置面板写入。完整说明见
`secrets/README.md`。

> 配置好真实模型后再跑 `docker compose run --rm smoke`，其最后一项会真的
> 发起一次模型调用（产生少量费用），而不再走 424 分支。

## 🚀 快速开始

### 1. 启动服务

```bash
cd "/Users/yaerken/Desktop/给排水agent"
docker compose up -d
```

### 2. 访问 Web 界面

在浏览器打开: http://127.0.0.1:8000/

### 3. 登录

- 用户名: `admin`
- 密码: 见 `secrets/admin_password`

### 4. 创建第一个会话

1. 点击「新建会话」
2. 输入问题，例如：
   - "给水管道的设计秒流量如何确定？"
   - "DN150、Q=20L/s，求流速"
   - "请审查这张图纸的排水系统"

## 🔧 CLI 替代方案

如果 Web 界面遇到问题，可以使用 CLI 模式：

### 规范问答

```bash
_venv/bin/python -m water_agent.cli \
  --mock-generator \
  "给水管道的设计秒流量如何确定？"
```

### 水力计算

```bash
_venv/bin/python -m water_agent.cli \
  --mode calc \
  --mock-generator \
  "DN150、Q=20L/s，求流速"
```

### 图纸审查

```bash
_venv/bin/python -m water_agent.cli \
  --mode drawing \
  --drawing-root data/drawings/real_samples \
  --mock-generator \
  "审查 P-02 图纸"
```

## 📊 系统状态检查

### 健康检查

```bash
# 存活检查
curl http://127.0.0.1:8000/health/live

# 就绪检查
curl http://127.0.0.1:8000/health/ready

# 系统能力
curl http://127.0.0.1:8000/api/system/capabilities
```

### 查看日志

```bash
# 应用日志
docker compose logs app

# PostgreSQL 日志
docker compose logs postgres

# 实时跟踪
docker compose logs -f app
```

## 🔒 CSRF 保护机制

### 什么是 CSRF Token

系统使用 **double-submit cookie** 模式防止跨站请求伪造（CSRF）攻击：

1. **登录成功后**，后端会设置两个 cookie：
   - `wa_session`：会话 ID（HttpOnly，前端不可读）
   - `wa_csrf`：CSRF token（非 HttpOnly，前端可读）

2. **所有状态变更操作**（POST/PUT/PATCH/DELETE）都需要：
   - 携带 `wa_session` cookie（浏览器自动发送）
   - 在 `X-CSRF-Token` header 中带上 `wa_csrf` cookie 的值

### 如何使用 CSRF Token

#### 方法 1：使用 login.html 提供的辅助函数

```javascript
// login.html 已经定义了全局函数 sendWithCsrf
const response = await window.sendWithCsrf('http://127.0.0.1:8000/api/projects', {
    method: 'POST',
    body: JSON.stringify({
        name: 'My Project',
        description: 'Test project'
    })
});
const data = await response.json();
```

#### 方法 2：手动获取并设置

```javascript
// 从 cookie 中读取 CSRF token
function getCsrfToken() {
    const match = document.cookie.match(/wa_csrf=([^;]+)/);
    return match ? match[1] : null;
}

// 在请求中使用
const csrfToken = getCsrfToken();
const response = await fetch('http://127.0.0.1:8000/api/projects', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken
    },
    credentials: 'include',
    body: JSON.stringify({ name: 'My Project' })
});
```

#### 需要 CSRF Token 的操作

所有会改变系统状态的操作都需要 CSRF token：
- 创建/删除项目
- 创建/更新会话
- 写入/删除记忆
- 修改密码
- 登出

查询操作（GET 请求）不需要 CSRF token。

## ⚠️ 故障排查

### 问题 1: 403 AUTH_CSRF_INVALID

**症状**: 创建项目或其他操作返回 403 错误，错误信息包含 `AUTH_CSRF_INVALID`

**原因**: 请求缺少 CSRF token 或 token 不匹配

**解决**:
1. 确认已登录（访问 `http://127.0.0.1:8000/api/auth/me` 检查）
2. 检查浏览器开发者工具 → Application → Cookies，确认存在 `wa_csrf` cookie
3. 确保请求中包含 `X-CSRF-Token` header，值与 `wa_csrf` cookie 相同
4. 如果 cookie 不存在，重新登录

### 问题 2: 401 Unauthorized

**症状**: 访问 API 返回 401 错误

**原因**: 需要登录认证

**解决**:
1. 确认已在 Web 界面登录
2. 检查浏览器 Cookie 是否被阻止
3. 尝试无痕模式或清除浏览器缓存

### 问题 2: 服务未启动

**症状**: 无法访问 http://127.0.0.1:8000/

**解决**:
```bash
# 检查服务状态
docker compose ps

# 如果未运行，启动服务
docker compose up -d

# 查看启动日志
docker compose logs app
```

### 问题 3: 数据库连接失败

**症状**: 日志显示 "connection refused"

**解决**:
```bash
# 检查 PostgreSQL 状态
docker compose ps postgres

# 重启服务
docker compose restart postgres
docker compose restart app
```

### 问题 4: 模型未配置（424 错误）

**症状**: 返回 `WEB_MODEL_NOT_CONFIGURED`

**原因**: 未配置 DeepSeek/百炼 API 密钥。这是设计上的诚实拒绝——不会拿假答案糊过去。

**解决**:
1. 登录后点右上角 **⚙** 填入密钥，保存即生效（无需重启）
2. 或使用 CLI 的 `--mock-generator` 模式（无需 API 密钥）
3. 两种配置方式详见上文「API 密钥配置」

### 问题 5: 设置面板保存报 503 `WEB_CREDENTIAL_STORE_UNAVAILABLE`

**症状**: 在设置面板保存密钥时返回 503

**原因**: 凭据卷没挂上。容器是 `read_only: true`，密钥要写进
`app_credentials` 卷（挂载于 `/var/lib/water-agent-credentials`）；裸跑
`uvicorn` 或旧镜像启动的容器没有这个卷。

**解决**:
```bash
docker compose build app && docker compose up -d app
docker compose exec app ls -ld /var/lib/water-agent-credentials   # 期望 wateragent 所有
```
若该目录属于 `root`，说明卷是在镜像预创建该路径之前建的——Docker 只在首次
填充空卷时复制所属关系。停掉容器后 `docker volume rm water-agent_app_credentials`
再启动即可（卷里只有密钥，删掉等于重填一次）。

临时替代：用环境变量或 `secrets/` 文件配置，两者都不需要可写卷。

## 📚 相关文档

- **完整审查报告**: `PROJECT_REVIEW_2024-09-20.md`
- **系统测试**: `bash test_complete_system.sh`
- **API 文档**: http://127.0.0.1:8000/docs（需登录）
- **真实图纸样本**: `data/drawings/real_samples/`

## 🔄 服务管理

### 启动服务
```bash
docker compose up -d
```

### 停止服务
```bash
docker compose down
```

### 重启服务
```bash
docker compose restart app
```

> ⚠️ `restart` 只是重启容器里的**同一份**镜像与配置。改了 `src/` 下的代码、
> `compose.yml`、或 `secrets/` 里的文件，都必须重建 / 重建容器才会生效：
> ```bash
> docker compose build app && docker compose up -d app
> ```
> 源码是在构建时 `COPY` 进镜像的（见 `Dockerfile`），不是挂载进去的。

### 查看服务状态
```bash
docker compose ps
```

### 清理并重建
```bash
docker compose down -v  # 删除数据库数据（谨慎！）
docker compose up -d
```

## 💡 使用建议

1. **首次使用**: 先尝试 CLI 模式熟悉功能
2. **真实场景**: 配置 API 密钥后使用 Web 界面
3. **开发调试**: 使用 `--mock-generator` 避免消耗 API 额度
4. **生产部署**: 修改管理员密码，配置 HTTPS

## 📞 技术支持

- **项目路径**: `/Users/yaerken/Desktop/给排水agent`
- **审查报告**: `PROJECT_REVIEW_2024-09-20.md`
- **系统测试**: `bash test_complete_system.sh`（18/19 通过）

---

**最后更新**: 2024-09-20  
**版本**: P5 (包含 P5.1)
