import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtemp, mkdir, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('..', import.meta.url));
const npmCli = process.env.npm_execpath;
assert.ok(npmCli, 'Run with npm run smoke:pack');
const npm = (args, cwd) => execFileSync(process.execPath, [npmCli, ...args, '--cache', join(root, 'npm-cache')], {
  cwd, encoding: 'utf8', timeout: 120000,
});
npm(['run', 'build'], root);
const fixture = await mkdtemp(join(tmpdir(), 'novelpilot-package-'));
const installDir = join(fixture, 'install');
const bookDir = join(fixture, '新书');
await mkdir(installDir);
await mkdir(bookDir);
const [packed] = JSON.parse(npm(['pack', '--ignore-scripts', '--json', '--pack-destination', fixture], root));
assert.ok(packed.files.some((file) => file.path === 'assets/prompts/base.md'));
assert.ok(packed.files.some((file) => file.path === 'dist/main.js'));
assert.ok(packed.files.every((file) => !/^(tests|src|node_modules|npm-cache|\.test-data)\//.test(file.path)));
npm(['install', join(fixture, packed.filename), '--prefer-offline', '--ignore-scripts', '--no-audit', '--no-fund',
  '--cache', join(root, 'npm-cache')], installDir);
const entry = join(installDir, 'node_modules', 'novelpilot-cli', 'dist', 'main.js');
const result = JSON.parse(execFileSync(process.execPath, [entry, '--check-startup'], {
  cwd: bookDir, env: { ...process.env, INIT_CWD: root }, encoding: 'utf8', timeout: 30000,
}));
assert.equal(result.bookDir, await realpath(bookDir));
assert.ok(result.basePromptPath.startsWith(installDir));
assert.equal(result.status, 'ready');
console.log(JSON.stringify({ ...result, verdict: 'PASS', fixture, packedFiles: packed.files.length }, null, 2));
