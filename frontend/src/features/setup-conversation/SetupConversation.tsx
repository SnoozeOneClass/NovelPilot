import { BookOpenText, Check, FileCheck2, MessageSquareText, PanelRightOpen, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, formatApiError } from "../../api/client";
import { Badge } from "../../components/ui/Badge";
import { Button } from "../../components/ui/Button";
import { Dialog } from "../../components/ui/Dialog";
import { Sheet } from "../../components/ui/Sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../../components/ui/Tabs";
import type { HarnessEvent, SetupStateDocument, SetupSuggestion } from "../../types/domain";
import { BookDirectionDocument } from "./BookDirectionDocument";
import { ConversationTranscript } from "./ConversationTranscript";
import { DirectionLedger } from "./DirectionInspector";
import { PlanningStageBar } from "./PlanningStageBar";
import { SetupDiscussion } from "./SetupDiscussion";
import { SetupReview, SetupReviewDetails } from "./SetupReview";
import { formatBookTitle } from "./setup-formatters";
import { deriveSetupPlanningStage, summarizeSetupChanges, type SetupChangeSummary } from "./setup-planning";
import type { BusyAction, Notice } from "./setup-types";
import styles from "./SetupConversation.module.css";

interface SetupConversationProps {
  projectId: string;
  events?: HarnessEvent[];
  onApproved: () => void | Promise<void>;
  onExit: () => void;
  onSetupChanged?: (state: SetupStateDocument) => void | Promise<void>;
}

type WorkspaceView = "conversation" | "document";
type DetailsView = "review" | "ledger";

const setupErrorCopy: Record<string, string> = {
  "Book direction is already approved.": "全书方向已经批准，不能再写入候选讨论。",
  "Book direction candidate is stale; review the latest candidate.": "这个候选版本已经过期，请处理最新候选。",
  "Book direction must be synthesized and reviewed before approval.": "全书方向必须先整理为候选并完成审阅。",
  "Book direction review has blocking issues.": "候选方向仍有阻断问题，暂时不能批准。",
  "Book direction candidate does not preserve every confirmed decision.": "候选方向没有完整保留全部已确认决定，请继续讨论并重新审阅。",
  "Book discussion state changed while the model was working; discard the stale result.": "模型工作期间讨论状态已经变化，本次过期结果已丢弃，请载入最新状态后继续。",
  "Discuss the novel direction before requesting a review.": "请先讨论小说方向并形成草稿，再请求审阅。",
  "Confirm the formal book title before requesting a review.": "请先完成全书讨论中的最后一个正式书名问题。",
  "Book Agent has not marked the direction ready for review.": "Book Agent 仍有问题需要确认，暂时不能进入审阅。",
  "Select an enabled LLM profile before continuing the book discussion.": "请先在设置中选择一个可用的 LLM 配置。",
  "Book title contains configured provider credentials or endpoint data. Choose a different title.": "书名中包含 Provider 凭据或接口地址，请换一个书名。",
  "The current Book Direction candidate has already been reviewed. Approve it or continue the discussion before preparing another candidate.": "当前候选已经审阅。请批准它，或继续讨论使它失效后再整理新候选。"
};

export function SetupConversation({ projectId, events = [], onApproved, onExit, onSetupChanged }: SetupConversationProps) {
  const draftStorageKey = `novelpilot:book-direction-input:${projectId}`;
  const [state, setState] = useState<SetupStateDocument | null>(null);
  const [input, setInput] = useState(() => readLocalDraft(draftStorageKey));
  const [selectedSuggestionId, setSelectedSuggestionId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busyAction, setBusyAction] = useState<BusyAction>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [activeView, setActiveView] = useState<WorkspaceView>("conversation");
  const [detailsView, setDetailsView] = useState<DetailsView>("ledger");
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [changeSummary, setChangeSummary] = useState<SetupChangeSummary | null>(null);
  const [streamStartIndex, setStreamStartIndex] = useState(0);
  const historyTriggerRef = useRef<HTMLElement | null>(null);

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
    if (state?.candidate) {
      setActiveView("conversation");
      setDetailsView("review");
    }
  }, [state?.candidate?.revision]);

  const candidate = state?.candidate ?? null;
  const finalTitle = state?.selected_title?.trim() ?? "";
  const blockingIssueCount = candidate?.review.issues.filter((issue) => issue.severity === "blocking").length ?? 0;
  const approvalAllowed = Boolean(
    candidate && candidate.review.status === "passed" && blockingIssueCount === 0
  );
  const canSend = Boolean(input.trim()) && busyAction === null && !state?.approved;
  const canReview = Boolean(state?.direction_draft.trim())
    && Boolean(finalTitle)
    && state?.readiness.status === "ready"
    && candidate === null
    && busyAction === null
    && !state?.approved;
  const canContinueRevision = Boolean(candidate && !approvalAllowed && busyAction === null && !state?.approved);
  const canApprove = approvalAllowed && Boolean(finalTitle) && !input.trim() && busyAction === null;
  const streamedCharacterCount = useMemo(
    () => events.slice(streamStartIndex).reduce((latest, event) => {
      const value = event.payload.received_characters;
      return event.kind === "llm_stream_progress" && typeof value === "number" ? value : latest;
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
    const submittedSuggestionId = selectedSuggestionId;
    setStreamStartIndex(events.length);
    setBusyAction("turn");
    setNotice(null);
    setInput("");
    setSelectedSuggestionId(null);
    writeLocalDraft(draftStorageKey, "");
    try {
      const nextState = submittedSuggestionId
        ? await api.continueSetupDiscussion(message, submittedSuggestionId)
        : await api.continueSetupDiscussion(message);
      setChangeSummary(summarizeSetupChanges(previousState, nextState));
      await applyState(nextState);
      setActiveView("conversation");
      setNotice({ kind: "success", text: "本轮讨论已合并进 Book Direction。" });
    } catch (error) {
      const currentDraft = readLocalDraft(draftStorageKey);
      if (!currentDraft.trim()) {
        writeLocalDraft(draftStorageKey, message);
        setInput(message);
        setSelectedSuggestionId(submittedSuggestionId);
      }
      setNotice({ kind: "error", text: `${formatSetupError(error)} 当前输入尚未提交，可以直接重试。` });
    } finally {
      setBusyAction(null);
    }
  }

  function useSuggestion(suggestion: SetupSuggestion) {
    setInput(suggestion.message);
    setSelectedSuggestionId(suggestion.id);
  }

  function changeInput(value: string) {
    setInput(value);
    const selected = state?.suggestions.find(
      (suggestion) => suggestion.id === selectedSuggestionId,
    );
    if (selected && selected.message !== value) {
      setSelectedSuggestionId(null);
    }
  }

  async function prepareReview() {
    if (!canReview && !canContinueRevision) return;
    setStreamStartIndex(events.length);
    setBusyAction("review");
    setNotice(null);
    try {
      const nextState = await api.prepareSetupReview();
      await applyState(nextState);
      setActiveView("conversation");
      setNotice({
        kind: nextState.candidate?.review.status === "passed" ? "success" : nextState.question ? "success" : "error",
        text: nextState.candidate?.review.status === "passed"
          ? "候选全书方向已完成独立审查，等待你的明确批准。"
          : nextState.question
            ? "审查需要你的一个明确决定，Book Agent 已返回逐问讨论。"
            : "本轮有限自动修订仍未通过。你可以查看证据后手动继续修订。"
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
      setNotice({ kind: "success", text: `${formatBookTitle(finalTitle)}与全书方向已正式批准并提交。` });
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

      <Tabs
        value={activeView}
        onValueChange={(value) => setActiveView(value as WorkspaceView)}
        className={styles.tabsWorkspace}
      >
        <div className={styles.viewToolbar}>
          <TabsList aria-label="全书共创主视图">
            <TabsTrigger value="conversation"><MessageSquareText size={15} />对话</TabsTrigger>
            <TabsTrigger value="document"><BookOpenText size={15} />文稿</TabsTrigger>
          </TabsList>
          <Button variant="ghost" size="sm" onClick={() => setDetailsOpen(true)}>
            <PanelRightOpen size={16} />
            查看详情
            {blockingIssueCount > 0 && <Badge tone="danger">{blockingIssueCount}</Badge>}
          </Button>
        </div>

        {busyAction && busyAction !== "approve" && (
          <div className={styles.streamProgress} role="status" aria-live="polite">
            <span />
            {streamedCharacterCount
              ? `模型正在流式生成，已接收 ${streamedCharacterCount.toLocaleString()} 个字符`
              : "正在连接模型流..."}
          </div>
        )}

        <TabsContent value="conversation" className={styles.viewPanel}>
          {state.approved ? (
            <section className={styles.approvedHandoff}>
              <span><Check size={24} /></span>
              <p>全书方向已批准</p>
              <h2>{state.approved_title ? formatBookTitle(state.approved_title) : "规划已经完成"}</h2>
              <small>后续只滚动规划当前故事弧，不会覆盖已批准的最高层方向。</small>
              <Button variant="primary" size="lg" onClick={() => void onApproved()}><ShieldCheck size={17} />进入创作工作台</Button>
            </section>
          ) : candidate ? (
            <SetupReview
              messages={state.messages}
              candidate={candidate}
              selectedTitle={finalTitle}
              input={input}
              busyAction={busyAction}
              notice={notice}
              approvalAllowed={approvalAllowed}
              canSend={canSend}
              canApprove={canApprove}
              onInputChange={setInput}
              onSend={() => void sendMessage()}
              onApprove={() => void approve()}
              onContinueRevision={() => void prepareReview()}
            />
          ) : (
            <SetupDiscussion
              state={state}
              input={input}
              busyAction={busyAction}
              notice={notice}
              canSend={canSend}
              canReview={canReview}
              onInputChange={changeInput}
              onUseSuggestion={useSuggestion}
              onSend={() => void sendMessage()}
              onReview={() => void prepareReview()}
            />
          )}
        </TabsContent>

        <TabsContent value="document" className={`${styles.viewPanel} ${styles.documentPanel}`}>
          <BookDirectionDocument
            markdown={documentMarkdown}
            revision={documentRevision}
            mode={candidate ? "candidate" : "draft"}
            changeSummary={changeSummary}
          />
        </TabsContent>
      </Tabs>

      <Sheet
        open={detailsOpen}
        onOpenChange={setDetailsOpen}
        title="方向详情"
        description="审查证据、滚动契约与持续维护的方向账本。"
      >
        <Tabs
          value={candidate ? detailsView : "ledger"}
          onValueChange={(value) => setDetailsView(value as DetailsView)}
          className={styles.detailsTabs}
        >
          <TabsList className={styles.detailsTabList} aria-label="方向详情视图">
            {candidate && <TabsTrigger value="review">审查详情</TabsTrigger>}
            <TabsTrigger value="ledger">方向账本</TabsTrigger>
          </TabsList>
          {candidate && (
            <TabsContent value="review" className={styles.detailsPanel}>
              <SetupReviewDetails candidate={candidate} />
            </TabsContent>
          )}
          <TabsContent value="ledger" className={styles.detailsPanel}>
            <DirectionLedger state={state} />
          </TabsContent>
        </Tabs>
      </Sheet>

      <Dialog open={historyOpen} title="全书共创讨论记录" onClose={closeHistory}>
        <div className={styles.historyList}>
          <ConversationTranscript messages={state.messages} emptyText="还没有讨论记录。" />
        </div>
      </Dialog>

      {busyAction === "review" && (
        <div className={styles.busyOverlay}>
          <FileCheck2 size={21} />
          <strong>正在综合与审查全书方向</strong>
          <span>候选仍未进入正式设定。</span>
        </div>
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
