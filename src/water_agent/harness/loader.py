"""JSONL 评测集加载器。"""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from water_agent.domain.models import CalcCase, EvalCase


class EvalLoadError(ValueError):
    """评测集加载失败。"""


@dataclass(slots=True)
class EvalLoadIssue:
    line_no: int
    error: str
    raw: str = ""


@dataclass(slots=True)
class EvalLoadBundle:
    cases: list[EvalCase | CalcCase]
    issues: list[EvalLoadIssue]
    line_nos: list[int] = field(default_factory=list)  # 与 cases 逐位对齐的 JSONL 行号

    @property
    def has_issues(self) -> bool:
        return bool(self.issues)


def _parse_eval_case(payload: dict[str, Any], *, line_no: int) -> EvalCase | CalcCase:
    model = CalcCase if payload.get("type") == "calc" else EvalCase
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        message = first.get("msg", "invalid eval case")
        raise EvalLoadError(f"line {line_no}: {message}") from exc


def iter_eval_records(path: str | Path) -> Iterator[tuple[int, str]]:
    source = Path(path)
    with source.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            stripped = line.strip()
            if stripped:
                yield line_no, stripped


def load_eval_cases(path: str | Path, *, strict: bool = True) -> list[EvalCase | CalcCase]:
    bundle = load_eval_bundle(path, strict=strict)
    return bundle.cases


def load_eval_bundle(path: str | Path, *, strict: bool = True) -> EvalLoadBundle:
    cases: list[EvalCase | CalcCase] = []
    line_nos: list[int] = []
    issues: list[EvalLoadIssue] = []
    for line_no, raw_line in iter_eval_records(path):
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            issue = EvalLoadIssue(line_no=line_no, error=f"invalid json: {exc.msg}", raw=raw_line)
            if strict:
                raise EvalLoadError(f"line {line_no}: invalid json: {exc.msg}") from exc
            issues.append(issue)
            continue
        if not isinstance(payload, dict):
            issue = EvalLoadIssue(line_no=line_no, error="record must be a JSON object", raw=raw_line)
            if strict:
                raise EvalLoadError(f"line {line_no}: record must be a JSON object")
            issues.append(issue)
            continue
        try:
            cases.append(_parse_eval_case(payload, line_no=line_no))
            line_nos.append(line_no)
        except EvalLoadError as exc:
            issue = EvalLoadIssue(line_no=line_no, error=str(exc), raw=raw_line)
            if strict:
                raise
            issues.append(issue)
    return EvalLoadBundle(cases=cases, issues=issues, line_nos=line_nos)
