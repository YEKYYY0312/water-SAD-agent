#!/usr/bin/env bash
# 统一验证入口：复用 PR 门禁，避免与 P2/mypy/baseline 规则漂移。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec bash "$ROOT/scripts/ci_gate.sh"
