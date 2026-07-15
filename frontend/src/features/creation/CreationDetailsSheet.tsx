import { FileText } from "lucide-react";
import { Button } from "../../components/ui/Button";
import { Sheet } from "../../components/ui/Sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../components/ui/Tabs";
import type { ArtifactSummary, HarnessEvent, ProjectReadiness } from "../../types/domain";
import { formatEventKind, formatEventMessage, formatGateId, formatGateStatus } from "../../types/display";
import styles from "./CreationView.module.css";

interface CreationDetailsSheetProps {
  open: boolean;
  events: HarnessEvent[];
  summaries: ArtifactSummary[];
  readiness: ProjectReadiness | null;
  onOpenChange: (open: boolean) => void;
  onSelectArtifact: (path: string) => void;
}

export function CreationDetailsSheet({ open, events, summaries, readiness, onOpenChange, onSelectArtifact }: CreationDetailsSheetProps) {
  const diagnosticEvents = events.filter((event) => !["chapter_draft_delta", "user_feedback", "feedback_processed"].includes(event.kind)).slice(-80).reverse();
  return (
    <Sheet open={open} title="创作详情" description="运行事件、验证门禁与产物仅在这里展示。" onOpenChange={onOpenChange}>
      <Tabs defaultValue="status" className={styles.detailsTabs}>
        <TabsList className={styles.detailsTabList}>
          <TabsTrigger value="status">状态</TabsTrigger>
          <TabsTrigger value="events">事件</TabsTrigger>
          <TabsTrigger value="artifacts">产物</TabsTrigger>
        </TabsList>
        <TabsContent value="status" className={styles.detailsPanel}>
          <div className={styles.detailList}>
            {readiness?.gates.map((gate) => <article key={gate.id}><div><strong>{formatGateId(gate.id)}</strong><span>{formatGateStatus(gate.status)}</span></div><p>{gate.message}</p></article>)}
            {!readiness?.gates.length && <p className={styles.emptyDetail}>暂无门禁信息。</p>}
          </div>
        </TabsContent>
        <TabsContent value="events" className={styles.detailsPanel}>
          <div className={styles.detailList}>
            {diagnosticEvents.map((event) => <article key={event.event_id}><div><strong>{formatEventKind(event.kind)}</strong><span>{event.status}</span></div><p>{formatEventMessage(event.message)}</p>{event.artifact_path && <Button size="sm" variant="ghost" onClick={() => onSelectArtifact(event.artifact_path!)}><FileText size={13} />查看证据</Button>}</article>)}
            {!diagnosticEvents.length && <p className={styles.emptyDetail}>暂无运行事件。</p>}
          </div>
        </TabsContent>
        <TabsContent value="artifacts" className={styles.detailsPanel}>
          <div className={styles.detailList}>
            {summaries.slice().reverse().map((summary) => <button type="button" key={summary.path} onClick={() => onSelectArtifact(summary.path)}><strong>{summary.title}</strong><span>{summary.status}</span><small>{summary.path}</small></button>)}
            {!summaries.length && <p className={styles.emptyDetail}>暂无产物。</p>}
          </div>
        </TabsContent>
      </Tabs>
    </Sheet>
  );
}
