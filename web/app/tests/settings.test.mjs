import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  credentialRows,
  formatUpdatedAt,
  modelReadinessMessage,
  modelReadinessTone,
  sourceLabel,
  validateCredentialInput,
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
  assert.match(rows[0].purpose, /对话生成/);
  assert.match(rows[1].purpose, /检索|重排/);
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
  assert.match(api, /import \{ csrfToken \} from "\.\/workspaceApi"/);
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
