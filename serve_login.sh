#!/bin/bash
# 临时 HTTP 服务器，用于访问登录页面和 CSRF 测试页面
# 运行后访问：
#   http://127.0.0.1:8001/login.html
#   http://127.0.0.1:8001/test_csrf.html

set -e

PORT=8001

echo "🚀 启动临时 HTTP 服务器 (端口 $PORT)..."
echo ""
echo "📝 可访问的页面："
echo "   • 登录页面: http://127.0.0.1:$PORT/login.html"
echo "   • CSRF 测试: http://127.0.0.1:$PORT/test_csrf.html"
echo ""
echo "⚠️  注意：这是临时方案，下次重新构建 Docker 镜像后可直接通过 8000 端口访问"
echo ""
echo "按 Ctrl+C 停止服务器"
echo ""

cd "$(dirname "$0")"
exec python3 -m http.server "$PORT"
