export const MEMORY_API_VERSION = "v1";

export type MemoryScope = "user" | "project";
export type MemoryKind =
  | "user_preference"
  | "project_fact"
  | "project_decision"
  | "workflow_note";
export type MemoryStatus =
  | "active"
  | "superseded"
  | "expired"
  | "forgotten"
  | "quarantined";
export type CandidateDisposition = "ready" | "reaffirm" | "conflict" | "correction" | "quarantined";
export type ConflictResolution = "correct_existing" | "keep_both" | "cancel";

export interface MemoryRecord {
  memory_id: string;
  scope_type: MemoryScope;
  project_id: string | null;
  kind: MemoryKind;
  semantic_key: string;
  content: string;
  status: MemoryStatus;
  revision: number;
  created_at: string;
  updated_at: string;
  expires_at: string | null;
  source_thread_id: string | null;
  source_message_id?: string | null;
  origin?: string;
}

export interface MemoryConflictPreview {
  memory_id: string;
  revision: number;
  current_content: string;
  current_content_hash: string;
  proposed_content: string;
  proposed_content_hash: string;
}

export interface MemoryCandidate {
  candidate_id: string;
  operation: "create" | "correct";
  disposition: CandidateDisposition;
  scope_type: MemoryScope;
  project_id: string | null;
  kind: MemoryKind;
  semantic_key: string;
  content: string;
  content_hash: string;
  memory_expires_at?: string | null;
  requires_confirmation: true;
  target_memory_id?: string | null;
  expected_revision?: number | null;
  conflict?: MemoryConflictPreview | null;
  guard_rule_ids?: string[];
}

export interface MemoryUsage {
  usage_id?: string;
  memory_id: string;
  source_thread_id?: string | null;
  run_id?: string | null;
  score?: number | null;
  created_at: string;
}

export interface MemoryEvent {
  event_id: string;
  event_type: string;
  memory_id: string;
  revision: number;
  created_at: string;
  reason?: string;
}

export interface CurrentAnswerSummary {
  memory_ids: string[];
  run_id?: string | null;
}

export interface MemoryListResult {
  memories: MemoryRecord[];
  currentAnswer?: CurrentAnswerSummary;
}

export interface ProjectSummary {
  project_id: string;
  name: string;
}

export interface CandidateInput {
  scope_type: MemoryScope;
  project_id: string | null;
  kind: MemoryKind;
  semantic_key: string;
  content: string;
  expires_at: string | null;
}

export interface MemoryConfirmation {
  confirmed: true;
  content_hash: string;
  resolution?: ConflictResolution;
  expected_revision?: number;
  alternative_semantic_key?: string;
}

export interface MemoryTombstone {
  memory_id: string;
  status: "forgotten";
  revision: number;
}

type ApiEnvelope<T> = {
  version: string;
  ok: boolean;
  data?: T;
  error?: { code?: string; message?: string };
};

export class MemoryApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "MemoryApiError";
    this.code = code;
    this.status = status;
  }
}

function csrfToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const cookie = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("wa_csrf="));
  return cookie ? decodeURIComponent(cookie.slice("wa_csrf=".length)) : undefined;
}

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : {};
}

function normalizeList(data: unknown): MemoryListResult {
  if (Array.isArray(data)) return { memories: data as MemoryRecord[] };
  const record = asRecord(data);
  const memories = record.memories ?? record.items;
  const currentAnswer = record.current_answer ?? record.currentAnswer;
  return {
    memories: Array.isArray(memories) ? (memories as MemoryRecord[]) : [],
    currentAnswer: currentAnswer as CurrentAnswerSummary | undefined,
  };
}

function normalizeCollection<T>(data: unknown, key: string): T[] {
  if (Array.isArray(data)) return data as T[];
  const value = asRecord(data)[key] ?? asRecord(data).items;
  return Array.isArray(value) ? (value as T[]) : [];
}

export class MemoryApiClient {
  private readonly fetcher: typeof fetch;

  constructor(fetcher: typeof fetch = globalThis.fetch.bind(globalThis)) {
    this.fetcher = fetcher;
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    if (init.body) headers.set("Content-Type", "application/json");
    if (init.method && init.method !== "GET") {
      const token = csrfToken();
      if (token) headers.set("X-CSRF-Token", token);
    }

    let response: Response;
    try {
      response = await this.fetcher(path, { ...init, credentials: "same-origin", headers });
    } catch {
      throw new MemoryApiError("MEMORY_STORE_UNAVAILABLE", "记忆服务暂时不可用，请稍后重试。", 503);
    }

    let envelope: ApiEnvelope<T>;
    try {
      envelope = (await response.json()) as ApiEnvelope<T>;
    } catch {
      throw new MemoryApiError("WEB_API_INVALID_RESPONSE", "服务端返回了无法识别的响应。", response.status);
    }

    if (!response.ok || envelope.ok === false) {
      const error = envelope.error ?? {};
      throw new MemoryApiError(
        error.code ?? "WEB_API_ERROR",
        error.message ?? "请求未完成。",
        response.status,
      );
    }
    if (envelope.version !== MEMORY_API_VERSION || envelope.data === undefined) {
      throw new MemoryApiError("WEB_API_INVALID_RESPONSE", "服务端响应版本或数据缺失。", response.status);
    }
    return envelope.data;
  }

  async listMemories(filters: {
    scope?: MemoryScope;
    project_id?: string;
    kind?: MemoryKind;
    query?: string;
  } = {}): Promise<MemoryListResult> {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value) params.set(key, value);
    }
    const suffix = params.toString() ? `?${params.toString()}` : "";
    return normalizeList(await this.request<unknown>(`/api/memories${suffix}`));
  }

  async getMemory(memoryId: string): Promise<MemoryRecord> {
    return this.request<MemoryRecord>(`/api/memories/${encodeURIComponent(memoryId)}`);
  }

  async createCandidate(input: CandidateInput): Promise<MemoryCandidate> {
    return this.request<MemoryCandidate>("/api/memories/candidates", {
      method: "POST",
      body: JSON.stringify(input),
    });
  }

  async confirmCandidate(candidateId: string, confirmation: MemoryConfirmation): Promise<{
    action: string;
    memory: MemoryRecord | null;
  }> {
    return this.request("/api/memories", {
      method: "POST",
      body: JSON.stringify({ candidate_id: candidateId, confirmation }),
    });
  }

  async correctMemory(memoryId: string, expectedRevision: number, content: string): Promise<MemoryRecord> {
    return this.request<MemoryRecord>(`/api/memories/${encodeURIComponent(memoryId)}`, {
      method: "PATCH",
      body: JSON.stringify({ content, expected_revision: expectedRevision }),
    });
  }

  async setMemoryEnabled(memoryId: string, expectedRevision: number, enabled: boolean): Promise<MemoryRecord> {
    return this.request<MemoryRecord>(`/api/memories/${encodeURIComponent(memoryId)}`, {
      method: "PATCH",
      body: JSON.stringify({ enabled, expected_revision: expectedRevision }),
    });
  }

  async forgetMemory(memoryId: string, expectedRevision: number): Promise<MemoryTombstone> {
    return this.request<MemoryTombstone>(`/api/memories/${encodeURIComponent(memoryId)}`, {
      method: "DELETE",
      body: JSON.stringify({ expected_revision: expectedRevision }),
    });
  }

  async getEvents(memoryId: string): Promise<MemoryEvent[]> {
    return normalizeCollection<MemoryEvent>(
      await this.request<unknown>(`/api/memories/${encodeURIComponent(memoryId)}/events`),
      "events",
    );
  }

  async getUsage(memoryId: string): Promise<MemoryUsage[]> {
    return normalizeCollection<MemoryUsage>(
      await this.request<unknown>(`/api/memories/${encodeURIComponent(memoryId)}/usage`),
      "usage",
    );
  }

  async listProjects(): Promise<ProjectSummary[]> {
    return normalizeCollection<ProjectSummary>(await this.request<unknown>("/api/projects"), "projects");
  }
}
