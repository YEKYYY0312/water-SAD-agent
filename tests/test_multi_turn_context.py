"""Regression: per-turn recall context must not leak into the next turn.

``memory_context`` / ``upload_context`` are reducer-less graph state keys, so the
checkpointer persists them.  They used to be written only when non-empty, which
left turn N's value in the thread for turn N+1.  Read back through the
production serializer (``default_checkpoint_serde`` allow-lists only
``CitationClaim``), the value is no longer a ``MemoryRecallResult`` /
``UploadRecallResult`` but a plain dict, and ``PromptAssembler`` raised
``TypeError`` -- every Web conversation died on the second question whenever the
second question had no recall hits (the common case for uploads).

Every test here uses a **real** ``AgenticSession`` over a checkpointer with the
production serde.  The Web-level tests use fake sessions, which is exactly why
this was never caught: the bug only exists across two real graph invocations.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from water_agent.documents.retrieval import UploadRecallHit, UploadRecallResult
from water_agent.memory.recall import MemoryRecallResult
from water_agent.runtime.agentic import build_agentic_session
from water_agent.session import default_checkpoint_serde


def _args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        db_path=None,
        dimensions=None,
        rerank="none",
        rerank_model=None,
        rerank_base_url=None,
        rerank_api_key=None,
        top_k=5,
        recall_top_k=20,
        hybrid_alpha=0.7,
        model="unused",
        base_url="unused",
        api_key_env="DEEPSEEK_API_KEY",
        api_key=None,
        mock_generator=True,
        session_dir=str(tmp_path / "sessions"),
        trace_dir=str(tmp_path / "traces"),
        stream=False,
        drawing_root=None,
    )


def _upload_context() -> UploadRecallResult:
    hit = UploadRecallHit(
        upload_id="u-1",
        chunk_id="c-1",
        ordinal=0,
        page=None,
        filename="设计说明.txt",
        content="本工程给水干管采用 DN100 球墨铸铁管。",
        suspicious=False,
        score=1.0,
    )
    return UploadRecallResult(
        query="给水干管", hits=(hit,), candidate_count=1, omitted_count=0, character_budget=1200
    )


def _memory_context() -> MemoryRecallResult:
    return MemoryRecallResult(
        query="给水",
        hits=(),
        candidate_count=0,
        omitted_count=0,
        used_characters=0,
        character_budget=1200,
        item_limit=5,
    )


def _two_turns(tmp_path: Path, *, first: dict, second: dict) -> tuple[dict, dict, InMemorySaver]:
    saver = InMemorySaver(serde=default_checkpoint_serde())
    with ExitStack() as stack:
        turn1 = build_agentic_session(_args(tmp_path), stack=stack, thread_id="t-1", checkpointer=saver)
        r1 = asyncio.run(turn1.aask("给水干管管径是多少", run_id="r-1", **first))
        # 新的 session 对象 = Web 每个 run 各自装配一次（RunManager 就是这么做的）
        turn2 = build_agentic_session(_args(tmp_path), stack=stack, thread_id="t-1", checkpointer=saver)
        r2 = asyncio.run(turn2.aask("污水管道最小坡度", run_id="r-2", **second))
    return r1, r2, saver


@pytest.mark.parametrize(
    "first",
    [
        {"upload_context": _upload_context()},
        {"memory_context": _memory_context()},
        {"upload_context": _upload_context(), "memory_context": _memory_context()},
    ],
    ids=["upload", "memory", "both"],
)
def test_second_turn_without_recall_does_not_crash(tmp_path: Path, first: dict) -> None:
    r1, r2, _ = _two_turns(tmp_path, first=first, second={})
    assert r1["status"] == "succeeded"
    assert r2["status"] == "succeeded", r2.get("errors")


def test_previous_turn_upload_does_not_reach_the_next_prompt(tmp_path: Path) -> None:
    """即便类型能读回，上一问的检索资料也不该进这一问的提示词。"""

    _, _, saver = _two_turns(tmp_path, first={"upload_context": _upload_context()}, second={})
    state = saver.get_tuple({"configurable": {"thread_id": "t-1"}}).checkpoint["channel_values"]
    assert state.get("upload_context") is None
    assert state.get("memory_context") is None


def test_second_turn_with_its_own_recall_still_works(tmp_path: Path) -> None:
    r1, r2, _ = _two_turns(
        tmp_path,
        first={"upload_context": _upload_context()},
        second={"upload_context": _upload_context()},
    )
    assert r1["status"] == r2["status"] == "succeeded"


def test_sync_cli_path_clears_leftover_context_from_a_web_turn(tmp_path: Path) -> None:
    """同一 thread 先被 Web（aask + 召回）用过、再被 CLI 的同步 invoke 续问。"""

    saver = InMemorySaver(serde=default_checkpoint_serde())
    with ExitStack() as stack:
        web = build_agentic_session(_args(tmp_path), stack=stack, thread_id="t-2", checkpointer=saver)
        asyncio.run(web.aask("给水干管管径", run_id="r-1", upload_context=_upload_context()))
        cli = build_agentic_session(_args(tmp_path), stack=stack, thread_id="t-2", checkpointer=saver)
        result = cli.ask("污水管道最小坡度")
    assert result["status"] == "succeeded", result.get("errors")
