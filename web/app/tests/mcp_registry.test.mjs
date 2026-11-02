// P7-S14-A07：设置页 MCP 页的「从目录发现」区（纯文本渲染、只读、没有连接 / 启用 / 应用控件、提案请求体只有 name 与 version）。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { McpRegistryApi } from "../src/mcpRegistryApi.ts";
import {
  MAX_QUERY_CHARS,
  PROPOSAL_HASH_HINT,
  REGISTRY_NOTICE,
  REGISTRY_PROPOSAL_FILENAME,
  entryTags,
  entryTone,
  notProposableLabel,
  registryErrorMessage,
  unsafeLabel,
  validateRegistryQuery,
} from "../src/mcpRegistryState.ts";
import { WorkspaceApiError } from "../src/workspaceApi.ts";

function entry(overrides = {}) {
  return {
    name: "weather-station",
    version: "1.0.0",
    status: "ok",
    description: "查询气象站实时数据的 MCP 服务。",
    remotes: [{ type: "streamable-http", url: "https://mcp.weather-station.example/mcp" }],
    packages: [],
    dropped_fields: 0,
    unsafe_reason: "",
    proposable: true,
    not_proposable_reason: null,
    ...overrides,
  };
}

/** 去掉块注释与行注释：源码断言只看代码，不看说明文字里提到的名字（"不用 dangerouslySetInnerHTML" 不是使用）。 */
function stripComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`])\/\/[^\n]*/g, "$1");
}

/** 记录请求的假 fetch：按顺序回放给定的响应。 */
function recordingFetch(responses) {
  const calls = [];
  const fetchImpl = async (input, init) => {
    calls.push({ input, init });
    const next = responses.shift() ?? { status: 200, body: { ok: true, data: {} } };
    return new Response(JSON.stringify(next.body), { status: next.status, headers: { "Content-Type": "application/json" } });
  };
  return { fetchImpl, calls };
}

test("query validation mirrors the backend: single line, at most 64 characters, empty means browse", () => {
  assert.equal(validateRegistryQuery(""), null);
  assert.equal(validateRegistryQuery("  pipe  "), null);
  assert.equal(validateRegistryQuery("管道 计算"), null);
  assert.equal(MAX_QUERY_CHARS, 64);
  assert.match(validateRegistryQuery("x".repeat(65)), /64/);
  assert.match(validateRegistryQuery("line1\nline2"), /换行/);
  assert.match(validateRegistryQuery("tab\there"), /控制字符/);
  assert.match(validateRegistryQuery("bell\u0007"), /控制字符/);
  assert.match(validateRegistryQuery("zero\u200bwidth"), /不可见字符/);
  assert.match(validateRegistryQuery("rtl\u202eoverride"), /不可见字符/);
  assert.match(validateRegistryQuery("bom\ufeffhere"), /不可见字符/);
  // 同形字前端不拦（后端的字符白名单兜底，422）；中文与西文标点照常通过
  assert.equal(validateRegistryQuery("管径 DN200（给水）"), null);
});

test("entries are labelled without ever echoing entry content", () => {
  assert.deepEqual(entryTags(entry()), ["可提案"]);
  assert.deepEqual(entryTags(entry({ proposable: false, not_proposable_reason: "no_remote" })), ["仅展示"]);
  assert.deepEqual(entryTags(entry({ status: "unsafe", proposable: false, dropped_fields: 3 })), ["不安全", "丢弃 3 个未知字段"]);
  assert.equal(entryTone(entry()), "succeeded");
  assert.equal(entryTone(entry({ proposable: false })), "not_configured");
  assert.equal(entryTone(entry({ status: "unsafe", proposable: false })), "failed");
  // 原因短码 → 固定文案；未登记的短码原样显示（不会把条目内容当文案）
  assert.match(notProposableLabel("ip_literal"), /IP 字面量/);
  assert.match(notProposableLabel("no_streamable_http"), /stdio/);
  assert.equal(notProposableLabel("something_new"), "something_new");
  assert.equal(notProposableLabel(null), "可生成提案");
  assert.match(unsafeLabel("injection"), /注入/);
  assert.match(unsafeLabel("charset"), /不可见/);
  assert.equal(unsafeLabel("whatever"), "未通过安全检查");
});

test("error copy covers every stable code the backend can return, and falls back to the server message", () => {
  for (const code of [
    "WEB_MCP_REGISTRY_NOT_CONFIGURED",
    "WEB_MCP_REGISTRY_RATE_LIMITED",
    "MCP_REGISTRY_UNAVAILABLE",
    "MCP_REGISTRY_AUTH",
    "MCP_REGISTRY_RATE_LIMITED",
    "MCP_REGISTRY_BAD_RESPONSE",
    "MCP_REGISTRY_ENTRY_NOT_FOUND",
    "MCP_REGISTRY_ENTRY_UNSAFE",
    "MCP_REGISTRY_NOT_PROPOSABLE",
  ]) {
    assert.notEqual(registryErrorMessage(code, "fallback"), "fallback", code);
  }
  assert.equal(registryErrorMessage("SOMETHING_ELSE", "来自服务端"), "来自服务端");
  assert.match(REGISTRY_NOTICE, /不会连接条目里的任何地址/);
  assert.match(PROPOSAL_HASH_HINT, /不是签名/);
  assert.equal(REGISTRY_PROPOSAL_FILENAME, "mcp_registry_proposal.json");
});

test("api: search sends only the query (and cursor), the proposal body is exactly name and version, writes carry CSRF", async () => {
  Object.defineProperty(globalThis, "document", { value: { cookie: "wa_csrf=tok-123" }, configurable: true });
  const { fetchImpl, calls } = recordingFetch([
    { status: 200, body: { ok: true, data: { entries: [], count: 0, pages: 1, truncated: false, registry_host: "r", notice: "" } } },
    { status: 200, body: { ok: true, data: { entries: [], count: 0, pages: 1, truncated: false, registry_host: "r", notice: "" } } },
    { status: 200, body: { ok: true, data: { proposal: { name: "weather-station" } } } },
    { status: 200, body: { ok: true, data: { configured: true } } },
  ]);
  const api = new McpRegistryApi(fetchImpl);
  await api.search("pipe");
  await api.search("pipe", "p1");
  await api.propose("weather-station", "1.0.0");
  await api.status();

  assert.deepEqual(JSON.parse(calls[0].init.body), { query: "pipe" });
  assert.deepEqual(JSON.parse(calls[1].init.body), { query: "pipe", cursor: "p1" });
  assert.deepEqual(JSON.parse(calls[2].init.body), { name: "weather-station", version: "1.0.0" });
  assert.deepEqual(
    calls.map((call) => [call.input, call.init.method ?? "GET"]),
    [
      ["/api/admin/mcp/registry/search", "POST"],
      ["/api/admin/mcp/registry/search", "POST"],
      ["/api/admin/mcp/registry/proposal", "POST"],
      ["/api/admin/mcp/registry/status", "GET"],
    ],
  );
  for (const call of calls.slice(0, 3)) assert.equal(new Headers(call.init.headers).get("X-CSRF-Token"), "tok-123");
  assert.equal(new Headers(calls[3].init.headers).get("X-CSRF-Token"), null, "读接口不带 CSRF");
});

test("api: failures surface the stable code and HTTP status", async () => {
  Object.defineProperty(globalThis, "document", { value: { cookie: "wa_csrf=tok" }, configurable: true });
  const { fetchImpl } = recordingFetch([
    { status: 502, body: { ok: false, error: { code: "MCP_REGISTRY_UNAVAILABLE", message: "x" } } },
    { status: 409, body: { ok: false, error: { code: "WEB_MCP_REGISTRY_NOT_CONFIGURED", message: "y" } } },
  ]);
  const api = new McpRegistryApi(fetchImpl);
  await assert.rejects(api.search("a"), (error) => error instanceof WorkspaceApiError && error.code === "MCP_REGISTRY_UNAVAILABLE" && error.status === 502);
  await assert.rejects(api.propose("a", "1.0.0"), (error) => error instanceof WorkspaceApiError && error.status === 409);
});

test("section is read-only: plain text only, no connect / enable / apply / save controls, nothing executes entry content", async () => {
  const section = stripComments(await readFile(new URL("../src/McpRegistrySection.tsx", import.meta.url), "utf8"));
  const api = stripComments(await readFile(new URL("../src/mcpRegistryApi.ts", import.meta.url), "utf8"));
  const state = stripComments(await readFile(new URL("../src/mcpRegistryState.ts", import.meta.url), "utf8"));
  const panel = await readFile(new URL("../src/McpAdminPanel.tsx", import.meta.url), "utf8");

  // 挂在 MCP 页里（不新增副栏子页），且在导入区之前
  assert.match(panel, /<McpRegistrySection \/>/);
  assert.ok(panel.indexOf("<McpRegistrySection />") < panel.indexOf("<ImportSection />"));

  // 纯文本：条目内容只经 React 文本节点；没有 HTML 注入口、没有 Markdown 渲染、没有可点击链接
  assert.doesNotMatch(section, /dangerouslySetInnerHTML|innerHTML|<a[\s>]|href=|react-markdown|ReactMarkdown|AnswerMarkdown|<img/);
  assert.doesNotMatch(section, /window\.open|location\.(href|assign)|<iframe/);

  // 没有连接 / 启用 / 应用 / 保存 / 探测控件：能点的只有「搜索」「生成提案」「复制」「下载」
  // 逐个 <button …>…</button>：取开合之间的文本，`{cond ? "a" : "b"}` 这类表达式里的字符串字面量都算按钮可能显示的文案
  const buttonLabels = [...section.matchAll(/<button\b[\s\S]*?<\/button>/g)].map((match) => {
    const block = match[0];
    const open = block.indexOf("}>") >= 0 && block.indexOf("}>") < block.indexOf("</button>") ? block.indexOf("}>") + 2 : block.indexOf(">") + 1;
    const body = block.slice(open, block.indexOf("</button>"));
    const literals = [...body.matchAll(/"([^"]*)"/g)].map((m) => m[1]);
    const plain = body.replace(/\{[^}]*\}/g, "").replace(/\s+/g, " ").trim();
    return [...literals, plain].filter(Boolean);
  });
  assert.equal(buttonLabels.length, 4, `应有 4 个按钮：${JSON.stringify(buttonLabels)}`);
  for (const label of buttonLabels.flat()) assert.doesNotMatch(label, /连接|启用|应用|保存|探测|授权|安装|添加|导入到/, label);
  // 能点的只有：搜索（提交）/ 生成提案 / 复制 / 下载
  assert.ok(buttonLabels.some((labels) => labels.includes("搜索")));
  assert.ok(buttonLabels.some((labels) => labels.includes("生成提案")));
  assert.ok(buttonLabels.some((labels) => labels.includes("复制")));
  assert.ok(buttonLabels.some((labels) => labels.includes("下载")));
  assert.match(section, /生成提案/);

  // 前端只会调用这三个接口：类里的方法恰好是 constructor / request（私有）/ status / search / propose，
  // 多出任何一个（apply / connect / probe / updateServer …）都红。同时断言所有请求路径都在 /api/admin/mcp/registry/ 下
  const methods = [...api.matchAll(/^\s{2}(?:private\s+|async\s+)*(\w+)(?:<[^>]*>)?\(/gm)].map((match) => match[1]);
  assert.deepEqual(methods, ["constructor", "request", "status", "search", "propose"]);
  const paths = [...api.matchAll(/"(\/api\/[^"]*)"/g)].map((match) => match[1]);
  assert.ok(paths.length >= 3);
  for (const path of paths) assert.match(path, /^\/api\/admin\/mcp\/registry\/(status|search|proposal)$/, path);
  assert.doesNotMatch(section + api, /probe|updateServer|putCredential|startOAuth|importPreview|McpAdminApi/);

  // 提案请求体类型：propose 只有两个参数
  assert.match(api, /propose\(name: string, version: string\)/);
  assert.doesNotMatch(section, /api\.propose\([^)]*(description|remotes|url)/);

  // 复用既有的 CSRF 实现，不另写一份
  assert.match(api, /import \{ csrfToken, WorkspaceApiError, type ApiEnvelope \} from "\.\/workspaceApi\.ts"/);
  assert.doesNotMatch(section + api + state, /wa_csrf/);

  // 读状态失败（含 403）时整块不渲染
  assert.match(section, /if \(status === null\) return null;/);
  // 固定说明在页面上
  assert.match(section, /REGISTRY_NOTICE/);
});

test("unsafe entries show only the name, version and a reason — never description, addresses or packages", async () => {
  const section = stripComments(await readFile(new URL("../src/McpRegistrySection.tsx", import.meta.url), "utf8"));
  // 渲染分支：unsafe 走单独一支，只输出原因；description / remotes / packages 只在另一支里出现
  const start = section.indexOf('entry.status === "unsafe" ? (');
  assert.ok(start >= 0, "找不到 unsafe 渲染分支");
  const split = section.indexOf(") : (", start);
  assert.ok(split > start, "unsafe 分支没有 else 支");
  const unsafeBranch = section.slice(start, split);
  const otherBranch = section.slice(split, section.indexOf("</>", split));
  assert.match(unsafeBranch, /unsafeLabel\(entry\.unsafe_reason\)/);
  assert.doesNotMatch(unsafeBranch, /entry\.description|entry\.remotes|entry\.packages/);
  assert.match(otherBranch, /entry\.description/);
  assert.match(otherBranch, /entry\.remotes\.map/);
  // 提案按钮只对可提案条目出现
  assert.match(section, /entry\.proposable && \(/);
});
