import type { CompatibilityStatus, CredentialSource, CredentialStatus, LlmEndpointView } from "./settingsApi";

/** 与后端 config/credentials.py 的 MIN_CREDENTIAL_LENGTH 保持一致。 */
export const MIN_CREDENTIAL_LENGTH = 8;

const CREDENTIAL_PURPOSES: Record<string, string> = {
  DEEPSEEK_API_KEY: "对话模型（OpenAI 兼容端点，缺省 DeepSeek）",
  WATER_AGENT_LLM_API_KEY: "对话模型（OpenAI 兼容端点专用 Key）",
  DASHSCOPE_API_KEY: "向量检索（OpenAI 兼容 embedding 端点，缺省阿里云百炼）＋重排（阿里云百炼，可选）",
  WATER_AGENT_EMBEDDING_API_KEY: "向量检索（OpenAI 兼容 embedding 端点专用 Key）",
};

/** 每把 Key 管什么、什么时候必须填（P7-S10：端点改到别家时不回落旧 Key——密钥只发给所属服务商）。 */
const CREDENTIAL_HINTS: Record<string, string> = {
  DEEPSEEK_API_KEY: "对话端点仍是 DeepSeek 时使用；端点改到别的服务商后不会再发出这把 Key。",
  WATER_AGENT_LLM_API_KEY: "对话端点不是 DeepSeek 时必填；配了它就优先用它。",
  DASHSCOPE_API_KEY: "同时服务向量检索（缺省百炼端点）、重排与 PDF 视觉审阅。不配也能提问：检索会走关键词模式。",
  WATER_AGENT_EMBEDDING_API_KEY: "embedding 端点不是百炼时必填；换 embedding 模型必须重建索引。",
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
  if (!MODEL_NAME.test(model.trim())) return "模型名只能含字母、数字与 . _ : / @ + -，长度 1–200";
  return null;
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
