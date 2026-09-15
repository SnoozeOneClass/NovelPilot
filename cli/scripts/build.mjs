import { execFileSync } from 'node:child_process';
import { lstat, realpath, rm } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = await realpath(fileURLToPath(new URL('..', import.meta.url)));
const output = join(root, 'dist');
try {
  const stat = await lstat(output);
  const resolved = await realpath(output);
  if (stat.isSymbolicLink() || dirname(resolved) !== root || resolved !== output) throw new Error('拒绝清理不属于本包的构建目录');
  await rm(resolved, { recursive: true });
} catch (error) { if (error.code !== 'ENOENT') throw error; }
const tsc = fileURLToPath(import.meta.resolve('typescript/bin/tsc'));
execFileSync(process.execPath, [tsc, '-p', join(root, 'tsconfig.build.json')], { cwd: root, stdio: 'inherit' });
execFileSync(process.execPath, [join(root, 'scripts/build-info.mjs')], { cwd: root, stdio: 'inherit' });
