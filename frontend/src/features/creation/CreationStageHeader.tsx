import { Activity, PanelRightOpen } from "lucide-react";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import type { CreationViewModel } from "./creation-view-model";
import styles from "./CreationView.module.css";

interface CreationStageHeaderProps {
  model: CreationViewModel;
  detailCount: number;
  onOpenDetails: () => void;
}

export function CreationStageHeader({ model, detailCount, onOpenDetails }: CreationStageHeaderProps) {
  return (
    <header className={styles.stageHeader}>
      <div className={styles.stageCopy}>
        <div><Activity size={14} /><span>{model.eyebrow}</span></div>
        <h1>{model.title}</h1>
        <p>{model.description}</p>
      </div>
      <div className={styles.stageTools}>
        <Badge tone={model.stage === "failed" || model.stage === "chapter_recovery" ? "danger" : model.isRunning ? "accent" : "neutral"}>
          {model.isRunning ? "运行中" : model.primaryAction ? "需要处理" : "已同步"}
        </Badge>
        <Button variant="ghost" onClick={onOpenDetails}>
          <PanelRightOpen size={15} />查看详情{detailCount > 0 ? ` ${detailCount}` : ""}
        </Button>
      </div>
    </header>
  );
}
