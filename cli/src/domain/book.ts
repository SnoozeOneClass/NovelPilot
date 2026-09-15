import { createHash } from 'node:crypto';
import { DataError, choice, integer, list, object, strings, text, version } from './validation.js';

export const normalizeContent = (content: string) => content.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n');
export const digest = (content: string) => createHash('sha256').update(content).digest('hex');
export const contentHash = (content: string) => digest(normalizeContent(content));
export interface Identity { taskId: string; attemptId: string }
export function decodeIdentity(value: unknown): Identity {
  const v = object(value); return { taskId: text(v.taskId), attemptId: text(v.attemptId) };
}
export interface ChapterFacts {
  title: string; summary: string; characters: string[]; keyEvents: string[];
  timeline: { time: string; event: string }[];
  stateChanges: { subject: string; state: string }[];
  relationships: { from: string; to: string; relation: string }[];
  foreshadows: { id: string; action: 'plant' | 'advance' | 'resolve'; description: string }[];
}
export function decodeFacts(value: unknown): ChapterFacts {
  const v = object(value);
  return {
    title: text(v.title), summary: text(v.summary), characters: strings(v.characters), keyEvents: strings(v.keyEvents),
    timeline: list(v.timeline, (item) => { const x = object(item); return { time: text(x.time), event: text(x.event) }; }),
    stateChanges: list(v.stateChanges, (item) => { const x = object(item); return { subject: text(x.subject), state: text(x.state) }; }),
    relationships: list(v.relationships, (item) => { const x = object(item); return { from: text(x.from), to: text(x.to), relation: text(x.relation) }; }),
    foreshadows: list(v.foreshadows, (item) => { const x = object(item); return { id: text(x.id), action: choice(x.action, ['plant', 'advance', 'resolve']), description: text(x.description, true) }; }),
  };
}
export interface ChapterRecord {
  version: 1; chapter: number; revision: number; origin: 'generated' | 'user';
  content: string; contentHash: string; facts: ChapterFacts; style: string[]; acceptedAt: string;
}
export function decodeRecord(value: unknown): ChapterRecord {
  const v = object(value);
  const result: ChapterRecord = {
    version: version(v.version), chapter: integer(v.chapter, 1), revision: integer(v.revision, 1),
    origin: choice(v.origin, ['generated', 'user']), content: text(v.content), contentHash: text(v.contentHash),
    facts: decodeFacts(v.facts), style: strings(v.style), acceptedAt: text(v.acceptedAt),
  };
  if (result.contentHash !== contentHash(result.content) || !Number.isFinite(Date.parse(result.acceptedAt))) throw new DataError('章节哈希或接纳时间无效');
  return result;
}
export interface Progress {
  version: 1; revision: number; phase: 'discussion' | 'writing' | 'complete';
  completed: number[]; rewrites: { chapter: number; reason: string }[]; active: (Identity & { chapter: number; kind: 'write' | 'rewrite' }) | null;
}
export function decodeProgress(value: unknown): Progress {
  const v = object(value);
  const completed = list(v.completed, (item) => integer(item, 1));
  if (completed.some((chapter, i) => chapter !== i + 1)) throw new DataError('已完成章节必须连续且唯一');
  const active = v.active === null ? null : object(v.active);
  const rewrites = list(v.rewrites, (item) => { const x = object(item); return { chapter: integer(x.chapter, 1), reason: text(x.reason) }; });
  if (new Set(rewrites.map((x) => x.chapter)).size !== rewrites.length || rewrites.some((x) => !completed.includes(x.chapter))) throw new DataError('返工范围无效');
  const result: Progress = { version: version(v.version), revision: integer(v.revision), phase: choice(v.phase, ['discussion', 'writing', 'complete']), completed, rewrites,
    active: active ? { ...decodeIdentity(active), chapter: integer(active.chapter, 1), kind: choice(active.kind, ['write', 'rewrite']) } : null };
  if (result.phase === 'discussion' && (completed.length || rewrites.length || result.active)) throw new DataError('共创期存在非法创作进度');
  if (result.active && (result.active.kind === 'write'
    ? result.phase !== 'writing' || rewrites.length > 0 || result.active.chapter !== completed.length + 1
    : rewrites[0]?.chapter !== result.active.chapter)) throw new DataError('活动任务与章节授权不一致');
  return result;
}
export const emptyProgress = (): Progress => ({ version: 1, revision: 0, phase: 'discussion', completed: [], rewrites: [], active: null });
export interface CommitReceipt extends Identity { chapter: number; revision: number; path: string; digest: string }
export function decodeReceipt(value: unknown): CommitReceipt {
  const v = object(value); return { ...decodeIdentity(v), chapter: integer(v.chapter, 1), revision: integer(v.revision, 1), path: text(v.path), digest: text(v.digest) };
}
export interface PendingCommit {
  version: 1; stage: 'started' | 'state_applied' | 'progress_marked' | 'signal_saved';
  record: ChapterRecord; baseRevision: number; before: Progress; after: Progress; receipt: CommitReceipt;
}
export function decodePending(value: unknown): PendingCommit {
  const v = object(value);
  const p: PendingCommit = { version: version(v.version), stage: choice(v.stage, ['started', 'state_applied', 'progress_marked', 'signal_saved']), record: decodeRecord(v.record),
    baseRevision: integer(v.baseRevision), before: decodeProgress(v.before), after: decodeProgress(v.after), receipt: decodeReceipt(v.receipt) };
  if (p.record.revision !== p.baseRevision + 1 || p.receipt.chapter !== p.record.chapter || p.receipt.revision !== p.record.revision ||
      p.receipt.digest !== digest(JSON.stringify(p.record)) || p.after.revision !== p.before.revision + 1 ||
      !p.before.active || p.before.active.chapter !== p.record.chapter || p.before.active.taskId !== p.receipt.taskId || p.before.active.attemptId !== p.receipt.attemptId ||
      p.after.active !== null || !p.after.completed.includes(p.record.chapter)) throw new DataError('提交恢复记录不一致');
  const active = p.before.active;
  if ((active.kind === 'write' && (p.baseRevision !== 0 || p.before.phase !== 'writing' || p.before.rewrites.length || p.record.chapter !== p.before.completed.length + 1)) ||
      (active.kind === 'rewrite' && (p.baseRevision < 1 || p.before.rewrites[0]?.chapter !== p.record.chapter))) throw new DataError('恢复任务范围无效');
  const expected = { ...p.before, revision: p.before.revision + 1, active: null,
    completed: [...new Set([...p.before.completed, p.record.chapter])].sort((a, b) => a - b),
    rewrites: p.before.rewrites.filter((r) => r.chapter !== p.record.chapter) };
  if (JSON.stringify(expected) !== JSON.stringify(p.after)) throw new DataError('恢复记录扩展了提交范围');
  return p;
}

/** Derive from accepted records every time; rewriting must not accumulate stale deltas. */
export function projectRecords(records: ChapterRecord[]) {
  const states = new Map<string, string>();
  const relationships = new Map<string, { from: string; to: string; relation: string }>();
  const foreshadows = new Map<string, { description: string; plantedAt: number; status: string }>();
  const timeline: { chapter: number; time: string; event: string }[] = [];
  for (const record of [...records].sort((a, b) => a.chapter - b.chapter)) {
    for (const x of record.facts.stateChanges) states.set(x.subject, x.state);
    for (const x of record.facts.relationships) relationships.set(JSON.stringify([x.from, x.to]), x);
    for (const x of record.facts.timeline) timeline.push({ chapter: record.chapter, ...x });
    for (const x of record.facts.foreshadows) {
      const prior = foreshadows.get(x.id);
      if (x.action === 'plant') {
        if (prior) throw new DataError(`伏笔重复埋设：${x.id}`);
        foreshadows.set(x.id, { description: x.description, plantedAt: record.chapter, status: 'plant' });
      } else {
        if (!prior || prior.status === 'resolve') throw new DataError(`伏笔没有有效前置状态：${x.id}`);
        foreshadows.set(x.id, { ...prior, status: x.action });
      }
    }
  }
  return { states: Object.fromEntries(states), relationships: [...relationships.values()], foreshadows: Object.fromEntries(foreshadows), timeline,
    summaries: records.map((r) => ({ chapter: r.chapter, revision: r.revision, summary: r.facts.summary })),
    style: [...new Set(records.flatMap((r) => r.style))] };
}
