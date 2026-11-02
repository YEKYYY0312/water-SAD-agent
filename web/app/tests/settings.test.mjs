import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  compatibilityView,
  credentialHint,
  credentialRows,
  endpointSourceLabel,
  formatUpdatedAt,
  lockedReasonMessage,
  modelReadinessMessage,
  modelReadinessTone,
  sourceLabel,
  validateCredentialInput,
  validateEndpointInput,
} from "../src/settingsState.ts";

test("rejects the shapes that would silently save an unusable key", () => {
  assert.equal(validateCredentialInput("sk-a-long-enough-value"), null);

  assert.match(validateCredentialInput(""), /填写/);
  assert.match(validateCredentialInput("   "), /填写/);
  assert.match(validateCredentialInput("short"), /长度/);
  // 误粘整行 export 或多行文本是最常见的失误，静默保存只会产出永远认证失败的配置。
  assert.match(validateCredentialInput("export KEY=sk-pasted-whole-line"), /空格/);
  assert.match(validateCredentialInput("sk-with\nnewline"), /空格|换行/);
  assert.match(validateCredentialInput("sk-trailing value"), /空格/);
});

test("names the source a reader can act on", () => {
  // 本面板写入的值也会被注入环境变量，但后端记得那是注入的，所以仍报 volume；
  // 若这里把 volume 也叫「环境变量」，用户就无从判断该去哪里改。
  assert.equal(sourceLabel("volume"), "本面板写入");
  assert.equal(sourceLabel("secret_file"), "Compose secret 文件");
  assert.equal(sourceLabel("env"), "环境变量");
  assert.equal(sourceLabel("none"), "未配置");
});

test("maps credential rows onto the existing status tones", () => {
  const rows = credentialRows([
    { name: "DEEPSEEK_API_KEY", configured: true, source: "volume", updated_at: null },
    { name: "DASHSCOPE_API_KEY", configured: false, source: "none", updated_at: null },
  ]);

  assert.equal(rows.length, 2);
  assert.equal(rows[0].tone, "succeeded");
  assert.equal(rows[1].tone, "not_configured");
  // P7-S10：文案与能力一致——对话模型是任意 OpenAI 兼容端点，百炼 Key 同时服务检索、重排与视觉审阅
  assert.match(rows[0].purpose, /对话模型（OpenAI 兼容端点/);
  assert.match(rows[1].purpose, /向量检索（OpenAI 兼容 embedding 端点/);
  assert.match(rows[1].purpose, /重排（阿里云百炼，可选）/);
  assert.equal(rows[0].sourceLabel, "本面板写入");
});

test("never renders an unparseable timestamp", () => {
  assert.equal(formatUpdatedAt(null), null);
  assert.equal(formatUpdatedAt("not-a-date"), null);
  assert.ok(formatUpdatedAt("2026-09-21T10:21:41.764341+00:00"));
});

test("states the consequence of an unconfigured model, not just its status", () => {
  assert.match(modelReadinessMessage(false), /424/);
  assert.match(modelReadinessMessage(false), /无需重启/);
  assert.match(modelReadinessMessage(true), /已可/);
  assert.match(modelReadinessMessage(null), /加载中/);

  assert.equal(modelReadinessTone(true), "succeeded");
  assert.equal(modelReadinessTone(false), "not_configured");
  assert.equal(modelReadinessTone(null), "running");
});

test("settings panel reuses shared primitives instead of forking them", async () => {
  const panel = await readFile(new URL("../src/SettingsPanel.tsx", import.meta.url), "utf8");
  const api = await readFile(new URL("../src/settingsApi.ts", import.meta.url), "utf8");

  // 自检状态复用 Inspector 既有组件，不另画一套状态徽记。
  assert.match(panel, /CapabilityStatus/);
  assert.match(panel, /from "\.\/workspaceInspector"/);
  // CSRF 只能有一份实现：从 workspaceApi 导入，不得自己解析 cookie。
  assert.match(api, /import \{ csrfToken \} from "\.\/workspaceApi(?:\.ts)?"/);
  assert.doesNotMatch(api, /wa_csrf/);
  assert.doesNotMatch(panel, /wa_csrf/);
  // 密钥输入必须遮蔽且不进浏览器自动填充。
  assert.match(panel, /type="password"/);
  assert.match(panel, /autoComplete="off"/);
});

test("workbench keeps the two panels mutually exclusive", async () => {
  const source = await readFile(new URL("../src/Workbench.tsx", import.meta.url), "utf8");

  assert.match(source, /SettingsPanel/);
  assert.match(source, /settingsOpen/);
  // 主区只有一处：打开设置必须关掉记忆，反之亦然，否则「返回会话」含义含糊。
  assert.match(source, /setSettingsOpen\(false\)/);
  assert.match(source, /setMemoryOpen\(false\)/);
  assert.match(source, /aria-pressed=\{workspace\.settingsOpen\}/);
  assert.match(source, /aria-label=\{workspace\.settingsOpen \? "返回会话" : "设置"\}/);
});

test("P7-S10 endpoint input mirrors the backend rules: https only, local exceptions, no userinfo", () => {
  assert.equal(validateEndpointInput("https://api.example.com/v1", "gpt-4.1-mini"), null);
  assert.equal(validateEndpointInput("http://127.0.0.1:11434/v1", "qwen3:8b"), null);
  assert.equal(validateEndpointInput("http://localhost:8000/v1", "Qwen/Qwen2.5-72B-Instruct"), null);
  assert.equal(validateEndpointInput("http://local-model:11434/v1", "qwen3:8b"), null);
  assert.match(validateEndpointInput("http://api.example.com/v1", "m"), /https/);
  assert.match(validateEndpointInput("https://user:pw@api.example.com/v1", "m"), /用户名或密码/);
  assert.match(validateEndpointInput("https://api.example.com/v1#x", "m"), /#/);
  assert.match(validateEndpointInput("api.example.com", "m"), /完整地址/);
  assert.match(validateEndpointInput("https://api.example.com/v1", "bad model"), /模型名/);
});

test("P7-S10 compatibility status says what happens next, and the default endpoint is exempt", () => {
  assert.equal(compatibilityView("default_exempt").tone, "succeeded");
  assert.match(compatibilityView("default_exempt").message, /免检/);
  assert.equal(compatibilityView("verified").tone, "succeeded");
  assert.equal(compatibilityView("unverified").tone, "not_configured");
  assert.match(compatibilityView("unverified").message, /WEB_MODEL_NOT_VERIFIED/);
  assert.match(lockedReasonMessage("deployment"), /部署配置/);
  assert.equal(lockedReasonMessage(null), null);
  assert.match(endpointSourceLabel("panel"), /本面板/);
  // 密钥只发给所属服务商：专用 Key 的说明写明端点改了就必须填
  assert.match(credentialHint("WATER_AGENT_LLM_API_KEY"), /不是 DeepSeek 时必填/);
  assert.match(credentialHint("DASHSCOPE_API_KEY"), /重排与 PDF 视觉审阅/);
  assert.match(credentialHint("DASHSCOPE_API_KEY"), /关键词模式/);
});

test("P7-S10 the endpoint card takes the key write-only, confirms the billed check and fetches models server-side", async () => {
  const panel = await readFile(new URL("../src/SettingsPanel.tsx", import.meta.url), "utf8");
  assert.match(panel, /LlmEndpointCard/);
  assert.match(panel, /window\.confirm\(/);
  const card = panel.slice(panel.indexOf("function LlmEndpointCard"), panel.indexOf('const MANUAL_OPTION'));
  // Key 输入框：遮蔽、不进自动填充，写进只写不读的槽位，保存后立刻清空
  assert.match(card, /type="password"/);
  assert.match(card, /autoComplete="off"/);
  assert.match(card, /api\.putCredential\(slot, typedKey\)/);
  assert.match(card, /setApiKey\(""\)/);
  // 获取模型：用表单里未保存的地址与 Key 交给服务端取，浏览器不直连端点
  assert.match(card, /endpointApi\.listModels\(baseUrl\.trim\(\), apiKey\.trim\(\) \|\| undefined\)/);
  assert.doesNotMatch(card, /fetch\(/);
});
