import type { AgentSession } from '@earendil-works/pi-coding-agent';
import type { AssistantMessage } from '@earendil-works/pi-ai';
import { object, strings, text } from '../domain/validation.js';

export interface CoCreateReply {
  reply: string;
  draft: string;
  ready: boolean;
  suggestions: string[];
}
export function decodeDiscussionSnapshot(value: unknown): CoCreateReply {
  const v = object(value);
  if (typeof v.ready !== 'boolean') throw new Error('讨论草稿状态损坏');
  return { reply: text(v.reply, true), draft: text(v.draft, true), ready: v.ready, suggestions: strings(v.suggestions) };
}

/** Strict complete-message protocol; malformed output never becomes visible prose. */
export function decodeDiscussion(text: string): CoCreateReply {
  const match = /^\s*<reply>([\s\S]*?)<\/reply>\s*<draft>([\s\S]*?)<\/draft>\s*<ready>(true|false)<\/ready>\s*<suggestions>([\s\S]*?)<\/suggestions>\s*$/.exec(text);
  if (!match || !match[1]?.trim()) throw new Error('共创回复格式不完整，请重试；原有草稿已保留');
  const fields = [match[1], match[2] ?? '', match[4] ?? ''];
  if (fields.some((field) => /<\/?(?:reply|draft|ready|suggestions)\b/i.test(field))) {
    throw new Error('共创回复包含重复协议标签');
  }
  return {
    reply: match[1].trim(), draft: (match[2] ?? '').trim(), ready: match[3] === 'true',
    suggestions: (match[4] ?? '').split('\n').map((line) => line.trim().replace(/^(?:[-*]|\d+\.)\s+/, ''))
      .filter((line) => [...line].length >= 2).slice(0, 3),
  };
}

/** Return only a safe reply preview, excluding incomplete tags and other protocol fields. */
export function discussionPreview(text: string): string {
  const start = text.indexOf('<reply>');
  if (start < 0) return '';
  return text.slice(start + 7).split('<', 1)[0]?.trim() ?? '';
}

export class Discussion {
  private tail: Promise<unknown> = Promise.resolve();
  private state: CoCreateReply = { reply: '', draft: '', ready: false, suggestions: [] };
  constructor(readonly session: AgentSession, initial?: CoCreateReply) { if (initial) this.state = decodeDiscussionSnapshot(initial); }

  get snapshot(): CoCreateReply { return { ...this.state, suggestions: [...this.state.suggestions] }; }
  get canStart(): boolean { return this.state.draft.trim().length > 0; }
  buildPrompt(): string {
    if (!this.canStart) throw new Error('请先讨论并形成创作要求草稿');
    return this.state.draft;
  }

  /** Own this session exclusively. Serial submission avoids Pi's early-return streaming queue. */
  submit(input: string, signal?: AbortSignal): Promise<CoCreateReply> {
    const operation = this.tail.then(async () => {
      if (!input.trim()) throw new Error('讨论内容不能为空');
      signal?.throwIfAborted();
      if (this.session.isStreaming) throw new Error('讨论会话已被其他操作占用');
      this.state = { ...this.state, suggestions: [], ready: false };
      let final: AssistantMessage | undefined;
      const unsubscribe = this.session.subscribe((event) => {
        if (event.type === 'message_end' && event.message.role === 'assistant') final = event.message;
      });
      const abort = () => { void this.session.abort().catch(() => undefined); };
      signal?.addEventListener('abort', abort, { once: true });
      try {
        await this.session.prompt(input.trim());
        signal?.throwIfAborted();
        if (!final || final.stopReason !== 'stop') throw new Error('本轮共创未正常完成，原有草稿已保留');
        const decoded = decodeDiscussion(final.content.filter((block) => block.type === 'text').map((block) => block.text).join(''));
        this.state = { ...decoded, draft: decoded.draft || this.state.draft };
        return this.snapshot;
      } finally {
        unsubscribe();
        signal?.removeEventListener('abort', abort);
      }
    });
    this.tail = operation.catch(() => undefined);
    return operation;
  }

  async drain(): Promise<void> { await this.tail; }
}
