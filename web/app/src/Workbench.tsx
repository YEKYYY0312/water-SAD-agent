import { useEffect, useState } from "react";

import { MemoryWorkspace } from "./MemoryWorkspace";

type CapabilityEnvelope = {
  version: string;
  ok: boolean;
  data: {
    real_llm: boolean;
    authentication: string;
    persistence: string;
    sse: string;
  };
};

export function Workbench() {
  const [capabilities, setCapabilities] = useState<CapabilityEnvelope["data"]>();
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    let active = true;
    void fetch("/api/system/capabilities")
      .then(async (response) => {
        if (!response.ok) throw new Error("capabilities unavailable");
        return (await response.json()) as CapabilityEnvelope;
      })
      .then((envelope) => {
        if (active && envelope.ok) setCapabilities(envelope.data);
      })
      .catch(() => {
        if (active) setLoadError(true);
      });
    return () => {
      active = false;
    };
  }, []);

  const modelState = capabilities?.real_llm ? "已配置" : "未配置";
  const serviceState = loadError ? "能力状态不可用" : "自托管工作台";

  return (
    <main className="workbench">
      <header className="topbar">
        <div className="brand" aria-label="给排水 Agent">
          <span className="brand-mark" aria-hidden="true">WA</span>
          <div><h1>给排水 Agent 工作台</h1><p>Self-hosted engineering workspace</p></div>
        </div>
        <div className="topbar-status" aria-live="polite"><span>● {serviceState}</span><span>模型：{modelState}</span></div>
      </header>

      <div className="workspace">
        <aside className="sidebar" aria-labelledby="scope-heading">
          <p className="kicker">WORKSPACE</p>
          <h2 id="scope-heading">项目与会话</h2>
          <section className="empty-state" aria-label="项目与会话尚未接入"><span aria-hidden="true">·</span><p>账号、项目成员关系和会话由服务端授权后加载。</p></section>
          <section className="boundary-note" aria-label="工作区授权边界"><strong>服务端授权</strong><p>客户端只选择服务端返回的资源，不自行决定用户、项目或记忆权限。</p></section>
        </aside>

        <section className="conversation" aria-labelledby="conversation-heading">
          <header className="conversation-header"><div><p className="kicker">SESSION</p><h2 id="conversation-heading">工程会话</h2></div><span className="status-label">记忆工作区 · Step 43</span></header>
          <MemoryWorkspace />
        </section>

        <aside className="inspector" aria-labelledby="inspector-heading">
          <p className="kicker">INSPECTOR</p><h2 id="inspector-heading">运行检查</h2>
          <dl><div><dt>认证</dt><dd>{capabilities?.authentication ?? "读取中"}</dd></div><div><dt>持久化</dt><dd>{capabilities?.persistence ?? "读取中"}</dd></div><div><dt>SSE</dt><dd>{capabilities?.sse ?? "读取中"}</dd></div></dl>
          <section className="boundary-note" aria-label="记忆安全边界"><strong>记忆安全边界</strong><p>正文来自受权记忆 API；前端不从 trace 推断正文，不计算水力结果，也不把记忆提升为系统指令。ƒ 程序化内核仍是唯一计算来源。</p></section>
        </aside>
      </div>
      <footer className="statusbar"><span>● /health/ready</span><span>API envelope v1</span><span>UTF-8 · 同源静态资源</span></footer>
    </main>
  );
}
