import {
  BookOpen,
  Boxes,
  Columns3,
  Feather,
  GitBranch,
  Home,
  RefreshCcw,
  Send,
  Settings2,
  ShieldCheck,
  X
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { api, apiUrl, formatApiError } from "../../api/client";
import { harnessVisibleOutputForLatestAction, isHarnessEvent } from "../../types/domain";
import {
  formatLoopLayer,
  formatOperationMode,
  formatOptionalId,
  formatProjectTitle,
  formatRunStatus
} from "../../types/display";
import { LlmProfilesPanel } from "../llm-profiles/LlmProfilesPanel";
import { SetupConversation } from "../setup-conversation/SetupConversation";
import type {
  ArtifactSummary,
  CurrentArcState,
  HarnessEvent,
  LlmProfilesDocument,
  OperationMode,
  ProjectCompletionAudit,
  ProjectReadiness,
  ProjectSummary
} from "../../types/domain";
import { CanonView } from "./CanonView";
import { CockpitView } from "./CockpitView";
import { ProjectOverview } from "./ProjectOverview";
import { StoryArcsView } from "./StoryArcsView";
import { TraceConsole } from "./TraceConsole";
import { canonFiles, type CanonKind, parseCanonDocument } from "./workspace-utils";

interface WorkspaceProps {
  project: ProjectSummary;
  onProjectClosed: () => void;
}

type WorkspaceView = "overview" | "plan" | "cockpit" | "arcs" | "canon" | "trace" | "settings";
type WorkspaceCommand = "start" | "resume" | "pause" | "export" | "approve" | "retry" | "recover" | "revision" | "mode";
type WorkspaceNotice = { kind: "success" | "error"; text: string };

const viewLabels: Record<WorkspaceView, string> = {
  overview: "项目概览",
  plan: "开书规划",
  cockpit: "创作工作台",
  arcs: "故事弧与章节",
  canon: "正史状态",
  trace: "运行证据与产物",
  settings: "设置与模型"
};

const navItems: Array<{ id: WorkspaceView; label: string; icon: typeof Home }> = [
  { id: "overview", label: "项目概览", icon: Home },
  { id: "plan", label: "开书规划", icon: BookOpen },
  { id: "cockpit", label: "创作工作台", icon: Columns3 },
  { id: "arcs", label: "故事弧与章节", icon: Boxes },
  { id: "canon", label: "正史状态", icon: ShieldCheck },
  { id: "trace", label: "运行证据与产物", icon: GitBranch },
  { id: "settings", label: "设置与模型", icon: Settings2 }
];

const emptyCanonContents: Record<CanonKind, string> = {
  characters: "{\"schema_version\":1,\"version\":1,\"items\":{}}",
  relationships: "{\"schema_version\":1,\"version\":1,\"items\":{}}",
  world_facts: "{\"schema_version\":1,\"version\":1,\"items\":{}}",
  foreshadowing: "{\"schema_version\":1,\"version\":1,\"items\":{}}"
};

export function Workspace({ project, onProjectClosed }: WorkspaceProps) {
  const [activeView, setActiveView] = useState<WorkspaceView>(
    project.metadata.active_arc_id ? "cockpit" : "plan"
  );
  const [projectState, setProjectState] = useState<ProjectSummary>(project);
  const [events, setEvents] = useState<HarnessEvent[]>([]);
  const [feedback, setFeedback] = useState("");
  const [feedbackNotice, setFeedbackNotice] = useState<WorkspaceNotice | null>(null);
  const [sendingFeedback, setSendingFeedback] = useState(false);
  const [workspaceNotice, setWorkspaceNotice] = useState<WorkspaceNotice | null>(null);
  const [pendingCommands, setPendingCommands] = useState<Set<WorkspaceCommand>>(() => new Set());
  const [currentArc, setCurrentArc] = useState<CurrentArcState | null>(null);
  const [artifactPaths, setArtifactPaths] = useState<string[]>([]);
  const [artifactSummaries, setArtifactSummaries] = useState<ArtifactSummary[]>([]);
  const [selectedArtifactPath, setSelectedArtifactPath] = useState<string | null>(null);
  const [activeArtifact, setActiveArtifact] = useState<{ path: string; content: string } | null>(null);
  const [artifactDrawerOpen, setArtifactDrawerOpen] = useState(false);
  const [profiles, setProfiles] = useState<LlmProfilesDocument | null>(null);
  const [completionAudit, setCompletionAudit] = useState<ProjectCompletionAudit | null>(null);
  const [readiness, setReadiness] = useState<ProjectReadiness | null>(null);
  const [canonContents, setCanonContents] = useState<Record<CanonKind, string>>(emptyCanonContents);

  const latestEvent = events.at(-1) ?? null;
  const latestStatusEvent = [...events].reverse().find((event) => event.kind !== "llm_output_delta") ?? null;
  const metadata = projectState.metadata;
  const runStatus = metadata.run_status;
  const runInFlight = runStatus === "running" || runStatus === "pause_requested";
  const canStart = Boolean(readiness?.can_start_run && readiness.next_action.id === "start_run");
  const canResume = Boolean(readiness?.can_start_run && readiness.next_action.id === "resume_run");
  const canPause = runStatus === "running";
  const canRecover = readiness?.next_action.id === "recover_stale_run";
  const commandBusy = pendingCommands.size > 0;
  const currentProfile = profiles?.profiles.find((profile) => profile.id === profiles.active_profile_id) ?? null;
  const modelOutput = useMemo(() => harnessVisibleOutputForLatestAction(events), [events]);
  const canonCounts = useMemo(
    () => Object.fromEntries(
      Object.entries(canonContents).map(([kind, content]) => [kind, Object.keys(parseCanonDocument(content).items).length])
    ) as Record<CanonKind, number>,
    [canonContents]
  );
  const retryableChapterArtifact = useMemo(() => {
    if (!metadata.active_chapter_id) return false;
    const prefix = `chapters/${metadata.active_chapter_id}/`;
    return artifactSummaries.some(
      (summary) => summary.path.startsWith(prefix) &&
        ((summary.kind === "verification" && summary.status === "failed") || summary.kind === "state_patch_rejection")
    );
  }, [artifactSummaries, metadata.active_chapter_id]);

  const refreshWorkspaceState = useCallback(async () => {
    try {
      const [activeProject, arc, nextProfiles] = await Promise.all([
        api.activeProject(),
        api.currentArc(),
        api.profiles()
      ]);
      if (activeProject) setProjectState(activeProject);
      setCurrentArc(arc);
      setProfiles(nextProfiles);
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

  const refreshCanon = useCallback(async () => {
    const entries = await Promise.all(
      (Object.entries(canonFiles) as Array<[CanonKind, string]>).map(async ([kind, path]) => {
        try {
          const artifact = await api.artifactContent(path);
          return [kind, artifact.content] as const;
        } catch {
          return [kind, emptyCanonContents[kind]] as const;
        }
      })
    );
    setCanonContents(Object.fromEntries(entries) as Record<CanonKind, string>);
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
    await Promise.all([refreshArtifacts(), refreshCanon(), refreshCompletionAudit()]);
  }, [refreshArtifacts, refreshCanon, refreshCompletionAudit]);

  useEffect(() => {
    setProjectState(project);
    setEvents([]);
    setCurrentArc(null);
    setArtifactPaths([]);
    setArtifactSummaries([]);
    setSelectedArtifactPath(null);
    setActiveArtifact(null);
    setArtifactDrawerOpen(false);
    setCompletionAudit(null);
    setReadiness(null);
    setProfiles(null);
    setPendingCommands(new Set());
    setFeedbackNotice(null);
    setWorkspaceNotice(null);
    setCanonContents(emptyCanonContents);
    setActiveView(project.metadata.active_arc_id ? "cockpit" : "plan");

    let cancelled = false;
    api.setupState().then((setup) => {
      if (!cancelled && setup.approved && !project.metadata.active_arc_id) setActiveView("overview");
    }).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [project]);

  useEffect(() => {
    const source = new EventSource(apiUrl("/api/runs/events"));
    const handleHarnessEvent = (message: MessageEvent<string>) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(message.data);
      } catch {
        return;
      }
      if (!isHarnessEvent(parsed)) return;
      setEvents((current) => current.some((event) => event.event_id === parsed.event_id) ? current : [...current, parsed]);
    };
    source.onmessage = handleHarnessEvent;
    source.addEventListener("harness_event", (event) => handleHarnessEvent(event as MessageEvent<string>));
    source.addEventListener("stream_ready", () => undefined);
    return () => source.close();
  }, [project.metadata.project_id]);

  useEffect(() => {
    void Promise.all([refreshWorkspaceState(), refreshReadiness(), refreshArtifacts(), refreshCanon(), refreshCompletionAudit()]);
  }, [project.metadata.project_id, refreshArtifacts, refreshCanon, refreshCompletionAudit, refreshReadiness, refreshWorkspaceState]);

  useEffect(() => {
    if (!latestStatusEvent?.event_id) return;
    void Promise.all([refreshWorkspaceState(), refreshReadiness(), refreshInspection()]);
  }, [latestStatusEvent?.event_id, refreshInspection, refreshReadiness, refreshWorkspaceState]);

  useEffect(() => {
    if (latestEvent?.artifact_path) setSelectedArtifactPath(latestEvent.artifact_path);
  }, [latestEvent?.artifact_path]);

  useEffect(() => {
    if (!selectedArtifactPath) {
      setActiveArtifact(null);
      return;
    }
    let cancelled = false;
    api.artifactContent(selectedArtifactPath)
      .then((artifact) => {
        if (!cancelled) setActiveArtifact(artifact);
      })
      .catch(() => {
        if (!cancelled) setActiveArtifact(null);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedArtifactPath]);

  async function runCommand(command: WorkspaceCommand, action: () => Promise<void>): Promise<boolean> {
    setPendingCommands((current) => new Set(current).add(command));
    setWorkspaceNotice(null);
    try {
      await action();
      await Promise.all([refreshWorkspaceState(), refreshReadiness(), refreshInspection()]);
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
    await runCommand("start", async () => { await api.startRun(); });
  }

  async function resumeRun() {
    await runCommand("resume", async () => { await api.resumeRun(); });
  }

  async function pauseRun() {
    let status = "";
    const ok = await runCommand("pause", async () => { status = (await api.pauseRun()).status; });
    if (ok) setFeedbackNotice({ kind: "success", text: status === "pause_requested" ? "已请求暂停，将在当前原子动作结束后生效。" : `当前状态：${formatRunStatus(status)}。` });
  }

  async function exportManuscript() {
    let path = "";
    const ok = await runCommand("export", async () => { path = (await api.exportManuscript()).artifact_path; });
    if (ok) setFeedbackNotice({ kind: "success", text: `已导出：${path}` });
  }

  async function approveArc(): Promise<boolean> {
    const ok = await runCommand("approve", async () => { await api.approveCurrentArc(); });
    if (ok) setFeedbackNotice({ kind: "success", text: "当前故事弧已批准，可以继续章节写作。" });
    return ok;
  }

  async function requestArcRevision(message: string): Promise<boolean> {
    const ok = await runCommand("revision", async () => {
      await api.submitFeedback(`请修改当前 arc plan 与 pacing：${message}`);
      await api.resumeRun();
    });
    if (ok) setFeedbackNotice({ kind: "success", text: "故事弧修改意见已处理。" });
    return ok;
  }

  async function retryCurrentChapter() {
    let path = "";
    const ok = await runCommand("retry", async () => { path = (await api.retryCurrentChapter()).artifact_path; });
    if (ok) setFeedbackNotice({ kind: "success", text: `已准备重试：${path}` });
  }

  async function recoverStaleRun() {
    if (!canRecover) return;
    const confirmed = window.confirm("仅在确认没有仍在执行的 harness 请求时恢复。恢复后项目会停在 paused。");
    if (!confirmed) return;
    await runCommand("recover", async () => { await api.recoverStaleRun(); });
  }

  async function changeOperationMode(nextMode: OperationMode) {
    if (nextMode === metadata.operation_mode) return;
    if (runInFlight) {
      setWorkspaceNotice({
        kind: "error",
        text: runStatus === "pause_requested"
          ? "正在等待安全暂停，暂停完成后才能更换创作模式。"
          : "运行中不能更换创作模式，请先请求暂停并等待安全检查点。"
      });
      return;
    }
    if (
      metadata.operation_mode === "participatory"
      && nextMode === "full_auto"
      && metadata.active_arc_id !== null
      && currentArc === null
    ) {
      setWorkspaceNotice({
        kind: "error",
        text: "暂时无法确认当前故事弧的审批状态。请刷新或恢复故事弧状态后再切换到全自动模式。"
      });
      return;
    }

    const ok = await runCommand("mode", async () => {
      setProjectState(await api.updateProjectMode(nextMode));
    });
    if (ok) {
      setWorkspaceNotice({
        kind: "success",
        text: `已切换为${formatOperationMode(nextMode)}，已有创作内容和审批记录保持不变。`
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
      setFeedbackNotice({ kind: "success", text: "意见已记录，会在当前原子动作结束后的安全检查点注入。" });
    } catch (error) {
      setFeedbackNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setSendingFeedback(false);
    }
  }

  async function closeProject() {
    setWorkspaceNotice(null);
    try {
      await api.closeProject();
      onProjectClosed();
    } catch (error) {
      setWorkspaceNotice({ kind: "error", text: formatApiError(error) });
    }
  }

  function handleProfilesChanged(nextProfiles: LlmProfilesDocument) {
    setProfiles(nextProfiles);
    void Promise.all([refreshWorkspaceState(), refreshReadiness()]);
  }

  function openArtifact(path: string) {
    setSelectedArtifactPath(path);
    setArtifactDrawerOpen(true);
  }

  function renderView() {
    switch (activeView) {
      case "overview":
        return (
          <ProjectOverview
            project={projectState}
            currentArc={currentArc}
            readiness={readiness}
            summaries={artifactSummaries}
            canonCounts={canonCounts}
            canStart={canStart}
            canResume={canResume}
            busy={commandBusy}
            modeChanging={pendingCommands.has("mode")}
            onStart={startRun}
            onResume={resumeRun}
            onExport={exportManuscript}
            onModeChange={changeOperationMode}
            onNavigate={setActiveView}
            onSelectArtifact={openArtifact}
          />
        );
      case "plan":
        return (
          <SetupConversation
            key={metadata.project_id}
            projectId={metadata.project_id}
            onSetupChanged={() => { void Promise.all([refreshReadiness(), refreshWorkspaceState(), refreshArtifacts()]); }}
            onExit={() => setActiveView("overview")}
            onApproved={async () => {
              await Promise.all([refreshWorkspaceState(), refreshReadiness(), refreshArtifacts()]);
              setActiveView("cockpit");
            }}
          />
        );
      case "cockpit":
        return (
          <CockpitView
            project={projectState}
            events={events}
            currentArc={currentArc}
            summaries={artifactSummaries}
            modelOutput={modelOutput}
            activeArtifact={activeArtifact}
            canonCounts={canonCounts}
            onSelectArtifact={openArtifact}
            onOpenTrace={() => setActiveView("trace")}
            onOpenCanon={() => setActiveView("canon")}
          />
        );
      case "arcs":
        return (
          <StoryArcsView
            currentArc={currentArc}
            activeChapterId={metadata.active_chapter_id}
            artifactPaths={artifactPaths}
            summaries={artifactSummaries}
            approving={pendingCommands.has("approve")}
            onApprove={approveArc}
            onRequestRevision={requestArcRevision}
            onSelectArtifact={openArtifact}
          />
        );
      case "canon":
        return (
          <CanonView
            contents={canonContents}
            summaries={artifactSummaries}
            onSelectArtifact={openArtifact}
            onRefresh={refreshInspection}
          />
        );
      case "trace":
        return (
          <TraceConsole
            events={events}
            summaries={artifactSummaries}
            artifactPaths={artifactPaths}
            selectedArtifactPath={selectedArtifactPath}
            activeArtifact={activeArtifact}
            readiness={readiness}
            completionAudit={completionAudit}
            canPause={canPause}
            canResume={canResume}
            canRetry={Boolean(metadata.active_chapter_id && retryableChapterArtifact && !runInFlight)}
            busy={commandBusy}
            onSelectArtifact={setSelectedArtifactPath}
            onPause={pauseRun}
            onResume={resumeRun}
            onRetry={retryCurrentChapter}
            onRefreshAudit={refreshCompletionAudit}
          />
        );
      case "settings":
        return <LlmProfilesPanel onProfilesChanged={handleProfilesChanged} />;
    }
  }

  return (
    <main className="novelpilot-app">
      <aside className="app-sidebar">
        <header className="sidebar-brand"><Feather size={21} /><strong>NovelPilot</strong></header>
        <nav>
          {navItems.map((item) => {
            const Icon = item.icon;
            return (
              <button key={item.id} aria-label={item.label} className={activeView === item.id ? "active" : ""} onClick={() => setActiveView(item.id)}>
                <Icon size={18} /> <span>{item.label}</span>
              </button>
            );
          })}
        </nav>
        <footer>
          <span>当前书籍</span>
          <button className="current-book-button" title={formatProjectTitle(projectState.title)} onClick={() => setActiveView("overview")}>
            <span className="book-gem" />
            <strong>{formatProjectTitle(projectState.title)}</strong>
            <small>⌄</small>
          </button>
          <div className="sidebar-utilities">
            <button title="刷新工作区" onClick={() => void Promise.all([refreshWorkspaceState(), refreshReadiness(), refreshInspection()])}><RefreshCcw size={16} /></button>
            <button title="恢复卡住的运行" disabled={!canRecover} onClick={() => void recoverStaleRun()}><ShieldCheck size={16} /></button>
            <button title="关闭项目" disabled={runInFlight} onClick={() => void closeProject()}><X size={16} /></button>
          </div>
        </footer>
      </aside>

      <section className="app-workspace">
        <header className="app-topbar">
          <div className="provider-status">
            <span>{currentProfile?.name ?? formatOptionalId(profiles?.active_profile_id)}</span>
            <small>/ {currentProfile?.model ?? "未选择模型"}</small>
            <i className={currentProfile?.has_api_key ? "ready" : ""} />
          </div>
          <div className="project-location">
            <strong>《{formatProjectTitle(projectState.title)}》</strong>
            <span>/ {viewLabels[activeView]}</span>
          </div>
          <div className="topbar-statuses">
            <span className="soft-badge amber">{formatOperationMode(metadata.operation_mode)}</span>
            {latestStatusEvent && <span className="soft-badge">{formatLoopLayer(latestStatusEvent.loop_layer)}</span>}
            <span className={`soft-badge run-${runStatus}`}><span className={`status-dot ${runStatus}`} />{formatRunStatus(runStatus)}</span>
          </div>
        </header>

        {workspaceNotice && (
          <div className={`workspace-notice ${workspaceNotice.kind}`}>
            <span>{workspaceNotice.text}</span>
            <button title="关闭" onClick={() => setWorkspaceNotice(null)}><X size={15} /></button>
          </div>
        )}

        <div className={`workspace-view view-${activeView}`}>{renderView()}</div>

        {activeView === "cockpit" && (
          <footer className="feedback-dock">
            <label>
              <input value={feedback} disabled={sendingFeedback} onChange={(event) => setFeedback(event.target.value)} onKeyDown={(event) => event.key === "Enter" && void sendFeedback()} placeholder="告诉 NovelPilot 需要如何纠偏..." />
            </label>
            <span className="feedback-scope">范围：<strong>模型自动判断</strong></span>
            <div className="feedback-suggestions">
              {["节奏太快", "增加伏笔", "强化动机"].map((suggestion) => <button key={suggestion} onClick={() => setFeedback(suggestion)}>{suggestion}</button>)}
            </div>
            <button className="send-feedback-button" title="提交反馈" disabled={sendingFeedback || !feedback.trim()} onClick={() => void sendFeedback()}><Send size={18} /></button>
          </footer>
        )}

        {feedbackNotice && (
          <div className={`feedback-toast ${feedbackNotice.kind}`}>
            <span>{feedbackNotice.text}</span>
            <button title="关闭" onClick={() => setFeedbackNotice(null)}><X size={14} /></button>
          </div>
        )}
      </section>

      {artifactDrawerOpen && (
        <div className="artifact-drawer-backdrop" onMouseDown={() => setArtifactDrawerOpen(false)}>
          <aside className="artifact-drawer" onMouseDown={(event) => event.stopPropagation()}>
            <header>
              <div><span>产物预览</span><strong>{selectedArtifactPath}</strong></div>
              <button title="关闭预览" onClick={() => setArtifactDrawerOpen(false)}><X size={18} /></button>
            </header>
            <pre>{activeArtifact?.content ?? "正在读取产物..."}</pre>
          </aside>
        </div>
      )}
    </main>
  );
}
