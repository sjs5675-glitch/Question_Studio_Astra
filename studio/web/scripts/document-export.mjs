import { readFile, writeFile, realpath } from 'node:fs/promises';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { createInterface } from 'node:readline';
import { outputViews } from '../document-output.js';
import { openPrintSession, closePrintSession, printCurrentPage, LIMITS } from './document-pdf.mjs';

const root = 'C:/Question_Studio_Astra';
const abort = new AbortController();
const lines = createInterface({ input: process.stdin });
let started = false;
lines.on('line', line => {
  if (started) { if (line === 'cancel') abort.abort(); return; }
  started = true;
  const job = JSON.parse(line);
  execute(job).then(result => { if (result.status !== 'rendered') process.exitCode = 1; process.stdout.write(`${JSON.stringify(result).replaceAll(job.token, '[capability]')}\n`); }, error => {
    process.stdout.write(`${JSON.stringify({ status: 'failed', error: String(error.message).replace(/[a-f0-9]{32}/g, '[capability]') })}\n`);
    process.exitCode = 1;
  }).finally(() => { lines.close(); process.stdin.destroy(); });
});
lines.on('close', () => { if (started) abort.abort(); });
process.on('SIGTERM', () => abort.abort());

async function execute(job) {
  const owner = await realpath(job.root);
  if (!owner.toLowerCase().startsWith(`${root.toLowerCase()}/`.replaceAll('/', path.sep)) && !owner.toLowerCase().startsWith(`${root.toLowerCase()}/`)) throw Error('Unsafe print owner');
  if (!/^[a-f0-9]{32}$/.test(job.token) || !Array.isArray(job.assets) || job.assets.some(value => typeof value !== 'string' || !value.startsWith('/') || value.includes('..'))) throw Error('Invalid print assets');
  const bytes = await readFile(path.join(owner, 'document.json'));
  if (bytes.length > LIMITS.inputBytes) throw Error('Document exceeds 20 MiB');
  const current = JSON.parse(bytes);
  const currentHash = createHash('sha256').update(JSON.stringify(current)).digest('hex');
  await writeFile(path.join(owner, 'print.json'), JSON.stringify({ ...outputViews(current), currentHash }), { flag: 'wx' });
  let session;
  const consoleErrors = [], failedRequests = [];
  try {
    if (abort.signal.aborted) throw Error('PDF interrupted before browser');
    session = await openPrintSession({ mode: 'document', port: job.port, token: job.token, profile: job.profile, assets: new Set(job.assets) });
    session.page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()); });
    session.page.on('requestfailed', request => failedRequests.push({ path: new URL(request.url()).pathname, error: request.failure()?.errorText }));
    const pdf = await printCurrentPage(session.page, { mode: 'document', pages: 1, inputBytes: bytes.length, output: path.join(owner, 'render.pdf'), signal: abort.signal });
    const layout = await session.page.evaluate(() => window.documentPrint);
    if (session.errors.length || consoleErrors.length || failedRequests.length || session.network.some(entry => !entry.allowed)) throw Error('PDF resource or renderer error');
    return { status: 'rendered', pdf, layout, browserVersion: session.context.browser()?.version() ?? null, network: session.network, consoleErrors, failedRequests };
  } catch (error) {
    return { status: 'failed', error: String(error.message), network: session?.network ?? [], errors: session?.errors ?? [], consoleErrors, failedRequests };
  } finally { if (session) await closePrintSession(session.context); }
}
