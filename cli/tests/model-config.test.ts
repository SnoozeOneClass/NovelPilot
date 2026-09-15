import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { ModelConfiguration, validateModelProfile, type ModelProfile } from '../src/runtime/pi/model-config.js';

const profile: ModelProfile = {
  provider: 'example', id: 'test-model', api: 'openai-completions', baseUrl: 'https://example.invalid/v1',
  contextWindow: 32000, maxTokens: 4000, reasoning: false, thinkingLevel: 'off',
};
async function fixture(t: { after(fn: () => Promise<void>): void }) {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-models-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  return new ModelConfiguration(join(root, 'book'), join(root, 'global'));
}
test('role overrides, default fallback and source are explicit across scopes', async (t) => {
  const config = await fixture(t);
  await assert.rejects(config.resolve('writer'), /尚未设置/);
  await config.set('global', 'default', profile);
  await config.set('global', 'writer', { ...profile, id: 'global-writer' });
  await config.set('book', 'default', { ...profile, id: 'book-default' });
  assert.equal((await config.resolve('discussion')).profile.id, 'book-default');
  assert.equal((await config.resolve('arbiter')).slot, 'default');
  assert.equal((await config.resolve('planner')).source, 'book');
  assert.equal((await config.resolve('writer')).profile.id, 'global-writer');
  await config.set('book', 'writer', { ...profile, id: 'book-writer' });
  assert.equal((await config.resolve('writer')).path, config.bookPath);
  await config.set('book', 'writer');
  assert.equal((await config.resolve('writer')).source, 'global');
});
test('profile validation rejects secrets, bad capacities and unsupported reasoning without changing saved data', async (t) => {
  const config = await fixture(t);
  await config.set('global', 'default', profile);
  for (const input of [
    { ...profile, apiKey: 'do-not-display' }, { ...profile, maxTokens: 90000 },
    { ...profile, thinkingLevel: 'high' }, { ...profile, api: 'unsupported' },
    { ...profile, baseUrl: 'https://user:secret@example.invalid' },
    { ...profile, baseUrl: 'https://example.invalid?api_key=secret' },
    { ...profile, pricing: { input: 0 } },
  ]) assert.throws(() => validateModelProfile(input));
  await assert.rejects(config.set('global', 'default', { ...profile, maxTokens: -1 }));
  assert.equal((await config.resolve('default')).profile.maxTokens, 4000);
  await writeFile(config.globalPath, '{not valid');
  await assert.rejects(config.read('global'), /JSON 损坏/);
});
test('credentials are separate, preserved by edits and endpoint-bound', async (t) => {
  const config = await fixture(t);
  await config.set('global', 'default', profile);
  await config.setApiKey(profile, 'test-secret-123');
  await config.setApiKey(profile, undefined);
  await config.set('global', 'default', { ...profile, maxTokens: 5000 });
  assert.equal(await config.hasApiKey(profile), true);
  assert.equal(await config.hasApiKey({ ...profile, baseUrl: 'https://different.invalid' }), false);
  assert.equal((await readFile(config.globalPath, 'utf8')).includes('test-secret'), false);
  assert.equal(JSON.stringify(await config.resolve('default')).includes('test-secret'), false);
  await config.setApiKey(profile, null);
  assert.equal(await config.hasApiKey(profile), false);
});
test('concurrent independent config instances preserve all slot updates', async (t) => {
  const config = await fixture(t);
  // Sharing a destination must serialize read/modify/write, including across instances.
  const other = new ModelConfiguration(join(config.bookPath, '..', '..'), join(config.globalPath, '..'));
  await Promise.all([
    config.set('global', 'default', profile), other.set('global', 'writer', profile),
    config.set('global', 'editor', profile), other.set('global', 'planner', profile),
  ]);
  assert.equal(Object.keys((await config.read('global')).models).length, 4);
});
test('public SDK registration resolves credentials without network and preserves unknown pricing', async (t) => {
  const config = await fixture(t);
  await config.set('global', 'default', profile);
  await assert.rejects(config.buildModelRuntime('discussion'), /凭证/);
  await config.setApiKey(profile, 'fixture-key');
  const fetchBefore = globalThis.fetch;
  globalThis.fetch = async () => { throw new Error('unexpected network'); };
  t.after(async () => { globalThis.fetch = fetchBefore; });
  const runtime = await config.buildModelRuntime('discussion');
  assert.equal(runtime.model.id, profile.id);
  assert.equal(runtime.model.contextWindow, 32000);
  assert.equal(runtime.model.maxTokens, 4000);
  assert.equal(runtime.thinkingLevel, 'off');
  assert.equal(runtime.pricing, null);
  const auth = await runtime.modelRuntime.getAuth(runtime.model);
  assert.ok(auth);
  assert.equal(runtime.modelRuntime.getRegisteredProviderConfig(runtime.model.provider)?.apiKey, undefined);
  assert.equal(JSON.stringify(runtime.effective).includes('fixture-key'), false);
  await config.set('book', 'default', { ...profile, id: 'next-model', pricing: { input: 1, output: 2, cacheRead: 0, cacheWrite: 0 } });
  const next = await config.buildModelRuntime('discussion');
  assert.equal(next.pricing?.input, 1);
  assert.equal(runtime.model.id, 'test-model');
  assert.equal(next.model.id, 'next-model');
  const switched = await config.configureRuntime(runtime.modelRuntime, 'discussion');
  assert.equal(switched.model.id, 'next-model');
  assert.ok(await runtime.modelRuntime.getAuth(switched.model));
  assert.equal(runtime.model.id, 'test-model');
});
