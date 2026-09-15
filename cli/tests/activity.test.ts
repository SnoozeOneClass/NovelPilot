import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { defineTool } from '@earendil-works/pi-coding-agent';
import { Type } from '@earendil-works/pi-ai';
import { ActivityTimeline } from '../src/app/activity.js';
import { AppSessions, type AppSessionEvent } from '../src/app/sessions.js';
import { EventQueue } from '../src/app/lifetime.js';
import { ModelConfiguration } from '../src/runtime/pi/model-config.js';
import { createRoleSession } from '../src/runtime/pi/session.js';
import { BookFiles } from '../src/store/files.js';
import { scriptedProvider } from './fixtures/provider.js';

test('tool start/end update one child under its observed task with status and measured duration', () => {
  const timeline = new ActivityTimeline();
  timeline.recordTask({ type: 'task_start', taskId: 'chapter-1', role: 'writer', status: 'running', reason: '撰写第1章', time: new Date(1000).toISOString() });
  timeline.recordTool({ id: 'call-1', name: 'save_draft', role: 'writer', status: 'running', at: 1200 });
  assert.equal(timeline.lines().length, 2);
  assert.match(timeline.lines()[1]!, /save_draft · 进行中/);
  timeline.recordTool({ id: 'call-1', name: 'save_draft', role: 'writer', status: 'completed', at: 2700 });
  timeline.recordTool({ id: 'call-1', name: 'save_draft', role: 'writer', status: 'completed', at: 2700 });
  timeline.recordTask({ type: 'task_end', taskId: 'chapter-1', role: 'writer', status: 'completed', reason: 'saved', time: new Date(3000).toISOString() });
  const lines = timeline.lines();
  assert.equal(lines.length, 2); assert.match(lines[0]!, /写作 · 撰写第1章 · 完成 · 2.0s/);
  assert.doesNotMatch(lines[0]!, /chapter-1/);
  assert.match(lines[1]!, /└─ .*save_draft · 完成 · 1.5s/);
});
test('different roles and repeated tool IDs keep separate actual task parents', () => {
  const timeline = new ActivityTimeline();
  for (const role of ['planner', 'editor'] as const) {
    timeline.recordTask({ type: 'task_start', taskId: `${role}-task`, role, status: 'running', reason: role === 'planner' ? '规划后续章节' : '评审已写章节', time: new Date(1000).toISOString() });
    timeline.recordTool({ id: 'same-id', name: 'read_context', role, status: 'running', at: 1100 });
  }
  timeline.recordTool({ id: 'same-id', name: 'read_context', role: 'editor', status: 'failed', at: 1500 });
  const lines = timeline.lines();
  assert.equal(lines.length, 4);
  assert.match(lines[0]!, /规划 · 规划后续章节/); assert.match(lines[1]!, /进行中/);
  assert.match(lines[2]!, /评审 · 评审已写章节/); assert.match(lines[3]!, /失败 · 0.4s/);
});
test('unknown tool start does not invent duration; history is bounded and duplicate notes collapse', () => {
  const timeline = new ActivityTimeline(5);
  timeline.recordTool({ id: 'orphan', name: 'load_skill', role: 'discussion', status: 'completed', at: 2000 });
  assert.match(timeline.lines()[0]!, /共创 · 工具调用/); assert.doesNotMatch(timeline.lines()[1]!, /\d+\.\d+s/);
  timeline.recordNote('提示', '已暂停'); timeline.recordNote('提示', '已暂停');
  assert.equal(timeline.lines().filter((s) => s.includes('已暂停')).length, 1);
  for (let i = 0; i < 10; i++) timeline.recordNote('提示', `操作 ${i}`);
  assert.ok(timeline.lines().length <= 5); assert.match(timeline.lines().at(-1)!, /操作 9/);
});
test('redispatched logical task gets its own elapsed interval and tool rows', () => {
  const timeline = new ActivityTimeline();
  for (const at of [1000, 4000]) {
    timeline.recordTask({ type: 'task_start', taskId: 'same-task', role: 'writer', status: 'running', reason: '', time: new Date(at).toISOString() });
    timeline.recordTool({ id: 'same-call', name: 'save_draft', role: 'writer', status: 'running', at });
    timeline.recordTool({ id: 'same-call', name: 'save_draft', role: 'writer', status: 'failed', at: at + 500 });
    timeline.recordTask({ type: 'task_end', taskId: 'same-task', role: 'writer', status: 'failed', reason: '', time: new Date(at + 500).toISOString() });
  }
  assert.equal(timeline.lines().length, 4);
  assert.ok(timeline.lines().every((line) => line.endsWith('0.5s')));
});
test('real SDK tool start/end retain call ID and failure status without arguments/results', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-activity-'));
  const events: AppSessionEvent[] = [];
  const queue = new EventQueue<AppSessionEvent>(async (event) => { events.push(event); });
  const provider = await scriptedProvider((index) => index === 0 ? [{ type: 'toolCall', id: 'real-call-1', name: 'failure', arguments: { value: 'private-key' } }] : [{ type: 'text', text: 'done' }]);
  const sessions = new AppSessions(root, new ModelConfiguration(root, join(root, 'global')), new BookFiles(root), queue,
    async (_role, tools) => createRoleSession({ bookDir: root, systemPrompt: 'test', modelRuntime: provider.runtime, model: provider.model, tools }));
  t.after(async () => { await sessions.close(); await queue.close(); await rm(root, { recursive: true, force: true }); });
  const tool = defineTool({ name: 'failure', label: 'Failure', description: 'Test error', parameters: Type.Object({ value: Type.String() }), execute: async () => { throw new Error('sensitive result'); } });
  const session = await sessions.make('writer', [tool]); await session.prompt('run'); await queue.close();
  const tools = events.filter((e) => e.tool);
  assert.equal(tools.length, 2); assert.equal(tools[0]?.tool?.id, 'real-call-1'); assert.equal(tools[1]?.tool?.id, 'real-call-1');
  assert.equal(tools[0]?.tool?.status, 'running'); assert.equal(tools[1]?.tool?.status, 'failed');
  assert.equal(tools[0]?.tool?.role, 'writer'); assert.doesNotMatch(JSON.stringify(tools), /private-key|sensitive result|arguments|args/);
});
