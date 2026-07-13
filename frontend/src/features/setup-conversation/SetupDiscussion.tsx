import { FileCheck2, MessageSquareText, Send, Sparkles } from "lucide-react";
import { useEffect, useRef } from "react";
import type { SetupStateDocument } from "../../types/domain";
import type { BusyAction, Notice } from "./setup-types";
import styles from "./SetupConversation.module.css";

interface SetupDiscussionProps {
  state: SetupStateDocument;
  input: string;
  busyAction: BusyAction;
  notice: Notice | null;
  canSend: boolean;
  canReview: boolean;
  onInputChange: (value: string) => void;
  onUseSuggestion: (value: string) => void;
  onSend: () => void;
  onReview: () => void;
  onExit: () => void;
}

export function SetupDiscussion({
  state,
  input,
  busyAction,
  notice,
  canSend,
  canReview,
  onInputChange,
  onUseSuggestion,
  onSend,
  onReview,
  onExit
}: SetupDiscussionProps) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => endRef.current?.scrollIntoView({ block: "end", behavior: "smooth" }), [state.messages.length, busyAction]);

  return (
    <div className={styles.discussionStage}>
      <header className={styles.stageHeader}>
        <div><p>全书 Loop · 深度共创</p><h1>讨论这本书真正要成为怎样的作品</h1></div>
        <div className={styles.stageStatus}>
          <span data-tone={state.readiness.status === "ready" ? "success" : "accent"}><Sparkles size={13} />{state.readiness.status === "ready" ? "可以整理候选" : "继续讨论"}</span>
          <span>{state.turn_count} 轮</span>
        </div>
      </header>

      {notice && <div className={styles.notice} data-kind={notice.kind}>{notice.text}</div>}

      <div className={styles.conversation} aria-live="polite">
        {state.messages.length === 0 && (
          <div className={styles.emptyConversation}>
            <MessageSquareText size={28} />
            <h2>从一个模糊想法开始也可以</h2>
            <p>写下题材、人物、读者体验，或者任何你不希望这本书变成的样子。</p>
          </div>
        )}
        {state.messages.map((message) => (
          <article key={message.id} className={styles.message} data-role={message.role}>
            <header><strong>{message.role === "user" ? "你" : "NovelPilot"}</strong><span>第 {message.turn} 轮{message.model_snapshot ? ` · ${message.model_snapshot}` : ""}{message.migrated ? " · 旧版迁移" : ""}</span></header>
            <p>{message.content}</p>
          </article>
        ))}
        {busyAction === "turn" && (
          <article className={styles.message} data-role="assistant" data-pending="true">
            <header><strong>NovelPilot</strong><span>正在更新方向草稿</span></header>
            <p>正在重新识别已确认决定、待定项、假设和矛盾...</p>
          </article>
        )}
        <div ref={endRef} />
      </div>

      {state.suggestions.length > 0 && busyAction === null && (
        <div className={styles.suggestions}>
          <span>参考方向</span>
          <div>{state.suggestions.map((suggestion) => <button key={suggestion.id} title={suggestion.message} onClick={() => onUseSuggestion(suggestion.message)}>{suggestion.label}</button>)}</div>
        </div>
      )}

      <div className={styles.composer}>
        <textarea
          value={input}
          maxLength={32000}
          disabled={busyAction !== null}
          placeholder="继续描述、纠正、否定或提出新的方向..."
          onChange={(event) => onInputChange(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter" && event.ctrlKey) { event.preventDefault(); onSend(); } }}
        />
        <button title="发送本轮讨论" disabled={!canSend} onClick={onSend}><Send size={17} /></button>
      </div>

      <footer className={styles.discussionFooter}>
        <div><strong>{state.readiness.reason}</strong><span>模型的就绪判断不会自动结束讨论。</span></div>
        <button className={styles.secondaryButton} disabled={busyAction !== null} onClick={onExit}>退出共创</button>
        <button className={styles.primaryButton} disabled={!canReview} onClick={onReview}><FileCheck2 size={16} />整理并审阅</button>
      </footer>
    </div>
  );
}
