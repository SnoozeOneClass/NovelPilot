import { Bot, Clock3, Feather, FolderOpen, Plus, UserRound } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { api, formatApiError } from "../../api/client";
import { formatOperationMode, formatProjectTitle, formatRunStatus } from "../../types/display";
import type { OperationMode, ProjectSummary } from "../../types/domain";

interface ProjectSelectorProps {
  onProjectOpened: (project: ProjectSummary) => void;
}

export function ProjectSelector({ onProjectOpened }: ProjectSelectorProps) {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
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

  async function createProject() {
    if (actionLockRef.current) return;
    actionLockRef.current = true;
    setCreating(true);
    setNotice(null);
    try {
      const project = await api.createProject(mode);
      onProjectOpened(project);
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      actionLockRef.current = false;
      setCreating(false);
    }
  }

  async function openProject(name: string) {
    if (actionLockRef.current) return;
    actionLockRef.current = true;
    setOpeningProject(name);
    setNotice(null);
    try {
      const project = await api.openProject(name);
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

      <div className="project-selector-grid">
        <section className="np-surface create-project-panel">
          <p className="eyebrow">开始新书</p>
          <h1>先确定创作方式，再和 AI 一起找到这本书</h1>
          <p>新项目会以“未命名新书”开始。书名将在全书方向成熟后，由你从推荐结果中选择或亲自确定。</p>

          <fieldset>
            <legend>创作模式</legend>
            <button disabled={selectorBusy} className={mode === "full_auto" ? "selected" : ""} onClick={() => setMode("full_auto")}>
              <Bot size={19} />
              <span><strong>全自动模式</strong><small>故事弧与章节由 harness 连续推进，你可以随时提出意见。</small></span>
            </button>
            <button disabled={selectorBusy} className={mode === "participatory" ? "selected" : ""} onClick={() => setMode("participatory")}>
              <UserRound size={19} />
              <span><strong>参与模式</strong><small>每个故事弧计划都等待你审批，章节 loop 自动执行。</small></span>
            </button>
          </fieldset>

          <button className="gold-button create-project-button" disabled={selectorBusy} onClick={() => void createProject()}>
            <Plus size={17} /> {creating ? "正在创建未命名新书..." : "开始新书"}
          </button>
        </section>

        <section className="np-surface recent-projects-panel">
          <header className="view-heading compact-heading">
            <div><p className="eyebrow">继续创作</p><h2>打开已有小说项目</h2><p>恢复原有内容、进度和创作模式。</p></div>
          </header>
          <div className="project-list-modern">
            {projects.map((project) => (
              <button key={project.metadata.project_id} disabled={selectorBusy} onClick={() => void openProject(project.name)}>
                <span className="project-folder"><FolderOpen size={20} /></span>
                <span className="project-list-copy">
                  <strong>{formatProjectTitle(project.title)}</strong>
                  <small title={project.path}>{project.path}</small>
                  <span><em>{formatOperationMode(project.metadata.operation_mode)}</em><em>{formatRunStatus(project.metadata.run_status)}</em></span>
                </span>
                <Clock3 size={16} />
              </button>
            ))}
            {projectsLoading && (
              <div className="empty-state">
                <Clock3 size={26} />
                <h2>正在读取本地项目</h2>
                <p>请稍候，正在恢复可以继续创作的项目列表。</p>
              </div>
            )}
            {!projectsLoading && projectsLoadFailed && (
              <div className="empty-state">
                <FolderOpen size={26} />
                <h2>项目列表加载失败</h2>
                <p>请确认后端服务正在运行，然后刷新页面重试。</p>
              </div>
            )}
            {!projectsLoading && !projectsLoadFailed && projects.length === 0 && (
              <div className="empty-state">
                <FolderOpen size={26} />
                <h2>还没有可以继续的项目</h2>
                <p>从左侧选择创作模式，开始你的第一本新书。</p>
              </div>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
