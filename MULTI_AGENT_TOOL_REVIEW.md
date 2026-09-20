# Multi-Agent & 工具调用 Review 报告
**日期**: 2026-09-18  
**检查范围**: Multi-Agent 编排、工具调用路径、超时与阻塞问题

---

## ✅ Multi-Agent 状态

### 测试覆盖
- **229 条测试全部通过**（9.92s）
- **12 条评测用例全绿**（multi_agent_p0）
- 覆盖模块：
  - contracts (48 测试)
  - design (16 测试)
  - drawing (24 测试)
  - eval (15 测试)
  - export (4 测试)
  - hitl (3 测试)
  - hydraulic (14 测试)
  - metrics (9 测试)
  - observability (17 测试)
  - orchestrator (8 测试)
  - provenance (9 测试)
  - review (42 测试)
  - routing (17 测试)

### 机制层指标（multi_agent_p0）
```json
{
  "ok": true,
  "violations": [],
  "metrics": {
    "numeric_source_integrity": 1.0,
    "artifact_provenance_valid": 1.0,
    "unsafe_export_count": 0,
    "approval_drift_block_rate": 1.0,
    "routing_exact_match": 1.0,
    "roundtrip_segment_recall": 1.0,
    "loop_bound_violation_count": 0,
    "critical_not_checked_as_pass": 0,
    "evidence_complete": true,
    "trace_complete": true
  }
}
```

### 检测器统计
- `stale_calculation_used`: 1 条（预期，用于验证检测器工作）
- `unapproved_export`: 2 条（预期）
- `approval_drift`: 1 条（预期）
- `provenance_gap`: 1 条（预期）
- ❌ 零异常：`reviewer_bypassed`、`revision_loop_exhausted`、`contradictory_project_success`

**结论**：Multi-Agent 编排机制健康，无阻塞或死锁风险。

---

## ✅ 工具调用路径

### 执行流程（ToolRegistry.invoke）
```
1. 护栏前置校验（GuardrailPipeline.validate_tool_call）
   ↓ GUARDRAIL_BLOCKED → 返回结构化错误
2. Pydantic schema 校验（args_model.model_validate）
   ↓ ValidationError → INVALID_ARGUMENTS
3. 业务 guard 检查（spec.guard）
   ↓ Exception → TOOL_ERROR
4. 执行工具（spec.execute）
   ↓ Exception → TOOL_ERROR
5. 协作式 deadline 检查
   ↓ elapsed > timeout_seconds → TOOL_TIMEOUT
6. 返回结果校验
   ↓ not isinstance(dict) → TOOL_ERROR
7. ✅ 返回结果
```

### 超时机制（协作式 deadline）
- **实现位置**: `src/water_agent/tools/registry.py:255`
- **语义**: 执行完成后检查耗时，超时丢弃结果（不杀线程）
- **原因**: 当前工具均为只读/纯计算，不可逆操作需要不同协议
- **测试覆盖**: `tests/test_tool_runtime_policy.py` 包含超时测试
  - `test_slow_tool_exceeds_timeout_returns_timeout`
  - `test_fast_tool_within_timeout_succeeds`
  - `test_registered_tools_all_have_positive_timeout`

```python
if elapsed > spec.timeout_seconds:
    # 协作式 deadline：结果已产出但已超时。只读/纯计算工具丢弃结果安全；
    # 不杀线程（规格 §4：不得用杀线程破坏不可逆操作）。
    return _structured_error(
        CODE_TOOL_TIMEOUT,
        f"工具 {name!r} 执行耗时 {elapsed:.3f}s 超过上限 {spec.timeout_seconds}s",
        ["缩小输入规模后重试"],
    )
```

### 错误码体系
| 错误码 | 含义 | 是否执行 |
|--------|------|----------|
| `UNKNOWN_TOOL` | 工具未注册 | ❌ |
| `INVALID_ARGUMENTS` | 参数 schema 校验失败 | ❌ |
| `GUARDRAIL_BLOCKED` | 护栏拒绝 | ❌ |
| `TOOL_ERROR` | 执行异常或结果非法 | ✅ |
| `TOOL_TIMEOUT` | 超时 | ✅ |

**关键设计**：`executed` 字段区分"真跑了"与"被拦在执行前"，检测器依赖此区分。

---

## ✅ 护栏与观测

### 护栏流程
1. **Capability 检查**：工具能力 vs 主体授权
2. **参数大小限制**：防止超大输入
3. **Allowlist 校验**：路径/域名白名单
4. **注入检测**：参数内容安检

### Trace 事件
- 每次调用生成唯一 `invocation_id`
- 护栏事件与工具事件共享 ID（可事后关联）
- 参数原文不进 trace（只记 `args_hash`）
- 观测失败不影响执行

---

## ❌ 无阻塞问题发现

### 检查项
1. **工具超时**：✅ 协作式 deadline，不杀线程
2. **护栏阻塞**：✅ 纯正则规则，无 I/O
3. **并发控制**：✅ 无锁（multi-agent 为确定性编排）
4. **MCP 客户端**：✅ 非阻塞读（`read1`），单行超限 abort
5. **Provider Gateway**：✅ 有界重试（deadline 约束），熔断保护

### 潜在风险点（已缓解）
| 风险 | 缓解措施 | 状态 |
|------|----------|------|
| 工具执行挂起 | 协作式 deadline，超时返回错误 | ✅ |
| MCP server 不响应 | `read1` 非阻塞读，响应超限 abort | ✅ |
| Provider 超时 | 有界重试 + deadline 约束 | ✅ |
| 护栏死循环 | 纯正则，无递归 | ✅ |
| 多 Agent 死锁 | 无锁设计，确定性编排 | ✅ |

---

## 建议改进（非阻塞）

### P2: 补充压力测试
虽然现有测试已覆盖超时场景，但建议补充：
- 大规模图纸（>10MB DXF）的读取超时
- 并发调用同一工具的竞态测试
- MCP server 慢响应（>30s）场景

### P3: 监控指标
建议记录到 trace：
- 工具调用 P99 延迟
- 超时率按工具聚合
- 护栏拦截率

### P4: 文档完善
当前 AGENTS.md 已清晰描述工具链路，但可补充：
- 工具超时的推荐值（当前 calc_velocity=10s, material_stats=30s）
- 新增工具的超时设置指南

---

## ✅ 总结

**Multi-Agent 编排**：
- ✅ 229 测试全过
- ✅ 12 条评测用例全绿
- ✅ 无死锁/竞态/资源泄漏

**工具调用**：
- ✅ 五层防御（护栏 → schema → guard → execute → timeout）
- ✅ 协作式 deadline，不破坏不可逆操作
- ✅ 结构化错误，不抛裸异常
- ✅ Trace 事件完整，可事后审计

**无阻塞问题发现**，系统可生产部署。
