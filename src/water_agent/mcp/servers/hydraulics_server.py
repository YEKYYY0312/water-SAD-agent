#!/usr/bin/env python3
"""水力计算 MCP Server（P4 Step 24）。

把 ``water_agent.tools.hydraulics`` 的 7 个公式包装为 MCP ``tools/call``。
协议面与 ``mcp/client.py`` 对齐：行分隔 JSON-RPC 2.0，支持：

- ``initialize`` → ``{protocolVersion, capabilities:{tools:{}}, serverInfo}``
- ``notifications/initialized``（无回包）
- ``tools/list``（一次性返回全部工具）
- ``tools/call``（``isError=true`` 承载业务失败，协议失败走 JSON-RPC error）

全部数值计算仍走本地程序化 ``hydraulics.run_calc``，缺参/非法/未知公式均
**不抛异常**，而是 ``content=[{type:"text", text: <说明>}], isError:true``。

用法（被网关 spawn，非人工直接调）：

    python -m water_agent.mcp.servers.hydraulics_server
"""
from __future__ import annotations

import json
import sys
from typing import Any

from water_agent.tools.hydraulics import FORMULA_SET, run_calc

SERVER_INFO: dict[str, str] = {
    "name": "water-agent-hydraulics",
    "version": "p4-step24-2026-09-09",
}

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "calc_velocity",
        "description": "计算满管流圆管平均流速（m/s），需 flow_lps 与 diameter_mm，scenario 可选 water_supply/drainage。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "flow_lps": {
                    "type": "number",
                    "minimum": 0,
                    "description": "设计流量 L/s",
                },
                "diameter_mm": {
                    "type": "number",
                    "minimum": 0,
                    "description": "管内径 mm",
                },
                "scenario": {
                    "type": "string",
                    "enum": ["water_supply", "drainage"],
                    "default": "water_supply",
                    "description": "场景：给水或排水",
                },
            },
            "required": ["flow_lps", "diameter_mm"],
        },
        "formula": "velocity",
    },
    {
        "name": "calc_head_loss_darcy_weisbach",
        "description": "Darcy-Weisbach 沿程水头损失，需 length_m/diameter_mm/velocity_mps/friction_factor。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "length_m": {"type": "number", "minimum": 0, "description": "管长 m"},
                "diameter_mm": {
                    "type": "number",
                    "minimum": 0,
                    "description": "管径 mm",
                },
                "velocity_mps": {
                    "type": "number",
                    "minimum": 0,
                    "description": "流速 m/s",
                },
                "friction_factor": {
                    "type": "number",
                    "minimum": 0,
                    "description": "摩阻系数 f",
                },
                "gravity_mps2": {
                    "type": "number",
                    "minimum": 0,
                    "description": "重力加速度 m/s²，默认 9.81",
                },
            },
            "required": [
                "length_m",
                "diameter_mm",
                "velocity_mps",
                "friction_factor",
            ],
        },
        "formula": "head_loss_darcy_weisbach",
    },
    {
        "name": "calc_head_loss_hazen_williams",
        "description": "Hazen-Williams 沿程水头损失，需 length_m/diameter_mm/flow_lps/hazen_williams_c。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "length_m": {"type": "number", "minimum": 0},
                "diameter_mm": {"type": "number", "minimum": 0},
                "flow_lps": {"type": "number", "minimum": 0},
                "hazen_williams_c": {
                    "type": "number",
                    "minimum": 0,
                    "description": "Hazen-Williams 系数 C",
                },
            },
            "required": [
                "length_m",
                "diameter_mm",
                "flow_lps",
                "hazen_williams_c",
            ],
        },
        "formula": "head_loss_hazen_williams",
    },
    {
        "name": "calc_head_loss_shevelev",
        "description": "舍维列夫沿程水头损失，需 length_m/diameter_mm/velocity_mps。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "length_m": {"type": "number", "minimum": 0},
                "diameter_mm": {"type": "number", "minimum": 0},
                "velocity_mps": {"type": "number", "minimum": 0},
            },
            "required": ["length_m", "diameter_mm", "velocity_mps"],
        },
        "formula": "head_loss_shevelev",
    },
    {
        "name": "calc_diameter",
        "description": "由流量与流速求管径，需 flow_lps 与 velocity_mps。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "flow_lps": {"type": "number", "minimum": 0},
                "velocity_mps": {"type": "number", "minimum": 0},
            },
            "required": ["flow_lps", "velocity_mps"],
        },
        "formula": "diameter",
    },
    {
        "name": "calc_slope_manning",
        "description": "曼宁公式求排水坡度，需 flow_lps/diameter_mm/fill_ratio/manning_n。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "flow_lps": {"type": "number", "minimum": 0},
                "diameter_mm": {"type": "number", "minimum": 0},
                "fill_ratio": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "description": "充满度 (0,1)",
                },
                "manning_n": {
                    "type": "number",
                    "minimum": 0,
                    "description": "曼宁粗糙系数",
                },
            },
            "required": ["flow_lps", "diameter_mm", "fill_ratio", "manning_n"],
        },
        "formula": "slope_manning",
    },
    {
        "name": "calc_pump_head",
        "description": "泵扬程 H=H_static+H_friction+H_local+H_residual，四项均必填（可为 0）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "static_head_m": {"type": "number", "minimum": 0},
                "friction_head_m": {"type": "number", "minimum": 0},
                "local_head_m": {"type": "number", "minimum": 0},
                "residual_head_m": {"type": "number", "minimum": 0},
            },
            "required": [
                "static_head_m",
                "friction_head_m",
                "local_head_m",
                "residual_head_m",
            ],
        },
        "formula": "pump_head",
    },
]

TOOL_MAP: dict[str, dict[str, Any]] = {t["name"]: t for t in TOOL_DEFS}


def _send(obj: dict[str, Any]) -> None:
    from water_agent.mcp.protocol import encode_message
    sys.stdout.write(encode_message(obj))
    sys.stdout.flush()


def _ok_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err_response(req_id: Any, code: int, msg: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": msg[:500]},
    }


def handle_tools_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(name, str) or name not in TOOL_MAP:
        return {
            "content": [{"type": "text", "text": f"未知工具 {name!r}"}],
            "isError": True,
        }
    if not isinstance(arguments, dict):
        return {
            "content": [{"type": "text", "text": "arguments 必须是 object"}],
            "isError": True,
        }
    formula = TOOL_MAP[name]["formula"]
    if formula not in FORMULA_SET:
        return {
            "content": [{"type": "text", "text": f"未知公式 {formula!r}"}],
            "isError": True,
        }
    result = run_calc(formula, dict(arguments))
    if not result.ok:
        detail = "; ".join(result.missing_inputs + result.errors) or "参数校验失败"
        return {"content": [{"type": "text", "text": detail}], "isError": True}
    text = json.dumps(
        {
            "formula": formula,
            "values": result.values,
            "assumptions": result.assumptions,
        },
        ensure_ascii=False,
    )
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {
            "formula": formula,
            "values": result.values,
            "assumptions": result.assumptions,
        },
        "isError": False,
    }


def main() -> int:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        req_id = msg.get("id")
        if method == "initialize":
            params = msg.get("params") or {}
            ver = params.get("protocolVersion") or "2025-06-18"
            _send(
                _ok_result(
                    req_id,
                    {
                        "protocolVersion": ver,
                        "capabilities": {"tools": {}},
                        "serverInfo": SERVER_INFO,
                    },
                )
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            tools = [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "inputSchema": t["inputSchema"],
                }
                for t in TOOL_DEFS
            ]
            _send(_ok_result(req_id, {"tools": tools}))
        elif method == "tools/call":
            params = msg.get("params") or {}
            result = handle_tools_call(params)
            _send(_ok_result(req_id, result))
        elif req_id is not None:
            _send(_err_response(req_id, -32601, f"不支持的方法 {method!r}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
