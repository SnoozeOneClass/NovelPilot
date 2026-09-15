import { type Identity } from './book.js';
import { choice, integer, list, object, strings, text, version, DataError } from './validation.js';

export type Role = 'planner' | 'writer' | 'editor';
export interface ChapterPlan { chapter: number; title: string; outline: string; volume: number; arc: number; arcEnd: boolean; volumeEnd: boolean }
export interface Foundation { title: string; premise: string; characters: string; world: string; ending: string; tier: 'short' | 'long' }
export interface Aggregate { key: string; kind: 'review' | 'arc_summary' | 'volume_summary'; start: number; end: number; basis: string; content: string; score: number | null; issues: { chapter: number; reason: string; revision: number }[] }
export interface PlanningState {
  version: 1; revision: number; foundation: Foundation | null; chapters: ChapterPlan[]; aggregates: Aggregate[];
  active: (Identity & { key: string }) | null; budgets: Record<string, number>;
  receipts: { taskId: string; attemptId: string; revision: string; path: string }[];
}
export const planningPath = 'meta/planning.json';
export const emptyPlanning = (): PlanningState => ({ version: 1, revision: 0, foundation: null, chapters: [], aggregates: [], active: null, budgets: {}, receipts: [] });
export function decodeFoundation(value: unknown): Foundation {
  const v = object(value); return { title: text(v.title), premise: text(v.premise), characters: text(v.characters), world: text(v.world), ending: text(v.ending), tier: choice(v.tier, ['short', 'long']) };
}
export function decodePlans(value: unknown): ChapterPlan[] {
  const plans = list(value, (item) => {
    const v = object(item);
    if (typeof v.arcEnd !== 'boolean' || typeof v.volumeEnd !== 'boolean' || (v.volumeEnd && !v.arcEnd)) throw new DataError('弧/卷结束标志无效');
    return { chapter: integer(v.chapter, 1), title: text(v.title), outline: text(v.outline), volume: integer(v.volume, 1), arc: integer(v.arc, 1), arcEnd: v.arcEnd, volumeEnd: v.volumeEnd };
  });
  if (!plans.length || plans.some((p, i) => i > 0 && p.chapter !== plans[i - 1]!.chapter + 1)) throw new DataError('章节计划必须连续且非空');
  return plans;
}
export function decodePlanning(value: unknown): PlanningState {
  const v = object(value); const a = v.active === null ? null : object(v.active);
  const budgets: Record<string, number> = {};
  for (const [key, amount] of Object.entries(object(v.budgets))) budgets[key] = integer(amount);
  return { version: version(v.version), revision: integer(v.revision), foundation: v.foundation === null ? null : decodeFoundation(v.foundation),
    chapters: Array.isArray(v.chapters) && !v.chapters.length ? [] : decodePlans(v.chapters), budgets,
    active: a ? { taskId: text(a.taskId), attemptId: text(a.attemptId), key: text(a.key) } : null,
    receipts: list(v.receipts, (item) => { const r = object(item); return { taskId: text(r.taskId), attemptId: text(r.attemptId), revision: text(r.revision), path: text(r.path) }; }),
    aggregates: list(v.aggregates, (item) => { const r = object(item);
      if (r.score !== null && (typeof r.score !== 'number' || !Number.isFinite(r.score) || r.score < 0 || r.score > 10)) throw new DataError('评分范围 0–10');
      return { key: text(r.key), kind: choice(r.kind, ['review', 'arc_summary', 'volume_summary']), start: integer(r.start, 1), end: integer(r.end, 1), basis: text(r.basis), content: text(r.content), score: r.score,
        issues: list(r.issues, (item) => { const i = object(item); return { chapter: integer(i.chapter, 1), revision: integer(i.revision, 1), reason: text(i.reason) }; }) }; }),
  };
}
export const decodeFeedback = strings;
