import { lstat, mkdir, open, realpath, type FileHandle } from 'node:fs/promises';
import { basename, dirname, join } from 'node:path';
import { tryLock, unlock } from 'fs-native-extensions';
import { BookFiles, missing } from './files.js';

async function lockPath(bookDir: string): Promise<string> {
  const identity = await new BookFiles(bookDir).json('meta/library-workspace.json', (value) => {
    if (!value || typeof value !== 'object' || !('version' in value) || value.version !== 1 || !('id' in value) ||
        typeof value.id !== 'string' || !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(value.id)) throw new Error('作品身份记录损坏');
    return value.id;
  });
  const parent = dirname(bookDir);
  if (!identity) {
    if (basename(parent) === '.drafts') throw new Error('未命名作品缺少身份记录');
    return join(bookDir, '.novelpilot.lock');
  }
  const isDraft = basename(parent) === '.drafts';
  const library = isDraft ? dirname(parent) : parent;
  if (basename(library) !== 'books' || (isDraft && basename(bookDir) !== identity)) throw new Error('作品身份与书库路径不一致');
  const locks = join(library, '.locks');
  try { await mkdir(locks); } catch (error) {
    if (!(error && typeof error === 'object' && 'code' in error && error.code === 'EEXIST')) throw error;
  }
  const stat = await lstat(locks);
  if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error('作品锁目录不能是链接');
  return join(locks, `${identity}.lock`);
}

export class BookInUseError extends Error {
  constructor(bookDir: string) {
    super(`小说目录已被另一个 NovelPilot 实例占用：${bookDir}`);
    this.name = 'BookInUseError';
  }
}

/** Keep the file: unlinking it would allow two different inodes to be locked. */
export class BookLease {
  private closing: Promise<void> | undefined;
  private constructor(readonly bookDir: string, private readonly file: FileHandle) {}

  static async acquire(directory: string): Promise<BookLease> {
    const bookDir = await realpath(directory);
    // Managed books keep stable OS-lock identity outside the movable title directory.
    const path = await lockPath(bookDir);
    try { if ((await lstat(path)).isSymbolicLink()) throw new Error('作品锁文件不能是链接'); }
    catch (error) { if (!missing(error)) throw error; }
    const file = await open(path, 'a+', 0o600);
    try {
      if (!tryLock(file.fd)) throw new BookInUseError(bookDir);
      return new BookLease(bookDir, file);
    } catch (error) {
      await file.close();
      throw error;
    }
  }

  close(): Promise<void> {
    this.closing ??= (async () => {
      try { unlock(this.file.fd); } finally { await this.file.close(); }
    })();
    return this.closing;
  }
}
