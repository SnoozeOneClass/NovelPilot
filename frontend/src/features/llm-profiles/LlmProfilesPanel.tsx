import { Check, KeyRound, Pencil, PlugZap, Plus, Save } from "lucide-react";
import { useEffect, useState } from "react";
import { api, formatApiError } from "../../api/client";
import { formatProtocol } from "../../types/display";
import type { LlmProfileMutation, LlmProfilesDocument, LlmProfilePublic } from "../../types/domain";
import styles from "./LlmProfilesPanel.module.css";

interface LlmProfilesPanelProps {
  onProfilesChanged?: (profiles: LlmProfilesDocument) => void;
}

const emptyForm: LlmProfileMutation = {
  id: "",
  name: "",
  protocol: "openai-compatible",
  base_url: "https://api.openai.com/v1",
  api_key: "",
  model: "",
  request_options: {},
  enabled: true
};

export function LlmProfilesPanel({ onProfilesChanged }: LlmProfilesPanelProps) {
  const [profiles, setProfiles] = useState<LlmProfilesDocument | null>(null);
  const [form, setForm] = useState<LlmProfileMutation>(emptyForm);
  const [requestOptionsText, setRequestOptionsText] = useState("{}");
  const [editingExisting, setEditingExisting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(false);
  const [testingProfileId, setTestingProfileId] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  useEffect(() => {
    void refreshProfiles();
  }, []);

  async function refreshProfiles() {
    setLoading(true);
    try {
      const nextProfiles = await api.profiles();
      setProfiles(nextProfiles);
      onProfilesChanged?.(nextProfiles);
      setNotice(null);
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setLoading(false);
    }
  }

  function editProfile(profile: LlmProfilePublic) {
    setEditingExisting(true);
    setForm({
      id: profile.id,
      name: profile.name,
      protocol: profile.protocol,
      base_url: profile.base_url,
      api_key: "",
      model: profile.model,
      request_options: profile.request_options,
      enabled: profile.enabled
    });
    setRequestOptionsText(JSON.stringify(profile.request_options, null, 2));
  }

  async function saveProfile() {
    let requestOptions: Record<string, unknown>;
    try {
      const parsed: unknown = JSON.parse(requestOptionsText || "{}");
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("请求参数必须是 JSON 对象。");
      }
      requestOptions = parsed as Record<string, unknown>;
    } catch (error) {
      setNotice({ kind: "error", text: error instanceof Error ? error.message : "请求参数不是有效 JSON。" });
      return;
    }
    setSaving(true);
    setNotice(null);
    try {
      await api.upsertProfile({
        ...form,
        request_options: requestOptions,
        api_key: form.api_key?.trim() || null
      });
      const nextProfiles = await api.profiles();
      setProfiles(nextProfiles);
      onProfilesChanged?.(nextProfiles);
      setForm(emptyForm);
      setRequestOptionsText("{}");
      setEditingExisting(false);
      setNotice({ kind: "success", text: "LLM 配置已保存。" });
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setSaving(false);
    }
  }

  async function selectProfile(profileId: string) {
    setSaving(true);
    setNotice(null);
    try {
      const nextProfiles = await api.selectProfile(profileId);
      setProfiles(nextProfiles);
      onProfilesChanged?.(nextProfiles);
      setNotice({ kind: "success", text: "已切换当前 LLM 配置。" });
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setSaving(false);
    }
  }

  async function testProfile(profileId: string) {
    setTestingProfileId(profileId);
    setNotice(null);
    try {
      const result = await api.testProfile(profileId);
      setNotice({
        kind: "success",
        text: `Agent 能力测试通过：${result.model_snapshot || result.profile_id}（Tool Calling + Structured Output）`
      });
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      try {
        const nextProfiles = await api.profiles();
        setProfiles(nextProfiles);
        onProfilesChanged?.(nextProfiles);
      } catch {
        // Keep the capability test result or failure visible; a later refresh retries this read.
      }
      setTestingProfileId(null);
    }
  }

  return (
    <section className={styles.view}>
      <header className={styles.heading}>
        <div>
          <h1>设置与模型</h1>
          <p>配置保存在本机全局文件中；小说项目只记录 profile_id 与模型快照。</p>
        </div>
        <button className={styles.outlineButton} onClick={() => { setForm(emptyForm); setRequestOptionsText("{}"); setEditingExisting(false); }}>
          <Plus size={16} /> 新建配置
        </button>
      </header>

      {notice && <p className={notice.kind === "error" ? styles.error : styles.success}>{notice.text}</p>}

      <div className={styles.grid}>
        <section className={styles.catalog}>
          <h2>模型配置</h2>
          <div>
            {profiles?.profiles.map((profile) => {
              const active = profile.id === profiles.active_profile_id;
              return (
                <article key={profile.id} className={active ? styles.active : ""}>
                  <span className={styles.providerMark}><KeyRound size={18} /></span>
                  <div>
                    <div className={styles.nameLine}>
                      <strong>{profile.name}</strong>
                      {active && <span className={styles.activeBadge}><Check size={13} /> 当前配置</span>}
                    </div>
                    <CapabilityBadge profile={profile} />
                    <p>{formatProtocol(profile.protocol)} · {profile.model}</p>
                    <small>{profile.base_url} · {profile.has_api_key ? "已保存密钥" : "未保存密钥"}</small>
                  </div>
                  <div className={styles.profileActions}>
                    <button title="设为当前配置" disabled={saving || loading || testingProfileId !== null || active} onClick={() => void selectProfile(profile.id)}><Check size={16} /></button>
                    <button title="测试连接" disabled={saving || loading || testingProfileId !== null || !profile.has_api_key} onClick={() => void testProfile(profile.id)}><PlugZap size={16} /></button>
                    <button title="编辑配置" disabled={saving || testingProfileId !== null} onClick={() => editProfile(profile)}><Pencil size={16} /></button>
                  </div>
                </article>
              );
            })}
            {profiles?.profiles.length === 0 && !loading && <p className={styles.empty}>还没有配置可用的 LLM。</p>}
          </div>
        </section>

        <section className={styles.editor}>
          <header>
            <div><h2>{editingExisting ? "编辑配置" : "新建配置"}</h2><p>兼容 OpenAI 与 Anthropic 协议的 Base URL。</p></div>
          </header>
          <div className={styles.formGrid}>
            <label><span>Profile ID</span><input value={form.id} disabled={editingExisting} onChange={(event) => setForm({ ...form, id: event.target.value })} placeholder="main" /></label>
            <label><span>显示名称</span><input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="主要写作模型" /></label>
            <label><span>协议</span><select value={form.protocol} onChange={(event) => setForm({ ...form, protocol: event.target.value as LlmProfileMutation["protocol"] })}><option value="openai-compatible">OpenAI 兼容协议</option><option value="anthropic-compatible">Anthropic 兼容协议</option></select></label>
            <label><span>模型名</span><input value={form.model} onChange={(event) => setForm({ ...form, model: event.target.value })} placeholder="gpt-4.1" /></label>
            <label className={styles.wide}><span>Base URL</span><input value={form.base_url} onChange={(event) => setForm({ ...form, base_url: event.target.value })} placeholder="https://api.example.com/v1" /></label>
            <label className={styles.wide}><span>API Key</span><input value={form.api_key ?? ""} onChange={(event) => setForm({ ...form, api_key: event.target.value })} placeholder={editingExisting ? "留空则保留原密钥" : "API Key"} type="password" /></label>
            <label className={styles.wide}><span>额外请求参数（JSON）</span><textarea aria-label="额外请求参数（JSON）" value={requestOptionsText} onChange={(event) => setRequestOptionsText(event.target.value)} placeholder={'{"reasoning_effort":"high"}'} /><small>字段会合并到 Provider 请求体；model、messages/system 与 stream 由 NovelPilot 管理。Anthropic 如要求 max_tokens，请在这里显式填写。不要存放额外密钥。</small></label>
            <label className={styles.enabled}><input checked={form.enabled} type="checkbox" onChange={(event) => setForm({ ...form, enabled: event.target.checked })} /><span>启用这个配置</span></label>
          </div>
          <footer>
            <button className={styles.primaryButton} disabled={saving || loading || !form.id.trim() || !form.name.trim() || !form.model.trim()} onClick={() => void saveProfile()}>
              <Save size={16} /> {saving ? "正在保存..." : "保存配置"}
            </button>
          </footer>
        </section>
      </div>
    </section>
  );
}

function CapabilityBadge({ profile }: { profile: LlmProfilePublic }) {
  if (profile.capability_test?.ready_for_harness) {
    return <span className={`${styles.capabilityBadge} ${styles.capabilityReady}`}>Agent 能力已验证</span>;
  }
  if (profile.capability_test) {
    return <span className={`${styles.capabilityBadge} ${styles.capabilityFailed}`}>Harness 不可用</span>;
  }
  return <span className={`${styles.capabilityBadge} ${styles.capabilityUnknown}`}>尚未验证 Agent 能力</span>;
}
