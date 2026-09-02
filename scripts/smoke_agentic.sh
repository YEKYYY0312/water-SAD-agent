#!/usr/bin/env bash
# Step 7 冒烟：真实 DeepSeek function calling 链路（--mode agentic）。
#
# - 无 DEEPSEEK_API_KEY → 打印 SKIP 并 exit 0。这是合法验证：无 Key 时全工程
#   依然可完整离线跑通（--mock-generator / fake 替代），SKIP 不算失败也不算通过。
# - 有 Key → 连跑 3 个真实规范问题，全部成功 exit 0，任一失败 exit 1
#   （真实检索需另行配置 DASHSCOPE_API_KEY 与索引，缺失时按失败/报错处理）。
set -u

cd "$(dirname "$0")/.."

PY_BIN="${PY_BIN:-$PWD/_venv/bin/python}"

if [ -z "${DEEPSEEK_API_KEY:-}" ]; then
    echo "SKIP: DEEPSEEK_API_KEY 未配置——真实模型链路跳过；离线闭环用 --mock-generator 验证"
    exit 0
fi

questions=(
    "给水管道的设计秒流量如何确定？"
    "DN150、Q=20L/s，求管内流速"
    "排水管道的最小设计流速要求是什么？"
)

fail=0
i=0
for q in "${questions[@]}"; do
    i=$((i + 1))
    echo "=== Q$i: $q ==="
    if ! "$PY_BIN" -m water_agent.cli --mode agentic "$q" --json; then
        echo "FAIL: 第 $i 问失败" >&2
        fail=1
    fi
    echo
done

if [ "$fail" -eq 0 ]; then
    echo "OK: 真实模型链路 3 问全部完成且退出码为 0"
fi
exit "$fail"