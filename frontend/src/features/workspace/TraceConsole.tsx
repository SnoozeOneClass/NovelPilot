import {
  Archive,
  Check,
  Circle,
  FileText,
  Pause,
  Play,
  RefreshCw,
  Search,
  ShieldCheck
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { apiUrl } from "../../api/client";
import {
  formatArtifactDetail,
  formatArtifactTitle,
  formatAtomicAction,
  formatEventKind,
  formatEventMessage,
  formatEvidence,
  formatGateId,
  formatGateMessage,
  formatGateStatus,
  formatGenericStatus,
  formatLoopLayer,
  formatRoutingDecision
} from "../../types/display";
import type {
  ArtifactSummary,
  HarnessEvent,
  ProjectCompletionAudit,
  ProjectReadiness
} from "../../types/domain";
import { LiteraryReviewPanel } from "./LiteraryReviewPanel";
import { eventMatches, formatClock, parseJsonRecord } from "./workspace-utils";

type TraceTab = "trace" | "artifacts" | "validation" | "events";

interface TraceConsoleProps {
  events: HarnessEvent[];
  summaries: ArtifactSummary[];
  artifactPaths: string[];
  selectedArtifactPath: string | null;
  activeArtifact: { path: string; content: string } | null;
  readiness: ProjectReadiness | null;
  completionAudit: ProjectCompletionAudit | null;
  canPause: boolean;
  canResume: boolean;
  canRetry: boolean;
  busy: boolean;
  onSelectArtifact: (path: string) => void;
  onPause: () => Promise<void>;
  onResume: () => Promise<void>;
  onRetry: () => Promise<void>;
  onRefreshAudit: () => Promise<void>;
}

const traceTabs: Array<{ id: TraceTab; label: string }> = [
  { id: "trace", label: "Run Trace" },
  { id: "artifacts", label: "Artifacts" },
  { id: "validation", label: "Validation" },
  { id: "events", label: "Events" }
];

function statusTone(status: string): string {
  if (["completed", "passed", "committed"].includes(status)) return "success";
  if (["failed", "rejected"].includes(status)) return "danger";
  if (["started", "running", "delta"].includes(status)) return "active";
  return "neutral";
}

export function TraceConsole({
  events,
  summaries,
  artifactPaths,
  selectedArtifactPath,
  activeArtifact,
  readiness,
  completionAudit,
  canPause,
  canResume,
  canRetry,
  busy,
  onSelectArtifact,
  onPause,
  onResume,
  onRetry,
  onRefreshAudit
}: TraceConsoleProps) {
  const [tab, setTab] = useState<TraceTab>("trace");
  const [loopFilter, setLoopFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [query, setQuery] = useState("");
  const statusEvents = useMemo(() => events.filter((event) => event.kind !== "llm_output_delta"), [events]);
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);

  useEffect(() => {
    if (!selectedEventId && statusEvents.length) setSelectedEventId(statusEvents.at(-1)?.event_id ?? null);
  }, [selectedEventId, statusEvents]);

  const filteredEvents = statusEvents.filter((event) => eventMatches(event, loopFilter, statusFilter, query));
  const selectedEvent = statusEvents.find((event) => event.event_id === selectedEventId) ?? filteredEvents.at(-1) ?? null;
  const selectedSummary = summaries.find((summary) => summary.path === selectedArtifactPath) ?? null;
  const selectedPayload = selectedEvent ? JSON.stringify(selectedEvent.payload, null, 2) : "";
  const contextSnapshot =
    activeArtifact?.path.endsWith("context_snapshot.json") ? parseJsonRecord(activeArtifact.content) : null;

  function selectEvent(event: HarnessEvent) {
    setSelectedEventId(event.event_id);
    if (event.artifact_path) onSelectArtifact(event.artifact_path);
  }

  return (
    <section className="np-surface trace-console">
      <header className="view-heading">
        <div>
          <h1>运行证据与产物 · Trace Console</h1>
          <p>面向 reviewer 的 Harness Trace：事件、Artifact 与 Validation 全部可追踪。</p>
        </div>
      </header>

      <nav className="tab-bar trace-tabs">
        {traceTabs.map((item) => (
          <button key={item.id} className={tab === item.id ? "active" : ""} onClick={() => setTab(item.id)}>
            {item.label}
          </button>
        ))}
      </nav>

      {tab === "trace" && (
        <>
          <div className="trace-filters">
            <select value={loopFilter} onChange={(event) => setLoopFilter(event.target.value)}>
              <option value="all">全部 Loop</option>
              <option value="book">全书 Loop</option>
              <option value="story_arc">故事弧 Loop</option>
              <option value="chapter">章节 Loop</option>
              <option value="system">系统</option>
            </select>
            <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
              <option value="all">全部状态</option>
              <option value="started">进行中</option>
              <option value="completed">已完成</option>
              <option value="failed">失败</option>
              <option value="requested">已请求</option>
            </select>
            <label>
              <Search size={16} />
              <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索事件、artifact、routing decision..." />
            </label>
          </div>

          <div className="trace-main-grid">
            <section className="event-timeline-panel">
              <h2>事件时间线</h2>
              <ol>
                {filteredEvents.map((event) => (
                  <li key={event.event_id} className={selectedEvent?.event_id === event.event_id ? "selected" : ""}>
                    <button onClick={() => selectEvent(event)}>
                      <span className={`event-node ${statusTone(event.status)}`} />
                      <time>{formatClock(event.timestamp)}</time>
                      <strong>{formatAtomicAction(event.atomic_action) || formatEventKind(event.kind)}</strong>
                      <span className="mini-badge loop">{formatLoopLayer(event.loop_layer)}</span>
                      <span className={`mini-badge ${statusTone(event.status)}`}>{event.status}</span>
                      <small>{formatEventMessage(event.message)}</small>
                    </button>
                  </li>
                ))}
                {filteredEvents.length === 0 && <li className="empty-state">没有符合筛选条件的事件。</li>}
              </ol>
            </section>

            <aside className="event-detail-panel">
              <h2>事件详情</h2>
              {selectedEvent ? (
                <>
                  <dl>
                    <div><dt>事件类型</dt><dd>{formatEventKind(selectedEvent.kind)}</dd></div>
                    <div><dt>时间</dt><dd>{formatClock(selectedEvent.timestamp)}</dd></div>
                    <div><dt>Loop Layer</dt><dd>{formatLoopLayer(selectedEvent.loop_layer)}</dd></div>
                    <div><dt>Atomic Action</dt><dd>{formatAtomicAction(selectedEvent.atomic_action)}</dd></div>
                    <div><dt>状态</dt><dd>{selectedEvent.status}</dd></div>
                    <div><dt>Routing Decision</dt><dd>{formatRoutingDecision(selectedEvent.routing_decision)}</dd></div>
                    <div><dt>关联产物</dt><dd>{selectedEvent.artifact_path ?? "-"}</dd></div>
                  </dl>
                  {contextSnapshot && (
                    <section className="snapshot-summary">
                      <h3>上下文快照摘要</h3>
                      <p>直接注入：{Array.isArray(contextSnapshot.sources) ? contextSnapshot.sources.length : 0} 个来源</p>
                      <p>排除内容：{Array.isArray(contextSnapshot.excluded) ? contextSnapshot.excluded.length : 0} 项</p>
                      <p>{typeof contextSnapshot.assembly_rationale === "string" ? contextSnapshot.assembly_rationale : ""}</p>
                    </section>
                  )}
                  {selectedPayload !== "{}" && (
                    <details>
                      <summary>事件 Payload</summary>
                      <pre>{selectedPayload}</pre>
                    </details>
                  )}
                </>
              ) : (
                <p className="empty-state">选择一个事件查看证据。</p>
              )}
            </aside>
          </div>

          <footer className="trace-actions">
            <button className="pause-button" disabled={!canPause || busy} onClick={() => void onPause()}>
              <Pause size={16} /> 暂停运行
            </button>
            <button className="outline-button" disabled={!canResume || busy} onClick={() => void onResume()}>
              <Play size={16} /> 恢复运行
            </button>
            <button className="outline-button" disabled={!canRetry || busy} onClick={() => void onRetry()}>
              <RefreshCw size={16} /> 重试当前章节
            </button>
            <a className="gold-button download-button" href={apiUrl("/api/runs/archive")} download>
              <Archive size={16} /> 下载 Run 包 ZIP
            </a>
          </footer>
        </>
      )}

      {tab === "artifacts" && (
        <div className="artifact-center-grid">
          <aside className="artifact-browser">
            <h2>全部产物</h2>
            <div>
              {summaries.map((summary) => (
                <button
                  key={summary.path}
                  className={selectedArtifactPath === summary.path ? "selected" : ""}
                  onClick={() => onSelectArtifact(summary.path)}
                >
                  <FileText size={15} />
                  <span><strong>{formatArtifactTitle(summary)}</strong><small>{summary.path}</small></span>
                  <em className={statusTone(summary.status)}>{formatGenericStatus(summary.status)}</em>
                </button>
              ))}
              {artifactPaths.length === 0 && <p className="empty-state">还没有落盘产物。</p>}
            </div>
          </aside>
          <section className="artifact-inspector">
            <header>
              <div>
                <h2>{selectedSummary ? formatArtifactTitle(selectedSummary) : "产物预览"}</h2>
                <p>{selectedArtifactPath ?? "从左侧选择一个文件"}</p>
              </div>
              {selectedSummary && <span className={`soft-badge ${statusTone(selectedSummary.status)}`}>{formatGenericStatus(selectedSummary.status)}</span>}
            </header>
            {selectedSummary && <p className="artifact-description">{formatArtifactDetail(selectedSummary.detail)}</p>}
            <pre>{activeArtifact?.content ?? "还没有选择产物。"}</pre>
          </section>
        </div>
      )}

      {tab === "validation" && (
        <div className="validation-workspace">
          <div className="validation-grid">
            <section>
            <header><ShieldCheck size={18} /><h2>运行准备</h2></header>
            {readiness ? (
              <div className="gate-list">
                {readiness.gates.map((gate) => (
                  <article key={gate.id}>
                    <span className={`gate-icon ${gate.status}`}>{gate.status === "passed" ? <Check size={14} /> : <Circle size={14} />}</span>
                    <div><strong>{formatGateId(gate.id)}</strong><p>{formatGateMessage(gate.message)}</p></div>
                    <em>{formatGateStatus(gate.status)}</em>
                    {gate.evidence.length > 0 && <small>{gate.evidence.map(formatEvidence).join(" · ")}</small>}
                  </article>
                ))}
              </div>
            ) : <p className="empty-state">尚未加载运行准备状态。</p>}
            </section>
            <section>
            <header><ShieldCheck size={18} /><h2>完成审查</h2></header>
            {completionAudit ? (
              <div className="gate-list">
                {completionAudit.gates.map((gate) => (
                  <article key={gate.id}>
                    <span className={`gate-icon ${gate.status}`}>{gate.status === "passed" ? <Check size={14} /> : <Circle size={14} />}</span>
                    <div><strong>{formatGateId(gate.id)}</strong><p>{formatGateMessage(gate.message)}</p></div>
                    <em>{formatGateStatus(gate.status)}</em>
                    {gate.evidence.length > 0 && <small>{gate.evidence.map(formatEvidence).join(" · ")}</small>}
                  </article>
                ))}
              </div>
            ) : <p className="empty-state">尚未加载完成审查。</p>}
            </section>
          </div>
          <LiteraryReviewPanel completionAudit={completionAudit} onRecorded={onRefreshAudit} />
        </div>
      )}

      {tab === "events" && (
        <div className="raw-events-list">
          {events.map((event) => (
            <article key={event.event_id}>
              <span className={`event-node ${statusTone(event.status)}`} />
              <time>{formatClock(event.timestamp)}</time>
              <strong>{event.kind}</strong>
              <code>{event.loop_layer}/{event.atomic_action ?? "-"}</code>
              <p>{event.message}</p>
            </article>
          ))}
          {events.length === 0 && <p className="empty-state">events.jsonl 目前为空。</p>}
        </div>
      )}
    </section>
  );
}
