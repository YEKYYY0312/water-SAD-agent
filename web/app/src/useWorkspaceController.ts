import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";

import {
  streamRunEvents,
  WorkspaceApi,
  WorkspaceApiError,
  type Project,
  type SystemCapabilities,
  type WorkspaceSession,
} from "./workspaceApi";
import { applyRunEvent, emptyRunState, isTerminalStatus, type WorkspaceRunState } from "./workspaceState";
import type { ConversationMessage } from "./workspaceComponents";

const PROJECT_STORAGE_KEY = "water-agent:selected-project";
const SESSION_STORAGE_PREFIX = "water-agent:selected-session:";

function storedValue(key: string): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(key);
}

function saveValue(key: string, value: string | null) {
  if (typeof window === "undefined") return;
  if (value) window.localStorage.setItem(key, value);
  else window.localStorage.removeItem(key);
}

function apiMessage(error: unknown): string {
  if (error instanceof WorkspaceApiError) return `${error.code}：${error.message}`;
  return "请求未完成，请检查服务端状态后重试";
}

export type WorkspaceController = {
  capabilities: SystemCapabilities | null;
  projects: Project[];
  sessions: WorkspaceSession[];
  selectedProjectId: string | null;
  selectedThreadId: string | null;
  selectedSession: WorkspaceSession | null;
  newProjectName: string;
  draft: string;
  messages: ConversationMessage[];
  run: WorkspaceRunState;
  pageError: string | null;
  streaming: boolean;
  feedbackState: string | null;
  memoryOpen: boolean;
  setNewProjectName: (value: string) => void;
  setDraft: (value: string) => void;
  setMemoryOpen: (value: boolean | ((current: boolean) => boolean)) => void;
  createProject: (event: FormEvent<HTMLFormElement>) => void;
  selectProject: (projectId: string | null) => void;
  selectSession: (threadId: string) => void;
  createSession: () => void;
  sendMessage: (event: FormEvent<HTMLFormElement>) => void;
  cancelRun: () => void;
  submitFeedback: (rating: "useful" | "not_useful") => void;
};

export function useWorkspaceController(): WorkspaceController {
  const api = useRef(new WorkspaceApi()).current;
  const [capabilities, setCapabilities] = useState<SystemCapabilities | null>(null);
  const [projects, setProjects] = useState<Project[]>([]);
  const [sessions, setSessions] = useState<WorkspaceSession[]>([]);
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null);
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null);
  const [newProjectName, setNewProjectName] = useState("");
  const [draft, setDraft] = useState("");
  const [messages, setMessages] = useState<ConversationMessage[]>([]);
  const [run, setRun] = useState<WorkspaceRunState>(emptyRunState);
  const [pageError, setPageError] = useState<string | null>(null);
  const [streaming, setStreaming] = useState(false);
  const [feedbackState, setFeedbackState] = useState<string | null>(null);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const streamAbort = useRef<AbortController | null>(null);
  const reconnectTimer = useRef<number | null>(null);
  const cursor = useRef("0");
  const runStatus = useRef("idle");

  const selectedSession = sessions.find((item) => item.thread_id === selectedThreadId) ?? null;

  useEffect(() => {
    let active = true;
    void Promise.all([api.getCapabilities(), api.listProjects()])
      .then(([system, projectData]) => {
        if (!active) return;
        setCapabilities(system);
        setProjects(projectData.projects);
        const storedProject = storedValue(PROJECT_STORAGE_KEY);
        const validProject = storedProject && projectData.projects.some((item) => item.project_id === storedProject);
        const nextProject = validProject ? storedProject : null;
        setSelectedProjectId(nextProject);
        saveValue(PROJECT_STORAGE_KEY, nextProject);
      })
      .catch((error) => {
        if (active) setPageError(apiMessage(error));
      });
    return () => { active = false; };
  }, [api]);

  useEffect(() => {
    let active = true;
    setSessions([]);
    setSelectedThreadId(null);
    setMessages([]);
    setRun(emptyRunState());
    setFeedbackState(null);
    const storageKey = `${SESSION_STORAGE_PREFIX}${selectedProjectId ?? "personal"}`;
    void api.listSessions(selectedProjectId)
      .then((data) => {
        if (!active) return;
        setSessions(data.sessions);
        const saved = storedValue(storageKey);
        const valid = saved && data.sessions.some((item) => item.thread_id === saved);
        const nextThread = valid ? saved : data.sessions[0]?.thread_id ?? null;
        setSelectedThreadId(nextThread);
        saveValue(storageKey, nextThread);
      })
      .catch((error) => { if (active) setPageError(apiMessage(error)); });
    return () => { active = false; };
  }, [api, selectedProjectId]);

  const stopStream = useCallback(() => {
    streamAbort.current?.abort();
    streamAbort.current = null;
    if (reconnectTimer.current !== null) window.clearTimeout(reconnectTimer.current);
    reconnectTimer.current = null;
    setStreaming(false);
  }, []);

  const connectRun = useCallback(async (runId: string, lastEventId: string) => {
    streamAbort.current?.abort();
    const controller = new AbortController();
    streamAbort.current = controller;
    try {
      await streamRunEvents(runId, {
        lastEventId: lastEventId === "0" ? undefined : lastEventId,
        signal: controller.signal,
        onEvent: (event) => {
          cursor.current = event.event_id;
          setRun((current) => {
            const next = applyRunEvent(current, event);
            runStatus.current = next.status;
            return next;
          });
          if (event.type === "finalize") {
            setStreaming(false);
            void api.getTrace(runId).then((trace) => setRun((current) => ({ ...current, trace }))).catch(() => undefined);
            void api.listSessions(selectedProjectId).then((data) => setSessions(data.sessions)).catch(() => undefined);
          }
        },
      });
      if (!isTerminalStatus(runStatus.current) && !controller.signal.aborted) {
        setPageError("SSE 连接已结束，正在等待后端事件继续恢复");
        reconnectTimer.current = window.setTimeout(() => void connectRun(runId, cursor.current), 500);
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      setPageError(`SSE 连接中断，正在从事件 ${cursor.current} 恢复：${apiMessage(error)}`);
      reconnectTimer.current = window.setTimeout(() => void connectRun(runId, cursor.current), 800);
    }
  }, [api, selectedProjectId]);

  useEffect(() => {
    if (!selectedThreadId) return;
    let active = true;
    void api.getSession(selectedThreadId)
      .then((session) => {
        if (!active) return;
        setSessions((current) => current.map((item) => item.thread_id === selectedThreadId ? session : item));
        const latest = session.latest_run;
        if (latest) {
          setStreaming(true);
          cursor.current = "0";
          runStatus.current = latest.status;
          void connectRun(latest.run_id, "0");
        }
      })
      .catch((error) => { if (active) setPageError(apiMessage(error)); });
    return () => { active = false; stopStream(); };
  }, [api, connectRun, selectedThreadId, stopStream]);

  useEffect(() => () => stopStream(), [stopStream]);

  const selectProject = (projectId: string | null) => {
    stopStream();
    setSelectedProjectId(projectId);
    saveValue(PROJECT_STORAGE_KEY, projectId);
    setPageError(null);
  };

  const selectSession = (threadId: string) => {
    stopStream();
    setSelectedThreadId(threadId);
    saveValue(`${SESSION_STORAGE_PREFIX}${selectedProjectId ?? "personal"}`, threadId);
    setMessages([]);
    setRun(emptyRunState());
    setFeedbackState(null);
    setPageError(null);
  };

  const createProject = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = newProjectName.trim();
    if (!name) return;
    setPageError(null);
    void api.createProject(name)
      .then((project) => {
        setProjects((current) => [project, ...current]);
        setNewProjectName("");
        selectProject(project.project_id);
      })
      .catch((error) => setPageError(apiMessage(error)));
  };

  const createSession = () => {
    setPageError(null);
    void api.createSession(selectedProjectId, "新工程会话")
      .then((session) => {
        setSessions((current) => [session, ...current]);
        selectSession(session.thread_id);
      })
      .catch((error) => setPageError(apiMessage(error)));
  };

  const sendMessage = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedThreadId || !draft.trim()) return;
    const message = draft.trim();
    setDraft("");
    setPageError(null);
    setFeedbackState(null);
    setMessages((current) => [...current, { role: "user", text: message }]);
    setRun(emptyRunState());
    cursor.current = "0";
    runStatus.current = "queued";
    setStreaming(true);
    void api.sendMessage(selectedThreadId, message)
      .then((created) => {
        setRun((current) => ({ ...current, runId: created.run_id, threadId: created.thread_id, status: created.status }));
        void connectRun(created.run_id, "0");
      })
      .catch((error) => { setStreaming(false); setPageError(apiMessage(error)); });
  };

  const cancelRun = () => {
    if (!run.runId) return;
    void api.cancelRun(run.runId)
      .then((result) => { setRun((current) => ({ ...current, status: result.status })); setPageError(null); })
      .catch((error) => setPageError(apiMessage(error)));
  };

  const submitFeedback = (rating: "useful" | "not_useful") => {
    if (!run.runId) return;
    setFeedbackState("提交中");
    void api.submitFeedback(run.runId, rating)
      .then(() => setFeedbackState("saved"))
      .catch((error) => setFeedbackState(apiMessage(error)));
  };

  return {
    capabilities,
    projects,
    sessions,
    selectedProjectId,
    selectedThreadId,
    selectedSession,
    newProjectName,
    draft,
    messages,
    run,
    pageError,
    streaming,
    feedbackState,
    memoryOpen,
    setNewProjectName,
    setDraft,
    setMemoryOpen,
    createProject,
    selectProject,
    selectSession,
    createSession,
    sendMessage,
    cancelRun,
    submitFeedback,
  };
}
