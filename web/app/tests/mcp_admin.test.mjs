// P7-S6-A09：设置页的 MCP 扩展区（白名单编辑、凭据只写、探测、恢复、导入预览、读接口 403 时不渲染）。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { McpAdminApi } from "../src/mcpAdminApi.ts";
import {
  MCP_ADMIN_NOTICE,
  PROPOSAL_FILENAME,
  codeLabel,
  layerLabel,
  mcpAdminErrorMessage,
  probeSummary,
  sameAllow,
  serverStatusMessage,
  serverTags,
  serverTone,
  sourceSummary,
  toggleAllowItem,
  toolChoices,
  validateToolName,
} from "../src/mcpAdminState.ts";
import { WorkspaceApiError } from "../src/workspaceApi.ts";

function server(overrides = {}) {
  return {
    server_id: "cad",
    disabled: false,
    allow: ["read_dwg"],
    human_confirmation: false,
    lifecycle: "per_run",
    command_name: "python",
    sources: { command: "overlay", disabled: "overlay", allow: "web" },
    credentials: [],
    web_override: null,
    path_problems: [],
    last_probe: null,
    ...overrides,
  };
}

function probe(overrides = {}) {
  return {
    server_id: "cad",
    probed_at: "2026-10-07T09:00:00+00:00",
    status: "ok",
    server_code: null,
    tools: [
      { name: "read_dwg", exposed: true, code: null },
      { name: "write_dwg", exposed: false, code: "MCP_NOT_IN_ALLOWLIST" },
    ],
    truncated: false,
    ...overrides,
  };
}

/** 记录请求的假 fetch：按顺序回放给定的响应。 */
function recordingFetch(responses) {
  const calls = [];
  const queue = [...responses];
  const fetchImpl = async (input, init = {}) => {
    calls.push({ url: input, method: init.method ?? "GET", headers: new Headers(init.headers), body: init.body });
    const next = queue.shift() ?? { status: 200, body: { version: "v1", ok: true, data: {} } };
    return new Response(JSON.stringify(next.body), { status: next.status, headers: { "Content-Type": "application/json" } });
  };
  return { calls, fetchImpl };
}

test("allow editing keeps order, never duplicates, and treats null as deny-all", () => {
  assert.deepEqual(toggleAllowItem(null, "read_dwg", true), ["read_dwg"]);
  assert.deepEqual(toggleAllowItem(["a"], "b", true), ["a", "b"]);
  assert.deepEqual(toggleAllowItem(["a", "b"], "a", true), ["a", "b"]);
  assert.deepEqual(toggleAllowItem(["a", "b"], "a", false), ["b"]);
  assert.deepEqual(toggleAllowItem(null, "a", false), []);

  assert.equal(sameAllow(null, null), true);
  assert.equal(sameAllow(null, []), false, "全拒与空列表是两个状态：保存按钮要能区分");
  assert.equal(sameAllow(["a", "b"], ["a", "b"]), true);
  assert.equal(sameAllow(["a", "b"], ["b", "a"]), false);

  // 与后端同规则：不含空白、1–128 个字符
  assert.equal(validateToolName("read_dwg"), null);
  assert.match(validateToolName(""), /填写/);
  assert.match(validateToolName("read dwg"), /空白/);
  assert.match(validateToolName("x".repeat(129)), /128/);
});

test("tool choices merge the last probe with the allow list and flag what the probe did not see", () => {
  const rows = toolChoices(["read_dwg", "manual_tool"], probe());
  assert.deepEqual(
    rows.map((row) => [row.name, row.allowed, row.seen, row.code]),
    [
      ["read_dwg", true, true, null],
      ["write_dwg", false, true, "MCP_NOT_IN_ALLOWLIST"],
      ["manual_tool", true, false, null],
    ],
  );
  // 没探测过：白名单里的工具 seen = null（未知），不是"没看到"
  assert.deepEqual(toolChoices(["read_dwg"], null).map((row) => row.seen), [null]);
  assert.deepEqual(toolChoices(null, null), []);

  assert.match(probeSummary(null), /刷新工具清单/);
  assert.match(probeSummary(probe()), /2 个工具.*暴露 1 个/);
  assert.match(probeSummary(probe({ status: "failed", server_code: "MCP_CONNECT_FAILED", tools: [] })), /连接失败/);
  assert.equal(codeLabel(null), "已暴露");
  assert.equal(codeLabel("MCP_NOT_IN_ALLOWLIST"), "未放行");
  assert.equal(codeLabel("SOMETHING_NEW"), "SOMETHING_NEW");
});

test("server status explains what will happen on the next run", () => {
  assert.equal(serverTone(server()), "succeeded");
  assert.match(serverStatusMessage(server()), /放行 1 个工具/);
  assert.match(serverStatusMessage(server({ allow: null })), /全拒/);
  assert.equal(serverTone(server({ disabled: true })), "not_configured");
  assert.match(serverStatusMessage(server({ disabled: true })), /停用/);
  const missing = server({ credentials: [{ name: "CAD_API_TOKEN", configured: false, updated_at: null }] });
  assert.equal(serverTone(missing), "not_configured");
  assert.match(serverStatusMessage(missing), /CAD_API_TOKEN/);
  const broken = server({ path_problems: ["cwd"] });
  assert.equal(serverTone(broken), "failed");
  assert.match(serverStatusMessage(broken), /cwd/);

  assert.deepEqual(serverTags(server({ human_confirmation: true, lifecycle: "shared", web_override: { disabled: true } })), [
    "每次执行需人工确认",
    "共享进程",
    "Web 管理层已改动",
  ]);
  assert.equal(layerLabel("overlay"), "部署覆盖层");
  assert.equal(layerLabel("web"), "Web 管理层");
  assert.equal(layerLabel(undefined), "缺省值");
  assert.match(sourceSummary(server()), /启停来自部署覆盖层，白名单来自Web 管理层/);
  assert.match(MCP_ADMIN_NOTICE, /下一个 run 生效/);
  assert.match(MCP_ADMIN_NOTICE, /只能由运维在部署覆盖层修改/);
  assert.match(mcpAdminErrorMessage("WEB_MCP_REVISION_CONFLICT", "x"), /重新读取/);
  assert.equal(mcpAdminErrorMessage("UNKNOWN", "服务端原文"), "服务端原文");
  assert.equal(PROPOSAL_FILENAME, "mcp_overlay_proposal.json");
});

test("api sends only the editable fields, with CSRF on writes, and surfaces error codes", async () => {
  globalThis.document = { cookie: "wa_csrf=csrf-token-1" };
  try {
    const { calls, fetchImpl } = recordingFetch([
      { status: 200, body: { version: "v1", ok: true, data: { server: server(), revision: 1 } } },
      { status: 200, body: { version: "v1", ok: true, data: { revision: 2 } } },
      { status: 200, body: { version: "v1", ok: true, data: { credential: { name: "CAD_API_TOKEN", configured: true, updated_at: null } } } },
      { status: 409, body: { version: "v1", ok: false, error: { code: "WEB_MCP_REVISION_CONFLICT", message: "m" } } },
      { status: 403, body: { version: "v1", ok: false, error: { code: "AUTH_FORBIDDEN", message: "Admin access required" } } },
    ]);
    const api = new McpAdminApi(fetchImpl);

    await api.updateServer("cad", { allow: null, expected_revision: 0, command: "/bin/sh" });
    assert.equal(calls[0].method, "PATCH");
    assert.equal(calls[0].url, "/api/admin/mcp/servers/cad");
    assert.deepEqual(JSON.parse(calls[0].body), { expected_revision: 0, allow: null }, "多余的键不会被带上去");
    assert.equal(calls[0].headers.get("X-CSRF-Token"), "csrf-token-1");

    await api.removeOverrides("cad", 1);
    assert.equal(calls[1].url, "/api/admin/mcp/servers/cad/overrides?expected_revision=1");
    assert.equal(calls[1].method, "DELETE");

    await api.putCredential("cad", "CAD_API_TOKEN", "slot-value-123456");
    assert.equal(calls[2].url, "/api/admin/mcp/servers/cad/credentials/CAD_API_TOKEN");
    assert.deepEqual(JSON.parse(calls[2].body), { value: "slot-value-123456" });

    await assert.rejects(api.probe("cad"), (error) => {
      assert.ok(error instanceof WorkspaceApiError);
      assert.equal(error.code, "WEB_MCP_REVISION_CONFLICT");
      assert.equal(error.status, 409);
      return true;
    });
    await assert.rejects(api.listServers(), (error) => error.status === 403);
    assert.equal(calls[4].method, "GET");
    assert.equal(calls[4].headers.get("X-CSRF-Token"), null, "读接口不带 CSRF");
  } finally {
    delete globalThis.document;
  }
});

test("panel is admin-only, write-only for credentials, and states what it cannot change", async () => {
  const panel = await readFile(new URL("../src/McpAdminPanel.tsx", import.meta.url), "utf8");
  const api = await readFile(new URL("../src/mcpAdminApi.ts", import.meta.url), "utf8");
  const settings = await readFile(new URL("../src/SettingsPanel.tsx", import.meta.url), "utf8");
  const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

  // 读接口 403（非管理员）时整块不渲染
  assert.match(panel, /caught\.status === 403/);
  assert.match(panel, /if \(hidden\) return null;/);
  assert.match(settings, /<McpAdminPanel \/>/);

  // 凭据只写：遮蔽输入、不进自动填充、提交后立刻清空
  assert.match(panel, /type="password"/);
  assert.match(panel, /autoComplete="off"/);
  assert.match(panel, /await api\.putCredential\(serverId, slot\.name, value\);\n\s+\/\/ [^\n]*\n\s+setValue\(""\);/);

  // 固定说明 + 复用既有组件；CSRF 只有一份实现
  assert.match(panel, /MCP_ADMIN_NOTICE/);
  assert.match(panel, /CapabilityStatus/);
  assert.match(panel, /from "\.\/workspaceInspector"/);
  assert.match(api, /import \{ csrfToken, WorkspaceApiError, type ApiEnvelope \} from "\.\/workspaceApi\.ts"/);
  assert.doesNotMatch(panel + api, /wa_csrf/);

  // 写接口的请求体类型只有这三个键：面板没有任何能写命令 / 参数 / 环境变量 / 工作目录的通道
  const patchType = api.slice(api.indexOf("export type McpServerPatch"), api.indexOf("};", api.indexOf("export type McpServerPatch")));
  assert.deepEqual(
    [...patchType.matchAll(/^\s+(\w+)\??:/gm)].map((match) => match[1]),
    ["disabled", "allow", "expected_revision"],
  );
  assert.doesNotMatch(panel, /updateServer\([^)]*command/);

  // 导入只预览：复制 / 下载，没有"应用"
  assert.match(panel, /api\.importPreview\(text\)/);
  assert.match(panel, /navigator\.clipboard\.writeText/);
  assert.doesNotMatch(panel, /应用提案|apply/i);

  // 样式只用 token，不写字面色值
  const block = styles.slice(styles.indexOf("/* ---------- MCP 扩展（P7-S6）"));
  const ownBlock = block.slice(0, block.indexOf("/* ----------", 10));
  assert.ok(ownBlock.length > 0);
  assert.doesNotMatch(ownBlock, /#[0-9a-fA-F]{3,8}\b|rgb\(/);
});
