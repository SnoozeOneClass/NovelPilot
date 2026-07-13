import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, BookOpen, Clock3, Feather, FolderOpen, Plus } from "lucide-react";
import { useState } from "react";
import { api, formatApiError } from "../../api/client";
import { Dialog } from "../../components/ui/Dialog";
import { ThemeToggle } from "../../components/ui/ThemeToggle";
import { formatOperationMode, formatProjectTitle, formatRunStatus } from "../../types/display";
import type { OperationMode, ProjectSummary } from "../../types/domain";
import styles from "./ProjectSelector.module.css";

interface ProjectSelectorProps {
  onProjectOpened: (project: ProjectSummary) => void;
}

function formatUpdatedAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "更新时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit"
  }).format(date);
}

export function ProjectSelector({ onProjectOpened }: ProjectSelectorProps) {
  const queryClient = useQueryClient();
  const [newBookOpen, setNewBookOpen] = useState(false);
  const [mode, setMode] = useState<OperationMode>("full_auto");
  const [notice, setNotice] = useState<string | null>(null);
  const projectsQuery = useQuery({ queryKey: ["projects"], queryFn: api.listProjects });

  const createMutation = useMutation({
    mutationFn: (operationMode: OperationMode) => api.createProject(operationMode),
    onSuccess: (project) => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      onProjectOpened(project);
    },
    onError: (error) => setNotice(formatApiError(error))
  });

  const openMutation = useMutation({
    mutationFn: async (project: ProjectSummary) => {
      const active = await api.activeProject();
      return active?.name === project.name ? active : api.openProject(project.name);
    },
    onSuccess: (openedProject) => onProjectOpened(openedProject),
    onError: (error) => setNotice(formatApiError(error))
  });

  const busy = createMutation.isPending || openMutation.isPending;
  const projects = projectsQuery.data ?? [];
  const error = notice ?? (projectsQuery.error ? formatApiError(projectsQuery.error) : null);

  function createProject() {
    setNotice(null);
    createMutation.mutate(mode);
  }

  function openProject(project: ProjectSummary) {
    setNotice(null);
    openMutation.mutate(project);
  }

  return (
    <main className={styles.shell}>
      <header className={styles.topbar}>
        <div className={styles.brand}>
          <span><Feather size={20} /></span>
          <div><strong>NovelPilot</strong><small>本地长篇小说工作台</small></div>
        </div>
        <ThemeToggle />
      </header>

      <section className={styles.content}>
        <header className={styles.heading}>
          <div>
            <p>小说项目</p>
            <h1>继续写作，或者开始一本新书</h1>
            <span>所有正文、设定和运行证据都保存在本地项目目录中。</span>
          </div>
          <button className={styles.primaryButton} disabled={busy} onClick={() => setNewBookOpen(true)}>
            <Plus size={17} /> 新建小说
          </button>
        </header>

        {error && <div className={styles.errorNotice}>{error}</div>}

        <section className={styles.projectPanel} aria-label="本地小说项目">
          <header className={styles.listHeader}>
            <span>小说</span><span>当前位置</span><span>模式</span><span>更新时间</span><span />
          </header>
          <div className={styles.projectList}>
            {projects.map((project) => (
              <button
                key={project.metadata.project_id}
                className={styles.projectRow}
                disabled={busy}
                onClick={() => openProject(project)}
              >
                <span className={styles.projectIdentity}>
                  <span className={styles.bookIcon}><BookOpen size={18} /></span>
                  <span><strong>{formatProjectTitle(project.title)}</strong><small>{project.name}</small></span>
                </span>
                <span className={styles.progress}>
                  <strong>{project.metadata.active_chapter_id ?? project.metadata.active_arc_id ?? "尚未开始"}</strong>
                  <small>{formatRunStatus(project.metadata.run_status)}</small>
                </span>
                <span className={styles.mode}>{formatOperationMode(project.metadata.operation_mode)}</span>
                <span className={styles.updated}><Clock3 size={14} /> {formatUpdatedAt(project.metadata.updated_at)}</span>
                <ArrowRight size={16} />
              </button>
            ))}

            {projectsQuery.isLoading && (
              <div className={styles.empty}><Clock3 size={22} /><strong>正在读取本地项目</strong></div>
            )}
            {!projectsQuery.isLoading && !projectsQuery.isError && projects.length === 0 && (
              <div className={styles.empty}>
                <FolderOpen size={24} />
                <strong>还没有小说项目</strong>
                <span>新建一本小说，从开放式全书共创开始。</span>
              </div>
            )}
          </div>
        </section>
      </section>

      <footer className={styles.footer}>
        <span>本地单用户</span><span>数据不会上传到 NovelPilot 服务</span>
      </footer>

      <Dialog open={newBookOpen} title="开始一本新书" onClose={() => !createMutation.isPending && setNewBookOpen(false)}>
        <div className={styles.newBookDialog}>
          <p>项目会先以“未命名新书”创建，正式书名在全书方向审阅通过后确定。</p>
          <fieldset className={styles.modePicker}>
            <legend>初始创作模式</legend>
            <button type="button" className={mode === "full_auto" ? styles.selected : ""} onClick={() => setMode("full_auto")}>
              <strong>全自动</strong><span>故事弧和章节由 Harness 连续推进。</span>
            </button>
            <button type="button" className={mode === "participatory" ? styles.selected : ""} onClick={() => setMode("participatory")}>
              <strong>参与模式</strong><span>每个故事弧计划等待你的批准。</span>
            </button>
          </fieldset>
          <footer>
            <button type="button" className={styles.cancelButton} disabled={createMutation.isPending} onClick={() => setNewBookOpen(false)}>取消</button>
            <button type="button" className={styles.primaryButton} disabled={createMutation.isPending} onClick={createProject}>
              {createMutation.isPending ? "正在创建..." : "创建并进入共创"} <ArrowRight size={16} />
            </button>
          </footer>
        </div>
      </Dialog>
    </main>
  );
}
