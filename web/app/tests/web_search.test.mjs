// P7-S12-A11：输入框旁的「联网」逐条开关、设置页「联网搜索」子页（只读状态 + 密钥槽位）、联网相关警告码的中文说明。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { SettingsApi } from "../src/settingsApi.ts";
import {
  CHAT_KEY_SLOTS,
  WEB_SEARCH_KEY_SLOT,
  credentialRows,
  otherCredentialRows,
} from "../src/settingsState.ts";
import { WebSearchApi } from "../src/webSearchApi.ts";
import {
  NO_WEB_SEARCH_TOGGLE,
  WEB_SEARCH_PANEL_NOTICE,
  WEB_SEARCH_PRIVACY_NOTE,
  WEB_SEARCH_TOGGLE_TITLE,
  isWebSearchReady,
  messageWebSearchValue,
  webSearchActive,
  webSearchConfigError,
  webSearchDetailText,
  webSearchKeyRow,
  webSearchKeySourceNote,
  webSearchLoadError,
  webSearchRows,
  webSearchSummary,
  webSearchToggleReducer,
  webSearchTone,
} from "../src/webSearchState.ts";
import { WorkspaceApi } from "../src/workspaceApi.ts";
import { applyRunEvent, emptyRunState, warningDisplayText } from "../src/workspaceState.ts";

const threadId = "55555555-5555-4555-8555-555555555555";
const otherThread = "66666666-6666-4666-8666-666666666666";
const runId = "44444444-4444-4444-8444-444444444444";

const read = (name) => readFile(new URL(`../src/${name}`, import.meta.url), "utf8");

function status(overrides = {}) {
  return {
    enabled: true,
    ready: true,
    provider: "brave",
    host: "api.search.example",
    needs_key: true,
    key: { name: WEB_SEARCH_KEY_SLOT, configured: true, source: "volume" },
    detail: null,
    can_manage: true,
    error: null,
    ...overrides,
  };
}

function stubFetch(handler) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({
      url,
      method: init.method ?? "GET",
      headers: new Headers(init.headers),
      body: init.body ? JSON.parse(init.body) : undefined,
      raw: init.body,
    });
    const result = handler(url, init);
    return new Response(JSON.stringify(result.body), { status: result.status ?? 200, headers: { "Content-Type": "application/json" } });
  };
  return { calls, restore: () => { globalThis.fetch = original; } };
}

const ok = (data) => ({ body: { version: "v1", ok: true, data } });

// ---------------------------------------------------------------------------
// 请求体：只有勾选才带 web_search，未勾选时与此前逐字相同
// ---------------------------------------------------------------------------

test("P7-S12-A11 the message body carries web_search only when ticked, and is byte-identical otherwise", async () => {
  const stub = stubFetch(() => ok({ run_id: runId, thread_id: threadId, status: "queued" }));
  try {
    const api = new WorkspaceApi();
    await api.sendMessage(threadId, "问题");
    await api.sendMessage(threadId, "问题", [], undefined, messageWebSearchValue(false));
    await api.sendMessage(threadId, "问题", [], undefined, false);
    await api.sendMessage(threadId, "问题", ["u-1"], "fast-1", messageWebSearchValue(true));
    await api.sendMessage(threadId, "问题", [], undefined, true);
  } finally {
    stub.restore();
  }
  // 未勾选：三种写法的请求体字符串逐字等于没有这个功能时的 {"message":"问题"}
  assert.equal(stub.calls[0].raw, '{"message":"问题"}');
  assert.equal(stub.calls[1].raw, stub.calls[0].raw);
  assert.equal(stub.calls[2].raw, stub.calls[0].raw);
  for (const call of stub.calls.slice(0, 3)) assert.equal("web_search" in call.body, false);
  // 勾选：恰好是布尔 true（后端对非布尔值 422），与附件 / 模型字段并存互不影响
  assert.deepEqual(stub.calls[3].body, { message: "问题", attachments: ["u-1"], model: "fast-1", web_search: true });
  assert.deepEqual(stub.calls[4].body, { message: "问题", web_search: true });
  assert.strictEqual(stub.calls[4].body.web_search, true);
  assert.equal(messageWebSearchValue(true), true);
  assert.equal(messageWebSearchValue(false), undefined);
});

// ---------------------------------------------------------------------------
// 「联网」开关何时渲染：只看就绪状态，读不到一律当没配好
// ---------------------------------------------------------------------------

test("P7-S12-A11 the toggle shows only when the status says ready === true; anything unreadable counts as not ready", () => {
  assert.equal(isWebSearchReady(status()), true);
  assert.equal(isWebSearchReady(status({ ready: false })), false);
  // 旧后端 / 字段缺失 / 形状不对：开关不渲染，不抛错
  for (const bad of [undefined, null, {}, "ready", 1, [], { ready: "true" }, { ready: 1 }, { enabled: true }]) {
    assert.equal(isWebSearchReady(bad), false);
  }
});

test("P7-S12-A11 a status read that fails (403 / 404 / old backend / network) throws a stable error and is treated as not ready by the controller", async () => {
  const api = new WebSearchApi();
  for (const [httpStatus, code] of [[403, "AUTH_FORBIDDEN"], [404, "WEB_NOT_FOUND"], [401, "AUTH_SESSION_INVALID"]]) {
    const stub = stubFetch(() => ({ status: httpStatus, body: { version: "v1", ok: false, error: { code, message: "x" } } }));
    try {
      await assert.rejects(api.get(), (error) => error.code === code && error.status === httpStatus);
    } finally {
      stub.restore();
    }
  }
  // 非 JSON 响应（旧后端对未知路径回 HTML）也是结构化错误，而不是未捕获异常
  const original = globalThis.fetch;
  globalThis.fetch = async () => new Response("<html>Not Found</html>", { status: 404 });
  try {
    await assert.rejects(api.get(), (error) => error.code === "WEB_INVALID_RESPONSE" && error.status === 404);
  } finally {
    globalThis.fetch = original;
  }

  // 控制器把任何失败都收成 ready=false，且只读 /api/settings/web-search，不碰能力端点
  const controller = await read("useWorkspaceController.ts");
  assert.match(controller, /\.catch\(\(\) => \{ if \(seq === webSearchSeq\.current\) setWebSearchReady\(false\); \}\)/);
  assert.match(controller, /setWebSearchReady\(isWebSearchReady\(view\)\)/);
  assert.doesNotMatch(controller, /capabilities\??\.web_search/);
});

// ---------------------------------------------------------------------------
// 勾选状态机：默认关、不持久、发送后关、换会话关
// ---------------------------------------------------------------------------

test("P7-S12-A11 the toggle starts off for every message and never survives a send or a session switch", () => {
  let state = NO_WEB_SEARCH_TOGGLE;
  assert.equal(webSearchActive(state, threadId, true), false, "默认关");

  state = webSearchToggleReducer(state, { type: "thread", threadId });
  state = webSearchToggleReducer(state, { type: "set", threadId, on: true });
  assert.equal(webSearchActive(state, threadId, true), true);
  assert.equal(webSearchActive(state, threadId, false), false, "运维未就绪时即便状态是勾选也不按联网发送");
  assert.equal(webSearchActive(state, null, true), false, "没有会话就没有可发送的对象");

  // 发送后自动回到关
  state = webSearchToggleReducer(state, { type: "sent", threadId });
  assert.equal(webSearchActive(state, threadId, true), false);

  // 切会话当场回到关；切回原会话也不会复活
  state = webSearchToggleReducer(state, { type: "set", threadId, on: true });
  state = webSearchToggleReducer(state, { type: "thread", threadId: otherThread });
  assert.equal(webSearchActive(state, otherThread, true), false);
  state = webSearchToggleReducer(state, { type: "thread", threadId });
  assert.equal(webSearchActive(state, threadId, true), false, "切回来不复活");

  // 勾选属于被勾选时的会话：对别的会话的发送不生效
  state = webSearchToggleReducer(state, { type: "set", threadId, on: true });
  assert.equal(webSearchActive(state, otherThread, true), false);

  // 没有会话时不能勾选
  assert.deepEqual(webSearchToggleReducer(state, { type: "set", threadId: null, on: true }), NO_WEB_SEARCH_TOGGLE);
});

test("P7-S12-A11 a refused send puts the tick back only for the same session, and an unavailable service clears it", () => {
  let state = webSearchToggleReducer(NO_WEB_SEARCH_TOGGLE, { type: "set", threadId, on: true });
  state = webSearchToggleReducer(state, { type: "sent", threadId });
  // 例如 409：上一问还在回答——发送没被受理，放回勾选，用户不必再点一遍
  const restored = webSearchToggleReducer(state, { type: "refused", threadId, wasOn: true });
  assert.equal(webSearchActive(restored, threadId, true), true);
  // 本来就没勾选：被拒绝也不会凭空打开
  assert.equal(webSearchActive(webSearchToggleReducer(state, { type: "refused", threadId, wasOn: false }), threadId, true), false);
  // 用户在等待期间已经切走：不放回（否则勾选会落到别的会话上）
  const switched = webSearchToggleReducer(state, { type: "thread", threadId: otherThread });
  assert.equal(webSearchActive(webSearchToggleReducer(switched, { type: "refused", threadId, wasOn: true }), otherThread, true), false);
  assert.equal(webSearchActive(webSearchToggleReducer(switched, { type: "refused", threadId, wasOn: true }), threadId, true), false);
  // 用户在等待期间又手动勾选了：保持，不重复翻转
  const again = webSearchToggleReducer(state, { type: "set", threadId, on: true });
  assert.equal(webSearchToggleReducer(again, { type: "refused", threadId, wasOn: true }), again);

  // 运维那边变成不可用：回到关，之后恢复就绪不会自己再打开
  const on = webSearchToggleReducer(NO_WEB_SEARCH_TOGGLE, { type: "set", threadId, on: true });
  const gone = webSearchToggleReducer(on, { type: "unavailable" });
  assert.equal(webSearchActive(gone, threadId, true), false);
  // 没勾选时 unavailable 是空操作（同一个对象，不触发多余的渲染）
  assert.equal(webSearchToggleReducer(NO_WEB_SEARCH_TOGGLE, { type: "unavailable" }), NO_WEB_SEARCH_TOGGLE);
});

test("P7-S12-A11 the toggle copy carries the privacy warning and never suggests web search is more accurate", () => {
  assert.match(WEB_SEARCH_PRIVACY_NOTE, /搜索词会发往运维配置的搜索服务/);
  assert.match(WEB_SEARCH_PRIVACY_NOTE, /请勿在问题里放保密信息/);
  assert.match(WEB_SEARCH_TOGGLE_TITLE, /只对本条消息/);
  assert.match(WEB_SEARCH_TOGGLE_TITLE, /请勿在问题里放保密信息/);
  for (const text of [WEB_SEARCH_PRIVACY_NOTE, WEB_SEARCH_TOGGLE_TITLE, WEB_SEARCH_PANEL_NOTICE]) {
    assert.doesNotMatch(text, /更准|更准确|更可靠|更全面|更权威|推荐开启|建议开启/);
  }
});

test("P7-S12-A11 wiring: the toggle sits in the composer bar with the textarea's disabled condition, and the tick is never stored", async () => {
  const components = await read("workspaceComponents.tsx");
  const toggle = await read("WebSearchToggle.tsx");
  const controller = await read("useWorkspaceController.ts");

  // 开关在 composer-bar 里，禁用条件与输入框、模型选择器一致
  const bar = components.slice(components.indexOf('className="composer-bar"'), components.indexOf('className="composer-scope"'));
  const toggleTag = bar.slice(bar.indexOf("<WebSearchToggle"), bar.indexOf("/>", bar.indexOf("<WebSearchToggle")));
  assert.match(toggleTag, /ready=\{webSearchReady\}/);
  assert.match(toggleTag, /disabled=\{!session \|\| streaming \|\| waiting\}/);
  assert.match(components, /<textarea[\s\S]*?disabled=\{!session \|\| streaming \|\| waiting\}/);
  // 隐私提示在输入框卡片里（勾选时才出现）
  assert.match(components, /<WebSearchNote ready=\{webSearchReady\} on=\{webSearchOn\} \/>/);
  // 未就绪时整个不渲染
  assert.match(toggle, /if \(!ready\) return null;/);
  assert.match(toggle, /aria-pressed=\{on\}/);
  // 纯文本渲染
  assert.doesNotMatch(toggle, /dangerouslySetInnerHTML|AnswerMarkdown/);

  // 不持久化、不随会话记忆：勾选状态不进 localStorage / 会话 PATCH / 草稿
  const toggleStateSource = await read("webSearchState.ts");
  assert.doesNotMatch(toggleStateSource + toggle, /localStorage|sessionStorage/);
  assert.doesNotMatch(controller.slice(controller.indexOf("webSearchToggle")), /saveValue\([^)]*[wW]eb[sS]earch/);
  assert.match(controller, /messageWebSearchValue\(sentWithWebSearch\)/);
  assert.match(controller, /dispatchWebSearch\(\{ type: "sent", threadId: selectedThreadId \}\)/);
  assert.match(controller, /dispatchWebSearch\(\{ type: "refused", threadId: selectedThreadId, wasOn: sentWithWebSearch \}\)/);
  assert.match(controller, /dispatchWebSearch\(\{ type: "thread", threadId: selectedThreadId \}\)/);
  // 会话级 PATCH 不带联网字段（逐条消息，不进会话默认）
  const api = await read("workspaceApi.ts");
  const patch = api.slice(api.indexOf("setSessionModel("), api.indexOf("/** ``pending_confirmation``"));
  assert.doesNotMatch(patch, /web_search/);
});

// ---------------------------------------------------------------------------
// 警告码 → 中文说明
// ---------------------------------------------------------------------------

test("P7-S12-A11 WEB_SEARCH_NOT_EXPOSED is shown in Chinese; unknown warnings are shown verbatim", () => {
  const text = warningDisplayText("WEB_SEARCH_NOT_EXPOSED");
  assert.equal(text, "本条消息勾选了联网，但这一轮联网搜索没有开放（工具预算已满或联网服务暂不可用），本回答未联网");
  // 其他 warning 保持原样，包括现有的稳定码与英文原文
  for (const other of ["MEMORY_RECALL_DEGRADED", "UPLOAD_RECALL_DEGRADED", "Long-term memory was unavailable; continued without it", "", "constructor", "__proto__", "toString"]) {
    assert.equal(warningDisplayText(other), other);
  }
  // 前后空白不影响命中，未命中时仍保持原文（含空白）
  assert.equal(warningDisplayText(" WEB_SEARCH_NOT_EXPOSED "), text);
  assert.equal(warningDisplayText(" other "), " other ");
});

test("P7-S12-A11 a warning event with the stable code displays the Chinese text, whichever of code or message the server sent", () => {
  const event = (type, payload, id) => ({ event_id: id, type, run_id: runId, thread_id: threadId, time: "", payload_version: "v1", payload });
  // SSE 的 warning 事件：payload.code 是稳定码，message 可能是英文原文——稳定码优先，用户看到的是中文
  let state = applyRunEvent(emptyRunState(), event("warning", { code: "WEB_SEARCH_NOT_EXPOSED", message: "web search was not exposed" }, "1"));
  assert.deepEqual(state.warnings.map(warningDisplayText), [warningDisplayText("WEB_SEARCH_NOT_EXPOSED")]);
  // 只有 code 或只有 message 同样可用
  state = applyRunEvent(emptyRunState(), event("warning", { code: "WEB_SEARCH_NOT_EXPOSED" }, "2"));
  assert.match(warningDisplayText(state.warnings[0]), /本回答未联网/);
  // 同一个警告再来一次（SSE 的 warning 与 finalize.warnings 都带它）不重复
  state = applyRunEvent(state, event("warning", { code: "WEB_SEARCH_NOT_EXPOSED", message: "x" }, "3"));
  assert.equal(state.warnings.length, 1);
  // run 停在人工确认门时，后端把原始警告串放在 message 里、code 固定为 RUN_WARNING（run_manager 的确认挂起分支）：
  // 稳定码在 message 里，照样要显示中文
  const suspended = applyRunEvent(emptyRunState(), event("warning", { code: "RUN_WARNING", message: "WEB_SEARCH_NOT_EXPOSED" }, "3b"));
  assert.deepEqual(suspended.warnings.map(warningDisplayText), [warningDisplayText("WEB_SEARCH_NOT_EXPOSED")]);
  assert.match(warningDisplayText(suspended.warnings[0]), /本回答未联网/);
  // finalize 的 warnings 数组里是原始码
  const done = applyRunEvent(emptyRunState(), event("finalize", { status: "succeeded", answer: "好", citations: [], warnings: ["WEB_SEARCH_NOT_EXPOSED"] }, "4"));
  assert.deepEqual(done.warnings.map(warningDisplayText), [warningDisplayText("WEB_SEARCH_NOT_EXPOSED")]);

  // 现有 warning 的归并与显示不变：没登记的码，message 优先、其次 code，原样显示
  const generic = applyRunEvent(emptyRunState(), event("warning", { code: "RUN_WARNING", message: "some detail" }, "5"));
  assert.deepEqual(generic.warnings.map(warningDisplayText), ["some detail"]);
  const memory = applyRunEvent(emptyRunState(), event("warning", { code: "MEMORY_RECALL_DEGRADED", message: "Long-term memory was unavailable; continued without it" }, "6"));
  assert.deepEqual(memory.warnings.map(warningDisplayText), ["Long-term memory was unavailable; continued without it"]);
  const codeOnly = applyRunEvent(emptyRunState(), event("warning", { code: "SOMETHING_NEW" }, "7"));
  assert.deepEqual(codeOnly.warnings, ["SOMETHING_NEW"]);
  assert.deepEqual(applyRunEvent(emptyRunState(), event("warning", {}, "8")).warnings, []);
});

test("P7-S12-A11 the conversation renders warnings through the mapping as plain text", async () => {
  const components = await read("workspaceComponents.tsx");
  const state = await read("workspaceState.ts");
  assert.match(components, /run\.warnings\.map\(\(warning\) => <div className="warning-state" role="status" key=\{warning\}>\{warningDisplayText\(warning\)\}<\/div>\)/);
  assert.doesNotMatch(components + state, /dangerouslySetInnerHTML/);
  // 映射是纯函数、不带 JSX：映射所在的是 .ts 文件，不从 react 导入，也没有 JSX 元素（元素后面紧跟属性或闭合，泛型 `Foo<Bar>` 不算）
  assert.doesNotMatch(state, /from "react"/);
  assert.doesNotMatch(state, /(?:return|=>|\()\s*<[A-Za-z][\w.]*(?:\s+[\w-]+=|\s*\/?>)/);
  const pure = await read("webSearchState.ts");
  assert.doesNotMatch(pure, /from "react"|dangerouslySetInnerHTML/);
});

// ---------------------------------------------------------------------------
// 设置页「联网搜索」：只读状态
// ---------------------------------------------------------------------------

test("P7-S12-A11 the status page reads state in plain language and never shows more than the host name", () => {
  assert.equal(webSearchTone(status()), "succeeded");
  assert.equal(webSearchTone(status({ ready: false })), "not_configured");
  assert.equal(webSearchTone(status({ ready: false, error: { code: "WEB_SEARCH_CONFIG_INVALID", message: "x" } })), "failed");

  assert.match(webSearchSummary(status()), /已就绪/);
  assert.match(webSearchSummary(status({ enabled: false, ready: false, provider: null, host: null, key: null, needs_key: false })), /未开启联网搜索/);
  assert.match(webSearchSummary(status({ ready: false, detail: "base_url_missing" })), /已开启，但还不能用：运维还没有配置搜索服务地址/);
  assert.match(webSearchSummary(status({ ready: false, error: { code: "WEB_SEARCH_CONFIG_INVALID", message: "x" } })), /配置不合法/);

  // 缺项原因码的中文说明；密钥一项按角色给出下一步
  assert.match(webSearchDetailText("provider_missing", false), /提供方/);
  assert.match(webSearchDetailText("base_url_missing", true), /地址/);
  assert.match(webSearchDetailText("api_key_missing", true), /请在下方填写/);
  assert.match(webSearchDetailText("api_key_missing", false), /联系管理员/);
  assert.equal(webSearchDetailText(null, true), null);
  assert.match(webSearchDetailText("something_new", true), /something_new/);

  const rows = webSearchRows(status());
  assert.deepEqual(rows.map((row) => row.label), ["运维开关", "提供方", "搜索服务主机", "密钥"]);
  assert.equal(rows.find((row) => row.label === "搜索服务主机").value, "api.search.example");
  assert.equal(rows.find((row) => row.label === "密钥").value, "已配置（本面板写入）");
  const noKey = webSearchRows(status({ provider: "searxng", needs_key: false, key: null }));
  assert.equal(noKey.find((row) => row.label === "密钥").value, "该提供方无需密钥");
  assert.equal(webSearchRows(status({ enabled: false, provider: null, host: null, key: null, needs_key: false, ready: false })).find((row) => row.label === "提供方").value, "未设置");
  // 展示的所有文字里没有完整地址或密钥形态
  for (const row of rows) assert.doesNotMatch(row.value, /https?:\/\/|sk-/);
});

test("P7-S12-A11 an invalid operator configuration is shown to everyone, but its server-side reason only to admins", () => {
  const error = { code: "WEB_SEARCH_CONFIG_INVALID", message: "WATER_AGENT_WEB_SEARCH 只接受 off / on" };
  const admin = webSearchConfigError(status({ error, can_manage: true }));
  const member = webSearchConfigError(status({ error, can_manage: false }));
  assert.equal(admin.code, "WEB_SEARCH_CONFIG_INVALID");
  assert.match(admin.message, /WATER_AGENT_WEB_SEARCH 只接受/);
  assert.equal(member.code, "WEB_SEARCH_CONFIG_INVALID");
  assert.doesNotMatch(member.message, /WATER_AGENT_WEB_SEARCH 只接受/);
  assert.match(member.message, /联系运维/);
  assert.equal(webSearchConfigError(status()), null);
});

test("P7-S12-A11 load failures read as sentences; an old backend without the endpoint is named as such", () => {
  assert.match(webSearchLoadError("WEB_NOT_FOUND", 404, "Not Found").message, /没有联网搜索功能/);
  assert.match(webSearchLoadError("AUTH_SESSION_INVALID", 401, "x").message, /重新登录/);
  assert.deepEqual(webSearchLoadError("WEB_DB", 503, "数据库不可用"), { code: "WEB_DB", message: "数据库不可用" });
});

// ---------------------------------------------------------------------------
// 设置页「联网搜索」：只读接口、没有写地址的通道、密钥只写不读
// ---------------------------------------------------------------------------

test("P7-S12-A11 the status API is read-only: one GET, no CSRF, no write method for address / provider / switch", async () => {
  const stub = stubFetch(() => ok(status()));
  try {
    const view = await new WebSearchApi().get();
    assert.equal(view.host, "api.search.example");
  } finally {
    stub.restore();
  }
  assert.deepEqual(stub.calls.map((call) => [call.method, call.url]), [["GET", "/api/settings/web-search"]]);
  assert.equal(stub.calls[0].headers.get("X-CSRF-Token"), null);

  const api = await read("webSearchApi.ts");
  assert.doesNotMatch(api, /method:\s*"(POST|PUT|PATCH|DELETE)"/);
  assert.doesNotMatch(api, /\b(put|post|patch|set|update|save)[A-Z]\w*\(/);
  // 整个前端里，没有任何请求体会写 base_url / provider / 开关到 web-search 接口
  const panel = await read("WebSearchPanel.tsx");
  const state = await read("webSearchState.ts");
  for (const source of [api, panel, state]) {
    assert.doesNotMatch(source, /JSON\.stringify/);
    assert.doesNotMatch(source, /<input[^>]*(base_url|provider|baseUrl)/);
  }
  // 唯一的写入口是既有的凭据接口，目标槽位写死
  assert.match(panel, /credentialApi\.putCredential\(WEB_SEARCH_KEY_SLOT, value\)/);
  assert.match(panel, /credentialApi\.deleteCredential\(WEB_SEARCH_KEY_SLOT\)/);
  assert.doesNotMatch(panel, /putCredential\(_?name/);
  assert.match(panel, /const saveKey = async \(_name: string, value: string\)/);
});

test("P7-S12-A11 the key goes through the existing credential endpoints: write-only, CSRF on writes, slot fixed", async () => {
  globalThis.document = { cookie: "wa_csrf=csrf-web-search" };
  const stub = stubFetch((_url, init) => ok({ credential: { name: WEB_SEARCH_KEY_SLOT, configured: init.method !== "DELETE", source: init.method === "DELETE" ? "none" : "volume", updated_at: null } }));
  try {
    const api = new SettingsApi();
    await api.putCredential(WEB_SEARCH_KEY_SLOT, "sk-search-key-0001");
    await api.deleteCredential(WEB_SEARCH_KEY_SLOT);
  } finally {
    stub.restore();
    delete globalThis.document;
  }
  assert.deepEqual(stub.calls.map((call) => [call.method, call.url]), [
    ["PUT", `/api/settings/credentials/${WEB_SEARCH_KEY_SLOT}`],
    ["DELETE", `/api/settings/credentials/${WEB_SEARCH_KEY_SLOT}`],
  ]);
  assert.deepEqual(stub.calls[0].body, { value: "sk-search-key-0001" });
  for (const call of stub.calls) assert.equal(call.headers.get("X-CSRF-Token"), "csrf-web-search");
});

test("P7-S12-A11 the key form: admin only, only when the provider needs a key, validated like every other key, value never echoed", async () => {
  const panel = await read("WebSearchPanel.tsx");
  const shared = await read("settingsShared.tsx");
  const settings = await read("SettingsPanel.tsx");

  // 仅管理员、仅 key !== null 才渲染表单（key 为 null = provider 不需要密钥）
  assert.match(panel, /\{view\.can_manage && view\.key && \(/);
  // 复用既有校验与既有卡片，不另写一套
  assert.match(shared, /validateCredentialInput\(value\)/);
  assert.match(panel, /<CredentialCard/);
  assert.match(shared, /type="password"/);
  assert.match(shared, /autoComplete="off"/);
  assert.match(shared, /setValue\(""\);/);
  // 任何接口都不回传密钥；页面里没有密钥值的展示位
  assert.doesNotMatch(panel, /value=\{[^}]*\.(value|secret|api_key)/);
  // 说明地址 / 提供方 / 开关是运维的，面板不能改
  assert.match(WEB_SEARCH_PANEL_NOTICE, /由运维在部署配置里设置，面板不能修改/);
  assert.match(WEB_SEARCH_PANEL_NOTICE, /开关/);
  assert.match(WEB_SEARCH_PANEL_NOTICE, /提供方/);
  assert.match(WEB_SEARCH_PANEL_NOTICE, /地址/);
  assert.match(panel, /WEB_SEARCH_PANEL_NOTICE/);
  // 保存 / 清除后通知工作台重读就绪状态，开关随之出现或消失
  assert.match(panel, /await reload\(\);\n\s+onChanged\?\.\(\);/);
  assert.match(settings, /onWebSearchChanged\?: \(\) => void;/);
  assert.match(settings, /<WebSearchPanel onChanged=\{onWebSearchChanged\} onBack=\{onBack\} \/>/);
  const workbench = await read("Workbench.tsx");
  assert.match(workbench, /onWebSearchChanged=\{workspace\.refreshWebSearch\}/);

  // 表单行总是写 WEB_SEARCH_KEY_SLOT，不取决于服务端回传的槽位名
  const row = webSearchKeyRow({ name: "SOMETHING_ELSE", configured: false, source: "none" });
  assert.equal(row.name, WEB_SEARCH_KEY_SLOT);
  assert.equal(row.configured, false);
  assert.equal(row.tone, "not_configured");
  assert.match(row.purpose, /联网搜索/);
  assert.equal(webSearchKeyRow({ name: WEB_SEARCH_KEY_SLOT, configured: true, source: "secret_file" }).sourceLabel, "Compose secret 文件");
});

test("P7-S12-A11 a key that comes from env or a Compose secret says it is not replaced by a panel write", () => {
  assert.match(webSearchKeySourceNote({ name: WEB_SEARCH_KEY_SLOT, configured: true, source: "env" }), /环境变量.*不会顶替/);
  assert.match(webSearchKeySourceNote({ name: WEB_SEARCH_KEY_SLOT, configured: true, source: "secret_file" }), /Compose secret.*不会顶替/);
  assert.equal(webSearchKeySourceNote({ name: WEB_SEARCH_KEY_SLOT, configured: true, source: "volume" }), null);
  assert.equal(webSearchKeySourceNote({ name: WEB_SEARCH_KEY_SLOT, configured: false, source: "none" }), null);
});

test("P7-S12-A11 the search key has exactly one form: it is listed on its own page, not again under 'other keys'", () => {
  const rows = credentialRows([
    { name: "DEEPSEEK_API_KEY", configured: true, source: "volume", updated_at: null },
    { name: "DASHSCOPE_API_KEY", configured: false, source: "none", updated_at: null },
    { name: WEB_SEARCH_KEY_SLOT, configured: false, source: "none", updated_at: null },
  ]);
  // 后端把这一行也列进 /api/settings/credentials：它有中文用途与提示
  const searchRow = rows.find((row) => row.name === WEB_SEARCH_KEY_SLOT);
  assert.match(searchRow.purpose, /联网搜索/);
  assert.match(searchRow.hint, /运维/);
  // 「模型与密钥」页底部不再重复出这一行；对话端点可编辑时，对话 Key 行也照旧被收起
  assert.deepEqual(otherCredentialRows(rows, false).map((row) => row.name), ["DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY"]);
  assert.deepEqual(otherCredentialRows(rows, true).map((row) => row.name), ["DASHSCOPE_API_KEY"]);
  assert.ok(CHAT_KEY_SLOTS.has("DEEPSEEK_API_KEY") && CHAT_KEY_SLOTS.has("WATER_AGENT_LLM_API_KEY"));
});

test("P7-S12-A11 plain-text rendering and token-only styles", async () => {
  const styles = await read("styles.css");
  for (const name of ["WebSearchPanel.tsx", "WebSearchToggle.tsx", "settingsShared.tsx"]) {
    const source = await read(name);
    assert.doesNotMatch(source, /dangerouslySetInnerHTML|AnswerMarkdown|innerHTML/, name);
  }
  const block = styles.slice(styles.indexOf("/* P7-S12 联网搜索"));
  assert.ok(block.length > 0);
  assert.doesNotMatch(block, /#[0-9a-fA-F]{3,8}\b|rgb\(/);
  for (const selector of [".web-search-toggle", ".web-search-note", ".web-search-panel", ".web-search-status"]) {
    assert.ok(block.includes(selector), selector);
  }
  // 底栏在窄屏放不下时让模型选择器先收缩，按钮保持原尺寸：否则「联网」开关多占的宽度会把发送键挤出输入框卡片
  assert.match(block, /\.composer-bar \{ min-width: 0; \}/);
  assert.match(block, /\.composer-bar \.composer-model-select \{ min-width: 0; \}/);
  assert.match(block, /\.composer-bar > \.icon-button, \.composer-bar > \.send-button, \.composer-bar > \.stop-button \{ flex: none; \}/);
  assert.match(block, /\.web-search-toggle \{[^}]*white-space: nowrap; flex: none;/);
});
