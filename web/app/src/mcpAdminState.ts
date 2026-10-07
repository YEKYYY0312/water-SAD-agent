import type { McpLayer, McpProbe, McpServerView } from "./mcpAdminApi.ts";

/** 面板上的固定说明（与后端 NOTICE 同义；后端没给时用它）。 */
export const MCP_ADMIN_NOTICE =
  "改动从下一个 run 生效；命令、参数、环境变量与工作目录只能由运维在部署覆盖层修改";

/** 与后端 mcp_admin_routes.MAX_TOOL_NAME_LENGTH 一致。 */
export const MAX_TOOL_NAME_LENGTH = 128;

/** 与后端 mcp_admin_routes.MAX_IMPORT_CHARACTERS 一致：超过就不发请求，直接说明原因。 */
export const MAX_IMPORT_CHARACTERS = 16_000;

/** 导入框的前置检查；通过返回 null。 */
export function validateImportText(text: string): string | null {
  if (!text.trim()) return "请粘贴 mcpServers 配置";
  if (text.length > MAX_IMPORT_CHARACTERS) {
    return `内容超过 ${MAX_IMPORT_CHARACTERS} 个字符：只粘贴 mcpServers 那一段`;
  }
  return null;
}

/** 下载导入提案时的文件名。 */
export const PROPOSAL_FILENAME = "mcp_overlay_proposal.json";

const LAYER_LABELS: Record<McpLayer, string> = {
  repo: "仓库默认层",
  overlay: "部署覆盖层",
  web: "Web 管理层",
  default: "缺省值",
};

export function layerLabel(layer: McpLayer | undefined): string {
  return layer ? (LAYER_LABELS[layer] ?? layer) : LAYER_LABELS.default;
}

/** 跳过码 → 一句人话。未登记的码原样显示。 */
const CODE_LABELS: Record<string, string> = {
  MCP_NOT_IN_ALLOWLIST: "未放行",
  MCP_DESCRIPTION_UNSAFE: "描述未通过安检，不会暴露",
  MCP_UNSUPPORTED_SCHEMA: "参数 schema 不受支持，不会暴露",
  MCP_NAME_COLLISION: "本地工具名冲突，不会暴露",
  MCP_CONFIRMATION_UNAVAILABLE: "需人工确认，本入口没有确认门",
  MCP_CONNECT_FAILED: "连接失败",
  MCP_TIMEOUT: "连接超时",
  MCP_PROTOCOL_ERROR: "协议错误",
  MCP_RESPONSE_TOO_LARGE: "响应过大",
  MCP_CREDENTIAL_MISSING: "凭据槽位还没填值",
  MCP_SERVER_DISABLED: "已停用",
  MCP_CONFIG_INVALID: "配置无法加载",
};

export function codeLabel(code: string | null): string {
  if (!code) return "已暴露";
  return CODE_LABELS[code] ?? code;
}

/** 接口错误码 → 用户能照着做的文案。未登记的码回落到服务端 message。 */
const ERROR_COPY: Record<string, string> = {
  WEB_MCP_REVISION_CONFLICT: "配置刚被别人改过，已重新读取，请再改一次。",
  WEB_MCP_SERVER_UNKNOWN: "找不到这台 server 或这个凭据槽位，请刷新后重试。",
  WEB_MCP_SERVER_DISABLED: "这台 server 已停用，启用后再探测。",
  WEB_MCP_PROBE_BUSY: "这台 server 正在探测中，请稍候。",
  WEB_MCP_ADMIN_STORE_UNAVAILABLE: "Web 管理层不可写：需运维挂载 app_mcp_admin 卷并设置 WATER_AGENT_MCP_WEB_LAYER。",
  WEB_CREDENTIAL_STORE_UNAVAILABLE: "凭据存储不可写：需运维挂载 app_credentials 卷。",
  AUTH_CSRF_INVALID: "登录状态已失效，请刷新页面后重试。",
  WEB_REQUEST_FAILED: "请求没有完成，请检查网络后重试。",
};

export function mcpAdminErrorMessage(code: string, fallback: string): string {
  return ERROR_COPY[code] ?? fallback;
}

/** 与后端同规则：1–128 个字符、不含空白。返回中文错误；通过返回 null。 */
export function validateToolName(name: string): string | null {
  if (!name) return "请填写工具名";
  if (/\s/.test(name)) return "工具名不能含空白（要与 server 的 tools/list 精确匹配）";
  if (name.length > MAX_TOOL_NAME_LENGTH) return `工具名不能超过 ${MAX_TOOL_NAME_LENGTH} 个字符`;
  return null;
}

/** 勾选 / 取消一个工具。``null``（全拒）勾上第一个工具即变成只含它的列表；顺序保持、不重复。 */
export function toggleAllowItem(allow: string[] | null, name: string, on: boolean): string[] {
  const current = allow ?? [];
  if (on) return current.includes(name) ? [...current] : [...current, name];
  return current.filter((item) => item !== name);
}

export function sameAllow(left: string[] | null, right: string[] | null): boolean {
  if (left === null || right === null) return left === right;
  return left.length === right.length && left.every((item, index) => item === right[index]);
}

export type ToolChoice = {
  name: string;
  allowed: boolean;
  /** 最近一次探测里有没有这个工具；没探测过时为 null。 */
  seen: boolean | null;
  code: string | null;
};

/**
 * 白名单编辑器的行：探测到的工具（按远端顺序）+ 白名单里有、但探测没看到的工具（可能是手工添加的，
 * 或者远端已经不提供了——如实标出来）。
 */
export function toolChoices(allow: string[] | null, probe: McpProbe | null): ToolChoice[] {
  const allowed = new Set(allow ?? []);
  const rows: ToolChoice[] = [];
  const seen = new Set<string>();
  for (const tool of probe?.tools ?? []) {
    if (seen.has(tool.name)) continue;
    seen.add(tool.name);
    rows.push({ name: tool.name, allowed: allowed.has(tool.name), seen: true, code: tool.code });
  }
  for (const name of allow ?? []) {
    if (seen.has(name)) continue;
    seen.add(name);
    rows.push({ name, allowed: true, seen: probe ? false : null, code: null });
  }
  return rows;
}

export function probeSummary(probe: McpProbe | null): string {
  if (!probe) return "还没有探测过：点「刷新工具清单」看这台 server 提供哪些工具。";
  if (probe.status === "failed") return `探测失败：${codeLabel(probe.server_code)}`;
  const exposed = probe.tools.filter((tool) => tool.exposed).length;
  const truncated = probe.truncated ? "（列表已截断）" : "";
  return `最近一次探测：${probe.tools.length} 个工具，当时暴露 ${exposed} 个${truncated}`;
}

/** 复用 CapabilityStatus 既有的状态类名。 */
export function serverTone(server: McpServerView): string {
  if (server.disabled) return "not_configured";
  if (server.path_problems.length) return "failed";
  if (server.credentials.some((slot) => !slot.configured)) return "not_configured";
  return "succeeded";
}

export function serverStatusMessage(server: McpServerView): string {
  if (server.disabled) return "已停用：不启动、不暴露任何工具";
  if (server.path_problems.length) {
    return `部署配置里的 ${server.path_problems.join("、")} 不存在：停用它，或由运维修复`;
  }
  const missing = server.credentials.filter((slot) => !slot.configured).map((slot) => slot.name);
  if (missing.length) return `凭据槽位还没填值：${missing.join("、")}；填好前不会启动`;
  if (server.allow === null) return "已启用，白名单为空（全拒）：不暴露任何工具";
  return `已启用，放行 ${server.allow.length} 个工具`;
}

export function serverTags(server: McpServerView): string[] {
  const tags: string[] = [];
  if (server.human_confirmation) tags.push("每次执行需人工确认");
  if (server.lifecycle === "shared") tags.push("共享进程");
  if (server.web_override) tags.push("Web 管理层已改动");
  return tags;
}

export function sourceSummary(server: McpServerView): string {
  return `启停来自${layerLabel(server.sources.disabled)}，白名单来自${layerLabel(server.sources.allow)}`;
}
