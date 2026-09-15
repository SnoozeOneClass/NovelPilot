import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import type { AgentSession, ToolDefinition } from '@earendil-works/pi-coding-agent';
import { ModelConfiguration, type ModelRole } from '../runtime/pi/model-config.js';
import { createRoleSession } from '../runtime/pi/session.js';
import { SkillCatalog } from '../skills/catalog.js';
import { discussionPreview } from './discussion.js';
import { EventQueue } from './lifetime.js';
import type { BookFiles } from '../store/files.js';
import type { ToolActivity } from './activity.js';

export type SessionFactory = (role: ModelRole, tools: ToolDefinition[], anchor: string) => Promise<AgentSession>;
export interface AppSessionEvent { role: string; text: string; delta?: boolean; tool?: ToolActivity }
export interface SessionDisplay {
  modelLabel?: string;
  usage: { input?: number; output?: number; cacheRead?: number; cacheWrite?: number; contextUsed?: number; contextWindow?: number; cost?: number };
}
export class AppSessions {
  private display: SessionDisplay = { usage: {} };
  private unknownCost = false;
  displaySnapshot(): SessionDisplay { return structuredClone(this.display); }
  private readonly active = new Map<AgentSession, { role: ModelRole; pricingKnown: boolean }>();
  constructor(private readonly bookDir: string, private readonly config: ModelConfiguration, private readonly files: BookFiles,
    private readonly events: EventQueue<AppSessionEvent>, private readonly injected?: SessionFactory) {}
  async make(role: ModelRole, tools: ToolDefinition[] = [], anchor: string | (() => string) = ''): Promise<AgentSession> {
    const currentAnchor = typeof anchor === 'function' ? anchor : () => anchor;
    if (this.injected) {
      const session = await this.injected(role, tools, currentAnchor());
      this.track(session, role, false);
      return session;
    }
    const skillDir = fileURLToPath(new URL('../../assets/skills/', import.meta.url));
    const catalog = await SkillCatalog.discover(skillDir);
    const promptName = ['planner', 'writer', 'editor', 'discussion'].includes(role) ? role : 'base';
    const prompt = await readFile(new URL(`../../assets/prompts/${promptName}.md`, import.meta.url), 'utf8');
    const configured = await this.config.buildModelRuntime(role);
    const session = await createRoleSession({ bookDir: this.bookDir, systemPrompt: `${prompt}\n${role === 'arbiter' ? '' : catalog.prompt()}`,
      modelRuntime: configured.modelRuntime, model: configured.model, tools: role === 'arbiter' ? [] : [...tools, ...catalog.tools()],
      contextAnchor: () => `${currentAnchor()}\n${catalog.loadedResources().map((x) => x.content).join('\n')}` });
    session.setThinkingLevel(configured.thinkingLevel);
    this.track(session, role, configured.pricing !== null);
    return session;
  }
  private track(session: AgentSession, role: ModelRole, pricingKnown: boolean) {
    const info = { role, pricingKnown };
    this.active.set(session, info);
    let requestPricingKnown = pricingKnown;
    const stream = session.agent.streamFunction;
    session.agent.streamFunction = (model, context, options) => {
      requestPricingKnown = info.pricingKnown;
      this.display.modelLabel = model.name || model.id;
      this.display.usage.contextWindow = model.contextWindow;
      return stream(model, context, options);
    };
    let streamed = ''; let visible = '';
    const unsubscribe = session.subscribe((event) => {
      if (event.type === 'message_start' && event.message.role === 'assistant') { streamed = ''; visible = ''; }
      if (event.type === 'message_update' && event.assistantMessageEvent.type === 'text_delta') {
        streamed += event.assistantMessageEvent.delta;
        if (role === 'discussion') {
          const preview = discussionPreview(streamed);
          if (preview.startsWith(visible)) this.events.enqueue({ role, text: preview.slice(visible.length), delta: true });
          visible = preview;
        } else if (role !== 'arbiter') this.events.enqueue({ role, text: event.assistantMessageEvent.delta, delta: true });
      }
      if (event.type === 'tool_execution_start' || event.type === 'tool_execution_end') this.events.enqueue({
        role: '工具', text: event.toolName,
        tool: { id: event.toolCallId, name: event.toolName, role, at: Date.now(),
          status: event.type === 'tool_execution_start' ? 'running' : event.isError ? 'failed' : 'completed' },
      });
      if (event.type === 'message_end' && event.message.role === 'assistant') {
        const message = event.message;
        const usage = message.usage;
        if (usage.input + usage.output + usage.cacheRead + usage.cacheWrite > 0) {
          this.display.usage.input = (this.display.usage.input ?? 0) + usage.input;
          this.display.usage.output = (this.display.usage.output ?? 0) + usage.output;
          this.display.usage.cacheRead = (this.display.usage.cacheRead ?? 0) + usage.cacheRead;
          this.display.usage.cacheWrite = (this.display.usage.cacheWrite ?? 0) + usage.cacheWrite;
          this.unknownCost ||= !requestPricingKnown;
          if (!this.unknownCost) this.display.usage.cost = (this.display.usage.cost ?? 0) + usage.cost.total;
          else delete this.display.usage.cost;
        }
        const context = session.getContextUsage();
        if (context?.tokens !== null && context?.tokens !== undefined) this.display.usage.contextUsed = context.tokens;
        else delete this.display.usage.contextUsed;
        this.events.enqueue({ role: '用量', text: `${message.model} · 输入 ${message.usage.input} / 输出 ${message.usage.output} token · 费用${requestPricingKnown ? ` ${message.usage.cost.total}` : '未知'}` });
      }
    });
    const dispose = session.dispose.bind(session);
    session.dispose = () => { unsubscribe(); this.active.delete(session); dispose(); };
  }
  async refreshModels() {
    for (const [session, info] of this.active) {
      const configured = await this.config.configureRuntime(session.modelRuntime, info.role);
      await session.setModel(configured.model);
      session.setThinkingLevel(configured.thinkingLevel);
      info.pricingKnown = configured.pricing !== null;
    }
  }
  async close() {
    for (const session of this.active.keys()) await session.abort();
    for (const session of [...this.active.keys()]) { await session.agent.waitForIdle(); session.dispose(); }
  }
  /** Persist only numeric usage / public model metadata; no prompts, thinking or credentials. */
  async saveUsage(role: string, text: string) {
    await this.files.append('meta/usage.jsonl', { type: 'usage', role, text, time: new Date().toISOString() });
  }
}
