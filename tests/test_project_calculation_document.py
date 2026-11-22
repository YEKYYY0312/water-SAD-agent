"""T2-7D candidate Word: offline, node-local values and provenance."""

from copy import deepcopy
from io import BytesIO

import pytest
from docx import Document
from test_project_calculation_execution import fixture

from water_agent.multi_agent.project_calculation import _identity, execute_project_plan


def candidate():
    plan, ctx, state = fixture()
    result = execute_project_plan(plan, ctx, state)
    assert result.status == "ready"
    return {
        "context": _identity(ctx),
        "plan": plan,
        "result": result.model_dump(),
        "execution_manifest_hash": result.manifest_hash,
    }


def render(run):
    from water_agent.docgen.project_calculation_document import (
        project_document_text,
        render_project_calculation_docx,
    )

    content = render_project_calculation_docx(run)
    assert isinstance(content, bytes) and content.startswith(b"PK")
    return content, project_document_text(content)


def test_T27_074_word_preserves_nodes_precision_units_and_sources():
    run = candidate()
    original = deepcopy(run)
    content, text = render(run)
    assert run == original
    doc = Document(BytesIO(content))
    headings = [p.text for p in doc.paragraphs if p.style.name == "Heading 1"]
    assert headings == ["v — 流速", "hf — 沿程水头损失", "H — 所需扬程"]
    # Same name stays in its own node, never merged through the old renderer.
    assert sum(row.cells[0].text == "diameter_mm" for table in doc.tables for row in table.rows) == 2
    for record in run["result"]["node_records"]:
        assert record["algorithm_version"] in text
        assert record["contract_version"] in text
        assert record["tool_identity_version"] in text
        assert record["case_id"] in text
        for key, value in record["resolved_inputs"].items():
            assert repr(value) in text
            assert record["units"][key] in text
            source = record["input_sources"][key]
            if source["kind"] == "user":
                assert source["input_id"] in text and source["input_set_id"] in text
            else:
                for field in ("node_id", "artifact_id", "revision", "case_id", "output_key", "payload_hash"):
                    assert str(source[field]) in text
        for value in record["outputs"].values():
            assert repr(value) in text
    assert "v = (Q / 1000) / (π × (d / 1000)² / 4)" in text
    assert "hf = f × (L / (d / 1000)) × v² / (2 × g)" in text
    assert "H = static_head_m + friction_head_m + local_head_m + residual_head_m" in text


def test_candidate_state_and_limitations_cannot_claim_publication():
    run = candidate()
    run["status"] = "published"
    run["release_manifest_hash"] = "f" * 64
    _, text = render(run)
    assert "ready_for_approval" in text and "published" not in text
    assert "发布审批见随附记录" in text
    assert "仅供参考" in text
    assert "参数来源和工程适用性未验证" in text
    assert "未检索条文" in text and "未做选泵合规校核" in text
    assert run["context"]["project_id"] in text
    assert run["context"]["plan_id"] in text
    assert run["context"]["run_id"] in text
    assert run["context"]["owner_user_id"] not in text
    assert run["execution_manifest_hash"] in text
    assert run["release_manifest_hash"] not in text


def test_warnings_assumptions_remain_in_each_node_with_original_source():
    run = candidate()
    note = {"source_node_id": "v", "code": "synthetic_warning", "message": "上游合成警示原句", "severity": "warning"}
    for record in run["result"]["node_records"]:
        record["warnings"].append(dict(note))
        record["assumptions"].append({**note, "message": "上游假设完整原句"})
    _, text = render(run)
    assert text.count("上游合成警示原句") == 3
    assert text.count("上游假设完整原句") == 3
    assert text.count("来源节点=v") >= 3
    assert "工具提示，未进行规范校核" in text
    for record in run["result"]["node_records"]:
        for note in record["assumptions"]:
            assert note["message"] in text


@pytest.mark.parametrize("fault", ["node_missing", "node_order", "missing_unit", "missing_source", "missing_input", "extra_output", "unverified", "wrong_manifest", "not_ready", "bool", "extra_record_field", "context_drift"])
def test_incomplete_candidate_rejected_before_export(fault):
    run = candidate()
    records = run["result"]["node_records"]
    if fault == "node_missing":
        records.pop()
    elif fault == "node_order":
        records.reverse()
    elif fault == "missing_unit":
        records[0]["units"].pop("diameter_mm")
    elif fault == "missing_source":
        records[0]["input_sources"].pop("diameter_mm")
    elif fault == "missing_input":
        records[0]["resolved_inputs"].pop("diameter_mm")
    elif fault == "extra_output":
        records[0]["outputs"]["invented"] = 1.0
    elif fault == "unverified":
        records[0]["status"] = "started"
    elif fault == "wrong_manifest":
        run["execution_manifest_hash"] = "a" * 64
    elif fault == "not_ready":
        run["result"]["status"] = "failed"
    elif fault == "bool":
        records[0]["resolved_inputs"]["flow_lps"] = True
    elif fault == "extra_record_field":
        records[0]["invented"] = "drop silently"
    else:
        run["context"]["run_id"] = "00000000-0000-0000-0000-000000000000"
    from water_agent.docgen.project_calculation_document import render_project_calculation_docx

    with pytest.raises(ValueError):
        render_project_calculation_docx(run)
