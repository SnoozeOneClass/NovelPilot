import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test, type TestContext } from 'node:test';
import { NovelApplication, type ApplicationUI } from '../src/app/application.js';
import { type RoleRunner } from '../src/app/host.js';
import { createRoleSession } from '../src/runtime/pi/session.js';
import { deterministicBaselineRunner } from '../src/eval/runner.js';
import { BookStore } from '../src/store/book-store.js';
import { scriptedProvider } from './fixtures/provider.js';
import type { NovelView } from '../src/ui/novel-tui.js';
import { AppSessions } from '../src/app/sessions.js';
import { EventQueue } from '../src/app/lifetime.js';
import { ModelConfiguration } from '../src/runtime/pi/model-config.js';
import { Discussion } from '../src/app/discussion.js';

const discussion = '<reply>我们讨论人物和世界观。</reply><draft>守灯人回乡点灯，一章结束。</draft><ready>true</ready><suggestions>补充动机</suggestions>';
async function fixture(t: TestContext, options: { runner?: RoleRunner; decision?: () => unknown } = {}) {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-app-'));
  const store = await BookStore.open(root);
  const lines: string[] = [], views: NovelView[] = [], roles: string[] = [];
  let closed = false;
  const ui: ApplicationUI = { update: (view) => { assert.equal(closed, false); views.push(view); }, append: (_role, value) => { assert.equal(closed, false); lines.push(value); }, appendDelta: (value) => { assert.equal(closed, false); lines.push(value); }, requestInput: async () => null, close: async () => { closed = true; } };
  const app = new NovelApplication(store, ui, { configDir: join(root, 'global'), runner: options.runner ?? deterministicBaselineRunner(),
    sessionFactory: async (role, tools, anchor) => {
      roles.push(role);
      const provider = await scriptedProvider(() => [{ type: 'text', text: role === 'discussion' ? discussion : JSON.stringify(options.decision?.() ?? { kind: 'query', reply: '当前已完成一章。' }) }]);
      return createRoleSession({ bookDir: root, systemPrompt: 'Novel session.', modelRuntime: provider.runtime, model: provider.model, tools, contextAnchor: () => anchor });
    } });
  t.after(async () => { await app.close(); await rm(root, { recursive: true, force: true }); });
  await app.initialize();
  return { app, store, root, lines, views, roles, ui };
}
async function start(app: NovelApplication) { await app.command('先讨论守灯人的经历'); await app.command('/start'); await app.idle(); }

test('application requires discussion and explicit start, then automatically completes one book', async (t) => {
  const f = await fixture(t);
  await f.app.command('/start'); assert.equal((await f.store.snapshot()).progress.phase, 'discussion');
  await f.app.command('讨论世界观'); assert.equal((await f.store.snapshot()).records.length, 0);
  assert.equal(f.views.at(-1)?.draft, '守灯人回乡点灯，一章结束。');
  await f.app.command('/start'); await f.app.idle();
  assert.equal((await f.store.snapshot()).progress.phase, 'complete');
  assert.equal((await f.store.snapshot()).records.length, 1);
  assert.equal(f.views.at(-1)?.phase, 'complete');
  assert.equal(f.views.at(-1)?.bookTitle, '灯塔');
  assert.equal(f.views.at(-1)?.plannedChapters, 1);
  assert.equal(f.views.at(-1)?.chapters?.[0]?.title, '归乡');
  assert.ok((f.views.at(-1)?.wordCount ?? 0) > 0);
  assert.ok(f.views.at(-1)?.events?.some((event) => event.includes('规划')));
});
test('queries preserve story state; completed books refuse additional plot', async (t) => {
  let decision: unknown = { kind: 'query', reply: '一章。' };
  const f = await fixture(t, { decision: () => decision }); await start(f.app);
  const before = await f.store.snapshot();
  await f.app.command('目前写了几章？');
  assert.deepEqual(await f.store.snapshot(), before);
  decision = { kind: 'plan', reply: '继续扩展', text: '新增一章' };
  await f.app.command('增加下一章'); await f.app.idle();
  assert.deepEqual(await f.store.snapshot(), before);
  assert.equal(await f.store.files.read('meta/planning_feedback.json'), null);
});

test('production settings entry uses one draft form with explicit save and no prompt chain', async (t) => {
  const f = await fixture(t);
  f.ui.requestInput = async () => { throw new Error('旧逐项提示不应被调用'); };
  f.ui.requestModelSettings = async (actions) => {
    assert.equal((await actions.load('book', 'default')).profile, null);
    return actions.save({ scope: 'book', slot: 'default', apiKey: 'private-form-key', profile: {
      provider: 'test', id: 'single-page-model', api: 'openai-completions', baseUrl: 'https://example.invalid',
      contextWindow: 32000, maxTokens: 4000, reasoning: false, thinkingLevel: 'off',
    } });
  };
  await f.app.command('/settings');
  assert.equal((await f.app.models.resolve('default')).profile.id, 'single-page-model');
  assert.ok(!f.lines.join('\n').includes('private-form-key'));
  f.ui.requestModelSettings = async () => null;
  await f.app.command('/settings');
  assert.equal(f.views.at(-1)?.error, '');
  assert.equal((await f.app.models.resolve('default')).profile.id, 'single-page-model');
});
test('explicit existing-chapter rewrite defaults to pause and cannot reopen completed books', async (t) => {
  const baseline = deterministicBaselineRunner();
  const runner: RoleRunner = (r) => baseline(r.instruction.kind === 'rewrite' ? { ...r, instruction: { ...r.instruction, kind: 'write' } } : r);
  const f = await fixture(t, { runner, decision: () => ({ kind: 'rewrite', reply: '修改第一章', text: '强化动作', chapters: [1], resume: true }) });
  await start(f.app); await f.app.command('修改第1章动作描写'); await f.app.idle();
  const state = await f.store.snapshot();
  assert.equal(state.records[0]?.revision, 2); assert.equal(state.records.length, 1); assert.equal(state.progress.phase, 'complete');
});
test('stage cancel retains paused state; applying full draft uses intervention path and resumes', async (t) => {
  const baseline = deterministicBaselineRunner(); let pauseOnce = true;
  const runner: RoleRunner = async (r) => {
    if (r.instruction.kind === 'review' && pauseOnce) { pauseOnce = false; return { ...r.identity, status: 'cancelled', reason: 'test pause', evidence: [] }; }
    return baseline(r);
  };
  const f = await fixture(t, { runner, decision: () => ({ kind: 'rule', reply: '记住叙述方向', rules: '保留守灯主题' }) });
  await start(f.app);
  await f.app.command('/cocreate'); await f.app.command('讨论新方向'); await f.app.command('/cancel');
  assert.equal(f.views.at(-1)?.phase, 'paused'); assert.equal(await f.store.files.read('meta/rules.md'), null);
  await f.app.command('/cocreate'); await f.app.command('讨论新方向'); await f.app.command('/apply'); await f.app.idle();
  assert.match(await f.store.files.read('meta/rules.md') ?? '', /保留守灯主题/);
  assert.equal((await f.store.snapshot()).progress.phase, 'complete');
});
test('worker-boundary feedback is deferred and recalculated after chapter facts change', async (t) => {
  const entered = Promise.withResolvers<void>(), release = Promise.withResolvers<void>();
  const baseline = deterministicBaselineRunner(); let decisions = 0;
  const runner: RoleRunner = async (r) => {
    if (r.instruction.kind === 'write') { entered.resolve(); await release.promise; }
    return baseline(r);
  };
  const f = await fixture(t, { runner, decision: () => { decisions++; return { kind: 'pause', reply: '在边界暂停', stopAfter: null }; } });
  await f.app.command('讨论'); await f.app.command('/start'); await entered.promise;
  await f.app.command('当前任务结束后暂停');
  assert.equal(await f.store.files.read('meta/control.json'), null);
  release.resolve(); await f.app.idle();
  assert.ok(decisions >= 2); assert.equal(f.views.at(-1)?.phase, 'paused');
  assert.equal((await f.store.snapshot()).records.length, 1);
});
test('unsynced changes block resume without another model or task', async (t) => {
  const f = await fixture(t); await start(f.app);
  await f.store.files.write('chapters/01.md', '用户手改正文');
  const calls = f.roles.length;
  await f.app.command('/resume'); await f.app.idle();
  assert.equal(f.roles.length, calls); assert.match(f.views.at(-1)?.error ?? '', /sync/);
  assert.equal(await f.store.files.read('chapters/01.md'), '用户手改正文');
});
test('shutdown cancels pending settings input before waiting and leaves no writes after lease release', async (t) => {
  const f = await fixture(t);
  const input = Promise.withResolvers<string | null>(), entered = Promise.withResolvers<void>();
  f.ui.requestInput = async () => { entered.resolve(); return input.promise; };
  f.ui.cancelInput = () => input.resolve(null);
  const command = f.app.command('/settings'); await entered.promise;
  await f.app.close(); await command;
  const reopened = await BookStore.open(f.root); await reopened.close();
});
test('model switch does not relabel an in-flight unknown-price response as known', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-price-'));
  const store = await BookStore.open(root);
  const config = new ModelConfiguration(root, join(root, 'global'));
  const profile = { provider: 'custom', id: 'priced', api: 'openai-completions' as const, baseUrl: 'https://example.invalid', contextWindow: 32000, maxTokens: 4000,
    reasoning: false, thinkingLevel: 'off' as const, pricing: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0 } };
  await config.set('global', 'default', profile); await config.setApiKey(profile, 'test-key');
  const entered = Promise.withResolvers<void>(), release = Promise.withResolvers<void>();
  const provider = await scriptedProvider(async () => { entered.resolve(); await release.promise; return [{ type: 'text', text: 'response' }]; });
  const messages: string[] = [];
  const events = new EventQueue<{ role: string; text: string; delta?: boolean }>(async (event) => { if (event.role === '用量') messages.push(event.text); });
  const sessions = new AppSessions(root, config, store.files, events, async (_role, tools) => createRoleSession({ bookDir: root, systemPrompt: 'role', tools, modelRuntime: provider.runtime, model: provider.model }));
  t.after(async () => { release.resolve(); await sessions.close(); await events.close(); await store.close(); await rm(root, { recursive: true, force: true }); });
  const session = await sessions.make('discussion');
  const pending = session.prompt('first request'); await entered.promise;
  await sessions.refreshModels(); release.resolve(); await pending;
  await events.close();
  assert.equal(messages.length, 1); assert.match(messages[0]!, /^one.*费用未知$/);
});
test('production arbiter has no skills or domain tools', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-arbiter-')); const store = await BookStore.open(root);
  const config = new ModelConfiguration(root, join(root, 'global'));
  const profile = { provider: 'custom', id: 'one', api: 'openai-completions' as const, baseUrl: 'https://example.invalid', contextWindow: 32000, maxTokens: 4000, reasoning: false, thinkingLevel: 'off' as const };
  await config.set('global', 'default', profile); await config.setApiKey(profile, 'test-key');
  const events = new EventQueue<{ role: string; text: string; delta?: boolean }>(async () => undefined);
  const sessions = new AppSessions(root, config, store.files, events);
  t.after(async () => { await sessions.close(); await events.close(); await store.close(); await rm(root, { recursive: true, force: true }); });
  const session = await sessions.make('arbiter'); assert.deepEqual(session.getActiveToolNames(), []);
});
test('production discussion restores its latest complete draft after actual SDK compaction', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-draft-anchor-')); const store = await BookStore.open(root);
  const config = new ModelConfiguration(root, join(root, 'global'));
  const profile = { provider: 'custom', id: 'one', api: 'openai-completions' as const, baseUrl: 'https://example.invalid', contextWindow: 32000, maxTokens: 4000, reasoning: false, thinkingLevel: 'off' as const };
  await config.set('global', 'default', profile); await config.setApiKey(profile, 'test-key');
  const configured = await config.buildModelRuntime('discussion');
  const provider = await scriptedProvider((index) => [{ type: 'text', text: `<reply>讨论回复</reply><draft>唯一完整草稿-${index}：人物林青，世界为海边小城，结局点灯。</draft><ready>true</ready><suggestions></suggestions>` }]);
  // Only the provider is deterministic; keep the production AppSessions / Skill / anchor path.
  config.buildModelRuntime = async () => ({ ...configured, modelRuntime: provider.runtime, model: provider.model });
  const events = new EventQueue<{ role: string; text: string; delta?: boolean }>(async () => undefined);
  const sessions = new AppSessions(root, config, store.files, events);
  t.after(async () => { await sessions.close(); await events.close(); await store.close(); await rm(root, { recursive: true, force: true }); });
  let active: Discussion | undefined;
  const session = await sessions.make('discussion', [], () => `开书任务。当前完整草稿：${active?.snapshot.draft ?? ''}`);
  active = new Discussion(session); session.settingsManager.setCompactionEnabled(false);
  await active.submit('讨论旧背景。'.repeat(14000));
  await active.submit('补充人物动机。'.repeat(20000));
  const established = active.snapshot.draft;
  await session.compact('整理旧讨论');
  assert.ok(session.sessionManager.getEntries().some((entry) => entry.type === 'compaction'));
  await active.submit('继续讨论结局');
  const systemPrompt = provider.requests.at(-1)?.systemPrompt ?? '';
  assert.ok(systemPrompt.includes(established));
  assert.equal(systemPrompt.split(established).length - 1, 1);
  assert.doesNotMatch(systemPrompt, /唯一完整草稿-0/);
});
