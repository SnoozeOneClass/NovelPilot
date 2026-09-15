#!/usr/bin/env node
import { resolveStartupPaths } from './app/paths.js';
import { BookLease } from './store/book-lease.js';
import { BookStore } from './store/book-store.js';
import { BookFiles } from './store/files.js';
import { NovelApplication } from './app/application.js';
import { createNovelTui } from './ui/novel-tui.js';
import { diagnoseBook, renderDiagnostics } from './diagnostics/diagnose.js';
import { runEvalCommand } from './eval/command.js';
import { BookLibrary, type BookSelection } from './app/library.js';
import type { BookStartIntent } from './app/naming.js';
import { pickBook } from './ui/book-picker.js';

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  if (args[0] === '--eval') { await runEvalCommand(args.slice(1)); return; }
  let title: string | undefined;
  const bookOption = args.indexOf('--book');
  if (bookOption >= 0) {
    title = args[bookOption + 1]; if (!title || title.startsWith('--')) throw new Error('--book 需要书名');
    args.splice(bookOption, 2);
  }
  if (args[0] === '--diag' && args.length === 1) {
    if (!title) throw new Error('诊断请指定 --book 书名');
    const paths = await resolveStartupPaths();
    const directory = await new BookLibrary(paths.projectDir).select(title, false);
    console.log(renderDiagnostics(await diagnoseBook(new BookFiles(directory)))); return;
  }
  if (args.length > 1 || (args.length && args[0] !== '--check-startup' && args[0] !== '--help')) {
    throw new Error('未知参数。使用 --help 查看当前入口。');
  }
  if (args[0] === '--help') {
    console.log('NovelPilot\n在项目目录启动，新书直接共创，无需预先命名\n--book 书名  打开已有书或使用预先确定的书名\n--check-startup  检查项目路径和安装资源\n--diag --book 书名  只读诊断\n--eval [输出目录] [--repeat N] [--real]  隔离评测');
    return;
  }
  const paths = await resolveStartupPaths();
  const library = new BookLibrary(paths.projectDir);
  if (!args.length && (!process.stdin.isTTY || !process.stdout.isTTY)) {
    throw new Error('TUI 需要交互终端；非交互检查请使用 --check-startup。');
  }
  if (args[0] === '--check-startup') {
    const directory = title ? await library.select(title) : null;
    const lease = directory ? await BookLease.acquire(directory) : null;
    try {
      console.log(JSON.stringify({ projectDir: paths.projectDir, booksDir: library.booksDir, bookDir: directory, basePromptPath: paths.basePromptPath, status: 'ready' }));
    } finally { await lease?.close(); }
    return;
  }
  let selected: BookSelection | null = title ? { title, directory: await library.select(title) } : await pickBook(library);
  while (selected) {
    const current = selected;
    const store = await BookStore.open(current.directory);
    let startIntent: BookStartIntent | null = null;
    try {
      const exit = Promise.withResolvers<BookStartIntent | null>();
      let application: NovelApplication;
      const ui = createNovelTui({ bookDir: current.directory, onExit: () => exit.resolve(null),
        onCommand: (input) => { void application.command(input).catch((error: unknown) => ui.append('错误', String(error))); } });
      application = new NovelApplication(store, ui, current.title ? { bookTitle: current.title } : { onNameChosen: (intent) => exit.resolve(intent) });
      const stop = () => exit.resolve(null);
      process.on('SIGINT', stop); process.on('SIGTERM', stop);
      try {
        ui.start();
        try { await application.initialize(); } catch (error) { ui.append('启动检查', String(error)); }
        startIntent = await exit.promise;
      } finally {
        process.off('SIGINT', stop); process.off('SIGTERM', stop);
        await application.close();
      }
    } finally { await store.close(); }
    // Close all sessions, queues and old paths first. The managed lease identity stays stable across the move.
    if (!startIntent) break;
    selected = await library.promoteDraft(current.directory, startIntent.title);
  }
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
