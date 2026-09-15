import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
const entry = fileURLToPath(new URL('../dist/main.js', import.meta.url));
const child = spawn(process.execPath, [entry, '--eval', ...process.argv.slice(2)], {
  cwd: process.env.INIT_CWD ?? process.cwd(), stdio: 'inherit',
});
child.on('error', (error) => { console.error(error.message); process.exitCode = 1; });
child.on('exit', (code) => { process.exitCode = code ?? 1; });
