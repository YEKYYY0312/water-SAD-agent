"""Opt-in local OpenAI-compatible model configuration (P5 Step 46).

The default CLI/Web paths remain DeepSeek or mock. A local endpoint is usable
only when the operator enables it and points at a compatibility report whose
seven required checks passed for the exact endpoint/model and stayed below the
existing 4500-token tool-schema ceiling.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

LOCAL_MODEL_ENABLED = "LOCAL_MODEL_ENABLED"
LOCAL_MODEL_BASE_URL = "LOCAL_MODEL_BASE_URL"
LOCAL_MODEL_NAME = "LOCAL_MODEL_NAME"
LOCAL_MODEL_COMPATIBILITY_REPORT = "LOCAL_MODEL_COMPATIBILITY_REPORT"
LOCAL_MODEL_DEFAULT_BASE_URL = "http://local-model:11434/v1"
LOCAL_MODEL_DEFAULT_NAME = "qwen3:8b"
LOCAL_MODEL_DEFAULT_API_KEY = "ollama"
MAX_TOOL_SCHEMA_TOKENS = 4500

COMPATIBILITY_CHECKS = (
    "function_calling",
    "structured_citations",
    "programmatic_calculation",
    "memory_p0",
    "cancellation_timeout",
    "error_handling",
    "tool_schema_budget",
)


class LocalModelCompatibilityReport(BaseModel):
    """Machine-readable evidence; this is not an assertion that tests ran."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str = Field(min_length=1)
    status: Literal["compatible", "blocked", "incomplete"]
    model: str = Field(min_length=1)
    endpoint: str = Field(min_length=1)
    checks: dict[str, Literal["passed", "failed", "blocked", "skipped"]]
    schema_tokens: int = Field(ge=0)
    evidence: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_compatible(self) -> bool:
        return (
            self.status == "compatible"
            and set(self.checks) == set(COMPATIBILITY_CHECKS)
            and all(self.checks[name] == "passed" for name in COMPATIBILITY_CHECKS)
            and self.schema_tokens < MAX_TOOL_SCHEMA_TOKENS
            and self.evidence.get("probe_version") == self.version
        )


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def local_model_configured(environment=os.getenv) -> bool:
    """Return whether the operator explicitly selected a local endpoint."""

    return bool(
        _truthy(environment(LOCAL_MODEL_ENABLED))
        and (environment(LOCAL_MODEL_BASE_URL) or LOCAL_MODEL_DEFAULT_BASE_URL).strip()
        and (environment(LOCAL_MODEL_NAME) or LOCAL_MODEL_DEFAULT_NAME).strip()
    )


def _report_path(environment=os.getenv) -> Path | None:
    raw = environment(LOCAL_MODEL_COMPATIBILITY_REPORT)
    return Path(raw).expanduser() if raw and raw.strip() else None


def read_compatibility_report(path: str | Path) -> LocalModelCompatibilityReport | None:
    """Read evidence without printing its contents or treating bad data as proof."""

    try:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        return LocalModelCompatibilityReport.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError, TypeError, ValueError):
        return None


def local_model_ready(environment=os.getenv) -> bool:
    """Require opt-in plus exact, complete, passing compatibility evidence."""

    if not local_model_configured(environment):
        return False
    report_path = _report_path(environment)
    report = read_compatibility_report(report_path) if report_path is not None else None
    if report is None or not report.is_compatible:
        return False
    endpoint = (environment(LOCAL_MODEL_BASE_URL) or LOCAL_MODEL_DEFAULT_BASE_URL).rstrip("/")
    model = environment(LOCAL_MODEL_NAME) or LOCAL_MODEL_DEFAULT_NAME
    return report.endpoint.rstrip("/") == endpoint and report.model == model


def local_model_settings(environment=os.getenv) -> dict[str, Any]:
    """Return non-secret local model settings for the shared Agent assembly."""

    return {
        "model": environment(LOCAL_MODEL_NAME) or LOCAL_MODEL_DEFAULT_NAME,
        "base_url": environment(LOCAL_MODEL_BASE_URL) or LOCAL_MODEL_DEFAULT_BASE_URL,
        "api_key": LOCAL_MODEL_DEFAULT_API_KEY,
    }


def build_local_chat_model(**kwargs: Any):
    """Build the shared governed chat model only after the evidence gate."""

    from water_agent.generation.chat_model import build_chat_model

    if not local_model_ready():
        raise ValueError(
            "local model is not enabled with complete compatibility evidence; "
            "run the Step 46 probe before selecting it"
        )
    settings = local_model_settings()
    settings.update(kwargs)
    settings.setdefault("api_key", LOCAL_MODEL_DEFAULT_API_KEY)
    return build_chat_model(**settings)


__all__ = [
    "COMPATIBILITY_CHECKS",
    "LOCAL_MODEL_BASE_URL",
    "LOCAL_MODEL_COMPATIBILITY_REPORT",
    "LOCAL_MODEL_DEFAULT_API_KEY",
    "LOCAL_MODEL_DEFAULT_BASE_URL",
    "LOCAL_MODEL_DEFAULT_NAME",
    "LOCAL_MODEL_ENABLED",
    "LOCAL_MODEL_NAME",
    "MAX_TOOL_SCHEMA_TOKENS",
    "LocalModelCompatibilityReport",
    "build_local_chat_model",
    "local_model_configured",
    "local_model_ready",
    "local_model_settings",
    "read_compatibility_report",
]
