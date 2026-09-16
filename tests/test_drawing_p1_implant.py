"""drawing_p1_implant 套件测试：生成器同步性 + runner 端到端 + 反向例。

与 tests/test_drawing_eval.py 的关系：那套测 drawing_p0（全合成 smoke），
这套测 drawing_p1_implant（真实底图 + 植入缺陷）。判分口径同一 harness
（drawing_eval.run_suite），但本套件 DXF 不入库 —— 测试全部跳过需要底图
的用例（real_samples 未检出时结构化 SKIP），只保留不依赖文件的纯逻辑断言。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_DIR = REPO_ROOT / "tests" / "eval" / "drawing_p1_implant"
CASES_DIR = SUITE_DIR / "cases"
REAL_SAMPLES = REPO_ROOT / "data" / "drawings" / "real_samples"

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _real_samples_available() -> bool:
    return (REAL_SAMPLES / "P-01.dxf").is_file() and (REAL_SAMPLES / "S-03.dxf").is_file()


requires_real_samples = pytest.mark.skipif(
    not _real_samples_available(),
    reason="real_samples 未检出（gitignored）：drawing_p1_implant 需本地底图",
)


class TestSuiteHonesty:
    def test_suite_metadata_declares_implants(self) -> None:
        meta = json.loads((SUITE_DIR / "suite.json").read_text(encoding="utf-8"))
        assert meta["suite"] == "drawing_p1_implant"
        assert meta["real_samples"] is True
        assert meta["synthetic_implants"] is True
        assert meta["accepts_recall_gate"] is False

    def test_gold_only_references_registered_rules(self) -> None:
        from water_agent.drawing.rules import ALL_RULES  # type: ignore[import-untyped]

        known = {r.rule_id for r in ALL_RULES}
        for gold_path in sorted(CASES_DIR.glob("*.json")):
            gold = json.loads(gold_path.read_text(encoding="utf-8"))
            assert gold["synthetic_implants"] is True
            for defect in gold["defects"]:
                assert defect["rule_id"] in known, gold_path.name

    def test_readme_states_not_recall_evidence(self) -> None:
        readme = (SUITE_DIR / "README.md").read_text(encoding="utf-8")
        assert "只测植入段" in readme
        assert "accepts_recall_gate=false" in readme


class TestGeneratorSync:
    @requires_real_samples
    def test_every_gold_has_its_dxf(self) -> None:
        for gold_path in sorted(CASES_DIR.glob("*.json")):
            gold = json.loads(gold_path.read_text(encoding="utf-8"))
            assert (CASES_DIR / gold["drawing"]).is_file(), gold["case_id"]

    @requires_real_samples
    def test_gold_handles_exist_in_their_drawings(self) -> None:
        from water_agent.drawing.dxf_reader import read_drawing  # type: ignore[import-untyped]
        from water_agent.runtime.governance.pipeline import (  # type: ignore[import-untyped]
            ToolPolicy,
        )

        policy = ToolPolicy(allowed_read_dirs=frozenset({CASES_DIR.resolve()}))
        checked = 0
        for gold_path in sorted(CASES_DIR.glob("*.json")):
            gold = json.loads(gold_path.read_text(encoding="utf-8"))
            if gold.get("expected_status") != "succeeded":
                continue
            extraction = read_drawing(
                gold["drawing"].removesuffix(".dxf"), root=CASES_DIR, policy=policy
            )
            handles = {e.handle for e in extraction.entities}
            for defect in gold["defects"]:
                assert defect["handle"] in handles, (
                    f"{gold['case_id']}: gold handle {defect['handle']} 不在图内，"
                    "请重跑 build_implants.py"
                )
                checked += 1
        assert checked == 4, f"期望 4 条植入 gold 缺陷，实际 {checked}"

    @requires_real_samples
    def test_implants_visible_under_default_truncation(self) -> None:
        """植入段必须在默认 max_entities=500 下可见（否则 runner 永远判不出）。"""
        from water_agent.drawing.dxf_reader import read_drawing  # type: ignore[import-untyped]
        from water_agent.runtime.governance.pipeline import (  # type: ignore[import-untyped]
            ToolPolicy,
        )

        policy = ToolPolicy(allowed_read_dirs=frozenset({CASES_DIR.resolve()}))
        for gold_path in sorted(CASES_DIR.glob("*.json")):
            gold = json.loads(gold_path.read_text(encoding="utf-8"))
            extraction = read_drawing(
                gold["drawing"].removesuffix(".dxf"), root=CASES_DIR, policy=policy
            )
            assert extraction.truncated in (True, False)
            layers = {e.layer for e in extraction.entities}
            assert "DR-PIPE" in layers, gold["case_id"]


class TestRunnerEndToEnd:
    @requires_real_samples
    def test_all_cases_pass_with_full_recall_zero_fpr(self, tmp_path: Path) -> None:
        from water_agent.harness.drawing_eval import run_suite  # type: ignore[import-untyped]

        payload, path = run_suite(SUITE_DIR, report_path=tmp_path / "implant_head.json")
        assert payload["total"] == payload["passed"] == 5
        assert payload["failed"] == 0
        assert payload["defect_recall"] == 1.0
        assert payload["false_positive_rate"] == 0.0
        assert payload["aggregate"]["gold_defects_total"] == 4
        assert payload["aggregate"]["matched_defects_total"] == 4
        assert payload["aggregate"]["false_positives_total"] == 0
        assert path.is_file()

    @requires_real_samples
    def test_broken_gold_fails_the_case(self, tmp_path: Path) -> None:
        import shutil

        from water_agent.harness.drawing_eval import run_suite  # type: ignore[import-untyped]

        tampered = tmp_path / "suite"
        shutil.copytree(SUITE_DIR, tampered, ignore=shutil.ignore_patterns("*.dxf"))
        shutil.copytree(CASES_DIR, tampered / "cases", dirs_exist_ok=True)
        gold_path = tampered / "cases" / "pi-001-p01-single-violation.json"
        gold = json.loads(gold_path.read_text(encoding="utf-8"))
        gold["defects"][0]["handle"] = "ZZZ"
        gold_path.write_text(json.dumps(gold, ensure_ascii=False, indent=2), encoding="utf-8")
        payload, _ = run_suite(tampered, report_path=tmp_path / "tampered.json")
        assert payload["failed"] >= 1
        failing = next(c for c in payload["cases"] if not c["passed"])
        assert "ZZZ" in " | ".join(failing["failure_reasons"])
