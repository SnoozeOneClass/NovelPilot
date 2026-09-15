import { digest, type ChapterRecord, type Progress } from '../domain/book.js';
import type { PlanningState, Role } from '../domain/planning.js';

export interface Instruction { key: string; role: Role; kind: 'foundation' | 'outline' | 'revise' | 'write' | 'rewrite' | 'review' | 'arc_summary' | 'volume_summary' | 'conclude'; start: number; end: number; reason: string; basis: string }
export const recordBasis = (records: ChapterRecord[], start: number, end: number) => digest(JSON.stringify(records.filter((r) => r.chapter >= start && r.chapter <= end).map((r) => [r.chapter, r.revision, r.contentHash])));
/** Deterministic scheduling; no provider calls and no file IO. */
export function route(progress: Progress, state: PlanningState, records: ChapterRecord[], feedback: string[]): Instruction | null {
  const last = progress.completed.length;
  const make = (role: Role, kind: Instruction['kind'], start: number, end: number, reason: string): Instruction => {
    const basis = recordBasis(records, start, end);
    return { role, kind, start, end, reason, basis, key: `${kind}:${start}:${end}:${basis}:${kind === 'revise' ? digest(JSON.stringify(feedback)) : ''}` };
  };
  if (progress.phase === 'discussion') return null;
  if (progress.rewrites[0]) { const r = progress.rewrites[0]; return make('writer', 'rewrite', r.chapter, r.chapter, r.reason); }
  for (const aggregate of [...state.aggregates].sort((a, b) => a.start - b.start)) {
    if (aggregate.basis !== recordBasis(records, aggregate.start, aggregate.end)) return make('editor', aggregate.kind, aggregate.start, aggregate.end, '接纳章节版本变化，重新生成失效的评审或摘要');
  }
  if (progress.phase === 'complete') return null;
  if (!state.foundation) return make('planner', 'foundation', 1, 1, '整理人物、世界、创作要求与篇幅规划');
  if (feedback.length) return make('planner', 'revise', last + 1, last + 1, feedback.join('\n'));
  const boundary = state.chapters.find((p) => p.chapter === last);
  const short = state.foundation.tier === 'short';
  if (last && ((short && (last % 5 === 0 || last === state.chapters.at(-1)?.chapter)) || (!short && boundary?.arcEnd))) {
    const start = short ? 1 : state.chapters.find((p) => p.volume === boundary!.volume && p.arc === boundary!.arc)!.chapter;
    const kinds: Instruction['kind'][] = short ? ['review'] : boundary?.volumeEnd ? ['review', 'arc_summary', 'volume_summary'] : ['review', 'arc_summary'];
    for (const kind of kinds) {
      const from = kind === 'volume_summary' ? state.chapters.find((p) => p.volume === boundary!.volume)!.chapter : start;
      const task = make('editor', kind, from, last, '阶段评审或摘要必须先于后续写作');
      if (!state.aggregates.some((a) => a.key === task.key && a.basis === task.basis)) return task;
    }
  }
  if (!state.chapters.length) return make('planner', 'outline', 1, 1, '生成首批连续章节计划；长篇只展开当前弧');
  if (!state.chapters.some((p) => p.chapter === last + 1)) return make('planner', 'conclude', last + 1, last + 1, '核对要求与故事结局；完结或追加下一弧/卷计划');
  return make('writer', 'write', last + 1, last + 1, '按已保存计划写下一章');
}
