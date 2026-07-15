import { useVirtualizer } from "@tanstack/react-virtual";
import { Archive, Check, Circle, FileText, Pause, Play, RefreshCw, Search, ShieldCheck, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { apiUrl } from "../../api/client";
import type { EvidenceTab } from "../../app/types";
import {
  formatArtifactDetail,
  formatArtifactTitle,
  formatAtomicAction,
  formatEventKind,
  formatEventMessage,
  formatEventStatus,
  formatEvidence,
  formatGateId,
  formatGateMessage,
  formatGateStatus,
  formatGenericStatus,
  formatLoopLayer,
  formatRoutingDecision
} from "../../types/display";
import { harnessEventEvidencePaths, type ArtifactSummary, type HarnessEvent, type ProjectCompletionAudit, type ProjectReadiness } from "../../types/domain";
import { LiteraryReviewPanel } from "../workspace/LiteraryReviewPanel";
import { eventMatches, formatClock, parseJsonRecord } from "../workspace/workspace-utils";
import styles from "./EvidenceCenter.module.css";

interface EvidenceCenterProps {
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

const tabs: Array<{ id: EvidenceTab; label: string }> = [
  { id: "trace", label: "运行轨迹" },
  { id: "artifacts", label: "产物" },
  { id: "verification", label: "验证" },
  { id: "events", label: "原始事件" }
];

function tone(status: string): string {
  if (["completed", "passed", "committed"].includes(status)) return "success";
  if (["failed", "rejected"].includes(status)) return "danger";
  if (["started", "running", "delta"].includes(status)) return "active";
  return "neutral";
}

function GateSection({ title, gates }: { title: string; gates: Array<{ id: string; status: string; message: string; evidence: string[] }> | null }) {
  return (
    <section className={styles.gateSection}>
      <header><ShieldCheck size={17} /><h2>{title}</h2></header>
      {gates?.length ? gates.map((gate) => (
        <article key={gate.id}>
          <span data-status={gate.status}>{gate.status === "passed" ? <Check size={13} /> : <Circle size={13} />}</span>
          <div><strong>{formatGateId(gate.id)}</strong><p>{formatGateMessage(gate.message)}</p>{gate.evidence.length > 0 && <small>{gate.evidence.map(formatEvidence).join(" · ")}</small>}</div>
          <em>{formatGateStatus(gate.status)}</em>
        </article>
      )) : <p className={styles.empty}>尚未加载门禁状态。</p>}
    </section>
  );
}

export function EvidenceCenter({ events, summaries, artifactPaths, selectedArtifactPath, activeArtifact, readiness, completionAudit, canPause, canResume, canRetry, busy, onSelectArtifact, onPause, onResume, onRetry, onRefreshAudit }: EvidenceCenterProps) {
  const [tab, setTab] = useState<EvidenceTab>("trace");
  const [loopFilter, setLoopFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [eventInspectorOpen, setEventInspectorOpen] = useState(false);
  const [artifactInspectorOpen, setArtifactInspectorOpen] = useState(false);
  const statusEvents = useMemo(
    () => events.filter(
      (event) => event.kind !== "llm_output_delta" && event.kind !== "llm_stream_progress"
    ),
    [events]
  );
  const filteredEvents = useMemo(() => statusEvents.filter((event) => eventMatches(event, loopFilter, statusFilter, query)), [loopFilter, query, statusEvents, statusFilter]);
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  const traceRef = useRef<HTMLDivElement>(null);
  const artifactRef = useRef<HTMLDivElement>(null);
  const rawRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!selectedEventId && statusEvents.length) setSelectedEventId(statusEvents.at(-1)?.event_id ?? null);
  }, [selectedEventId, statusEvents]);

  const traceVirtualizer = useVirtualizer({ count: filteredEvents.length, getScrollElement: () => traceRef.current, estimateSize: () => 64, overscan: 10, initialRect: { width: 640, height: 520 } });
  const artifactVirtualizer = useVirtualizer({ count: summaries.length, getScrollElement: () => artifactRef.current, estimateSize: () => 58, overscan: 10, initialRect: { width: 360, height: 580 } });
  const rawVirtualizer = useVirtualizer({ count: events.length, getScrollElement: () => rawRef.current, estimateSize: () => 62, overscan: 12, initialRect: { width: 900, height: 620 } });
  const traceRows = traceVirtualizer.getVirtualItems();
  const visibleTraceRows = traceRows.length ? traceRows : Array.from({ length: Math.min(filteredEvents.length, 18) }, (_, index) => ({ index, start: index * 64, size: 64 }));
  const artifactRows = artifactVirtualizer.getVirtualItems();
  const visibleArtifactRows = artifactRows.length ? artifactRows : Array.from({ length: Math.min(summaries.length, 20) }, (_, index) => ({ index, start: index * 58, size: 58 }));
  const rawRows = rawVirtualizer.getVirtualItems();
  const visibleRawRows = rawRows.length ? rawRows : Array.from({ length: Math.min(events.length, 20) }, (_, index) => ({ index, start: index * 62, size: 62 }));

  const selectedEvent = statusEvents.find((event) => event.event_id === selectedEventId) ?? filteredEvents.at(-1) ?? null;
  const selectedEventEvidencePaths = selectedEvent ? harnessEventEvidencePaths(selectedEvent) : [];
  const selectedSummary = summaries.find((summary) => summary.path === selectedArtifactPath) ?? null;
  const selectedEventArtifact = selectedEvent?.artifact_path === activeArtifact?.path ? activeArtifact : null;
  const contextSnapshot = selectedEventArtifact?.path.endsWith("context_snapshot.json") ? parseJsonRecord(selectedEventArtifact.content) : null;

  function selectEvent(event: HarnessEvent) {
    setSelectedEventId(event.event_id);
    setEventInspectorOpen(true);
    const primaryEvidence = harnessEventEvidencePaths(event)[0];
    if (primaryEvidence) onSelectArtifact(primaryEvidence);
  }

  function selectArtifact(path: string) {
    onSelectArtifact(path);
    setArtifactInspectorOpen(true);
  }

  return (
    <section className={styles.center}>
      <header className={styles.heading}>
        <div><p>证据中心</p><h1>Harness 运行审计</h1></div>
        <nav aria-label="证据中心视图">{tabs.map((item) => <button key={item.id} className={tab === item.id ? styles.active : ""} onClick={() => setTab(item.id)}>{item.label}</button>)}</nav>
      </header>

      {tab === "trace" && (
        <div className={styles.traceLayout}>
          <div className={styles.filters}>
            <select aria-label="Loop 筛选" value={loopFilter} onChange={(event) => setLoopFilter(event.target.value)}><option value="all">全部 Loop</option><option value="book">全书 Loop</option><option value="story_arc">故事弧 Loop</option><option value="chapter">章节 Loop</option><option value="system">系统</option></select>
            <select aria-label="状态筛选" value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}><option value="all">全部状态</option><option value="started">进行中</option><option value="completed">已完成</option><option value="failed">失败</option><option value="requested">已请求</option></select>
            <label><Search size={15} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索事件、产物或路由决策" /></label>
          </div>
          <div className={styles.traceGrid}>
            <section className={styles.virtualPanel}>
              <header><h2>事件时间线</h2><span>{filteredEvents.length} 条</span></header>
              <div ref={traceRef} className={styles.virtualViewport} data-testid="virtual-event-list">
                <div style={{ height: traceVirtualizer.getTotalSize() || filteredEvents.length * 64, position: "relative" }}>
                  {visibleTraceRows.map((row) => {
                    const event = filteredEvents[row.index];
                    return (
                      <button key={event.event_id} className={selectedEvent?.event_id === event.event_id ? styles.selected : ""} style={{ position: "absolute", transform: `translateY(${row.start}px)`, height: row.size, width: "100%" }} onClick={() => selectEvent(event)}>
                        <span className={styles.node} data-tone={tone(event.status)} />
                        <time>{formatClock(event.timestamp)}</time>
                        <div><strong>{formatAtomicAction(event.atomic_action) || formatEventKind(event.kind)}</strong><small>{formatEventMessage(event.message)}</small></div>
                        <em>{formatLoopLayer(event.loop_layer)}</em>
                      </button>
                    );
                  })}
                </div>
                {!filteredEvents.length && <p className={styles.empty}>没有符合筛选条件的事件。</p>}
              </div>
            </section>
            <aside className={styles.inspector} data-open={eventInspectorOpen}>
              <header><h2>事件检查器</h2><div>{selectedEvent && <span data-tone={tone(selectedEvent.status)}>{formatEventStatus(selectedEvent.status)}</span>}<button className={styles.closeInspector} title="关闭事件检查器" onClick={() => setEventInspectorOpen(false)}><X size={16} /></button></div></header>
              {selectedEvent ? (
                <>
                  <dl>
                    <div><dt>事件类型</dt><dd>{formatEventKind(selectedEvent.kind)}</dd></div>
                    <div><dt>时间</dt><dd>{selectedEvent.timestamp}</dd></div>
                    <div><dt>Loop</dt><dd>{formatLoopLayer(selectedEvent.loop_layer)}</dd></div>
                    <div><dt>原子动作</dt><dd>{formatAtomicAction(selectedEvent.atomic_action)}</dd></div>
                    <div><dt>路由决策</dt><dd>{formatRoutingDecision(selectedEvent.routing_decision)}</dd></div>
                    <div><dt>关联产物</dt><dd>{selectedEvent.artifact_path ?? "-"}</dd></div>
                  </dl>
                  {selectedEventEvidencePaths.length > 0 && (
                    <section className={styles.evidenceLinks} aria-label="Agent 证据文件">
                      <h3>Agent 证据文件</h3>
                      {selectedEventEvidencePaths.map((path) => (
                        <button key={path} onClick={() => selectArtifact(path)}>
                          <FileText size={13} />
                          <span>{path}</span>
                        </button>
                      ))}
                    </section>
                  )}
                  {contextSnapshot && <section className={styles.snapshot}><h3>上下文装配快照</h3><p>来源：{Array.isArray(contextSnapshot.sources) ? contextSnapshot.sources.length : 0} 项</p><p>排除：{Array.isArray(contextSnapshot.excluded) ? contextSnapshot.excluded.length : 0} 项</p><small>{typeof contextSnapshot.assembly_rationale === "string" ? contextSnapshot.assembly_rationale : ""}</small></section>}
                  {JSON.stringify(selectedEvent.payload) !== "{}" && <details><summary>查看事件 Payload</summary><pre>{JSON.stringify(selectedEvent.payload, null, 2)}</pre></details>}
                </>
              ) : <p className={styles.empty}>选择一个事件查看证据。</p>}
            </aside>
          </div>
          <footer className={styles.runControls}>
            <button disabled={!canPause || busy} onClick={() => void onPause()}><Pause size={15} />暂停</button>
            <button disabled={!canResume || busy} onClick={() => void onResume()}><Play size={15} />恢复</button>
            <button disabled={!canRetry || busy} onClick={() => void onRetry()}><RefreshCw size={15} />重试当前章节</button>
            <a href={apiUrl("/api/runs/archive")} download><Archive size={15} />下载 Run 归档</a>
          </footer>
        </div>
      )}

      {tab === "artifacts" && (
        <div className={styles.artifactGrid}>
          <aside className={styles.virtualPanel}>
            <header><h2>落盘产物</h2><span>{artifactPaths.length} 个文件</span></header>
            <div ref={artifactRef} className={styles.virtualViewport} data-testid="virtual-artifact-list">
              <div style={{ height: artifactVirtualizer.getTotalSize() || summaries.length * 58, position: "relative" }}>
                {visibleArtifactRows.map((row) => {
                  const summary = summaries[row.index];
                  return <button key={summary.path} className={selectedArtifactPath === summary.path ? styles.selected : ""} style={{ position: "absolute", transform: `translateY(${row.start}px)`, height: row.size, width: "100%" }} onClick={() => selectArtifact(summary.path)}><FileText size={14} /><div><strong>{formatArtifactTitle(summary)}</strong><small>{summary.path}</small></div><em data-tone={tone(summary.status)}>{formatGenericStatus(summary.status)}</em></button>;
                })}
              </div>
              {!summaries.length && <p className={styles.empty}>还没有落盘产物。</p>}
            </div>
          </aside>
          <section className={styles.artifactInspector} data-open={artifactInspectorOpen}>
            <header><div><p>产物检查器</p><h2>{selectedSummary ? formatArtifactTitle(selectedSummary) : "尚未选择产物"}</h2><small>{selectedArtifactPath}</small></div><div>{selectedSummary && <span data-tone={tone(selectedSummary.status)}>{formatGenericStatus(selectedSummary.status)}</span>}<button className={styles.closeInspector} title="关闭产物检查器" onClick={() => setArtifactInspectorOpen(false)}><X size={16} /></button></div></header>
            {selectedSummary && <p className={styles.description}>{formatArtifactDetail(selectedSummary.detail)}</p>}
            <pre>{activeArtifact?.content ?? "从左侧选择一个文件。"}</pre>
          </section>
        </div>
      )}

      {tab === "verification" && (
        <div className={styles.verification}>
          <div className={styles.gateGrid}><GateSection title="运行准备门禁" gates={readiness?.gates ?? null} /><GateSection title="完成审查门禁" gates={completionAudit?.gates ?? null} /></div>
          <LiteraryReviewPanel completionAudit={completionAudit} onRecorded={onRefreshAudit} />
        </div>
      )}

      {tab === "events" && (
        <section className={styles.rawEvents}>
          <header><div><p>原始事件</p><h2>events.jsonl</h2></div><span>{events.length} 条</span></header>
          <div ref={rawRef} className={styles.rawViewport} data-testid="virtual-raw-event-list">
            <div style={{ height: rawVirtualizer.getTotalSize() || events.length * 62, position: "relative" }}>
              {visibleRawRows.map((row) => {
                const event = events[row.index];
                return <article key={event.event_id} style={{ position: "absolute", transform: `translateY(${row.start}px)`, height: row.size, width: "100%" }}><span className={styles.node} data-tone={tone(event.status)} /><time>{formatClock(event.timestamp)}</time><strong>{event.kind}</strong><code>{event.loop_layer}/{event.atomic_action ?? "-"}</code><p>{event.message}</p></article>;
              })}
            </div>
            {!events.length && <p className={styles.empty}>events.jsonl 当前为空。</p>}
          </div>
        </section>
      )}
    </section>
  );
}
