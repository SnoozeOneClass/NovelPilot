import { AlertTriangle, Check, ChevronDown, ChevronUp, RotateCcw, Send, ShieldCheck } from "lucide-react";
import { useRef, useState } from "react";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { Separator } from "../../components/ui/Separator";
import { Textarea } from "../../components/ui/Textarea";
import type { BookDirectionCandidate, SetupMessage } from "../../types/domain";
import { ConversationTranscript } from "./ConversationTranscript";
import { formatBookTitle } from "./setup-formatters";
import type { BusyAction, Notice } from "./setup-types";
import styles from "./SetupReview.module.css";

interface SetupReviewProps {
  messages: SetupMessage[];
  candidate: BookDirectionCandidate;
  selectedTitle: string;
  input: string;
  busyAction: BusyAction;
  notice: Notice | null;
  approvalAllowed: boolean;
  canSend: boolean;
  canApprove: boolean;
  onInputChange: (value: string) => void;
  onSend: () => void;
  onApprove: () => void;
  onContinueRevision: () => void;
}

export function SetupReview({
  messages,
  candidate,
  selectedTitle,
  input,
  busyAction,
  notice,
  approvalAllowed,
  canSend,
  canApprove,
  onInputChange,
  onSend,
  onApprove,
  onContinueRevision
}: SetupReviewProps) {
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const [feedbackExpanded, setFeedbackExpanded] = useState(Boolean(input.trim()));
  const passed = candidate.review.status === "passed" && approvalAllowed;
  const finalTitle = selectedTitle.trim();

  return (
    <section className={styles.reviewView} aria-labelledby="review-panel-title">
      <div className={styles.reviewScroll}>
        <div className={styles.reviewColumn}>
          <header className={styles.reviewHeader}>
            <div>
              <span>Candidate Review</span>
              <h1 id="review-panel-title">{passed ? "确认全书方向" : "处理阻断问题"}</h1>
            </div>
            <Badge tone={passed ? "success" : "danger"}>候选 v{candidate.revision}</Badge>
          </header>

          <ConversationTranscript messages={messages} />

          {notice && <div className={styles.notice} data-kind={notice.kind}>{notice.text}</div>}

          <section className={styles.reviewSummary} data-status={passed ? "passed" : "blocked"}>
            <span>{passed ? <Check size={19} /> : <AlertTriangle size={19} />}</span>
            <div>
              <h2>{passed ? "候选方向通过语义审查" : "候选方向仍有阻断问题"}</h2>
              <p>{candidate.review.summary}</p>
            </div>
          </section>

          {candidate.review.issues.length > 0 && (
            <section className={styles.issueSection} aria-labelledby="review-issues-title">
              <header>
                <div><span>需要处理</span><h2 id="review-issues-title">审查问题</h2></div>
                <Badge tone="danger">{candidate.review.issues.length}</Badge>
              </header>
              <div className={styles.issueList}>
                {candidate.review.issues.map((issue, index) => (
                  <article key={`${issue.kind}-${index}`} data-severity={issue.severity}>
                    <Badge tone={issue.severity === "blocking" ? "danger" : "neutral"}>
                      {issue.severity === "blocking" ? "阻断" : "提醒"}
                    </Badge>
                    <div>
                      <strong>{issue.message}</strong>
                      {issue.evidence.length > 0 && <small>{issue.evidence.join(" · ")}</small>}
                    </div>
                  </article>
                ))}
              </div>
            </section>
          )}

          <Separator />

          {passed && <section className={styles.titlePicker} aria-labelledby="title-picker-title">
            <header>
              <div>
                <span>Approval task</span>
                <h2 id="title-picker-title">批准已确认的全书方向</h2>
                <p>正式书名已经在逐问讨论的最后一步由你确认。</p>
              </div>
              <Badge tone={finalTitle ? "success" : "danger"}>{finalTitle ? "书名已确认" : "缺少书名"}</Badge>
            </header>
            <div className={styles.confirmedTitle} role="status">
              <span><Check size={18} /></span>
              <div>
                <strong>{finalTitle ? formatBookTitle(finalTitle) : "正式书名尚未确认"}</strong>
                <small>{finalTitle ? "批准只提交整套方向，不会再次修改书名。" : "请返回讨论阶段完成最后一个书名问题。"}</small>
              </div>
            </div>
          </section>}
        </div>
      </div>

      {passed ? <div className={styles.composerShell}>
        <div className={styles.composerInner}>
          <Button
            aria-controls="book-review-feedback"
            aria-expanded={feedbackExpanded}
            className={styles.feedbackToggle}
            variant="ghost"
            onClick={() => setFeedbackExpanded((expanded) => !expanded)}
          >
            <span>对当前方向不满意？继续修改</span>
            {feedbackExpanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
          </Button>
          {feedbackExpanded && (
            <div id="book-review-feedback" className={styles.feedbackPanel}>
              <label htmlFor="book-review-composer">对当前候选方向的修改意见，发送后返回讨论阶段。</label>
              <div className={styles.composerRow}>
                <Textarea
                  ref={inputRef}
                  id="book-review-composer"
                  value={input}
                  disabled={busyAction !== null}
                  placeholder="补充、纠正或否定当前候选；发送后将返回讨论阶段……"
                  onChange={(event) => onInputChange(event.target.value)}
                />
                <Button variant="secondary" disabled={!canSend} onClick={onSend}>
                  <Send size={16} />
                  发送修改意见
                </Button>
              </div>
            </div>
          )}
          <div className={styles.reviewFooter}>
            <span>
              {input.trim()
                ? "存在尚未发送的修改，批准已锁定。"
                : finalTitle
                  ? `正式书名为${formatBookTitle(finalTitle)}，点击右侧按钮后才会正式批准。`
                  : "正式书名尚未确认，不能批准。"}
            </span>
            <Button variant="primary" disabled={!canApprove} onClick={onApprove}>
              <ShieldCheck size={16} />
              {busyAction === "approve"
                ? "正在提交..."
                : finalTitle
                  ? `批准并采用${formatBookTitle(finalTitle)}`
                  : "缺少已确认书名"}
            </Button>
          </div>
        </div>
      </div> : <div className={styles.composerShell}>
        <div className={styles.composerInner}>
          <div className={styles.revisionStopped} role="status">
            <div>
              <strong>本轮自动修订已停止</strong>
              <span>候选仍未通过审查。查看上方证据后，可由你明确开启一轮新的有限修订。</span>
            </div>
            <Button variant="primary" disabled={busyAction !== null} onClick={onContinueRevision}>
              <RotateCcw size={16} />
              {busyAction === "review" ? "正在继续修订..." : "继续修订"}
            </Button>
          </div>
        </div>
      </div>}
    </section>
  );
}

export function SetupReviewDetails({ candidate }: { candidate: BookDirectionCandidate }) {
  return (
    <div className={styles.reviewDetails}>
      <section>
        <header><h2>已确认决定覆盖</h2><Badge>{candidate.confirmed_decision_coverage.length}</Badge></header>
        <ul className={styles.coverageList}>
          {candidate.confirmed_decision_coverage.map((item) => (
            <li key={item.decision}><strong>{item.decision}</strong><span>{item.candidate_evidence}</span></li>
          ))}
        </ul>
      </section>

      <Separator />

      <section>
        <header><h2>验证信号</h2><Badge>{candidate.review.signals.length}</Badge></header>
        {candidate.review.signals.length > 0
          ? <ul className={styles.signalList}>{candidate.review.signals.map((signal) => <li key={signal}>{formatBookReviewSignal(signal)}</li>)}</ul>
          : <p className={styles.emptyDetails}>当前没有附加验证信号。</p>}
      </section>

      <Separator />

      <section>
        <header><h2>滚动故事弧契约</h2><Badge tone="accent">批准后生效</Badge></header>
        <pre className={styles.reviewContract}>{candidate.rolling_plan_markdown}</pre>
        <div className={styles.constraintGroups}>
          {constraintSections.map(({ key, label }) => candidate.constraints[key].length > 0 ? (
            <div key={key}>
              <strong>{label}</strong>
              <ul>{candidate.constraints[key].map((item) => <li key={item}>{item}</li>)}</ul>
            </div>
          ) : null)}
        </div>
      </section>
    </div>
  );
}

const constraintSections: Array<{
  key: keyof BookDirectionCandidate["constraints"];
  label: string;
}> = [
  { key: "confirmed", label: "已确认决定" },
  { key: "must_preserve", label: "必须维护" },
  { key: "must_avoid", label: "必须避免" },
  { key: "creative_freedoms", label: "创作自由" },
  { key: "open_decisions", label: "仍待决定" }
];

function formatBookReviewSignal(signal: string): string {
  const countedSignals: Array<[RegExp, string]> = [
    [/^direction_characters:(\d+)$/, "全书方向字符数"],
    [/^rolling_contract_characters:(\d+)$/, "滚动规划契约字符数"],
    [/^constraint_items:(\d+)$/, "结构化约束项数"]
  ];
  for (const [pattern, label] of countedSignals) {
    const match = signal.match(pattern);
    if (match) return `${label}：${match[1]}`;
  }
  const coverage = signal.match(/^confirmed_decision_coverage:(\d+)\/(\d+)$/);
  if (coverage) return `已确认决定覆盖：${coverage[1]}/${coverage[2]}`;
  const status = signal.match(/^([^:]+):(passed|failed|warning)$/);
  if (!status) return signal;
  const labels: Record<string, string> = {
    confirmed_decisions_preserved: "已确认决定保持一致",
    rolling_scope: "滚动规划范围"
  };
  const statuses: Record<string, string> = { passed: "通过", failed: "未通过", warning: "提醒" };
  return `${labels[status[1]] ?? status[1]}：${statuses[status[2]]}`;
}
