import assert from 'node:assert/strict';
import { mkdtemp, mkdir, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { SkillCatalog, type SkillLoad } from '../src/skills/catalog.js';
import { createRoleSession } from '../src/runtime/pi/session.js';
import { scriptedProvider } from './fixtures/provider.js';

test('catalog is metadata only; model can choose a skill with no native tools', async (t) => {
  const events: SkillLoad[] = [];
  const catalog = await SkillCatalog.discover(fileURLToPath(new URL('../assets/skills/', import.meta.url)), (e) => events.push(e));
  assert.ok(!catalog.prompt().includes('害怕付出的代价'));
  const provider = await scriptedProvider((index, context) => {
    if (index === 0) return [{ type: 'toolCall', id: 'skill', name: 'load_skill', arguments: { name: 'character-conflict' } }];
    assert.ok(context.messages.some((m) => m.role === 'toolResult'));
    return [{ type: 'text', text: 'loaded' }];
  });
  const session = await createRoleSession({ bookDir: await mkdtemp(join(tmpdir(), 'skill-session-')), systemPrompt: catalog.prompt(),
    tools: catalog.tools(), modelRuntime: provider.runtime, model: provider.model });
  t.after(() => session.dispose());
  await session.prompt('讨论人物');
  assert.deepEqual(session.getActiveToolNames(), ['load_skill', 'read_skill_resource']);
  assert.deepEqual(events, [{ skill: 'character-conflict', resource: 'SKILL.md' }]);
  assert.equal(catalog.loadedResources().length, 1);
  assert.ok(catalog.loadedResources()[0]?.content.includes('害怕付出的代价'));
  await catalog.load('character-conflict', 'references/questions.md');
  assert.equal(catalog.loadedResources().length, 2);
});

test('skill paths reject traversal, outside symlink, scripts and unknown resources', async () => {
  const base = await mkdtemp(join(tmpdir(), 'skill-paths-'));
  const skills = join(base, 'skills');
  const root = join(skills, 'test');
  const outside = join(base, 'outside');
  await mkdir(root, { recursive: true });
  await mkdir(outside);
  await writeFile(join(root, 'SKILL.md'), '---\nname: test\ndescription: Test skill\n---\nmethod');
  await writeFile(join(root, 'notes.txt'), 'allowed');
  await writeFile(join(outside, 'secret.txt'), 'secret');
  await symlink(outside, join(root, 'escape'), process.platform === 'win32' ? 'junction' : 'dir');
  const catalog = await SkillCatalog.discover(skills);
  assert.equal(await catalog.load('test', 'notes.txt'), 'allowed');
  for (const resource of ['../outside/secret.txt', 'escape/secret.txt', 'C:/secret.txt', 'a\\secret.txt', 'run.js', 'scripts/data.md', 'missing.md']) {
    await assert.rejects(catalog.load('test', resource));
  }
  await assert.rejects(catalog.load('unknown'));
  await symlink(outside, join(skills, 'outside'), process.platform === 'win32' ? 'junction' : 'dir');
  await assert.rejects(SkillCatalog.discover(skills), /越界/);
});
