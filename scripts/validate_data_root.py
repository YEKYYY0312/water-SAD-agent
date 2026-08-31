"""DATA_ROOT 验证入口脚本（P1 增量 2）。

用法
----
_venv/bin/python scripts/validate_data_root.py
_venv/bin/python scripts/validate_data_root.py --strict
_venv/bin/python scripts/validate_data_root.py --data-root /explicit/path

退出码
-------
0  数据有效，或 report-only 模式完成
1  strict 数据质量失败
2  配置、路径或命令参数错误
"""
from __future__ import annotations

import sys

from water_agent.ingestion.quality_cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
