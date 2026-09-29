import { csrfToken, WorkspaceApiError, type ApiEnvelope } from "./workspaceApi.ts";

/** 与后端 documents/extractors.py 同一口径；前端先拦只是省一次往返，服务端仍是权威。 */
export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
export const ALLOWED_UPLOAD_SUFFIXES = [".docx", ".pdf", ".txt", ".md", ".dxf", ".dwg"] as const;
export const UPLOAD_ACCEPT = ALLOWED_UPLOAD_SUFFIXES.join(",");

export type UploadRecord = {
  upload_id: string;
  filename: string;
  size_bytes: number;
  extracted_text_length: number;
  page_count: number | null;
  chunk_count: number;
  /** 注入检测命中：内容照常可用，但回答据此作答时要能被识别。 */
  suspicious: boolean;
  /** PII/凭据已从提取文本中剥离（原文件保留）。与 suspicious 是两件事。 */
  redacted: boolean;
  project_id: string | null;
  created_at: string;
  suffix?: string;
  extracted_text_preview?: string;
};

/** 错误码 → 用户能照着做的文案。未登记的码回落到服务端 message。 */
const UPLOAD_ERROR_COPY: Record<string, string> = {
  UPLOAD_TOO_LARGE: "文件超过 10 MB，请拆分后再上传。",
  UPLOAD_TYPE_NOT_ALLOWED: "只支持 Word（.docx）、PDF、TXT、Markdown 和 DXF/DWG 图纸。",
  UPLOAD_MAGIC_MISMATCH: "文件内容和扩展名不一致，请用原始文件重新上传。",
  UPLOAD_EXTRACTION_FAILED: "文件无法读取，可能已损坏或加密。",
  UPLOAD_INJECTION_BLOCKED: "文件内容未通过安全检查。",
  WEB_UPLOAD_STORAGE_UNAVAILABLE: "服务器的上传目录不可写，请联系管理员。",
  WEB_FORBIDDEN: "只读账号不能上传文件。",
  WEB_NOT_FOUND: "当前项目不存在或你不是成员。",
  AUTH_CSRF_INVALID: "登录状态已失效，请刷新页面后重试。",
  UPLOAD_NETWORK_FAILED: "上传中断，请检查网络后重试。",
};

export function uploadErrorMessage(code: string, fallback = "上传未完成"): string {
  return UPLOAD_ERROR_COPY[code] ?? fallback;
}

/** 上传前的本地校验。返回错误码，通过则返回 null。 */
export function precheckUpload(file: { name: string; size: number }): string | null {
  const dot = file.name.lastIndexOf(".");
  const suffix = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
  if (!(ALLOWED_UPLOAD_SUFFIXES as readonly string[]).includes(suffix)) return "UPLOAD_TYPE_NOT_ALLOWED";
  if (file.size > MAX_UPLOAD_BYTES) return "UPLOAD_TOO_LARGE";
  return null;
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** 给文件卡片的一句状态说明：扫描件、可疑内容、已脱敏都要让人看得见。 */
/**
 * DXF 上传的"文本"是程序化审查报告（P5-S50）。报告首句是固定格式的计数，
 * 卡片从上传响应的预览里读它；列表接口不带预览时只说明"已生成审查报告"，不猜数字。
 */
const DRAWING_COUNTS = /已审查管段 (\d+) 条，待核问题 (\d+) 条，未检查 (\d+) 项/;

function drawingNotice(record: UploadRecord): { tone: "ok" | "warn"; text: string } {
  if (record.suspicious) return { tone: "warn", text: "图纸已审查，但部分图元文字含疑似指令，已排除在推导之外。" };
  const counts = (record.extracted_text_preview ?? "").match(DRAWING_COUNTS);
  if (!counts) return { tone: "ok", text: "DXF 图纸 · 已生成程序化审查报告" };
  const [, pipes, findings, notChecked] = counts;
  if (pipes === "0") return { tone: "warn", text: "图纸已读取，但没有识别出可审查的管段。" };
  const unchecked = notChecked === "0" ? "" : `，${notChecked} 项未检查`;
  const tone = findings !== "0" || notChecked !== "0" ? "warn" : "ok";
  return { tone, text: `已审查 ${pipes} 条管段，${findings} 条待核问题${unchecked}` };
}

export function uploadNotice(record: UploadRecord): { tone: "ok" | "warn"; text: string } {
  if (record.suffix === ".dxf" || record.suffix === ".dwg") return drawingNotice(record);
  if (record.extracted_text_length === 0) {
    return { tone: "warn", text: "没有读到文字，可能是扫描件。Agent 无法引用它。" };
  }
  if (record.suspicious) return { tone: "warn", text: "含疑似指令的文字，已按普通资料处理。" };
  if (record.redacted) return { tone: "warn", text: "已隐去其中的密钥或个人信息。" };
  const pages = record.page_count ? `${record.page_count} 页 · ` : "";
  return { tone: "ok", text: `${pages}已读取 ${record.extracted_text_length.toLocaleString("zh-CN")} 字` };
}

export type UploadTransport = (request: {
  url: string;
  body: FormData;
  headers: Record<string, string>;
  onProgress: (fraction: number) => void;
  signal?: AbortSignal;
}) => Promise<{ status: number; text: string }>;

/** XHR：fetch 拿不到上传进度。 */
export const xhrTransport: UploadTransport = ({ url, body, headers, onProgress, signal }) =>
  new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.withCredentials = true;
    for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => resolve({ status: xhr.status, text: xhr.responseText });
    xhr.onerror = () => reject(new WorkspaceApiError("UPLOAD_NETWORK_FAILED", "network error", 0));
    xhr.onabort = () => reject(new DOMException("aborted", "AbortError"));
    signal?.addEventListener("abort", () => xhr.abort(), { once: true });
    xhr.send(body);
  });

function decode<T>(status: number, text: string): T {
  let envelope: ApiEnvelope<T>;
  try {
    envelope = JSON.parse(text) as ApiEnvelope<T>;
  } catch {
    throw new WorkspaceApiError("WEB_INVALID_RESPONSE", "服务端返回了无法识别的响应", status);
  }
  if (status < 200 || status >= 300 || !envelope.ok || envelope.data === undefined) {
    throw new WorkspaceApiError(
      envelope.error?.code ?? "WEB_REQUEST_FAILED",
      envelope.error?.message ?? "请求未完成",
      status,
    );
  }
  return envelope.data;
}

export class UploadApi {
  // 不用 TS 参数属性：node --experimental-strip-types 的测试运行方式不支持它。
  private readonly transport: UploadTransport;

  constructor(transport: UploadTransport = xhrTransport) {
    this.transport = transport;
  }

  async upload(
    file: Blob & { name: string },
    options: { projectId: string | null; onProgress?: (fraction: number) => void; signal?: AbortSignal },
  ): Promise<UploadRecord> {
    const body = new FormData();
    body.append("file", file, file.name);
    if (options.projectId) body.append("project_id", options.projectId);
    const headers: Record<string, string> = { Accept: "application/json" };
    const token = csrfToken();
    if (token) headers["X-CSRF-Token"] = token;
    const { status, text } = await this.transport({
      url: "/api/uploads",
      body,
      headers,
      onProgress: options.onProgress ?? (() => undefined),
      signal: options.signal,
    });
    return decode<UploadRecord>(status, text);
  }

  async list(projectId: string | null): Promise<UploadRecord[]> {
    const path = projectId ? `/api/projects/${encodeURIComponent(projectId)}/uploads` : "/api/uploads";
    const response = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
    return decode<{ uploads: UploadRecord[] }>(response.status, await response.text()).uploads;
  }

  async remove(uploadId: string): Promise<void> {
    const headers: Record<string, string> = { Accept: "application/json" };
    const token = csrfToken();
    if (token) headers["X-CSRF-Token"] = token;
    const response = await fetch(`/api/uploads/${encodeURIComponent(uploadId)}`, {
      method: "DELETE",
      credentials: "same-origin",
      headers,
    });
    decode<unknown>(response.status, await response.text());
  }
}
