// P7-S7-A07：人工确认卡片（确认请求解析、运行态、决定接口、刷新后从历史恢复）。
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  ConfirmationApi,
  MAX_CONFIRMATION_REASON_LENGTH,
  WAITING_CONFIRMATION,
  confirmationErrorMessage,
  isWaitingConfirmation,
  parseConfirmationRequest,
} from "../src/confirmationApi.ts";
import { WorkspaceApiError } from "../src/workspaceApi.ts";
import {
  applyRunEvent,
  emptyRunState,
  replayUnavailable,
  runStateFromPendingConfirmation,
} from "../src/workspaceState.ts";

const runId = "77777777-7777-4777-8777-777777777777";
const threadId = "88888888-8888-4888-8888-888888888888";

const request = (overrides = {}) => ({
  confirmation_id: "cid-1",
  tool_name: "mcp_cad_write",
  server_id: "cad",
  remote_tool: "write",
  arguments_text: '{\n  "path": "<script>alert(1)</script>"\n}',
  arguments_redacted: false,
  arguments_truncated: false,
  message: "Agent 请求调用 MCP server 'cad' 的工具 'write'。",
  ...overrides,
});

function event(type, payload, eventId = "1") {
  return { event_id: eventId, type, run_id: runId, thread_id: threadId, time: "2026-10-07T08:00:00Z", payload_version: "v1", payload };
}

test("confirmation requests are parsed strictly", () => {
  const parsed = parseConfirmationRequest(request({ arguments_redacted: "yes", run_id: runId, error: "旧决定无效" }));
  assert.equal(parsed.confirmation_id, "cid-1");
  assert.equal(parsed.arguments_redacted, false, "只有严格 true 才算已脱敏");
  assert.equal(parsed.run_id, runId);
  assert.equal(parsed.error, "旧决定无效");
  assert.equal(parseConfirmationRequest(request({ confirmation_id: "" })), null);
  assert.equal(parseConfirmationRequest(request({ tool_name: 7 })), null);
  assert.equal(parseConfirmationRequest(null), null);
  assert.equal(parseConfirmationRequest(request({ remote_tool: "" })).remote_tool, "mcp_cad_write");
});

test("confirmation_required pauses the run and shows the card; the decision clears it", () => {
  let state = applyRunEvent(emptyRunState(), event("draft_delta", { text: "我来写入。", offset: 0 }));
  state = applyRunEvent(state, event("confirmation_required", request(), "2"));
  assert.equal(state.status, WAITING_CONFIRMATION);
  assert.ok(isWaitingConfirmation(state.status));
  assert.equal(state.confirmation.confirmation_id, "cid-1");
  assert.equal(state.draft, "", "停在确认门时草稿作废");
  assert.equal(applyRunEvent(state, event("confirmation_required", { tool_name: "x" }, "3")).confirmation.confirmation_id, "cid-1");

  state = applyRunEvent(state, event("confirmation_decided", { confirmation_id: "cid-1", decision: "approved" }, "4"));
  assert.equal(state.confirmation, null);
  state = applyRunEvent(state, event("run_queued", { status: "queued" }, "5"));
  assert.equal(state.status, "queued");
  state = applyRunEvent(state, event("run_started", { status: "running" }, "6"));
  assert.equal(state.status, "running");
});

test("a reload restores the card from history, only for the run that is waiting", () => {
  const latest = { run_id: runId, thread_id: threadId, status: WAITING_CONFIRMATION };
  const restored = runStateFromPendingConfirmation(request({ run_id: runId }), latest);
  assert.equal(restored.runId, runId);
  assert.equal(restored.status, WAITING_CONFIRMATION);
  assert.equal(restored.confirmation.confirmation_id, "cid-1");
  assert.equal(runStateFromPendingConfirmation(request({ run_id: "another-run" }), latest), null);
  assert.equal(runStateFromPendingConfirmation(request(), { ...latest, status: "succeeded" }), null);
  assert.equal(runStateFromPendingConfirmation(null, latest), null);

  // 停在确认门不是"被重启打断"：不补中断说明
  const messages = replayUnavailable([{ role: "user", text: "写入图纸" }], WAITING_CONFIRMATION);
  assert.deepEqual(messages.map((m) => m.text), ["写入图纸"]);
});

test("the decision is posted with CSRF, strict bool and a trimmed reason", async () => {
  globalThis.document = { cookie: "wa_csrf=token-1" };
  const seen = [];
  globalThis.fetch = async (input, init) => {
    seen.push({ url: String(input), init });
    return new Response(JSON.stringify({
      version: "v1",
      ok: true,
      data: { run_id: runId, thread_id: threadId, status: "queued", event_cursor: "12" },
    }), { status: 202 });
  };
  const accepted = await new ConfirmationApi().decide(runId, {
    confirmation_id: "cid-1",
    approved: false,
    reason: `  ${"理".repeat(MAX_CONFIRMATION_REASON_LENGTH + 20)}  `,
  });
  assert.equal(accepted.event_cursor, "12");
  assert.equal(seen[0].url, `/api/runs/${runId}/confirmation`);
  assert.equal(seen[0].init.method, "POST");
  assert.equal(seen[0].init.headers.get("X-CSRF-Token"), "token-1");
  const body = JSON.parse(seen[0].init.body);
  assert.equal(body.approved, false);
  assert.equal(body.reason.length, MAX_CONFIRMATION_REASON_LENGTH);

  await new ConfirmationApi().decide(runId, { confirmation_id: "cid-1", approved: true, reason: "   " });
  assert.equal(JSON.parse(seen[1].init.body).reason, null);
  delete globalThis.fetch;
  delete globalThis.document;
});

test("refusals keep the server code and have actionable copy", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({
    version: "v1",
    ok: false,
    error: { code: "WEB_CONFIRMATION_STALE", message: "This run is not waiting for this confirmation" },
  }), { status: 409 });
  await assert.rejects(
    () => new ConfirmationApi().decide(runId, { confirmation_id: "cid-1", approved: true }),
    (error) => error instanceof WorkspaceApiError && error.code === "WEB_CONFIRMATION_STALE" && error.status === 409,
  );
  globalThis.fetch = async () => { throw new TypeError("network down"); };
  await assert.rejects(
    () => new ConfirmationApi().decide(runId, { confirmation_id: "cid-1", approved: true }),
    (error) => error instanceof WorkspaceApiError && error.code === "WEB_REQUEST_FAILED",
  );
  delete globalThis.fetch;

  for (const code of ["WEB_CONFIRMATION_STALE", "WEB_CONFIRMATION_PENDING", "WEB_FORBIDDEN", "WEB_NOT_FOUND", "WEB_RUN_IN_PROGRESS"]) {
    assert.notEqual(confirmationErrorMessage(code), "确认没有提交成功", code);
  }
  assert.equal(confirmationErrorMessage("NEW_CODE", "原话"), "原话");
});

test("the card renders arguments as escaped plain text and only uses style tokens", async () => {
  const card = await readFile(new URL("../src/ConfirmationCard.tsx", import.meta.url), "utf8");
  assert.doesNotMatch(card, /dangerouslySetInnerHTML/);
  assert.match(card, /<pre className="confirmation-args">\{/);
  assert.match(card, /批准执行/);
  assert.match(card, /拒绝/);
  assert.match(card, /arguments_redacted/);
  assert.match(card, /arguments_truncated/);
  assert.match(card, /maxLength=\{MAX_CONFIRMATION_REASON_LENGTH\}/);

  const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
  const rules = styles.split("\n").filter((line) => line.includes(".confirmation-"));
  assert.ok(rules.length > 0, "确认卡片有样式");
  for (const line of rules) {
    assert.doesNotMatch(line, /#[0-9a-fA-F]{3,8}\b|rgb\(|hsl\(/, `只用 token，不写字面色值：${line}`);
  }
});

test("the controller pauses on waiting_confirmation and resumes from the server cursor", async () => {
  const controller = await readFile(new URL("../src/useWorkspaceController.ts", import.meta.url), "utf8");
  // 停在确认门时流正常结束，不当成断线去重连
  assert.match(controller, /!isWaitingConfirmation\(runStatus\.current\)/);
  // 决定之后按 202 返回的 event_cursor 重新订阅（不是客户端自己的游标）
  assert.match(controller, /connectRun\(accepted\.run_id, accepted\.event_cursor\)/);
  assert.match(controller, /runStateFromPendingConfirmation\(/);
  const components = await readFile(new URL("../src/workspaceComponents.tsx", import.meta.url), "utf8");
  assert.match(components, /<ConfirmationCard/);
  assert.match(components, /waiting_confirmation: "等待确认"/);
});
