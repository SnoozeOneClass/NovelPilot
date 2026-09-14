import assert from 'node:assert/strict';
import { test } from 'node:test';
import { BookLifetime, EventQueue } from '../src/app/lifetime.js';

test('shutdown waits for cancelled work and delayed events before closing output and releasing lease', async () => {
  const order: string[] = [];
  const consumer = Promise.withResolvers<void>();
  const entered = Promise.withResolvers<void>();
  const events = new EventQueue<string>(async (event) => { await consumer.promise; order.push(event); });
  const lifetime = new BookLifetime(events,
    async () => { order.push('output'); }, async () => { order.push('lease'); });
  const work = lifetime.run(async (signal) => {
    await new Promise<void>((resolve) => {
      signal.addEventListener('abort', () => resolve(), { once: true });
      entered.resolve();
    });
    order.push('task');
    events.enqueue('log');
  });
  await entered.promise;
  const closing = lifetime.close();
  assert.equal(lifetime.close(), closing);
  await assert.rejects(lifetime.run(async () => undefined));
  await work;
  assert.deepEqual(order, ['task']);
  consumer.resolve();
  await closing;
  assert.deepEqual(order, ['task', 'log', 'output', 'lease']);
  assert.throws(() => events.enqueue('late'));
});

test('observer failure is collected and does not prevent remaining events or lease release', async () => {
  const seen: string[] = [];
  const events = new EventQueue<string>(async (event) => {
    if (event === 'bad') throw new Error('disk full');
    seen.push(event);
  });
  const lifetime = new BookLifetime(events, async () => undefined, async () => { seen.push('released'); });
  events.enqueue('bad');
  events.enqueue('good');
  await lifetime.close();
  assert.equal(events.failures.length, 1);
  assert.deepEqual(seen, ['good', 'released']);
});
