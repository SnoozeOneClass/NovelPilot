import type { AgentSession } from '@earendil-works/pi-coding-agent';

export interface TaskIdentity { taskId: string; attemptId: string }
export interface ArtifactEvidence { taskId: string; attemptId: string; path: string; revision: string }
export interface TaskBudget { taskId: string; maxTurns: number; usedTurns: number }
export type TaskResult = TaskIdentity & {
  status: 'completed' | 'cancelled' | 'failed' | 'incomplete';
  evidence: ArtifactEvidence[];
  reason: string;
};

/** One session per attempt. The caller retains the budget across re-dispatches. */
export async function runTask(options: {
  session: AgentSession;
  identity: TaskIdentity;
  budget: TaskBudget;
  prompt: string;
  signal: AbortSignal;
  completedEvidence: () => ArtifactEvidence[];
}): Promise<TaskResult> {
  const { session, identity, budget, signal } = options;
  if (budget.taskId !== identity.taskId || !Number.isInteger(budget.maxTurns) || budget.maxTurns < 1 ||
    !Number.isInteger(budget.usedTurns) || budget.usedTurns < 0) throw new Error('任务预算无效');
  const evidence = () => options.completedEvidence().filter((item) =>
    item.taskId === identity.taskId && item.attemptId === identity.attemptId);
  const result = (status: TaskResult['status'], reason: string): TaskResult =>
    ({ ...identity, status, reason, evidence: evidence() });
  let aborting: Promise<void> | undefined;
  const abort = () => { aborting ??= session.abort(); void aborting.catch(() => undefined); };
  const previousStop = session.agent.shouldStopAfterTurn;
  session.agent.shouldStopAfterTurn = () => {
    // No IO, throwing decoders or user callbacks in this SDK termination hook.
    return signal.aborted || budget.usedTurns >= budget.maxTurns || hasEvidence;
  };
  let hasEvidence = false;
  const unsubscribe = session.subscribe((event) => {
    if (event.type === 'turn_start') budget.usedTurns += 1;
    if (event.type === 'turn_end') {
      try { hasEvidence = evidence().length > 0; } catch { hasEvidence = false; }
    }
  });
  signal.addEventListener('abort', abort, { once: true });
  try {
    let prompt = options.prompt;
    while (true) {
      if (evidence().length) return result('completed', '本次任务产物已确认');
      if (signal.aborted) return result('cancelled', '用户取消');
      if (budget.usedTurns >= budget.maxTurns) return result('incomplete', '任务预算耗尽');
      const start = session.messages.length;
      await session.prompt(prompt);
      if (evidence().length) return result('completed', '本次任务产物已确认');
      const last = session.messages.slice(start).findLast((message) => message.role === 'assistant');
      if (signal.aborted || last?.stopReason === 'aborted') return result('cancelled', '用户取消');
      if (!last || last.stopReason === 'error') return result('failed', last?.errorMessage ?? '模型没有成功响应');
      if (last.stopReason !== 'stop') return result('incomplete', '模型或运行边界停止');
      prompt = '本次任务要求的产物尚未保存。请使用已授权工具完成剩余工作。';
    }
  } catch (error) {
    if (evidence().length) return result('completed', '产物已保存，后续运行发生错误');
    return result(signal.aborted ? 'cancelled' : 'failed', String(error));
  } finally {
    signal.removeEventListener('abort', abort);
    if (aborting) await aborting.catch(() => undefined);
    await session.agent.waitForIdle();
    unsubscribe();
    if (previousStop) session.agent.shouldStopAfterTurn = previousStop;
    else delete session.agent.shouldStopAfterTurn;
  }
}
