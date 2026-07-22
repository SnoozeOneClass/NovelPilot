import { FileCheck2, PencilLine, Send, Sparkles } from "lucide-react";
import { useRef } from "react";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { IconButton } from "../../components/ui/IconButton";
import { OptionCard } from "../../components/ui/OptionCard";
import { Textarea } from "../../components/ui/Textarea";
import type { SetupStateDocument, SetupSuggestion } from "../../types/domain";
import { ConversationTranscript } from "./ConversationTranscript";
import type { BusyAction, Notice } from "./setup-types";
import styles from "./SetupDiscussion.module.css";

interface SetupDiscussionProps {
  state: SetupStateDocument;
  input: string;
  busyAction: BusyAction;
  notice: Notice | null;
  canSend: boolean;
  canReview: boolean;
  onInputChange: (value: string) => void;
  onUseSuggestion: (suggestion: SetupSuggestion) => void;
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

  function populateComposer(suggestion: SetupSuggestion) {
    onUseSuggestion(suggestion);
    requestAnimationFrame(() => inputRef.current?.focus());
  }

  return (
    <section className={styles.decisionView} aria-label="全书方向对话">
      <div className={styles.conversationScroll}>
        <div className={styles.conversationColumn}>
          <header className={styles.conversationHeader}>
            <div>
              <span>Book Loop</span>
              <h1>全书共创</h1>
            </div>
            <Badge tone={state.readiness.status === "ready" ? "success" : "accent"}>
              <Sparkles size={13} />{state.readiness.status === "ready" ? "可审阅" : `第 ${state.turn_count + 1} 轮`}
            </Badge>
          </header>

          <ConversationTranscript messages={state.messages} />

          {notice && <div className={styles.notice} data-kind={notice.kind}>{notice.text}</div>}

          {question ? (
            <section className={styles.questionCard} aria-labelledby="current-decision-title">
              <Badge tone="accent">当前最高优先级决定</Badge>
              <h2 id="current-decision-title">{question}</h2>
              <p>选择一个建议会填入下方输入框，你仍可修改后再发送。</p>

              {state.suggestions.length > 0 && busyAction === null && (
                <div className={styles.answerOptions} role="group" aria-label="同一决策的候选回答">
                  {state.suggestions.map((suggestion, index) => (
                    <OptionCard
                      key={suggestion.id}
                      marker={String.fromCharCode(65 + index)}
                      title={suggestion.label}
                      description={suggestion.rationale}
                      detail={suggestion.message}
                      selected={input === suggestion.message}
                      recommended={suggestion.recommended === true}
                      onClick={() => populateComposer(suggestion)}
                    />
                  ))}
                  <OptionCard
                    marker={<PencilLine size={15} />}
                    title="自己输入"
                    description="直接提出你的决定、异议或补充条件。"
                    onClick={() => {
                      onInputChange("");
                      inputRef.current?.focus();
                    }}
                  />
                </div>
              )}
            </section>
          ) : (
            <section className={styles.startPrompt}>
              <Badge tone="accent">开始规划</Badge>
              <h2>你希望这本书最终带给读者怎样的体验？</h2>
              <p>可以从模糊想法开始，也可以直接纠正当前方向。系统不会强制固定题目顺序。</p>
            </section>
          )}
        </div>
      </div>

      <div className={styles.composerShell}>
        <div className={styles.composerInner}>
          <label htmlFor="book-direction-composer">你的意见</label>
          <div className={styles.composerRow}>
            <Textarea
              id="book-direction-composer"
              ref={inputRef}
              value={input}
              disabled={busyAction !== null}
              placeholder={question ? "选择建议后可以继续编辑，或直接输入自己的回答..." : "描述、纠正、否定或提出新的方向..."}
              onChange={(event) => onInputChange(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && event.ctrlKey) {
                  event.preventDefault();
                  onSend();
                }
              }}
            />
            <IconButton label="发送本轮讨论" variant="primary" disabled={!canSend} onClick={onSend}>
              <Send size={18} />
            </IconButton>
          </div>
          <div className={styles.composerMeta}>
            <span>Ctrl + Enter 发送；选项只会填入，不会自动提交。</span>
            <div>
              <span><strong>{state.readiness.reason}</strong> 就绪判断不会自动结束讨论。</span>
              <Button variant="primary" disabled={!canReview} onClick={onReview}><FileCheck2 size={16} />准备审阅</Button>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
