import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { BookLibrary } from '../src/app/library.js';
import { NovelApplication, type ApplicationUI } from '../src/app/application.js';
import { decodeTitleProposal, type BookStartIntent } from '../src/app/naming.js';
import { BookStore } from '../src/store/book-store.js';
import { BookLease } from '../src/store/book-lease.js';
import { deterministicBaselineRunner } from '../src/eval/runner.js';
import { createRoleSession } from '../src/runtime/pi/session.js';
import { scriptedProvider } from './fixtures/provider.js';
import type { SessionFactory } from '../src/app/sessions.js';
import type { NovelView } from '../src/ui/novel-tui.js';

const reply = '<reply>先讨论归乡与承诺。</reply><draft>守灯人归乡点灯，中文一章短篇。</draft><ready>true</ready><suggestions>补充人物关系</suggestions>';
test('title proposals reject unsafe names and cannot invent a user-provided title', () => {
  assert.deepEqual(decodeTitleProposal({ userTitle: '虚构用户书名', candidates: ['../bad', '《灯塔》', '灯塔', '归乡'] }, ['还没想好书名']), { userTitle: null, candidates: ['灯塔', '归乡'] });
  assert.equal(decodeTitleProposal({ userTitle: '灯塔', candidates: [] }, ['书名就叫灯塔']).userTitle, '灯塔');
});

test('unnamed discussion persists, names only at start, moves safely, and recovers the start intent once', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-naming-'));
  const library = new BookLibrary(root);
  const selection = await library.createDraft();
  let store = await BookStore.open(selection.directory);
  let intent: BookStartIntent | undefined;
  let namingCalls = 0; let choices = 0; const views: NovelView[] = [];
  const sessions: SessionFactory = async (role, tools) => {
    const p = await scriptedProvider(() => {
      if (role === 'arbiter') { namingCalls++; return [{ type: 'text', text: JSON.stringify({ userTitle: null, candidates: ['灯塔', '归乡', '海岸'] }) }]; }
      return [{ type: 'text', text: reply }];
    });
    return createRoleSession({ bookDir: store.files.root, modelRuntime: p.runtime, model: p.model, systemPrompt: 'naming fixture', tools });
  };
  const ui: ApplicationUI = { update: (v) => views.push(v), append() {}, appendDelta() {}, requestInput: async () => { throw new Error('不应提前询问书名'); },
    requestChoice: async (_label, items) => { choices++; return items[0]!.value; }, close: async () => undefined };
  let app = new NovelApplication(store, ui, { configDir: join(root, 'global'), sessionFactory: sessions, runner: deterministicBaselineRunner(), onNameChosen: (value) => { intent = value; } });
  t.after(() => app.close());
  await app.initialize();
  assert.equal(views.at(-1)?.bookTitle, '未命名作品'); assert.equal(choices, 0); assert.equal(namingCalls, 0);
  await app.command('我还没想好书名，先讨论一个守灯人归乡的故事');
  assert.equal(namingCalls, 0); assert.equal(choices, 0); assert.deepEqual(await library.list(), []);
  const saved = JSON.parse(await readFile(join(selection.directory, 'meta/discussion.json'), 'utf8'));
  assert.match(saved.state.draft, /守灯人/);
  // Restart an unnamed discussion; a title is still not required to recover it.
  await app.close(); store = await BookStore.open(selection.directory);
  app = new NovelApplication(store, ui, { configDir: join(root, 'global'), sessionFactory: sessions, runner: deterministicBaselineRunner(), onNameChosen: (value) => { intent = value; } });
  await app.initialize(); assert.match(views.at(-1)?.draft ?? '', /守灯人/);
  await app.command('/start');
  assert.equal(intent?.title, '灯塔'); assert.equal(choices, 1); assert.equal(namingCalls, 1);
  assert.equal((await store.snapshot()).progress.phase, 'discussion');
  await app.close();
  // Crash window before rename: saved naming intent is reused without another model call.
  store = await BookStore.open(selection.directory); intent = undefined;
  app = new NovelApplication(store, ui, { configDir: join(root, 'global'), sessionFactory: sessions, onNameChosen: (value) => { intent = value; } });
  await app.initialize(); assert.ok(intent); assert.equal(namingCalls, 1); await app.close();
  const named = await library.promoteDraft(selection.directory, '灯塔');
  assert.equal(named.directory, join(root, 'books', '灯塔'));
  store = await BookStore.open(named.directory);
  app = new NovelApplication(store, ui, { bookTitle: '灯塔', configDir: join(root, 'global'), sessionFactory: sessions, runner: deterministicBaselineRunner() });
  await app.initialize(); await app.idle();
  assert.equal((await store.snapshot()).progress.phase, 'complete');
  assert.equal(await store.files.read('meta/start-intent.json'), null);
  assert.match(await store.files.read('meta/requirements.md') ?? '', /^书名：灯塔/);
  assert.equal(namingCalls, 1);
});

test('managed identity stays locked after a title-directory rename and rejects corrupt identity', async () => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-managed-lock-'));
  const library = new BookLibrary(root); const draft = await library.createDraft();
  const before = JSON.parse(await readFile(join(draft.directory, 'meta/library-workspace.json'), 'utf8'));
  const named = await library.promoteDraft(draft.directory, '新书');
  assert.deepEqual(JSON.parse(await readFile(join(named.directory, 'meta/library-workspace.json'), 'utf8')), before);
  const lease = await BookLease.acquire(named.directory);
  try { await assert.rejects(BookLease.acquire(named.directory), /占用/); } finally { await lease.close(); }
  await writeFile(join(named.directory, 'meta/library-workspace.json'), '{bad-json');
  await assert.rejects(BookLease.acquire(named.directory), /无法读取/);
});

for (const mode of ['back', 'custom', 'provided'] as const) {
  test(`title selection ${mode} preserves the discussion and avoids an upfront name requirement`, async (t) => {
    const root = await mkdtemp(join(tmpdir(), 'novelpilot-title-choice-'));
    const store = await BookStore.open(root); let intent: BookStartIntent | undefined; let choices = 0;
    const ui: ApplicationUI = { update() {}, append() {}, appendDelta() {}, close: async () => undefined,
      requestInput: async () => '手选书名', requestChoice: async () => { choices++; return mode === 'back' ? '__back__' : '__custom__'; } };
    const app = new NovelApplication(store, ui, { configDir: join(root, 'global'), onNameChosen: (value) => { intent = value; },
      sessionFactory: async (role, tools) => {
        const p = await scriptedProvider(() => [{ type: 'text', text: role === 'arbiter' ? JSON.stringify({ userTitle: mode === 'provided' ? '灯塔' : null, candidates: ['推荐书名'] }) : reply }]);
        return createRoleSession({ bookDir: root, modelRuntime: p.runtime, model: p.model, tools, systemPrompt: 'title-choice-fixture' });
      } });
    t.after(() => app.close()); await app.initialize();
    await app.command(mode === 'provided' ? '书名就叫灯塔，讨论守灯人归乡。' : '先讨论守灯人归乡，没想好书名。');
    assert.equal(choices, 0); await app.command('/start');
    assert.equal((await store.snapshot()).progress.phase, 'discussion');
    assert.match((await store.files.read('meta/discussion.json')) ?? '', /守灯人/);
    if (mode === 'back') { assert.equal(intent, undefined); assert.equal(await store.files.read('meta/start-intent.json'), null); }
    else assert.equal(intent?.title, mode === 'provided' ? '灯塔' : '手选书名');
    assert.equal(choices, mode === 'provided' ? 0 : 1);
  });
}
