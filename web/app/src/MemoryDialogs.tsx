import { FormEvent, useEffect, useRef, useState } from "react";

import {
  MemoryApiError,
  type CandidateInput,
  type ConflictResolution,
  type MemoryCandidate,
  type MemoryConfirmation,
  type MemoryKind,
  type MemoryRecord,
  type MemoryScope,
  type ProjectSummary,
} from "./memoryApi";

const kindLabels: Record<MemoryKind, string> = {
  user_preference: "个人偏好",
  project_fact: "项目事实",
  project_decision: "项目决策",
  workflow_note: "工作备注",
};

const scopeLabels: Record<MemoryScope, string> = {
  user: "个人记忆",
  project: "项目记忆",
};

function errorInfo(error: unknown): { code: string; message: string } {
  if (error instanceof MemoryApiError) return { code: error.code, message: error.message };
  return { code: "WEB_API_ERROR", message: "请求未完成，请稍后重试。" };
}

function expiresAtFromChoice(choice: string): string | null {
  if (!choice) return null;
  const date = new Date();
  date.setDate(date.getDate() + Number(choice));
  return date.toISOString();
}

function displayDate(value: string | null | undefined): string {
  if (!value) return "永不过期";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeZone: "Asia/Shanghai",
  }).format(new Date(value));
}

function ModalShell({
  title,
  eyebrow,
  onClose,
  children,
}: {
  title: string;
  eyebrow: string;
  onClose: () => void;
  children: React.ReactNode;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="modal-backdrop">
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="memory-dialog-title">
        <header className="modal-header">
          <div>
            <p className="kicker">{eyebrow}</p>
            <h2 id="memory-dialog-title">{title}</h2>
          </div>
          <button ref={closeRef} className="icon-button" type="button" onClick={onClose} aria-label="关闭弹窗">
            ×
          </button>
        </header>
        {children}
      </div>
    </div>
  );
}

function DialogError({ error }: { error: { code: string; message: string } | null }) {
  if (!error) return null;
  return (
    <div className="dialog-error" role="alert">
      <strong>{error.code}</strong>
      <span>{error.message}</span>
    </div>
  );
}

export function RememberModal({
  projects,
  projectsLoading,
  onClose,
  onPreview,
  onConfirm,
}: {
  projects: ProjectSummary[];
  projectsLoading: boolean;
  onClose: () => void;
  onPreview: (input: CandidateInput) => Promise<MemoryCandidate>;
  onConfirm: (candidate: MemoryCandidate, confirmation: MemoryConfirmation) => Promise<void>;
}) {
  const [scope, setScope] = useState<MemoryScope>("user");
  const [kind, setKind] = useState<MemoryKind>("user_preference");
  const [projectId, setProjectId] = useState("");
  const [semanticKey, setSemanticKey] = useState("");
  const [content, setContent] = useState("");
  const [expiry, setExpiry] = useState("");
  const [candidate, setCandidate] = useState<MemoryCandidate | null>(null);
  const [resolution, setResolution] = useState<ConflictResolution | undefined>();
  const [alternativeKey, setAlternativeKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code: string; message: string } | null>(null);

  useEffect(() => {
    if (scope === "user" && !["user_preference", "workflow_note"].includes(kind)) {
      setKind("user_preference");
    }
    if (scope === "project" && kind === "user_preference") setKind("project_fact");
  }, [kind, scope]);

  const kindOptions: MemoryKind[] = scope === "user"
    ? ["user_preference", "workflow_note"]
    : ["project_fact", "project_decision", "workflow_note"];

  const submitPreview = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const nextCandidate = await onPreview({
        scope_type: scope,
        project_id: scope === "project" ? projectId || null : null,
        kind,
        semantic_key: semanticKey,
        content,
        expires_at: expiresAtFromChoice(expiry),
      });
      setCandidate(nextCandidate);
      setResolution(undefined);
      setAlternativeKey("");
    } catch (nextError) {
      setError(errorInfo(nextError));
    } finally {
      setBusy(false);
    }
  };

  const submitConfirmation = async (nextResolution?: ConflictResolution) => {
    if (!candidate) return;
    if (candidate.disposition === "quarantined") return;
    if (candidate.conflict && !nextResolution) return;
    if (nextResolution === "keep_both" && !alternativeKey.trim()) {
      setError({ code: "MEMORY_CONFLICT_REQUIRES_CONFIRMATION", message: "请为保留的新记忆提供不同的语义键。" });
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await onConfirm(candidate, {
        confirmed: true,
        content_hash: candidate.content_hash,
        resolution: nextResolution,
        expected_revision: candidate.conflict?.revision,
        alternative_semantic_key: nextResolution === "keep_both" ? alternativeKey.trim() : undefined,
      });
    } catch (nextError) {
      setError(errorInfo(nextError));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalShell title={candidate ? "确认记忆候选" : "记住此信息"} eyebrow="EXPLICIT CONFIRMATION" onClose={onClose}>
      {!candidate ? (
        <form className="dialog-form" onSubmit={submitPreview}>
          <p className="dialog-intro">记忆会跨会话使用。提交前请确认最终正文、类型、作用域和过期时间；后端会再次执行授权与治理校验。</p>
          <label>
            最终正文
            <textarea required rows={5} value={content} onChange={(event) => setContent(event.target.value)} placeholder="例如：默认使用 SI 单位。" />
          </label>
          <div className="form-grid">
            <label>
              作用域
              <select value={scope} onChange={(event) => setScope(event.target.value as MemoryScope)}>
                <option value="user">◎ 个人记忆</option>
                <option value="project">▣ 项目记忆</option>
              </select>
            </label>
            <label>
              类型
              <select value={kind} onChange={(event) => setKind(event.target.value as MemoryKind)}>
                {kindOptions.map((option) => <option key={option} value={option}>{kindLabels[option]}</option>)}
              </select>
            </label>
          </div>
          {scope === "project" && (
            <label>
              项目
              <select required value={projectId} onChange={(event) => setProjectId(event.target.value)} disabled={projectsLoading || projects.length === 0}>
                <option value="">{projectsLoading ? "读取授权项目…" : "选择已授权项目"}</option>
                {projects.map((project) => <option key={project.project_id} value={project.project_id}>{project.name}</option>)}
              </select>
              {!projectsLoading && projects.length === 0 && <small className="field-hint">没有从服务端收到可写项目；不会允许前端手动填写项目 ID。</small>}
            </label>
          )}
          <label>
            语义键
            <input required value={semanticKey} onChange={(event) => setSemanticKey(event.target.value)} placeholder="例如：default unit" />
            <small className="field-hint">键由服务端规范化并用于冲突检测。</small>
          </label>
          <label>
            过期时间
            <select value={expiry} onChange={(event) => setExpiry(event.target.value)}>
              <option value="">永不过期</option>
              <option value="7">7 天后</option>
              <option value="30">30 天后</option>
              <option value="90">90 天后</option>
            </select>
          </label>
          <DialogError error={error} />
          <div className="dialog-actions">
            <button type="button" className="button-secondary" onClick={onClose}>取消</button>
            <button type="submit" className="button-primary" disabled={busy}>{busy ? "正在请求候选…" : "生成候选预览"}</button>
          </div>
        </form>
      ) : (
        <div className="dialog-form">
          <p className="dialog-intro">下面是后端返回的候选预览。只有确认后才会写入；被后端拒绝的内容不会在前端保留为活动记忆。</p>
          <dl className="preview-meta">
            <div><dt>最终正文</dt><dd className="preview-content">{candidate.content || "（正文未被保留；候选已被隔离）"}</dd></div>
            <div><dt>类型</dt><dd>{kindLabels[candidate.kind]}</dd></div>
            <div><dt>作用域</dt><dd>{candidate.scope_type === "user" ? "◎ 个人记忆" : "▣ 项目记忆"}</dd></div>
            <div><dt>过期时间</dt><dd>{displayDate(candidate.memory_expires_at)}</dd></div>
            <div><dt>候选摘要</dt><dd>{candidate.disposition} · hash {candidate.content_hash}</dd></div>
          </dl>
          {candidate.conflict && (
            <div className="conflict-panel" aria-labelledby="conflict-title">
              <div className="conflict-heading"><span className="kicker">CONFLICT</span><strong id="conflict-title">同一语义键已有活动记忆 · revision {candidate.conflict.revision}</strong></div>
              <div className="comparison-grid">
                <section><span>现有值</span><p>{candidate.conflict.current_content}</p></section>
                <section><span>新值</span><p>{candidate.conflict.proposed_content}</p></section>
              </div>
              <label className="choice-row"><input type="radio" name="resolution" checked={resolution === "correct_existing"} onChange={() => setResolution("correct_existing")} />更正现有记忆</label>
              <label className="choice-row"><input type="radio" name="resolution" checked={resolution === "keep_both"} onChange={() => setResolution("keep_both")} />保留为另一条记忆</label>
              {resolution === "keep_both" && <label>新语义键<input value={alternativeKey} onChange={(event) => setAlternativeKey(event.target.value)} placeholder="例如：default unit · 新值" /></label>}
              <p className="field-hint">取消不会覆盖现有记忆，也不会写入这条候选。</p>
            </div>
          )}
          {candidate.disposition === "quarantined" && <div className="dialog-error" role="alert"><strong>MEMORY_INJECTION_BLOCKED</strong><span>后端已隔离候选；规则：{candidate.guard_rule_ids?.join(", ") || "未提供"}</span></div>}
          <DialogError error={error} />
          <div className="dialog-actions">
            <button type="button" className="button-secondary" onClick={onClose}>取消，不保存</button>
            {candidate.conflict ? (
              <button type="button" className="button-primary" disabled={busy || !resolution} onClick={() => submitConfirmation(resolution)}>{busy ? "正在确认…" : "确认冲突选择"}</button>
            ) : (
              <button type="button" className="button-primary" disabled={busy || candidate.disposition === "quarantined"} onClick={() => submitConfirmation()}>{busy ? "正在保存…" : "确认并记住"}</button>
            )}
          </div>
        </div>
      )}
    </ModalShell>
  );
}

export function CorrectionModal({
  memory,
  onClose,
  onCorrect,
}: {
  memory: MemoryRecord;
  onClose: () => void;
  onCorrect: (content: string) => Promise<void>;
}) {
  const [content, setContent] = useState(memory.content);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code: string; message: string } | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onCorrect(content);
    } catch (nextError) {
      setError(errorInfo(nextError));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalShell title="更正记忆" eyebrow="OPTIMISTIC REVISION" onClose={onClose}>
      <form className="dialog-form" onSubmit={submit}>
        <p className="dialog-intro">当前 revision 为 <code>{memory.revision}</code>。提交时服务端会检查版本；旧版本会返回冲突，不会静默覆盖他人的更新。</p>
        <label>最终正文<textarea required rows={6} value={content} onChange={(event) => setContent(event.target.value)} /></label>
        <DialogError error={error} />
        <div className="dialog-actions"><button type="button" className="button-secondary" onClick={onClose}>取消</button><button type="submit" className="button-primary" disabled={busy}>{busy ? "正在更正…" : "保存新 revision"}</button></div>
      </form>
    </ModalShell>
  );
}

export function ForgetModal({
  memory,
  onClose,
  onForget,
}: {
  memory: MemoryRecord;
  onClose: () => void;
  onForget: () => Promise<void>;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code: string; message: string } | null>(null);
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await onForget();
    } catch (nextError) {
      setError(errorInfo(nextError));
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalShell title="忘记这条记忆？" eyebrow="DESTRUCTIVE ACTION" onClose={onClose}>
      <div className="dialog-form">
        <div className="danger-copy"><strong>这会影响后续召回</strong><p>“{memory.content}”会从活动列表和新的召回结果中消失；历史回答不会被改写，服务端只保留不含正文的 tombstone 与审计摘要。</p></div>
        <p className="field-hint">删除完成前不会显示成功状态。该操作不能通过前端撤销。</p>
        <DialogError error={error} />
        <div className="dialog-actions"><button type="button" className="button-secondary" onClick={onClose}>取消</button><button type="button" className="button-danger" disabled={busy} onClick={confirm}>{busy ? "正在忘记…" : "确认忘记"}</button></div>
      </div>
    </ModalShell>
  );
}

export { kindLabels, scopeLabels };
