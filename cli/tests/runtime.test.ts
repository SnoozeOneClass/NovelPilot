import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test, type TestContext } from 'node:test';
import { defineTool, type ToolDefinition } from '@earendil-works/pi-coding-agent';
import { Type } from '@earendil-works/pi-ai';
import { createRoleSession } from '../src/runtime/pi/session.js';
import { runTask, type ArtifactEvidence, type TaskBudget } from '../src/runtime/pi/task-runner.js';
import { scriptedProvider } from './fixtures/provider.js';

const identity = { taskId: 'chapter-1', attemptId: 'attempt-1' };
const accepted = { ...identity, path: 'chapters/1.md', revision: '1' };
const text = [{ type: 'text' as const, text: 'done' }];
const call = (name: string, id: string) => ({ type: 'toolCall' as const, id, name, arguments: {} });

async function fixture(t: TestContext, respond: Parameters<typeof scriptedProvider>[0], tools: ToolDefinition[] = []) {
  const provider = await scriptedProvider(respond);
  const bookDir = await mkdtemp(join(tmpdir(), 'novelpilot-pi-'));
  const session = await createRoleSession({ bookDir, systemPrompt: 'Test novel role.', tools,
    modelRuntime: provider.runtime, model: provider.model });
  t.after(async () => { await session.abort(); session.dispose(); });
  const budget: TaskBudget = { taskId: identity.taskId, maxTurns: 3, usedTurns: 0 };
  const controller = new AbortController();
  const evidence: ArtifactEvidence[] = [];
  const run = () => runTask({ session, identity, budget, prompt: 'Write assigned chapter',
    signal: controller.signal, completedEvidence: () => evidence });
  return { ...provider, session, budget, controller, evidence, run };
}

test('missing current artifact is bounded; old artifacts and re-dispatch do not reset budget', async (t) => {
  const f = await fixture(t, () => text);
  f.evidence.push({ ...accepted, attemptId: 'old-attempt' });
  assert.equal((await f.run()).status, 'incomplete');
  assert.equal(f.requests.length, 3);
  assert.equal((await f.run()).status, 'incomplete');
  assert.equal(f.requests.length, 3);
});

test('provider error fails once without missing-artifact correction', async (t) => {
  const f = await fixture(t, () => { throw new Error('provider rejected'); });
  const result = await f.run();
  assert.equal(result.status, 'failed');
  assert.equal(f.requests.length, 1);
});

test('current evidence completes task after tool; no additional model call', async (t) => {
  const evidence: ArtifactEvidence[] = [];
  const commit = defineTool({ name: 'commit', label: 'Commit', description: 'Save chapter', parameters: Type.Object({}),
    execute: async () => { evidence.push(accepted); return { content: text, details: {} }; } });
  const f = await fixture(t, () => [call('commit', '1')], [commit]);
  const result = await runTask({ session: f.session, identity, budget: f.budget,
    prompt: 'Write', signal: f.controller.signal, completedEvidence: () => evidence });
  assert.equal(result.status, 'completed');
  assert.equal(f.requests.length, 1);
  assert.deepEqual(f.session.getActiveToolNames(), ['commit']);
});

test('cancellation waits for a cooperative tool; cancelled work is not retried', async (t) => {
  const entered = Promise.withResolvers<void>();
  let settled = false;
  const tool = defineTool({ name: 'slow', label: 'Slow', description: 'Slow read', parameters: Type.Object({}),
    execute: async (_id, _params, signal) => {
      await new Promise<void>((resolve) => {
        signal?.addEventListener('abort', () => resolve(), { once: true }); entered.resolve();
      });
      settled = true;
      return { content: text, details: {} };
    } });
  const f = await fixture(t, () => [call('slow', '1')], [tool]);
  const running = f.run();
  await entered.promise;
  f.controller.abort();
  assert.equal((await running).status, 'cancelled');
  assert.equal(settled, true);
  assert.equal(f.requests.length, 1);
});

test('serial tools never overlap and use no native coding tools', async (t) => {
  let active = 0;
  let maximum = 0;
  const tool = defineTool({ name: 'read_fact', label: 'Fact', description: 'Read book fact', parameters: Type.Object({}),
    execute: async () => {
      maximum = Math.max(maximum, ++active);
      await new Promise<void>((resolve) => setImmediate(resolve));
      active -= 1;
      return { content: text, details: {} };
    } });
  const f = await fixture(t, (index) => index === 0 ? [call('read_fact', '1'), call('read_fact', '2')] : text, [tool]);
  await f.run();
  assert.equal(maximum, 1);
  assert.equal(f.session.agent.toolExecution, 'sequential');
  assert.deepEqual(f.requests[0]?.tools?.map((item) => item.name), ['read_fact']);
});

test('a saved artifact remains completed when cancellation arrives after commit', async (t) => {
  const controller = new AbortController();
  const evidence: ArtifactEvidence[] = [];
  const tool = defineTool({ name: 'commit', label: 'Commit', description: 'Commit then cancel', parameters: Type.Object({}),
    execute: async () => {
      evidence.push(accepted);
      controller.abort();
      return { content: text, details: {} };
    } });
  const f = await fixture(t, () => [call('commit', '1')], [tool]);
  const result = await runTask({ session: f.session, identity, budget: f.budget,
    prompt: 'Write', signal: controller.signal, completedEvidence: () => evidence });
  assert.equal(result.status, 'completed');
  assert.equal(f.requests.length, 1);
});

test('same discussion retains history; fresh role session does not inherit it', async (t) => {
  const f = await fixture(t, () => text);
  await f.session.prompt('Character name: 林舟');
  await f.session.prompt('Continue discussing');
  assert.match(JSON.stringify(f.requests[1]?.messages), /林舟/);
  const fresh = await fixture(t, () => text);
  await fresh.session.prompt('Different task');
  assert.doesNotMatch(JSON.stringify(fresh.requests[0]?.messages), /林舟/);
});

test('changing model during a request leaves in-flight metadata intact and refreshes next turn', async (t) => {
  const entered = Promise.withResolvers<void>();
  const release = Promise.withResolvers<void>();
  const f = await fixture(t, async (index) => {
    if (!index) { entered.resolve(); await release.promise; }
    return text;
  });
  const first = f.session.prompt('First');
  await entered.promise;
  const next = f.runtime.getModel('novelpilot-test', 'two');
  assert.ok(next);
  await f.session.setModel(next);
  release.resolve();
  await first;
  await f.session.prompt('Second');
  const models = f.session.messages.filter((m) => m.role === 'assistant').map((m) => m.model);
  assert.deepEqual(models, ['one', 'two']);
});
