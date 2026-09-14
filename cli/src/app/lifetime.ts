/** SDK callbacks enqueue observations synchronously; this class owns their settlement. */
export class EventQueue<T> {
  private tail: Promise<void> = Promise.resolve();
  private sealed = false;
  readonly failures: unknown[] = [];

  constructor(private readonly consume: (event: T) => Promise<void>) {}

  enqueue(event: T): void {
    if (this.sealed) throw new Error('事件队列已关闭');
    this.tail = this.tail.then(() => this.consume(event)).catch((error: unknown) => {
      this.failures.push(error);
    });
  }

  async close(): Promise<void> {
    this.sealed = true;
    await this.tail;
  }
}

/** Own all operations that may emit events or touch book files. */
export class BookLifetime<T> {
  private readonly controller = new AbortController();
  private readonly pending = new Set<Promise<unknown>>();
  private closing: Promise<void> | undefined;
  private accepting = true;

  constructor(
    readonly events: EventQueue<T>,
    private readonly closeOutput: () => Promise<void>,
    private readonly releaseLease: () => Promise<void>,
  ) {}

  run<R>(operation: (signal: AbortSignal) => Promise<R>): Promise<R> {
    if (!this.accepting) return Promise.reject(new Error('正在退出，不能启动新任务'));
    const result = Promise.resolve().then(() => operation(this.controller.signal));
    this.pending.add(result);
    void result.then(() => this.pending.delete(result), () => this.pending.delete(result));
    return result;
  }

  close(): Promise<void> {
    if (this.closing) return this.closing;
    this.accepting = false;
    this.controller.abort();
    this.closing = (async () => {
      await Promise.allSettled([...this.pending]);
      await this.events.close();
      try { await this.closeOutput(); } finally { await this.releaseLease(); }
    })();
    return this.closing;
  }
}
