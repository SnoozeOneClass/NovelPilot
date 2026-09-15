import { readdir } from 'node:fs/promises';
import {
  contentHash, decodeFacts, decodeIdentity, decodePending, decodeProgress, decodeRecord, digest, emptyProgress, normalizeContent, projectRecords,
  type ChapterFacts, type ChapterRecord, type CommitReceipt, type Identity, type PendingCommit, type Progress,
} from '../domain/book.js';
import { DataError, integer, object, text } from '../domain/validation.js';
import { BookLease } from './book-lease.js';
import { BookFiles, missing } from './files.js';
import { MutationQueue } from './io.js';
import { readCheckpoints, appendCheckpoint } from './checkpoints.js';

const progressPath = 'meta/progress.json';
const pendingPath = 'meta/pending_commit.json';
export const chapterPath = (chapter: number) => `chapters/${String(integer(chapter, 1)).padStart(2, '0')}.md`;
export const recordPath = (chapter: number) => `meta/chapter_records/${String(integer(chapter, 1)).padStart(6, '0')}.json`;
const draftPath = (chapter: number) => `drafts/${String(integer(chapter, 1)).padStart(2, '0')}.draft.md`;
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);
export type FaultPoint = 'pending_saved' | 'chapter_written' | 'record_written' | 'projections_written' | 'state_applied' | 'progress_written' | 'progress_marked' | 'checkpoint_saved' | 'signal_saved';

export class BookStore {
  readonly files: BookFiles;
  private readonly mutations = new MutationQueue();
  private closing: Promise<void> | undefined;
  private constructor(private readonly lease: BookLease, private readonly fault: (point: FaultPoint) => Promise<void>) {
    this.files = new BookFiles(lease.bookDir);
  }
  static async open(bookDir: string, fault: (point: FaultPoint) => Promise<void> = async () => undefined): Promise<BookStore> {
    const lease = await BookLease.acquire(bookDir);
    const store = new BookStore(lease, fault);
    try {
      await store.mutate(async () => {
        const progress = await store.files.json(progressPath, decodeProgress);
        const checkpoints = await store.checkpoints();
        if (!progress) {
          for (const relative of ['chapters', 'meta/chapter_records', 'drafts', 'summaries']) {
            try { if ((await readdir(await store.files.path(relative))).length) throw new DataError('缺少进度记录，不能将已有作品当作空书'); }
            catch (error) { if (!missing(error)) throw error; }
          }
          if (checkpoints.length) throw new DataError('缺少进度记录，但存在作品检查点');
          for (const path of [pendingPath, 'meta/pending_revision.json', 'meta/requirements.md', 'meta/story_state.json', 'meta/planning.json', 'meta/discussion.json', 'meta/start-intent.json']) {
            if (await store.files.read(path) !== null) throw new DataError('缺少进度记录，但存在作品资料，不能初始化空书');
          }
          await store.files.writeJSON(progressPath, emptyProgress());
        }
        await store.recoverUnlocked();
        await store.records(await store.progressUnlocked());
      });
      return store;
    } catch (error) { await lease.close(); throw error; }
  }
  close(): Promise<void> {
    this.closing ??= this.mutations.run(() => this.lease.close());
    return this.closing;
  }
  private mutate<T>(operation: () => Promise<T>): Promise<T> {
    if (this.closing) return Promise.reject(new Error('作品已关闭'));
    return this.mutations.run(operation);
  }
  private async progressUnlocked(): Promise<Progress> {
    const progress = await this.files.json(progressPath, decodeProgress);
    if (!progress) throw new DataError('缺少进度文件');
    return progress;
  }
  async snapshot() {
    return this.mutate(async () => {
      const progress = await this.progressUnlocked();
      return { progress, records: await this.records(progress), pending: await this.files.json(pendingPath, decodePending) };
    });
  }
  private async records(progress: Progress): Promise<ChapterRecord[]> {
    const records: ChapterRecord[] = [];
    for (const chapter of progress.completed) {
      const record = await this.files.json(recordPath(chapter), decodeRecord);
      if (!record || record.chapter !== chapter) throw new DataError(`第 ${chapter} 章接纳记录缺失或错位`);
      records.push(record);
    }
    return records;
  }
  async beginCreation(requirements: string): Promise<void> {
    text(requirements);
    await this.mutate(async () => {
      await this.ensureNoPending();
      const p = await this.progressUnlocked();
      if (p.phase !== 'discussion') throw new DataError('本书已开始创作');
      await this.files.write('meta/requirements.md', requirements);
      await this.files.writeJSON(progressPath, { ...p, revision: p.revision + 1, phase: 'writing' });
    });
  }
  async queueRewrites(chapters: number[], reason: string): Promise<void> {
    text(reason);
    await this.mutate(async () => {
      await this.ensureNoPending();
      const p = await this.progressUnlocked();
      if (p.active) throw new DataError('必须等待当前任务结束');
      if (!chapters.length || chapters.some((chapter) => !p.completed.includes(integer(chapter, 1)))) throw new DataError('返工只能指向已完成章节');
      for (const chapter of [...new Set(chapters)].sort((a, b) => a - b)) {
        if (!p.rewrites.some((x) => x.chapter === chapter)) p.rewrites.push({ chapter, reason });
      }
      p.revision += 1;
      await this.files.writeJSON(progressPath, p);
    });
  }
  async beginChapter(identity: Identity, chapter: number): Promise<void> {
    decodeIdentity(identity); integer(chapter, 1);
    await this.mutate(async () => {
      await this.ensureNoPending();
      const p = await this.progressUnlocked();
      const target = p.rewrites[0]?.chapter ?? p.completed.length + 1;
      if (chapter !== target || (p.phase !== 'writing' && !p.rewrites.length)) throw new DataError('当前章节不在授权范围');
      if (p.active && (p.active.taskId !== identity.taskId || p.active.chapter !== chapter)) throw new DataError('另一任务尚未结束');
      p.active = { ...identity, chapter, kind: p.rewrites.length ? 'rewrite' : 'write' };
      p.revision += 1;
      await this.files.writeJSON(progressPath, p);
    });
  }
  private authorize(p: Progress, identity: Identity, chapter: number) {
    if (!p.active || p.active.chapter !== chapter || p.active.taskId !== identity.taskId || p.active.attemptId !== identity.attemptId) throw new DataError('任务已结束或授权已过期');
  }
  private async ensureNoPending(allowRevision = false) {
    if (await this.files.read(pendingPath) !== null) throw new DataError('存在未完成提交，请先恢复');
    if (!allowRevision && await this.files.read('meta/pending_revision.json') !== null) throw new DataError('存在未完成同步，请先恢复');
  }
  /** Internal Host/service operations only. Never await model calls in this critical section. */
  async transaction<T>(operation: (files: BookFiles, progress: Progress) => Promise<T>, allowRevision = false): Promise<T> {
    return this.mutate(async () => { await this.ensureNoPending(allowRevision); return operation(this.files, await this.progressUnlocked()); });
  }
  async finishTask(identity: Identity): Promise<void> {
    await this.mutate(async () => {
      await this.ensureNoPending();
      const p = await this.progressUnlocked();
      if (!p.active) return;
      this.authorize(p, identity, p.active.chapter);
      await this.files.writeJSON(progressPath, { ...p, revision: p.revision + 1, active: null });
    });
  }
  async saveDraft(identity: Identity, chapter: number, content: string, beforeWrite?: (files: BookFiles, progress: Progress) => Promise<void>): Promise<void> {
    text(content);
    await this.mutate(async () => {
      await this.ensureNoPending();
      const progress = await this.progressUnlocked();
      this.authorize(progress, identity, chapter);
      await beforeWrite?.(this.files, progress);
      await this.files.write(draftPath(chapter), normalizeContent(content));
    });
  }
  async commit(identity: Identity, chapter: number, rawFacts: ChapterFacts, beforeWrite?: (files: BookFiles, progress: Progress) => Promise<void>): Promise<CommitReceipt> {
    integer(chapter, 1); decodeIdentity(identity);
    return this.mutate(async () => {
      await beforeWrite?.(this.files, await this.progressUnlocked());
      if (await this.files.read('meta/pending_revision.json') !== null) throw new DataError('存在未完成同步，请先恢复');
      const pending = await this.files.json(pendingPath, decodePending);
      if (pending) {
        if (pending.record.chapter !== chapter) throw new DataError('另一个章节正在恢复');
        return this.replay(pending);
      }
      const p = await this.progressUnlocked();
      if (p.completed.includes(chapter) && !p.rewrites.some((x) => x.chapter === chapter)) {
        const old = (await this.checkpoints()).findLast((cp) => cp.receipt?.chapter === chapter);
        if (!old?.receipt) throw new DataError('已完成章节缺少提交检查点');
        return old.receipt;
      }
      this.authorize(p, identity, chapter);
      const content = await this.files.read(draftPath(chapter));
      if (!content?.trim()) throw new DataError('章节草稿缺失或为空');
      const base = await this.files.json(recordPath(chapter), decodeRecord);
      if (p.active?.kind === 'write' && base) throw new DataError('新章节已有无法解释的接纳记录');
      if (p.active?.kind === 'rewrite' && (!base || p.rewrites[0]?.chapter !== chapter)) throw new DataError('返工队首或接纳记录不一致');
      const facts = decodeFacts(rawFacts);
      // Keep this chapter's original plants so downstream accepted advances remain valid.
      const declaredPlants = new Set(facts.foreshadows.filter((f) => f.action === 'plant').map((f) => f.id));
      facts.foreshadows.unshift(...(base?.facts.foreshadows.filter((f) => f.action === 'plant' && !declaredPlants.has(f.id)) ?? []));
      const record: ChapterRecord = { version: 1, chapter, revision: (base?.revision ?? 0) + 1, origin: 'generated',
        content: normalizeContent(content), contentHash: contentHash(content), facts, style: base?.style ?? [], acceptedAt: new Date().toISOString() };
      const records = (await this.records(p)).filter((r) => r.chapter !== chapter).concat(record);
      projectRecords(records); // Validate cross-chapter invariants before the first write.
      const after = decodeProgress({ ...p, revision: p.revision + 1, active: null,
        completed: [...new Set([...p.completed, chapter])].sort((a, b) => a - b), rewrites: p.rewrites.filter((r) => r.chapter !== chapter) });
      const receipt: CommitReceipt = { ...identity, chapter, revision: record.revision, path: chapterPath(chapter), digest: digest(JSON.stringify(record)) };
      const frozen: PendingCommit = { version: 1, stage: 'started', record, baseRevision: base?.revision ?? 0, before: p, after, receipt };
      await this.files.writeJSON(pendingPath, frozen);
      await this.fault('pending_saved');
      return this.replay(frozen);
    });
  }
  async recover(): Promise<CommitReceipt | null> { return this.mutate(() => this.recoverUnlocked()); }
  private async recoverUnlocked() {
    const pending = await this.files.json(pendingPath, decodePending);
    if (pending && await this.files.read('meta/pending_revision.json') !== null) throw new DataError('提交与同步恢复记录冲突，不能自动恢复');
    return pending ? this.replay(pending) : null;
  }
  private async replay(p: PendingCommit): Promise<CommitReceipt> {
    const progress = await this.progressUnlocked();
    if (!same(progress, p.before) && !same(progress, p.after)) throw new DataError('提交恢复与进度版本冲突');
    if (p.receipt.path !== chapterPath(p.record.chapter)) throw new DataError('提交恢复路径无效');
    const existing = await this.files.json(recordPath(p.record.chapter), decodeRecord);
    if ((existing?.revision ?? 0) !== p.baseRevision && !same(existing, p.record)) throw new DataError('接纳记录与恢复版本冲突');
    const completedRecords = await this.records({ ...p.before, completed: p.before.completed.filter((n) => n !== p.record.chapter) });
    const records = completedRecords.concat(p.record).sort((a, b) => a.chapter - b.chapter);
    const projection = projectRecords(records);
    if (p.stage === 'started') {
      await this.files.write(chapterPath(p.record.chapter), p.record.content); await this.fault('chapter_written');
      await this.files.writeJSON(recordPath(p.record.chapter), p.record); await this.fault('record_written');
      await this.files.writeJSON('meta/story_state.json', projection);
      await this.files.write(`summaries/${String(p.record.chapter).padStart(2, '0')}.md`, p.record.facts.summary);
      await this.fault('projections_written');
      p.stage = 'state_applied'; await this.files.writeJSON(pendingPath, p); await this.fault('state_applied');
    }
    if (p.stage === 'state_applied') {
      await this.files.writeJSON(progressPath, p.after); await this.fault('progress_written');
      p.stage = 'progress_marked'; await this.files.writeJSON(pendingPath, p); await this.fault('progress_marked');
    }
    if (p.stage === 'progress_marked') {
      await this.appendCheckpoint(p.receipt); await this.fault('checkpoint_saved');
      p.stage = 'signal_saved'; await this.files.writeJSON(pendingPath, p); await this.fault('signal_saved');
    }
    const checkpoint = (await this.checkpoints()).find((cp) => same(cp.receipt, p.receipt));
    if (!checkpoint || !same(await this.progressUnlocked(), p.after) || !same(await this.files.json(recordPath(p.record.chapter), decodeRecord), p.record)) throw new DataError('提交终态缺少对应工件');
    if (contentHash(await this.files.read(chapterPath(p.record.chapter)) ?? '') !== p.record.contentHash ||
        !same(await this.files.json('meta/story_state.json', object), projection) ||
        await this.files.read(`summaries/${String(p.record.chapter).padStart(2, '0')}.md`) !== p.record.facts.summary) throw new DataError('提交工件与固定载荷不一致');
    await this.files.remove(pendingPath);
    return p.receipt;
  }
  private checkpoints() { return readCheckpoints(this.files); }
  private async appendCheckpoint(receipt: CommitReceipt) {
    await appendCheckpoint(this.files, { scope: `chapter:${receipt.chapter}`, step: 'commit_chapter', digest: receipt.digest, receipt });
  }
  async completion(identity: Identity): Promise<CommitReceipt[]> {
    return this.mutate(async () => (await this.checkpoints()).flatMap((cp) => cp.receipt?.taskId === identity.taskId && cp.receipt.attemptId === identity.attemptId ? [cp.receipt] : []));
  }
}
