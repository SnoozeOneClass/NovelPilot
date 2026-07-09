import { Activity, CheckCircle2, KeyRound, Pencil, Plus, Save } from "lucide-react";
import { useEffect, useState } from "react";
import { api, formatApiError } from "../../api/client";
import { formatProtocol } from "../../types/display";
import type { LlmProfileMutation, LlmProfilesDocument, LlmProfilePublic } from "../../types/domain";

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
  enabled: true
};

export function LlmProfilesPanel({ onProfilesChanged }: LlmProfilesPanelProps) {
  const [profiles, setProfiles] = useState<LlmProfilesDocument | null>(null);
  const [form, setForm] = useState<LlmProfileMutation>(emptyForm);
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
      enabled: profile.enabled
    });
  }

  async function saveProfile() {
    setSaving(true);
    setNotice(null);
    try {
      await api.upsertProfile({
        ...form,
        api_key: form.api_key?.trim() || null
      });
      const nextProfiles = await api.profiles();
      setProfiles(nextProfiles);
      onProfilesChanged?.(nextProfiles);
      setForm(emptyForm);
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
        text: `连接测试通过：${result.model_snapshot || result.profile_id}`
      });
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setTestingProfileId(null);
    }
  }

  return (
    <section className="subpanel">
      <div className="panel-title">
        <KeyRound size={18} />
        <span>LLM 配置</span>
      </div>
      <div className="profile-list">
        {profiles?.profiles.map((profile) => (
          <div key={profile.id} className={profile.id === profiles.active_profile_id ? "active" : ""}>
            <div>
              <strong>{profile.name}</strong>
              <span>{formatProtocol(profile.protocol)}</span>
              <small>
                {profile.model} | {profile.has_api_key ? "已保存密钥" : "未保存密钥"}
              </small>
            </div>
            <div className="profile-row-actions">
              <button
                title="设为当前配置"
                disabled={saving || loading || testingProfileId !== null}
                onClick={() => selectProfile(profile.id)}
              >
                <CheckCircle2 size={16} />
              </button>
              <button
                title="测试连接"
                disabled={saving || loading || testingProfileId !== null || !profile.has_api_key}
                onClick={() => testProfile(profile.id)}
              >
                <Activity size={16} />
              </button>
              <button
                title="编辑配置"
                disabled={saving || testingProfileId !== null}
                onClick={() => editProfile(profile)}
              >
                <Pencil size={16} />
              </button>
            </div>
          </div>
        ))}
        {profiles?.profiles.length === 0 && !loading && (
          <p className="muted">还没有配置 LLM。</p>
        )}
      </div>
      {notice && <p className={`notice-banner compact ${notice.kind}`}>{notice.text}</p>}
      <div className="profile-form">
        <div className="profile-form-title">
          <span>{editingExisting ? "编辑配置" : "新建配置"}</span>
          <button
            title="新建配置"
            onClick={() => {
              setForm(emptyForm);
              setEditingExisting(false);
            }}
          >
            <Plus size={16} />
          </button>
        </div>
        <input
          value={form.id}
          disabled={editingExisting}
          onChange={(event) => setForm({ ...form, id: event.target.value })}
          placeholder="profile-id"
        />
        <input
          value={form.name}
          onChange={(event) => setForm({ ...form, name: event.target.value })}
          placeholder="配置名称"
        />
        <select
          value={form.protocol}
          onChange={(event) =>
            setForm({
              ...form,
              protocol: event.target.value as LlmProfileMutation["protocol"]
            })
          }
        >
          <option value="openai-compatible">OpenAI 兼容协议</option>
          <option value="anthropic-compatible">Anthropic 兼容协议</option>
        </select>
        <input
          value={form.base_url}
          onChange={(event) => setForm({ ...form, base_url: event.target.value })}
          placeholder="https://api.example.com/v1"
        />
        <input
          value={form.model}
          onChange={(event) => setForm({ ...form, model: event.target.value })}
          placeholder="模型名"
        />
        <input
          value={form.api_key ?? ""}
          onChange={(event) => setForm({ ...form, api_key: event.target.value })}
          placeholder={editingExisting ? "留空则保留原密钥" : "API Key"}
          type="password"
        />
        <label className="profile-enabled">
          <input
            checked={form.enabled}
            type="checkbox"
            onChange={(event) => setForm({ ...form, enabled: event.target.checked })}
          />
          启用
        </label>
        <button className="primary-button" disabled={saving || loading} onClick={saveProfile}>
          <Save size={16} />
          保存
        </button>
      </div>
    </section>
  );
}
