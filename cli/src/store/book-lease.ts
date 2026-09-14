import { open, realpath, type FileHandle } from 'node:fs/promises';
import { join } from 'node:path';
import { tryLock, unlock } from 'fs-native-extensions';

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
    const file = await open(join(bookDir, '.novelpilot.lock'), 'a+', 0o600);
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
