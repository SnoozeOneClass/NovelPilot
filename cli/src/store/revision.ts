import { contentHash, decodeFacts, decodeProgress, decodeRecord, digest, normalizeContent, projectRecords, type ChapterFacts, type ChapterRecord, type Progress } from '../domain/book.js';
import { DataError, choice, list, object, strings, version } from '../domain/validation.js';
import { BookStore, chapterPath, recordPath } from './book-store.js';
import type { BookFiles } from './files.js';
import { appendCheckpoint } from './checkpoints.js';

export interface ChangedChapter { chapter: number; base: ChapterRecord; content: string; hash: string }
export interface RevisionAnalysis { facts: ChapterFacts; style: string[]; feedback: string[] }
export const decodeAnalysis = (value: unknown): RevisionAnalysis => {
  const v = object(value); return { facts: decodeFacts(v.facts), style: strings(v.style), feedback: strings(v.feedback) };
};
interface PendingRevision {
  version: 1; stage: 'prepared' | 'records_applied' | 'projections_applied';
  items: { base: ChapterRecord; record: ChapterRecord }[];
  before: Progress; after: Progress; feedbackBefore: string[]; feedbackAfter: string[];
}
const pendingPath = 'meta/pending_revision.json';
export const decodePendingRevision = (value: unknown): PendingRevision => {
  const v = object(value);
  const p: PendingRevision = { version: version(v.version), stage: choice(v.stage, ['prepared', 'records_applied', 'projections_applied']),
    items: list(v.items, (item) => { const x = object(item); return { base: decodeRecord(x.base), record: decodeRecord(x.record) }; }),
    before: decodeProgress(v.before), after: decodeProgress(v.after), feedbackBefore: strings(v.feedbackBefore), feedbackAfter: strings(v.feedbackAfter) };
  if (!p.items.length || new Set(p.items.map((x) => x.record.chapter)).size !== p.items.length || p.before.active ||
      JSON.stringify({ ...p.before, revision: p.before.revision + 1 }) !== JSON.stringify(p.after) ||
      p.items.some((x) => x.base.chapter !== x.record.chapter || x.record.revision !== x.base.revision + 1 || x.record.origin !== 'user' || !p.before.completed.includes(x.record.chapter))) throw new DataError('同步恢复记录不一致');
  return p;
};
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
class StaleRevisionText extends DataError {}
export class RevisionService {
  private running = false;
  constructor(private readonly store: BookStore, private readonly fault: (stage: string) => Promise<void> = async () => undefined) {}
  async check(): Promise<ChangedChapter[]> {
    const { records } = await this.store.snapshot();
    const changed: ChangedChapter[] = [];
    for (const base of records) {
      const content = await this.store.files.read(chapterPath(base.chapter));
      if (content === null) throw new DataError(`已完成第 ${base.chapter} 章正文缺失`);
      const hash = contentHash(content);
      if (hash !== base.contentHash) changed.push({ chapter: base.chapter, base, content: normalizeContent(content), hash });
    }
    return changed;
  }
  async sync(analyze: (change: ChangedChapter, signal: AbortSignal) => Promise<RevisionAnalysis>, signal: AbortSignal): Promise<number[]> {
    if (this.running) throw new DataError('同步正在进行');
    this.running = true;
    try {
      const pending = await this.store.files.json(pendingPath, decodePendingRevision);
      if (pending) return await this.apply();
      const before = (await this.store.snapshot()).progress;
      if (before.active) throw new DataError('请先暂停并等待当前任务结束');
      const changed = await this.check();
      if (!changed.length) return [];
      const items: PendingRevision['items'] = [];
      const feedback: string[] = [];
      for (const change of changed) {
        signal.throwIfAborted();
        const analysis = decodeAnalysis(await analyze(change, signal));
        const ownPlants = change.base.facts.foreshadows.filter((x) => x.action === 'plant' && !analysis.facts.foreshadows.some((y) => y.id === x.id && y.action === 'plant'));
        analysis.facts.foreshadows.unshift(...ownPlants);
        items.push({ base: change.base, record: { ...change.base, origin: 'user', revision: change.base.revision + 1,
          content: change.content, contentHash: change.hash, facts: analysis.facts, style: [...new Set([...change.base.style, ...analysis.style])], acceptedAt: new Date().toISOString() } });
        feedback.push(...analysis.feedback);
      }
      signal.throwIfAborted();
      await this.store.transaction(async (files, progress) => {
        if (!same(progress, before)) throw new DataError('分析期间作品进度已变化，请重新同步');
        for (const item of items) await this.validateItem(files, item);
        const records = (await this.readRecords(files, progress)).map((r) => items.find((x) => x.record.chapter === r.chapter)?.record ?? r);
        projectRecords(records);
        const feedbackBefore = await files.json('meta/planning_feedback.json', strings) ?? [];
        const p: PendingRevision = { version: 1, stage: 'prepared', before, after: { ...before, revision: before.revision + 1 },
          items, feedbackBefore, feedbackAfter: [...feedbackBefore, ...feedback] };
        await files.writeJSON(pendingPath, p);
        await this.fault('prepared');
      });
      return await this.apply();
    } finally { this.running = false; }
  }
  async resume(): Promise<number[]> {
    if (this.running) throw new DataError('同步正在进行');
    this.running = true;
    try { return await this.apply(); } finally { this.running = false; }
  }
  private async validateItem(files: BookFiles, item: PendingRevision['items'][number]) {
    const content = await files.read(chapterPath(item.record.chapter));
    const record = await files.json(recordPath(item.record.chapter), decodeRecord);
    if (!same(record, item.base) && !same(record, item.record)) throw new DataError('接纳记录在分析后发生变化');
    if (content === null) throw new DataError(`第 ${item.record.chapter} 章正文缺失`);
    if (contentHash(content) !== item.record.contentHash) throw new StaleRevisionText(`第 ${item.record.chapter} 章在分析后再次修改，请重新同步`);
  }
  private async readRecords(files: BookFiles, progress: Progress) {
    const records: ChapterRecord[] = [];
    for (const chapter of progress.completed) {
      const record = await files.json(recordPath(chapter), decodeRecord);
      if (!record || record.chapter !== chapter) throw new DataError('缺少接纳记录');
      records.push(record);
    }
    return records;
  }
  private async apply(): Promise<number[]> {
    return this.store.transaction(async (files, progress) => {
      const p = await files.json(pendingPath, decodePendingRevision);
      if (!p) return [];
      if (!same(progress, p.before) && !same(progress, p.after)) throw new DataError('同步恢复进度冲突');
      const current = await Promise.all(p.items.map((item) => files.json(recordPath(item.record.chapter), decodeRecord)));
      if (current.some((record, index) => !same(record, p.items[index]!.base) && !same(record, p.items[index]!.record))) throw new DataError('同步接纳记录冲突');
      if (p.stage === 'prepared') {
        const applied = current.filter((record, index) => same(record, p.items[index]!.record)).length;
        // Once every record is durable, finish its frozen projection even if a user edits text again.
        // That newer text remains untouched and appears as a fresh change on the next /sync.
        if (applied !== p.items.length) {
          try { for (const item of p.items) await this.validateItem(files, item); }
          catch (error) {
            // Safe discard only before ANY accepted record changed. Partial application retains evidence.
            if (error instanceof StaleRevisionText && applied === 0 && same(progress, p.before)) await files.remove(pendingPath);
            if (error instanceof StaleRevisionText && applied > 0) throw new DataError('正文再次修改且同步已部分接纳；恢复记录已保留，需要核对部分提交与当前正文');
            throw error;
          }
        }
        for (const item of p.items) {
          await files.writeJSON(recordPath(item.record.chapter), item.record);
          await this.fault(`record:${item.record.chapter}`);
        }
        p.stage = 'records_applied'; await files.writeJSON(pendingPath, p); await this.fault('records_applied');
      }
      for (const item of p.items) {
        if (!same(await files.json(recordPath(item.record.chapter), decodeRecord), item.record)) throw new DataError('已接纳同步记录缺失或变化');
      }
      if (p.stage === 'records_applied') {
        const records = await this.readRecords(files, progress);
        const priorFeedback = await files.json('meta/planning_feedback.json', strings) ?? [];
        if (!same(priorFeedback, p.feedbackBefore) && !same(priorFeedback, p.feedbackAfter)) throw new DataError('同步规划反馈发生冲突');
        await files.writeJSON('meta/story_state.json', projectRecords(records));
        for (const item of p.items) await files.write(`summaries/${String(item.record.chapter).padStart(2, '0')}.md`, item.record.facts.summary);
        await files.writeJSON('meta/planning_feedback.json', p.feedbackAfter);
        await files.writeJSON('meta/progress.json', p.after);
        await this.fault('projections_written');
        p.stage = 'projections_applied'; await files.writeJSON(pendingPath, p); await this.fault('projections_applied');
      }
      const projection = projectRecords(await this.readRecords(files, p.after));
      if (!same(await files.json('meta/progress.json', decodeProgress), p.after)
        || !same(await files.json('meta/story_state.json', object), projection)
        || !same(await files.json('meta/planning_feedback.json', strings), p.feedbackAfter)) throw new DataError('同步终态工件不一致');
      for (const item of p.items) {
        if (await files.read(`summaries/${String(item.record.chapter).padStart(2, '0')}.md`) !== item.record.facts.summary) throw new DataError('同步摘要工件不一致');
      }
      const key = digest(JSON.stringify(p.items));
      await appendCheckpoint(files, { scope: 'book', digest: key, step: 'revision_sync', receipt: null });
      await this.fault('checkpoint_saved');
      await files.remove(pendingPath);
      return p.items.map((x) => x.record.chapter);
    }, true);
  }
}
