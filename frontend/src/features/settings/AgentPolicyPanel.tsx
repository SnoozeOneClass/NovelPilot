import { Check, ShieldAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, formatApiError } from "../../api/client";
import type {
  AgentPolicy,
  LlmProfilePublic,
  LlmProfilesDocument,
  ProjectSummary
} from "../../types/domain";
import styles from "./SettingsView.module.css";

interface AgentPolicyPanelProps {
  project: ProjectSummary;
  profiles: LlmProfilesDocument | null;
  locked: boolean;
  onProjectChanged: (project: ProjectSummary) => void;
}

const defaultPolicy: AgentPolicy = {
  schema_version: 1,
  book_profile_id: null,
  story_arc_profile_id: null,
  chapter_profile_id: null,
  evaluator_profile_id: null,
  book_max_turns: 20,
  story_arc_max_turns: 20,
  chapter_max_turns: 30,
  tool_schema_repair_limit: 2,
  semantic_revision_limit: 10,
  transport_retry_limit: 3
};

const bindings: Array<{
  key: "book_profile_id" | "story_arc_profile_id" | "chapter_profile_id" | "evaluator_profile_id";
  label: string;
  detail: string;
}> = [
  { key: "book_profile_id", label: "全书 Loop", detail: "讨论、方向候选与全书级修订" },
  { key: "story_arc_profile_id", label: "故事弧 Loop", detail: "当前滚动故事弧的创建与修订" },
  { key: "chapter_profile_id", label: "章节 Loop", detail: "章节规划、正文 Tool 与候选状态补丁" },
  { key: "evaluator_profile_id", label: "统一评测器", detail: "一次原生 Structured Output 语义评测" }
];

export function AgentPolicyPanel({
  project,
  profiles,
  locked,
  onProjectChanged
}: AgentPolicyPanelProps) {
  const [policy, setPolicy] = useState<AgentPolicy>(project.metadata.agent_policy ?? defaultPolicy);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<{ kind: "success" | "error"; text: string } | null>(null);

  useEffect(() => {
    setPolicy(project.metadata.agent_policy ?? defaultPolicy);
  }, [project]);

  const activeProfile = useMemo(
    () => profiles?.profiles.find((profile) => profile.id === profiles.active_profile_id) ?? null,
    [profiles]
  );
  const defaultReady = activeProfile?.capability_test?.ready_for_harness === true;

  function updateNumber(key: keyof AgentPolicy, value: string, minimum: number, maximum: number) {
    const parsed = Number.parseInt(value, 10);
    if (!Number.isFinite(parsed)) return;
    setPolicy({ ...policy, [key]: Math.min(maximum, Math.max(minimum, parsed)) });
  }

  async function save() {
    if (saving || locked) return;
    setSaving(true);
    setNotice(null);
    try {
      const nextProject = await api.updateAgentPolicy(policy);
      onProjectChanged(nextProject);
      setNotice({ kind: "success", text: "Agent 模型绑定与预算已保存。" });
    } catch (error) {
      setNotice({ kind: "error", text: formatApiError(error) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className={styles.agentPolicy}>
      <header>
        <div><p>Agent Runtime</p><h3>模型绑定与有界预算</h3></div>
        <span className={defaultReady ? styles.readyBadge : styles.missingBadge}>
          {defaultReady ? <Check size={13} /> : <ShieldAlert size={13} />}
          {defaultReady ? "默认模型能力已验证" : "默认模型尚未通过能力测试"}
        </span>
      </header>
      <p className={styles.agentPolicyIntro}>
        默认情况下三个 Loop 和评测器共用当前模型；只在明确选择覆盖模型时分开。
        可选模型必须同时通过 Tool Calling 与 Structured Output 测试。
      </p>
      {notice && <p className={notice.kind === "error" ? styles.error : styles.success}>{notice.text}</p>}
      {locked && <p className={styles.warning}>Harness 正在运行，需要等到安全检查点后才能修改 Agent 配置。</p>}

      <div className={styles.bindingGrid}>
        {bindings.map((binding) => (
          <label key={binding.key}>
            <span><strong>{binding.label}</strong><small>{binding.detail}</small></span>
            <select
              aria-label={`${binding.label} 模型`}
              value={policy[binding.key] ?? ""}
              disabled={saving || locked}
              onChange={(event) => setPolicy({ ...policy, [binding.key]: event.target.value || null })}
            >
              <option value="">跟随当前默认模型</option>
              {profiles?.profiles.map((profile) => (
                <ProfileOption key={profile.id} profile={profile} />
              ))}
            </select>
          </label>
        ))}
      </div>

      <div className={styles.budgetGrid}>
        <NumberField label="全书单次激活回合" value={policy.book_max_turns} min={1} max={200} disabled={saving || locked} onChange={(value) => updateNumber("book_max_turns", value, 1, 200)} />
        <NumberField label="故事弧单次激活回合" value={policy.story_arc_max_turns} min={1} max={200} disabled={saving || locked} onChange={(value) => updateNumber("story_arc_max_turns", value, 1, 200)} />
        <NumberField label="章节单次激活回合" value={policy.chapter_max_turns} min={1} max={200} disabled={saving || locked} onChange={(value) => updateNumber("chapter_max_turns", value, 1, 200)} />
        <NumberField label="Tool 单次动作修正" value={policy.tool_schema_repair_limit} min={0} max={20} disabled={saving || locked} onChange={(value) => updateNumber("tool_schema_repair_limit", value, 0, 20)} />
        <NumberField label="语义单候选修订" value={policy.semantic_revision_limit} min={0} max={20} disabled={saving || locked} onChange={(value) => updateNumber("semantic_revision_limit", value, 0, 20)} />
        <NumberField label="Provider 单次请求重试" value={policy.transport_retry_limit} min={0} max={20} disabled={saving || locked} onChange={(value) => updateNumber("transport_retry_limit", value, 0, 20)} />
      </div>
      <footer>
        <button type="button" disabled={saving || locked} onClick={() => void save()}>
          {saving ? "正在保存…" : "保存 Agent 配置"}
        </button>
      </footer>
    </section>
  );
}

function ProfileOption({ profile }: { profile: LlmProfilePublic }) {
  const ready = profile.enabled && profile.capability_test?.ready_for_harness === true;
  return (
    <option value={profile.id} disabled={!ready}>
      {profile.name} · {profile.model}{ready ? "" : "（能力未验证）"}
    </option>
  );
}

function NumberField({ label, value, min, max, disabled, onChange }: {
  label: string;
  value: number;
  min: number;
  max: number;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  const [draft, setDraft] = useState(String(value));

  useEffect(() => {
    setDraft(String(value));
  }, [value]);

  return (
    <label>
      <span>{label}</span>
      <input
        aria-label={label}
        type="number"
        value={draft}
        min={min}
        max={max}
        disabled={disabled}
        onChange={(event) => {
          const nextDraft = event.target.value;
          setDraft(nextDraft);
          if (nextDraft !== "") onChange(nextDraft);
        }}
        onBlur={() => {
          if (draft === "") setDraft(String(value));
        }}
      />
    </label>
  );
}
