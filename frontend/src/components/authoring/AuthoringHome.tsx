import { Plus, WandSparkles } from "lucide-react";
import { useState, type FormEvent } from "react";
import { authoringApi } from "../../api/authoring-client";
import { ThemeToggle } from "../ui/ThemeToggle";
import { authoringStatusLabel, authoringStageLabel, isModelReady, modelAvailabilityLabel, type AuthoringProject, type CreateAuthoringProject, type ModelSettingsDocument } from "../../types/authoring";
import { ModelSelect } from "./ModelSelect";
import styles from "../../AuthoringApp.module.css";

interface HomeProps {
  projects: AuthoringProject[];
  settings?: ModelSettingsDocument;
  error: Error | null;
  onSelect: (id: string) => void;
  onCreated: (project: AuthoringProject) => Promise<void>;
  onSettings: () => void;
}

export function AuthoringHome({ projects, settings, error, onSelect, onCreated, onSettings }: HomeProps) {
  const profiles = settings?.profiles ?? [];
  const [brief, setBrief] = useState("");
  const [advanced, setAdvanced] = useState(false);
  const [targetKind, setTargetKind] = useState<"chapters" | "words">("chapters");
  const [target, setTarget] = useState("");
  const [profile, setProfile] = useState("");
  const [planningProfile, setPlanningProfile] = useState("");
  const [writingProfile, setWritingProfile] = useState("");
  const [editingProfile, setEditingProfile] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const selectedModelId = profile || settings?.selected_profile_id;
  const selectedModel = profiles.find((item) => item.profile_id === selectedModelId);
  const unavailable = settings !== undefined && !isModelReady(selectedModel);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!brief.trim()) return;
    setCreating(true); setCreateError(null);
    const input: CreateAuthoringProject = { brief: brief.trim() };
    const amount = Number(target);
    if (advanced && amount > 0) {
      if (targetKind === "chapters") input.target_chapters = amount;
      else input.target_words = amount;
    }
    if (profile) input.default_profile_id = profile;
    if (planningProfile) input.architect_profile_id = planningProfile;
    if (writingProfile) input.writer_profile_id = writingProfile;
    if (editingProfile) input.editor_profile_id = editingProfile;
    try {
      const created = await authoringApi.createProject(input);
      await onCreated(created);
    } catch (caught) {
      setCreateError(caught instanceof Error ? caught.message : "创建失败");
    } finally { setCreating(false); }
  }

  return <main className={styles.shell}>
    <header className={styles.header}><div><WandSparkles /><strong>NovelPilot 自动创作</strong></div><nav><button type="button" onClick={onSettings}>模型设置</button><ThemeToggle /></nav></header>
    <section className={styles.hero}><span>给第一次写小说的你</span><h1>说出一个想法，剩下的交给我们。</h1><p>系统会自动准备故事、持续写作、检查质量，并在完成后提供整本下载。</p></section>
    <div className={styles.grid}>
      <form className={styles.card} onSubmit={submit}>
        <h2>开始一本新作品</h2>
        <label>你的故事想法<textarea autoFocus rows={7} value={brief} onChange={(e) => setBrief(e.target.value)} placeholder="例如：一个守钟人必须在黎明前兑现许下多年的承诺。" /></label>
        <button type="button" className={styles.linkButton} onClick={() => setAdvanced((v) => !v)}>{advanced ? "收起可选设置" : "设置篇幅和模型（可选）"}</button>
        {advanced && <fieldset className={styles.advanced}><legend>可选设置</legend>
          <label>目标方式<select value={targetKind} onChange={(e) => setTargetKind(e.target.value as "chapters" | "words")}><option value="chapters">目标章节数</option><option value="words">目标字数</option></select></label>
          <label>目标数量<input type="number" min="1" value={target} onChange={(e) => setTarget(e.target.value)} placeholder="留空则自动决定" /></label>
          <ModelSelect label="创作模型" value={profile} onChange={setProfile} profiles={profiles} emptyLabel="使用服务默认设置" />
          <ModelSelect label="故事准备" value={planningProfile} onChange={setPlanningProfile} profiles={profiles} emptyLabel="跟随创作模型" />
          <ModelSelect label="正文写作" value={writingProfile} onChange={setWritingProfile} profiles={profiles} emptyLabel="跟随创作模型" />
          <ModelSelect label="质量检查" value={editingProfile} onChange={setEditingProfile} profiles={profiles} emptyLabel="跟随创作模型" />
        </fieldset>}
        {unavailable && <div className={styles.error}><p>{selectedModelId ? `创作模型：${modelAvailabilityLabel(selectedModel)}。` : "尚未设置默认创作模型。"}</p><button type="button" className={styles.linkButton} onClick={onSettings}>前往模型设置</button></div>}
        {createError && <div className={styles.error} role="alert"><p>{createError}</p><button type="button" className={styles.linkButton} onClick={onSettings}>检查模型设置</button></div>}
        <button className={styles.primary} disabled={creating || !brief.trim()}><Plus size={17} />{creating ? "正在启动…" : "开始自动创作"}</button>
      </form>
      <section className={styles.card}><div className={styles.cardTitle}><h2>我的作品</h2><span>{projects.length} 本</span></div>{error && <p className={styles.error} role="alert">{error.message}</p>}<div className={styles.list}>{projects.map((item) => <button type="button" key={item.project_id} onClick={() => onSelect(item.project_id)}><strong>{item.title ?? "正在取名…"}</strong><span>{authoringStatusLabel(item.status)} · {authoringStageLabel(item.stage)} · {item.completed_chapters}/{item.target_chapters} 章</span><progress aria-label={`${item.title ?? "未命名作品"}创作进度`} value={item.progress_percent} max="100" /></button>)}{projects.length === 0 && <p className={styles.muted}>还没有作品，从左边的一个想法开始吧。</p>}</div></section>
    </div>
  </main>;
}

