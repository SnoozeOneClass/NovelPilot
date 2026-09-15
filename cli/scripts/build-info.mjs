import { writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { packageProvenance } from '../dist/eval/runner.js';

const packageDir = fileURLToPath(new URL('../', import.meta.url));
const info = await packageProvenance(packageDir);
await writeFile(new URL('../dist/build-info.json', import.meta.url), JSON.stringify(info, null, 2) + '\n');
