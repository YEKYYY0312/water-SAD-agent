import type { FormEvent } from "react";

import type { Project, WorkspaceSession } from "./workspaceApi";
import type { WorkspaceRunState } from "./workspaceState";

export type ConversationMessage = { role: "user" | "agent"; text: string };

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

export function ProjectSidebar({
  projects,
  selectedProjectId,
  sessions,
  selectedThreadId,
  newProjectName,
  onProjectNameChange,
  onCreateProject,
  onSelectProject,
  onSelectSession,
  onCreateSession,
}: {
  projects: Project[];
  selectedProjectId: string | null;
  sessions: WorkspaceSession[];
  selectedThreadId: string | null;
  newProjectName: string;
  onProjectNameChange: (value: string) => void;
  onCreateProject: (event: FormEvent<HTMLFormElement>) => void;
  onSelectProject: (projectId: string | null) => void;
  onSelectSession: (threadId: string) => void;
  onCreateSession: () => void;
}) {
  return (
    <aside className="sidebar" aria-labelledby="scope-heading">
      <p className="kicker">PROJECTS</p>
      <div className="section-heading">
        <h2 id="scope-heading">项目</h2>
        <span className="count-label">{projects.length}</span>
      </div>
      <div className="project-list" role="listbox" aria-label="项目列表">
        <button
          type="button"
          className={`tree-row ${selectedProjectId === null ? "is-selected" : ""}`}
          aria-selected={selectedProjectId === null}
          onClick={() => onSelectProject(null)}
        >
          <span aria-hidden="true">›</span><span>个人会话</span>
        </button>
        {projects.map((project) => (
          <button
            type="button"
            className={`tree-row ${selectedProjectId === project.project_id ? "is-selected" : ""}`}
            aria-selected={selectedProjectId === project.project_id}
            key={project.project_id}
            onClick={() => onSelectProject(project.project_id)}
          >
            <span aria-hidden="true">›</span><span>{project.name}</span><small>{project.role}</small>
          </button>
        ))}
      </div>
      <form className="inline-form" onSubmit={onCreateProject}>
        <label htmlFor="new-project">新建项目</label>
        <div className="inline-form-row">
          <input
            id="new-project"
            value={newProjectName}
            maxLength={200}
            onChange={(event) => onProjectNameChange(event.target.value)}
            placeholder="项目名称"
          />
          <button type="submit" aria-label="创建项目">＋</button>
        </div>
      </form>
      <div className="sidebar-divider" />
      <p className="kicker">SESSIONS</p>
      <div className="section-heading">
        <h2>会话</h2>
        <button type="button" className="quiet-button" onClick={onCreateSession}>＋ 新建</button>
      </div>
      <div className="session-list" role="listbox" aria-label="会话列表">
        {sessions.length === 0 ? (
          <p className="muted-copy">暂无会话。创建一个开始追踪运行。</p>
        ) : sessions.map((session) => (
          <button
            type="button"
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
    </aside>
  );
}

export function ConversationPanel({
  session,
  run,
  messages,
  draft,
  error,
  streaming,
  onDraftChange,
  onSubmit,
  onCancel,
  onFeedback,
  feedbackState,
}: {
  session: WorkspaceSession | null;
  run: WorkspaceRunState;
  messages: ConversationMessage[];
  draft: string;
  error: string | null;
  streaming: boolean;
  onDraftChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  onCancel: () => void;
  onFeedback: (rating: "useful" | "not_useful") => void;
  feedbackState: string | null;
}) {
  const answer = run.answer || run.tokens;
  return (
    <section className="conversation" aria-labelledby="conversation-heading">
      <header className="conversation-header">
        <div>
          <p className="kicker">SESSION / RUN</p>
          <h2 id="conversation-heading">{session?.title || "工程会话"}</h2>
          {session && <p className="mono-subtitle">thread {session.thread_id}</p>}
        </div>
        <div className="run-status">
          <StatusMark status={run.status} /> <span>{run.status === "idle" ? "等待输入" : run.status}</span>
          {run.runId && <small>run {run.runId.slice(0, 8)}</small>}
        </div>
      </header>
      <div className="message-log" role="log" aria-live="polite" aria-label="工程会话消息">
        {!session && <div className="empty-state" role="status"><strong>选择或创建会话</strong><p>项目和会话由服务端授权后加载。</p></div>}
        {session && messages.map((message, index) => (
          <article className={`message message-${message.role}`} key={`${message.role}-${index}`}>
            <p className="message-role">{message.role === "user" ? "❯ 用户" : "AGENT"}</p>
            <p>{message.text}</p>
          </article>
        ))}
        {session && !messages.length && !streaming && (
          <div className="empty-state" role="status"><strong>会话已恢复</strong><p>发送问题后，工具、引用和计算结果会由真实 SSE 事件逐步出现。</p></div>
        )}
        {streaming && <p className="streaming-note"><StatusMark status="running" /> 正在从后端接收事件，游标 {run.lastEventId}</p>}
        {error && <div className="error-state" role="alert"><strong>运行未完成</strong><p>{error}</p></div>}
        {run.warnings.map((warning) => <div className="warning-state" role="status" key={warning}><span>!</span>{warning}</div>)}
        {answer && run.status !== "idle" && (
          <article className="message message-agent final-answer">
            <p className="message-role">AGENT / FINAL</p>
            <p>{answer}</p>
            {run.status === "succeeded" && run.runId && (
              <div className="feedback-row" aria-label="回答反馈">
                <button type="button" aria-pressed={feedbackState === "useful"} onClick={() => onFeedback("useful")}>有用</button>
                <button type="button" aria-pressed={feedbackState === "not_useful"} onClick={() => onFeedback("not_useful")}>无用</button>
                {feedbackState && <span>{feedbackState === "saved" ? "已进入反馈候选区" : feedbackState}</span>}
              </div>
            )}
          </article>
        )}
      </div>
      <form className="composer" aria-label="发送工程问题" onSubmit={onSubmit}>
        <label htmlFor="message">工程问题</label>
        <textarea
          id="message"
          value={draft}
          disabled={!session || streaming}
          onChange={(event) => onDraftChange(event.target.value)}
          placeholder={session ? "描述需要检索、计算或审查的问题" : "先选择一个会话"}
          rows={3}
        />
        <div className="composer-actions">
          {streaming && <button type="button" className="danger-button" onClick={onCancel}>取消运行</button>}
          <button type="submit" disabled={!session || streaming || !draft.trim()}>发送</button>
        </div>
      </form>
    </section>
  );
}
