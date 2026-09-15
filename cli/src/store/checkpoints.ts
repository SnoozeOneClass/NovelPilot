import { decodeReceipt, type CommitReceipt } from '../domain/book.js';
import { DataError, integer, object, text } from '../domain/validation.js';
import type { BookFiles } from './files.js';

export interface Checkpoint { seq: number; scope: string; step: string; digest: string; receipt: CommitReceipt | null }
function decodeCheckpoint(value: unknown): Checkpoint {
  const v = object(value);
  const cp = { seq: integer(v.seq, 1), scope: text(v.scope), step: text(v.step), digest: text(v.digest), receipt: v.receipt === null ? null : decodeReceipt(v.receipt) };
  if (cp.step === 'commit_chapter' && (!cp.receipt || cp.scope !== `chapter:${cp.receipt.chapter}` || cp.digest !== cp.receipt.digest)) throw new DataError('提交检查点载荷无效');
  return cp;
}
export async function readCheckpoints(files: BookFiles): Promise<Checkpoint[]> {
  const content = await files.read('meta/checkpoints.jsonl');
  if (content === null) return [];
  const lines = content.split('\n');
  if (lines.at(-1) === '') lines.pop();
  return lines.map((line, i) => {
    try {
      const cp = decodeCheckpoint(JSON.parse(line));
      if (cp.seq !== i + 1) throw new DataError('检查点序号无效');
      return cp;
    } catch (error) { throw new DataError(`检查点第 ${i + 1} 行损坏：${String(error)}`); }
  });
}
/** Caller owns the book mutation queue. Append succeeds before returning new evidence. */
export async function appendCheckpoint(files: BookFiles, entry: Omit<Checkpoint, 'seq'>): Promise<Checkpoint> {
  const checkpoints = await readCheckpoints(files);
  const next = decodeCheckpoint({ ...entry, seq: checkpoints.length + 1 });
  const existing = checkpoints.find((cp) => cp.scope === entry.scope && cp.step === entry.step && cp.digest === entry.digest);
  if (existing) {
    if (JSON.stringify(existing.receipt) !== JSON.stringify(next.receipt)) throw new DataError('检查点幂等键对应不同载荷');
    return existing;
  }
  await files.append('meta/checkpoints.jsonl', next);
  return next;
}
