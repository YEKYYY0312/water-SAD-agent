import { useCallback, useEffect, useState } from "react";

import { SkillsApi, type SkillDetail, type SkillListing, type SkillView } from "./skillsApi";
import {
  SKILL_LIBRARY_NOTICE,
  canToggle,
  libraryStateMessage,
  mcpCountRows,
  skillErrorMessage,
  skillMeta,
  statusLabel,
  statusTone,
  toggleLabel,
} from "./skillsState";
import type { SystemCapabilities } from "./workspaceApi";
import { WorkspaceApiError } from "./workspaceApi";
import { CapabilityStatus } from "./workspaceInspector";

const api = new SkillsApi();

type ErrorInfo = { code: string; message: string };

function errorInfo(error: unknown): ErrorInfo {
  if (error instanceof WorkspaceApiError) {
    return { code: error.code, message: skillErrorMessage(error.code, error.message) };
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

function SkillCard({
  skill,
  listing,
  onChanged,
}: {
  skill: SkillView;
  listing: SkillListing;
  onChanged: () => Promise<void>;
}) {
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ErrorInfo | null>(null);

  const toggle = async () => {
    if (listing.revision === null) return;
    setBusy(true);
    setError(null);
    try {
      await api.setDisabled(skill.name, {
        disabled: skill.status !== "SKILL_DISABLED",
        expected_revision: listing.revision,
      });
      await onChanged();
    } catch (caught) {
      const info = errorInfo(caught);
      setError(info);
      // 别人先改了：重新读取，让 revision 与界面都回到最新
      if (info.code === "WEB_MCP_REVISION_CONFLICT") await onChanged();
    } finally {
      setBusy(false);
    }
  };

  const showBody = async () => {
    if (detail) {
      setDetail(null);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await api.getSkill(skill.name);
      setDetail(result.skill);
    } catch (caught) {
      setError(errorInfo(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className="settings-card skill-card" aria-label={`skill ${skill.name}`}>
      <div className="settings-card-head">
        <CapabilityStatus label={skill.name} status={statusTone(skill.status)} message={statusLabel(skill.status)} />
      </div>
      <p className="skill-description">{skill.description}</p>
      <dl className="mcp-meta">
        {skillMeta(skill).map((row) => (
          <div key={row.label} className="skill-meta-row">
            <dt>{row.label}</dt>
            <dd>{row.value}</dd>
          </div>
        ))}
      </dl>
      {listing.can_manage && (
        <div className="dialog-actions mcp-actions">
          <button type="button" className="button-secondary" onClick={toggle} disabled={busy || !canToggle(listing)}>
            {toggleLabel(skill)}
          </button>
          <button type="button" className="quiet-button" onClick={showBody} disabled={busy}>
            {detail ? "收起正文" : "查看正文"}
          </button>
        </div>
      )}
      {/* 正文按纯文本显示：不渲染 Markdown / HTML */}
      {detail && <pre className="skill-body">{detail.body}</pre>}
      <PanelError error={error} />
    </article>
  );
}

/**
 * 设置页里的「Skill 库」区（P7-S9）。所有已改密的登录用户都能看有哪些 skill；管理员另有启停与查看正文。
 *
 * 面板不能新增、编辑或上传 skill：内置的走仓库评审，其余只能由运维放进只读目录。启停写进 Web 管理层，
 * 从下一个 run 生效。下方的 MCP 计数来自能力端点（server 清单只在管理员的 MCP 扩展区）。
 */
export function SkillLibraryPanel({ capabilities }: { capabilities: SystemCapabilities | null }) {
  const [listing, setListing] = useState<SkillListing | null>(null);
  const [error, setError] = useState<ErrorInfo | null>(null);

  const reload = useCallback(async () => {
    try {
      setListing(await api.listSkills());
      setError(null);
    } catch (caught) {
      setError(errorInfo(caught));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const jumpToMcp = () => {
    document.getElementById("mcp-admin-heading")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <section className="skill-library" aria-labelledby="skill-library-heading">
      <div className="settings-heading-row">
        <h2 id="skill-library-heading">Skill 库</h2>
      </div>
      <p className="muted-copy">{listing?.notice ?? SKILL_LIBRARY_NOTICE}</p>

      <PanelError error={error} />
      {listing === null && !error && <p className="muted-copy">正在读取 skill 库…</p>}
      {listing && <p className="field-hint">{libraryStateMessage(listing)}</p>}
      {listing?.can_manage && listing.revision === null && (
        <p className="field-hint">未配置 Web 管理层（WATER_AGENT_MCP_WEB_LAYER）：只能查看，不能启停。</p>
      )}

      {listing && listing.skills.length === 0 && (
        <p className="muted-copy">没有可用的 skill。</p>
      )}
      {listing && listing.skills.length > 0 && (
        <div className="settings-card-list">
          {listing.skills.map((skill) => (
            <SkillCard key={`${skill.name}-${skill.status}-${listing.revision}`} skill={skill} listing={listing} onChanged={reload} />
          ))}
        </div>
      )}

      {listing && listing.unavailable.length > 0 && (
        <div className="settings-card skill-unavailable">
          <h3>未加载</h3>
          <ul className="mcp-tool-list">
            {listing.unavailable.map((item) => (
              <li key={`${item.name}-${item.status}`}>
                <code>{item.name}</code>
                <span className="field-hint">{statusLabel(item.status)}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="settings-card skill-mcp-counts" aria-label="MCP 扩展计数">
        <h3>MCP 扩展</h3>
        <dl className="mcp-meta">
          {mcpCountRows(capabilities?.mcp).map((row) => (
            <div key={row.label} className="skill-meta-row">
              <dt>{row.label}</dt>
              <dd>{row.value}</dd>
            </div>
          ))}
        </dl>
        {listing?.can_manage ? (
          <div className="dialog-actions mcp-actions">
            <button type="button" className="quiet-button" onClick={jumpToMcp}>
              查看 MCP 扩展区
            </button>
          </div>
        ) : (
          <p className="field-hint">server 清单与工具白名单只有管理员能看。</p>
        )}
      </div>
    </section>
  );
}
