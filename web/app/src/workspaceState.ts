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
  /** 正在写的回答草稿（已过输出护栏，可能被撤回；收口后只认 answer）。 */
  draft: string;
  /** 本轮已撤回过草稿的次数（回流重写或护栏拦截），界面据此提示"正在修订"。 */
  draftRetractions: number;
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
  draft: "",
  draftRetractions: 0,
  events: [],
  tools: [],
  citations: [],
  calculations: [],
  warnings: [],
  errors: [],
  trace: null,
  lastEventId: "0",
});

/** 对话里的一条消息。agent 消息带收口状态：失败的回答也要留在对话里，但必须标出来。 */
export type ConversationMessage = {
  role: "user" | "agent";
  text: string;
  status?: string;
  withheld?: boolean;
};

const TERMINAL = new Set(["succeeded", "failed", "blocked", "cancelled"]);

/**
 * 发下一条消息前，把上一轮已收口的回答归档进对话。
 *
 * 此前回答只活在 run 状态里，发第二问时 setRun(emptyRunState()) 直接把它清掉——
 * 截图里两个问题只剩一个回答就是这个原因（P5-S49）。
 */
export function archiveRun(messages: ConversationMessage[], run: WorkspaceRunState): ConversationMessage[] {
  if (!TERMINAL.has(run.status)) return messages;
  const text = run.answer;
  if (!text && run.status === "succeeded") return messages;
  return [...messages, { role: "agent", text, status: run.status }];
}

/**
 * 切换会话时合并后端历史与"最新一次运行"的回放。
 *
 * 最新运行会经 SSE 重放进 run 状态并单独渲染；历史里它的回答若不去掉就会出现两遍。
 * 只去掉最后一个用户问题之后的 agent 消息——问题本身保留（run 视图不渲染问题）。
 */
export function mergeHistory(
  history: { role: "user" | "agent"; text: string; withheld?: boolean }[],
  hasLatestRun: boolean,
): ConversationMessage[] {
  const messages: ConversationMessage[] = history.map((item) => (
    item.role === "agent"
      ? { role: "agent", text: item.text, status: item.withheld ? "blocked" : "succeeded", withheld: Boolean(item.withheld) }
      : { role: "user", text: item.text }
  ));
  if (!hasLatestRun) return messages;
  let lastUser = -1;
  messages.forEach((item, index) => { if (item.role === "user") lastUser = index; });
  return lastUser === -1 ? messages : messages.slice(0, lastUser + 1);
}

/**
 * 最新运行的实时回放已不可用（服务重启后事件日志随进程消失）时，对话退回完整历史。
 *
 * - 已收口的运行：它的最终回答就在历史里，原样显示；
 * - 未收口的运行（queued/running）：服务重启打断了它，补一条明确的中断说明——
 *   不能让"正在检索与计算…"一直挂着，也不能什么都不显示。
 */
export function replayUnavailable(
  history: { role: "user" | "agent"; text: string; withheld?: boolean }[],
  latestStatus: string,
): ConversationMessage[] {
  const messages = mergeHistory(history, false);
  if (TERMINAL.has(latestStatus)) return messages;
  const last = messages[messages.length - 1];
  if (last && last.role === "agent") return messages;
  return [...messages, { role: "agent", text: "这次运行因服务重启中断，没有完成。可以重新发送问题。", status: "failed" }];
}

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
  if (event.type === "draft_delta") {
    // offset 是这段文字在本条草稿里的起点：重连回放或乱序时只接上紧挨着的那一段，
    // 重复的、跳号的一律丢弃——宁可少显示，也不拼出模型没写过的句子。
    const text = asString(payload.text) ?? "";
    const offset = typeof payload.offset === "number" ? payload.offset : next.draft.length;
    if (!text || offset !== next.draft.length) return next;
    return { ...next, status: "running", draft: next.draft + text };
  }
  if (event.type === "draft_retract") {
    return { ...next, draft: "", draftRetractions: next.draftRetractions + 1 };
  }
  if (event.type === "draft_closed") {
    return next;
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
    // 最终回答只认 finalize 的 answer（输出护栏之后）。token 流里是草稿、工具输出或被拦截的
    // 原文——此前 answer 为空时回落到 tokens，blocked 的运行把被拦下的内容当回答显示。
    const finalAnswer = asString(payload.answer) ?? "";
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
      // 收口后草稿作废：最终回答只认 finalize 的 answer（输出护栏 + 引用校验之后）
      draft: "",
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
