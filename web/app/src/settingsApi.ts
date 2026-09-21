import { csrfToken } from "./workspaceApi";

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
