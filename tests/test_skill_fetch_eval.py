"""P7-S13 ``skill_fetch_p0``：评测本身的门禁 + 每个硬指标一条反证（harness 文档 §2.7）+ 假注册表自测。

反证的做法：``monkeypatch`` 把被测保护换成"放行"版本，只跑相关的几条用例，断言对应指标离开目标值；同一批用例在不拿掉保护时全部达标。
**不修改源码、不改数据集**。没有反证的指标不算数（``test_every_hard_metric_has_a_counterexample``）。
"""
from __future__ import annotations

import json
import shutil
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from water_agent.harness import skill_fetch_eval as ev

REPO_ROOT = Path(__file__).resolve().parents[1]
DATASET = REPO_ROOT / "data" / "eval" / "skill_fetch_p0.jsonl"
BASELINE = REPO_ROOT / "data" / "eval" / "skill_fetch_p0_main.json"


def _run(ids: set[str]) -> dict[str, Any]:
    report, problems = ev.run(DATASET, only=lambda case: case.id in ids)
    assert report is not None, problems
    return report


def _patch_everywhere(monkeypatch: pytest.MonkeyPatch, original: Any, replacement: Any) -> int:
    """把 ``water_agent`` 各模块里**指向同一对象**的名字都换掉（``from x import f`` 会在导入方另存一份绑定，只改定义处不够）。"""

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


# ---------------------------------------------------------------------------
# 门禁
# ---------------------------------------------------------------------------


def test_skill_fetch_p0_hard_metrics() -> None:
    """P7-S13-A11 ``skill_fetch_p0`` 全部用例通过、硬指标全部达标；数据集全文件 ASCII（不可见字符一律运行期拼接）。"""

    assert DATASET.read_text(encoding="utf-8").isascii()
    report, problems = ev.run(DATASET)
    assert report is not None, problems
    failed = [case["id"] for case in report["cases"] if not case["passed"] and not case["skipped"]]
    assert failed == [], failed
    assert report["hard_violations"] == [], report["hard_violations"]
    assert report["mechanism"] == "present" and report["total"] >= 44
    assert ev.undeclared_skips(report["cases"]) == []


def test_report_is_reproducible_and_matches_the_baseline() -> None:
    """P7-S13-A11 两次运行报告逐字相同（不含时间戳 / 临时路径 / 端口）；与入仓基线逐字相同。"""

    first, _ = ev.run(DATASET)
    second, _ = ev.run(DATASET)
    assert first is not None and second is not None
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(second, ensure_ascii=False, sort_keys=True)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(baseline, ensure_ascii=False, sort_keys=True)


def test_dataset_shape_is_frozen() -> None:
    """P7-S13-A11 数据集形状：id 唯一且前缀 ``sf-``；harness 文档 §5 的七个类别都在；每个敌意包生成器至少被一条用例引用；
    良性对照 ≥ 8 条（否则"全拒"也能拿满分）；两条崩溃用例族各覆盖三个崩溃点；全部用例的 ``setup`` 里没有字面不可见字符。"""

    cases, problems = ev.load_cases(DATASET)
    assert problems == []
    assert all(case.id.startswith("sf-") for case in cases)
    assert {case.category for case in cases} == {"registry", "package", "quarantine", "approve", "loading", "surface", "lifecycle"}
    module = ev.fake_module()
    referenced = {item.package for case in cases for item in case.setup.registry.publish}
    assert set(module.HOSTILE_PACKAGES) <= referenced, sorted(set(module.HOSTILE_PACKAGES) - referenced)
    assert sum(1 for case in cases if case.labels.get("benign")) >= 8
    crashes = {step.crash for case in cases for step in case.steps if step.crash}
    assert crashes == set(ev.CRASH_POINTS)
    first_install = [case for case in cases if case.id.startswith("sf-ap-crash-first-install-")]
    upgrade = [case for case in cases if case.id.startswith("sf-ap-crash-upgrade-")]
    assert len(first_install) == len(upgrade) == 3


# ---------------------------------------------------------------------------
# 反证：每个硬指标拿掉对应保护即变红
# ---------------------------------------------------------------------------


def _skip_installed_digest_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿掉加载期的字节摘要 / 大小比对（lock 不再是主防线）。"""

    from water_agent.skills import installed

    original = installed.sha256_of

    class _AlwaysEqual(str):
        def __ne__(self, other: object) -> bool:
            return False

    monkeypatch.setattr(installed, "sha256_of", lambda raw: _AlwaysEqual(original(raw)))
    # 大小也不比：把 lock 里的 size 当成任何文件都相符
    monkeypatch.setattr(installed.InstalledMeta, "size", property(lambda self: 0), raising=False)


def _approve_allows_non_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    from water_agent.skills.remote import approve

    monkeypatch.setattr(approve, "require_human", lambda io: None)


def _approve_ignores_the_typed_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from water_agent.skills.remote import approve

    monkeypatch.setattr(approve, "confirm_by_typing", lambda io, expected_name, *, prompt: None)


def _approve_skips_the_digest_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿掉三方摘要比对：把 ``approve`` 里读到的候选文件摘要与 ``candidate.json`` 都当成等于参数。"""

    from water_agent.skills.remote import approve

    original = approve.sha256_of

    class _Wildcard(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

        __hash__ = str.__hash__

    monkeypatch.setattr(approve, "sha256_of", lambda raw: _Wildcard(original(raw)))
    monkeypatch.setattr(approve.quarantine_store, "find_candidate", _wild_candidate(approve.quarantine_store.find_candidate))


def _wild_candidate(original: Callable[..., Any]) -> Callable[..., Any]:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        candidate = original(*args, **kwargs)
        if candidate is None:
            return None

        class _Proxy:
            def __getattr__(self, name: str) -> Any:
                return _wild(getattr(candidate, name)) if name == "sha256" else getattr(candidate, name)

        return _Proxy()

    return wrapped


def _wild(value: str) -> str:
    class _Wildcard(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

        __hash__ = str.__hash__

    return _Wildcard(value)


def _parse_skips_the_charset(monkeypatch: pytest.MonkeyPatch) -> None:
    from water_agent.skills.remote import parse

    monkeypatch.setattr(parse, "disallowed_characters", lambda text, limit=8: [])


def _parse_skips_the_yaml_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    from water_agent.skills.remote import parse

    monkeypatch.setattr(parse, "yaml_hazards", lambda header: [])


def _safety_skips_links_and_folding(monkeypatch: pytest.MonkeyPatch) -> None:
    from water_agent.skills.remote import safety

    monkeypatch.setattr(safety, "link_hits", lambda text: [])
    monkeypatch.setattr(safety, "fold_greek_lookalikes", lambda text: text)


def _quarantine_inside_the_load_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿掉"隔离区不在任何加载根下"的结构保证：启动校验放行重叠的根目录，**且**候选目录名退化成合法的 skill 目录名
    （只拿掉启动校验不够——候选目录名带 ``@版本-sha8``，本来就不是合法 skill 目录名，库照样读不到；这是纵深防御，要两层都拿掉才暴露）。"""

    from water_agent.skills.remote import quarantine

    monkeypatch.setattr(quarantine, "roots_overlap", lambda paths: None)
    monkeypatch.setattr(quarantine, "candidate_dirname", lambda name, version, sha256: name)


def _failed_fetch_leaves_a_partial_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿掉"失败关闭、磁盘零写入"：``fetch`` 失败时在隔离区里留下一个半成品目录（模拟"先写后查"或失败路径不清理）。"""

    from water_agent.skills import cli

    original = cli._do_fetch

    def messy(deps: Any, paths: Any, ref: str) -> Any:
        try:
            return original(deps, paths, ref)
        except cli._Failure:
            leftover = paths.quarantine / ".partial-leftover"
            leftover.mkdir(parents=True, exist_ok=True)
            (leftover / "SKILL.md").write_bytes(b"half written")
            raise

    monkeypatch.setattr(cli, "_do_fetch", messy)


def _fetch_forgets_the_version_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """拿掉降级 / 相等 / 重放的版本守卫：``version_tuple`` 对一切版本给出同一个值，``fetch`` 与 ``approve`` 的版本比较都失效；
    重放比对也失效（已知版本的 sha 视为一致）。"""

    from water_agent.skills import cli
    from water_agent.skills.remote import approve

    monkeypatch.setattr(cli, "version_tuple", lambda version: (1, 0, 0) if False else (0, 0, int(version.split(".")[0])))
    monkeypatch.setattr(approve, "version_tuple", lambda version: (0, 0, int(version.split(".")[0]) + 1000))
    monkeypatch.setattr(cli, "_local_versions", lambda paths, name: {})


def _leak_the_registry_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """审计里写进完整注册表 URL（含路径哨兵）。"""

    from water_agent.skills import cli

    original = cli._audit

    def leaky(deps: Any, paths: Any, *, action: str, ref: str, sha256: str, code: str, registry_host: str | None = None) -> None:
        original(deps, paths, action=action, ref=f"{ref} {deps.env.get('WATER_AGENT_SKILL_REGISTRY_URL', '')}", sha256=sha256, code=code,
                 registry_host=registry_host)

    monkeypatch.setattr(cli, "_audit", leaky)


def _model_gets_an_install_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """让治理清单里多一个名叫 ``approve_skill`` 的模型工具（登记表被篡改）。"""

    from water_agent.runtime.governance import inventory

    entry = inventory.ToolCapabilityEntry(
        tool_name="approve_skill", module="water_agent/tools/rogue.py",
        required_capability=inventory.Capability.LOAD_SKILL, effect="rogue", guardrail_validated=True,
    )
    monkeypatch.setattr(inventory, "TOOL_CAPABILITIES", (*inventory.TOOL_CAPABILITIES, entry))


def _only_the_cli_guards_its_roots(monkeypatch: pytest.MonkeyPatch) -> None:
    from water_agent.skills.remote import quarantine

    monkeypatch.setattr(quarantine, "roots_overlap", lambda paths: None)


#: 指标 → (拿掉保护的补丁, 只跑的用例)
COUNTEREXAMPLES: dict[str, tuple[Callable[[pytest.MonkeyPatch], None], set[str]]] = {
    "hostile_package_rejected_rate": (_parse_skips_the_charset, {"sf-pkg-tag-smuggle", "sf-pkg-variation-selector", "sf-pkg-cyrillic-homoglyph"}),
    "fail_closed_zero_write_rate": (_failed_fetch_leaves_a_partial_candidate, {"sf-reg-sha-mismatch", "sf-reg-index-missing", "sf-pkg-nul"}),
    "approve_requires_human_rate": (_approve_allows_non_tty, {"sf-ap-non-tty", "sf-lc-rollback-non-tty", "sf-lc-remove-non-tty"}),
    "hash_pin_enforced_rate": (_skip_installed_digest_check, {"sf-ld-tamper-bytes", "sf-ld-tamper-size", "sf-ld-tamper-crlf"}),
    "quarantine_visible_to_library_count": (_quarantine_inside_the_load_roots, {"sf-q-misconfigured-as-operator-dir-still-not-loadable", "sf-q-not-visible-to-library"}),
    "model_or_panel_install_surface_count": (_model_gets_an_install_tool, {"sf-no-model-tool"}),
    "partial_install_count": (_skip_installed_digest_check, {"sf-ap-crash-upgrade-after-replace", "sf-ap-crash-upgrade-before-lock-replace"}),
    "downgrade_replay_accepted_count": (_fetch_forgets_the_version_floor, {"sf-lc-downgrade-refused", "sf-lc-replay-refused", "sf-lc-equal-version-refused"}),
    "secret_leak_count": (_leak_the_registry_url, {"sf-lc-secret-free-trail", "sf-ap-happy-path"}),
    "benign_package_rejected_rate": (_parse_skips_the_charset.__class__ and _safety_skips_links_and_folding, set()),
}


def _benign_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """让安检"全拒"：良性包也被拒——误拒率必须因此变红（否则全拒能拿满分）。"""

    from water_agent.skills.remote import safety

    def refuse(skill: Any, *, available_tools: Any) -> Any:
        raise safety.RemoteSafetyError("SKILL_UNSAFE", "全拒")

    monkeypatch.setattr(safety, "check_remote_skill", refuse)
    from water_agent.skills import cli

    monkeypatch.setattr(cli, "check_remote_skill", refuse)


COUNTEREXAMPLES["benign_package_rejected_rate"] = (_benign_rejected, {"sf-reg-benign-baseline", "sf-pkg-benign-cjk-engineering", "sf-ap-happy-path"})


@pytest.mark.parametrize("metric", sorted(COUNTEREXAMPLES))
def test_each_hard_metric_goes_red_when_its_protection_is_removed(metric: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S13-A11 反证：拿掉对应保护，该指标离开目标值；不拿掉时同一批用例在目标上。"""

    patch, ids = COUNTEREXAMPLES[metric]
    clean = _run(ids)
    assert clean["metrics"][metric] == ev.HARD_TARGETS[metric], (metric, clean["metrics"][metric])
    assert clean["metrics"][metric] is not None
    patch(monkeypatch)
    broken = _run(ids)
    assert broken["metrics"][metric] != ev.HARD_TARGETS[metric], (metric, broken["metrics"][metric])


def test_every_hard_metric_has_a_counterexample() -> None:
    """P7-S13-A11 完备性：除 ``harness_error_count``（harness 自身出错即红，另由下一条覆盖）外，每个硬指标都有反证。"""

    assert set(ev.HARD_TARGETS) - {"harness_error_count"} == set(COUNTEREXAMPLES)


def test_harness_error_is_counted_not_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S13-A11 用例执行出错算失败（``harness_error_count``），不算跳过。"""

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("harness 自身出错")

    monkeypatch.setattr(ev, "_run_cli_step", boom)
    report = _run({"sf-reg-benign-baseline"})
    assert report["metrics"]["harness_error_count"] == 1
    assert report["hard_violations"]


def test_an_unmeasured_metric_is_none_and_fails_the_gate() -> None:
    """P7-S13-A11 未测量不是通过：只跑一条不含任何人工确认门步骤的用例时，``approve_requires_human_rate`` 是 ``None`` 并判不达标。"""

    report = _run({"sf-no-model-tool"})
    assert report["metrics"]["approve_requires_human_rate"] is None
    assert any("approve_requires_human_rate" in item for item in report["hard_violations"])


def test_a_missing_mechanism_is_reported_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """P7-S13-A11 机制不存在时报告标 ``absent``，用例如实失败（不吞成通过），比较器据此拒绝把它当门禁对象。"""

    monkeypatch.setattr(ev, "mechanism_present", lambda: False)
    report = _run({"sf-reg-benign-baseline"})
    assert report["mechanism"] == "absent" and report["passed"] == 0 and report["ok"] is False


def test_comparator_rejects_hand_edited_reports_and_regressions(tmp_path: Path) -> None:
    """P7-S13-A11 比较器：拒绝手改的报告（指标与用例明细不一致）、数据集变化、基线用例消失、PASS_TO_PASS 违规；未声明前置条件的 SKIP 是违规。"""

    import importlib.util

    spec = importlib.util.spec_from_file_location("compare_skill_fetch_p0", REPO_ROOT / "scripts" / "compare_skill_fetch_p0.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    base = json.loads(BASELINE.read_text(encoding="utf-8"))
    assert module.compare_reports(json.loads(json.dumps(base)), base)["passed"] is True

    edited = json.loads(json.dumps(base))
    edited["metrics"]["secret_leak_count"] = 5
    assert any("手改" in item for item in module.compare_reports(edited, base)["violations"])

    changed = json.loads(json.dumps(base))
    changed["dataset_sha256"] = "0" * 64
    assert any("数据集" in item for item in module.compare_reports(changed, base)["violations"])

    missing = json.loads(json.dumps(base))
    missing["cases"] = missing["cases"][1:]
    missing["total"] = len(missing["cases"])
    missing["passed"] = sum(1 for case in missing["cases"] if case["passed"])
    assert any("消失" in item for item in module.compare_reports(missing, base)["violations"])

    skipped = json.loads(json.dumps(base))
    skipped["cases"][0].update({"skipped": True, "requires": [], "passed": False})
    skipped["passed"] = sum(1 for case in skipped["cases"] if case["passed"])
    assert any("undeclared_skip" in item for item in module.compare_reports(skipped, base)["violations"])


# ---------------------------------------------------------------------------
# 假注册表自测（不依赖机制）
# ---------------------------------------------------------------------------


def test_fake_registry_serves_the_protocol_and_records_requests() -> None:
    """P7-S13-A01 假注册表：``ok`` 模式下 index 与文件讲协议 v1、sha256 / size 与实际字节一致；每个请求都被记录；路径前缀生效。"""

    import urllib.request

    module = ev.fake_module()
    raw = module.HOSTILE_PACKAGES["benign"]("pump-room-notes", "1.0.0")
    with module.FakeSkillRegistry("ok", catalog={"pump-room-notes": {"1.0.0": raw}}, path_prefix="/p") as registry:
        index = json.loads(urllib.request.urlopen(registry.base_url + "/index.json").read())
        assert index == {"version": 1, "skills": [{"name": "pump-room-notes", "versions": [
            {"version": "1.0.0", "sha256": module.sha256_hex(raw), "size": len(raw)}]}]}
        assert urllib.request.urlopen(registry.base_url + "/skills/pump-room-notes/1.0.0/SKILL.md").read() == raw
        assert [item["path"] for item in registry.requests] == ["/p/index.json", "/p/skills/pump-room-notes/1.0.0/SKILL.md"]
        assert registry.skill_file_requests() == 1


def test_fake_registry_modes_change_only_the_response_shape() -> None:
    """P7-S13-A01 协议模式只控制响应形态：``sha_mismatch`` 让声明与字节不符；``echo_mismatch`` 让声明与返回字节**一致**但返回的是另一个
    skill（所以身份不符只能靠 frontmatter 核对）；``traversal_name_in_index`` 往 index 里塞 ``../etc/passwd``；``status_503`` 的响应体夹带上游串。"""

    import urllib.error
    import urllib.request

    module = ev.fake_module()
    raw = module.HOSTILE_PACKAGES["benign"]("pump-room-notes", "1.0.0")
    catalog = {"pump-room-notes": {"1.0.0": raw}}

    with module.FakeSkillRegistry("sha_mismatch", catalog=catalog) as registry:
        entry = json.loads(urllib.request.urlopen(registry.base_url + "/index.json").read())["skills"][0]["versions"][0]
        assert entry["sha256"] != module.sha256_hex(raw)
    with module.FakeSkillRegistry("echo_mismatch", catalog=catalog) as registry:
        entry = json.loads(urllib.request.urlopen(registry.base_url + "/index.json").read())["skills"][0]["versions"][0]
        served = urllib.request.urlopen(registry.base_url + "/skills/pump-room-notes/1.0.0/SKILL.md").read()
        assert entry["sha256"] == module.sha256_hex(served) and b"name: other-skill" in served
    with module.FakeSkillRegistry("traversal_name_in_index", catalog=catalog) as registry:
        names = [item["name"] for item in json.loads(urllib.request.urlopen(registry.base_url + "/index.json").read())["skills"]]
        assert "../etc/passwd" in names
    with module.FakeSkillRegistry("status_503", catalog=catalog) as registry:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(registry.base_url + "/index.json")
        assert module.UPSTREAM_SECRET_BODY.encode() in exc.value.read()
    with module.FakeSkillRegistry("oversize_file", catalog=catalog) as registry:
        assert len(urllib.request.urlopen(registry.base_url + "/skills/pump-room-notes/1.0.0/SKILL.md").read()) > 64 * 1024


@pytest.mark.skipif(shutil.which("openssl") is None, reason="需要 openssl 生成自签证书")
def test_fake_registry_tls_mode_serves_https_with_a_trusted_self_signed_certificate(tmp_path: Path) -> None:
    """假注册表 TLS 模式起 https，客户端上下文信任自签证书（需要 ``openssl``；没有时由 ``skipif`` 跳过）。

    **不登记为 P7-S13-A01 的验证项**：追溯检查不允许 ``delivered`` 行引用可能被跳过的测试。这一面由数据集里 ``requires: ["openssl"]`` 的
    两条用例（``sf-reg-tls-untrusted`` / ``sf-reg-tls-trusted-benign``）承担——无 openssl 时它们结构化 SKIP 并带 ``requires``，不算违规。"""

    import urllib.request

    module = ev.fake_module()
    with module.FakeSkillRegistry("ok", catalog={"pump-room-notes": {"1.0.0": module.HOSTILE_PACKAGES["benign"]("pump-room-notes", "1.0.0")}},
                                  tls_dir=tmp_path / "tls") as registry:
        assert registry.base_url.startswith("https://")
        assert json.loads(urllib.request.urlopen(registry.base_url + "/index.json", context=registry.ssl_context()).read())["version"] == 1


def test_hostile_generators_produce_the_bytes_they_claim() -> None:
    """P7-S13-A02 每个包生成器产出的字节确实带着它声称的特征（不可见字符一律运行期拼接；源文件里没有字面不可见字符）。"""

    module = ev.fake_module()
    make = lambda key: module.HOSTILE_PACKAGES[key]("pump-room-notes", "1.0.0")
    assert any(0xE0000 <= ord(char) <= 0xE007F for char in make("tag_smuggle").decode())
    assert any(0xFE00 <= ord(char) <= 0xFE0F or 0xE0100 <= ord(char) <= 0xE01EF for char in make("variation_selector").decode())
    assert b"\r\n" in make("crlf") and b"\x00" in make("nul") and make("bom").startswith(b"\xef\xbb\xbf")
    with pytest.raises(UnicodeDecodeError):
        make("bad_utf8").decode("utf-8")
    assert chr(0x200B) in make("zero_width").decode() and chr(0x202E) in make("bidi_override").decode()
    assert chr(0x430) in make("cyrillic_homoglyph").decode() and chr(0x212A) in make("kelvin_name").decode()
    header = make("yaml_alias_bomb").split(b"---\n")[1]
    assert len(header) < 400 and b"*a" in header and b"&a" in header

    folded = make("greek_fold_injection").decode()
    from water_agent.skills.charset import disallowed_characters, fold_greek_lookalikes

    assert module.INJECTION_SENTENCE not in folded and disallowed_characters(folded) == []
    assert module.INJECTION_SENTENCE in fold_greek_lookalikes(folded)
    for key in ("yaml_anchor", "yaml_tag", "yaml_duplicate_key", "yaml_merge_key"):
        assert key.startswith("yaml_") and make(key).startswith(b"---\n")
    assert b"https://" in make("links_body") and b"```bash" in make("shell_fence_body") and b"autocad_read_dwg" in make("requires_missing_tool")

    source = (REPO_ROOT / "tests" / "fixtures" / "fake_skill_registry.py").read_text(encoding="utf-8")
    hazardous = [(char, hex(ord(char))) for char in source
                 if unicodedata.category(char) in {"Cf", "Mn", "Co", "Cn", "Zl", "Zp"} or 0xE0000 <= ord(char) <= 0xE01EF]
    assert hazardous == [], "源文件里不得有字面不可见字符（编辑中会静默丢失）"
