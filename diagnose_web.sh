#!/bin/bash
# 给排水 Agent Web 界面问题诊断脚本
# 生成时间: 2024-09-20

set -e

echo "╔════════════════════════════════════════════════════════════════╗"
echo "║          给排水 Agent Web 界面问题诊断                          ║"
echo "╚════════════════════════════════════════════════════════════════╝"
echo ""

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

ISSUES=0

check_pass() {
    echo -e "${GREEN}✅ $1${NC}"
}

check_warn() {
    echo -e "${YELLOW}⚠️  $1${NC}"
    ISSUES=$((ISSUES + 1))
}

check_fail() {
    echo -e "${RED}❌ $1${NC}"
    ISSUES=$((ISSUES + 1))
}

check_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

echo "==> 1. Docker 服务状态检查"
echo ""

if docker compose ps | grep -q "water-agent-app-1.*Up.*healthy"; then
    check_pass "应用服务运行中且健康"
else
    check_fail "应用服务未运行或不健康"
    docker compose ps
fi

if docker compose ps | grep -q "water-agent-postgres-1.*Up.*healthy"; then
    check_pass "PostgreSQL 数据库运行中且健康"
else
    check_fail "PostgreSQL 数据库未运行或不健康"
fi

echo ""
echo "==> 2. 网络连接检查"
echo ""

if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/ | grep -q "200"; then
    check_pass "HTTP 服务可访问（端口 8000）"
else
    check_fail "HTTP 服务不可访问"
fi

if curl -s http://127.0.0.1:8000/health/live | jq -e '.ok == true' > /dev/null 2>&1; then
    check_pass "存活检查通过"
else
    check_warn "存活检查失败"
fi

if curl -s http://127.0.0.1:8000/health/ready | jq -e '.ok == true' > /dev/null 2>&1; then
    check_pass "就绪检查通过"
else
    check_warn "就绪检查失败"
fi

echo ""
echo "==> 3. 前端资源检查"
echo ""

JS_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/assets/index-ClVhx4RQ.js)
if [ "$JS_STATUS" = "200" ]; then
    check_pass "JavaScript 资源可加载"
else
    check_fail "JavaScript 资源加载失败（状态码: $JS_STATUS）"
fi

CSS_STATUS=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/assets/index-Cl_ZXz7X.css)
if [ "$CSS_STATUS" = "200" ]; then
    check_pass "CSS 资源可加载"
else
    check_fail "CSS 资源加载失败（状态码: $CSS_STATUS）"
fi

echo ""
echo "==> 4. API 端点检查"
echo ""

CAPS_STATUS=$(curl -s http://127.0.0.1:8000/api/system/capabilities | jq -e '.ok == true' > /dev/null 2>&1 && echo "ok" || echo "fail")
if [ "$CAPS_STATUS" = "ok" ]; then
    check_pass "系统能力接口正常"

    # 检查关键配置
    REAL_LLM=$(curl -s http://127.0.0.1:8000/api/system/capabilities | jq -r '.data.real_llm')
    DRAWING_STATUS=$(curl -s http://127.0.0.1:8000/api/system/capabilities | jq -r '.data.drawing.status')

    if [ "$REAL_LLM" = "false" ]; then
        check_warn "真实 LLM 未配置（使用 mock 模式）"
    else
        check_pass "真实 LLM 已配置"
    fi

    if [ "$DRAWING_STATUS" = "not_configured" ]; then
        check_warn "图纸审查未配置（DRAWING_ROOT 未设置）"
    else
        check_pass "图纸审查已配置"
    fi
else
    check_fail "系统能力接口异常"
fi

echo ""
echo "==> 5. 认证系统检查"
echo ""

LOGIN_RESPONSE=$(curl -s -X POST http://127.0.0.1:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"<见 secrets/admin_password>"}' \
  -c /tmp/water_agent_cookies.txt)

if echo "$LOGIN_RESPONSE" | jq -e '.ok == true' > /dev/null 2>&1; then
    check_pass "管理员登录成功"
    USER_ID=$(echo "$LOGIN_RESPONSE" | jq -r '.data.user.user_id')
    check_info "用户 ID: $USER_ID"
else
    check_fail "管理员登录失败"
    echo "$LOGIN_RESPONSE" | jq .
fi

# 测试认证后的接口
if [ -f /tmp/water_agent_cookies.txt ]; then
    PROJECTS_RESPONSE=$(curl -s -X GET http://127.0.0.1:8000/api/projects \
      -b /tmp/water_agent_cookies.txt)

    if echo "$PROJECTS_RESPONSE" | jq -e '.ok == true' > /dev/null 2>&1; then
        check_pass "项目列表接口正常（已认证）"
        PROJECT_COUNT=$(echo "$PROJECTS_RESPONSE" | jq '.data.projects | length')
        check_info "当前项目数: $PROJECT_COUNT"
    else
        check_warn "项目列表接口异常"
        echo "$PROJECTS_RESPONSE" | jq .
    fi
fi

echo ""
echo "==> 6. 数据库连接检查"
echo ""

if docker compose exec -T postgres psql -U water_agent -d water_agent -c "SELECT 1" > /dev/null 2>&1; then
    check_pass "PostgreSQL 数据库连接正常"

    # 检查关键表
    USER_COUNT=$(docker compose exec -T postgres psql -U water_agent -d water_agent -tAc "SELECT COUNT(*) FROM users" 2>/dev/null || echo "0")
    check_info "用户表记录数: $USER_COUNT"

    PROJECT_COUNT=$(docker compose exec -T postgres psql -U water_agent -d water_agent -tAc "SELECT COUNT(*) FROM projects" 2>/dev/null || echo "0")
    check_info "项目表记录数: $PROJECT_COUNT"

    SESSION_COUNT=$(docker compose exec -T postgres psql -U water_agent -d water_agent -tAc "SELECT COUNT(*) FROM web_sessions" 2>/dev/null || echo "0")
    check_info "会话表记录数: $SESSION_COUNT"
else
    check_fail "PostgreSQL 数据库连接失败"
fi

echo ""
echo "==> 7. 应用日志检查（最近 20 条）"
echo ""

echo -e "${BLUE}最近的应用日志:${NC}"
docker compose logs app --tail=20 2>&1 | tail -20

echo ""
echo "==> 8. 环境变量检查"
echo ""

if [ -f secrets/admin_password ]; then
    check_pass "管理员密码文件存在"
else
    check_fail "管理员密码文件不存在"
fi

if [ -f secrets/postgres_password ]; then
    check_pass "PostgreSQL 密码文件存在"
else
    check_fail "PostgreSQL 密码文件不存在"
fi

if [ -f secrets/deepseek_api_key ]; then
    check_info "DeepSeek API Key 已配置（真实 LLM 模式）"
else
    check_info "DeepSeek API Key 未配置（Mock 模式）"
fi

if [ -f secrets/dashscope_api_key ]; then
    check_info "百炼 API Key 已配置"
else
    check_info "百炼 API Key 未配置"
fi

echo ""
echo "==> 9. 浏览器访问建议"
echo ""

check_info "请在浏览器中打开: http://127.0.0.1:8000/"
check_info "登录凭据: admin / <见 secrets/admin_password>"
echo ""
check_info "如果浏览器显示空白页面，请检查浏览器控制台："
echo "   1. 按 F12 打开开发者工具"
echo "   2. 切换到 Console 标签"
echo "   3. 查看是否有 JavaScript 错误"
echo ""
check_info "如果看到登录界面但无法登录，请检查："
echo "   1. 用户名是否为: admin（小写）"
echo "   2. 密码是否完全复制（32 字符，无空格）"
echo "   3. 浏览器是否阻止了 Cookie"

echo ""
echo "==> 10. 诊断总结"
echo ""

if [ $ISSUES -eq 0 ]; then
    echo -e "${GREEN}✅ 所有检查通过！系统运行正常。${NC}"
    echo ""
    echo "如果浏览器仍然无法使用，请提供以下信息："
    echo "  1. 浏览器类型和版本"
    echo "  2. 浏览器控制台的错误信息（F12 → Console）"
    echo "  3. 具体的错误现象（空白页、无法登录、功能不响应等）"
else
    echo -e "${YELLOW}⚠️  发现 $ISSUES 个潜在问题，请查看上述检查结果。${NC}"
    echo ""
    echo "常见解决方案："
    echo "  1. 重启服务: docker compose restart app"
    echo "  2. 查看详细日志: docker compose logs app -f"
    echo "  3. 完全重启: docker compose down && docker compose up -d"
    echo "  4. 检查端口占用: lsof -i :8000"
fi

echo ""
echo "╚════════════════════════════════════════════════════════════════╝"

# 清理
rm -f /tmp/water_agent_cookies.txt

exit 0
