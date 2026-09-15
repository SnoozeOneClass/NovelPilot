import { mkdtemp, mkdir, readdir, readFile, writeFile, stat } from 'node:fs/promises';
import { join } from 'node:path';
import { createHash } from 'node:crypto';
import { BookHost, type RoleRunner, type RoleRun } from '../app/host.js';
import { BookStore } from '../store/book-store.js';
import { missing } from '../store/files.js';
import { decodePlanning, planningPath } from '../domain/planning.js';

export interface EvalCase { id: string; requirements: string; expectedChapters: number }
export const baselineCases: EvalCase[] = [{ id: 'short-complete-v1', requirements: '一章中文短篇：守灯人归乡点亮灯塔，结尾兑现承诺。', expectedChapters: 1 }];
export interface EvalResult { caseId: string; repeat: number; bookDir: string; status: string; reason: string; passed: boolean; checks: { name: string; passed: boolean }[]; chapters: number; turns: number; elapsedMs: number; internalEditorScores: number[]; cost: number | null; usage: { inputTokens: number; outputTokens: number; cacheReadTokens: number; cacheWriteTokens: number } | null; observationError: string | null }
export interface EvalReport { version: 1; scoringVersion: string; variant: string; modelConfiguration: string; sourceFingerprint: string; dependencyFingerprint: string; startedAt: string; cases: EvalCase[]; limits: { caseTimeoutMs: number; maxTotalTurns: number; maxChapters: number | null; maxTasks: number }; results: EvalResult[]; reportPath: string }

export interface BuildInfo { version: 1; sourceFingerprint: string; dependencyFingerprint: string }
export async function packageProvenance(packageDir: string): Promise<BuildInfo> {
  let hasSource = true;
  try { if (!(await stat(join(packageDir, 'src'))).isDirectory()) throw new Error('源码路径不是目录'); }
  catch (error) { if (!missing(error)) throw error; hasSource = false; }
  if (hasSource) return { version: 1, sourceFingerprint: await sourceFingerprint(packageDir), dependencyFingerprint: createHash('sha256').update(await readFile(join(packageDir, 'package-lock.json'))).digest('hex') };
  const info: unknown = JSON.parse(await readFile(join(packageDir, 'dist/build-info.json'), 'utf8'));
  if (!info || typeof info !== 'object' || !('version' in info) || info.version !== 1 || !('sourceFingerprint' in info) || !('dependencyFingerprint' in info) ||
      typeof info.sourceFingerprint !== 'string' || !/^[a-f0-9]{64}$/.test(info.sourceFingerprint) || typeof info.dependencyFingerprint !== 'string' || !/^[a-f0-9]{64}$/.test(info.dependencyFingerprint)) throw new Error('安装包构建指纹损坏');
  return { version: 1, sourceFingerprint: info.sourceFingerprint, dependencyFingerprint: info.dependencyFingerprint };
}

/** Includes actual working-tree source/assets and lockfile bytes, including uncommitted edits. */
export async function sourceFingerprint(packageDir: string): Promise<string> {
  const hash = createHash('sha256');
  const walk = async (relative: string): Promise<void> => {
    for (const entry of (await readdir(join(packageDir, relative), { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name))) {
      const path = `${relative}/${entry.name}`;
      if (entry.isDirectory()) await walk(path);
      else if (entry.isFile()) { hash.update(path); hash.update(await readFile(join(packageDir, path))); }
    }
  };
  await walk('src'); await walk('assets'); hash.update(await readFile(join(packageDir, 'package-lock.json')));
  return hash.digest('hex');
}

export async function runEvaluation(options: {
  outputDir: string; packageDir: string; runnerFactory: (case_: EvalCase, repeat: number, bookDir: string, signal: AbortSignal) => Promise<RoleRunner>;
  variant: string; modelConfiguration: string; cases?: EvalCase[]; repeats?: number; maxTurns?: number;
  caseTimeoutMs?: number; maxTotalTurns?: number; maxChapters?: number; maxTasks?: number;
}): Promise<EvalReport> {
  const cases = options.cases ?? baselineCases;
  const repeats = options.repeats ?? 1;
  if (!Number.isSafeInteger(repeats) || repeats < 1 || !cases.length) throw new Error('评测次数和案例不能为空');
  const limits = { caseTimeoutMs: options.caseTimeoutMs ?? 120_000, maxTotalTurns: options.maxTotalTurns ?? 96, maxChapters: options.maxChapters ?? null, maxTasks: options.maxTasks ?? 100 };
  for (const limit of [limits.caseTimeoutMs, limits.maxTotalTurns, limits.maxChapters ?? 1, limits.maxTasks]) if (!Number.isSafeInteger(limit) || limit < 1) throw new Error('评测运行上限必须为正整数');
  for (const case_ of cases) if (!Number.isSafeInteger(case_.expectedChapters) || case_.expectedChapters < 1 || !case_.requirements.trim()) throw new Error('评测案例的章节数和要求无效');
  const provenance = await packageProvenance(options.packageDir);
  await mkdir(options.outputDir, { recursive: true });
  const runDir = await mkdtemp(join(options.outputDir, 'run-'));
  const report: EvalReport = { version: 1, scoringVersion: 'artifact-correctness-v1', variant: options.variant, modelConfiguration: options.modelConfiguration,
    sourceFingerprint: provenance.sourceFingerprint, dependencyFingerprint: provenance.dependencyFingerprint, startedAt: new Date().toISOString(), cases, limits, results: [], reportPath: join(runDir, 'report.json') };
  for (const case_ of cases) for (let repeat = 1; repeat <= repeats; repeat++) {
    // Generated directory names never contain user case identifiers or point at existing books.
    const bookDir = await mkdtemp(join(runDir, 'book-'));
    const store = await BookStore.open(bookDir);
    const started = performance.now();
    const controller = new AbortController();
    let timeout = false; let stopReason: string | null = null;
    const timer = setTimeout(() => { timeout = true; controller.abort(); }, limits.caseTimeoutMs);
    let host: BookHost | undefined;
    let chapters = 0; let turns = 0; let taskCount = 0; let scores: number[] = [];
    let usage: EvalResult['usage'] = null;
    const collect = async () => {
      // Independent reads retain available evidence even when another artifact is corrupt.
      try { chapters = (await store.snapshot()).records.length; } catch { /* Keep last verified count. */ }
      try {
        const planning = await store.files.json(planningPath, decodePlanning);
        if (planning) {
          turns = Math.max(turns, Object.entries(planning.budgets).filter(([key]) => !key.startsWith('review-cycles:')).reduce((sum, [, n]) => sum + n, 0));
          scores = planning.aggregates.flatMap((a) => a.score === null ? [] : [a.score]);
        }
      } catch { /* Keep already observed usage. */ }
    };
    try {
      const runner = await options.runnerFactory(case_, repeat, bookDir, controller.signal);
      const bounded: RoleRunner = async (r) => {
        await collect(); taskCount++;
        if (turns >= limits.maxTotalTurns) stopReason = '评测累计回合上限已达到';
        if (taskCount > limits.maxTasks) stopReason = '评测任务数量上限已达到';
        if (r.instruction.role === 'writer' && r.instruction.start > (limits.maxChapters ?? case_.expectedChapters)) stopReason = '评测章节上限已达到';
        if (stopReason || controller.signal.aborted) return { ...r.identity, status: 'incomplete', reason: stopReason ?? '评测超时', evidence: [] };
        const before = r.budget.usedTurns;
        r.budget.maxTurns = Math.min(r.budget.maxTurns, before + limits.maxTotalTurns - turns);
        try { return await runner(r); }
        finally { turns += Math.max(0, r.budget.usedTurns - before); await collect(); }
      };
      host = new BookHost(store, bounded, { maxTurns: options.maxTurns ?? 24, onEvent: (event) => {
        if (event.type !== 'task_end' || !event.usage) return;
        usage ??= { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0 };
        for (const key of ['inputTokens', 'outputTokens', 'cacheReadTokens', 'cacheWriteTokens'] as const) usage[key] += event.usage[key];
      } });
      await host.start(case_.requirements);
      const outcome = await host.run(controller.signal);
      clearTimeout(timer);
      const snapshot = await host.snapshot();
      await collect();
      const checks = [
        { name: 'within-run-limits', passed: !timeout && !stopReason },
        { name: 'book-complete', passed: outcome.status === 'complete' && snapshot.progress.phase === 'complete' },
        { name: 'expected-chapter-count', passed: snapshot.records.length === case_.expectedChapters },
        { name: 'no-pending-commit-or-rewrite', passed: snapshot.pending === null && snapshot.progress.rewrites.length === 0 && snapshot.progress.active === null },
        { name: 'accepted-nonempty-text', passed: snapshot.records.every((r) => r.content.trim().length > 0) },
      ];
      report.results.push({ caseId: case_.id, repeat, bookDir, status: timeout ? 'timeout' : outcome.status, reason: timeout ? '评测超时；在途操作已结束' : stopReason ?? outcome.reason, checks, passed: checks.every((c) => c.passed), chapters,
        turns, elapsedMs: performance.now() - started,
        internalEditorScores: scores, cost: null, usage, observationError: host.observationError });
    } catch (error) {
      await collect();
      report.results.push({ caseId: case_.id, repeat, bookDir, status: timeout ? 'timeout' : 'failed', reason: String(error), passed: false, checks: [], chapters, turns, elapsedMs: performance.now() - started, internalEditorScores: scores, cost: null, usage, observationError: host?.observationError ?? null });
    } finally { clearTimeout(timer); await store.close(); }
    // Preserve successful and failed cases after each run; do not select only the best attempt.
    await writeFile(report.reportPath, JSON.stringify(report, null, 2) + '\n', { mode: 0o600 });
  }
  return report;
}

/** Zero-model-cost correctness fixture. This is not evidence of language-model quality. */
export function deterministicBaselineRunner(): RoleRunner {
  const invoke = async (r: RoleRun, name: string, params: unknown) => {
    const tool = r.tools.find((t) => t.name === name);
    if (!tool) throw new Error(`缺少预期工具 ${name}`);
    await tool.execute('eval', params, r.signal, undefined, {} as never);
  };
  return async (r) => {
    r.budget.usedTurns++; await r.persistBudget();
    switch (r.instruction.kind) {
      case 'foundation': await invoke(r, 'save_foundation', { title: '灯塔', premise: '守灯人归乡', characters: '林青：守灯人', world: '海边小城', ending: '点亮灯塔', tier: 'short' }); break;
      case 'outline': await invoke(r, 'save_outline', { reason: '一章短篇', chapters: [{ chapter: 1, title: '归乡', outline: '归乡点灯，兑现承诺', volume: 1, arc: 1, arcEnd: true, volumeEnd: true }] }); break;
      case 'write':
        await invoke(r, 'plan_chapter', { title: '归乡', goal: '兑现点灯承诺', conflict: '久别故乡的愧疚', hook: '灯光照向海面', continuityChecks: ['保持林青守灯人身份'] });
        await invoke(r, 'save_draft', { content: '# 归乡\n林青回到了灯塔。他推开旧木门，点亮了那盏许久未亮的灯。' });
        await invoke(r, 'check_consistency', {});
        await invoke(r, 'commit_chapter', { facts: { title: '归乡', summary: '林青点亮灯塔，兑现承诺。', characters: ['林青'], keyEvents: ['点灯'], timeline: [], stateChanges: [], relationships: [], foreshadows: [] } }); break;
      case 'review': await invoke(r, 'save_review', { content: '结局兑现承诺。', score: 8, issues: [] }); break;
      case 'conclude': await invoke(r, 'complete_book', { reason: '一章完成，承诺兑现。' }); break;
      default: throw new Error(`固定案例未定义 ${r.instruction.kind}`);
    }
    return { ...r.identity, status: 'completed', reason: '固定案例工具完成', evidence: r.evidence };
  };
}
