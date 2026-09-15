import { createHash } from 'node:crypto';
import { mkdir, open, readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import { tryLock, unlock } from 'fs-native-extensions';
import { InMemoryCredentialStore, InMemoryModelsStore, type ModelCostRates } from '@earendil-works/pi-ai';
import { ModelRuntime } from '@earendil-works/pi-coding-agent';
import { MutationQueue, writeFileAtomic } from '../../store/io.js';

export type ModelRole = 'default' | 'planner' | 'writer' | 'editor' | 'discussion' | 'arbiter';
export type ModelScope = 'global' | 'book';
export type ModelSlot = Exclude<ModelRole, 'discussion' | 'arbiter'>;
export type ModelThinking = 'off' | 'minimal' | 'low' | 'medium' | 'high';
export interface ModelProfile {
  provider: string;
  id: string;
  api: 'openai-completions' | 'openai-responses' | 'anthropic-messages';
  baseUrl: string;
  contextWindow: number;
  maxTokens: number;
  reasoning: boolean;
  thinkingLevel: ModelThinking;
  /** Explicit rates per million tokens; absent means unknown, not free. */
  pricing?: ModelCostRates;
}
export interface ModelSettings { version: 1; models: Partial<Record<ModelSlot, ModelProfile>> }
export interface EffectiveModel {
  profile: ModelProfile;
  role: ModelRole;
  slot: ModelSlot;
  source: ModelScope;
  path: string;
}
const slots: ModelSlot[] = ['default', 'planner', 'writer', 'editor'];
const queues = new Map<string, MutationQueue>();

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('模型配置必须是对象');
  return value as Record<string, unknown>;
}
function only(value: Record<string, unknown>, keys: readonly string[]): void {
  if (Object.keys(value).some((key) => !keys.includes(key))) throw new Error('模型配置包含不支持的字段（凭证须单独保存）');
}
export function validateModelProfile(input: unknown): ModelProfile {
  const p = object(input);
  only(p, ['provider', 'id', 'api', 'baseUrl', 'contextWindow', 'maxTokens', 'reasoning', 'thinkingLevel', 'pricing']);
  if (typeof p.provider !== 'string' || !/^[a-zA-Z0-9_-]{1,80}$/.test(p.provider)) throw new Error('Provider 名称无效');
  if (typeof p.id !== 'string' || !p.id.trim() || /[\r\n]/.test(p.id)) throw new Error('模型标识无效');
  if (!['openai-completions', 'openai-responses', 'anthropic-messages'].includes(String(p.api))) throw new Error('不支持该模型协议');
  let url: URL;
  try { url = new URL(String(p.baseUrl)); } catch { throw new Error('模型服务地址无效'); }
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error('模型服务地址须使用 HTTP(S)，且不能包含凭证、查询参数或片段');
  }
  for (const field of ['contextWindow', 'maxTokens']) {
    if (!Number.isSafeInteger(p[field]) || Number(p[field]) < 1) throw new Error('上下文容量和输出上限须为正整数');
  }
  if (Number(p.maxTokens) > Number(p.contextWindow)) throw new Error('输出上限不能超过上下文容量');
  if (typeof p.reasoning !== 'boolean' || !['off', 'minimal', 'low', 'medium', 'high'].includes(String(p.thinkingLevel))) throw new Error('推理设置无效');
  if (!p.reasoning && p.thinkingLevel !== 'off') throw new Error('该模型未声明推理能力，请关闭推理强度');
  if (p.pricing !== undefined) {
    const rates = object(p.pricing);
    const keys = ['input', 'output', 'cacheRead', 'cacheWrite'];
    only(rates, keys);
    if (keys.some((k) => typeof rates[k] !== 'number' || !Number.isFinite(rates[k]) || Number(rates[k]) < 0)) throw new Error('价格须提供四项非负费率');
  }
  return structuredClone({ ...p, baseUrl: url.toString().replace(/\/$/, '') }) as unknown as ModelProfile;
}
function decodeSettings(input: unknown): ModelSettings {
  const root = object(input);
  only(root, ['version', 'models']);
  if (root.version !== 1) throw new Error('不支持的模型配置版本');
  const values = object(root.models);
  only(values, slots);
  const models: ModelSettings['models'] = {};
  for (const slot of slots) if (values[slot] !== undefined) models[slot] = validateModelProfile(values[slot]);
  return { version: 1, models };
}
async function readJson(path: string): Promise<unknown | undefined> {
  try { return JSON.parse(await readFile(path, 'utf8')) as unknown; }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined;
    // Do not expose malformed configuration/credential contents through JSON parser diagnostics.
    if (error instanceof SyntaxError) throw new Error('配置文件 JSON 损坏，未重置原文件');
    throw error;
  }
}
async function locked<T>(path: string, operation: () => Promise<T>): Promise<T> {
  let queue = queues.get(path);
  if (!queue) { queue = new MutationQueue(); queues.set(path, queue); }
  return queue.run(async () => {
    await mkdir(dirname(path), { recursive: true });
    const file = await open(`${path}.lock`, 'a+', 0o600);
    let acquired = false;
    try {
      acquired = tryLock(file.fd);
      if (!acquired) throw new Error('另一进程正在保存模型配置，请稍后重试');
      return await operation();
    } finally { if (acquired) unlock(file.fd); await file.close(); }
  });
}
/** Bind credentials to the destination as well as the user-facing provider name. */
function credentialId(profile: ModelProfile): string {
  return `novelpilot-${createHash('sha256').update(`${profile.provider}\n${profile.api}\n${profile.baseUrl}`).digest('hex')}`;
}

export class ModelConfiguration {
  readonly globalPath: string;
  readonly bookPath: string;
  readonly credentialsPath: string;
  constructor(bookDir: string, globalDir = join(homedir(), '.novelpilot')) {
    this.globalPath = join(globalDir, 'models.json');
    this.bookPath = join(bookDir, 'meta', 'models.json');
    this.credentialsPath = join(globalDir, 'credentials.json');
  }
  async read(scope: ModelScope): Promise<ModelSettings> {
    const raw = await readJson(this.path(scope));
    return raw === undefined ? { version: 1, models: {} } : decodeSettings(raw);
  }
  private path(scope: ModelScope): string {
    if (scope !== 'global' && scope !== 'book') throw new Error('配置范围无效');
    return scope === 'global' ? this.globalPath : this.bookPath;
  }
  /** Replace a full profile; undefined explicitly removes this scope's override. Credentials are untouched. */
  async set(scope: ModelScope, slot: ModelSlot, profile?: ModelProfile): Promise<void> {
    if (!slots.includes(slot)) throw new Error('模型角色无效');
    const checked = profile === undefined ? undefined : validateModelProfile(profile);
    await locked(this.path(scope), async () => {
      const settings = await this.read(scope);
      if (checked) settings.models[slot] = checked; else delete settings.models[slot];
      await writeFileAtomic(this.path(scope), `${JSON.stringify(settings, null, 2)}\n`);
    });
  }
  async resolve(role: ModelRole): Promise<EffectiveModel> {
    if (![...slots, 'discussion', 'arbiter'].includes(role)) throw new Error('模型角色无效');
    const [global, book] = await Promise.all([this.read('global'), this.read('book')]);
    const selected: ModelSlot = role === 'discussion' || role === 'arbiter' ? 'default' : role;
    for (const slot of selected === 'default' ? ['default'] as const : [selected, 'default'] as const) {
      for (const source of ['book', 'global'] as const) {
        const profile = (source === 'book' ? book : global).models[slot];
        if (profile) return { profile, role, slot, source, path: this.path(source) };
      }
    }
    throw new Error('尚未设置默认模型，请先配置 Provider 和模型');
  }
  private async credentials(): Promise<Record<string, string>> {
    const raw = await readJson(this.credentialsPath);
    if (raw === undefined) return {};
    const root = object(raw);
    only(root, ['version', 'keys']);
    if (root.version !== 1) throw new Error('凭证格式版本无效');
    const keys = object(root.keys);
    if (Object.entries(keys).some(([id, key]) => !/^novelpilot-[a-f0-9]{64}$/.test(id) || typeof key !== 'string' || !key.trim())) throw new Error('凭证文件格式无效');
    return keys as Record<string, string>;
  }
  /** Undefined means preserve; null means explicit deletion. This API never returns secret values. */
  async setApiKey(profile: ModelProfile, key: string | null | undefined): Promise<void> {
    if (key === undefined) return;
    const id = credentialId(validateModelProfile(profile));
    if (key !== null && (!key.trim() || /[\r\n]/.test(key))) throw new Error('凭证不能为空或含换行；删除请明确指定 null');
    await locked(this.credentialsPath, async () => {
      const keys = await this.credentials();
      if (key === null) delete keys[id]; else keys[id] = key;
      await writeFileAtomic(this.credentialsPath, `${JSON.stringify({ version: 1, keys }, null, 2)}\n`);
    });
  }
  async hasApiKey(profile: ModelProfile): Promise<boolean> {
    return Boolean((await this.credentials())[credentialId(validateModelProfile(profile))]);
  }
  /** No catalog network calls, ambient Pi configuration, or real inference during construction. */
  async buildModelRuntime(role: ModelRole) {
    const modelRuntime = await ModelRuntime.create({ credentials: new InMemoryCredentialStore(), modelsPath: null, modelsStore: new InMemoryModelsStore(), allowModelNetwork: false, refreshOnCreate: false });
    return { modelRuntime, ...await this.configureRuntime(modelRuntime, role) };
  }
  /** Register the selected profile on a live session's runtime before session.setModel(). */
  async configureRuntime(modelRuntime: ModelRuntime, role: ModelRole) {
    const effective = await this.resolve(role);
    const p = effective.profile;
    const providerId = credentialId(p);
    const key = (await this.credentials())[providerId];
    if (!key) throw new Error('当前服务地址尚未保存凭证，请先配置 API Key');
    // Pi requires numeric rates. These internal placeholders MUST NOT become displayed/accounted prices.
    const pricing = p.pricing ?? null;
    modelRuntime.registerProvider(providerId, {
      api: p.api, baseUrl: p.baseUrl,
      models: [{ id: p.id, name: p.id, api: p.api, baseUrl: p.baseUrl, reasoning: p.reasoning,
        input: ['text'], contextWindow: p.contextWindow, maxTokens: p.maxTokens,
        cost: pricing ?? { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    });
    await modelRuntime.setRuntimeApiKey(providerId, key);
    const model = modelRuntime.getModel(providerId, p.id);
    if (!model) throw new Error('Pi 未能注册指定模型');
    return { model, thinkingLevel: p.thinkingLevel, pricing, effective };
  }
}
