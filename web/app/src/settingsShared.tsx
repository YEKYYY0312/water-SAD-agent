import { FormEvent, useState } from "react";

import { SettingsApiError } from "./settingsApi.ts";
import { validateCredentialInput, type CredentialRow } from "./settingsState.ts";
import { CapabilityStatus } from "./workspaceInspector";

/** 设置页各子页共用的小件：错误条与「只写不读」的密钥卡片。 */

export function errorInfo(error: unknown): { code: string; message: string } {
  if (error instanceof SettingsApiError) return { code: error.code, message: error.message };
  return { code: "WEB_API_ERROR", message: "请求未完成，请稍后重试。" };
}

export function PanelError({ error }: { error: { code: string; message: string } | null }) {
  if (!error) return null;
  return (
    <div className="dialog-error" role="alert">
      <strong>{error.code}</strong>
      <span>{error.message}</span>
    </div>
  );
}

export function CredentialCard({
  row,
  onSave,
  onClear,
  onBack,
  savedMessage = "已保存，下一次提问即使用新密钥。",
}: {
  row: CredentialRow;
  onSave: (name: string, value: string) => Promise<void>;
  onClear: (name: string) => Promise<void>;
  onBack?: () => void;
  /** 保存成功后的提示；缺省沿用「模型与密钥」页的说法。 */
  savedMessage?: string;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code: string; message: string } | null>(null);
  const [saved, setSaved] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const complaint = validateCredentialInput(value);
    if (complaint) {
      setError({ code: "WEB_CREDENTIAL_INVALID", message: complaint });
      return;
    }
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await onSave(row.name, value);
      // 立刻丢弃输入：值已落盘，留在 DOM 里只会增加被旁人看到的机会。
      setValue("");
      setSaved(true);
    } catch (caught) {
      setError(errorInfo(caught));
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      await onClear(row.name);
      setValue("");
    } catch (caught) {
      setError(errorInfo(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="settings-card dialog-form" onSubmit={submit}>
      <div className="settings-card-head">
        <CapabilityStatus label={row.purpose} status={row.tone} message={row.sourceLabel} />
      </div>
      <code className="settings-card-name">{row.name}</code>
      {row.hint && <p className="field-hint">{row.hint}</p>}
      {row.updatedAt && <p className="field-hint">更新于 {row.updatedAt}</p>}

      <label>
        {row.configured ? "替换密钥" : "填写密钥"}
        <input
          type="password"
          value={value}
          autoComplete="off"
          spellCheck={false}
          placeholder={row.configured ? "留空则保持不变" : "sk-…"}
          onChange={(event) => setValue(event.target.value)}
          disabled={busy}
        />
      </label>
      <p className="field-hint">
        只粘贴密钥本身，不要带 <code>export</code> 或引号。保存后立即生效，无需重启。
      </p>

      <PanelError error={error} />
      {saved && (
        <div className="workspace-feedback feedback-info" role="status">
          {savedMessage}
          {onBack && (
            <button type="button" className="button-primary" onClick={onBack}>
              返回会话提问
            </button>
          )}
        </div>
      )}

      <div className="dialog-actions">
        {row.configured && (
          <button type="button" className="button-danger" onClick={clear} disabled={busy}>
            清除
          </button>
        )}
        <button type="submit" className="button-primary" disabled={busy || !value}>
          {busy ? "正在保存…" : "保存"}
        </button>
      </div>
    </form>
  );
}
