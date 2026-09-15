import assert from 'node:assert/strict';
import { mkdtemp } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { Discussion, decodeDiscussion, discussionPreview } from '../src/app/discussion.js';
import { createRoleSession } from '../src/runtime/pi/session.js';
import { scriptedProvider } from './fixtures/provider.js';

const xml = (draft = '## 人物\n小明', ready = true) => `<reply>请补充世界观</reply><draft>${draft}</draft><ready>${ready}</ready><suggestions>- 我想写幻想故事\n2. 主角有一个秘密</suggestions>`;

test('discussion requires complete strict protocol and previews no markup', () => {
  assert.equal(decodeDiscussion(xml()).ready, true);
  for (const invalid of [xml().slice(0, -2), xml().replace('<ready>true', '<ready>yes'), 'plain answer', xml() + xml()]) {
    assert.throws(() => decodeDiscussion(invalid));
  }
  assert.equal(discussionPreview('<reply>你好</rep'), '你好');
  assert.equal(discussionPreview('<repl'), '');
});

test('real Pi discussion serializes inputs, retains history/draft, never reuses old success', async (t) => {
  let active = 0;
  let maximum = 0;
  const provider = await scriptedProvider(async (index, context) => {
    maximum = Math.max(maximum, ++active);
    await new Promise<void>((resolve) => setImmediate(resolve));
    active--;
    if (index > 0) assert.ok(context.messages.some((m) => m.role === 'assistant'));
    if (index === 2) throw new Error('provider failure');
    if (index === 3) return [{ type: 'thinking', thinking: xml('wrong') }];
    if (index === 4) return [{ type: 'text', text: '<reply>truncated' }];
    return [{ type: 'text', text: xml(index === 1 ? '' : '## 人物\n小明', false) }];
  });
  const session = await createRoleSession({ bookDir: await mkdtemp(join(tmpdir(), 'discussion-')), systemPrompt: 'discussion',
    tools: [], modelRuntime: provider.runtime, model: provider.model });
  t.after(() => session.dispose());
  const discussion = new Discussion(session);
  assert.throws(() => discussion.buildPrompt());
  await Promise.all([discussion.submit('人物'), discussion.submit('世界观')]);
  assert.equal(maximum, 1);
  assert.equal(discussion.canStart, true);
  assert.equal(discussion.snapshot.ready, false);
  const draft = discussion.buildPrompt();
  for (let i = 0; i < 3; i++) {
    await assert.rejects(discussion.submit('继续'));
    assert.equal(discussion.buildPrompt(), draft);
  }
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(discussion.submit('cancelled', controller.signal));
  assert.equal(provider.requests.length, 5);
  await discussion.submit('恢复讨论');
  assert.equal(discussion.buildPrompt(), draft);
});
