import { lstat, mkdir, open, readFile, unlink } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { DataError } from '../domain/validation.js';
import { writeFileAtomic } from './io.js';

export function missing(error: unknown): boolean { return !!error && typeof error === 'object' && 'code' in error && error.code === 'ENOENT'; }

/** Relative paths are application-owned. Reject links rather than following them out of the book. */
export class BookFiles {
  constructor(readonly root: string) {}
  async path(relative: string): Promise<string> {
    const parts = relative.split('/');
    if (!relative || parts.some((p) => !p || p === '.' || p === '..' || /[\\:\0]/.test(p))) throw new DataError('无效作品路径');
    let path = this.root;
    for (const part of parts) {
      path = join(path, part);
      try { if ((await lstat(path)).isSymbolicLink()) throw new DataError(`作品路径不能是符号链接：${relative}`); }
      catch (error) { if (!missing(error)) throw error; }
    }
    return path;
  }
  async read(relative: string): Promise<string | null> {
    try { return await readFile(await this.path(relative), 'utf8'); }
    catch (error) { if (missing(error)) return null; throw error; }
  }
  async json<T>(relative: string, decode: (value: unknown) => T): Promise<T | null> {
    const content = await this.read(relative);
    if (content === null) return null;
    try { return decode(JSON.parse(content)); }
    catch (error) { throw new DataError(`无法读取 ${relative}：${String(error)}`); }
  }
  async write(relative: string, content: string): Promise<void> { await writeFileAtomic(await this.path(relative), content); }
  async writeJSON(relative: string, value: unknown): Promise<void> { await this.write(relative, JSON.stringify(value, null, 2) + '\n'); }
  async append(relative: string, value: unknown): Promise<void> {
    const path = await this.path(relative);
    await mkdir(dirname(path), { recursive: true });
    const file = await open(path, 'a', 0o600);
    try { await file.writeFile(JSON.stringify(value) + '\n'); await file.sync(); } finally { await file.close(); }
  }
  async remove(relative: string): Promise<void> {
    try { await unlink(await this.path(relative)); } catch (error) { if (!missing(error)) throw error; }
  }
}
