import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { editSettings, type RequestInput } from '../src/app/settings.js';
import { ModelConfiguration, type ModelProfile } from '../src/runtime/pi/model-config.js';

const profile: ModelProfile = { provider: 'test', id: 'one', api: 'openai-completions', baseUrl: 'https://example.invalid', contextWindow: 32000, maxTokens: 4000, reasoning: false, thinkingLevel: 'off', pricing: { input: 1, output: 2, cacheRead: 0, cacheWrite: 0 } };
test('settings masks key entry, preserves credentials, and discards stale price on model change', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-settings-')); t.after(() => rm(root, { recursive: true, force: true }));
  const config = new ModelConfiguration(join(root, 'book'), join(root, 'global'));
  await config.set('global', 'default', profile); await config.setApiKey(profile, 'private-key');
  const inputs = ['book', 'writer', 'test', 'openai-completions', 'https://example.invalid', 'two', '32000', '4000', 'off', ''];
  const request: RequestInput = async (label, options) => {
    if (label.startsWith('API Key')) { assert.equal(options?.secret, true); assert.equal(options.initial, ''); }
    assert.ok(!String(options?.initial).includes('private-key')); return inputs.shift() ?? null;
  };
  const saved = await editSettings(config, request);
  assert.equal(saved.role, 'writer');
  const effective = await config.resolve('writer');
  assert.equal(effective.profile.id, 'two'); assert.equal(effective.profile.pricing, undefined);
  assert.equal(await config.hasApiKey(effective.profile), true);
  assert.equal((await config.resolve('default')).profile.pricing?.input, 1);
});
test('cancel and invalid settings leave prior configuration intact', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-settings-')); t.after(() => rm(root, { recursive: true, force: true }));
  const config = new ModelConfiguration(join(root, 'book'), join(root, 'global'));
  await config.set('global', 'default', profile);
  await assert.rejects(editSettings(config, async () => null), /取消/);
  await assert.rejects(editSettings(config, async () => 'invalid-scope'), /范围/);
  assert.deepEqual((await config.resolve('default')).profile, profile);
});

test('settings uses selection menus for scope, role, protocol and thinking', async () => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-settings-menu-'));
  const config = new ModelConfiguration(join(root, 'book'), join(root, 'global'));
  await config.setApiKey(profile, 'test-key');
  const values = ['test', 'https://example.invalid', 'one', '32000', '4000', ''];
  const selections = ['book', 'writer', 'openai-completions', 'off'];
  const menuLabels: string[] = [];
  const effective = await editSettings(config, async () => values.shift() ?? null, async (label, choices) => {
    menuLabels.push(label); const selected = selections.shift(); assert.ok(choices.some((c) => c.value === selected)); return selected ?? null;
  });
  assert.equal(effective.role, 'writer');
  assert.equal(menuLabels.length, 4);
  assert.equal(values.length, 0);
});
