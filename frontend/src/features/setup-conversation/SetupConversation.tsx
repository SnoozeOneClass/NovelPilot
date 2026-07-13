import { Check, FileCheck2, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, formatApiError } from "../../api/client";
import { Dialog } from "../../components/ui/Dialog";
import type { HarnessEvent, SetupStateDocument } from "../../types/domain";
import { BookDirectionDocument } from "./BookDirectionDocument";
import { DirectionLedger } from "./DirectionInspector";
import { PlanningStageBar } from "./PlanningStageBar";
import { SetupDiscussion } from "./SetupDiscussion";
import { SetupReview } from "./SetupReview";
import { deriveSetupPlanningStage, summarizeSetupChanges, type SetupChangeSummary } from "./setup-planning";
import type { BusyAction, Notice, TitleChoice } from "./setup-types";
import styles from "./SetupConversation.module.css";

interface SetupConversationProps {
  projectId: string;
  events?: HarnessEvent[];
  onApproved: () => void | Promise<void>;
  onExit: () => void;
  onSetupChanged?: (state: SetupStateDocument) => void | Promise<void>;
}

type WorkspaceView = "plan" | "decision" | "ledger";

const setupErrorCopy: Record<string, string> = {
  "Book direction is already approved.": "全书方向已经批准，不能再写入候选讨论。",
  "Book direction candidate is stale; review the latest candidate.": "这个候选版本已经过期，请处理最新候选。",
  "Book direction must be synthesized and reviewed before approval.": "全书方向必须先整理为候选并完成审阅。",
  "Book direction review has blocking issues.": "候选方向仍有阻断问题，暂时不能批准。",
  "Book direction candidate does not preserve every confirmed decision.": "候选方向没有完整保留全部已确认决定，请继续讨论并重新审阅。",
  "Book discussion state changed while the model was working; discard the stale result.": "模型工作期间讨论状态已经变化，本次过期结果已丢弃，请载入最新状态后继续。",
  "Discuss the novel direction before requesting a review.": "请先讨论小说方向并形成草稿，再请求审阅。",
  "Select an enabled LLM profile before continuing the book discussion.": "请先在设置中选择一个可用的 LLM 配置。",
  "Book title contains configured provider credentials or endpoint data. Choose a different title.": "书名中包含 Provider 凭据或接口地址，请换一个书名。",
  "The current Book Direction candidate has already been reviewed. Approve it or continue the discussion before preparing another candidate.": "当前候选已经审阅。请批准它，或继续讨论使它失效后再整理新候选。"
};

export function SetupConversation({ projectId, events = [], onApproved, onExit, onSetupChanged }: SetupConversationProps) {
  const draftStorageKey = `novelpilot:book-direction-input:${projectId}`;
  const [state, setState] = useState<SetupStateDocument | null>(null);
  const [input, setInput] = useState(() => readLocalDraft(draftStorageKey));
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<BusyAction>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [titleChoice, setTitleChoice] = useState<TitleChoice>(null);
  const [activeView, setActiveView] = useState<WorkspaceView>("plan");
  const [historyOpen, setHistoryOpen] = useState(false);
  const [changeSummary, setChangeSummary] = useState<SetupChangeSummary | null>(null);
  const [streamStartIndex, setStreamStartIndex] = useState(0);
  const historyTriggerRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    api.setupState()
      .then((nextState) => { if (!cancelled) setState(nextState); })
      .catch((error) => { if (!cancelled) setNotice({ kind: "error", text: formatSetupError(error) }); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => writeLocalDraft(draftStorageKey, input), [draftStorageKey, input]);
  useEffect(() => {
    setTitleChoice(null);
  }, [projectId, state?.candidate?.revision]);

  useEffect(() => {
    if (state?.candidate) setActiveView("decision");
  }, [state?.candidate?.revision]);

  const candidate = state?.candidate ?? null;
  const finalTitle = titleChoice?.title.trim() ?? "";
  const approvalAllowed = Boolean(
    candidate && candidate.review.status === "passed" && !candidate.review.issues.some((issue) => issue.severity === "blocking")
  );
  const canSend = Boolean(input.trim()) && busyAction === null && !state?.approved;
  const canReview = Boolean(state?.direction_draft.trim()) && candidate === null && busyAction === null && !state?.approved;
  const canApprove = approvalAllowed && Boolean(finalTitle) && !input.trim() && busyAction === null;
  const streamedCharacterCount = useMemo(
    () => events.slice(streamStartIndex).reduce((latest, event) => {
      const value = event.payload.received_characters;
      return event.kind === "llm_stream_progress" && typeof value === "number"
        ? value
        : latest;
    }, 0),
    [events, streamStartIndex]
  );

  async function applyState(nextState: SetupStateDocument) {
    setState(nextState);
    await onSetupChanged?.(nextState);
  }

  async function sendMessage() {
    const message = input.trim();
    if (!message || !canSend || !state) return;
    const previousState = state;
    setStreamStartIndex(events.length);
    setBusyAction("turn");
    setNotice(null);
    setInput("");
    writeLocalDraft(draftStorageKey, "");
    try {
      const nextState = await api.continueSetupDiscussion(message);
      setChangeSummary(summarizeSetupChanges(previousState, nextState));
      await applyState(nextState);
      setActiveView("decision");
      setNotice({ kind: "success", text: "本轮讨论已合并进 Book Direction。" });
    } catch (error) {
      const currentDraft = readLocalDraft(draftStorageKey);
      if (!currentDraft.trim()) {
        writeLocalDraft(draftStorageKey, message);
        setInput(message);
      }
      setNotice({ kind: "error", text: `${formatSetupError(error)} 当前输入尚未提交，可以直接重试。` });
    } finally {
      setBusyAction(null);
    }
  }

  async function prepareReview() {
    if (!canReview) return;
    setStreamStartIndex(events.length);
    setBusyAction("review");
    setNotice(null);
    try {
      const nextState = await api.prepareSetupReview();
      await applyState(nextState);
      setActiveView("decision");
      setNotice({
        kind: nextState.candidate?.review.status === "passed" ? "success" : "error",
        text: nextState.candidate?.review.status === "passed"
          ? "候选全书方向已完成独立审查，等待你的明确批准。"
          : "候选方向存在阻断问题。继续讨论会自动废止当前候选。"
      });
    } catch (error) {
      setNotice({ kind: "error", text: `${formatSetupError(error)} 审查失败不会批准或覆盖任何正式设定。` });
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
      await applyState(nextState);
      setNotice({ kind: "success", text: `《${finalTitle}》与全书方向已正式批准并提交。` });
      await onApproved();
    } catch (error) {
      setNotice({ kind: "error", text: formatSetupError(error) });
    } finally {
      setBusyAction(null);
    }
  }

  function closeHistory() {
    setHistoryOpen(false);
    requestAnimationFrame(() => historyTriggerRef.current?.focus());
  }

  if (loading) return <div className={styles.centerState}>正在读取全书共创状态...</div>;
  if (!state) return <div className={`${styles.centerState} ${styles.error}`}>无法读取全书共创状态。</div>;

  const stage = deriveSetupPlanningStage(state);
  const documentMarkdown = candidate?.direction_markdown ?? state.direction_draft;
  const documentRevision = candidate?.revision ?? state.revision;

  return (
    <section className={styles.workspace} data-stage={stage}>
      <PlanningStageBar
        state={state}
        stage={stage}
        historyTriggerRef={historyTriggerRef}
        onOpenHistory={() => setHistoryOpen(true)}
        onExit={onExit}
      />

      <nav className={styles.mobileTabs} role="tablist" aria-label="规划工作区视图">
        {(["plan", "decision", "ledger"] as WorkspaceView[]).map((view) => (
          <button key={view} type="button" role="tab" aria-selected={activeView === view} aria-controls={`setup-${view}-panel`} onClick={() => setActiveView(view)}>
            {{ plan: "计划", decision: candidate ? "审阅" : "当前决策", ledger: "账本" }[view]}
          </button>
        ))}
      </nav>

      <main id="setup-plan-panel" role="tabpanel" className={styles.planPane} data-mobile-visible={activeView === "plan"}>
        <BookDirectionDocument
          markdown={documentMarkdown}
          revision={documentRevision}
          mode={candidate ? "candidate" : "draft"}
          changeSummary={changeSummary}
        />
      </main>

      <aside className={styles.contextPane} data-mobile-visible={activeView !== "plan"}>
        {busyAction && (
          <div className={styles.streamProgress} role="status" aria-live="polite">
            <span />
            {streamedCharacterCount
              ? `模型正在流式生成，已接收 ${streamedCharacterCount.toLocaleString()} 个字符`
              : "正在连接模型流..."}
          </div>
        )}
        <nav className={styles.contextTabs} role="tablist" aria-label="决策上下文">
          <button type="button" role="tab" aria-selected={activeView !== "ledger"} aria-controls="setup-decision-panel" onClick={() => setActiveView("decision")}>{candidate ? "候选审阅" : "当前决策"}</button>
          <button type="button" role="tab" aria-selected={activeView === "ledger"} aria-controls="setup-ledger-panel" onClick={() => setActiveView("ledger")}>方向账本</button>
        </nav>
        <div id="setup-decision-panel" role="tabpanel" className={styles.contextPanel} data-visible={activeView !== "ledger"} data-mobile-visible={activeView === "decision"}>
          {state.approved ? (
            <section className={styles.approvedHandoff}>
              <span><Check size={22} /></span>
              <p>全书方向已批准</p>
              <h2>{state.approved_title ? `《${state.approved_title}》` : "规划已经完成"}</h2>
              <small>后续只滚动规划当前故事弧，不会覆盖已批准的最高层方向。</small>
              <button onClick={() => void onApproved()}><ShieldCheck size={16} />进入创作工作台</button>
            </section>
          ) : candidate ? (
            <SetupReview
              candidate={candidate}
              input={input}
              titleChoice={titleChoice}
              busyAction={busyAction}
              notice={notice}
              approvalAllowed={approvalAllowed}
              canSend={canSend}
              canApprove={canApprove}
              onInputChange={setInput}
              onTitleChange={setTitleChoice}
              onUseSuggestion={setInput}
              onSend={() => void sendMessage()}
              onApprove={() => void approve()}
            />
          ) : (
            <SetupDiscussion
              state={state}
              input={input}
              busyAction={busyAction}
              notice={notice}
              canSend={canSend}
              canReview={canReview}
              onInputChange={setInput}
              onUseSuggestion={setInput}
              onSend={() => void sendMessage()}
              onReview={() => void prepareReview()}
            />
          )}
        </div>
        <div id="setup-ledger-panel" role="tabpanel" className={styles.contextPanel} data-visible={activeView === "ledger"} data-mobile-visible={activeView === "ledger"}>
          <DirectionLedger state={state} />
        </div>
      </aside>

      <Dialog open={historyOpen} title="全书共创讨论记录" onClose={closeHistory}>
        <div className={styles.historyList}>
          {state.messages.length > 0 ? state.messages.map((message) => (
            <article key={message.id} data-role={message.role}>
              <header><strong>{message.role === "user" ? "你" : "NovelPilot"}</strong><span>第 {message.turn} 轮{message.model_snapshot ? ` · ${message.model_snapshot}` : ""}</span></header>
              <p>{message.content}</p>
            </article>
          )) : <p className={styles.emptyHistory}>还没有讨论记录。</p>}
        </div>
      </Dialog>

      {busyAction === "review" && (
        <div className={styles.busyOverlay}><FileCheck2 size={20} /><strong>正在综合与审查全书方向</strong><span>候选仍未进入正式设定。</span></div>
      )}
    </section>
  );
}

function formatSetupError(error: unknown): string {
  const message = formatApiError(error);
  return setupErrorCopy[message] ?? message;
}

function readLocalDraft(key: string): string {
  try { return window.sessionStorage.getItem(key) ?? ""; } catch { return ""; }
}

function writeLocalDraft(key: string, value: string): void {
  try {
    if (value.trim()) window.sessionStorage.setItem(key, value);
    else window.sessionStorage.removeItem(key);
  } catch {
    // The in-memory draft remains usable when browser storage is unavailable.
  }
}
