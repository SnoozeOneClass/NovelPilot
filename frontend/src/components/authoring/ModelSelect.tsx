import { isModelReady, modelAvailabilityLabel, type ModelSettingsProfile } from "../../types/authoring";

interface ModelSelectProps {
  label: string;
  value: string;
  onChange: (value: string) => void;
  profiles: ModelSettingsProfile[];
  emptyLabel: string;
}

export function ModelSelect({ label, value, onChange, profiles, emptyLabel }: ModelSelectProps) {
  return <label>{label}
    <select value={value} onChange={(event) => onChange(event.target.value)}>
      <option value="">{emptyLabel}</option>
      {value && !profiles.some((profile) => profile.profile_id === value)
        && <option value={value} disabled>{value} · 模型不存在</option>}
      {profiles.map((profile) => <option key={profile.profile_id} value={profile.profile_id} disabled={!isModelReady(profile)}>
        {profile.display_name} · {profile.model_id}{isModelReady(profile) ? "" : ` · ${modelAvailabilityLabel(profile)}`}
      </option>)}
    </select>
  </label>;
}
