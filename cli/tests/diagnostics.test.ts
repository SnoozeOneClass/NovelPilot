import assert from 'node:assert/strict';
import { mkdtemp, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { contentHash, emptyProgress } from '../src/domain/book.js';
import { BookFiles } from '../src/store/files.js';
import { emptyPlanning } from '../src/domain/planning.js';
import { diagnoseBook, renderDiagnostics, exportDiagnostics } from '../src/diagnostics/diagnose.js';

test('diagnosing an empty directory creates nothing and reports missing evidence', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-diag-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const report = await diagnoseBook(new BookFiles(root));
  assert.ok(report.findings.some((f) => f.code === 'missing_progress'));
  assert.equal(report.completed, null);
  assert.deepEqual(await readdir(root), []);
});
test('corrupt, pending, missing and failed evidence is reported without exposing text or repairing files', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-diag-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const files = new BookFiles(root);
  await files.writeJSON('meta/progress.json', { ...emptyProgress(), completed: [1], phase: 'writing' });
  await files.write('meta/pending_commit.json', '{secret-token:broken');
  await files.write('meta/runs.jsonl', JSON.stringify({ status: 'failed', reason: 'PRIVATE credential and story', prompt: 'hidden prose' }) + '\n');
  const report = await diagnoseBook(files);
  for (const code of ['missing_record', 'missing_chapter', 'corrupt_pending', 'failed_runs', 'missing_planning']) assert.ok(report.findings.some((f) => f.code === code), code);
  const safe = renderDiagnostics(report);
  assert.ok(!safe.includes('PRIVATE') && !safe.includes('secret-token') && !safe.includes('hidden prose'));
  assert.equal(await files.read('meta/pending_commit.json'), '{secret-token:broken');
  assert.ok(!(await readdir(root)).includes('.novelpilot.lock'));
  await exportDiagnostics(report, join(root, 'report.md'));
  await assert.rejects(exportDiagnostics(report, join(root, 'report.md')), { code: 'EEXIST' });
  assert.ok(report.findings.length > 0); // Export failure did not discard the calculated report.
});
test('external edits are identified against accepted hash without rewriting the manuscript', async (t) => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-diag-'));
  t.after(() => rm(root, { recursive: true, force: true }));
  const files = new BookFiles(root);
  await files.writeJSON('meta/progress.json', { ...emptyProgress(), phase: 'writing', completed: [1] });
  await files.writeJSON('meta/chapter_records/000001.json', {
    version: 1, chapter: 1, revision: 1, origin: 'generated', content: 'old prose', contentHash: contentHash('old prose'), style: [], acceptedAt: new Date().toISOString(),
    facts: { title: 'secret-title', summary: 'secret-summary', characters: [], keyEvents: [], timeline: [], stateChanges: [], relationships: [], foreshadows: [] },
  });
  await files.write('chapters/01.md', 'new private prose');
  const report = await diagnoseBook(files);
  assert.ok(report.findings.some((f) => f.code === 'unsynced_chapter' && f.chapter === 1));
  assert.equal(await files.read('chapters/01.md'), 'new private prose');
  assert.ok(!JSON.stringify(report).includes('prose'));
  assert.ok(!renderDiagnostics(report).includes('secret'));
});

test('valid revision checkpoints are not mislabeled corrupt and invalid planning is rejected', async () => {
  const root = await mkdtemp(join(tmpdir(), 'novelpilot-diag-revision-'));
  const files = new BookFiles(root);
  await files.writeJSON('meta/progress.json', { ...emptyProgress(), phase: 'writing' });
  await files.write('meta/checkpoints.jsonl', JSON.stringify({ seq: 1, scope: 'book', step: 'revision_sync', digest: 'revision-test', receipt: null }) + '\n');
  await files.writeJSON('meta/planning.json', {});
  let report = await diagnoseBook(files);
  assert.ok(!report.findings.some((f) => f.code === 'corrupt_checkpoints'));
  assert.ok(report.findings.some((f) => f.code === 'corrupt_planning'));
  await files.writeJSON('meta/planning.json', emptyPlanning());
  report = await diagnoseBook(files);
  assert.ok(!report.findings.some((f) => f.code === 'corrupt_planning'));
});
