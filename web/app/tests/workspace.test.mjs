import assert from "node:assert/strict";
import test from "node:test";

import { parseSseBlock, streamRunEvents, WorkspaceApi, WorkspaceApiError } from "../src/workspaceApi.ts";
import { applyRunEvent, emptyRunState } from "../src/workspaceState.ts";

const runId = "44444444-4444-4444-8444-444444444444";
const threadId = "55555555-5555-4555-8555-555555555555";

function event(type, payload, eventId = "1") {
  return {
    event_id: eventId,
    type,
    run_id: runId,
    thread_id: threadId,
    time: "2026-09-13T08:00:00Z",
    payload_version: "v1",
    payload,
  };
}

test("projects real tool lifecycle and only final validated source ids", () => {
  let state = emptyRunState();
  state = applyRunEvent(state, event("tool_started", { tool_name: "calc_velocity" }));
  assert.equal(state.tools[0].status, "running");
  state = applyRunEvent(state, event("tool_result", {
    tool_name: "calc_velocity",
    status: "succeeded",
    calculation: { programmatic: true },
  }, "2"));
  assert.equal(state.tools[0].status, "succeeded");
  assert.deepEqual(state.citations, []);
  state = applyRunEvent(state, event("finalize", {
    status: "succeeded",
    answer: "后端回答",
    citations: [{ source_id: "GB50014-2021 5.2.1" }, { source_id: "" }],
    calculations: [{ tool_name: "calc_velocity", programmatic: true, values: { velocity_mps: 0.42 }, units: { velocity_mps: "m/s" }, assumptions: [] }],
  }, "3"));
  assert.equal(state.answer, "后端回答");
  assert.deepEqual(state.citations, [{ source_id: "GB50014-2021 5.2.1" }]);
  assert.equal(state.calculations[0].programmatic, true);
  assert.equal(state.lastEventId, "3");
});

test("renders a real failed tool result as failed, never as success", () => {
  const state = applyRunEvent(emptyRunState(), event("tool_result", {
    tool_name: "read_drawing",
    status: "failed",
    code: "DRAWING_UNSUPPORTED_FORMAT",
  }));
  assert.equal(state.tools[0].status, "failed");
  assert.equal(state.tools[0].code, "DRAWING_UNSUPPORTED_FORMAT");
});

test("parses SSE envelopes without accepting a different run", () => {
  const parsed = parseSseBlock(`id: 4\nevent: finalize\ndata: ${JSON.stringify(event("finalize", { status: "failed" }, "4"))}`);
  assert.equal(parsed.type, "finalize");
  assert.equal(parsed.event_id, "4");
  assert.equal(parseSseBlock("event: ping\n\n"), null);
});

test("reconnect request carries the last event id and consumes real SSE data", async () => {
  const requests = [];
  const payload = event("finalize", { status: "succeeded" }, "9");
  globalThis.fetch = async (input, init = {}) => {
    requests.push({ url: String(input), headers: init.headers });
    const body = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(`id: 9\nevent: finalize\ndata: ${JSON.stringify(payload)}\n\n`));
        controller.close();
      },
    });
    return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
  };
  const received = [];
  await streamRunEvents(runId, { lastEventId: "8", onEvent: (item) => received.push(item) });
  assert.equal(requests[0].headers.get("Last-Event-ID"), "8");
  assert.equal(received[0].event_id, "9");
  delete globalThis.fetch;
});

test("API preserves server error codes and never invents authorization", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({
    version: "v1",
    ok: false,
    error: { code: "WEB_NOT_FOUND", message: "Resource not found" },
  }), { status: 404, headers: { "content-type": "application/json" } });
  await assert.rejects(
    () => new WorkspaceApi().getTrace(runId),
    (error) => error instanceof WorkspaceApiError && error.code === "WEB_NOT_FOUND" && error.status === 404,
  );
  delete globalThis.fetch;
});

test("run-level failure message is surfaced, not just a failed badge", () => {
  const state = applyRunEvent(emptyRunState(), event("finalize", { status: "failed", error: "agent run failed" }));
  assert.equal(state.status, "failed");
  assert.deepEqual(state.errors, ["agent run failed"]);
});

// ---------------------------------------------------------------------------
// P5-S49: the conversation keeps every turn; failures stay visible
// ---------------------------------------------------------------------------

import { archiveRun, mergeHistory } from "../src/workspaceState.ts";

function finished(status, answer) {
  return applyRunEvent(emptyRunState(), event("finalize", { status, answer, errors: status === "failed" ? ["空引用收口失败"] : [] }));
}

test("archives the previous answer before the next question is sent", () => {
  const first = [{ role: "user", text: "能读 DXF 吗" }];
  const archived = archiveRun(first, finished("succeeded", "网页端暂不支持 DXF。"));
  assert.deepEqual(archived, [
    { role: "user", text: "能读 DXF 吗" },
    { role: "agent", text: "网页端暂不支持 DXF。", status: "succeeded" },
  ]);
});

test("a failed run with an answer is archived with its failed status, never as a plain answer", () => {
  const archived = archiveRun([], finished("failed", "未通过校验的回答"));
  assert.equal(archived[0].status, "failed");
  assert.equal(archived[0].text, "未通过校验的回答");
});

test("an unfinished run is not archived", () => {
  const running = applyRunEvent(emptyRunState(), event("token", { text: "半截" }));
  assert.deepEqual(archiveRun([], running), []);
});

test("history drops the latest turn's answer because the run replay renders it", () => {
  const history = [
    { role: "user", text: "问 1" },
    { role: "agent", text: "答 1" },
    { role: "user", text: "问 2" },
    { role: "agent", text: "答 2" },
  ];
  assert.deepEqual(mergeHistory(history, true).map((m) => m.text), ["问 1", "答 1", "问 2"]);
  assert.deepEqual(mergeHistory(history, false).map((m) => m.text), ["问 1", "答 1", "问 2", "答 2"]);
});

test("a withheld history answer stays withheld", () => {
  const [, agent] = mergeHistory([{ role: "user", text: "问" }, { role: "agent", text: "", withheld: true }], false);
  assert.equal(agent.withheld, true);
  assert.equal(agent.status, "blocked");
});

test("sendMessage only sends attachments when there are some", async () => {
  const bodies = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (_url, init) => {
    bodies.push(JSON.parse(init.body));
    return new Response(JSON.stringify({ version: "v1", ok: true, data: { run_id: runId, thread_id: threadId, status: "queued" } }), { status: 202, headers: { "Content-Type": "application/json" } });
  };
  try {
    const api = new WorkspaceApi();
    await api.sendMessage(threadId, "问题");
    await api.sendMessage(threadId, "读这个", ["u-1"]);
  } finally {
    globalThis.fetch = originalFetch;
  }
  assert.deepEqual(bodies, [{ message: "问题" }, { message: "读这个", attachments: ["u-1"] }]);
});

test("answers render through Markdown without raw HTML", async () => {
  const { readFile } = await import("node:fs/promises");
  const markdown = await readFile(new URL("../src/AnswerMarkdown.tsx", import.meta.url), "utf8");
  const components = await readFile(new URL("../src/workspaceComponents.tsx", import.meta.url), "utf8");
  assert.match(markdown, /skipHtml/);
  assert.doesNotMatch(markdown, /rehype-raw|dangerouslySetInnerHTML/);
  assert.match(components, /<AnswerMarkdown text=\{answer\} \/>/);
  // 有回答的失败运行也必须显示横幅（此前只在"没有回答"时才显示错误）
  assert.match(components, /isTerminal\(run\.status\) && <UnverifiedBanner/);
});

// ---------------------------------------------------------------------------
// Replay after a server restart: the event log is gone, the history is not
// ---------------------------------------------------------------------------

import { replayUnavailable } from "../src/workspaceState.ts";

test("a finished run whose replay is gone falls back to the full history", () => {
  const history = [{ role: "user", text: "问" }, { role: "agent", text: "答" }];
  assert.deepEqual(replayUnavailable(history, "failed").map((m) => m.text), ["问", "答"]);
});

test("an unfinished run interrupted by a restart says so instead of spinning", () => {
  const messages = replayUnavailable([{ role: "user", text: "问" }], "running");
  assert.equal(messages.length, 2);
  assert.equal(messages[1].status, "failed");
  assert.match(messages[1].text, /服务重启中断/);
});

test("the stream controller treats 404 as permanent and caps other retries", async () => {
  const { readFile } = await import("node:fs/promises");
  const source = await readFile(new URL("../src/useWorkspaceController.ts", import.meta.url), "utf8");
  assert.match(source, /error\.status === 404/);
  assert.match(source, /MAX_STREAM_RETRIES/);
  assert.doesNotMatch(source, /setTimeout\(\(\) => void connectRun\(runId, cursor\.current\), 800\)/);
});
