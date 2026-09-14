import {
  createAgentSession, DefaultResourceLoader, SessionManager, SettingsManager,
  type AgentSession, type CreateAgentSessionOptions, type ToolDefinition,
} from '@earendil-works/pi-coding-agent';
import { join } from 'node:path';

export interface RoleSessionOptions {
  bookDir: string;
  systemPrompt: string;
  modelRuntime: NonNullable<CreateAgentSessionOptions['modelRuntime']>;
  model: NonNullable<CreateAgentSessionOptions['model']>;
  tools: ToolDefinition[];
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
  return session;
}
