import { ArrowRight, Check, ChevronRight, Circle, FileText, GitBranch, Radio } from "lucide-react";
import {
  formatAtomicAction,
  formatEventKind,
  formatEventMessage,
  formatGenericStatus,
  formatLoopLayer,
  formatOptionalId,
  formatRunStatus
} from "../../types/display";
import type {
  ArtifactSummary,
  CurrentArcState,
  HarnessEvent,
  ProjectSummary
} from "../../types/domain";
import { artifactForChapter, chapterPipeline, formatClock, pipelineState } from "./workspace-utils";

interface CockpitViewProps {
  project: ProjectSummary;
  events: HarnessEvent[];
  currentArc: CurrentArcState | null;
  summaries: ArtifactSummary[];
  modelOutput: string;
  activeArtifact: { path: string; content: string } | null;
  canonCounts: Record<string, number>;
  onSelectArtifact: (path: string) => void;
  onOpenTrace: () => void;
  onOpenCanon: () => void;
}

const chapterArtifacts = [
  ["context_snapshot.json", "context_snapshot.json"],
  ["goal.md", "goal.md"],
  ["draft.md", "draft.md"],
  ["observations.json", "observations.json"],
  ["review.md", "review.md"],
  ["verification.json", "verification.json"],
  ["final.md", "final.md"]
] as const;

export function CockpitView({
  project,
  events,
  currentArc,
  summaries,
  modelOutput,
  activeArtifact,
  canonCounts,
  onSelectArtifact,
  onOpenTrace,
  onOpenCanon
}: CockpitViewProps) {
  const metadata = project.metadata;
  const statusEvents = events.filter((event) => event.kind !== "llm_output_delta");
  const latestEvent = statusEvents.at(-1) ?? null;
  const recentEvents = statusEvents.slice(-5);
  const activeChapter = metadata.active_chapter_id;
  const visibleContent = modelOutput || activeArtifact?.content || "等待 harness 生成新的可见输出。";
  const isStreaming = Boolean(modelOutput) && metadata.run_status === "running";

  return (
    <div className="cockpit-grid">
      <aside className="np-surface cockpit-status-panel">
        <section>
          <h2>Harness 状态</h2>
          <dl className="runtime-facts">
            <div>
              <dt>当前 Loop</dt>
              <dd>{formatLoopLayer(latestEvent?.loop_layer ?? "system")}</dd>
            </div>
            <div>
              <dt>当前原子动作</dt>
              <dd>{formatAtomicAction(latestEvent?.atomic_action)}</dd>
            </div>
            <div>
              <dt>运行状态</dt>
              <dd className={`status-text ${metadata.run_status}`}>
                {formatRunStatus(metadata.run_status)}
              </dd>
            </div>
            <div>
              <dt>Active Arc</dt>
              <dd>{formatOptionalId(currentArc?.arc_id ?? metadata.active_arc_id)}</dd>
            </div>
            <div>
              <dt>Active Chapter</dt>
              <dd>{formatOptionalId(activeChapter)}</dd>
            </div>
            <div>
              <dt>安全检查点</dt>
              <dd>{events.some((event) => event.kind === "safe_checkpoint_reached") ? "已到达" : "未触发"}</dd>
            </div>
          </dl>
        </section>

        <section className="recent-runtime-events">
          <h3>最近事件</h3>
          <ol>
            {recentEvents.map((event) => (
              <li key={event.event_id}>
                <span className={`event-node ${event.status}`} />
                <time>{formatClock(event.timestamp)}</time>
                <strong>{formatEventKind(event.kind)}</strong>
                <small>{event.status === "completed" ? "✓" : event.status}</small>
              </li>
            ))}
            {recentEvents.length === 0 && <li className="empty-line">还没有运行事件</li>}
          </ol>
          <button className="text-link" onClick={onOpenTrace}>查看全部事件 <ArrowRight size={15} /></button>
        </section>
      </aside>

      <section className="np-surface execution-panel">
        <header className="view-heading compact-heading execution-heading">
          <div>
            <h1>当前执行流</h1>
            <p>章节 loop 的候选产物会在验证后才提交为正史。</p>
          </div>
        </header>

        <div className="pipeline-strip">
          {chapterPipeline.map((step) => {
            const state = pipelineState(step.id, latestEvent, events);
            return (
              <div key={step.id} className={`pipeline-step ${state}`}>
                <span>{state === "done" ? <Check size={14} strokeWidth={3} /> : <Circle size={12} />}</span>
                <small>{step.label}</small>
              </div>
            );
          })}
        </div>

        <article className="live-output-panel">
          <header>
            <span>{formatClock(latestEvent?.timestamp ?? new Date().toISOString())}</span>
            <strong>
              LLM {metadata.run_status === "running" ? "正在" : "最近"}{formatAtomicAction(latestEvent?.atomic_action)}
            </strong>
            <em>{isStreaming ? "streaming" : formatRunStatus(metadata.run_status)}</em>
          </header>
          <div className="live-output-copy">
            <h2>{formatOptionalId(activeChapter)} · {formatAtomicAction(latestEvent?.atomic_action)}</h2>
            <pre>{visibleContent}</pre>
          </div>
          <footer>
            <span>可见输出：{visibleContent.length.toLocaleString("zh-CN")} 字符</span>
            {activeArtifact && (
              <button className="text-button" onClick={() => onSelectArtifact(activeArtifact.path)}>
                查看完整产物
              </button>
            )}
          </footer>
        </article>

        {latestEvent && (
          <div className="current-event-note">
            <Radio size={15} />
            <span>{formatEventMessage(latestEvent.message)}</span>
          </div>
        )}
      </section>

      <aside className="np-surface story-context-panel">
        <section>
          <h2>故事上下文</h2>
          <article className="current-arc-card">
            <strong>{currentArc ? `${currentArc.arc_id} · 当前故事弧` : "尚未创建故事弧"}</strong>
            <p>
              {currentArc
                ? `${currentArc.completed_chapter_ids.length} / ${currentArc.target_chapter_count} 章已完成，状态为${formatGenericStatus(currentArc.status)}。`
                : "批准全书方向并启动 harness 后，将只规划当前故事弧。"}
            </p>
            {currentArc && <span>{currentArc.human_review === "awaiting_review" ? "等待审批" : "运行中"}</span>}
          </article>
        </section>

        <section>
          <h3>正史状态 Canon</h3>
          <div className="canon-count-grid">
            <button onClick={onOpenCanon}><span>角色</span><strong>{canonCounts.characters ?? 0}</strong></button>
            <button onClick={onOpenCanon}><span>关系</span><strong>{canonCounts.relationships ?? 0}</strong></button>
            <button onClick={onOpenCanon}><span>世界事实</span><strong>{canonCounts.world_facts ?? 0}</strong></button>
            <button onClick={onOpenCanon}><span>伏笔</span><strong>{canonCounts.foreshadowing ?? 0}</strong></button>
          </div>
        </section>

        <section className="artifact-checklist">
          <h3>当前产物（摘要）</h3>
          <ul>
            {chapterArtifacts.map(([fileName, label]) => {
              const summary = artifactForChapter(summaries, activeChapter, fileName);
              return (
                <li key={fileName}>
                  {summary ? <span className={`artifact-dot ${summary.status}`} /> : <Circle size={14} />}
                  <button
                    disabled={!summary}
                    onClick={() => summary && onSelectArtifact(summary.path)}
                  >
                    {label}
                  </button>
                  <small>{summary ? formatGenericStatus(summary.status) : "待生成"}</small>
                </li>
              );
            })}
          </ul>
          <button className="text-link" onClick={onOpenTrace}>
            进入产物中心 <ChevronRight size={15} />
          </button>
        </section>

        <div className="project-path-note">
          <FileText size={14} />
          <span title={project.path}>{project.path}</span>
        </div>
      </aside>
    </div>
  );
}
