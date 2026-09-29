import { useEffect, useState, type FormEvent, type ReactNode } from "react";

import { AnswerMarkdown } from "./AnswerMarkdown";
import { DrawingApi, DrawingApiError, type DrawingOptions, type GenerationResponse } from "./drawingApi.ts";
import {
  DRAFT_NOTICE,
  defaultForm,
  drawingErrorMessage,
  dwgLine,
  fieldErrors,
  pipeLine,
  reviewLine,
  toRequest,
  unitsLabel,
  validateForm,
  withBuildingType,
  type DrawingForm,
  type FormErrors,
} from "./drawingState.ts";

const api = new DrawingApi();

type PanelProblem = { code: string; message: string; details: string[] };

function problemOf(error: unknown): PanelProblem {
  if (error instanceof DrawingApiError) {
    const { other } = fieldErrors(error.fields);
    return { code: error.code, message: drawingErrorMessage(error.code, error.message), details: other };
  }
  return { code: "WEB_API_ERROR", message: "请求未完成，请检查服务端状态后重试。", details: [] };
}

function Problem({ problem }: { problem: PanelProblem | null }) {
  if (!problem) return null;
  return (
    <div className="dialog-error" role="alert">
      <strong>{problem.code}</strong>
      <span>{problem.message}</span>
      {problem.details.map((line) => (
        <span key={line}>{line}</span>
      ))}
    </div>
  );
}

function Field({ label, error, children }: { label: string; error?: string; children: ReactNode }) {
  return (
    <label className={error ? "has-error" : undefined}>
      {label}
      {children}
      {error && <span className="field-error" role="alert">{error}</span>}
    </label>
  );
}

function Result({ result, onDownload, downloading }: { result: GenerationResponse; onDownload: () => void; downloading: boolean }) {
  return (
    <section className="drawing-result" aria-labelledby="drawing-result-heading">
      <div className="settings-heading-row">
        <h3 id="drawing-result-heading">已生成 · {result.download.filename}</h3>
        <button type="button" className="button-primary" onClick={onDownload} disabled={downloading}>
          {downloading ? "正在下载…" : "下载图纸（ZIP）"}
        </button>
      </div>
      <div className="workspace-feedback feedback-info" role="note">
        {result.notice}。{result.disclaimer}
      </div>

      <div className="drawing-block">
        <h4>计算结果（tools/hydraulics.py::calc_velocity）</h4>
        <ul>
          {result.calculations.map((record) => (
            <li key={record.name}>{record.note}</li>
          ))}
        </ul>
      </div>

      {result.warnings.length > 0 && (
        <div className="warning-state drawing-block" role="status">
          <strong>提示</strong>
          <ul>
            {result.warnings.map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      )}
      {result.missing_inputs.length > 0 && (
        <div className="drawing-block">
          <h4>缺输入（不补默认值）</h4>
          <ul>
            {result.missing_inputs.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="drawing-sheets">
        {result.sheets.map((sheet) => (
          <article className="settings-card drawing-sheet" key={sheet.sheet_id}>
            <h4>
              {sheet.title} <code>{sheet.filename}</code>
            </h4>
            <p className="muted-copy">{pipeLine(sheet)}</p>
            <p>{reviewLine(sheet)}</p>
            {/* 复审结论与未检查项是审查器原文（含 **加粗**），按 Markdown 渲染但不改一个字。 */}
            <div className="drawing-review-text">
              <AnswerMarkdown text={`结论（原文）：${sheet.review.summary}`} />
            </div>
            {sheet.review.not_checked_items.length > 0 && (
              <div className="drawing-unchecked">
                <AnswerMarkdown text={sheet.review.not_checked_items.map((item) => `- ${item}`).join("\n")} />
              </div>
            )}
            {dwgLine(sheet) && <p className="muted-copy">{dwgLine(sheet)}</p>}
            <details>
              <summary>复审报告全文</summary>
              <pre className="drawing-report">{sheet.review.report_text}</pre>
            </details>
          </article>
        ))}
      </div>

      {result.assumptions.length > 0 && (
        <details className="drawing-block">
          <summary>假设与已知局限（{result.assumptions.length} 条）</summary>
          <ul>
            {result.assumptions.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}

export function DrawingPanel({ onBack }: { onBack?: () => void }) {
  const [options, setOptions] = useState<DrawingOptions | null>(null);
  const [form, setForm] = useState<DrawingForm | null>(null);
  const [errors, setErrors] = useState<FormErrors>({});
  const [problem, setProblem] = useState<PanelProblem | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<GenerationResponse | null>(null);
  const [downloading, setDownloading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .options()
      .then((loaded) => {
        if (cancelled) return;
        setOptions(loaded);
        setForm(defaultForm(loaded));
      })
      .catch((caught) => {
        if (!cancelled) setProblem(problemOf(caught));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const update = (field: keyof DrawingForm, value: string | boolean) => {
    setForm((current) => (current ? { ...current, [field]: value } : current));
    setErrors((current) => ({ ...current, [field]: undefined }));
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!options || !form) return;
    const local = validateForm(form, options);
    setErrors(local);
    if (Object.keys(local).length > 0) return;
    setBusy(true);
    setProblem(null);
    setResult(null);
    try {
      setResult(await api.generate(toRequest(form, options)));
    } catch (caught) {
      if (caught instanceof DrawingApiError) setErrors(fieldErrors(caught.fields).byField);
      setProblem(problemOf(caught));
    } finally {
      setBusy(false);
    }
  };

  const download = async () => {
    if (!result) return;
    setDownloading(true);
    try {
      await api.download(result);
    } catch (caught) {
      setProblem(problemOf(caught));
    } finally {
      setDownloading(false);
    }
  };

  const type = options && form ? options.building_types.find((item) => item.value === form.building_type) : undefined;

  return (
    <section className="settings-panel drawing-panel" aria-labelledby="drawing-heading">
      <div className="settings-heading-row">
        <h2 id="drawing-heading">生成给水图纸</h2>
        {onBack && (
          <button type="button" className="quiet-button" onClick={onBack}>
            返回会话
          </button>
        )}
      </div>
      <div className="workspace-feedback feedback-info" role="note">
        {DRAFT_NOTICE}。按参数由程序确定性生成标准层平面图、给水系统轴测图、节点大样图三张 DXF；只做给水系统，不画建筑结构、不标长度尺寸，未考虑结构梁高与其他专业管线。
      </div>

      <Problem problem={problem} />
      {!options && !problem && <p className="muted-copy">正在读取表单选项…</p>}

      {options && form && (
        <form className="settings-card dialog-form drawing-form" onSubmit={submit} noValidate>
          <div className="form-grid">
            <Field label="系统类型">
              <select value={options.system_types[0]} disabled>
                {options.system_types.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </Field>
            <Field label="建筑类型" error={errors.building_type}>
              <select value={form.building_type} onChange={(e) => setForm(withBuildingType(form, options, e.target.value))}>
                {options.building_types.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.value}（{item.routing}）
                  </option>
                ))}
              </select>
            </Field>
            <Field label="层数" error={errors.floors}>
              <input type="number" inputMode="numeric" min={options.ranges.floors[0]} max={options.ranges.floors[1]} step={1} value={form.floors} onChange={(e) => update("floors", e.target.value)} />
            </Field>
            <Field label={unitsLabel(options, form.building_type)} error={errors.units_per_floor}>
              <input type="number" inputMode="numeric" min={options.ranges.units_per_floor[0]} max={options.ranges.units_per_floor[1]} step={1} value={form.units_per_floor} onChange={(e) => update("units_per_floor", e.target.value)} />
            </Field>
            <Field label="立管位置" error={errors.riser_position}>
              <select value={form.riser_position} onChange={(e) => update("riser_position", e.target.value)}>
                {(type?.riser_positions ?? []).map((value) => (
                  <option key={value} value={value}>
                    {value}
                    {value === type?.default_riser_position ? "（规则默认）" : ""}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="层高（m）" error={errors.floor_height_m}>
              <input type="number" inputMode="decimal" min={options.ranges.floor_height_m[0]} max={options.ranges.floor_height_m[1]} step={0.1} value={form.floor_height_m} onChange={(e) => update("floor_height_m", e.target.value)} />
            </Field>
            <Field label="主管管径" error={errors.main_dn}>
              <select value={form.main_dn} onChange={(e) => update("main_dn", e.target.value)}>
                {options.diameters.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </Field>
            <Field label="主管材质" error={errors.main_material}>
              <select value={form.main_material} onChange={(e) => update("main_material", e.target.value)}>
                {options.materials.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </Field>
            <Field label="支管管径" error={errors.branch_dn}>
              <select value={form.branch_dn} onChange={(e) => update("branch_dn", e.target.value)}>
                {options.diameters.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </Field>
            <Field label="支管材质" error={errors.branch_material}>
              <select value={form.branch_material} onChange={(e) => update("branch_material", e.target.value)}>
                {options.materials.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </Field>
            <Field label="设计流量（L/s）" error={errors.flow_rate_lps}>
              <input type="number" inputMode="decimal" min={0} step={0.01} value={form.flow_rate_lps} onChange={(e) => update("flow_rate_lps", e.target.value)} />
            </Field>
            <Field label="支管设计流量（L/s，可不填）" error={errors.branch_flow_rate_lps}>
              <input type="number" inputMode="decimal" min={0} step={0.01} placeholder="不填则支管流速不校核" value={form.branch_flow_rate_lps} onChange={(e) => update("branch_flow_rate_lps", e.target.value)} />
            </Field>
            <Field label="供水压力（MPa）" error={errors.pressure_mpa}>
              <input type="number" inputMode="decimal" min={options.ranges.pressure_mpa[0]} max={options.ranges.pressure_mpa[1]} step={0.01} value={form.pressure_mpa} onChange={(e) => update("pressure_mpa", e.target.value)} />
            </Field>
          </div>
          <label className="choice-row">
            <input type="checkbox" checked={form.include_dwg} disabled={!options.dwg.available} onChange={(e) => update("include_dwg", e.target.checked)} />
            同时输出 DWG（实验性：LibreDWG 写出、回读核对，未在 AutoCAD/天正中验证
            {options.dwg.available ? "" : "；服务器未安装转换器"}）
          </label>
          <p className="field-hint">
            布置规则来自规则表 {options.rules_version}（多为工程惯例，需人工确认）；流速只由 calc_velocity 计算，超出建议区间只提示、不阻断。
          </p>
          <div className="dialog-actions">
            <button type="submit" className="button-primary" disabled={busy}>
              {busy ? "正在生成…" : "生成图纸"}
            </button>
          </div>
        </form>
      )}

      {result && <Result result={result} onDownload={download} downloading={downloading} />}
    </section>
  );
}
