"""容器冒烟的 agentic 探针：钉住它依赖的前提，以及它自己的失败路径。

``webapp/smoke.py`` 的 :func:`_run_agentic_probe` 在镜像内真跑一次 agentic run
（假模型 + 真索引），断言 ``runtime_status.is_fallback is False``。那条断言只在
容器里才会失败，所以本机必须把它**依赖的前提**逐条钉住，否则改坏了要等一次完整
``docker compose build`` + ``run --rm smoke`` 才暴露：

1. ``.dockerignore`` 要把索引整目录打进镜像，且 re-include 排在 ``*.db``/``*.vec``
   之后（该文件按"最后匹配生效"解析）；
2. compose 的 smoke 服务要给 trace 一个可写落点——根文件系统是只读的，运行收尾
   写不动 trace 就会整轮失败；
3. CLI ``--json`` 载荷里 ``retriever_runtime_status`` 的字段名要和探针读的一致。

另外两条与容器直接相关的降级路径也在这里：会话目录建不出来时 checkpointer 退成
MemorySaver（而不是让装配崩掉），以及探针在"没索引/跑挂了"时给出结构化失败而不是
抛异常打断整轮冒烟。
"""

from __future__ import annotations

import inspect
import json
import subprocess
from contextlib import ExitStack
from pathlib import Path

import pytest

from water_agent.cli import main
from water_agent.ingestion.pipeline import ingest_to_trivium
from water_agent.runtime.agentic import build_cli_checkpointer
from water_agent.webapp import smoke

REPO_ROOT = Path(__file__).resolve().parents[1]

# 迷你语料：够 mock 模型命中一条并引用即可，绝不碰 data/indexes/ 下的真实索引
# （3.5 MB、构建要 DASHSCOPE 凭据，单测既不该依赖它也不该改动它）。
MINI = [
    {
        "spec_id": "GB50013-2018",
        "article": "9.0.1",
        "title": "设计流量",
        "text": "给水管道设计秒流量应根据用水定额确定。",
        "fragment_id": "smoke-1",
        "source_line": 1,
    },
    {
        "spec_id": "GB50014-2021",
        "article": "9.0.2",
        "title": "排水体制",
        "text": "排水体制分流制或合流制的选择应因地制宜。",
        "fragment_id": "smoke-2",
        "source_line": 2,
    },
]
MINI_SOURCE_IDS = {"GB50013-2018 9.0.1", "GB50014-2021 9.0.2"}
# 与镜像里那份索引同维，让 mock_embed 走的是生产同一条路径。
MINI_DIM = 512


class _StubProvider:
    """确定性伪 embedding：入库不需要凭据，也不需要网络。"""

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.config = type(
            "Cfg",
            (),
            {
                "dimensions": dim,
                "model": "stub",
                "index_version": f"stub__{dim}__curated-v1",
                "fallback_enabled": False,
            },
        )()

    def embed(self, text: str) -> list[float]:
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [((digest[i % len(digest)] / 255.0) - 0.5) for i in range(self.dim)]


def _build_mini_index(root: Path) -> Path:
    """真库入库：``ingest_to_trivium`` 会 flush 出 ``.text`` 侧文件，与镜像里一致。"""

    clauses = root / "clauses"
    clauses.mkdir(parents=True, exist_ok=True)
    by_spec: dict[str, list[dict]] = {}
    for row in MINI:
        by_spec.setdefault(str(row["spec_id"]), []).append(row)
    for spec, rows in by_spec.items():
        (clauses / f"{spec}.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
            encoding="utf-8",
        )
    db = root / "mini.db"
    provider = _StubProvider(MINI_DIM)
    for path in sorted(clauses.glob("*.jsonl")):
        ingest_to_trivium(path, db, embedding_provider=provider, curated=True, dry_run=False)
    return db


def _dockerignore_lines() -> list[str]:
    text = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
    return [
        stripped
        for stripped in (line.strip() for line in text.splitlines())
        if stripped and not stripped.startswith("#")
    ]


def _smoke_service_block() -> str:
    """smoke 服务那一段，去掉注释行——否则注释里提到的挂载名会被当成真挂载。"""

    compose = (REPO_ROOT / "compose.yml").read_text(encoding="utf-8")
    block = compose[compose.index("\n  smoke:") : compose.index("\n  reverse-proxy:")]
    return "\n".join(
        line for line in block.splitlines() if not line.strip().startswith("#")
    )


class TestProbePremises:
    """探针成立的两个部署前提：索引在镜像里，trace 有地方写。"""

    def test_dockerignore_ships_the_index_after_the_sidecar_denials(self) -> None:
        """索引的 re-include 必须排在 ``*.db``/``*.vec`` 之后，否则被逐个侧文件剥掉。"""
        lines = _dockerignore_lines()

        assert "!data/indexes/" in lines
        assert "!data/indexes/**" in lines, "只放目录不放 ``**``，库文件与侧文件都进不来"
        first = min(lines.index("!data/indexes/"), lines.index("!data/indexes/**"))
        last = max(lines.index("!data/indexes/"), lines.index("!data/indexes/**"))

        # .dockerignore 按最后匹配生效：这些 deny 排在 re-include 之后就等于没打进索引。
        # .lock/.flush_ok 缺一个都打不开库，.text/.text.meta 缺了 hybrid 退成纯向量。
        for deny in ("*.db", "*.db-*", "*.wal", "*.vec"):
            assert deny in lines, f"{deny} 不在 .dockerignore 里，断言已失焦"
            assert lines.index(deny) < first, f"{deny} 排在索引 re-include 之后，索引会被剥掉"

        # ``!data/`` 复活了 data/ 下所有 deny，所以那份 re-deny 清单不得涵盖索引。
        revived = [line for line in lines[last + 1 :] if line.startswith("data/indexes")]
        assert not revived, f"索引在 re-include 之后又被拒了：{revived}"

    def test_compose_smoke_service_can_write_traces_on_a_read_only_rootfs(self) -> None:
        """冒烟自己要跑一次 agentic run，收尾写 trace；落点必须是它写得动的 tmpfs。"""
        block = _smoke_service_block()

        values = [
            line.split(":", 1)[1].strip()
            for line in block.splitlines()
            if "WATER_AGENT_TRACE_DIR:" in line
        ]
        assert len(values) == 1, f"smoke 服务里 WATER_AGENT_TRACE_DIR 命中 {len(values)} 行"
        assert "read_only: true" in block, "冒烟也跑在只读根文件系统上，这正是要测的条件"
        assert "tmpfs: [/tmp]" in block
        assert values[0].startswith("/tmp/"), f"trace 落点 {values[0]} 不在 tmpfs 上，写不动"
        # 冒烟的 trace 是一次性产物，不该混进 app 的 trace 卷（里面是真实用户问答原文）。
        assert "app_traces" not in block

    def test_main_still_asserts_the_probe_did_not_fall_back(self) -> None:
        """探针留着但断言被删掉，本地一切照绿——所以这里钉住 main 里的接线。"""
        source = inspect.getsource(smoke.main)

        assert "_run_agentic_probe()" in source
        assert "is_fallback" in source, "只看 status == succeeded 的话，回落 fixture 也算通过"
        for name in (
            "agentic_run_completes_in_image",
            "agentic_run_used_the_shipped_corpus",
            "agentic_run_has_a_text_index",
            "agentic_run_cited_the_corpus",
        ):
            assert name in source, f"检查 {name} 不再被记录"


class TestCliJsonContract:
    """探针读的是 CLI ``--json`` 载荷，字段名与取值语义在这里钉死。"""

    @staticmethod
    def _run(tmp_path: Path, capsys: pytest.CaptureFixture, *extra: str) -> dict:
        """跑一次与探针同形的 agentic run，返回它读的那份 JSON。"""

        code = main(
            [
                "给水管道的设计秒流量如何确定",
                "--mode",
                "agentic",
                "--mock-generator",
                "--json",
                "--session-dir",
                str(tmp_path / "sessions"),
                # 探针刻意不传 --trace-dir（要测部署给的 WATER_AGENT_TRACE_DIR）；
                # 单测反过来必须传，否则 trace 落进仓库的 data/traces/。
                "--trace-dir",
                str(tmp_path / "traces"),
                *extra,
            ]
        )
        out = capsys.readouterr().out
        assert code == 0, out
        return json.loads(out[out.index("{") : out.rindex("}") + 1])

    def test_explicit_db_path_reports_a_non_fallback_retriever(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        db = _build_mini_index(tmp_path / "index")

        payload = self._run(tmp_path, capsys, "--db-path", str(db))

        assert payload["status"] == "succeeded"
        assert payload["db_path"] == str(db)
        runtime = payload["retriever_runtime_status"]
        # 探针断言的那两个字段
        assert runtime["is_fallback"] is False
        assert runtime["db_opened"] is True
        # 只读打开 + 持久化文本索引：镜像的只读根文件系统只能走这条路
        assert runtime["access_mode"] == "read_only"
        assert runtime["text_index_ready"] is True
        assert runtime["text_index_source"] == "persisted"
        # 走 persisted 时 ``text_indexed_nodes`` 按设计保持 0（"本进程没重建过"），
        # 不代表没有文本索引——冒烟的 detail 因此报 source/access_mode 而不是它。
        assert runtime["text_indexed_nodes"] == 0
        # ``fallback_kind == "mock"`` 指的是 embedding 是假的，语料是真的——别把
        # 探针改成断言 kind == "none"，那会连"假模型 + 真索引"这个组合一起否掉。
        assert runtime["fallback_kind"] == "mock"
        assert runtime["db_error"] is None
        assert payload["citations"], "mock 模型会引用首条命中，没引用说明检索没结果"
        assert {c["source_id"] for c in payload["citations"]} <= MINI_SOURCE_IDS

    def test_configured_index_is_used_without_an_explicit_flag(
        self, tmp_path: Path, capsys: pytest.CaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """端到端复现 item 3 的缺陷：``WATER_AGENT_INDEX_PATH`` + 假模型必须真检索。

        修好前这条路会无声回落 2 KB fixture —— 答案照样 succeeded、照样有引用，
        只是引的不是部署配的那份语料。
        """
        from water_agent.retrieval.trivium import INDEX_PATH_ENV

        db = _build_mini_index(tmp_path / "configured")
        monkeypatch.setenv(INDEX_PATH_ENV, str(db))

        payload = self._run(tmp_path, capsys)

        assert payload["db_path"] == str(db)
        assert payload["retriever_runtime_status"]["is_fallback"] is False
        assert {c["source_id"] for c in payload["citations"]} <= MINI_SOURCE_IDS


class TestCheckpointerDegradation:
    """镜像内 ``/app/data`` 只读且不收 ``data/sessions``，建会话库这一步会失败。"""

    def test_unwritable_session_dir_degrades_to_memory_and_says_so(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """容器里真实触发的是 ``OSError(Errno 30)``；这里用"父路径是文件"落到同一分支，
        因为 ``chmod`` 拦不住以 root 跑的 CI。关键是别让装配在建目录这步就死。
        """
        from langgraph.checkpoint.memory import MemorySaver

        blocked = tmp_path / "sessions"
        blocked.write_text("not a directory", encoding="utf-8")

        with ExitStack() as stack:
            saver = build_cli_checkpointer(blocked, stack)

        assert isinstance(saver, MemorySaver)
        # 降级但不静默：stderr 上要有结构化说明，说清这轮不落盘。
        err = capsys.readouterr().err
        payload = json.loads(err.strip().splitlines()[-1])
        assert payload["ok"] is False
        assert "MemorySaver" in payload["error"]

    def test_writable_session_dir_still_persists_to_sqlite(self, tmp_path: Path) -> None:
        """降级路径不得吃掉正常路径：目录可写时照旧落盘。"""
        from langgraph.checkpoint.sqlite import SqliteSaver

        with ExitStack() as stack:
            saver = build_cli_checkpointer(tmp_path / "deep" / "sessions", stack)
            assert isinstance(saver, SqliteSaver)

        assert (tmp_path / "deep" / "sessions" / "sessions.sqlite").exists()


class TestProbeFailurePaths:
    """探针不抛异常：每条失败都变成一句可读的 detail，让其余检查照常跑完。"""

    @staticmethod
    def _forbid_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
        def _never(*args: object, **kwargs: object) -> None:
            raise AssertionError("选不出索引时不该起子进程")

        monkeypatch.setattr(smoke.subprocess, "run", _never)

    def test_missing_index_fails_before_running_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "water_agent.retrieval.trivium.resolve_index_path", lambda *a, **k: None
        )
        self._forbid_subprocess(monkeypatch)

        payload, failure = smoke._run_agentic_probe()

        assert payload == {}
        assert "no retrieval index" in failure

    def test_ambiguous_index_selection_is_reported_not_raised(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from water_agent.retrieval.trivium import IndexSelectionError

        def _raise(*_args: object, **_kwargs: object) -> None:
            raise IndexSelectionError("目录里没有那份具名索引")

        monkeypatch.setattr("water_agent.retrieval.trivium.resolve_index_path", _raise)
        self._forbid_subprocess(monkeypatch)

        payload, failure = smoke._run_agentic_probe()

        assert payload == {}
        assert "index selection failed" in failure
        assert "具名索引" in failure

    def _patch_index(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        index = tmp_path / "index.db"
        index.write_bytes(b"")
        monkeypatch.setattr(
            "water_agent.retrieval.trivium.resolve_index_path", lambda *a, **k: index
        )
        return index

    def _capture_argv(
        self, monkeypatch: pytest.MonkeyPatch, *, returncode: int, stdout: str, stderr: str = ""
    ) -> list[list[str]]:
        seen: list[list[str]] = []

        def _fake(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            seen.append(list(command))
            return subprocess.CompletedProcess(command, returncode, stdout, stderr)

        monkeypatch.setattr(smoke.subprocess, "run", _fake)
        return seen

    def test_successful_run_returns_the_payload_and_keeps_the_deployed_trace_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        index = self._patch_index(monkeypatch, tmp_path)
        seen = self._capture_argv(
            monkeypatch, returncode=0, stdout=json.dumps({"status": "succeeded"})
        )

        payload, failure = smoke._run_agentic_probe()

        assert failure == ""
        assert payload == {"status": "succeeded"}
        argv = seen[0]
        assert argv[1:3] == ["-m", "water_agent.cli"]
        assert "--mock-generator" in argv and "--json" in argv
        assert argv[argv.index("--mode") + 1] == "agentic"
        assert argv[argv.index("--db-path") + 1] == str(index)
        # trace 落点刻意不覆盖：部署给的 WATER_AGENT_TRACE_DIR 写不动的话这轮要失败。
        assert "--trace-dir" not in argv
        # 会话库必须落在临时目录：镜像里 data/ 只读，落那儿会退成进程内 MemorySaver。
        session_dir = Path(argv[argv.index("--session-dir") + 1])
        assert not session_dir.is_relative_to(Path("/app/data"))

    def test_nonzero_exit_reports_the_last_stderr_line_only(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._patch_index(monkeypatch, tmp_path)
        self._capture_argv(
            monkeypatch,
            returncode=2,
            stdout="",
            stderr='{"ok": false, "error": "早先的噪声"}\n{"ok": false, "error": "真正的死因"}\n',
        )

        payload, failure = smoke._run_agentic_probe()

        assert payload == {}
        assert "exit=2" in failure
        assert "真正的死因" in failure
        assert "早先的噪声" not in failure

    def test_non_json_stdout_is_described_without_echoing_the_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._patch_index(monkeypatch, tmp_path)
        self._capture_argv(
            monkeypatch, returncode=0, stdout="给水管道设计秒流量应根据用水定额确定。", stderr=""
        )

        payload, failure = smoke._run_agentic_probe()

        assert payload == {}
        assert "not JSON" in failure
        # 冒烟日志不回显正文：那是用户问答原文的落点之一。
        assert "用水定额" not in failure

    def test_timeout_is_reported_instead_of_propagating(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._patch_index(monkeypatch, tmp_path)

        def _timeout(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
            raise subprocess.TimeoutExpired(command, smoke.AGENTIC_TIMEOUT_SECONDS)

        monkeypatch.setattr(smoke.subprocess, "run", _timeout)

        payload, failure = smoke._run_agentic_probe()

        assert payload == {}
        assert "exceeded" in failure
