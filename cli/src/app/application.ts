import { randomUUID } from 'node:crypto';
import type { AgentSession } from '@earendil-works/pi-coding-agent';
import { join } from 'node:path';
import { BookStore } from '../store/book-store.js';
import { RevisionService, decodeAnalysis } from '../store/revision.js';
import { object, list, text, choice, integer } from '../domain/validation.js';
import { ModelConfiguration } from '../runtime/pi/model-config.js';
import { exportBook } from '../export/exporter.js';
import { diagnoseBook, renderDiagnostics, exportDiagnostics } from '../diagnostics/diagnose.js';
import { Discussion, decodeDiscussionSnapshot, type CoCreateReply } from './discussion.js';
import { decodeStartIntent, proposeTitles, type BookStartIntent } from './naming.js';
import { validateBookTitle } from './library.js';
import { BookHost, createPiRunner, type RoleRunner, type HostEvent } from './host.js';
import { decideIntervention, decodeIntervention, type InterventionBasis, type InterventionResult } from './intervention.js';
import { AppSessions, type SessionFactory, type AppSessionEvent } from './sessions.js';
import { ActivityTimeline } from './activity.js';
import { editSettings, modelFormActions, type RequestInput, type RequestChoice } from './settings.js';
import type { ModelFormActions } from '../ui/model-settings.js';
import type { EffectiveModel } from '../runtime/pi/model-config.js';
import { BookLifetime, EventQueue } from './lifetime.js';
import { MutationQueue } from '../store/io.js';
import type { NovelView } from '../ui/novel-tui.js';

export interface ApplicationUI {
  update(view: NovelView): void; append(role: string, text: string): void; appendDelta(text: string): void;
  requestInput: RequestInput; close(): Promise<void>;
  requestChoice?: RequestChoice;
  requestModelSettings?: (actions: ModelFormActions) => Promise<EffectiveModel | null>;
  cancelInput?(): void;
}
interface FeedbackEntry { id: string; original: string; status: 'received' | 'decided' | 'applied'; result: InterventionResult | null }
function decodeEntries(value: unknown): FeedbackEntry[] {
  return list(value, (item) => {
    const v = object(item); let result: InterventionResult | null = null;
    if (v.result !== null) {
      const r = object(v.result); const b = object(r.basis);
      const basis: InterventionBasis = { phase: text(b.phase), completedChapters: integer(b.completedChapters),
        ...(typeof b.active === 'string' ? { active: b.active } : {}), ...(typeof b.rewriteState === 'string' ? { rewriteState: b.rewriteState } : {}) };
      const original = text(r.original); result = { original, basis, decision: decodeIntervention(JSON.stringify(r.decision), { original, basis }) };
    }
    return { id: text(v.id), original: text(v.original), status: choice(v.status, ['received', 'decided', 'applied']), result };
  });
}
interface Control { pauseAfterRewrites: boolean; stopAfter: number | null }
type ApplicationEvent = AppSessionEvent & { refresh?: boolean; task?: HostEvent };
function decodeControl(value: unknown): Control {
  const v = object(value);
  if (typeof v.pauseAfterRewrites !== 'boolean') throw new Error('运行意图损坏');
  return { pauseAfterRewrites: v.pauseAfterRewrites, stopAfter: v.stopAfter === null ? null : integer(v.stopAfter, 1) };
}

/** Coordinate UI commands and the sole book Host. Model work never holds a store transaction. */
export class NovelApplication {
  readonly host: BookHost;
  readonly models: ModelConfiguration;
  private readonly sessions: AppSessions;
  private readonly revisions: RevisionService;
  private readonly commands = new MutationQueue();
  private readonly feedback = new MutationQueue();
  private readonly lifetime: BookLifetime<ApplicationEvent>;
  private readonly shutdown = new AbortController();
  private discussion: Discussion | undefined;
  private discussionState: CoCreateReply | undefined;
  private userMessages: string[] = [];
  private stage = false;
  private busy: Promise<unknown> | undefined;
  private phase = 'discussion';
  private lastError = '';
  private configuredModel = '未配置模型';
  private readonly activity = new ActivityTimeline();
  private activeCommand: AbortController | undefined;
  private closing = false;

  private readonly bookTitle: string | undefined;
  private readonly onNameChosen: ((intent: BookStartIntent) => void) | undefined;
  constructor(readonly store: BookStore, private readonly ui: ApplicationUI, options: { configDir?: string; sessionFactory?: SessionFactory; runner?: RoleRunner; bookTitle?: string; onNameChosen?: (intent: BookStartIntent) => void } = {}) {
    this.bookTitle = options.bookTitle;
    this.onNameChosen = options.onNameChosen;
    this.models = new ModelConfiguration(store.files.root, options.configDir);
    const events = new EventQueue<ApplicationEvent>(async (event) => {
      if (event.refresh) { await this.refresh(); return; }
      if (event.task && ['task_start', 'task_end'].includes(event.task.type)) { this.activity.recordTask(event.task); await this.refresh(); return; }
      if (event.tool) { this.activity.recordTool(event.tool); await this.refresh(); return; }
      if (!event.delta && ['工具', '用量', 'planner', 'writer', 'editor', 'host'].includes(event.role)) {
        this.activity.recordNote(event.role, event.text);
        if (event.role === '用量') await this.sessions.saveUsage(event.role, event.text);
        await this.refresh(); return;
      }
      if (event.delta) ui.appendDelta(event.text); else ui.append(event.role, event.text);
    });
    this.sessions = new AppSessions(store.files.root, this.models, store.files, events, options.sessionFactory);
    this.revisions = new RevisionService(store);
    this.lifetime = new BookLifetime(events, async () => { await this.sessions.close(); await ui.close(); }, () => store.close());
    const runner = options.runner ?? createPiRunner((role, tools, request) => this.sessions.make(role, tools, `本次任务与权限：${JSON.stringify(request.instruction)}`));
    this.host = new BookHost(store, runner, { onEvent: (event) => {
      events.enqueue({ role: event.role, text: event.reason, task: event });
      if (event.type === 'task_start') { this.phase = event.role === 'writer' ? 'writing' : event.role === 'planner' ? 'planning' : 'reviewing'; }
    } });
    this.host.setBoundaryHandler(async () => {
      await this.feedback.run(() => this.processFeedback(this.shutdown.signal));
      const control = await this.control();
      const p = (await this.store.snapshot()).progress;
      if ((control.stopAfter !== null && p.completed.length >= control.stopAfter) || (control.pauseAfterRewrites && !p.rewrites.length)) {
        await this.saveControl({ pauseAfterRewrites: false, stopAfter: null }); this.host.pause();
      }
    });
  }
  private say(role: string, message: string) { this.lifetime.events.enqueue({ role, text: message }); }
  private async refresh() {
    if (this.closing) return;
    const snapshot = await this.host.snapshot();
    const display = this.sessions.displaySnapshot();
    const activeChapter = snapshot.progress.active?.chapter;
    this.ui.update({ phase: this.stage ? 'stage-discussion' : this.phase, completedChapters: snapshot.progress.completed.length,
      bookTitle: snapshot.planning.foundation?.title ?? this.bookTitle ?? '未命名作品', modelLabel: display.modelLabel ?? this.configuredModel,
      plannedChapters: snapshot.planning.chapters.length,
      wordCount: snapshot.records.reduce((total, record) => total + [...record.content].length, 0),
      ...(activeChapter === undefined ? {} : { currentChapter: activeChapter }),
      roleLabel: { planning: '规划', writing: '写作', reviewing: '评审', discussion: '讨论' }[this.phase] ?? '待命',
      chapters: snapshot.planning.chapters.map((chapter) => ({ number: chapter.chapter, title: chapter.title,
        status: chapter.chapter === activeChapter ? 'running' : snapshot.progress.completed.includes(chapter.chapter) ? 'complete' : 'planned' })),
      characters: snapshot.planning.foundation?.characters.split(/\r?\n/).filter(Boolean) ?? [],
      world: snapshot.planning.foundation?.world ?? '', usage: display.usage,
      events: this.activity.lines(),
      draft: this.discussion?.snapshot.draft ?? this.discussionState?.draft ?? '', error: this.lastError,
      status: this.stage ? '阶段共创' : { discussion: '开书讨论', writing: '写作中', planning: '规划中', reviewing: '评审中', paused: '已暂停', failed: '失败', complete: '已完本' }[this.phase] ?? this.phase });
  }
  async initialize() {
    try { this.configuredModel = (await this.models.resolve('default')).profile.id; } catch { /* Setup remains accessible. */ }
    await this.revisions.resume();
    const state = await this.host.snapshot();
    if (state.progress.phase === 'discussion') {
      const saved = await this.store.files.json('meta/discussion.json', (value) => {
        const v = object(value); if (v.version !== 1) throw new Error('讨论草稿版本不受支持');
        return { state: decodeDiscussionSnapshot(v.state), userMessages: list(v.userMessages, (item) => text(item)) };
      });
      if (saved) { this.discussionState = saved.state; this.userMessages = saved.userMessages; }
    }
    const intent = await this.store.files.json('meta/start-intent.json', decodeStartIntent);
    if (intent) {
      if (this.onNameChosen) { this.onNameChosen(intent); return; }
      await this.beginPrepared(intent.draft);
      return;
    }
    this.phase = state.progress.phase === 'complete' ? 'complete' : state.progress.phase === 'discussion' ? 'discussion' : 'paused';
    await this.refresh();
    if (state.progress.phase !== 'discussion') await this.resume();
    else {
      this.say('提示', this.discussionState?.draft ? '已恢复上次的共创草稿，可以继续讨论或开始创作。' : '直接聊人物、世界观和故事方向，书名可以之后再定。尚未配置模型时，点击底部「设置」。');
      if (this.discussionState?.reply) this.say('上次讨论', this.discussionState.reply);
    }
  }
  command(input: string): Promise<void> {
    if (input.trim() === '/pause') {
      this.host.pause(); this.activeCommand?.abort();
      return this.lifetime.run(async () => { await this.busy; this.phase = 'paused'; await this.refresh(); });
    }
    return this.lifetime.run((signal) => this.commands.run(async () => {
      this.lastError = '';
      const controller = new AbortController(); this.activeCommand = controller;
      const abort = () => controller.abort(); signal.addEventListener('abort', abort, { once: true });
      if (signal.aborted) controller.abort();
      try { await this.dispatch(input.trim(), controller.signal); }
      catch (error) {
        if (controller.signal.aborted || (error instanceof Error && error.message === '已取消模型配置')) {
          this.lastError = ''; this.say('提示', '已取消操作，原有内容保持不变。');
        } else { this.lastError = error instanceof Error ? error.message : String(error); this.say('提示', this.lastError); }
      }
      finally { signal.removeEventListener('abort', abort); this.activeCommand = undefined; await this.refresh(); }
    }));
  }
  private async dispatch(input: string, signal: AbortSignal) {
    if (!input) return;
    if (input === '/settings') {
      let effective: EffectiveModel;
      if (this.ui.requestModelSettings) {
        const abort = () => this.ui.cancelInput?.();
        signal.addEventListener('abort', abort, { once: true });
        try {
          signal.throwIfAborted();
          const saved = await this.ui.requestModelSettings(modelFormActions(this.models));
          if (!saved) return;
          effective = saved;
        } finally { signal.removeEventListener('abort', abort); }
      } else effective = await editSettings(this.models, (label, options) => new Promise<string | null>((resolve, reject) => {
        if (signal.aborted) { resolve(null); return; }
        const abort = () => { this.ui.cancelInput?.(); resolve(null); };
        signal.addEventListener('abort', abort, { once: true });
        void this.ui.requestInput(label, options).then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
      }), this.ui.requestChoice ? (label, choices, initial) => new Promise<string | null>((resolve, reject) => {
        if (signal.aborted) { resolve(null); return; }
        const abort = () => { this.ui.cancelInput?.(); resolve(null); };
        signal.addEventListener('abort', abort, { once: true });
        void this.ui.requestChoice!(label, choices, initial).then(resolve, reject).finally(() => signal.removeEventListener('abort', abort));
      }) : undefined);
      await this.sessions.refreshModels();
      try { this.configuredModel = (await this.models.resolve('default')).profile.id; } catch { /* Default still needs setup. */ }
      const roleName = { default: '默认', planner: '规划', writer: '写作', editor: '评审', discussion: '讨论', arbiter: '意见判断' }[effective.role];
      this.say('模型', `${roleName}：${effective.profile.id}（${effective.source === 'book' ? '本书设置' : '全局设置'}）已保存，对后续请求生效。`); return;
    }
    if (input === '/start') {
      if (this.stage) throw new Error('阶段共创请使用 /apply');
      const draft = this.discussion?.buildPrompt() ?? this.discussionState?.draft;
      if (!draft?.trim()) throw new Error('请先讨论并形成创作要求');
      if ((await this.store.snapshot()).progress.phase !== 'discussion') throw new Error('本书已经开始创作');
      if (this.onNameChosen && !this.bookTitle) {
        const title = await this.chooseTitle(draft, signal);
        if (!title) return;
        const intent: BookStartIntent = { version: 1, title, draft: `书名：${title}\n${draft}` };
        await this.store.transaction((files) => files.writeJSON('meta/start-intent.json', intent));
        this.onNameChosen(intent); return;
      }
      const prepared = `${this.bookTitle ? `书名：${this.bookTitle}\n` : ''}${draft}`;
      await this.beginPrepared(prepared); return;
    }
    if (input === '/resume') { if (this.stage) throw new Error('请先应用或取消阶段共创'); await this.saveControl({ pauseAfterRewrites: false, stopAfter: null }); await this.resume(); return; }
    if (input === '/cocreate' || input === '/plan') {
      if (this.stage) throw new Error('已处于阶段共创');
      if ((await this.host.snapshot()).progress.phase !== 'writing') throw new Error('只有已开始、未完本的作品可以进入阶段共创');
      await this.pauseAndWait();
      const state = await this.host.snapshot();
      if (state.progress.phase !== 'writing') throw new Error('作品已完本，不能进入阶段共创');
      this.stage = true; this.discussionState = undefined; this.discussion?.session.dispose();
      const stageContext = `本次讨论后续方向，不直接改稿。当前作品：${JSON.stringify({ foundation: state.planning.foundation, chapters: state.records.map((r) => ({ chapter: r.chapter, summary: r.facts.summary })) })}`;
      this.discussion = new Discussion(await this.sessions.make('discussion', [], () => `${stageContext}\n当前完整方向草稿（恢复依据）：\n${this.discussion?.snapshot.draft ?? ''}`));
      this.say('提示', '已暂停写作。讨论后续方向后选择「应用方向」，或在操作菜单中选择「取消讨论」。'); return;
    }
    if (input === '/cancel') {
      if (!this.stage) throw new Error('当前没有阶段共创');
      this.discussion?.session.dispose(); this.discussion = undefined; this.stage = false; this.phase = 'paused'; return;
    }
    if (input === '/apply') {
      if (!this.stage || !this.discussion) throw new Error('当前没有阶段共创草稿');
      const draft = this.discussion.buildPrompt();
      await this.pauseAndWait();
      await this.receiveFeedback(`${draft}\n请按以上完整方向调整，改完继续。`, signal);
      this.stage = false; this.discussion.session.dispose(); this.discussion = undefined;
      await this.resume(); return;
    }
    if (input === '/sync' || input === '/sync --check') {
      if (this.stage) throw new Error('阶段共创期间不能同步');
      if (input.endsWith('--check')) { const changes = await this.revisions.check(); this.say('同步检查', changes.length ? `变更章节：${changes.map((x) => x.chapter).join('、')}` : '没有未接纳的正文变更'); return; }
      await this.pauseAndWait();
      const changed = await this.revisions.sync(async (change, cancel) => {
        const session = await this.sessions.make('editor', [], '只分析手动改稿，不重写用户正文。输出 JSON {facts:{title,summary,characters,keyEvents,timeline:[{time,event}],stateChanges:[{subject,state}],relationships:[{from,to,relation}],foreshadows:[{id,action,description}]},style:[],feedback:[]}。style 仅记录明确风格偏好；普通错别字不增加偏好。');
        try {
          const response = await this.modelText(session, JSON.stringify({ chapter: change.chapter, before: change.base, content: change.content }), cancel);
          return decodeAnalysis(JSON.parse(response));
        } finally { session.dispose(); }
      }, signal);
      this.say('同步', changed.length ? `已接纳第 ${changed.join('、')} 章，用户正文保持原样。` : '无变更'); return;
    }
    if (input.startsWith('/export')) {
      const match = /^\/export(?:\s+(txt|epub))?(?:\s+(\d+)-(\d+))?(?:\s+(--overwrite))?$/.exec(input);
      if (!match) throw new Error('用法：/export [txt|epub] [1-3] [--overwrite]');
      await this.pauseAndWait(); const state = await this.host.snapshot();
      const format = match[1] === 'epub' ? 'epub' : 'txt'; const title = state.planning.foundation?.title ?? '未命名作品';
      const result = await exportBook(this.store.files, { output: join(this.store.files.root, 'exports', `novel.${format}`), title, format,
        ...(match[2] ? { from: Number(match[2]), to: Number(match[3]) } : {}), overwrite: !!match[4] });
      this.say('导出', `${result.path}\n已导出：${result.chapters.join('、')}；跳过：${result.skipped.join('、') || '无'}`); return;
    }
    if (input === '/diag' || input === '/diag --export') {
      const report = await diagnoseBook(this.store.files); this.say('诊断', renderDiagnostics(report));
      if (input.endsWith('--export')) { const output = join(this.store.files.root, 'exports', `diagnostic-${Date.now()}.txt`); await exportDiagnostics(report, output); this.say('诊断报告', output); } return;
    }
    if (input.startsWith('/')) throw new Error('未知命令，使用 /help 查看操作');
    this.say('你', input);
    const state = await this.store.snapshot();
    if (state.progress.phase === 'discussion' || this.stage) {
      if (!this.stage) { this.userMessages.push(input); await this.saveDiscussion(); }
      this.discussion ??= new Discussion(await this.sessions.make('discussion', [], () => `开书讨论：先确定人物、世界观和设定，书名非必填；用户明确开始前不写正文。\n当前完整创作要求草稿（恢复依据）：\n${this.discussion?.snapshot.draft ?? this.discussionState?.draft ?? ''}\n最近用户意见：${JSON.stringify(this.userMessages.slice(-6))}`), this.discussionState);
      const result = await this.discussion.submit(input, signal); this.say('助手', result.reply);
      if (!this.stage) { this.discussionState = result; await this.saveDiscussion(); }
      if (result.suggestions.length) this.say('讨论建议', result.suggestions.join('\n'));
      if (result.ready) this.say('提示', this.stage ? '方向草稿已准备好，可继续讨论或选择「应用方向」。' : '已有开书草稿，可继续讨论或选择「开始创作」。');
    } else await this.receiveFeedback(input, signal);
  }
  private async saveDiscussion() {
    const state = this.discussion?.snapshot ?? this.discussionState ?? { reply: '', draft: '', ready: false, suggestions: [] };
    await this.store.transaction((files) => files.writeJSON('meta/discussion.json', { version: 1, state, userMessages: this.userMessages }));
  }
  private async beginPrepared(draft: string) {
    const phase = (await this.store.snapshot()).progress.phase;
    if (phase === 'discussion') await this.host.start(draft);
    else if (await this.store.files.read('meta/requirements.md') !== draft) throw new Error('启动意图与作品要求不一致');
    await this.store.transaction((files) => files.remove('meta/start-intent.json'));
    this.discussion?.session.dispose(); this.discussion = undefined; this.discussionState = undefined;
    await this.resume();
  }
  private async chooseTitle(draft: string, signal: AbortSignal): Promise<string | null> {
    const session = await this.sessions.make('arbiter', [], '根据已形成的小说设定生成书名，不生成正文。');
    const proposal = await proposeTitles(session, draft, this.userMessages, signal).finally(() => session.dispose());
    if (proposal.userTitle) return proposal.userTitle;
    const recommended = proposal.candidates[0]!;
    if (!this.ui.requestChoice) return recommended;
    const abort = () => this.ui.cancelInput?.();
    signal.addEventListener('abort', abort, { once: true });
    try {
      signal.throwIfAborted();
      const selected = await this.ui.requestChoice('给这个故事定一个书名', [
        { label: `直接采用推荐：${recommended}`, value: 'candidate:0', description: '无需再起名，继续创作' },
        ...proposal.candidates.slice(1).map((title, index) => ({ label: title, value: `candidate:${index + 1}` })),
        { label: '自己填写书名', value: '__custom__' }, { label: '返回讨论，稍后再定', value: '__back__' },
      ], 'candidate:0');
      signal.throwIfAborted();
      if (!selected || selected === '__back__') return null;
      if (selected === '__custom__') {
        const entered = await this.ui.requestInput('书名（Esc 返回讨论）');
        signal.throwIfAborted(); return entered === null ? null : validateBookTitle(entered.trim());
      }
      const index = /^candidate:(\d+)$/.exec(selected);
      const title = index ? proposal.candidates[Number(index[1])] : undefined;
      if (!title) throw new Error('无效的书名选择');
      return validateBookTitle(title);
    } finally { signal.removeEventListener('abort', abort); }
  }
  private async modelText(session: AgentSession, prompt: string, signal: AbortSignal): Promise<string> {
    let final: string | undefined;
    const unsubscribe = session.subscribe((event) => {
      if (event.type === 'message_end' && event.message.role === 'assistant') final = event.message.stopReason === 'stop' ? event.message.content.filter((x) => x.type === 'text').map((x) => x.text).join('') : undefined;
    });
    const abort = () => { void session.abort().catch(() => undefined); };
    signal.addEventListener('abort', abort, { once: true });
    try { signal.throwIfAborted(); await session.prompt(prompt); signal.throwIfAborted(); if (!final) throw new Error('模型未正常返回完整分析'); return final; }
    finally { signal.removeEventListener('abort', abort); unsubscribe(); }
  }
  private async basis(): Promise<InterventionBasis> {
    const { progress } = await this.store.snapshot();
    return { phase: progress.phase, completedChapters: progress.completed.length, active: JSON.stringify(progress.active), rewriteState: JSON.stringify({ rewrites: progress.rewrites, revision: progress.revision }) };
  }
  private async receiveFeedback(original: string, signal: AbortSignal) {
    await this.store.transaction(async (files) => {
      const entries = await files.json('meta/interventions.json', decodeEntries) ?? [];
      entries.push({ id: randomUUID(), original, status: 'received', result: null }); await files.writeJSON('meta/interventions.json', entries);
    });
    this.say('意见', '已接收，正在判断处理范围。');
    await this.feedback.run(() => this.processFeedback(signal, !!this.busy));
    if (!this.busy && !this.stage) {
      const snapshot = await this.store.snapshot(); if (snapshot.progress.rewrites.length) await this.resume();
    }
  }
  private async processFeedback(signal = new AbortController().signal, deferControl = false) {
    signal.throwIfAborted();
    const entries = await this.store.files.json('meta/interventions.json', decodeEntries) ?? [];
    for (const entry of entries.filter((e) => e.status !== 'applied')) {
      signal.throwIfAborted();
      const basis = await this.basis();
      if (!entry.result || JSON.stringify(entry.result.basis) !== JSON.stringify(basis)) {
        const session = await this.sessions.make('arbiter');
        try { entry.result = await decideIntervention(session, { original: entry.original, basis }, signal); }
        finally { session.dispose(); }
        entry.status = 'decided'; await this.saveFeedback(entry);
      }
      const decision = entry.result.decision;
      if (deferControl && !['query', 'rule', 'clarify', 'unsupported'].includes(decision.kind)) { this.say('意见', '范围已判断，将在当前任务结束后应用。'); continue; }
      if (decision.kind === 'rule') {
        await this.store.transaction(async (files) => { const existing = await files.read('meta/rules.md') ?? ''; if (!existing.includes(`<!-- ${entry.id} -->`)) await files.write('meta/rules.md', `${existing}\n<!-- ${entry.id} -->\n${decision.rules}\n`); });
      } else if (decision.kind === 'plan') {
        await this.store.transaction(async (files) => { const old = await files.json('meta/planning_feedback.json', (v) => list(v, (x) => text(x))) ?? []; if (!old.includes(decision.text)) await files.writeJSON('meta/planning_feedback.json', [...old, decision.text]); });
      } else if (decision.kind === 'rewrite') {
        await this.host.requestRewrites(decision.chapters, `${decision.text}\n用户原文：${entry.original}`);
        const control = await this.control(); await this.saveControl({ ...control, pauseAfterRewrites: !decision.resume });
      } else if (decision.kind === 'pause') {
        const control = await this.control(); await this.saveControl({ ...control, stopAfter: decision.stopAfter });
        if (decision.stopAfter === null) this.host.pause();
      }
      entry.status = 'applied'; await this.saveFeedback(entry); this.say('意见', decision.reply);
    }
  }
  private async saveFeedback(entry: FeedbackEntry) {
    await this.store.transaction(async (files) => {
      const entries = await files.json('meta/interventions.json', decodeEntries) ?? [];
      const index = entries.findIndex((e) => e.id === entry.id); if (index < 0) throw new Error('意见记录丢失');
      entries[index] = entry; await files.writeJSON('meta/interventions.json', entries);
    });
  }
  private async control(): Promise<Control> { return await this.store.files.json('meta/control.json', decodeControl) ?? { pauseAfterRewrites: false, stopAfter: null }; }
  private async saveControl(value: Control) { await this.store.transaction((files) => files.writeJSON('meta/control.json', value)); }
  private async resume() {
    if (this.busy) return;
    const changes = await this.revisions.check(); if (changes.length) throw new Error('正文有未接纳的修改，请先 /sync');
    this.phase = 'writing';
    this.busy = this.lifetime.run(async (signal) => {
      try {
        const result = await this.host.run(signal);
        this.phase = result.status === 'incomplete' ? 'failed' : result.status;
        this.say('运行', result.reason); if (result.status === 'failed' || result.status === 'incomplete') this.lastError = result.reason;
      } finally { this.busy = undefined; await this.refresh(); }
    });
    void this.busy.catch((error: unknown) => { this.lastError = String(error); });
  }
  private async pauseAndWait() { this.host.pause(); await this.busy; this.phase = (await this.store.snapshot()).progress.phase === 'complete' ? 'complete' : 'paused'; }
  async idle() { await this.commands.run(async () => undefined); await this.busy; }
  async close() {
    this.closing = true; this.shutdown.abort(); this.host.pause(); this.activeCommand?.abort(); this.ui.cancelInput?.();
    await this.lifetime.close();
  }
}
