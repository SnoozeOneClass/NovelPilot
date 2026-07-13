import { Check, ChevronRight, Circle, FileText, MessageSquareText } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, formatApiError } from "../../api/client";
import { formatGenericStatus } from "../../types/display";
import type { ArtifactSummary, CurrentArcState } from "../../types/domain";
import { arcIdsFromArtifacts, parseMarkdownSections } from "./workspace-utils";
import styles from "./StoryArcsView.module.css";

interface StoryArcsViewProps {
  currentArc: CurrentArcState | null;
  activeChapterId: string | null;
  artifactPaths: string[];
  summaries: ArtifactSummary[];
  approving: boolean;
  onApprove: (targetChapterCount: number) => Promise<boolean>;
  onRequestRevision: (message: string) => Promise<boolean>;
  onSelectArtifact: (path: string) => void;
}

const sectionLabels: Record<string, string> = {
  "arc goal": "故事弧目标",
  goal: "故事弧目标",
  conflicts: "核心冲突",
  "core conflict": "核心冲突",
  "chapter direction": "章节方向",
  "pacing signal": "节奏信号",
  "foreshadowing movement": "伏笔推进",
  "stop conditions": "停止条件"
};

function sectionTitle(title: string): string {
  return sectionLabels[title.toLowerCase()] ?? title;
}

function displayArcNumber(arcId: string): string {
  const match = arcId.match(/(\d+)$/);
  return match ? String(Number(match[1])) : arcId;
}

function parseArcState(content: string, fallbackArcId: string): CurrentArcState | null {
  try {
    const payload: unknown = JSON.parse(content);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;

    const state = payload as Record<string, unknown>;
    const arcId = typeof state.arc_id === "string" && state.arc_id ? state.arc_id : fallbackArcId;
    const rawReview = state.human_review;
    const humanReview = rawReview === "awaiting_review" || rawReview === "approved" || rawReview === "not_required"
      ? rawReview
      : "not_required";
    const completedChapterIds = Array.isArray(state.completed_chapter_ids)
      ? state.completed_chapter_ids.filter((value): value is string => typeof value === "string")
      : [];
    const targetChapterCount = typeof state.target_chapter_count === "number" && state.target_chapter_count > 0
      ? Math.floor(state.target_chapter_count)
      : Math.max(completedChapterIds.length, 1);
    const recommendedTargetChapterCount = typeof state.recommended_target_chapter_count === "number" && state.recommended_target_chapter_count > 0
      ? Math.floor(state.recommended_target_chapter_count)
      : targetChapterCount;

    return {
      arc_id: arcId,
      status: typeof state.status === "string" ? state.status : "planned",
      plan_path: typeof state.plan_path === "string" ? state.plan_path : `arcs/${arcId}/plan.md`,
      human_review: humanReview,
      approved_at: typeof state.approved_at === "string" ? state.approved_at : null,
      recommended_target_chapter_count: recommendedTargetChapterCount,
      target_chapter_count: targetChapterCount,
      completed_chapter_ids: completedChapterIds,
      completed_at: typeof state.completed_at === "string" ? state.completed_at : null
    };
  } catch {
    return null;
  }
}

export function StoryArcsView({
  currentArc,
  activeChapterId,
  artifactPaths,
  summaries,
  approving,
  onApprove,
  onRequestRevision,
  onSelectArtifact
}: StoryArcsViewProps) {
  const arcIds = useMemo(() => {
    const ids = arcIdsFromArtifacts(artifactPaths);
    if (currentArc && !ids.includes(currentArc.arc_id)) ids.push(currentArc.arc_id);
    return ids;
  }, [artifactPaths, currentArc]);
  const [selectedArcId, setSelectedArcId] = useState(currentArc?.arc_id ?? arcIds[0] ?? "");
  const [planContent, setPlanContent] = useState("");
  const [loadingPlan, setLoadingPlan] = useState(false);
  const [selectedArcState, setSelectedArcState] = useState<CurrentArcState | null>(currentArc);
  const [loadingArcState, setLoadingArcState] = useState(false);
  const [showRevision, setShowRevision] = useState(false);
  const [revisionMessage, setRevisionMessage] = useState("");
  const [revisionNotice, setRevisionNotice] = useState<string | null>(null);
  const [submittingRevision, setSubmittingRevision] = useState(false);
  const [targetChapterCount, setTargetChapterCount] = useState(currentArc?.target_chapter_count ?? 3);

  useEffect(() => {
    if (currentArc?.arc_id) setSelectedArcId(currentArc.arc_id);
  }, [currentArc?.arc_id]);

  useEffect(() => {
    if (currentArc) setTargetChapterCount(currentArc.target_chapter_count);
  }, [currentArc]);

  useEffect(() => {
    const path = `arcs/${selectedArcId}/plan.md`;
    if (!selectedArcId || !artifactPaths.includes(path)) {
      setPlanContent("");
      return;
    }
    let cancelled = false;
    setLoadingPlan(true);
    api
      .artifactContent(path)
      .then((artifact) => {
        if (!cancelled) setPlanContent(artifact.content);
      })
      .catch(() => {
        if (!cancelled) setPlanContent("");
      })
      .finally(() => {
        if (!cancelled) setLoadingPlan(false);
      });
    return () => {
      cancelled = true;
    };
  }, [artifactPaths, selectedArcId]);

  useEffect(() => {
    if (!selectedArcId) {
      setSelectedArcState(null);
      setLoadingArcState(false);
      return;
    }
    if (currentArc?.arc_id === selectedArcId) {
      setSelectedArcState(currentArc);
      setLoadingArcState(false);
      return;
    }

    const path = `arcs/${selectedArcId}/state.json`;
    if (!artifactPaths.includes(path)) {
      setSelectedArcState(null);
      setLoadingArcState(false);
      return;
    }

    let cancelled = false;
    setSelectedArcState(null);
    setLoadingArcState(true);
    api
      .artifactContent(path)
      .then((artifact) => {
        if (!cancelled) setSelectedArcState(parseArcState(artifact.content, selectedArcId));
      })
      .catch(() => {
        if (!cancelled) setSelectedArcState(null);
      })
      .finally(() => {
        if (!cancelled) setLoadingArcState(false);
      });
    return () => {
      cancelled = true;
    };
  }, [artifactPaths, currentArc, selectedArcId]);

  const sections = parseMarkdownSections(planContent).slice(0, 6);
  const isCurrentArc = currentArc?.arc_id === selectedArcId;
  const awaitingReview = isCurrentArc && currentArc?.human_review === "awaiting_review";
  const validTargetChapterCount = Number.isInteger(targetChapterCount) && targetChapterCount >= 1 && targetChapterCount <= 30;
  const arcChapterIds = selectedArcState?.completed_chapter_ids ?? [];
  const chapterIds = [...arcChapterIds];
  if (isCurrentArc && activeChapterId && !chapterIds.includes(activeChapterId)) chapterIds.push(activeChapterId);
  const chapterSlots = selectedArcState
    ? Array.from(
        { length: Math.max(selectedArcState.target_chapter_count, chapterIds.length) },
        (_, index) => chapterIds[index] ?? null
      )
    : [];

  async function requestRevision() {
    if (!revisionMessage.trim() || submittingRevision) return;
    setSubmittingRevision(true);
    setRevisionNotice(null);
    try {
      const ok = await onRequestRevision(revisionMessage.trim());
      if (ok) {
        setRevisionMessage("");
        setShowRevision(false);
        setRevisionNotice("修改意见已进入安全检查点队列，harness 正在据此重写当前故事弧。 ");
      }
    } catch (error) {
      setRevisionNotice(formatApiError(error));
    } finally {
      setSubmittingRevision(false);
    }
  }

  return (
    <section className={styles.view}>
      <header className={styles.heading}>
        <div>
          <h1>故事弧与章节</h1>
          <p>章节只属于当前故事弧；后续故事弧会在提交状态之上滚动生成。</p>
        </div>
      </header>

      {awaitingReview && (
        <section className={styles.reviewBanner}>
          <div>
            <span className={styles.reviewBadge}><span /> 等待你的确认</span>
            <h2>第 {displayArcNumber(selectedArcId)} 故事弧计划 · 审批中</h2>
            <p>AI 已完成当前故事弧计划。该人工门禁必须批准后，才会开始章节创作。</p>
          </div>
          <div className={styles.reviewActions}>
            <label className={styles.chapterCountField}>
              <span>计划章节数</span>
              <input
                type="number"
                min={1}
                max={30}
                value={targetChapterCount}
                disabled={approving}
                onChange={(event) => setTargetChapterCount(Number(event.target.value))}
              />
              <small>Loop 建议 {currentArc?.recommended_target_chapter_count ?? targetChapterCount} 章</small>
            </label>
            <button className={styles.primaryButton} disabled={approving || !validTargetChapterCount} onClick={() => void onApprove(targetChapterCount)}>
              <Check size={17} /> {approving ? "正在批准..." : "批准并继续"}
            </button>
            <button className={styles.secondaryButton} onClick={() => setShowRevision((value) => !value)}>
              <MessageSquareText size={16} /> 要求修改
            </button>
            <button className={styles.quietButton} onClick={() => onSelectArtifact(`arcs/${selectedArcId}/plan.md`)}>
              <FileText size={15} /> 查看完整计划
            </button>
          </div>
        </section>
      )}

      {showRevision && (
        <div className={styles.revisionBox}>
          <textarea
            value={revisionMessage}
            disabled={submittingRevision}
            onChange={(event) => setRevisionMessage(event.target.value)}
            placeholder="说明希望调整的节奏、冲突、人物重点或章节方向..."
          />
          <button className={styles.primaryButton} disabled={!revisionMessage.trim() || submittingRevision} onClick={requestRevision}>
            提交修改意见
          </button>
        </div>
      )}
      {revisionNotice && <p className={styles.notice}>{revisionNotice}</p>}

      <div className={styles.columns}>
        <aside className={styles.listPanel}>
          <h2>故事弧列表</h2>
          <div className={styles.arcList}>
            {arcIds.map((arcId) => {
              const selected = selectedArcId === arcId;
              const status = arcId === currentArc?.arc_id
                ? currentArc.human_review === "awaiting_review" ? "审批中" : formatGenericStatus(currentArc.status)
                : "已归档";
              return (
                <button key={arcId} className={selected ? styles.selected : ""} onClick={() => setSelectedArcId(arcId)}>
                  <strong>第 {displayArcNumber(arcId)} 故事弧</strong>
                  <span>{arcId}</span>
                  <small>{status}</small>
                </button>
              );
            })}
            {arcIds.length === 0 && <p className={styles.empty}>还没有故事弧。</p>}
          </div>
        </aside>

        <section className={styles.planSummary}>
          <h2>故事弧计划摘要</h2>
          {loadingPlan ? (
            <p className={styles.empty}>正在读取计划...</p>
          ) : sections.length ? (
            <div className={styles.planSections}>
              {sections.map((section) => (
                <article key={`${section.title}-${section.body.slice(0, 12)}`}>
                  <h3>{sectionTitle(section.title)}</h3>
                  <p>{section.body}</p>
                </article>
              ))}
            </div>
          ) : (
            <div className={styles.empty}>
              <FileText size={24} />
              <p>这个故事弧还没有计划产物。</p>
            </div>
          )}
          <footer>
            <span>{planContent ? "计划已落盘，可追溯原始 Markdown。" : "等待故事弧 loop 生成计划。"}</span>
          </footer>
        </section>

        <aside className={styles.chapterList}>
          <h2>本弧章节</h2>
          <div>
            {loadingArcState && <p className={styles.empty}>正在读取故事弧状态...</p>}
            {!loadingArcState && chapterSlots.map((chapterId, index) => {
              const completed = chapterId ? arcChapterIds.includes(chapterId) : false;
              const active = isCurrentArc && chapterId === activeChapterId;
              const chapterSummary = chapterId
                ? summaries.find(
                    (summary) => summary.path.startsWith(`chapters/${chapterId}/`) && ["final", "draft"].includes(summary.kind)
                  )
                : undefined;
              return (
                <article key={chapterId ?? `pending-${selectedArcId}-${index}`} className={active ? styles.active : ""}>
                  <span>{completed ? <Check size={13} /> : <Circle size={13} />}</span>
                  <strong>第 {String(index + 1).padStart(2, "0")} 章</strong>
                  <p>{chapterSummary?.detail ?? chapterId ?? "待规划"}</p>
                  <small>{completed ? "已提交" : active ? "进行中" : "未开始"}</small>
                </article>
              );
            })}
            {!loadingArcState && chapterSlots.length === 0 && <p className={styles.empty}>故事弧批准后生成章节。</p>}
          </div>
          {isCurrentArc && currentArc && (
            <button className={styles.textLink} onClick={() => onSelectArtifact(currentArc.plan_path)}>
              查看计划原文 <ChevronRight size={15} />
            </button>
          )}
        </aside>
      </div>
    </section>
  );
}
