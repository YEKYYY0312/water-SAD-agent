import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { LlmEndpointApi, SettingsApiError } from "../src/settingsApi.ts";
import {
  SETTINGS_SECTIONS,
  clampSection,
  endpointKeySlot,
  initialSettingsSection,
  modelChoices,
  oauthReturnedToSettings,
  validateBaseUrlInput,
  validateEndpointInput,
  visibleSections,
} from "../src/settingsState.ts";

test("sub-navigation lists the four pages and hides MCP only once we know the user is not an admin", () => {
  // P7-S12：联网搜索追加在最后，所有登录用户可见（密钥表单才限管理员）
  assert.deepEqual(SETTINGS_SECTIONS.map((section) => section.id), ["models", "skills", "mcp", "web_search"]);
  assert.deepEqual(visibleSections(true).map((section) => section.id), ["models", "skills", "mcp", "web_search"]);
  // 还不知道角色时先显示（读到非管理员再收起，避免管理员每次进来都先少一项）
  assert.deepEqual(visibleSections(null).map((section) => section.id), ["models", "skills", "mcp", "web_search"]);
  assert.deepEqual(visibleSections(false).map((section) => section.id), ["models", "skills", "web_search"]);
});

test("a hidden section falls back to the first page instead of rendering an empty one", () => {
  assert.equal(clampSection("mcp", false), "models");
  assert.equal(clampSection("mcp", true), "mcp");
  assert.equal(clampSection("skills", false), "skills");
  // 联网搜索页成员也看得到，不会被收起
  assert.equal(clampSection("web_search", false), "web_search");
  assert.equal(clampSection("web_search", true), "web_search");
});

test("returning from an MCP authorization lands on the MCP page, otherwise the first page", () => {
  assert.equal(oauthReturnedToSettings("?mcp_oauth=authorized&server=crm"), true);
  assert.equal(oauthReturnedToSettings("?mcp_oauth=failed&server=crm&code=MCP_OAUTH_REQUIRED"), true);
  assert.equal(oauthReturnedToSettings(""), false);
  assert.equal(oauthReturnedToSettings("?mcp_oauth=bogus"), false);
  assert.equal(initialSettingsSection("?mcp_oauth=authorized&server=crm"), "mcp");
  assert.equal(initialSettingsSection("?other=1"), "models");
});

test("fetching models only needs a valid address, not a chosen model", () => {
  assert.equal(validateBaseUrlInput("https://api.example.com/v1"), null);
  assert.equal(validateBaseUrlInput("http://127.0.0.1:11434/v1"), null);
  assert.match(validateBaseUrlInput("http://llm.example/v1"), /https/);
  assert.match(validateBaseUrlInput("https://user:pw@llm.example/v1"), /用户名/);
  assert.match(validateBaseUrlInput("not a url"), /完整地址/);
  // 保存仍要求模型名合法
  assert.match(validateEndpointInput("https://api.example.com/v1", "bad model"), /模型名/);
  assert.equal(validateEndpointInput("https://api.example.com/v1", "deepseek-chat"), null);
});

test("the key typed next to the address goes to the slot the backend will read for that address", () => {
  assert.equal(endpointKeySlot("https://api.deepseek.com"), "DEEPSEEK_API_KEY");
  assert.equal(endpointKeySlot(" https://API.deepseek.com/ "), "DEEPSEEK_API_KEY");
  assert.equal(endpointKeySlot("https://llm.other-provider.example/v1"), "WATER_AGENT_LLM_API_KEY");
  assert.equal(endpointKeySlot("https://api.deepseek.com/v1"), "WATER_AGENT_LLM_API_KEY");
});

test("the dropdown never drops the model that is currently selected", () => {
  assert.deepEqual(modelChoices(["a", "b"], "b"), ["a", "b"]);
  assert.deepEqual(modelChoices(["a", "b"], "custom"), ["custom", "a", "b"]);
  assert.deepEqual(modelChoices(["a"], "  "), ["a"]);
});

test("listModels posts the unsaved address with CSRF, omits an empty key and surfaces stable error codes", async () => {
  const calls = [];
  globalThis.document = { cookie: "wa_csrf=tok-123" };
  globalThis.fetch = async (path, init) => {
    calls.push({ path, init });
    const body = JSON.parse(init.body);
    if (body.base_url.includes("bad")) {
      return new Response(
        JSON.stringify({ version: "v1", ok: false, error: { code: "WEB_LLM_MODELS_UNAUTHORIZED", message: "端点拒绝了这把 Key" } }),
        { status: 422 },
      );
    }
    return new Response(JSON.stringify({ version: "v1", ok: true, data: { models: ["m1", "m2"] } }), { status: 200 });
  };
  try {
    const api = new LlmEndpointApi();
    assert.deepEqual(await api.listModels("https://llm.example/v1"), { models: ["m1", "m2"] });
    await api.listModels("https://llm.example/v1", "sk-typed-key-0001");
    assert.equal(calls[0].path, "/api/settings/llm-endpoint/models");
    assert.equal(calls[0].init.method, "POST");
    assert.equal(calls[0].init.headers.get("X-CSRF-Token"), "tok-123");
    assert.deepEqual(JSON.parse(calls[0].init.body), { base_url: "https://llm.example/v1" });
    assert.deepEqual(JSON.parse(calls[1].init.body), { base_url: "https://llm.example/v1", api_key: "sk-typed-key-0001" });
    await assert.rejects(api.listModels("https://bad.example/v1", "sk-x-key-000001"), (error) => {
      assert.ok(error instanceof SettingsApiError);
      assert.equal(error.code, "WEB_LLM_MODELS_UNAUTHORIZED");
      assert.equal(error.status, 422);
      assert.doesNotMatch(error.message, /sk-x-key/);
      return true;
    });
  } finally {
    delete globalThis.document;
    delete globalThis.fetch;
  }
});

test("the settings shell renders one page at a time and wires the sub-navigation", async () => {
  const panel = await readFile(new URL("../src/SettingsPanel.tsx", import.meta.url), "utf8");
  const workbench = await readFile(new URL("../src/useWorkspaceController.ts", import.meta.url), "utf8");
  const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
  assert.match(panel, /<nav className="settings-subnav" aria-label="设置分类">/);
  assert.match(panel, /aria-pressed=\{section === item\.id\}/);
  assert.match(panel, /section === "models" && \(/);
  assert.match(panel, /section === "skills" && <SkillLibraryPanel/);
  assert.match(panel, /section === "mcp" && <McpAdminPanel \/>/);
  assert.match(panel, /section === "web_search" && <WebSearchPanel /);
  // 授权回调回首页时设置页直接打开
  assert.match(workbench, /useState\(\(\) => oauthReturnedToSettings\(window\.location\.search\)\)/);
  for (const selector of [".settings-layout", ".settings-subnav", ".model-picker"]) {
    assert.ok(styles.includes(selector), selector);
  }
});
