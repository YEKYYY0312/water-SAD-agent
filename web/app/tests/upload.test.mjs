import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_UPLOAD_BYTES,
  UploadApi,
  formatBytes,
  precheckUpload,
  uploadErrorMessage,
  uploadNotice,
} from "../src/uploadApi.ts";
import { WorkspaceApiError } from "../src/workspaceApi.ts";

const record = (overrides = {}) => ({
  upload_id: "u-1",
  filename: "设计说明.docx",
  size_bytes: 12345,
  extracted_text_length: 5000,
  page_count: null,
  chunk_count: 3,
  suspicious: false,
  redacted: false,
  project_id: null,
  created_at: "2026-09-28T08:00:00Z",
  ...overrides,
});

test("precheck mirrors the server whitelist and size ceiling", () => {
  assert.equal(precheckUpload({ name: "a.DOCX", size: 10 }), null);
  assert.equal(precheckUpload({ name: "a.pdf", size: MAX_UPLOAD_BYTES }), null);
  assert.equal(precheckUpload({ name: "a.pdf", size: MAX_UPLOAD_BYTES + 1 }), "UPLOAD_TOO_LARGE");
  assert.equal(precheckUpload({ name: "a.xlsx", size: 10 }), "UPLOAD_TYPE_NOT_ALLOWED");
  assert.equal(precheckUpload({ name: "noext", size: 10 }), "UPLOAD_TYPE_NOT_ALLOWED");
});

test("every stable server upload code has actionable copy", () => {
  for (const code of [
    "UPLOAD_TOO_LARGE",
    "UPLOAD_TYPE_NOT_ALLOWED",
    "UPLOAD_MAGIC_MISMATCH",
    "UPLOAD_EXTRACTION_FAILED",
    "UPLOAD_INJECTION_BLOCKED",
  ]) {
    assert.notEqual(uploadErrorMessage(code), "上传未完成", code);
  }
  assert.equal(uploadErrorMessage("SOMETHING_NEW", "服务端原话"), "服务端原话");
});

test("file card notice surfaces scans, suspicious and redacted content", () => {
  assert.equal(uploadNotice(record({ extracted_text_length: 0, page_count: 2 })).tone, "warn");
  assert.match(uploadNotice(record({ extracted_text_length: 0 })).text, /扫描件/);
  assert.match(uploadNotice(record({ suspicious: true })).text, /疑似指令/);
  assert.match(uploadNotice(record({ redacted: true })).text, /隐去/);
  assert.deepEqual(uploadNotice(record({ page_count: 3 })), { tone: "ok", text: "3 页 · 已读取 5,000 字" });
});

test("formats sizes for the file card", () => {
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(12345), "12.1 KB");
  assert.equal(formatBytes(3 * 1024 * 1024), "3.0 MB");
});

test("upload sends multipart with project id and csrf, reports progress", async () => {
  globalThis.document = { cookie: "wa_csrf=tok%3D1" };
  const seen = [];
  const progress = [];
  const api = new UploadApi(async (request) => {
    seen.push(request);
    request.onProgress(0.5);
    request.onProgress(1);
    return { status: 201, text: JSON.stringify({ version: "v1", ok: true, data: record() }) };
  });
  const file = new File(["hello"], "设计说明.docx");
  const result = await api.upload(file, { projectId: "p-1", onProgress: (value) => progress.push(value) });
  assert.equal(result.upload_id, "u-1");
  assert.equal(seen[0].url, "/api/uploads");
  assert.equal(seen[0].headers["X-CSRF-Token"], "tok=1");
  assert.equal(seen[0].body.get("project_id"), "p-1");
  assert.equal(seen[0].body.get("file").name, "设计说明.docx");
  assert.deepEqual(progress, [0.5, 1]);
  delete globalThis.document;
});

test("personal upload omits project id", async () => {
  let body;
  const api = new UploadApi(async (request) => {
    body = request.body;
    return { status: 201, text: JSON.stringify({ version: "v1", ok: true, data: record() }) };
  });
  await api.upload(new File(["x"], "a.txt"), { projectId: null });
  assert.equal(body.get("project_id"), null);
});

test("server error codes survive to the caller", async () => {
  const api = new UploadApi(async () => ({
    status: 400,
    text: JSON.stringify({ version: "v1", ok: false, error: { code: "UPLOAD_MAGIC_MISMATCH", message: "x" } }),
  }));
  await assert.rejects(
    () => api.upload(new File(["x"], "a.docx"), { projectId: null }),
    (error) => error instanceof WorkspaceApiError && error.code === "UPLOAD_MAGIC_MISMATCH" && error.status === 400,
  );
});

test("lists project uploads and personal uploads from the right path", async () => {
  const urls = [];
  globalThis.fetch = async (input) => {
    urls.push(String(input));
    return new Response(JSON.stringify({ version: "v1", ok: true, data: { uploads: [record()] } }), { status: 200 });
  };
  const api = new UploadApi();
  assert.equal((await api.list("p-1")).length, 1);
  await api.list(null);
  assert.deepEqual(urls, ["/api/projects/p-1/uploads", "/api/uploads"]);
  delete globalThis.fetch;
});
