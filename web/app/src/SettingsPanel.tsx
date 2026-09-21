import { FormEvent, useCallback, useEffect, useState } from "react";

import { SettingsApi, SettingsApiError, type CredentialStatus } from "./settingsApi";
import {
  credentialRows,
  modelReadinessMessage,
  modelReadinessTone,
  validateCredentialInput,
} from "./settingsState";
import type { SystemCapabilities } from "./workspaceApi";
import { CapabilityStatus } from "./workspaceInspector";

const api = new SettingsApi();

function errorInfo(error: unknown): { code: string; message: string } {
  if (error instanceof SettingsApiError) return { code: error.code, message: error.message };
  return { code: "WEB_API_ERROR", message: "请求未完成，请稍后重试。" };
}

function PanelError({ error }: { error: { code: string; message: string } | null }) {
  if (!error) return null;
  return (
    <div className="dialog-error" role="alert">
      <strong>{error.code}</strong>
      <span>{error.message}</span>
    </div>
  );
}

function CredentialCard({
  row,
  onSave,
  onClear,
}: {
  row: ReturnType<typeof credentialRows>[number];
  onSave: (name: string, value: string) => Promise<void>;
  onClear: (name: string) => Promise<void>;
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
          已保存，下一次提问即使用新密钥。
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

export function SettingsPanel({
  capabilities,
  onCapabilitiesChanged,
}: {
  capabilities: SystemCapabilities | null;
  onCapabilitiesChanged: () => void;
}) {
  const [statuses, setStatuses] = useState<CredentialStatus[] | null>(null);
  const [error, setError] = useState<{ code: string; message: string } | null>(null);

  const reload = useCallback(async () => {
    try {
      const data = await api.listCredentials();
      setStatuses(data.credentials);
      setError(null);
    } catch (caught) {
      setError(errorInfo(caught));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const save = async (name: string, value: string) => {
    await api.putCredential(name, value);
    await reload();
    // 让 topbar 的 model 指示与 Inspector 的能力状态跟着变。
    onCapabilitiesChanged();
  };

  const clear = async (name: string) => {
    await api.deleteCredential(name);
    await reload();
    onCapabilitiesChanged();
  };

  const realLlm = capabilities ? capabilities.real_llm : null;
  const rows = statuses ? credentialRows(statuses) : [];

  return (
    <section className="settings-panel" aria-labelledby="settings-heading">
      <p className="kicker">SETTINGS</p>
      <h2 id="settings-heading">模型与密钥</h2>

      <div className="settings-readiness">
        <CapabilityStatus
          label="真实模型"
          status={modelReadinessTone(realLlm)}
          message={modelReadinessMessage(realLlm)}
        />
      </div>

      <PanelError error={error} />

      {statuses === null && !error && <p className="muted-copy">正在读取凭据状态…</p>}

      {rows.length > 0 && (
        <div className="settings-card-list">
          {rows.map((row) => (
            <CredentialCard key={row.name} row={row} onSave={save} onClear={clear} />
          ))}
        </div>
      )}

      <p className="muted-copy settings-footnote">
        密钥只写不读：本页只能看到「是否已配置、来自哪一层、何时更新」，任何接口都不会
        回传密钥内容。要确认填对了，回到会话发一条消息。
      </p>
    </section>
  );
}
