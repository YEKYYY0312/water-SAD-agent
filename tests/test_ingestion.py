"""P1 ingestion / TriviumDB 回归测试。"""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from water_agent.ingestion.pipeline import (
    IngestError,
    _settle_text_sidecars,
    _snapshot_text_sidecars,
    ingest_to_trivium,
    load_parent_store,
)
from water_agent.retrieval.trivium import TriviumRetriever, mock_embed

FIXTURE = Path("data/fixtures/rag_fixture.jsonl")


def test_resume_skips_embed_for_manifest_chunks():
    calls = {"n": 0}

    def counting_embed(text: str) -> list[float]:
        calls["n"] += 1
        return mock_embed(text)

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p1.db"
        first = ingest_to_trivium(FIXTURE, db_path, embedding_fn=counting_embed)
        assert first["inserted"] == 10
        first_calls = calls["n"]
        assert first_calls == 10
        second = ingest_to_trivium(FIXTURE, db_path, embedding_fn=counting_embed)
        assert second["skipped_existing"] == 10
        assert second["inserted"] == 0
        assert calls["n"] == first_calls


def test_reingest_keeps_the_persisted_text_index():
    """语料没变时重跑入库，不得清掉 ``.text`` 侧文件。

    manifest 命中原先连 ``index_text`` 一起跳过，然后照样 ``build_text_index`` +
    ``flush``，于是用空索引把侧文件删了。本机看不出来（检索会退回进程内重建），
    但容器以只读根文件系统运行时 read_write 打不开库，会一路静默退到 fixture ——
    等于拿 2KB 夹具冒充真实语料回答。
    """
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p1.db"
        sidecar = Path(str(db_path) + ".text")

        first = ingest_to_trivium(FIXTURE, db_path)
        assert first["inserted"] == 10
        assert sidecar.is_file(), "首次入库应写出持久化文本索引"
        first_bytes = sidecar.read_bytes()
        assert first_bytes

        second = ingest_to_trivium(FIXTURE, db_path)
        assert second["skipped_existing"] == 10
        assert second["failed"] == 0
        assert sidecar.is_file(), "重跑入库把 .text 侧文件删了"
        # 不比字节：词项落盘顺序不稳定，同样的语料两次 build 出来内容等价而字节不同。
        # 比长度足以钉住"索引没被清空/截断"，可用性由下面的只读检索证明。
        assert len(sidecar.read_bytes()) == len(first_bytes)

        # 侧文件在位 ⇒ 检索侧才能以只读方式打开（容器部署的前提）
        retriever = TriviumRetriever(db_path=db_path)
        status = retriever.runtime_status
        assert status["text_index_source"] == "persisted", status
        assert status["access_mode"] == "read_only", status
        assert retriever.retrieve("污水管道最大充满度", top_k=3)
        retriever.close()


def test_ingesting_nothing_does_not_wipe_an_existing_text_index():
    """空语料文件不得把已有的侧文件冲掉。

    read_write 打开的库在 ``close()`` 时按进程内文本索引重写侧文件，进程内为空就把
    ``.text`` 和 ``.text.meta`` 一起删掉。指错语料路径这种小失误因此足以毁掉索引，
    所以入库前快照、关库后按需还原。
    """
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p1.db"
        sidecar = Path(str(db_path) + ".text")
        meta = Path(str(db_path) + ".text.meta")
        ingest_to_trivium(FIXTURE, db_path)
        before = sidecar.stat().st_size

        empty = Path(tmp) / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        ingest_to_trivium(empty, db_path)

        # 不比字节：close() 会按进程内索引重写一遍侧文件，内容等价而字节不同
        assert sidecar.stat().st_size == before, "空语料入库把 .text 侧文件冲掉了"
        assert meta.is_file(), "空语料入库把 .text.meta 侧文件冲掉了"
        # 快照文件不得留在索引目录里
        assert not list(Path(tmp).glob("*.bak"))

        # 索引必须真能用：只读打开 + 纯文本检索（hybrid_alpha=0）
        assert any("充满度" in text for text in _pure_text_hits(db_path, "污水管道设计充满度"))
        retriever = TriviumRetriever(db_path=db_path)
        assert retriever.runtime_status["access_mode"] == "read_only"
        assert retriever.retrieve("污水管道最大充满度", top_k=3)
        retriever.close()


def _write_subset(target: Path, rows: slice) -> Path:
    lines = FIXTURE.read_text(encoding="utf-8").splitlines(keepends=True)
    target.write_text("".join(lines[rows]), encoding="utf-8")
    return target


def _pure_text_hits(db_path: Path, query: str, dim: int = 1536) -> list[str]:
    """以只读方式按纯文本检索（``hybrid_alpha=0``），返回命中正文。

    只读打开 + ``load_text_index`` 是容器部署的检索路径，``hybrid_alpha=0`` 让向量
    分量彻底不参与——命中与否完全由持久化文本索引决定，正是这里要考的东西。
    """
    import triviumdb

    handle = triviumdb.TriviumDB(str(db_path), dim=dim, access_mode="read_only", load_text_index=True)
    try:
        hits = handle.search_hybrid([0.0] * dim, query, top_k=5, hybrid_alpha=0.0)
        return [str((hit.payload or {}).get("text", "")) for hit in hits]
    finally:
        handle.close()


def test_second_corpus_file_does_not_shrink_the_text_index():
    """逐文件入库时，后一个文件不得把前一个文件的文本索引挤掉。

    ``scripts/build_index.py`` 对 ``data/curated/clauses/*.jsonl`` 逐个调用
    ``ingest_to_trivium``，而文本索引只存在于进程内。若打开库时不加载已持久化的索引，
    每个文件的 ``build_text_index()`` 落盘的就只是自己这一批节点：实测 3313 个节点的
    3.5MB 索引在重跑后只剩末位文件 81 个节点的 80KB。侧文件仍在、仍非空，
    ``has_persisted_text_index()`` 照样为真，检索侧以只读打开并信任它 ——
    于是绝大部分语料静默失去文本检索，hybrid 退化成纯向量而无一处报错。
    """
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p1.db"
        sidecar = Path(str(db_path) + ".text")
        first_file = _write_subset(Path(tmp) / "a.jsonl", slice(0, 5))
        second_file = _write_subset(Path(tmp) / "b.jsonl", slice(5, 10))

        ingest_to_trivium(first_file, db_path)
        after_first = sidecar.stat().st_size
        ingest_to_trivium(second_file, db_path)

        assert sidecar.stat().st_size > after_first, "第二个文件把第一个文件的文本索引挤掉了"
        # 两个文件各自的独有内容都必须仍能被纯文本检索命中
        assert any("水流速度" in text for text in _pure_text_hits(db_path, "给水管道内水流速度")), \
            "第一个文件的条款从持久化文本索引里消失了"
        assert any("充满度" in text for text in _pure_text_hits(db_path, "污水管道设计充满度")), \
            "第二个文件的条款不在持久化文本索引里"


def test_failed_ingest_does_not_damage_the_existing_text_index():
    """嵌入全挂的入库必须响亮失败，且既有文本索引一字不少。"""
    def exploding_embed(_text: str) -> list[float]:
        raise RuntimeError("embedding provider down")

    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p1.db"
        sidecar = Path(str(db_path) + ".text")
        ingest_to_trivium(FIXTURE, db_path)
        before = sidecar.stat().st_size

        # manifest 会让重跑走"跳过嵌入"的路径，删掉它以强制重新嵌入（并全部失败）
        Path(str(db_path) + ".manifest.json").unlink()
        with pytest.raises(IngestError) as excinfo:
            ingest_to_trivium(FIXTURE, db_path, embedding_fn=exploding_embed)

        assert {issue.operation for issue in excinfo.value.issues} == {"embed"}
        assert excinfo.value.stats.inserted == 0
        assert sidecar.stat().st_size == before, "失败的入库把既有 .text 侧文件毁了"
        assert not list(Path(tmp).glob("*.bak"))
        assert any("充满度" in text for text in _pure_text_hits(db_path, "污水管道设计充满度"))


def test_sidecar_snapshot_restores_a_half_written_text_index():
    """兜底层：build/flush 写坏侧文件时（磁盘满、进程被杀）用入库前的快照还原。"""
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p1.db"
        text, meta = Path(str(db_path) + ".text"), Path(str(db_path) + ".text.meta")
        ingest_to_trivium(FIXTURE, db_path)
        good = text.read_bytes()

        assert _snapshot_text_sidecars(db_path) is True
        text.write_bytes(b"TIDX truncated")  # 模拟写到一半就断了
        meta.unlink()
        _settle_text_sidecars(db_path, restore=True)
        assert text.read_bytes() == good
        assert meta.is_file()
        assert not list(Path(tmp).glob("*.bak")), "还原后必须清掉快照"

        # 不需要还原时快照同样要清掉，不能在索引目录里留垃圾
        assert _snapshot_text_sidecars(db_path) is True
        _settle_text_sidecars(db_path, restore=False)
        assert not list(Path(tmp).glob("*.bak"))
        assert text.read_bytes() == good

        # 侧文件不全时不算有快照（缺 .text.meta 则 load_text_index 用不了）
        meta.unlink()
        assert _snapshot_text_sidecars(db_path) is False


def test_ingest_is_idempotent_and_persists_parents():
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p1.db"
        # first ingest
        stats1 = ingest_to_trivium(FIXTURE, db_path)
        assert stats1["inserted"] == 10
        assert stats1["failed"] == 0
        parents_path = Path(str(db_path) + ".parents.json")
        assert parents_path.exists()
        parent_store = load_parent_store(db_path)
        assert parent_store
        # second ingest should not duplicate
        stats2 = ingest_to_trivium(FIXTURE, db_path)
        assert stats2["inserted"] == 0
        assert stats2["skipped_existing"] == 10
        assert stats2["failed"] == 0
        # new retriever should load parents from disk automatically
        retriever = TriviumRetriever(db_path=db_path)
        hits = retriever.retrieve("污水管道最大充满度", top_k=3)
        assert hits
        assert any(h.text.startswith("【") for h in hits)
        assert all(h.source_id.startswith("FIX ") for h in hits)
        retriever.close()


def test_retrieve_real_db_failure_does_not_fallback_to_fixture(monkeypatch):
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "p1.db"
        stats = ingest_to_trivium(FIXTURE, db_path)
        assert stats["inserted"] == 10
        retriever = TriviumRetriever(db_path=db_path)
        assert not retriever.is_fallback
        # monkeypatch search_hybrid to explode
        class DummyDB:
            def search_hybrid(self, *args, **kwargs):
                raise RuntimeError("boom")

            def close(self):
                pass

        retriever._db = DummyDB()  # type: ignore[attr-defined]
        with pytest.raises(RuntimeError):
            retriever.retrieve("污水管道最大充满度")
        retriever.close()


def test_plain_fixture_mode_still_allowed_when_db_missing():
    retriever = TriviumRetriever(db_path=None)
    assert retriever.is_fallback
    hits = retriever.retrieve("给水管道设计问题")
    assert hits


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ingest_script_overwrite_removes_manifest_and_rebuilds(tmp_path):
    """scripts/ingest.py --overwrite 必须连 manifest 一起删，否则第二次建出来是空库。"""
    import subprocess
    import sys

    db = tmp_path / "ow.db"
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "ingest.py"), str(REPO_ROOT / FIXTURE), "--db-path", str(db), "--overwrite"]
    for _ in range(2):
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT, check=False)
        assert proc.returncode == 0, proc.stderr
        assert "'inserted': 10" in proc.stdout, proc.stdout  # 第二次也必须真正写入 10 条，而非 skipped
    import triviumdb

    handle = triviumdb.TriviumDB(str(db))
    assert len(list(handle.all_node_ids())) == 10, "overwrite 后重建应仍有 10 个子块"
    handle.close()


def test_build_index_dry_run_needs_no_api_key(tmp_path, monkeypatch):
    import subprocess
    import sys

    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "build_index.py"), "--db-path", str(tmp_path / "x.db"), "--dry-run"],
        capture_output=True, text=True, cwd=REPO_ROOT, check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    import json as _json

    out = _json.loads(proc.stdout)
    assert out["dry_run"] is True and out["total"]["child_payloads"] > 0


def test_multi_file_ingest_keeps_parents_from_every_file():
    """review 09-28 #5：parents.json 曾被逐文件覆盖，只剩最后一个文件的父块。

    scripts/build_index.py 对 data/curated/clauses/*.jsonl 逐个调用 ingest_to_trivium，
    实测已发布索引的父块只剩末位 GB50318 的 24 个（应 147）。用两本真实入库规范验证。
    """
    curated = Path("data/curated/clauses")
    first, second = curated / "GB50013-2018.jsonl", curated / "GB50318-2017.jsonl"
    with TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "multi.db"
        ingest_to_trivium(first, db_path)
        after_first = load_parent_store(db_path)
        ingest_to_trivium(second, db_path)
        after_both = load_parent_store(db_path)
        # 再跑一遍第一本：幂等，不增不减
        ingest_to_trivium(first, db_path)
        after_rerun = load_parent_store(db_path)

    specs_first = {chunk.spec_id for chunk in after_first.values()}
    specs_both = {chunk.spec_id for chunk in after_both.values()}
    assert specs_first == {"GB50013-2018"}
    assert specs_both == {"GB50013-2018", "GB50318-2017"}, "第二个文件冲掉了第一个文件的父块"
    assert set(after_first) <= set(after_both)
    assert after_rerun.keys() == after_both.keys()
