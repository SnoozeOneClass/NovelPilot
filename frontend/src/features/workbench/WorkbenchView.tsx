import { Check, Circle, FileDown, FileText, GitBranch, PanelLeft, PanelRight, Play, Radio, RotateCw, ShieldCheck, X } from "lucide-react";
import { useState } from "react";
import {
  formatArtifactTitle,
  formatAtomicAction,
  formatEventKind,
  formatEventMessage,
  formatGateId,
  formatGateMessage,
  formatGateStatus,
  formatGenericStatus,
  formatLoopLayer,
  formatOptionalId,
  formatRunNextActionMessage,
  formatRunStatus
} from "../../types/display";
import type { ArtifactSummary, BookRevisionState, CurrentArcState, HarnessEvent, ProjectReadiness, ProjectSummary } from "../../types/domain";
import { artifactForChapter, chapterPipeline, eventBelongsToChapter, formatClock, pipelineState } from "../workspace/workspace-utils";
import styles from "./WorkbenchView.module.css";

interface WorkbenchViewProps {
  project: ProjectSummary;
  events: HarnessEvent[];
  currentArc: CurrentArcState | null;
  summaries: ArtifactSummary[];
  modelOutput: string;
  activeArtifact: { path: string; content: string } | null;
  canonCounts: Record<string, number>;
  readiness: ProjectReadiness | null;
  bookRevision: BookRevisionState | null;
  canStart: boolean;
  canResume: boolean;
  busy: boolean;
  onStart: () => Promise<void>;
  onResume: () => Promise<void>;
  onApproveBookRevision: () => Promise<void>;
  onExport: () => Promise<void>;
  onSelectArtifact: (path: string) => void;
  onOpenEvidence: () => void;
  onOpenStory: () => void;
}

const chapterArtifacts = [
  ["context_snapshot.json", "上下文快照"],
  ["goal.md", "章节目标"],
  ["draft.md", "候选正文"],
  ["observations.json", "候选观测"],
  ["review.md", "语义审查"],
  ["verification.json", "章节验证"],
  ["final.md", "正式章节"]
] as const;

function eventTone(status: HarnessEvent["status"]): string {
  if (status === "completed") return "success";
  if (status === "failed") return "danger";
  if (status === "started" || status === "delta") return "active";
  return "neutral";
}

export function WorkbenchView({ project, events, currentArc, summaries, modelOutput, activeArtifact, canonCounts, readiness, bookRevision, canStart, canResume, busy, onStart, onResume, onApproveBookRevision, onExport, onSelectArtifact, onOpenEvidence, onOpenStory }: WorkbenchViewProps) {
  const [auxPanel, setAuxPanel] = useState<"runtime" | "context" | null>(null);
  const metadata = project.metadata;
  const statusEvents = events.filter(
    (event) => event.kind !== "llm_output_delta" && event.kind !== "llm_stream_progress"
  );
  const latestEvent = statusEvents.at(-1) ?? null;
  const recentEvents = statusEvents.slice(-12);
  const activeChapter = metadata.active_chapter_id;
  const chapterEvents = activeChapter ? statusEvents.filter((event) => eventBelongsToChapter(event, activeChapter)) : [];
  const latestChapterEvent = chapterEvents.at(-1) ?? null;
  const visibleContent = modelOutput || activeArtifact?.content || "等待 Harness 生成新的可见输出。";
  const isStreaming = Boolean(modelOutput) && metadata.run_status === "running";
  const safeCheckpoint = latestChapterEvent?.kind === "safe_checkpoint_reached";
  const recentArtifacts = summaries.slice(-5).reverse();

  return (
    <section className={styles.workbench}>
      <aside className={styles.runtimePanel} data-open={auxPanel === "runtime"}>
        <header><div><p>Harness</p><h2>运行状态</h2></div><div className={styles.panelHeaderActions}><span data-status={metadata.run_status}>{formatRunStatus(metadata.run_status)}</span><button className={styles.closePanel} title="关闭运行详情" onClick={() => setAuxPanel(null)}><X size={16} /></button></div></header>
        <dl className={styles.runtimeFacts}>
          <div><dt>当前 Loop</dt><dd>{formatLoopLayer(latestEvent?.loop_layer ?? "system")}</dd></div>
          <div><dt>原子动作</dt><dd>{formatAtomicAction(latestEvent?.atomic_action)}</dd></div>
          <div><dt>故事弧</dt><dd>{formatOptionalId(currentArc?.arc_id ?? metadata.active_arc_id)}</dd></div>
          <div><dt>章节</dt><dd>{formatOptionalId(activeChapter)}</dd></div>
          <div><dt>安全检查点</dt><dd>{safeCheckpoint ? "已到达" : "等待中"}</dd></div>
        </dl>

        <section className={styles.pipeline}>
          <h3>章节 Pipeline</h3>
          {chapterPipeline.map((step) => {
            const state = pipelineState(step.id, latestChapterEvent, chapterEvents);
            return (
              <div key={step.id} data-state={state}>
                <span>{state === "done" ? <Check size={12} /> : <Circle size={11} />}</span>
                <strong>{step.label}</strong>
                <small>{state === "done" ? "完成" : state === "active" ? "进行中" : "等待"}</small>
              </div>
            );
          })}
        </section>

        <section className={styles.checkpoints}>
          <h3>最近检查点</h3>
          {statusEvents.filter((event) => event.kind.includes("checkpoint") || event.status === "failed").slice(-4).reverse().map((event) => (
            <button key={event.event_id} onClick={onOpenEvidence}><span data-tone={eventTone(event.status)} /><div><strong>{formatEventKind(event.kind)}</strong><small>{formatClock(event.timestamp)}</small></div></button>
          ))}
          {!statusEvents.some((event) => event.kind.includes("checkpoint") || event.status === "failed") && <p>尚无检查点记录</p>}
        </section>
      </aside>

      <main className={styles.executionPanel}>
        <header className={styles.executionHeader}>
          <div><p>当前执行流</p><h1>{latestEvent ? formatAtomicAction(latestEvent.atomic_action) : "等待启动 Harness"}</h1></div>
          <div className={styles.runActions}>
            <button className={styles.runtimeToggle} title="查看运行详情" onClick={() => setAuxPanel("runtime")}><PanelLeft size={15} /></button>
            <button className={styles.contextToggle} title="查看故事上下文" onClick={() => setAuxPanel("context")}><PanelRight size={15} /></button>
            <button className={styles.startButton} disabled={!canStart || busy} onClick={() => void onStart()}><Play size={15} />启动</button>
            <button disabled={!canResume || busy} onClick={() => void onResume()}><RotateCw size={15} />继续</button>
            <button disabled={busy} onClick={() => void onExport()}><FileDown size={15} />导出</button>
          </div>
        </header>

        {bookRevision ? (
          <section className={styles.bookRevisionApproval}>
            <header>
              <div><p>全书契约修订</p><h2>候选已通过评测，等待你的明确批准</h2></div>
              <span>v{bookRevision.base_book_version} → v{bookRevision.target_book_version}</span>
            </header>
            <p>{bookRevision.summary}</p>
            <dl>
              <div><dt>冲突字段</dt><dd>{bookRevision.contract_field}</dd></div>
              <div><dt>修订原因</dt><dd>{bookRevision.impossibility_reason}</dd></div>
              <div><dt>来源</dt><dd>{bookRevision.source_loop} / {bookRevision.source_artifact}</dd></div>
            </dl>
            <footer>
              <button onClick={() => onSelectArtifact(bookRevision.candidate.direction_path)}>查看候选方向</button>
              <button onClick={() => onSelectArtifact(bookRevision.review_path)}>查看评测</button>
              <button className={styles.approveRevisionButton} disabled={busy} onClick={() => void onApproveBookRevision()}>
                <ShieldCheck size={15} />{busy ? "正在批准..." : "批准并替换未来全书契约"}
              </button>
            </footer>
          </section>
        ) : recentEvents.length === 0 ? (
          <section className={styles.idleState}>
            <ShieldCheck size={24} />
            <h2>{readiness ? formatRunNextActionMessage(readiness.next_action.message) : "正在检查运行条件"}</h2>
            <p>全书方向、模型配置和运行状态通过门禁后，Harness 会开始滚动规划当前故事弧。</p>
            {readiness && <div>{readiness.gates.map((gate) => <span key={gate.id} data-status={gate.status}><strong>{formatGateId(gate.id)}</strong>{formatGateStatus(gate.status)}</span>)}</div>}
          </section>
        ) : (
          <section className={styles.eventStream}>
            <header><h2>事件流</h2><button onClick={onOpenEvidence}><GitBranch size={14} />全部事件</button></header>
            <ol>{recentEvents.map((event) => (
              <li key={event.event_id}>
                <span data-tone={eventTone(event.status)} />
                <time>{formatClock(event.timestamp)}</time>
                <strong>{formatAtomicAction(event.atomic_action) || formatEventKind(event.kind)}</strong>
                <small>{formatEventMessage(event.message)}</small>
              </li>
            ))}</ol>
          </section>
        )}

        <section className={styles.outputPanel}>
          <header>
            <div><Radio size={14} /><strong>{isStreaming ? "模型输出中" : "最近可见输出"}</strong></div>
            <span>{visibleContent.length.toLocaleString("zh-CN")} 字符</span>
          </header>
          <pre>{visibleContent}</pre>
          <footer>
            <span>{latestEvent ? formatEventMessage(latestEvent.message) : "尚未开始运行"}</span>
            {activeArtifact && <button onClick={() => onSelectArtifact(activeArtifact.path)}>查看完整产物</button>}
          </footer>
        </section>
      </main>

      <aside className={styles.contextPanel} data-open={auxPanel === "context"}>
        <header><div><p>故事上下文</p><h2>{currentArc?.arc_id ?? "尚未创建故事弧"}</h2></div><div className={styles.panelHeaderActions}><button onClick={onOpenStory}>查看世界</button><button className={styles.closePanel} title="关闭故事上下文" onClick={() => setAuxPanel(null)}><X size={16} /></button></div></header>
        <section className={styles.arcSummary}>
          <strong>{currentArc ? `${currentArc.completed_chapter_ids.length}/${currentArc.target_chapter_count} 章已完成` : "等待首次故事弧规划"}</strong>
          <p>{currentArc ? `当前状态：${formatGenericStatus(currentArc.status)}。${currentArc.human_review === "awaiting_review" ? "计划正在等待人工审批。" : "Harness 将依据已提交状态继续推进。"}` : "全书方向批准后，系统只规划当前故事弧。"}</p>
        </section>

        <section className={styles.canonSummary}>
          <h3>正史状态</h3>
          <div>
            {[['角色', canonCounts.characters ?? 0], ['关系', canonCounts.relationships ?? 0], ['事实', canonCounts.world_facts ?? 0], ['伏笔', canonCounts.foreshadowing ?? 0]].map(([label, count]) => <button key={label} onClick={onOpenStory}><span>{label}</span><strong>{count}</strong></button>)}
          </div>
        </section>

        <section className={styles.artifacts}>
          <h3>当前章节产物</h3>
          {chapterArtifacts.map(([fileName, label]) => {
            const summary = artifactForChapter(summaries, activeChapter, fileName);
            return <button key={fileName} disabled={!summary} onClick={() => summary && onSelectArtifact(summary.path)}><span data-status={summary?.status ?? "pending"}>{summary ? <FileText size={13} /> : <Circle size={12} />}</span><strong>{label}</strong><small>{summary ? formatGenericStatus(summary.status) : "待生成"}</small></button>;
          })}
        </section>

        <section className={styles.recentArtifacts}>
          <h3>最近产物</h3>
          {recentArtifacts.map((summary) => <button key={summary.path} onClick={() => onSelectArtifact(summary.path)}><strong>{formatArtifactTitle(summary)}</strong><small>{summary.path}</small></button>)}
        </section>
      </aside>
    </section>
  );
}
