import { useEffect, useState, type FormEvent } from "react";

import { ExportApi } from "./exportApi";
import {
  buildProjectPlan, fieldsFromPlan, PROJECT_FIELDS, ProjectCalculationApi,
  projectError, type ProjectHead, type ProjectPreview, type ProjectRun,
} from "./projectCalculation";

const api = new ProjectCalculationApi();
const exports = new ExportApi();
const WAITING = new Set(["confirmed", "queued", "running", "ready", "approved"]);
const STATUS: Record<string, string> = {
  confirmed: "等待计算", queued: "等待计算", running: "正在计算", ready: "正在校验结果",
  approved: "正在发布", waiting_approval: "待审批", succeeded: "已发布",
  blocked: "计算已阻断", failed: "未通过", cancelled: "已取消", stale: "已被修订",
};
const OUTPUT_LABELS: Record<string, string> = {
  velocity_mps: "流速", head_loss_m: "沿程水头损失", pump_head_m: "所需扬程",
};

function ProjectResult({ run }: { run: ProjectRun }) {
  const warnings = new Map((run.candidate ?? []).flatMap((node) => node.assumptions.map((note) => [
    JSON.stringify([note.source_node_id, note.code, note.message]), note.message,
  ] as const)));
  return (
    <>
      <p className="project-calc-status" role="status">
        第 {run.revision} 版 · {run.historical ? "历史成果" : (STATUS[run.status] ?? "状态待确认")}
      </p>
      {run.candidate && (
        <dl className="project-calc-metrics">
          {run.candidate.flatMap((node) => Object.entries(node.outputs).map(([key, value]) => (
            <div key={`${node.node_id}:${key}`}>
              <dt>{OUTPUT_LABELS[key] ?? key}</dt>
              <dd>{String(value)} <small>{node.units[key]}</small></dd>
            </div>
          )))}
        </dl>
      )}
      {warnings.size > 0 && (
        <ul className="project-calc-notes">{[...warnings].map(([key, note]) => <li key={key}>{note}</li>)}</ul>
      )}
    </>
  );
}

export function ProjectCalculationPanel({ projectId, threadId, projectName, onBack }: {
  projectId: string; threadId: string; projectName: string; onBack: () => void;
}) {
  const [head, setHead] = useState<ProjectHead | null>(null);
  const [values, setValues] = useState(() => fieldsFromPlan());
  const [preview, setPreview] = useState<ProjectPreview | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<ProjectRun | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  function useHead(result: ProjectHead) {
    setHead(result);
    setValues(fieldsFromPlan(result.active_plan));
    setRunId(result.active_run_id);
    setRun(null);
    setPreview(null);
    setReason("");
  }

  useEffect(() => {
    const controller = new AbortController();
    void api.head(threadId, controller.signal).then((result) => {
      if (!controller.signal.aborted) useHead(result);
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setError(projectError(error));
    });
    return () => controller.abort();
  }, [threadId]);

  useEffect(() => {
    if (!runId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const load = async () => {
      try {
        const result = await api.state(runId, controller.signal);
        if (controller.signal.aborted) return;
        setRun(result);
        if (WAITING.has(result.status)) timer = setTimeout(load, 750);
      } catch (error: unknown) {
        if (!controller.signal.aborted) setError(projectError(error));
      }
    };
    void load();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [runId]);

  async function act(action: () => Promise<void>) {
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try { await action(); }
    catch (error: unknown) { setError(projectError(error)); }
    finally { setBusy(false); }
  }
  function previewPlan(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!head) return;
    void act(async () => {
      setPreview(null);
      setPreview(await api.preview(threadId, buildProjectPlan(projectId, head, values)));
    });
  }
  function execute() {
    if (!preview) return;
    void act(async () => {
      setPreview(null);
      const started = await api.execute(threadId, preview);
      setRun(null);
      setRunId(started.run_id);
      setReason("");
      setHead(await api.head(threadId));
    });
  }
  function decide(approved: boolean) {
    if (!run || run.status !== "waiting_approval" || !reason.trim()) return;
    void act(async () => {
      await api.approve(run.run_id, approved, reason.trim());
      setRun(await api.state(run.run_id));
      setHead(await api.head(threadId));
      setReason("");
    });
  }
  const selected = run?.run_id === runId ? run : null;
  const working = Boolean(runId && (!selected || WAITING.has(selected.status)));
  const revisions = (head && [
    ...(head.active_run_id ? [{ run_id: head.active_run_id, revision: head.base_revision }] : []),
    ...head.published_runs.filter((entry) => entry.run_id !== head.active_run_id),
  ]) ?? [];
  const confirmedValues = preview ? fieldsFromPlan(preview.plan) : null;

  return (
    <div className="settings-panel project-calc">
      <header className="memory-workspace-header">
        <div>
          <h2 id="project-calculation-heading">项目计算</h2>
          <p className="memory-subtitle">{projectName} · 单管段给水</p>
        </div>
        <button className="button-secondary" type="button" onClick={onBack}>返回会话</button>
      </header>
      <p className="muted-copy">按流速、沿程水头损失、所需扬程的顺序计算。结果供工程复核。</p>
      {error && <p className="dialog-error" role="alert">{error}</p>}
      {notice && <p className="muted-copy" role="status">{notice}</p>}
      {!head && !error && <p role="status">正在读取项目修订…</p>}
      {head && (
        <div className="project-calc-layout">
          <form className="settings-card project-calc-card" onSubmit={previewPlan}>
            <h3>{head.base_revision ? `从第 ${head.base_revision} 版修订` : "计算参数"}</h3>
            <fieldset className="project-calc-fields" disabled={busy || working}>
              {PROJECT_FIELDS.map((field) => (
                <label key={field.key}>
                  {field.label}（{field.unit}）
                  <input name={field.key} type="number" step="any" min="0" required
                    max={"max" in field ? field.max : undefined} value={values[field.key]}
                    onChange={(event) => {
                      setValues({ ...values, [field.key]: event.target.value });
                      setPreview(null);
                    }} />
                </label>
              ))}
            </fieldset>
            <div className="project-calc-actions">
              <button className="button-primary" type="submit" disabled={busy || working}>预览计算</button>
            </div>
            {preview && confirmedValues && (
              <div className="project-calc-preview">
                <h4>核对第 {preview.plan.revision} 版参数</h4>
                <dl className="project-calc-values">
                  {PROJECT_FIELDS.map(({ key, label, unit }) => (
                    <div key={key}><dt>{label}</dt><dd>{confirmedValues[key]} {unit}</dd></div>
                  ))}
                </dl>
                <button className="button-primary" type="button" onClick={execute} disabled={busy || working}>确认并计算</button>
              </div>
            )}
          </form>
          <section className="settings-card project-calc-card" aria-labelledby="project-calc-result">
            <h3 id="project-calc-result">成果与审批</h3>
            {revisions.length > 0 && (
              <label className="project-calc-revision">查看修订
                <select disabled={busy || working} value={runId ?? ""} onChange={(event) => {
                  setRun(null); setReason(""); setRunId(event.target.value);
                }}>
                  {revisions.map((entry) => (
                    <option key={entry.run_id} value={entry.run_id}>
                      第 {entry.revision} 版{entry.run_id === head.active_run_id ? "（当前）" : "（历史）"}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {!selected && <p className="muted-copy" role="status">{runId ? "正在读取计算状态…" : "计算完成后，可在这里查看结果并审批。"}</p>}
            {selected && <ProjectResult run={selected} />}
            {selected?.status === "waiting_approval" && (
              <div className="project-calc-card">
                <label className="confirmation-reason">审批意见
                  <textarea maxLength={1000} value={reason} disabled={busy} onChange={(event) => setReason(event.target.value)} />
                </label>
                <div className="project-calc-actions">
                  <button className="button-primary" type="button" disabled={busy || !reason.trim()} onClick={() => decide(true)}>批准并发布</button>
                  <button className="button-secondary" type="button" disabled={busy || !reason.trim()} onClick={() => decide(false)}>拒绝</button>
                </div>
              </div>
            )}
            {selected?.status === "succeeded" && selected.export_formats?.includes("docx") && (
              <div className="project-calc-actions">
                <button className="button-primary" type="button" disabled={busy} onClick={() => void act(async () => {
                  await exports.download(selected.run_id, "docx");
                  setNotice("已请求下载 Word 计算书。");
                })}>下载 Word 计算书</button>
                <a className="button-secondary" href={`/api/runs/${encodeURIComponent(selected.run_id)}/project-approval`} download>下载审批记录</a>
              </div>
            )}
          </section>
        </div>
      )}
      <button className="button-secondary project-calc-refresh" type="button" disabled={busy}
        onClick={() => void act(async () => {
          const current = await api.head(threadId);
          useHead(current);
          if (current.active_run_id) setRun(await api.state(current.active_run_id));
        })}>刷新修订</button>
    </div>
  );
}
