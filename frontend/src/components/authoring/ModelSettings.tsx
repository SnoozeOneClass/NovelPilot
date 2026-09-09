import { Settings } from "lucide-react";
import { useState } from "react";
import { authoringApi } from "../../api/authoring-client";
import { isModelReady, modelAvailabilityLabel, type ModelSettingsDocument } from "../../types/authoring";
import { ThemeToggle } from "../ui/ThemeToggle";
import { ModelProfileForm } from "./ModelProfileForm";
import styles from "../../AuthoringApp.module.css";

interface ModelSettingsProps {
  document?: ModelSettingsDocument;
  loading: boolean;
  error: Error | null;
  onRetry: () => void;
  onChanged: (document: ModelSettingsDocument) => void;
  onBack: () => void;
}

export function ModelSettings({ document, loading, error, onRetry, onChanged, onBack }: ModelSettingsProps) {
  const [editing, setEditing] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const profiles = document?.profiles ?? [];
  const selected = profiles.find((profile) => profile.profile_id === document?.selected_profile_id);

  async function action(id: string, operation: () => Promise<ModelSettingsDocument>, success: string) {
    setBusy(id); setActionError(null); setNotice(null);
    try { onChanged(await operation()); setNotice(success); }
    catch (caught) {
      setActionError(caught instanceof Error ? caught.message : "操作失败，请重试。");
      onRetry();
    }
    finally { setBusy(null); }
  }

  return <main className={styles.shell}>
    <header className={styles.header}><div><Settings /><strong>模型设置</strong></div><nav>
      <button type="button" onClick={onBack}>返回创作</button><ThemeToggle />
    </nav></header>
    <section className={styles.settingsIntro}><h1>设置自动创作使用的模型</h1>
      <p className={styles.muted}>保存连接信息后，点击验证连接与能力。验证通过的模型可设为默认，新作品即可直接开始。</p>
      {document && <p>新作品默认使用：<strong>{selected?.display_name ?? document.selected_profile_id ?? "尚未设置"}</strong>
        {document.selected_profile_id && <span> · {modelAvailabilityLabel(selected)}</span>}</p>}
    </section>
    {loading && <p role="status">正在读取模型设置…</p>}
    {error && <div className={styles.error} role="alert"><p>{error.message}</p><button className={styles.linkButton} onClick={onRetry}>重新读取模型设置</button></div>}
    {actionError && <p className={styles.error} role="alert">{actionError}</p>}
    {notice && <p role="status" className={styles.muted}>{notice}</p>}
    {document && <div className={styles.settingsGrid}>
      <section className={styles.card}><div className={styles.cardTitle}><h2>已配置模型</h2>
        <button className={styles.linkButton} disabled={busy !== null} onClick={() => { setAdding(true); setEditing(null); setNotice(null); }}>添加模型</button>
      </div>
        {profiles.length === 0 && <p className={styles.muted}>还没有模型，添加一个后验证连接即可使用。</p>}
        <div className={styles.modelList}>{profiles.map((profile) => <article key={profile.profile_id} className={styles.modelItem} aria-label={`模型 ${profile.display_name}`}>
          <h3>{profile.display_name}{document.selected_profile_id === profile.profile_id && <span className={styles.badge}>默认</span>}</h3>
          <p className={styles.muted}>{profile.model_id}</p><p>{modelAvailabilityLabel(profile)}</p>
          <p className={styles.muted}>{profile.has_api_key ? "已保存密钥" : "未设置密钥"}</p>
          <div className={styles.formActions}>
            <button className={styles.secondary} disabled={busy !== null} onClick={() => { setEditing(profile.profile_id); setAdding(false); setNotice(null); }}>编辑</button>
            <button className={styles.secondary} disabled={busy !== null || editing !== null || adding || !profile.enabled || !profile.has_api_key || profile.context_window === null || profile.max_output_tokens === null}
              onClick={() => void action(profile.profile_id, () => authoringApi.validateModelProfile(profile.profile_id), "连接与能力验证通过，可以设为默认模型。")}>{busy === profile.profile_id ? "正在处理…" : "验证连接与能力"}</button>
            <button className={styles.secondary} disabled={busy !== null || editing !== null || adding || !isModelReady(profile) || document.selected_profile_id === profile.profile_id}
              onClick={() => void action(profile.profile_id, () => authoringApi.selectDefaultProfile(profile.profile_id), "默认模型已更新，新作品会使用此设置。")}>设为默认</button>
          </div>
        </article>)}</div>
      </section>
      {(adding || editing) && <ModelProfileForm key={adding ? "new" : editing}
        profile={profiles.find((profile) => profile.profile_id === editing)} existingIds={profiles.map((profile) => profile.profile_id)}
        onCancel={() => { setAdding(false); setEditing(null); }}
        onSaved={(updated) => { onChanged(updated); setAdding(false); setEditing(null); setActionError(null); setNotice("模型设置已保存，请验证连接与能力后使用。"); }} />}
    </div>}
  </main>;
}
