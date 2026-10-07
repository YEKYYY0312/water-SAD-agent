import { csrfToken, WorkspaceApiError, type ApiEnvelope } from "./workspaceApi.ts";

/** run 停在人工确认门时的状态（P7-S7）。不是终态：决定之后同一个 run 继续。 */
export const WAITING_CONFIRMATION = "waiting_confirmation";

/** 与后端 ConfirmationDecisionRequest.reason 的上限一致；前端先截只是少一次 422。 */
export const MAX_CONFIRMATION_REASON_LENGTH = 500;

/**
 * 确认请求（SSE `confirmation_required` 的 payload，或历史里的 `pending_confirmation`）。
 * 参数已由服务端渲染成文本并按 SSE 同一口径脱敏；`arguments_redacted` / `arguments_truncated`
 * 为真时卡片必须说明"你看到的不是完整原文"。
 */
export type ConfirmationRequest = {
  confirmation_id: string;
  tool_name: string;
  server_id: string;
  remote_tool: string;
  arguments_text: string;
  arguments_redacted: boolean;
  arguments_truncated: boolean;
  message: string;
  error?: string;
  /** 只有历史投影带：刷新后据此知道要对哪个 run 做决定。 */
  run_id?: string | null;
};

export type ConfirmationDecision = {
  confirmation_id: string;
  approved: boolean;
  reason?: string;
};

/** 202 的返回：重新订阅事件流时用 `event_cursor` 作 Last-Event-ID（不是客户端自己的游标）。 */
export type ConfirmationAccepted = {
  run_id: string;
  thread_id: string;
  status: string;
  event_cursor: string;
};

export function isWaitingConfirmation(status: string): boolean {
  return status === WAITING_CONFIRMATION;
}

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** 校验服务端给的确认请求形态；缺 id 或工具名时返回 null（不显示一张没法提交的卡片）。 */
export function parseConfirmationRequest(value: unknown): ConfirmationRequest | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  const confirmationId = text(raw.confirmation_id);
  const toolName = text(raw.tool_name);
  if (!confirmationId || !toolName) return null;
  const request: ConfirmationRequest = {
    confirmation_id: confirmationId,
    tool_name: toolName,
    server_id: text(raw.server_id),
    remote_tool: text(raw.remote_tool) || toolName,
    arguments_text: text(raw.arguments_text),
    arguments_redacted: raw.arguments_redacted === true,
    arguments_truncated: raw.arguments_truncated === true,
    message: text(raw.message),
  };
  if (text(raw.error)) request.error = text(raw.error);
  if (typeof raw.run_id === "string" || raw.run_id === null) request.run_id = raw.run_id;
  return request;
}

/** 错误码 → 用户能照着做的文案。未登记的码回落到服务端 message。 */
const CONFIRMATION_ERROR_COPY: Record<string, string> = {
  WEB_CONFIRMATION_STALE: "这条确认已经处理过或已过期，请刷新页面查看最新状态。",
  WEB_CONFIRMATION_PENDING: "这个会话正在等你确认一次工具调用，请先批准或拒绝。",
  WEB_FORBIDDEN: "你在这个项目里没有写权限，不能批准或拒绝这次操作。",
  WEB_NOT_FOUND: "找不到这次运行，或它不是你发起的。",
  WEB_RUN_IN_PROGRESS: "会话还在运行，请稍候再试。",
  WEB_RUN_UNAVAILABLE: "服务暂时无法继续这次运行，请稍后重试。",
  WEB_INVALID_REQUEST: "提交的内容不完整，请刷新页面后重试。",
  AUTH_CSRF_INVALID: "登录状态已失效，请刷新页面后重试。",
  WEB_REQUEST_FAILED: "确认没有提交成功，请检查网络后重试。",
};

export function confirmationErrorMessage(code: string, fallback = "确认没有提交成功"): string {
  return CONFIRMATION_ERROR_COPY[code] ?? fallback;
}

export class ConfirmationApi {
  /** 提交决定。成功返回 202 的数据；失败抛带服务端错误码的 WorkspaceApiError。 */
  async decide(runId: string, decision: ConfirmationDecision): Promise<ConfirmationAccepted> {
    const headers = new Headers({ "Content-Type": "application/json" });
    const token = csrfToken();
    if (token) headers.set("X-CSRF-Token", token);
    const reason = (decision.reason ?? "").trim().slice(0, MAX_CONFIRMATION_REASON_LENGTH);
    let response: Response;
    try {
      response = await fetch(`/api/runs/${encodeURIComponent(runId)}/confirmation`, {
        method: "POST",
        headers,
        credentials: "same-origin",
        body: JSON.stringify({
          confirmation_id: decision.confirmation_id,
          approved: decision.approved,
          reason: reason || null,
        }),
      });
    } catch {
      throw new WorkspaceApiError("WEB_REQUEST_FAILED", "确认没有提交成功", 0);
    }
    let envelope: ApiEnvelope<ConfirmationAccepted> | null = null;
    try {
      envelope = (await response.json()) as ApiEnvelope<ConfirmationAccepted>;
    } catch {
      envelope = null;
    }
    if (!response.ok || !envelope?.ok || !envelope.data) {
      throw new WorkspaceApiError(
        envelope?.error?.code ?? "WEB_REQUEST_FAILED",
        envelope?.error?.message ?? "确认没有提交成功",
        response.status,
      );
    }
    return envelope.data;
  }
}
