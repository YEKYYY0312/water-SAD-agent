import { parseOAuthOutcome } from "./mcpAdminState.ts";
import type { CompatibilityStatus, CredentialSource, CredentialStatus, LlmEndpointView } from "./settingsApi";

/** 与后端 config/credentials.py 的 MIN_CREDENTIAL_LENGTH 保持一致。 */
export const MIN_CREDENTIAL_LENGTH = 8;

/** 联网搜索服务的密钥槽位（P7-S12）。地址 / 提供方 / 开关是运维的，面板只写这一把密钥。 */
export const WEB_SEARCH_KEY_SLOT = "WATER_AGENT_WEB_SEARCH_API_KEY";

const CREDENTIAL_PURPOSES: Record<string, string> = {
  DEEPSEEK_API_KEY: "对话模型（OpenAI 兼容端点，缺省 DeepSeek）",
  WATER_AGENT_LLM_API_KEY: "对话模型（OpenAI 兼容端点专用 Key）",
  DASHSCOPE_API_KEY: "向量检索（OpenAI 兼容 embedding 端点，缺省阿里云百炼）＋重排（阿里云百炼，可选）",
  WATER_AGENT_EMBEDDING_API_KEY: "向量检索（OpenAI 兼容 embedding 端点专用 Key）",
  [WEB_SEARCH_KEY_SLOT]: "联网搜索（搜索服务的 API Key）",
};

/** 每把 Key 管什么、什么时候必须填（P7-S10：端点改到别家时不回落旧 Key——密钥只发给所属服务商）。 */
const CREDENTIAL_HINTS: Record<string, string> = {
  DEEPSEEK_API_KEY: "对话端点仍是 DeepSeek 时使用；端点改到别的服务商后不会再发出这把 Key。",
  WATER_AGENT_LLM_API_KEY: "对话端点不是 DeepSeek 时必填；配了它就优先用它。",
  DASHSCOPE_API_KEY: "同时服务向量检索（缺省百炼端点）、重排与 PDF 视觉审阅。不配也能提问：检索会走关键词模式。",
  WATER_AGENT_EMBEDDING_API_KEY: "embedding 端点不是百炼时必填；换 embedding 模型必须重建索引。",
  [WEB_SEARCH_KEY_SLOT]: "只有运维选定的搜索服务需要密钥时才用到；搜索服务的地址、提供方与开关由运维设置，这里改不了。",
};

const SOURCE_LABELS: Record<CredentialSource, string> = {
  // 只有运维显式设置的环境变量才会落到这一档；本面板写入的值虽然也会注入
  // 环境变量，后端会记住那是注入的，因此仍报 volume。
  env: "环境变量",
  secret_file: "Compose secret 文件",
  volume: "本面板写入",
  none: "未配置",
};

export type CredentialRow = {
  name: string;
  purpose: string;
  hint: string;
  configured: boolean;
  sourceLabel: string;
  updatedAt: string | null;
  /** 复用 StatusMark 既有的状态类名。 */
  tone: "succeeded" | "not_configured";
};

/**
 * 校验输入框里的值，返回中文错误消息；通过则返回 null。
 *
 * 与后端 validate_credential_value 同规则：拒绝含空白的值，因为密钥本身不含
 * 空格或换行，出现即说明用户误粘了整行 `export FOO=...` 或多行文本，此时
 * 静默保存只会产出一个永远认证失败的配置。
 */
export function validateCredentialInput(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) return "请填写密钥";
  if (/\s/.test(trimmed)) return "密钥不能包含空格或换行，请只粘贴密钥本身";
  if (trimmed.length < MIN_CREDENTIAL_LENGTH) return `密钥长度不足 ${MIN_CREDENTIAL_LENGTH} 个字符`;
  return null;
}

export function sourceLabel(source: CredentialSource): string {
  return SOURCE_LABELS[source] ?? source;
}

export function credentialPurpose(name: string): string {
  return CREDENTIAL_PURPOSES[name] ?? name;
}

export function credentialHint(name: string): string {
  return CREDENTIAL_HINTS[name] ?? "";
}

/** 把 mtime 渲染成本地时间；缺失或无法解析时返回 null，交由调用方省略该行。 */
export function formatUpdatedAt(value: string | null): string | null {
  if (!value) return null;
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return null;
  return parsed.toLocaleString();
}

export function credentialRows(statuses: CredentialStatus[]): CredentialRow[] {
  return statuses.map((status) => ({
    name: status.name,
    purpose: credentialPurpose(status.name),
    hint: credentialHint(status.name),
    configured: status.configured,
    sourceLabel: sourceLabel(status.source),
    updatedAt: formatUpdatedAt(status.updated_at),
    tone: status.configured ? "succeeded" : "not_configured",
  }));
}

/** 对话端点卡片自己填写的两把 Key：端点可编辑时，「其他密钥」里不再重复出同槽位的卡片。 */
export const CHAT_KEY_SLOTS: ReadonlySet<string> = new Set(["DEEPSEEK_API_KEY", "WATER_AGENT_LLM_API_KEY"]);

/**
 * 「模型与密钥」页底部「其他密钥」要列哪些行。联网搜索的槽位只在「联网搜索」页填写：
 * 那里保存后会同步刷新输入框旁「联网」开关的就绪状态，这里写则不会；两处各放一份表单也容易让人填错地方。
 */
export function otherCredentialRows(rows: CredentialRow[], endpointOwnsChatKeys: boolean): CredentialRow[] {
  return rows.filter(
    (row) => row.name !== WEB_SEARCH_KEY_SLOT && !(endpointOwnsChatKeys && CHAT_KEY_SLOTS.has(row.name)),
  );
}

/**
 * 真实模型的总体状态文案。
 *
 * real_llm 为 false 时提问会被拒为 424（诚实拒绝而不是假装回答），所以这里
 * 直说后果，而不是只显示一个状态词。
 */
export function modelReadinessMessage(realLlm: boolean | null): string {
  if (realLlm === null) return "能力状态加载中。";
  if (realLlm) return "已可发起真实模型调用。";
  return "尚未配置或未通过兼容性检查：提问会被拒绝（424）。填入对话端点对应的密钥后即刻生效，无需重启；换了端点还要先运行兼容性检查。";
}

export function modelReadinessTone(realLlm: boolean | null): string {
  if (realLlm === null) return "running";
  return realLlm ? "succeeded" : "not_configured";
}


// ---------------------------------------------------------------------------
// P7-S10：对话模型端点
// ---------------------------------------------------------------------------

/** 面板写端点时 http:// 只收这些主机（与后端 generation/endpoints.py 的 PANEL_HTTP_HOSTS 一致）。 */
const PANEL_HTTP_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "local-model"]);
const MODEL_NAME = /^[A-Za-z0-9._:/@+-]{1,200}$/;

/** 端点输入校验（与后端同规则，后端仍是唯一判定）。通过返回 null。 */
export function validateEndpointInput(baseUrl: string, model: string): string | null {
  return validateBaseUrlInput(baseUrl) ?? validateModelInput(model);
}

export function validateModelInput(model: string): string | null {
  if (!MODEL_NAME.test(model.trim())) return "模型名只能含字母、数字与 . _ : / @ + -，长度 1–200";
  return null;
}

/** 只校验 base_url（「获取模型」在选定模型之前就要用）。通过返回 null。 */
export function validateBaseUrlInput(baseUrl: string): string | null {
  const trimmed = baseUrl.trim();
  let parsed: URL;
  try {
    parsed = new URL(trimmed);
  } catch {
    return "base_url 必须是完整地址，如 https://api.example.com/v1";
  }
  if (parsed.username || parsed.password) return "base_url 不得包含用户名或密码（Key 填在下方的密钥槽位）";
  if (parsed.hash || trimmed.includes("#")) return "base_url 不得包含 #";
  if (parsed.protocol === "http:" && !PANEL_HTTP_HOSTS.has(parsed.hostname)) {
    return "base_url 只接受 https://（本机 http://localhost、http://127.0.0.1 与 http://local-model 例外）";
  }
  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") return "base_url 只接受 https://";
  return null;
}

/** 与后端 generation/endpoints.py 的 DEFAULT_CHAT_BASE_URL 一致。 */
const DEFAULT_CHAT_BASE_URL = "https://api.deepseek.com";

/**
 * 在对话端点卡片里填的 Key 要写进哪个凭据槽位：端点仍是缺省 DeepSeek 写旧的 DEEPSEEK_API_KEY，
 * 其余写专用槽位——与后端 `_key_name` 的回落规则一致（密钥只发给所属服务商）。
 */
export function endpointKeySlot(baseUrl: string): string {
  const normalized = baseUrl.trim().replace(/\/+$/, "").toLowerCase();
  return normalized === DEFAULT_CHAT_BASE_URL ? "DEEPSEEK_API_KEY" : "WATER_AGENT_LLM_API_KEY";
}

/** 下拉里的选项：取到的列表，加上当前已选的模型（它不在列表里也不能丢）。 */
export function modelChoices(models: string[], current: string): string[] {
  const trimmed = current.trim();
  return trimmed && !models.includes(trimmed) ? [trimmed, ...models] : models;
}

const SOURCE_TEXT: Record<string, string> = {
  default: "缺省（DeepSeek）",
  env: "由部署配置（环境变量）",
  panel: "本面板配置",
  local_model: "本地模型（LOCAL_MODEL_*）",
  explicit: "命令行参数",
};

export function endpointSourceLabel(source: string): string {
  return SOURCE_TEXT[source] ?? source;
}

export function lockedReasonMessage(reason: LlmEndpointView["locked_reason"]): string | null {
  if (reason === "deployment") return "由部署配置（WATER_AGENT_LLM_BASE_URL / WATER_AGENT_LLM_MODEL），面板只读。";
  if (reason === "local_model") return "本地模型路径（LOCAL_MODEL_ENABLED）启用中，与面板端点二选一。";
  return null;
}

/** 兼容性状态：缺省端点免检；其余必须有通过的报告才能提问（换对话模型的硬门槛）。 */
export function compatibilityView(status: CompatibilityStatus | undefined): { tone: string; message: string } {
  if (status === "default_exempt") return { tone: "succeeded", message: "缺省 DeepSeek 端点免检（现有质量证据在它上面取得）。" };
  if (status === "verified") return { tone: "succeeded", message: "已通过兼容性检查（七项全部通过）。" };
  return { tone: "not_configured", message: "未验证：提问会被拒绝（424 WEB_MODEL_NOT_VERIFIED），请先运行兼容性检查。" };
}

export const COMPATIBILITY_BILLING_NOTE = "运行检查会对当前对话端点发出约五次计费请求。";


// ---------------------------------------------------------------------------
// 设置页副栏
// ---------------------------------------------------------------------------

export type SettingsSection = "models" | "skills" | "mcp" | "web_search";

export const SETTINGS_SECTIONS: { id: SettingsSection; label: string; adminOnly: boolean }[] = [
  { id: "models", label: "模型与密钥", adminOnly: false },
  { id: "skills", label: "Skill 库", adminOnly: false },
  { id: "mcp", label: "MCP 扩展", adminOnly: true },
  { id: "web_search", label: "联网搜索", adminOnly: false },
];

/**
 * 副栏里看得到的分类。`isAdmin` 为 null（还不知道）时先按"可能是管理员"显示，
 * 读到明确的非管理员再收起——MCP 扩展区对非管理员本来就整块不渲染，留着只会是一个空页。
 */
export function visibleSections(isAdmin: boolean | null): typeof SETTINGS_SECTIONS {
  return SETTINGS_SECTIONS.filter((section) => !section.adminOnly || isAdmin !== false);
}

/** 当前分类被收起（权限变化）时退回第一项。 */
export function clampSection(section: SettingsSection, isAdmin: boolean | null): SettingsSection {
  return visibleSections(isAdmin).some((item) => item.id === section) ? section : "models";
}

/** MCP 授权回调 303 回首页时地址栏带着结果：此时设置页应直接停在 MCP 扩展，否则结果提示没有地方显示。 */
export function oauthReturnedToSettings(search: string): boolean {
  return parseOAuthOutcome(search) !== null;
}

export function initialSettingsSection(search: string): SettingsSection {
  return oauthReturnedToSettings(search) ? "mcp" : "models";
}
