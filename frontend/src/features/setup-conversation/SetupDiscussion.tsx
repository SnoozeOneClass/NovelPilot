import { FileCheck2, PencilLine, Send, Sparkles } from "lucide-react";
import { useRef } from "react";
import type { SetupStateDocument } from "../../types/domain";
import { latestSetupExchange } from "./setup-planning";
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
  onReview
}: SetupDiscussionProps) {
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const question = state.question;
  const latest = latestSetupExchange(state.messages);

  function populateComposer(value: string) {
    onUseSuggestion(value);
    requestAnimationFrame(() => inputRef.current?.focus());
  }

  return (
    <section className={styles.decisionView} aria-labelledby="current-decision-title">
      <header className={styles.contextHeader}>
        <div><span>Current Decision</span><h2 id="current-decision-title">当前决策</h2></div>
        <strong data-tone={state.readiness.status === "ready" ? "success" : "accent"}><Sparkles size={12} />{state.readiness.status === "ready" ? "可审阅" : `第 ${state.turn_count + 1} 轮`}</strong>
      </header>

      <div className={styles.decisionScroll}>
        {notice && <div className={styles.notice} data-kind={notice.kind}>{notice.text}</div>}

        {(latest.user || latest.assistant) && (
          <section className={styles.latestExchange} aria-label="最近一轮讨论摘要">
            <header><h3>最近一轮</h3><span>完整记录在“历史”中</span></header>
            {latest.user && <article data-role="user"><strong>你</strong><p>{latest.user.content}</p></article>}
            {latest.assistant && <article data-role="assistant"><strong>NovelPilot</strong><p>{latest.assistant.content}</p></article>}
          </section>
        )}

        <section className={styles.activeQuestion} data-empty={!question}>
          <span>{question ? "模型判断的最高影响缺口" : "开始规划"}</span>
          <h2>{question ?? "你希望这本书最终带给读者怎样的体验？"}</h2>
          {!question && <p>可以从模糊想法开始，也可以直接纠正当前方向。系统不会强制固定题目顺序。</p>}
        </section>

        {question && state.suggestions.length > 0 && busyAction === null && (
          <div className={styles.answerOptions} role="group" aria-label="同一决策的候选回答">
            {state.suggestions.map((suggestion, index) => (
              <button
                key={suggestion.id}
                type="button"
                data-selected={input === suggestion.message}
                data-recommended={suggestion.recommended === true}
                onClick={() => populateComposer(suggestion.message)}
              >
                <span>{String.fromCharCode(65 + index)}</span>
                <div>
                  <header><strong>{suggestion.label}</strong>{suggestion.recommended && <em>推荐</em>}</header>
                  {suggestion.rationale && <p>{suggestion.rationale}</p>}
                  <small>{suggestion.message}</small>
                </div>
              </button>
            ))}
            <button
              type="button"
              onClick={() => {
                onInputChange("");
                inputRef.current?.focus();
              }}
            >
              <span><PencilLine size={14} /></span>
              <div><header><strong>自己输入</strong></header><p>直接提出你的决定、异议或补充条件。</p></div>
            </button>
          </div>
        )}
      </div>

      <div className={styles.composer}>
        <label htmlFor="book-direction-composer">你的意见</label>
        <textarea
          id="book-direction-composer"
          ref={inputRef}
          value={input}
          disabled={busyAction !== null}
          placeholder={question ? "选择建议后可以继续编辑，或直接输入自己的回答..." : "描述、纠正、否定或提出新的方向..."}
          onChange={(event) => onInputChange(event.target.value)}
          onKeyDown={(event) => { if (event.key === "Enter" && event.ctrlKey) { event.preventDefault(); onSend(); } }}
        />
        <button title="发送本轮讨论" aria-label="发送本轮讨论" disabled={!canSend} onClick={onSend}><Send size={17} /></button>
        <span>Ctrl + Enter 发送；选项只会填入，不会自动提交。</span>
      </div>

      <footer className={styles.decisionFooter}>
        <div><strong>{state.readiness.reason}</strong><span>就绪判断不会自动结束讨论。</span></div>
        <button className={styles.primaryButton} disabled={!canReview} onClick={onReview}><FileCheck2 size={15} />准备审阅</button>
      </footer>
    </section>
  );
}
