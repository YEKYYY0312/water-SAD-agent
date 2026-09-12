"""Step 35 documentation contract: scope, threat coverage, and release exclusions."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DESIGN_DIR = REPO_ROOT / "docs" / "design"


def _read(name: str) -> str:
    return (DESIGN_DIR / name).read_text(encoding="utf-8")


def test_step35_required_artifacts_exist_and_are_scoped() -> None:
    spec = _read("15-p5-web-memory.md")
    assert "Step 35 规格、威胁模型与开源预检产出" in spec
    assert "不实现 Web、认证、数据库、迁移、SSE 或长期记忆" in spec
    assert "ProviderGateway" in spec
    assert "ToolRegistry" in spec
    assert "GuardrailPipeline" in spec
    assert "Principal.authenticated" in spec
    assert "不为 Web、记忆或四 Agent 复制一套" in spec


def test_step35_threat_model_covers_required_attack_surfaces() -> None:
    threat = _read("p5-threat-model.md")
    required_terms = (
        "身份伪造",
        "IDOR",
        "CSRF",
        "session fixation",
        "跨用户和跨项目记忆泄漏",
        "记忆投毒",
        "secret 落盘",
        "删除残留",
        "SSE 未授权重连",
        "备份泄漏",
        "Prompt/tool schema 膨胀",
        "真实图纸",
        "规范文本",
        "用户数据",
    )
    for term in required_terms:
        assert term in threat, term

    assert "blocked" in threat
    assert "确定性正则规则" in threat
    assert "不能覆盖任意注入改写" in threat


def test_step35_release_inventory_forbids_sensitive_publication() -> None:
    inventory = _read("p5-release-data-inventory.md")
    forbidden_terms = (
        ".env",
        "data/sessions/",
        "data/traces/",
        "data/eval/traces/",
        "data/indexes/",
        "真实设计院 DXF/DWG",
        "未授权规范全文",
        "用户上传",
    )
    for term in forbidden_terms:
        assert term in inventory, term

    assert "allowlist" in inventory
    assert "公开发布保持 blocked" in inventory


def test_step35_audit_reports_are_value_free_and_evidence_based() -> None:
    dependency = _read("p5-dependency-license-sbom-preflight-2026-09-12.md")
    secret = _read("p5-secret-scan-2026-09-12.md")
    evidence = dependency + secret
    assert "uv lock --check" in evidence
    assert "uv audit --locked" in evidence
    assert "uv pip check --python _venv/bin/python" in evidence
    assert "必须轮换" in secret
    assert "不打印、复制、验证、调用或轮换 secret" in secret
    assert not re.search(r"(?:sk-|AIza|AKIA|gh[pousr]_)[A-Za-z0-9_-]{20,}", evidence)
