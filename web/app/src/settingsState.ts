import type { CredentialSource, CredentialStatus } from "./settingsApi";

/** 与后端 config/credentials.py 的 MIN_CREDENTIAL_LENGTH 保持一致。 */
export const MIN_CREDENTIAL_LENGTH = 8;

const CREDENTIAL_PURPOSES: Record<string, string> = {
  DEEPSEEK_API_KEY: "对话生成（DeepSeek）",
  DASHSCOPE_API_KEY: "向量检索与重排（阿里云百炼）",
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
  return "尚未配置：提问会被拒绝（424），填入下方任一对话密钥后即刻生效，无需重启。";
}

export function modelReadinessTone(realLlm: boolean | null): string {
  if (realLlm === null) return "running";
  return realLlm ? "succeeded" : "not_configured";
}
