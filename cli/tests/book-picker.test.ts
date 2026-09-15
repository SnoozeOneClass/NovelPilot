import assert from 'node:assert/strict';
import { mkdtemp, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import type { Terminal } from '@earendil-works/pi-tui';
import { BookLibrary } from '../src/app/library.js';
import { pickBook } from '../src/ui/book-picker.js';

class PickerTerminal implements Terminal {
  columns = 120; rows = 36; kittyProtocolActive = false; writes: string[] = [];
  input: (data: string) => void = () => undefined;
  start(input: (data: string) => void) { this.input = input; }
  stop() {} async drainInput() {} write(value: string) { this.writes.push(value); }
  moveBy() {} hideCursor() {} showCursor() {} clearLine() {} clearFromCursor() {} clearScreen() {} setTitle() {} setProgress() {}
}
const paint = () => new Promise((resolve) => setTimeout(resolve, 40));

test('book picker opens an existing book through arrow selection rather than a typed command', async () => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-picker-'));
  const library = new BookLibrary(root);
  const original = await library.select('灯塔');
  const terminal = new PickerTerminal();
  const picked = pickBook(library, terminal);
  await paint();
  assert.match(terminal.writes.join(''), /我的书籍/);
  assert.match(terminal.writes.join(''), /新小说/);
  terminal.input('\x1b[B'); terminal.input('\r');
  assert.deepEqual(await picked, { title: '灯塔', directory: original });
});

test('empty library goes directly to an untitled discussion without starting terminal or asking a name', async () => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-picker-empty-'));
  const library = new BookLibrary(root); const terminal = new PickerTerminal();
  const picked = await pickBook(library, terminal);
  assert.equal(picked?.title, undefined);
  assert.ok(picked?.directory.startsWith(join(root, 'books', '.drafts')));
  assert.deepEqual(terminal.writes, []);
  assert.deepEqual(await library.list(), []);
  assert.deepEqual(await pickBook(library, terminal), picked);
});

test('new choice beside existing books creates untitled draft without a name form', async () => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-picker-new-'));
  const library = new BookLibrary(root); await library.select('已有书');
  const terminal = new PickerTerminal(); const pending = pickBook(library, terminal);
  await paint(); terminal.input('\r');
  const picked = await pending;
  assert.equal(picked?.title, undefined);
  assert.ok(picked?.directory.includes('.drafts'));
  assert.ok(!terminal.writes.join('').includes('为这本小说起一个名字'));
});

test('corrupt draft does not auto-resume or prevent selecting a named book', async () => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-picker-corrupt-'));
  const library = new BookLibrary(root); const bad = await library.createDraft();
  await writeFile(join(bad.directory, 'meta/library-workspace.json'), '{broken');
  const terminal = new PickerTerminal();
  const sole = pickBook(library, terminal); await paint();
  assert.ok(terminal.writes.join('').includes('无法恢复'));
  terminal.input('\x1b[B'); terminal.input('\r'); await paint();
  assert.ok(terminal.writes.join('').includes('缺失或损坏'));
  terminal.input('\x1b'); assert.equal(await sole, null);
  const named = await library.select('可用作品');
  const next = pickBook(library, terminal); await paint();
  terminal.input('\x1b[B'); terminal.input('\r');
  assert.deepEqual(await next, { title: '可用作品', directory: named });
});
