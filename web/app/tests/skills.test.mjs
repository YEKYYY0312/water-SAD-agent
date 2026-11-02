// P7-S9-A12：设置页的 Skill 库区（成员只读列表、管理员启停与查看正文、MCP 计数卡片）。
// P7-S13：第三个来源 installed 只展示出处；取回 / 批准 / 安装没有任何 Web 入口（入口封闭断言）。
import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";

import { SkillsApi, normalizeSkill, parseInstalledMeta } from "../src/skillsApi.ts";
import { formatUpdatedAt } from "../src/settingsState.ts";
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

const INSTALLED_META = {
  sha8: "ab12cd34",
  approved_by: "ops",
  approved_at: "2026-10-22T03:00:00Z",
  registry_host: "registry.example.com",
};

function installedSkill(overrides = {}) {
  return skill({ name: "fetched-flow", source: "installed", installed: { ...INSTALLED_META }, ...overrides });
}

test("P7-S13 source label for installed skills, existing labels unchanged", () => {
  assert.equal(sourceLabel("installed"), "已安装");
  assert.equal(sourceLabel("builtin"), "内置");
  assert.equal(sourceLabel("operator"), "运维目录");
  assert.equal(sourceLabel("something-else"), "something-else", "未登记的来源原样显示");
});

test("P7-S13 installed detail row carries digest prefix, approver, approval time and registry host", () => {
  const rows = skillMeta(installedSkill());
  assert.deepEqual(rows.map((row) => row.label), ["来源", "安装信息", "触发词", "依赖工具"]);
  assert.equal(rows[0].value, "已安装 · v1.0.0");

  const detail = rows[1].value;
  assert.match(detail, /ab12cd34/);
  assert.match(detail, /ops/);
  assert.match(detail, /registry\.example\.com/);
  // 批准时间沿用设置页既有的本地时间格式化（formatUpdatedAt）
  assert.ok(detail.includes(formatUpdatedAt(INSTALLED_META.approved_at)), detail);

  // 摘要只留前 8 位
  const long = skillMeta(installedSkill({ installed: { ...INSTALLED_META, sha8: "ab12cd34ef567890" } }));
  assert.match(long[1].value, /ab12cd34/);
  assert.doesNotMatch(long[1].value, /ef56/);
});

test("P7-S13 builtin and operator detail rows stay exactly as before", () => {
  assert.deepEqual(skillMeta(skill()), [
    { label: "来源", value: "内置 · v1.0.0" },
    { label: "触发词", value: "计算书" },
    { label: "依赖工具", value: "calc_velocity、retrieve_clauses" },
  ]);
  assert.deepEqual(skillMeta(skill({ source: "operator", triggers: [], requires_tools: [] })), [
    { label: "来源", value: "运维目录 · v1.0.0" },
  ]);
  // 非 installed 来源即使带了多余的 installed 键也不渲染安装信息行
  const stray = skillMeta(skill({ installed: { ...INSTALLED_META } }));
  assert.deepEqual(stray.map((row) => row.label), ["来源", "触发词", "依赖工具"]);
});

test("P7-S13 missing or malformed installed metadata neither throws nor renders the row", () => {
  const malformed = [
    undefined,
    null,
    "ab12cd34",
    42,
    [],
    {},
    { sha8: "ab12cd34" },
    { ...INSTALLED_META, registry_host: undefined },
    { ...INSTALLED_META, approved_by: "" },
    { ...INSTALLED_META, approved_at: "   " },
    { ...INSTALLED_META, sha8: 12345678 },
    { ...INSTALLED_META, approved_by: { name: "ops" } },
  ];
  for (const installed of malformed) {
    assert.equal(parseInstalledMeta(installed), null, JSON.stringify(installed));
    const rows = skillMeta(installedSkill({ installed }));
    assert.deepEqual(rows.map((row) => row.label), ["来源", "触发词", "依赖工具"], JSON.stringify(installed));
    assert.equal(rows[0].value, "已安装 · v1.0.0");
  }
  assert.doesNotThrow(() => {
    const bare = installedSkill();
    delete bare.installed;
    skillMeta(bare);
  });
  // 无法解析的时间原样显示，不抛
  const odd = skillMeta(installedSkill({ installed: { ...INSTALLED_META, approved_at: "昨天下午" } }));
  assert.match(odd[1].value, /昨天下午/);
});

test("P7-S13 api normalizes installed metadata and leaves other sources untouched", async () => {
  const builtinWithStrayKey = skill({ name: "stray-key", installed: { junk: true } });
  const { fetchImpl } = recordingFetch([
    {
      status: 200,
      body: {
        version: "v1",
        ok: true,
        data: listing({
          skills: [
            skill(),
            installedSkill(),
            installedSkill({ name: "bad-meta", installed: { sha8: "ab12cd34" } }),
            installedSkill({ name: "no-meta", installed: undefined }),
            builtinWithStrayKey,
          ],
        }),
      },
    },
    {
      status: 200,
      body: { version: "v1", ok: true, data: { skill: { ...installedSkill(), body: "正文", digest: "0123456789abcdef" } } },
    },
    {
      status: 200,
      body: { version: "v1", ok: true, data: { skill: { ...installedSkill({ installed: "oops" }), body: "正文", digest: "0123456789abcdef" } } },
    },
  ]);
  const api = new SkillsApi(fetchImpl);

  const data = await api.listSkills();
  const byName = Object.fromEntries(data.skills.map((item) => [item.name, item]));
  assert.deepEqual(byName["fetched-flow"].installed, INSTALLED_META);
  assert.ok(!("installed" in byName["bad-meta"]), "畸形元数据被去掉");
  assert.ok(!("installed" in byName["no-meta"]));
  assert.deepEqual(byName["calc-book-workflow"], skill(), "builtin 对象逐字不变");
  assert.deepEqual(byName[builtinWithStrayKey.name], builtinWithStrayKey, "其他来源不动");

  const detail = await api.getSkill("fetched-flow");
  assert.deepEqual(detail.skill.installed, INSTALLED_META);
  assert.equal(detail.skill.body, "正文");
  const odd = await api.getSkill("fetched-flow");
  assert.ok(!("installed" in odd.skill));

  // 直接调用也宽容
  assert.equal(normalizeSkill(null), null);
  assert.deepEqual(normalizeSkill(skill()), skill());
});

// ---- 入口封闭：取回 / 批准 / 安装只在主机 shell 的 CLI，Web 前端不得出现任何对应入口 ----

const FORBIDDEN_ENTRY_SNIPPETS = [
  "/skills/fetch",
  "/skills/approve",
  "/skills/install",
  "/skills/candidate",
  "/skills/registry",
  "skill_registry",
  "approveSkill",
  "fetchSkill",
  "installSkill",
];

function forbiddenHits(text) {
  const lowered = text.toLowerCase();
  return FORBIDDEN_ENTRY_SNIPPETS.filter((snippet) => lowered.includes(snippet.toLowerCase()));
}

async function sourceFiles(dir) {
  const found = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = new URL(entry.name + (entry.isDirectory() ? "/" : ""), dir);
    if (entry.isDirectory()) found.push(...(await sourceFiles(path)));
    else if (/\.(ts|tsx)$/.test(entry.name)) found.push(path);
  }
  return found;
}

function buttonLabels(source) {
  const labels = [];
  for (const match of source.matchAll(/<button\b[^>]*>([\s\S]*?)<\/button>/g)) {
    const inner = match[1];
    for (const expression of inner.matchAll(/\{([^}]*)\}/g)) {
      const literals = [...expression[1].matchAll(/"([^"]*)"/g)].map((item) => item[1]);
      if (literals.length) labels.push(...literals);
      else labels.push(expression[1].trim().replace(/\(.*$/, ""));
    }
    const text = inner.replace(/\{[^}]*\}/g, "").trim();
    if (text) labels.push(text);
  }
  return labels.sort();
}

test("P7-S13 no front-end source can reach fetch / approve / install endpoints for skills", async () => {
  // 反证：扫描器自己确实认得这些字符串（大小写不敏感），否则下面的"零命中"没有意义
  for (const snippet of FORBIDDEN_ENTRY_SNIPPETS) {
    assert.deepEqual(forbiddenHits(`x "${snippet.toUpperCase()}" y`), [snippet], snippet);
  }
  assert.deepEqual(forbiddenHits("`/api/admin/skills/${name}`"), []);

  const files = await sourceFiles(new URL("../src/", import.meta.url));
  const names = files.map((file) => file.pathname.split("/").pop());
  for (const required of ["skillsApi.ts", "skillsState.ts", "SkillLibraryPanel.tsx", "SettingsPanel.tsx", "mcpAdminApi.ts"]) {
    assert.ok(names.includes(required), `扫描没有覆盖 ${required}`);
  }
  for (const file of files) {
    const text = await readFile(file, "utf8");
    assert.deepEqual(forbiddenHits(text), [], `${file.pathname} 出现了取回 / 批准 / 安装类入口`);
  }

  // skill 相关接口只有列表、管理员看正文与启停（后两者同一路径，PATCH 只发两个键，见上面的测试）
  const api = await readFile(new URL("../src/skillsApi.ts", import.meta.url), "utf8");
  const paths = [...new Set([...api.matchAll(/\/api\/[^"`\s]*/g)].map((item) => item[0]))].sort();
  assert.deepEqual(paths, ["/api/admin/skills/${encodeURIComponent(name)}", "/api/skills"]);
  assert.doesNotMatch(api, /method: "(POST|PUT|DELETE)"/);
  assert.deepEqual([...api.matchAll(/method: "(\w+)"/g)].map((item) => item[1]), ["PATCH"]);
});

test("P7-S13 the skill panel keeps exactly its existing controls", async () => {
  const panel = await readFile(new URL("../src/SkillLibraryPanel.tsx", import.meta.url), "utf8");
  const state = await readFile(new URL("../src/skillsState.ts", import.meta.url), "utf8");

  // 白名单：启停（文案来自 toggleLabel）、查看 / 收起正文、跳到 MCP 扩展页
  assert.equal((panel.match(/<button\b/g) ?? []).length, 3);
  assert.deepEqual(buttonLabels(panel), ["toggleLabel", "收起正文", "查看正文", "查看 MCP 扩展"].sort());
  // 反证：解析器认得出新增的按钮
  assert.deepEqual(buttonLabels('<button type="button">安装</button>'), ["安装"]);
  assert.deepEqual(buttonLabels("<button>{approveLabel(skill)}</button>"), ["approveLabel"]);

  // toggleLabel 只会给出启用 / 停用
  const toggleBody = state.slice(state.indexOf("export function toggleLabel"), state.indexOf("export function skillMeta"));
  assert.deepEqual([...toggleBody.matchAll(/"([^"]*)"/g)].map((item) => item[1]).sort(), ["SKILL_DISABLED", "启用", "停用"].sort());
  for (const status of ["enabled", "SKILL_DISABLED", "SKILL_UNSAFE", "SKILL_SHADOWED"]) {
    assert.ok(["启用", "停用"].includes(toggleLabel(skill({ status }))), status);
  }
  for (const source of ["builtin", "operator", "installed"]) {
    assert.ok(["启用", "停用"].includes(toggleLabel(skill({ source }))), source);
  }

  // 面板只调这三个接口方法；表单控件一个都没有
  assert.deepEqual([...new Set([...panel.matchAll(/\bapi\.(\w+)\(/g)].map((item) => item[1]))].sort(), ["getSkill", "listSkills", "setDisabled"]);
  assert.doesNotMatch(panel, /<(input|textarea|select|form)\b/);
});
