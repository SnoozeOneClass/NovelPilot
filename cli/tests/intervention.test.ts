import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { decodeIntervention, decideIntervention, type InterventionInput } from '../src/app/intervention.js';
import { createRoleSession } from '../src/runtime/pi/session.js';
import { scriptedProvider } from './fixtures/provider.js';

const input: InterventionInput = { original: '修改第2章，改完继续', basis: { phase: 'writing', completedChapters: 5 } };
const rewrite = { kind: 'rewrite', reply: '安排修订', text: '修订冲突', chapters: [2], resume: true };

test('strict intervention actions cannot smuggle mutations or expand chapter scope', () => {
  assert.throws(() => decodeIntervention(JSON.stringify({ kind: 'query', reply: '进度', chapters: [1] }), input));
  assert.throws(() => decodeIntervention(JSON.stringify({ ...rewrite, chapters: [6] }), input));
  assert.throws(() => decodeIntervention(JSON.stringify({ ...rewrite, chapters: [2, 2] }), input));
  assert.equal(decodeIntervention(JSON.stringify({ ...rewrite, chapters: [1, 2] }), input).kind, 'clarify');
  assert.equal(decodeIntervention(JSON.stringify(rewrite), { ...input, original: '调整一下' }).kind, 'clarify');
  const approved = decodeIntervention(JSON.stringify(rewrite), input);
  assert.equal(approved.kind === 'rewrite' && approved.resume, true);
  for (const original of ['修改第二章', '修改第二章，改完不要继续']) {
    const decision = decodeIntervention(JSON.stringify(rewrite), { ...input, original });
    assert.equal(decision.kind === 'rewrite' && decision.resume, false);
  }
  const range = decodeIntervention(JSON.stringify({ ...rewrite, chapters: [1, 2, 3] }), { ...input, original: '改第一章至第三章' });
  assert.equal(range.kind, 'rewrite');
  assert.throws(() => decodeIntervention('```json\n{}\n```', input));
});

test('completion prevents new planning and rewrite resume; pause target stays a run boundary', () => {
  const completed = { ...input, basis: { ...input.basis, phase: 'complete' } };
  assert.equal(decodeIntervention(JSON.stringify({ kind: 'plan', reply: '新增', text: '下一卷' }), completed).kind, 'unsupported');
  const decision = decodeIntervention(JSON.stringify(rewrite), completed);
  assert.equal(decision.kind === 'rewrite' && decision.resume, false);
  assert.throws(() => decodeIntervention(JSON.stringify({ kind: 'pause', reply: '暂停', stopAfter: 3 }), input));
  assert.deepEqual(decodeIntervention(JSON.stringify({ kind: 'pause', reply: '暂停', stopAfter: 6 }), input),
    { kind: 'pause', reply: '暂停', stopAfter: 6 });
});

test('real Pi arbiter corrects formats with fixed limit and preserves original/basis', async (t) => {
  const provider = await scriptedProvider((index) => [{ type: 'text', text: index === 0 ? 'invalid' : JSON.stringify(rewrite) }]);
  const session = await createRoleSession({ bookDir: await mkdtemp(join(tmpdir(), 'arbiter-')), systemPrompt: 'arbiter',
    tools: [], modelRuntime: provider.runtime, model: provider.model });
  t.after(() => session.dispose());
  const result = await decideIntervention(session, input);
  assert.deepEqual(result.original, input.original);
  assert.deepEqual(result.basis, input.basis);
  assert.equal(result.decision.kind, 'rewrite');
  assert.equal(provider.requests.length, 2);
});

test('provider error is not retried; repeated invalid responses stop at three calls', async (t) => {
  for (const providerError of [true, false]) {
    const provider = await scriptedProvider(() => { if (providerError) throw new Error('provider failed'); return [{ type: 'text', text: 'invalid' }]; });
    const session = await createRoleSession({ bookDir: await mkdtemp(join(tmpdir(), 'arbiter-error-')), systemPrompt: 'arbiter',
      tools: [], modelRuntime: provider.runtime, model: provider.model });
    t.after(() => session.dispose());
    await assert.rejects(decideIntervention(session, input));
    assert.equal(provider.requests.length, providerError ? 1 : 3);
    const controller = new AbortController(); controller.abort();
    await assert.rejects(decideIntervention(session, input, controller.signal));
    assert.equal(provider.requests.length, providerError ? 1 : 3);
  }
});
