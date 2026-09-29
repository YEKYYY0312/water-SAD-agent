import { useRef, useState, type DragEvent, type FormEvent, type KeyboardEvent, type ReactNode } from "react";

import { availableExports, type ExportFormat } from "./exportApi.ts";
import { Icon } from "./icons";
import { formatBytes, UPLOAD_ACCEPT, uploadNotice, type UploadRecord } from "./uploadApi.ts";
import type { Attachment, ExportState } from "./useWorkspaceController";
import type { Project, WorkspaceSession } from "./workspaceApi";
import type { WorkspaceRunState } from "./workspaceState";

export type ConversationMessage = { role: "user" | "agent"; text: string };

const STATUS_LABEL: Record<string, string> = {
  idle: "等待提问",
  queued: "排队中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败",
  blocked: "被拦截",
  cancelled: "已取消",
  cancellation_requested: "正在取消",
};

function mark(status: string): string {
  if (status === "succeeded" || status === "available") return "✓";
  if (status === "failed" || status === "blocked" || status === "unavailable") return "✗";
  if (status === "running" || status === "queued") return "▸";
  if (status === "not_configured") return "·";
  return "!";
}

export function StatusMark({ status }: { status: string }) {
  return <span className={`status-mark status-${status}`} aria-label={status}>{mark(status)}</span>;
}

function suffixBadge(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot >= 0 ? filename.slice(dot + 1, dot + 5) : "file";
}

export function ProjectSidebar({
  projects,
  selectedProjectId,
  sessions,
  selectedThreadId,
  newProjectName,
  uploads,
  uploadsError,
  open,
  footer,
  onProjectNameChange,
  onCreateProject,
  onSelectProject,
  onSelectSession,
  onCreateSession,
  onDeleteUpload,
}: {
  projects: Project[];
  selectedProjectId: string | null;
  sessions: WorkspaceSession[];
  selectedThreadId: string | null;
  newProjectName: string;
  uploads: UploadRecord[];
  uploadsError: string | null;
  open: boolean;
  footer: ReactNode;
  onProjectNameChange: (value: string) => void;
  onCreateProject: (event: FormEvent<HTMLFormElement>) => void;
  onSelectProject: (projectId: string | null) => void;
  onSelectSession: (threadId: string) => void;
  onCreateSession: () => void;
  onDeleteUpload: (uploadId: string) => void;
}) {
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const scopeName = projects.find((item) => item.project_id === selectedProjectId)?.name;
  return (
    <aside className={`sidebar ${open ? "is-open" : ""}`} aria-label="项目与会话">
      <div className="brand">
        <span className="brand-mark" aria-hidden="true">WA</span>
        <div><h1>给排水 Agent 工作台</h1><p>规范问答 · 计算 · 审查</p></div>
      </div>
      <button type="button" className="nav-row is-primary" onClick={onCreateSession}>
        <Icon name="plus" /><span>新建会话</span>
      </button>

      <div className="sidebar-scroll">
        <section className="sidebar-group" aria-labelledby="scope-heading">
          <div className="group-heading"><h2 id="scope-heading">项目</h2><span className="count">{projects.length}</span></div>
          <div className="project-list" role="listbox" aria-label="项目列表">
            <button
              type="button"
              role="option"
              className={`tree-row ${selectedProjectId === null ? "is-selected" : ""}`}
              aria-selected={selectedProjectId === null}
              onClick={() => onSelectProject(null)}
            >
              <Icon name="chat" /><span>个人会话</span>
            </button>
            {projects.map((project) => (
              <button
                type="button"
                role="option"
                className={`tree-row ${selectedProjectId === project.project_id ? "is-selected" : ""}`}
                aria-selected={selectedProjectId === project.project_id}
                key={project.project_id}
                onClick={() => onSelectProject(project.project_id)}
              >
                <Icon name="folder" /><span>{project.name}</span><small>{project.role === "owner" ? "" : project.role}</small>
              </button>
            ))}
          </div>
          <form className="inline-form" onSubmit={onCreateProject}>
            <label htmlFor="new-project" className="sr-only">新建项目名称</label>
            <input id="new-project" value={newProjectName} maxLength={200} onChange={(event) => onProjectNameChange(event.target.value)} placeholder="新建项目" />
            <button type="submit" aria-label="创建项目" disabled={!newProjectName.trim()}><Icon name="plus" /></button>
          </form>
        </section>

        <section className="sidebar-group" aria-labelledby="sessions-heading">
          <div className="group-heading"><h2 id="sessions-heading">会话</h2><span className="count">{sessions.length}</span></div>
          <div className="session-list" role="listbox" aria-label="会话列表">
            {sessions.length === 0 ? (
              <p className="sidebar-empty">还没有会话，点上方「新建会话」开始。</p>
            ) : sessions.map((session) => (
              <button
                type="button"
                role="option"
                className={`session-row ${selectedThreadId === session.thread_id ? "is-selected" : ""}`}
                aria-selected={selectedThreadId === session.thread_id}
                key={session.thread_id}
                onClick={() => onSelectSession(session.thread_id)}
              >
                <span>{session.title || "未命名会话"}</span>
                {session.latest_run && <StatusMark status={session.latest_run.status} />}
              </button>
            ))}
          </div>
        </section>

        <section className="sidebar-group" aria-labelledby="uploads-heading">
          <div className="group-heading"><h2 id="uploads-heading">{scopeName ? "项目资料" : "个人资料"}</h2><span className="count">{uploads.length}</span></div>
          {uploadsError && <p className="sidebar-error" role="alert">{uploadsError}</p>}
          {uploads.length === 0 ? (
            <p className="sidebar-empty">上传的文件会保存在这里，之后的会话都能引用。</p>
          ) : (
            <ul className="upload-list" aria-label="已上传资料">
              {uploads.map((upload) => {
                const notice = uploadNotice(upload);
                return (
                  <li key={upload.upload_id} className="upload-row" title={`${upload.filename} · ${notice.text}`}>
                    <span className="file-glyph"><Icon name="file" /></span>
                    <span className="upload-name">{upload.filename}</span>
                    {notice.tone === "warn" && <span className="upload-flag" aria-label={notice.text}>!</span>}
                    <span className="upload-meta">{formatBytes(upload.size_bytes)}</span>
                    {pendingDelete === upload.upload_id ? (
                      <button type="button" className="row-action is-confirming" onClick={() => { setPendingDelete(null); onDeleteUpload(upload.upload_id); }} onBlur={() => setPendingDelete(null)}>
                        确认删除
                      </button>
                    ) : (
                      <button type="button" className="row-action" aria-label={`删除 ${upload.filename}`} onClick={() => setPendingDelete(upload.upload_id)}>
                        <Icon name="trash" />
                      </button>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>

      {footer}
    </aside>
  );
}

function AttachmentCard({ attachment, onDismiss }: { attachment: Attachment; onDismiss: () => void }) {
  let meta: { text: string; className: string };
  if (attachment.status === "uploading") {
    meta = { text: `${formatBytes(attachment.sizeBytes)} · 正在上传 ${Math.round(attachment.progress * 100)}%`, className: "attachment-meta" };
  } else if (attachment.status === "failed") {
    meta = { text: attachment.error ?? "上传未完成", className: "attachment-meta is-bad" };
  } else {
    const notice = attachment.record ? uploadNotice(attachment.record) : { tone: "ok", text: "已保存" };
    meta = { text: notice.text, className: `attachment-meta ${notice.tone === "warn" ? "is-warn" : ""}` };
  }
  return (
    <div className={`attachment ${attachment.status === "failed" ? "is-failed" : ""}`} role="group" aria-label={`附件 ${attachment.filename}`}>
      <span className="file-badge" aria-hidden="true">{suffixBadge(attachment.filename)}</span>
      <strong title={attachment.filename}>{attachment.filename}</strong>
      <span className={meta.className} role={attachment.status === "failed" ? "alert" : undefined}>{meta.text}</span>
      <button type="button" className="icon-button" aria-label={attachment.status === "uploading" ? "隐藏" : `移除 ${attachment.filename}`} onClick={onDismiss}>
        <Icon name="close" />
      </button>
      {attachment.status === "uploading" && (
        <span className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(attachment.progress * 100)}>
          <span style={{ width: `${Math.max(4, attachment.progress * 100)}%` }} />
        </span>
      )}
    </div>
  );
}

const EXPORT_LABEL: Record<ExportFormat, string> = { docx: "下载 Word", xlsx: "下载 Excel" };

function AnswerActions({
  run,
  exportState,
  feedbackState,
  onDownload,
  onFeedback,
}: {
  run: WorkspaceRunState;
  exportState: ExportState;
  feedbackState: string | null;
  onDownload: (format: ExportFormat) => void;
  onFeedback: (rating: "useful" | "not_useful") => void;
}) {
  const formats = availableExports(run);
  if (run.status !== "succeeded" || !run.runId) return null;
  return (
    <div className="answer-actions" aria-label="回答操作">
      {formats.map((format) => {
        const busy = exportState?.format === format && exportState.status === "downloading";
        return (
          <button key={format} type="button" className="pill-button" disabled={busy} onClick={() => onDownload(format)}>
            <Icon name="download" />{busy ? "正在生成…" : EXPORT_LABEL[format]}
          </button>
        );
      })}
      {formats.length > 0 && <span className="divider" aria-hidden="true" />}
      <button type="button" className="feedback-button" aria-pressed={feedbackState === "useful"} onClick={() => onFeedback("useful")}>
        <Icon name="thumbUp" />有用
      </button>
      <button type="button" className="feedback-button" aria-pressed={feedbackState === "not_useful"} onClick={() => onFeedback("not_useful")}>
        <Icon name="thumbDown" />无用
      </button>
      {feedbackState && <span className={`action-note ${feedbackState === "saved" ? "is-ok" : ""}`}>{feedbackState === "saved" ? "已记下，谢谢反馈" : feedbackState}</span>}
      {exportState?.status === "done" && <span className="action-note is-ok" role="status">已下载 {exportState.message}</span>}
      {exportState?.status === "failed" && <p className="action-error" role="alert">{exportState.message}</p>}
    </div>
  );
}

const RUN_ERROR_COPY: Record<string, string> = {
  "agent run failed": "Agent 运行中断。详情已记入服务器运行记录，可以换个问法重试。",
  "run persistence unavailable": "服务器数据库暂时不可用，请稍后重试。",
  "run state unavailable": "服务器数据库暂时不可用，请稍后重试。",
  "run state persistence unavailable": "服务器数据库暂时不可用，请稍后重试。",
  "memory policy blocked this run": "长期记忆的访问策略拦下了这次提问。",
  "memory data could not be authorized": "长期记忆暂时无法读取，这次提问没有继续。",
};

function isTerminal(status: string): boolean {
  return ["failed", "blocked", "cancelled"].includes(status);
}

function runFailureCopy(run: WorkspaceRunState): string {
  if (run.status === "cancelled") return "你停止了这次运行，可以修改问题后重新发送。";
  const first = run.errors[0];
  return (first && RUN_ERROR_COPY[first]) ?? first ?? "运行没有返回结果，可以重试一次。";
}

const STARTERS = [
  "DN100 给水管流量 12.5 L/s，流速是多少？",
  "污水管道最小设计坡度的规范依据",
  "按管段表统计材料清单",
  "根据上传的设计说明，复核消火栓间距",
];

export function ConversationPanel({
  session,
  scopeName,
  run,
  messages,
  draft,
  error,
  streaming,
  attachments,
  exportState,
  feedbackState,
  inspectorOpen,
  onDraftChange,
  onSubmit,
  onCancel,
  onFeedback,
  onAttach,
  onDismissAttachment,
  onDownload,
  onToggleInspector,
  onOpenSidebar,
}: {
  session: WorkspaceSession | null;
  scopeName: string;
  run: WorkspaceRunState;
  messages: ConversationMessage[];
  draft: string;
  error: string | null;
  streaming: boolean;
  attachments: Attachment[];
  exportState: ExportState;
  feedbackState: string | null;
  inspectorOpen: boolean;
  onDraftChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onCancel: () => void;
  onFeedback: (rating: "useful" | "not_useful") => void;
  onAttach: (files: FileList | File[]) => void;
  onDismissAttachment: (key: string) => void;
  onDownload: (format: ExportFormat) => void;
  onToggleInspector: () => void;
  onOpenSidebar: () => void;
}) {
  const fileInput = useRef<HTMLInputElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  const [dragging, setDragging] = useState(false);
  const answer = run.answer || run.tokens;
  const uploading = attachments.some((item) => item.status === "uploading");
  const empty = session !== null && messages.length === 0 && !answer && !streaming && run.status === "idle";

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter 发送、Shift+Enter 换行；输入法组字时的 Enter 不算。
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      formRef.current?.requestSubmit();
    }
  };
  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    if (session && event.dataTransfer.files.length) onAttach(event.dataTransfer.files);
  };

  return (
    <section className={`conversation ${empty || !session ? "is-empty" : ""}`} aria-labelledby="conversation-heading">
      <header className="conversation-header">
        <div className="header-title">
          <button type="button" className="icon-button mobile-only" aria-label="打开侧栏" onClick={onOpenSidebar}><Icon name="menu" /></button>
          <div className="header-text">
            <h2 id="conversation-heading">{session?.title || "工程会话"}</h2>
            <p className="scope">{scopeName}{run.runId ? ` · 运行 ${run.runId.slice(0, 8)}` : ""}</p>
          </div>
        </div>
        <div className="header-tools">
          {run.status !== "idle" && <span className="run-status"><StatusMark status={run.status} />{STATUS_LABEL[run.status] ?? run.status}</span>}
          <button type="button" className="icon-button" aria-pressed={inspectorOpen} aria-label={inspectorOpen ? "收起运行检查" : "展开运行检查"} title="运行检查" onClick={onToggleInspector}>
            <Icon name="panel" />
          </button>
        </div>
      </header>

      <div className="message-log" role="log" aria-live="polite" aria-label="工程会话消息">
        {!session ? (
          <div className="welcome" role="status">
            <h2>先选一个会话</h2>
            <p>在左侧选中已有会话，或点「新建会话」。上传的资料按项目保存，换会话也能引用。</p>
          </div>
        ) : empty ? (
          <div className="welcome">
            <h2>今天要查什么？</h2>
            <p>规范条文会给出条款号，数值由计算程序算出。可以附上设计说明、计算书，Agent 会一并参考。</p>
            <div className="starter-row">
              {STARTERS.map((text) => (
                <button key={text} type="button" className="pill-button" onClick={() => onDraftChange(text)}>{text}</button>
              ))}
            </div>
          </div>
        ) : (
          <div className="message-column">
            {messages.map((message, index) => (
              <article className={`message message-${message.role}`} key={`${message.role}-${index}`} aria-label={message.role === "user" ? "你的问题" : "Agent 回答"}>
                {message.text}
              </article>
            ))}
            {answer && run.status !== "idle" && (
              <article className="message message-agent final-answer" aria-label="Agent 回答">
                {answer}
                <AnswerActions run={run} exportState={exportState} feedbackState={feedbackState} onDownload={onDownload} onFeedback={onFeedback} />
              </article>
            )}
            {streaming && <p className="streaming-note"><span className="pulse" aria-hidden="true" />正在检索与计算…</p>}
            {run.warnings.map((warning) => <div className="warning-state" role="status" key={warning}>{warning}</div>)}
            {run.status !== "idle" && run.status !== "succeeded" && isTerminal(run.status) && !answer && (
              <div className="error-state" role="alert">
                <strong>{run.status === "cancelled" ? "已停止" : "这次没有完成"}</strong>
                {runFailureCopy(run)}
              </div>
            )}
            {error && <div className="error-state" role="alert"><strong>连接出了问题</strong>{error}</div>}
          </div>
        )}
      </div>

      <div
        className={`composer-wrap ${dragging ? "drop-active" : ""}`}
        onDragOver={(event) => { if (session) { event.preventDefault(); setDragging(true); } }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <form ref={formRef} className="composer" aria-label="发送工程问题" onSubmit={onSubmit}>
          {attachments.length > 0 && (
            <div className="attachment-list" aria-label="附件">
              {attachments.map((item) => <AttachmentCard key={item.key} attachment={item} onDismiss={() => onDismissAttachment(item.key)} />)}
            </div>
          )}
          <label htmlFor="message" className="sr-only">工程问题</label>
          <textarea
            id="message"
            value={draft}
            disabled={!session || streaming}
            onChange={(event) => onDraftChange(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder={session ? "描述要检索、计算或审查的问题，Enter 发送" : "先选择一个会话"}
            rows={2}
          />
          <div className="composer-bar">
            <input
              ref={fileInput}
              type="file"
              accept={UPLOAD_ACCEPT}
              multiple
              hidden
              onChange={(event) => { if (event.target.files?.length) onAttach(event.target.files); event.target.value = ""; }}
            />
            <button type="button" className="icon-button" aria-label="上传文件" title="上传 Word、PDF、TXT（10 MB 以内）" disabled={!session} onClick={() => fileInput.current?.click()}>
              <Icon name="paperclip" />
            </button>
            {uploading && <span className="composer-hint">文件上传完成后才能发送</span>}
            <span className="spacer" />
            {streaming ? (
              <button type="button" className="stop-button" onClick={onCancel}><Icon name="stop" />停止</button>
            ) : (
              <button type="submit" className="send-button" aria-label="发送" disabled={!session || !draft.trim() || uploading}>
                <Icon name="arrowUp" />
              </button>
            )}
          </div>
        </form>
        <p className="composer-scope"><Icon name="folder" />资料范围：{scopeName} · 拖拽文件到输入框也可上传</p>
      </div>
    </section>
  );
}
