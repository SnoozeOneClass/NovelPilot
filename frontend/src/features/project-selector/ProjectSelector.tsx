import { Bot, Clock3, Feather, FolderOpen, Plus, UserRound } from "lucide-react";
import { useEffect, useState } from "react";
import { api, formatApiError } from "../../api/client";
import { formatOperationMode, formatRunStatus } from "../../types/display";
import type { OperationMode, ProjectSummary } from "../../types/domain";

interface ProjectSelectorProps {
  onProjectOpened: (project: ProjectSummary) => void;
}

export function ProjectSelector({ onProjectOpened }: ProjectSelectorProps) {
  const [projects, setProjects] = useState<ProjectSummary[]>([]);
  const [title, setTitle] = useState("");
  const [mode, setMode] = useState<OperationMode>("full_auto");
  const [creating, setCreating] = useState(false);
  const [openingProject, setOpeningProject] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "error"; text: string } | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .listProjects()
      .then((result) => {
        if (!cancelled) {
          setProjects(result);
          setNotice(null);
        }
      })
      .catch((error) => {
        if (!cancelled) setNotice({ kind: "error", text: formatApiError(error) });
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function createProject() {
    if (!title.trim()) return;
    setCreating(true);
    setNotice(null);
    try {
      const project = await api.createProject(title.trim(), mode);
      onProjectOpened(project);
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setCreating(false);
    }
  }

  async function openProject(name: string) {
    setOpeningProject(name);
    setNotice(null);
    try {
      const project = await api.openProject(name);
      onProjectOpened(project);
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setOpeningProject(null);
    }
  }

  return (
    <main className="project-selector-shell">
      <header className="selector-brand">
        <span><Feather size={22} /></span>
        <div><strong>NovelPilot</strong><small>本地 AI 长篇小说创作系统</small></div>
      </header>

      <div className="project-selector-grid">
        <section className="np-surface create-project-panel">
          <p className="eyebrow">新建小说</p>
          <h1>从一个新的创作项目开始</h1>
          <p>小说、设定、产物与正史状态都会保存在本地项目目录中。</p>

          <label>
            <span>小说名称</span>
            <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="输入小说名称" onKeyDown={(event) => event.key === "Enter" && void createProject()} />
          </label>

          <fieldset>
            <legend>创作模式</legend>
            <button className={mode === "full_auto" ? "selected" : ""} onClick={() => setMode("full_auto")}>
              <Bot size={19} />
              <span><strong>全自动模式</strong><small>故事弧与章节由 harness 连续推进，你可以随时提出意见。</small></span>
            </button>
            <button className={mode === "participatory" ? "selected" : ""} onClick={() => setMode("participatory")}>
              <UserRound size={19} />
              <span><strong>参与模式</strong><small>每个故事弧计划都等待你审批，章节 loop 自动执行。</small></span>
            </button>
          </fieldset>

          <button className="gold-button create-project-button" disabled={creating || !title.trim()} onClick={() => void createProject()}>
            <Plus size={17} /> {creating ? "正在创建..." : "新建项目"}
          </button>
          {notice && <p className="notice-banner error">{notice.text}</p>}
        </section>

        <section className="np-surface recent-projects-panel">
          <header className="view-heading compact-heading">
            <div><h2>打开本地项目</h2><p>一次只打开一个小说项目继续创作。</p></div>
          </header>
          <div className="project-list-modern">
            {projects.map((project) => (
              <button key={project.metadata.project_id} disabled={openingProject === project.name} onClick={() => void openProject(project.name)}>
                <span className="project-folder"><FolderOpen size={20} /></span>
                <span className="project-list-copy">
                  <strong>{project.title}</strong>
                  <small title={project.path}>{project.path}</small>
                  <span><em>{formatOperationMode(project.metadata.operation_mode)}</em><em>{formatRunStatus(project.metadata.run_status)}</em></span>
                </span>
                <Clock3 size={16} />
              </button>
            ))}
            {projects.length === 0 && (
              <div className="empty-state">
                <FolderOpen size={26} />
                <h2>还没有本地项目</h2>
                <p>在左侧输入小说名称即可创建。</p>
              </div>
            )}
          </div>
        </section>
      </div>
    </main>
  );
}
