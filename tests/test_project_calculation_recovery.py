import json
import os
import subprocess
from pathlib import Path

import pytest
from test_project_calculation_revision import setup_service, confirm


def test_recover_no_replay(tmp_path, monkeypatch):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    run = service.execute(ctx)
    from water_agent.tools import project_calculation as tools

    monkeypatch.setattr(tools, "run_calc", lambda *_: pytest.fail("replayed"))
    assert service.recover(ctx) == run
    assert service.recover(ctx) == run


@pytest.mark.parametrize("point", ["started", "associated", "verified"])
def test_persistence_failure_stops_successors(tmp_path, monkeypatch, point):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    calls = []
    from water_agent.tools import project_calculation as tools

    original = tools.run_calc

    def kernel(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(tools, "run_calc", kernel)

    def fault(stage):
        if stage == point:
            raise OSError("synthetic storage failure")

    service.fault = fault
    run = service.execute(ctx)
    assert run["status"] != "waiting_approval"
    assert len(calls) == (1 if point == "verified" else 0)
    assert not any(r["status"] == "verified" for r in run["execution_records"].values())


def test_cancel_late_result(tmp_path, monkeypatch):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    from water_agent.tools import project_calculation as tools

    original = tools.run_calc
    calls = []

    def kernel(*args):
        calls.append(args)
        service.cancel(ctx)
        return original(*args)

    monkeypatch.setattr(tools, "run_calc", kernel)
    run = service.execute(ctx)
    assert run["status"] == "cancelled" and len(calls) == 1
    recovered = service.recover(ctx)
    assert recovered["active_artifact_ids"] == {}
    assert all(r["status"] == "cancelled" for r in recovered["execution_records"].values())


def test_authority_revoked_during_execution(tmp_path, monkeypatch):
    service, plan, ctx, authority = setup_service(tmp_path)
    confirm(service, plan, ctx)
    from water_agent.tools import project_calculation as tools

    original = tools.run_calc

    def kernel(*args):
        authority.epoch += 1
        return original(*args)

    monkeypatch.setattr(tools, "run_calc", kernel)
    run = service.execute(ctx)
    assert run["status"] == "blocked"
    assert not run["active_artifact_ids"]


CHILD = """
import json, os, sys
from dataclasses import replace
from pathlib import Path
from test_project_calculation_execution import fixture
from test_project_calculation_revision import Authority
from water_agent.multi_agent.project_calculation_service import CalculationService
p, c, _ = fixture()
data=json.loads(Path(sys.argv[2]).read_text())
c=replace(c, **data['identity'])
s=CalculationService(Path(sys.argv[1]), Authority(c))
mode=sys.argv[3]
if mode in ('started','associated'):
 s.fault=lambda point: os._exit(17) if point=='after_'+mode else None
 s.execute(c)
elif mode=='recover':
 from water_agent.tools import project_calculation as tools
 tools.run_calc=lambda *_: os._exit(99)
 print(json.dumps(s.recover(c)))
elif mode=='race':
 import time
 while not Path(sys.argv[1], 'go').exists(): time.sleep(.01)
 try:
  s.confirm(data['plan'], c, base_revision=0, generation=0)
  from water_agent.tools import project_calculation as tools
  original=tools.run_calc
  calls=[]
  def count(*args):
   calls.append(1)
   return original(*args)
  tools.run_calc=count
  result=s.execute(c)
  assert result['status']=='waiting_approval'
  assert len(calls)==3
  print('winner')
 except ValueError as e: print(str(e))
"""


def child_args(tmp_path, ctx, plan, mode):
    identity = {k: getattr(ctx, k) for k in ("owner_user_id", "thread_id", "run_id")}
    data = tmp_path / ("fixture-" + mode + "-" + ctx.run_id + ".json")
    data.write_text(json.dumps({"identity": identity, "plan": plan}))
    data.chmod(0o600)
    return ["_venv/bin/python", "-c", CHILD, str(tmp_path), str(data), mode]


def env():
    return {**os.environ, "PYTHONPATH": "src:tests"}


@pytest.mark.parametrize("point", ["started", "associated"])
def test_real_process_crash_then_reopen(tmp_path, point):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    child = subprocess.run(
        child_args(tmp_path, ctx, plan, point), env=env(), capture_output=True, text=True
    )
    assert child.returncode == 17, child.stderr
    with service._locked(ctx):
        stored = service._load(ctx)["runs"][ctx.run_id]
    records = list(stored["execution_records"].values())
    assert len(records) == 1 and records[0]["status"] == "started"
    assert bool(records[0]["invocation_id"]) == (point == "associated")

    reopened = subprocess.run(
        child_args(tmp_path, ctx, plan, "recover"), env=env(), capture_output=True, text=True
    )
    assert reopened.returncode == 0, reopened.stderr
    run = json.loads(reopened.stdout)
    assert run["error"] == "PROJECT_EXECUTION_INDETERMINATE"
    assert run["status"] == "indeterminate"
    assert all(r["outputs"] == {} for r in run["execution_records"].values())


def test_T27_065_real_process_same_base(tmp_path):
    from dataclasses import replace
    from uuid import uuid4

    service, plan, ctx, _ = setup_service(tmp_path)
    first = subprocess.Popen(
        child_args(tmp_path, ctx, plan, "race"),
        env=env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    other = replace(ctx, run_id=str(uuid4()))
    second = subprocess.Popen(
        child_args(tmp_path, other, plan, "race"),
        env=env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    (tmp_path / "go").touch(mode=0o600)
    outputs = [p.communicate(timeout=20) for p in (first, second)]
    saved = service.inspect(ctx)
    assert len(saved["runs"]) == 1
    assert set(saved["runs"]).issubset({ctx.run_id, other.run_id})
    assert ctx.run_id != other.run_id
    assert all(p.returncode == 0 for p in (first, second)), outputs
    assert sorted(o[0].strip() for o in outputs) == ["PROJECT_REVISION_CONFLICT", "winner"]


@pytest.mark.parametrize("state", ["waiting_approval", "approved", "cancelled", "failed"])
def test_process_reopen_known_states_zero_calls(tmp_path, state):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    service.execute(ctx)
    if state == "approved":
        service.approve(ctx, True, "ok")
    elif state == "cancelled":
        service.cancel(ctx)
    elif state == "failed":
        service.approve(ctx, False, "no")
    before = service.recover(ctx)
    reopened = subprocess.run(
        child_args(tmp_path, ctx, plan, "recover"), env=env(), capture_output=True, text=True
    )
    assert reopened.returncode == 0, reopened.stderr
    assert json.loads(reopened.stdout) == before


@pytest.mark.parametrize("identity", ["owner_user_id", "thread_id", "project_id"])
def test_recovery_cross_identity_rejected(tmp_path, identity):
    from dataclasses import replace
    from uuid import uuid4

    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    service.execute(ctx)
    other = replace(ctx, **{identity: "other" if identity == "project_id" else str(uuid4())})
    with pytest.raises(ValueError, match="PROJECT_SCOPE_MISMATCH"):
        service.recover(other)


@pytest.mark.parametrize("field", ["schema_version", "parent_artifact_ids", "node_record"])
def test_T27_056_legacy_checkpoint_not_trusted(tmp_path, field):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    service.execute(ctx)
    with service._locked(ctx):
        state = service._load(ctx)
        run = state["runs"][ctx.run_id]
        env = run["artifacts"][run["active_artifact_ids"]["hf"]]
        if field == "schema_version":
            env[field] = "1.0"
        elif field == "parent_artifact_ids":
            env[field] = []
        else:
            env["payload"] = {"cases": []}
        service._save(ctx, state)
    with pytest.raises(ValueError, match="PROJECT_UNTRUSTED_SOURCE"):
        service.recover(ctx)


def test_real_process_live_cancel_and_late_result(tmp_path):
    import time

    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    code = CHILD.replace(
        "if mode in ('started','associated'):",
        """if mode == 'live':
 from water_agent.tools import project_calculation as tools
 original=tools.run_calc
 calls=[]
 def kernel(*args):
  calls.append(1)
  Path(sys.argv[1], 'executing').touch(mode=0o600)
  import time
  while not Path(sys.argv[1], 'release').exists(): time.sleep(.01)
  return original(*args)
 tools.run_calc=kernel
 result=s.execute(c)
 print(json.dumps({'run':result,'calls':len(calls)}))
elif mode in ('started','associated'):""",
    )
    args = child_args(tmp_path, ctx, plan, "live")
    args[2] = code
    process = subprocess.Popen(
        args, env=env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        deadline = time.monotonic() + 15
        while not (tmp_path / "executing").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (tmp_path / "executing").exists()
        assert service.recover(ctx)["status"] == "running"
        service.cancel(ctx)
        (tmp_path / "release").touch(mode=0o600)
        out, err = process.communicate(timeout=15)
        assert process.returncode == 0, err
        result = json.loads(out)
        assert result["calls"] == 1 and result["run"]["status"] == "cancelled"
        assert result["run"]["active_artifact_ids"] == {}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_authorize_before_checkpoint_read(tmp_path, monkeypatch):
    service, plan, ctx, authority = setup_service(tmp_path)
    confirm(service, plan, ctx)
    authority.allowed = False
    monkeypatch.setattr(service, "_load", lambda *_: pytest.fail("unauthorized checkpoint read"))
    with pytest.raises(ValueError, match="PROJECT_SCOPE_MISMATCH"):
        service.execute(ctx)
    with pytest.raises(ValueError, match="PROJECT_SCOPE_MISMATCH"):
        service.approve(ctx, True, "no")


def test_missing_context_checkpoint_fails_structured(tmp_path):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    with service._locked(ctx):
        state = service._load(ctx)
        state["runs"][ctx.run_id].pop("context")
        service._save(ctx, state)
    with pytest.raises(ValueError, match="PROJECT_UNTRUSTED_SOURCE"):
        service.recover(ctx)
