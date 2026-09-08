"""Step 5 测试：引用声明工具 cite_clauses + 确定性引用校验器 validate_citations。

覆盖：引用 ∈ context 通过 / ∉ context 拦截（含可行动建议文本）/ 版本未知仅标记 /
cite_clauses 工具声明与注册 / RAG 图 observation 回流。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from water_agent.domain.citation import CitationClaim, validate_citations
from water_agent.domain.state import default_state
from water_agent.generation.base import Generator
from water_agent.generation.mock import MockGenerator
from water_agent.graphs.rag import build_rag_graph
from water_agent.retrieval.in_memory import InMemoryRetriever
from water_agent.retrieval.loader import load_clauses_jsonl
from water_agent.tools.cite import build_cite_clauses_tool
from water_agent.tools.registry import ToolRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "data" / "fixtures" / "rag_fixture.jsonl"


class TestCitationClaim:
    def test_fields_and_spec_id(self) -> None:
        claim = CitationClaim(source_id="GB50015-2019 5.4.2", declared_by="model")
        assert claim.source_id == "GB50015-2019 5.4.2"
        assert claim.spec_version is None
        assert claim.declared_by == "model"
        assert claim.message_id == ""
        # spec_id 是 source_id 的规范号部分（Step 8 版本注册表消费）
        assert claim.spec_id == "GB50015-2019"

    def test_extra_forbidden(self) -> None:
        with pytest.raises(ValueError):
            CitationClaim(source_id="GB50015 5.4.2", unexpected=1)  # type: ignore[call-arg]


class TestValidateCitations:
    def test_in_context_passes(self) -> None:
        result = validate_citations(["FIX 1.1.1"], ["FIX 1.1.1", "FIX 1.1.2"])
        assert result.ok is True
        assert result.invalid == []
        assert result.missing_version == []
        assert result.errors == []

    def test_out_of_context_rejected(self) -> None:
        result = validate_citations(["FIX 9.9.9"], ["FIX 1.1.1"])
        assert result.ok is False
        assert result.invalid == ["FIX 9.9.9"]
        assert result.errors, "errors 不得为空"

    def test_fake_citation_actionable_error(self) -> None:
        """验收单测：validate_citations(["FAKE 0.0.0"], ["FIX 1.1.1"]) → ok=False 且 errors 含可行动建议。"""
        result = validate_citations(["FAKE 0.0.0"], ["FIX 1.1.1"])
        assert result.ok is False
        assert result.invalid == ["FAKE 0.0.0"]
        assert result.errors, "errors 必须含可行动建议文本（而非空字符串）"
        joined = " ".join(result.errors)
        assert "不在本次检索结果" in joined
        assert "FIX 1.1.1" in joined  # 候选 source_id 可见，供模型自纠

    def test_missing_version_marked_not_invalid(self) -> None:
        """版本未知仅标记进 missing_version，不判失败、不判废止。"""

        class FakeRegistry:
            def get_spec_status(self, spec_id: str) -> dict:
                return {"status": "unknown"}

        result = validate_citations(["FIX 1.1.1"], ["FIX 1.1.1"], version_registry=FakeRegistry())
        assert result.ok is True  # 引用本身在 context 内，不因版本未知失败
        assert result.invalid == []
        assert result.missing_version == ["FIX 1.1.1"]

    def test_normalize_whitespace_and_dedupe(self) -> None:
        result = validate_citations([" FIX 1.1.1 ", "FIX 1.1.1", "FIX 1.1.2"], ["FIX 1.1.1"])
        assert result.ok is False
        assert result.invalid == ["FIX 1.1.2"]


class TestVersionRegistryWiring:
    """Step 8：validate_citations 接真实 VersionRegistry；版本状态写回 CitationClaim。"""

    @pytest.fixture
    def registry(self, tmp_path):
        import json

        from water_agent.ingestion.version_registry import VersionRegistry

        path = tmp_path / "versions.json"
        path.write_text(
            json.dumps(
                # 夹具须是一条**合法的已核实记录**：current 必须带 effective_date
                # （2026-09-08 加固，缺它会被降级 unknown）。本组测的是引用校验接线，
                # 不是核实纪律，所以补齐依据而不是放宽守卫。
                [{"spec_id": "GB50015", "version": "2019", "status": "current",
                  "effective_date": "2020-03-01", "source": "人工确认", "verified_at": "2026-09-03"}]
            ),
            encoding="utf-8",
        )
        return VersionRegistry(path)

    def test_known_version_resolved_and_unknown_marked(self, registry) -> None:
        ctx = ["GB50015-2019 5.4.2", "GB99999-2099 1.1.1"]
        result = validate_citations(ctx, ctx, version_registry=registry)
        assert result.ok is True
        assert result.errors == [], "版本未知不进 errors（不是模型能重试纠正的事）"
        assert result.version_status == {"GB50015-2019 5.4.2": "current", "GB99999-2099 1.1.1": "unknown"}
        assert result.missing_version == ["GB99999-2099 1.1.1"]
        assert len(result.warnings) == 1 and "GB99999-2099 1.1.1" in result.warnings[0]

    def test_superseded_version_warns_but_not_fails(self, tmp_path) -> None:
        """已知非现行（superseded/deprecated）：不判失败，但必须产生可见警示（S8-02）。"""
        import json

        from water_agent.ingestion.version_registry import VersionRegistry

        path = tmp_path / "versions.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "spec_id": "GB50015",
                        "version": "2003",
                        "status": "superseded",
                        "successor": "GB50015-2019",
                        "source": "人工确认",
                        "verified_at": "2026-09-03",
                    }
                ]
            ),
            encoding="utf-8",
        )
        registry = VersionRegistry(path)
        cited = ["GB50015-2003 5.4.2"]
        result = validate_citations(cited, cited, version_registry=registry)
        assert result.ok is True
        assert result.missing_version == []
        assert result.version_status == {cited[0]: "superseded"}
        assert any("superseded" in warning for warning in result.warnings)

    def test_version_mismatch_is_unknown_not_inferred(self, registry) -> None:
        """注册表只录了 2019 版：引用 2003 版 → unknown（不因同一规范号推断为现行）。"""
        result = validate_citations(["GB50015-2003 1.1.1"], ["GB50015-2003 1.1.1"], version_registry=registry)
        assert result.version_status["GB50015-2003 1.1.1"] == "unknown"

    def test_enrich_claims_writes_version_fields(self, registry) -> None:
        from water_agent.domain.citation import enrich_claims

        ctx = ["GB50015-2019 5.4.2", "FIX 1.1.1"]
        check = validate_citations(ctx, ctx, version_registry=registry)
        claims = enrich_claims([CitationClaim(source_id=c) for c in ctx], check)
        assert claims[0].spec_version == "2019" and claims[0].version_status == "current"
        assert claims[1].spec_version is None and claims[1].version_status == "unknown"

    def test_registry_exception_degrades_to_unknown(self) -> None:
        class Broken:
            def get_spec_status(self, spec_id):
                raise RuntimeError("boom")

        result = validate_citations(["FIX 1.1.1"], ["FIX 1.1.1"], version_registry=Broken())
        assert result.ok is True
        assert result.version_status["FIX 1.1.1"] == "unknown"


class TestCiteClausesTool:
    def test_declares_and_registers_claims(self) -> None:
        sink: list[CitationClaim] = []
        registry = ToolRegistry()
        registry.register(build_cite_clauses_tool(claims_sink=sink))
        result = registry.invoke(
            "cite_clauses",
            article_ids=["GB50015-2019 5.4.2", "GB50014-2021 3.1.1"],
        )
        assert result["registered"] is True
        assert result["cited"] == ["GB50015-2019 5.4.2", "GB50014-2021 3.1.1"]
        assert [c.source_id for c in sink] == result["cited"]
        assert all(c.declared_by == "model" for c in sink)
        # 返回的 claims 与 sink 一致，Step 6 可直接写入 state
        assert [claim["source_id"] for claim in result["claims"]] == result["cited"]

    def test_empty_list_rejected_structured(self) -> None:
        registry = ToolRegistry()
        registry.register(build_cite_clauses_tool())
        result = registry.invoke("cite_clauses", article_ids=[])
        assert result["error"] is True
        assert result["code"] == "INVALID_ARGUMENTS"
        assert result["suggestions"], "结构化错误必须带建议"

    def test_bad_format_rejected_structured(self) -> None:
        registry = ToolRegistry()
        registry.register(build_cite_clauses_tool())
        result = registry.invoke("cite_clauses", article_ids=["没有空格的条款号"])
        assert result["error"] is True
        assert result["code"] == "INVALID_ARGUMENTS"

    def test_registered_claims_flow_into_validator(self) -> None:
        sink: list[CitationClaim] = []
        registry = ToolRegistry()
        registry.register(build_cite_clauses_tool(claims_sink=sink))
        declared = registry.invoke("cite_clauses", article_ids=["FIX 1.1.1"])
        assert declared["registered"] is True
        check = validate_citations(
            [c.source_id for c in sink],
            ["FIX 1.1.1", "FIX 1.1.2"],
        )
        assert check.ok is True


class _FakeBadGenerator(Generator):
    """生成不在检索结果中的引用，用于测试校验失败路径与 observation 回流。"""

    def generate(self, query: str, context):
        from water_agent.domain.models import Citation, GeneratedAnswer

        bad = Citation(spec_id="FAKE", article="0.0.0", title="不存在的条款", text_snippet="编造")
        return GeneratedAnswer(answer="包含编造引用的回答。", citations=[bad], intent="rag")


class TestRagGraphCitationObservation:
    @pytest.fixture(scope="module")
    def clauses(self):
        return load_clauses_jsonl(FIXTURE_PATH)

    def test_bad_citation_records_actionable_errors(self, clauses) -> None:
        """invalid 时把可行动错误文本作为 observation 记录进 errors（Step 6 回流用）。"""
        app = build_rag_graph(InMemoryRetriever(clauses), _FakeBadGenerator())
        result = app.invoke(default_state(query="给水管道的设计秒流量"))
        assert result["status"] == "failed"
        # observation：validate_citations 的可行动错误文本已回流进 errors
        assert any("不在本次检索结果" in e for e in result["errors"])
        assert any("FAKE 0.0.0" in e for e in result["errors"])

    def test_normal_path_state_citations_are_claims(self, clauses) -> None:
        """legacy 生成器的 Citation 在 state 层统一为 CitationClaim。"""
        app = build_rag_graph(InMemoryRetriever(clauses), MockGenerator())
        result = app.invoke(default_state(query="给水管道的设计秒流量"))
        assert result["status"] == "succeeded"
        assert result["citations"]
        assert all(isinstance(c, CitationClaim) for c in result["citations"])
        assert all(c.source_id for c in result["citations"])
        # Step 8：legacy 图默认接 data/versions.json；fixture 的 FIX 未录入 → 只标记 unknown，不影响成功
        assert all(c.version_status == "unknown" for c in result["citations"])
        assert not any("版本" in e for e in result["errors"]), "版本未知不得进 errors"
