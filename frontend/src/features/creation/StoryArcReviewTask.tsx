import { Check, FileText } from "lucide-react";
import { useEffect, useState } from "react";
import { Button } from "../../components/ui/Button";
import type { CurrentArcState } from "../../types/domain";
import styles from "./CreationView.module.css";

interface StoryArcReviewTaskProps {
  arc: CurrentArcState;
  plan: string;
  loading: boolean;
  busy: boolean;
  onApprove: (targetChapterCount: number) => Promise<boolean>;
  onOpenPlan: () => void;
}

export function StoryArcReviewTask({ arc, plan, loading, busy, onApprove, onOpenPlan }: StoryArcReviewTaskProps) {
  const [targetCount, setTargetCount] = useState(arc.target_chapter_count);
  useEffect(() => setTargetCount(arc.target_chapter_count), [arc.arc_id, arc.target_chapter_count]);
  const valid = Number.isInteger(targetCount) && targetCount >= 1 && targetCount <= 30;
  return (
    <section className={styles.reviewTask}>
      <header><span>当前主任务</span><h2>审阅并批准 {arc.arc_id}</h2><p>批准后会自动进入章节生成，不需要返回其他页面点击继续。</p></header>
      <div className={styles.arcPlan}>{loading ? "正在读取故事弧计划…" : plan || "计划产物尚未就绪。"}</div>
      <footer>
        <label><span>计划章节数</span><input type="number" min={1} max={30} value={targetCount} disabled={busy} onChange={(event) => setTargetCount(Number(event.target.value))} /><small>Agent 建议 {arc.recommended_target_chapter_count} 章</small></label>
        <div>
          <Button variant="ghost" onClick={onOpenPlan}><FileText size={15} />查看原始文稿</Button>
          <Button variant="primary" size="lg" disabled={busy || !valid} onClick={() => void onApprove(targetCount)}><Check size={16} />{busy ? "正在批准…" : "批准计划并开始章节创作"}</Button>
        </div>
      </footer>
    </section>
  );
}
