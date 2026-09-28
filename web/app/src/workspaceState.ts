import type { TraceSnapshot, WorkspaceRunEvent } from "./workspaceApi";

export type ToolCard = {
  tool_name: string;
  status: "running" | "succeeded" | "failed";
  code?: string;
  message?: string;
};

export type CitationCard = { source_id: string };

export type CalculationCard = {
  tool_name: string;
  programmatic: true;
  values: Record<string, number>;
  units: Record<string, string>;
  assumptions: string[];
};

export type WorkspaceRunState = {
  runId: string | null;
  threadId: string | null;
  status: string;
  answer: string;
  tokens: string;
  events: WorkspaceRunEvent[];
  tools: ToolCard[];
  citations: CitationCard[];
  calculations: CalculationCard[];
  warnings: string[];
  errors: string[];
  trace: TraceSnapshot | null;
  lastEventId: string;
};

export const emptyRunState = (): WorkspaceRunState => ({
  runId: null,
  threadId: null,
  status: "idle",
  answer: "",
  tokens: "",
  events: [],
  tools: [],
  citations: [],
  calculations: [],
  warnings: [],
  errors: [],
  trace: null,
  lastEventId: "0",
});

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value ? value : undefined;
}

function citationsFromFinalPayload(value: unknown): CitationCard[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object") return [];
    const sourceId = asString((item as { source_id?: unknown }).source_id);
    return sourceId ? [{ source_id: sourceId }] : [];
  });
}

function calculationsFromFinalPayload(value: unknown): CalculationCard[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is CalculationCard => {
    if (!item || typeof item !== "object") return false;
    const candidate = item as Partial<CalculationCard>;
    return candidate.programmatic === true && typeof candidate.tool_name === "string";
  });
}

function replaceTool(tools: ToolCard[], next: ToolCard): ToolCard[] {
  const index = tools.findIndex((item) => item.tool_name === next.tool_name);
  if (index < 0) return [...tools, next];
  return tools.map((item, itemIndex) => (itemIndex === index ? { ...item, ...next } : item));
}

export function applyRunEvent(state: WorkspaceRunState, event: WorkspaceRunEvent): WorkspaceRunState {
  const next: WorkspaceRunState = {
    ...state,
    events: [...state.events, event],
    runId: event.run_id,
    threadId: event.thread_id,
    lastEventId: event.event_id,
  };
  const payload = event.payload;

  if (event.type === "run_started" || event.type === "state") {
    return { ...next, status: asString(payload.status) ?? "running" };
  }
  if (event.type === "token") {
    const text = asString(payload.text) ?? "";
    return { ...next, status: "running", tokens: next.tokens + text };
  }
  if (event.type === "agent_message") {
    return { ...next, status: "running" };
  }
  if (event.type === "tool_started") {
    const toolName = asString(payload.tool_name);
    return toolName
      ? { ...next, status: "running", tools: replaceTool(next.tools, { tool_name: toolName, status: "running" }) }
      : next;
  }
  if (event.type === "tool_result") {
    const toolName = asString(payload.tool_name);
    const status = payload.status === "failed" ? "failed" : "succeeded";
    const calculation = calculationsFromFinalPayload([payload.calculation]);
    return toolName
      ? {
          ...next,
          tools: replaceTool(next.tools, {
            tool_name: toolName,
            status,
            code: asString(payload.code),
            message: asString(payload.message),
          }),
          calculations: calculation.length > 0 ? [...next.calculations, ...calculation] : next.calculations,
        }
      : next;
  }
  if (event.type === "warning") {
    const warning = asString(payload.message) ?? asString(payload.code);
    return warning && !next.warnings.includes(warning)
      ? { ...next, warnings: [...next.warnings, warning] }
      : next;
  }
  if (event.type === "finalize") {
    const finalStatus = asString(payload.status) ?? "failed";
    const finalAnswer = asString(payload.answer) ?? next.tokens;
    const errors = Array.isArray(payload.errors) ? payload.errors.filter((value): value is string => typeof value === "string") : [];
    // 运行级失败（装配失败、持久化不可用等）只带一个 error 字段、没有 errors 数组；
    // 不收进来的话界面只剩一个「失败」徽标，用户不知道发生了什么。
    const runError = asString(payload.error);
    if (runError && !errors.includes(runError)) errors.push(runError);
    const warnings = Array.isArray(payload.warnings) ? payload.warnings.filter((value): value is string => typeof value === "string") : next.warnings;
    return {
      ...next,
      status: finalStatus,
      answer: finalAnswer,
      citations: citationsFromFinalPayload(payload.citations),
      calculations: calculationsFromFinalPayload(payload.calculations),
      errors,
      warnings,
    };
  }
  return next;
}

export function isTerminalStatus(status: string): boolean {
  return ["succeeded", "failed", "blocked", "cancelled"].includes(status);
}
