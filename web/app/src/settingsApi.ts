import { csrfToken } from "./workspaceApi.ts";

export type CredentialSource = "env" | "secret_file" | "volume" | "none";

/** 凭据状态。后端刻意不返回明文，也不返回末四位。 */
export type CredentialStatus = {
  name: string;
  configured: boolean;
  source: CredentialSource;
  updated_at: string | null;
};

type ApiEnvelope<T> = {
  version: string;
  ok: boolean;
  data?: T;
  error?: { code: string; message: string };
};

export class SettingsApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "SettingsApiError";
    this.code = code;
    this.status = status;
  }
}

async function decode<T>(response: Response): Promise<T> {
  let envelope: ApiEnvelope<T>;
  try {
    envelope = (await response.json()) as ApiEnvelope<T>;
  } catch {
    throw new SettingsApiError("WEB_INVALID_RESPONSE", "服务端返回了无法识别的响应", response.status);
  }
  if (!response.ok || !envelope.ok || envelope.data === undefined) {
    throw new SettingsApiError(
      envelope.error?.code ?? "WEB_REQUEST_FAILED",
      envelope.error?.message ?? "请求未完成",
      response.status,
    );
  }
  return envelope.data;
}

export class SettingsApi {
  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    if (init.method && init.method !== "GET" && init.method !== "HEAD") {
      const token = csrfToken();
      if (token) headers.set("X-CSRF-Token", token);
    }
    const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
    return decode<T>(response);
  }

  listCredentials(): Promise<{ credentials: CredentialStatus[] }> {
    return this.request("/api/settings/credentials");
  }

  putCredential(name: string, value: string): Promise<{ credential: CredentialStatus }> {
    return this.request(`/api/settings/credentials/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ value }),
    });
  }

  deleteCredential(name: string): Promise<{ credential: CredentialStatus }> {
    return this.request(`/api/settings/credentials/${encodeURIComponent(name)}`, {
      method: "DELETE",
    });
  }
}

/** P7-S10：对话模型端点（成员只看到主机名与模型名，管理员多一个完整 base_url）。 */
export type LlmEndpointSource = "default" | "env" | "panel" | "local_model" | "explicit";
export type CompatibilityStatus = "default_exempt" | "verified" | "unverified";

export type LlmEndpointView = {
  endpoint: {
    host: string;
    model: string;
    source: LlmEndpointSource;
    is_default: boolean;
    base_url?: string;
  } | null;
  editable: boolean;
  locked_reason: "deployment" | "local_model" | null;
  can_manage: boolean;
  api_key: { name: string; configured: boolean } | null;
  compatibility: {
    status: CompatibilityStatus;
    reason: string;
    report: { status: string; checks: Record<string, string>; probe_version: string; checked_at: string | null } | null;
  } | null;
  check: { running: boolean; last: { status: string; finished_at?: string; requires?: string[]; error?: string } | null };
  error: { code: string; message: string } | null;
};

export class LlmEndpointApi {
  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    if (init.method && init.method !== "GET" && init.method !== "HEAD") {
      const token = csrfToken();
      if (token) headers.set("X-CSRF-Token", token);
    }
    const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
    return decode<T>(response);
  }

  get(): Promise<LlmEndpointView> {
    return this.request("/api/settings/llm-endpoint");
  }

  put(baseUrl: string, model: string): Promise<LlmEndpointView> {
    return this.request("/api/settings/llm-endpoint", {
      method: "PUT",
      body: JSON.stringify({ base_url: baseUrl, model }),
    });
  }

  /**
   * 用表单里尚未保存的 base_url（和可选的 Key）取模型列表。`apiKey` 缺省时服务端用已存的 Key，前端拿不到已存的值。
   * 失败抛 SettingsApiError，code 是稳定码（WEB_LLM_MODELS_*），message 是可直接展示的中文说明。
   */
  listModels(baseUrl: string, apiKey?: string): Promise<{ models: string[] }> {
    const body: { base_url: string; api_key?: string } = { base_url: baseUrl };
    if (apiKey) body.api_key = apiKey;
    return this.request("/api/settings/llm-endpoint/models", { method: "POST", body: JSON.stringify(body) });
  }

  /** 会对当前对话端点发出少量计费请求；202 后轮询 get()。 */
  runCompatibility(): Promise<{ check: { running: boolean } }> {
    return this.request("/api/settings/llm-endpoint/compatibility", { method: "POST" });
  }
}
