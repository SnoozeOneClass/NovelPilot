import { Editor, ProcessTerminal, Text, TuiAltScreen, matchesKey, type Terminal } from '@earendil-works/pi-tui';

const plain = (text: string) => text;

/** P0 terminal shell. Book workflow commands will be connected through BookHost. */
export function createStartupTui(bookDir: string, onExit: () => void, terminal: Terminal = new ProcessTerminal()) {
  const tui = new TuiAltScreen(terminal);
  const status = new Text(`NovelPilot\n作品目录：${bookDir}\n创作流程正在实现。输入 /quit 或按 Ctrl+C 退出。`, 1, 1);
  const input = new Editor(tui, {
    borderColor: plain,
    selectList: { selectedPrefix: plain, selectedText: plain, description: plain, scrollInfo: plain, noMatch: plain },
  });
  input.onSubmit = (text) => {
    if (text.trim() === '/quit') { onExit(); return; }
    status.setText(`NovelPilot\n作品目录：${bookDir}\n当前只验证终端入口，尚未接入创作流程。输入 /quit 退出。`);
    input.setText('');
    tui.requestRender();
  };
  const remove = tui.addInputListener((data) => {
    if (matchesKey(data, 'ctrl+c')) { onExit(); return { consume: true }; }
    return undefined;
  });
  tui.addChild(status);
  tui.addChild(input);
  tui.setFocus(input);
  return {
    start: () => tui.start(),
    async close() { remove(); tui.stop(); await terminal.drainInput(); },
  };
}
