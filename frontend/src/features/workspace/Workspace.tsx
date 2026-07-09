import {
  BookOpen,
  CheckCircle2,
  CirclePause,
  CirclePlay,
  FileDown,
  FileText,
  PanelRightOpen,
  RefreshCcw,
  RotateCw,
  Send,
  ShieldAlert,
  ShieldCheck,
  X
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, formatApiError } from "../../api/client";
import { harnessVisibleOutputForLatestAction, isHarnessEvent } from "../../types/domain";
import {
  formatArtifactTitle,
  formatAtomicAction,
  formatEventKind,
  formatEventMessage,
  formatEventStatusLine,
  formatGateMessage,
  formatGenericStatus,
  formatLiteraryDecision,
  formatLoopLayer,
  formatOperationMode,
  formatOptionalId,
  formatRunStatus
} from "../../types/display";
import { HarnessPanel } from "../harness-panel/HarnessPanel";
import { LlmProfilesPanel } from "../llm-profiles/LlmProfilesPanel";
import { SetupConversation } from "../setup-conversation/SetupConversation";
import type {
  ArtifactSummary,
  CurrentArcState,
  HarnessEvent,
  LiteraryReviewDecision,
  LlmProfilesDocument,
  ProjectCompletionAudit,
  ProjectReadiness,
  ProjectSummary
} from "../../types/domain";

interface WorkspaceProps {
  project: ProjectSummary;
  onProjectClosed: () => void;
}

type WorkspaceCommand =
  | "start"
  | "resume"
  | "pause"
  | "export"
  | "approve"
  | "retry"
  | "recover";
type WorkspaceNotice = { kind: "success" | "error"; text: string };

export function Workspace({ project, onProjectClosed }: WorkspaceProps) {
  const [projectState, setProjectState] = useState<ProjectSummary>(project);
  const [events, setEvents] = useState<HarnessEvent[]>([]);
  const [feedback, setFeedback] = useState("");
  const [feedbackNotice, setFeedbackNotice] = useState<WorkspaceNotice | null>(null);
  const [literaryNotice, setLiteraryNotice] = useState<WorkspaceNotice | null>(null);
  const [sendingFeedback, setSendingFeedback] = useState(false);
  const [recordingReview, setRecordingReview] = useState(false);
  const [workspaceNotice, setWorkspaceNotice] = useState<WorkspaceNotice | null>(null);
  const [pendingCommands, setPendingCommands] = useState<Set<WorkspaceCommand>>(() => new Set());
  const [currentArc, setCurrentArc] = useState<CurrentArcState | null>(null);
  const [artifactPaths, setArtifactPaths] = useState<string[]>([]);
  const [artifactSummaries, setArtifactSummaries] = useState<ArtifactSummary[]>([]);
  const [selectedArtifactPath, setSelectedArtifactPath] = useState<string | null>(null);
  const [activeArtifact, setActiveArtifact] = useState<{ path: string; content: string } | null>(
    null
  );
  const [selectedProfileId, setSelectedProfileId] = useState<string | null>(
    project.metadata.active_profile_id
  );
  const [completionAudit, setCompletionAudit] = useState<ProjectCompletionAudit | null>(null);
  const [literaryDecision, setLiteraryDecision] =
    useState<LiteraryReviewDecision>("approved");
  const [literaryReviewer, setLiteraryReviewer] = useState("人工审查");
  const [chapterAssessment, setChapterAssessment] = useState("");
  const [statePatchAssessment, setStatePatchAssessment] = useState("");
  const [reviewNotes, setReviewNotes] = useState("");
  const [readiness, setReadiness] = useState<ProjectReadiness | null>(null);

  const latestEvent = events.at(-1);
  const latestStatusEvent =
    [...events].reverse().find((event) => event.kind !== "llm_output_delta") ?? null;
  const currentLoop = latestStatusEvent?.loop_layer ?? "system";
  const currentAction = latestStatusEvent?.atomic_action ?? "idle";
  const projectMetadata = projectState.metadata;
  const runStatus = projectMetadata.run_status;
  const activeArcId = currentArc?.arc_id ?? projectMetadata.active_arc_id;
  const activeChapterId = projectMetadata.active_chapter_id;
  const activeProfileId = selectedProfileId ?? projectMetadata.active_profile_id;
  const runInFlight = runStatus === "running" || runStatus === "pause_requested";
  const canStartOrResume = readiness?.can_start_run ?? false;
  const canStartRun = canStartOrResume && readiness?.next_action.id === "start_run";
  const canResumeRun = canStartOrResume && readiness?.next_action.id === "resume_run";
  const canPauseRun = runStatus === "running" && readiness?.next_action.id === "wait_for_safe_checkpoint";
  const canRecoverStaleRun = readiness?.next_action.id === "recover_stale_run";
  const isCommandPending = (command: WorkspaceCommand) => pendingCommands.has(command);
  const liveSmokeGate =
    completionAudit?.gates.find((gate) => gate.id === "live_provider_smoke") ?? null;
  const liveSmokePassed = liveSmokeGate?.status === "passed";
  const literaryReviewReady =
    liveSmokePassed &&
    Boolean(
      literaryReviewer.trim() &&
        chapterAssessment.trim() &&
        statePatchAssessment.trim()
    );
  const literaryReviewBlocker =
    completionAudit === null || liveSmokePassed
      ? null
      : formatGateMessage(liveSmokeGate?.message ?? "Live provider smoke has not passed.");
  const retryableChapterArtifact = useMemo(() => {
    const chapterId = projectMetadata.active_chapter_id;
    if (!chapterId) {
      return false;
    }
    const chapterPrefix = `chapters/${chapterId}/`;
    return artifactSummaries.some(
      (summary) =>
        summary.path.startsWith(chapterPrefix) &&
        ((summary.kind === "verification" && summary.status === "failed") ||
          summary.kind === "state_patch_rejection")
    );
  }, [artifactSummaries, projectMetadata.active_chapter_id]);

  const refreshWorkspaceState = useCallback(async () => {
    try {
      const [activeProject, arc, profiles] = await Promise.all([
        api.activeProject(),
        api.currentArc(),
        api.profiles()
      ]);
      if (activeProject) {
        setProjectState(activeProject);
      }
      setCurrentArc(arc);
      setSelectedProfileId(profiles.active_profile_id);
    } catch (error) {
      setWorkspaceNotice({ kind: "error", text: formatApiError(error) });
    }
  }, []);

  const refreshArtifacts = useCallback(async () => {
    try {
      const [paths, summaries] = await Promise.all([api.listArtifacts(), api.artifactSummaries()]);
      setArtifactPaths(paths);
      setArtifactSummaries(summaries);
    } catch (error) {
      setWorkspaceNotice({ kind: "error", text: formatApiError(error) });
    }
  }, []);

  const refreshReadiness = useCallback(async () => {
    try {
      setReadiness(await api.readiness());
    } catch (error) {
      setReadiness(null);
      setWorkspaceNotice({ kind: "error", text: formatApiError(error) });
    }
  }, []);

  const refreshCompletionAudit = useCallback(async () => {
    try {
      setCompletionAudit(await api.completionAudit());
    } catch (error) {
      setCompletionAudit(null);
      setWorkspaceNotice({ kind: "error", text: formatApiError(error) });
    }
  }, []);

  const refreshInspection = useCallback(async () => {
    await Promise.all([refreshArtifacts(), refreshCompletionAudit()]);
  }, [refreshArtifacts, refreshCompletionAudit]);

  useEffect(() => {
    setProjectState(project);
    setEvents([]);
    setCurrentArc(null);
    setArtifactPaths([]);
    setArtifactSummaries([]);
    setSelectedArtifactPath(null);
    setActiveArtifact(null);
    setCompletionAudit(null);
    setReadiness(null);
    setPendingCommands(new Set());
    setSelectedProfileId(project.metadata.active_profile_id);
    setFeedbackNotice(null);
    setLiteraryNotice(null);
    setWorkspaceNotice(null);
  }, [project]);

  useEffect(() => {
    if (!latestEvent?.artifact_path) {
      return;
    }
    setSelectedArtifactPath(latestEvent.artifact_path);
    void refreshInspection();
  }, [latestEvent?.artifact_path, refreshInspection]);

  useEffect(() => {
    const artifactPath = selectedArtifactPath;
    if (!artifactPath) {
      setActiveArtifact(null);
      return;
    }

    let cancelled = false;
    api
      .artifactContent(artifactPath)
      .then((result) => {
        if (!cancelled) {
          setActiveArtifact(result);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setActiveArtifact(null);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [selectedArtifactPath]);

  function handleProfilesChanged(profiles: LlmProfilesDocument) {
    setSelectedProfileId(profiles.active_profile_id);
    void refreshWorkspaceState();
    void refreshReadiness();
  }

  useEffect(() => {
    const source = new EventSource("/api/runs/events");
    const handleHarnessEvent = (event: MessageEvent<string>) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(event.data);
      } catch {
        return;
      }
      if (!isHarnessEvent(parsed)) {
        return;
      }
      setEvents((current) => {
        if (current.some((existing) => existing.event_id === parsed.event_id)) {
          return current;
        }
        return [...current, parsed];
      });
    };
    source.onmessage = handleHarnessEvent;
    source.addEventListener("harness_event", (event) => {
      handleHarnessEvent(event as MessageEvent<string>);
    });
    source.addEventListener("stream_ready", () => undefined);
    return () => source.close();
  }, [project.metadata.project_id]);

  useEffect(() => {
    refreshWorkspaceState();
  }, [project.metadata.project_id, latestEvent?.event_id, refreshWorkspaceState]);

  useEffect(() => {
    void refreshReadiness();
  }, [project.metadata.project_id, latestEvent?.event_id, refreshReadiness]);

  useEffect(() => {
    void refreshArtifacts();
  }, [project.metadata.project_id, refreshArtifacts]);

  useEffect(() => {
    void refreshCompletionAudit();
  }, [project.metadata.project_id, latestEvent?.event_id, refreshCompletionAudit]);

  const eventRows = useMemo(
    () =>
      events
        .filter((event) => event.kind !== "llm_output_delta")
        .slice(-20)
        .reverse(),
    [events]
  );
  const modelOutput = useMemo(() => harnessVisibleOutputForLatestAction(events), [events]);
  const arcNeedsApproval =
    currentArc?.human_review === "awaiting_review" ||
    latestStatusEvent?.kind === "story_arc_review_required";
  const navigationArtifacts = useMemo(
    () =>
      artifactSummaries
        .filter((summary) =>
          [
            "context_snapshot",
            "candidate_observations",
            "verification",
            "candidate_state_patch",
            "committed_state_patch",
            "state_patch_rejection",
            "retry_manifest",
            "review",
            "draft",
            "final",
            "arc_plan",
            "arc_revision",
            "book_feedback",
            "export"
          ].includes(summary.kind)
        )
        .sort((left, right) => left.path.localeCompare(right.path))
        .slice(-24),
    [artifactSummaries]
  );

  async function closeProject() {
    setWorkspaceNotice(null);
    try {
      await api.closeProject();
      onProjectClosed();
    } catch (error) {
      setWorkspaceNotice({ kind: "error", text: formatApiError(error) });
    }
  }

  async function runCommand(
    command: WorkspaceCommand,
    action: () => Promise<void>
  ): Promise<boolean> {
    setPendingCommands((current) => new Set(current).add(command));
    setWorkspaceNotice(null);
    try {
      await action();
      await Promise.all([refreshWorkspaceState(), refreshReadiness()]);
      return true;
    } catch (error) {
      setWorkspaceNotice({ kind: "error", text: formatApiError(error) });
      return false;
    } finally {
      setPendingCommands((current) => {
        const next = new Set(current);
        next.delete(command);
        return next;
      });
    }
  }

  async function startRun() {
    await runCommand("start", async () => {
      await api.startRun();
    });
  }

  async function resumeRun() {
    await runCommand("resume", async () => {
      await api.resumeRun();
    });
  }

  async function pauseRun() {
    let pauseStatus = "";
    const paused = await runCommand("pause", async () => {
      const result = await api.pauseRun();
      pauseStatus = result.status;
    });
    if (paused) {
      setFeedbackNotice({
        kind: "success",
        text:
          pauseStatus === "pause_requested"
            ? "已请求暂停，将在当前原子动作结束后生效。"
            : `当前没有可暂停的 harness 动作：${formatRunStatus(pauseStatus)}。`
      });
    }
  }

  async function exportManuscript() {
    let artifactPath = "";
    const exported = await runCommand("export", async () => {
      const result = await api.exportManuscript();
      artifactPath = result.artifact_path;
    });
    if (exported) {
      setFeedbackNotice({ kind: "success", text: `已导出：${artifactPath}` });
    }
  }

  async function approveArc() {
    const approved = await runCommand("approve", async () => {
      await api.approveCurrentArc();
    });
    if (approved) {
      setFeedbackNotice({ kind: "success", text: "故事弧已批准。" });
    }
  }

  async function retryCurrentChapter() {
    let artifactPath = "";
    const retried = await runCommand("retry", async () => {
      const result = await api.retryCurrentChapter();
      artifactPath = result.artifact_path;
    });
    if (retried) {
      setFeedbackNotice({ kind: "success", text: `已准备重试：${artifactPath}` });
      await refreshArtifacts();
    }
  }

  async function recoverStaleRun() {
    if (!canRecoverStaleRun) {
      return;
    }
    const confirmed = window.confirm(
      "仅在确认没有仍在执行的 harness 请求时恢复。恢复后项目会停在 paused，可从已提交状态继续。"
    );
    if (!confirmed) {
      return;
    }

    let previousStatus = "";
    const recovered = await runCommand("recover", async () => {
      const result = await api.recoverStaleRun();
      previousStatus = result.previous_status;
    });
    if (recovered) {
      setFeedbackNotice({
        kind: "success",
        text: `已恢复陈旧运行锁：${formatRunStatus(previousStatus)} -> ${formatRunStatus("paused")}。`
      });
    }
  }

  async function sendFeedback() {
    if (!feedback.trim() || sendingFeedback) return;
    setSendingFeedback(true);
    setFeedbackNotice(null);
    try {
      await api.submitFeedback(feedback.trim());
      setFeedback("");
      setFeedbackNotice({ kind: "success", text: "已记录，会在下一个安全检查点处理。" });
    } catch (error) {
      setFeedbackNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setSendingFeedback(false);
    }
  }

  function updateLiteraryDecision(value: string) {
    if (value === "approved" || value === "rejected") {
      setLiteraryDecision(value);
    }
  }

  async function recordLiteraryReview() {
    if (
      recordingReview ||
      !literaryReviewReady
    ) {
      if (!recordingReview && literaryReviewBlocker) {
        setLiteraryNotice({ kind: "error", text: literaryReviewBlocker });
      }
      return;
    }

    setRecordingReview(true);
    setLiteraryNotice(null);
    try {
      const record = await api.recordLiteraryReview({
        decision: literaryDecision,
        reviewer: literaryReviewer.trim(),
        chapter_assessment: chapterAssessment.trim(),
        state_patch_assessment: statePatchAssessment.trim(),
        notes: reviewNotes.trim()
      });
      setLiteraryNotice({
        kind: "success",
        text: `已记录：${record.literary_review_json}`
      });
      await refreshInspection();
    } catch (error) {
      setLiteraryNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setRecordingReview(false);
    }
  }

  return (
    <main className="workspace">
      <header className="workspace-topbar">
        <div>
          <p className="eyebrow">当前小说</p>
          <h1>{projectState.title}</h1>
        </div>
        <div className="status-cluster">
          <span>
            <small>模式</small>
            {formatOperationMode(projectMetadata.operation_mode)}
          </span>
          <span>
            <small>运行</small>
            {formatRunStatus(runStatus)}
          </span>
          <span>
            <small>配置</small>
            {formatOptionalId(activeProfileId)}
          </span>
          <span>
            <small>故事弧</small>
            {formatOptionalId(activeArcId)}
          </span>
          <span>
            <small>章节</small>
            {formatOptionalId(activeChapterId)}
          </span>
          <span>
            <small>流程</small>
            {formatLoopLayer(currentLoop)}
          </span>
          <span>
            <small>动作</small>
            {formatAtomicAction(currentAction)}
          </span>
        </div>
        <div className="toolbar">
          <button
            title="启动"
            disabled={isCommandPending("start") || runInFlight || !canStartRun}
            onClick={startRun}
          >
            <CirclePlay size={18} />
          </button>
          <button
            title="继续"
            disabled={isCommandPending("resume") || runInFlight || !canResumeRun}
            onClick={resumeRun}
          >
            <RotateCw size={18} />
          </button>
          <button
            title="暂停"
            disabled={isCommandPending("pause") || !canPauseRun}
            onClick={pauseRun}
          >
            <CirclePause size={18} />
          </button>
          <button
            title="恢复卡住的运行"
            disabled={isCommandPending("recover") || !canRecoverStaleRun}
            onClick={recoverStaleRun}
          >
            <ShieldAlert size={18} />
          </button>
          <button
            title="导出全书"
            disabled={isCommandPending("export")}
            onClick={exportManuscript}
          >
            <FileDown size={18} />
          </button>
          <button
            title="重试当前章节"
            disabled={
              isCommandPending("retry") ||
              runInFlight ||
              projectMetadata.active_chapter_id === null ||
              !retryableChapterArtifact
            }
            onClick={retryCurrentChapter}
          >
            <RefreshCcw size={18} />
          </button>
          <button title="关闭项目" disabled={runInFlight} onClick={closeProject}>
            <X size={18} />
          </button>
        </div>
      </header>

      <section className="workspace-grid">
        <aside className="left-rail">
          <div className="panel-title">
            <BookOpen size={18} />
            <span>流程层级</span>
          </div>
          <nav className="loop-nav">
            <button className={currentLoop === "book" ? "active" : ""}>全书流程</button>
            <button className={currentLoop === "story_arc" ? "active" : ""}>故事弧流程</button>
            <button className={currentLoop === "chapter" ? "active" : ""}>章节流程</button>
          </nav>
          {currentArc && (
            <div className="arc-progress">
              <p className="eyebrow">当前故事弧</p>
              <strong>{currentArc.arc_id}</strong>
              <span>{formatGenericStatus(currentArc.status)}</span>
              <progress
                max={currentArc.target_chapter_count}
                value={currentArc.completed_chapter_ids.length}
              />
              <small>
                {currentArc.completed_chapter_ids.length} / {currentArc.target_chapter_count} 章
              </small>
            </div>
          )}
          <div className="artifact-nav-block">
            <div className="panel-title compact">
              <FileText size={18} />
              <span>项目文件</span>
            </div>
            <div className="artifact-nav">
              {navigationArtifacts.map((summary) => (
                <button
                  key={summary.path}
                  className={summary.path === selectedArtifactPath ? "active" : ""}
                  onClick={() => setSelectedArtifactPath(summary.path)}
                >
                  <span>{formatArtifactTitle(summary)}</span>
                  <small>{summary.path}</small>
                  <em>{formatGenericStatus(summary.status)}</em>
                </button>
              ))}
              {navigationArtifacts.length === 0 && <p className="muted">还没有产物。</p>}
            </div>
          </div>
          <LlmProfilesPanel onProfilesChanged={handleProfilesChanged} />
        </aside>

        <section className="center-stage">
          <SetupConversation
            projectId={projectMetadata.project_id}
            onSetupChanged={() => {
              void refreshReadiness();
              void refreshWorkspaceState();
            }}
          />
          {workspaceNotice && (
            <p className={`notice-banner ${workspaceNotice.kind}`}>{workspaceNotice.text}</p>
          )}
          {arcNeedsApproval && (
            <div className="arc-approval-card">
              <div>
                <p className="eyebrow">故事弧审查</p>
                <h2>{formatOptionalId(activeArcId)}</h2>
                <p>{currentArc?.plan_path ?? "当前故事弧计划正在等待批准。"}</p>
              </div>
              <button
                className="primary-button"
                disabled={isCommandPending("approve")}
                onClick={approveArc}
              >
                <CheckCircle2 size={18} />
                批准故事弧
              </button>
            </div>
          )}
          <div className="stream-card">
            <div className="panel-title">
              <PanelRightOpen size={18} />
              <span>可见输出</span>
            </div>
            {modelOutput && (
              <div className="model-output">
                <p className="eyebrow">模型输出</p>
                <pre>{modelOutput}</pre>
              </div>
            )}
            {activeArtifact && (
              <div className="active-output">
                <div>
                  <p className="eyebrow">当前产物</p>
                  <strong>{activeArtifact.path}</strong>
                </div>
                <pre>{activeArtifact.content}</pre>
              </div>
            )}
            <div className="event-stream">
              {eventRows.map((event) => (
                <article key={event.event_id}>
                  <strong>{formatEventKind(event.kind)}</strong>
                  <small>{formatEventStatusLine(event)}</small>
                  <p>{formatEventMessage(event.message)}</p>
                </article>
              ))}
            </div>
          </div>
          <div className="literary-review-card">
            <div className="panel-title">
              <ShieldCheck size={18} />
              <span>文学审查</span>
            </div>
            <div className="review-form-grid">
              <select
                value={literaryDecision}
                disabled={recordingReview}
                onChange={(event) => updateLiteraryDecision(event.target.value)}
              >
                <option value="approved">{formatLiteraryDecision("approved")}</option>
                <option value="rejected">{formatLiteraryDecision("rejected")}</option>
              </select>
              <input
                value={literaryReviewer}
                disabled={recordingReview}
                onChange={(event) => setLiteraryReviewer(event.target.value)}
                placeholder="审查人"
              />
            </div>
            <textarea
              value={chapterAssessment}
              disabled={recordingReview}
              onChange={(event) => setChapterAssessment(event.target.value)}
              placeholder="章节正文评价"
            />
            <textarea
              value={statePatchAssessment}
              disabled={recordingReview}
              onChange={(event) => setStatePatchAssessment(event.target.value)}
              placeholder="状态补丁评价"
            />
            <textarea
              value={reviewNotes}
              disabled={recordingReview}
              onChange={(event) => setReviewNotes(event.target.value)}
              placeholder="补充记录"
            />
            <button
              className="primary-button"
              disabled={
                recordingReview || !literaryReviewReady
              }
              onClick={recordLiteraryReview}
            >
              <ShieldCheck size={18} />
              记录审查
            </button>
            {literaryReviewBlocker && !literaryNotice && (
              <p className="feedback-status error">{literaryReviewBlocker}</p>
            )}
            {literaryNotice && (
              <p className={`feedback-status ${literaryNotice.kind}`}>{literaryNotice.text}</p>
            )}
          </div>
          <div className="feedback-box">
            <input
              value={feedback}
              disabled={sendingFeedback}
              onChange={(event) => setFeedback(event.target.value)}
              placeholder="给下一个安全检查点的反馈"
            />
            <button
              title="提交反馈"
              disabled={sendingFeedback || !feedback.trim()}
              onClick={sendFeedback}
            >
              <Send size={18} />
            </button>
          </div>
          {feedbackNotice && (
            <p className={`feedback-status ${feedbackNotice.kind}`}>{feedbackNotice.text}</p>
          )}
        </section>

        <HarnessPanel
          events={events}
          artifacts={artifactPaths}
          summaries={artifactSummaries}
          selectedArtifactPath={selectedArtifactPath}
          artifactContent={activeArtifact?.content ?? ""}
          readiness={readiness}
          completionAudit={completionAudit}
          onSelectArtifact={setSelectedArtifactPath}
          onRefreshArtifacts={refreshInspection}
        />
      </section>
    </main>
  );
}
