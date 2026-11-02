"""P7-S14 ``mcp_registry_p0``：评测本身的门禁 + 每个硬指标一条反证（harness 文档 §2.7）。

反证的做法：``monkeypatch`` 把被测保护换成"放行"版本，只跑相关的几条用例，断言对应指标离开目标值；同一批用例在不拿掉
保护时全部达标。**不修改源码、不改数据集**。没有反证的指标不算数（``test_every_hard_metric_has_a_counterexample``）。

**每条反证要同时拿掉冗余的两层保护**：S13 的手工变异发现——只拿掉一层、另一层纵深仍把它挡住时，指标不会变红，
反证等于没写。下面每个补丁的文档说明了拿掉了哪几层。
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi import Cookie

from water_agent.harness import mcp_registry_eval as ev

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = REPO_ROOT / "data" / "eval" / "mcp_registry_p0.jsonl"
BASELINE = REPO_ROOT / "data" / "eval" / "mcp_registry_p0_main.json"


def _run(ids: set[str]) -> dict[str, Any]:
    report, problems = ev.run(DATASET, only=lambda case: case.id in ids)
    assert report is not None, problems
    return report


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------


def test_mcp_registry_p0_hard_metrics() -> None:
    """P7-S14-A09 ``mcp_registry_p0`` 全部用例通过、硬指标全部达标；数据集全文件 ASCII（不可见字符一律不写进 JSONL）。"""

    assert DATASET.read_text(encoding="utf-8").isascii()
    report, problems = ev.run(DATASET)
    assert report is not None, problems
    failed = [case["id"] for case in report["cases"] if not case["passed"]]
    assert failed == [], failed
    assert report["hard_violations"] == [], report["hard_violations"]
    assert report["mechanism"] == "present"
    assert report["total"] >= 36


def test_report_is_reproducible_and_matches_the_baseline() -> None:
    """P7-S14-A09 两次运行报告逐字相同（不含时间戳 / 临时路径）；与入仓基线逐字相同。"""

    first, _ = ev.run(DATASET)
    second, _ = ev.run(DATASET)
    assert first is not None and second is not None
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(second, ensure_ascii=False, sort_keys=True)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(baseline, ensure_ascii=False, sort_keys=True)


def test_dataset_shape_is_frozen() -> None:
    """P7-S14-A09 数据集形状：用例 id 唯一、前缀 ``mr-``、覆盖 harness 文档 §6 的全部类别；敌意条目由假目录运行期生成，不写进 JSONL。"""

    cases, problems = ev.load_cases(DATASET)
    assert problems == []
    assert all(case.id.startswith("mr-") for case in cases)
    assert {case.category for case in cases} >= {"protocol", "entry", "proposal", "no_connect", "api", "governance"}
    text = DATASET.read_text(encoding="utf-8")
    for hostile in ("Ignore all previous", "<script", "WEB_CONTENT id", "SECRET"):
        assert hostile not in text, f"敌意内容不应写进数据集：{hostile}"
    proposed = [c for c in cases if c.op == "propose" and c.expect.get("proposed") is True]
    refused = [c for c in cases if c.op == "propose" and c.expect.get("proposed") is False]
    assert len(proposed) >= 8 and len(refused) >= 12, "良性邻近用例与拒绝用例都要有（只有拒绝的评测会奖励'全拒'）"


# ---------------------------------------------------------------------------
# 反证补丁：每个都同时拿掉冗余的两层
# ---------------------------------------------------------------------------


def _connect_entry_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """提案阶段去探测条目地址：拿掉"不连接"的结构保证——``build_proposal`` 在生成提案前对**每个条目主机**做一次 DNS 解析，
    并向探针端口（评测里的"条目地址"）发一次 TCP 连接、写几个字节。（SDD17-I13 的 AST 约束只管导入，不管运行期行为；这里直接
    模拟"有人给提案加了探测"。条目 URL 指向不存在的域名，所以探测要自己去找探针：从环境里取当前评测的 sink 端口。）"""

    import contextlib
    import socket
    from urllib.parse import urlsplit

    from water_agent.mcp import registry

    original = registry.build_proposal

    def probing(entry: Any, **kwargs: Any) -> Any:
        sink_port = getattr(ev, "_ACTIVE_SINK_PORT", 0)
        for remote in getattr(entry, "remotes", ()):
            host = urlsplit(remote.url).hostname or ""
            with contextlib.suppress(OSError, ValueError):
                socket.getaddrinfo(host, 443)
            if sink_port:
                with contextlib.suppress(OSError):
                    sock = socket.create_connection(("127.0.0.1", sink_port), timeout=0.2)
                    sock.sendall(b"GET / HTTP/1.0\r\n\r\n")
                    sock.close()
        return original(entry, **kwargs)

    monkeypatch.setattr(registry, "build_proposal", probing)


def _allow_every_remote_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """放行 stdio / 包类 / sse / IP 字面量 / localhost 条目：**两层**都拿掉——资格判定恒为"可提案"，并且 ``build_proposal``
    对条目没有 streamable-http 域名地址时的兜底（取第一个 remote）也不拒绝，而是用条目里第一个 URL（没有就用占位）。"""

    from water_agent.mcp import registry

    monkeypatch.setattr(registry, "_proposal_eligibility", lambda remotes: None)
    original = registry.build_proposal

    def lenient(entry: Any, **kwargs: Any) -> Any:
        patched = registry.RegistryEntry(
            name=entry.name, version=entry.version, status=registry.STATUS_OK, description=entry.description,
            remotes=entry.remotes or (registry.RemoteView(type="streamable-http", url="https://placeholder.example.com/mcp"),),
            packages=entry.packages, dropped_fields=entry.dropped_fields, not_proposable_reason=None,
        )
        # 把条目里的每个 remote 都当成 streamable-http（stdio / sse / IP 字面量都走提案）
        patched = registry.RegistryEntry(
            name=patched.name, version=patched.version, status=patched.status, description=patched.description,
            remotes=tuple(registry.RemoteView(type=registry.PROPOSABLE_REMOTE_TYPE, url=r.url) for r in patched.remotes),
            packages=patched.packages, dropped_fields=patched.dropped_fields, not_proposable_reason=None,
        )
        return original(patched, **kwargs)

    monkeypatch.setattr(registry, "build_proposal", lenient)


def _emit_enabled_proposals_with_allow(monkeypatch: pytest.MonkeyPatch) -> None:
    """提案不再停用、还带 ``allow``：**两层**都拿掉——``importer`` 输出的 server 被改成启用 + 放行任意工具，并且
    ``build_proposal`` 里的防御性复核被绕过（直接用被篡改后的 ``preview_import``，复核发现不了的写法：先过复核再改）。"""

    from water_agent.mcp import registry

    original = registry.build_proposal

    def tampering(entry: Any, **kwargs: Any) -> Any:
        proposal = original(entry, **kwargs)
        data = json.loads(proposal.text)
        for server in data["servers"]:
            server.pop("disabled", None)
            server["allow"] = ["anything"]
        return registry.Proposal(
            name=proposal.name, version=proposal.version, server_id=proposal.server_id,
            text=json.dumps(data, ensure_ascii=False, indent=2) + "\n", proposal_hash=proposal.proposal_hash,
            notes=proposal.notes, secret_dropped=proposal.secret_dropped, registry_host=proposal.registry_host,
        )

    monkeypatch.setattr(registry, "build_proposal", tampering)


def _keep_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """不丢弃敏感载荷：**两层**都拿掉——解析阶段把非白名单字段（含 headers / env）原样塞回条目的展示视图，并且提案文本里
    附带条目的 URL 查询串原文。"""

    from water_agent.mcp import registry

    original_parse = registry._parse_entry

    def keeping(raw: Any) -> Any:
        entry = original_parse(raw)
        leak = json.dumps({key: value for key, value in raw.items() if key not in registry._ENTRY_KEYS}, ensure_ascii=False)
        return registry.RegistryEntry(
            name=entry.name, version=entry.version, status=entry.status, description=entry.description + leak,
            remotes=entry.remotes, packages=entry.packages, dropped_fields=entry.dropped_fields,
            unsafe_reason=entry.unsafe_reason, not_proposable_reason=entry.not_proposable_reason,
        )

    monkeypatch.setattr(registry, "_parse_entry", keeping)
    original_proposal = registry.build_proposal

    def with_query(entry: Any, **kwargs: Any) -> Any:
        proposal = original_proposal(entry, **kwargs)
        raw_urls = " ".join(r.url for r in entry.remotes)
        return registry.Proposal(
            name=proposal.name, version=proposal.version, server_id=proposal.server_id, text=proposal.text + raw_urls,
            proposal_hash=proposal.proposal_hash, notes=proposal.notes, secret_dropped=proposal.secret_dropped,
            registry_host=proposal.registry_host, idna_host=proposal.idna_host,
        )

    monkeypatch.setattr(registry, "build_proposal", with_query)


def _propose_unsafe_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """unsafe 条目也能提案：**两层**都拿掉——条目解析不再标 unsafe（字符白名单 / 注入 / 归一化三重检查都放行），并且
    ``build_proposal`` 不再检查 ``status``。"""

    from water_agent.mcp import registry

    monkeypatch.setattr(registry, "_text_is_clean", lambda text: (True, ""))
    monkeypatch.setattr(registry, "disallowed_codepoints", lambda text, limit=8: [])
    original = registry.build_proposal

    def ignoring_status(entry: Any, **kwargs: Any) -> Any:
        patched = registry.RegistryEntry(
            name=entry.name, version=entry.version, status=registry.STATUS_OK, description=entry.description,
            remotes=entry.remotes or (registry.RemoteView(type=registry.PROPOSABLE_REMOTE_TYPE, url="https://placeholder.example.com/mcp"),),
            packages=entry.packages, dropped_fields=entry.dropped_fields, not_proposable_reason=None,
        )
        return original(patched, **kwargs)

    monkeypatch.setattr(registry, "build_proposal", ignoring_status)


def _hash_ignores_the_definition(monkeypatch: pytest.MonkeyPatch) -> None:
    """哈希不含 URL（也不含目录主机）：只含名称与版本。"""

    from water_agent.mcp import registry

    monkeypatch.setattr(
        registry, "proposal_hash_of",
        lambda definition, *, registry_host, name, version: registry.hashlib.sha256(f"{name}@{version}".encode()).hexdigest(),
    )


def _partially_accept_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """整页严格校验改成"跳过坏条目、采纳其余"，并且去掉总量 / 条目大小上限与页数上限：**多层**一起拿掉。"""

    from water_agent.mcp import registry

    original_parse = registry._parse_entry

    def lenient_page(body: bytes) -> Any:
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            raise registry.McpRegistryError(registry.MCP_REGISTRY_BAD_RESPONSE) from None
        entries = []
        raw_entries = payload.get("entries") if isinstance(payload, dict) else None
        for item in raw_entries if isinstance(raw_entries, list) else []:
            try:
                entries.append(original_parse(item))
            except registry._Malformed:
                continue
        # 形状整体不对（不是列表）也当作"一页空条目"，而不是失败
        return entries, None

    monkeypatch.setattr(registry, "parse_page", lenient_page)
    monkeypatch.setattr(registry, "MAX_TOTAL_BYTES", 10**9)
    monkeypatch.setattr(registry, "MAX_PAGES", 10**6)
    monkeypatch.setattr(registry, "MAX_ENTRY_BYTES", 10**9)


def _benign_text_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """字符白名单"全拒"：任何非 ASCII 字符都不通过——中文工程文本、希腊工程符号全被误拒（否则全拒能拿满分）。"""

    from water_agent.mcp import registry

    monkeypatch.setattr(registry, "disallowed_codepoints", lambda text, limit=8: [ord(c) for c in text if ord(c) > 127][:limit])


#: 指标 → (拿掉保护的补丁, 只跑的用例)
COUNTEREXAMPLES: dict[str, tuple[Callable[[pytest.MonkeyPatch], None], set[str]]] = {
    "remote_only_proposal_rate": (
        _allow_every_remote_type,
        {"mr-prop-stdio-not-proposed", "mr-prop-npm-not-proposed", "mr-prop-ip-v4-refused", "mr-prop-localhost-refused", "mr-prop-http-refused"},
    ),
    "proposal_disabled_no_allow_rate": (_emit_enabled_proposals_with_allow, {"mr-prop-remote-ok", "mr-prop-disabled-no-allow"}),
    "bytes_to_entry_urls": (_connect_entry_addresses, {"mr-nc-zero-bytes-to-entry", "mr-prop-remote-ok"}),
    "dns_lookups_for_entry_hosts": (_connect_entry_addresses, {"mr-nc-no-dns-for-entry-host"}),
    "secret_drop_rate": (_keep_secrets, {"mr-prop-headers-dropped", "mr-prop-unknown-fields-dropped", "mr-entry-unknown-fields"}),
    "unsafe_entry_proposed_count": (_propose_unsafe_entries, {"mr-prop-unsafe-refused"}),
    "proposal_hash_determinism_rate": (_hash_ignores_the_definition, {"mr-prop-hash-deterministic-and-sensitive"}),
    "registry_failure_fail_closed_rate": (_partially_accept_pages, {"mr-proto-junk", "mr-proto-bad-shape", "mr-proto-bad-entry-shape", "mr-proto-oversize-entry"}),
    "benign_entry_proposed_rate": (lambda mp: mp.setattr(__import__("water_agent.mcp.registry", fromlist=["x"]), "_proposal_eligibility", lambda remotes: "url_rejected"), {"mr-prop-remote-ok", "mr-prop-remote-ok-2"}),
    "benign_text_false_reject_count": (_benign_text_is_rejected, {"mr-entry-benign-neighbors"}),
}


@pytest.mark.parametrize("metric", sorted(COUNTEREXAMPLES))
def test_each_hard_metric_goes_red_when_its_protection_is_removed(metric: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S14-A09 反证：拿掉对应保护，该指标离开目标值；不拿掉时同一批用例在目标上。"""

    patch, ids = COUNTEREXAMPLES[metric]
    clean = _run(ids)
    assert clean["metrics"][metric] == ev.HARD_TARGETS[metric], (metric, clean["metrics"][metric])
    patch(monkeypatch)
    broken = _run(ids)
    assert broken["metrics"][metric] != ev.HARD_TARGETS[metric], (metric, broken["metrics"][metric])


def test_non_admin_access_goes_red_when_the_admin_check_is_removed() -> None:
    """P7-S14-A09 反证（``non_admin_access_count``）：把路由依赖换成"任何已登录用户"（FastAPI 的 ``dependency_overrides``），
    成员访问三个接口即放行，指标离开 0。不拿掉时同一用例达标。"""

    from water_agent.webapp.auth.dependencies import (
        _build_principal,
        _resolve_session,
        require_admin,
    )

    clean = _run({"mr-api-admin-only"})
    assert clean["metrics"]["non_admin_access_count"] == 0

    original_make = ev.ApiRig.__init__

    def permissive_rig(self: Any, case_dir: Path, modes: str = "ok") -> None:
        original_make(self, case_dir, modes)

        def any_logged_in_user(wa_session: str | None = Cookie(default=None)) -> tuple[Any, Any]:
            user, _session = _resolve_session(wa_session)
            return user, _build_principal(user)

        self.app.dependency_overrides[require_admin] = any_logged_in_user

    ev.ApiRig.__init__ = permissive_rig  # type: ignore[method-assign]
    try:
        broken = _run({"mr-api-admin-only"})
    finally:
        ev.ApiRig.__init__ = original_make  # type: ignore[method-assign]
    assert broken["metrics"]["non_admin_access_count"] != 0, broken["metrics"]["non_admin_access_count"]


def test_every_hard_metric_has_a_counterexample() -> None:
    """P7-S14-A09 完备性：除 ``harness_error_count``（harness 自身出错即红，另由下一条覆盖）与 ``non_admin_access_count``
    （上一条用 ``dependency_overrides`` 单独反证）外，每个硬指标都有反证。"""

    assert set(ev.HARD_TARGETS) - {"harness_error_count", "non_admin_access_count"} == set(COUNTEREXAMPLES)


def test_harness_error_is_counted_not_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S14-A09 用例执行出错算失败（``harness_error_count``），不算跳过。"""

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("harness 自身出错")

    monkeypatch.setattr(ev, "run_invariant_case", boom)
    report = _run({"mr-gov-site-registered"})
    assert report["metrics"]["harness_error_count"] == 1
    assert report["hard_violations"]


def test_unmeasured_metrics_are_none_and_fail_the_gate() -> None:
    """P7-S14-A09 未测量不是通过：只跑一条不产生任何提案计数的用例，依赖提案的速率是 ``None``，硬指标判不达标。"""

    report = _run({"mr-gov-capability-surface"})
    assert report["metrics"]["proposal_disabled_no_allow_rate"] is None
    assert report["metrics"]["non_admin_access_count"] is None
    assert any("proposal_disabled_no_allow_rate" in item for item in report["hard_violations"])


def test_the_comparator_rejects_hand_edited_and_regressed_reports(tmp_path: Path) -> None:
    """P7-S14-A09 比较器：拒绝手改的指标、数据集变化、用例消失、PASS_TO_PASS 回退；通过时退出码 0。"""

    from scripts import compare_mcp_registry_p0 as cmp

    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert cmp.compare_reports(json.loads(json.dumps(base)), base)["passed"] is True

    hand_edited = json.loads(json.dumps(base))
    hand_edited["metrics"]["bytes_to_entry_urls"] = 5
    assert not cmp.compare_reports(hand_edited, base)["passed"]

    changed = json.loads(json.dumps(base))
    changed["dataset_sha256"] = "0" * 64
    assert any("数据集" in item for item in cmp.compare_reports(changed, base)["violations"])

    vanished = json.loads(json.dumps(base))
    vanished["cases"] = vanished["cases"][1:]
    vanished["total"] -= 1
    vanished["passed"] -= 1
    assert any("消失" in item for item in cmp.compare_reports(vanished, base)["violations"])

    regressed = json.loads(json.dumps(base))
    regressed["cases"][0]["passed"] = False
    regressed["passed"] -= 1
    assert not cmp.compare_reports(regressed, base)["passed"]
