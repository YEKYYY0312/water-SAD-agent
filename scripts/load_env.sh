#!/usr/bin/env bash
# 把 .env 中的密钥注入当前 shell（方案 C 极简加载器，零依赖）。
#
# 只识别 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY 两个已知变量，其余忽略；
# 值带引号或纯 token 均可。用法：
#   source scripts/load_env.sh      # 之后当前终端即可运行真实链路命令
#   source scripts/load_env.sh && _venv/bin/python -m water_agent.cli "..."
set -a
# shellcheck disable=SC1091
[ -f .env ] && . ./.env
set +a

if [ -z "${DEEPSEEK_API_KEY:-}" ] && [ -z "${DASHSCOPE_API_KEY:-}" ]; then
    echo "load_env: .env 中 DEEPSEEK_API_KEY / DASHSCOPE_API_KEY 均未配置" >&2
else
    [ -n "${DEEPSEEK_API_KEY:-}" ] && echo "load_env: DEEPSEEK_API_KEY 已注入"
    [ -n "${DASHSCOPE_API_KEY:-}" ] && echo "load_env: DASHSCOPE_API_KEY 已注入"
fi
