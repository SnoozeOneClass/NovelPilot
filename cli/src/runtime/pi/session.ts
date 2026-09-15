import {
  createAgentSession, DefaultResourceLoader, SessionManager, SettingsManager,
  type AgentSession, type CreateAgentSessionOptions, type ToolDefinition,
} from '@earendil-works/pi-coding-agent';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';

export interface RoleSessionOptions {
  bookDir: string;
  systemPrompt: string;
  modelRuntime: NonNullable<CreateAgentSessionOptions['modelRuntime']>;
  model: NonNullable<CreateAgentSessionOptions['model']>;
  tools: ToolDefinition[];
  /** Current task and selected Skill text, rebuilt from app-owned state after compaction. */
  contextAnchor?: () => string;
}

export async function createRoleSession(options: RoleSessionOptions): Promise<AgentSession> {
  if (!options.systemPrompt.trim()) throw new Error('角色提示词不能为空');
  const settingsManager = SettingsManager.inMemory({
    enableAnalytics: false, enableInstallTelemetry: false, enableSkillCommands: false,
    retry: { enabled: false },
  });
  const agentDir = join(options.bookDir, 'meta', 'runtime');
  const loader = new DefaultResourceLoader({
    cwd: options.bookDir, agentDir, settingsManager,
    noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true, noContextFiles: true,
    systemPromptOverride: () => options.systemPrompt,
  });
  await loader.reload();
  const { session } = await createAgentSession({
    cwd: options.bookDir, agentDir, modelRuntime: options.modelRuntime, model: options.model,
    resourceLoader: loader, settingsManager, sessionManager: SessionManager.inMemory(options.bookDir),
    tools: options.tools.map((tool) => tool.name), customTools: options.tools,
  });
  session.agent.toolExecution = 'sequential';
  // Keep the SDK-installed prepareNextTurnWithContext hook (compaction/model/tool refresh).
  if (options.contextAnchor) {
    const anchor = options.contextAnchor;
    const start = `\n\n<novelpilot-context-${randomUUID()}>\n`;
    const end = '\n</novelpilot-context>';
    const withAnchor = (systemPrompt: string): string => {
      const position = systemPrompt.indexOf(start);
      const base = position >= 0 ? systemPrompt.slice(0, position) : systemPrompt;
      const current = anchor();
      if (typeof current !== 'string') throw new Error('任务上下文锚点必须返回文本');
      return current.trim() ? `${base}${start}${current}${end}` : base;
    };
    const sdkRefresh = session.agent.prepareNextTurnWithContext;
    session.agent.prepareNextTurnWithContext = async (turn, signal) => {
      const refreshed = await sdkRefresh?.(turn, signal);
      const context = refreshed?.context ?? turn.context;
      return { ...refreshed, context: { ...context, systemPrompt: withAnchor(context.systemPrompt) } };
    };
    // Pi's next-turn hook is not called before the first assistant response of a prompt.
    // Apply the same projection at the stream boundary; never modify model/tools/history.
    const sdkStream = session.agent.streamFunction;
    session.agent.streamFunction = (model, context, streamOptions) => sdkStream(model, {
      ...context, systemPrompt: withAnchor(context.systemPrompt ?? ''),
    }, streamOptions);
  }
  return session;
}
