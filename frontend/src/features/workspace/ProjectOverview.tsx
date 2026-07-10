import { ArrowRight, BookOpen, FileDown, Play, RotateCw, ShieldCheck } from "lucide-react";
import {
  formatArtifactTitle,
  formatGateId,
  formatGateMessage,
  formatGateStatus,
  formatGenericStatus,
  formatOperationMode,
  formatOptionalId,
  formatRunStatus
} from "../../types/display";
import type {
  ArtifactSummary,
  CurrentArcState,
  ProjectReadiness,
  ProjectSummary
} from "../../types/domain";

interface ProjectOverviewProps {
  project: ProjectSummary;
  currentArc: CurrentArcState | null;
  readiness: ProjectReadiness | null;
  summaries: ArtifactSummary[];
  canonCounts: Record<string, number>;
  canStart: boolean;
  canResume: boolean;
  busy: boolean;
  onStart: () => Promise<void>;
  onResume: () => Promise<void>;
  onExport: () => Promise<void>;
  onNavigate: (view: "plan" | "cockpit" | "arcs" | "canon" | "trace") => void;
  onSelectArtifact: (path: string) => void;
}

export function ProjectOverview({
  project,
  currentArc,
  readiness,
  summaries,
  canonCounts,
  canStart,
  canResume,
  busy,
  onStart,
  onResume,
  onExport,
  onNavigate,
  onSelectArtifact
}: ProjectOverviewProps) {
  const metadata = project.metadata;
  const recentArtifacts = summaries.slice(-6).reverse();

  return (
    <section className="np-surface overview-view">
      <header className="view-heading overview-heading">
        <div>
          <p className="eyebrow">当前小说项目</p>
          <h1>{project.title}</h1>
          <p>本地单用户项目 · {formatOperationMode(metadata.operation_mode)}</p>
        </div>
        <div className="overview-actions">
          <button className="gold-button" disabled={!canStart || busy} onClick={() => void onStart()}>
            <Play size={16} /> 启动
          </button>
          <button className="outline-button" disabled={!canResume || busy} onClick={() => void onResume()}>
            <RotateCw size={16} /> 继续
          </button>
          <button className="outline-button" disabled={busy} onClick={() => void onExport()}>
            <FileDown size={16} /> 导出全书
          </button>
        </div>
      </header>

      <div className="overview-stat-grid">
        <article><span>运行状态</span><strong>{formatRunStatus(metadata.run_status)}</strong><small>{readiness?.next_action.message ?? "等待状态检查"}</small></article>
        <article><span>当前故事弧</span><strong>{formatOptionalId(currentArc?.arc_id ?? metadata.active_arc_id)}</strong><small>{currentArc ? `${currentArc.completed_chapter_ids.length}/${currentArc.target_chapter_count} 章` : "尚未规划"}</small></article>
        <article><span>当前章节</span><strong>{formatOptionalId(metadata.active_chapter_id)}</strong><small>候选产物验证后提交</small></article>
        <article><span>正史条目</span><strong>{Object.values(canonCounts).reduce((total, count) => total + count, 0)}</strong><small>角色、关系、事实与伏笔</small></article>
      </div>

      <div className="overview-columns">
        <section className="overview-readiness">
          <header><ShieldCheck size={18} /><h2>下一步与运行门禁</h2></header>
          {readiness ? (
            <>
              <article className="next-action-card">
                <strong>{readiness.next_action.message}</strong>
                <span className={`soft-badge ${readiness.status}`}>{formatGateStatus(readiness.status)}</span>
              </article>
              <div className="compact-gates">
                {readiness.gates.map((gate) => (
                  <div key={gate.id}>
                    <span className={`status-dot ${gate.status}`} />
                    <strong>{formatGateId(gate.id)}</strong>
                    <small>{formatGateMessage(gate.message)}</small>
                    <em>{formatGateStatus(gate.status)}</em>
                  </div>
                ))}
              </div>
            </>
          ) : <p className="empty-state">正在加载运行门禁...</p>}
        </section>

        <section className="overview-artifacts">
          <header><BookOpen size={18} /><h2>最近产物</h2></header>
          <div>
            {recentArtifacts.map((summary) => (
              <button key={summary.path} onClick={() => onSelectArtifact(summary.path)}>
                <span className={`status-dot ${summary.status}`} />
                <strong>{formatArtifactTitle(summary)}</strong>
                <small>{summary.path}</small>
                <em>{formatGenericStatus(summary.status)}</em>
              </button>
            ))}
            {recentArtifacts.length === 0 && <p className="empty-state">还没有写作产物。</p>}
          </div>
          <button className="text-link" onClick={() => onNavigate("trace")}>进入产物中心 <ArrowRight size={15} /></button>
        </section>
      </div>

      <nav className="overview-shortcuts">
        <button onClick={() => onNavigate("plan")}><span>开书规划</span><small>全书方向与用户确认</small><ArrowRight size={16} /></button>
        <button onClick={() => onNavigate("cockpit")}><span>创作工作台</span><small>实时运行与模型输出</small><ArrowRight size={16} /></button>
        <button onClick={() => onNavigate("arcs")}><span>故事弧与章节</span><small>滚动计划与参与审批</small><ArrowRight size={16} /></button>
        <button onClick={() => onNavigate("canon")}><span>正史状态</span><small>只读已提交事实</small><ArrowRight size={16} /></button>
      </nav>

      <footer className="project-root-line">
        <span>项目路径</span>
        <code>{project.path}</code>
      </footer>
    </section>
  );
}
