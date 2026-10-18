import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { ModelCatalogApi } from "../src/modelChoiceApi.ts";
import {
  FOLLOW_DEFAULT,
  TIER_NOTE,
  draftChanged,
  draftFromView,
  draftToPayload,
  followDefaultLabel,
  messageModelValue,
  modelCardView,
  modelErrorMessage,
  modelFromFinalPayload,
  modelOptionLabel,
  newDraftRow,
  selectableModels,
  sessionDefaultProblem,
  showModelPicker,
  thinkingLabel,
  tierLabel,
  unselectableReason,
  validateDraft,
} from "../src/modelChoiceState.ts";
import { WorkspaceApi } from "../src/workspaceApi.ts";
import { applyRunEvent, emptyRunState } from "../src/workspaceState.ts";

const threadId = "55555555-5555-4555-8555-555555555555";
const runId = "44444444-4444-4444-8444-444444444444";

const model = (id, extra = {}) => ({
  id,
  label: `模型 ${id}`,
  tier: "fast",
  thinking: "default",
  verified: true,
  selectable: true,
  ...extra,
});
const catalog = (models, extra = {}) => ({ can_manage: false, editable: false, locked_reason: null, models, error: null, ...extra });

function stubFetch(handler) {
  const original = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    calls.push({ url, method: init.method ?? "GET", body: init.body ? JSON.parse(init.body) : undefined });
    return new Response(JSON.stringify({ version: "v1", ok: true, data: handler(url, init) }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  return { calls, restore: () => { globalThis.fetch = original; } };
}

test("P7-S15 the picker renders only when the catalog has something selectable", () => {
  assert.equal(showModelPicker(null), false);
  assert.equal(showModelPicker(catalog([])), false);
  // 未验证 / 思考型 / 已停用的条目不可选，全是它们时同样不渲染
  assert.equal(showModelPicker(catalog([model("a", { verified: false, selectable: false })])), false);
  assert.equal(showModelPicker(catalog([model("t", { thinking: "roundtrip", selectable: false })])), false);
  assert.equal(showModelPicker(catalog([model("a")], { error: { code: "WEB_MODEL_CATALOG_INVALID", message: "x" } })), false);
  // 部署锁定：即便条目标了可选，也一个都不给
  assert.deepEqual(selectableModels(catalog([model("a")], { locked_reason: "deployment" })), []);
  assert.equal(showModelPicker(catalog([model("a"), model("b", { selectable: false })])), true);
  assert.deepEqual(selectableModels(catalog([model("a"), model("b", { selectable: false })])).map((m) => m.id), ["a"]);
});

test("P7-S15 thinking:roundtrip shows as unsupported and is never selectable", () => {
  const entry = model("t", { thinking: "roundtrip", selectable: false, enabled: true });
  assert.match(unselectableReason(entry, null), /暂不支持/);
  assert.match(thinkingLabel("roundtrip"), /暂不支持/);
  assert.match(unselectableReason(model("u", { verified: false, selectable: false }), null), /兼容性检查/);
  assert.match(unselectableReason(model("d", { enabled: false, selectable: false }), null), /停用/);
  assert.match(unselectableReason(model("a"), "deployment"), /部署/);
  assert.equal(unselectableReason(model("a"), null), null);
});

test("P7-S15 wording never suggests the advanced tier is more accurate", () => {
  assert.equal(tierLabel("fast"), "快速档");
  assert.equal(tierLabel("advanced"), "进阶档");
  assert.match(TIER_NOTE, /不代表回答更准确/);
  for (const text of [tierLabel("fast"), tierLabel("advanced"), TIER_NOTE, modelOptionLabel(model("a", { tier: "advanced" }))]) {
    assert.doesNotMatch(text, /高级|更强|更智能|推荐/);
  }
});

test("P7-S15 the request body carries model only when the user picked one", async () => {
  const stub = stubFetch(() => ({ run_id: runId, thread_id: threadId, status: "queued" }));
  try {
    const api = new WorkspaceApi();
    await api.sendMessage(threadId, "问题");
    await api.sendMessage(threadId, "问题", [], messageModelValue(FOLLOW_DEFAULT));
    await api.sendMessage(threadId, "问题", ["u-1"], messageModelValue("fast-1"));
  } finally {
    stub.restore();
  }
  assert.deepEqual(stub.calls.map((call) => call.body), [
    { message: "问题" },
    { message: "问题" },
    { message: "问题", attachments: ["u-1"], model: "fast-1" },
  ]);
  for (const call of stub.calls) {
    assert.equal("base_url" in call.body || "api_key" in call.body, false);
  }
});

test("P7-S15 session default is set and cleared through PATCH /api/sessions/{id}", async () => {
  const stub = stubFetch(() => ({ thread_id: threadId, project_id: null, title: "t", created_at: "", updated_at: "" }));
  try {
    const api = new WorkspaceApi();
    await api.setSessionModel(threadId, "fast-1");
    await api.setSessionModel(threadId, null);
  } finally {
    stub.restore();
  }
  assert.deepEqual(stub.calls.map((call) => [call.method, call.url, call.body]), [
    ["PATCH", `/api/sessions/${threadId}`, { model: "fast-1" }],
    ["PATCH", `/api/sessions/${threadId}`, { model: null }],
  ]);
});

test("P7-S15 catalog API sends only identity fields and checks one model at a time", async () => {
  const stub = stubFetch(() => catalog([]));
  try {
    const api = new ModelCatalogApi();
    await api.get();
    await api.put([{ id: "a", label: "A", tier: "fast", thinking: "off", enabled: true }]);
    await api.runCompatibility("a");
  } finally {
    stub.restore();
  }
  assert.deepEqual(stub.calls.map((call) => [call.method, call.url]), [
    ["GET", "/api/settings/llm-models"],
    ["PUT", "/api/settings/llm-models"],
    ["POST", "/api/settings/llm-endpoint/compatibility"],
  ]);
  assert.equal(JSON.stringify(stub.calls[1].body).includes("base_url"), false);
  assert.deepEqual(stub.calls[2].body, { model: "a" });
});

test("P7-S15 the model chosen for a run comes from finalize and is absent otherwise", () => {
  const event = (payload) => ({ event_id: "9", type: "finalize", run_id: runId, thread_id: threadId, time: "", payload_version: "v1", payload });
  const chosen = applyRunEvent(emptyRunState(), event({ status: "succeeded", answer: "好", citations: [], model: "fast-1" }));
  assert.deepEqual(chosen.model, { id: "fast-1" });
  const none = applyRunEvent(emptyRunState(), event({ status: "succeeded", answer: "好", citations: [] }));
  assert.equal(none.model, null);
  assert.equal(emptyRunState().model, null);
  assert.equal(modelFromFinalPayload({ model: "" }), null);
  assert.equal(modelCardView(null, null), null);
  const view = modelCardView({ id: "fast-1" }, catalog([model("fast-1", { label: "快速模型" })]));
  assert.equal(view.title, "快速模型");
  assert.match(view.detail, /fast-1 · 快速档/);
  assert.equal(modelCardView({ id: "gone" }, catalog([])).title, "gone");
});

test("P7-S15 a stale session default is called out, never silently replaced", () => {
  const view = catalog([model("a")]);
  assert.equal(sessionDefaultProblem(view, null), null);
  assert.equal(sessionDefaultProblem(view, "a"), null);
  assert.match(sessionDefaultProblem(view, "gone"), /不会悄悄换成别的模型/);
  assert.match(sessionDefaultProblem(catalog([model("a")], { locked_reason: "local_model" }), "a"), /不再生效/);
  assert.equal(followDefaultLabel(view, null), "沿用全局模型");
  assert.match(followDefaultLabel(view, "a"), /沿用本会话默认：模型 a/);
});

test("P7-S15 catalog editor validates rows and keeps price and role limits it does not edit", () => {
  const view = catalog(
    [
      model("a", { enabled: true, allowed_roles: ["admin"], price: { input_per_1m: 1, output_per_1m: 2 } }),
      model("b", { enabled: false }),
    ],
    { can_manage: true, editable: true },
  );
  const rows = draftFromView(view);
  assert.equal(draftChanged(rows, view), false);
  const payload = draftToPayload(rows);
  assert.deepEqual(payload[0].allowed_roles, ["admin"]);
  assert.deepEqual(payload[0].price, { input_per_1m: 1, output_per_1m: 2 });
  assert.equal(payload[1].enabled, false);
  assert.equal(JSON.stringify(payload).includes("base_url"), false);
  assert.equal(JSON.stringify(payload).includes("api_key"), false);

  rows[1].enabled = true;
  assert.equal(draftChanged(rows, view), true);

  const fresh = { ...newDraftRow(), id: "bad name!", label: "x" };
  assert.match(validateDraft([...rows, fresh]), /模型 id 只能含/);
  assert.match(validateDraft([...rows, { ...fresh, id: "a" }]), /重复/);
  assert.match(validateDraft([...rows, { ...fresh, id: "c", label: " " }]), /显示名称/);
  assert.match(validateDraft([...rows, { ...fresh, id: "c", label: "长".repeat(41) }]), /过长/);
  assert.equal(validateDraft([...rows, { ...fresh, id: "c" }]), null);
  // 与后端一致：最多 32 个
  assert.match(validateDraft(Array.from({ length: 33 }, (_, i) => ({ ...fresh, id: `m${i}` }))), /最多 32/);
  assert.match(modelErrorMessage("WEB_MODEL_THINKING_UNSUPPORTED"), /尚不支持/);
  assert.equal(modelErrorMessage("SOMETHING_ELSE", "后端说明"), "后端说明");
});

test("P7-S15 wiring: picker in the composer bar, admin catalog inside the models page, no new sub-page", async () => {
  const read = (name) => readFile(new URL(`../src/${name}`, import.meta.url), "utf8");
  const components = await read("workspaceComponents.tsx");
  const choice = await read("ModelChoice.tsx");
  const panel = await read("ModelCatalogPanel.tsx");
  const settings = await read("SettingsPanel.tsx");
  const state = await read("settingsState.ts");
  const controller = await read("useWorkspaceController.ts");
  const inspector = await read("workspaceInspector.tsx");
  // 选择器在 composer-bar 里，禁用条件与输入框一致
  const bar = components.slice(components.indexOf('className="composer-bar"'), components.indexOf('className="composer-scope"'));
  assert.match(bar, /<ComposerModelSelect/);
  assert.match(bar, /disabled=\{!session \|\| streaming \|\| waiting\}/);
  assert.match(components, /<textarea[\s\S]*?disabled=\{!session \|\| streaming \|\| waiting\}/);
  // 类名不与设置页的 .model-picker 冲突
  assert.doesNotMatch(choice, /model-picker/);
  assert.match(choice, /if \(!showModelPicker\(catalog\)\) return null;/);
  // 管理员区：成员不渲染；放在「模型与密钥」页里，副栏仍是三页
  assert.match(panel, /if \(!view \|\| !view\.can_manage\) return null;/);
  assert.match(settings, /<ModelCatalogPanel \/>/);
  assert.doesNotMatch(state, /id: "catalog"/);
  assert.doesNotMatch(panel, /fetch\(/);
  // 发送与会话默认走控制器；Inspector 有「本次模型」
  assert.match(controller, /messageModelValue\(messageModel\)/);
  assert.match(inspector, /<ModelUsedSection model=\{run\.model\}/);
});
