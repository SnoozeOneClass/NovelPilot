import {
  AlertTriangle,
  Check,
  FileCheck2,
  MessageSquareText,
  Send,
  ShieldCheck,
  Sparkles
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, formatApiError } from "../../api/client";
import type {
  BookDirectionCandidate,
  BookDirectionConstraints,
  SetupStateDocument
} from "../../types/domain";

interface SetupConversationProps {
  projectId: string;
  onApproved: () => void | Promise<void>;
  onExit: () => void;
  onSetupChanged?: (state: SetupStateDocument) => void;
}

type BusyAction = "turn" | "review" | "approve" | null;
type Notice = { kind: "success" | "error"; text: string };
type TitleChoice =
  | { kind: "recommended"; title: string }
  | { kind: "custom"; title: string }
  | null;

const setupErrorCopy: Record<string, string> = {
  "Book direction is already approved.": "全书方向已经批准，不能再写入候选讨论。",
  "Book direction candidate is stale; review the latest candidate.":
    "这个候选版本已经过期，请审阅并处理最新候选。",
  "Book direction must be synthesized and reviewed before approval.":
    "全书方向必须先整理为候选并完成审阅。",
  "Book direction review has blocking issues.": "候选方向仍有阻断问题，暂时不能批准。",
  "Book direction candidate does not preserve every confirmed decision.":
    "候选方向没有完整保留全部已确认决定，请继续讨论并重新整理审阅。",
  "Book discussion state changed while the model was working; discard the stale result.":
    "模型工作期间全书讨论状态已经变化，本次过期结果已丢弃，请载入最新状态后继续。",
  "Discuss the novel direction before requesting a review.":
    "请先讨论小说方向并形成草稿，再请求审阅。",
  "Select an enabled LLM profile before continuing the book discussion.":
    "请先在“设置与模型”中选择一个可用的 LLM 配置。",
  "Book title contains configured provider credentials or endpoint data. Choose a different title.":
    "书名中包含已配置的 Provider 凭据或接口地址，请换一个书名。",
  "The current Book Direction candidate has already been reviewed. Approve it or continue the discussion before preparing another candidate.":
    "当前候选已经审阅。请批准它，或继续讨论使它失效后再整理新候选。"
};

const constraintSections: Array<{
  key: keyof BookDirectionConstraints;
  title: string;
}> = [
  { key: "confirmed", title: "已确认决定" },
  { key: "must_preserve", title: "必须维护" },
  { key: "must_avoid", title: "必须避免" },
  { key: "creative_freedoms", title: "创作自由" },
  { key: "open_decisions", title: "仍待决定" }
];

export function SetupConversation({
  projectId,
  onApproved,
  onExit,
  onSetupChanged
}: SetupConversationProps) {
  const [state, setState] = useState<SetupStateDocument | null>(null);
  const draftStorageKey = `novelpilot:book-direction-input:${projectId}`;
  const [input, setInput] = useState(() => readLocalDraft(draftStorageKey));
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<BusyAction>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [titleChoice, setTitleChoice] = useState<TitleChoice>(null);
  const conversationEndRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .setupState()
      .then((nextState) => {
        if (!cancelled) setState(nextState);
      })
      .catch((error) => {
        if (!cancelled) setNotice({ kind: "error", text: formatSetupError(error) });
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    conversationEndRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [state?.messages.length, busyAction]);

  useEffect(() => {
    writeLocalDraft(draftStorageKey, input);
  }, [draftStorageKey, input]);

  const candidate = state?.candidate ?? null;
  const finalTitle = titleChoice?.title.trim() ?? "";

  useEffect(() => {
    setTitleChoice(null);
  }, [projectId, candidate?.revision]);

  const approvalAllowed = Boolean(
    candidate
      && candidate.review.status === "passed"
      && !candidate.review.issues.some((issue) => issue.severity === "blocking")
  );
  const canSend = Boolean(input.trim()) && busyAction === null && !state?.approved;
  const canApprove = approvalAllowed
    && candidate !== null
    && Boolean(finalTitle)
    && !input.trim()
    && busyAction === null;
  const canReview = Boolean(state?.direction_draft.trim())
    && candidate === null
    && busyAction === null
    && !state?.approved;
  const directionLedger = useMemo(
    () => [
      { title: "已确认", items: state?.confirmed_decisions ?? [], tone: "confirmed" },
      { title: "待澄清", items: state?.unresolved_questions ?? [], tone: "open" },
      { title: "当前假设", items: state?.assumptions ?? [], tone: "assumption" },
      { title: "矛盾", items: state?.contradictions ?? [], tone: "conflict" },
      {
        title: "已取代",
        items: (state?.superseded_decisions ?? []).slice(-5).map((item) => (
          item.replacement ? `${item.decision} → ${item.replacement}` : `${item.decision} → 已撤销`
        )),
        tone: "superseded"
      }
    ],
    [state]
  );

  function applyState(nextState: SetupStateDocument) {
    setState(nextState);
    onSetupChanged?.(nextState);
  }

  function useSuggestedText(message: string) {
    setInput((current) => current.trim() ? `${current.trimEnd()}\n\n${message}` : message);
  }

  async function sendMessage() {
    const message = input.trim();
    if (!message || !canSend) return;
    setBusyAction("turn");
    setNotice(null);
    try {
      const nextState = await api.continueSetupDiscussion(message);
      applyState(nextState);
      setInput("");
      setNotice({ kind: "success", text: "本轮讨论已完成，方向草稿和不确定项已经更新。" });
    } catch (error) {
      setNotice({
        kind: "error",
        text: `${formatSetupError(error)} 当前输入尚未提交，可以直接重试。`
      });
    } finally {
      setBusyAction(null);
    }
  }

  async function prepareReview() {
    if (!canReview) return;
    setBusyAction("review");
    setNotice(null);
    try {
      const nextState = await api.prepareSetupReview();
      applyState(nextState);
      setNotice({
        kind: nextState.candidate?.review.status === "passed" ? "success" : "error",
        text:
          nextState.candidate?.review.status === "passed"
            ? "候选全书方向已完成独立审查，等待你的明确批准。"
            : "候选方向存在阻断问题。可以继续讨论，下一轮会自动废止当前候选。"
      });
    } catch (error) {
      setNotice({
        kind: "error",
        text: `${formatSetupError(error)} 审查失败不会批准或覆盖任何正式设定。`
      });
    } finally {
      setBusyAction(null);
    }
  }

  async function approve() {
    if (!candidate || !canApprove) return;
    setBusyAction("approve");
    setNotice(null);
    try {
      const nextState = await api.approveSetup(candidate.revision, finalTitle);
      applyState(nextState);
      setNotice({ kind: "success", text: `《${finalTitle}》与全书方向已正式批准并提交。` });
      await onApproved();
    } catch (error) {
      setNotice({ kind: "error", text: formatSetupError(error) });
    } finally {
      setBusyAction(null);
    }
  }

  if (loading) {
    return <div className="center-state">正在读取全书讨论状态...</div>;
  }
  if (!state) {
    return <div className="center-state error">无法读取全书讨论状态。</div>;
  }

  return (
    <div className="plan-layout book-discovery-layout">
      <section className="np-surface plan-main book-discussion-panel">
        <header className="view-heading compact-heading">
          <div>
            <h1>开书规划 · 深度共创</h1>
            <p>没有固定问题数量。持续讨论，直到你愿意整理、审阅并批准全书方向。</p>
          </div>
          <div className="book-discussion-statuses">
            <span className={`soft-badge ${state.readiness.status === "ready" ? "green" : "gold"}`}>
              <Sparkles size={13} />
              {state.readiness.status === "ready" ? "模型认为可整理" : "继续讨论中"}
            </span>
            <span className="soft-badge">{state.turn_count} 轮</span>
          </div>
        </header>

        {notice && <p className={`notice-banner ${notice.kind}`}>{notice.text}</p>}

        {state.approved ? (
          <div className="plan-approved-state">
            <span className="approval-mark"><Check size={28} /></span>
            <h2>
              {state.approved_title
                ? `《${state.approved_title}》与全书方向已经批准`
                : "全书方向已经批准"}
            </h2>
            <p>后续只滚动规划当前故事弧，讨论草稿不会再覆盖已批准设定。</p>
            <button className="gold-button" onClick={() => void onApproved()}>
              进入创作工作台
            </button>
          </div>
        ) : (
          <>
            <div className="book-conversation" aria-live="polite">
              {state.messages.length === 0 && (
                <div className="book-conversation-empty">
                  <MessageSquareText size={30} />
                  <h2>从你的核心想法开始</h2>
                  <p>可以只有一句模糊灵感，也可以一次写下题材、人物、氛围和不想出现的内容。</p>
                </div>
              )}
              {state.messages.map((message) => (
                <article key={message.id} className={`book-message ${message.role}`}>
                  <header>
                    <strong>{message.role === "user" ? "你" : "NovelPilot"}</strong>
                    <span>
                      第 {message.turn} 轮
                      {message.model_snapshot ? ` · ${message.model_snapshot}` : ""}
                      {message.migrated ? " · 旧版迁移" : ""}
                    </span>
                  </header>
                  <p>{message.content}</p>
                </article>
              ))}
              {busyAction === "turn" && (
                <article className="book-message assistant pending">
                  <header><strong>NovelPilot</strong><span>正在整理本轮讨论</span></header>
                  <p>正在更新完整方向草稿，并重新识别待定项、假设与矛盾...</p>
                </article>
              )}
              <div ref={conversationEndRef} />
            </div>

            {state.suggestions.length > 0 && busyAction === null && (
              <div className="book-suggestions">
                <span>AI 提供的表达方向</span>
                <div>
                  {state.suggestions.map((suggestion) => (
                    <button
                      key={suggestion.id}
                      className="quiet-button"
                      title={suggestion.message}
                      onClick={() => useSuggestedText(suggestion.message)}
                    >
                      {suggestion.label}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {candidate && (
              <CandidateReview candidate={candidate} onUseQuestion={useSuggestedText} />
            )}

            {candidate && (
              <BookTitlePicker
                candidate={candidate}
                choice={titleChoice}
                disabled={busyAction !== null}
                onChange={setTitleChoice}
              />
            )}

            <div className="book-composer">
              <textarea
                value={input}
                maxLength={32000}
                disabled={busyAction !== null}
                placeholder="继续描述、纠正、否定或提出新的方向。"
                onChange={(event) => setInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && event.ctrlKey) {
                    event.preventDefault();
                    void sendMessage();
                  }
                }}
              />
              <button
                className="send-book-message"
                title="发送本轮讨论"
                disabled={!canSend}
                onClick={() => void sendMessage()}
              >
                <Send size={18} />
              </button>
            </div>

            <footer className="book-discussion-actions">
              <div>
                <strong>{state.readiness.reason}</strong>
                <small>就绪提示不会自动结束讨论，你仍然可以继续输入。</small>
              </div>
              <button className="outline-button" disabled={busyAction !== null} onClick={onExit}>
                退出规划
              </button>
              <button className="gold-button" disabled={!canReview} onClick={() => void prepareReview()}>
                <FileCheck2 size={17} />
                {busyAction === "review" ? "正在综合与审查..." : "整理并审阅"}
              </button>
              {approvalAllowed && candidate && (
                <button
                  className="green-button"
                  disabled={!canApprove}
                  title={
                    input.trim()
                      ? "请先发送或清空尚未提交的讨论内容"
                      : !finalTitle
                        ? "请先选择推荐书名或输入自定义书名"
                        : "批准当前候选版本与正式书名"
                  }
                  onClick={() => void approve()}
                >
                  <ShieldCheck size={17} />
                  {busyAction === "approve" ? "正在提交..." : `批准候选 v${candidate.revision}`}
                </button>
              )}
            </footer>
          </>
        )}
      </section>

      <aside className="np-surface direction-draft book-direction-panel">
        <header className="view-heading compact-heading">
          <div>
            <h2>{state.approved ? "已批准全书方向" : "全书方向草稿"}</h2>
            <p>
              {state.approved
                ? "这是已经明确批准并提交的全书方向。"
                : "候选内容，批准前不会进入正式全书状态。"}
            </p>
          </div>
          <span className={`soft-badge ${state.approved ? "green" : "gold"}`}>
            {state.approved ? "已提交" : `候选 r${state.revision}`}
          </span>
        </header>

        <pre className={`book-direction-markdown ${state.direction_draft ? "" : "empty"}`}>
          {state.direction_draft || "对话开始后，模型会在这里持续维护一份完整方向草稿。"}
        </pre>

        <div className="direction-ledger">
          {directionLedger.map((section) => (
            <section key={section.title} className={section.tone}>
              <h3>{section.title}<span>{section.items.length}</span></h3>
              {section.items.length > 0 ? (
                <ul>{section.items.map((item) => <li key={item}>{item}</li>)}</ul>
              ) : (
                <p>暂无</p>
              )}
            </section>
          ))}
        </div>

        {candidate && (
          <div className="candidate-contract">
            <h3>{state.approved ? "已批准滚动规划契约" : "候选滚动规划契约"}</h3>
            <pre>{candidate.rolling_plan_markdown}</pre>
            <div className="candidate-constraints">
              {constraintSections.map((section) => {
                const items = candidate.constraints[section.key];
                return items.length > 0 ? (
                  <section key={section.key}>
                    <strong>{section.title}</strong>
                    <ul>{items.map((item) => <li key={item}>{item}</li>)}</ul>
                  </section>
                ) : null;
              })}
            </div>
          </div>
        )}
      </aside>
    </div>
  );
}

function BookTitlePicker({
  candidate,
  choice,
  disabled,
  onChange
}: {
  candidate: BookDirectionCandidate;
  choice: TitleChoice;
  disabled: boolean;
  onChange: (choice: TitleChoice) => void;
}) {
  const customTitle = choice?.kind === "custom" ? choice.title : "";

  return (
    <section className="book-title-picker">
      <header>
        <div>
          <h2>为这本书确定正式书名</h2>
          <p>书名会与当前候选全书方向一起批准；继续讨论不会提前改动项目名称。</p>
        </div>
        <span className={`soft-badge ${choice?.title.trim() ? "green" : "gold"}`}>
          {choice?.title.trim() ? "已选择" : "批准前必填"}
        </span>
      </header>

      <div className="recommended-title-list">
        {candidate.recommended_titles.map((option, index) => {
          const selected = choice?.kind === "recommended" && choice.title === option.title;
          return (
            <button
              key={`${option.title}-${index}`}
              type="button"
              className={selected ? "selected" : ""}
              aria-pressed={selected}
              disabled={disabled}
              onClick={() => onChange({ kind: "recommended", title: option.title })}
            >
              <span>{index + 1}</span>
              <div>
                <strong>《{option.title}》</strong>
                <small>{option.rationale}</small>
              </div>
            </button>
          );
        })}
      </div>

      <label className={`custom-book-title ${choice?.kind === "custom" ? "selected" : ""}`}>
        <span>自定义书名</span>
        <input
          value={customTitle}
          maxLength={200}
          disabled={disabled}
          placeholder="输入你希望采用的正式书名"
          onFocus={() => {
            if (choice?.kind !== "custom") onChange({ kind: "custom", title: "" });
          }}
          onChange={(event) => onChange({ kind: "custom", title: event.target.value })}
        />
      </label>
    </section>
  );
}

function CandidateReview({
  candidate,
  onUseQuestion
}: {
  candidate: BookDirectionCandidate;
  onUseQuestion: (question: string) => void;
}) {
  const passed = candidate.review.status === "passed"
    && !candidate.review.issues.some((issue) => issue.severity === "blocking");
  return (
    <section className={`book-candidate-review ${passed ? "passed" : "blocked"}`}>
      <header>
        <span>{passed ? <ShieldCheck size={20} /> : <AlertTriangle size={20} />}</span>
        <div>
          <h2>{passed ? "候选方向通过语义审查" : "候选方向仍有阻断问题"}</h2>
          <p>{candidate.review.summary}</p>
        </div>
        <strong>v{candidate.revision}</strong>
      </header>
      {candidate.review.issues.length > 0 && (
        <div className="book-review-issues">
          {candidate.review.issues.map((issue, index) => (
            <article key={`${issue.kind}-${index}`} className={issue.severity}>
              <span>{issue.severity === "blocking" ? "阻断" : "提醒"}</span>
              <div>
                <strong>{issue.message}</strong>
                {issue.evidence.length > 0 && <small>{issue.evidence.join(" · ")}</small>}
                {issue.suggested_question && (
                  <button className="text-button" onClick={() => onUseQuestion(issue.suggested_question ?? "")}>
                    用建议问题继续讨论
                  </button>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
      {candidate.review.signals.length > 0 && (
        <div className="book-review-signals">
          <strong>验证信号</strong>
          <ul>
            {candidate.review.signals.map((signal) => (
              <li key={signal}>{formatBookReviewSignal(signal)}</li>
            ))}
          </ul>
        </div>
      )}
      {candidate.confirmed_decision_coverage.length > 0 && (
        <div className="book-decision-coverage">
          <strong>已确认决定覆盖</strong>
          <ul>
            {candidate.confirmed_decision_coverage.map((item) => (
              <li key={item.decision}>
                <span>{item.decision}</span>
                <small>{item.candidate_evidence}</small>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}

function formatSetupError(error: unknown): string {
  const message = formatApiError(error);
  return setupErrorCopy[message] ?? message;
}

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
  const coverageMatch = signal.match(/^confirmed_decision_coverage:(\d+)\/(\d+)$/);
  if (coverageMatch) {
    return `已确认决定覆盖：${coverageMatch[1]}/${coverageMatch[2]}`;
  }
  const statusMatch = signal.match(/^([^:]+):(passed|failed|warning)$/);
  if (!statusMatch) return signal;
  const labels: Record<string, string> = {
    confirmed_decisions_preserved: "已确认决定保持一致",
    rolling_scope: "滚动规划范围"
  };
  const statuses: Record<string, string> = {
    passed: "通过",
    failed: "未通过",
    warning: "提醒"
  };
  return `${labels[statusMatch[1]] ?? statusMatch[1]}：${statuses[statusMatch[2]]}`;
}

function readLocalDraft(key: string): string {
  try {
    return window.sessionStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}

function writeLocalDraft(key: string, value: string): void {
  try {
    if (value.trim()) {
      window.sessionStorage.setItem(key, value);
    } else {
      window.sessionStorage.removeItem(key);
    }
  } catch {
    // The in-memory draft still works when browser storage is unavailable.
  }
}
