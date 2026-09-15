import { readFile, realpath } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

export async function resolveStartupPaths(cwd: string = process.cwd()) {
  const projectDir = await realpath(cwd);
  const basePromptPath = fileURLToPath(new URL('../../assets/prompts/base.md', import.meta.url));
  const basePrompt = await readFile(basePromptPath, 'utf8');
  if (!basePrompt.trim()) throw new Error(`内置提示词为空：${basePromptPath}`);
  return { projectDir, basePromptPath, basePrompt };
}
