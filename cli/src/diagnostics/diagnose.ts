import { contentHash, decodePending, decodeProgress, decodeRecord } from '../domain/book.js';
import { decodePlanning } from '../domain/planning.js';
import { readCheckpoints } from '../store/checkpoints.js';
import { decodePendingRevision } from '../store/revision.js';
import { chapterPath, recordPath } from '../store/book-store.js';
import { BookFiles } from '../store/files.js';
import { publishOutput } from '../export/exporter.js';

export interface DiagnosticFinding {
  code: string; severity: 'error' | 'warning' | 'info'; category: 'flow' | 'quality' | 'planning' | 'context';
  path: string; chapter?: number; count?: number;
}
export interface DiagnosticReport { version: 1; findings: DiagnosticFinding[]; completed: number | null; inspectedAt: string; consistent: boolean }
const advice: Record<string, string> = {
  missing_progress: '缺少进度，不能确定作品状态。', corrupt_progress: '进度损坏；请检查原文件，不能按空书初始化。',
  missing_record: '缺少已完成章节接纳记录。', corrupt_record: '接纳记录损坏或错位。',
  missing_chapter: '已完成章节正文缺失或为空。', unreadable_chapter: '正文无法安全读取。',
  unsynced_chapter: '正文与接纳记录不同；继续创作前运行 /sync。',
  pending_commit: '存在未完成提交；通过正常启动恢复，诊断未做修复。', corrupt_pending: '恢复记录损坏，需检查原文件。',
  pending_revision: '存在未完成的人工改稿同步。', invalidation: '规划或评审依据需要重新生成。',
  active_task: '存在活动任务记录；如果没有运行中的任务，请通过恢复流程处理。', rewrites: '存在待执行返工。',
  missing_runs: '缺少运行记录，无法判断模型调用失败情况。', corrupt_runs: '运行记录格式损坏，部分运行证据不可用。',
  failed_runs: '运行记录包含失败或未完成任务；检查本地运行记录后修改配置或重试。',
  missing_checkpoints: '已完成章节缺少检查点记录。', corrupt_checkpoints: '检查点 JSONL 损坏；不能静默跳过。',
  missing_planning: '创作阶段缺少规划依据。', corrupt_planning: '规划文件格式损坏。',
  changed_during_check: '检查期间进度发生变化，报告不代表一致快照；暂停后重试。',
  editor_findings: '已有评审记录仍指出当前章节的问题；这是内部质量信号，不是独立文学评分。',
};

/** Read-only: never opens BookStore, creates a lease, initializes files or replays pending operations. */
export async function diagnoseBook(files: BookFiles): Promise<DiagnosticReport> {
  const findings: DiagnosticFinding[] = [];
  const add = (code: string, severity: DiagnosticFinding['severity'], path: string, extra: Partial<Pick<DiagnosticFinding, 'chapter' | 'count'>> = {}, category: DiagnosticFinding['category'] = 'flow') => findings.push({ code, severity, category, path, ...extra });
  let before: string | null = null;
  let progress: ReturnType<typeof decodeProgress> | null = null;
  try {
    before = await files.read('meta/progress.json');
    if (before === null) add('missing_progress', 'error', 'meta/progress.json');
    else progress = decodeProgress(JSON.parse(before));
  } catch { add('corrupt_progress', 'error', 'meta/progress.json'); }
  for (const chapter of progress?.completed ?? []) {
    const path = recordPath(chapter);
    let record: ReturnType<typeof decodeRecord> | null = null;
    try {
      record = await files.json(path, decodeRecord);
      if (!record) add('missing_record', 'error', path, { chapter });
      else if (record.chapter !== chapter) { record = null; add('corrupt_record', 'error', path, { chapter }); }
    } catch { add('corrupt_record', 'error', path, { chapter }); }
    try {
      const body = await files.read(chapterPath(chapter));
      if (!body?.trim()) add('missing_chapter', 'error', chapterPath(chapter), { chapter });
      else if (record && contentHash(body) !== record.contentHash) add('unsynced_chapter', 'warning', chapterPath(chapter), { chapter }, 'context');
    } catch { add('unreadable_chapter', 'error', chapterPath(chapter), { chapter }); }
  }
  try { if (await files.json('meta/pending_commit.json', decodePending)) add('pending_commit', 'warning', 'meta/pending_commit.json'); }
  catch { add('corrupt_pending', 'error', 'meta/pending_commit.json'); }
  for (const [path, code] of [['meta/pending_revision.json', 'pending_revision'], ['meta/invalidation.json', 'invalidation']] as const) {
    try {
      const raw = await files.read(path);
      if (raw !== null) {
        const value: unknown = JSON.parse(raw);
        if (code === 'pending_revision') decodePendingRevision(value);
        if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid pending record');
        add(code, 'warning', path, {}, code === 'invalidation' ? 'planning' : 'flow');
      }
    }
    catch { add('corrupt_pending', 'error', path); }
  }
  if (progress?.active) add('active_task', 'info', 'meta/progress.json', { chapter: progress.active.chapter });
  if (progress?.rewrites.length) add('rewrites', 'info', 'meta/progress.json', { count: progress.rewrites.length });
  for (const [path, code] of [['meta/checkpoints.jsonl', 'checkpoints'], ['meta/runs.jsonl', 'runs']] as const) {
    try {
      const raw = await files.read(path);
      if (!raw?.trim()) {
        if (code === 'runs' || progress?.completed.length) add(`missing_${code}`, 'info', path);
        continue;
      }
      let failures = 0;
      if (code === 'checkpoints') { await readCheckpoints(files); continue; }
      for (const line of raw.trimEnd().split('\n')) {
        const value: unknown = JSON.parse(line);
        if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid row');
        if ('status' in value && ['failed', 'incomplete'].includes(String(value.status))) failures++;
      }
      if (code === 'runs' && failures) add('failed_runs', 'warning', path, { count: failures });
    } catch { add(`corrupt_${code}`, 'error', path); }
  }
  if (progress && progress.phase !== 'discussion') {
    try {
      const raw = await files.read('meta/planning.json');
      if (raw === null) add('missing_planning', 'warning', 'meta/planning.json', {}, 'planning');
      else {
        const planning = decodePlanning(JSON.parse(raw));
        let currentIssues = 0;
        for (const issue of planning.aggregates.flatMap((entry) => entry.issues)) {
          const record = await files.json(recordPath(issue.chapter), decodeRecord);
          if (record?.revision === issue.revision) currentIssues += 1;
        }
        if (currentIssues) add('editor_findings', 'warning', 'meta/planning.json', { count: currentIssues }, 'quality');
      }
    } catch { add('corrupt_planning', 'error', 'meta/planning.json', {}, 'planning'); }
  }
  let consistent = true;
  try { consistent = before === await files.read('meta/progress.json'); } catch { consistent = false; }
  if (!consistent) add('changed_during_check', 'warning', 'meta/progress.json');
  return { version: 1, findings, completed: progress?.completed.length ?? null, inspectedAt: new Date().toISOString(), consistent };
}

/** Allowlisted projection: never include title, prose, prompts, exception text, model outputs or credentials. */
export function renderDiagnostics(report: DiagnosticReport): string {
  const lines = ['# NovelPilot 诊断', '', `已完成章节数：${report.completed ?? '未知'}`, ''];
  for (const finding of report.findings) {
    const description = advice[finding.code];
    if (!description) continue;
    // Evidence paths are reconstructed from rule codes; callers cannot smuggle free text into an export.
    lines.push(`- ${description}${Number.isSafeInteger(finding.chapter) && finding.chapter! > 0 ? ` 章节：${finding.chapter}。` : ''}${Number.isSafeInteger(finding.count) && finding.count! >= 0 ? ` 数量：${finding.count}。` : ''}`);
  }
  if (!report.findings.length) lines.push('当前已实现检查未发现问题；不代表文学质量合格。');
  lines.push('', '本报告只含固定规则描述和数值；没有正文、提示词、模型输出或凭证。');
  return `${lines.join('\n')}\n`;
}
export async function exportDiagnostics(report: DiagnosticReport, output: string, overwrite = false): Promise<void> {
  await publishOutput(output, Buffer.from(renderDiagnostics(report)), overwrite);
}
