import { Check, ChevronLeft, ChevronRight, Sparkles } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, formatApiError } from "../../api/client";
import {
  composeSetupOptionAnswer,
  formatSetupOption,
  formatSetupQuestionPrompt,
  formatSetupQuestionTitle,
  formatSetupSource
} from "../../types/display";
import type { SetupQuestion, SetupStateDocument } from "../../types/domain";

interface SetupConversationProps {
  projectId: string;
  onSetupChanged?: (state: SetupStateDocument) => void;
  onExit?: () => void;
  onApproved?: () => void;
}

const CUSTOM_ANSWER = "__custom_answer__";

function splitAnswer(answer: string | undefined): { title: string; detail: string } {
  if (!answer) return { title: "待确认", detail: "尚未形成方向" };
  const separator = answer.indexOf(":");
  if (separator < 0) return { title: answer, detail: "用户自定义方向" };
  return {
    title: answer.slice(0, separator).trim(),
    detail: answer.slice(separator + 1).trim()
  };
}

export function SetupConversation({
  projectId,
  onSetupChanged,
  onExit,
  onApproved
}: SetupConversationProps) {
  const [state, setState] = useState<SetupStateDocument | null>(null);
  const [activeQuestionId, setActiveQuestionId] = useState<string | null>(null);
  const [selectedAnswer, setSelectedAnswer] = useState("");
  const [customAnswer, setCustomAnswer] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<{ kind: "error" | "success"; text: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    setNotice(null);
    setState(null);
    api
      .setupState()
      .then((nextState) => {
        if (cancelled) return;
        setState(nextState);
        setActiveQuestionId(nextState.next_question?.id ?? nextState.questions.at(-1)?.id ?? null);
      })
      .catch((error) => {
        if (!cancelled) setNotice({ kind: "error", text: formatApiError(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const answerMap = useMemo(
    () => new Map(state?.answers.map((answer) => [answer.question_id, answer.answer]) ?? []),
    [state?.answers]
  );
  const activeQuestion = useMemo(
    () => state?.questions.find((question) => question.id === activeQuestionId) ?? state?.next_question ?? null,
    [activeQuestionId, state]
  );
  const activeIndex = activeQuestion
    ? state?.questions.findIndex((question) => question.id === activeQuestion.id) ?? 0
    : state?.questions.length ?? 0;
  const answeredCount = state?.questions.filter((question) => answerMap.has(question.id)).length ?? 0;

  useEffect(() => {
    if (!activeQuestion) return;
    const existing = answerMap.get(activeQuestion.id) ?? "";
    const matchedOption = activeQuestion.options.find(
      (option) => composeSetupOptionAnswer(activeQuestion, option) === existing
    );
    if (matchedOption) {
      setSelectedAnswer(composeSetupOptionAnswer(activeQuestion, matchedOption));
      setCustomAnswer("");
    } else if (existing) {
      setSelectedAnswer(CUSTOM_ANSWER);
      setCustomAnswer(existing);
    } else {
      setSelectedAnswer("");
      setCustomAnswer("");
    }
  }, [activeQuestion, answerMap]);

  function chooseQuestion(question: SetupQuestion) {
    if (state?.approved) return;
    const questionIndex = state?.questions.findIndex((item) => item.id === question.id) ?? -1;
    if (questionIndex <= answeredCount) setActiveQuestionId(question.id);
  }

  async function saveCurrentAnswer() {
    if (!activeQuestion || saving) return;
    const answer = selectedAnswer === CUSTOM_ANSWER ? customAnswer.trim() : selectedAnswer;
    if (!answer) return;
    setSaving(true);
    setNotice(null);
    try {
      const nextState = await api.answerSetup(activeQuestion.id, answer);
      setState(nextState);
      setActiveQuestionId(nextState.next_question?.id ?? activeQuestion.id);
      onSetupChanged?.(nextState);
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setSaving(false);
    }
  }

  async function approve() {
    if (saving) return;
    setSaving(true);
    setNotice(null);
    try {
      const nextState = await api.approveSetup();
      setState(nextState);
      onSetupChanged?.(nextState);
      if (nextState.approved) {
        setNotice({ kind: "success", text: "全书方向已批准，可以启动三层写作流程。" });
        onApproved?.();
      }
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setSaving(false);
    }
  }

  function movePrevious() {
    if (!state || activeIndex <= 0) return;
    setActiveQuestionId(state.questions[activeIndex - 1].id);
  }

  if (!state) {
    return (
      <section className="plan-loading np-surface">
        <span className="status-dot active" />
        <p>{notice?.text ?? "正在读取全书规划状态..."}</p>
      </section>
    );
  }

  const canSubmit =
    selectedAnswer === CUSTOM_ANSWER ? Boolean(customAnswer.trim()) : Boolean(selectedAnswer);
  const showApproval = !state.approved && state.next_question === null && answeredCount === state.questions.length;

  return (
    <div className="plan-layout">
      <section className="np-surface plan-main">
        <header className="view-heading compact-heading">
          <div>
            <h1>开书规划 · Plan Mode</h1>
            <p>一次确认一个创作决策，直到 Book Direction 可以被批准。</p>
          </div>
        </header>

        <nav className="plan-stepper" aria-label="开书规划步骤">
          {state.questions.map((question, index) => {
            const answered = answerMap.has(question.id);
            const active = activeQuestion?.id === question.id && !showApproval;
            return (
              <button
                key={question.id}
                className={`${answered ? "done" : ""} ${active ? "active" : ""}`}
                disabled={state.approved || index > answeredCount}
                onClick={() => chooseQuestion(question)}
              >
                <span>{answered ? <Check size={13} strokeWidth={3} /> : index + 1}</span>
                <small>{formatSetupQuestionTitle(question)}</small>
              </button>
            );
          })}
          <button className={`${showApproval || state.approved ? "active" : ""}`} disabled={!showApproval}>
            <span>{state.approved ? <Check size={13} strokeWidth={3} /> : state.questions.length + 1}</span>
            <small>批准</small>
          </button>
        </nav>

        {notice && <p className={`notice-banner ${notice.kind}`}>{notice.text}</p>}

        {state.approved ? (
          <div className="plan-approved-state">
            <span className="approval-mark"><Check size={28} /></span>
            <h2>全书方向已经批准</h2>
            <p>后续只滚动规划当前故事弧，章节候选内容通过验证后才会进入正史。</p>
            <button className="gold-button" onClick={onApproved}>
              进入创作工作台 <ChevronRight size={17} />
            </button>
          </div>
        ) : showApproval ? (
          <div className="plan-approved-state ready">
            <span className="approval-mark"><Sparkles size={27} /></span>
            <h2>Book Direction 已准备好</h2>
            <p>确认右侧方向草稿后，批准全书 loop 开始滚动创作。</p>
            <button className="gold-button" disabled={saving} onClick={approve}>
              {saving ? "正在批准..." : "批准并开始"} <ChevronRight size={17} />
            </button>
          </div>
        ) : activeQuestion ? (
          <div className="plan-question-stage">
            <div className="question-heading-row">
              <div>
                <div className="title-with-badge">
                  <h2>{formatSetupQuestionTitle(activeQuestion)}</h2>
                  <span className="soft-badge green"><Sparkles size={13} /> AI 推荐</span>
                </div>
                <p>{formatSetupQuestionPrompt(activeQuestion)}</p>
              </div>
              <span className="source-badge">
                {formatSetupSource(activeQuestion.source)}
                {activeQuestion.model_snapshot ? ` · ${activeQuestion.model_snapshot}` : ""}
              </span>
            </div>

            <div className="plan-options">
              {activeQuestion.options.map((option, index) => {
                const answer = composeSetupOptionAnswer(activeQuestion, option);
                const display = formatSetupOption(activeQuestion, option);
                const selected = selectedAnswer === answer;
                return (
                  <button
                    key={option.id}
                    className={selected ? "selected" : ""}
                    disabled={saving}
                    onClick={() => setSelectedAnswer(answer)}
                  >
                    <span className="option-number">{index + 1}</span>
                    <span className="option-copy">
                      <strong>{display.label}</strong>
                      <small>{display.description}</small>
                    </span>
                    <span className="option-check">{selected && <Check size={14} strokeWidth={3} />}</span>
                  </button>
                );
              })}
              <button
                className={selectedAnswer === CUSTOM_ANSWER ? "selected" : ""}
                disabled={saving}
                onClick={() => setSelectedAnswer(CUSTOM_ANSWER)}
              >
                <span className="option-number">{activeQuestion.options.length + 1}</span>
                <span className="option-copy">
                  <strong>我自己描述</strong>
                  <small>不采用上面的方向，直接告诉 NovelPilot 你的想法。</small>
                </span>
                <span className="option-check">
                  {selectedAnswer === CUSTOM_ANSWER && <Check size={14} strokeWidth={3} />}
                </span>
              </button>
            </div>

            <textarea
              className="plan-custom-answer"
              value={customAnswer}
              disabled={saving}
              onFocus={() => setSelectedAnswer(CUSTOM_ANSWER)}
              onChange={(event) => {
                setCustomAnswer(event.target.value);
                setSelectedAnswer(CUSTOM_ANSWER);
              }}
              placeholder="补充你的设想，或者直接写出这一项的完整答案..."
            />

            <footer className="plan-question-actions">
              <button className="outline-button" disabled={activeIndex <= 0 || saving} onClick={movePrevious}>
                <ChevronLeft size={17} /> 上一步
              </button>
              <button className="gold-button" disabled={!canSubmit || saving} onClick={saveCurrentAnswer}>
                {saving ? "正在整理..." : "下一步"} <ChevronRight size={17} />
              </button>
            </footer>
          </div>
        ) : null}
      </section>

      <aside className="np-surface direction-draft">
        <header className="view-heading compact-heading">
          <div>
            <h2>Book Direction 草稿</h2>
            <p>随着问答实时同步整理。</p>
          </div>
        </header>
        <div className="direction-list">
          {state.questions.map((question) => {
            const answer = splitAnswer(answerMap.get(question.id));
            return (
              <article key={question.id} className={!answerMap.has(question.id) ? "pending" : ""}>
                <strong>{formatSetupQuestionTitle(question)}</strong>
                <span>{answer.title}</span>
                <small>{answer.detail}</small>
              </article>
            );
          })}
          <article>
            <strong>Rolling Strategy</strong>
            <span>当前故事弧滚动规划</span>
            <small>每个故事弧独立规划，不预写整本书。</small>
          </article>
        </div>
        <footer className="direction-footer">
          <span>已回答 {answeredCount} / {state.questions.length} 个问题</span>
          <button className="text-button" onClick={onExit}>退出规划</button>
        </footer>
      </aside>
    </div>
  );
}
