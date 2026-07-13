import { useQuery, useQueryClient } from "@tanstack/react-query";
import { X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, formatApiError } from "../../api/client";
import { AppShell } from "../../app/AppShell";
import { useHarnessEvents } from "../../app/harness-events";
import type { TaskDomain } from "../../app/types";
import { useWorkspaceQueries, workspaceQueryKeys } from "../../app/workspace-queries";
import { harnessVisibleOutputForLatestAction } from "../../types/domain";
import { formatRunStatus } from "../../types/display";
import type { LlmProfilesDocument, ProjectSummary } from "../../types/domain";
import { LlmProfilesPanel } from "../llm-profiles/LlmProfilesPanel";
import { SetupConversation } from "../setup-conversation/SetupConversation";
import { FeedbackComposer } from "../workbench/FeedbackComposer";
import { WorkbenchView } from "../workbench/WorkbenchView";
import { CanonView } from "./CanonView";
import { StoryArcsView } from "./StoryArcsView";
import { TraceConsole } from "./TraceConsole";
import { type CanonKind, parseCanonDocument } from "./workspace-utils";

interface WorkspaceProps {
  project: ProjectSummary;
  onProjectClosed: () => void;
}

type WorkspaceLocation = TaskDomain | "settings";
type WorkspaceCommand = "start" | "resume" | "pause" | "export" | "approve" | "retry" | "recover" | "revision";
type WorkspaceNotice = { kind: "success" | "error"; text: string };
type StoryTab = "arcs" | "chapters" | "canon";

function initialLocation(project: ProjectSummary): WorkspaceLocation {
  try {
    const stored = window.sessionStorage.getItem(`novelpilot.location.${project.metadata.project_id}`);
    if (["cocreate", "workbench", "story", "evidence", "settings"].includes(stored ?? "")) {
      return stored as WorkspaceLocation;
    }
  } catch {
    // Use the project-derived location when session storage is unavailable.
  }
  return project.title ? "workbench" : "cocreate";
}

export function Workspace({ project, onProjectClosed }: WorkspaceProps) {
  const projectId = project.metadata.project_id;
  const queryClient = useQueryClient();
  const queries = useWorkspaceQueries(projectId);
  const events = useHarnessEvents(projectId);
  const [location, setLocation] = useState<WorkspaceLocation>(() => initialLocation(project));
  const [storyTab, setStoryTab] = useState<StoryTab>("arcs");
  const [feedback, setFeedback] = useState("");
  const [feedbackNotice, setFeedbackNotice] = useState<WorkspaceNotice | null>(null);
  const [sendingFeedback, setSendingFeedback] = useState(false);
  const [workspaceNotice, setWorkspaceNotice] = useState<WorkspaceNotice | null>(null);
  const [pendingCommands, setPendingCommands] = useState<Set<WorkspaceCommand>>(() => new Set());
  const [selectedArtifactPath, setSelectedArtifactPath] = useState<string | null>(null);
  const [artifactDrawerOpen, setArtifactDrawerOpen] = useState(false);

  const projectState = queries.activeProject.data ?? project;
  const metadata = projectState.metadata;
  const currentArc = queries.currentArc.data ?? null;
  const readiness = queries.readiness.data ?? null;
  const profiles = queries.profiles.data ?? null;
  const artifactPaths = queries.artifactPaths.data ?? [];
  const artifactSummaries = queries.artifactSummaries.data ?? [];
  const completionAudit = queries.completionAudit.data ?? null;
  const activeArtifactQuery = useQuery({
    queryKey: workspaceQueryKeys.artifact(projectId, selectedArtifactPath),
    queryFn: () => api.artifactContent(selectedArtifactPath ?? ""),
    enabled: Boolean(selectedArtifactPath)
  });
  const activeArtifact = activeArtifactQuery.data ?? null;
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
      Object.entries(queries.canonContents).map(([kind, content]) => [kind, Object.keys(parseCanonDocument(content).items).length])
    ) as Record<CanonKind, number>,
    [queries.canonContents]
  );
  const retryableChapterArtifact = useMemo(() => {
    if (!metadata.active_chapter_id) return false;
    const prefix = `chapters/${metadata.active_chapter_id}/`;
    return artifactSummaries.some(
      (summary) => summary.path.startsWith(prefix) &&
        ((summary.kind === "verification" && summary.status === "failed") || summary.kind === "state_patch_rejection")
    );
  }, [artifactSummaries, metadata.active_chapter_id]);

  useEffect(() => {
    try { window.sessionStorage.setItem(`novelpilot.location.${projectId}`, location); } catch { /* no-op */ }
  }, [location, projectId]);

  useEffect(() => {
    if (queries.setup.data && !queries.setup.data.approved) setLocation("cocreate");
  }, [queries.setup.data]);

  useEffect(() => {
    const latest = events.at(-1);
    if (latest?.artifact_path) setSelectedArtifactPath(latest.artifact_path);
  }, [events]);

  async function refreshWorkspace() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.project(projectId) }),
      queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.profiles() })
    ]);
  }

  async function runCommand(command: WorkspaceCommand, action: () => Promise<void>): Promise<boolean> {
    setPendingCommands((current) => new Set(current).add(command));
    setWorkspaceNotice(null);
    try {
      await action();
      await refreshWorkspace();
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

  async function startRun() { await runCommand("start", async () => { await api.startRun(); }); }
  async function resumeRun() { await runCommand("resume", async () => { await api.resumeRun(); }); }

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
    if (!window.confirm("仅在确认没有仍在执行的 Harness 请求时恢复。恢复后项目会停在已暂停状态。")) return;
    await runCommand("recover", async () => { await api.recoverStaleRun(); });
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
      queryClient.removeQueries({ queryKey: workspaceQueryKeys.project(projectId) });
      onProjectClosed();
    } catch (error) {
      setWorkspaceNotice({ kind: "error", text: formatApiError(error) });
    }
  }

  function handleProfilesChanged(nextProfiles: LlmProfilesDocument) {
    queryClient.setQueryData(workspaceQueryKeys.profiles(), nextProfiles);
    void refreshWorkspace();
  }

  function openArtifact(path: string) {
    setSelectedArtifactPath(path);
    setArtifactDrawerOpen(true);
  }

  function renderWorkbench() {
    return (
      <WorkbenchView
        project={projectState}
        events={events}
        currentArc={currentArc}
        summaries={artifactSummaries}
        modelOutput={modelOutput}
        activeArtifact={activeArtifact}
        canonCounts={canonCounts}
        readiness={readiness}
        canStart={canStart}
        canResume={canResume}
        busy={commandBusy}
        onStart={startRun}
        onResume={resumeRun}
        onExport={exportManuscript}
        onSelectArtifact={openArtifact}
        onOpenEvidence={() => setLocation("evidence")}
        onOpenStory={() => setLocation("story")}
      />
    );
  }

  function renderStoryWorld() {
    return (
      <section className="story-world-domain">
        <header className="domain-heading">
          <div><h1>故事世界</h1><p>滚动故事弧、章节进度与已提交正史。</p></div>
          <nav className="domain-tabs">
            {(["arcs", "chapters", "canon"] as StoryTab[]).map((tab) => (
              <button key={tab} className={storyTab === tab ? "active" : ""} onClick={() => setStoryTab(tab)}>
                {{ arcs: "故事弧", chapters: "章节", canon: "正史" }[tab]}
              </button>
            ))}
          </nav>
        </header>
        {storyTab === "canon" ? (
          <CanonView contents={queries.canonContents} summaries={artifactSummaries} onSelectArtifact={openArtifact} onRefresh={refreshWorkspace} />
        ) : (
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
        )}
      </section>
    );
  }

  function renderContent() {
    switch (location) {
      case "cocreate":
        return (
          <SetupConversation
            key={projectId}
            projectId={projectId}
            onSetupChanged={refreshWorkspace}
            onExit={() => setLocation("workbench")}
            onApproved={async () => { await refreshWorkspace(); setLocation("workbench"); }}
          />
        );
      case "workbench":
        return renderWorkbench();
      case "story":
        return renderStoryWorld();
      case "evidence":
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
            onRefreshAudit={async () => { await queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.completion(projectId) }); }}
          />
        );
      case "settings":
        return <LlmProfilesPanel onProfilesChanged={handleProfilesChanged} />;
    }
  }

  const notice = workspaceNotice ? (
    <div className={`workspace-notice ${workspaceNotice.kind}`}>
      <span>{workspaceNotice.text}</span>
      <button title="关闭" onClick={() => setWorkspaceNotice(null)}><X size={15} /></button>
    </div>
  ) : null;

  const feedbackDock = location === "workbench" ? (
    <FeedbackComposer
      value={feedback}
      sending={sendingFeedback}
      onChange={setFeedback}
      onSend={() => void sendFeedback()}
    />
  ) : null;

  return (
    <>
      <AppShell
        project={projectState}
        location={location}
        profile={currentProfile}
        canRecover={Boolean(canRecover)}
        runInFlight={runInFlight}
        notice={notice}
        feedbackDock={feedbackDock}
        onLocationChange={setLocation}
        onRefresh={() => void refreshWorkspace()}
        onRecover={() => void recoverStaleRun()}
        onCloseProject={() => void closeProject()}
      >
        {renderContent()}
      </AppShell>

      {feedbackNotice && (
        <div className={`feedback-toast ${feedbackNotice.kind}`}>
          <span>{feedbackNotice.text}</span>
          <button title="关闭" onClick={() => setFeedbackNotice(null)}><X size={14} /></button>
        </div>
      )}

      {artifactDrawerOpen && (
        <div className="artifact-drawer-backdrop" onMouseDown={() => setArtifactDrawerOpen(false)}>
          <aside className="artifact-drawer" onMouseDown={(event) => event.stopPropagation()}>
            <header>
              <div><span>产物预览</span><strong>{selectedArtifactPath}</strong></div>
              <button title="关闭预览" onClick={() => setArtifactDrawerOpen(false)}><X size={18} /></button>
            </header>
            <pre>{activeArtifactQuery.isLoading ? "正在读取产物..." : activeArtifact?.content ?? "没有可显示的产物。"}</pre>
          </aside>
        </div>
      )}
    </>
  );
}
