import assert from 'node:assert/strict';
import { fork } from 'node:child_process';
import { once } from 'node:events';
import { mkdtemp, readFile, readdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { BookLease } from '../src/store/book-lease.js';
import { MutationQueue, writeFileAtomic } from '../src/store/io.js';

test('OS lease excludes a second process and releases after normal exit and process kill', { timeout: 15000 }, async (t) => {
  const book = await mkdtemp(join(tmpdir(), 'novelpilot-lease-'));
  const child = () => {
    const proc = fork(new URL('./fixtures/lease-child.ts', import.meta.url), [book], {
      execArgv: ['--import', 'tsx'], stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
    });
    t.after(() => { if (proc.exitCode === null) proc.kill(); });
    return proc;
  };
  const first = child();
  assert.deepEqual((await once(first, 'message'))[0], { status: 'acquired' });
  const second = child();
  const secondExit = once(second, 'exit');
  assert.equal((await once(second, 'message'))[0].status, 'blocked');
  await secondExit;
  const firstExit = once(first, 'exit');
  first.send('close');
  await firstExit;
  const third = child();
  assert.equal((await once(third, 'message'))[0].status, 'acquired');
  const thirdExit = once(third, 'exit');
  third.kill('SIGKILL');
  await thirdExit;
  const recovered = await BookLease.acquire(book);
  await recovered.close();
  await recovered.close();
  assert.ok((await readdir(book)).includes('.novelpilot.lock'));
});

test('atomic replacement preserves complete UTF-8 content and leaves no temporary file', async () => {
  const book = await mkdtemp(join(tmpdir(), 'novelpilot-io-'));
  const file = join(book, '第一章.md');
  await writeFileAtomic(file, '旧正文');
  await writeFileAtomic(file, '新正文\n人物：林舟');
  assert.equal(await readFile(file, 'utf8'), '新正文\n人物：林舟');
  assert.deepEqual(await readdir(book), ['第一章.md']);
});

test('mutation queue protects the read/check/write sequence and survives a failed operation', async () => {
  const book = await mkdtemp(join(tmpdir(), 'novelpilot-mutation-'));
  const path = join(book, 'counter.json');
  await writeFile(path, '0');
  const queue = new MutationQueue();
  await assert.rejects(queue.run(async () => { throw new Error('injected'); }));
  await Promise.all(Array.from({ length: 20 }, () => queue.run(async () => {
    const current = Number(await readFile(path, 'utf8'));
    await writeFileAtomic(path, String(current + 1));
  })));
  assert.equal(await readFile(path, 'utf8'), '20');
});
