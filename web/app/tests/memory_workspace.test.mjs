import assert from "node:assert/strict";
import test, { afterEach, beforeEach } from "node:test";

import { MemoryApiClient, MemoryApiError } from "../src/memoryApi.ts";
import {
  removeMemory,
  replaceMemory,
  selectCurrentAnswerMemories,
  upsertMemory,
} from "../src/memoryWorkspaceState.ts";

const memoryId = "11111111-1111-4111-8111-111111111111";
const projectId = "22222222-2222-4222-8222-222222222222";
const candidateId = "33333333-3333-4333-8333-333333333333";
const runId = "44444444-4444-4444-8444-444444444444";

const memory = {
  memory_id: memoryId,
  scope_type: "user",
  project_id: null,
  kind: "user_preference",
  semantic_key: "default unit",
  content: "默认使用 SI 单位",
  status: "active",
  revision: 1,
  created_at: "2026-09-13T08:00:00Z",
  updated_at: "2026-09-13T08:00:00Z",
  expires_at: null,
  source_thread_id: "55555555-5555-4555-8555-555555555555",
};

function response(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

let requests;
let fetchHandler;
let listCalls;

beforeEach(() => {
  requests = [];
  listCalls = 0;
  fetchHandler = async (input, init = {}) => {
    const request = {
      url: String(input),
      method: init.method ?? "GET",
      body: init.body ? JSON.parse(String(init.body)) : undefined,
    };
    requests.push(request);
    return response({ version: "v1", ok: true, data: {} });
  };
  globalThis.fetch = (input, init) => fetchHandler(input, init);
});

afterEach(() => {
  delete globalThis.fetch;
});

test("completes remember, recall, correct, use, and forget through real API boundaries", async () => {
  fetchHandler = async (input, init = {}) => {
    const url = String(input);
    const method = init.method ?? "GET";
    requests.push({ url, method, body: init.body ? JSON.parse(String(init.body)) : undefined });

    if (method === "POST" && url.endsWith("/api/memories/candidates")) {
      return response({
        version: "v1",
        ok: true,
        data: {
          candidate_id: candidateId,
          operation: "create",
          disposition: "ready",
          scope_type: "user",
          project_id: null,
          kind: "user_preference",
          semantic_key: "default unit",
          content: "默认使用 SI 单位",
          content_hash: "hash-v1",
          requires_confirmation: true,
        },
      });
    }
    if (method === "POST" && url.endsWith("/api/memories")) {
      return response({ version: "v1", ok: true, data: { action: "created", memory } }, 201);
    }
    if (method === "GET" && url.startsWith("/api/memories?")) {
      listCalls += 1;
      if (listCalls > 1) return response({ version: "v1", ok: true, data: { memories: [] } });
      return response({
        version: "v1",
        ok: true,
        data: { memories: [memory], current_answer: { memory_ids: [memoryId], run_id: runId } },
      });
    }
    if (method === "PATCH" && url.endsWith(`/api/memories/${memoryId}`)) {
      return response({
        version: "v1",
        ok: true,
        data: {
          ...memory,
          content: "默认使用公制 SI 单位",
          revision: 2,
          updated_at: "2026-09-13T09:00:00Z",
        },
      });
    }
    if (method === "GET" && url.endsWith(`/api/memories/${memoryId}/usage`)) {
      return response({
        version: "v1",
        ok: true,
        data: { usage: [{ memory_id: memoryId, run_id: runId, score: 1, created_at: "2026-09-13T09:05:00Z" }] },
      });
    }
    if (method === "DELETE" && url.endsWith(`/api/memories/${memoryId}`)) {
      return response({
        version: "v1",
        ok: true,
        data: { memory_id: memoryId, status: "forgotten", revision: 3 },
      });
    }
    throw new Error(`unexpected request: ${method} ${url}`);
  };

  const api = new MemoryApiClient();
  const candidate = await api.createCandidate({
    scope_type: "user",
    kind: "user_preference",
    semantic_key: "default unit",
    content: "默认使用 SI 单位",
    project_id: null,
    expires_at: null,
  });
  const created = await api.confirmCandidate(candidate.candidate_id, {
    confirmed: true,
    content_hash: candidate.content_hash,
  });
  const recalled = await api.listMemories({ query: "默认单位" });
  const corrected = await api.correctMemory(memoryId, 1, "默认使用公制 SI 单位");
  const usage = await api.getUsage(memoryId);
  const forgotten = await api.forgetMemory(memoryId, 2);
  const afterForget = await api.listMemories({ query: "默认单位" });

  assert.equal(created.memory?.content, memory.content);
  assert.equal(recalled.memories[0].memory_id, memoryId);
  assert.equal(recalled.currentAnswer?.memory_ids[0], memoryId);
  assert.equal(corrected.revision, 2);
  assert.equal(usage[0].run_id, runId);
  assert.equal(forgotten.status, "forgotten");
  assert.deepEqual(afterForget.memories, []);
  assert.equal(requests[0].body.user_id, undefined);
  assert.equal(requests[0].body.principal, undefined);
  assert.equal(requests.filter((item) => item.method === "DELETE").length, 1);
});

test("keeps conflict preview until the user explicitly chooses a resolution", async () => {
  fetchHandler = async (input, init = {}) => {
    const url = String(input);
    requests.push({ url, method: init.method ?? "GET", body: init.body ? JSON.parse(String(init.body)) : undefined });
    if ((init.method ?? "GET") === "POST" && url.endsWith("/api/memories/candidates")) {
      return response({
        version: "v1",
        ok: true,
        data: {
          candidate_id: candidateId,
          operation: "create",
          disposition: "conflict",
          scope_type: "user",
          project_id: null,
          kind: "user_preference",
          semantic_key: "default unit",
          content: "默认使用英制单位",
          content_hash: "hash-new",
          requires_confirmation: true,
          conflict: {
            memory_id: memoryId,
            revision: 1,
            current_content: "默认使用 SI 单位",
            current_content_hash: "hash-old",
            proposed_content: "默认使用英制单位",
            proposed_content_hash: "hash-new",
          },
        },
      });
    }
    if ((init.method ?? "GET") === "POST" && url.endsWith("/api/memories")) {
      return response({ version: "v1", ok: true, data: { action: "corrected", memory } });
    }
    throw new Error(`unexpected request: ${init.method ?? "GET"} ${url}`);
  };

  const api = new MemoryApiClient();
  const candidate = await api.createCandidate({
    scope_type: "user",
    kind: "user_preference",
    semantic_key: "default unit",
    content: "默认使用英制单位",
    project_id: null,
    expires_at: null,
  });

  assert.equal(candidate.disposition, "conflict");
  assert.equal(candidate.conflict?.current_content, "默认使用 SI 单位");
  await api.confirmCandidate(candidate.candidate_id, {
    confirmed: true,
    content_hash: candidate.content_hash,
    resolution: "correct_existing",
    expected_revision: candidate.conflict?.revision,
  });
  assert.equal(requests[1].body.confirmation.resolution, "correct_existing");
});

test("surfaces stable authorization errors without inventing client-side permission", async () => {
  fetchHandler = async () => response({
    version: "v1",
    ok: false,
    error: { code: "MEMORY_SCOPE_DENIED", message: "project scope is not authorized" },
  }, 403);

  await assert.rejects(
    () => new MemoryApiClient().createCandidate({
      scope_type: "project",
      kind: "project_fact",
      semantic_key: "pump room",
      content: "项目事实",
      project_id: projectId,
      expires_at: null,
    }),
    (error) => error instanceof MemoryApiError && error.code === "MEMORY_SCOPE_DENIED" && error.status === 403,
  );
});

test("keeps list and current-answer projections deterministic after mutations", () => {
  const corrected = { ...memory, content: "新值", revision: 2 };
  assert.deepEqual(replaceMemory([memory], corrected), [corrected]);
  assert.deepEqual(upsertMemory([memory], corrected), [corrected]);
  assert.deepEqual(upsertMemory([], corrected), [corrected]);
  assert.deepEqual(removeMemory([memory], memoryId), []);
  assert.deepEqual(selectCurrentAnswerMemories([corrected], [memoryId]), [corrected]);
});

test("memory UI declares trustworthy lifecycle, danger, and keyboard semantics", async () => {
  const workspace = await (await import("node:fs/promises")).readFile(
    new URL("../src/MemoryWorkspace.tsx", import.meta.url),
    "utf8",
  );
  const dialogs = await (await import("node:fs/promises")).readFile(
    new URL("../src/MemoryDialogs.tsx", import.meta.url),
    "utf8",
  );
  const source = `${workspace}\n${dialogs}`;

  for (const label of ["记忆面板", "当前回答使用的记忆", "记住此信息", "冲突", "更正", "忘记", "使用记录"]) {
    assert.match(source, new RegExp(label));
  }
  for (const marker of ["aria-live=\"polite\"", "aria-busy=\"true\"", "role=\"alert\"", "role=\"dialog\"", "role=\"switch\""]) {
    assert.match(source, new RegExp(marker));
  }
  assert.match(source, /error\.code/);
  assert.match(source, /MemoryApiError/);
  assert.match(source, /不会从 trace 猜测正文/);
  assert.match(source, /已有记忆不会被删除/);
  assert.doesNotMatch(source, /\/api\/.*trace/);
});
