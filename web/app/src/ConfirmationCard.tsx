import { useState } from "react";

import { MAX_CONFIRMATION_REASON_LENGTH, type ConfirmationRequest } from "./confirmationApi.ts";

/** 提交决定的进度：提交中按钮禁用；失败时显示可照着做的文案（confirmationErrorMessage）。 */
export type ConfirmationSubmitState = { status: "submitting" } | { status: "failed"; message: string } | null;

/**
 * 人工确认卡片（P7-S7）：Agent 要调用一个被运维配置为"需人工确认"的 MCP 工具，执行之前停下来问你。
 *
 * 参数原样按纯文本显示（React 转义，不渲染 HTML / Markdown）：确认的就是将要执行的那一份。
 * 服务端按 SSE 同一口径脱敏、超长截断时，卡片如实说明你看到的不是完整原文。
 */
export function ConfirmationCard({
  request,
  state,
  onDecide,
}: {
  request: ConfirmationRequest;
  state: ConfirmationSubmitState;
  onDecide: (approved: boolean, reason: string) => void;
}) {
  const [reason, setReason] = useState("");
  const submitting = state?.status === "submitting";
  const headingId = `confirmation-${request.confirmation_id}`;

  return (
    <section className="confirmation-card" role="group" aria-labelledby={headingId}>
      <p className="kicker">执行前需要你确认</p>
      <h3 id={headingId}>是否允许 Agent 调用 <code>{request.remote_tool}</code>？</h3>
      <p className="confirmation-message">{request.message}</p>
      <dl className="confirmation-meta">
        <dt>MCP server</dt>
        <dd><code>{request.server_id || "未知"}</code></dd>
        <dt>工具</dt>
        <dd><code>{request.tool_name}</code></dd>
      </dl>
      <p className="confirmation-label">将要使用的参数</p>
      <pre className="confirmation-args">{request.arguments_text || "（无参数）"}</pre>
      {request.arguments_redacted && (
        <p className="confirmation-note">参数里的敏感信息已打码显示；批准后执行的是原始参数。</p>
      )}
      {request.arguments_truncated && (
        <p className="confirmation-note">参数过长，这里只显示了前一部分。</p>
      )}
      {request.error && (
        <div className="warning-state" role="status">上一次提交的决定没有生效：{request.error}</div>
      )}
      <label className="confirmation-reason">
        <span>原因（可选，会记入审计）</span>
        <textarea
          value={reason}
          maxLength={MAX_CONFIRMATION_REASON_LENGTH}
          rows={2}
          disabled={submitting}
          onChange={(event) => setReason(event.target.value)}
        />
      </label>
      <div className="confirmation-actions">
        <button type="button" className="button-primary" disabled={submitting} onClick={() => onDecide(true, reason)}>
          批准执行
        </button>
        <button type="button" className="button-secondary" disabled={submitting} onClick={() => onDecide(false, reason)}>
          拒绝
        </button>
        {submitting && <span className="action-note">正在提交…</span>}
      </div>
      {state?.status === "failed" && <p className="action-error" role="alert">{state.message}</p>}
    </section>
  );
}
