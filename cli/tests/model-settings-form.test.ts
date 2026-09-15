import assert from 'node:assert/strict';
import { test } from 'node:test';
import { visibleWidth } from '@earendil-works/pi-tui';
import { createModelSettingsForm, type ModelFormActions } from '../src/ui/model-settings.js';
import type { EffectiveModel, ModelProfile } from '../src/runtime/pi/model-config.js';
const profile: ModelProfile = { provider: 'test', id: 'old-model', api: 'openai-completions', baseUrl: 'https://example.invalid', contextWindow: 32000, maxTokens: 4000, reasoning: false, thinkingLevel: 'off', pricing: { input: 1, output: 2, cacheRead: 0, cacheWrite: 0 } };
const tick = () => new Promise((resolve) => setImmediate(resolve));
const result = (p: ModelProfile): EffectiveModel => ({ profile: p, role: 'default', slot: 'default', source: 'book', path: 'models.json' });
function fixture(save?: ModelFormActions['save']) {
  const saved: Parameters<ModelFormActions['save']>[0][] = [];
  const form = createModelSettingsForm({ rows: () => 32, requestRender() {}, actions: {
    load: async () => ({ profile, source: '全局默认（继承）', hasApiKey: true }),
    save: save ?? (async (value) => { saved.push(value); return result(value.profile); }),
  } });
  return { ...form, saved };
}
function input(form: ReturnType<typeof fixture>, key: string) { form.component.handleInput?.(key); }
function move(form: ReturnType<typeof fixture>, count: number) { for (let i = 0; i < count; i++) input(form, '\x1b[B'); }

test('single page exposes fields together and edits only persist on Save', { timeout: 3000 }, async () => {
  const f = fixture(); await tick();
  const screen = f.component.render(86).join('\n');
  for (const label of ['保存范围', '使用角色', '接口类型', '模型标识', 'API Key', '保存当前配置']) assert.ok(screen.includes(label));
  move(f, 5); input(f, '\r'); input(f, '\x15'); input(f, 'new-model'); input(f, '\r');
  assert.equal(f.saved.length, 0);
  assert.match(f.component.render(86).join('\n'), /new-model/);
  input(f, '\x13');
  assert.equal((await f.result)?.profile.id, 'new-model');
  assert.equal(f.saved.length, 1);
  assert.equal(f.saved[0]?.profile.pricing, undefined);
});

test('secret stays masked during inline editing and cancel discards it without saving', async () => {
  const f = fixture(); await tick(); move(f, 9); input(f, '\r'); input(f, 'private-key-value');
  assert.ok(!f.component.render(86).join('\n').includes('private-key-value'));
  input(f, '\r');
  assert.ok(!f.component.render(86).join('\n').includes('private-key-value'));
  f.cancel(); assert.equal(await f.result, null); assert.equal(f.saved.length, 0);
});

test('save validation error stays on the form and cancellation waits in-flight saves', async () => {
  const bad = fixture(async () => { throw new Error('服务地址无效'); }); await tick();
  input(bad, '\x13'); await tick();
  assert.match(bad.component.render(76).join('\n'), /服务地址无效/);
  bad.cancel(); assert.equal(await bad.result, null);
  const saving = Promise.withResolvers<EffectiveModel>();
  const f = fixture(() => saving.promise); await tick(); input(f, '\x13'); f.cancel();
  let settled = false; void f.result.then(() => { settled = true; }); await tick(); assert.equal(settled, false);
  saving.resolve(result(profile)); assert.equal((await f.result)?.profile.id, 'old-model');
});

test('short terminals scroll focused fields and capacity editing accepts K notation', { timeout: 3000 }, async () => {
  const f = fixture(); await tick(); move(f, 6); input(f, '\r'); input(f, '\x15'); input(f, '128K'); input(f, '\r');
  input(f, '\x13'); assert.equal((await f.result)?.profile.contextWindow, 128000);
  const small = createModelSettingsForm({ rows: () => 14, requestRender() {}, actions: { load: async () => ({ profile, source: '全局', hasApiKey: true }), save: async () => result(profile) } });
  await tick(); for (let i = 0; i < 10; i++) small.component.handleInput?.('\x1b[B');
  const lines = small.component.render(60); assert.ok(lines.length <= 14); assert.ok(lines.every((line) => visibleWidth(line) <= 60));
  assert.match(lines.join('\n'), /保存当前配置/); small.cancel(); await small.result;
});
