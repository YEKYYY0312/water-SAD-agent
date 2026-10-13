import type { DrawingCapability, SystemCapabilities, TraceSnapshot } from "./workspaceApi";
import { retrievalModeView, type CalculationCard, type CitationCard, type RetrievalModeCard, type ToolCard, type WorkspaceRunState } from "./workspaceState";
import { Icon } from "./icons";
import { StatusMark } from "./workspaceComponents";

function ToolList({ tools }: { tools: ToolCard[] }) {
  return <section className="inspector-section" aria-labelledby="tools-heading"><h3 id="tools-heading">工具调用</h3>{tools.length === 0 ? <p className="muted-copy">这次还没有调用工具。</p> : <div className="event-list">{tools.map((tool) => <div className="event-row" key={tool.tool_name}><StatusMark status={tool.status} /><code>{tool.tool_name}</code>{tool.code && <small>{tool.code}</small>}</div>)}</div>}</section>;
}

function CitationList({ citations }: { citations: CitationCard[] }) {
  return <section className="inspector-section" aria-labelledby="citations-heading"><h3 id="citations-heading">已校验引用</h3>{citations.length === 0 ? <p className="muted-copy">回答完成后，这里列出通过校验的条款号。</p> : <div className="citation-list">{citations.map((citation) => <div className="citation-row" key={citation.source_id}><code>{citation.source_id}</code><small>已校验</small></div>)}</div>}</section>;
}

function RetrievalMode({ retrieval }: { retrieval: RetrievalModeCard | null }) {
  const view = retrievalModeView(retrieval);
  if (!view) return null;
  // 不复用 CapabilityStatus：它的右侧徽标是"未配置 / 可用"，套在"向量模型与索引不匹配"上会说错
  return <section className="inspector-section" aria-labelledby="retrieval-heading"><h3 id="retrieval-heading">检索方式</h3><div className="capability-list"><div className="capability-row"><StatusMark status={view.tone} /><div><strong>{view.label}</strong><p>{view.warning ?? "按向量与关键词综合排序。"}</p></div></div></div></section>;
}

function CalculationList({ calculations }: { calculations: CalculationCard[] }) {
  return <section className="inspector-section" aria-labelledby="calculations-heading"><h3 id="calculations-heading">计算结果</h3>{calculations.length === 0 ? <p className="muted-copy">涉及计算时，结果会出现在这里。</p> : calculations.map((calculation) => <div className="calculation-card" key={calculation.tool_name}><div className="calculation-title"><code>{calculation.tool_name}</code><small>程序计算，非模型估算</small></div>{Object.entries(calculation.values).map(([name, value]) => <div className="calculation-value" key={name}><code>{name}</code><strong>{String(value)}</strong><span>{calculation.units[name] || ""}</span></div>)}{calculation.assumptions.length > 0 && <p className="assumption-copy">! {calculation.assumptions.join("；")}</p>}</div>)}</section>;
}

function TraceList({ trace }: { trace: TraceSnapshot | null }) {
  return <section className="inspector-section trace-section" aria-labelledby="trace-heading"><h3 id="trace-heading">运行轨迹（已脱敏）</h3>{!trace ? <p className="muted-copy">运行结束后加载。</p> : <div className="trace-list">{trace.events.map((event, index) => <div className="trace-row" key={event.event_id || index}><div><code>{event.event_type || "event"}</code><small>{event.node_name || ""}</small></div><span>{event.status || ""}</span>{event.error && <p>{event.error}</p>}</div>)}</div>}</section>;
}

const CAPABILITY_LABEL: Record<string, string> = {
  available: "可用",
  succeeded: "可用",
  not_configured: "未配置",
  unavailable: "不可用",
  blocked: "暂不支持",
  failed: "异常",
};

export function CapabilityStatus({ label, status, message }: { label: string; status: string; message: string }) {
  return <div className="capability-row"><StatusMark status={status} /><div><strong>{label}</strong><p>{message}</p></div><span className={`capability-state status-${status}`}>{CAPABILITY_LABEL[status] ?? status}</span></div>;
}

export function Inspector({
  run,
  capabilities,
  open,
  onClose,
}: {
  run: WorkspaceRunState;
  capabilities: SystemCapabilities | null;
  open: boolean;
  onClose: () => void;
}) {
  const drawing: DrawingCapability | null = capabilities?.drawing ?? null;
  return (
    <aside className="inspector" aria-labelledby="inspector-heading" hidden={!open}>
      <div className="inspector-header">
        <h2 id="inspector-heading">运行检查</h2>
        <button type="button" className="icon-button" aria-label="收起运行检查" onClick={onClose}><Icon name="close" /></button>
      </div>
      <ToolList tools={run.tools} />
      <CitationList citations={run.citations} />
      <RetrievalMode retrieval={run.retrieval} />
      <CalculationList calculations={run.calculations} />
      <TraceList trace={run.trace} />
      <section className="inspector-section" aria-labelledby="capabilities-heading"><h3 id="capabilities-heading">能力状态</h3><div className="capability-list"><CapabilityStatus label="P3 DXF 图纸" status={drawing?.status || "unavailable"} message={drawing?.message || "能力状态不可用"} /><CapabilityStatus label="P3 DWG 直读" status={drawing?.dwg.status || "blocked"} message={drawing?.dwg.message || "DWG 直读 blocked"} /><CapabilityStatus label="P4 真实 MCP" status={capabilities?.mcp.status || "unavailable"} message={capabilities?.mcp.message || "能力状态不可用"} /></div></section>
      {run.errors.length > 0 && <section className="inspector-section"><h3>错误</h3><div className="error-list">{run.errors.map((error) => <p key={error}>{error}</p>)}</div></section>}
    </aside>
  );
}
