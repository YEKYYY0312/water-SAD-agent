"""多轮会话记忆测试（Step 3）：checkpointer + thread_id。

断言：① 第二次提问时 messages 含第一条记录（add_messages 累积）；
② 不同 thread_id 互不串扰；③ sqlite 可用时落盘后可跨实例恢复，不可用则 skip。
"""
from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from water_agent.graphs.chat import build_chat_app
from water_agent.session import (
    new_session_id,
    save_session_meta,
    session_db_path,
)


def _invoke(app, thread_id: str, text: str, query: str = "") -> dict:
    return app.invoke(
        {"messages": [HumanMessage(content=text)], "query": query},
        config={"configurable": {"thread_id": thread_id}},
    )


def _contents(state: dict) -> list[str]:
    return [getattr(m, "content", str(m)) for m in state.get("messages", [])]


class TestAddMessagesAccumulation:
    def test_second_question_sees_first_record(self) -> None:
        app = build_chat_app(checkpointer=MemorySaver())
        _invoke(app, "sess-1", "第一条问题")
        second = _invoke(app, "sess-1", "第二条问题")
        texts = _contents(second)
        assert "第一条问题" in texts, "第二次提问必须能看到第一条记录"
        assert len(texts) >= 2

    def test_answer_count_monotonic(self) -> None:
        app = build_chat_app(checkpointer=MemorySaver())
        first = _invoke(app, "sess-1", "第一条问题")
        second = _invoke(app, "sess-1", "第二条问题")
        assert second["answer"] != first["answer"], "第二次回答的计数必须递增"
        assert "第" in second["answer"] and "条" in second["answer"]

    def test_without_checkpointer_starts_fresh(self) -> None:
        app = build_chat_app()
        _invoke(app, "sess-1", "旧问题")
        fresh = _invoke(app, "sess-1", "新问题")
        texts = _contents(fresh)
        assert "旧问题" not in texts, "无 checkpointer 时每次调用是全新会话"


class TestThreadIsolation:
    def test_thread_ids_do_not_leak(self) -> None:
        app = build_chat_app(checkpointer=MemorySaver())
        _invoke(app, "thread-a", "属于A的问题")
        b_result = _invoke(app, "thread-b", "属于B的问题")
        texts = _contents(b_result)
        assert "属于A的问题" not in texts, "不同 thread_id 之间不得串扰"
        assert "属于B的问题" in texts


class TestSqlitePersistence:
    def test_state_survives_new_app_instance(self, tmp_path) -> None:
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError:  # pragma: no cover - 取决于环境
            pytest.skip("langgraph-checkpoint-sqlite 未安装，跳过持久化用例")
        db_path = tmp_path / "sessions.sqlite"

        with SqliteSaver.from_conn_string(str(db_path)) as saver:
            app1 = build_chat_app(checkpointer=saver)
            _invoke(app1, "t1", "重启前的历史消息")

        # 模拟新进程：重新打开同一 sqlite 文件构建新 app
        with SqliteSaver.from_conn_string(str(db_path)) as saver2:
            app2 = build_chat_app(checkpointer=saver2)
            resumed = _invoke(app2, "t1", "重启后的消息")

        texts = _contents(resumed)
        assert "重启前的历史消息" in texts, "磁盘 checkpointer 落盘后应可恢复历史会话"


class TestSessionHelpers:
    def test_new_session_id_unique(self) -> None:
        assert new_session_id() != new_session_id()

    def test_session_db_path_single_db(self, tmp_path) -> None:
        path = session_db_path(tmp_path)
        assert path == tmp_path / "sessions.sqlite"

    def test_save_session_meta_writes_json(self, tmp_path) -> None:
        sid = new_session_id()
        meta_path = save_session_meta(sid, session_dir=tmp_path, query="测试")
        assert meta_path.exists()
        import json

        data = json.loads(meta_path.read_text(encoding="utf-8"))
        assert data["session_id"] == sid
        assert data["query"] == "测试"
        assert data["status"] == "running"

    def test_save_session_meta_atomic(self, tmp_path) -> None:
        # 写入后再写，旧内容被覆盖且无 .tmp 残留
        sid = new_session_id()
        save_session_meta(sid, session_dir=tmp_path, query="第一次")
        save_session_meta(sid, session_dir=tmp_path, query="第二次")
        leftovers = list(tmp_path.rglob("*.tmp"))
        assert leftovers == [], "原子写入不得残留 .tmp 文件"
        import json

        meta_path = tmp_path / "meta" / f"{sid}.json"
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        assert data["query"] == "第二次"