"""P7-S12 ``web_search_p0``：评测本身的门禁 + 每个硬指标一条反证（harness 文档 §2.7）。

反证的做法：``monkeypatch`` 把被测保护换成"放行"版本，只跑相关的几条用例，断言对应指标离开目标值；同一批用例在不拿掉
保护时全部达标。**不修改源码、不改数据集**。没有反证的指标不算数（``test_every_hard_metric_has_a_counterexample``）。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from water_agent.harness import web_search_eval as ev

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = REPO_ROOT / "data" / "eval" / "web_search_p0.jsonl"
BASELINE = REPO_ROOT / "data" / "eval" / "web_search_p0_main.json"


def _run(ids: set[str]) -> dict[str, Any]:
    report, problems = ev.run(DATASET, only=lambda case: case.id in ids)
    assert report is not None, problems
    return report


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------


def test_web_search_p0_hard_metrics() -> None:
    """P7-S12-A13 ``web_search_p0`` 全部用例通过、硬指标全部达标；数据集全文件 ASCII（不可见字符一律转义）。"""

    assert DATASET.read_text(encoding="utf-8").isascii()
    report, problems = ev.run(DATASET)
    assert report is not None, problems
    failed = [case["id"] for case in report["cases"] if not case["passed"]]
    assert failed == [], failed
    assert report["hard_violations"] == [], report["hard_violations"]
    assert report["mechanism"] == "present"
    assert report["total"] >= 52


def test_report_is_reproducible_and_matches_the_baseline() -> None:
    """P7-S12-A13 两次运行报告逐字相同（不含时间戳 / 临时路径）；与入仓基线逐字相同。"""

    first, _ = ev.run(DATASET)
    second, _ = ev.run(DATASET)
    assert first is not None and second is not None
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(second, ensure_ascii=False, sort_keys=True)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(baseline, ensure_ascii=False, sort_keys=True)


def test_dataset_shape_is_frozen() -> None:
    """P7-S12-A13 数据集形状：用例 id 唯一、前缀 ``ws-``、覆盖 harness 文档 §4 的全部类别；守卫集合应拦 ≥ 9 类、良性 ≥ 5 条。"""

    cases, problems = ev.load_cases(DATASET)
    assert problems == []
    assert all(case.id.startswith("ws-") for case in cases)
    categories = {case.category for case in cases}
    assert {"switch", "exposure", "destination", "query_guard", "results", "discipline", "failure", "audit"} <= categories
    leaky = [case for case in cases if case.category == "query_guard" and any(t.labels.get("leaky") for t in case.turns)]
    assert len(leaky) >= 9
    assert len(ev._GUARD_BENIGN) >= 5 and len(ev._GUARD_LEAKY) >= 9


# ---------------------------------------------------------------------------
# 反证：每个硬指标拿掉对应保护即变红
# ---------------------------------------------------------------------------


def _patch_everywhere(monkeypatch: pytest.MonkeyPatch, original: Any, replacement: Any) -> int:
    """把 ``water_agent`` 各模块里**指向同一对象**的名字都换掉（``from x import f`` 会在导入方另存一份绑定，
    只改定义处不够——反证没咬到的常见原因）。返回替换处数。"""

    import sys

    count = 0
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "water_agent" or name.startswith("water_agent.")):
            continue
        for attr, value in list(vars(module).items()):
            if value is original:
                monkeypatch.setattr(module, attr, replacement)
                count += 1
    assert count, "反证补丁没有替换到任何绑定"
    return count


def _allow_every_destination(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿掉目的地分类：任何解析结果都当作公网地址。"""

    from water_agent.mcp import http_transport

    _patch_everywhere(monkeypatch, http_transport.classify_address, lambda address: http_transport.PUBLIC)


def _drop_url_traceability(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿掉终答网址追溯：任何网址都算可追溯。"""

    from water_agent.external import taint

    _patch_everywhere(monkeypatch, taint.unverified_urls, lambda *args, **kwargs: [])


def _cite_accepts_web_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """让 ``web_refs`` 并入"本 thread 检索到的 source_id"：联网网址就能当引用（校验器与会话的可追溯集合都受影响）。"""

    from water_agent.graphs import agent_rag

    original = agent_rag._extract_validation_signal

    def leaky(state: Any) -> Any:
        context_ids, cited, retrieved = original(state)
        _tainted, refs = agent_rag.web_signal(state.get("messages", []))
        return [*context_ids, *refs], cited, retrieved

    _patch_everywhere(monkeypatch, original, leaky)


def _never_taint(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿掉回合污点：联网工具的结果不再被识别为联网内容。"""

    from water_agent.graphs import agent_rag

    _patch_everywhere(monkeypatch, agent_rag.web_signal, lambda messages: (False, []))


def _guard_allows_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿掉查询出口守卫。"""

    from water_agent.web_search import guard

    monkeypatch.setattr(guard.QueryEgressGuard, "check", lambda self, query, searches_this_turn=0: guard.ALLOW)


def _guard_blocks_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """"全拒"的守卫：泄露查询全被拦下，良性查询也全被误拦——误拦率必须因此变红（否则全拒能拿满分）。"""

    from water_agent.web_search import guard

    monkeypatch.setattr(
        guard.QueryEgressGuard, "check",
        lambda self, query, searches_this_turn=0: guard.GuardVerdict(allowed=False, reason=guard.REASON_PII),
    )


def _skip_neutralization(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿掉信封里的核心边界归一化与注入丢弃。"""

    from water_agent.external import envelope
    from water_agent.web_search import results

    monkeypatch.setattr(envelope, "neutralize_text", lambda text, limit=600: text)
    monkeypatch.setattr(envelope, "_is_suspicious", lambda *fields: False)
    monkeypatch.setattr(results, "_suspicious", lambda hit, url: False)


def _expose_on_every_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿掉"逐条消息勾选"这道门：部署开了就对每条消息开放联网。"""

    from water_agent.plugins import builtin

    original = builtin.WebSearchPlugin.__init__

    def always(self: Any, runtime: Any = None, *, config: Any = None, requested: bool = False) -> None:
        original(self, runtime, config=config, requested=True)

    monkeypatch.setattr(builtin.WebSearchPlugin, "__init__", always)


def _switch_always_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿掉部署开关：``off`` 也当成 ``on``。"""

    from water_agent.web_search import config

    _patch_everywhere(monkeypatch, config.resolve_switch, lambda environ: "on")


def _leak_upstream_in_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """错误模板回显上游正文与网址。"""

    from water_agent.web_search import errors, tool

    def leaky(code: str) -> dict[str, Any]:
        return {"ok": False, "error": True, "code": code,
                "message": "upstream-body-SECRET-TOKEN https://upstream.internal/leak"}

    monkeypatch.setattr(errors, "error_result", leaky)
    monkeypatch.setattr(tool, "error_result", leaky)


def _key_in_query_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """把服务商密钥塞进查询串（访问日志与代理看得到）。"""

    from water_agent.web_search.adapters import base, brave

    original = brave.build_request

    def leaky(query: str, count: int, key: str) -> base.RequestSpec:
        spec = original(query, count, key)
        return base.RequestSpec(path=spec.path, params={**spec.params, "key": key}, headers=spec.headers)

    monkeypatch.setattr(brave, "ADAPTER", base.ProviderAdapter("brave", True, leaky, brave.parse_response))
    from water_agent.web_search import adapters

    monkeypatch.setitem(adapters._ADAPTERS, "brave", brave.ADAPTER)


def _web_into_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """把联网工具结果复制进系统提示（可承载指令的段）。"""

    from langchain_core.messages import SystemMessage

    from water_agent.prompting.assembler import PromptAssembler

    original = PromptAssembler.assemble_prompt

    def leaky(self: Any, state: Any = None) -> Any:
        message = original(self, state)
        extra = [
            str(m.content)
            for m in (state or {}).get("messages", []) or []
            if getattr(m, "type", "") == "tool" and getattr(m, "name", "") == "web_search"
        ]
        if not extra:
            return message
        texts: list[str] = []
        for item in extra:
            try:
                texts.append(str(json.loads(item).get("content", "")))
            except ValueError:
                texts.append(item)
        return SystemMessage(content=str(message.content) + "\n\n" + "\n".join(texts))

    monkeypatch.setattr(PromptAssembler, "assemble_prompt", leaky)


#: 指标 → (拿掉保护的补丁, 只跑的用例, 变红后应偏离目标的判定)
COUNTEREXAMPLES: dict[str, tuple[Callable[[pytest.MonkeyPatch], None], set[str]]] = {
    "exposure_correct_rate": (_expose_on_every_message, {"ws-toggle-off-hidden", "ws-toggle-not-sticky"}),
    "off_identical_rate": (_switch_always_on, {"ws-off-no-tool"}),
    "destination_zero_byte_rate": (_allow_every_destination, {"ws-dest-metadata", "ws-dest-rebinding-pinned"}),
    "hostile_neutralized_rate": (_skip_neutralization, {"ws-r-injection-dropped", "ws-r-forged-marker", "ws-r-md-image"}),
    "instruction_bearing_leak_count": (_web_into_system_prompt, {"ws-r-injection-dropped", "ws-d-genuine-url"}),
    "query_block_recall": (_guard_allows_everything, {"ws-q-pii-id", "ws-q-pii-phone", "ws-q-credential", "ws-q-private-upload-overlap"}),
    "query_false_block_rate": (_guard_blocks_everything, {"ws-q-benign-set", "ws-q-user-overlap-allowed"}),
    "web_url_traceability_rate": (_drop_url_traceability, {"ws-d-fabricated-url"}),
    "citation_integrity_violations": (_cite_accepts_web_urls, {"ws-d-web-not-in-source-set"}),
    "taint_effect_rate": (_never_taint, {"ws-d-disclosure-line", "ws-d-cache-refused"}),
    "key_leak_count": (_key_in_query_string, {"ws-a-key-no-leak"}),
    "error_leak_count": (_leak_upstream_in_errors, {"ws-f-401", "ws-f-5xx-retry-once"}),
}


@pytest.mark.parametrize("metric", sorted(COUNTEREXAMPLES))
def test_each_hard_metric_goes_red_when_its_protection_is_removed(metric: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S12-A13 反证：拿掉对应保护，该指标离开目标值；不拿掉时同一批用例在目标上。"""

    patch, ids = COUNTEREXAMPLES[metric]
    clean = _run(ids)
    assert clean["metrics"][metric] == ev.HARD_TARGETS[metric], (metric, clean["metrics"][metric])
    patch(monkeypatch)
    broken = _run(ids)
    assert broken["metrics"][metric] != ev.HARD_TARGETS[metric], (metric, broken["metrics"][metric])


def test_every_hard_metric_has_a_counterexample() -> None:
    """P7-S12-A13 完备性：除 ``harness_error_count``（harness 自身出错即红，另由下一条覆盖）外，每个硬指标都有反证。"""

    assert set(ev.HARD_TARGETS) - {"harness_error_count"} == set(COUNTEREXAMPLES)


def test_harness_error_is_counted_not_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S12-A13 用例执行出错算失败（``harness_error_count``），不算跳过。"""

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("harness 自身出错")

    monkeypatch.setattr(ev, "run_unit_case", boom)
    report = _run({"ws-no-url-param"})
    assert report["metrics"]["harness_error_count"] == 1
    assert report["hard_violations"]
