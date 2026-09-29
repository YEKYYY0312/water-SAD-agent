import type { DrawingOptions, FieldError, GenerationRequest, SheetResult } from "./drawingApi.ts";

/** 执行 prompt Step 6：面板文案必须出现这句原话。 */
export const DRAFT_NOTICE = "程序生成的初稿，需设计人复核";

/** 表单状态：输入框里是字符串，提交时才转数字（空串 ≠ 0）。 */
export type DrawingForm = {
  building_type: string;
  floors: string;
  units_per_floor: string;
  riser_position: string;
  main_dn: string;
  main_material: string;
  branch_dn: string;
  branch_material: string;
  flow_rate_lps: string;
  branch_flow_rate_lps: string;
  pressure_mpa: string;
  floor_height_m: string;
  include_dwg: boolean;
};

/** 后端字段路径 → 表单字段。 */
export const FIELD_OF_LOC: Record<string, keyof DrawingForm> = {
  building_type: "building_type",
  floors: "floors",
  units_per_floor: "units_per_floor",
  riser_position: "riser_position",
  "main_pipe.dn": "main_dn",
  "main_pipe.material": "main_material",
  main_pipe: "main_dn",
  "branch_pipe.dn": "branch_dn",
  "branch_pipe.material": "branch_material",
  branch_pipe: "branch_dn",
  flow_rate_lps: "flow_rate_lps",
  branch_flow_rate_lps: "branch_flow_rate_lps",
  pressure_mpa: "pressure_mpa",
  floor_height_m: "floor_height_m",
};

export type FormErrors = Partial<Record<keyof DrawingForm, string>>;

/** 缺省值：只取 spec D1 的示例与规则表默认（立管位置），不替用户编设计参数之外的东西。 */
export function defaultForm(options: DrawingOptions): DrawingForm {
  const first = options.building_types[0];
  return {
    building_type: first?.value ?? "",
    floors: "6",
    units_per_floor: "2",
    riser_position: first?.default_riser_position ?? "",
    main_dn: options.diameters.includes("DN50") ? "DN50" : options.diameters[0] ?? "",
    main_material: options.materials[0] ?? "",
    branch_dn: options.diameters.includes("DN25") ? "DN25" : options.diameters[0] ?? "",
    branch_material: options.materials[0] ?? "",
    flow_rate_lps: "",
    branch_flow_rate_lps: "",
    pressure_mpa: "",
    floor_height_m: String(options.defaults.floor_height_m),
    include_dwg: false,
  };
}

/** 切换建筑类型时立管位置跟着换成该类型的默认值（规则表里住宅没有核心筒）。 */
export function withBuildingType(form: DrawingForm, options: DrawingOptions, value: string): DrawingForm {
  const type = options.building_types.find((item) => item.value === value);
  return { ...form, building_type: value, riser_position: type?.default_riser_position ?? "" };
}

export function unitsLabel(options: DrawingOptions, buildingType: string): string {
  const unit = options.building_types.find((item) => item.value === buildingType)?.unit_label ?? "单元";
  return unit.length === 1 ? `每层${unit}数` : `每层${unit}数量`;
}

function numberIn(value: string, [low, high]: [number, number], label: string, integer = false): string | null {
  if (!value.trim()) return `请填写${label}`;
  const number = Number(value);
  if (!Number.isFinite(number)) return `${label}必须是数字`;
  if (integer && !Number.isInteger(number)) return `${label}必须是整数`;
  if (number < low || number > high) return `${label}须在 ${low}~${high} 之间`;
  return null;
}

/** 本地校验：省一次往返；服务端仍是权威，422 的字段错误同样会标到对应格子上。 */
export function validateForm(form: DrawingForm, options: DrawingOptions): FormErrors {
  const errors: FormErrors = {};
  const set = (field: keyof DrawingForm, message: string | null) => {
    if (message) errors[field] = message;
  };
  set("floors", numberIn(form.floors, options.ranges.floors, "层数", true));
  set("units_per_floor", numberIn(form.units_per_floor, options.ranges.units_per_floor, "每层数量", true));
  set("pressure_mpa", numberIn(form.pressure_mpa, options.ranges.pressure_mpa, "供水压力"));
  set("floor_height_m", numberIn(form.floor_height_m, options.ranges.floor_height_m, "层高"));
  const flow = Number(form.flow_rate_lps);
  if (!form.flow_rate_lps.trim()) errors.flow_rate_lps = "请填写设计流量";
  else if (!Number.isFinite(flow) || flow <= 0) errors.flow_rate_lps = "设计流量必须大于 0";
  if (form.branch_flow_rate_lps.trim()) {
    const branch = Number(form.branch_flow_rate_lps);
    if (!Number.isFinite(branch) || branch <= 0) errors.branch_flow_rate_lps = "支管设计流量必须大于 0";
    else if (Number.isFinite(flow) && branch > flow) errors.branch_flow_rate_lps = "支管设计流量不得大于总设计流量";
  }
  // 管径按白名单顺序比较（DN15 … DN150），不做数值换算。
  if (options.diameters.indexOf(form.branch_dn) > options.diameters.indexOf(form.main_dn)) {
    errors.branch_dn = "支管管径不得大于主管管径";
  }
  const type = options.building_types.find((item) => item.value === form.building_type);
  if (!type) errors.building_type = "请选择建筑类型";
  else if (!type.riser_positions.includes(form.riser_position)) errors.riser_position = "请选择立管位置";
  return errors;
}

export function toRequest(form: DrawingForm, options: DrawingOptions): GenerationRequest {
  const request: GenerationRequest = {
    system_type: options.system_types[0] ?? "给水",
    building_type: form.building_type,
    floors: Number(form.floors),
    units_per_floor: Number(form.units_per_floor),
    riser_position: form.riser_position,
    main_pipe: { dn: form.main_dn, material: form.main_material },
    branch_pipe: { dn: form.branch_dn, material: form.branch_material },
    flow_rate_lps: Number(form.flow_rate_lps),
    pressure_mpa: Number(form.pressure_mpa),
    floor_height_m: Number(form.floor_height_m),
    include_dwg: form.include_dwg,
  };
  // 不填支管流量就不发送：后端会如实报"支管流速未校核"，前端不替用户补一个。
  if (form.branch_flow_rate_lps.trim()) request.branch_flow_rate_lps = Number(form.branch_flow_rate_lps);
  return request;
}

/** 422 的字段错误 → 表单格子。未登记的路径汇总到 `_form`。 */
export function fieldErrors(fields: FieldError[]): { byField: FormErrors; other: string[] } {
  const byField: FormErrors = {};
  const other: string[] = [];
  for (const field of fields) {
    const target = FIELD_OF_LOC[field.loc];
    if (target && !byField[target]) byField[target] = field.message;
    else if (!target) other.push(`${field.loc}：${field.message}`);
  }
  return { byField, other };
}

const DRAWING_ERROR_COPY: Record<string, string> = {
  GEN_INVALID_INPUT: "参数不合法，请按标出的字段修改。",
  GEN_UNSUPPORTED_SYSTEM: "本步只支持给水系统（排水、消防、热水为后续工作）。",
  GEN_RENDER_FAILED: "图纸生成失败（内部错误），请稍后重试；如持续出现请联系管理员。",
  WEB_FORBIDDEN: "只读账号不能生成图纸。",
  WEB_EXPORT_STORAGE_UNAVAILABLE: "服务器的导出目录不可写，请联系管理员。",
  WEB_NOT_FOUND: "找不到这套图纸（只有生成者可以下载）。",
  AUTH_CSRF_INVALID: "登录状态已失效，请刷新页面后重试。",
  AUTH_SESSION_INVALID: "会话已失效，请重新登录。",
};

export function drawingErrorMessage(code: string, fallback = "请求未完成"): string {
  return DRAWING_ERROR_COPY[code] ?? fallback;
}

/**
 * 复审摘要的一行。计数原样来自 S50 复审；"未检查 N 项"必须原样显示（执行 prompt Step 6），
 * 且不得出现"审查通过 / 0 缺陷"之类的说法。
 */
export function reviewLine(sheet: Pick<SheetResult, "review">): string {
  const review = sheet.review;
  return `复审（S50 原样）：已审查管段 ${review.pipes_reviewed} 条（审查器口径），待核问题 ${review.findings} 条，未检查 ${review.not_checked} 项`;
}

export function pipeLine(sheet: Pick<SheetResult, "pipe_segments" | "dn_matched">): string {
  return `图上管段 ${sheet.pipe_segments} 条，读回核对管径一致 ${sheet.dn_matched} 条`;
}

export function dwgLine(sheet: Pick<SheetResult, "dwg">): string | null {
  if (!sheet.dwg) return null;
  const state: Record<string, string> = { ok: "已生成", discarded: "已丢弃", unavailable: "不可用", failed: "失败" };
  return `DWG（实验性）${state[sheet.dwg.status] ?? sheet.dwg.status}：${sheet.dwg.reason}`;
}
