# 登录页面快速使用指南

## 问题说明

之前登录页面 (login.html) 存在以下问题：
1. 直接用浏览器打开文件会遇到 CORS 跨域错误（`Failed to fetch`）
2. 登录后没有提供 CSRF token 的使用方法
3. 用户创建项目等操作会收到 403 错误

## 解决方案

### 1. 启动服务

```bash
# 终端 1：启动后端服务
cd "/Users/yaerken/Desktop/给排水agent"
docker compose up -d

# 终端 2：启动临时 HTTP 服务器
bash serve_login.sh
```

### 2. 访问登录页面

在浏览器中打开：**http://127.0.0.1:8001/login.html**

- 用户名已自动填充：`admin`
- 密码已自动填充：`ZUMegbehOEWFij8uIIjKNT5c-ig6o6VO`
- 点击「登录」按钮

### 3. 测试 CSRF Token

登录成功后，访问：**http://127.0.0.1:8001/test_csrf.html**

测试页面提供：
- ✅ 检查 CSRF token 状态
- ✅ 测试 GET 请求（不需要 CSRF）
- ✅ 对比测试 POST 请求（有/无 CSRF token）
- ✅ 完整代码示例

### 4. 在自己的代码中使用 CSRF Token

登录后，所有状态变更操作都需要带上 CSRF token：

```javascript
// 方法 1：使用全局辅助函数（login.html 已定义）
const response = await window.sendWithCsrf('http://127.0.0.1:8000/api/projects', {
    method: 'POST',
    body: JSON.stringify({ name: 'My Project' })
});

// 方法 2：手动获取和设置
const csrfToken = document.cookie.match(/wa_csrf=([^;]+)/)?.[1];
const response = await fetch('http://127.0.0.1:8000/api/projects', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrfToken  // 关键！
    },
    credentials: 'include',
    body: JSON.stringify({ name: 'My Project' })
});
```

## 常见错误

### ❌ 网络错误: Failed to fetch

**原因**：直接用浏览器打开了本地文件（`file://` 协议），导致 CORS 跨域

**解决**：使用 `bash serve_login.sh` 启动 HTTP 服务器，通过 `http://127.0.0.1:8001/login.html` 访问

### ❌ 403 AUTH_CSRF_INVALID

**原因**：请求缺少 `X-CSRF-Token` header

**解决**：使用上面的代码示例，确保在 header 中带上 CSRF token

### ❌ 401 Unauthorized

**原因**：未登录或登录过期

**解决**：重新访问 `http://127.0.0.1:8001/login.html` 登录

## 技术细节

- **CSRF 保护机制**：double-submit cookie 模式
- **Cookie 类型**：
  - `wa_session`：HttpOnly（前端不可读，浏览器自动发送）
  - `wa_csrf`：非 HttpOnly（前端可读，需手动在 header 中回传）
- **需要 CSRF token 的操作**：所有 POST/PUT/PATCH/DELETE 请求
- **不需要 CSRF token 的操作**：所有 GET 请求

## 未来改进

下次重新构建 Docker 镜像时，login.html 和 test_csrf.html 会被打包进去，  
届时可以直接通过 http://127.0.0.1:8000/login.html 访问，无需启动临时服务器。

构建命令：
```bash
docker compose build
docker compose up -d
```
