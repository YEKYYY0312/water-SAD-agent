"""运行时治理层（P2.5）。

本包只承载**不依赖 LangGraph / CLI / provider SDK** 的运行时边界定义：
- `contracts`：Principal / RequestContext / ContentEnvelope / SourceTag / Capability /
  PolicyDecision / ProviderCall / UsageRecord / CacheDisposition 与稳定错误码；
- `inventory`：远程 provider 调用点清单与工具 capability 声明（治理接入的唯一真源）。

约束（见 `docs/design/12-p2.5-production-governance.md`）：
- 本层**不发起任何远程调用**，也不导入 langchain/langgraph；契约必须能在纯离线测试中构造。
- Step 11 只定义边界与清单，`ProviderGateway`（Step 12）与 `GuardrailPipeline`（Step 14）
  是本层契约的消费者，未实现前不得声称已具备生产级治理能力。
"""
from __future__ import annotations
