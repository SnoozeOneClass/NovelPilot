import { ModelConfiguration, validateModelProfile, type ModelProfile, type ModelScope, type ModelSlot, type EffectiveModel } from '../runtime/pi/model-config.js';
import type { ModelFormActions } from '../ui/model-settings.js';
export type RequestInput = (label: string, options?: { secret?: boolean; initial?: string }) => Promise<string | null>;
export type RequestChoice = (label: string, choices: { label: string; value: string; description?: string }[], initial?: string) => Promise<string | null>;

/** The TUI edits one draft locally; only this controller can persist it. */
export function modelFormActions(config: ModelConfiguration): ModelFormActions {
  return {
    async load(scope, slot) {
      const settings = await config.read(scope);
      let profile = settings.models[slot] ?? null;
      let source = profile ? (scope === 'book' ? '本书设置' : '全局设置') : '尚未配置';
      if (!profile) {
        try {
          const effective = await config.resolve(slot); profile = effective.profile;
          source = `${effective.source === 'book' ? '本书' : '全局'} ${effective.slot === 'default' ? '默认模型' : '角色配置'}（继承）`;
        } catch (error) { if (!(error instanceof Error) || !error.message.includes('尚未设置默认模型')) throw error; }
      }
      return { profile, source, hasApiKey: profile ? await config.hasApiKey(profile) : false };
    },
    async save(value) {
      const profile = validateModelProfile(value.profile);
      await config.read(value.scope); // Reject existing corruption before even saving a credential.
      if (!value.apiKey && !await config.hasApiKey(profile)) throw new Error('该服务尚未保存凭证，请填写 API Key');
      await config.setApiKey(profile, value.apiKey || undefined);
      await config.set(value.scope, value.slot, profile);
      return config.resolve(value.slot);
    },
  };
}

/** Gather all values before changing configuration. Secret input never enters command history. */
export async function editSettings(config: ModelConfiguration, request: RequestInput, requestChoice?: RequestChoice): Promise<EffectiveModel> {
  async function ask(label: string, initial = '', secret = false): Promise<string> {
    const value = await request(label, { initial, secret });
    if (value === null) throw new Error('已取消模型配置');
    return value.trim();
  }
  async function choose(label: string, choices: { label: string; value: string; description?: string }[], initial: string) {
    if (!requestChoice) return ask(label, initial);
    const value = await requestChoice(label, choices, initial);
    if (value === null) throw new Error('已取消模型配置');
    return value;
  }
  const scope = await choose('模型设置 · 保存范围', [
    { label: '仅本书', value: 'book', description: '只影响当前小说' },
    { label: '全局默认', value: 'global', description: '供所有未单独配置的小说使用' },
  ], 'book');
  if (scope !== 'book' && scope !== 'global') throw new Error('保存范围须为 book 或 global');
  const slot = await choose('模型设置 · 使用角色', [
    { label: '默认模型', value: 'default', description: '讨论、意见判断及未配置的角色' },
    { label: '规划', value: 'planner' }, { label: '写作', value: 'writer' }, { label: '评审', value: 'editor' },
  ], 'default');
  if (!['default', 'planner', 'writer', 'editor'].includes(slot)) throw new Error('角色无效');
  let current: ModelProfile | undefined;
  try { current = (await config.resolve(slot as ModelSlot)).profile; }
  catch (error) { if (!(error instanceof Error) || !error.message.includes('尚未设置默认模型')) throw error; }
  const provider = await ask('Provider 名称（字母、数字、横线）', current?.provider ?? 'custom');
  const api = await choose('模型设置 · 接口类型', [
    { label: 'OpenAI Chat Completions', value: 'openai-completions', description: '常见兼容服务' },
    { label: 'OpenAI Responses', value: 'openai-responses' }, { label: 'Anthropic Messages', value: 'anthropic-messages' },
  ], current?.api ?? 'openai-completions');
  const baseUrl = await ask('服务地址（例如 https://你的服务/v1）', current?.baseUrl ?? '');
  const id = await ask('模型标识', current?.id ?? '');
  const contextWindow = Number(await ask('上下文容量（token）', String(current?.contextWindow ?? 32768)));
  const maxTokens = Number(await ask('单次最大输出（token）', String(current?.maxTokens ?? 4096)));
  const thinkingLevel = await choose('模型设置 · 推理强度', [
    { label: '关闭', value: 'off' }, { label: '最低', value: 'minimal' }, { label: '低', value: 'low' }, { label: '中', value: 'medium' }, { label: '高', value: 'high' },
  ], current?.thinkingLevel ?? 'off');
  const sameModel = current && current.provider === provider && current.api === api && current.baseUrl === baseUrl.replace(/\/$/, '') && current.id === id;
  const profile = validateModelProfile({ provider, api, baseUrl, id, contextWindow, maxTokens, thinkingLevel,
    reasoning: thinkingLevel !== 'off', ...(sameModel && current?.pricing ? { pricing: current.pricing } : {}) });
  const key = await ask('API Key（隐藏输入；留空保留已有凭证）', '', true);
  if (key) await config.setApiKey(profile, key);
  else if (!await config.hasApiKey(profile)) throw new Error('该服务地址没有可保留的凭证，请填写 API Key');
  await config.set(scope as ModelScope, slot as ModelSlot, profile);
  return config.resolve(slot as ModelSlot);
}
