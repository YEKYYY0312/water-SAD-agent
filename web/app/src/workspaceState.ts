import type { TraceSnapshot, WorkspaceRunEvent } from "./workspaceApi";
import { modelFromFinalPayload, type ModelCard } from "./modelChoiceState.ts";
import { parseConfirmationRequest, WAITING_CONFIRMATION, type ConfirmationRequest } from "./confirmationApi.ts";

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

/** 本次运行实际用的检索模式（P7-S10）：text 是关键词检索，不是语义检索，界面必须如实标出。 */
export type RetrievalModeCard = { mode: string; reason: string | null };

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
  /** 挂起的人工确认请求（P7-S7）：有值时界面显示确认卡片，决定提交后清空。 */
  confirmation: ConfirmationRequest | null;
  /** 本次运行的检索模式（P7-S10，来自 finalize）。 */
  retrieval: RetrievalModeCard | null;
  /** 用户为本次运行选的模型（P7-S15，来自 finalize；没选过 = null，用的是全局模型）。 */
  model: ModelCard | null;
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
  confirmation: null,
  retrieval: null,
  model: null,
});

/**
 * 后端放进 run.warnings 的稳定码 → 中文说明（P7-S12）。用 Map 而不是对象字面量：
 * 警告文本来自服务端，`constructor` 之类的串不能在原型链上查到东西。
 */
const WARNING_TEXT: ReadonlyMap<string, string> = new Map([
  [
    "WEB_SEARCH_NOT_EXPOSED",
    "本条消息勾选了联网，但这一轮联网搜索没有开放（工具预算已满或联网服务暂不可用），本回答未联网",
  ],
]);

/** 警告怎么显示：登记过的稳定码换成中文说明，其余一律原样——不改变其他 warning 现有的显示。 */
export function warningDisplayText(warning: string): string {
  return WARNING_TEXT.get(warning.trim()) ?? warning;
}

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
 * 发送被拒（例如 409 WEB_RUN_IN_PROGRESS：上一问还在回答）时撤回乐观追加的提问。
 *
 * 只撤回末尾那条、且正文就是这次发送的用户消息；其余一律不动（上一轮已归档的回答照留）。
 */
export function withdrawUnsentQuestion(messages: ConversationMessage[], text: string): ConversationMessage[] {
  const last = messages[messages.length - 1];
  if (!last || last.role !== "user" || last.text !== text) return messages;
  return messages.slice(0, -1);
}

/**
 * 切换会话时合并后端历史与"最新一次运行"的回放。
 *
 * 最新运行会经 SSE 重放进 run 状态并单独渲染；历史里它的回答若不去掉就会出现两遍。
 * 只去掉最后一个用户问题之后的 agent 消息——问题本身保留（run 视图不渲染问题）。
 */
export function mergeHistory(
  history: { role: "user" | "agent"; text: string; withheld?: boolean; status?: string }[],
  hasLatestRun: boolean,
): ConversationMessage[] {
  // 状态以后端随答案持久化的收口状态为准；没有标记的是旧会话的回答（只能按"已显示"处理）。
  // 此前非 withheld 一律标 succeeded：失败的回答刷新后"未通过校验"的横幅就没了。
  const messages: ConversationMessage[] = history.map((item) => (
    item.role === "agent"
      ? {
          role: "agent",
          text: item.text,
          status: item.withheld ? "blocked" : (item.status ?? "succeeded"),
          withheld: Boolean(item.withheld),
        }
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
  history: { role: "user" | "agent"; text: string; withheld?: boolean; status?: string }[],
  latestStatus: string,
): ConversationMessage[] {
  const messages = mergeHistory(history, false);
  // 停在确认门不是被重启打断：挂起在服务端 checkpoint 里，确认卡片从历史恢复
  if (TERMINAL.has(latestStatus) || latestStatus === WAITING_CONFIRMATION) return messages;
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

function retrievalFromFinalPayload(payload: Record<string, unknown>): RetrievalModeCard | null {
  const mode = asString(payload.retrieval_mode);
  return mode ? { mode, reason: asString(payload.retrieval_mode_reason) ?? null } : null;
}

const RETRIEVAL_REASON_TEXT: Record<string, string> = {
  embedding_not_configured: "未配置向量模型",
  embedding_mismatch: "向量模型与索引不匹配",
  forced: "部署指定",
  fixture: "未接入真实索引，仅演示语料",
  embedding_matched: "向量模型与索引匹配",
};

/**
 * 检索模式的界面文案。关键词模式一律标出，并说明对口语化问法的影响——不得把关键词检索说成语义检索。
 * 口语化问法的实测召回见 docs/design/p7-step-10-decision.md。
 */
export function retrievalModeView(card: RetrievalModeCard | null): { label: string; tone: string; warning: string | null } | null {
  if (!card) return null;
  const reason = card.reason ? RETRIEVAL_REASON_TEXT[card.reason] ?? card.reason : null;
  if (card.mode === "hybrid") {
    return { label: `检索：混合检索（向量 + 关键词）${reason ? `，${reason}` : ""}`, tone: "succeeded", warning: null };
  }
  const warning =
    card.reason === "embedding_mismatch"
      ? "当前向量模型与索引不匹配，需按当前向量模型重建索引；在此之前只用关键词检索，口语化问法可能检索不到。"
      : "未配置向量模型时，口语化问法可能检索不到；换成规范里的说法再问会更准。";
  return { label: `检索：关键词模式${reason ? `（${reason}）` : ""}`, tone: "not_configured", warning };
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

  if (event.type === "confirmation_required") {
    // 作答停在人工确认门：卡片取代"正在检索与计算…"，草稿作废（同 run_ended 撤回）
    const confirmation = parseConfirmationRequest(payload);
    return confirmation
      ? { ...next, status: WAITING_CONFIRMATION, confirmation, draft: "" }
      : next;
  }
  if (event.type === "confirmation_decided" || event.type === "run_queued") {
    return { ...next, confirmation: null, status: event.type === "run_queued" ? "queued" : next.status };
  }
  if (event.type === "run_started" || event.type === "state") {
    return { ...next, confirmation: null, status: asString(payload.status) ?? "running" };
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
    const code = asString(payload.code);
    // 登记过中文说明的稳定码优先于 message：message 可能是英文原文，那样用户看到的就不是中文说明
    const warning = (code && WARNING_TEXT.has(code) ? code : undefined) ?? asString(payload.message) ?? code;
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
      retrieval: retrievalFromFinalPayload(payload),
      model: modelFromFinalPayload(payload),
      errors,
      warnings,
    };
  }
  return next;
}

export function isTerminalStatus(status: string): boolean {
  return ["succeeded", "failed", "blocked", "cancelled"].includes(status);
}

/**
 * 刷新 / 重启后从历史恢复确认卡片：最新运行停在确认门、但实时事件已不可用（或尚未回放）时，
 * 用历史投影里的挂起请求重建运行态。挂起请求不属于这个运行就不用它（防止卡片挂错 run）。
 */
export function runStateFromPendingConfirmation(
  pending: unknown,
  latest: { run_id: string; thread_id: string; status: string } | null | undefined,
): WorkspaceRunState | null {
  const confirmation = parseConfirmationRequest(pending);
  if (!confirmation || !latest || latest.status !== WAITING_CONFIRMATION) return null;
  if (confirmation.run_id && confirmation.run_id !== latest.run_id) return null;
  return {
    ...emptyRunState(),
    runId: latest.run_id,
    threadId: latest.thread_id,
    status: WAITING_CONFIRMATION,
    confirmation,
  };
}
