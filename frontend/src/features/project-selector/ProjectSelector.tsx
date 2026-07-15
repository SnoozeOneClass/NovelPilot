import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, BookOpen, Clock3, Feather, FolderOpen, Plus, Trash2 } from "lucide-react";
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

interface PageNotice {
  kind: "error" | "success";
  text: string;
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
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [mode, setMode] = useState<OperationMode>("full_auto");
  const [notice, setNotice] = useState<PageNotice | null>(null);
  const [selectedProjectIds, setSelectedProjectIds] = useState<Set<string>>(new Set());
  const projectsQuery = useQuery({ queryKey: ["projects"], queryFn: api.listProjects });

  const createMutation = useMutation({
    mutationFn: (operationMode: OperationMode) => api.createProject(operationMode),
    onSuccess: (project) => {
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      onProjectOpened(project);
    },
    onError: (error) => setNotice({ kind: "error", text: formatApiError(error) })
  });

  const openMutation = useMutation({
    mutationFn: async (project: ProjectSummary) => {
      const active = await api.activeProject();
      return active?.name === project.name ? active : api.openProject(project.name);
    },
    onSuccess: (openedProject) => onProjectOpened(openedProject),
    onError: (error) => setNotice({ kind: "error", text: formatApiError(error) })
  });

  const deleteMutation = useMutation({
    mutationFn: (projectIds: string[]) => api.deleteProjects(projectIds),
    onSuccess: (response) => {
      const deletedIds = new Set(response.deleted.map((project) => project.project_id));
      queryClient.setQueryData<ProjectSummary[]>(["projects"], (current) =>
        current?.filter((project) => !deletedIds.has(project.metadata.project_id)) ?? []
      );
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      setSelectedProjectIds(new Set());
      setDeleteDialogOpen(false);
      setNotice({
        kind: "success",
        text: `已从本地 output 目录永久删除 ${response.deleted.length} 本小说。`
      });
    },
    onError: (error) => setNotice({ kind: "error", text: formatApiError(error) })
  });

  const busy = createMutation.isPending || openMutation.isPending || deleteMutation.isPending;
  const projects = projectsQuery.data ?? [];
  const selectedProjects = projects.filter((project) => selectedProjectIds.has(project.metadata.project_id));
  const allSelected = projects.length > 0 && selectedProjects.length === projects.length;
  const queryError = projectsQuery.error ? formatApiError(projectsQuery.error) : null;
  const displayedNotice = notice ?? (queryError ? { kind: "error" as const, text: queryError } : null);

  function createProject() {
    setNotice(null);
    createMutation.mutate(mode);
  }

  function openProject(project: ProjectSummary) {
    setNotice(null);
    openMutation.mutate(project);
  }

  function toggleProject(projectId: string) {
    setSelectedProjectIds((current) => {
      const next = new Set(current);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  }

  function toggleAllProjects() {
    setSelectedProjectIds(
      allSelected
        ? new Set()
        : new Set(projects.map((project) => project.metadata.project_id))
    );
  }

  function confirmDeletion() {
    if (selectedProjects.length === 0 || deleteMutation.isPending) return;
    setNotice(null);
    deleteMutation.mutate(selectedProjects.map((project) => project.metadata.project_id));
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

        {displayedNotice && (
          <div className={displayedNotice.kind === "error" ? styles.errorNotice : styles.successNotice}>
            <span>{displayedNotice.text}</span>
            {projectsQuery.isError && (
              <button disabled={projectsQuery.isFetching} onClick={() => void projectsQuery.refetch()}>
                {projectsQuery.isFetching ? "正在重连..." : "重新连接"}
              </button>
            )}
          </div>
        )}

        <section className={styles.projectPanel} aria-label="本地小说项目">
          <div className={styles.selectionToolbar}>
            <span>
              {selectedProjects.length > 0
                ? `已选择 ${selectedProjects.length} / ${projects.length} 本`
                : `共 ${projects.length} 本小说`}
            </span>
            <div>
              <button type="button" disabled={busy || projects.length === 0} onClick={toggleAllProjects}>
                {allSelected ? "取消全选" : "全选"}
              </button>
              <button
                type="button"
                className={styles.deleteButton}
                disabled={busy || selectedProjects.length === 0}
                onClick={() => { setNotice(null); setDeleteDialogOpen(true); }}
              >
                <Trash2 size={15} /> 删除选中{selectedProjects.length > 0 ? `（${selectedProjects.length}）` : ""}
              </button>
            </div>
          </div>
          <header className={styles.listHeader}>
            <span>选择</span><span>小说</span><span>当前位置</span><span>模式</span><span>更新时间</span><span />
          </header>
          <div className={styles.projectList}>
            {projects.map((project) => {
              const title = formatProjectTitle(project.title);
              const selected = selectedProjectIds.has(project.metadata.project_id);
              return (
                <article
                  key={project.metadata.project_id}
                  className={styles.projectRow}
                  data-selected={selected || undefined}
                >
                  <label className={styles.selectionCell}>
                    <input
                      type="checkbox"
                      aria-label={`选择《${title}》`}
                      checked={selected}
                      disabled={busy}
                      onChange={() => toggleProject(project.metadata.project_id)}
                    />
                  </label>
                  <button
                    type="button"
                    className={styles.projectOpenButton}
                    disabled={busy}
                    onClick={() => openProject(project)}
                  >
                    <span className={styles.projectIdentity}>
                      <span className={styles.bookIcon}><BookOpen size={18} /></span>
                      <span><strong>{title}</strong><small>{project.name}</small></span>
                    </span>
                    <span className={styles.progress}>
                      <strong>{project.metadata.active_chapter_id ?? project.metadata.active_arc_id ?? "尚未开始"}</strong>
                      <small>{formatRunStatus(project.metadata.run_status)}</small>
                    </span>
                    <span className={styles.mode}>{formatOperationMode(project.metadata.operation_mode)}</span>
                    <span className={styles.updated}><Clock3 size={14} /> {formatUpdatedAt(project.metadata.updated_at)}</span>
                    <ArrowRight size={16} />
                  </button>
                </article>
              );
            })}

            {projectsQuery.isLoading && (
              <div className={styles.empty}>
                <Clock3 size={22} />
                <strong>{projectsQuery.failureCount ? "正在等待本地服务" : "正在读取本地项目"}</strong>
                {projectsQuery.failureCount > 0 && <span>后端启动后会自动继续，无需强制刷新。</span>}
              </div>
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

      <Dialog
        open={deleteDialogOpen}
        title={`确认删除 ${selectedProjects.length} 本小说？`}
        onClose={() => !deleteMutation.isPending && setDeleteDialogOpen(false)}
      >
        <div className={styles.deleteDialog}>
          <p>将从本地 <code>output</code> 目录永久删除以下项目的全部正文、设定和运行证据。此操作不可撤销。</p>
          <ul>
            {selectedProjects.slice(0, 6).map((project) => (
              <li key={project.metadata.project_id}>
                <strong>{formatProjectTitle(project.title)}</strong>
                <span>{project.name}</span>
              </li>
            ))}
          </ul>
          {selectedProjects.length > 6 && <small>以及另外 {selectedProjects.length - 6} 本小说</small>}
          {deleteMutation.isError && <div className={styles.deleteError}>{formatApiError(deleteMutation.error)}</div>}
          <footer>
            <button type="button" className={styles.cancelButton} disabled={deleteMutation.isPending} onClick={() => setDeleteDialogOpen(false)}>取消</button>
            <button type="button" className={styles.confirmDeleteButton} disabled={deleteMutation.isPending || selectedProjects.length === 0} onClick={confirmDeletion}>
              <Trash2 size={16} /> {deleteMutation.isPending ? "正在删除..." : "永久删除"}
            </button>
          </footer>
        </div>
      </Dialog>
    </main>
  );
}
