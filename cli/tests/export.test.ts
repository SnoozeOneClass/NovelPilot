import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { contentHash, emptyProgress } from '../src/domain/book.js';
import { BookFiles } from '../src/store/files.js';
import { exportBook, publishOutput } from '../src/export/exporter.js';

async function fixture(t: { after(fn: () => Promise<void>): void }) {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-export-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const files = new BookFiles(root);
  const body = '# 第1章 启程\n\n秘密正文 <风> & 雨。';
  await files.writeJSON('meta/progress.json', { ...emptyProgress(), phase: 'writing', completed: [1] });
  await files.write('chapters/01.md', body);
  await files.writeJSON('meta/chapter_records/000001.json', {
    version: 1, chapter: 1, revision: 1, origin: 'generated', content: body, contentHash: contentHash(body), style: [], acceptedAt: new Date().toISOString(),
    facts: { title: '启程', summary: 'summary', characters: [], keyEvents: [], timeline: [], stateChanges: [], relationships: [], foreshadows: [] },
  });
  return { files, root };
}
test('TXT exports actual final text once, reports skipped and unsynced chapters', async (t) => {
  const { files, root } = await fixture(t);
  await files.write('chapters/01.md', '# 第1章 启程\n\n用户修改。');
  const result = await exportBook(files, { title: '书名', output: join(root, 'novel.txt'), to: 3 });
  assert.deepEqual(result.chapters, [1]); assert.deepEqual(result.skipped, [2, 3]); assert.deepEqual(result.unsynced, [1]);
  const out = await readFile(result.path, 'utf8');
  assert.equal(out.match(/第1章/g)?.length, 1); assert.ok(out.includes('用户修改。')); assert.ok(!out.includes('秘密正文'));
  await assert.rejects(exportBook(files, { title: '书名', output: result.path }), { code: 'EEXIST' });
  assert.equal(await readFile(result.path, 'utf8'), out);
});
test('missing completed chapter errors and no output is published', async (t) => {
  const { files, root } = await fixture(t);
  await files.remove('chapters/01.md');
  await assert.rejects(exportBook(files, { title: '书名', output: join(root, 'novel.txt') }), /正文缺失/);
  assert.ok(!(await readdir(root)).includes('novel.txt'));
});
test('EPUB has stored first mimetype, valid directory offsets, navigation and escaped XHTML', async (t) => {
  const { files, root } = await fixture(t);
  await exportBook(files, { title: '书&名', output: join(root, 'novel.epub') });
  const data = await readFile(join(root, 'novel.epub'));
  const entries = new Map<string, string>();
  let cursor = 0;
  while (data.readUInt32LE(cursor) === 0x04034b50) {
    const size = data.readUInt32LE(cursor + 18), length = data.readUInt16LE(cursor + 26), extra = data.readUInt16LE(cursor + 28);
    assert.equal(data.readUInt16LE(cursor + 8), 0);
    const name = data.subarray(cursor + 30, cursor + 30 + length).toString();
    entries.set(name, data.subarray(cursor + 30 + length + extra, cursor + 30 + length + extra + size).toString());
    cursor += 30 + length + extra + size;
  }
  assert.equal(entries.keys().next().value, 'mimetype'); assert.equal(entries.get('mimetype'), 'application/epub+zip');
  assert.equal(data.readUInt32LE(cursor), 0x02014b50);
  assert.equal(data.readUInt32LE(data.length - 6), cursor);
  assert.ok(entries.get('OEBPS/nav.xhtml')?.includes('chapter-1.xhtml'));
  assert.ok(entries.get('OEBPS/content.opf')?.includes('properties="nav"'));
  assert.ok(entries.get('OEBPS/chapter-1.xhtml')?.includes('&lt;风&gt; &amp; 雨。'));
  assert.equal(entries.get('OEBPS/chapter-1.xhtml')?.match(/<h1>/g)?.length, 1);
});
test('concurrent no-overwrite publication has exactly one winner and no staging remnants', async (t) => {
  const { root } = await fixture(t);
  const path = join(root, 'race.txt');
  const results = await Promise.allSettled([publishOutput(path, Buffer.from('a')), publishOutput(path, Buffer.from('b'))]);
  assert.equal(results.filter((r) => r.status === 'fulfilled').length, 1);
  assert.ok(['a', 'b'].includes(await readFile(path, 'utf8')));
  assert.ok(!(await readdir(root)).some((name) => name.endsWith('.tmp')));
});
