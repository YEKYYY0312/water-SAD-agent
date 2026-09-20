# 给排水 Agent Web 界面使用指南

## 🌐 访问地址

**本地开发**: http://127.0.0.1:8000/

## 🔐 登录凭据

### 管理员账号

- **用户名**: `admin`
- **密码**: `ZUMegbehOEWFij8uIIjKNT5c-ig6o6VO`

> ⚠️  **安全提示**: 
> - 密码存储在 `secrets/admin_password` 文件中
> - 首次登录后建议修改密码
> - 该文件已被 `.gitignore` 排除，不会提交到仓库

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

### API 密钥配置（可选）

如需使用真实 LLM 模型（非 mock），需要配置以下 API 密钥：

```bash
# DeepSeek API（生成模型）
printf 'sk-your-deepseek-key' > secrets/deepseek_api_key

# 百炼 API（嵌入与重排序）
printf 'sk-your-dashscope-key' > secrets/dashscope_api_key

# 重启服务
docker compose down
docker compose up -d
```

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
- 密码: `ZUMegbehOEWFij8uIIjKNT5c-ig6o6VO`

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

**原因**: 未配置 DeepSeek/百炼 API 密钥

**解决**:
1. 使用 CLI 的 `--mock-generator` 模式（无需 API 密钥）
2. 或者按上文「API 密钥配置」章节配置真实密钥

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
