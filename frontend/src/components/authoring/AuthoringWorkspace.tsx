import { BookOpen, CircleAlert, Download, Pause, Play, Square } from "lucide-react";
import { useState } from "react";
import { authoringApi } from "../../api/authoring-client";
import { ThemeToggle } from "../ui/ThemeToggle";
import { authoringActivityLabel, authoringStatusLabel, authoringStageLabel, type AuthoringProject, type ModelSettingsDocument } from "../../types/authoring";
import { ProjectModels } from "./ProjectModels";
import { ManuscriptPreview } from "./ManuscriptPreview";
import styles from "../../AuthoringApp.module.css";

interface WorkspaceProps {
  project: AuthoringProject;
  settings?: ModelSettingsDocument;
  settingsError: Error | null;
  starting: boolean;
  notice: string | null;
  onNotice: (value: string | null) => void;
  onBack: () => void;
  onChanged: (value: AuthoringProject) => void;
  onRefresh: () => void;
  onSettings: () => void;
}

export function AuthoringWorkspace({ project, settings, settingsError, starting, notice, onNotice, onBack, onChanged, onRefresh, onSettings }: WorkspaceProps) {
  const [acting, setBusy] = useState(false);
  const [bindingBusy, setBindingBusy] = useState(false);
  const busy = starting || acting || bindingBusy;
  async function action(operation: () => Promise<{ project: AuthoringProject }>) {
    setBusy(true); onNotice(null);
    try { onChanged((await operation()).project); }
    catch (error) { onNotice(error instanceof Error ? error.message : "操作失败"); onRefresh(); }
    finally { setBusy(false); }
  }
  const canPause = project.status === "running";
  const canResume = project.status === "ready" || project.status === "paused" || project.status === "failure_paused";
  const canCancel = !["cancelled", "completed"].includes(project.status);
  return <main className={styles.shell}>
    <header className={styles.header}><div><BookOpen /><strong>{project.title ?? "正在准备作品名称…"}</strong></div><nav><button type="button" onClick={onBack}>全部作品</button><ThemeToggle /></nav></header>
    <section className={styles.workspaceHero}><div><span>{authoringStatusLabel(project.status)}</span><h1>{project.completed_chapters} / {project.target_chapters} 章已完成</h1><p>{project.brief}</p></div><div className={styles.actions}>{canPause && <button disabled={busy} onClick={() => void action(() => authoringApi.pause(project.project_id))}><Pause size={16} />暂停</button>}{canResume && <button disabled={busy} className={styles.primary} onClick={() => void action(() => authoringApi.startOrResume(project.project_id))}><Play size={16} />继续创作</button>}{canCancel && <button disabled={busy} className={styles.danger} onClick={() => void action(() => authoringApi.cancel(project.project_id))}><Square size={15} />取消创作</button>}</div></section>
    <section className={styles.progressCard}><div><strong>整本进度</strong><span>{project.progress_percent}%</span></div><p>当前阶段：<strong>{authoringStageLabel(project.stage)}</strong></p><progress aria-label="整本创作进度" value={project.progress_percent} max="100" /></section>
    {starting && <p role="status">作品已创建，正在启动自动创作…</p>}
    {project.failure_reason && <section className={styles.failure} role="alert"><CircleAlert /><div><strong>创作已安全暂停</strong><p>{project.failure_reason}</p></div></section>}
    {notice && <p className={styles.error} role="alert">{notice}</p>}
    <div className={styles.workspaceGrid}><section className={styles.card}><h2>最近进展</h2>{project.recent_activity.length === 0 ? <p className={styles.muted}>正在等待第一条创作进展…</p> : <ol className={styles.timeline}>{[...project.recent_activity].reverse().slice(0, 10).map((item) => <li key={item.sequence}><span /><div><strong>{authoringActivityLabel(item)}</strong><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleString()}</time></div></li>)}</ol>}</section><section className={styles.card}><h2>成稿下载</h2><p className={styles.muted}>{project.status === "completed" ? "作品已完成，可以选择喜欢的格式保存。" : "写作过程中也可以下载当前已完成的章节。"}</p><div className={styles.downloads}><a href={authoringApi.exportUrl(project.project_id, "markdown")}><Download size={17} />下载 Markdown</a><a href={authoringApi.exportUrl(project.project_id, "txt")}><Download size={17} />下载 TXT</a></div></section></div>
    {project.status === "completed" && <ManuscriptPreview projectId={project.project_id} />}
    <ProjectModels project={project} settings={settings} settingsError={settingsError} busy={busy} onBusy={setBindingBusy} onChanged={onChanged} onRefresh={onRefresh} onSettings={onSettings} />
  </main>;
}
