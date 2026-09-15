import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test, type TestContext } from 'node:test';
import { BookStore, chapterPath } from '../src/store/book-store.js';
import { RevisionService, type RevisionAnalysis } from '../src/store/revision.js';
import type { ChapterFacts } from '../src/domain/book.js';

const facts = (chapter: number): ChapterFacts => ({ title: `第${chapter}章`, summary: '原摘要', characters: [], keyEvents: [], timeline: [], stateChanges: [], relationships: [], foreshadows: [] });
const analysis = (chapter: number): RevisionAnalysis => ({ facts: { ...facts(chapter), summary: `修订后摘要${chapter}` }, style: [], feedback: ['调整后续计划'] });
async function setup(t: TestContext) {
  const dir = await mkdtemp(join(tmpdir(), 'novelpilot-revision-'));
  const store = await BookStore.open(dir);
  t.after(() => store.close());
  await store.beginCreation('两章小说');
  for (const chapter of [1, 2]) {
    const id = { taskId: `write-${chapter}`, attemptId: 'a' };
    await store.beginChapter(id, chapter); await store.saveDraft(id, chapter, `原正文${chapter}`); await store.commit(id, chapter, facts(chapter));
  }
  return { store, dir };
}
for (const stage of ['prepared', 'record:1', 'record:2', 'records_applied', 'projections_written', 'projections_applied', 'checkpoint_saved']) {
  test(`manual sync resumes ${stage} without reanalysis or rewriting user text`, async (t) => {
    const { store, dir } = await setup(t);
    for (const n of [1, 2]) await writeFile(join(dir, chapterPath(n)), `用户改稿${n}`);
    let calls = 0;
    const service = new RevisionService(store, async (point) => { if (point === stage) throw new Error('injected'); });
    assert.equal((await service.check()).length, 2);
    await assert.rejects(service.sync(async (change) => { calls++; return analysis(change.chapter); }, new AbortController().signal), /injected/);
    assert.equal(calls, 2);
    await store.close();
    const recovered = await BookStore.open(dir); t.after(() => recovered.close());
    const resume = new RevisionService(recovered);
    assert.deepEqual(await resume.sync(async () => { throw new Error('must not call model'); }, new AbortController().signal), [1, 2]);
    assert.deepEqual(await resume.check(), []);
    assert.deepEqual(await resume.sync(async () => { throw new Error('no change'); }, new AbortController().signal), []);
    const snapshot = await recovered.snapshot();
    assert.deepEqual(snapshot.records.map((r) => r.revision), [2, 2]);
    for (const n of [1, 2]) assert.equal(await readFile(join(dir, chapterPath(n)), 'utf8'), `用户改稿${n}`);
    assert.equal((await readFile(join(dir, 'meta/checkpoints.jsonl'), 'utf8')).trim().split('\n').length, 3);
    assert.deepEqual(JSON.parse((await recovered.files.read('meta/planning_feedback.json'))!), ['调整后续计划', '调整后续计划']);
  });
}
test('analysis becomes stale when user edits again, preserving new text and baseline', async (t) => {
  const { store, dir } = await setup(t);
  await writeFile(join(dir, chapterPath(1)), '第一次手改');
  const service = new RevisionService(store);
  await assert.rejects(service.sync(async () => {
    await writeFile(join(dir, chapterPath(1)), '第二次手改'); return analysis(1);
  }, new AbortController().signal), /再次修改/);
  assert.equal(await store.files.read('meta/pending_revision.json'), null);
  assert.equal((await store.snapshot()).records[0]?.revision, 1);
  assert.equal(await readFile(join(dir, chapterPath(1)), 'utf8'), '第二次手改');
});
test('BOM and line-ending normalization do not create spurious changes', async (t) => {
  const { store, dir } = await setup(t);
  await writeFile(join(dir, chapterPath(1)), '\uFEFF原正文1');
  assert.deepEqual(await new RevisionService(store).check(), []);
});

test('stale prepared analysis with zero applied records is discarded and next sync can proceed', async (t) => {
  const { store, dir } = await setup(t);
  await writeFile(join(dir, chapterPath(1)), '第一次修改');
  await assert.rejects(new RevisionService(store, async (stage) => { if (stage === 'prepared') throw new Error('stop'); })
    .sync(async () => analysis(1), new AbortController().signal));
  await writeFile(join(dir, chapterPath(1)), '第二次修改');
  const service = new RevisionService(store);
  await assert.rejects(service.resume(), /再次修改/);
  assert.equal(await store.files.read('meta/pending_revision.json'), null);
  assert.equal((await store.snapshot()).records[0]?.revision, 1);
  assert.deepEqual(await service.sync(async () => analysis(1), new AbortController().signal), [1]);
  assert.equal((await store.snapshot()).records[0]?.content, '第二次修改');
});

for (const stage of ['record:2', 'records_applied', 'projections_applied']) {
  test(`fully accepted sync at ${stage} finishes projection without overwriting newer external text`, async (t) => {
    const { store, dir } = await setup(t);
    for (const chapter of [1, 2]) await writeFile(join(dir, chapterPath(chapter)), `第一次修改${chapter}`);
    await assert.rejects(new RevisionService(store, async (point) => { if (point === stage) throw new Error('stop'); })
      .sync(async (change) => analysis(change.chapter), new AbortController().signal));
    await writeFile(join(dir, chapterPath(1)), '再次修改的新文本');
    const service = new RevisionService(store);
    assert.deepEqual(await service.resume(), [1, 2]);
    assert.equal(await store.files.read(chapterPath(1)), '再次修改的新文本');
    assert.equal((await store.snapshot()).records[0]?.content, '第一次修改1');
    assert.deepEqual((await service.check()).map((change) => change.chapter), [1]);
    await service.sync(async () => analysis(1), new AbortController().signal);
    assert.equal((await store.snapshot()).records[0]?.revision, 3);
  });
}

test('partially accepted stale batch keeps recovery evidence rather than losing accepted projections', async (t) => {
  const { store, dir } = await setup(t);
  for (const chapter of [1, 2]) await writeFile(join(dir, chapterPath(chapter)), `第一次修改${chapter}`);
  await assert.rejects(new RevisionService(store, async (stage) => { if (stage === 'record:1') throw new Error('stop'); })
    .sync(async (change) => analysis(change.chapter), new AbortController().signal));
  const pending = await store.files.read('meta/pending_revision.json');
  await writeFile(join(dir, chapterPath(2)), '第二章再次修改');
  await assert.rejects(new RevisionService(store).resume(), /再次修改/);
  assert.equal(await store.files.read('meta/pending_revision.json'), pending);
  assert.deepEqual((await store.snapshot()).records.map((r) => r.revision), [2, 1]);
  assert.equal(await store.files.read(chapterPath(2)), '第二章再次修改');
});

test('sync refuses mismatched final projection without manufacturing a checkpoint', async (t) => {
  const { store, dir } = await setup(t);
  await writeFile(join(dir, chapterPath(1)), '用户修改');
  await assert.rejects(new RevisionService(store, async (stage) => { if (stage === 'projections_applied') throw new Error('stop'); })
    .sync(async () => analysis(1), new AbortController().signal));
  const checkpoints = await store.files.read('meta/checkpoints.jsonl');
  await writeFile(join(dir, 'meta/story_state.json'), '{}');
  await assert.rejects(new RevisionService(store).resume(), /终态/);
  assert.equal(await store.files.read('meta/checkpoints.jsonl'), checkpoints);
  assert.notEqual(await store.files.read('meta/pending_revision.json'), null);
});
