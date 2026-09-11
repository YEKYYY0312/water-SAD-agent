"""Step33 narrow export adapter — model only gives output_file_id, real path assembled here."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from pydantic import ValidationError

from water_agent.multi_agent.contracts import (
    MULTI_APPROVAL_REQUIRED,
    MULTI_APPROVAL_STALE,
    ApprovalRecord,
    ArtifactEnvelope,
    DrawingPackage,
)
from water_agent.multi_agent.drawing_executor import compute_file_sha256
from water_agent.multi_agent.provenance import assert_approval_binding

_FILE_ID_RE = re.compile(r"^[a-z0-9_-]{1,64}$")


def assemble_export_path(
    *,
    file_id: str,
    staging_dir: str,
    final_dir: str,
    approval: ApprovalRecord | None = None,
    artifacts: dict[str, ArtifactEnvelope] | list[ArtifactEnvelope] | None = None,
) -> str:
    """Map file_id to real paths — no model-provided path accepted."""
    if not _FILE_ID_RE.match(file_id):
        raise ValueError(f"file_id must match {_FILE_ID_RE.pattern!r}")
    if approval is None:
        raise ValueError(f"{MULTI_APPROVAL_REQUIRED}: final export requires an approval record")
    if artifacts is None:
        raise ValueError("artifacts are required when approval is supplied")
    if not approval.approved:
        raise ValueError(f"{MULTI_APPROVAL_REQUIRED}: approval record is not approved")
    assert_approval_binding(approval, artifacts)

    collection = list(artifacts.values()) if isinstance(artifacts, dict) else list(artifacts)
    drawing_envelopes = [
        envelope for envelope in collection if envelope.artifact_type == "drawing_package"
    ]
    if len(drawing_envelopes) != 1:
        raise ValueError(
            f"{MULTI_APPROVAL_STALE}: release manifest must contain one drawing package"
        )
    try:
        drawing_package = DrawingPackage.model_validate(drawing_envelopes[0].payload)
    except ValidationError as exc:
        raise ValueError(
            f"{MULTI_APPROVAL_STALE}: drawing package content binding is invalid"
        ) from exc
    if (
        not drawing_package.staged_file_verified
        or drawing_package.staged_file_id != file_id
        or drawing_package.staged_content_sha256 is None
    ):
        raise ValueError(
            f"{MULTI_APPROVAL_STALE}: drawing package lacks verified staged content"
        )

    staging = Path(staging_dir) / f"{file_id}.dxf"
    if not staging.exists():
        raise FileNotFoundError(f"staged file not found: {staging}")
    expected_sha256 = drawing_package.staged_content_sha256
    if compute_file_sha256(staging) != expected_sha256:
        raise ValueError(f"{MULTI_APPROVAL_STALE}: staged DXF content hash mismatch")

    final_p = Path(final_dir) / f"{file_id}.dxf"
    final_p.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{file_id}-",
        suffix=".tmp",
        dir=final_p.parent,
        delete=False,
    ) as temp_file:
        temp_path = Path(temp_file.name)
    try:
        shutil.copy2(str(staging), str(temp_path))
        if compute_file_sha256(temp_path) != expected_sha256:
            raise ValueError(f"{MULTI_APPROVAL_STALE}: copied DXF content hash mismatch")
        if compute_file_sha256(staging) != expected_sha256:
            raise ValueError(f"{MULTI_APPROVAL_STALE}: staged DXF changed during export")
        os.replace(temp_path, final_p)
        if compute_file_sha256(final_p) != expected_sha256:
            final_p.unlink(missing_ok=True)
            raise ValueError(f"{MULTI_APPROVAL_STALE}: final DXF content hash mismatch")
    finally:
        temp_path.unlink(missing_ok=True)
    return str(final_p)
