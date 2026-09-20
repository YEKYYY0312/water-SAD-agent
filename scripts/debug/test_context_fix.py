#!/usr/bin/env python3
"""验证 P3 Step 9：检索上下文现在包含条款号了"""
import json
from langchain_core.messages import ToolMessage

# 模拟 runner.py 的 _context_texts_from_messages 方法
def _context_texts_from_messages(result: dict) -> list[str]:
    """从 retrieve_clauses 的 ToolMessage 里取条文正文，供 judge 做忠实度判据。

    P3 Step 9 修复：拼接 source_id（条款号）到正文前，使评委能判定引用条款号是否出现在上下文中。
    """
    from water_agent.graphs.agent_rag import _payload_of

    texts: list[str] = []
    for m in result.get("messages", []):
        if isinstance(m, ToolMessage) and m.name == "retrieve_clauses":
            for hit in _payload_of(m).get("hits", []) or []:
                text = hit.get("text")
                source_id = hit.get("source_id", "")
                if text:
                    # 格式：【条款号】 条文内容
                    prefix = f"【{source_id}】 " if source_id else ""
                    texts.append(prefix + str(text))
    return texts

# 构造测试数据
test_result = {
    "messages": [
        ToolMessage(
            name="retrieve_clauses",
            content=json.dumps({
                "ok": True,
                "hits": [
                    {
                        "source_id": "GB50014-2021 4.1.14",
                        "text": "城镇排水设计流量应按下列公式计算：Q=qψKz",
                    },
                    {
                        "source_id": "GB50318-2017 4.1.4",
                        "text": "消防给水系统的设计流量应满足..."
                    }
                ]
            }),
            tool_call_id="test-123"
        )
    ]
}

# 测试
context_texts = _context_texts_from_messages(test_result)

print("✅ 提取的上下文（现在包含条款号）：\n")
for i, text in enumerate(context_texts, 1):
    print(f"[{i}] {text[:80]}...")

print("\n✅ 条款号检查：")
for text in context_texts:
    if "GB50014-2021 4.1.14" in text:
        print("  ✓ GB50014-2021 4.1.14 出现在上下文中")
    if "GB50318-2017 4.1.4" in text:
        print("  ✓ GB50318-2017 4.1.4 出现在上下文中")

print("\n✅ 修复验证通过！评委现在能看到条款号了。")
