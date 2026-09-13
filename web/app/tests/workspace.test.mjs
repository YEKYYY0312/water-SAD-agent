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
