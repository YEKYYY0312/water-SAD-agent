import { useEffect, useState } from "react";

import { ConversationPanel, ProjectSidebar } from "./workspaceComponents";
import { authApi } from "./authApi";
import { Icon } from "./icons";
import { MemoryWorkspace } from "./MemoryWorkspace";
import { SettingsPanel } from "./SettingsPanel";
import { useWorkspaceController } from "./useWorkspaceController";
import { Inspector } from "./workspaceInspector";

const INSPECTOR_KEY = "water-agent:inspector-open";
/** 与 styles.css 中检查面板改为覆盖层的断点一致。 */
const OVERLAY_QUERY = "(max-width: 78rem)";

export function Workbench() {
  const workspace = useWorkspaceController();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // 窄屏上检查面板是覆盖层，默认收起，否则一进来就挡住整个会话。
  const [inspectorOpen, setInspectorOpen] = useState(() => {
    if (typeof window === "undefined") return true;
    if (window.matchMedia?.(OVERLAY_QUERY).matches) return false;
    return window.localStorage.getItem(INSPECTOR_KEY) !== "false";
  });
  useEffect(() => {
    // 只记住宽屏上的选择：窄屏的"收起"是被迫的，不该覆盖桌面偏好。
    if (!window.matchMedia?.(OVERLAY_QUERY).matches) window.localStorage.setItem(INSPECTOR_KEY, String(inspectorOpen));
  }, [inspectorOpen]);
  // 遮罩只在覆盖层断点内渲染（CSS 控制）；侧栏只在更窄的 52rem 以下才是覆盖层。
  const overlayOpen = sidebarOpen || inspectorOpen;

  const modelState = workspace.capabilities
    ? (workspace.capabilities.real_llm ? "模型已配置" : "模型未配置")
    : "正在连接";

  const logout = async () => {
    try {
      await authApi.logout();
    } finally {
      // 即使请求失败也回登录页：本地 cookie 可能已失效，AuthGate 的 status
      // 探针会给出真实状态；用 reload 让 AuthGate 重新走一遍三态判定。
      window.location.reload();
    }
  };
  // 两个面板互斥：主区只有一处，同时打开会让「返回会话」按钮的含义变得含糊。
  const openMemory = () => {
    workspace.setSettingsOpen(false);
    workspace.setMemoryOpen((current) => !current);
    setSidebarOpen(false);
  };
  const openSettings = () => {
    workspace.setMemoryOpen(false);
    workspace.setSettingsOpen((current) => !current);
    setSidebarOpen(false);
  };
  const serviceState = workspace.pageError
    ? "attention"
    : workspace.capabilities
      ? "ready"
      : "connecting";
  const projectName = workspace.projects.find(
    (item) => item.project_id === workspace.selectedProjectId,
  )?.name || "个人会话";

  const footer = (
    <div className="sidebar-footer">
      <button type="button" className="nav-row workspace-memory-toggle" aria-pressed={workspace.memoryOpen} onClick={openMemory}>
        <Icon name="memory" /><span>{workspace.memoryOpen ? "返回会话" : "长期记忆"}</span>
      </button>
      <button type="button" className="nav-row settings-toggle" aria-pressed={workspace.settingsOpen} aria-label={workspace.settingsOpen ? "返回会话" : "设置"} title="设置" onClick={openSettings}>
        <Icon name="settings" /><span>{workspace.settingsOpen ? "返回会话" : "设置"}</span>
      </button>
      <button type="button" className="nav-row" onClick={logout} title="退出登录">
        <Icon name="logout" /><span>退出登录</span>
      </button>
      <p className="service" aria-live="polite">
        <span className={`dot ${serviceState === "ready" ? "is-ready" : serviceState === "attention" ? "is-attention" : ""}`} aria-hidden="true" />
        {modelState}{workspace.run.runId ? ` · 事件 ${workspace.run.lastEventId}` : ""}
      </p>
    </div>
  );

  return (
    <main className="workbench">
      <ProjectSidebar
        projects={workspace.projects}
        selectedProjectId={workspace.selectedProjectId}
        sessions={workspace.sessions}
        selectedThreadId={workspace.selectedThreadId}
        newProjectName={workspace.newProjectName}
        uploads={workspace.uploads}
        uploadsError={workspace.uploadsError}
        open={sidebarOpen}
        footer={footer}
        onProjectNameChange={workspace.setNewProjectName}
        onCreateProject={workspace.createProject}
        onSelectProject={(projectId) => { workspace.selectProject(projectId); setSidebarOpen(false); }}
        onSelectSession={(threadId) => { workspace.selectSession(threadId); setSidebarOpen(false); }}
        onCreateSession={() => { workspace.setMemoryOpen(false); workspace.setSettingsOpen(false); workspace.createSession(); setSidebarOpen(false); }}
        onDeleteUpload={workspace.deleteUpload}
      />
      <div className="main">
        {/* 会话面板自己在消息流里渲染错误；只有记忆/设置页需要顶部横幅。 */}
        {workspace.pageError && (workspace.memoryOpen || workspace.settingsOpen) && <div className="global-error" role="alert">{workspace.pageError}</div>}
        {workspace.settingsOpen ? (
          <section className="panel-page settings-conversation" aria-labelledby="settings-heading">
            <SettingsPanel
              capabilities={workspace.capabilities}
              onCapabilitiesChanged={workspace.refreshCapabilities}
              onBack={() => workspace.setSettingsOpen(false)}
            />
          </section>
        ) : workspace.memoryOpen ? (
          <section className="panel-page memory-conversation" aria-labelledby="memory-heading">
            <MemoryWorkspace />
          </section>
        ) : (
          <ConversationPanel
            session={workspace.selectedSession}
            scopeName={projectName}
            run={workspace.run}
            messages={workspace.messages}
            draft={workspace.draft}
            error={workspace.pageError}
            streaming={workspace.streaming}
            attachments={workspace.attachments}
            exportState={workspace.exportState}
            feedbackState={workspace.feedbackState}
            inspectorOpen={inspectorOpen}
            onDraftChange={workspace.setDraft}
            onSubmit={workspace.sendMessage}
            onCancel={workspace.cancelRun}
            onFeedback={workspace.submitFeedback}
            onAttach={workspace.attachFiles}
            onDismissAttachment={workspace.dismissAttachment}
            onDownload={workspace.downloadExport}
            onToggleInspector={() => setInspectorOpen((current) => !current)}
            onOpenSidebar={() => setSidebarOpen(true)}
          />
        )}
      </div>
      {overlayOpen && (
        <button
          type="button"
          className="overlay-scrim"
          aria-label="关闭面板"
          onClick={() => { setSidebarOpen(false); setInspectorOpen(false); }}
        />
      )}
      {!workspace.memoryOpen && !workspace.settingsOpen && (
        <Inspector run={workspace.run} capabilities={workspace.capabilities} open={inspectorOpen} onClose={() => setInspectorOpen(false)} />
      )}
    </main>
  );
}
