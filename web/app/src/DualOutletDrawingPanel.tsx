import { useState, type FormEvent } from "react";

import {
  DrawingApi, DrawingApiError, type DrainageResponse, type DualDrainageOptions,
} from "./drawingApi";
import { DRAFT_NOTICE, drawingErrorMessage } from "./drawingState";
import { dualFormFrom, dualRequest, emptyDualPoint, type DualNodeForm } from "./drainageDualState";

const api = new DrawingApi();
const coordinates = [["x_mm", "X 坐标"], ["y_mm", "Y 坐标"], ["invert_mm", "管底标高"]] as const;

function issueTitle(path: string, rows: DualNodeForm[]): string {
  const [group, index, field] = path.split(".");
  const row = (group === "nodes" ? rows : rows.filter((point) => point.kind !== "outlet"))[Number(index)];
  const names: Record<string, string> = {
    id: "编号", x_mm: "X 坐标", y_mm: "Y 坐标", invert_mm: "管底标高",
    start: "上游接点", end: "下游接点", mark: "管径写法", size_mm: "管径",
  };
  return row ? [row.id, names[field]].filter(Boolean).join(" · ") : "";
}

export function DualOutletDrawingPanel({ options, system }: { options: DualDrainageOptions; system: string }) {
  const [form, setForm] = useState(() => dualFormFrom());
  const [result, setResult] = useState<DrainageResponse | null>(null);
  const [error, setError] = useState("");
  const [issues, setIssues] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [downloading, setDownloading] = useState(false);

  function clear() {
    setError(""); setIssues([]); setResult(null);
  }

  function update(id: string, patch: Partial<DualNodeForm>) {
    clear();
    setForm((old) => ({ ...old, nodes: old.nodes.map((row) => row.id === id ? { ...row, ...patch } : row) }));
  }

  function addPoint() {
    clear();
    setForm((old) => {
      if (old.nodes.length >= options.max_nodes) return old;
      const taken = new Set(old.nodes.map((row) => row.id));
      let number = 1;
      while (taken.has(`N${number}`)) number++;
      return { ...old, nodes: [...old.nodes, emptyDualPoint(`N${number}`)] };
    });
  }

  function removePoint(id: string) {
    clear();
    setForm((old) => ({
      ...old, nodes: old.nodes.filter((row) => row.id !== id)
        .map((row) => row.end === id ? { ...row, end: "" } : row),
    }));
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    clear();
    setBusy(true);
    try {
      setResult(await api.generateDrainage(dualRequest(form, system)));
    } catch (caught) {
      if (caught instanceof DrawingApiError) {
        setError(drawingErrorMessage(caught.code, caught.message));
        setIssues(caught.fields.map((field) => {
          const title = issueTitle(field.loc, form.nodes);
          return title ? `${title}：${field.message}` : field.message;
        }));
      } else {
        setError(caught instanceof RangeError ? caught.message : "排水图纸未能生成，请稍后重试。");
      }
    } finally {
      setBusy(false);
    }
  }

  async function download() {
    if (!result) return;
    setDownloading(true); setError("");
    try { await api.download(result); }
    catch (caught) {
      setError(caught instanceof DrawingApiError
        ? drawingErrorMessage(caught.code, caught.message) : "排水图纸下载未完成，请重试。");
    } finally { setDownloading(false); }
  }

  function number(label: string, value: string, changed: (value: string) => void) {
    return <label className="drainage-field">
      {label}（mm）
      <input type="number" required step={1} value={value} onChange={(e) => changed(e.target.value)} />
    </label>;
  }

  function kindLabel(value: DualNodeForm["kind"]) {
    return options.point_kinds.find((item) => item.value === value)?.label;
  }

  return <>
    <div className="workspace-feedback feedback-info" role="note">
      {DRAFT_NOTICE}。从卫生间平面左下角起量，X 向右、Y 向上。所有管底标高使用同一基准。
      为每个接点指定下游，转弯处添加接点；两路分别接到两个排水出口。
    </div>
    {error && <div className="dialog-error" role="alert">
      {error}
      {issues.length > 0 && <ul>{Array.from(new Set(issues)).map((issue) => <li key={issue}>{issue}</li>)}</ul>}
    </div>}
    <form className="settings-card dialog-form drainage-form" onSubmit={submit}>
      <fieldset disabled={busy}>
        <div className="settings-heading-row">
          <h3>双出口排水接点</h3>
          <button className="button-secondary" type="button" onClick={() => {
            clear(); setForm(dualFormFrom(options.example));
          }}>填入双出口示例</button>
        </div>
        <div className="drainage-inputs">
          {number("卫生间宽度", form.width, (width) => { clear(); setForm({ ...form, width }); })}
          {number("卫生间深度", form.depth, (depth) => { clear(); setForm({ ...form, depth }); })}
        </div>
        {form.nodes.map((row) => (
          <fieldset className="drainage-point" key={row.id}>
            <legend>{row.id}{kindLabel(row.kind) ? ` · ${kindLabel(row.kind)}` : ""}</legend>
            <div className="drainage-inputs">
              {row.kind !== "outlet" && <label className="drainage-field">
                接点类型
                <select required value={row.kind}
                  onChange={(e) => {
                    const kind = options.point_kinds.find((item) => item.value === e.target.value)?.value;
                    if (kind) update(row.id, { kind });
                  }}>
                  <option value="">请选择</option>
                  {options.point_kinds.filter((item) => item.value !== "outlet").map((item) =>
                    <option key={item.value} value={item.value}>{item.label}</option>)}
                </select>
              </label>}
              {coordinates.map(([field, label]) => <div key={field}>
                {number(label, row[field], (value) => update(row.id, { [field]: value }))}
              </div>)}
            </div>
            {row.kind !== "outlet" && <div className="drainage-inputs">
              <label className="drainage-field">
                下游接点
                <select required value={row.end} onChange={(e) => update(row.id, { end: e.target.value })}>
                  <option value="">请选择</option>
                  {form.nodes.filter((other) => other.id !== row.id && (other.kind === "outlet" || other.kind === "junction"))
                    .map((other) => <option key={other.id} value={other.id}>{other.id} · {kindLabel(other.kind)}</option>)}
                </select>
              </label>
              <label className="drainage-field">
                管径写法
                <select required value={row.mark} onChange={(e) => update(row.id, {
                  mark: options.diameter_marks.find((mark) => mark === e.target.value) ?? "",
                })}>
                  <option value="">请选择</option>
                  {options.diameter_marks.map((mark) => <option key={mark}>{mark}</option>)}
                </select>
              </label>
              {number("管径", row.size_mm, (size_mm) => update(row.id, { size_mm }))}
              <div className="dialog-actions">
                <button className="quiet-button" type="button" disabled={form.nodes.length <= 4}
                  onClick={() => removePoint(row.id)} aria-label={`移除接点 ${row.id}`}>移除</button>
              </div>
            </div>}
          </fieldset>
        ))}
        <div className="dialog-actions">
          <button className="button-secondary" type="button" onClick={addPoint}
            disabled={form.nodes.length >= options.max_nodes}>添加接点</button>
          <button className="button-primary" type="submit">{busy ? "正在生成…" : "生成双出口排水图纸"}</button>
        </div>
      </fieldset>
    </form>
    {result && <section className="drawing-result" aria-labelledby="dual-drawing-result-title">
      <div className="settings-heading-row">
        <h3 id="dual-drawing-result-title">双出口排水图纸已生成</h3>
        <button className="button-primary" type="button" disabled={downloading} onClick={download}>
          {downloading ? "正在下载…" : "下载排水图纸（ZIP）"}
        </button>
      </div>
      <ul className="drawing-block">{result.sheets.map((sheet) => <li key={sheet.filename}>{sheet.title}</li>)}</ul>
      {result.outlet_ids?.map((outlet) => <div className="drawing-block" key={outlet}>
        <h4>出口 {outlet} 的管段</h4>
        <ul>{result.segments.filter((segment) => segment.outlet_id === outlet).map((segment) => (
          <li key={segment.id}>{segment.note}，管底 {segment.from_invert_mm} → {segment.to_invert_mm} mm</li>
        ))}</ul>
      </div>)}
      <p className="muted-copy">{result.limitation}</p>
    </section>}
  </>;
}
