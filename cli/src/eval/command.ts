import { fileURLToPath } from 'node:url';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { runEvaluation, deterministicBaselineRunner } from './runner.js';
import { AppSessions } from '../app/sessions.js';
import { ModelConfiguration } from '../runtime/pi/model-config.js';
import { BookFiles } from '../store/files.js';
import { EventQueue } from '../app/lifetime.js';
import { createPiRunner } from '../app/host.js';
import { BookLibrary } from '../app/library.js';

export async function runEvalCommand(args: string[]): Promise<void> {
  let outputDir = join(tmpdir(), 'novelpilot-evaluations'); let repeats = 1; let real = false; let pathSeen = false;
  let sourceTitle: string | undefined;
  for (let i = 0; i < args.length; i++) {
    const arg = args[i]!;
    if (arg === '--repeat') { repeats = Number(args[++i]); if (!Number.isSafeInteger(repeats) || repeats < 1) throw new Error('--repeat 需要正整数'); }
    else if (arg === '--real') real = true;
    else if (arg === '--book') { sourceTitle = args[++i]; if (!sourceTitle) throw new Error('--book 需要书名'); }
    else if (!arg.startsWith('-') && !pathSeen) { outputDir = resolve(arg); pathSeen = true; }
    else throw new Error('用法：--eval [输出目录] [--repeat N] [--real]');
  }
  const configDir = sourceTitle ? await new BookLibrary(process.cwd()).select(sourceTitle, false) : process.cwd();
  const config = new ModelConfiguration(configDir);
  const description = real ? JSON.stringify(await Promise.all((['default', 'planner', 'writer', 'editor'] as const).map(async (role) => {
    const effective = await config.resolve(role); return { role, source: effective.source, profile: effective.profile };
  }))) : 'deterministic:no-model';
  const packageDir = fileURLToPath(new URL('../..', import.meta.url));
  const managers: AppSessions[] = [];
  const queues: EventQueue<{ role: string; text: string; delta?: boolean }>[] = [];
  try {
    const report = await runEvaluation({ outputDir, packageDir, repeats, variant: real ? 'real-model-baseline' : 'deterministic-baseline', modelConfiguration: description,
      runnerFactory: async (_case, _repeat, bookDir) => {
        if (!real) return deterministicBaselineRunner();
        const events = new EventQueue<{ role: string; text: string; delta?: boolean }>(async (event) => {
          if (event.role === '用量') await new BookFiles(bookDir).append('meta/usage.jsonl', { ...event, time: new Date().toISOString() });
        }); queues.push(events);
        const sessions = new AppSessions(bookDir, config, new BookFiles(bookDir), events); managers.push(sessions);
        return createPiRunner((role, tools, request) => sessions.make(role, tools, `评测任务：${JSON.stringify(request.instruction)}`));
      } });
    console.log(JSON.stringify({ report: report.reportPath, passed: report.results.filter((r) => r.passed).length, total: report.results.length, modelCalls: real ? '真实模型，按配置计费' : '无' }, null, 2));
    if (report.results.some((r) => !r.passed)) process.exitCode = 1;
  } finally {
    for (const manager of managers) await manager.close();
    for (const queue of queues) await queue.close();
  }
}
