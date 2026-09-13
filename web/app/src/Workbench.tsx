import {
  ConversationPanel,
  ProjectSidebar,
} from "./workspaceComponents";
import { MemoryWorkspace } from "./MemoryWorkspace";
import { useWorkspaceController } from "./useWorkspaceController";
import { Inspector } from "./workspaceInspector";

export function Workbench() {
  const workspace = useWorkspaceController();
  const modelState = workspace.capabilities
    ? (workspace.capabilities.real_llm ? "real model" : "mock / 未配置")
    : "loading";
  const serviceState = workspace.pageError
    ? "attention"
    : workspace.capabilities
      ? "ready"
      : "connecting";
  const projectName = workspace.projects.find(
    (item) => item.project_id === workspace.selectedProjectId,
  )?.name || "personal";

  return (
    <main className="workbench">
      <header className="topbar">
        <div className="brand" aria-label="给排水 Agent">
          <span className="brand-mark" aria-hidden="true">WA</span>
          <div><h1>给排水 Agent 工作台</h1><p>TRACEABLE ENGINEERING WORKSPACE</p></div>
        </div>
        <div className="topbar-context">
          <span>project / {projectName}</span>
          <span>model / {modelState}</span>
          <span>● {serviceState}</span>
          <button type="button" className="workspace-memory-toggle" aria-pressed={workspace.memoryOpen} onClick={() => workspace.setMemoryOpen((current) => !current)}>{workspace.memoryOpen ? "返回会话" : "长期记忆"}</button>
        </div>
        <button type="button" className="workspace-memory-toggle mobile-memory-toggle" aria-pressed={workspace.memoryOpen} onClick={() => workspace.setMemoryOpen((current) => !current)}>{workspace.memoryOpen ? "返回会话" : "长期记忆"}</button>
      </header>
      {workspace.pageError && <div className="global-error" role="alert">{workspace.pageError}</div>}
      <div className="workspace">
        <ProjectSidebar
          projects={workspace.projects}
          selectedProjectId={workspace.selectedProjectId}
          sessions={workspace.sessions}
          selectedThreadId={workspace.selectedThreadId}
          newProjectName={workspace.newProjectName}
          onProjectNameChange={workspace.setNewProjectName}
          onCreateProject={workspace.createProject}
          onSelectProject={workspace.selectProject}
          onSelectSession={workspace.selectSession}
          onCreateSession={workspace.createSession}
        />
        {workspace.memoryOpen ? (
          <section className="conversation memory-conversation" aria-labelledby="memory-heading">
            <MemoryWorkspace />
          </section>
        ) : (
          <ConversationPanel
            session={workspace.selectedSession}
            run={workspace.run}
            messages={workspace.messages}
            draft={workspace.draft}
            error={workspace.pageError}
            streaming={workspace.streaming}
            onDraftChange={workspace.setDraft}
            onSubmit={workspace.sendMessage}
            onCancel={workspace.cancelRun}
            onFeedback={workspace.submitFeedback}
            feedbackState={workspace.feedbackState}
          />
        )}
        <Inspector run={workspace.run} capabilities={workspace.capabilities} />
      </div>
      <footer className="statusbar">
        <span>● /api/runs/{workspace.run.runId ? workspace.run.runId.slice(0, 8) : "—"}</span>
        <span>cursor {workspace.run.lastEventId}</span>
        <span>API envelope v1</span>
        <span>UTF-8 · LF</span>
      </footer>
    </main>
  );
}
