"""Private per-run snapshots; no model calls or real user data."""
from __future__ import annotations

import copy
import json
import stat
from uuid import uuid4

import pytest

from water_agent.documents.storage import ExportSnapshotStore


def record():
    run, thread, owner = (str(uuid4()) for _ in range(3))
    snapshot = {"run_id": run, "status": "succeeded", "answer": "验收回答", "citations": [], "calculations": [], "calc_inputs": {}, "material_table": None}
    event = {"event_id": "8", "type": "finalize", "run_id": run, "thread_id": thread, "time": "2026-11-09T00:00:00+00:00", "payload_version": "v1", "payload": {"status": "succeeded", "answer": "验收回答", "citations": [], "calculations": [], "export_formats": ["docx"]}}
    return {"version": 1, "owner_user_id": owner, "snapshot": snapshot, "finalize": event}


def test_roundtrip_bound_to_run_and_private_permissions(tmp_path):
    store = ExportSnapshotStore(tmp_path / ".snapshots")
    data = record(); run = data["snapshot"]["run_id"]
    store.write(data)
    assert store.read(run) == data
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((store.root / f"{run}.json").stat().st_mode) == 0o600
    assert store.read(str(uuid4())) is None


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode", "corrupt", "oversize", "wrong_run"])
def test_unsafe_or_corrupt_snapshots_are_not_restored(tmp_path, kind):
    store = ExportSnapshotStore(tmp_path / ".snapshots")
    data = record(); run = data["snapshot"]["run_id"]; store.write(data)
    p = store.root / f"{run}.json"
    if kind == "symlink":
        external = tmp_path / "external"; p.rename(external); p.symlink_to(external)
    elif kind == "hardlink":
        (tmp_path / "other").hardlink_to(p)
    elif kind == "mode":p.chmod(0o644)
    elif kind == "corrupt":p.write_text("{")
    elif kind == "oversize":
        with p.open("wb") as f:f.truncate(5 * 1024 * 1024)
    else:
        data["snapshot"]["run_id"] = str(uuid4()); p.write_text(json.dumps(data))
    assert store.read(run) is None


def test_invalid_data_never_replaces_a_valid_snapshot(tmp_path):
    store = ExportSnapshotStore(tmp_path / ".snapshots")
    data = record(); run = data["snapshot"]["run_id"]; store.write(data)
    bad = copy.deepcopy(data);bad["snapshot"]["calc_inputs"] = {"x": float("nan")}
    with pytest.raises((ValueError, TypeError)):
        store.write(bad)
    assert store.read(run) == data
    assert [p.name for p in store.root.iterdir()] == [f"{run}.json"]


def test_store_refuses_symlink_root_and_traversal(tmp_path):
    external = tmp_path / "external"; external.mkdir(mode=0o700)
    link = tmp_path / "link"; link.symlink_to(external)
    store = ExportSnapshotStore(link)
    assert store.read(str(uuid4())) is None
    with pytest.raises((ValueError, OSError)):
        store.write(record())
    assert store.read("../outside") is None


@pytest.mark.parametrize("kind", ["private_payload", "bad_material", "bad_calculation", "bad_citations", "forged_formats"])
def test_nested_payloads_fail_closed(tmp_path, kind):
    data = record()
    if kind == "private_payload":data["finalize"]["payload"]["internal_prompt"] = "private"
    elif kind == "bad_material":
        data["snapshot"]["material_table"] = {"rows": [{}], "total_length_m": 1, "assumptions": []}
        data["finalize"]["payload"]["export_formats"] = ["docx", "xlsx"]
    elif kind == "bad_calculation":
        data["snapshot"]["calculations"] = [{"programmatic": False}]
        data["finalize"]["payload"]["calculations"] = data["snapshot"]["calculations"]
    elif kind == "bad_citations":
        data["snapshot"]["citations"] = [1]
        data["finalize"]["payload"]["citations"] = [1]
    else:data["finalize"]["payload"]["export_formats"] = ["docx", "xlsx"]
    with pytest.raises(ValueError):
        ExportSnapshotStore(tmp_path / ".snapshots").write(data)


def test_strings_are_sanitized_without_changing_numeric_values(tmp_path):
    data = record(); marker = "sk-" + "a" * 40
    data["snapshot"]["material_table"] = {
        "rows": [{"material": marker, "spec": "DN150", "quantity": 12.345, "raw_quantity": 12.345, "segments": [marker], "detail": marker}],
        "total_length_m": 12.345, "assumptions": [marker],
    }
    data["finalize"]["payload"]["export_formats"] = ["docx", "xlsx"]
    store = ExportSnapshotStore(tmp_path / ".snapshots");store.write(data)
    restored = store.read(data["snapshot"]["run_id"])
    assert marker not in json.dumps(restored)
    assert marker not in (store.root / f"{data['snapshot']['run_id']}.json").read_text()
    assert restored["snapshot"]["material_table"]["rows"][0]["quantity"] == 12.345

@pytest.mark.parametrize('value',[0,1])
def test_final_event_cannot_replace_numeric_result_with_bool(tmp_path,value):
    data=record()
    calc={'tool_name':'calc_pump_head','programmatic':True,'values':{'pump_head_m':value},'units':{'pump_head_m':'m'},'assumptions':[]}
    data['snapshot']['calculations']=[calc]
    data['finalize']['payload']['calculations']=copy.deepcopy([calc])
    data['finalize']['payload']['calculations'][0]['values']['pump_head_m']=bool(value)
    with pytest.raises(ValueError):ExportSnapshotStore(tmp_path/'.snapshots').write(data)
