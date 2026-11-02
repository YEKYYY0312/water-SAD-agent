import { csrfToken, WorkspaceApiError, type ApiEnvelope } from "./workspaceApi.ts";

/** 目录条目的纯文本视图（P7-S14）。**所有字段都是第三方写的文本**：前端只能按纯文本渲染，不得当 Markdown / HTML / 链接。 */
export type RegistryRemote = { type: string; url: string };
export type RegistryPackage = { registry_type: string; identifier: string };

export type RegistryEntry = {
  name: string;
  version: string;
  /** `ok` 或 `unsafe`。`unsafe` 条目没有 description / remotes / packages，不可提案。 */
  status: "ok" | "unsafe";
  description: string;
  remotes: RegistryRemote[];
  packages: RegistryPackage[];
  dropped_fields: number;
  unsafe_reason: string;
  proposable: boolean;
  /** 稳定短码（不含条目内容）；可提案时为 null。 */
  not_proposable_reason: string | null;
};

export type RegistrySearchResult = {
  registry_host: string;
  notice: string;
  entries: RegistryEntry[];
  count: number;
  pages: number;
  truncated: boolean;
};

export type RegistryProposal = {
  name: string;
  version: string;
  server_id: string;
  /** 覆盖层格式的提案文本：已停用、不含 allow（全拒）。 */
  proposal_text: string;
  proposal_hash: string;
  notes: string[];
  secret_dropped: number;
  idna_host: string | null;
};

export type RegistryStatus = {
  configured: boolean;
  registry_host: string | null;
  allow_private_network: boolean | null;
  notice: string;
  limits: { page_limit: number; max_pages: number; max_query_chars: number };
};

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/**
 * MCP 目录发现的接口（全部要求管理员；POST 带 CSRF）。
 *
 * 只有三个调用：读状态、搜索、为 `{name, version}` 生成提案。**没有**连接 / 启用 / 应用 / 保存的调用——
 * 提案只是文本，由运维核对后贴进部署覆盖层（`tests/mcp_registry.test.mjs` 断言这一点）。
 * 错误统一抛 WorkspaceApiError（带服务端错误码与 HTTP 状态）；读接口 403 时面板整块不渲染。
 */
export class McpRegistryApi {
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

  status(): Promise<RegistryStatus> {
    return this.request("/api/admin/mcp/registry/status");
  }

  /** 搜索 / 浏览。只发 `{query}`（可带 `cursor`）；条目内容全由服务端从目录取。 */
  search(query: string, cursor?: string): Promise<RegistrySearchResult> {
    const body: { query: string; cursor?: string } = { query };
    if (cursor) body.cursor = cursor;
    return this.request("/api/admin/mcp/registry/search", { method: "POST", body: JSON.stringify(body) });
  }

  /** 生成提案。请求体**只有** name 与 version——条目的其余内容由服务端重新从目录取，前端回传什么都不会被采纳。 */
  propose(name: string, version: string): Promise<{ proposal: RegistryProposal }> {
    return this.request("/api/admin/mcp/registry/proposal", {
      method: "POST",
      body: JSON.stringify({ name, version }),
    });
  }
}
