import { FormEvent, useCallback, useEffect, useState } from "react";

import {
  McpAdminApi,
  type McpCredentialSlot,
  type McpImportPreview,
  type McpListing,
  type McpOrphan,
  type McpProbe,
  type McpServerView,
} from "./mcpAdminApi";
import {
  MCP_ADMIN_NOTICE,
  PROPOSAL_FILENAME,
  codeLabel,
  layerLabel,
  mcpAdminErrorMessage,
  probeSummary,
  sameAllow,
  serverStatusMessage,
  serverTags,
  serverTone,
  sourceSummary,
  toggleAllowItem,
  toolChoices,
  validateToolName,
} from "./mcpAdminState";
import { validateCredentialInput } from "./settingsState";
import { WorkspaceApiError } from "./workspaceApi";
import { CapabilityStatus } from "./workspaceInspector";

const api = new McpAdminApi();

type ErrorInfo = { code: string; message: string };

function errorInfo(error: unknown): ErrorInfo {
  if (error instanceof WorkspaceApiError) {
    return { code: error.code, message: mcpAdminErrorMessage(error.code, error.message) };
  }
  return { code: "WEB_API_ERROR", message: "请求未完成，请稍后重试。" };
}

function PanelError({ error }: { error: ErrorInfo | null }) {
  if (!error) return null;
  return (
    <div className="dialog-error" role="alert">
      <strong>{error.code}</strong>
      <span>{error.message}</span>
    </div>
  );
}

/** 一个凭据槽位：只写不读。提交成功后立即清空输入框。 */
function SlotForm({
  serverId,
  slot,
  onChanged,
}: {
  serverId: string;
  slot: McpCredentialSlot;
  onChanged: () => Promise<void>;
}) {
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ErrorInfo | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const complaint = validateCredentialInput(value);
    if (complaint) {
      setError({ code: "WEB_CREDENTIAL_INVALID", message: complaint });
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.putCredential(serverId, slot.name, value);
      // 立刻丢弃输入：值已落盘，留在 DOM 里只会增加被旁人看到的机会。
      setValue("");
      await onChanged();
    } catch (caught) {
      setError(errorInfo(caught));
    } finally {
      setBusy(false);
    }
  };

  const clear = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.deleteCredential(serverId, slot.name);
      setValue("");
      await onChanged();
    } catch (caught) {
      setError(errorInfo(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <form className="mcp-slot" onSubmit={submit}>
      <label>
        <span>
          <code>{slot.name}</code> {slot.configured ? "已填写" : "未填写"}
        </span>
        <input
          type="password"
          value={value}
          autoComplete="off"
          spellCheck={false}
          placeholder={slot.configured ? "填写新值以替换" : "填写凭据"}
          onChange={(event) => setValue(event.target.value)}
          disabled={busy}
        />
      </label>
      <div className="dialog-actions">
        {slot.configured && (
          <button type="button" className="button-danger" onClick={clear} disabled={busy}>
            清除
          </button>
        )}
        <button type="submit" className="button-secondary" disabled={busy || !value}>
          保存
        </button>
      </div>
      <PanelError error={error} />
    </form>
  );
}

function ServerCard({
  server,
  revision,
  writable,
  onChanged,
}: {
  server: McpServerView;
  revision: number;
  writable: boolean;
  onChanged: () => Promise<void>;
}) {
  const [draftAllow, setDraftAllow] = useState<string[] | null>(server.allow);
  const [manual, setManual] = useState("");
  const [probe, setProbe] = useState<McpProbe | null>(server.last_probe);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ErrorInfo | null>(null);

  const run = async (action: () => Promise<unknown>, reloadAfter = true) => {
    setBusy(true);
    setError(null);
    try {
      await action();
      if (reloadAfter) await onChanged();
    } catch (caught) {
      const info = errorInfo(caught);
      setError(info);
      // 别人先改了：重新读取，让 revision 与界面都回到最新
      if (info.code === "WEB_MCP_REVISION_CONFLICT") await onChanged();
    } finally {
      setBusy(false);
    }
  };

  const toggleEnabled = () =>
    run(() => api.updateServer(server.server_id, { disabled: !server.disabled, expected_revision: revision }));
  const saveAllow = () =>
    run(() => api.updateServer(server.server_id, { allow: draftAllow, expected_revision: revision }));
  const restore = () => run(() => api.removeOverrides(server.server_id, revision));
  const refreshTools = () =>
    run(async () => {
      const result = await api.probe(server.server_id);
      setProbe(result.probe);
    }, false);

  const addManual = (event: FormEvent) => {
    event.preventDefault();
    const name = manual.trim();
    const complaint = validateToolName(name);
    if (complaint) {
      setError({ code: "WEB_MCP_CONFIG_REJECTED", message: complaint });
      return;
    }
    setDraftAllow(toggleAllowItem(draftAllow, name, true));
    setManual("");
    setError(null);
  };

  const choices = toolChoices(draftAllow, probe);
  const dirty = !sameAllow(draftAllow, server.allow);
  const editable = writable && !busy;

  return (
    <article className="settings-card mcp-server-card" aria-label={`MCP server ${server.server_id}`}>
      <div className="settings-card-head">
        <CapabilityStatus label={server.server_id} status={serverTone(server)} message={serverStatusMessage(server)} />
      </div>
      <dl className="mcp-meta">
        <dt>命令</dt>
        <dd>
          <code>{server.command_name}</code>（{layerLabel(server.sources.command)}）
        </dd>
        <dt>来源</dt>
        <dd>{sourceSummary(server)}</dd>
        {serverTags(server).length > 0 && (
          <>
            <dt>标记</dt>
            <dd className="mcp-tags">
              {serverTags(server).map((tag) => (
                <span key={tag} className="mcp-tag">
                  {tag}
                </span>
              ))}
            </dd>
          </>
        )}
      </dl>

      <div className="dialog-actions mcp-actions">
        <button type="button" className="button-secondary" onClick={toggleEnabled} disabled={!editable}>
          {server.disabled ? "启用" : "停用"}
        </button>
        <button type="button" className="button-secondary" onClick={refreshTools} disabled={busy || server.disabled}>
          刷新工具清单
        </button>
        {server.web_override && (
          <button type="button" className="quiet-button" onClick={restore} disabled={!editable}>
            恢复为部署配置
          </button>
        )}
      </div>

      <section className="mcp-allow" aria-label="工具白名单">
        <h3>工具白名单</h3>
        <p className="field-hint">{probeSummary(probe)}</p>
        {draftAllow === null && <p className="field-hint">当前全拒：不暴露这台 server 的任何工具。</p>}
        {choices.length > 0 && (
          <ul className="mcp-tool-list">
            {choices.map((choice) => (
              <li key={choice.name}>
                <label>
                  <input
                    type="checkbox"
                    checked={choice.allowed}
                    disabled={!editable}
                    onChange={(event) => setDraftAllow(toggleAllowItem(draftAllow, choice.name, event.target.checked))}
                  />
                  <code>{choice.name}</code>
                </label>
                <span className="field-hint">
                  {choice.seen === false ? "最近一次探测没有看到这个工具" : choice.seen ? codeLabel(choice.code) : "手工添加"}
                </span>
              </li>
            ))}
          </ul>
        )}
        <form className="mcp-inline-form" onSubmit={addManual}>
          <input
            type="text"
            value={manual}
            placeholder="手工添加工具名"
            spellCheck={false}
            onChange={(event) => setManual(event.target.value)}
            disabled={!editable}
          />
          <button type="submit" className="button-secondary" disabled={!editable || !manual.trim()}>
            添加
          </button>
        </form>
        <div className="dialog-actions">
          <button type="button" className="quiet-button" onClick={() => setDraftAllow(null)} disabled={!editable}>
            全拒
          </button>
          <button type="button" className="button-primary" onClick={saveAllow} disabled={!editable || !dirty}>
            保存白名单
          </button>
        </div>
        <p className="field-hint">放行不是授权：放行的工具照样经过 schema 子集、描述安检、确认门与执行期护栏。</p>
      </section>

      {server.credentials.length > 0 && (
        <section className="mcp-slots" aria-label="凭据槽位">
          <h3>凭据槽位</h3>
          <p className="field-hint">槽位名由运维声明；这里只能填值，填写后不可读回。</p>
          {server.credentials.map((slot) => (
            <SlotForm key={slot.name} serverId={server.server_id} slot={slot} onChanged={onChanged} />
          ))}
        </section>
      )}

      <PanelError error={error} />
    </article>
  );
}

function OrphanList({
  orphans,
  revision,
  onChanged,
}: {
  orphans: McpOrphan[];
  revision: number;
  onChanged: () => Promise<void>;
}) {
  const [error, setError] = useState<ErrorInfo | null>(null);
  if (!orphans.length) return null;
  const clean = async (serverId: string) => {
    setError(null);
    try {
      await api.removeOverrides(serverId, revision);
      await onChanged();
    } catch (caught) {
      setError(errorInfo(caught));
    }
  };
  return (
    <section className="settings-card" aria-label="孤立条目">
      <h3>孤立条目</h3>
      <p className="field-hint">这些 server 已从部署配置里删掉，Web 管理层里的改动不再生效。</p>
      <ul className="mcp-tool-list">
        {orphans.map((orphan) => (
          <li key={orphan.server_id}>
            <code>{orphan.server_id}</code>
            <button type="button" className="quiet-button" onClick={() => clean(orphan.server_id)}>
              清理
            </button>
          </li>
        ))}
      </ul>
      <PanelError error={error} />
    </section>
  );
}

function ImportSection() {
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<McpImportPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<ErrorInfo | null>(null);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(null);
    setCopied(false);
    try {
      const result = await api.importPreview(text);
      setPreview(result.preview);
    } catch (caught) {
      setPreview(null);
      setError(errorInfo(caught));
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    if (!preview) return;
    try {
      await navigator.clipboard.writeText(preview.proposal_text);
      setCopied(true);
    } catch {
      setError({ code: "WEB_API_ERROR", message: "浏览器不允许写剪贴板：请改用下载。" });
    }
  };

  const download = () => {
    if (!preview) return;
    const url = URL.createObjectURL(new Blob([preview.proposal_text], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = PROPOSAL_FILENAME;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  return (
    <form className="settings-card dialog-form mcp-import" onSubmit={submit} aria-label="导入 mcpServers">
      <h3>导入 mcpServers</h3>
      <p className="field-hint">
        粘贴 Claude Code / Cursor 的 <code>mcpServers</code> JSON 或 Codex 的 <code>[mcp_servers.*]</code> TOML。
        这里只生成<strong>已停用、全拒</strong>的待审提案，env 的值一律丢弃（键改成凭据槽位）；不会应用到任何配置，
        由运维审阅后贴进部署覆盖层。
      </p>
      <textarea
        rows={8}
        value={text}
        spellCheck={false}
        onChange={(event) => setText(event.target.value)}
        disabled={busy}
      />
      <div className="dialog-actions">
        <button type="submit" className="button-primary" disabled={busy || !text.trim()}>
          {busy ? "正在生成…" : "生成提案"}
        </button>
      </div>
      <PanelError error={error} />
      {preview && (
        <div className="mcp-import-result">
          <ul className="mcp-tool-list">
            {preview.entries.map((entry) => (
              <li key={entry.name}>
                <code>{entry.server_id ?? entry.name}</code>
                <span className="field-hint">
                  {entry.status === "proposed" ? "已生成提案" : entry.status === "unsupported" ? "不支持" : "不合格"}
                  {[...entry.reasons, ...entry.notes].map((line) => (
                    <span key={line} className="mcp-note">
                      {line}
                    </span>
                  ))}
                </span>
              </li>
            ))}
          </ul>
          <pre className="confirmation-args">{preview.proposal_text}</pre>
          <div className="dialog-actions">
            <button type="button" className="button-secondary" onClick={copy} disabled={!preview.proposed_count}>
              {copied ? "已复制" : "复制"}
            </button>
            <button type="button" className="button-secondary" onClick={download} disabled={!preview.proposed_count}>
              下载
            </button>
          </div>
        </div>
      )}
    </form>
  );
}

/**
 * 设置页里的「MCP 扩展」区（P7-S6）。只给管理员：读接口 403 时整块不渲染。
 *
 * 能改的只有启停、工具白名单与运维已声明的凭据槽位的值；命令、参数、环境变量名与工作目录只能由运维
 * 在部署覆盖层修改。改动写进 Web 管理层，从下一个 run 生效。
 */
export function McpAdminPanel() {
  const [listing, setListing] = useState<McpListing | null>(null);
  const [hidden, setHidden] = useState(false);
  const [error, setError] = useState<ErrorInfo | null>(null);
  const [resetting, setResetting] = useState(false);

  const reload = useCallback(async () => {
    try {
      const data = await api.listServers();
      setListing(data);
      setError(null);
    } catch (caught) {
      if (caught instanceof WorkspaceApiError && caught.status === 403) {
        setHidden(true);
        return;
      }
      setError(errorInfo(caught));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  if (hidden) return null;

  const resetLayer = async () => {
    setResetting(true);
    setError(null);
    try {
      await api.resetWebLayer();
      await reload();
    } catch (caught) {
      setError(errorInfo(caught));
    } finally {
      setResetting(false);
    }
  };

  const revision = listing?.web_layer.revision ?? 0;
  const writable = Boolean(listing?.web_layer.configured && listing.web_layer.writable && listing.web_layer.revision !== null);

  return (
    <section className="mcp-admin" aria-labelledby="mcp-admin-heading">
      <div className="settings-heading-row">
        <h2 id="mcp-admin-heading">MCP 扩展</h2>
      </div>
      <p className="muted-copy">{listing?.notice ?? MCP_ADMIN_NOTICE}</p>

      <PanelError error={error} />
      {listing === null && !error && <p className="muted-copy">正在读取 MCP 配置…</p>}

      {listing?.config_error && (
        <div className="dialog-error" role="alert">
          <strong>{listing.config_error.code}</strong>
          <span>{listing.config_error.message}</span>
        </div>
      )}
      {listing && !listing.web_layer.configured && (
        <p className="field-hint">未配置 Web 管理层（WATER_AGENT_MCP_WEB_LAYER）：只能查看，不能修改。</p>
      )}

      {listing && listing.servers.length === 0 && !listing.config_error && (
        <p className="muted-copy">部署配置里没有任何 MCP server。新增 server 只能由运维写进部署覆盖层。</p>
      )}

      {listing && listing.servers.length > 0 && (
        <div className="settings-card-list">
          {listing.servers.map((server) => (
            <ServerCard
              key={`${server.server_id}-${revision}-${server.allow?.join(",") ?? "none"}-${server.disabled}`}
              server={server}
              revision={revision}
              writable={writable}
              onChanged={reload}
            />
          ))}
        </div>
      )}

      {listing && <OrphanList orphans={listing.orphans} revision={revision} onChanged={reload} />}

      <ImportSection />

      {listing?.web_layer.configured && (
        <div className="dialog-actions">
          <button type="button" className="button-danger" onClick={resetLayer} disabled={resetting}>
            恢复为部署配置（撤回全部面板改动）
          </button>
        </div>
      )}
    </section>
  );
}
