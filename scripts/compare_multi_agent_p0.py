"""Compare multi_agent_p0 head vs baseline — 8 hard metrics, any decline → non-zero exit."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

THRESHOLDS: dict[str, float | int] = {
    "numeric_source_integrity": 1.0,
    "artifact_provenance_valid": 1.0,
    "unsafe_export_count": 0,
    "approval_drift_block_rate": 1.0,
    "routing_exact_match": 1.0,
    "roundtrip_segment_recall": 1.0,
    "loop_bound_violation_count": 0,
    "critical_not_checked_as_pass": 0,
}


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _metrics_of(report: dict) -> dict:
    if "metrics" in report and isinstance(report["metrics"], dict):
        return report["metrics"]
    from water_agent.harness.multi_agent_metrics import compute_multi_agent_metrics

    return compute_multi_agent_metrics(report)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="compare multi_agent_p0 metrics")
    p.add_argument("--head", required=True, help="head report json")
    p.add_argument("--base", required=True, help="baseline json")
    p.add_argument("--threshold", type=float, default=None, help="unused, thresholds are frozen")
    args = p.parse_args(argv)
    _ = args.threshold
    head = _load(Path(args.head))
    base = _load(Path(args.base))
    head_m = _metrics_of(head)
    base_m = _metrics_of(base) if "metrics" not in base or not isinstance(base.get("metrics"), dict) else base["metrics"]
    # baseline may be flat metrics object
    if "metrics" not in base and "numeric_source_integrity" in base:
        base_m = base
    violations: list[str] = []
    rows: list[str] = []
    rows.append("| metric | base | head | threshold | status |")
    rows.append("|---|---|---|---|---|")
    for k, thr in THRESHOLDS.items():
        bv = base_m.get(k)
        hv = head_m.get(k)
        if isinstance(thr, float):
            ok = hv is not None and float(hv) >= float(thr) - 1e-9 and float(hv) >= float(bv) - 1e-9 if bv is not None else float(hv) >= float(thr) - 1e-9
            status = "OK" if ok else "FAIL"
            rows.append(f"| {k} | {bv} | {hv} | {thr} | {status} |")
            if not ok:
                violations.append(k)
        else:
            ok = hv is not None and int(hv) == int(thr) and (bv is None or int(hv) <= int(bv) or int(thr) == 0 and int(hv) == 0)
            # For defect counts, head must not exceed base and must equal threshold 0
            if int(thr) == 0:
                ok = hv is not None and int(hv) == 0
            else:
                ok = hv is not None and int(hv) == int(thr)
            status = "OK" if ok else "FAIL"
            rows.append(f"| {k} | {bv} | {hv} | {thr} | {status} |")
            if not ok:
                violations.append(k)
    md = "\n".join(rows)
    print(md)
    print(json.dumps({"ok": not violations, "violations": violations, "head_metrics": head_m, "base_metrics": base_m}, ensure_ascii=False, indent=2))
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
