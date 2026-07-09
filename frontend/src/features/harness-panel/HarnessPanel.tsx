import { FileText, GitBranch, RefreshCw, ShieldCheck } from "lucide-react";
import {
  formatArtifactDetail,
  formatArtifactTitle,
  formatEventFlag,
  formatEventKind,
  formatEvidence,
  formatGateId,
  formatGateMessage,
  formatGateStatus,
  formatGenericStatus,
  formatRoutingDecision,
  formatRunNextAction,
  formatRunNextActionMessage,
  formatSummaryFlags
} from "../../types/display";
import type {
  ArtifactSummary,
  CompletionGate,
  HarnessEvent,
  ProjectCompletionAudit,
  ProjectReadiness
} from "../../types/domain";

interface HarnessPanelProps {
  events: HarnessEvent[];
  artifacts: string[];
  summaries: ArtifactSummary[];
  selectedArtifactPath: string | null;
  artifactContent: string;
  readiness: ProjectReadiness | null;
  completionAudit: ProjectCompletionAudit | null;
  onSelectArtifact: (path: string) => void;
  onRefreshArtifacts: () => Promise<void>;
}

function EvidenceList({ evidence }: { evidence: string[] }) {
  if (evidence.length === 0) {
    return null;
  }

  return (
    <div className="summary-signal-list gate-evidence">
      {evidence.map((item) => (
        <span key={item}>{formatEvidence(item)}</span>
      ))}
    </div>
  );
}

function GateEvidence({ gate }: { gate: CompletionGate }) {
  return <EvidenceList evidence={gate.evidence} />;
}

export function HarnessPanel({
  events,
  artifacts,
  summaries,
  selectedArtifactPath,
  artifactContent,
  readiness,
  completionAudit,
  onSelectArtifact,
  onRefreshArtifacts
}: HarnessPanelProps) {
  const latestStatusEvent = [...events].reverse().find((event) => event.kind !== "llm_output_delta");
  const selectedSummary = summaries.find((summary) => summary.path === selectedArtifactPath) ?? null;
  const recentStatusEvents = events.filter((event) => event.kind !== "llm_output_delta").slice(-6);
  const harnessSummaries = summaries.filter((summary) =>
    [
      "context_snapshot",
      "candidate_observations",
      "verification",
      "candidate_state_patch",
      "committed_state_patch",
      "state_patch_rejection",
      "retry_manifest",
      "review",
      "arc_revision",
      "book_feedback"
    ].includes(summary.kind)
  );

  return (
    <aside className="right-panel">
      <div className="panel-title panel-title-split">
        <span>
          <ShieldCheck size={18} />
          Harness 面板
        </span>
        <button title="刷新产物" onClick={() => void onRefreshArtifacts()}>
          <RefreshCw size={16} />
        </button>
      </div>
      <section className="signal-card">
        <h2>路由</h2>
        <p>{formatRoutingDecision(latestStatusEvent?.routing_decision)}</p>
      </section>
      <section className="signal-card">
        <h2>运行准备</h2>
        {readiness ? (
          <div className="completion-gates">
            <span className={`summary-status ${readiness.status}`}>
              {formatGateStatus(readiness.status)}
            </span>
            <div className="completion-gate">
              <strong>下一步：{formatRunNextAction(readiness.next_action)}</strong>
              {readiness.next_action.command && <small>{readiness.next_action.command}</small>}
              <p>{formatRunNextActionMessage(readiness.next_action.message)}</p>
              <EvidenceList evidence={readiness.next_action.evidence} />
            </div>
            {readiness.gates.map((gate) => (
              <div key={gate.id} className="completion-gate">
                <strong>{formatGateId(gate.id)}</strong>
                <span className={`summary-status ${gate.status}`}>
                  {formatGateStatus(gate.status)}
                </span>
                <p>{formatGateMessage(gate.message)}</p>
                <GateEvidence gate={gate} />
              </div>
            ))}
          </div>
        ) : (
          <p className="muted">尚未加载运行准备状态。</p>
        )}
      </section>
      <section className="signal-card">
        <h2>完成审查</h2>
        {completionAudit ? (
          <div className="completion-gates">
            <span className={`summary-status ${completionAudit.status}`}>
              {formatGateStatus(completionAudit.status)}
            </span>
            {completionAudit.gates.map((gate) => (
              <div key={gate.id} className="completion-gate">
                <strong>{formatGateId(gate.id)}</strong>
                <span className={`summary-status ${gate.status}`}>
                  {formatGateStatus(gate.status)}
                </span>
                <p>{formatGateMessage(gate.message)}</p>
                <GateEvidence gate={gate} />
              </div>
            ))}
          </div>
        ) : (
          <p className="muted">尚未加载完成审查。</p>
        )}
      </section>
      <section className="signal-card">
        <h2>最近产物</h2>
        <p>{latestStatusEvent?.artifact_path ?? "无"}</p>
      </section>
      <section className="signal-card">
        <h2>事件信号</h2>
        <div className="signal-list">
          {recentStatusEvents.map((event) => (
            <div key={event.event_id}>
              <GitBranch size={14} />
              <span>{formatEventKind(event.kind)}</span>
            </div>
          ))}
        </div>
      </section>
      <section className="signal-card">
        <h2>Harness 产物</h2>
        <div className="summary-list">
          {harnessSummaries.slice(-8).map((summary) => (
            <button
              key={summary.path}
              className={summary.path === selectedArtifactPath ? "active" : ""}
              onClick={() => onSelectArtifact(summary.path)}
            >
              <span className={`summary-status ${summary.status}`}>
                {formatGenericStatus(summary.status)}
              </span>
              <strong>{formatArtifactTitle(summary)}</strong>
              <small>{formatArtifactDetail(summary.detail)}</small>
              <span className="summary-path">{summary.path}</span>
              <span className="summary-flags">{formatSummaryFlags(summary).join(" / ")}</span>
            </button>
          ))}
          {harnessSummaries.length === 0 && <p className="muted">还没有 harness 产物。</p>}
        </div>
      </section>
      {selectedSummary && (
        <section className="signal-card">
          <h2>选中证据</h2>
          <div className="selected-summary">
            <strong>{formatArtifactTitle(selectedSummary)}</strong>
            <span className={`summary-status ${selectedSummary.status}`}>
              {formatGenericStatus(selectedSummary.status)}
            </span>
            <p>{formatArtifactDetail(selectedSummary.detail)}</p>
            <div className="summary-flags-row">
              {selectedSummary.candidate && <span>候选</span>}
              {selectedSummary.committed && <span>已提交</span>}
              {selectedSummary.routing_decision && (
                <span>{formatRoutingDecision(selectedSummary.routing_decision)}</span>
              )}
              {selectedSummary.profile_id && <span>配置：{selectedSummary.profile_id}</span>}
              {selectedSummary.model_snapshot && (
                <span>模型：{selectedSummary.model_snapshot}</span>
              )}
              {formatEventFlag(selectedSummary.event_status) && (
                <span className={`event-${selectedSummary.event_status}`}>
                  {formatEventFlag(selectedSummary.event_status)}
                </span>
              )}
            </div>
            {selectedSummary.event_note && (
              <small className={`event-note ${selectedSummary.event_status}`}>
                {formatArtifactDetail(selectedSummary.event_note)}
              </small>
            )}
            <div className="summary-signal-list">
              {selectedSummary.signals.slice(0, 8).map((signal) => (
                <span key={signal}>{signal}</span>
              ))}
              {selectedSummary.signals.length === 0 && <small>没有结构化信号。</small>}
            </div>
          </div>
        </section>
      )}
      <section className="signal-card">
        <h2>全部产物</h2>
        <div className="artifact-list">
          {artifacts.map((path) => (
            <button
              key={path}
              className={path === selectedArtifactPath ? "active" : ""}
              onClick={() => onSelectArtifact(path)}
            >
              <FileText size={14} />
              <span>{path}</span>
            </button>
          ))}
        </div>
      </section>
      <section className="signal-card artifact-preview">
        <h2>{selectedArtifactPath ?? "预览"}</h2>
        <pre>{artifactContent || "还没有选择产物。"}</pre>
      </section>
    </aside>
  );
}
