import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, readdir, rename, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { BookLibrary } from '../src/app/library.js';
import { BookStore } from '../src/store/book-store.js';
import { BookLease } from '../src/store/book-lease.js';
import { spawn } from 'node:child_process';
import { once } from 'node:events';

test('books live under project/books/title and opening an existing title preserves its files', async () => {
  const project = await mkdtemp(join(tmpdir(), 'novelpilot-library-'));
  const library = new BookLibrary(project);
  assert.deepEqual(await library.list(), []);
  assert.deepEqual(await readdir(project), []);
  const directory = await library.select('灯塔');
  assert.equal(directory, join(project, 'books', '灯塔'));
  await writeFile(join(directory, 'note.txt'), '保留已有内容');
  assert.equal(await library.select('灯塔'), directory);
  assert.equal(await readFile(join(directory, 'note.txt'), 'utf8'), '保留已有内容');
  assert.deepEqual(await library.list(), ['灯塔']);
  const store = await BookStore.open(directory);
  await store.close();
  assert.ok((await readdir(directory)).includes('meta'));
  assert.ok(!(await readdir(project)).includes('meta'));
});

test('invalid titles and linked book roots cannot escape the project library', async () => {
  const project = await mkdtemp(join(tmpdir(), 'novelpilot-library-path-'));
  const library = new BookLibrary(project);
  for (const title of ['', '..', '../other', 'x/y', 'C:\\other', 'CON', '书名.', ' 书名']) await assert.rejects(library.select(title));
  assert.deepEqual(await readdir(project), []);
  const outside = await mkdtemp(join(tmpdir(), 'novelpilot-library-outside-'));
  await symlink(outside, join(project, 'books'), process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(library.select('新书'), /普通目录/);
  assert.deepEqual(await readdir(outside), []);
});

test('concurrent startup selects one directory and read-only selection does not create a book', async () => {
  const project = await mkdtemp(join(tmpdir(), 'novelpilot-library-race-'));
  const library = new BookLibrary(project);
  await assert.rejects(library.select('不存在', false));
  assert.deepEqual(await readdir(project), []);
  const paths = await Promise.all([library.select('同一本书'), new BookLibrary(project).select('同一本书')]);
  assert.equal(paths[0], paths[1]);
  assert.deepEqual(await library.list(), ['同一本书']);
});

test('untitled drafts remain resumable and promotion preserves every file without overwriting title collision', async () => {
  const root = await mkdtemp(join(tmpdir(), 'library-promote-'));
  const library = new BookLibrary(root);
  const existing = await library.select('归乡');
  await writeFile(join(existing, 'original.txt'), 'existing');
  const draft = await library.createDraft();
  await mkdir(join(draft.directory, 'meta'), { recursive: true });
  const documents = { 'discussion.json': '{"draft":"完整世界观与人物","userMessages":["用户原文"]}', 'start-intent.json': '{"version":1,"title":"归乡","draft":"完整要求"}', 'model-settings.json': '{"configured":true}',
    'progress.json': '{"version":1,"revision":0,"phase":"discussion","completed":[],"rewrites":[],"active":null}', 'credentials.json': '{"apiKey":"fixture-only"}' };
  for (const [file, content] of Object.entries(documents)) await writeFile(join(draft.directory, 'meta', file), content);
  assert.deepEqual(await library.list(), ['归乡']);
  const listed = await library.listDrafts();
  assert.equal(listed.length, 1); assert.ok(!listed[0]!.label.includes('用户原文'));
  const promoted = await library.promoteDraft(draft.directory, '归乡');
  assert.equal(promoted.title, '归乡');
  assert.equal(promoted.directory, join(root, 'books', '归乡 (2)'));
  for (const [file, content] of Object.entries(documents)) assert.equal(await readFile(join(promoted.directory, 'meta', file), 'utf8'), content);
  assert.equal(await readFile(join(existing, 'original.txt'), 'utf8'), 'existing');
  assert.deepEqual(await library.listDrafts(), []);
  const lease = await BookLease.acquire(promoted.directory); await lease.close();
});

test('promotion refuses an occupied draft and unsafe source or symlink escapes', async () => {
  const root = await mkdtemp(join(tmpdir(), 'library-lock-'));
  const library = new BookLibrary(root); const draft = await library.createDraft();
  const lease = await BookLease.acquire(draft.directory);
  try { await assert.rejects(library.promoteDraft(draft.directory, '标题'), /占用/); }
  finally { await lease.close(); }
  assert.deepEqual(await library.selectDraft(draft.directory), draft);
  const outside = await mkdtemp(join(tmpdir(), 'library-outside-'));
  await assert.rejects(library.promoteDraft(outside, '标题'), /项目内/);
  const link = join(root, 'books', '.drafts', 'outside-link');
  await symlink(outside, link, process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(library.promoteDraft(link, '标题'), /普通目录/);
  assert.deepEqual(await readdir(outside), []);
  assert.equal((await library.promoteDraft(draft.directory, '标题')).title, '标题');
});

test('catalog mutation rejects a competing process holding the catalog OS lock', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'library-catalog-'));
  const library = new BookLibrary(root); await library.createDraft();
  const child = spawn(process.execPath, ['--input-type=module', '-e',
    "import {open} from 'node:fs/promises'; import {tryLock} from 'fs-native-extensions'; const f=await open(process.argv[1],'a+'); if(!tryLock(f.fd))process.exit(2); process.stdout.write('ready'); setInterval(()=>{},1000);",
    join(root, 'books', '.catalog.lock')], { stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => { child.kill(); });
  await Promise.race([once(child.stdout!, 'data'), once(child, 'exit').then(() => { throw new Error('catalog lock child exited before ready'); })]);
  await assert.rejects(library.createDraft(), /其他进程/);
  const exited = once(child, 'exit'); child.kill(); await exited;
  assert.equal((await library.listDrafts()).length, 1);
  await library.createDraft();
  assert.equal((await library.listDrafts()).length, 2);
});

test('partial creation and corrupt draft markers are listed unavailable without losing evidence', async () => {
  const root = await mkdtemp(join(tmpdir(), 'library-incomplete-'));
  const library = new BookLibrary(root); const valid = await library.createDraft();
  const partial = join(root, 'books', '.drafts', 'partial'); await mkdir(partial);
  const corrupt = await library.createDraft();
  await writeFile(join(corrupt.directory, 'meta/library-workspace.json'), '{broken');
  const list = await library.listDrafts();
  assert.equal(list.length, 3);
  assert.equal(list.filter((entry) => entry.available).length, 1);
  await assert.rejects(library.selectDraft(partial), /缺失或损坏/);
  await assert.rejects(library.selectDraft(corrupt.directory), /缺失或损坏/);
  assert.equal((await library.selectDraft(valid.directory)).directory, valid.directory);
  assert.equal(await readFile(join(corrupt.directory, 'meta/library-workspace.json'), 'utf8'), '{broken');
  assert.deepEqual(await readdir(partial), []);
});

test('managed source and renamed destination remain covered by the same OS lease', async () => {
  const root = await mkdtemp(join(tmpdir(), 'library-identity-'));
  const library = new BookLibrary(root); const draft = await library.createDraft();
  const destination = join(root, 'books', '命名后');
  assert.ok(draft.directory.startsWith(join(root, 'books', '.drafts')));
  assert.equal(destination, join(library.booksDir, '命名后'));
  const sourceLease = await BookLease.acquire(draft.directory);
  try {
    await rename(draft.directory, destination);
    await assert.rejects(BookLease.acquire(destination), /占用/);
  } finally { await sourceLease.close(); }
  const targetLease = await BookLease.acquire(destination); await targetLease.close();
});
