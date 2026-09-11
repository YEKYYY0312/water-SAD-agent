"""Step33 narrow export adapter — model only gives output_file_id, real path assembled here."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from water_agent.multi_agent.contracts import (
    MULTI_APPROVAL_REQUIRED,
    ApprovalRecord,
    ArtifactEnvelope,
)
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
    staging = Path(staging_dir) / f"{file_id}.dxf"
    if not staging.exists():
        raise FileNotFoundError(f"staged file not found: {staging}")
    final_p = Path(final_dir) / f"{file_id}.dxf"
    final_p.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(staging), str(final_p))
    return str(final_p)
