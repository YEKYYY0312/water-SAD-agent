"""测试引用可追溯性校验（Step 8，2026-09-16）。

验证 cite_clauses 工具在启用可追溯性检查后，能够：
1. 拒绝引用不在检索上下文中的条款号
2. 返回清晰的错误消息，列出可用的条款号
3. 允许引用检索到的条款号
"""
from water_agent.tools.cite import build_cite_clauses_tool_with_context_check


def test_cite_with_empty_context_allows_all():
    """未提供检索上下文时，不进行校验（向后兼容）"""
    cite_tool, _ = build_cite_clauses_tool_with_context_check()

    result = cite_tool.execute(cite_tool.args_model(
        article_ids=["GB50014-2021 3.1.2", "GB50015-2019 5.4.2"]
    ))

    assert result["registered"] is True
    assert result["cited"] == ["GB50014-2021 3.1.2", "GB50015-2019 5.4.2"]
    assert "error" not in result


def test_cite_with_context_rejects_invalid_ids():
    """提供检索上下文后，拒绝不在其中的条款号"""
    cite_tool, update_context = build_cite_clauses_tool_with_context_check()

    # 模拟检索返回 3 个条款
    update_context(["GB50014-2021 3.1.2", "GB50014-2021 3.1.3", "GB50015-2019 5.4.2"])

    # 尝试引用一个未检索到的条款
    result = cite_tool.execute(cite_tool.args_model(
        article_ids=["GB50014-2021 3.1.2", "GB50013-2018 5.3.1"]  # 后者未检索到
    ))

    assert result["registered"] is False
    assert result["cited"] == []
    assert "error" in result
    assert "GB50013-2018 5.3.1" in result["error"]
    assert "不在检索上下文中" in result["error"]
    assert result["invalid_ids"] == ["GB50013-2018 5.3.1"]
    assert result["available_count"] == 3


def test_cite_with_context_accepts_valid_ids():
    """提供检索上下文后，允许引用其中的条款号"""
    cite_tool, update_context = build_cite_clauses_tool_with_context_check()

    # 模拟检索返回 3 个条款
    update_context(["GB50014-2021 3.1.2", "GB50014-2021 3.1.3", "GB50015-2019 5.4.2"])

    # 引用检索到的条款
    result = cite_tool.execute(cite_tool.args_model(
        article_ids=["GB50014-2021 3.1.2", "GB50015-2019 5.4.2"]
    ))

    assert result["registered"] is True
    assert result["cited"] == ["GB50014-2021 3.1.2", "GB50015-2019 5.4.2"]
    assert "error" not in result


def test_cite_context_accumulates_across_retrievals():
    """检索上下文累积模式：多次检索的条款号都保留"""
    cite_tool, update_context = build_cite_clauses_tool_with_context_check()

    # 第一次检索
    update_context(["GB50014-2021 3.1.2", "GB50014-2021 3.1.3"])

    # 第二次检索
    update_context(["GB50015-2019 5.4.2", "GB50015-2019 5.4.3"])

    # 可以引用两次检索的所有条款
    result = cite_tool.execute(cite_tool.args_model(
        article_ids=["GB50014-2021 3.1.2", "GB50015-2019 5.4.2"]
    ))

    assert result["registered"] is True
    assert result["cited"] == ["GB50014-2021 3.1.2", "GB50015-2019 5.4.2"]


def test_cite_error_message_shows_available_sample():
    """错误消息应列出部分可用条款号作为提示"""
    cite_tool, update_context = build_cite_clauses_tool_with_context_check()

    # 模拟检索返回 15 个条款
    available_ids = [f"GB50014-2021 3.1.{i}" for i in range(1, 16)]
    update_context(available_ids)

    # 尝试引用未检索到的条款
    result = cite_tool.execute(cite_tool.args_model(
        article_ids=["GB50013-2018 5.3.1"]
    ))

    assert result["registered"] is False
    assert "可用的条款号" in result["error"]
    # 应该显示前 10 个 + 省略提示
    assert "共 15 个可用条款" in result["error"]
    assert "GB50014-2021 3.1.1" in result["error"]


def test_cite_multiple_invalid_ids():
    """同时引用多个无效条款号，应全部列出"""
    cite_tool, update_context = build_cite_clauses_tool_with_context_check()

    update_context(["GB50014-2021 3.1.2"])

    result = cite_tool.execute(cite_tool.args_model(
        article_ids=["GB50013-2018 5.3.1", "GB50015-2019 5.4.2", "GB50318-2017 3.5.3"]
    ))

    assert result["registered"] is False
    assert len(result["invalid_ids"]) == 3
    assert set(result["invalid_ids"]) == {
        "GB50013-2018 5.3.1",
        "GB50015-2019 5.4.2",
        "GB50318-2017 3.5.3"
    }
