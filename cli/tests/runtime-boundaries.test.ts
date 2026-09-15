import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test, type TestContext } from 'node:test';
import { defineTool, type ToolDefinition } from '@earendil-works/pi-coding-agent';
import { Type } from '@earendil-works/pi-ai';
import { createRoleSession } from '../src/runtime/pi/session.js';
import { runTask } from '../src/runtime/pi/task-runner.js';
import { scriptedProvider } from './fixtures/provider.js';

const identity = { taskId: 'bounded', attemptId: '1' };
const text = [{ type: 'text' as const, text: 'summary keeps facts' }];
async function fixture(t: TestContext, respond: Parameters<typeof scriptedProvider>[0], tools: ToolDefinition[] = [], contextAnchor?: () => string) {
  const provider = await scriptedProvider(respond);
  const bookDir = await mkdtemp(join(tmpdir(), 'novelpilot-boundaries-'));
  const session = await createRoleSession({ bookDir, systemPrompt: 'Novel role.', tools, modelRuntime: provider.runtime, model: provider.model, ...(contextAnchor ? { contextAnchor } : {}) });
  t.after(async () => { await session.abort(); session.dispose(); await rm(bookDir, { recursive: true, force: true }); });
  return { ...provider, session };
}
test('evidence decoder failure at turn end stops immediately as failed without another provider call', async (t) => {
  let broken = false;
  const tool = defineTool({ name: 'persist', label: 'Persist', description: 'Save data', parameters: Type.Object({}),
    execute: async () => { broken = true; return { content: text, details: {} }; } });
  const f = await fixture(t, () => [{ type: 'toolCall', id: '1', name: 'persist', arguments: {} }], [tool]);
  const result = await runTask({ session: f.session, identity, budget: { taskId: identity.taskId, usedTurns: 0, maxTurns: 8 }, prompt: 'Run', signal: new AbortController().signal,
    completedEvidence: () => { if (broken) throw new Error('corrupt private record'); return []; } });
  assert.equal(result.status, 'failed'); assert.deepEqual(result.evidence, []);
  assert.equal(f.requests.length, 1); assert.doesNotMatch(result.reason, /private/);
});
test('initial evidence error returns failed and never calls model', async (t) => {
  const f = await fixture(t, () => text);
  const result = await runTask({ session: f.session, identity, budget: { taskId: identity.taskId, usedTurns: 0, maxTurns: 2 }, prompt: 'Run', signal: new AbortController().signal,
    completedEvidence: () => { throw new Error('invalid'); } });
  assert.equal(result.status, 'failed'); assert.equal(f.requests.length, 0);
});
test('deadline and budget aborts differ from user cancellation and wait for active tools', async (t) => {
  for (const cause of ['user', 'deadline', 'budget'] as const) {
    const controller = new AbortController();
    let settled = false;
    const tool = defineTool({ name: 'slow', label: 'Slow', description: 'wait', parameters: Type.Object({}),
      execute: async () => { controller.abort(); await new Promise<void>((resolve) => setImmediate(resolve)); settled = true; return { content: text, details: {} }; } });
    const f = await fixture(t, () => [{ type: 'toolCall', id: '1', name: 'slow', arguments: {} }], [tool]);
    const result = await runTask({ session: f.session, identity, budget: { taskId: identity.taskId, usedTurns: 0, maxTurns: 4 }, prompt: 'Run', signal: controller.signal, stopCause: cause, completedEvidence: () => [] });
    assert.equal(result.status, cause === 'user' ? 'cancelled' : 'incomplete');
    assert.equal(result.stopCause, cause); assert.equal(settled, true); assert.equal(f.requests.length, 1);
  }
});
test('context anchor follows current skills through SDK next-turn model/tool refresh without duplication', async (t) => {
  let anchor = 'task=current; skill=before';
  const first = defineTool({ name: 'select_skill', label: 'Skill', description: 'Select', parameters: Type.Object({}),
    execute: async () => {
      anchor = 'task=current; skill=selected';
      const next = f.runtime.getModel('novelpilot-test', 'two'); assert.ok(next);
      await f.session.setModel(next);
      f.session.setActiveToolsByName(['read_fact']);
      return { content: text, details: {} };
    } });
  const second = defineTool({ name: 'read_fact', label: 'Fact', description: 'Read', parameters: Type.Object({}), execute: async () => ({ content: text, details: {} }) });
  const f = await fixture(t, (i) => i === 0 ? [{ type: 'toolCall', id: '1', name: 'select_skill', arguments: {} }] : text, [first, second], () => anchor);
  await f.session.prompt('Choose skill');
  assert.match(f.requests[0]?.systemPrompt ?? '', /skill=before/);
  assert.match(f.requests[1]?.systemPrompt ?? '', /skill=selected/);
  assert.doesNotMatch(f.requests[1]?.systemPrompt ?? '', /skill=before/);
  assert.equal((f.requests[1]?.systemPrompt ?? '').match(/task=current/g)?.length, 1);
  assert.deepEqual(f.requests[1]?.tools?.map((x) => x.name), ['read_fact']);
  assert.deepEqual(f.session.messages.filter((x) => x.role === 'assistant').map((x) => x.model), ['one', 'two']);
  anchor = 'task=current; skill=changed-between-prompts';
  await f.session.prompt('Next discussion message');
  assert.match(f.requests[2]?.systemPrompt ?? '', /changed-between-prompts/);
});
test('manual SDK compaction still permits fresh task and skill anchor on the next request', async (t) => {
  let anchor = 'AUTHORITATIVE_TASK_AND_SELECTED_SKILL';
  const f = await fixture(t, () => text, [], () => anchor);
  f.session.settingsManager.setCompactionEnabled(false);
  await f.session.prompt('Old story '.repeat(12000));
  await f.session.prompt('Recent story '.repeat(8000));
  const summary = await f.session.compact('Preserve current story facts');
  assert.ok(summary.summary.includes('summary keeps facts'));
  assert.ok(f.session.sessionManager.getEntries().some((entry) => entry.type === 'compaction'));
  anchor = 'UPDATED_TASK_AND_SELECTED_SKILL';
  const next = f.runtime.getModel('novelpilot-test', 'two'); assert.ok(next);
  await f.session.setModel(next);
  await f.session.prompt('Continue after compaction');
  const last = f.requests.at(-1);
  assert.match(last?.systemPrompt ?? '', /UPDATED_TASK_AND_SELECTED_SKILL/);
  assert.equal((last?.systemPrompt ?? '').match(/UPDATED_TASK_AND_SELECTED_SKILL/g)?.length, 1);
  assert.equal(f.session.messages.filter((x) => x.role === 'assistant').at(-1)?.model, 'two');
});
test('attempt usage sums actual responses including failure and excludes prior session history', async (t) => {
  const f = await fixture(t, (index) => {
    if (index === 2) throw new Error('provider failed after consuming tokens');
    return text;
  });
  await f.session.prompt('Old discussion outside this attempt');
  const result = await runTask({ session: f.session, identity, budget: { taskId: identity.taskId, usedTurns: 0, maxTurns: 4 },
    prompt: 'Task', signal: new AbortController().signal, completedEvidence: () => [] });
  assert.equal(result.status, 'failed');
  // The fixture explicitly reports input=1/output=1 even on its error response.
  assert.deepEqual(result.usage, { inputTokens: 2, outputTokens: 2, cacheReadTokens: 0, cacheWriteTokens: 0 });
  assert.equal(f.requests.length, 3);
  const controller = new AbortController(); controller.abort();
  const cancelled = await runTask({ session: f.session, identity: { ...identity, attemptId: '2' }, budget: { taskId: identity.taskId, usedTurns: 0, maxTurns: 4 },
    prompt: 'Task', signal: controller.signal, completedEvidence: () => [] });
  assert.equal(cancelled.status, 'cancelled'); assert.equal(cancelled.usage, undefined);
});
test('all-zero SDK usage placeholder remains unavailable instead of becoming measured zero', async (t) => {
  const f = await fixture(t, () => text);
  const unsubscribe = f.session.agent.subscribe((event) => {
    if (event.type === 'message_start' && event.message.role === 'assistant') {
      // Scripted provider intentionally emits this same usage object in its final response.
      event.message.usage.input = 0; event.message.usage.output = 0; event.message.usage.totalTokens = 0;
    }
  });
  t.after(unsubscribe);
  const result = await runTask({ session: f.session, identity, budget: { taskId: identity.taskId, usedTurns: 0, maxTurns: 1 },
    prompt: 'Task', signal: new AbortController().signal, completedEvidence: () => [] });
  assert.equal(result.usage, undefined);
});
