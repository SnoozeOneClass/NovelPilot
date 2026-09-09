import { useState, type FormEvent } from "react";
import { authoringApi } from "../../api/authoring-client";
import {
  authoringProfilesBody, isModelReady, modelAvailabilityLabel,
  type AuthoringProject, type ModelSettingsDocument, type UpdateAuthoringProfiles
} from "../../types/authoring";
import { ModelSelect } from "./ModelSelect";
import styles from "../../AuthoringApp.module.css";

interface ProjectModelsProps {
  project: AuthoringProject;
  settings?: ModelSettingsDocument;
  settingsError: Error | null;
  busy: boolean;
  onBusy: (busy: boolean) => void;
  onChanged: (project: AuthoringProject) => void;
  onRefresh: () => void;
  onSettings: () => void;
}

const roles = [
  { field: "architect_profile_id", binding: "architect", label: "故事准备" },
  { field: "writer_profile_id", binding: "writer", label: "正文写作" },
  { field: "editor_profile_id", binding: "editor", label: "质量检查" },
  { field: "arbiter_profile_id", binding: "arbiter", label: "异常处理" },
  { field: "fallback_profile_id", binding: "fallback", label: "备用模型" }
] as const;

export function ProjectModels(props: ProjectModelsProps) {
  const { project, settings, settingsError, busy, onSettings } = props;
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const canEdit = ["ready", "paused", "failure_paused"].includes(project.status);
  const profiles = settings?.profiles ?? [];
  const defaultId = project.profile_bindings.default || settings?.selected_profile_id;
  const primary = profiles.find((profile) => profile.profile_id === defaultId);
  const assignments = roles.filter((role) => project.profile_bindings[role.binding]);
  const roleFallbacks = roles.filter((role) => role.binding !== "fallback"
    && project.profile_bindings[`fallback:${role.binding}`]);

  function modelName(id: string): string {
    const model = profiles.find((profile) => profile.profile_id === id);
    return `${model?.display_name ?? id}${isModelReady(model) ? "" : `（${modelAvailabilityLabel(model)}）`}`;
  }

  return <section className={`${styles.card} ${styles.projectModels}`}>
    <div className={styles.cardTitle}><h2>本作品的模型</h2><button className={styles.linkButton} onClick={onSettings}>模型设置</button></div>
    {settingsError ? <p role="alert" className={styles.error}>{settingsError.message}，请前往模型设置重试。</p>
      : settings === undefined ? <p role="status">正在读取模型设置…</p>
      : <>
        <p>创作模型：{defaultId ? modelName(defaultId) : "尚未设置"}{!project.profile_bindings.default && " · 跟随服务默认设置"}</p>
        {assignments.length > 0 && <ul className={styles.assignments}>{assignments.map((role) => <li key={role.binding}>{role.label}：{modelName(project.profile_bindings[role.binding]!)}</li>)}</ul>}
        {roleFallbacks.length > 0 && <ul className={styles.assignments}>{roleFallbacks.map((role) => <li key={role.binding}>{role.label}的备用模型：{modelName(project.profile_bindings[`fallback:${role.binding}`]!)}</li>)}</ul>}
        {!isModelReady(primary) && <p className={styles.error}>创作模型当前不可用，请前往模型设置补充配置并验证，或为本作品选择可用模型。</p>}
      </>}
    {canEdit && settings && <button type="button" className={styles.linkButton} disabled={busy} onClick={() => setEditing(!editing)}>{editing ? "收起模型调整" : "调整本作品模型"}</button>}
    {project.status === "running" && <p className={styles.muted}>如需更换本作品的模型，请先暂停创作。</p>}
    {error && <p role="alert" className={styles.error}>{error}</p>}
    {editing && canEdit && settings && <ProjectModelForm {...props} settings={settings} onError={setError} onSaved={() => setEditing(false)} />}
  </section>;
}

function ProjectModelForm({ project, settings, busy, onBusy, onChanged, onRefresh, onSaved, onError }: ProjectModelsProps & { settings: ModelSettingsDocument; onSaved: () => void; onError: (message: string | null) => void }) {
  const [changes, setChanges] = useState<Partial<UpdateAuthoringProfiles>>({});
  const current = authoringProfilesBody(project.profile_bindings);
  const input = { ...current, ...changes };
  const update = (field: keyof UpdateAuthoringProfiles, value: string) => setChanges((previous) => ({ ...previous, [field]: value || null }));

  async function save(event: FormEvent) {
    event.preventDefault();
    onBusy(true); onError(null);
    try {
      // PUT replaces the entire binding set. Merge edits with the latest decoded project,
      // including optional assignments that the user did not open or change.
      onChanged(await authoringApi.updateProfiles(project.project_id, input));
      onSaved();
    } catch (caught) {
      onError(caught instanceof Error ? caught.message : "模型调整失败，请重试。");
      onRefresh();
    } finally { onBusy(false); }
  }

  return <form onSubmit={(event) => void save(event)} className={styles.bindingForm}>
    <p className={styles.muted}>保存后用于后续创作。正在处理的请求会继续使用原来的模型。</p>
    <fieldset disabled={busy}>
      <ModelSelect label="本作品创作模型" value={input.default_profile_id ?? ""} onChange={(value) => update("default_profile_id", value)} profiles={settings.profiles} emptyLabel="使用服务默认设置" />
      <details className={styles.details}><summary>按创作分工选择（可选）</summary>
        {roles.map((role) => <ModelSelect key={role.field} label={role.label} value={input[role.field] ?? ""}
          onChange={(value) => update(role.field, value)} profiles={settings.profiles}
          emptyLabel={role.binding === "fallback" ? "不使用备用模型" : "跟随创作模型"} />)}
      </details>
    </fieldset>
    <button className={styles.primary} disabled={busy || Object.keys(changes).length === 0} type="submit">{busy ? "正在保存…" : "保存本作品模型"}</button>
  </form>;
}
