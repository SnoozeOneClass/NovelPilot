import { useQuery } from "@tanstack/react-query";
import { ArrowDown, BookOpen, Play, RotateCw, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import { Button } from "../../components/ui/Button";
import type { ArtifactSummary, BookRevisionState, CurrentArcState, HarnessEvent, ProjectReadiness, ProjectSummary } from "../../types/domain";
import { CreationComposer } from "./CreationComposer";
import { CreationDetailsSheet } from "./CreationDetailsSheet";
import { CreationStageHeader } from "./CreationStageHeader";
import { LiveChapterDocument } from "./LiveChapterDocument";
import { StoryArcReviewTask } from "./StoryArcReviewTask";
import { latestChapterDraft, reduceChapterDraftStreams } from "./chapter-draft-stream";
import { deriveCreationViewModel, type CreationViewModel } from "./creation-view-model";
import styles from "./CreationView.module.css";

interface CreationViewProps {
  project: ProjectSummary;
  events: HarnessEvent[];
  currentArc: CurrentArcState | null;
  summaries: ArtifactSummary[];
  readiness: ProjectReadiness | null;
  bookRevision: BookRevisionState | null;
  busy: boolean;
  feedback: string;
  sendingFeedback: boolean;
  onFeedbackChange: (value: string) => void;
  onSendFeedback: () => Promise<boolean>;
  onRequestArcRevision: (message: string) => Promise<boolean>;
  onStart: () => Promise<void>;
  onApproveArc: (targetChapterCount: number) => Promise<boolean>;
  onApproveBookRevision: () => Promise<void>;
  onResume: () => Promise<void>;
  onRetryFailedRun: () => Promise<void>;
  onRetryChapter: () => Promise<void>;
  onRecoverStale: () => Promise<void>;
  onSelectArtifact: (path: string) => void;
}

function feedbackText(event: HarnessEvent): string | null {
  const value = event.payload.feedback;
  return typeof value === "string" && value.trim() ? value : null;
}

function failureSummary(event: HarnessEvent): string {
  const category = event.payload.category;
  const message = event.message;
  if (category === "provider_auth" || category === "unsupported_capability") {
    return "当前模型配置或接口能力不可用。请先在设置中修正模型配置，再重试同一个检查点；已有候选和正式正文不会丢失。";
  }
  const providerFailure = category === "transport_provider"
    || /provider (?:request failed|returned \d{3}|authentication is unavailable)/i.test(message)
    || /\b(?:SSL|TLS|EOF|timeout|timed out|auth_unavailable)\b/i.test(message);
  if (providerFailure) {
    return "模型服务连接意外中断。候选内容和创作现场已保留，可以在服务恢复后重新连接并继续。原始网络错误可在“查看详细证据”中查看。";
  }
  return message;
}

function latestActionableFailure(events: HarnessEvent[], model: CreationViewModel): HarnessEvent | null {
  const needsFailureAction = model.stage === "chapter_recovery"
    || model.stage === "failed" && model.primaryAction !== "recover_stale";
  if (!needsFailureAction) return null;

  let currentRunId: string | null = null;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if ((event.kind === "run_started" || event.kind === "run_resumed") && event.run_id) {
      currentRunId = event.run_id;
      break;
    }
  }
  if (!currentRunId) return null;

  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.status === "failed" && event.run_id === currentRunId) return event;
  }
  return null;
}

function scrollToBottom(element: HTMLDivElement | null, behavior: ScrollBehavior = "smooth") {
  if (!element) return;
  if (typeof element.scrollTo === "function") {
    element.scrollTo({ top: element.scrollHeight, behavior });
  } else {
    element.scrollTop = element.scrollHeight;
  }
}

export function CreationView({ project, events, currentArc, summaries, readiness, bookRevision, busy, feedback, sendingFeedback, onFeedbackChange, onSendFeedback, onRequestArcRevision, onStart, onApproveArc, onApproveBookRevision, onResume, onRetryFailedRun, onRetryChapter, onRecoverStale, onSelectArtifact }: CreationViewProps) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [following, setFollowing] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const streams = useMemo(() => reduceChapterDraftStreams(events), [events]);
  const latestDraft = latestChapterDraft(streams);
  const model = useMemo(() => deriveCreationViewModel({ project, readiness, currentArc, bookRevision, events }), [bookRevision, currentArc, events, project, readiness]);
  const arcPlanQuery = useQuery({
    queryKey: ["creation", project.metadata.project_id, "arc-plan", currentArc?.plan_path],
    queryFn: () => api.artifactContent(currentArc?.plan_path ?? ""),
    enabled: Boolean(currentArc?.plan_path && model.stage === "story_arc_review")
  });
  const activeChapterId = project.metadata.active_chapter_id ?? latestDraft?.chapterId ?? null;
  const finalSummary = activeChapterId ? summaries.find((summary) => summary.path === `chapters/${activeChapterId}/final.md`) : null;
  const fallbackSummary = activeChapterId ? summaries.find((summary) => summary.path === `chapters/${activeChapterId}/draft.md`) : null;
  const prosePath = finalSummary?.path ?? latestDraft?.artifactPath ?? fallbackSummary?.path ?? null;
  const proseQuery = useQuery({
    queryKey: ["creation", project.metadata.project_id, "prose", prosePath],
    queryFn: () => api.artifactContent(prosePath ?? ""),
    enabled: Boolean(prosePath)
  });
  const prose = latestDraft?.status === "streaming" ? latestDraft.text : proseQuery.data?.content ?? latestDraft?.text ?? "";
  const proseStatus = finalSummary
    ? "committed" as const
    : latestDraft?.status ?? (fallbackSummary ? "candidate" as const : "streaming" as const);
  const completedChapterIds = useMemo(() => [...new Set(summaries.map((summary) => summary.path.match(/^chapters\/([^/]+)\/final\.md$/)?.[1]).filter((value): value is string => Boolean(value)))], [summaries]);
  const actionableFailure = latestActionableFailure(events, model);
  const authorEvents = events.filter((event) => ["user_feedback", "feedback_processed"].includes(event.kind) || event.event_id === actionableFailure?.event_id).slice(-20);

  useEffect(() => {
    if (!following) return;
    scrollToBottom(scrollRef.current);
  }, [authorEvents.length, following, prose]);

  async function submitComposer() {
    if (!feedback.trim()) return;
    if (model.stage === "story_arc_review") {
      const accepted = await onRequestArcRevision(feedback.trim());
      if (accepted) onFeedbackChange("");
      return;
    }
    await onSendFeedback();
  }

  return (
    <section className={styles.creation}>
      <CreationStageHeader model={model} detailCount={events.length} onOpenDetails={() => setDetailsOpen(true)} />
      <div
        ref={scrollRef}
        className={styles.timeline}
        onScroll={(event) => {
          const element = event.currentTarget;
          setFollowing(element.scrollHeight - element.scrollTop - element.clientHeight < 80);
        }}
      >
        <div className={styles.readingColumn}>
          {model.stage === "ready_to_start" && <section className={styles.startTask}><BookOpen size={26} /><h2>从这里开始连续创作</h2><p>这是唯一一次显式启动。之后故事弧批准、章节生成和内部步数分段都会自动衔接。</p><Button variant="primary" size="lg" disabled={busy} onClick={() => void onStart()}><Play size={17} />{busy ? "正在启动…" : "开始创作"}</Button></section>}

          {bookRevision && <section className={styles.bookRevisionTask}><span>全书契约修订</span><h2>批准未来方向 v{bookRevision.base_book_version} → v{bookRevision.target_book_version}</h2><p>{bookRevision.summary}</p><dl><div><dt>冲突字段</dt><dd>{bookRevision.contract_field}</dd></div><div><dt>修订原因</dt><dd>{bookRevision.impossibility_reason}</dd></div></dl><div><Button variant="ghost" onClick={() => onSelectArtifact(bookRevision.candidate.direction_path)}>查看候选方向</Button><Button variant="primary" size="lg" disabled={busy} onClick={() => void onApproveBookRevision()}><ShieldCheck size={16} />批准并继续未来创作</Button></div></section>}

          {model.stage === "story_arc_review" && currentArc && <StoryArcReviewTask arc={currentArc} plan={arcPlanQuery.data?.content ?? ""} loading={arcPlanQuery.isLoading} busy={busy} onApprove={onApproveArc} onOpenPlan={() => onSelectArtifact(currentArc.plan_path)} />}

          {authorEvents.map((event) => {
            const text = feedbackText(event);
            if (event.kind === "user_feedback" && text) return <article className={styles.message} data-role="user" key={event.event_id}><span>你</span><p>{text}</p></article>;
            if (event.kind === "feedback_processed" && text) return <article className={styles.message} data-role="agent" key={event.event_id}><span>NovelPilot</span><p>已接收并路由这条反馈：{text}</p></article>;
            if (event.status === "failed") return <article className={styles.failureNotice} key={event.event_id}><strong>当前步骤未能继续</strong><p>{failureSummary(event)}</p><div className={styles.failureActions}><Button variant="ghost" size="sm" onClick={() => setDetailsOpen(true)}>查看详细证据</Button>{model.primaryAction === "retry_failed_run" && <Button variant="primary" size="sm" disabled={busy} onClick={() => void onRetryFailedRun()}><RotateCw size={15} />{busy ? "正在重试……" : readiness?.next_action.id === "retry_provider_connection" ? "重新连接并继续" : "重试当前步骤"}</Button>}</div></article>;
            return null;
          })}

          {activeChapterId && (prose || model.stage === "writing_chapter" || model.stage === "evaluating_chapter" || model.stage === "repairing_chapter") && <LiveChapterDocument chapterId={activeChapterId} prose={prose} status={proseStatus} stageLabel={model.title} />}

          {completedChapterIds.filter((chapterId) => chapterId !== activeChapterId).map((chapterId) => <button type="button" className={styles.completedChapter} key={chapterId} onClick={() => onSelectArtifact(`chapters/${chapterId}/final.md`)}><span><BookOpen size={15} /></span><div><strong>{chapterId}</strong><small>正文已提交，点击只读查看</small></div></button>)}

          {model.stage === "chapter_recovery" && <section className={styles.recoveryTask}><h2>正文已保留，继续自动修订证据</h2><p>不会要求你编辑补丁或重写章节；Harness 会继续有限次语义修订。</p><div><Button variant="ghost" onClick={() => setDetailsOpen(true)}>查看失败详情</Button><Button variant="primary" disabled={busy} onClick={() => void onRetryChapter()}><RotateCw size={16} />继续自动修订</Button></div></section>}
          {model.primaryAction === "resume" && <section className={styles.recoveryTask}><h2>连续创作当前已暂停</h2><p>现在没有模型生成或后台创作任务。恢复后将从最近的一致检查点继续。</p><Button variant="primary" disabled={busy} onClick={() => void onResume()}><Play size={16} />{busy ? "正在恢复……" : "恢复连续创作"}</Button></section>}
          {model.primaryAction === "recover_stale" && <section className={styles.recoveryTask}><h2>异常运行恢复</h2><p>只有确认后台请求已经结束时才执行恢复。</p><Button variant="primary" disabled={busy} onClick={() => void onRecoverStale()}>恢复异常状态</Button></section>}
        </div>
      </div>
      {!following && <Button className={styles.jumpLatest} variant="secondary" onClick={() => { setFollowing(true); scrollToBottom(scrollRef.current); }}><ArrowDown size={14} />跳到最新</Button>}
      <div className={styles.composerWrap}><CreationComposer value={feedback} sending={sendingFeedback || busy && model.stage === "story_arc_review"} mode={model.stage === "story_arc_review" ? "arc_revision" : "feedback"} onChange={onFeedbackChange} onSend={() => void submitComposer()} /></div>
      <CreationDetailsSheet open={detailsOpen} events={events} summaries={summaries} readiness={readiness} onOpenChange={setDetailsOpen} onSelectArtifact={onSelectArtifact} />
    </section>
  );
}
