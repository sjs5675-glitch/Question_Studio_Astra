import { readFile, writeFile, mkdir, realpath } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { createInterface } from 'node:readline';
import path from 'node:path';
import { outputViews } from '../document-output.js';
import { openPrintSession, closePrintSession } from './document-pdf.mjs';

const lines = createInterface({ input: process.stdin }), signal = new AbortController();
let started = false;
lines.on('line', line => {
  if (started) { if (line === 'cancel') signal.abort(); return; }
  started = true; const job = JSON.parse(line);
  execute(job).then(value => process.stdout.write(JSON.stringify(value).replaceAll(job.token, '[capability]') + '\n'), error => {
    process.stdout.write(JSON.stringify({ status: 'failed', error: error.message, diagnostic: error.diagnostic }).replaceAll(job.token, '[capability]') + '\n'); process.exitCode = 1;
  }).finally(() => { lines.close(); process.stdin.destroy(); });
});
lines.on('close', () => signal.abort());

async function execute(job) {
  const owner = await realpath(job.root);
  if (!owner.replaceAll('\\', '/').toLowerCase().startsWith('c:/question_studio_astra/')) throw Error('Unsafe formula owner');
  if (!Array.isArray(job.equation_ids) || job.equation_ids.some(id => !/^[a-f0-9-]{36}$/.test(id))) throw Error('Invalid equation IDs');
  const data = await readFile(path.join(owner, 'document.json'));
  if (data.length > 20 * 1024 * 1024) throw Error('Document exceeds20MiB');
  const current = JSON.parse(data), views = outputViews(current);
  await writeFile(path.join(owner, 'print.json'), JSON.stringify({ document: views.document, equationIds: job.equation_ids, equationStyles: job.equation_styles }), { flag: 'wx' });
  let session, closeTask;
  const stop = () => { if (session) closeTask = closePrintSession(session.context).catch(error => error); };
  signal.signal.addEventListener('abort', stop);
  const timer = setTimeout(() => signal.abort(), Math.min(60000, job.timeout_ms));
  try {
    signal.signal.throwIfAborted();
    session = await openPrintSession({ mode: 'document', port: job.port, token: job.token, profile: job.profile, assets: new Set(job.assets), rasterScale: 3 });
    signal.signal.throwIfAborted();
    await session.page.waitForFunction(() => ['ready', 'blocked'].includes(window.documentFormulas?.status), undefined, { timeout: Math.min(60000, job.timeout_ms) });
    const measured = await session.page.evaluate(() => window.documentFormulas);
    if (measured.status !== 'ready') { const error = Error(measured.message); error.diagnostic = measured.diagnostic; throw error; }
    if (session.errors.length || session.network.some(entry => !entry.allowed)) throw Error('Formula resource failure');
    await mkdir(path.join(owner, 'formulas'), { recursive: true });
    const images = [];
    for (const record of measured.formulas) {
      signal.signal.throwIfAborted();
      const element = session.page.locator(`[data-node-id="${record.node_id}"]`);
      const png = await element.screenshot({ scale: 'device' });
      signal.signal.throwIfAborted();
      const relative = `formulas/${record.node_id}.png`;
      await writeFile(path.join(owner, relative), png, { flag: 'wx' });
      const node = current.content.nodes.find(node => node.id === record.node_id);
      images.push({ ...record, path: relative, sha256: createHash('sha256').update(png).digest('hex'), latex_sha256: createHash('sha256').update(node.payload.latex).digest('hex'), width_px: png.readUInt32BE(16), height_px: png.readUInt32BE(20) });
    }
    return { status: 'rendered', images, fonts: measured.fonts, network: session.network, errors: session.errors };
  } finally { clearTimeout(timer); signal.signal.removeEventListener('abort', stop); if (session) { const closed = await (closeTask ?? closePrintSession(session.context)); if (closed instanceof Error) throw closed; } }
}
