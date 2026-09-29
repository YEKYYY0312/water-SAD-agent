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
import {
  applyRunEvent,
  archiveRun,
  emptyRunState,
  isTerminalStatus,
  mergeHistory,
  type ConversationMessage,
  type WorkspaceRunState,
} from "./workspaceState";
import { ExportApi, exportErrorMessage, type ExportFormat } from "./exportApi.ts";
import { precheckUpload, UploadApi, uploadErrorMessage, type UploadRecord } from "./uploadApi.ts";

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

/** 输入框上方的一张文件卡片：上传中 / 已入库 / 失败。 */
export type Attachment = {
  key: string;
  filename: string;
  sizeBytes: number;
  status: "uploading" | "ready" | "failed";
  progress: number;
  record?: UploadRecord;
  error?: string;
};

export type ExportState = { format: ExportFormat; status: "downloading" | "done" | "failed"; message?: string } | null;

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
  settingsOpen: boolean;
  setNewProjectName: (value: string) => void;
  setDraft: (value: string) => void;
  setMemoryOpen: (value: boolean | ((current: boolean) => boolean)) => void;
  setSettingsOpen: (value: boolean | ((current: boolean) => boolean)) => void;
  refreshCapabilities: () => void;
  createProject: (event: FormEvent<HTMLFormElement>) => void;
  selectProject: (projectId: string | null) => void;
  selectSession: (threadId: string) => void;
  createSession: () => void;
  sendMessage: (event: FormEvent<HTMLFormElement>) => void;
  cancelRun: () => void;
  submitFeedback: (rating: "useful" | "not_useful") => void;
  attachments: Attachment[];
  uploads: UploadRecord[];
  uploadsError: string | null;
  attachFiles: (files: FileList | File[]) => void;
  dismissAttachment: (key: string) => void;
  deleteUpload: (uploadId: string) => void;
  exportState: ExportState;
  downloadExport: (format: ExportFormat) => void;
};

export function useWorkspaceController(): WorkspaceController {
  const api = useRef(new WorkspaceApi()).current;
  const uploadApi = useRef(new UploadApi()).current;
  const exportApi = useRef(new ExportApi()).current;
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
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [uploads, setUploads] = useState<UploadRecord[]>([]);
  const [uploadsError, setUploadsError] = useState<string | null>(null);
  const [exportState, setExportState] = useState<ExportState>(null);
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

  const refreshUploads = useCallback(() => {
    void uploadApi.list(selectedProjectId)
      .then((items) => { setUploads(items); setUploadsError(null); })
      .catch((error) => setUploadsError(error instanceof WorkspaceApiError ? uploadErrorMessage(error.code, error.message) : "资料列表加载失败"));
  }, [uploadApi, selectedProjectId]);

  useEffect(() => {
    setAttachments([]);
    refreshUploads();
  }, [refreshUploads]);

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
          if (event.type === "stream_end") {
            // 后端显式告知流正常结束：以 payload.status 为准收口，不再重连。
            setStreaming(false);
            const finalStatus = event.payload.status;
            if (typeof finalStatus === "string") runStatus.current = finalStatus;
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
    void Promise.all([
      api.getSession(selectedThreadId),
      // 历史读不到不该挡住会话本身：降级为空历史并提示，最新运行照常回放。
      api.getMessages(selectedThreadId).catch((error) => {
        if (active) setPageError(`历史消息加载失败：${apiMessage(error)}`);
        return { thread_id: selectedThreadId, messages: [] };
      }),
    ])
      .then(([session, history]) => {
        if (!active) return;
        setSessions((current) => current.map((item) => item.thread_id === selectedThreadId ? session : item));
        const latest = session.latest_run;
        setMessages(mergeHistory(history.messages, Boolean(latest)));
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
    // 再点一次当前会话不做任何事：下面会清空消息与运行态，但 selectedThreadId 没变、
    // 加载会话的 effect 不会重跑，结果是当前回答被清掉且再也回不来。
    if (threadId === selectedThreadId) return;
    stopStream();
    setSelectedThreadId(threadId);
    saveValue(`${SESSION_STORAGE_PREFIX}${selectedProjectId ?? "personal"}`, threadId);
    setMessages([]);
    setRun(emptyRunState());
    setFeedbackState(null);
    setExportState(null);
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
    if (attachments.some((item) => item.status === "uploading")) return;
    const message = draft.trim();
    const attachmentIds = attachments
      .filter((item) => item.status === "ready" && item.record)
      .map((item) => item.record!.upload_id);
    setDraft("");
    setPageError(null);
    setFeedbackState(null);
    setExportState(null);
    // 已入库的附件随本次提问寄出：后端按原文顺序读进提示词，卡片功成身退。
    setAttachments((current) => current.filter((item) => item.status === "failed"));
    // 先把上一轮的回答归档进对话，再清空运行态——否则第一轮回答随 setRun 一起消失。
    setMessages((current) => [...archiveRun(current, run), { role: "user", text: message }]);
    setRun(emptyRunState());
    cursor.current = "0";
    runStatus.current = "queued";
    setStreaming(true);
    void api.sendMessage(selectedThreadId, message, attachmentIds)
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

  const attachFiles = (files: FileList | File[]) => {
    for (const file of Array.from(files)) {
      const key = `${file.name}-${file.size}-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
      const base: Attachment = { key, filename: file.name, sizeBytes: file.size, status: "uploading", progress: 0 };
      const precheck = precheckUpload(file);
      if (precheck) {
        setAttachments((current) => [...current, { ...base, status: "failed", error: uploadErrorMessage(precheck) }]);
        continue;
      }
      setAttachments((current) => [...current, base]);
      const patch = (next: Partial<Attachment>) =>
        setAttachments((current) => current.map((item) => (item.key === key ? { ...item, ...next } : item)));
      void uploadApi.upload(file, { projectId: selectedProjectId, onProgress: (progress) => patch({ progress }) })
        .then((record) => {
          patch({ status: "ready", progress: 1, record });
          setUploads((current) => [record, ...current.filter((item) => item.upload_id !== record.upload_id)]);
        })
        .catch((error) => patch({
          status: "failed",
          error: error instanceof WorkspaceApiError ? uploadErrorMessage(error.code, error.message) : uploadErrorMessage("UPLOAD_NETWORK_FAILED"),
        }));
    }
  };

  const dismissAttachment = (key: string) => setAttachments((current) => current.filter((item) => item.key !== key));

  const deleteUpload = (uploadId: string) => {
    void uploadApi.remove(uploadId)
      .then(() => {
        setUploads((current) => current.filter((item) => item.upload_id !== uploadId));
        setAttachments((current) => current.filter((item) => item.record?.upload_id !== uploadId));
      })
      .catch((error) => setUploadsError(error instanceof WorkspaceApiError ? uploadErrorMessage(error.code, error.message) : "删除未完成"));
  };

  const downloadExport = (format: ExportFormat) => {
    if (!run.runId) return;
    setExportState({ format, status: "downloading" });
    void exportApi.download(run.runId, format)
      .then((filename) => setExportState({ format, status: "done", message: filename }))
      .catch((error) => setExportState({
        format,
        status: "failed",
        message: error instanceof WorkspaceApiError
          ? exportErrorMessage(error.code, error.message) + (error.code === "WEB_EXPORT_SELF_CHECK_FAILED" ? `（${error.message}）` : "")
          : "下载未完成",
      }));
  };

  // 设置面板写入密钥后调用：topbar 的 model 指示与 Inspector 的能力状态都读
  // capabilities，不刷新就会继续显示「mock / 未配置」。
  const refreshCapabilities = () => {
    void api.getCapabilities()
      .then(setCapabilities)
      .catch((error) => setPageError(apiMessage(error)));
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
    settingsOpen,
    setNewProjectName,
    setDraft,
    setMemoryOpen,
    setSettingsOpen,
    refreshCapabilities,
    createProject,
    selectProject,
    selectSession,
    createSession,
    sendMessage,
    cancelRun,
    submitFeedback,
    attachments,
    uploads,
    uploadsError,
    attachFiles,
    dismissAttachment,
    deleteUpload,
    exportState,
    downloadExport,
  };
}
