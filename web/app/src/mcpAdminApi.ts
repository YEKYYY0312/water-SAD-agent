import { csrfToken, WorkspaceApiError, type ApiEnvelope } from "./workspaceApi.ts";

/** 字段来自哪一层（P7-S6）。合并顺序：仓库默认层 → 部署覆盖层 → Web 管理层。 */
export type McpLayer = "repo" | "overlay" | "web" | "default";

/** 凭据槽位的状态。后端只返回是否已填与更新时间，从不返回值。 */
export type McpCredentialSlot = { name: string; configured: boolean; updated_at: string | null };

export type McpProbeTool = { name: string; exposed: boolean; code: string | null };

/** 探测结果：只有远端工具名、是否暴露与跳过码，没有远端描述原文。 */
export type McpProbe = {
  server_id: string;
  probed_at: string;
  status: "ok" | "failed";
  server_code: string | null;
  tools: McpProbeTool[];
  truncated: boolean;
};

/** 这台 server 在 Web 管理层里的改动（没写的字段沿用部署配置）。 */
export type McpWebOverride = {
  disabled?: boolean;
  allow?: string[] | null;
  updated_at?: string;
  updated_by?: string;
};

export type McpServerView = {
  server_id: string;
  disabled: boolean;
  /** null = 全拒。 */
  allow: string[] | null;
  human_confirmation: boolean;
  lifecycle: "per_run" | "shared";
  /** 只有命令的文件名：完整路径与参数只有运维能看能改。 */
  command_name: string;
  sources: Record<string, McpLayer>;
  credentials: McpCredentialSlot[];
  web_override: McpWebOverride | null;
  path_problems: string[];
  last_probe: McpProbe | null;
};

export type McpConfigError = {
  code: string;
  layer: "operator" | "web" | "effective";
  message: string;
  servers?: string[];
};

export type McpOrphan = McpWebOverride & { server_id: string };

export type McpListing = {
  notice: string;
  web_layer: { configured: boolean; writable: boolean; revision: number | null };
  config_error: McpConfigError | null;
  servers: McpServerView[];
  orphans: McpOrphan[];
};

export type McpServerPatch = {
  disabled?: boolean;
  allow?: string[] | null;
  expected_revision: number;
};

export type McpImportEntry = {
  name: string;
  status: "proposed" | "unsupported" | "invalid";
  server_id: string | null;
  credential_env: string[];
  notes: string[];
  reasons: string[];
};

export type McpImportPreview = {
  source_format: string;
  proposal_text: string;
  proposed_count: number;
  entries: McpImportEntry[];
};

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/**
 * MCP 扩展面板的接口（全部要求管理员；写接口带 CSRF）。
 *
 * 错误统一抛 WorkspaceApiError（带服务端错误码与 HTTP 状态）；读接口 403 时面板整块不渲染。
 */
export class McpAdminApi {
  private readonly fetchImpl: FetchLike;

  constructor(fetchImpl?: FetchLike) {
    this.fetchImpl = fetchImpl ?? ((input, init) => fetch(input, init));
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    if (init.method && init.method !== "GET" && init.method !== "HEAD") {
      const token = csrfToken();
      if (token) headers.set("X-CSRF-Token", token);
    }
    let response: Response;
    try {
      response = await this.fetchImpl(path, { ...init, headers, credentials: "same-origin" });
    } catch {
      throw new WorkspaceApiError("WEB_REQUEST_FAILED", "请求没有发出去，请检查网络后重试", 0);
    }
    let envelope: ApiEnvelope<T> | null = null;
    try {
      envelope = (await response.json()) as ApiEnvelope<T>;
    } catch {
      envelope = null;
    }
    if (!response.ok || !envelope?.ok || envelope.data === undefined) {
      throw new WorkspaceApiError(
        envelope?.error?.code ?? "WEB_REQUEST_FAILED",
        envelope?.error?.message ?? "请求未完成",
        response.status,
      );
    }
    return envelope.data;
  }

  listServers(): Promise<McpListing> {
    return this.request("/api/admin/mcp/servers");
  }

  updateServer(serverId: string, patch: McpServerPatch): Promise<{ server: McpServerView; revision: number }> {
    // 只发这三个键：面板改不了别的字段，也不该把它们带上去。
    const body: McpServerPatch = { expected_revision: patch.expected_revision };
    if (patch.disabled !== undefined) body.disabled = patch.disabled;
    if (patch.allow !== undefined) body.allow = patch.allow;
    return this.request(`/api/admin/mcp/servers/${encodeURIComponent(serverId)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }

  removeOverrides(serverId: string, expectedRevision: number): Promise<{ revision: number }> {
    const query = new URLSearchParams({ expected_revision: String(expectedRevision) });
    return this.request(`/api/admin/mcp/servers/${encodeURIComponent(serverId)}/overrides?${query}`, {
      method: "DELETE",
    });
  }

  resetWebLayer(): Promise<{ existed: boolean; revision: number }> {
    return this.request("/api/admin/mcp/web-layer", { method: "DELETE" });
  }

  putCredential(serverId: string, slot: string, value: string): Promise<{ credential: McpCredentialSlot }> {
    return this.request(
      `/api/admin/mcp/servers/${encodeURIComponent(serverId)}/credentials/${encodeURIComponent(slot)}`,
      { method: "PUT", body: JSON.stringify({ value }) },
    );
  }

  deleteCredential(serverId: string, slot: string): Promise<{ credential: McpCredentialSlot }> {
    return this.request(
      `/api/admin/mcp/servers/${encodeURIComponent(serverId)}/credentials/${encodeURIComponent(slot)}`,
      { method: "DELETE" },
    );
  }

  probe(serverId: string): Promise<{ probe: McpProbe }> {
    return this.request(`/api/admin/mcp/servers/${encodeURIComponent(serverId)}/probe`, { method: "POST" });
  }

  importPreview(text: string): Promise<{ preview: McpImportPreview }> {
    return this.request("/api/admin/mcp/import-preview", { method: "POST", body: JSON.stringify({ text }) });
  }
}
