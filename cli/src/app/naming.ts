import type { AgentSession } from '@earendil-works/pi-coding-agent';
import type { AssistantMessage } from '@earendil-works/pi-ai';
import { validateBookTitle } from './library.js';
import { object, text, version } from '../domain/validation.js';

export interface BookStartIntent { version: 1; title: string; draft: string }
export function decodeStartIntent(value: unknown): BookStartIntent {
  const v = object(value);
  return { version: version(v.version), title: validateBookTitle(text(v.title)), draft: text(v.draft) };
}
export interface TitleProposal { userTitle: string | null; candidates: string[] }
export function decodeTitleProposal(value: unknown, userMessages: string[]): TitleProposal {
  const v = object(value);
  if (!Array.isArray(v.candidates)) throw new Error('命名结果缺少候选书名');
  const valid = (value: unknown): string | null => {
    if (typeof value !== 'string') return null;
    const title = value.trim().replace(/^《(.+)》$/, '$1');
    try { return validateBookTitle(title); } catch { return null; }
  };
  const candidates = [...new Set(v.candidates.map(valid).filter((v): v is string => v !== null))].slice(0, 5);
  const declared = valid(v.userTitle);
  // Model inference cannot mark a title absent from user input as user-provided.
  const userTitle = declared && userMessages.some((message) => message.includes(declared)) ? declared : null;
  if (!userTitle && !candidates.length) throw new Error('没有可用的书名，请重新生成或自行填写');
  return { userTitle, candidates: userTitle ? [userTitle, ...candidates.filter((t) => t !== userTitle)] : candidates };
}
export async function proposeTitles(session: AgentSession, draft: string, userMessages: string[], signal: AbortSignal): Promise<TitleProposal> {
  let prompt = `根据已讨论的小说设定和大纲方向命名。仅返回 JSON {"userTitle":null,"candidates":["推荐书名","备选一","备选二"]}。
仅当用户明确给出并采用了书名时填写 userTitle；提问、举例或模型草稿中的建议不算用户确定书名。没有明确书名时列出三个简短候选，第一项为推荐。
不要包含书名号、路径符号或文件系统保留名称。不要改动人物设定或生成正文。
用户原话：${JSON.stringify(userMessages)}\n当前完整创作要求：${draft}`;
  const abort = () => { void session.abort().catch(() => undefined); };
  signal.addEventListener('abort', abort, { once: true });
  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      signal.throwIfAborted();
      let final: AssistantMessage | undefined;
      const unsubscribe = session.subscribe((event) => { if (event.type === 'message_end' && event.message.role === 'assistant') final = event.message; });
      try { await session.prompt(prompt); } finally { unsubscribe(); }
      signal.throwIfAborted();
      if (!final || final.stopReason !== 'stop') throw new Error('命名请求未正常完成，讨论草稿已保留');
      try {
        return decodeTitleProposal(JSON.parse(final.content.filter((part) => part.type === 'text').map((part) => part.text).join('')), userMessages);
      } catch (error) {
        if (attempt) throw error;
        prompt = '请修正格式，只返回 userTitle 和非空 candidates 的合法 JSON，书名不能含路径符号。';
      }
    }
    throw new Error('命名失败');
  } finally { signal.removeEventListener('abort', abort); }
}
