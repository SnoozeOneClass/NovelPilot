import assert from 'node:assert/strict';
import { mkdtemp, readFile, mkdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { deterministicBaselineRunner, packageProvenance, runEvaluation } from '../src/eval/runner.js';
const packageDir = fileURLToPath(new URL('../', import.meta.url));

test('eval uses production Host, isolates repeated books and preserves failures and provenance', async () => {
  const outputDir = await mkdtemp(join(tmpdir(), 'novelpilot-eval-'));
  const report = await runEvaluation({ outputDir, packageDir: fileURLToPath(new URL('../', import.meta.url)), variant: 'baseline-v1', modelConfiguration: 'deterministic:no-provider', repeats: 2,
    runnerFactory: async (_case, repeat) => repeat === 1 ? deterministicBaselineRunner() : async (r) => {
      r.budget.usedTurns++; await r.persistBudget(); return { ...r.identity, status: 'failed', reason: '故障注入', evidence: [] };
    } });
  assert.equal(report.results[0]?.passed, true); assert.equal(report.results[1]?.passed, false);
  assert.notEqual(report.results[0]?.bookDir, report.results[1]?.bookDir);
  assert.equal(report.results[0]?.cost, null); assert.equal(report.results[0]?.turns, 5);
  assert.equal(report.results[0]?.usage, null);
  assert.match(report.sourceFingerprint, /^[a-f0-9]{64}$/);
  assert.equal(JSON.parse(await readFile(report.reportPath, 'utf8')).results.length, 2);
});

test('reported actual usage includes failed attempts and is separate from unavailable price', async () => {
  const outputDir = await mkdtemp(join(tmpdir(), 'novelpilot-eval-usage-'));
  const report = await runEvaluation({ outputDir, packageDir, variant: 'usage', modelConfiguration: 'provider-fixture', runnerFactory: async () => async (r) => {
    r.budget.usedTurns++; await r.persistBudget();
    return { ...r.identity, status: 'failed', reason: 'provider failure with usage', evidence: [], usage: { inputTokens: 21, outputTokens: 3, cacheReadTokens: 4, cacheWriteTokens: 0 } };
  } });
  assert.deepEqual(report.results[0]?.usage, { inputTokens: 21, outputTokens: 3, cacheReadTokens: 4, cacheWriteTokens: 0 });
  assert.equal(report.results[0]?.cost, null);
});

test('installed package uses validated build provenance, but broken source tree does not fall back', async () => {
  const installed = await mkdtemp(join(tmpdir(), 'novelpilot-installed-eval-'));
  await mkdir(join(installed, 'dist'));
  const provenance = await packageProvenance(packageDir);
  await writeFile(join(installed, 'dist/build-info.json'), JSON.stringify(provenance));
  const report = await runEvaluation({ outputDir: join(installed, 'evaluations'), packageDir: installed, variant: 'installed', modelConfiguration: 'deterministic', runnerFactory: async () => deterministicBaselineRunner() });
  assert.equal(report.results[0]?.passed, true);
  assert.equal(report.sourceFingerprint, provenance.sourceFingerprint);
  await writeFile(join(installed, 'dist/build-info.json'), '{"version":1,"sourceFingerprint":"bad"}');
  await assert.rejects(() => packageProvenance(installed), /指纹损坏/);
  await writeFile(join(installed, 'dist/build-info.json'), JSON.stringify(provenance));
  await mkdir(join(installed, 'src'));
  await assert.rejects(() => packageProvenance(installed), /ENOENT/);
});

test('per-case timeout cancels cooperative work and retains already accepted chapters and consumed turns', async () => {
  const outputDir = await mkdtemp(join(tmpdir(), 'novelpilot-eval-timeout-'));
  let settled = false;
  const report = await runEvaluation({ outputDir, packageDir, variant: 'timeout', modelConfiguration: 'deterministic', caseTimeoutMs: 1000,
    runnerFactory: async (_case, _repeat, bookDir) => {
      assert.ok(bookDir.startsWith(outputDir));
      const baseline = deterministicBaselineRunner();
      return async (r) => {
        if (r.instruction.kind !== 'review') return baseline(r);
        r.budget.usedTurns++; await r.persistBudget();
        await new Promise<void>((resolve) => { if (r.signal.aborted) resolve(); else r.signal.addEventListener('abort', () => resolve(), { once: true }); });
        settled = true;
        return { ...r.identity, status: 'cancelled', reason: '已停止', evidence: [] };
      };
    } });
  assert.equal(settled, true); assert.equal(report.results[0]?.status, 'timeout');
  assert.equal(report.results[0]?.chapters, 1); assert.equal(report.results[0]?.turns, 4);
});

test('malformed planning failure retains earlier accepted chapters and observed failed-turn consumption', async () => {
  const outputDir = await mkdtemp(join(tmpdir(), 'novelpilot-eval-corrupt-'));
  const report = await runEvaluation({ outputDir, packageDir, variant: 'corrupt', modelConfiguration: 'deterministic', runnerFactory: async (_case, _repeat, bookDir) => {
    const baseline = deterministicBaselineRunner();
    return async (r) => {
      if (r.instruction.kind !== 'review') return baseline(r);
      r.budget.usedTurns++; await r.persistBudget();
      await writeFile(join(bookDir, 'meta/planning.json'), '{broken');
      throw new Error('planning corruption');
    };
  } });
  assert.equal(report.results[0]?.status, 'failed');
  assert.equal(report.results[0]?.chapters, 1); assert.equal(report.results[0]?.turns, 4);
});

test('global eval turn limit spans different logical authoring tasks', async () => {
  const outputDir = await mkdtemp(join(tmpdir(), 'novelpilot-eval-turns-'));
  const report = await runEvaluation({ outputDir, packageDir, variant: 'limit', modelConfiguration: 'deterministic', maxTotalTurns: 2, runnerFactory: async () => deterministicBaselineRunner() });
  assert.equal(report.results[0]?.status, 'incomplete'); assert.equal(report.results[0]?.turns, 2);
  assert.equal(report.results[0]?.chapters, 0); assert.match(report.results[0]?.reason ?? '', /累计回合/);
});

test('chapter limit rejects surplus writer work after accepting the allowed chapter', async () => {
  const outputDir = await mkdtemp(join(tmpdir(), 'novelpilot-eval-chapters-'));
  const report = await runEvaluation({ outputDir, packageDir, variant: 'chapter-limit', modelConfiguration: 'deterministic', runnerFactory: async () => {
    const baseline = deterministicBaselineRunner();
    return async (r) => {
      if (r.instruction.kind !== 'outline') return baseline(r);
      r.budget.usedTurns++; await r.persistBudget();
      const outline = r.tools.find((t) => t.name === 'save_outline')!;
      await outline.execute('eval-outline', { reason: '尝试增加章节', chapters: [1, 2].map((chapter) => ({ chapter, title: '灯塔', outline: '点灯', volume: 1, arc: 1, arcEnd: chapter === 2, volumeEnd: chapter === 2 })) }, r.signal, undefined, {} as never);
      return { ...r.identity, status: 'completed', reason: 'saved', evidence: r.evidence };
    };
  } });
  assert.equal(report.results[0]?.status, 'incomplete'); assert.equal(report.results[0]?.chapters, 1);
  assert.match(report.results[0]?.reason ?? '', /章节上限/);
});
