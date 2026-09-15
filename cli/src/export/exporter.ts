import { randomUUID } from 'node:crypto';
import { mkdir, open, link, rename, unlink } from 'node:fs/promises';
import { basename, dirname, extname, join, resolve, relative, isAbsolute } from 'node:path';
import { contentHash, decodeProgress, decodeRecord } from '../domain/book.js';
import { chapterPath, recordPath } from '../store/book-store.js';
import { BookFiles } from '../store/files.js';

export interface ExportOptions { output: string; title: string; author?: string; format?: 'txt' | 'epub'; from?: number; to?: number; overwrite?: boolean }
export interface ExportResult { path: string; chapters: number[]; skipped: number[]; unsynced: number[]; bytes: number }
interface Chapter { number: number; title: string; body: string }

/** No-clobber publication uses a hard link: an existing destination is never replaced by a race. */
export async function publishOutput(path: string, data: Uint8Array, overwrite = false): Promise<void> {
  await mkdir(dirname(path), { recursive: true });
  const temporary = join(dirname(path), `.${basename(path)}.${randomUUID()}.tmp`);
  const file = await open(temporary, 'wx', 0o600);
  try {
    try { await file.writeFile(data); await file.sync(); } finally { await file.close(); }
    if (overwrite) await rename(temporary, path); else await link(temporary, path);
  } finally { await unlink(temporary).catch((e: NodeJS.ErrnoException) => { if (e.code !== 'ENOENT') throw e; }); }
}
const xml = (s: string) => s.replace(/[^\u0009\u000A\u000D\u0020-\uD7FF\uE000-\uFFFD\u{10000}-\u{10FFFF}]/gu, '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&apos;' })[c]!);
function prepare(number: number, title: string, text: string): Chapter {
  const lines = text.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n').trim().split('\n');
  const first = lines[0]?.replace(/^#{1,6}\s+/, '').trim() ?? '';
  const heading = /^第\s*[0-9一二三四五六七八九十百千万零〇两]+\s*章(?:\s|$|[：:])/u.test(first);
  if (first === title || first === `第${number}章 ${title}` || heading) lines.shift();
  return { number, title: heading ? first : /^第\s*[0-9一二三四五六七八九十百千万零〇两]+\s*章/u.test(title) ? title : `第${number}章 ${title}`, body: lines.join('\n').trim() };
}

/** Minimal ZIP32 stored archive. EPUB requires mimetype to be the first, uncompressed member. */
function zip(entries: [string, string][]): Buffer {
  if (entries.length > 65535) throw new Error('EPUB 章节过多');
  const local: Buffer[] = [], central: Buffer[] = [];
  let offset = 0;
  for (const [name, value] of entries) {
    const filename = Buffer.from(name), body = Buffer.from(value);
    let crc = 0xffffffff;
    for (const byte of body) { crc ^= byte; for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0); }
    crc = (crc ^ 0xffffffff) >>> 0;
    if (offset + body.length > 0xffffffff) throw new Error('EPUB 超出 ZIP32 大小限制');
    const h = Buffer.alloc(30);
    h.writeUInt32LE(0x04034b50); h.writeUInt16LE(20, 4); h.writeUInt16LE(0x800, 6);
    h.writeUInt16LE(33, 12); h.writeUInt32LE(crc, 14); h.writeUInt32LE(body.length, 18); h.writeUInt32LE(body.length, 22); h.writeUInt16LE(filename.length, 26);
    local.push(h, filename, body);
    const c = Buffer.alloc(46);
    c.writeUInt32LE(0x02014b50); c.writeUInt16LE(20, 4); c.writeUInt16LE(20, 6); c.writeUInt16LE(0x800, 8); c.writeUInt16LE(33, 14);
    c.writeUInt32LE(crc, 16); c.writeUInt32LE(body.length, 20); c.writeUInt32LE(body.length, 24); c.writeUInt16LE(filename.length, 28); c.writeUInt32LE(offset, 42);
    central.push(c, filename); offset += h.length + filename.length + body.length;
  }
  const directory = Buffer.concat(central), end = Buffer.alloc(22);
  if (offset + directory.length > 0xffffffff) throw new Error('EPUB 超出 ZIP32 大小限制');
  end.writeUInt32LE(0x06054b50); end.writeUInt16LE(entries.length, 8); end.writeUInt16LE(entries.length, 10); end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...local, directory, end]);
}
function epub(options: ExportOptions, chapters: Chapter[]): Buffer {
  const page = (title: string, body: string) => `<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="zh"><head><title>${xml(title)}</title></head><body>${body}</body></html>`;
  const entries: [string, string][] = [
    ['mimetype', 'application/epub+zip'],
    ['META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'],
    ['OEBPS/nav.xhtml', page(options.title, `<nav epub:type="toc" id="toc"><h1>${xml(options.title)}</h1><ol>${chapters.map((c) => `<li><a href="chapter-${c.number}.xhtml">${xml(c.title)}</a></li>`).join('')}</ol></nav>`)],
    ['OEBPS/content.opf', `<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="book-id">urn:uuid:${randomUUID()}</dc:identifier><dc:title>${xml(options.title)}</dc:title><dc:language>zh</dc:language><dc:creator>${xml(options.author ?? '')}</dc:creator><meta property="dcterms:modified">${new Date().toISOString().replace(/\.\d{3}Z$/, 'Z')}</meta></metadata><manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>${chapters.map((c) => `<item id="ch${c.number}" href="chapter-${c.number}.xhtml" media-type="application/xhtml+xml"/>`).join('')}</manifest><spine>${chapters.map((c) => `<itemref idref="ch${c.number}"/>`).join('')}</spine></package>`],
  ];
  for (const c of chapters) entries.push([`OEBPS/chapter-${c.number}.xhtml`, page(c.title, `<h1>${xml(c.title)}</h1>${c.body.split(/\n\s*\n/).map((p) => `<p>${xml(p).replace(/\n/g, '<br/>')}</p>`).join('')}`)]);
  return zip(entries);
}

/** Host should pause/drain mutations before export; snapshots are rechecked before publishing. */
export async function exportBook(files: BookFiles, options: ExportOptions): Promise<ExportResult> {
  if (!options.title.trim()) throw new Error('导出书名不能为空');
  const path = resolve(options.output);
  const within = relative(resolve(files.root), path);
  if (!isAbsolute(within) && !within.startsWith('..') && /^(?:meta|chapters|drafts)(?:[\\/]|$)/i.test(within)) throw new Error('导出不能覆盖作品内部数据目录');
  const format = options.format ?? (extname(options.output).toLowerCase() === '.epub' ? 'epub' : extname(options.output).toLowerCase() === '.txt' ? 'txt' : undefined);
  if (!format || !['txt', 'epub'].includes(format)) throw new Error('导出仅支持 TXT 和 EPUB');
  const progressText = await files.read('meta/progress.json');
  const progress = await files.json('meta/progress.json', decodeProgress);
  if (!progress?.completed.length) throw new Error('尚无已完成章节可导出');
  const from = options.from ?? 1, to = options.to ?? progress.completed.length;
  if (!Number.isSafeInteger(from) || !Number.isSafeInteger(to) || from < 1 || to < from || to - from > 100000) throw new Error('章节范围无效或过大');
  const chapters: Chapter[] = [], skipped: number[] = [], unsynced: number[] = [];
  const snapshot = new Map<string, string>();
  for (let n = from; n <= to; n++) {
    if (!progress.completed.includes(n)) { skipped.push(n); continue; }
    const record = await files.json(recordPath(n), decodeRecord);
    if (!record || record.chapter !== n) throw new Error(`第 ${n} 章接纳记录缺失或无效`);
    const body = await files.read(chapterPath(n));
    if (!body?.trim()) throw new Error(`已完成第 ${n} 章正文缺失或为空`);
    snapshot.set(chapterPath(n), body);
    const recordText = await files.read(recordPath(n));
    if (recordText === null || JSON.stringify(decodeRecord(JSON.parse(recordText))) !== JSON.stringify(record)) throw new Error('导出期间接纳记录改变，请重试');
    snapshot.set(recordPath(n), recordText);
    if (contentHash(body) !== record.contentHash) unsynced.push(n);
    chapters.push(prepare(n, record.facts.title, body));
  }
  if (!chapters.length) throw new Error('范围内没有已完成章节');
  const data = format === 'epub' ? epub(options, chapters) : Buffer.from(`${options.title}\n\n${chapters.map((c) => `${c.title}\n\n${c.body}`).join('\n\n')}\n`);
  if (progressText !== await files.read('meta/progress.json')) throw new Error('导出期间作品进度改变，请重试');
  for (const [path, body] of snapshot) if (await files.read(path) !== body) throw new Error('导出期间正文改变，请重试');
  await publishOutput(path, data, options.overwrite);
  return { path, chapters: chapters.map((c) => c.number), skipped, unsynced, bytes: data.length };
}
