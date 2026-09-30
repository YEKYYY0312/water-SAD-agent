import { filenameFromDisposition } from "./exportApi.ts";
import { csrfToken, WorkspaceApiError, type ApiEnvelope } from "./workspaceApi.ts";

/** 表单词汇：来自后端 `GET /api/drawings/options`（请求契约 + 规则表），前端不写死枚举。 */
export type BuildingTypeOption = {
  value: string;
  unit_label: string;
  riser_positions: string[];
  default_riser_position: string;
  routing: string;
  detail: string;
};

export type DrawingOptions = {
  system_types: string[];
  building_types: BuildingTypeOption[];
  diameters: string[];
  materials: string[];
  ranges: {
    floors: [number, number];
    units_per_floor: [number, number];
    pressure_mpa: [number, number];
    floor_height_m: [number, number];
  };
  defaults: { floor_height_m: number };
  dwg: { available: boolean; experimental: true };
  notice: string;
  rules_version: string;
};

export type PipeSpec = { dn: string; material: string };

/** 与后端 GenerationRequest 一一对应。 */
export type GenerationRequest = {
  system_type: string;
  building_type: string;
  floors: number;
  units_per_floor: number;
  riser_position: string;
  main_pipe: PipeSpec;
  branch_pipe: PipeSpec;
  flow_rate_lps: number;
  branch_flow_rate_lps?: number;
  pressure_mpa: number;
  floor_height_m: number;
  include_dwg: boolean;
};

export type CalculationRecord = {
  name: string;
  label: string;
  ok: boolean;
  values: Record<string, number>;
  missing_inputs: string[];
  assumptions: string[];
  advisory: string;
  /** 与图面流速标注同一句话，由后端给出；前端不自己格式化数字。 */
  note: string;
};

export type SheetReview = {
  pipes_reviewed: number;
  findings: number;
  not_checked: number;
  not_checked_items: string[];
  suspicious_entities: number;
  truncated: boolean;
  summary: string;
  report_text: string;
};

export type SheetResult = {
  sheet_id: string;
  title: string;
  filename: string;
  scale: string;
  pipe_segments: number;
  pipe_segments_read_back: number;
  dn_matched: number;
  entity_count: number;
  review: SheetReview;
  dwg: { status: string; experimental: true; reason: string; filename: string | null; converter_warnings: number } | null;
};

export type GenerationResponse = {
  generation_id: string;
  created_at: string;
  notice: string;
  disclaimer: string;
  riser_position: string;
  riser_position_defaulted: boolean;
  rules_version: string;
  calculations: CalculationRecord[];
  warnings: string[];
  missing_inputs: string[];
  assumptions: string[];
  sheets: SheetResult[];
  dwg_requested: boolean;
  download: { url: string; filename: string; size_bytes: number };
};

export type FieldError = { loc: string; type: string; message: string };

/** 带字段级错误的 API 错误：422 时 `fields` 指到具体字段（只有路径与原因，没有提交值）。 */
export class DrawingApiError extends WorkspaceApiError {
  readonly fields: FieldError[];

  constructor(code: string, message: string, status: number, fields: FieldError[] = []) {
    super(code, message, status);
    this.name = "DrawingApiError";
    this.fields = fields;
  }
}

type DrawingEnvelope<T> = ApiEnvelope<T> & { error?: { code: string; message: string; fields?: FieldError[] } };

async function decode<T>(response: Response): Promise<T> {
  let envelope: DrawingEnvelope<T>;
  try {
    envelope = (await response.json()) as DrawingEnvelope<T>;
  } catch {
    throw new DrawingApiError("WEB_INVALID_RESPONSE", "服务端返回了无法识别的响应", response.status);
  }
  if (!response.ok || !envelope.ok || envelope.data === undefined) {
    throw new DrawingApiError(
      envelope.error?.code ?? "WEB_REQUEST_FAILED",
      envelope.error?.message ?? "请求未完成",
      response.status,
      envelope.error?.fields ?? [],
    );
  }
  return envelope.data;
}

export class DrawingApi {
  async options(): Promise<DrawingOptions> {
    return decode<DrawingOptions>(await fetch("/api/drawings/options", { credentials: "same-origin" }));
  }

  async generate(request: GenerationRequest): Promise<GenerationResponse> {
    const headers = new Headers({ "Content-Type": "application/json" });
    const token = csrfToken();
    if (token) headers.set("X-CSRF-Token", token);
    const response = await fetch("/api/drawings/generate", {
      method: "POST",
      headers,
      body: JSON.stringify(request),
      credentials: "same-origin",
    });
    return decode<GenerationResponse>(response);
  }

  /** 取回 ZIP 并触发同页下载（与导出下载同一做法），返回文件名。 */
  async download(result: Pick<GenerationResponse, "download">): Promise<string> {
    const response = await fetch(result.download.url, { credentials: "same-origin" });
    if (!response.ok) await decode<never>(response);
    const blob = await response.blob();
    const filename = filenameFromDisposition(response.headers.get("Content-Disposition"), result.download.filename);
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    // 同步 revoke 会在部分浏览器里取消尚未开始的下载。
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    return filename;
  }
}
