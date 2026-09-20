#!/bin/bash
# 给排水 Agent 完整系统测试脚本
# 生成时间: 2024-09-20

set -e

echo "=========================================="
echo "给排水 Agent 完整系统测试"
echo "=========================================="
echo ""

# 颜色定义
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# 测试计数
TOTAL=0
PASSED=0
FAILED=0

run_test() {
    local name=$1
    local cmd=$2
    TOTAL=$((TOTAL + 1))

    echo -e "${YELLOW}[$TOTAL] 测试: $name${NC}"
    if eval "$cmd" > /dev/null 2>&1; then
        echo -e "${GREEN}✅ 通过${NC}"
        PASSED=$((PASSED + 1))
    else
        echo -e "${RED}❌ 失败${NC}"
        FAILED=$((FAILED + 1))
    fi
    echo ""
}

echo "==> 1. CLI 离线测试"
echo ""

run_test "规范问答（fixture 模式）" \
    "_venv/bin/python -m water_agent.cli --mock-generator '给水管道的设计秒流量如何确定？' | grep -q 'succeeded'"

run_test "水力计算（流速计算）" \
    "_venv/bin/python -m water_agent.cli --mode calc 'DN150、Q=20L/s，求流速' --mock-generator | grep -q 'velocity_mps'"

run_test "水力计算（管径选择）" \
    "_venv/bin/python -m water_agent.cli --mode calc 'Q=30L/s、v=1.2m/s，求管径' --mock-generator | grep -q 'diameter_mm'"

echo ""
echo "==> 2. 图纸解析测试"
echo ""

run_test "P-02 平面图解析" \
    "_venv/bin/python -c \"
from water_agent.drawing import read_drawing
from water_agent.runtime.governance.pipeline import ToolPolicy
from pathlib import Path
root = Path('data/drawings/real_samples').absolute()
policy = ToolPolicy(allowed_read_dirs=frozenset([root]))
result = read_drawing('P-02', root=root, policy=policy)
assert result.ok and len(result.entities) > 0
\""

run_test "S-01 系统图解析" \
    "_venv/bin/python -c \"
from water_agent.drawing import read_drawing
from water_agent.runtime.governance.pipeline import ToolPolicy
from pathlib import Path
root = Path('data/drawings/real_samples').absolute()
policy = ToolPolicy(allowed_read_dirs=frozenset([root]))
result = read_drawing('S-01', root=root, policy=policy)
assert result.ok and len(result.entities) > 0
\""

run_test "PUMP-01 泵房图解析" \
    "_venv/bin/python -c \"
from water_agent.drawing import read_drawing
from water_agent.runtime.governance.pipeline import ToolPolicy
from pathlib import Path
root = Path('data/drawings/real_samples').absolute()
policy = ToolPolicy(allowed_read_dirs=frozenset([root]))
result = read_drawing('PUMP-01', root=root, policy=policy)
assert result.ok and len(result.entities) > 0
\""

echo ""
echo "==> 3. Docker 服务测试"
echo ""

run_test "Docker 服务运行中" \
    "docker compose ps | grep -q 'water-agent-app-1.*Up.*healthy'"

run_test "PostgreSQL 健康检查" \
    "docker compose ps | grep -q 'water-agent-postgres-1.*Up.*healthy'"

run_test "Web 界面可访问" \
    "curl -s http://127.0.0.1:8000/ | grep -q '给排水 Agent 工作台'"

run_test "健康检查端点" \
    "curl -s http://127.0.0.1:8000/health/live | grep -q 'ok'"

run_test "就绪检查端点" \
    "curl -s http://127.0.0.1:8000/health/ready | grep -q 'ok'"

run_test "系统能力接口" \
    "curl -s http://127.0.0.1:8000/api/system/capabilities | grep -q 'capabilities'"

echo ""
echo "==> 4. 治理机制测试"
echo ""

run_test "Principal 授权机制" \
    "_venv/bin/python -c \"
from water_agent.runtime.governance.contracts import Principal, Capability
p = Principal.local('test-session')
assert Capability.RETRIEVE_CLAUSES in p.capabilities
assert Capability.EXECUTE_SHELL not in p.capabilities
\""

run_test "危险能力拒绝" \
    "_venv/bin/python -c \"
from water_agent.runtime.governance.contracts import Principal, Capability
try:
    p = Principal.local('test', capabilities=frozenset([Capability.EXECUTE_SHELL]))
    assert False, 'Should have rejected'
except ValueError:
    pass
\""

run_test "治理错误码契约" \
    "_venv/bin/python -c \"
from water_agent.runtime.governance.contracts import GOVERNANCE_ERROR_CODES
assert 'RATE_LIMITED' in GOVERNANCE_ERROR_CODES
assert 'GUARDRAIL_BLOCKED' in GOVERNANCE_ERROR_CODES
assert len(GOVERNANCE_ERROR_CODES) == 13
\""

echo ""
echo "==> 5. 知识库完整性"
echo ""

run_test "原始语料已入库" \
    "test $(ls knowledge/*.md 2>/dev/null | wc -l) -eq 221"

run_test "审校语料已入库" \
    "test -f data/curated/clauses/GB50015-2019.jsonl"

run_test "Fixture 数据可用" \
    "test -f data/fixtures/rag_fixture.jsonl"

run_test "真实图纸样本完整" \
    "test $(ls data/drawings/real_samples/*.dxf 2>/dev/null | wc -l) -eq 18"

echo ""
echo "=========================================="
echo "测试总结"
echo "=========================================="
echo -e "总计: $TOTAL"
echo -e "${GREEN}通过: $PASSED${NC}"
if [ $FAILED -gt 0 ]; then
    echo -e "${RED}失败: $FAILED${NC}"
else
    echo -e "失败: $FAILED"
fi
echo ""

if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}✅ 所有测试通过！系统运行正常。${NC}"
    exit 0
else
    echo -e "${RED}❌ 部分测试失败，请检查日志。${NC}"
    exit 1
fi
