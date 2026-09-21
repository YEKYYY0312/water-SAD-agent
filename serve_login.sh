#!/bin/bash
# 临时 HTTP 服务器，仅用于打开登录页（登录页并入 React 应用后本脚本即可删除）。
#
# 只服务一个临时目录，绝不服务项目根目录。此前的版本直接
# `python3 -m http.server` 于仓库根，导致 secrets/admin_password、.env
# （内含真实 API key）、.git/config 全部可通过 HTTP 读取——为绕开 CORS
# 而开的洞比 CORS 本身大得多。

set -euo pipefail

PORT=8001
cd "$(dirname "$0")"

SERVE_DIR="$(mktemp -d)"
trap 'rm -rf "${SERVE_DIR}"' EXIT INT TERM
cp login.html "${SERVE_DIR}/"

# 花括号是必需的：$PORT 紧跟中文全角标点时，bash 会把多字节字符的首字节
# 当成变量名的一部分，set -u 下直接报 unbound variable。
echo "🚀 临时 HTTP 服务器（端口 ${PORT}，仅暴露 login.html）"
echo ""
echo "   登录页面: http://127.0.0.1:${PORT}/login.html"
echo "   密码取用: cat secrets/admin_password | pbcopy"
echo ""
echo "   服务目录: ${SERVE_DIR}（退出时自动删除）"
echo "   项目目录未被暴露。"
echo ""
echo "按 Ctrl+C 停止"
echo ""

python3 -m http.server "${PORT}" --directory "${SERVE_DIR}" --bind 127.0.0.1
