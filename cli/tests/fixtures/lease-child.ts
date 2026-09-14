import { BookLease } from '../../src/store/book-lease.js';
const bookDir = process.argv[2];
if (!bookDir) throw new Error('Missing book directory');
try {
  const lease = await BookLease.acquire(bookDir);
  process.send?.({ status: 'acquired' });
  process.on('message', () => { void lease.close().then(() => process.exit(0)); });
} catch (error) {
  process.send?.({ status: 'blocked', error: String(error) });
  process.exitCode = 2;
  process.disconnect?.();
}
