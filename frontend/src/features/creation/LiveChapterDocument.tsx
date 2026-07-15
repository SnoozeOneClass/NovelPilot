import { AlertTriangle, CheckCircle2, FileText } from "lucide-react";
import { Badge } from "../../components/ui/Badge";
import type { ChapterDraftStatus } from "./chapter-draft-stream";
import styles from "./CreationView.module.css";

interface LiveChapterDocumentProps {
  chapterId: string;
  prose: string;
  status: ChapterDraftStatus | "committed";
  stageLabel: string;
}

export function LiveChapterDocument({ chapterId, prose, status, stageLabel }: LiveChapterDocumentProps) {
  const tone = status === "committed" ? "success" : status === "discarded" ? "danger" : "accent";
  const label = status === "committed"
    ? "已提交正文"
    : status === "discarded"
      ? "未提交片段"
      : status === "candidate"
        ? "候选正文"
        : "实时草稿";
  return (
    <article className={styles.liveDocument} aria-live="polite">
      <header>
        <div>
          {status === "discarded" ? <AlertTriangle size={17} /> : status === "committed" ? <CheckCircle2 size={17} /> : <FileText size={17} />}
          <div><span>{chapterId}</span><strong>{stageLabel}</strong></div>
        </div>
        <Badge tone={tone}>{label}</Badge>
      </header>
      <div className={styles.prose}>{prose || "正文开始生成后会在这里逐步出现。"}</div>
      <footer>{status === "committed" ? "此版本已进入正式章节记录。" : "当前内容只读；通过下方反馈告诉 Harness 后续需要调整的方向。"}</footer>
    </article>
  );
}
