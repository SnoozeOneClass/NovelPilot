import { AlertTriangle, Check, History, Send, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Dialog } from "../../components/ui/Dialog";
import type { BookDirectionCandidate, SetupStateDocument } from "../../types/domain";
import type { BusyAction, Notice, TitleChoice } from "./setup-types";
import styles from "./SetupConversation.module.css";

interface SetupReviewProps {
  state: SetupStateDocument;
  candidate: BookDirectionCandidate;
  input: string;
  titleChoice: TitleChoice;
  busyAction: BusyAction;
  notice: Notice | null;
  approvalAllowed: boolean;
  canSend: boolean;
  canApprove: boolean;
  onInputChange: (value: string) => void;
  onTitleChange: (choice: TitleChoice) => void;
  onUseSuggestion: (value: string) => void;
  onSend: () => void;
  onApprove: () => void;
  onExit: () => void;
}

export function SetupReview({ state, candidate, input, titleChoice, busyAction, notice, approvalAllowed, canSend, canApprove, onInputChange, onTitleChange, onUseSuggestion, onSend, onApprove, onExit }: SetupReviewProps) {
  const [historyOpen, setHistoryOpen] = useState(false);
  const passed = candidate.review.status === "passed" && approvalAllowed;
  const finalTitle = titleChoice?.title.trim() ?? "";

  return (
    <div className={styles.reviewStage}>
      <header className={styles.stageHeader}>
        <div><p>全书 Loop · 候选审阅</p><h1>{passed ? "确认方向与正式书名" : "处理候选中的阻断问题"}</h1></div>
        <button className={styles.iconTextButton} onClick={() => setHistoryOpen(true)}><History size={15} />讨论记录</button>
      </header>
      {notice && <div className={styles.notice} data-kind={notice.kind}>{notice.text}</div>}

      <section className={styles.reviewSummary} data-status={passed ? "passed" : "blocked"}>
        <span>{passed ? <Check size={18} /> : <AlertTriangle size={18} />}</span>
        <div><h2>{passed ? "候选方向通过语义审查" : "候选方向仍有阻断问题"}</h2><p>{candidate.review.summary}</p></div>
        <strong>v{candidate.revision}</strong>
      </section>

      <section className={styles.reviewSection}>
        <header><h2>候选全书方向</h2><span>{candidate.direction_markdown.length} 字符</span></header>
        <div className={styles.candidateDirection}>{candidate.direction_markdown}</div>
      </section>

      {candidate.review.issues.length > 0 && (
        <section className={styles.reviewSection}>
          <header><h2>审查问题</h2><span>{candidate.review.issues.length}</span></header>
          <div className={styles.issueList}>{candidate.review.issues.map((issue, index) => (
            <article key={`${issue.kind}-${index}`} data-severity={issue.severity}>
              <span>{issue.severity === "blocking" ? "阻断" : "提醒"}</span>
              <div><strong>{issue.message}</strong>{issue.evidence.length > 0 && <small>{issue.evidence.join(" · ")}</small>}{issue.suggested_question && <button onClick={() => onUseSuggestion(issue.suggested_question ?? "")}>用这个问题继续讨论</button>}</div>
            </article>
          ))}</div>
        </section>
      )}

      <section className={styles.reviewColumns}>
        <div className={styles.reviewSection}>
          <header><h2>已确认决定覆盖</h2><span>{candidate.confirmed_decision_coverage.length}</span></header>
          <ul className={styles.coverageList}>{candidate.confirmed_decision_coverage.map((item) => <li key={item.decision}><strong>{item.decision}</strong><span>{item.candidate_evidence}</span></li>)}</ul>
        </div>
        <div className={styles.reviewSection}>
          <header><h2>验证信号</h2><span>{candidate.review.signals.length}</span></header>
          <ul className={styles.signalList}>{candidate.review.signals.map((signal) => <li key={signal}>{formatBookReviewSignal(signal)}</li>)}</ul>
        </div>
      </section>

      <section className={styles.titlePicker}>
        <header><div><h2>确定正式书名</h2><p>书名与当前候选方向在同一事务中批准。</p></div><span>{finalTitle ? "已选择" : "批准前必填"}</span></header>
        <div className={styles.recommendedTitles}>{candidate.recommended_titles.map((option, index) => {
          const selected = titleChoice?.kind === "recommended" && titleChoice.title === option.title;
          return <button key={`${option.title}-${index}`} data-selected={selected} disabled={busyAction !== null} onClick={() => onTitleChange({ kind: "recommended", title: option.title })}><span>{index + 1}</span><div><strong>《{option.title}》</strong><small>{option.rationale}</small></div></button>;
        })}</div>
        <label className={styles.customTitle}><span>自定义书名</span><input value={titleChoice?.kind === "custom" ? titleChoice.title : ""} maxLength={200} disabled={busyAction !== null} placeholder="输入你希望采用的正式书名" onFocus={() => titleChoice?.kind !== "custom" && onTitleChange({ kind: "custom", title: "" })} onChange={(event) => onTitleChange({ kind: "custom", title: event.target.value })} /></label>
      </section>

      <section className={styles.reviewComposer}>
        <div><strong>还需要调整？</strong><span>发送新消息会废止当前候选，并回到讨论阶段。</span></div>
        <textarea value={input} disabled={busyAction !== null} placeholder="补充、纠正或否定当前候选..." onChange={(event) => onInputChange(event.target.value)} />
        <button title="继续讨论" disabled={!canSend} onClick={onSend}><Send size={16} /></button>
      </section>

      <footer className={styles.reviewFooter}>
        <button className={styles.secondaryButton} disabled={busyAction !== null} onClick={onExit}>退出共创</button>
        <span>{input.trim() ? "存在尚未发送的修改，批准已锁定。" : finalTitle ? `将采用《${finalTitle}》` : "请选择或输入正式书名。"}</span>
        <button className={styles.approveButton} disabled={!canApprove} onClick={onApprove}><ShieldCheck size={16} />{busyAction === "approve" ? "正在提交..." : `批准候选 v${candidate.revision}`}</button>
      </footer>

      <Dialog open={historyOpen} title="全书共创讨论记录" onClose={() => setHistoryOpen(false)}>
        <div className={styles.historyList}>{state.messages.map((message) => <article key={message.id} data-role={message.role}><header><strong>{message.role === "user" ? "你" : "NovelPilot"}</strong><span>第 {message.turn} 轮</span></header><p>{message.content}</p></article>)}</div>
      </Dialog>
    </div>
  );
}

function formatBookReviewSignal(signal: string): string {
  const countedSignals: Array<[RegExp, string]> = [[/^direction_characters:(\d+)$/, "全书方向字符数"], [/^rolling_contract_characters:(\d+)$/, "滚动规划契约字符数"], [/^constraint_items:(\d+)$/, "结构化约束项数"]];
  for (const [pattern, label] of countedSignals) { const match = signal.match(pattern); if (match) return `${label}：${match[1]}`; }
  const coverage = signal.match(/^confirmed_decision_coverage:(\d+)\/(\d+)$/);
  if (coverage) return `已确认决定覆盖：${coverage[1]}/${coverage[2]}`;
  const status = signal.match(/^([^:]+):(passed|failed|warning)$/);
  if (!status) return signal;
  const labels: Record<string, string> = { confirmed_decisions_preserved: "已确认决定保持一致", rolling_scope: "滚动规划范围" };
  const statuses: Record<string, string> = { passed: "通过", failed: "未通过", warning: "提醒" };
  return `${labels[status[1]] ?? status[1]}：${statuses[status[2]]}`;
}
