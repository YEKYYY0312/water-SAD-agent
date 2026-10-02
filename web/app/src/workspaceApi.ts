export type ApiEnvelope<T> = {
  version: string;
  ok: boolean;
  data?: T;
  error?: { code: string; message: string };
};

export type Project = {
  project_id: string;
  name: string;
  role: string;
  created_at: string;
  updated_at: string;
};

export type RunSummary = {
  run_id: string;
  thread_id: string;
  status: string;
  created_at: string;
  updated_at: string;
};

export type WorkspaceSession = {
  thread_id: string;
  project_id: string | null;
  title: string;
  created_at: string;
  updated_at: string;
  runs?: RunSummary[];
  latest_run?: RunSummary | null;
};

export type DrawingCapability = {
  status: "available" | "not_configured" | "unavailable";
  configured_status: string;
  message: string;
  supported_format: "dxf";
  dwg: { status: "blocked"; message: string };
  real_samples: false;
  recall_gate: false;
};

export type SystemCapabilities = {
  real_llm: boolean;
  authentication: string;
  persistence: string;
  sse: string;
  drawing: DrawingCapability;
  mcp: {
    status: "blocked" | "not_configured" | "unavailable";
    message: string;
    configured_server_count: number | null;
    real_endpoints_verified: false;
  };
};

export type WorkspaceRunEvent = {
  event_id: string;
  type: string;
  run_id: string;
  thread_id: string;
  time: string;
  payload_version: string;
  payload: Record<string, unknown>;
};

export type TraceEvent = {
  event_id?: string;
  event_type?: string;
  node_name?: string;
  timestamp?: string;
  duration_ms?: number;
  retry_count?: number;
  status?: string;
  error?: string;
  data?: Record<string, unknown>;
  citation_source_ids?: string[];
  retrieved_count?: number;
};

export type TraceSnapshot = {
  run_id: string;
  trace_id?: string;
  status: string;
  started_at?: string | null;
  finished_at?: string | null;
  events: TraceEvent[];
};

export type MessageRun = { run_id: string; thread_id: string; status: string };

export class WorkspaceApiError extends Error {
  readonly code: string;
  readonly status: number;

  constructor(code: string, message: string, status: number) {
    super(message);
    this.name = "WorkspaceApiError";
    this.code = code;
    this.status = status;
  }
}

/** 读取非 HttpOnly 的 wa_csrf cookie。导出以供 settingsApi 复用，避免第三份实现。 */
export function csrfToken(): string | undefined {
  if (typeof document === "undefined") return undefined;
  const pair = document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith("wa_csrf="));
  return pair ? decodeURIComponent(pair.slice("wa_csrf=".length)) : undefined;
}

async function decodeEnvelope<T>(response: Response): Promise<T> {
  let envelope: ApiEnvelope<T>;
  try {
    envelope = (await response.json()) as ApiEnvelope<T>;
  } catch {
    throw new WorkspaceApiError("WEB_INVALID_RESPONSE", "服务端返回了无法识别的响应", response.status);
  }
  if (!response.ok || !envelope.ok || envelope.data === undefined) {
    throw new WorkspaceApiError(
      envelope.error?.code ?? "WEB_REQUEST_FAILED",
      envelope.error?.message ?? "请求未完成",
      response.status,
    );
  }
  return envelope.data;
}

/** 历史对话的一条（后端只投影用户问题与每轮最终回答，被输出护栏拦下的回答 withheld）。 */
export type HistoryMessage = { role: "user" | "agent"; text: string; withheld?: boolean; status?: string };

export class WorkspaceApi {
  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    if (init.method && init.method !== "GET" && init.method !== "HEAD") {
      const token = csrfToken();
      if (token) headers.set("X-CSRF-Token", token);
    }
    const response = await fetch(path, { ...init, headers, credentials: "same-origin" });
    return decodeEnvelope<T>(response);
  }

  listProjects(): Promise<{ projects: Project[] }> {
    return this.request("/api/projects");
  }

  createProject(name: string): Promise<Project> {
    return this.request("/api/projects", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
  }

  listSessions(projectId: string | null): Promise<{ sessions: WorkspaceSession[] }> {
    return this.request(
      projectId ? `/api/projects/${encodeURIComponent(projectId)}/sessions` : "/api/sessions",
    );
  }

  createSession(projectId: string | null, title: string): Promise<WorkspaceSession> {
    const path = projectId
      ? `/api/projects/${encodeURIComponent(projectId)}/sessions`
      : "/api/sessions";
    return this.request(path, { method: "POST", body: JSON.stringify({ title }) });
  }

  getSession(threadId: string): Promise<WorkspaceSession> {
    return this.request(`/api/sessions/${encodeURIComponent(threadId)}`);
  }

  sendMessage(threadId: string, message: string, attachments: string[] = []): Promise<MessageRun> {
    // 附件 id 随消息寄出：后端校验归属后把这些文件按原文顺序读进提示词（P5-S49）。
    // 空数组不发送，保持与旧后端的请求体兼容。
    const body = attachments.length > 0 ? { message, attachments } : { message };
    return this.request(`/api/sessions/${encodeURIComponent(threadId)}/messages`, {
      method: "POST",
      body: JSON.stringify(body),
    });
  }

  getMessages(threadId: string): Promise<{ thread_id: string; messages: HistoryMessage[] }> {
    return this.request(`/api/sessions/${encodeURIComponent(threadId)}/messages`);
  }

  getCapabilities(): Promise<SystemCapabilities> {
    return this.request("/api/system/capabilities");
  }

  getTrace(runId: string): Promise<TraceSnapshot> {
    return this.request(`/api/runs/${encodeURIComponent(runId)}/trace`);
  }

  cancelRun(runId: string): Promise<{ run_id: string; status: string }> {
    return this.request(`/api/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
  }

  submitFeedback(
    runId: string,
    rating: "useful" | "not_useful",
    comment = "",
  ): Promise<{ feedback_id: string; status: string; created: boolean }> {
    return this.request(`/api/runs/${encodeURIComponent(runId)}/feedback`, {
      method: "POST",
      body: JSON.stringify({ rating, comment }),
    });
  }
}

export function parseSseBlock(block: string): WorkspaceRunEvent | null {
  const data = block
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice("data:".length).trimStart())
    .join("\n");
  if (!data) return null;
  try {
    const event = JSON.parse(data) as WorkspaceRunEvent;
    if (!event || typeof event !== "object" || typeof event.event_id !== "string") return null;
    return event;
  } catch {
    return null;
  }
}

export async function streamRunEvents(
  runId: string,
  options: {
    lastEventId?: string;
    signal?: AbortSignal;
    onEvent: (event: WorkspaceRunEvent) => void;
  },
): Promise<void> {
  const headers = new Headers({ Accept: "text/event-stream" });
  if (options.lastEventId) headers.set("Last-Event-ID", options.lastEventId);
  const response = await fetch(`/api/runs/${encodeURIComponent(runId)}/events`, {
    headers,
    credentials: "same-origin",
    signal: options.signal,
  });
  if (!response.ok) await decodeEnvelope<never>(response);
  if (!response.body) throw new WorkspaceApiError("WEB_SSE_UNAVAILABLE", "浏览器未提供 SSE 响应流", response.status);

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() ?? "";
      for (const block of blocks) {
        const event = parseSseBlock(block);
        if (event && event.run_id === runId) options.onEvent(event);
      }
    }
    const finalEvent = parseSseBlock(buffer);
    if (finalEvent && finalEvent.run_id === runId) options.onEvent(finalEvent);
  } finally {
    reader.releaseLock();
  }
}
