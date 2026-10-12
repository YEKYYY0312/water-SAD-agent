import type { SkillListing, SkillSource, SkillView } from "./skillsApi.ts";
import type { SystemCapabilities } from "./workspaceApi.ts";

/** 面板上的固定说明（与后端 skill_routes.NOTICE 同义；后端没给时用它）。 */
export const SKILL_LIBRARY_NOTICE =
  "改动从下一个 run 生效；skill 只能经仓库评审（内置）或运维只读目录新增，面板只能启停。";

const SOURCE_LABELS: Record<SkillSource, string> = {
  builtin: "内置",
  operator: "运维目录",
};

export function sourceLabel(source: SkillSource | string): string {
  return SOURCE_LABELS[source as SkillSource] ?? source;
}

/** 状态码 → 一句人话。未登记的码原样显示。 */
const STATUS_LABELS: Record<string, string> = {
  enabled: "已启用",
  SKILL_DISABLED: "管理员已停用",
  SKILL_INVALID: "格式不合格，未加载",
  SKILL_UNSAFE: "未通过安检，未加载",
  SKILL_CONFLICTS_IRON_RULE: "与三条铁律冲突，未加载",
  SKILL_TOOL_UNAVAILABLE: "依赖的工具当前不可用",
  SKILL_SHADOWED: "与内置 skill 同名，已被内置版本遮蔽",
  SKILL_CATALOG_FULL: "目录超出长度上限，未列入",
};

export function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

/** 复用 CapabilityStatus 既有的状态类名。 */
export function statusTone(status: string): string {
  if (status === "enabled") return "succeeded";
  if (status === "SKILL_DISABLED" || status === "SKILL_SHADOWED") return "not_configured";
  return "failed";
}

/** 列表头部那一行：开关、可改性、层文件错误。 */
export function libraryStateMessage(listing: SkillListing): string {
  if (!listing.active) {
    return "Skill 机制未开启（WATER_AGENT_SKILLS=off）：run 里不会出现 load_skill，下面只是可用清单。";
  }
  if (listing.web_layer_error) return `Web 管理层无法加载（${listing.web_layer_error}）：启停状态未知，需在 MCP 扩展区恢复。`;
  const enabled = listing.skills.filter((skill) => skill.status === "enabled").length;
  return `已启用 ${enabled} 个，共 ${listing.skills.length} 个可用`;
}

/** 管理员能不能点启停：要有可写的 Web 管理层，且层文件没坏。 */
export function canToggle(listing: SkillListing): boolean {
  return listing.can_manage && listing.revision !== null && !listing.web_layer_error;
}

export function toggleLabel(skill: SkillView): string {
  return skill.status === "SKILL_DISABLED" ? "启用" : "停用";
}

export function skillMeta(skill: SkillView): { label: string; value: string }[] {
  const rows = [
    { label: "来源", value: `${sourceLabel(skill.source)} · v${skill.version}` },
  ];
  if (skill.triggers.length) rows.push({ label: "触发词", value: skill.triggers.join("、") });
  if (skill.requires_tools.length) rows.push({ label: "依赖工具", value: skill.requires_tools.join("、") });
  return rows;
}

/** 接口错误码 → 用户能照着做的文案。未登记的码回落到服务端 message。 */
const ERROR_COPY: Record<string, string> = {
  WEB_MCP_REVISION_CONFLICT: "配置刚被别人改过，已重新读取，请再改一次。",
  WEB_SKILL_UNKNOWN: "找不到这个 skill（可能已被运维删除），请刷新后重试。",
  WEB_SKILL_LIBRARY_UNAVAILABLE: "skill 库不可用：运维 skill 目录配置有误，需运维修复。",
  WEB_MCP_ADMIN_STORE_UNAVAILABLE: "Web 管理层不可写：需运维挂载 app_mcp_admin 卷并设置 WATER_AGENT_MCP_WEB_LAYER。",
  AUTH_CSRF_INVALID: "登录状态已失效，请刷新页面后重试。",
  WEB_REQUEST_FAILED: "请求没有完成，请检查网络后重试。",
};

export function skillErrorMessage(code: string, fallback: string): string {
  return ERROR_COPY[code] ?? fallback;
}

/**
 * MCP 计数卡片（P7-S9）：复用能力端点的计数，不新增 MCP 列表接口——成员不该看到 server 清单（S6 决定）。
 * 计数缺失（旧后端 / 配置坏了）显示为"未知"，不显示成 0。
 */
export function mcpCountRows(mcp: SystemCapabilities["mcp"] | undefined): { label: string; value: string }[] {
  const show = (value: number | null | undefined) => (typeof value === "number" ? String(value) : "未知");
  return [
    { label: "已配置", value: show(mcp?.configured_server_count) },
    { label: "启用", value: show(mcp?.enabled_server_count) },
    { label: "停用", value: show(mcp?.disabled_server_count) },
    { label: "需人工确认", value: show(mcp?.confirmation_server_count) },
    { label: "共享进程", value: show(mcp?.shared_server_count) },
  ];
}
