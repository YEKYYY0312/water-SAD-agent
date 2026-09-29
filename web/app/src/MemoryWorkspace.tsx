import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import {
  MemoryApiClient,
  MemoryApiError,
  type CandidateInput,
  type CurrentAnswerSummary,
  type MemoryCandidate,
  type MemoryConfirmation,
  type MemoryEvent,
  type MemoryKind,
  type MemoryRecord,
  type MemoryScope,
  type MemoryUsage,
  type ProjectSummary,
} from "./memoryApi";
import {
  CorrectionModal,
  ForgetModal,
  RememberModal,
  kindLabels,
} from "./MemoryDialogs";
import { removeMemory, replaceMemory, selectCurrentAnswerMemories, upsertMemory } from "./memoryWorkspaceState";
import "./MemoryWorkspace.css";

const statusLabels: Record<MemoryRecord["status"], string> = {
  active: "启用中",
  superseded: "已停用",
  expired: "已过期",
  forgotten: "已忘记",
  quarantined: "已隔离",
};

function errorInfo(error: unknown): { code: string; message: string } {
  if (error instanceof MemoryApiError) return { code: error.code, message: error.message };
  return { code: "WEB_API_ERROR", message: "请求未完成，请稍后重试。" };
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeZone: "Asia/Shanghai" }).format(date);
}

function formatScore(score: number | null | undefined): string {
  return score === null || score === undefined ? "未提供" : score.toFixed(3);
}

function ScopeMark({ scope }: { scope: MemoryScope }) {
  return <span className={`scope-mark scope-${scope}`} aria-hidden="true">{scope === "user" ? "◎" : "▣"}</span>;
}

function MemoryCard({
  memory,
  selected,
  onSelect,
  onCorrect,
  onToggle,
  onForget,
}: {
  memory: MemoryRecord;
  selected: boolean;
  onSelect: () => void;
  onCorrect: () => void;
  onToggle: () => void;
  onForget: () => void;
}) {
  const projectLabel = memory.scope_type === "project" ? `项目 ${memory.project_id ?? "（服务端未提供）"}` : "个人记忆";
  const enabled = memory.status === "active";
  return (
    <article className={`memory-card ${selected ? "is-selected" : ""}`}>
      <button type="button" className="memory-card-main" onClick={onSelect} aria-pressed={selected}>
        <div className="memory-card-heading">
          <span className="scope-label"><ScopeMark scope={memory.scope_type} />{projectLabel}</span>
          <span className={`status-text status-${memory.status}`}>{statusLabels[memory.status]}</span>
        </div>
        <p className="memory-card-content">{memory.content || "（无正文）"}</p>
        <div className="memory-card-meta"><span>{kindLabels[memory.kind]}</span><span>revision {memory.revision}</span><span>创建于 {formatDate(memory.created_at)}</span></div>
      </button>
      <div className="memory-card-actions" aria-label={`${memory.semantic_key} 操作`}>
        <button type="button" onClick={onSelect}>查看 usage</button>
        <button type="button" onClick={onCorrect} disabled={!enabled}>更正</button>
        <button type="button" onClick={onToggle}>{enabled ? "停用" : "启用"}</button>
        <button type="button" className="danger-link" onClick={onForget}>忘记</button>
      </div>
    </article>
  );
}

function MemoryDetail({
  memory,
  usage,
  events,
  loading,
  error,
  onCorrect,
  onForget,
}: {
  memory: MemoryRecord | null;
  usage: MemoryUsage[];
  events: MemoryEvent[];
  loading: boolean;
  error: { code: string; message: string } | null;
  onCorrect: () => void;
  onForget: () => void;
}) {
  if (!memory) {
    return <section className="memory-detail empty-detail" aria-label="记忆详情"><span aria-hidden="true">◇</span><h3>选择一条记忆</h3><p>这里展示来源、revision 和服务端记录的使用历史，不从 trace 推断正文。</p></section>;
  }
  return (
    <section className="memory-detail" aria-labelledby="memory-detail-title">
      <div className="detail-header"><div><h3 id="memory-detail-title"><ScopeMark scope={memory.scope_type} />{memory.semantic_key}</h3></div><span className={`status-text status-${memory.status}`}>{statusLabels[memory.status]}</span></div>
      <p className="detail-content">{memory.content || "（无正文）"}</p>
      <dl className="record-facts">
        <div><dt>类型</dt><dd>{kindLabels[memory.kind]}</dd></div>
        <div><dt>作用域</dt><dd>{memory.scope_type === "user" ? "◎ 个人记忆" : `▣ 项目记忆 · ${memory.project_id ?? "服务端未提供"}`}</dd></div>
        <div><dt>来源会话</dt><dd>{memory.source_thread_id ?? "未提供"}</dd></div>
        <div><dt>创建时间</dt><dd>{formatDate(memory.created_at)}</dd></div>
        <div><dt>更新时间</dt><dd>{formatDate(memory.updated_at)}</dd></div>
        <div><dt>revision</dt><dd>{memory.revision}</dd></div>
        <div><dt>过期时间</dt><dd>{formatDate(memory.expires_at)}</dd></div>
      </dl>
      <div className="detail-section"><div className="section-heading"><span className="kicker">使用记录</span><span>{loading ? "读取中" : `${usage.length} 条服务端记录`}</span></div>{error ? <div className="inline-error" role="alert"><strong>{error.code}</strong><span>{error.message}</span></div> : usage.length === 0 ? <p className="muted">尚无使用记录；不会从 trace 猜测使用情况。</p> : <ul className="usage-list">{usage.map((item, index) => <li key={item.usage_id ?? `${item.created_at}-${index}`}><span>{formatDate(item.created_at)}</span><span>run {item.run_id ?? "未提供"}</span><span>score {formatScore(item.score)}</span></li>)}</ul>}</div>
      <div className="detail-section"><div className="section-heading"><span className="kicker">变更记录</span><span>{loading ? "读取中" : `${events.length} 条`}</span></div>{error ? null : events.length === 0 ? <p className="muted">暂无事件摘要。</p> : <ul className="event-list">{events.map((event) => <li key={event.event_id}><span>{event.event_type}</span><span>revision {event.revision}</span><span>{formatDate(event.created_at)}</span></li>)}</ul>}</div>
      <div className="detail-actions"><button type="button" className="button-secondary" onClick={onCorrect} disabled={memory.status !== "active"}>更正</button><button type="button" className="button-danger" onClick={onForget}>忘记此记忆</button></div>
    </section>
  );
}

export function MemoryWorkspace({ api: suppliedApi }: { api?: MemoryApiClient }) {
  const [localApi] = useState(() => new MemoryApiClient());
  const api = suppliedApi ?? localApi;
  const [records, setRecords] = useState<MemoryRecord[]>([]);
  const [currentAnswer, setCurrentAnswer] = useState<CurrentAnswerSummary | undefined>();
  const [loadState, setLoadState] = useState<"loading" | "ready" | "error">("loading");
  const [loadError, setLoadError] = useState<{ code: string; message: string } | null>(null);
  const [scopeFilter, setScopeFilter] = useState<"" | MemoryScope>("");
  const [kindFilter, setKindFilter] = useState<"" | MemoryKind>("");
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [usage, setUsage] = useState<MemoryUsage[]>([]);
  const [events, setEvents] = useState<MemoryEvent[]>([]);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<{ code: string; message: string } | null>(null);
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [projectsLoading, setProjectsLoading] = useState(false);
  const [autoRecallEnabled, setAutoRecallEnabled] = useState(true);
  const [dialog, setDialog] = useState<"remember" | "correct" | "forget" | null>(null);
  const [feedback, setFeedback] = useState<{ tone: "success" | "info"; message: string } | null>(null);

  const selectedMemory = records.find((record) => record.memory_id === selectedId) ?? null;
  const currentAnswerMemories = useMemo(
    () => autoRecallEnabled && currentAnswer ? selectCurrentAnswerMemories(records, currentAnswer.memory_ids) : [],
    [autoRecallEnabled, currentAnswer, records],
  );

  const loadMemories = useCallback(async () => {
    setLoadState("loading");
    setLoadError(null);
    try {
      const result = await api.listMemories({ scope: scopeFilter || undefined, kind: kindFilter || undefined, query: query || undefined });
      setRecords(result.memories);
      setCurrentAnswer(result.currentAnswer);
      setSelectedId((previous) => previous && result.memories.some((memory) => memory.memory_id === previous) ? previous : result.memories[0]?.memory_id ?? null);
      setLoadState("ready");
    } catch (error) {
      setLoadState("error");
      setLoadError(errorInfo(error));
    }
  }, [api, kindFilter, query, scopeFilter]);

  useEffect(() => { void loadMemories(); }, [loadMemories]);

  useEffect(() => {
    if (!selectedId) {
      setUsage([]);
      setEvents([]);
      setDetailError(null);
      return;
    }
    let active = true;
    setDetailLoading(true);
    setDetailError(null);
    void Promise.all([api.getUsage(selectedId), api.getEvents(selectedId)]).then(([nextUsage, nextEvents]) => {
      if (!active) return;
      setUsage(nextUsage);
      setEvents(nextEvents);
    }).catch((error) => {
      if (active) setDetailError(errorInfo(error));
    }).finally(() => {
      if (active) setDetailLoading(false);
    });
    return () => { active = false; };
  }, [api, selectedId]);

  const submitSearch = (event: FormEvent) => {
    event.preventDefault();
    setQuery(search.trim());
  };

  const openRemember = () => {
    setDialog("remember");
    if (projects.length === 0 && !projectsLoading) {
      setProjectsLoading(true);
      void api.listProjects().then(setProjects).catch(() => setProjects([])).finally(() => setProjectsLoading(false));
    }
  };

  const handlePreview = (input: CandidateInput) => api.createCandidate(input);

  const handleConfirm = async (candidate: MemoryCandidate, confirmation: MemoryConfirmation) => {
    const result = await api.confirmCandidate(candidate.candidate_id, confirmation);
    if (result.memory) setRecords((previous) => upsertMemory(previous, result.memory!));
    setDialog(null);
    setFeedback({ tone: "success", message: result.action === "reaffirmed" ? "已确认，原记忆保持不变。" : "已确认并保存记忆。" });
    await loadMemories();
  };

  const handleCorrect = async (content: string) => {
    if (!selectedMemory) return;
    const corrected = await api.correctMemory(selectedMemory.memory_id, selectedMemory.revision, content);
    setRecords((previous) => replaceMemory(previous, corrected));
    setDialog(null);
    setFeedback({ tone: "success", message: `更正已保存，当前 revision 为 ${corrected.revision}。` });
    await loadMemories();
  };

  const handleToggle = async (memory: MemoryRecord) => {
    try {
      const next = await api.setMemoryEnabled(memory.memory_id, memory.revision, memory.status !== "active");
      setRecords((previous) => replaceMemory(previous, next));
      setFeedback({ tone: "success", message: next.status === "active" ? "记忆已启用。" : "记忆已停用，不会进入新的召回结果。" });
    } catch (error) {
      setFeedback({ tone: "info", message: `${errorInfo(error).code}：${errorInfo(error).message}` });
    }
  };

  const handleForget = async () => {
    if (!selectedMemory) return;
    await api.forgetMemory(selectedMemory.memory_id, selectedMemory.revision);
    setRecords((previous) => removeMemory(previous, selectedMemory.memory_id));
    setCurrentAnswer((previous) => previous ? { ...previous, memory_ids: previous.memory_ids.filter((id) => id !== selectedMemory.memory_id) } : previous);
    setSelectedId(null);
    setDialog(null);
    setFeedback({ tone: "success", message: "记忆已忘记，已从当前列表移除；新的召回不会再包含它。" });
    await loadMemories();
  };

  return (
    <div className="memory-workspace">
      <header className="memory-workspace-header">
        <div><h2 id="memory-heading">记忆面板</h2><p className="memory-subtitle">只管理经过服务端授权和确认的长期记忆。</p></div>
        <button type="button" className="button-primary" onClick={openRemember}>＋ 记住此信息</button>
      </header>

      <section className="answer-usage" role="log" aria-live="polite" aria-labelledby="answer-usage-title">
        <div className="section-heading"><div><h3 id="answer-usage-title">当前回答使用的记忆</h3></div><label className="switch-control"><input type="checkbox" role="switch" checked={autoRecallEnabled} onChange={(event) => setAutoRecallEnabled(event.target.checked)} /><span>{autoRecallEnabled ? "自动召回已开" : "自动召回已关"}</span></label></div>
        {!autoRecallEnabled ? <p className="muted">已关闭本工作台的自动读取展示；已有记忆不会被删除。</p> : currentAnswerMemories.length === 0 ? <p className="muted">本次列表响应没有提供当前回答的记忆使用摘要；不会从 trace 猜测正文。</p> : <div className="answer-memory-list">{currentAnswerMemories.map((memory) => <button type="button" key={memory.memory_id} onClick={() => setSelectedId(memory.memory_id)}><ScopeMark scope={memory.scope_type} /><span>{memory.content}</span><small>revision {memory.revision}</small></button>)}</div>}
      </section>

      <form className="memory-filters" onSubmit={submitSearch} aria-label="筛选记忆">
        <label className="search-field"><span className="sr-only">搜索记忆</span><input type="search" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索正文或语义键" /><button type="submit">搜索</button></label>
        <label><span className="sr-only">作用域</span><select value={scopeFilter} onChange={(event) => setScopeFilter(event.target.value as "" | MemoryScope)}><option value="">全部作用域</option><option value="user">◎ 个人记忆</option><option value="project">▣ 项目记忆</option></select></label>
        <label><span className="sr-only">类型</span><select value={kindFilter} onChange={(event) => setKindFilter(event.target.value as "" | MemoryKind)}><option value="">全部类型</option>{Object.entries(kindLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      </form>

      {feedback && <div className={`workspace-feedback feedback-${feedback.tone}`} role="status"><span aria-hidden="true">{feedback.tone === "success" ? "✓" : "!"}</span>{feedback.message}<button type="button" aria-label="关闭提示" onClick={() => setFeedback(null)}>×</button></div>}
      {loadState === "loading" && <div className="memory-loading" aria-busy="true" aria-label="正在读取记忆"><span /><span /><span /></div>}
      {loadState === "error" && loadError && <div className="memory-state error-state" role="alert"><strong>{loadError.code}</strong><p>{loadError.message}</p><button type="button" className="button-secondary" onClick={() => void loadMemories()}>重新读取</button></div>}
      {loadState === "ready" && records.length === 0 && <div className="memory-state empty-state-large" role="status"><span aria-hidden="true">·</span><h3>还没有符合条件的记忆</h3><p>从一条明确的工程信息开始；候选会先预览，确认后才写入。</p><button type="button" className="button-secondary" onClick={openRemember}>新建记忆</button></div>}
      {loadState === "ready" && records.length > 0 && <div className="memory-content-grid"><div className="memory-list" role="list" aria-label="长期记忆列表">{records.map((memory) => <MemoryCard key={memory.memory_id} memory={memory} selected={selectedId === memory.memory_id} onSelect={() => setSelectedId(memory.memory_id)} onCorrect={() => { setSelectedId(memory.memory_id); setDialog("correct"); }} onToggle={() => void handleToggle(memory)} onForget={() => { setSelectedId(memory.memory_id); setDialog("forget"); }} />)}</div><MemoryDetail memory={selectedMemory} usage={usage} events={events} loading={detailLoading} error={detailError} onCorrect={() => setDialog("correct")} onForget={() => setDialog("forget")} /></div>}

      {dialog === "remember" && <RememberModal projects={projects} projectsLoading={projectsLoading} onClose={() => setDialog(null)} onPreview={handlePreview} onConfirm={handleConfirm} />}
      {dialog === "correct" && selectedMemory && <CorrectionModal memory={selectedMemory} onClose={() => setDialog(null)} onCorrect={handleCorrect} />}
      {dialog === "forget" && selectedMemory && <ForgetModal memory={selectedMemory} onClose={() => setDialog(null)} onForget={handleForget} />}
    </div>
  );
}
