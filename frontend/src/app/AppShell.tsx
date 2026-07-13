import {
  BookHeart,
  ChevronsLeft,
  ChevronsRight,
  GitBranch,
  LibraryBig,
  MessagesSquare,
  RefreshCw,
  Settings2,
  ShieldCheck,
  SquareTerminal,
  X
} from "lucide-react";
import { useState, type ReactNode } from "react";
import type { TaskDomain } from "./types";
import type { LlmProfilePublic, ProjectSummary } from "../types/domain";
import { formatOperationMode, formatProjectTitle, formatRunStatus } from "../types/display";
import { ThemeToggle } from "../components/ui/ThemeToggle";
import styles from "./AppShell.module.css";

type ShellLocation = TaskDomain | "settings";

const domainItems: Array<{ id: TaskDomain; label: string; icon: typeof MessagesSquare }> = [
  { id: "cocreate", label: "共创", icon: MessagesSquare },
  { id: "workbench", label: "工作台", icon: SquareTerminal },
  { id: "story", label: "故事世界", icon: LibraryBig },
  { id: "evidence", label: "证据中心", icon: GitBranch }
];

const locationLabels: Record<ShellLocation, string> = {
  cocreate: "全书共创",
  workbench: "创作工作台",
  story: "故事世界",
  evidence: "证据中心",
  settings: "设置"
};

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem("novelpilot.sidebar.collapsed") === "true";
  } catch {
    return false;
  }
}

interface AppShellProps {
  project: ProjectSummary;
  location: ShellLocation;
  profile: LlmProfilePublic | null;
  canRecover: boolean;
  runInFlight: boolean;
  notice?: ReactNode;
  feedbackDock?: ReactNode;
  children: ReactNode;
  onLocationChange: (location: ShellLocation) => void;
  onRefresh: () => void;
  onRecover: () => void;
  onCloseProject: () => void;
}

export function AppShell({
  project,
  location,
  profile,
  canRecover,
  runInFlight,
  notice,
  feedbackDock,
  children,
  onLocationChange,
  onRefresh,
  onRecover,
  onCloseProject
}: AppShellProps) {
  const [collapsed, setCollapsed] = useState(readCollapsed);

  function toggleCollapsed() {
    const next = !collapsed;
    setCollapsed(next);
    try { window.localStorage.setItem("novelpilot.sidebar.collapsed", String(next)); } catch { /* no-op */ }
  }

  return (
    <main className={`${styles.shell} ${collapsed ? styles.collapsed : ""}`}>
      <aside className={styles.sidebar}>
        <header className={styles.brand}>
          <BookHeart size={19} />
          <strong>NovelPilot</strong>
          <button title={collapsed ? "展开侧边栏" : "收起侧边栏"} onClick={toggleCollapsed}>
            {collapsed ? <ChevronsRight size={16} /> : <ChevronsLeft size={16} />}
          </button>
        </header>

        <nav className={styles.domainNav} aria-label="任务域">
          {domainItems.map((item) => {
            const Icon = item.icon;
            return (
              <button
                key={item.id}
                title={item.label}
                className={location === item.id ? styles.active : ""}
                onClick={() => onLocationChange(item.id)}
              >
                <Icon size={18} /><span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <footer className={styles.sidebarFooter}>
          <button title="设置" className={location === "settings" ? styles.active : ""} onClick={() => onLocationChange("settings")}>
            <Settings2 size={18} /><span>设置</span>
          </button>
          <div className={styles.bookSummary}>
            <span>当前小说</span>
            <strong title={formatProjectTitle(project.title)}>{formatProjectTitle(project.title)}</strong>
            <small>{project.metadata.active_chapter_id ?? project.metadata.active_arc_id ?? "尚未开始"}</small>
          </div>
          <div className={styles.utilityRow}>
            <ThemeToggle />
            <button title="刷新工作区" onClick={onRefresh}><RefreshCw size={16} /></button>
            <button title="恢复卡住的运行" disabled={!canRecover} onClick={onRecover}><ShieldCheck size={16} /></button>
            <button title="切换小说" disabled={runInFlight} onClick={onCloseProject}><X size={16} /></button>
          </div>
        </footer>
      </aside>

      <section className={styles.workspace}>
        <header className={styles.topbar}>
          <div className={styles.provider}>
            <span className={profile ? styles.readyDot : styles.missingDot} />
            <strong>{profile?.name ?? "未选择模型"}</strong>
            <small>{profile?.model ?? "请在设置中配置"}</small>
          </div>
          <div className={styles.location}>
            <strong>{formatProjectTitle(project.title)}</strong>
            <span>/ {locationLabels[location]}</span>
          </div>
          <div className={styles.statuses}>
            <span>{formatOperationMode(project.metadata.operation_mode)}</span>
            <strong data-status={project.metadata.run_status}>{formatRunStatus(project.metadata.run_status)}</strong>
          </div>
        </header>
        {notice}
        <div className={styles.content}>{children}</div>
        {feedbackDock}
      </section>

      <nav className={styles.mobileNav} aria-label="任务域">
        {domainItems.map((item) => {
          const Icon = item.icon;
          return (
            <button key={item.id} className={location === item.id ? styles.active : ""} onClick={() => onLocationChange(item.id)}>
              <Icon size={18} /><span>{item.label}</span>
            </button>
          );
        })}
      </nav>
    </main>
  );
}
