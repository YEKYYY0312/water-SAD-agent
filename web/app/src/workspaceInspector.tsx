import type { DrawingCapability, SystemCapabilities, TraceSnapshot } from "./workspaceApi";
import type { CalculationCard, CitationCard, ToolCard, WorkspaceRunState } from "./workspaceState";
import { StatusMark } from "./workspaceComponents";

function ToolList({ tools }: { tools: ToolCard[] }) {
  return <section className="inspector-section" aria-labelledby="tools-heading"><p className="kicker">TOOLS</p><h3 id="tools-heading">真实工具事件</h3>{tools.length === 0 ? <p className="muted-copy">尚未执行工具。</p> : <div className="event-list">{tools.map((tool) => <div className="event-row" key={tool.tool_name}><StatusMark status={tool.status} /><code>{tool.tool_name}</code>{tool.code && <small>{tool.code}</small>}</div>)}</div>}</section>;
}

function CitationList({ citations }: { citations: CitationCard[] }) {
  return <section className="inspector-section" aria-labelledby="citations-heading"><p className="kicker">CITATIONS</p><h3 id="citations-heading">已校验引用</h3>{citations.length === 0 ? <p className="muted-copy">本次终态没有可展示的已校验 source_id。</p> : <div className="citation-list">{citations.map((citation) => <div className="citation-row" key={citation.source_id}><span aria-hidden="true">│</span><code>{citation.source_id}</code><small>已校验</small></div>)}</div>}</section>;
}

function CalculationList({ calculations }: { calculations: CalculationCard[] }) {
  return <section className="inspector-section" aria-labelledby="calculations-heading"><p className="kicker">CALCULATION</p><h3 id="calculations-heading">计算结果</h3>{calculations.length === 0 ? <p className="muted-copy">尚未收到程序化计算结果。</p> : calculations.map((calculation) => <div className="calculation-card" key={calculation.tool_name}><div className="calculation-title"><span>ƒ</span><code>{calculation.tool_name}</code><small>程序化内核</small></div>{Object.entries(calculation.values).map(([name, value]) => <div className="calculation-value" key={name}><code>{name}</code><strong>{String(value)}</strong><span>{calculation.units[name] || ""}</span></div>)}{calculation.assumptions.length > 0 && <p className="assumption-copy">! {calculation.assumptions.join("；")}</p>}</div>)}</section>;
}

function TraceList({ trace }: { trace: TraceSnapshot | null }) {
  return <section className="inspector-section trace-section" aria-labelledby="trace-heading"><p className="kicker">TRACE</p><h3 id="trace-heading">脱敏运行轨迹</h3>{!trace ? <p className="muted-copy">运行结束后加载 trace。</p> : <div className="trace-list">{trace.events.map((event, index) => <div className="trace-row" key={event.event_id || index}><div><code>{event.event_type || "event"}</code><small>{event.node_name || ""}</small></div><span>{event.status || ""}</span>{event.error && <p>{event.error}</p>}</div>)}</div>}</section>;
}

export function CapabilityStatus({ label, status, message }: { label: string; status: string; message: string }) {
  return <div className="capability-row"><StatusMark status={status} /><div><strong>{label}</strong><p>{message}</p></div><code>{status}</code></div>;
}

export function Inspector({ run, capabilities }: { run: WorkspaceRunState; capabilities: SystemCapabilities | null }) {
  const drawing: DrawingCapability | null = capabilities?.drawing ?? null;
  return (
    <aside className="inspector" aria-labelledby="inspector-heading">
      <p className="kicker">INSPECTOR</p><h2 id="inspector-heading">运行检查</h2>
      <ToolList tools={run.tools} />
      <CitationList citations={run.citations} />
      <CalculationList calculations={run.calculations} />
      <TraceList trace={run.trace} />
      <section className="inspector-section" aria-labelledby="capabilities-heading"><p className="kicker">CAPABILITIES</p><h3 id="capabilities-heading">能力状态</h3><div className="capability-list"><CapabilityStatus label="P3 DXF 图纸" status={drawing?.status || "unavailable"} message={drawing?.message || "能力状态不可用"} /><CapabilityStatus label="P3 DWG 直读" status={drawing?.dwg.status || "blocked"} message={drawing?.dwg.message || "DWG 直读 blocked"} /><CapabilityStatus label="P4 真实 MCP" status={capabilities?.mcp.status || "unavailable"} message={capabilities?.mcp.message || "能力状态不可用"} /></div></section>
      {run.errors.length > 0 && <section className="inspector-section"><p className="kicker">ERRORS</p><div className="error-list">{run.errors.map((error) => <p key={error}>{error}</p>)}</div></section>}
    </aside>
  );
}
