import assert from 'node:assert/strict';
import { test } from 'node:test';
import { stripTerminalSequences, visibleWidth, type Terminal } from '@earendil-works/pi-tui';
import { createNovelTui, workbenchWidths } from '../src/ui/novel-tui.js';

class FakeTerminal implements Terminal {
  columns = 80;
  rows = 26;
  kittyProtocolActive = false;
  writes: string[] = [];
  stopped = false;
  drained = false;
  input: (data: string) => void = () => undefined;
  resize: () => void = () => undefined;
  start(input: (data: string) => void, resize: () => void) { this.input = input; this.resize = resize; }
  stop() { this.stopped = true; }
  async drainInput() { this.drained = true; }
  write(data: string) { this.writes.push(data); }
  moveBy(_lines: number) {}
  hideCursor() {}
  showCursor() {}
  clearLine() {}
  clearFromCursor() {}
  clearScreen() {}
  setTitle(_title: string) {}
  setProgress(_active: boolean) {}
}
const paint = () => new Promise((resolve) => setTimeout(resolve, 35));

test('real alternate screen renders Chinese transcript/draft and resizes without losing input', async () => {
  const terminal = new FakeTerminal();
  const commands: string[] = [];
  const ui = createNovelTui({ bookDir: '小说目录', terminal, onCommand: (text) => commands.push(text), onExit: () => undefined });
  try {
    ui.start();
    ui.update({ phase: 'discussion', completedChapters: 2, draft: '## 世界观\n主角生活在海边' });
    ui.append('用户', '讨论人物');
    ui.appendDelta('人物有');
    ui.appendDelta('两个目标');
    await paint();
    const output = terminal.writes.join('');
    assert.ok(output.includes('\x1b[?1049h'));
    assert.ok(output.includes('主角生活在海边'));
    assert.ok(output.includes('人物有两个目标'));
    terminal.input('中文意见');
    terminal.columns = 28;
    terminal.rows = 12;
    terminal.resize();
    await paint();
    terminal.input('\x1b\r');
    terminal.input('第二行');
    terminal.input('\r');
    assert.deepEqual(commands, ['中文意见\n第二行']);
    for (let index = 0; index < 40; index++) ui.append('助手', `历史段落 ${index}`);
    await paint();
    terminal.input('\x1b[5~');
    terminal.input('\x1b[6~');
    await paint();
  } finally { await ui.close(); }
  assert.ok(terminal.writes.join('').includes('\x1b[?1049l'));
  assert.equal(terminal.stopped, true);
  assert.equal(terminal.drained, true);
});

test('keyboard commands reflect phase; help and quit stay responsive', async () => {
  const terminal = new FakeTerminal();
  const commands: string[] = [];
  let exits = 0;
  const ui = createNovelTui({ bookDir: 'book', terminal, onCommand: (text) => commands.push(text), onExit: () => { exits++; } });
  try {
    ui.start();
    terminal.input('\x13');
    ui.update({ phase: 'stage-discussion', completedChapters: 1, draft: '后续方向' });
    terminal.input('\x13');
    ui.update({ phase: 'running', completedChapters: 1, draft: '' });
    terminal.input('\x03');
    terminal.input('/settings'); terminal.input('\r');
    terminal.input('/help'); terminal.input('\r');
    await paint();
    assert.ok(terminal.writes.join('').includes('/sync'));
    ui.update({ phase: 'paused', completedChapters: 1, draft: '' });
    terminal.input('\x03');
    terminal.input('/quit'); terminal.input('\r');
    assert.deepEqual(commands, ['/start', '/apply', '/pause', '/settings']);
    assert.equal(exits, 2);
  } finally { await ui.close(); }
});

test('settings modal masks credentials, keeps them out of commands/history, and cancels', async () => {
  const terminal = new FakeTerminal();
  const commands: string[] = [];
  const ui = createNovelTui({ bookDir: 'book', terminal, onCommand: (text) => commands.push(text), onExit: () => undefined });
  try {
    ui.start();
    const pending = ui.requestInput('输入 API Key', { secret: true });
    terminal.input('test-secret-12345');
    await paint();
    assert.ok(terminal.writes.join('').includes('•'));
    assert.ok(!terminal.writes.join('').includes('test-secret'));
    terminal.input('\r');
    assert.equal(await pending, 'test-secret-12345');
    terminal.input('\x1b[A');
    await paint();
    assert.ok(!terminal.writes.join('').includes('test-secret'));
    assert.deepEqual(commands, []);
    const normal = ui.requestInput('模型名称', { initial: 'model-one' });
    terminal.input('\r');
    assert.equal(await normal, 'model-one');
    const cancelled = ui.requestInput('凭证', { secret: true, initial: 'second-secret' });
    terminal.input('\x1b');
    assert.equal(await cancelled, null);
    assert.ok(!terminal.writes.join('').includes('second-secret'));
    const cancelledByHost = ui.requestInput('关闭前取消设置');
    ui.cancelInput();
    assert.equal(await cancelledByHost, null);
    assert.equal(terminal.stopped, false);
    const closing = ui.requestInput('未完成设置');
    await ui.close();
    assert.equal(await closing, null);
  } finally { await ui.close(); }
});

for (const quitCommand of [false, true]) {
  test(`close restores shell without dumping the UI or painting delayed frames (quit=${quitCommand})`, async () => {
    const terminal = new FakeTerminal();
    let closing: Promise<void> | undefined;
    const ui = createNovelTui({ bookDir: 'shell-restore-book', terminal, onCommand: () => undefined,
      onExit: () => { closing = ui.close(); } });
    ui.start();
    ui.append('助手', 'TRANSCRIPT MUST STAY IN ALTERNATE SCREEN');
    await paint();
    ui.update({ phase: 'paused', completedChapters: 7, draft: 'DRAFT MUST NOT BE PRINTED TO SHELL' });
    ui.appendDelta('PENDING FRAME');
    if (quitCommand) { terminal.input('/quit'); terminal.input('\r'); await closing; }
    else await ui.close();
    // Exercise both queued nextTick renders and delayed timers after shutdown.
    await new Promise<void>((resolve) => process.nextTick(resolve));
    await paint();
    terminal.resize();
    ui.append('助手', 'LATE APPEND');
    ui.update({ phase: 'complete', completedChapters: 8, draft: 'LATE UPDATE' });
    await paint();
    const afterRestore = terminal.writes.join('').split('\x1b[?1049l')[1];
    assert.notEqual(afterRestore, undefined);
    assert.ok(!afterRestore!.includes('NovelPilot'));
    assert.ok(!afterRestore!.includes('TRANSCRIPT'));
    assert.ok(!afterRestore!.includes('DRAFT'));
    assert.ok(!afterRestore!.includes('LATE'));
    assert.ok(!afterRestore!.includes('PENDING'));
    assert.equal(terminal.stopped, true);
    assert.equal(terminal.drained, true);
  });
}

test('wide workbench shows three actual panels, real values, and CJK-safe snapshot widths', async () => {
  const terminal = new FakeTerminal();
  const ui = createNovelTui({ bookDir: 'book', terminal, onCommand: () => undefined, onExit: () => undefined });
  try {
    ui.update({ phase: 'writing', status: '运行中', bookTitle: '凡骨', modelLabel: 'model / 128K', completedChapters: 171,
      plannedChapters: 182, wordCount: 927459, currentChapter: 172, roleLabel: 'WRITER', draft: '## 世界观\n凡人修仙',
      chapters: [{ number: 171, title: '同声', status: 'completed' }, { number: 172, title: '残讯', status: 'writing' }],
      characters: ['沈渊（主角）', '周怀安（导师）'], world: '修仙世界，凡人求道。', activity: '章节已保存，开始下一章',
      usage: { input: 1000, output: 200, cacheRead: 600 } });
    ui.append('用户', '我想继续讨论主角的目标。');
    ui.appendDelta('可以从他必须付出的代价展开。');
    for (const width of [150, 213]) {
      terminal.columns = width; terminal.rows = 48;
      const lines = ui.renderSnapshot(width, 48);
      const plain = lines.map(stripTerminalSequences).join('\n');
      for (const label of ['凡骨', '概览', '运行角色', '上下文', '事件流', '实时输出', '章节', '角色', '世界与设定', '927,459', 'WRITER']) assert.ok(plain.includes(label), label);
      assert.ok(plain.includes('缓存写') && plain.includes('—'));
      assert.ok(!plain.includes('62%'));
      assert.ok(lines.every((line) => visibleWidth(line) <= width));
      const actionBar = stripTerminalSequences(lines.at(-2)!);
      for (const label of ['操作 F2', '暂停', '讨论后续', '导出', '设置', '帮助']) assert.ok(actionBar.includes(label), actionBar);
      const columns = workbenchWidths(width);
      assert.equal(columns.left + columns.center + columns.right + 2, width);
      assert.ok(columns.right >= Math.min(56, Math.floor(width * 0.26)));
    }
    ui.start(); await paint();
    terminal.columns = 95; terminal.resize(); await paint();
    terminal.columns = 55; terminal.rows = 20; terminal.resize(); await paint();
  } finally { await ui.close(); }
});

test('primary action follows phase and overview value columns align by CJK cells', async () => {
  const terminal = new FakeTerminal();
  const ui = createNovelTui({ bookDir: 'book', terminal, onCommand: () => undefined, onExit: () => undefined });
  try {
    for (const [phase, label] of [['discussion', '开始创作'], ['stage-discussion', '应用方向'], ['paused', '继续写作'], ['running', '暂停']]) {
      ui.update({ phase: phase!, completedChapters: 12, wordCount: 345, draft: '' });
      const lines = ui.renderSnapshot(180, 40).map(stripTerminalSequences);
      assert.ok(lines.at(-2)!.includes(label!));
      const completed = lines.find((line) => line.includes('已完成'))!;
      const words = lines.find((line) => line.includes('字数'))!;
      if (completed && words) assert.equal(visibleWidth(completed.slice(0, completed.indexOf('12'))), visibleWidth(words.slice(0, words.indexOf('345'))));
    }
  } finally { await ui.close(); }
});

test('F2 action menu and settings choice select without slash command typing', async () => {
  const terminal = new FakeTerminal();
  const commands: string[] = [];
  const ui = createNovelTui({ bookDir: 'book', terminal, onCommand: (text) => commands.push(text), onExit: () => undefined });
  try {
    ui.start();
    ui.update({ phase: 'running', completedChapters: 1, draft: '' });
    terminal.input('\x1bOQ'); await paint();
    assert.ok(terminal.writes.join('').includes('作品操作'));
    terminal.input('pause'); terminal.input('\r'); await paint();
    assert.deepEqual(commands, ['/pause']);
    const choice = ui.requestChoice('选择模型用途', [{ value: 'default', label: '默认模型' }, { value: 'writer', label: '写作模型' }], 'writer');
    terminal.input('\r');
    assert.equal(await choice, 'writer');
    terminal.input('\x1bOQ'); await paint();
    terminal.input('\x1b');
    await paint();
    assert.deepEqual(commands, ['/pause']);
  } finally { await ui.close(); }
});

test('persistent action bar accepts a real mouse click on its operations button', async () => {
  const terminal = new FakeTerminal();
  terminal.columns = 150;
  const ui = createNovelTui({ bookDir: 'book', terminal, onCommand: () => undefined, onExit: () => undefined });
  try {
    ui.start(); await paint();
    terminal.writes = [];
    terminal.input(`\x1b[<0;3;${terminal.rows - 1}M`);
    terminal.input(`\x1b[<0;3;${terminal.rows - 1}m`);
    await paint();
    assert.ok(terminal.writes.join('').includes('作品操作'));
    terminal.input('\x1b');
  } finally { await ui.close(); }
});

test('terminal scheme reports adapt foreground without imposing background colors', async () => {
  const terminal = new FakeTerminal();
  terminal.columns = 150;
  const ui = createNovelTui({ bookDir: 'book', terminal, onCommand: () => undefined, onExit: () => undefined });
  try {
    ui.start();
    terminal.input('\x1b[?997;2n'); await paint();
    const light = ui.renderSnapshot(150, 35).join('\n');
    assert.ok(light.includes('\x1b[38;2;164;120;24m'));
    assert.ok(!light.includes('\x1b[48;'));
    terminal.input('\x1b[?997;1n'); await paint();
    const dark = ui.renderSnapshot(150, 35).join('\n');
    assert.ok(dark.includes('\x1b[38;2;216;178;100m'));
    assert.ok(!dark.includes('\x1b[48;'));
  } finally { await ui.close(); }
});

test('empty editor hint is visible but never enters input history; unstarted snapshots stay silent', async () => {
  const terminal = new FakeTerminal();
  const commands: string[] = [];
  const ui = createNovelTui({ bookDir: 'book', terminal, onCommand: (text) => commands.push(text), onExit: () => undefined });
  try {
    const empty = ui.renderSnapshot(150, 30).map(stripTerminalSequences).join('\n');
    assert.ok(empty.includes('› 输入讨论或修改意见…'));
    await paint();
    assert.deepEqual(terminal.writes, []);
    ui.start();
    terminal.input('主角想回家');
    const typed = ui.renderSnapshot(150, 30).map(stripTerminalSequences).join('\n');
    assert.ok(!typed.includes('输入讨论或修改意见…'));
    assert.ok(typed.includes('主角想回家'));
    terminal.input('\r');
    assert.deepEqual(commands, ['主角想回家']);
    assert.ok(ui.renderSnapshot(150, 30).map(stripTerminalSequences).join('\n').includes('输入讨论或修改意见…'));
  } finally { await ui.close(); }
  const silent = new FakeTerminal();
  const unused = createNovelTui({ bookDir: 'book', terminal: silent, onCommand: () => undefined, onExit: () => undefined });
  await unused.close();
  assert.deepEqual(silent.writes, []);
});

test('discussion uses two focused content panes and hides monitoring until writing', async () => {
  const terminal = new FakeTerminal(); terminal.columns = 180; terminal.rows = 42;
  const commands: string[] = [];
  const ui = createNovelTui({ bookDir: 'book', terminal, onCommand: (text) => commands.push(text), onExit: () => undefined });
  try {
    ui.start();
    for (const phase of ['discussion', 'stage-discussion']) {
      ui.update({ phase, completedChapters: 3, draft: '完整世界观草稿' });
      const snapshot = ui.renderSnapshot(180, 42).map(stripTerminalSequences).join('\n');
      for (const hidden of ['事件流', '运行角色', '本次用量', '缓存读', '概览']) assert.ok(!snapshot.includes(hidden));
      assert.ok(snapshot.includes('共创对话') && snapshot.includes('完整世界观草稿'));
    }
    terminal.input('\t');
    assert.ok(ui.renderSnapshot(180, 42).map(stripTerminalSequences).join('\n').includes('▸ 当前完整草稿'));
    terminal.input('\x1b[5~'); terminal.input('\x1b[B');
    terminal.input('\t');
    assert.ok(ui.renderSnapshot(180, 42).map(stripTerminalSequences).join('\n').includes('▸ 共创对话'));
    terminal.input('\x1bOQ'); await paint();
    terminal.input('应用'); terminal.input('\r'); await paint();
    assert.deepEqual(commands, ['/apply']);
    ui.update({ phase: 'writing', completedChapters: 3, draft: '完整世界观草稿' });
    assert.ok(ui.renderSnapshot(180, 42).map(stripTerminalSequences).join('\n').includes('事件流'));
    ui.update({ phase: 'discussion', completedChapters: 0, draft: '窄屏草稿' });
    assert.ok(ui.renderSnapshot(55, 30).map(stripTerminalSequences).join('\n').includes('窄屏草稿'));
  } finally { await ui.close(); }
});

test('settings overlay cancellation waits for its in-flight load before closing the terminal', async () => {
  const terminal = new FakeTerminal();
  const ui = createNovelTui({ bookDir: 'book', terminal, onCommand: () => undefined, onExit: () => undefined });
  ui.start();
  const gate = Promise.withResolvers<void>();
  const result = ui.requestModelSettings({ load: async () => { await gate.promise; return { profile: null, source: '未配置', hasApiKey: false }; },
    save: async () => { throw new Error('save must not run'); } });
  let closed = false;
  const closing = ui.close().then(() => { closed = true; });
  await paint();
  assert.equal(closed, false);
  assert.equal(terminal.stopped, false);
  gate.resolve();
  assert.equal(await result, null);
  await closing;
  assert.equal(terminal.stopped, true);
});
