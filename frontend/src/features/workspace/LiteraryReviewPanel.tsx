import { ShieldCheck } from "lucide-react";
import { useState } from "react";
import { api, formatApiError } from "../../api/client";
import { formatGateMessage, formatLiteraryDecision } from "../../types/display";
import type {
  LiteraryReviewDecision,
  ProjectCompletionAudit
} from "../../types/domain";
import styles from "./LiteraryReviewPanel.module.css";

interface LiteraryReviewPanelProps {
  completionAudit: ProjectCompletionAudit | null;
  onRecorded: () => Promise<void>;
}

export function LiteraryReviewPanel({ completionAudit, onRecorded }: LiteraryReviewPanelProps) {
  const [decision, setDecision] = useState<LiteraryReviewDecision>("approved");
  const [reviewer, setReviewer] = useState("人工审查");
  const [chapterAssessment, setChapterAssessment] = useState("");
  const [statePatchAssessment, setStatePatchAssessment] = useState("");
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  const smokeGate = completionAudit?.gates.find((gate) => gate.id === "live_provider_smoke") ?? null;
  const smokePassed = smokeGate?.status === "passed";
  const complete = Boolean(reviewer.trim() && chapterAssessment.trim() && statePatchAssessment.trim());

  async function recordReview() {
    if (!smokePassed || !complete || saving) return;
    setSaving(true);
    setNotice(null);
    try {
      const result = await api.recordLiteraryReview({
        decision,
        reviewer: reviewer.trim(),
        chapter_assessment: chapterAssessment.trim(),
        state_patch_assessment: statePatchAssessment.trim(),
        notes: notes.trim()
      });
      setNotice({ kind: "success", text: `文学审查已记录：${result.literary_review_json}` });
      await onRecorded();
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className={styles.panel}>
      <header><ShieldCheck size={18} /><div><h2>人工文学审查</h2><p>真实模型冒烟通过后，记录章节正文与状态补丁的人工判断。</p></div></header>
      {!smokePassed && (
        <p className={styles.error}>
          {formatGateMessage(smokeGate?.message ?? "Live provider smoke has not passed.")}
        </p>
      )}
      <div className={styles.form}>
        <select value={decision} disabled={saving} onChange={(event) => setDecision(event.target.value as LiteraryReviewDecision)}>
          <option value="approved">{formatLiteraryDecision("approved")}</option>
          <option value="rejected">{formatLiteraryDecision("rejected")}</option>
        </select>
        <input value={reviewer} disabled={saving} onChange={(event) => setReviewer(event.target.value)} placeholder="审查人" />
        <textarea value={chapterAssessment} disabled={saving} onChange={(event) => setChapterAssessment(event.target.value)} placeholder="章节正文评价" />
        <textarea value={statePatchAssessment} disabled={saving} onChange={(event) => setStatePatchAssessment(event.target.value)} placeholder="状态补丁评价" />
        <textarea value={notes} disabled={saving} onChange={(event) => setNotes(event.target.value)} placeholder="补充记录（可选）" />
        <button className={styles.primaryButton} disabled={!smokePassed || !complete || saving} onClick={() => void recordReview()}>
          <ShieldCheck size={16} /> {saving ? "正在记录..." : "记录审查"}
        </button>
      </div>
      {notice && <p className={notice.kind === "error" ? styles.error : styles.success}>{notice.text}</p>}
    </section>
  );
}
