import assert from "node:assert/strict";
import test from "node:test";

import { ExportApi, availableExports, exportErrorMessage, filenameFromDisposition } from "../src/exportApi.ts";
import { WorkspaceApiError } from "../src/workspaceApi.ts";

const run = (overrides = {}) => ({ status: "succeeded", runId: "r-1", answer: "回答", tools: [], ...overrides });

test("download buttons only appear for succeeded runs with content", () => {
  assert.deepEqual(availableExports(run()), ["docx"]);
  assert.deepEqual(availableExports(run({ status: "running" })), []);
  assert.deepEqual(availableExports(run({ status: "failed" })), []);
  assert.deepEqual(availableExports(run({ runId: null })), []);
  assert.deepEqual(availableExports(run({ answer: "   " })), []);
});

test("excel only appears when material statistics actually succeeded", () => {
  assert.deepEqual(
    availableExports(run({ tools: [{ tool_name: "material_stats", status: "succeeded" }] })),
    ["docx", "xlsx"],
  );
  assert.deepEqual(availableExports(run({ tools: [{ tool_name: "material_stats", status: "failed" }] })), ["docx"]);
  // 流速计算进 Word 计算书，不单独出 Excel
  assert.deepEqual(availableExports(run({ tools: [{ tool_name: "calc_velocity", status: "succeeded" }] })), ["docx"]);
});

test("chinese filename is decoded from RFC 5987", () => {
  const header = "attachment; filename=\"1a2b3c4d.docx\"; filename*=UTF-8''%E7%BB%99%E6%B0%B4-1a2b3c4d.docx";
  assert.equal(filenameFromDisposition(header, "x"), "给水-1a2b3c4d.docx");
  assert.equal(filenameFromDisposition('attachment; filename="a.xlsx"', "x"), "a.xlsx");
  assert.equal(filenameFromDisposition(null, "export.docx"), "export.docx");
});

test("export codes have actionable copy", () => {
  assert.match(exportErrorMessage("WEB_EXPORT_UNAVAILABLE"), /重新提问/);
  assert.match(exportErrorMessage("WEB_EXPORT_SELF_CHECK_FAILED"), /条款/);
  assert.equal(exportErrorMessage("NEW_CODE", "原话"), "原话");
});

test("click triggers a same-page download, not a navigation", async () => {
  const clicks = [];
  const appended = [];
  globalThis.fetch = async (input) => {
    assert.equal(String(input), "/api/runs/r-1/export?format=docx");
    return new Response(new Blob(["PK"]), {
      status: 200,
      headers: { "Content-Disposition": "attachment; filename=\"r.docx\"; filename*=UTF-8''%E8%AE%A1%E7%AE%97.docx" },
    });
  };
  globalThis.document = {
    createElement: () => ({ click() { clicks.push(this.download); }, remove() {} }),
    body: { append: (node) => appended.push(node) },
  };
  globalThis.window = { setTimeout: () => 0 };
  const originalCreate = URL.createObjectURL;
  URL.createObjectURL = () => "blob:1";
  const name = await new ExportApi().download("r-1", "docx");
  assert.equal(name, "计算.docx");
  assert.deepEqual(clicks, ["计算.docx"]);
  assert.equal(appended.length, 1);
  URL.createObjectURL = originalCreate;
  delete globalThis.fetch;
  delete globalThis.document;
  delete globalThis.window;
});

test("server refusal keeps its code and problem list", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({
    version: "v1",
    ok: false,
    error: { code: "WEB_EXPORT_SELF_CHECK_FAILED", message: "章节「正文」出现未经声明的引用：GB50014-2021 5.2.10" },
  }), { status: 422 });
  await assert.rejects(
    () => new ExportApi().fetchExport("r-1", "docx"),
    (error) => error instanceof WorkspaceApiError && error.code === "WEB_EXPORT_SELF_CHECK_FAILED" && /5\.2\.10/.test(error.message),
  );
  delete globalThis.fetch;
});
