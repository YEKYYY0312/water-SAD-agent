import { useEffect, useState } from "react";

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

const pendingStages = [
  ["项目与会话", "Step 37/38 接入账号与持久化"],
  ["实时运行事件", "Step 42 接入真实 SSE"],
  ["长期记忆", "Step 39–43 接入受治理记忆"],
];

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
  const serviceState = loadError ? "能力状态不可用" : "开发骨架";

  return (
    <main className="workbench">
      <header className="topbar">
        <div className="brand" aria-label="给排水 Agent">
          <span className="brand-mark" aria-hidden="true">WA</span>
          <div>
            <h1>给排水 Agent 工作台</h1>
            <p>Self-hosted engineering workspace</p>
          </div>
        </div>
        <div className="topbar-status" aria-live="polite">
          <span>● {serviceState}</span>
          <span>模型：{modelState}</span>
        </div>
      </header>

      <div className="workspace">
        <aside className="sidebar" aria-labelledby="scope-heading">
          <p className="kicker">WORKSPACE</p>
          <h2 id="scope-heading">项目与会话</h2>
          <section className="empty-state" aria-label="项目与会话尚未接入">
            <span aria-hidden="true">·</span>
            <p>登录、项目成员关系和会话恢复将在 Step 37/38 服务端接入。</p>
          </section>
          <p className="kicker">DELIVERY ROADMAP</p>
          <ul className="stage-list">
            {pendingStages.map(([title, detail]) => (
              <li key={title}>
                <strong>{title}</strong>
                <span>{detail}</span>
              </li>
            ))}
          </ul>
        </aside>

        <section className="conversation" aria-labelledby="conversation-heading">
          <header className="conversation-header">
            <div>
              <p className="kicker">SESSION</p>
              <h2 id="conversation-heading">工程会话</h2>
            </div>
            <span className="status-label">未建立会话</span>
          </header>
          <div className="message-log" role="log" aria-live="polite" aria-label="工程会话消息">
            <article className="notice-message">
              <p className="message-role">系统边界</p>
              <p>
                此页面是登录后工作台的视觉与静态资源骨架。当前不会创建账号、保存记忆、调用模型或模拟工具完成。
              </p>
            </article>
            <article className="notice-message">
              <p className="message-role">计算纪律</p>
              <p>
                水力计算结果仅由服务端程序化内核产生；前端只渲染经验证的结果，不执行数值计算。
              </p>
              <span className="trace-tag">ƒ 程序化内核 · 后续真实数据接入</span>
            </article>
          </div>
          <form className="composer" aria-label="会话输入占位">
            <label htmlFor="message">工程问题</label>
            <textarea
              id="message"
              disabled
              placeholder="认证、会话授权与真实 Agent SSE 将在后续 Step 接入"
              rows={3}
            />
            <button type="submit" disabled>发送（尚未接线）</button>
          </form>
        </section>

        <aside className="inspector" aria-labelledby="inspector-heading">
          <p className="kicker">INSPECTOR</p>
          <h2 id="inspector-heading">运行检查</h2>
          <dl>
            <div><dt>认证</dt><dd>{capabilities?.authentication ?? "读取中"}</dd></div>
            <div><dt>持久化</dt><dd>{capabilities?.persistence ?? "读取中"}</dd></div>
            <div><dt>SSE</dt><dd>{capabilities?.sse ?? "读取中"}</dd></div>
          </dl>
          <section className="boundary-note" aria-label="Step 36 边界说明">
            <strong>Step 36 边界</strong>
            <p>仅提供静态工作台、健康检查与版本化 HTTP 错误结构。</p>
          </section>
        </aside>
      </div>

      <footer className="statusbar">
        <span>● /health/ready</span>
        <span>API envelope v1</span>
        <span>UTF-8 · 同源静态资源</span>
      </footer>
    </main>
  );
}
