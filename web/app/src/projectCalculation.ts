import { csrfToken, decodeEnvelope, WorkspaceApiError } from "./workspaceApi.ts";

export const PROJECT_FIELDS = [
  { key: "flow_lps", label: "设计流量", unit: "L/s", max: 5000 },
  { key: "diameter_mm", label: "管道内径", unit: "mm", max: 3000 },
  { key: "length_m", label: "管长", unit: "m" },
  { key: "friction_factor", label: "达西摩阻系数", unit: "无量纲" },
  { key: "gravity_mps2", label: "重力加速度", unit: "m/s²" },
  { key: "static_head_m", label: "静扬程", unit: "m", zeroAllowed: true },
  { key: "local_head_m", label: "局部水头损失", unit: "m", zeroAllowed: true },
  { key: "residual_head_m", label: "剩余水头", unit: "m", zeroAllowed: true },
] as const;

type FieldKey = typeof PROJECT_FIELDS[number]["key"];
export type ProjectFields = Record<FieldKey, string>;
type UserInput = { kind: "user"; input_id: string; input_set_id: string; value: number; unit: string };
type PlannedOutput = { kind: "planned_output"; node_id: string; case_id: string; artifact_slot_id: string; unit: string; output_key: string };
type PlanNode = {
  node_id: string;
  case_id: string;
  formula: string;
  depends_on: string[];
  inputs: Record<string, UserInput | PlannedOutput>;
  requested_outputs: string[];
};
export type ProjectPlan = {
  contract_version: "t27-v1";
  project_id: string;
  plan_id: string;
  branch_id: string;
  system_id: string;
  segment_id: string;
  revision: number;
  base_revision: number;
  supersedes_plan_hash: string | null;
  system: "water_supply";
  input_set_id: string;
  nodes: PlanNode[];
};

export type ProjectHead = {
  base_revision: number;
  generation: number;
  active_branch_id: string;
  active_run_id: string | null;
  active_plan?: ProjectPlan | null;
  published_runs: { run_id: string; revision: number }[];
};

export type ProjectPreview = { confirmation_id: string; plan_hash: string; plan: ProjectPlan };
type ProjectPreviewRequest = { plan: ProjectPlan; base_revision: number; generation: number };
export type ProjectNode = {
  node_id: string;
  outputs: Record<string, number>;
  units: Record<string, string>;
  assumptions: { source_node_id: string; code: string; message: string }[];
};
export type ProjectRun = {
  run_id: string;
  revision: number;
  status: string;
  candidate?: ProjectNode[];
  historical?: boolean;
  export_formats?: string[];
};

const DECIMAL = /^(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i;

export function fieldsFromPlan(plan?: ProjectPlan | null): ProjectFields {
  const values = Object.fromEntries(PROJECT_FIELDS.map(({ key }) => [key, ""])) as ProjectFields;
  for (const node of plan?.nodes ?? []) {
    for (const { key } of PROJECT_FIELDS) {
      const input = node.inputs[key];
      if (input?.kind === "user") values[key] = String(input.value);
    }
  }
  return values;
}

export function buildProjectPlan(projectId: string, head: ProjectHead, values: ProjectFields): ProjectPreviewRequest {
  const inputSetId = crypto.randomUUID();
  const inputs = new Map<FieldKey, UserInput>();
  for (const field of PROJECT_FIELDS) {
    const raw = values[field.key].trim();
    const value = Number(raw);
    if (!DECIMAL.test(raw) || !Number.isFinite(value) ||
        (value === 0 && !("zeroAllowed" in field)) ||
        ("max" in field && value > field.max)) {
      throw new RangeError(`请填写有效的${field.label}${"max" in field ? `（大于 0，且不超过 ${field.max}）` : "。"}`);
    }
    inputs.set(field.key, { kind: "user", input_id: field.key, input_set_id: inputSetId,
      value, unit: field.unit === "无量纲" ? "1" : field.unit });
  }
  const input = (key: FieldKey) => inputs.get(key)!;
  const output = (node: "v" | "hf", key: string, unit: string): PlannedOutput => ({
    kind: "planned_output", node_id: node, case_id: node, artifact_slot_id: `slot-${node}`, output_key: key, unit,
  });
  const plan: ProjectPlan = {
    contract_version: "t27-v1",
    project_id: projectId,
    plan_id: head.active_plan?.plan_id ?? "project-calculation",
    branch_id: head.active_branch_id,
    system_id: head.active_plan?.system_id ?? "water-supply",
    segment_id: head.active_plan?.segment_id ?? "pipe",
    revision: head.base_revision + 1,
    base_revision: head.base_revision,
    supersedes_plan_hash: null,
    system: "water_supply",
    input_set_id: inputSetId,
    nodes: [
      { node_id: "v", case_id: "v", formula: "velocity", depends_on: [],
        inputs: { flow_lps: input("flow_lps"), diameter_mm: input("diameter_mm") }, requested_outputs: ["velocity_mps"] },
      { node_id: "hf", case_id: "hf", formula: "head_loss_darcy_weisbach", depends_on: ["v"],
        inputs: { length_m: input("length_m"), diameter_mm: input("diameter_mm"), friction_factor: input("friction_factor"),
          gravity_mps2: input("gravity_mps2"), velocity_mps: output("v", "velocity_mps", "m/s") },
        requested_outputs: ["head_loss_m"] },
      { node_id: "H", case_id: "H", formula: "pump_head", depends_on: ["hf"],
        inputs: { static_head_m: input("static_head_m"), local_head_m: input("local_head_m"),
          residual_head_m: input("residual_head_m"), friction_head_m: output("hf", "head_loss_m", "m") },
        requested_outputs: ["pump_head_m"] },
    ],
  };
  return { plan, base_revision: head.base_revision, generation: head.generation };
}

export class ProjectCalculationApi {
  private async request<T>(path: string, payload?: object, signal?: AbortSignal): Promise<T> {
    const headers = new Headers();
    if (payload) {
      headers.set("Content-Type", "application/json");
      const token = csrfToken();
      if (token) headers.set("X-CSRF-Token", token);
    }
    return decodeEnvelope<T>(await fetch(path, { method: payload ? "POST" : "GET",
      credentials: "same-origin", cache: "no-store", headers, signal,
      body: payload ? JSON.stringify(payload) : undefined }));
  }

  head(threadId: string, signal?: AbortSignal) {
    return this.request<ProjectHead>(`/api/sessions/${encodeURIComponent(threadId)}/project-calculations`, undefined, signal);
  }

  preview(threadId: string, data: ProjectPreviewRequest) {
    return this.request<ProjectPreview>(`/api/sessions/${encodeURIComponent(threadId)}/project-calculations/preview`, data);
  }

  execute(threadId: string, preview: Pick<ProjectPreview, "confirmation_id" | "plan_hash">) {
    return this.request<{ run_id: string }>(`/api/sessions/${encodeURIComponent(threadId)}/project-calculations/execute`, {
      confirmation_id: preview.confirmation_id, expected_plan_hash: preview.plan_hash,
    });
  }

  state(runId: string, signal?: AbortSignal) {
    return this.request<ProjectRun>(`/api/runs/${encodeURIComponent(runId)}/project-calculation`, undefined, signal);
  }

  approve(runId: string, approved: boolean, reason: string) {
    return this.request<{ status: string }>(`/api/runs/${encodeURIComponent(runId)}/project-approval`, { approved, reason });
  }
}

export function projectError(error: unknown) {
  if (error instanceof WorkspaceApiError) {
    if (error.status === 403 || error.status === 404) return "当前账户无法访问此项目计算。请确认您是该项目会话的创建者，并拥有写入权限。";
    if (error.status === 409) return "当前版本或确认状态已变化。请刷新修订，核对参数后重新操作。";
    if (error.status === 422) return "请检查参数的格式和取值。";
    if (error.status === 503) return "计算或成果暂时无法读取，请刷新状态后重试。";
  }
  return error instanceof RangeError ? error.message : "操作未完成，请刷新状态后重试。";
}
