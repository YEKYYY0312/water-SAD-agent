import { useCallback, useEffect, useState } from "react";

import { SettingsApi, SettingsApiError } from "./settingsApi.ts";
import { CredentialCard, PanelError } from "./settingsShared.tsx";
import { WEB_SEARCH_KEY_SLOT } from "./settingsState.ts";
import { WebSearchApi, type WebSearchStatusView } from "./webSearchApi.ts";
import {
  WEB_SEARCH_PANEL_NOTICE,
  WEB_SEARCH_USAGE_NOTE,
  webSearchConfigError,
  webSearchKeyRow,
  webSearchKeySourceNote,
  webSearchLoadError,
  webSearchRows,
  webSearchSummary,
  webSearchTone,
} from "./webSearchState.ts";
import { CapabilityStatus } from "./workspaceInspector";

const statusApi = new WebSearchApi();
const credentialApi = new SettingsApi();

type ErrorInfo = { code: string; message: string };

function loadErrorInfo(error: unknown): ErrorInfo {
  if (error instanceof SettingsApiError) return webSearchLoadError(error.code, error.status, error.message);
  return { code: "WEB_API_ERROR", message: "请求未完成，请稍后重试。" };
}

/**
 * 「联网搜索」页的内容（纯展示，不发请求）。所有已登录用户都能看到只读状态；
 * 密钥表单只在 `can_manage` 且 provider 需要密钥（`key !== null`）时出现。
 * 这里没有任何写地址 / 提供方 / 开关的入口，也不回显任何密钥值。
 */
export function WebSearchPanelBody({
  view,
  onSaveKey,
  onClearKey,
  onBack,
}: {
  view: WebSearchStatusView;
  onSaveKey: (name: string, value: string) => Promise<void>;
  onClearKey: (name: string) => Promise<void>;
  onBack?: () => void;
}) {
  const configError = webSearchConfigError(view);
  const sourceNote = view.key ? webSearchKeySourceNote(view.key) : null;
  return (
    <>
      <p className="muted-copy">{WEB_SEARCH_PANEL_NOTICE}</p>

      <div className="settings-card web-search-status" aria-label="联网搜索状态">
        <div className="settings-card-head">
          <CapabilityStatus label="联网搜索" status={webSearchTone(view)} message={webSearchSummary(view)} />
        </div>
        <dl className="mcp-meta">
          {webSearchRows(view).map((row) => (
            <div key={row.label} className="skill-meta-row">
              <dt>{row.label}</dt>
              <dd>{row.mono ? <code>{row.value}</code> : row.value}</dd>
            </div>
          ))}
        </dl>
        <PanelError error={configError} />
      </div>

      <p className="field-hint">{WEB_SEARCH_USAGE_NOTE}</p>

      {view.can_manage && view.key && (
        <div className="settings-card-list">
          <CredentialCard
            row={webSearchKeyRow(view.key)}
            onSave={onSaveKey}
            onClear={onClearKey}
            onBack={onBack}
            savedMessage="已保存，之后勾选「联网」的消息即使用新密钥。"
          />
          {sourceNote && <p className="field-hint">{sourceNote}</p>}
        </div>
      )}
    </>
  );
}

/**
 * 设置页「联网搜索」子页（P7-S12）。读 `GET /api/settings/web-search`；管理员写 / 清除密钥走既有的
 * `PUT|DELETE /api/settings/credentials/WATER_AGENT_WEB_SEARCH_API_KEY`（只写不读）。
 * 密钥变化后重新读取本页状态，并通知工作台重读就绪状态（输入框旁的「联网」开关随之出现或消失）。
 */
export function WebSearchPanel({ onChanged, onBack }: { onChanged?: () => void; onBack?: () => void }) {
  const [view, setView] = useState<WebSearchStatusView | null>(null);
  const [error, setError] = useState<ErrorInfo | null>(null);

  const reload = useCallback(async () => {
    try {
      setView(await statusApi.get());
      setError(null);
    } catch (caught) {
      setError(loadErrorInfo(caught));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  // 目标槽位固定为 WEB_SEARCH_KEY_SLOT：本页最多只能写这一把密钥，不取决于传进来的名字
  const saveKey = async (_name: string, value: string) => {
    await credentialApi.putCredential(WEB_SEARCH_KEY_SLOT, value);
    await reload();
    onChanged?.();
  };

  const clearKey = async () => {
    await credentialApi.deleteCredential(WEB_SEARCH_KEY_SLOT);
    await reload();
    onChanged?.();
  };

  return (
    <section className="web-search-panel" aria-labelledby="web-search-heading">
      <div className="settings-heading-row">
        <h2 id="web-search-heading">联网搜索</h2>
      </div>

      <PanelError error={error} />
      {view === null && !error && <p className="muted-copy">正在读取联网搜索状态…</p>}
      {view && <WebSearchPanelBody view={view} onSaveKey={saveKey} onClearKey={clearKey} onBack={onBack} />}
    </section>
  );
}
