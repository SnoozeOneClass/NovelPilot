import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  BookOpen,
  Check,
  CircleAlert,
  Download,
  LoaderCircle,
  MessageSquareText,
  Pause,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Send,
  Settings2,
  Trash2
} from "lucide-react";
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { workspaceApi } from "./api/workspace-client";
import { ThemeToggle } from "./components/ui/ThemeToggle";
import type {
  ArcOutlineEntryView,
  BookArcContractView,
  CommandId,
  MutationResponse,
  OperationMode,
  ProjectStateView
} from "./types/workspace";
import { decodeAgentLiveEvent, reduceLiveProse } from "./types/workspace";
import styles from "./App.module.css";

const projectsKey = ["workspace-v2", "projects"] as const;
const profilesKey = ["workspace-v2", "profiles"] as const;

function projectKey(projectId: string) {
  return ["workspace-v2", "project", projectId] as const;
}

function idempotencyKey(action: string): string {
  return `${action}:${crypto.randomUUID()}`;
}

function readSelectedProject(): string | null {
  try {
    return window.localStorage.getItem("novelpilot.workspace.project-id");
  } catch {
    return null;
  }
}

function command(state: ProjectStateView, commandId: CommandId) {
  return state.commands.find((item) => item.command_id === commandId) ?? {
    command_id: commandId,
    enabled: false,
    reason: "后端没有公开此动作"
  };
}

function formatStatus(status: string): string {
  const labels: Record<string, string> = {
    waiting_for_user: "等待你的操作",
    running: "生成中",
    pause_requested: "正在安全暂停",
    paused: "已暂停",
    failure_paused: "失败后暂停",
    completed: "全书已完成"
  };
  return labels[status] ?? status;
}

export function App() {
  const queryClient = useQueryClient();
  const [selectedProjectId, setSelectedProjectId] = useState(readSelectedProject);
  const projects = useQuery({ queryKey: projectsKey, queryFn: workspaceApi.listProjects });
  const profiles = useQuery({ queryKey: profilesKey, queryFn: workspaceApi.profiles });
  const project = useQuery({
    queryKey: projectKey(selectedProjectId ?? "none"),
    queryFn: () => workspaceApi.getProject(selectedProjectId as string),
    enabled: selectedProjectId !== null,
    refetchInterval: 3_000
  });
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [liveProse, setLiveProse] = useState("");

  useEffect(() => {
    if (!selectedProjectId || !project.data) return;
    const source = new EventSource(
      workspaceApi.eventStreamUrl(selectedProjectId, project.data.latest_event_sequence)
    );
    source.addEventListener("domain_event", () => {
      void queryClient.invalidateQueries({ queryKey: projectKey(selectedProjectId) });
      void queryClient.invalidateQueries({ queryKey: projectsKey });
    });
    source.addEventListener("agent_live", (rawEvent) => {
      const event = rawEvent as MessageEvent<string>;
      const value = decodeAgentLiveEvent(event.data);
      if (value === null || value.project_id !== selectedProjectId) return;
      setLiveProse((current) => reduceLiveProse(current, value));
      if (value.kind === "task_succeeded" || value.kind === "task_failed") {
        void queryClient.invalidateQueries({ queryKey: projectKey(selectedProjectId) });
      }
    });
    return () => source.close();
  }, [project.data?.latest_event_sequence, queryClient, selectedProjectId]);

  function selectProject(projectId: string | null) {
    setSelectedProjectId(projectId);
    setLiveProse("");
    setNotice(null);
    try {
      if (projectId) window.localStorage.setItem("novelpilot.workspace.project-id", projectId);
      else window.localStorage.removeItem("novelpilot.workspace.project-id");
    } catch {
      // Client selection still works for this tab when storage is unavailable.
    }
  }

  async function runMutation(
    label: string,
    operation: () => Promise<MutationResponse>
  ) {
    setBusyAction(label);
    setNotice(null);
    try {
      const response = await operation();
      queryClient.setQueryData(projectKey(response.state.project.project_id), response.state);
      await queryClient.invalidateQueries({ queryKey: projectsKey });
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusyAction(null);
    }
  }

  if (!selectedProjectId) {
    return (
      <ProjectHome
        projects={projects.data ?? []}
        profiles={profiles.data?.profiles ?? []}
        selectedProfileId={profiles.data?.selected_profile_id ?? null}
        loading={projects.isLoading || profiles.isLoading}
        error={projects.error ?? profiles.error}
        onSelect={selectProject}
        onCreated={(response) => {
          queryClient.setQueryData(projectKey(response.state.project.project_id), response.state);
          void queryClient.invalidateQueries({ queryKey: projectsKey });
          selectProject(response.state.project.project_id);
        }}
      />
    );
  }

  if (project.isLoading) return <CenteredMessage icon={<LoaderCircle />} text="正在读取权威状态…" />;
  if (project.error || !project.data) {
    return (
      <CenteredMessage
        icon={<CircleAlert />}
        text={project.error instanceof Error ? project.error.message : "项目不存在"}
        actionLabel="返回项目列表"
        onAction={() => selectProject(null)}
      />
    );
  }

  return (
    <ProjectWorkspace
      state={project.data}
      liveProse={liveProse}
      busyAction={busyAction}
      notice={notice}
      onSwitch={() => selectProject(null)}
      onRefresh={() => void project.refetch()}
      onMutate={runMutation}
      onDelete={async () => {
        if (!window.confirm("删除该项目及其全部运行证据？此操作不可撤销。")) return;
        setBusyAction("delete");
        try {
          await workspaceApi.deleteProject(
            selectedProjectId,
            idempotencyKey("delete-project")
          );
          await queryClient.invalidateQueries({ queryKey: projectsKey });
          selectProject(null);
        } catch (error) {
          setNotice(error instanceof Error ? error.message : "删除失败");
        } finally {
          setBusyAction(null);
        }
      }}
      onNotice={setNotice}
    />
  );
}

interface ProjectHomeProps {
  projects: Awaited<ReturnType<typeof workspaceApi.listProjects>>;
  profiles: Awaited<ReturnType<typeof workspaceApi.profiles>>["profiles"];
  selectedProfileId: string | null;
  loading: boolean;
  error: Error | null;
  onSelect: (projectId: string) => void;
  onCreated: (response: MutationResponse) => void;
}

function ProjectHome({
  projects,
  profiles,
  selectedProfileId,
  loading,
  error,
  onSelect,
  onCreated
}: ProjectHomeProps) {
  const readyProfiles = profiles.filter((profile) => profile.capability_status === "ready");
  const initialProfile = readyProfiles.some((profile) => profile.id === selectedProfileId)
    ? selectedProfileId
    : readyProfiles[0]?.id ?? null;
  const [brief, setBrief] = useState("");
  const [mode, setMode] = useState<OperationMode>("full_auto");
  const [profileId, setProfileId] = useState<string | null>(initialProfile);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  useEffect(() => {
    if (profileId === null && initialProfile !== null) setProfileId(initialProfile);
  }, [initialProfile, profileId]);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (!brief.trim() || !profileId) return;
    setCreating(true);
    setCreateError(null);
    try {
      const response = await workspaceApi.createProject(
        {
          project_id: `novel-${crypto.randomUUID()}`,
          creator_brief: brief.trim(),
          operation_mode: mode,
          default_profile_id: profileId
        },
        idempotencyKey("create-project")
      );
      onCreated(response);
    } catch (caught) {
      setCreateError(caught instanceof Error ? caught.message : "创建失败");
    } finally {
      setCreating(false);
    }
  }

  return (
    <main className={styles.home}>
      <header className={styles.homeHeader}>
        <div><BookOpen size={26} /><strong>NovelPilot</strong></div>
        <ThemeToggle />
      </header>
      <section className={styles.hero}>
        <p>确定性小说生成 Harness</p>
        <h1>从全书规划到章节提交，所有状态都可恢复、可审阅。</h1>
        <span>项目选择只保存在当前浏览器；后端没有“当前项目”隐式状态。</span>
      </section>
      <div className={styles.homeGrid}>
        <section className={styles.panel}>
          <div className={styles.panelHeading}><h2>小说项目</h2><span>{projects.length} 本</span></div>
          {loading && <p className={styles.muted}>正在读取…</p>}
          {error && <p className={styles.error}>{error.message}</p>}
          <div className={styles.projectList}>
            {projects.map((item) => (
              <button key={item.project_id} onClick={() => onSelect(item.project_id)}>
                <div><strong>{item.title ?? "未命名小说"}</strong><span>{formatStatus(item.run_status)}</span></div>
                <small>{item.operation_mode === "full_auto" ? "全自动" : "参与模式"} · 已提交 {item.committed_chapter_count} 章</small>
              </button>
            ))}
            {!loading && projects.length === 0 && <p className={styles.muted}>还没有项目。</p>}
          </div>
        </section>
        <form className={styles.panel} onSubmit={create}>
          <div className={styles.panelHeading}><h2>创建新小说</h2><Plus size={18} /></div>
          <label>创作母本 / 初始要求<textarea rows={9} value={brief} onChange={(event) => setBrief(event.target.value)} placeholder="粘贴完整的创作要求。后续 Book Agent 会逐步与你确认。" /></label>
          <label>运行模式<select value={mode} onChange={(event) => setMode(event.target.value as OperationMode)}><option value="full_auto">全自动（Book 仍需批准）</option><option value="participatory">参与模式（Book + 每个故事弧批准）</option></select></label>
          <label>模型 Profile<select value={profileId ?? ""} onChange={(event) => setProfileId(event.target.value || null)}><option value="">请选择已验证 Profile</option>{readyProfiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.display_name} · {profile.model_id}</option>)}</select></label>
          {readyProfiles.length === 0 && <p className={styles.warning}>没有 capability-ready Profile。请先用本地配置命令完成验证。</p>}
          {createError && <p className={styles.error}>{createError}</p>}
          <button className={styles.primary} disabled={creating || !brief.trim() || !profileId}>{creating ? <LoaderCircle size={16} /> : <Plus size={16} />} 创建项目</button>
        </form>
      </div>
    </main>
  );
}

interface ProjectWorkspaceProps {
  state: ProjectStateView;
  liveProse: string;
  busyAction: string | null;
  notice: string | null;
  onSwitch: () => void;
  onRefresh: () => void;
  onMutate: (label: string, operation: () => Promise<MutationResponse>) => Promise<void>;
  onDelete: () => Promise<void>;
  onNotice: (notice: string | null) => void;
}

function ProjectWorkspace(props: ProjectWorkspaceProps) {
  const { state, liveProse, busyAction, notice, onSwitch, onRefresh, onMutate, onDelete, onNotice } = props;
  const projectId = state.project.project_id;
  const [message, setMessage] = useState("");
  const [feedback, setFeedback] = useState("");
  const [feedbackLayer, setFeedbackLayer] = useState<"book" | "arc" | "chapter">("book");
  const [mode, setMode] = useState<OperationMode>(state.project.operation_mode);
  const commands = useMemo(
    () => new Map(state.commands.map((item) => [item.command_id, item])),
    [state.commands]
  );

  useEffect(() => setMode(state.project.operation_mode), [state.project.operation_mode]);

  useEffect(() => {
    if (state.creator_input_request) {
      setFeedbackLayer(state.creator_input_request.route_layer);
    }
  }, [
    state.creator_input_request?.review_id,
    state.creator_input_request?.route_layer
  ]);

  function runControl(
    action: "start" | "pause" | "resume" | "retry" | "retry-action"
  ) {
    return onMutate(action, () => workspaceApi.runControl(
      projectId,
      action,
      state.run.lock_version,
      idempotencyKey(`run-${action}`)
    ));
  }

  async function sendBookMessage(value: string, suggestionId?: string) {
    await onMutate("book-input", () => workspaceApi.sendBookInput(
      projectId,
      {
        expected_workspace_lock_version: state.book.workspace_lock_version,
        message: value,
        suggestion_id: suggestionId
      },
      idempotencyKey("book-input")
    ));
    setMessage("");
  }

  return (
    <main className={styles.workspace}>
      <aside className={styles.sidebar}>
        <div className={styles.brand}><BookOpen size={22} /><strong>NovelPilot</strong></div>
        <div className={styles.projectIdentity}><span>当前小说</span><strong>{state.project.title ?? "未命名小说"}</strong><small>{projectId}</small></div>
        <nav>
          <a href="#control">运行控制</a><a href="#book">全书规划</a><a href="#arc">故事弧</a><a href="#chapter">章节</a><a href="#evidence">执行证据</a><a href="#settings">项目设置</a>
        </nav>
        <footer><ThemeToggle /><button onClick={onRefresh} title="刷新"><RefreshCw size={17} /></button><button onClick={onSwitch}>切换项目</button></footer>
      </aside>
      <section className={styles.mainColumn}>
        <header className={styles.topbar}>
          <div><span className={styles.statusDot} data-status={state.run.status} /><div><strong>{formatStatus(state.run.status)}</strong><small>{state.run.wait_reason_code ?? `Run #${state.run.run_number}`}</small></div></div>
          <div className={styles.topActions}>
            <ActionButton icon={<Play size={16} />} label="开始" item={command(state, "start_run")} busy={busyAction === "start"} onClick={() => void runControl("start")} />
            <ActionButton icon={<Pause size={16} />} label="暂停" item={command(state, "pause_run")} busy={busyAction === "pause"} onClick={() => void runControl("pause")} />
            <ActionButton icon={<Play size={16} />} label="继续" item={command(state, "resume_run")} busy={busyAction === "resume"} onClick={() => void runControl("resume")} />
            <ActionButton icon={<RotateCcw size={16} />} label="重试失败任务" item={command(state, "retry_failed_task")} busy={busyAction === "retry"} danger onClick={() => void runControl("retry")} />
            <ActionButton icon={<RotateCcw size={16} />} label="重试 Harness 动作" item={command(state, "retry_failed_action")} busy={busyAction === "retry-action"} danger onClick={() => void runControl("retry-action")} />
          </div>
        </header>
        {notice && <div className={styles.notice}><CircleAlert size={17} /><span>{notice}</span><button onClick={() => onNotice(null)}>×</button></div>}
        {state.run.status === "failure_paused" && <section className={styles.failure}><strong>流程已在失败边界暂停</strong><p>{state.run.failure_code ?? "未知错误"}。普通继续不会绕过失败来源，请使用当前启用的显式重试操作。</p></section>}

        <section id="control" className={styles.summaryGrid}>
          <SummaryCard label="正式章节" value={`${state.project.committed_chapter_count}`} detail={state.book.whole_book_scale_guidance ?? "等待 Book 基线"} />
          <SummaryCard label="故事弧" value={state.current_arc ? `Arc ${state.current_arc.ordinal}` : "—"} detail={state.book.arc_contract_count ? `${state.current_arc?.ordinal ?? 0} / ${state.book.arc_contract_count} · ${state.current_arc?.lifecycle_status ?? "尚未创建"}` : "等待 Book 拓扑"} />
          <SummaryCard label="当前章节" value={state.current_chapter ? `第 ${state.current_chapter.book_ordinal} 章` : "—"} detail={state.current_chapter?.workspace_state ?? "尚未创建"} />
          <SummaryCard label="模型" value={state.default_profile_id ?? "未配置"} detail={`${state.recent_tasks[0]?.model_id ?? "尚无执行"}`} />
        </section>

        <section id="book" className={styles.section}>
          <div className={styles.sectionHeading}><div><span>Book Loop</span><h2>全书规划与审批</h2></div><StatePill text={state.book.workspace_state} /></div>
          <div className={styles.transcript}>
            {state.book.transcript.messages.map((item) => <article key={item.sequence} data-role={item.role}><span>{item.role === "user" ? "你" : "Book Strategist"}</span><p>{item.content}</p></article>)}
          </div>
          {state.book.discussion.question && <div className={styles.question}><strong>{state.book.discussion.question}</strong><div>{state.book.discussion.suggestions.map((suggestion) => <button key={suggestion.id} disabled={!command(state, "send_book_input").enabled || busyAction !== null} onClick={() => void sendBookMessage(suggestion.message, suggestion.id)}><span>{suggestion.label}{suggestion.recommended && " · 推荐"}</span><small>{suggestion.rationale}</small></button>)}</div></div>}
          <form className={styles.composer} onSubmit={(event) => { event.preventDefault(); if (message.trim()) void sendBookMessage(message.trim()); }}><textarea rows={3} value={message} onChange={(event) => setMessage(event.target.value)} placeholder="补充你的决定或回答问题" disabled={!command(state, "send_book_input").enabled} /><button className={styles.primary} disabled={!message.trim() || !command(state, "send_book_input").enabled || busyAction !== null}><Send size={16} />发送</button></form>
          {state.book.arc_topology.length > 0 && <BookTopologyList contracts={state.book.arc_topology} scaleGuidance={state.book.whole_book_scale_guidance} />}
          <div className={styles.gateRow}><div><strong>Book 正式基线</strong><span>{state.book.pending_review_decision === "pass" ? "独立 Evaluator 已通过，等待你的批准" : state.book.current_baseline_id ? `Baseline v${state.book.baseline_version}` : "尚未形成可批准候选"}</span></div><ActionButton icon={<Check size={16} />} label="批准全书规划" item={command(state, "approve_book")} busy={busyAction === "approve-book"} primary onClick={() => void onMutate("approve-book", () => workspaceApi.approveBook(projectId, idempotencyKey("approve-book")))} /></div>
        </section>

        <section id="arc" className={styles.section}>
          <div className={styles.sectionHeading}><div><span>Arc Loop</span><h2>故事弧</h2></div><StatePill text={state.current_arc?.lifecycle_status ?? "未创建"} /></div>
          {state.current_arc ? <div className={styles.factGrid}><Fact label="Arc ID" value={state.current_arc.arc_id} /><Fact label="正式版本" value={state.current_arc.baseline_version ? `v${state.current_arc.baseline_version}` : "候选中"} /><Fact label="拓扑位置" value={`Arc ${state.current_arc.ordinal}${state.current_arc.is_final ? " · 最终弧" : ""}`} /><Fact label="全书累计进度" value={`${state.current_arc.cumulative_committed_chapter_count} / ${state.current_arc.closure_cumulative_chapter_count ?? "待批准"}`} /><Fact label="本弧已提交" value={String(state.current_arc.arc_committed_chapter_count)} /><Fact label="审阅" value={state.current_arc.pending_review_decision ?? "—"} /></div> : <p className={styles.muted}>Book 批准后由 Run Engine 创建首个 Story Arc。</p>}
          {state.current_arc?.outline && <ArcOutlineList entries={state.current_arc.outline.entries} />}
          {command(state, "approve_arc").enabled && <div className={styles.gateRow}><div><strong>Story Arc 正式基线</strong><span>章节收束检查点由已批准的完整大纲确定，不需要手工填写章号。</span></div><ActionButton icon={<Check size={16} />} label="批准当前故事弧" item={command(state, "approve_arc")} busy={busyAction === "approve-arc"} primary onClick={() => void onMutate("approve-arc", () => workspaceApi.approveArc(projectId, idempotencyKey("approve-arc")))} /></div>}
        </section>

        <section id="chapter" className={styles.section}>
          <div className={styles.sectionHeading}><div><span>Chapter Loop</span><h2>{state.current_chapter?.chapter_title ?? (state.current_chapter ? `第 ${state.current_chapter.book_ordinal} 章` : "章节生成")}</h2></div><StatePill text={state.current_chapter?.workspace_state ?? "未创建"} /></div>
          {state.current_chapter && <div className={styles.factGrid}><Fact label="计划" value={state.current_chapter.has_plan ? "完成" : "等待"} /><Fact label="正文" value={state.current_chapter.has_prose ? "完成" : "等待"} /><Fact label="观察" value={state.current_chapter.has_observations ? "完成" : "等待"} /><Fact label="Canon Patch" value={state.current_chapter.has_canon_patch ? "完成" : "等待"} /></div>}
          {liveProse && <div className={styles.liveDocument}><span>实时正文（刷新后不回放 token）</span><pre>{liveProse}</pre></div>}
        </section>

        <section className={styles.section}>
          <div className={styles.sectionHeading}><div><span>Change Route</span><h2>提交分层反馈</h2></div><MessageSquareText size={20} /></div>
          {state.creator_input_request && <div className={styles.question}><strong>{state.creator_input_request.question.question}</strong><p>{state.creator_input_request.question.controlled_fact}</p>{state.creator_input_request.question.evidence.map((item) => <small key={item}>{item}</small>)}</div>}
          <form className={styles.feedback} onSubmit={(event) => { event.preventDefault(); if (!feedback.trim()) return; void onMutate("feedback", () => workspaceApi.submitFeedback(projectId, { content: feedback.trim(), route_layer: feedbackLayer }, idempotencyKey("feedback"))).then(() => setFeedback("")); }}><select value={feedbackLayer} disabled={state.creator_input_request !== null} onChange={(event) => setFeedbackLayer(event.target.value as "book" | "arc" | "chapter")}><option value="book">影响全书方向</option><option value="arc" disabled={!state.current_arc}>影响当前故事弧</option><option value="chapter" disabled={!state.current_chapter}>仅影响当前章节</option></select><textarea rows={3} value={feedback} onChange={(event) => setFeedback(event.target.value)} placeholder={state.creator_input_request ? "回答上面的创作者问题。输入会在当前原子动作结束后注入。" : "描述需要修改的内容。Harness 会在安全边界按所选层级处理。"} /><button className={styles.primary} disabled={!feedback.trim() || !commands.get("submit_feedback")?.enabled}><Send size={16} />{state.creator_input_request ? "提交回答" : "提交反馈"}</button></form>
          {state.recent_feedback.length > 0 && <div className={styles.taskTable}><div className={styles.taskHead}><span>最近反馈</span><span>状态</span><span>生效边界</span><span>目标</span></div>{state.recent_feedback.slice(0, 10).map((item) => <div key={item.feedback_id}><span><strong>{item.feedback_kind === "correction_wait_response" ? "创作者回答" : "分层反馈"}</strong><small>{item.content}</small></span><span><StatePill text={item.status} />{item.dismiss_reason_code && <small className={styles.error}>{item.dismiss_reason_code}</small>}</span><span><small>{item.status === "routed" ? "已排队；当前原子动作不受影响" : item.status === "applied" ? "已由 Run Engine 在安全边界注入" : item.status === "dismissed" ? "未改变权威状态" : "等待路由"}</small></span><span>{item.route_layer ?? "—"}</span></div>)}</div>}
        </section>

        <section id="evidence" className={styles.section}>
          <div className={styles.sectionHeading}><div><span>Execution Evidence</span><h2>最近 Agent 任务</h2></div><span>Cursor {state.latest_event_sequence}</span></div>
          <div className={styles.taskTable}><div className={styles.taskHead}><span>任务</span><span>状态</span><span>请求 / 重试</span><span>Tokens</span></div>{state.recent_tasks.slice(0, 30).map((task) => <div key={task.task_id}><span><strong>{task.task_kind}</strong><small>{task.role} · {task.model_id}</small></span><span><StatePill text={task.attempt_status ?? task.task_status} />{task.error_code && <small className={styles.error}>{task.error_code}</small>}</span><span>{task.provider_request_count ?? 0} / {task.transport_retry_count ?? 0}</span><span>{task.input_tokens ?? 0} → {task.output_tokens ?? 0}</span></div>)}{state.recent_tasks.length === 0 && <p className={styles.muted}>还没有 Agent 执行证据。</p>}</div>
        </section>

        <section id="settings" className={styles.section}>
          <div className={styles.sectionHeading}><div><span>Project Settings</span><h2>项目设置</h2></div><Settings2 size={20} /></div>
          <div className={styles.settingsRow}><label>运行模式<select value={mode} onChange={(event) => setMode(event.target.value as OperationMode)}><option value="full_auto">全自动</option><option value="participatory">参与模式</option></select></label><button disabled={mode === state.project.operation_mode || busyAction !== null} onClick={() => void onMutate("settings", () => workspaceApi.updateSettings(projectId, { expected_lock_version: state.settings_lock_version, operation_mode: mode, default_profile_id: state.default_profile_id, book_profile_id: state.book_profile_id, arc_profile_id: state.arc_profile_id, chapter_profile_id: state.chapter_profile_id, evaluator_profile_id: state.evaluator_profile_id }, idempotencyKey("settings")))}>保存模式</button><button disabled={!command(state, "export_markdown").enabled} onClick={async () => { try { const result = await workspaceApi.exportManuscript(projectId); onNotice(`已导出：${result.path}`); } catch (error) { onNotice(error instanceof Error ? error.message : "导出失败"); } }}><Download size={16} />导出 Markdown</button><button className={styles.dangerButton} disabled={busyAction !== null} onClick={() => void onDelete()}><Trash2 size={16} />删除项目</button></div>
        </section>
      </section>
    </main>
  );
}

function ActionButton({ icon, label, item, busy, danger, primary, onClick }: { icon: React.ReactNode; label: string; item: { enabled: boolean; reason: string }; busy: boolean; danger?: boolean; primary?: boolean; onClick: () => void }) {
  return <button className={`${primary ? styles.primary : ""} ${danger ? styles.dangerButton : ""}`} disabled={!item.enabled || busy} title={item.reason} onClick={onClick}>{busy ? <LoaderCircle size={16} /> : icon}{label}</button>;
}

function SummaryCard({ label, value, detail }: { label: string; value: string; detail: string }) {
  return <article className={styles.summaryCard}><span>{label}</span><strong>{value}</strong><small>{detail}</small></article>;
}

function Fact({ label, value }: { label: string; value: string }) {
  return <div><span>{label}</span><strong>{value}</strong></div>;
}

function StatePill({ text }: { text: string }) {
  return <span className={styles.pill} data-state={text}>{text}</span>;
}

function BookTopologyList({
  contracts,
  scaleGuidance
}: {
  contracts: BookArcContractView[];
  scaleGuidance: string | null;
}) {
  return (
    <div className={styles.bookTopology}>
      <div>
        <strong>正式 Arc 拓扑</strong>
        <span>{scaleGuidance ?? "全书规模由内容完成度决定，章节数仅作参考。"}</span>
      </div>
      <ol>
        {contracts.map((contract) => (
          <li key={contract.ordinal} data-status={contract.lifecycle_status}>
            <div>
              <span>Arc {contract.ordinal}{contract.is_final ? " · 最终弧" : ""}</span>
              <StatePill text={contract.lifecycle_status} />
            </div>
            <strong>{contract.whole_book_role}</strong>
            <p>{contract.core_goal}</p>
            <small>进入：{contract.handoff_from_previous}</small>
            <small>退出：{contract.exit_conditions.join("；")}</small>
          </li>
        ))}
      </ol>
    </div>
  );
}

function ArcOutlineList({ entries }: { entries: ArcOutlineEntryView[] }) {
  if (entries.length === 0) {
    return <p className={styles.muted}>当前 Arc 已到达大纲边界，正在等待语义收束评估。</p>;
  }
  return (
    <ol className={styles.arcOutline}>
      {entries.map((entry) => (
        <li key={`${entry.arc_ordinal}:${entry.source_arc_baseline_id}`} data-status={entry.status}>
          <div className={styles.arcOutlineIndex}>
            <span>第 {entry.book_ordinal} 章</span>
            <StatePill text={entry.status} />
          </div>
          <div className={styles.arcOutlineBody}>
            <div>
              <strong>{entry.actual_chapter_title ?? entry.assignment.title}</strong>
              {entry.actual_chapter_title && entry.actual_chapter_title !== entry.assignment.title && (
                <small>原规划：{entry.assignment.title}</small>
              )}
            </div>
            <p>{entry.assignment.core_event}</p>
            <small>场景：{entry.assignment.scenes.join(" → ")}</small>
            <small>交接：{entry.assignment.hook}</small>
            <span>Arc v{entry.source_arc_baseline_version}</span>
          </div>
        </li>
      ))}
    </ol>
  );
}

function CenteredMessage({ icon, text, actionLabel, onAction }: { icon: React.ReactNode; text: string; actionLabel?: string; onAction?: () => void }) {
  return <main className={styles.centered}>{icon}<p>{text}</p>{actionLabel && <button className={styles.primary} onClick={onAction}>{actionLabel}</button>}</main>;
}
