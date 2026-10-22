import { csrfToken, WorkspaceApiError, type ApiEnvelope } from "./workspaceApi.ts";

/**
 * skill 来自哪一级：随包内置（仓库评审）、运维只读目录（P7-S9），或运维在主机 shell 里取回并人工批准安装的
 * `installed`（P7-S13）。面板只展示来源，没有任何取回 / 批准 / 安装入口——那些只在主机上的 CLI。
 */
export type SkillSource = "builtin" | "operator" | "installed";

/** `installed` 来源独有的出处信息；后端不给路径。其余来源的对象没有这个键。 */
export type InstalledSkillMeta = {
  sha8: string;
  approved_by: string;
  approved_at: string;
  registry_host: string;
};

/** 列表里的一条：没有正文、没有运维目录路径。status 是 "enabled" 或错误码（如 SKILL_DISABLED）。 */
export type SkillView = {
  name: string;
  source: SkillSource;
  version: string;
  description: string;
  triggers: string[];
  requires_tools: string[];
  status: string;
  installed?: InstalledSkillMeta;
};

/** 未加载的 skill 只有名字与错误码：它们的 frontmatter 可能本身就是坏的，不能当可信文本展示。 */
export type SkillUnavailable = { name: string; status: string };

export type SkillListing = {
  /** 开关 WATER_AGENT_SKILLS 是否打开（关闭时 run 里没有 load_skill）。 */
  active: boolean;
  /** 只决定显示不显示启停与正文入口；真正的判定在管理接口上。 */
  can_manage: boolean;
  notice: string;
  /** Web 管理层的 revision；没配 WATER_AGENT_MCP_WEB_LAYER 时为 null（只能看不能改）。 */
  revision: number | null;
  web_layer_error: string | null;
  skills: SkillView[];
  unavailable: SkillUnavailable[];
};

export type SkillDetail = SkillView & { body: string; digest: string };

/** 启停请求体：面板只能改 disabled。 */
export type SkillPatch = { disabled: boolean; expected_revision: number };

/**
 * 宽容地读 `installed` 元数据：四个字段都得是非空字符串，否则当作没有元数据（不抛）。
 * 摘要只留前 8 位——面板展示的是出处提示，不是完整校验值。
 */
export function parseInstalledMeta(value: unknown): InstalledSkillMeta | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const pick = (key: string): string | null => {
    const field = record[key];
    return typeof field === "string" && field.trim() ? field.trim() : null;
  };
  const sha8 = pick("sha8");
  const approvedBy = pick("approved_by");
  const approvedAt = pick("approved_at");
  const registryHost = pick("registry_host");
  if (!sha8 || !approvedBy || !approvedAt || !registryHost) return null;
  return { sha8: sha8.slice(0, 8), approved_by: approvedBy, approved_at: approvedAt, registry_host: registryHost };
}

/** 只处理 installed 来源：元数据畸形就去掉该键；其他来源的对象原样返回。 */
export function normalizeSkill<T extends SkillView>(skill: T): T {
  if (typeof skill !== "object" || skill === null || skill.source !== "installed") return skill;
  const { installed, ...rest } = skill;
  const meta = parseInstalledMeta(installed);
  return (meta ? { ...rest, installed: meta } : rest) as T;
}

type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

/**
 * Skill 库的接口。列表给所有已改密的登录用户；正文与启停只给管理员（写接口带 CSRF）。
 *
 * 错误统一抛 WorkspaceApiError（带服务端错误码与 HTTP 状态）。
 */
export class SkillsApi {
  private readonly fetchImpl: FetchLike;

  constructor(fetchImpl?: FetchLike) {
    this.fetchImpl = fetchImpl ?? ((input, init) => fetch(input, init));
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers);
    if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    if (init.method && init.method !== "GET" && init.method !== "HEAD") {
      const token = csrfToken();
      if (token) headers.set("X-CSRF-Token", token);
    }
    let response: Response;
    try {
      response = await this.fetchImpl(path, { ...init, headers, credentials: "same-origin" });
    } catch {
      throw new WorkspaceApiError("WEB_REQUEST_FAILED", "请求没有发出去，请检查网络后重试", 0);
    }
    let envelope: ApiEnvelope<T> | null = null;
    try {
      envelope = (await response.json()) as ApiEnvelope<T>;
    } catch {
      envelope = null;
    }
    if (!response.ok || !envelope?.ok || envelope.data === undefined) {
      throw new WorkspaceApiError(
        envelope?.error?.code ?? "WEB_REQUEST_FAILED",
        envelope?.error?.message ?? "请求未完成",
        response.status,
      );
    }
    return envelope.data;
  }

  async listSkills(): Promise<SkillListing> {
    const listing = await this.request<SkillListing>("/api/skills");
    return Array.isArray(listing.skills) ? { ...listing, skills: listing.skills.map(normalizeSkill) } : listing;
  }

  async getSkill(name: string): Promise<{ skill: SkillDetail }> {
    const result = await this.request<{ skill: SkillDetail }>(`/api/admin/skills/${encodeURIComponent(name)}`);
    return result.skill && typeof result.skill === "object" ? { ...result, skill: normalizeSkill(result.skill) } : result;
  }

  setDisabled(name: string, patch: SkillPatch): Promise<{ name: string; disabled: boolean; revision: number }> {
    // 只发这两个键：面板改不了正文、来源或别的任何东西。
    const body: SkillPatch = { disabled: patch.disabled, expected_revision: patch.expected_revision };
    return this.request(`/api/admin/skills/${encodeURIComponent(name)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  }
}
