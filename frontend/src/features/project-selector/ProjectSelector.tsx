import { ArrowLeft, ArrowRight, Bot, Clock3, Feather, FolderOpen, Plus, UserRound } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, formatApiError } from "../../api/client";
import { formatOperationMode, formatProjectTitle, formatRunStatus } from "../../types/display";
import type { OperationMode, ProjectSummary } from "../../types/domain";

interface ProjectSelectorProps {
  onProjectOpened: (project: ProjectSummary) => void;
}

type SelectorStep = "home" | "new-mode" | "continue-list" | "continue-mode";

export function ProjectSelector({ onProjectOpened }: ProjectSelectorProps) {
  const [step, setStep] = useState<SelectorStep>("home");
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [selectedProject, setSelectedProject] = useState<ProjectSummary | null>(null);
  const [mode, setMode] = useState<OperationMode>("full_auto");
  const [creating, setCreating] = useState(false);
  const [openingProject, setOpeningProject] = useState<string | null>(null);
  const [projectsLoading, setProjectsLoading] = useState(true);
  const [projectsLoadFailed, setProjectsLoadFailed] = useState(false);
  const [notice, setNotice] = useState<{ kind: "error"; text: string } | null>(null);
  const actionLockRef = useRef(false);
  const selectorBusy = creating || openingProject !== null;

  useEffect(() => {
    let cancelled = false;
    api
      .listProjects()
      .then((result) => {
        if (!cancelled) {
          setProjects(result);
          setProjectsLoadFailed(false);
          setNotice(null);
        }
      })
      .catch((error) => {
        if (!cancelled) {
          setProjectsLoadFailed(true);
          setNotice({ kind: "error", text: formatApiError(error) });
        }
      })
      .finally(() => {
        if (!cancelled) setProjectsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function goHome() {
    if (selectorBusy) return;
    setStep("home");
    setSelectedProject(null);
    setMode("full_auto");
    setNotice(null);
  }

  function startNewBookFlow() {
    setMode("full_auto");
    setNotice(null);
    setStep("new-mode");
  }

  function startContinueFlow() {
    setSelectedProject(null);
    setNotice(null);
    setStep("continue-list");
  }

  function chooseProject(project: ProjectSummary) {
    setSelectedProject(project);
    setMode(project.metadata.operation_mode);
    setNotice(null);
    setStep("continue-mode");
  }

  async function createProject() {
    if (actionLockRef.current) return;
    actionLockRef.current = true;
    setCreating(true);
    setNotice(null);
    try {
      onProjectOpened(await api.createProject(mode));
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      actionLockRef.current = false;
      setCreating(false);
    }
  }

  async function continueProject() {
    if (!selectedProject || actionLockRef.current) return;
    const projectToOpen = selectedProject;
    actionLockRef.current = true;
    setOpeningProject(projectToOpen.name);
    setNotice(null);
    try {
      const activeProject = await api.activeProject();
      let project = activeProject?.name === projectToOpen.name
        ? activeProject
        : await api.openProject(projectToOpen.name);
      if (project.metadata.operation_mode !== mode) {
        project = await api.updateProjectMode(mode);
      }
      onProjectOpened(project);
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      actionLockRef.current = false;
      setOpeningProject(null);
    }
  }

  return (
    <main className="project-selector-shell">
      <header className="selector-brand">
        <span><Feather size={22} /></span>
        <div><strong>NovelPilot</strong><small>本地 AI 长篇小说创作系统</small></div>
      </header>

      {notice && <p className="notice-banner error selector-notice">{notice.text}</p>}

      {step === "home" && (
        <div className="project-selector-grid selector-home-grid">
          <section className="np-surface selector-choice-panel">
            <span className="selector-choice-icon"><Plus size={24} /></span>
            <p className="eyebrow">开始新书</p>
            <h1>从全书方向开始构思一本新小说</h1>
            <p>先选择创作模式，再与 AI 讨论题材、人物和故事方向。现在不需要提前确定书名。</p>
            <button className="gold-button" disabled={selectorBusy} onClick={startNewBookFlow}>
              开始新书 <ArrowRight size={17} />
            </button>
          </section>

          <section className="np-surface selector-choice-panel">
            <span className="selector-choice-icon"><FolderOpen size={24} /></span>
            <p className="eyebrow">继续创作</p>
            <h1>恢复一本已有小说的创作进度</h1>
            <p>选择本地小说，并决定这一次继续使用原有模式，还是切换到另一种创作模式。</p>
            <button className="outline-button" disabled={selectorBusy} onClick={startContinueFlow}>
              选择已有小说 <ArrowRight size={17} />
            </button>
          </section>
        </div>
      )}

      {step === "new-mode" && (
        <div className="project-selector-grid selector-flow-grid">
          <ModePanel
            eyebrow="开始新书"
            title="选择这本新书的创作模式"
            description="项目将以“未命名新书”开始；正式书名会在全书方向讨论完成后确定。"
            mode={mode}
            disabled={selectorBusy}
            onModeChange={setMode}
            onBack={goHome}
            actionLabel={creating ? "正在创建未命名新书..." : "创建未命名新书"}
            onConfirm={() => void createProject()}
          />
          <FlowSummary kind="new" mode={mode} />
        </div>
      )}

      {step === "continue-list" && (
        <div className="project-selector-grid selector-list-grid">
          <section className="np-surface recent-projects-panel">
            <header className="view-heading compact-heading">
              <div>
                <button className="selector-back-button" disabled={selectorBusy} onClick={goHome}><ArrowLeft size={15} /> 返回首页</button>
                <p className="eyebrow">继续创作</p>
                <h2>选择已有小说</h2>
                <p>下一步可以保留或更换这次创作使用的模式。</p>
              </div>
            </header>
            <div className="project-list-modern">
              {projects.map((project) => (
                <button key={project.metadata.project_id} disabled={selectorBusy} onClick={() => chooseProject(project)}>
                  <span className="project-folder"><FolderOpen size={20} /></span>
                  <span className="project-list-copy">
                    <strong>{formatProjectTitle(project.title)}</strong>
                    <small title={project.path}>{project.path}</small>
                    <span><em>{formatOperationMode(project.metadata.operation_mode)}</em><em>{formatRunStatus(project.metadata.run_status)}</em></span>
                  </span>
                  <Clock3 size={16} />
                </button>
              ))}
              {projectsLoading && <SelectorEmpty icon={<Clock3 size={26} />} title="正在读取本地项目" detail="请稍候，正在恢复可以继续创作的项目列表。" />}
              {!projectsLoading && projectsLoadFailed && <SelectorEmpty icon={<FolderOpen size={26} />} title="项目列表加载失败" detail="请确认后端服务正在运行，然后刷新页面重试。" />}
              {!projectsLoading && !projectsLoadFailed && projects.length === 0 && <SelectorEmpty icon={<FolderOpen size={26} />} title="还没有可以继续的项目" detail="返回首页，开始你的第一本新书。" />}
            </div>
          </section>
        </div>
      )}

      {step === "continue-mode" && selectedProject && (
        <div className="project-selector-grid selector-flow-grid">
          <ModePanel
            eyebrow="继续创作"
            title={`继续《${formatProjectTitle(selectedProject.title)}》`}
            description={`上次使用${formatOperationMode(selectedProject.metadata.operation_mode)}。你可以保持不变，也可以为后续创作切换模式。`}
            mode={mode}
            disabled={selectorBusy}
            modeLocked={selectedProject.metadata.run_status === "running" || selectedProject.metadata.run_status === "pause_requested"}
            onModeChange={setMode}
            onBack={() => setStep("continue-list")}
            actionLabel={openingProject ? "正在打开小说..." : "以此模式继续创作"}
            onConfirm={() => void continueProject()}
          />
          <FlowSummary kind="continue" mode={mode} project={selectedProject} />
        </div>
      )}
    </main>
  );
}

function ModePanel({
  eyebrow,
  title,
  description,
  mode,
  disabled,
  modeLocked = false,
  onModeChange,
  onBack,
  actionLabel,
  onConfirm
}: {
  eyebrow: string;
  title: string;
  description: string;
  mode: OperationMode;
  disabled: boolean;
  modeLocked?: boolean;
  onModeChange: (mode: OperationMode) => void;
  onBack: () => void;
  actionLabel: string;
  onConfirm: () => void;
}) {
  return (
    <section className="np-surface create-project-panel selector-mode-panel">
      <button className="selector-back-button" disabled={disabled} onClick={onBack}><ArrowLeft size={15} /> 返回</button>
      <p className="eyebrow">{eyebrow}</p>
      <h1>{title}</h1>
      <p>{description}</p>
      <fieldset>
        <legend>创作模式</legend>
        <button disabled={disabled || modeLocked} className={mode === "full_auto" ? "selected" : ""} onClick={() => onModeChange("full_auto")}>
          <Bot size={19} />
          <span><strong>全自动模式</strong><small>故事弧与章节由 harness 连续推进，你可以随时提出意见。</small></span>
        </button>
        <button disabled={disabled || modeLocked} className={mode === "participatory" ? "selected" : ""} onClick={() => onModeChange("participatory")}>
          <UserRound size={19} />
          <span><strong>参与模式</strong><small>每个故事弧计划都等待你审批，章节 loop 自动执行。</small></span>
        </button>
      </fieldset>
      {modeLocked && <p className="selector-mode-note">小说正在运行或等待安全暂停，本次只能按当前模式进入；暂停完成后才能切换。</p>}
      <button className="gold-button create-project-button" disabled={disabled} onClick={onConfirm}>
        {actionLabel} <ArrowRight size={17} />
      </button>
    </section>
  );
}

function FlowSummary({ kind, mode, project }: { kind: "new" | "continue"; mode: OperationMode; project?: ProjectSummary }) {
  return (
    <aside className="np-surface selector-summary-panel">
      <p className="eyebrow">本次创作</p>
      <h2>{kind === "new" ? "新书流程" : formatProjectTitle(project?.title)}</h2>
      <dl>
        {project && <div><dt>当前进度</dt><dd>{formatRunStatus(project.metadata.run_status)}</dd></div>}
        {project && <div><dt>上次模式</dt><dd>{formatOperationMode(project.metadata.operation_mode)}</dd></div>}
        <div><dt>本次模式</dt><dd>{formatOperationMode(mode)}</dd></div>
        <div><dt>已有内容</dt><dd>{kind === "new" ? "从空白方向开始" : "全部保留"}</dd></div>
        <div><dt>正式书名</dt><dd>{kind === "new" ? "全书方向确定后选择" : formatProjectTitle(project?.title)}</dd></div>
      </dl>
      <p className="selector-summary-note">
        {kind === "new"
          ? "创建后先进行全书方向讨论；推荐书名和自定义书名会在批准方向时出现。"
          : "切换模式只改变后续故事弧的人工门禁，不会重置章节、正史或审批记录。"}
      </p>
    </aside>
  );
}

function SelectorEmpty({ icon, title, detail }: { icon: ReactNode; title: string; detail: string }) {
  return <div className="empty-state">{icon}<h2>{title}</h2><p>{detail}</p></div>;
}
