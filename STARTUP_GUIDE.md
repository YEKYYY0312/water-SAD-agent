# 给排水 Agent - 完整启动指南

## 🚀 快速启动步骤

### 步骤 1: 启动 Docker Desktop

**方法 1: 通过应用程序启动**
```bash
# 在 Spotlight 搜索中输入 "Docker" 并打开
# 或在 Applications 文件夹中双击 Docker.app
```

**方法 2: 通过命令行启动**
```bash
open -a Docker
```

等待 Docker Desktop 完全启动（菜单栏图标变为静止状态，约 30-60 秒）

### 步骤 2: 验证 Docker 已启动

```bash
docker ps
```

如果看到容器列表（即使是空的），说明 Docker 已成功启动。

### 步骤 3: 启动给排水 Agent 服务

```bash
cd "/Users/yaerken/Desktop/给排水agent"
docker compose up -d
```

**预期输出**:
```
✔ Network water-agent_default     Created
✔ Container water-agent-postgres-1       Started
✔ Container water-agent-migrate-1        Started
✔ Container water-agent-bootstrap-admin-1 Started
✔ Container water-agent-app-1            Started
✔ Container water-agent-smoke-1          Started
```

### 步骤 4: 等待服务就绪（约 10-15 秒）

```bash
# 方法 1: 查看服务状态
docker compose ps

# 方法 2: 实时查看日志
docker compose logs -f app
# 看到 "Uvicorn running on http://0.0.0.0:8000" 即表示启动完成
# 按 Ctrl+C 退出日志查看
```

### 步骤 5: 打开 Web 界面

```bash
open http://127.0.0.1:8000/
```

或在浏览器中手动访问: **http://127.0.0.1:8000/**

---

## 🔐 登录信息

- **用户名**: `admin`
- **密码**: `<见 secrets/admin_password>`

**重要提示**: 密码是 32 个字符，请完整复制，不要有空格或换行。

---

## ✅ 验证服务正常

### 快速验证脚本

```bash
cd "/Users/yaerken/Desktop/给排水agent"

# 1. 检查服务状态
echo "==> 检查服务状态"
docker compose ps

# 2. 健康检查
echo -e "\n==> 健康检查"
curl -s http://127.0.0.1:8000/health/live | jq .

# 3. 就绪检查
echo -e "\n==> 就绪检查"
curl -s http://127.0.0.1:8000/health/ready | jq .

# 4. 测试登录
echo -e "\n==> 测试登录"
curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<见 secrets/admin_password>"}' | jq .
```

---

## 🔧 常见问题排查

### 问题 1: Docker 无法启动

**现象**: `Cannot connect to the Docker daemon`

**解决方案**:
1. 确认 Docker Desktop 应用已打开
2. 查看菜单栏是否有 Docker 图标
3. 如果图标一直转圈，等待 1-2 分钟
4. 如果仍失败，重启 Docker Desktop:
   ```bash
   killall Docker && open -a Docker
   ```

### 问题 2: 端口 8000 被占用

**现象**: `port is already allocated`

**解决方案**:
```bash
# 查找占用端口的进程
lsof -i :8000

# 停止占用端口的进程
kill -9 <PID>

# 或更改给排水 Agent 的端口（修改 compose.yml）
```

### 问题 3: 服务启动失败

**现象**: 容器状态显示 `Exited` 或 `Unhealthy`

**解决方案**:
```bash
# 查看错误日志
docker compose logs app
docker compose logs postgres

# 完全重启
docker compose down -v  # ⚠️ 会删除数据库数据
docker compose up -d
```

### 问题 4: Web 界面空白

**现象**: 浏览器显示空白页面

**解决方案**:
1. 按 F12 打开开发者工具
2. 查看 Console 标签的错误信息
3. 查看 Network 标签，确认资源加载状态
4. 尝试清除浏览器缓存: `Cmd+Shift+R` (Mac) 或 `Ctrl+Shift+R` (Windows)
5. 尝试无痕模式

### 问题 5: 登录失败

**现象**: 提示用户名或密码错误

**解决方案**:
1. 确认用户名是 `admin`（小写）
2. 确认密码完整复制（32 字符）：
   ```bash
   cat secrets/admin_password
   ```
3. 检查浏览器是否阻止 Cookie
4. 尝试其他浏览器

---

## 🛠️ 服务管理命令

### 启动服务
```bash
cd "/Users/yaerken/Desktop/给排水agent"
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

### 查看日志
```bash
# 查看所有日志
docker compose logs

# 查看应用日志
docker compose logs app

# 实时跟踪日志
docker compose logs -f app

# 查看最近 50 行
docker compose logs app --tail=50
```

### 查看服务状态
```bash
docker compose ps
```

### 进入容器调试
```bash
# 进入应用容器
docker compose exec app bash

# 进入数据库容器
docker compose exec postgres psql -U water_agent -d water_agent
```

---

## 📊 完整诊断

如果以上方法都无法解决问题，运行完整诊断脚本：

```bash
cd "/Users/yaerken/Desktop/给排水agent"
bash diagnose_web.sh
```

诊断脚本会检查：
- Docker 服务状态
- 网络连接
- 前端资源
- API 端点
- 认证系统
- 数据库连接
- 环境变量
- 应用日志

---

## 🆘 终极方案：使用 CLI 模式

如果 Web 界面确实无法使用，可以使用 CLI 模式（功能完全正常）：

### 规范问答
```bash
cd "/Users/yaerken/Desktop/给排水agent"
_venv/bin/python -m water_agent.cli --mock-generator \
  "给水管道的设计秒流量如何确定？"
```

### 水力计算
```bash
_venv/bin/python -m water_agent.cli --mode calc --mock-generator \
  "DN150、Q=20L/s，求流速"
```

### 图纸解析
```bash
_venv/bin/python -c "
from water_agent.drawing import read_drawing
from water_agent.runtime.governance.pipeline import ToolPolicy
from pathlib import Path

root = Path('data/drawings/real_samples').absolute()
policy = ToolPolicy(allowed_read_dirs=frozenset([root]))
result = read_drawing('P-02', root=root, policy=policy)
print(f'解析成功: {result.ok}')
print(f'实体数: {len(result.entities)}')
"
```

---

## 📞 获取帮助

如果问题仍未解决，请提供以下信息：

1. **Docker 状态**:
   ```bash
   docker --version
   docker compose ps
   ```

2. **服务日志**:
   ```bash
   docker compose logs app --tail=50
   ```

3. **诊断结果**:
   ```bash
   bash diagnose_web.sh > diagnostic_report.txt
   ```

4. **浏览器信息**:
   - 浏览器类型和版本
   - 浏览器控制台错误（F12 → Console）
   - Network 标签的请求状态

5. **具体错误现象**:
   - 空白页面？
   - 无法登录？
   - 某个功能不响应？
   - 报错信息？

---

## ✨ 成功启动后

访问 http://127.0.0.1:8000/，使用 `admin` / `<见 secrets/admin_password>` 登录。

你可以：
1. 创建工程项目
2. 新建问答会话
3. 进行规范查询
4. 执行水力计算
5. 上传 DXF 图纸进行审查

详细使用说明见 `WEB_LOGIN_GUIDE.md`。

---

**最后更新**: 2024-09-20  
**项目路径**: `/Users/yaerken/Desktop/给排水agent`
