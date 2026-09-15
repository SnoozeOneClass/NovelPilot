import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test, type TestContext } from 'node:test';
import { BookStore } from '../src/store/book-store.js';
import { BookHost, createPiRunner, type RoleRun, type RoleRunner } from '../src/app/host.js';
import { createRoleSession } from '../src/runtime/pi/session.js';
import { scriptedProvider } from './fixtures/provider.js';
import { decodePlanning, emptyPlanning, planningPath } from '../src/domain/planning.js';
import { contentHash, type ChapterRecord } from '../src/domain/book.js';
import { novelTools } from '../src/tools/novel-tools.js';
import { recordPath } from '../src/store/book-store.js';
import { recordBasis } from '../src/app/router.js';

const foundation = { title: '灯塔', premise: '归乡守灯', characters: '林青：守灯人', world: '海边小城', ending: '点亮灯塔', tier: 'short' };
const facts = { title: '归乡', summary: '林青归乡点亮灯塔。', characters: ['林青'], keyEvents: ['灯亮'], timeline: [], stateChanges: [], relationships: [], foreshadows: [] };
export async function invoke(request: RoleRun, name: string, params: unknown) {
  const tool = request.tools.find((t) => t.name === name);
  assert.ok(tool, `Missing ${name}`);
  return tool.execute('test', params, request.signal, undefined, {} as never);
}
async function fixture(t: TestContext, runner: RoleRunner) {
  const dir = await mkdtemp(join(tmpdir(), 'novelpilot-host-'));
  const store = await BookStore.open(dir); t.after(() => store.close());
  const host = new BookHost(store, runner, { maxTurns: 4 });
  await host.start('短篇小说，一章，林青回乡点亮灯塔。');
  return { dir, store, host };
}
export function scriptRunner(options: { long?: boolean; rewrite?: boolean; log?: string[] } = {}): RoleRunner {
  let reviewed = false;
  return async (r) => {
    r.budget.usedTurns++;
    options.log?.push(r.instruction.kind);
    const kind = r.instruction.kind;
    if (kind === 'foundation') await invoke(r, 'save_foundation', { ...foundation, tier: options.long ? 'long' : 'short' });
    else if (kind === 'outline' || kind === 'revise') await invoke(r, 'save_outline', { reason: '保持一章故事', chapters: [{ chapter: r.instruction.start, title: '归乡', outline: '归乡点灯', volume: 1, arc: 1, arcEnd: true, volumeEnd: true }] });
    else if (kind === 'write' || kind === 'rewrite') {
      await invoke(r, 'plan_chapter', { title: '归乡', goal: '点亮灯塔', conflict: '归乡心结', hook: '海面灯光' });
      await invoke(r, 'save_draft', { content: '# 归乡\n林青踏上石阶，点亮了灯。' });
      await invoke(r, 'check_consistency', {});
      await invoke(r, 'commit_chapter', { facts });
      await assert.rejects(() => invoke(r, 'save_draft', { content: '提交后非法改稿' }));
    } else if (kind === 'review') {
      await invoke(r, 'save_review', { content: '人物动机明确', score: 8, issues: options.rewrite && !reviewed ? [{ chapter: 1, reason: '补充点灯细节' }] : [] });
      reviewed = true;
    } else if (kind === 'arc_summary' || kind === 'volume_summary') await invoke(r, 'save_summary', { content: '林青完成守灯承诺。' });
    else await invoke(r, 'complete_book', { reason: '点灯结局已兑现' });
    return { ...r.identity, status: 'completed', reason: 'saved', evidence: r.evidence };
  };
}
test('same Host automatically plans, writes, reviews and finishes without chapter approval', async (t) => {
  const log: string[] = [];
  const f = await fixture(t, scriptRunner({ log }));
  assert.equal((await f.host.run()).status, 'complete');
  assert.deepEqual(log, ['foundation', 'outline', 'write', 'review', 'conclude']);
  const snapshot = await f.host.snapshot();
  assert.equal(snapshot.records.length, 1); assert.equal(snapshot.records[0]?.revision, 1);
  assert.equal((await f.host.run()).status, 'complete'); assert.equal(log.length, 5);
});
test('long arc and volume summaries and review-triggered rewrites precede conclusion', async (t) => {
  const log: string[] = [];
  const f = await fixture(t, scriptRunner({ long: true, rewrite: true, log }));
  assert.equal((await f.host.run()).status, 'complete');
  assert.deepEqual(log, ['foundation', 'outline', 'write', 'review', 'rewrite', 'review', 'arc_summary', 'volume_summary', 'conclude']);
  assert.equal((await f.host.snapshot()).records[0]?.revision, 2);
});
test('fake completion cannot advance; cumulative budgets survive repeated run calls', async (t) => {
  let calls = 0;
  const f = await fixture(t, async (r) => { calls++; r.budget.usedTurns = r.budget.maxTurns; return { ...r.identity, status: 'completed', reason: 'claims success', evidence: [] }; });
  assert.equal((await f.host.run()).status, 'incomplete');
  assert.equal((await f.host.run()).status, 'incomplete'); assert.equal(calls, 1);
  assert.equal((await f.host.snapshot()).records.length, 0);
});
test('persisted commit wins late cancellation but next task does not start', async (t) => {
  const script = scriptRunner(); let host: BookHost;
  const f = await fixture(t, async (r) => { const result = await script(r); if (r.instruction.kind === 'write') host.pause(); return result; });
  host = f.host;
  assert.equal((await host.run()).status, 'paused'); assert.equal((await host.snapshot()).records.length, 1);
  assert.equal((await host.run()).status, 'complete');
});
test('editor has no writer tool and cannot request changes beyond authorized review range', async (t) => {
  const script = scriptRunner();
  const f = await fixture(t, async (r) => {
    if (r.instruction.kind === 'review') {
      assert.ok(!r.tools.some((t) => t.name === 'commit_chapter'));
      await assert.rejects(() => invoke(r, 'save_review', { content: '扩大范围', issues: [{ chapter: 2, reason: '不允许' }] }));
    }
    return script(r);
  });
  assert.equal((await f.host.run()).status, 'complete');
});
test('actual Pi provider observes persisted turn budget before each request; re-dispatch keeps it', async (t) => {
  const observed: number[] = [];
  let store: BookStore;
  const provider = await scriptedProvider(async () => {
    const planning = await store.files.json(planningPath, decodePlanning);
    assert.ok(planning?.active);
    observed.push(planning.budgets[planning.active.taskId]!);
    return [{ type: 'text', text: '尚未保存' }];
  });
  const f = await fixture(t, createPiRunner(async (_role, tools) => createRoleSession({ bookDir: store.files.root, systemPrompt: '测试', tools, modelRuntime: provider.runtime, model: provider.model })));
  store = f.store;
  assert.equal((await f.host.run()).status, 'incomplete');
  assert.deepEqual(observed, [1, 2, 3, 4]);
  await f.host.run(); assert.equal(observed.length, 4);
});
test('direct Host refuses unsynced manual changes and does not call a model', async (t) => {
  const f = await fixture(t, scriptRunner());
  await f.host.run();
  await f.store.files.write('chapters/01.md', '手工修改正文');
  assert.equal((await f.host.run()).status, 'paused');
});
test('long planning expands the next volume after review and summaries, then completes', async (t) => {
  const log: string[] = []; const script = scriptRunner({ long: true, log });
  const f = await fixture(t, async (r) => {
    if (r.instruction.kind === 'conclude' && r.instruction.start === 2) {
      log.push('expand-volume'); r.budget.usedTurns++;
      await invoke(r, 'save_outline', { reason: '展开收官卷', chapters: [{ chapter: 2, title: '守灯', outline: '灯光传递', volume: 2, arc: 1, arcEnd: true, volumeEnd: true }] });
      return { ...r.identity, status: 'completed', reason: 'expanded', evidence: r.evidence };
    }
    return script(r);
  });
  assert.equal((await f.host.run()).status, 'complete');
  assert.equal((await f.host.snapshot()).records.length, 2);
  assert.deepEqual(log, ['foundation', 'outline', 'write', 'review', 'arc_summary', 'volume_summary', 'expand-volume', 'write', 'review', 'arc_summary', 'volume_summary', 'conclude']);
});

test('writer planning and consistency tools persist target artifacts without claiming literary correctness', async (t) => {
  const script = scriptRunner();
  const f = await fixture(t, async (r) => {
    if (r.instruction.kind === 'write') await assert.rejects(() => invoke(r, 'check_consistency', {}), /先保存/);
    return script(r);
  });
  assert.equal((await f.host.run()).status, 'complete');
  const plan = JSON.parse((await f.store.files.read('drafts/01.plan.json'))!);
  const check = JSON.parse((await f.store.files.read('drafts/01.check.json'))!);
  assert.equal(plan.goal, '点亮灯塔'); assert.equal(plan.chapter, 1);
  assert.equal(check.draftHash, contentHash((await f.store.files.read('drafts/01.draft.md'))!));
  assert.equal(check.verdict, 'model-review-required'); assert.equal(check.attemptId, plan.attemptId);
});

test('queued writer operation rechecks cancellation inside the actual store mutation', async (t) => {
  const script = scriptRunner(); let store: BookStore; let host: BookHost;
  const f = await fixture(t, async (r) => {
    if (r.instruction.kind !== 'write') return script(r);
    const entered = Promise.withResolvers<void>(); const release = Promise.withResolvers<void>();
    const blocker = store.transaction(async () => { entered.resolve(); await release.promise; });
    await entered.promise;
    const rejected = assert.rejects(() => invoke(r, 'save_draft', { content: '取消后的非法草稿' }), /取消/);
    host.pause(); release.resolve(); await blocker; await rejected;
    return { ...r.identity, status: 'cancelled', reason: 'cancelled', evidence: [] };
  });
  store = f.store; host = f.host;
  assert.equal((await host.run()).status, 'paused');
  assert.equal(await store.files.read('drafts/01.draft.md'), null);
});

test('late rewrite context uses bounded valid prior memory and keeps the accepted target baseline', async (t) => {
  const dir = await mkdtemp(join(tmpdir(), 'novelpilot-context-window-'));
  const store = await BookStore.open(dir); t.after(() => store.close());
  const records: ChapterRecord[] = Array.from({ length: 40 }, (_, i) => ({ version: 1, chapter: i + 1, revision: 1, origin: 'generated', content: `正文${i + 1}`, contentHash: contentHash(`正文${i + 1}`), facts: { ...facts, summary: `摘要${i + 1}`, stateChanges: i === 0 ? [{ subject: '旧约定', state: '继续有效' }] : [] }, style: [], acceptedAt: new Date().toISOString() }));
  const state = emptyPlanning(); state.foundation = { ...foundation, tier: 'long' };
  state.chapters = records.map((r) => ({ chapter: r.chapter, title: '章节', outline: '计划', volume: 1, arc: Math.ceil(r.chapter / 10), arcEnd: r.chapter % 10 === 0, volumeEnd: r.chapter === 40 }));
  state.aggregates.push({ key: 'prior-arc', kind: 'arc_summary', start: 1, end: 10, basis: recordBasis(records, 1, 10), content: '有效旧弧摘要', score: null, issues: [] });
  await store.transaction(async (files, p) => {
    await files.writeJSON('meta/progress.json', { ...p, phase: 'writing', completed: records.map((r) => r.chapter) });
    await files.writeJSON(planningPath, state);
    for (const record of records) await files.writeJSON(recordPath(record.chapter), record);
  });
  const tools = novelTools({ store, identity: { taskId: 'test', attemptId: 'test' }, signal: new AbortController().signal, evidence: [], instruction: { key: 'test', role: 'writer', kind: 'rewrite', start: 20, end: 20, reason: '修订', basis: '' } });
  const response = await tools[0]!.execute('context', {}, undefined, undefined, {} as never);
  const body = response.content.find((item) => item.type === 'text'); assert.ok(body && body.type === 'text');
  const context = JSON.parse(body.text);
  assert.equal(context.summaries.length, 3); assert.equal(context.factsBeforeTarget.length, 5);
  assert.ok(context.factsBeforeTarget.every((r: { chapter: number }) => r.chapter < 20));
  assert.equal(context.plans.length, 10); assert.equal(context.chapter.chapter, 20);
  assert.equal(context.currentStoryState.states['旧约定'], '继续有效');
  assert.equal(context.aggregates[0].content, '有效旧弧摘要');
  assert.equal(context.contextRange.omittedOlderChapterSummaries, 16);
});
