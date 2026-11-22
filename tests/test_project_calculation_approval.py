from datetime import UTC, datetime, timedelta
from dataclasses import replace
from uuid import uuid4

import pytest
from test_project_calculation_revision import setup_service, confirm


@pytest.mark.parametrize("decision", ["false", 0, 1, None, {}, []])
def test_T27_068_invalid_then_valid(tmp_path, decision):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    before = service.execute(ctx)
    assert service.approve(ctx, decision, "bad")["status"] == "waiting_approval"
    assert service.recover(ctx) == before
    result = service.approve(ctx, True, "accepted")
    assert result["status"] == "approved"
    assert result["approval"]["record"]["final_package_hash"] == result["release_manifest_hash"]
    assert (
        result["approval"]["record"]["artifact_collection_hash"] == result["release_manifest_hash"]
    )
    assert len(result["approval"]["record"]["artifact_ids"]) == 6
    assert result["execution_manifest_hash"] != result["release_manifest_hash"]
    assert service.release_check(ctx.run_id, ctx)["valid"]
    with pytest.raises(ValueError):
        service.approve(ctx, False, "duplicate")


def test_false_persisted_and_wrong_run_rejected(tmp_path):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    service.execute(ctx)
    with pytest.raises(ValueError):
        service.approve(replace(ctx, run_id=str(uuid4())), True, "wrong")
    run = service.approve(ctx, False, "needs new coefficients")
    assert run["status"] == "failed"
    assert run["approval"]["record"]["approved"] is False
    assert service.recover(ctx) == run


def test_T27_067_expiry_and_revocation(tmp_path):
    service, plan, ctx, auth = setup_service(tmp_path)
    confirm(service, plan, ctx)
    service.execute(ctx)
    service.approve(ctx, True, "ok")
    service.now = lambda: datetime.now(UTC) + timedelta(minutes=31)
    assert not service.release_check(ctx.run_id, ctx)["valid"]
    service.now = lambda: datetime.now(UTC)
    auth.allowed = False
    with pytest.raises(ValueError, match="PROJECT_SCOPE_MISMATCH"):
        service.recover(ctx)
    auth.allowed = True
    auth.broken = True
    with pytest.raises(ValueError, match="PROJECT_SCOPE_MISMATCH"):
        service.recover(ctx)


@pytest.mark.parametrize("mutation", ["parent", "revision", "version", "payload", "missing"])
def test_collection_drift(tmp_path, mutation):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    service.execute(ctx)
    service.approve(ctx, True, "ok")
    with service._locked(ctx):
        state = service._load(ctx)
        run = state["runs"][ctx.run_id]
        env = run["artifacts"][run["active_artifact_ids"]["document"]]
        if mutation == "parent":
            env["parent_artifact_ids"] = []
        elif mutation == "revision":
            env["revision"] += 1
        elif mutation == "version":
            env["schema_version"] = "1.0"
        elif mutation == "payload":
            env["payload"]["content"] = "drift"
        else:
            run["active_artifact_ids"].pop("review")
        service._save(ctx, state)
    assert not service.release_check(ctx.run_id, ctx)["valid"]


@pytest.mark.parametrize(
    "field,value", [("artifact_type", "calc_package"), ("producer", "drawing")]
)
def test_role_metadata_drift_rejected(tmp_path, field, value):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    service.execute(ctx)
    service.approve(ctx, True, "ok")
    with service._locked(ctx):
        state = service._load(ctx)
        run = state["runs"][ctx.run_id]
        run["artifacts"][run["active_artifact_ids"]["document"]][field] = value
        service._save(ctx, state)
    assert not service.release_check(ctx.run_id, ctx)["valid"]


@pytest.mark.parametrize(
    "field",
    [
        "context",
        "plan_hash",
        "review_artifact_id",
        "approved_by_user_id",
        "release_manifest_hash",
        "record",
    ],
)
def test_recovery_rechecks_approval(tmp_path, field):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    service.execute(ctx)
    service.approve(ctx, True, "ok")
    with service._locked(ctx):
        state = service._load(ctx)
        approval = state["runs"][ctx.run_id]["approval"]
        if field == "record":
            approval["record"]["approved"] = False
        elif field == "context":
            approval[field] = {}
        else:
            approval[field] = "wrong"
        service._save(ctx, state)
    with pytest.raises(ValueError):
        service.recover(ctx)


def test_confirmation_and_pending_approval_expiry(tmp_path):
    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    service.now = lambda: datetime.now(UTC) + timedelta(minutes=16)
    with pytest.raises(ValueError, match="PROJECT_STALE_SOURCE"):
        service.execute(ctx)
    service.now = lambda: datetime.now(UTC)
    service.execute(ctx)
    service.now = lambda: datetime.now(UTC) + timedelta(minutes=31)
    with pytest.raises(ValueError, match="PROJECT_APPROVAL_STALE"):
        service.approve(ctx, True, "late")


def test_complete_set_approval_binding(tmp_path):
    from water_agent.multi_agent.contracts import ApprovalRecord

    service, plan, ctx, _ = setup_service(tmp_path)
    confirm(service, plan, ctx)
    service.execute(ctx)
    service.approve(ctx, True, "ok")
    with service._locked(ctx):
        state = service._load(ctx)
        run = state["runs"][ctx.run_id]
        record = run["approval"]["record"]
        for key in (
            "artifact_ids",
            "artifact_hashes",
            "artifact_revisions",
            "artifact_parent_artifact_ids",
        ):
            record[key] = record[key][:-1]
        record["artifact_collection_hash"] = None
        record["final_package_hash"] = None
        run["approval"]["record"] = ApprovalRecord.model_validate(record).model_dump()
        service._save(ctx, state)
    assert not service.release_check(ctx.run_id, ctx)["valid"]
    with pytest.raises(ValueError):
        service.recover(ctx)
