"""A1 uses synthetic slots only; production source volumes are never mounted."""

from __future__ import annotations

import copy
import json
import logging
import os
import stat
from pathlib import Path

import pytest

from desktop.macos.runtime import credential_handoff as handoff
from desktop.macos.runtime.paths import private_directory, private_write
from water_agent.config import credentials
from desktop.macos.runtime import handoff_source

IMAGE = "sha256:" + "a" * 64
BINDING = {
    "version": 1,
    "volume": "wa-a1-test-source",
    "image": IMAGE,
    "chat": {"base_url": "https://model-endpoint.invalid/v1", "model": "deepseek-v4.1-flash"},
    "embedding": {
        "base_url": "https://model-endpoint.invalid/v1",
        "model": "text-embedding-3-large",
    },
}
NAMES = ("WATER_AGENT_LLM_API_KEY", "WATER_AGENT_EMBEDDING_API_KEY")


@pytest.fixture(autouse=True)
def isolate_operator_process_state(monkeypatch):
    """The real entry is a standalone process; reproduce that boundary in tests."""
    import urllib.request
    import httpx

    environment = dict(os.environ)
    injected = set(credentials._INJECTED)
    logging_level = logging.root.manager.disable
    umask = os.umask(0o077)
    os.umask(umask)

    def no_network(*_args, **_kwargs):
        pytest.fail("A1 mechanism tests must not send HTTP requests")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setattr(httpx.Client, "send", no_network)
    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    yield
    os.environ.clear()
    os.environ.update(environment)
    credentials._INJECTED.clear()
    credentials._INJECTED.update(injected)
    logging.disable(logging_level)
    os.umask(umask)


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = private_directory(tmp_path / "models")
    monkeypatch.setenv("WATER_AGENT_CREDENTIALS_DIR", str(root))
    for name in credentials.CREDENTIALS:
        monkeypatch.setenv(name, "")
        monkeypatch.setenv(name + "_FILE", "")
    credentials.forget_injected_credentials()
    yield root
    credentials.forget_injected_credentials()


def payload():
    return {
        "version": 1,
        "chat": copy.deepcopy(BINDING["chat"]),
        "embedding": copy.deepcopy(BINDING["embedding"]),
        "values": {NAMES[0]: "A1SyntheticChatValue", NAMES[1]: "A1SyntheticEmbeddingValue"},
    }


def test_binding_requires_exact_public_fields_and_private_file(tmp_path):
    p = tmp_path / "binding.json"
    private_write(p, json.dumps(BINDING))
    assert handoff.read_binding(p) == BINDING
    p.chmod(0o644)
    with pytest.raises(handoff.HandoffError, match="HANDOFF_UNSAFE_FILE"):
        handoff.read_binding(p)


@pytest.mark.parametrize(
    "bad",
    [
        {**BINDING, "image": "mutable:latest"},
        {**BINDING, "volume": "source:/another:rw"},
        {**BINDING, "values": {"secret": "must not be allowed"}},
        {**BINDING, "chat": {"base_url": "https://user:password@example.com", "model": "x"}},
    ],
)
def test_bad_binding_is_rejected_without_echo(tmp_path, bad):
    p = tmp_path / "binding.json"
    private_write(p, json.dumps(bad))
    with pytest.raises(handoff.HandoffError) as e:
        handoff.read_binding(p)
    assert "password" not in str(e.value) and "must not" not in str(e.value)


def test_batch_uses_production_writer_and_private_modes(store):
    handoff.install_payload(payload(), BINDING)
    for name in NAMES:
        assert credentials.resolve_secret(name) == payload()["values"][name]
        assert stat.S_IMODE((store / credentials.CREDENTIALS[name]).stat().st_mode) == 0o600


def test_second_write_failure_restores_original_slots(store, monkeypatch):
    for name in NAMES:
        credentials.write_credential(name, "A1OriginalSyntheticValue")
    original = credentials.write_credential
    calls = 0

    def fail_second(name, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("A1SyntheticEmbeddingValue")
        original(name, value)

    monkeypatch.setattr(credentials, "write_credential", fail_second)
    with pytest.raises(handoff.HandoffError, match="HANDOFF_WRITE_FAILED"):
        handoff.install_payload(payload(), BINDING)
    assert all(credentials.resolve_secret(n) == "A1OriginalSyntheticValue" for n in NAMES)


def test_missing_old_slots_are_removed_after_failure(store, monkeypatch):
    original = credentials.write_credential

    def fail_embedding(name, value):
        if name == NAMES[1]:
            raise RuntimeError(value)
        original(name, value)

    monkeypatch.setattr(credentials, "write_credential", fail_embedding)
    with pytest.raises(handoff.HandoffError):
        handoff.install_payload(payload(), BINDING)
    assert all(not (store / credentials.CREDENTIALS[n]).exists() for n in NAMES)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "broad_mode", "temporary"])
def test_target_unsafe_files_fail_before_secret_resolution(store, monkeypatch, kind):
    target = store / credentials.CREDENTIALS[NAMES[0]]
    external = store.parent / "other"
    private_write(external, "A1OriginalSyntheticValue")
    if kind == "symlink":
        target.symlink_to(external)
    if kind == "hardlink":
        target.hardlink_to(external)
    if kind == "fifo":
        os.mkfifo(target, 0o600)
    if kind == "broad_mode":
        target.write_text("A1OriginalSyntheticValue")
        target.chmod(0o644)
    if kind == "temporary":
        target.with_name(target.name + ".tmp").symlink_to(external)
    monkeypatch.setattr(
        credentials, "resolve_secret", lambda *_a, **_k: pytest.fail("unsafe file read")
    )
    with pytest.raises(handoff.HandoffError):
        handoff.install_payload(payload(), BINDING)
    assert external.read_text() == "A1OriginalSyntheticValue"


def test_mismatched_payload_writes_nothing(store):
    p = payload()
    p["embedding"]["model"] = "wrong-model"
    with pytest.raises(handoff.HandoffError, match="HANDOFF_BINDING_MISMATCH"):
        handoff.install_payload(p, BINDING)
    assert list(store.iterdir()) == []


def container_info(nonce):
    return {
        "Image": IMAGE,
        "Config": {
            "User": "10001:10001",
            "Entrypoint": ["/app/.venv/bin/python"],
            "Labels": {handoff.LABEL: nonce},
            "Env": ["PATH=/usr/local/bin:/usr/bin"],
        },
        "HostConfig": {
            "LogConfig": {"Type": "none"},
            "NetworkMode": "none",
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "CapAdd": None,
            "Privileged": False,
            "Memory": 256 * 1024 * 1024,
            "PidsLimit": 64,
            "SecurityOpt": ["no-new-privileges"],
            "Tmpfs": {"/handoff": "rw,noexec,nosuid,size=1m,mode=0700,uid=10001,gid=10001"},
        },
        "Mounts": [
            {
                "Type": "volume",
                "Name": BINDING["volume"],
                "Destination": handoff.SOURCE_ROOT,
                "RW": False,
            }
        ],
    }


class FakeDocker:
    def __init__(self, mutate=None, fail=None):
        self.calls = []
        self.name = None
        self.nonce = None
        self.removed = False
        self.started = False
        self.mutate = mutate
        self.fail = fail

    def call(self, args, **_):
        self.calls.append(args)
        if args[0] == "volume":
            return BINDING["volume"].encode()
        if args[0] == "create":
            self.name = args[args.index("--name") + 1]
            self.nonce = args[args.index("--label") + 1].split("=", 1)[1]
            if self.fail == "create":
                raise OSError("A1SyntheticChatValue")
            return b"container-id"
        if args[0] == "inspect":
            info = container_info(self.nonce)
            if self.mutate:
                self.mutate(info)
            return json.dumps([info]).encode()
        if args[0] == "start":
            self.started = True
            return self.name.encode()
        if args[0] == "ps":
            return self.name.encode() if self.name and not self.removed else b""
        if args[0] == "rm":
            if self.fail == "cleanup":
                raise RuntimeError("A1SyntheticChatValue")
            self.removed = True
            return self.name.encode()
        raise AssertionError(args[0])

    def copy_to_file(self, name, remote, destination, **_):
        self.calls.append(["private-copy", name, remote, str(destination)])
        if self.fail == "copy":
            raise OSError("A1SyntheticChatValue")
        private_write(
            destination, json.dumps({"ok": True} if remote == "status.json" else payload())
        )
        return True


def test_source_transport_copies_file_and_never_passes_value_in_arguments(tmp_path):
    stage = private_directory(tmp_path / "stage")
    docker = FakeDocker()
    result = handoff.fetch_payload(BINDING, stage, docker)
    assert result == payload() and docker.started and docker.removed
    create = next(a for a in docker.calls if a[0] == "create")
    assert create[create.index("--log-driver") + 1] == "none"
    assert create[create.index("--entrypoint") + 1] == "/app/.venv/bin/python"
    assert "-I" in create and "-S" in create and "-B" in create
    assert "readonly" in create[create.index("--mount") + 1]
    for secret in payload()["values"].values():
        assert secret not in repr(docker.calls)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda i: i["HostConfig"]["LogConfig"].update(Type="json-file"),
        lambda i: i["HostConfig"].update(NetworkMode="bridge"),
        lambda i: i["HostConfig"].update(ReadonlyRootfs=False),
        lambda i: i["HostConfig"].update(SecurityOpt=["no-new-privileges=false"]),
        lambda i: i["Mounts"][0].update(RW=True),
        lambda i: i["Config"].update(Entrypoint=["/app/start.sh"]),
        lambda i: i["Config"].update(Env=["WATER_AGENT_LLM_API_KEY=A1SyntheticChatValue"]),
        lambda i: i["Config"].update(Env=["DEEPSEEK_API_KEY_FILE=/unsafe/file"]),
        lambda i: i["HostConfig"]["Tmpfs"].update({"/handoff": "rw,noexec,nosuid,mode=0777"}),
    ],
)
def test_daemon_policy_is_checked_before_start(tmp_path, mutate):
    docker = FakeDocker(mutate=mutate)
    with pytest.raises(handoff.HandoffError, match="HANDOFF_UNSAFE_CONTAINER"):
        handoff.fetch_payload(BINDING, private_directory(tmp_path / "stage"), docker)
    assert not docker.started and docker.removed


@pytest.mark.parametrize(
    "failure,code",
    [
        ("create", "HANDOFF_SOURCE_UNAVAILABLE"),
        ("copy", "HANDOFF_SOURCE_UNAVAILABLE"),
        ("cleanup", "HANDOFF_CLEANUP_FAILED"),
    ],
)
def test_source_failures_are_sanitized_and_cleanup_is_attempted(tmp_path, failure, code, capsys):
    docker = FakeDocker(fail=failure)
    with pytest.raises(handoff.HandoffError, match=code) as e:
        handoff.fetch_payload(BINDING, private_directory(tmp_path / "stage"), docker)
    assert "A1SyntheticChatValue" not in str(e.value)
    assert any(a[0] == "ps" for a in docker.calls)
    assert docker.removed or failure == "cleanup"
    assert "A1SyntheticChatValue" not in repr(capsys.readouterr())


def test_cli_argument_errors_never_echo_submitted_value(capsys):
    assert handoff.main(["--api-key", "A1SyntheticChatValue"]) == 2
    out = capsys.readouterr()
    assert json.loads(out.out)["code"] == "HANDOFF_ARGUMENTS_INVALID"
    assert "A1SyntheticChatValue" not in out.out + out.err
    assert str(handoff.HandoffError("A1SyntheticChatValue")) == "HANDOFF_FAILED"


def test_copy_stream_is_connected_only_to_private_file_descriptor(tmp_path, monkeypatch, capsys):
    import subprocess

    transport = object.__new__(handoff.DockerTransport)
    transport.prefix = ["docker", "--host", "unix:///local-test.sock"]
    transport.env = {}
    target = private_directory(tmp_path / "stage") / "payload.json"
    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        assert isinstance(kwargs["stdout"], int) and kwargs["stdout"] != subprocess.PIPE
        assert stat.S_IMODE(os.fstat(kwargs["stdout"]).st_mode) == 0o600
        os.write(kwargs["stdout"], b"A1SyntheticChatValue")
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(handoff.subprocess, "run", run)
    assert transport.copy_to_file("own-container", "payload.json", target)
    assert target.read_bytes() == b"A1SyntheticChatValue"
    assert "A1SyntheticChatValue" not in repr(seen)
    assert "A1SyntheticChatValue" not in repr(capsys.readouterr())


def test_restoration_failure_is_distinct_and_sanitized(store, monkeypatch):
    for name in NAMES:
        credentials.write_credential(name, "A1OriginalSyntheticValue")
    monkeypatch.setattr(
        credentials,
        "write_credential",
        lambda *_: (_ for _ in ()).throw(RuntimeError("A1OriginalSyntheticValue")),
    )
    with pytest.raises(handoff.HandoffError, match="HANDOFF_ROLLBACK_FAILED") as e:
        handoff.install_payload(payload(), BINDING)
    assert "A1OriginalSyntheticValue" not in str(e.value)


def test_silent_bad_restoration_is_detected_by_readback(store, monkeypatch):
    for name in NAMES:
        credentials.write_credential(name, "A1OriginalSyntheticValue")
    calls = 0

    def corrupt_restore(name, value):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("A1SyntheticChatValue")
        private_write(store / credentials.CREDENTIALS[name], "short")

    monkeypatch.setattr(credentials, "write_credential", corrupt_restore)
    with pytest.raises(handoff.HandoffError, match="HANDOFF_ROLLBACK_FAILED"):
        handoff.install_payload(payload(), BINDING)


def test_cancelled_write_restores_slots(store, monkeypatch):
    original = credentials.write_credential
    calls = 0

    def interrupt(name, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        original(name, value)

    monkeypatch.setattr(credentials, "write_credential", interrupt)
    with pytest.raises(handoff.HandoffError, match="HANDOFF_WRITE_FAILED"):
        handoff.install_payload(payload(), BINDING)
    assert all(not (store / credentials.CREDENTIALS[n]).exists() for n in NAMES)


def test_source_snapshot_uses_production_resolver_and_preserves_source(tmp_path):
    source = private_directory(tmp_path / "source")
    out = private_directory(tmp_path / "out")
    for name, value in payload()["values"].items():
        private_write(source / credentials.CREDENTIALS[name], value)
    private_write(source / "llm_endpoint.json", json.dumps(BINDING["chat"]))
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    handoff_source.produce(BINDING, source, out)
    assert json.loads((out / "payload.json").read_text()) == payload()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    assert not list(out.glob("source-*"))
    assert stat.S_IMODE((out / "payload.json").stat().st_mode) == 0o600


def test_source_symlink_rejected_before_endpoint_resolution(tmp_path, monkeypatch):
    from water_agent.generation import endpoints

    source = private_directory(tmp_path / "source")
    out = private_directory(tmp_path / "out")
    real = tmp_path / "external"
    private_write(real, "A1SyntheticChatValue")
    (source / credentials.CREDENTIALS[NAMES[0]]).symlink_to(real)
    monkeypatch.setattr(
        endpoints, "read_panel_endpoint", lambda: pytest.fail("unsafe endpoint read")
    )
    with pytest.raises(handoff.HandoffError, match="HANDOFF_UNSAFE_FILE"):
        handoff_source.produce(BINDING, source, out)


@pytest.mark.parametrize("action", ["exec", "logs", "attach", "cp"])
def test_generic_capture_transport_cannot_be_used_for_secret_streams(action, monkeypatch):
    transport = object.__new__(handoff.DockerTransport)
    monkeypatch.setattr(
        handoff.subprocess, "run", lambda *_a, **_k: pytest.fail("subprocess started")
    )
    with pytest.raises(handoff.HandoffError, match="HANDOFF_UNSAFE_COMMAND"):
        transport.call([action, "container", "payload.json"])


def test_copy_timeout_removes_partial_file_and_suppresses_captured_exception(
    tmp_path, monkeypatch, capsys
):
    import subprocess

    transport = object.__new__(handoff.DockerTransport)
    transport.prefix = ["docker"]
    transport.env = {}
    target = private_directory(tmp_path / "stage") / "payload.json"

    def timeout(argv, **kwargs):
        os.write(kwargs["stdout"], b"A1SyntheticChatValue")
        raise subprocess.TimeoutExpired(argv, 5, output=b"A1SyntheticChatValue")

    monkeypatch.setattr(handoff.subprocess, "run", timeout)
    with pytest.raises(handoff.HandoffError, match="HANDOFF_DOCKER_FAILED") as exc:
        transport.copy_to_file("own-container", "payload.json", target)
    assert not target.exists()
    assert "A1SyntheticChatValue" not in str(exc.value) + repr(capsys.readouterr())


def test_copy_rejects_arbitrary_source_paths(tmp_path):
    transport = object.__new__(handoff.DockerTransport)
    with pytest.raises(handoff.HandoffError, match="HANDOFF_UNSAFE_COMMAND"):
        transport.copy_to_file("own-container", "../../secret", tmp_path / "payload.json")


def desktop_fixture(tmp_path):
    from desktop.macos.runtime.paths import DesktopPaths

    resources = tmp_path / "Resources"
    index = resources / "backend/data/indexes/clauses.db"
    index.parent.mkdir(parents=True)
    index.write_bytes(b"index-selection-only")
    paths = DesktopPaths.create(resources, tmp_path / "profile")
    private_write(paths.credentials / "models/llm_endpoint.json", json.dumps(BINDING["chat"]))
    private_write(
        paths.root / "config/embedding.json",
        json.dumps({**BINDING["embedding"], "retrieval_mode": "hybrid"}),
    )
    binding = tmp_path / "binding.json"
    private_write(binding, json.dumps(BINDING))
    return paths, binding


def test_running_app_lock_prevents_source_and_target_secret_access(tmp_path, monkeypatch):
    from desktop.macos.runtime.paths import InstanceLock

    paths, binding = desktop_fixture(tmp_path)
    monkeypatch.setattr(
        credentials, "resolve_secret", lambda *_a, **_k: pytest.fail("secret access")
    )
    monkeypatch.setattr(handoff, "DockerTransport", lambda *_: pytest.fail("source access"))
    with InstanceLock(paths.root / "instance.lock"):
        with pytest.raises(RuntimeError, match="DESKTOP_ALREADY_RUNNING"):
            handoff.execute(paths, binding, tmp_path / "socket")


def test_partial_copy_cleans_staging_before_any_target_write(tmp_path, store, monkeypatch):
    class Partial(FakeDocker):
        def copy_to_file(self, name, remote, destination, **kwargs):
            private_write(destination, "A1SyntheticChatValue")
            raise OSError("A1SyntheticChatValue")

    paths, binding = desktop_fixture(tmp_path)
    docker = Partial()
    saved = dict(os.environ)
    monkeypatch.setattr(handoff, "DockerTransport", lambda *_: docker)
    monkeypatch.setattr(handoff, "install_payload", lambda *_: pytest.fail("target write"))
    try:
        with pytest.raises(handoff.HandoffError, match="HANDOFF_SOURCE_UNAVAILABLE"):
            handoff.execute(paths, binding, tmp_path / "socket")
    finally:
        os.environ.clear()
        os.environ.update(saved)
    assert docker.removed and not list(paths.credentials.glob(".handoff-*"))


def test_source_timeout_is_non_success_and_still_cleans_container(tmp_path, monkeypatch):
    docker = FakeDocker()
    ticks = iter([0, 21])
    monkeypatch.setattr(handoff.time, "monotonic", lambda: next(ticks))
    with pytest.raises(handoff.HandoffError, match="HANDOFF_SOURCE_TIMEOUT"):
        handoff.fetch_payload(BINDING, private_directory(tmp_path / "stage"), docker)
    assert docker.removed


def test_docker_endpoint_and_environment_are_fixed(monkeypatch):
    import socket
    import tempfile
    import subprocess

    with tempfile.TemporaryDirectory(prefix="a1-sock-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        path = root / "s"
        sock = socket.socket(socket.AF_UNIX)
        sock.bind(str(path))
        try:
            monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted.invalid:2375")
            monkeypatch.setenv("DOCKER_CONTEXT", "untrusted-context")
            monkeypatch.setenv("WATER_AGENT_LLM_API_KEY", "A1SyntheticChatValue")
            transport = handoff.DockerTransport(path, private_directory(root / "cfg"))
            seen = []

            def run(argv, **kwargs):
                seen.append((argv, kwargs))
                return subprocess.CompletedProcess(argv, 0, stdout=b"safe")

            monkeypatch.setattr(handoff.subprocess, "run", run)
            assert transport.call(["volume", "inspect", "test"]) == b"safe"
            assert "--host" in seen[0][0] and "unix://" + str(path) in seen[0][0]
            assert set(seen[0][1]["env"]) == {"PATH", "HOME", "LANG"}
            assert "A1SyntheticChatValue" not in repr(seen)
        finally:
            sock.close()


def test_oversized_secret_file_rejected_without_reading_body(tmp_path):
    p = tmp_path / "oversized"
    private_write(p, b"x" * (handoff_source.MAX_PAYLOAD_BYTES + 1))
    with pytest.raises(handoff.HandoffError, match="HANDOFF_UNSAFE_FILE"):
        handoff_source.private_read(p)


def test_missing_panel_is_not_misclassified_as_missing_credentials(tmp_path):
    source = private_directory(tmp_path / "source")
    out = private_directory(tmp_path / "out")
    for name, value in payload()["values"].items():
        private_write(source / credentials.CREDENTIALS[name], value)
    with pytest.raises(handoff.HandoffError, match="HANDOFF_BINDING_MISMATCH"):
        handoff_source.produce(BINDING, source, out)


@pytest.mark.parametrize("failure", ["volume_create", "seed_timeout"])
def test_canary_harness_cleans_daemon_side_resources_after_cli_failure(
    tmp_path, monkeypatch, failure
):
    import subprocess
    from desktop.macos.packaging import handoff_canary

    state = {"volume": None, "nonce": None, "container": None, "removed": False}

    class Docker:
        def __init__(self, *_):
            self.prefix = ["docker"]
            self.env = {}

        def call(self, args, **_):
            if args[:2] == ["volume", "create"]:
                state["volume"] = args[-1]
                state["nonce"] = args[args.index("--label") + 1].split("=", 1)[1]
                if failure == "volume_create":
                    raise OSError("synthetic failure after daemon create")
                return state["volume"].encode()
            if args[:2] == ["volume", "ls"]:
                assert (
                    args[args.index("--filter") + 1]
                    == "label=" + handoff_canary.CANARY_LABEL + "=" + state["nonce"]
                )
                return state["volume"].encode() if state["volume"] else b""
            if args[:2] == ["volume", "inspect"]:
                return json.dumps({handoff_canary.CANARY_LABEL: state["nonce"]}).encode()
            if args[:2] == ["volume", "rm"]:
                if state["container"]:
                    raise handoff.HandoffError("HANDOFF_CLEANUP_FAILED")
                state["volume"] = None
                state["removed"] = True
                return b""
            if args[0] == "ps":
                return state["container"].encode() if state["container"] else b""
            if args[0] == "inspect":
                return json.dumps(
                    [{"Config": {"Labels": {handoff.LABEL: state["nonce"]}}}]
                ).encode()
            if args[0] == "rm":
                state["container"] = None
                return b""
            raise AssertionError(args[0])

    def seed_timeout(argv, **_):
        state["container"] = (
            argv[argv.index("--name") + 1] if "--name" in argv else "untracked-seed"
        )
        raise subprocess.TimeoutExpired(argv, 20)

    monkeypatch.setattr(handoff, "DockerTransport", Docker)
    monkeypatch.setattr(handoff_canary.subprocess, "run", seed_timeout)
    result = handoff_canary.run_canary(
        tmp_path / "Resources", IMAGE, tmp_path / "socket", tmp_path / "evidence/report.json"
    )
    assert result["ok"] is False
    assert state["removed"] and state["volume"] is None and state["container"] is None
    assert result["canary_volume_removed"] is True
