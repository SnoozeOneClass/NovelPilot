import { randomUUID } from 'node:crypto';
import type { AgentSession, ToolDefinition } from '@earendil-works/pi-coding-agent';
import { contentHash, type Identity } from '../domain/book.js';
import { decodeFeedback, decodePlanning, emptyPlanning, planningPath, type Role } from '../domain/planning.js';
import { integer, text } from '../domain/validation.js';
import { BookStore, chapterPath } from '../store/book-store.js';
import { novelTools } from '../tools/novel-tools.js';
import { runTask, type ArtifactEvidence, type TaskBudget, type TaskResult } from '../runtime/pi/task-runner.js';
import { route, type Instruction } from './router.js';

export interface RoleRun { instruction: Instruction; identity: Identity; budget: TaskBudget; tools: ToolDefinition[]; signal: AbortSignal; evidence: ArtifactEvidence[]; persistBudget: () => Promise<void> }
export type RoleRunner = (request: RoleRun) => Promise<TaskResult>;
export interface HostEvent { type: 'task_start' | 'task_end' | 'paused' | 'complete' | 'observation_error'; taskId: string; role: Role | 'host'; status: string; reason: string; time: string; usage?: { inputTokens: number; outputTokens: number; cacheReadTokens: number; cacheWriteTokens: number } }
export interface HostRunResult { status: 'complete' | 'paused' | 'failed' | 'incomplete' | 'discussion'; reason: string }
export function createPiRunner(factory: (role: Role, tools: ToolDefinition[], request: RoleRun) => Promise<AgentSession>): RoleRunner {
  return async (request) => {
    const session = await factory(request.instruction.role, request.tools, request);
    // AgentSession's listener updates runTask's count first. Core Agent awaits this listener
    // before streaming the next provider response; a crash cannot refund a used turn.
    const unsubscribe = session.agent.subscribe(async (event) => { if (event.type === 'turn_start') await request.persistBudget(); });
    try {
      return await runTask({ session, identity: request.identity, budget: request.budget,
        prompt: `当前任务与授权：${JSON.stringify(request.instruction)}\n先读取 novel_context，再完成本任务要求的工具保存。`, signal: request.signal, completedEvidence: () => request.evidence });
    } finally { unsubscribe(); await session.abort(); session.dispose(); }
  };
}

/** The only automatic scheduling loop, shared by TUI and isolated evaluations. */
export class BookHost {
  private controller: AbortController | undefined;
  private running: Promise<HostRunResult> | undefined;
  private boundary: () => Promise<void> = async () => undefined;
  private readonly maxTurns: number;
  observationError: string | null = null;
  constructor(readonly store: BookStore, private readonly runner: RoleRunner, private readonly options: { maxTurns?: number; onEvent?: (event: HostEvent) => void } = {}) {
    this.maxTurns = integer(options.maxTurns ?? 24, 1);
  }
  setBoundaryHandler(handler: () => Promise<void>) { this.boundary = handler; }
  async start(requirements: string) { await this.store.beginCreation(requirements); }
  async snapshot() {
    const snapshot = await this.store.snapshot();
    return { ...snapshot, planning: await this.store.files.json(planningPath, decodePlanning) ?? emptyPlanning(), running: !!this.running };
  }
  pause() { this.controller?.abort(); }
  async addRule(rule: string) {
    text(rule); await this.store.transaction(async (files) => { await files.write('meta/rules.md', `${await files.read('meta/rules.md') ?? ''}\n${rule}\n`); });
  }
  async addPlanningFeedback(feedback: string) {
    text(feedback); await this.store.transaction(async (files) => { const pending = await files.json('meta/planning_feedback.json', decodeFeedback) ?? []; await files.writeJSON('meta/planning_feedback.json', [...pending, feedback]); });
  }
  async requestRewrites(chapters: number[], reason: string) { await this.store.queueRewrites(chapters, reason); }
  async invalidateFrom(chapter: number) {
    integer(chapter, 1); // Revision-bound aggregate keys make stale summaries/reviews unusable automatically.
    await this.addPlanningFeedback(`第 ${chapter} 章及以后存在外部修订，请核对已接纳事实再更新后续规划。`);
  }
  run(signal?: AbortSignal): Promise<HostRunResult> {
    if (this.running) return this.running;
    this.controller = new AbortController();
    const abort = () => this.controller?.abort();
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) abort();
    this.running = this.loop(this.controller.signal).finally(() => { signal?.removeEventListener('abort', abort); this.running = undefined; this.controller = undefined; });
    return this.running;
  }
  private async emit(event: Omit<HostEvent, 'time'>) {
    const full = { ...event, time: new Date().toISOString() };
    try { await this.store.files.append('meta/runs.jsonl', full); } catch (error) {
      this.observationError = `运行记录写入失败，部分证据缺失：${String(error)}`;
      try { this.options.onEvent?.({ ...full, type: 'observation_error', status: 'warning', reason: this.observationError }); } catch { /* Observer only. */ }
    }
    try { this.options.onEvent?.(full); } catch { /* UI is an observer. */ }
  }
  private async loop(signal: AbortSignal): Promise<HostRunResult> {
    try {
      await this.store.recover();
      // Complete-book intent survives a crash between the receipt and progress projection.
      await this.store.transaction(async (files, p) => {
        const state = await files.json(planningPath, decodePlanning);
        if (state?.receipts.some((r) => r.path === 'meta/completion-intent') && p.phase !== 'complete' && !p.rewrites.length && !p.active) await files.writeJSON('meta/progress.json', { ...p, phase: 'complete', revision: p.revision + 1 });
        if (p.active) await files.writeJSON('meta/progress.json', { ...p, active: null, revision: p.revision + 1 });
      });
      while (!signal.aborted) {
        await this.boundary();
        if (signal.aborted) break;
        await this.store.transaction(async (files, p) => {
          const state = await files.json(planningPath, decodePlanning);
          if (state?.receipts.some((r) => r.path === 'meta/completion-intent') && p.phase !== 'complete' && !p.rewrites.length && !p.active) await files.writeJSON('meta/progress.json', { ...p, phase: 'complete', revision: p.revision + 1 });
        });
        let snapshot = await this.snapshot();
        for (const record of snapshot.records) {
          const actual = await this.store.files.read(chapterPath(record.chapter));
          if (actual === null || contentHash(actual) !== record.contentHash) return { status: 'paused', reason: `第 ${record.chapter} 章正文已被外部修改或缺失，请先 /sync` };
        }
        // Reviews carry source revisions. Replaying an issue after its rewrite is already accepted is a no-op.
        const issues = snapshot.planning.aggregates.flatMap((a) => a.issues).filter((i) => snapshot.records.some((r) => r.chapter === i.chapter && r.revision === i.revision));
        if (issues.length && !snapshot.progress.active) {
          for (const issue of issues) await this.store.queueRewrites([issue.chapter], issue.reason);
          snapshot = await this.snapshot();
        }
        const feedback = await this.store.files.json('meta/planning_feedback.json', decodeFeedback) ?? [];
        const instruction = route(snapshot.progress, snapshot.planning, snapshot.records, feedback);
        if (!instruction) {
          const status = snapshot.progress.phase === 'complete' ? 'complete' : 'discussion';
          if (status === 'complete') await this.emit({ type: 'complete', taskId: '', role: 'host', status, reason: '作品已完结' });
          return { status, reason: status === 'complete' ? '作品已完结' : '请先讨论并明确开始创作' };
        }
        const identity = { taskId: instruction.key, attemptId: randomUUID() };
        const budget: TaskBudget = { taskId: identity.taskId, maxTurns: this.maxTurns, usedTurns: snapshot.planning.budgets[identity.taskId] ?? 0 };
        if (budget.usedTurns >= budget.maxTurns) return { status: 'incomplete', reason: '同一逻辑任务累计预算耗尽；请检查失败依据和任务要求' };
        // Persist logical identity now; the Pi runner persists each actual turn before provider IO.
        await this.store.transaction(async (files) => {
          const state = await files.json(planningPath, decodePlanning) ?? emptyPlanning();
          state.active = { ...identity, key: instruction.key }; state.budgets[identity.taskId] = budget.usedTurns;
          await files.writeJSON(planningPath, state);
        });
        if (instruction.role === 'writer') await this.store.beginChapter(identity, instruction.start);
        const evidence: ArtifactEvidence[] = [];
        const tools = novelTools({ store: this.store, instruction, identity, signal, evidence });
        await this.emit({ type: 'task_start', taskId: identity.taskId, role: instruction.role, status: 'running', reason: instruction.reason });
        let result: TaskResult;
        const persistBudget = async () => this.store.transaction(async (files) => {
          const state = await files.json(planningPath, decodePlanning) ?? emptyPlanning();
          state.budgets[identity.taskId] = Math.max(state.budgets[identity.taskId] ?? 0, budget.usedTurns);
          await files.writeJSON(planningPath, state);
        });
        try { result = await this.runner({ instruction, identity, budget, tools, signal, evidence, persistBudget }); }
        catch (error) { result = { ...identity, status: signal.aborted ? 'cancelled' : 'failed', reason: String(error), evidence: [] }; }
        await this.store.recover();
        const saved = instruction.role === 'writer' ? await this.store.completion(identity) : (await this.snapshot()).planning.receipts.filter((r) => r.taskId === identity.taskId && r.attemptId === identity.attemptId);
        if (saved.length) result = { ...result, status: 'completed', reason: '已核对本次任务持久工件' };
        else if (result.status === 'completed') result = { ...result, status: 'incomplete', reason: '模型结束但缺少本次任务保存证据' };
        await this.store.finishTask(identity);
        await this.store.transaction(async (files) => {
          const state = await files.json(planningPath, decodePlanning) ?? emptyPlanning();
          state.active = null; state.budgets[identity.taskId] = Math.min(budget.maxTurns, Math.max(snapshot.planning.budgets[identity.taskId] ?? 0, budget.usedTurns));
          await files.writeJSON(planningPath, state);
          if (instruction.kind === 'revise' && result.status === 'completed') {
            const current = await files.json('meta/planning_feedback.json', decodeFeedback) ?? [];
            await files.writeJSON('meta/planning_feedback.json', current.slice(feedback.length));
          }
        });
        await this.emit({ type: 'task_end', taskId: identity.taskId, role: instruction.role, status: result.status, reason: result.reason, ...(result.usage ? { usage: result.usage } : {}) });
        if (result.status !== 'completed') return { status: result.status === 'cancelled' ? 'paused' : result.status, reason: result.reason };
      }
      await this.emit({ type: 'paused', taskId: '', role: 'host', status: 'paused', reason: '已暂停' });
      return { status: 'paused', reason: '已暂停；已提交章节保留' };
    } catch (error) { return { status: 'failed', reason: String(error) }; }
  }
}
