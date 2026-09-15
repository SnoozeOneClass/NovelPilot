import type { AgentSession } from '@earendil-works/pi-coding-agent';

export interface TaskIdentity { taskId: string; attemptId: string }
export interface ArtifactEvidence { taskId: string; attemptId: string; path: string; revision: string }
export interface TaskBudget { taskId: string; maxTurns: number; usedTurns: number }
export type TaskStopCause = 'user' | 'budget' | 'deadline';
export interface TaskUsage {
  inputTokens: number;
  outputTokens: number;
  cacheReadTokens: number;
  cacheWriteTokens: number;
}
export type TaskResult = TaskIdentity & {
  status: 'completed' | 'cancelled' | 'failed' | 'incomplete';
  evidence: ArtifactEvidence[];
  reason: string;
  stopCause?: TaskStopCause;
  /** Available provider-reported usage observed during this attempt; absent means unavailable. */
  usage?: TaskUsage;
};

/** One session per attempt. The caller retains the budget across re-dispatches. */
export async function runTask(options: {
  session: AgentSession;
  identity: TaskIdentity;
  budget: TaskBudget;
  prompt: string;
  signal: AbortSignal;
  completedEvidence: () => ArtifactEvidence[];
  /** Explains this controller's abort; defaults to user cancellation for existing callers. */
  stopCause?: TaskStopCause;
}): Promise<TaskResult> {
  const { session, identity, budget, signal } = options;
  if (budget.taskId !== identity.taskId || !Number.isInteger(budget.maxTurns) || budget.maxTurns < 1 ||
    !Number.isInteger(budget.usedTurns) || budget.usedTurns < 0) throw new Error('任务预算无效');
  let currentEvidence: ArtifactEvidence[] = [];
  let evidenceFailed = false;
  let usage: TaskUsage | undefined;
  const refreshEvidence = () => {
    if (evidenceFailed) return;
    try {
      currentEvidence = options.completedEvidence().filter((item) =>
        item.taskId === identity.taskId && item.attemptId === identity.attemptId);
    } catch { currentEvidence = []; evidenceFailed = true; }
  };
  const result = (status: TaskResult['status'], reason: string): TaskResult =>
    ({ ...identity, status, reason, evidence: [...currentEvidence], ...(usage ? { usage: { ...usage } } : {}) });
  const stopped = (cause: TaskStopCause): TaskResult => ({
    ...result(cause === 'user' ? 'cancelled' : 'incomplete', cause === 'user' ? '用户取消' : cause === 'deadline' ? '任务截止时间已到' : '任务预算耗尽'), stopCause: cause,
  });
  let aborting: Promise<void> | undefined;
  const abort = () => { aborting ??= session.abort(); void aborting.catch(() => undefined); };
  const previousStop = session.agent.shouldStopAfterTurn;
  session.agent.shouldStopAfterTurn = () => {
    // No IO, throwing decoders or user callbacks in this SDK termination hook.
    return signal.aborted || budget.usedTurns >= budget.maxTurns || currentEvidence.length > 0 || evidenceFailed;
  };
  const unsubscribe = session.subscribe((event) => {
    if (event.type === 'turn_start') budget.usedTurns += 1;
    if (event.type === 'message_end' && event.message.role === 'assistant') {
      const reported = event.message.usage;
      const counts = [reported?.input, reported?.output, reported?.cacheRead, reported?.cacheWrite];
      // SDK error placeholders can contain all-zero usage without a provider usage report.
      // Do not turn such absence (or malformed counters) into measured zero consumption.
      if (counts.every((count) => typeof count === 'number' && Number.isSafeInteger(count) && count >= 0) && counts.some((count) => count > 0)) {
        usage ??= { inputTokens: 0, outputTokens: 0, cacheReadTokens: 0, cacheWriteTokens: 0 };
        usage.inputTokens += reported.input;
        usage.outputTokens += reported.output;
        usage.cacheReadTokens += reported.cacheRead;
        usage.cacheWriteTokens += reported.cacheWrite;
      }
    }
    if (event.type === 'turn_end') {
      refreshEvidence();
    }
  });
  signal.addEventListener('abort', abort, { once: true });
  try {
    let prompt = options.prompt;
    while (true) {
      refreshEvidence();
      if (evidenceFailed) return result('failed', '无法验证本次任务产物，请检查存储记录');
      if (currentEvidence.length) return result('completed', '本次任务产物已确认');
      if (signal.aborted) return stopped(options.stopCause ?? 'user');
      if (budget.usedTurns >= budget.maxTurns) return stopped('budget');
      const start = session.messages.length;
      await session.prompt(prompt);
      refreshEvidence();
      if (evidenceFailed) return result('failed', '无法验证本次任务产物，请检查存储记录');
      if (currentEvidence.length) return result('completed', '本次任务产物已确认');
      const last = session.messages.slice(start).findLast((message) => message.role === 'assistant');
      if (signal.aborted || last?.stopReason === 'aborted') return stopped(options.stopCause ?? 'user');
      if (budget.usedTurns >= budget.maxTurns && last?.stopReason !== 'error') return stopped('budget');
      if (!last || last.stopReason === 'error') return result('failed', last?.errorMessage ?? '模型没有成功响应');
      if (last.stopReason !== 'stop') return result('incomplete', '模型或运行边界停止');
      prompt = '本次任务要求的产物尚未保存。请使用已授权工具完成剩余工作。';
    }
  } catch (error) {
    refreshEvidence();
    if (evidenceFailed) return result('failed', '无法验证本次任务产物，请检查存储记录');
    if (currentEvidence.length) return result('completed', '产物已保存，后续运行发生错误');
    if (signal.aborted) return stopped(options.stopCause ?? 'user');
    return result('failed', String(error));
  } finally {
    signal.removeEventListener('abort', abort);
    if (aborting) await aborting.catch(() => undefined);
    await session.agent.waitForIdle();
    unsubscribe();
    if (previousStop) session.agent.shouldStopAfterTurn = previousStop;
    else delete session.agent.shouldStopAfterTurn;
  }
}
