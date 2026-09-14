import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

// npm changes cwd to the package. Only this development wrapper uses INIT_CWD.
const entry = fileURLToPath(new URL('../src/main.ts', import.meta.url));
const loader = import.meta.resolve('tsx');
const child = spawn(process.execPath, ['--import', loader, entry, ...process.argv.slice(2)], {
  cwd: process.env.INIT_CWD ?? process.cwd(), stdio: 'inherit',
});
child.on('error', (error) => { console.error(error.message); process.exitCode = 1; });
child.on('exit', (code) => { process.exitCode = code ?? 1; });
