import { FolderOpen, Plus } from "lucide-react";
import { useEffect, useState } from "react";
import { api, formatApiError } from "../../api/client";
import { formatOperationMode } from "../../types/display";
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
        if (!cancelled) {
          setNotice({ kind: "error", text: formatApiError(error) });
        }
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
    <main className="project-shell">
      <section className="project-panel">
        <div>
          <p className="eyebrow">Novelpilot</p>
          <h1>打开小说项目</h1>
        </div>

        <div className="project-create">
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="小说名称"
          />
          <select value={mode} onChange={(event) => setMode(event.target.value as OperationMode)}>
            <option value="full_auto">全自动</option>
            <option value="participatory">参与模式</option>
          </select>
          <button className="primary-button" disabled={creating} onClick={createProject}>
            <Plus size={18} /> 新建
          </button>
        </div>
        {notice && <p className={`notice-banner ${notice.kind}`}>{notice.text}</p>}

        <div className="project-list">
          {projects.map((project) => (
            <button
              key={project.metadata.project_id}
              disabled={openingProject === project.name}
              onClick={() => openProject(project.name)}
            >
              <FolderOpen size={18} />
              <span>{project.title}</span>
              <small>{formatOperationMode(project.metadata.operation_mode)}</small>
            </button>
          ))}
        </div>
      </section>
    </main>
  );
}
