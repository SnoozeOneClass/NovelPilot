import { Check, History, LogOut } from "lucide-react";
import type { RefObject } from "react";
import type { SetupStateDocument } from "../../types/domain";
import type { SetupPlanningStage } from "./setup-planning";
import styles from "./SetupConversation.module.css";

interface PlanningStageBarProps {
  state: SetupStateDocument;
  stage: SetupPlanningStage;
  historyTriggerRef: RefObject<HTMLButtonElement | null>;
  onOpenHistory: () => void;
  onExit: () => void;
}

const stages: Array<{ id: SetupPlanningStage; label: string }> = [
  { id: "exploration", label: "探索" },
  { id: "convergence", label: "收敛" },
  { id: "review", label: "审阅" },
  { id: "approved", label: "执行交接" }
];

export function PlanningStageBar({ state, stage, historyTriggerRef, onOpenHistory, onExit }: PlanningStageBarProps) {
  const currentIndex = stages.findIndex((item) => item.id === stage);
  return (
    <header className={styles.planningHeader}>
      <div className={styles.planningTitle}>
        <span>全书 Loop · Plan Workspace</span>
        <strong>{stageMessage(state, stage)}</strong>
      </div>
      <ol className={styles.stageProgress} aria-label="全书方向规划阶段">
        {stages.map((item, index) => (
          <li key={item.id} data-state={index < currentIndex ? "complete" : index === currentIndex ? "current" : "upcoming"} aria-current={index === currentIndex ? "step" : undefined}>
            <span>{index < currentIndex ? <Check size={11} /> : index + 1}</span>
            <strong>{item.label}</strong>
          </li>
        ))}
      </ol>
      <div className={styles.planningActions}>
        <button ref={historyTriggerRef} type="button" onClick={onOpenHistory}><History size={14} />历史</button>
        <button type="button" onClick={onExit}><LogOut size={14} />退出</button>
      </div>
    </header>
  );
}

function stageMessage(state: SetupStateDocument, stage: SetupPlanningStage): string {
  if (stage === "exploration") return "从创作意图开始，模型会维护一份连续方向文档";
  if (stage === "convergence") {
    return state.readiness.status === "ready" ? "方向已具备审阅条件，由你决定何时进入审阅" : "继续解决当前最高影响的一个决定";
  }
  if (stage === "review") {
    return state.candidate?.review.status === "passed" ? "候选已通过审查，等待书名与明确批准" : "候选仍有阻断问题，需要回到讨论修订";
  }
  return "方向已批准，可以进入创作执行";
}
