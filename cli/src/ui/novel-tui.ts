import { CURSOR_MARKER, Editor, HStack, Input, MouseRegion, ProcessTerminal, ScrollView, SelectList, Text, TuiAltScreen, VStack, matchesKey, truncateToWidth, visibleWidth, type Component, type SelectItem, type Terminal } from '@earendil-works/pi-tui';
import { createNovelTheme } from './theme.js';
import { createModelSettingsForm, type ModelFormActions } from './model-settings.js';
import type { EffectiveModel } from '../runtime/pi/model-config.js';

export interface NovelView {
  phase: string;
  status?: string;
  completedChapters: number;
  draft: string;
  activity?: string;
  error?: string;
  bookTitle?: string;
  modelLabel?: string;
  plannedChapters?: number;
  wordCount?: number;
  currentChapter?: number;
  chapters?: { number: number; title: string; status?: string }[];
  characters?: string[];
  world?: string;
  roleLabel?: string;
  events?: string[];
  usage?: { input?: number; output?: number; cacheRead?: number; cacheWrite?: number; contextUsed?: number; contextWindow?: number; cost?: number };
}
export interface NovelTuiOptions {
  bookDir: string;
  onCommand: (text: string) => void;
  onExit: () => void;
  /** Injectable public Terminal for deterministic rendering and keyboard tests. */
  terminal?: Terminal;
  colorScheme?: 'light' | 'dark';
}

export const NOVEL_HELP = `开书：先讨论人物、世界观和设定，再 /start。
写作：/pause 暂停 · /resume 继续；直接输入修改意见。
阶段讨论：/cocreate 或 /plan；/apply 应用方向，/cancel 取消。
手动改稿：/sync --check 检查 · /sync 同步。
导出：/export txt 或 /export epub，可加章节范围，如 1-3。
其他：/diag 诊断 · /settings 模型配置 · /help 帮助 · /quit 退出。
Enter 发送 · Shift+Enter 或 Alt+Enter 换行 · Ctrl+S 开始/应用。
PgUp/PgDn 滚动对话 · Ctrl+End 回到底部；鼠标选择可复制。
Ctrl+C 在运行时请求暂停，其他状态退出。`;

const number = (value: number | undefined) => value === undefined ? '—' : value.toLocaleString('en-US');
const fit = (line: string, width: number) => { const text = truncateToWidth(line, Math.max(0, width)); return text + ' '.repeat(Math.max(0, width - visibleWidth(text))); };
export function workbenchWidths(width: number) {
  const left = width >= 125 ? Math.max(25, Math.min(46, Math.floor(width * 0.22))) : 0;
  const right = width >= 85 ? Math.max(32, Math.min(56, Math.floor(width * 0.26))) : 0;
  return { left, right, center: width - left - right - Number(left > 0) - Number(right > 0) };
}
const eventRows = (bodyRows: number) => Math.max(3, Math.min(16, Math.floor(bodyRows * 0.29)));
// Display untrusted book/model text without allowing terminal control-sequence injection.
const safeText = (text: string) => text.replace(/\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))?/g, '')
  .replace(/[\x00-\x08\x0b-\x1f\x7f-\x9f]/g, '');
const stage = (phase: string) => ['stage-discussion', 'stage_cocreate', 'cocreate', '阶段共创'].includes(phase);
const discussing = (phase: string) => phase === 'discussion' || stage(phase);
export function discussionWidths(width: number) {
  const content = Math.min(154, width);
  const margin = Math.floor((width - content) / 2);
  const right = content >= 72 ? Math.floor(content * 0.43) : 0;
  return { margin, content, right, left: content - right - Number(right > 0) };
}
const running = (phase: string) => ['running', 'writing', 'planning', 'reviewing', 'starting', 'preparing', '写作', '准备创作'].includes(phase);

export function createNovelTui(options: NovelTuiOptions) {
  const background = Number(process.env.COLORFGBG?.split(';').at(-1));
  const theme = createNovelTheme(options.colorScheme === 'light' || (options.colorScheme === undefined && background >= 8));
  const section = (label: string): Component => ({ invalidate() {}, render(width) {
    return [theme.gold(` ${label} `) + theme.line('─'.repeat(Math.max(0, width - visibleWidth(label) - 2)))];
  } });
  const underlyingTerminal = options.terminal ?? new ProcessTerminal();
  let resizeLayout = () => undefined as void;
  let mountLayout = () => undefined as void;
  const terminal: Terminal = new Proxy(underlyingTerminal, { get(target, key) {
    if (key === 'start') return (input: (data: string) => void, resize: () => void) => target.start(input, () => { resizeLayout(); resize(); });
    const value: unknown = Reflect.get(target, key, target);
    return typeof value === 'function' ? value.bind(target) : value;
  } });
  const tui = new TuiAltScreen(terminal);
  let view: NovelView = { phase: 'discussion', completedChapters: 0, draft: '' };
  let closed = false;
  let started = false;
  let cancelModal: (() => void) | undefined;
  let modalSettled: Promise<unknown> | undefined;
  let discussionFocus: 'conversation' | 'draft' = 'conversation';
  const transcript = new VStack();
  const scroll = new ScrollView(transcript, { follow: 'end', primary: true, scrollbar: 'auto' });
  const draft = new Text('', 1, 0);
  const draftScroll = new ScrollView(draft, { scrollbar: 'auto' });
  const footer = new Text('Enter 发送 · Shift/Alt+Enter 换行 · Ctrl+S 开始 · /help 查看命令', 1, 0);
  class NovelInputEditor extends Editor {
    override render(width: number): string[] {
      const lines = super.render(width);
      if (!this.getText() && lines.length >= 3) {
        // A visual-only hint: preserve the public cursor marker for IME placement.
        const hint = theme.gold('› ') + (this.focused ? CURSOR_MARKER : '')
          + theme.muted(truncateToWidth('输入讨论或修改意见…', Math.max(0, width - 2)));
        lines[1] = hint + ' '.repeat(Math.max(0, width - visibleWidth(hint)));
      }
      return lines;
    }
  }
  const editor = new NovelInputEditor(tui, {
    borderColor: theme.line,
    selectList: { selectedPrefix: theme.gold, selectedText: theme.gold, description: theme.muted, scrollInfo: theme.muted, noMatch: theme.muted },
  }, { paddingX: 2 });
  let streamText: Text | undefined;
  let streamContent = '';
  const overview = new Text('', 1, 1);
  const roleInfo = new Text('', 1, 1);
  const usageInfo = new Text('', 1, 1);
  const contextInfo = new Text('', 1, 1);
  const chapterInfo = new Text('', 1, 1);
  const characterInfo = new Text('', 1, 1);
  const worldInfo = new Text('', 1, 1);
  const eventInfo = new Text(theme.muted('等待讨论或创作活动'), 1, 1);
  const eventLines: string[] = [];
  const eventsScroll = new ScrollView(eventInfo, { follow: 'end', scrollbar: 'auto' });
  const leftContent = new VStack([section('概览'), overview, section('运行角色'), roleInfo, section('本次用量'), usageInfo, section('上下文'), contextInfo]);
  const rightContent = new VStack([section('章节'), chapterInfo, section('角色'), characterInfo, section('世界与设定'), worldInfo, section('当前创作要求草稿'), draft]);
  const leftScroll = new ScrollView(leftContent, { scrollbar: 'auto' });
  const rightScroll = new ScrollView(rightContent, { scrollbar: 'auto' });
  const divider: Component = { invalidate() {}, render: () => Array.from({ length: 200 }, () => theme.line('│')) };
  const top: Component = { invalidate() {}, render(width) {
    const modelWidth = Math.floor(width / 3);
    const statusWidth = Math.floor(width / 3);
    const titleWidth = width - modelWidth - statusWidth;
    const title = truncateToWidth(safeText(view.bookTitle || '未命名作品'), titleWidth);
    const padded = ' '.repeat(Math.max(0, Math.floor((titleWidth - visibleWidth(title)) / 2))) + theme.bold(title);
    const status = safeText(view.status ?? view.phase);
    const right = (view.error ? theme.error : running(view.phase) ? theme.green : theme.gold)(`● ${status}`);
    return [fit(theme.muted(safeText(view.modelLabel || '模型未配置')), modelWidth) + fit(padded, titleWidth)
      + ' '.repeat(Math.max(0, statusWidth - visibleWidth(right) - 1)) + right + ' ', theme.line('─'.repeat(width))];
  } };
  const center = new VStack();
  const body = new HStack();
  resizeLayout = () => {
    const widths = workbenchWidths(terminal.columns);
    center.clear();
    center.addChild(section('事件流'), { basis: 1, shrink: 0 });
    center.addChild(eventsScroll, { basis: eventRows(Math.max(4, terminal.rows - 7)), minSize: 2 });
    center.addChild(section('实时输出 · 讨论'), { basis: 1, shrink: 0 });
    center.addChild(scroll, { grow: 1, minSize: 3 });
    body.clear();
    if (widths.left) { body.addChild(leftScroll, { basis: widths.left, shrink: 0 }); body.addChild(divider, { basis: 1, shrink: 0 }); }
    body.addChild(center, { grow: 1, minSize: 1 });
    if (widths.right) { body.addChild(divider, { basis: 1, shrink: 0 }); body.addChild(rightScroll, { basis: widths.right, shrink: 0 }); }
    mountLayout();
  };
  resizeLayout();
  const actions: SelectItem[] = [
    { value: '/start', label: '开始创作', description: '将讨论草稿交给创作流程' },
    { value: '/pause', label: '暂停', description: '保留已保存进度' },
    { value: '/resume', label: '继续创作', description: '从本书进度继续' },
    { value: '/cocreate', label: '讨论后续', description: '暂停并共创下一阶段方向' },
    { value: '/apply', label: '应用讨论', description: '应用后续方向并继续' },
    { value: '/cancel', label: '取消讨论', description: '保持暂停，不应用草稿' },
    { value: '/sync --check', label: '检查手动改稿', description: '只检查正文变化' },
    { value: '/sync', label: '同步手动改稿', description: '接纳外部修改的正文' },
    { value: '/export txt', label: '导出 TXT', description: '导出已完成正文' },
    { value: '/export epub', label: '导出 EPUB', description: '生成电子书' },
    { value: '/settings', label: '模型设置', description: '配置模型与角色覆盖' },
    { value: '/diag', label: '本书诊断', description: '查看问题与建议' },
    { value: '/help', label: '使用帮助', description: '查看操作与快捷键' },
    { value: '/quit', label: '退出作品', description: '保存进度并返回终端' },
  ];
  const selectTheme = { selectedPrefix: theme.gold, selectedText: theme.gold, description: theme.muted, scrollInfo: theme.muted, noMatch: theme.muted };
  function availableActions() {
    const common = ['/settings', '/help', '/quit'];
    const allowed = discussing(view.phase)
      ? [...common, ...(stage(view.phase) ? ['/apply', '/cancel'] : ['/start'])]
      : [...common, '/diag', ...(view.completedChapters > 0 ? ['/export txt', '/export epub'] : []),
        ...(running(view.phase) ? ['/pause'] : ['/sync', '/sync --check']),
        ...(['complete', 'completed'].includes(view.phase) ? [] : ['/cocreate', ...(!running(view.phase) ? ['/resume'] : [])])];
    return actions.filter((item) => allowed.includes(item.value));
  }
  editor.setAutocompleteProvider({ triggerCharacters: ['/'], async getSuggestions(lines, cursorLine, cursorCol) {
    const prefix = (lines[cursorLine] ?? '').slice(0, cursorCol);
    if (!prefix.startsWith('/') || /\s/.test(prefix)) return null;
    const items = availableActions().filter((item) => item.value.startsWith(prefix));
    return items.length ? { items, prefix } : null;
  }, applyCompletion(lines, cursorLine, cursorCol, item, prefix) {
    const next = [...lines]; const line = next[cursorLine] ?? '';
    next[cursorLine] = line.slice(0, cursorCol - prefix.length) + item.value + line.slice(cursorCol);
    return { lines: next, cursorLine, cursorCol: cursorCol - prefix.length + item.value.length };
  } });
  function modalFrame(label: string, content: string[], hint: string, width: number): string[] {
    const inner = Math.max(1, width - 4);
    const row = (line: string) => theme.line('│') + ' ' + fit(line, inner) + ' ' + theme.line('│');
    return [theme.line('╭') + fit(theme.gold(` ${safeText(label)} `), width - 2) + theme.line('╮'), row(''),
      ...content.map(row), row(''), row(theme.muted(hint)), theme.line('╰' + '─'.repeat(Math.max(0, width - 2)) + '╯')];
  }
  function dispatch(command: string) {
    if (command === '/quit') options.onExit();
    else if (command === '/help') append('帮助', NOVEL_HELP);
    else options.onCommand(command);
  }
  function requestChoice(label: string, choices: SelectItem[], initial?: string, searchable = false): Promise<string | null> {
    if (closed) return Promise.resolve(null);
    if (cancelModal) return Promise.reject(new Error('已有输入正在进行'));
    return new Promise((resolve) => {
      const displayChoices = searchable ? choices.map((item) => ({ ...item, label: `${item.label}  ${item.value}` })) : choices;
      const maxItems = Math.min(14, Math.max(4, terminal.rows - 9 - Number(searchable)));
      let list = new SelectList(displayChoices, maxItems, selectTheme);
      const search = new Input({ prompt: '搜索 › ' });
      const index = choices.findIndex((item) => item.value === initial);
      if (index >= 0) list.setSelectedIndex(index);
      const overlay = { invalidate() { list.invalidate(); }, render: (width: number) => modalFrame(label,
        [...(searchable ? search.render(Math.max(1, width - 4)) : []), ...list.render(Math.max(1, width - 4))], '↑ ↓ 选择 · Enter 确认 · Esc 返回', width),
        handleInput(data: string) {
          if (!searchable || (['up', 'down', 'enter', 'escape', 'pageUp', 'pageDown'] as const).some((key) => matchesKey(data, key))) list.handleInput(data);
          else {
            search.handleInput(data);
            const query = search.getValue().toLowerCase();
            list = new SelectList(displayChoices.filter((item) => `${item.value} ${item.label} ${item.description ?? ''}`.toLowerCase().includes(query)), maxItems, selectTheme);
            list.onSelect = (item) => finish(item.value); list.onCancel = () => finish(null);
          }
          tui.requestRender();
        },
        handleMouse(event: Parameters<NonNullable<Component['handleMouse']>>[0]) {
          if (event.y < 2 || event.x < 2) return undefined;
          return list.handleMouse({ ...event, x: event.x - 2, y: event.y - 2 - Number(searchable), width: Math.max(1, event.width - 4) });
        } };
      const handle = tui.showOverlay(overlay, { width: Math.min(84, Math.max(24, terminal.columns - 8)), maxHeight: '90%' });
      const finish = (value: string | null) => { cancelModal = undefined; handle.hide(); resolve(value); tui.requestRender(); };
      cancelModal = () => finish(null);
      list.onSelect = (item) => finish(item.value);
      list.onCancel = () => finish(null);
    });
  }
  function openActions() { void requestChoice('作品操作', availableActions(), undefined, true).then((command) => { if (command && !closed) dispatch(command); }); }
  const primary = () => stage(view.phase) ? { label: '应用方向', command: '/apply' }
    : running(view.phase) ? { label: '暂停', command: '/pause' }
      : view.phase === 'discussion' ? { label: '开始创作', command: '/start' }
        : ['complete', 'completed'].includes(view.phase) ? { label: '导出作品', command: '/export txt' }
          : { label: '继续写作', command: '/resume' };
  const actionButtons: { label: () => string; action: () => void }[] = [
    { label: () => '操作 F2', action: openActions }, { label: () => primary().label, action: () => dispatch(primary().command) },
    { label: () => '讨论后续', action: () => dispatch('/cocreate') },
    { label: () => '导出', action: () => { void requestChoice('导出作品', actions.filter((x) => x.value.startsWith('/export'))).then((v) => { if (v) dispatch(v); }); } },
    { label: () => '设置', action: () => dispatch('/settings') }, { label: () => '帮助', action: () => dispatch('/help') },
  ];
  const buttons = new HStack(actionButtons.map(({ label, action }, index) => ({ component: new MouseRegion({ invalidate() {},
    render: (width: number) => new Text(theme.gold(` ${label()} `), 0, 0).render(width) }, (event) => {
    if (event.type === 'click' && event.button === 'left') { action(); return { handled: true }; }
    return undefined;
  }), basis: 0, grow: 1, minSize: 10, visible: () => index === 2 ? !discussing(view.phase) && !['complete', 'completed'].includes(view.phase)
    : index === 3 ? !discussing(view.phase) && view.completedChapters > 0 : true })), { gap: 1 });
  function renderStatus() {
    const field = (label: string, value: string) => theme.muted(label + ' '.repeat(Math.max(1, 9 - visibleWidth(label)))) + safeText(value);
    overview.setText([
      field('运行态', view.status ?? view.phase), field('已完成', `${number(view.completedChapters)} 章`),
      field('已规划', `${number(view.plannedChapters)} 章`), field('字数', number(view.wordCount)),
      field('当前章', view.currentChapter ? `第 ${view.currentChapter} 章` : '—'),
    ].join('\n'));
    roleInfo.setText(theme.teal(safeText(view.roleLabel || '等待任务')));
    usageInfo.setText([field('输入', number(view.usage?.input)), field('输出', number(view.usage?.output)),
      field('缓存读', number(view.usage?.cacheRead)), field('缓存写', number(view.usage?.cacheWrite)),
      field('费用', view.usage?.cost === undefined ? '—' : `$${view.usage.cost.toFixed(4)}`)].join('\n'));
    contextInfo.setText(field('已用', number(view.usage?.contextUsed)) + '\n' + field('容量', number(view.usage?.contextWindow)));
    const chapters = view.chapters ?? [];
    chapterInfo.setText(chapters.length ? chapters.map((chapter) => {
      const current = chapter.number === view.currentChapter;
      const completed = chapter.status === 'completed' || chapter.number <= view.completedChapters;
      return (current ? theme.gold : completed ? theme.green : theme.muted)(safeText(`${current ? '▸' : completed ? '●' : '○'} ${String(chapter.number).padStart(2)}  ${chapter.title}${current ? ' · 进行中' : ''}`));
    }).join('\n') : theme.muted('讨论完成后生成章节计划'));
    characterInfo.setText(view.characters?.length ? view.characters.map((name) => `· ${safeText(name)}`).join('\n') : theme.muted('人物将在讨论中逐步确定'));
    worldInfo.setText(safeText(view.world || '世界、时代与规则尚待讨论'));
    draft.setText(safeText(view.draft || '从人物、世界观或一个场景聊起。\n准备好后，选择「开始创作」。'));
    if (view.events) eventInfo.setText(view.events.map(safeText).join('\n'));
    else if (view.activity && eventLines.at(-1) !== view.activity) {
      eventLines.push(view.activity); if (eventLines.length > 80) eventLines.shift();
      eventInfo.setText(eventLines.map((line) => `${theme.teal('·')} ${safeText(line)}`).join('\n'));
    }
    footer.setText(view.error ? theme.error(safeText(`错误：${view.error}`))
      : theme.muted(discussing(view.phase) ? `Enter 发送 · Alt/Shift+Enter 换行 · Tab 切换${discussionFocus === 'conversation' ? '草稿' : '对话'} · ↑↓ 滚动 · F2 操作`
        : '输入讨论或修改意见 · Enter 发送 · Alt/Shift+Enter 换行 · Tab/F2 操作 · PgUp/PgDn 滚动'));
    tui.requestRender();
  }
  function append(role: string, text: string) {
    if (closed) return;
    streamText = undefined;
    streamContent = '';
    transcript.addChild(new Text((role === '用户' ? theme.teal : theme.gold)(safeText(role)) + '\n' + safeText(text), 1, 1));
    tui.requestRender();
  }
  editor.onSubmit = (text) => {
    const input = text.trim();
    if (!input || closed) return;
    editor.addToHistory(input);
    editor.setText('');
    if (input === '/quit') { options.onExit(); return; }
    else if (input === '/help') append('帮助', NOVEL_HELP);
    else options.onCommand(input);
    tui.requestRender();
  };
  const removeListener = tui.addInputListener((data) => {
    if (cancelModal) {
      if (matchesKey(data, 'ctrl+c')) { cancelModal(); return { consume: true }; }
      return undefined;
    }
    if (matchesKey(data, 'f2')) { openActions(); return { consume: true }; }
    if (matchesKey(data, 'tab') && !editor.getText().startsWith('/')) {
      if (discussing(view.phase)) { discussionFocus = discussionFocus === 'conversation' ? 'draft' : 'conversation'; mountLayout(); renderStatus(); }
      else openActions();
      return { consume: true };
    }
    if (matchesKey(data, 'ctrl+c')) {
      if (running(view.phase)) options.onCommand('/pause'); else options.onExit();
      return { consume: true };
    }
    if (matchesKey(data, 'ctrl+s')) {
      options.onCommand(stage(view.phase) ? '/apply' : '/start');
      return { consume: true };
    }
    if (matchesKey(data, 'pageUp') || matchesKey(data, 'pageDown')) {
      const target = discussing(view.phase) && discussionFocus === 'draft' ? draftScroll : scroll;
      target.scrollBy((matchesKey(data, 'pageUp') ? -1 : 1) * Math.max(1, target.viewportHeight - 1));
      tui.requestRender();
      return { consume: true };
    }
    if (discussing(view.phase) && (discussionFocus === 'draft' || !editor.getText()) && (matchesKey(data, 'up') || matchesKey(data, 'down'))) {
      (discussionFocus === 'draft' ? draftScroll : scroll).scrollBy(matchesKey(data, 'up') ? -1 : 1);
      tui.requestRender(); return { consume: true };
    }
    if (matchesKey(data, 'ctrl+end')) {
      (discussing(view.phase) && discussionFocus === 'draft' ? draftScroll : scroll).scrollToEnd(); tui.requestRender(); return { consume: true };
    }
    return undefined;
  });
  const conversationTitle: Component = { invalidate() {}, render: (width) => section(`${discussionFocus === 'conversation' ? '▸ ' : ''}共创对话`).render(width) };
  const draftTitle: Component = { invalidate() {}, render: (width) => section(`${discussionFocus === 'draft' ? '▸ ' : ''}当前完整草稿`).render(width) };
  mountLayout = () => {
    if (discussing(view.phase)) {
      const { margin, left, right } = discussionWidths(terminal.columns);
      const conversation = new VStack([{ component: conversationTitle, basis: 1, shrink: 0 }, { component: scroll, grow: 1, minSize: 3 },
        { component: editor, basis: 'auto', minSize: 3, maxSize: 6 }]);
      const draftPanel = new VStack([{ component: draftTitle, basis: 1, shrink: 0 }, { component: draftScroll, grow: 1, minSize: 2 }]);
      const panes = right ? new HStack([{ component: new Text(''), basis: margin, shrink: 0 }, { component: conversation, basis: left, shrink: 0 },
        { component: divider, basis: 1, shrink: 0 }, { component: draftPanel, basis: right, shrink: 0 }])
        : new VStack([{ component: conversation, grow: 1, minSize: 6 }, { component: draftPanel, basis: 5, minSize: 3 }]);
      tui.setLayoutRoot(new VStack([{ component: top, basis: 2, shrink: 0 }, { component: new Text(theme.muted(stage(view.phase) ? '规划后续走向，再继续创作' : '先把人物、世界与故事方向聊清楚，再开始创作'), 1, 1), basis: 3, shrink: 0 },
        { component: panes, grow: 1, minSize: 5 }, { component: buttons, basis: 1, shrink: 0, visible: (v) => v.width >= 70 }, { component: footer, basis: 1, shrink: 0 }]));
      return;
    }
    tui.setLayoutRoot(new VStack([
    { component: top, basis: 2, shrink: 0 },
    { component: body, grow: 1, minSize: 4 },
    { component: draftScroll, basis: 4, minSize: 1, visible: (v) => v.width < 85 && v.height >= 19 },
    { component: editor, basis: 'auto', maxSize: 6, minSize: 3 },
    { component: buttons, basis: 1, shrink: 0, visible: (v) => v.width >= 70 },
    { component: footer, basis: 1, shrink: 0 },
    ]));
  };
  mountLayout();
  tui.setFocus(editor);
  const applyScheme = (scheme: 'dark' | 'light') => {
    if (closed || options.colorScheme) return;
    Object.assign(theme, createNovelTheme(scheme === 'light'));
    editor.borderColor = theme.line;
    renderStatus();
  };
  const removeSchemeListener = tui.onTerminalColorSchemeChange(applyScheme);
  renderStatus();
  return {
    start() { if (!closed && !started) {
      started = true; resizeLayout(); tui.start(); tui.setTerminalColorSchemeNotifications(true);
      if (!options.colorScheme) void tui.queryTerminalColorScheme({ timeoutMs: 150 }).then((scheme) => { if (scheme) applyScheme(scheme); });
    } },
    async close() {
      if (closed) return;
      cancelModal?.();
      await modalSettled;
      closed = true; removeListener(); removeSchemeListener();
      if (started) {
        // Pi's default alt-screen stop prints its document onto the restored shell.
        // Preserve the original shell screen instead; queued SDK renders honor stopped.
        await terminal.drainInput();
        tui.stop({ preserveScreen: true });
      }
    },
    update(next: NovelView) { if (!closed) { const changed = view.phase !== next.phase; view = { ...next }; if (changed) mountLayout(); renderStatus(); } },
    append,
    requestChoice,
    async requestModelSettings(actions: ModelFormActions): Promise<EffectiveModel | null> {
      if (closed) return null;
      if (cancelModal) throw new Error('已有输入正在进行');
      const form = createModelSettingsForm({ actions, theme, requestRender: () => tui.requestRender(), rows: () => terminal.rows });
      const handle = tui.showOverlay(form.component, { width: Math.min(90, terminal.columns - 4), maxHeight: '90%' });
      cancelModal = form.cancel;
      modalSettled = form.result;
      try { return await form.result; }
      finally { handle.hide(); cancelModal = undefined; modalSettled = undefined; tui.requestRender(); }
    },
    renderSnapshot(width: number, height: number): string[] {
      const bodyHeight = Math.max(4, height - 7);
      const panel = (component: Component, w: number, h: number) => {
        const lines = component.render(w);
        return Array.from({ length: h }, (_, index) => fit(lines[index] ?? '', w));
      };
      if (discussing(view.phase)) {
        const widths = discussionWidths(width);
        const h = Math.max(5, height - 7);
        const leftHeight = widths.right ? h : Math.max(4, h - 5);
        const leftLines = [...conversationTitle.render(widths.left), ...panel(transcript, widths.left, Math.max(0, leftHeight - 4)), ...editor.render(widths.left).slice(0, 3)];
        const rightLines = [...draftTitle.render(widths.right || width), ...panel(draft, widths.right || width, widths.right ? h - 1 : 4)];
        const rows = widths.right ? leftLines.map((line, index) => ' '.repeat(widths.margin) + fit(line, widths.left) + theme.line('│') + fit(rightLines[index] ?? '', widths.right)) : [...leftLines, ...rightLines];
        return [...top.render(width), '', theme.muted(stage(view.phase) ? ' 规划后续走向，再继续创作' : ' 先把人物、世界与故事方向聊清楚，再开始创作'), '', ...rows,
          ...buttons.render(width).slice(0, 1), ...footer.render(width).slice(0, 1)].slice(0, height);
      }
      const { left: leftWidth, right: rightWidth, center: centerWidth } = workbenchWidths(width);
      const eventHeight = eventRows(bodyHeight);
      const middle = [...section('事件流').render(centerWidth), ...panel(eventInfo, centerWidth, eventHeight),
        ...section('实时输出 · 讨论').render(centerWidth), ...panel(transcript, centerWidth, Math.max(0, bodyHeight - eventHeight - 2))];
      const left = panel(leftContent, leftWidth || 1, bodyHeight);
      const right = panel(rightContent, rightWidth || 1, bodyHeight);
      return [...top.render(width), ...middle.map((line, index) => (leftWidth ? left[index]! + theme.line('│') : '') + fit(line, centerWidth)
        + (rightWidth ? theme.line('│') + right[index]! : '')), ...editor.render(width).slice(0, 3), ...buttons.render(width).slice(0, 1), ...footer.render(width).slice(0, 1)].slice(0, height);
    },
    cancelInput() { cancelModal?.(); },
    requestInput(label: string, inputOptions: { secret?: boolean; initial?: string } = {}): Promise<string | null> {
      if (closed) return Promise.resolve(null);
      if (cancelModal) return Promise.reject(new Error('已有配置输入正在进行'));
      return new Promise((resolve) => {
        const field = new Input();
        field.setValue(inputOptions.initial ?? '');
        const overlay = {
          get focused() { return field.focused; },
          set focused(value: boolean) { field.focused = value; },
          handleInput(data: string) { field.handleInput(data); tui.requestRender(); },
          invalidate() { field.invalidate(); },
          render(width: number): string[] {
            // Never call Input.render for a secret: no raw credential enters a render buffer.
            const lines = inputOptions.secret
              ? new Text(`> ${'•'.repeat(Math.min([...field.getValue()].length, Math.max(1, width - 8)))}`, 0, 0).render(Math.max(1, width - 4))
              : field.render(Math.max(1, width - 4));
            return modalFrame(label, lines, 'Enter 确认 · Esc 取消', width);
          },
        };
        const handle = tui.showOverlay(overlay, { width: Math.min(84, Math.max(24, terminal.columns - 8)), maxHeight: '80%' });
        const finish = (value: string | null) => {
          field.setValue('');
          delete field.onSubmit;
          delete field.onEscape;
          cancelModal = undefined;
          handle.hide();
          tui.requestRender();
          resolve(value);
        };
        cancelModal = () => finish(null);
        field.onSubmit = finish;
        field.onEscape = () => finish(null);
      });
    },
    appendDelta(text: string) {
      if (closed) return;
      if (!streamText) { streamText = new Text('', 1, 1); transcript.addChild(streamText); }
      streamContent += text;
      streamText.setText(safeText(`助手\n${streamContent}`));
      tui.requestRender();
    },
  };
}
