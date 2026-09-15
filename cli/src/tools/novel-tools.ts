import { defineTool, type ToolDefinition } from '@earendil-works/pi-coding-agent';
import { Type, type TSchema } from '@earendil-works/pi-ai';
import { contentHash, decodeFacts, decodeProgress, decodeRecord, projectRecords, type Identity, type Progress } from '../domain/book.js';
import { decodeFoundation, decodePlanning, decodePlans, emptyPlanning, planningPath, type PlanningState, type Aggregate } from '../domain/planning.js';
import { DataError, integer, list, object, strings, text } from '../domain/validation.js';
import { recordBasis, type Instruction } from '../app/router.js';
import { BookStore, recordPath } from '../store/book-store.js';
import type { ArtifactEvidence } from '../runtime/pi/task-runner.js';
import type { BookFiles } from '../store/files.js';

const string = Type.String({ minLength: 1 });
const foundationSchema = Type.Object({ title: string, premise: string, characters: string, world: string, ending: string, tier: Type.Union([Type.Literal('short'), Type.Literal('long')]) });
const planSchema = Type.Object({ chapter: Type.Integer({ minimum: 1 }), title: string, outline: string, volume: Type.Integer({ minimum: 1 }), arc: Type.Integer({ minimum: 1 }), arcEnd: Type.Boolean(), volumeEnd: Type.Boolean() });
const factsSchema = Type.Object({ title: string, summary: string, characters: Type.Array(string), keyEvents: Type.Array(string),
  timeline: Type.Array(Type.Object({ time: string, event: string })), stateChanges: Type.Array(Type.Object({ subject: string, state: string })),
  relationships: Type.Array(Type.Object({ from: string, to: string, relation: string })),
  foreshadows: Type.Array(Type.Object({ id: string, action: Type.Union([Type.Literal('plant'), Type.Literal('advance'), Type.Literal('resolve')]), description: Type.String() })) });

export interface NovelToolContext { store: BookStore; instruction: Instruction; identity: Identity; signal: AbortSignal; evidence: ArtifactEvidence[] }
export function novelTools(ctx: NovelToolContext): ToolDefinition[] {
  const { store, instruction: task, identity, evidence, signal } = ctx;
  const chapterStem = String(task.start).padStart(2, '0');
  const guard = (state: PlanningState) => {
    if (signal.aborted) throw new DataError('操作已取消');
    if (state.active?.taskId !== identity.taskId || state.active.attemptId !== identity.attemptId || state.active.key !== task.key || state.receipts.some((r) => r.attemptId === identity.attemptId)) throw new DataError('任务授权已过期或产物已经完成');
  };
  const writerGuard = async (files: BookFiles, progress: Progress) => {
    guard(await files.json(planningPath, decodePlanning) ?? emptyPlanning());
    if (task.role !== 'writer' || progress.active?.chapter !== task.start || progress.active.taskId !== identity.taskId || progress.active.attemptId !== identity.attemptId) throw new DataError('当前章节写入授权已过期');
  };
  const save = async (change: (state: PlanningState) => Promise<void> | void) => store.transaction(async (files) => {
    const state = await files.json(planningPath, decodePlanning) ?? emptyPlanning(); guard(state);
    await change(state);
    state.revision += 1;
    const receipt = { ...identity, path: planningPath, revision: String(state.revision) };
    state.receipts.push(receipt);
    await files.writeJSON(planningPath, state);
    evidence.push(receipt);
    return receipt;
  });
  const tool = (name: string, description: string, parameters: TSchema, execute: (params: Record<string, unknown>) => Promise<unknown>) => defineTool({
    name, label: name, description, parameters,
    execute: async (_id, params) => {
      const result = await execute(object(params));
      return { content: [{ type: 'text' as const, text: JSON.stringify(result) }], details: {} };
    },
  });
  const result: ToolDefinition[] = [tool('novel_context', '读取本书要求、设定、计划、接纳摘要与目标之前的故事事实。chapter 可选，用于读取指定已完成正文。', Type.Object({ chapter: Type.Optional(Type.Integer({ minimum: 1 })) }), async (params) => {
    const snapshot = await store.snapshot();
    const state = await store.files.json(planningPath, decodePlanning) ?? emptyPlanning();
    const chapter = params.chapter === undefined ? null : integer(params.chapter, 1);
    const target = snapshot.records.find((r) => r.chapter === chapter);
    if (chapter && !target) throw new DataError('指定章节尚未接纳');
    const long = state.foundation?.tier === 'long';
    const summaryWindow = long ? 3 : 10; const factsWindow = long ? 5 : 10;
    const contextChapter = chapter === null ? task.start : Math.min(task.start, chapter);
    const currentPlan = state.chapters.find((p) => p.chapter === contextChapter);
    const prior = snapshot.records.filter((r) => r.chapter < contextChapter);
    const plans = (long && currentPlan ? state.chapters.filter((p) => p.volume === currentPlan.volume && p.arc === currentPlan.arc) : state.chapters.filter((p) => p.chapter >= contextChapter)).slice(0, 20);
    const validAggregates = state.aggregates.filter((a) => a.end < contextChapter && a.basis === recordBasis(snapshot.records, a.start, a.end)).sort((a, b) => a.end - b.end);
    const projection = projectRecords(prior);
    const volumes = validAggregates.filter((a) => a.kind === 'volume_summary').slice(-8);
    const arcs = validAggregates.filter((a) => a.kind === 'arc_summary' && (!currentPlan || state.chapters.find((p) => p.chapter === a.start)?.volume === currentPlan.volume)).slice(-8);
    return { task, requirements: await store.files.read('meta/requirements.md'), rules: await store.files.read('meta/rules.md'), foundation: state.foundation,
      plans, summaries: prior.slice(-summaryWindow).map((r) => ({ chapter: r.chapter, revision: r.revision, summary: r.facts.summary })),
      factsBeforeTarget: prior.slice(-factsWindow).map((r) => ({ chapter: r.chapter, facts: r.facts })),
      currentStoryState: { states: projection.states, relationships: projection.relationships, activeForeshadows: Object.fromEntries(Object.entries(projection.foreshadows).filter(([, value]) => value.status !== 'resolve')), recentTimeline: projection.timeline.filter((event) => event.chapter >= contextChapter - factsWindow) },
      aggregates: [...volumes, ...arcs],
      contextRange: { beforeChapter: contextChapter, summaryWindow, factsWindow, selectedPlanChapters: plans.map((p) => p.chapter), totalPlanChapters: state.chapters.length, priorChapters: prior.length,
        omittedOlderChapterSummaries: Math.max(0, prior.length - summaryWindow), omittedOlderVolumeSummaries: Math.max(0, validAggregates.filter((a) => a.kind === 'volume_summary').length - volumes.length),
        olderMaterial: '需要更早的资料时再次调用 novel_context(chapter=已完成章号)，会返回该章完整接纳记录及其之前的上下文窗口。' },
      chapter: target ?? (task.kind === 'rewrite' ? snapshot.records.find((r) => r.chapter === task.start) ?? null : null),
      chapterPlan: task.role === 'writer' ? await store.files.json(`drafts/${chapterStem}.plan.json`, object) : null,
      workingDraft: task.role === 'writer' ? await store.files.read(`drafts/${chapterStem}.draft.md`) : null };
  })];
  if (task.role === 'planner') {
    if (task.kind === 'foundation') result.push(tool('save_foundation', '保存完整书名、核心故事、人物、世界、结局方向与篇幅策略。', foundationSchema, async (params) => save((state) => { state.foundation = decodeFoundation(params); })));
    if (['outline', 'conclude', 'revise'].includes(task.kind)) result.push(tool('save_outline', '保存从下一未完成章节开始的连续计划；保留已写章节。长篇每批必须以弧结束，可标记卷结束。修改任务可同时更新 foundation。', Type.Object({ chapters: Type.Array(planSchema, { minItems: 1 }), reason: string, foundation: Type.Optional(foundationSchema) }), async (params) => {
      const plans = decodePlans(params.chapters); text(params.reason);
      return save(async (state) => {
        const progress = await store.files.json('meta/progress.json', decodeProgress);
        if (!progress || progress.phase !== 'writing' || plans[0]!.chapter !== progress.completed.length + 1) throw new DataError('计划只能从下一未完成章节开始');
        if (params.foundation !== undefined) {
          if (task.kind !== 'revise') throw new DataError('当前任务未授权修改基础设定');
          state.foundation = decodeFoundation(params.foundation);
        }
        if (state.foundation?.tier === 'long' && !plans.at(-1)!.arcEnd) throw new DataError('滚动规划必须完整展开当前弧');
        state.chapters = state.chapters.filter((p) => p.chapter <= progress.completed.length).concat(plans);
      });
    }));
    if (task.kind === 'revise') result.push(tool('resolve_outline_feedback', '检查已接纳事实后，仅当后续计划无需变化时保存理由并完成意见处理。', Type.Object({ reason: string }), async (params) => {
      text(params.reason); return save(() => undefined);
    }));
    if (task.kind === 'conclude') result.push(tool('complete_book', '仅在大纲全部写完并满足创作要求时完结。必须先核对上下文并解释结局依据。', Type.Object({ reason: string }), async (params) => {
      text(params.reason);
      // A durable receipt and explicit intent are saved before the progress projection.
      const receipt = await save(async (state) => {
        const progress = await store.files.json('meta/progress.json', decodeProgress);
        if (!progress || progress.phase !== 'writing' || progress.rewrites.length || progress.active || !progress.completed.length || progress.completed.length !== state.chapters.at(-1)?.chapter) throw new DataError('尚有未完成计划，不能完结');
        state.receipts.push({ ...identity, path: 'meta/completion-intent', revision: text(params.reason) });
      });
      await store.transaction(async (files, progress) => {
        if (progress.rewrites.length || progress.active || progress.completed.length < 1) throw new DataError('仍有未完成工作');
        await files.writeJSON('meta/progress.json', { ...progress, revision: progress.revision + 1, phase: 'complete' });
      });
      return receipt;
    }));
  }
  if (task.role === 'writer') {
    result.push(tool('plan_chapter', '保存当前章节构思：目标、核心冲突、结尾钩子和连续性关注点。粒度由你决定，不强制拆分场景。', Type.Object({ title: string, goal: string, conflict: string, hook: string, notes: Type.Optional(Type.String()), requiredBeats: Type.Optional(Type.Array(string)), forbiddenMoves: Type.Optional(Type.Array(string)), continuityChecks: Type.Optional(Type.Array(string)) }), async (params) => store.transaction(async (files, progress) => {
      await writerGuard(files, progress);
      const plan = { version: 1, ...identity, chapter: task.start, title: text(params.title), goal: text(params.goal), conflict: text(params.conflict), hook: text(params.hook), notes: text(params.notes ?? '', true), requiredBeats: strings(params.requiredBeats ?? []), forbiddenMoves: strings(params.forbiddenMoves ?? []), continuityChecks: strings(params.continuityChecks ?? []) };
      await files.writeJSON(`drafts/${chapterStem}.plan.json`, plan);
      return { planned: true, chapter: task.start, next: 'save_draft' };
    })));
    result.push(tool('save_draft', '保存当前授权章节的正文草稿；不代表正式完成。', Type.Object({ content: string }), async (params) => {
      await store.saveDraft(identity, task.start, text(params.content), writerGuard); return { saved: true };
    }));
    result.push(tool('check_consistency', '在保存正文后加载本章草稿及对照数据，供你判断一致性。记录检查的是哪个草稿版本；不替你判定文学质量或自动修正文稿。', Type.Object({}), async () => store.transaction(async (files, progress) => {
      await writerGuard(files, progress);
      const draft = await files.read(`drafts/${chapterStem}.draft.md`);
      if (!draft?.trim()) throw new DataError('请先保存章节正文，再检查一致性');
      const state = await files.json(planningPath, decodePlanning) ?? emptyPlanning();
      const prior = [];
      for (const chapter of progress.completed.filter((n) => n < task.start)) {
        const record = await files.json(recordPath(chapter), decodeRecord);
        if (!record) throw new DataError('一致性对照接纳记录缺失');
        prior.push(record);
      }
      const receipt = { version: 1, ...identity, chapter: task.start, draftHash: contentHash(draft), checkedAt: new Date().toISOString(), verdict: 'model-review-required' };
      await files.writeJSON(`drafts/${chapterStem}.check.json`, receipt);
      const projection = projectRecords(prior);
      return { ...receipt, content: draft, foundation: state.foundation, rules: await files.read('meta/rules.md'), chapterPlan: await files.json(`drafts/${chapterStem}.plan.json`, object),
        factsBeforeTarget: { states: projection.states, relationships: projection.relationships, activeForeshadows: Object.fromEntries(Object.entries(projection.foreshadows).filter(([, value]) => value.status !== 'resolve')), recentTimeline: projection.timeline.filter((event) => event.chapter >= task.start - 5) },
        recentSummaries: prior.slice(-2).map((r) => ({ chapter: r.chapter, summary: r.facts.summary })) };
    })));
    result.push(tool('commit_chapter', '先自检草稿与计划、人物和时间线，再保存本章接纳事实并正式提交。', Type.Object({ facts: factsSchema }), async (params) => {
      const receipt = await store.commit(identity, task.start, decodeFacts(params.facts), writerGuard);
      if (receipt.taskId !== identity.taskId || receipt.attemptId !== identity.attemptId) throw new DataError('旧提交不能代表本次任务完成');
      evidence.push({ ...identity, path: receipt.path, revision: String(receipt.revision) }); return receipt;
    }));
  }
  if (task.role === 'editor') {
    const name = task.kind === 'review' ? 'save_review' : 'save_summary';
    result.push(tool(name, task.kind === 'review' ? '保存范围内的评审意见与内部质量评分；只列真正需要返工的章号。' : '保存当前授权弧或卷摘要，不以评审代替摘要。', Type.Object({ content: string, score: Type.Optional(Type.Number({ minimum: 0, maximum: 10 })), issues: Type.Optional(Type.Array(Type.Object({ chapter: Type.Integer({ minimum: 1 }), reason: string }))) }), async (params) => {
      const snapshot = await store.snapshot();
      if (recordBasis(snapshot.records, task.start, task.end) !== task.basis) throw new DataError('评审正文版本已变化');
      const issues = list(params.issues ?? [], (item) => { const i = object(item); const chapter = integer(i.chapter, 1); const record = snapshot.records.find((r) => r.chapter === chapter);
        if (chapter < task.start || chapter > task.end || !record) throw new DataError('评审不能扩大返工范围');
        return { chapter, reason: text(i.reason), revision: record.revision }; });
      if (task.kind !== 'review' && issues.length) throw new DataError('摘要任务不能派发返工');
      const score = params.score ?? null;
      if (score !== null && (typeof score !== 'number' || !Number.isFinite(score) || score < 0 || score > 10)) throw new DataError('评分无效');
      return save(async (state) => {
        const currentRecords = [];
        for (let chapter = task.start; chapter <= task.end; chapter++) {
          const record = await store.files.json(recordPath(chapter), decodeRecord);
          if (!record) throw new DataError('评审接纳记录缺失');
          currentRecords.push(record);
        }
        if (recordBasis(currentRecords, task.start, task.end) !== task.basis) throw new DataError('评审写入前正文版本已变化');
        if (issues.length) {
          const cycleKey = `review-cycles:${task.start}:${task.end}`;
          if ((state.budgets[cycleKey] ?? 0) >= 3) throw new DataError('同一阶段反复返工仍未通过，请人工检查评审依据');
          state.budgets[cycleKey] = (state.budgets[cycleKey] ?? 0) + 1;
        }
        const aggregate: Aggregate = { key: task.key, kind: task.kind as Aggregate['kind'], start: task.start, end: task.end, basis: task.basis, content: text(params.content), score, issues };
        state.aggregates = state.aggregates.filter((a) => !(a.kind === aggregate.kind && a.start === aggregate.start && a.end === aggregate.end)).concat(aggregate);
      });
    }));
  }
  return result;
}
