import { lstat, mkdir, open, readFile, readdir, realpath, rename, writeFile } from 'node:fs/promises';
import { basename, dirname, join, resolve } from 'node:path';
import { randomUUID } from 'node:crypto';
import { tryLock, unlock } from 'fs-native-extensions';
import { missing } from '../store/files.js';
import { BookLease } from '../store/book-lease.js';

export interface BookSelection { title?: string; directory: string }
export interface DraftListing { directory: string; label: string; available: boolean }
const queues = new Map<string, Promise<unknown>>();

export function validateBookTitle(title: string): string {
  if (!title || title !== title.trim() || title.length > 80 || /[<>:"/\\|?*\x00-\x1f]/.test(title) ||
      title.startsWith('.') || /[. ]$/.test(title) || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(title)) {
    throw new Error('书名须为 1–80 个字符，不能包含路径符号、首尾空格或系统保留名称');
  }
  return title;
}

/** One startup directory owns books/<title>. It is not a book itself. */
export class BookLibrary {
  readonly booksDir: string;
  constructor(readonly projectDir: string) { this.booksDir = join(projectDir, 'books'); }
  private async directory(path: string): Promise<boolean> {
    try {
      const stat = await lstat(path);
      if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error(`书籍路径必须是普通目录：${path}`);
      return true;
    } catch (error) { if (missing(error)) return false; throw error; }
  }
  async list(): Promise<string[]> {
    if (!await this.directory(this.booksDir)) return [];
    return (await readdir(this.booksDir, { withFileTypes: true })).filter((entry) => entry.isDirectory() && !entry.isSymbolicLink())
      .map((entry) => entry.name).filter((name) => !name.startsWith('.')).sort((a, b) => a.localeCompare(b, 'zh-CN'));
  }
  private async noLink(path: string): Promise<void> {
    try { if ((await lstat(path)).isSymbolicLink()) throw new Error('目录占用文件不能是符号链接'); }
    catch (error) { if (!missing(error)) throw error; }
  }
  private async catalog<T>(operation: () => Promise<T>): Promise<T> {
    const canonical = await realpath(this.projectDir);
    const key = process.platform === 'win32' ? canonical.toLowerCase() : canonical;
    const pending = (queues.get(key) ?? Promise.resolve()).then(async () => {
      if (!await this.directory(this.booksDir)) await mkdir(this.booksDir);
      await this.directory(this.booksDir);
      const path = join(this.booksDir, '.catalog.lock');
      await this.noLink(path);
      const lock = await open(path, 'a+', 0o600);
      let acquired = false;
      try {
        acquired = tryLock(lock.fd);
        if (!acquired) throw new Error('书籍目录正在被其他进程更新，请重试');
        return await operation();
      } finally { if (acquired) unlock(lock.fd); await lock.close(); }
    });
    const settled = pending.catch(() => undefined);
    queues.set(key, settled);
    try { return await pending; } finally { if (queues.get(key) === settled) queues.delete(key); }
  }
  async createDraft(): Promise<BookSelection> {
    return this.catalog(async () => {
      const drafts = join(this.booksDir, '.drafts');
      if (!await this.directory(drafts)) await mkdir(drafts);
      await this.directory(drafts);
      const id = randomUUID();
      const path = join(drafts, id);
      await mkdir(path);
      await mkdir(join(path, 'meta'));
      await writeFile(join(path, 'meta', 'library-workspace.json'), JSON.stringify({ version: 1, id }) + '\n', { flag: 'wx', mode: 0o600 });
      return { directory: await realpath(path) };
    });
  }
  async listDrafts(): Promise<DraftListing[]> {
    if (!await this.directory(this.booksDir)) return [];
    const drafts = join(this.booksDir, '.drafts');
    if (!await this.directory(drafts)) return [];
    const result: DraftListing[] = [];
    for (const entry of await readdir(drafts, { withFileTypes: true })) {
      if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
      const directory = await realpath(join(drafts, entry.name));
      const stat = await lstat(directory);
      let available = true;
      try { await this.selectDraft(directory); } catch { available = false; }
      result.push({ directory, available, label: `${available ? '未命名讨论' : '无法恢复的讨论（标识缺失或损坏）'} · ${stat.birthtime.toISOString().slice(0, 16).replace('T', ' ')} UTC · ${entry.name.slice(0, 8)}` });
    }
    return result.sort((a, b) => a.label.localeCompare(b.label));
  }
  async selectDraft(directory: string): Promise<BookSelection> {
    await this.directory(this.booksDir);
    const drafts = join(this.booksDir, '.drafts');
    if (!await this.directory(drafts) || dirname(resolve(directory)) !== resolve(drafts)) throw new Error('未命名作品路径不在本项目内');
    if (!await this.directory(directory)) throw new Error('未命名作品不存在');
    const canonical = await realpath(directory);
    if (dirname(canonical) !== await realpath(drafts)) throw new Error('未命名作品路径越界');
    await this.directory(join(canonical, 'meta'));
    const markerPath = join(canonical, 'meta', 'library-workspace.json');
    await this.noLink(markerPath);
    let marker: unknown;
    try { marker = JSON.parse(await readFile(markerPath, 'utf8')); }
    catch { throw new Error('未命名讨论标识缺失或损坏；请保留目录并检查记录，不能当作空书启动'); }
    if (!marker || typeof marker !== 'object' || !('version' in marker) || marker.version !== 1 || !('id' in marker) || marker.id !== basename(canonical)) throw new Error('未命名作品标识损坏');
    return { directory: canonical };
  }
  async promoteDraft(directory: string, title: string): Promise<BookSelection> {
    validateBookTitle(title);
    return this.catalog(async () => {
      const source = (await this.selectDraft(directory)).directory;
      await this.noLink(join(source, '.novelpilot.lock'));
      const lease = await BookLease.acquire(source);
      try {
        const root = await realpath(this.booksDir);
        let selected = title;
        let destination = join(root, selected);
        for (let suffix = 2; ; suffix++) {
          try { await lstat(destination); }
          catch (error) { if (missing(error)) break; throw error; }
          selected = `${title.slice(0, 70)} (${suffix})`;
          destination = join(root, selected);
        }
        if (dirname(resolve(destination)) !== root || dirname(source) !== await realpath(join(root, '.drafts')) || basename(source).startsWith('.')) throw new Error('作品重命名路径越界');
        // Same-volume rename while retaining the source's OS lease; never merge directories.
        await rename(source, destination);
        return { title, directory: destination };
      } finally { await lease.close(); }
    });
  }
  async select(title: string, create = true): Promise<string> {
    validateBookTitle(title);
    if (!create) {
      if (!await this.directory(this.booksDir) || !await this.directory(join(this.booksDir, title))) throw new Error(`找不到书籍：${title}`);
      return realpath(join(this.booksDir, title));
    }
    return this.catalog(async () => {
      const path = join(this.booksDir, title);
      if (!await this.directory(path)) await mkdir(path);
      await this.directory(path);
      return realpath(path);
    });
  }
}
