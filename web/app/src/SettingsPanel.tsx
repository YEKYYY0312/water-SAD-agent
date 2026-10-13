import { FormEvent, useCallback, useEffect, useState } from "react";

import { McpAdminPanel } from "./McpAdminPanel";
import { SkillLibraryPanel } from "./SkillLibraryPanel";
import { LlmEndpointApi, SettingsApi, SettingsApiError, type CredentialStatus, type LlmEndpointView } from "./settingsApi";
import {
  COMPATIBILITY_BILLING_NOTE,
  compatibilityView,
  credentialRows,
  endpointSourceLabel,
  lockedReasonMessage,
  modelReadinessMessage,
  modelReadinessTone,
  validateCredentialInput,
  validateEndpointInput,
} from "./settingsState";
import type { SystemCapabilities } from "./workspaceApi";
import { CapabilityStatus } from "./workspaceInspector";

const api = new SettingsApi();
const endpointApi = new LlmEndpointApi();

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
  onBack,
}: {
  row: ReturnType<typeof credentialRows>[number];
  onSave: (name: string, value: string) => Promise<void>;
  onClear: (name: string) => Promise<void>;
  onBack?: () => void;
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
          已保存，下一次提问即使用新密钥。
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

/**
 * 对话模型端点（P7-S10）：任意 OpenAI 兼容端点。改端点就决定了用户问题与检索条文发往哪里，所以只有管理员能改、
 * 只收 https、改了必须重新运行兼容性检查（硬门槛，缺省 DeepSeek 免检）。Key 不在这里填，填在下方的密钥槽位。
 */
function LlmEndpointCard({ onChanged }: { onChanged: () => void }) {
  const [view, setView] = useState<LlmEndpointView | null>(null);
  const [baseUrl, setBaseUrl] = useState("");
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code: string; message: string } | null>(null);
  const [saved, setSaved] = useState(false);

  const reload = useCallback(async (): Promise<LlmEndpointView | null> => {
    try {
      const data = await endpointApi.get();
      setView(data);
      setError(null);
      return data;
    } catch (caught) {
      setError(errorInfo(caught));
      return null;
    }
  }, []);

  useEffect(() => {
    void reload().then((data) => {
      if (data?.endpoint) {
        setBaseUrl(data.endpoint.base_url ?? "");
        setModel(data.endpoint.model);
      }
    });
  }, [reload]);

  // 检查在后台跑：进行中时轮询，结束后通知上层刷新能力状态
  const running = view?.check.running ?? false;
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => {
      void reload().then((data) => {
        if (data && !data.check.running) onChanged();
      });
    }, 1500);
    return () => window.clearInterval(timer);
  }, [running, reload, onChanged]);

  if (!view) return error ? <PanelError error={error} /> : null;
  const compatibility = compatibilityView(view.compatibility?.status);
  const locked = lockedReasonMessage(view.locked_reason);
  const failing = Object.entries(view.compatibility?.report?.checks ?? {}).filter(([, status]) => status !== "passed");

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const complaint = validateEndpointInput(baseUrl, model);
    if (complaint) {
      setError({ code: "WEB_LLM_ENDPOINT_INVALID", message: complaint });
      return;
    }
    setBusy(true);
    setError(null);
    setSaved(false);
    try {
      setView(await endpointApi.put(baseUrl.trim(), model.trim()));
      setSaved(true);
      onChanged();
    } catch (caught) {
      setError(errorInfo(caught));
    } finally {
      setBusy(false);
    }
  };

  const runCheck = async () => {
    if (!window.confirm(`${COMPATIBILITY_BILLING_NOTE}继续？`)) return;
    setError(null);
    try {
      await endpointApi.runCompatibility();
      await reload();
    } catch (caught) {
      setError(errorInfo(caught));
    }
  };

  return (
    <form className="settings-card dialog-form" onSubmit={submit}>
      <div className="settings-card-head">
        <CapabilityStatus label="对话模型（OpenAI 兼容端点）" status={compatibility.tone} message={compatibility.message} />
      </div>
      {view.endpoint && (
        <p className="field-hint">
          当前：<code>{view.endpoint.host}</code> · <code>{view.endpoint.model}</code>（{endpointSourceLabel(view.endpoint.source)}）
        </p>
      )}
      {view.error && <PanelError error={view.error} />}
      {locked && <p className="field-hint">{locked}</p>}
      {view.editable && (
        <>
          <label>
            base_url
            <input value={baseUrl} spellCheck={false} placeholder="https://api.example.com/v1" onChange={(event) => setBaseUrl(event.target.value)} disabled={busy || running} />
          </label>
          <label>
            模型
            <input value={model} spellCheck={false} placeholder="deepseek-chat" onChange={(event) => setModel(event.target.value)} disabled={busy || running} />
          </label>
          <p className="field-hint">只接受 https://（本机 http://localhost、http://127.0.0.1 例外）。改了端点或模型，旧的兼容性报告即作废。</p>
        </>
      )}
      {view.api_key && (
        <p className="field-hint">
          这个端点用 <code>{view.api_key.name}</code>：{view.api_key.configured ? "已配置" : "未配置（在下方密钥槽位填写）"}
        </p>
      )}
      {view.compatibility?.report?.checked_at && (
        <p className="field-hint">上次检查 {view.compatibility.report.checked_at}{failing.length > 0 && `，未通过：${failing.map(([name]) => name).join("、")}`}</p>
      )}
      {view.check.last && !running && <p className="field-hint">最近一次运行：{view.check.last.status}</p>}
      <PanelError error={error} />
      {saved && <div className="workspace-feedback feedback-info" role="status">已保存。{compatibility.tone === "succeeded" ? "" : "运行兼容性检查通过后才能提问。"}</div>}
      {view.can_manage && (
        <div className="dialog-actions">
          <button type="button" className="quiet-button" onClick={runCheck} disabled={running || busy}>
            {running ? "检查进行中…" : "运行兼容性检查"}
          </button>
          {view.editable && (
            <button type="submit" className="button-primary" disabled={busy || running}>
              {busy ? "正在保存…" : "保存端点"}
            </button>
          )}
        </div>
      )}
      {view.can_manage && <p className="field-hint">{COMPATIBILITY_BILLING_NOTE}</p>}
    </form>
  );
}

export function SettingsPanel({
  capabilities,
  onCapabilitiesChanged,
  onBack,
}: {
  capabilities: SystemCapabilities | null;
  onCapabilitiesChanged: () => void;
  onBack?: () => void;
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
      <div className="settings-heading-row">
        <h2 id="settings-heading">模型与密钥</h2>
        {onBack && (
          <button type="button" className="quiet-button" onClick={onBack}>
            返回会话
          </button>
        )}
      </div>

      <div className="settings-readiness">
        <CapabilityStatus
          label="真实模型"
          status={modelReadinessTone(realLlm)}
          message={modelReadinessMessage(realLlm)}
        />
      </div>

      <PanelError error={error} />

      {statuses === null && !error && <p className="muted-copy">正在读取凭据状态…</p>}

      {error?.code === "AUTH_SESSION_INVALID" && (
        <div className="dialog-error" role="alert">
          <span>会话已失效。</span>
          <button
            type="button"
            className="button-primary"
            onClick={() => window.dispatchEvent(new Event("water-agent:auth-expired"))}
          >
            返回登录
          </button>
        </div>
      )}

      <div className="settings-card-list">
        <LlmEndpointCard onChanged={onCapabilitiesChanged} />
      </div>

      {rows.length > 0 && (
        <div className="settings-card-list">
          {rows.map((row) => (
            <CredentialCard key={row.name} row={row} onSave={save} onClear={clear} onBack={onBack} />
          ))}
        </div>
      )}

      <p className="muted-copy settings-footnote">
        密钥只写不读：本页只能看到「是否已配置、来自哪一层、何时更新」，任何接口都不会
        回传密钥内容。要确认填对了，回到会话发一条消息。
      </p>

      {/* 所有登录用户可看；启停与正文只给管理员（P7-S9） */}
      <SkillLibraryPanel capabilities={capabilities} />

      {/* 只有管理员看得到：读接口 403 时整块不渲染（P7-S6） */}
      <McpAdminPanel />
    </section>
  );
}
