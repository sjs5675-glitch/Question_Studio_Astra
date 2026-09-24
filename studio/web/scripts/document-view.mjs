import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { outputViews } from '../document-output.js';

const data = await readFile(process.argv[2]);
if (data.length > 20 * 1024 * 1024) throw Error('Document exceeds 20 MiB');
const current = JSON.parse(data);
process.stdout.write(JSON.stringify({ ...outputViews(current), currentHash: createHash('sha256').update(JSON.stringify(current)).digest('hex') }));
