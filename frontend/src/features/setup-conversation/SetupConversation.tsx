import { CheckCircle2, CircleDot, Send } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, formatApiError } from "../../api/client";
import {
  composeSetupOptionAnswer,
  formatSetupAnswer,
  formatSetupOption,
  formatSetupQuestionPrompt,
  formatSetupQuestionTitle,
  formatSetupSource
} from "../../types/display";
import type { SetupStateDocument } from "../../types/domain";

interface SetupConversationProps {
  projectId: string;
  onSetupChanged?: () => void;
}

export function SetupConversation({ projectId, onSetupChanged }: SetupConversationProps) {
  const [state, setState] = useState<SetupStateDocument | null>(null);
  const [customAnswer, setCustomAnswer] = useState("");
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<{ kind: "error"; text: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    setNotice(null);
    setState(null);
    api
      .setupState()
      .then((nextState) => {
        if (!cancelled) {
          setState(nextState);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setNotice({ kind: "error", text: formatApiError(error) });
        }
      });

    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const answerMap = useMemo(() => {
    const entries =
      state?.answers.map(
        (answer): [string, string] => [answer.question_id, answer.answer]
      ) ?? [];
    return new Map<string, string>(entries);
  }, [state?.answers]);

  async function answerCurrent(answer: string) {
    if (!state?.next_question || !answer.trim()) return;
    setSaving(true);
    setNotice(null);
    try {
      const nextState = await api.answerSetup(state.next_question.id, answer.trim());
      setState(nextState);
      setCustomAnswer("");
      onSetupChanged?.();
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setSaving(false);
    }
  }

  async function approve() {
    setSaving(true);
    setNotice(null);
    try {
      setState(await api.approveSetup());
      onSetupChanged?.();
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setSaving(false);
    }
  }

  if (!state) {
    return (
      <section className="setup-card">
        <div className="panel-title">
          <CircleDot size={18} />
          <span>全书设定</span>
        </div>
        {notice && <p className={`notice-banner compact ${notice.kind}`}>{notice.text}</p>}
      </section>
    );
  }

  const currentQuestion = state.next_question;

  return (
    <section className="setup-card">
      <div className="panel-title">
        <CheckCircle2 size={18} />
        <span>全书设定</span>
      </div>
      {notice && <p className={`notice-banner compact ${notice.kind}`}>{notice.text}</p>}
      {state.approved ? (
        <p className="setup-approved">全书流程已批准</p>
      ) : currentQuestion ? (
        <div className="setup-question">
          <div className="question-meta">
            <p className="eyebrow">{formatSetupQuestionTitle(currentQuestion)}</p>
            <span>
              {formatSetupSource(currentQuestion.source)}
              {currentQuestion.model_snapshot ? ` / ${currentQuestion.model_snapshot}` : ""}
            </span>
          </div>
          <h2>{formatSetupQuestionPrompt(currentQuestion)}</h2>
          <div className="decision-row">
            {currentQuestion.options.map((option) => {
              const displayOption = formatSetupOption(currentQuestion, option);
              return (
                <button
                  key={option.id}
                  disabled={saving}
                  onClick={() => answerCurrent(composeSetupOptionAnswer(currentQuestion, option))}
                >
                  <strong>{displayOption.label}</strong>
                  <span>{displayOption.description}</span>
                </button>
              );
            })}
          </div>
          <div className="custom-answer">
            <input
              value={customAnswer}
              disabled={saving}
              onChange={(event) => setCustomAnswer(event.target.value)}
              placeholder="自定义回答"
            />
            <button title="提交回答" disabled={saving} onClick={() => answerCurrent(customAnswer)}>
              <Send size={18} />
            </button>
          </div>
        </div>
      ) : (
        <div className="setup-ready">
          <p>全书方向已准备好，可以批准进入写作。</p>
          <button className="primary-button" disabled={saving} onClick={approve}>
            批准全书流程
          </button>
        </div>
      )}
      <div className="setup-answers">
        {state.questions.map((question) => (
          <div key={question.id}>
            <strong>{formatSetupQuestionTitle(question)}</strong>
            <span>{formatSetupAnswer(question, answerMap.get(question.id))}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
