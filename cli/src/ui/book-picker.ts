import { ProcessTerminal, SelectList, Text, TuiAltScreen, matchesKey, visibleWidth, truncateToWidth, type Terminal, type TuiMouseEvent } from '@earendil-works/pi-tui';
import { BookLibrary, type BookSelection } from '../app/library.js';
import { createNovelTheme } from './theme.js';

export async function pickBook(library: BookLibrary, terminal: Terminal = new ProcessTerminal()): Promise<BookSelection | null> {
  const titles = await library.list();
  const drafts = await library.listDrafts();
  if (!titles.length && !drafts.length) return library.createDraft();
  if (!titles.length && drafts.length === 1 && drafts[0]!.available) return library.selectDraft(drafts[0]!.directory);
  const tui = new TuiAltScreen(terminal);
  let theme = createNovelTheme();
  let opening = false;
  let message = '';
  let menuRow = 0;
  const menu = new SelectList([
    { value: 'new', label: '＋  开始一本新小说', description: '先讨论人物与设定，书名之后再定' },
    ...titles.map((title) => ({ value: `title:${title}`, label: title, description: '打开并继续创作' })),
    ...drafts.map((draft, index) => ({ value: `draft:${index}`, label: draft.label, description: draft.available ? '继续未命名讨论' : '记录保留；选择后查看原因，或打开其他作品' })),
  ], 9, {
    selectedPrefix: (s) => theme.gold(s), selectedText: (s) => theme.bold(theme.gold(s)),
    description: (s) => theme.muted(s), scrollInfo: (s) => theme.muted(s), noMatch: (s) => theme.muted(s),
  });
  const done = Promise.withResolvers<BookSelection | null>();
  const open = (selection: () => Promise<BookSelection>) => {
    if (opening) return;
    opening = true;
    void selection().then((value) => done.resolve(value), (error: unknown) => {
      opening = false; message = error instanceof Error ? error.message : String(error); tui.requestRender();
    });
  };
  menu.onSelect = (item) => {
    if (item.value === 'new') open(() => library.createDraft());
    else if (item.value.startsWith('title:')) { const title = item.value.slice(6); open(async () => ({ title, directory: await library.select(title, false) })); }
    else { const draft = drafts[Number(item.value.slice(6))]; if (draft) open(() => library.selectDraft(draft.directory)); }
  };
  const back = () => {
    if (opening) return;
    done.resolve(null);
  };
  menu.onCancel = back;
  const panel = {
    focused: true,
    handleInput(data: string) { if (!opening) { menu.handleInput(data); tui.requestRender(); } },
    handleMouse(event: TuiMouseEvent) {
      if (event.y < menuRow || opening) return undefined;
      const local = { ...event, x: Math.max(0, event.x - 2), y: event.y - menuRow, width: Math.max(1, event.width - 4) };
      const result = menu.handleMouse(local);
      if (result) tui.requestRender();
      return result;
    },
    invalidate() { menu.invalidate(); },
    render(width: number): string[] {
      const inside = Math.max(1, width - 4);
      const heading = new Text(theme.bold(theme.gold('NOVELPILOT')) + theme.muted('  /  小说创作工作台'), 0, 0).render(inside);
      const lead = new Text('继续你的故事，或开启新的世界。', 0, 0).render(inside);
      const lines = ['', ...heading, theme.line('─'.repeat(inside)), '', ...lead, '',
        theme.teal('我的书籍'), ''];
      menuRow = lines.length;
      lines.push(...menu.render(inside));
      lines.push('', ...(message ? new Text(theme.error(message)).render(inside) : []),
        theme.line('─'.repeat(inside)),
        ...new Text(theme.muted('↑↓ 选择  ·  Enter 打开  ·  点击选择  ·  Esc 退出')).render(inside),
        ...new Text(theme.muted(library.booksDir)).render(inside), '');
      return lines.map((line) => {
        const clipped = truncateToWidth(line, inside);
        return '  ' + clipped + ' '.repeat(Math.max(0, width - 2 - visibleWidth(clipped)));
      });
    },
  };
  tui.showOverlay(panel, { width: Math.min(82, Math.max(24, terminal.columns - 4)), maxHeight: '95%', anchor: 'center' });
  const untheme = tui.onTerminalColorSchemeChange((scheme) => { theme = createNovelTheme(scheme === 'light'); tui.requestRender(true); });
  tui.setTerminalColorSchemeNotifications(true);
  const cancel = () => { if (!opening) done.resolve(null); };
  const remove = tui.addInputListener((data) => {
    if (matchesKey(data, 'ctrl+c')) { cancel(); return { consume: true }; }
    return undefined;
  });
  process.on('SIGINT', cancel); process.on('SIGTERM', cancel);
  try { tui.start(); return await done.promise; }
  finally {
    process.off('SIGINT', cancel); process.off('SIGTERM', cancel); remove(); untheme();
    await terminal.drainInput(); tui.stop({ preserveScreen: true });
  }
}
