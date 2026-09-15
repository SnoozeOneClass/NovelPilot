export class DataError extends Error {
  constructor(message: string) { super(message); this.name = 'DataError'; }
}
export function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new DataError('需要 JSON 对象');
  return value as Record<string, unknown>;
}
export function text(value: unknown, allowEmpty = false): string {
  if (typeof value !== 'string' || (!allowEmpty && !value.trim())) throw new DataError('需要非空文本');
  return value;
}
export function integer(value: unknown, min = 0): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min) throw new DataError(`需要不小于 ${min} 的整数`);
  return value;
}
export function list<T>(value: unknown, decode: (item: unknown) => T): T[] {
  if (!Array.isArray(value)) throw new DataError('需要数组');
  return value.map(decode);
}
export function choice<const T extends readonly string[]>(value: unknown, values: T): T[number] {
  if (typeof value !== 'string' || !values.includes(value)) throw new DataError(`无效选项：${String(value)}`);
  return value as T[number];
}
export const strings = (value: unknown): string[] => list(value, (item) => text(item));
export function version(value: unknown): 1 {
  if (value !== 1) throw new DataError('不支持的文件版本');
  return 1;
}
