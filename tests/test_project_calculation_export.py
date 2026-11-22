"""T27-069..078: immutable v2 publication, synthetic storage only."""

from dataclasses import replace
from uuid import uuid4

import pytest
from test_project_calculation_execution import fixture
from test_project_calculation_revision import Authority

from water_agent.documents.storage import ExportSnapshotStore
from water_agent.multi_agent.project_calculation_service import CalculationService
from water_agent.multi_agent.project_publication import DocumentBackend, ProjectPublisher


def setup_publication(tmp_path):
    plan, ctx, _ = fixture()
    authority = Authority(ctx)
    backend = DocumentBackend(tmp_path / "documents")
    service = CalculationService(tmp_path / "state", authority, document_backend=backend)
    service.confirm(plan, ctx, base_revision=0, generation=0)
    assert service.execute(ctx)["status"] == "waiting_approval"
    service.approve(ctx, True, "synthetic host approval")
    statuses = []
    publisher = ProjectPublisher(
        service,
        ExportSnapshotStore(tmp_path / "snapshots"),
        backend,
        set_succeeded=lambda c: statuses.append(c.run_id) if c.run_id not in statuses else None,
    )
    return publisher, service, plan, ctx, statuses


def test_T27_069_076_077_078_same_run_and_original_bytes(tmp_path, monkeypatch):
    pub, service, plan, ctx, statuses = setup_publication(tmp_path)
    record = pub.publish(ctx)
    content = pub.document(ctx)
    assert record["version"] == 2 and record["snapshot"]["calc_inputs"] == {}
    assert record["snapshot"]["project_calculation"]["status"] == "ready"
    assert pub.read(ctx) == record and pub.document(ctx) == content
    assert statuses == [ctx.run_id]
    from water_agent.tools import project_calculation as tools

    monkeypatch.setattr(tools, "run_calc", lambda *_: pytest.fail("replayed"))
    assert pub.publish(ctx) == record
    # A later revision cannot overwrite the already committed run.
    other = replace(ctx, run_id=str(uuid4()))
    service.confirm(plan, other, base_revision=1, generation=1)
    assert pub.document(ctx) == content


@pytest.mark.parametrize("point", ["prepared", "snapshot_written", "committed"])
def test_T27_073_075_two_phase_faults(tmp_path, point):
    pub, service, plan, ctx, statuses = setup_publication(tmp_path)
    pub.fault = lambda stage: (
        (_ for _ in ()).throw(OSError("synthetic crash")) if stage == point else None
    )
    with pytest.raises((OSError, ValueError)):
        pub.publish(ctx)
    assert statuses == []
    if point != "committed":
        with pytest.raises(ValueError):
            pub.read(ctx)
        with pytest.raises(ValueError):
            pub.publish(ctx)
    else:
        pub.fault = lambda _: None
        record = pub.read(ctx)
        assert record["version"] == 2 and statuses == [ctx.run_id]


@pytest.mark.parametrize("missing", ["snapshot", "blob", "blob_changed"])
def test_T27_074_commit_missing_parts_fail_closed(tmp_path, missing):
    pub, service, plan, ctx, statuses = setup_publication(tmp_path)
    record = pub.publish(ctx)
    if missing == "snapshot":
        (pub.snapshots.root / (ctx.run_id + ".json")).unlink()
    else:
        path = pub.documents.root / (
            record["snapshot"]["document_blob"]["document_file_id"] + ".docx"
        )
        if missing == "blob":
            path.unlink()
        else:
            path.write_bytes(b"changed")
    with pytest.raises(ValueError):
        pub.read(ctx)


def test_T27_079_output_guard_blocked(tmp_path, monkeypatch):
    pub, service, plan, ctx, statuses = setup_publication(tmp_path)
    monkeypatch.setattr(pub, "check_output", lambda _: False)
    with pytest.raises(ValueError):
        pub.publish(ctx)
    assert statuses == [] and pub.snapshots.read(ctx.run_id) is None


@pytest.mark.parametrize("field", ["revision", "generation"])
def test_v2_rejects_bool_metadata(tmp_path, field):
    pub, service, plan, ctx, statuses = setup_publication(tmp_path)
    record = pub.publish(ctx)
    record["snapshot"]["project_binding"][field] = True
    with pytest.raises(ValueError):
        pub.snapshots.write(record)


def test_cancel_cannot_invalidate_committed_run(tmp_path):
    pub, service, plan, ctx, statuses = setup_publication(tmp_path)
    record = pub.publish(ctx)
    content = pub.document(ctx)
    service.cancel(ctx)
    assert pub.read(ctx) == record and pub.document(ctx) == content


def test_document_guard_runs_before_any_write(tmp_path, monkeypatch):
    from test_project_calculation_revision import setup_service, confirm
    import water_agent.multi_agent.project_publication as module

    service, plan, ctx, _ = setup_service(tmp_path / "plain")
    confirm(service, plan, ctx)
    run = service.execute(ctx)
    backend = DocumentBackend(tmp_path / "withheld")
    monkeypatch.setattr(module, "check_output", lambda _: False)
    with pytest.raises(ValueError, match="PROJECT_OUTPUT_BLOCKED"):
        backend.create(run)
    assert not backend.root.exists()


def test_v2_context_generation_drift_rejected(tmp_path):
    pub, service, plan, ctx, statuses = setup_publication(tmp_path)
    record = pub.publish(ctx)
    record["snapshot"]["project_binding"]["generation"] += 1
    record["finalize"]["payload"]["project_binding"]["generation"] += 1
    with pytest.raises(ValueError):
        pub.snapshots.write(record)


def test_valid_docx_with_changed_bytes_is_unavailable(tmp_path):
    pub, service, plan, ctx, statuses = setup_publication(tmp_path)
    record = pub.publish(ctx)
    path = pub.documents.root / (record["snapshot"]["document_blob"]["document_file_id"] + ".docx")
    path.write_bytes(path.read_bytes() + b"\nsynthetic-modification")
    with pytest.raises(ValueError):
        pub.document(ctx)
