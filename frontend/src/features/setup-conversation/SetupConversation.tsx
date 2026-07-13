import { Check, FileCheck2, PanelRight, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";
import { api, formatApiError } from "../../api/client";
import type { SetupStateDocument } from "../../types/domain";
import { DirectionInspector } from "./DirectionInspector";
import { SetupDiscussion } from "./SetupDiscussion";
import { SetupReview } from "./SetupReview";
import type { BusyAction, Notice, TitleChoice } from "./setup-types";
import styles from "./SetupConversation.module.css";

interface SetupConversationProps {
  projectId: string;
  onApproved: () => void | Promise<void>;
  onExit: () => void;
  onSetupChanged?: (state: SetupStateDocument) => void | Promise<void>;
}

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

export function SetupConversation({ projectId, onApproved, onExit, onSetupChanged }: SetupConversationProps) {
  const draftStorageKey = `novelpilot:book-direction-input:${projectId}`;
  const [state, setState] = useState<SetupStateDocument | null>(null);
  const [input, setInput] = useState(() => readLocalDraft(draftStorageKey));
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<BusyAction>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [titleChoice, setTitleChoice] = useState<TitleChoice>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api.setupState()
      .then((nextState) => { if (!cancelled) setState(nextState); })
      .catch((error) => { if (!cancelled) setNotice({ kind: "error", text: formatSetupError(error) }); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  useEffect(() => writeLocalDraft(draftStorageKey, input), [draftStorageKey, input]);
  useEffect(() => setTitleChoice(null), [projectId, state?.candidate?.revision]);

  const candidate = state?.candidate ?? null;
  const finalTitle = titleChoice?.title.trim() ?? "";
  const approvalAllowed = Boolean(
    candidate && candidate.review.status === "passed" && !candidate.review.issues.some((issue) => issue.severity === "blocking")
  );
  const canSend = Boolean(input.trim()) && busyAction === null && !state?.approved;
  const canReview = Boolean(state?.direction_draft.trim()) && candidate === null && busyAction === null && !state?.approved;
  const canApprove = approvalAllowed && Boolean(finalTitle) && !input.trim() && busyAction === null;

  async function applyState(nextState: SetupStateDocument) {
    setState(nextState);
    await onSetupChanged?.(nextState);
  }

  function useSuggestedText(message: string) {
    setInput(message);
  }

  async function sendMessage() {
    const message = input.trim();
    if (!message || !canSend) return;
    setBusyAction("turn");
    setNotice(null);
    setInput("");
    writeLocalDraft(draftStorageKey, "");
    try {
      const nextState = await api.continueSetupDiscussion(message);
      await applyState(nextState);
      setNotice({ kind: "success", text: "本轮讨论完成，方向草稿和不确定项已经更新。" });
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
    setBusyAction("review");
    setNotice(null);
    try {
      const nextState = await api.prepareSetupReview();
      await applyState(nextState);
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

  if (loading) return <div className={styles.centerState}>正在读取全书共创状态...</div>;
  if (!state) return <div className={`${styles.centerState} ${styles.error}`}>无法读取全书共创状态。</div>;

  if (state.approved) {
    return (
      <section className={styles.approvedState}>
        <span><Check size={24} /></span>
        <p>全书方向已提交</p>
        <h1>{state.approved_title ? `《${state.approved_title}》` : "全书方向已经批准"}</h1>
        <div className={styles.approvedDirection}>{state.direction_draft}</div>
        <footer>
          <span>后续只滚动规划当前故事弧，候选讨论不会覆盖已批准设定。</span>
          <button onClick={() => void onApproved()}><ShieldCheck size={16} /> 进入创作工作台</button>
        </footer>
      </section>
    );
  }

  return (
    <section className={styles.workspace} data-stage={candidate ? "review" : "discussion"}>
      <main className={styles.mainPane}>
        <button className={styles.inspectorTrigger} title="查看方向账本" onClick={() => setInspectorOpen(true)}><PanelRight size={17} /></button>
        {candidate ? (
          <SetupReview
            state={state}
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
            onUseSuggestion={useSuggestedText}
            onSend={() => void sendMessage()}
            onApprove={() => void approve()}
            onExit={onExit}
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
            onUseSuggestion={useSuggestedText}
            onSend={() => void sendMessage()}
            onReview={() => void prepareReview()}
            onExit={onExit}
          />
        )}
      </main>
      <DirectionInspector state={state} open={inspectorOpen} onClose={() => setInspectorOpen(false)} />
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
