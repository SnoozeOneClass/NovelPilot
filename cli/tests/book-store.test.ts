import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test, type TestContext } from 'node:test';
import { BookStore, chapterPath, recordPath, type FaultPoint } from '../src/store/book-store.js';
import { contentHash, type ChapterFacts } from '../src/domain/book.js';
import { appendCheckpoint } from '../src/store/checkpoints.js';

const identity = { taskId: 'write-1', attemptId: 'attempt-1' };
const facts = (): ChapterFacts => ({ title: '风起', summary: '林舟出发', characters: ['林舟'], keyEvents: ['离家'],
  timeline: [{ time: '清晨', event: '离家' }], stateChanges: [{ subject: '林舟', state: '在路上' }], relationships: [],
  foreshadows: [{ id: 'letter', action: 'plant', description: '未拆的信' }] });
async function setup(t: TestContext, fault: (point: FaultPoint) => Promise<void> = async () => undefined) {
  const dir = await mkdtemp(join(tmpdir(), 'novelpilot-book-'));
  const store = await BookStore.open(dir, fault);
  t.after(() => store.close());
  await store.beginCreation('讨论好的完整设定');
  await store.beginChapter(identity, 1);
  await store.saveDraft(identity, 1, '\uFEFF第一章\r\n林舟出门了。');
  return { dir, store };
}

for (const stop of ['pending_saved', 'chapter_written', 'record_written', 'projections_written', 'state_applied', 'progress_written', 'progress_marked', 'checkpoint_saved', 'signal_saved'] as const) {
  test(`frozen chapter recovers exactly after ${stop}`, async (t) => {
    const { dir, store } = await setup(t, async (point) => { if (point === stop) throw new Error(`injected:${stop}`); });
    await assert.rejects(store.commit(identity, 1, facts()), /injected/);
    const frozen = JSON.parse(await readFile(join(dir, 'meta/pending_commit.json'), 'utf8'));
    await writeFile(join(dir, 'drafts/01.draft.md'), '后来被覆盖的新草稿');
    await store.close();
    const recovered = await BookStore.open(dir);
    t.after(() => recovered.close());
    const state = await recovered.snapshot();
    assert.equal(state.pending, null);
    assert.deepEqual(state.progress.completed, [1]);
    assert.equal(state.progress.active, null);
    assert.equal(state.records[0]?.content, '第一章\n林舟出门了。');
    assert.equal(state.records[0]?.revision, 1);
    assert.deepEqual((await recovered.completion(identity))[0], frozen.receipt);
    assert.deepEqual(await recovered.commit({ taskId: 'other', attemptId: 'new' }, 1, facts()), frozen.receipt);
    assert.equal((await readFile(join(dir, 'meta/checkpoints.jsonl'), 'utf8')).trim().split('\n').length, 1);
    assert.deepEqual(JSON.parse(await readFile(join(dir, 'meta/story_state.json'), 'utf8')).timeline, [{ chapter: 1, time: '清晨', event: '离家' }]);
    assert.equal(await recovered.recover(), null);
  });
}

test('rewrite authorization replaces facts without double-counting and protects later foreshadows', async (t) => {
  const { store } = await setup(t);
  await store.commit(identity, 1, facts());
  await assert.rejects(store.saveDraft(identity, 1, 'late write'), /过期/);
  await assert.rejects(store.queueRewrites([2], '越界'), /已完成/);
  await store.queueRewrites([1], '重写出门段落');
  const rewrite = { taskId: 'rewrite-1', attemptId: 'r1' };
  await store.beginChapter(rewrite, 1);
  await store.saveDraft(rewrite, 1, '林舟留在家里。');
  const changed = facts(); changed.summary = '留在家里'; changed.stateChanges[0]!.state = '在家'; changed.foreshadows = [];
  const receipt = await store.commit(rewrite, 1, changed);
  assert.equal(receipt.revision, 2);
  const snapshot = await store.snapshot();
  assert.deepEqual(snapshot.progress.completed, [1]);
  assert.deepEqual(snapshot.progress.rewrites, []);
  assert.equal(snapshot.records[0]?.facts.foreshadows[0]?.id, 'letter');
  assert.equal(snapshot.records[0]?.contentHash, contentHash('林舟留在家里。'));
  const projected = JSON.parse((await store.files.read('meta/story_state.json'))!);
  assert.equal(projected.states['林舟'], '在家');
  assert.equal(projected.timeline.length, 1);
});

test('invalid cross-chapter facts are rejected before writing a pending record or final text', async (t) => {
  const { store } = await setup(t);
  const invalid = facts(); invalid.foreshadows = [{ id: 'absent', action: 'resolve', description: '' }];
  await assert.rejects(store.commit(identity, 1, invalid), /前置状态/);
  assert.equal(await store.files.read('meta/pending_commit.json'), null);
  assert.equal(await store.files.read(chapterPath(1)), null);
});

test('malformed checkpoints and missing progress never silently create an empty book', async (t) => {
  const { dir, store } = await setup(t);
  await store.commit(identity, 1, facts());
  await store.close();
  await writeFile(join(dir, 'meta/checkpoints.jsonl'), '{"seq":');
  await assert.rejects(BookStore.open(dir), /检查点/);
  assert.equal(await readFile(join(dir, 'meta/checkpoints.jsonl'), 'utf8'), '{"seq":');
  const orphan = await mkdtemp(join(tmpdir(), 'novelpilot-orphan-'));
  await mkdir(join(orphan, 'chapters'));
  await writeFile(join(orphan, 'chapters/01.md'), '正文');
  await assert.rejects(BookStore.open(orphan), /空书/);
});

test('tampered record hashes and widened recovery progress fail without changing the original evidence', async (t) => {
  const { dir, store } = await setup(t, async (point) => { if (point === 'pending_saved') throw new Error('stop'); });
  await assert.rejects(store.commit(identity, 1, facts()));
  await store.close();
  const path = join(dir, 'meta/pending_commit.json');
  const pending = JSON.parse(await readFile(path, 'utf8'));
  pending.after.completed = [1, 2];
  await writeFile(path, JSON.stringify(pending));
  await assert.rejects(BookStore.open(dir), /扩展/);
  assert.equal(await readFile(path, 'utf8'), JSON.stringify(pending));
});

test('commit and late draft mutation are serialized and receipt belongs to the original attempt', async (t) => {
  const { store, dir } = await setup(t);
  const outcomes = await Promise.allSettled([store.commit(identity, 1, facts()), store.saveDraft(identity, 1, '不应生效')]);
  assert.equal(outcomes[0]?.status, 'fulfilled'); assert.equal(outcomes[1]?.status, 'rejected');
  assert.deepEqual(await store.completion({ ...identity, attemptId: 'different' }), []);
  assert.equal(JSON.parse(await readFile(join(dir, recordPath(1)), 'utf8')).revision, 1);
});

test('orphan requirements, planning, drafts or revision do not get a fresh progress file', async () => {
  for (const path of ['meta/requirements.md', 'meta/planning.json', 'drafts/01.draft.md', 'meta/pending_revision.json']) {
    const dir = await mkdtemp(join(tmpdir(), 'orphan-state-'));
    await mkdir(join(dir, path.split('/')[0]!), { recursive: true });
    await writeFile(join(dir, path), 'existing evidence');
    await assert.rejects(BookStore.open(dir), /空书|作品资料/);
    await assert.rejects(readFile(join(dir, 'meta/progress.json')), { code: 'ENOENT' });
  }
});

test('commit cannot overlap manual sync and conflicting recoveries stay untouched', async (t) => {
  const { store, dir } = await setup(t, async (point) => { if (point === 'pending_saved') throw new Error('stop'); });
  await store.files.writeJSON('meta/pending_revision.json', { pending: true });
  await assert.rejects(store.commit(identity, 1, facts()), /同步/);
  assert.equal(await store.files.read('meta/pending_commit.json'), null);
  await store.files.remove('meta/pending_revision.json');
  await assert.rejects(store.commit(identity, 1, facts()), /stop/);
  const frozen = await store.files.read('meta/pending_commit.json');
  await store.files.writeJSON('meta/pending_revision.json', { pending: true });
  await store.close();
  await assert.rejects(BookStore.open(dir), /冲突/);
  assert.equal(await readFile(join(dir, 'meta/pending_commit.json'), 'utf8'), frozen);
  await assert.rejects(readFile(join(dir, chapterPath(1))), { code: 'ENOENT' });
});

test('checkpoint validation precedes append and duplicate keys cannot change receipt ownership', async (t) => {
  const { store } = await setup(t);
  const receipt = await store.commit(identity, 1, facts());
  const previous = await store.files.read('meta/checkpoints.jsonl');
  await assert.rejects(appendCheckpoint(store.files, { scope: 'chapter:1', step: 'commit_chapter', digest: receipt.digest, receipt: null }));
  await assert.rejects(appendCheckpoint(store.files, { scope: 'chapter:1', step: 'commit_chapter', digest: receipt.digest,
    receipt: { ...receipt, attemptId: 'someone-else' } }), /幂等/);
  assert.equal(await store.files.read('meta/checkpoints.jsonl'), previous);
});
