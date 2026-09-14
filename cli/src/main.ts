#!/usr/bin/env node
import { resolveStartupPaths } from './app/paths.js';
import { BookLease } from './store/book-lease.js';
import { BookLifetime, EventQueue } from './app/lifetime.js';
import { createStartupTui } from './ui/startup.js';

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  if (args.length > 1 || (args.length && args[0] !== '--check-startup' && args[0] !== '--help')) {
    throw new Error('未知参数。使用 --help 查看当前入口。');
  }
  if (args[0] === '--help') {
    console.log('NovelPilot\n无参数启动 TUI\n--check-startup  检查当前作品目录、安装资源和目录独占\n创作流程正在实现。');
    return;
  }
  const paths = await resolveStartupPaths();
  if (!args.length && (!process.stdin.isTTY || !process.stdout.isTTY)) {
    throw new Error('TUI 需要交互终端；非交互检查请使用 --check-startup。');
  }
  const lease = await BookLease.acquire(paths.bookDir);
  if (args[0] === '--check-startup') {
    try {
      console.log(JSON.stringify({ bookDir: paths.bookDir, basePromptPath: paths.basePromptPath, status: 'ready' }));
    } finally { await lease.close(); }
    return;
  }
  try {
    const exit = Promise.withResolvers<void>();
    const ui = createStartupTui(paths.bookDir, () => exit.resolve());
    const lifetime = new BookLifetime(new EventQueue<string>(async () => undefined), () => ui.close(), () => lease.close());
    const stop = () => exit.resolve();
    process.on('SIGINT', stop);
    process.on('SIGTERM', stop);
    try { ui.start(); await exit.promise; }
    finally {
      process.off('SIGINT', stop);
      process.off('SIGTERM', stop);
      await lifetime.close();
    }
  } finally { await lease.close(); }
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
