import { useState, type FormEvent } from "react";
import { authoringApi } from "../../api/authoring-client";
import type { ModelSettingsDocument, ModelSettingsProfile, SaveModelProfile } from "../../types/authoring";
import styles from "../../AuthoringApp.module.css";

interface ModelProfileFormProps {
  profile?: ModelSettingsProfile;
  existingIds: string[];
  onSaved: (document: ModelSettingsDocument) => void;
  onCancel: () => void;
}

function readText(form: FormData, name: string): string {
  const value = form.get(name);
  return typeof value === "string" ? value.trim() : "";
}

function positiveInteger(form: FormData, name: string, label: string): number {
  const value = Number(readText(form, name));
  if (!Number.isSafeInteger(value) || value <= 0) throw new Error(`${label}必须是正整数。`);
  return value;
}

function price(form: FormData, name: string): number {
  const value = Number(readText(form, name));
  if (!Number.isFinite(value) || value < 0) throw new Error("价格必须是大于或等于零的数字。");
  return value;
}

function requestOptions(text: string, maxOutput: number, family: SaveModelProfile["api_family"]): Record<string, unknown> {
  let value: unknown;
  try { value = JSON.parse(text || "{}"); }
  catch { throw new Error("请求选项需要有效的 JSON 对象，请检查引号和逗号。"); }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("请求选项必须是 JSON 对象，例如 {}。");
  }
  const options = value as Record<string, unknown>;
  if ("max_output_tokens" in options) throw new Error("请求选项中的输出上限请使用 max_tokens。");
  if ("max_tokens" in options && options.max_tokens !== maxOutput) {
    throw new Error("请求选项的 max_tokens 必须与最大输出 token 数一致。");
  }
  // Anthropic requires this portable option. Preserve all other options verbatim.
  if (family === "anthropic_messages" && !("max_tokens" in options)) options.max_tokens = maxOutput;
  return options;
}

export function ModelProfileForm({ profile, existingIds, onSaved, onCancel }: ModelProfileFormProps) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(!profile?.context_window || !profile?.max_output_tokens);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setError(null);
    let input: SaveModelProfile;
    let id: string;
    try {
      id = profile?.profile_id ?? readText(form, "profile_id");
      if (!id || id.length > 200) throw new Error("请填写不超过 200 字的配置标识。");
      if (!profile && existingIds.includes(id)) throw new Error("配置标识已存在，请使用其他标识或编辑已有模型。");
      const displayName = readText(form, "display_name");
      const modelId = readText(form, "model_id");
      if (!displayName || !modelId) throw new Error("请填写显示名称和模型名称。");
      const baseUrl = readText(form, "base_url");
      let url: URL;
      try { url = new URL(baseUrl); }
      catch { throw new Error("服务地址需要完整的 http:// 或 https:// 地址。"); }
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
        throw new Error("服务地址需要 HTTP(S) 地址，且不能包含密钥、登录信息、查询参数或片段。");
      }
      const family = readText(form, "api_family");
      if (family !== "openai_responses" && family !== "anthropic_messages") throw new Error("请选择支持的连接方式。");
      const path = url.pathname.replace(/\/+$/, "");
      if (family === "openai_responses" && !path.endsWith("/v1")) throw new Error("OpenAI Responses 服务地址需要以 /v1 结尾。");
      if (family === "anthropic_messages" && path.endsWith("/v1")) throw new Error("Anthropic Messages 服务地址请去掉末尾的 /v1。");
      const key = readText(form, "api_key");
      if (!profile?.has_api_key && !key) throw new Error("请填写 API 密钥。");
      const contextWindow = positiveInteger(form, "context_window", "上下文窗口");
      const maxOutput = positiveInteger(form, "max_output_tokens", "最大输出 token 数");
      if (maxOutput >= contextWindow) throw new Error("最大输出 token 数必须小于上下文窗口。");
      input = {
        display_name: displayName, model_id: modelId, base_url: baseUrl, api_family: family,
        enabled: form.has("enabled"), ...(key ? { api_key: key } : {}),
        context_window: contextWindow, max_output_tokens: maxOutput,
        request_options: requestOptions(readText(form, "request_options"), maxOutput, family),
        input_price_per_million: price(form, "input_price_per_million"),
        output_price_per_million: price(form, "output_price_per_million"),
        cache_price_per_million: price(form, "cache_price_per_million")
      };
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "请检查设置。");
      setAdvancedOpen(true);
      return;
    }
    setSaving(true);
    // Keep the write-only key in this form/request only, outside query caches and browser storage.
    try { onSaved(await authoringApi.saveModelProfile(id, input)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "模型设置保存失败，请重试。"); }
    finally { setSaving(false); }
  }

  return <form className={styles.card} onSubmit={(event) => void submit(event)} noValidate>
    <h2>{profile ? `编辑模型：${profile.display_name}` : "添加模型"}</h2>
    <fieldset disabled={saving}>
      {!profile && <label>配置标识<input name="profile_id" maxLength={200} placeholder="例如 my-model" /></label>}
      <label>显示名称<input name="display_name" defaultValue={profile?.display_name ?? ""} placeholder="例如 我的创作模型" /></label>
      <label>连接方式<select name="api_family" defaultValue={profile?.api_family ?? "openai_responses"}>
        <option value="openai_responses">OpenAI Responses</option><option value="anthropic_messages">Anthropic Messages</option>
      </select></label>
      <label>服务地址<input name="base_url" type="url" defaultValue={profile?.base_url ?? ""} placeholder="https://…" spellCheck={false} /></label>
      <label>模型名称<input name="model_id" defaultValue={profile?.model_id ?? ""} spellCheck={false} /></label>
      <label>API 密钥<input name="api_key" type="password" autoComplete="new-password" defaultValue="" spellCheck={false}
        aria-describedby="key-help" placeholder={profile?.has_api_key ? "留空保留已保存的密钥" : "填写服务商提供的密钥"} /></label>
      <p id="key-help" className={styles.muted}>{profile?.has_api_key ? "密钥已保存。这里不会显示原值，留空保存可继续使用。" : "尚未保存密钥。"}</p>
      <label className={styles.checkbox}><input name="enabled" type="checkbox" defaultChecked={profile?.enabled ?? true} />启用此模型</label>
      <details open={advancedOpen} onToggle={(event) => setAdvancedOpen(event.currentTarget.open)} className={styles.details}>
        <summary>高级设置：容量、价格和请求选项</summary>
        <p className={styles.muted}>容量请按模型服务提供的信息填写。价格用于统计费用，可以留为 0。</p>
        <div className={styles.advanced}>
          <label>上下文窗口（token）<input name="context_window" type="number" min={1} step={1} defaultValue={profile?.context_window ?? ""} /></label>
          <label>最大输出 token 数<input name="max_output_tokens" type="number" min={1} step={1} defaultValue={profile?.max_output_tokens ?? ""} /></label>
          <label>输入价格（每百万 token）<input name="input_price_per_million" type="number" min={0} step="any" defaultValue={profile?.input_price_per_million ?? 0} /></label>
          <label>输出价格（每百万 token）<input name="output_price_per_million" type="number" min={0} step="any" defaultValue={profile?.output_price_per_million ?? 0} /></label>
          <label>缓存输入价格（每百万 token）<input name="cache_price_per_million" type="number" min={0} step="any" defaultValue={profile?.cache_price_per_million ?? 0} /></label>
        </div>
        <label>请求选项（JSON）<textarea name="request_options" rows={5} spellCheck={false} defaultValue={JSON.stringify(profile?.request_options ?? {}, null, 2)} /></label>
        <p className={styles.muted}>可填写服务支持的模型选项；max_tokens 如有填写，需与最大输出一致。密钥请填在上方密钥栏。服务会校验选项后再保存。</p>
      </details>
    </fieldset>
    {error && <p role="alert" className={styles.error}>{error}</p>}
    <div className={styles.formActions}>
      <button className={styles.primary} disabled={saving} type="submit">{saving ? "正在保存…" : "保存模型设置"}</button>
      <button type="button" className={styles.secondary} disabled={saving} onClick={onCancel}>取消编辑</button>
    </div>
  </form>;
}
