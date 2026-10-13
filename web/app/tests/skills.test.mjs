// P7-S9-A12：设置页的 Skill 库区（成员只读列表、管理员启停与查看正文、MCP 计数卡片）。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { SkillsApi } from "../src/skillsApi.ts";
import {
  SKILL_LIBRARY_NOTICE,
  canToggle,
  libraryStateMessage,
  mcpCountRows,
  skillErrorMessage,
  skillMeta,
  sourceLabel,
  statusLabel,
  statusTone,
  toggleLabel,
} from "../src/skillsState.ts";
import { WorkspaceApiError } from "../src/workspaceApi.ts";

function skill(overrides = {}) {
  return {
    name: "calc-book-workflow",
    source: "builtin",
    version: "1.0.0",
    description: "编写计算书时的步骤。",
    triggers: ["计算书"],
    requires_tools: ["calc_velocity", "retrieve_clauses"],
    status: "enabled",
    ...overrides,
  };
}

function listing(overrides = {}) {
  return {
    active: true,
    can_manage: false,
    notice: "n",
    revision: 0,
    web_layer_error: null,
    skills: [skill(), skill({ name: "leak-test-notes", source: "operator", status: "SKILL_DISABLED" })],
    unavailable: [{ name: "unsafe-override", status: "SKILL_UNSAFE" }],
    ...overrides,
  };
}

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

test("status, source and library state read as plain sentences", () => {
  assert.equal(statusLabel("enabled"), "已启用");
  assert.equal(statusLabel("SKILL_DISABLED"), "管理员已停用");
  assert.match(statusLabel("SKILL_UNSAFE"), /安检/);
  assert.match(statusLabel("SKILL_CONFLICTS_IRON_RULE"), /铁律/);
  assert.match(statusLabel("SKILL_SHADOWED"), /遮蔽/);
  assert.equal(statusLabel("SKILL_SOMETHING_NEW"), "SKILL_SOMETHING_NEW");
  assert.equal(statusTone("enabled"), "succeeded");
  assert.equal(statusTone("SKILL_DISABLED"), "not_configured");
  assert.equal(statusTone("SKILL_UNSAFE"), "failed");
  assert.equal(sourceLabel("builtin"), "内置");
  assert.equal(sourceLabel("operator"), "运维目录");

  assert.equal(libraryStateMessage(listing()), "已启用 1 个，共 2 个可用");
  assert.match(libraryStateMessage(listing({ active: false })), /WATER_AGENT_SKILLS=off/);
  assert.match(libraryStateMessage(listing({ web_layer_error: "MCP_CONFIG_INVALID" })), /MCP_CONFIG_INVALID/);

  assert.equal(toggleLabel(skill()), "停用");
  assert.equal(toggleLabel(skill({ status: "SKILL_DISABLED" })), "启用");
  assert.deepEqual(skillMeta(skill()).map((row) => row.label), ["来源", "触发词", "依赖工具"]);
  assert.deepEqual(skillMeta(skill({ triggers: [], requires_tools: [] })).map((row) => row.label), ["来源"]);

  assert.match(SKILL_LIBRARY_NOTICE, /下一个 run 生效/);
  assert.match(SKILL_LIBRARY_NOTICE, /面板只能启停/);
  assert.match(skillErrorMessage("WEB_MCP_REVISION_CONFLICT", "x"), /重新读取/);
  assert.equal(skillErrorMessage("UNKNOWN", "服务端原文"), "服务端原文");
});

test("only admins with a writable, healthy web layer can toggle", () => {
  assert.equal(canToggle(listing()), false, "成员只读");
  assert.equal(canToggle(listing({ can_manage: true })), true);
  assert.equal(canToggle(listing({ can_manage: true, revision: null })), false, "没配 Web 管理层");
  assert.equal(canToggle(listing({ can_manage: true, web_layer_error: "MCP_CONFIG_INVALID" })), false);
});

test("mcp counts come from the capabilities endpoint and unknown is not zero", () => {
  const rows = mcpCountRows({
    status: "not_configured",
    message: "m",
    configured_server_count: 3,
    enabled_server_count: 2,
    disabled_server_count: 1,
    confirmation_server_count: 0,
    shared_server_count: null,
    real_endpoints_verified: false,
  });
  assert.deepEqual(rows.map((row) => [row.label, row.value]), [
    ["已配置", "3"],
    ["启用", "2"],
    ["停用", "1"],
    ["需人工确认", "0"],
    ["共享进程", "未知"],
  ]);
  assert.ok(mcpCountRows(undefined).every((row) => row.value === "未知"));
});

test("api sends only disabled + expected_revision, with CSRF on writes, and surfaces error codes", async () => {
  globalThis.document = { cookie: "wa_csrf=csrf-token-9" };
  try {
    const { calls, fetchImpl } = recordingFetch([
      { status: 200, body: { version: "v1", ok: true, data: listing() } },
      { status: 200, body: { version: "v1", ok: true, data: { name: "calc-book-workflow", disabled: true, revision: 1 } } },
      { status: 200, body: { version: "v1", ok: true, data: { skill: { ...skill(), body: "正文", digest: "0123456789abcdef" } } } },
      { status: 409, body: { version: "v1", ok: false, error: { code: "WEB_MCP_REVISION_CONFLICT", message: "m" } } },
      { status: 403, body: { version: "v1", ok: false, error: { code: "AUTH_FORBIDDEN", message: "Admin access required" } } },
    ]);
    const api = new SkillsApi(fetchImpl);

    const data = await api.listSkills();
    assert.equal(calls[0].url, "/api/skills");
    assert.equal(calls[0].headers.get("X-CSRF-Token"), null, "读接口不带 CSRF");
    assert.equal(data.skills.length, 2);

    await api.setDisabled("calc-book-workflow", { disabled: true, expected_revision: 0, body: "改写", source: "operator" });
    assert.equal(calls[1].method, "PATCH");
    assert.equal(calls[1].url, "/api/admin/skills/calc-book-workflow");
    assert.deepEqual(JSON.parse(calls[1].body), { disabled: true, expected_revision: 0 }, "多余的键不会被带上去");
    assert.equal(calls[1].headers.get("X-CSRF-Token"), "csrf-token-9");

    const detail = await api.getSkill("calc-book-workflow");
    assert.equal(calls[2].url, "/api/admin/skills/calc-book-workflow");
    assert.equal(detail.skill.body, "正文");

    await assert.rejects(api.setDisabled("x", { disabled: false, expected_revision: 0 }), (error) => {
      assert.ok(error instanceof WorkspaceApiError);
      assert.equal(error.code, "WEB_MCP_REVISION_CONFLICT");
      assert.equal(error.status, 409);
      return true;
    });
    await assert.rejects(api.getSkill("calc-book-workflow"), (error) => error.status === 403);
  } finally {
    delete globalThis.document;
  }
});

test("panel shows the list to everyone, admin controls only to admins, and no way to edit a skill", async () => {
  const panel = await readFile(new URL("../src/SkillLibraryPanel.tsx", import.meta.url), "utf8");
  const api = await readFile(new URL("../src/skillsApi.ts", import.meta.url), "utf8");
  const settings = await readFile(new URL("../src/SettingsPanel.tsx", import.meta.url), "utf8");
  const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

  assert.match(settings, /<SkillLibraryPanel capabilities=\{capabilities\} onOpenMcp=\{\(\) => setRequested\("mcp"\)\} \/>/);
  // Skill 库与 MCP 扩展是并列的子页；计数卡片的"查看 MCP 扩展"切到 MCP 子页，不再滚动
  assert.ok(settings.indexOf("<SkillLibraryPanel") < settings.indexOf("<McpAdminPanel"));
  assert.doesNotMatch(panel, /scrollIntoView/);

  // 启停与查看正文只在 can_manage 时渲染；正文按纯文本显示
  assert.match(panel, /\{listing\.can_manage && \(\n\s+<div className="dialog-actions mcp-actions">/);
  assert.match(panel, /<pre className="skill-body">\{detail\.body\}<\/pre>/);
  assert.doesNotMatch(panel, /dangerouslySetInnerHTML|AnswerMarkdown/);

  // 写接口请求体只有两个键：面板没有任何能改正文 / 来源 / 目录的通道
  const patchType = api.slice(api.indexOf("export type SkillPatch"), api.indexOf("};", api.indexOf("export type SkillPatch")));
  assert.deepEqual([...patchType.matchAll(/(\w+):/g)].map((match) => match[1]), ["disabled", "expected_revision"]);
  assert.doesNotMatch(api, /method: "(POST|PUT|DELETE)"/, "不能新增、上传或删除 skill");

  // 未加载的 skill 只显示名字与状态码，不显示描述
  const unavailable = panel.slice(panel.indexOf("listing.unavailable.map"), panel.indexOf("</ul>", panel.indexOf("listing.unavailable.map")));
  assert.doesNotMatch(unavailable, /description/);

  // MCP 只显示计数，不调任何 MCP 列表接口
  assert.match(panel, /mcpCountRows\(capabilities\?\.mcp\)/);
  assert.doesNotMatch(panel + api, /\/api\/admin\/mcp/);

  // 复用既有组件；CSRF 只有一份实现
  assert.match(panel, /CapabilityStatus/);
  assert.match(api, /import \{ csrfToken, WorkspaceApiError, type ApiEnvelope \} from "\.\/workspaceApi\.ts"/);
  assert.doesNotMatch(panel + api, /wa_csrf/);

  // 样式只用 token，不写字面色值
  const block = styles.slice(styles.indexOf("/* ---------- Skill 库（P7-S9）"));
  const ownBlock = block.slice(0, block.indexOf("/* ----------", 10));
  assert.ok(ownBlock.length > 0);
  assert.doesNotMatch(ownBlock, /#[0-9a-fA-F]{3,8}\b|rgb\(/);
});
