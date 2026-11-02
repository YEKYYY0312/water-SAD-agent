import type { RegistryEntry } from "./mcpRegistryApi.ts";

/** 面板上的固定说明（与后端 NOTICE 同义；后端没给时用它）。 */
export const REGISTRY_NOTICE =
  "目录条目是第三方写的文本：这里只生成已停用、全拒的待审提案，不会连接条目里的任何地址，也不会应用到任何配置；" +
  "运维核对后把提案贴进部署覆盖层，启用与放行是之后的独立步骤";

/** 与后端 mcp/registry.py::MAX_QUERY_CHARS 一致：超过就不发请求。 */
export const MAX_QUERY_CHARS = 64;

/** 下载提案时的文件名。 */
export const REGISTRY_PROPOSAL_FILENAME = "mcp_registry_proposal.json";

/** 搜索词的前置检查（单行、≤ 64 字符、无控制字符）；通过返回 null。空串合法（= 浏览）。 */
export function validateRegistryQuery(query: string): string | null {
  const trimmed = query.trim();
  if (trimmed.length > MAX_QUERY_CHARS) return `搜索词不得超过 ${MAX_QUERY_CHARS} 个字符`;
  // 控制字符、换行，以及常见的不可见格式字符（零宽、方向控制、词连接符、BOM）。后端的字符白名单才是权威，
  // 这里只是让用户少走一次 422；后端会拒绝的字符比这里多（同形字等），前端放行的由后端兜底。
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f-\u009f\u00ad\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]/.test(trimmed)) {
    return "搜索词不能含换行、控制字符或不可见字符";
  }
  return null;
}

/** 不可提案原因（后端稳定短码）→ 一句人话。**不含条目内容**；未登记的码原样显示。 */
const NOT_PROPOSABLE_LABELS: Record<string, string> = {
  unsafe: "未通过安全检查，不可生成提案",
  no_remote: "没有远程地址（需要本地安装包或进程），本面板不生成提案",
  no_streamable_http: "没有 streamable-http 远程（stdio / 旧版 SSE 等），本面板不生成提案",
  url_rejected: "远程地址不合格（需要 https、不含用户信息 / 片段）",
  ip_literal: "远程地址是 IP 字面量，本面板不生成提案",
  local_host: "远程地址是 localhost 或单标签主机，本面板不生成提案",
  ambiguous_name: "名称含糊，不可生成提案",
};

export function notProposableLabel(reason: string | null): string {
  if (!reason) return "可生成提案";
  return NOT_PROPOSABLE_LABELS[reason] ?? reason;
}

/** `unsafe` 条目的原因短码 → 说明。故意不细分到攻击手法，只说"哪一类检查没过"。 */
const UNSAFE_LABELS: Record<string, string> = {
  injection: "描述疑似提示注入",
  charset: "含不允许的字符（不可见 / 控制 / 同形字等）",
  markup: "含边界标记、模板标记或外发语法",
  duplicate: "目录里存在同名或归一后同名的条目",
};

export function unsafeLabel(reason: string): string {
  return UNSAFE_LABELS[reason] ?? "未通过安全检查";
}

/** 条目一行的标签。 */
export function entryTags(entry: RegistryEntry): string[] {
  const tags: string[] = [];
  if (entry.status === "unsafe") tags.push("不安全");
  else if (entry.proposable) tags.push("可提案");
  else tags.push("仅展示");
  if (entry.dropped_fields > 0) tags.push(`丢弃 ${entry.dropped_fields} 个未知字段`);
  return tags;
}

/** 条目状态：复用 CapabilityStatus 既有的状态类名（succeeded / failed / not_configured）。 */
export function entryTone(entry: RegistryEntry): "succeeded" | "failed" | "not_configured" {
  if (entry.status === "unsafe") return "failed";
  return entry.proposable ? "succeeded" : "not_configured";
}

/** 接口错误码 → 用户能照着做的文案。未登记的码回落到服务端 message。 */
const ERROR_COPY: Record<string, string> = {
  WEB_MCP_REGISTRY_NOT_CONFIGURED: "运维还没有配置 MCP 目录（WATER_AGENT_MCP_REGISTRY_URL），这里没有可搜索的目录。",
  WEB_MCP_REGISTRY_RATE_LIMITED: "目录操作太频繁了，请稍后再试。",
  MCP_REGISTRY_UNAVAILABLE: "目录暂时不可用，请稍后重试。",
  MCP_REGISTRY_AUTH: "目录拒绝了这次请求（本部署不向目录发送任何凭据）。",
  MCP_REGISTRY_RATE_LIMITED: "目录触发了限流，请稍后再试。",
  MCP_REGISTRY_BAD_RESPONSE: "目录返回的内容不符合协议，整页已被拒绝，没有采纳任何条目。",
  MCP_REGISTRY_ENTRY_NOT_FOUND: "目录里已经没有这个版本的条目，请重新搜索。",
  MCP_REGISTRY_ENTRY_UNSAFE: "这个条目未通过安全检查，不能生成提案。",
  MCP_REGISTRY_NOT_PROPOSABLE: "这个条目没有可生成提案的 streamable-http 域名地址。",
  WEB_INVALID_REQUEST: "请求内容不合法：请检查搜索词（单行、不超过 64 个字符）。",
  AUTH_CSRF_INVALID: "登录状态已失效，请刷新页面后重试。",
  WEB_REQUEST_FAILED: "请求没有完成，请检查网络后重试。",
};

export function registryErrorMessage(code: string, fallback: string): string {
  return ERROR_COPY[code] ?? fallback;
}

/** 提示语：提案哈希只用于核对"贴进去的就是看到的这份"，不是签名。 */
export const PROPOSAL_HASH_HINT = "提案哈希只用来核对贴进覆盖层的就是这一份，它不是签名，也不证明条目可信。";
