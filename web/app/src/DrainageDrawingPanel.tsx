import { useState, type FormEvent } from "react";

import { DrawingApi, DrawingApiError, type DrainageOptions, type DrainageResponse } from "./drawingApi";
import { DualOutletDrawingPanel } from "./DualOutletDrawingPanel";
import { DRAFT_NOTICE, drawingErrorMessage } from "./drawingState";
import { drainageFormFrom, drainageRequest, type DrainageForm } from "./drainageState";

const api = new DrawingApi();
const coordinates = [["x_mm", "X 坐标"], ["y_mm", "Y 坐标"], ["invert_mm", "管底标高"]] as const;

export function DrainageDrawingPanel({ options }: { options: DrainageOptions }) {
  const [mode, setMode] = useState("single_outlet");
  const dual = options.dual_outlet;
  return <>
    {dual && <div className="dialog-form">
      <label className="drainage-field">
        排水布置
        <select value={mode} onChange={(event) => setMode(event.target.value)}>
          <option value="single_outlet">单出口合流</option>
          <option value={dual.topology}>双出口分别排出</option>
        </select>
      </label>
    </div>}
    {dual && mode === dual.topology ? <DualOutletDrawingPanel options={dual} system={options.system_type} />
      : <SingleOutletDrawingPanel options={options} />}
  </>;
}

function SingleOutletDrawingPanel({ options }: { options: DrainageOptions }) {
  const [form, setForm] = useState<DrainageForm>({});
  const [issues, setIssues] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const [result, setResult] = useState<DrainageResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [downloading, setDownloading] = useState(false);

  function update(key: string, value: string) {
    setForm((previous) => ({ ...previous, [key]: value }));
    setIssues((previous) => Object.fromEntries(Object.entries(previous).filter(([field]) => field !== key)));
    setResult(null);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setIssues({});
    setResult(null);
    setBusy(true);
    try {
      setResult(await api.generateDrainage(drainageRequest(form, options.system_type)));
    } catch (caught) {
      if (caught instanceof DrawingApiError) {
        setError(drawingErrorMessage(caught.code, caught.message));
        setIssues(Object.fromEntries(caught.fields.map((field) => [field.loc, field.message])));
      } else {
        setError(caught instanceof RangeError ? caught.message : "排水图纸未能生成，请稍后重试。");
      }
    } finally {
      setBusy(false);
    }
  }

  function fieldError(key: string) {
    return issues[key] && <span id={`drainage-error-${key}`} className="field-error">{issues[key]}</span>;
  }

  function dimension(key: string, label: string) {
    return <label className="drainage-field">
      {label}（mm）
      <input name={key} aria-invalid={Boolean(issues[key])} value={form[key] ?? ""}
        aria-describedby={issues[key] ? `drainage-error-${key}` : undefined}
        type="number" step={1} required onChange={(event) => update(key, event.target.value)} />
      {fieldError(key)}
    </label>;
  }

  function diameter(key: string, label: string) {
    return <label className="drainage-field">
      {label}
      <select aria-invalid={Boolean(issues[key])} value={form[key] ?? ""} required
        aria-describedby={issues[key] ? `drainage-error-${key}` : undefined}
        onChange={(event) => update(key, event.target.value)}>
        <option value="">请选择</option>
        {options.diameters_mm.map((size) => <option key={size} value={size}>DN{size}</option>)}
      </select>
      {fieldError(key)}
    </label>;
  }

  async function download() {
    if (!result) return;
    setDownloading(true);
    setError("");
    try {
      await api.download(result);
    } catch (caught) {
      setError(caught instanceof DrawingApiError
        ? drawingErrorMessage(caught.code, caught.message) : "排水图纸下载未完成，请重试。");
    } finally {
      setDownloading(false);
    }
  }

  return <>
    <div className="workspace-feedback feedback-info" role="note">
      {DRAFT_NOTICE}。单卫生间污废合流。坐标原点在卫生间平面左下角，X 向右，Y 向上。各点管底标高使用同一基准，可填负值。
    </div>
    {error && <p className="dialog-error" role="alert">{error}</p>}
    <form className="settings-card dialog-form drainage-form" onSubmit={submit}>
      <fieldset disabled={busy}>
        <div className="settings-heading-row">
          <h3>排水接点</h3>
          <button className="button-secondary" type="button" onClick={() => {
            setForm(drainageFormFrom(options.example)); setIssues({}); setError(""); setResult(null);
          }}>填入示例参数</button>
        </div>
        <div className="drainage-inputs">
          {dimension("room_width_mm", "卫生间宽度")}
          {dimension("room_depth_mm", "卫生间深度")}
          {diameter("collector_dn_mm", "合流管管径")}
        </div>
        {[
          ...options.fixtures.map(({ key, label }) => ({ key: `fixtures.${key}`, label, hasDiameter: true })),
          { key: "junction", label: "汇合点", hasDiameter: false },
          { key: "riser", label: "立管接驳点", hasDiameter: false },
        ].map(({ key, label, hasDiameter }) => (
          <fieldset key={key} className="drainage-point">
            <legend>{label}</legend>
            <div className="drainage-inputs">
              {coordinates.map(([field, coordinateLabel]) => <div key={field}>{dimension(`${key}.${field}`, coordinateLabel)}</div>)}
              {hasDiameter && diameter(`${key}.dn_mm`, "支管管径")}
            </div>
            {fieldError(key)}
          </fieldset>
        ))}
        <div className="dialog-actions">
          <button className="button-primary" type="submit">{busy ? "正在生成…" : "生成排水图纸"}</button>
        </div>
      </fieldset>
    </form>

    {result && <section className="drawing-result" aria-labelledby="drainage-result-title">
      <div className="settings-heading-row">
        <h3 id="drainage-result-title">排水图纸已生成</h3>
        <button className="button-primary" type="button" disabled={downloading} onClick={download}>
          {downloading ? "正在下载…" : "下载排水图纸（ZIP）"}
        </button>
      </div>
      <ul className="drawing-block">
        {result.sheets.map((sheet) => <li key={sheet.filename}>{sheet.title}</li>)}
      </ul>
      <h4>管段与流向</h4>
      <ul className="drawing-block">
        {result.segments.map((segment) => (
          <li key={segment.id}>{segment.note}，管底 {segment.from_invert_mm} → {segment.to_invert_mm} mm</li>
        ))}
      </ul>
      <p className="muted-copy">{result.limitation}</p>
    </section>}
  </>;
}
