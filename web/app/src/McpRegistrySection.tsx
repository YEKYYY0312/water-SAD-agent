import { FormEvent, useCallback, useEffect, useState } from "react";

import {
  McpRegistryApi,
  type RegistryEntry,
  type RegistryProposal,
  type RegistrySearchResult,
  type RegistryStatus,
} from "./mcpRegistryApi";
import {
  PROPOSAL_HASH_HINT,
  REGISTRY_NOTICE,
  REGISTRY_PROPOSAL_FILENAME,
  entryTags,
  entryTone,
  notProposableLabel,
  registryErrorMessage,
  unsafeLabel,
  validateRegistryQuery,
} from "./mcpRegistryState";
import { WorkspaceApiError } from "./workspaceApi";

const api = new McpRegistryApi();

type ErrorInfo = { code: string; message: string };

function errorInfo(error: unknown): ErrorInfo {
  if (error instanceof WorkspaceApiError) {
    return { code: error.code, message: registryErrorMessage(error.code, error.message) };
  }
  return { code: "WEB_API_ERROR", message: "请求未完成，请稍后重试。" };
}

/**
 * 「从目录发现」（P7-S14）：管理员带外搜索运维配置的 MCP 目录，为合适的条目生成**已停用、全拒**的待审提案文本。
 *
 * 目录条目是第三方写的文本——**这里的所有内容一律按纯文本渲染**（React 文本节点，不用 dangerouslySetInnerHTML，不渲染
 * Markdown / 链接）。面板没有「连接 / 启用 / 应用 / 保存」控件：提案只是文本，只能复制或下载，由运维核对后贴进部署覆盖层。
 */
export function McpRegistrySection() {
  const [status, setStatus] = useState<RegistryStatus | null>(null);
  const [query, setQuery] = useState("");
  const [result, setResult] = useState<RegistrySearchResult | null>(null);
  const [proposal, setProposal] = useState<RegistryProposal | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<ErrorInfo | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .status()
      .then((value) => {
        if (alive) setStatus(value);
      })
      .catch(() => {
        // 读状态失败（含 403）：这一块不渲染，不影响 MCP 页的其余部分
        if (alive) setStatus(null);
      });
    return () => {
      alive = false;
    };
  }, []);

  const search = useCallback(async (event?: FormEvent) => {
    event?.preventDefault();
    const complaint = validateRegistryQuery(query);
    if (complaint) {
      setResult(null);
      setError({ code: "WEB_INVALID_REQUEST", message: complaint });
      return;
    }
    setBusy(true);
    setError(null);
    setProposal(null);
    setCopied(false);
    try {
      setResult(await api.search(query.trim()));
    } catch (caught) {
      setResult(null);
      setError(errorInfo(caught));
    } finally {
      setBusy(false);
    }
  }, [query]);

  const propose = async (entry: RegistryEntry) => {
    setBusy(true);
    setError(null);
    setCopied(false);
    try {
      // 请求体只有 name 与 version：服务端重新从目录取条目
      setProposal((await api.propose(entry.name, entry.version)).proposal);
    } catch (caught) {
      setProposal(null);
      setError(errorInfo(caught));
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    if (!proposal) return;
    try {
      await navigator.clipboard.writeText(proposal.proposal_text);
      setCopied(true);
    } catch {
      setError({ code: "WEB_API_ERROR", message: "浏览器不允许写剪贴板：请改用下载。" });
    }
  };

  const download = () => {
    if (!proposal) return;
    const url = URL.createObjectURL(new Blob([proposal.proposal_text], { type: "application/json" }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = REGISTRY_PROPOSAL_FILENAME;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  if (status === null) return null;

  return (
    <form className="settings-card dialog-form mcp-registry" onSubmit={search} aria-label="从目录发现 MCP server">
      <h3>从目录发现</h3>
      <p className="field-hint">{status.notice || REGISTRY_NOTICE}</p>
      {!status.configured ? (
        <p className="muted-copy">运维还没有配置 MCP 目录（WATER_AGENT_MCP_REGISTRY_URL），这里没有可搜索的目录。</p>
      ) : (
        <>
          <p className="field-hint">
            目录：<code>{status.registry_host}</code>
            {status.allow_private_network ? "（运维已放行私有网络）" : ""}
          </p>
          <input
            type="search"
            value={query}
            maxLength={64}
            spellCheck={false}
            autoComplete="off"
            placeholder="搜索词（留空 = 浏览）"
            onChange={(event) => setQuery(event.target.value)}
            disabled={busy}
          />
          <div className="dialog-actions">
            <button type="submit" className="button-primary" disabled={busy}>
              {busy ? "正在读取…" : "搜索"}
            </button>
          </div>
        </>
      )}

      {error && (
        <div className="dialog-error" role="alert">
          <strong>{error.code}</strong>
          <span>{error.message}</span>
        </div>
      )}

      {result && (
        <div className="mcp-import-result">
          <p className="field-hint">
            共 {result.count} 条（读取 {result.pages} 页{result.truncated ? "，还有更多未读取" : ""}）
          </p>
          <ul className="mcp-tool-list">
            {result.entries.map((entry) => (
              <li key={`${entry.name}@${entry.version}-${entry.status}`}>
                <span>
                  <code>
                    {entry.name}@{entry.version}
                  </code>{" "}
                  <span className={`capability-state status-${entryTone(entry)}`}>{entryTags(entry).join(" · ")}</span>
                  {entry.status === "unsafe" ? (
                    <span className="mcp-note">{unsafeLabel(entry.unsafe_reason)}：描述与地址不展示</span>
                  ) : (
                    <>
                      <span className="mcp-note">{entry.description}</span>
                      {entry.remotes.map((remote) => (
                        <span key={`${remote.type}-${remote.url}`} className="mcp-note">
                          {remote.type}：{remote.url}
                        </span>
                      ))}
                      {entry.packages.map((pkg) => (
                        <span key={`${pkg.registry_type}-${pkg.identifier}`} className="mcp-note">
                          包（{pkg.registry_type}）：{pkg.identifier}
                        </span>
                      ))}
                      {!entry.proposable && <span className="mcp-note">{notProposableLabel(entry.not_proposable_reason)}</span>}
                    </>
                  )}
                </span>
                {entry.proposable && (
                  <button type="button" className="button-secondary" disabled={busy} onClick={() => void propose(entry)}>
                    生成提案
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {proposal && (
        <div className="mcp-import-result">
          <h3>
            提案：{proposal.name}@{proposal.version}
          </h3>
          {proposal.notes.map((note) => (
            <span key={note} className="mcp-note">
              {note}
            </span>
          ))}
          <pre className="confirmation-args">{proposal.proposal_text}</pre>
          <p className="field-hint">
            提案哈希 <code>{proposal.proposal_hash}</code>。{PROPOSAL_HASH_HINT}
          </p>
          <div className="dialog-actions">
            <button type="button" className="button-secondary" onClick={copy}>
              {copied ? "已复制" : "复制"}
            </button>
            <button type="button" className="button-secondary" onClick={download}>
              下载
            </button>
          </div>
        </div>
      )}
    </form>
  );
}
