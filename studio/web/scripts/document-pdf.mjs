import { chromium } from 'playwright-core';
import { mkdir, writeFile, rename, unlink } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import path from 'node:path';

export const EDGE = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
export const LIMITS = Object.freeze({ timeoutMs: 60000, pages: 200, inputBytes: 20 * 1024 * 1024 });
const ownedContexts = new WeakSet();
const closingContexts = new WeakMap();
async function bounded(operation, milliseconds, stage) {
  let timer;
  try {
    return await Promise.race([operation, new Promise((_, reject) => {
      timer = setTimeout(() => reject(Error(`${stage} exceeded ${milliseconds}ms; worker remains unavailable until cleanup`)), milliseconds);
    })]);
  } finally { clearTimeout(timer); }
}
export async function closePrintSession(context) {
  if (!ownedContexts.has(context)) throw Error('Refusing to close an unowned browser context');
  if (!closingContexts.has(context)) closingContexts.set(context, context.close());
  await bounded(closingContexts.get(context), 15000, 'Owned browser close');
}
export async function openPrintSession(job) {
  if (!Number.isInteger(job.port) || job.port < 1024 || job.port > 65535 || !/^[a-f0-9]{32}$/.test(job.token)) throw Error('Invalid internal print receipt');
  const origin = `http://127.0.0.1:${job.port}`;
  const documentMode = job.mode === 'document';
  if (job.mode && !documentMode) throw Error('Invalid print mode');
  if (job.rasterScale && ![1, 3].includes(job.rasterScale)) throw Error('Invalid raster scale');
  const entry = documentMode ? `/document-print/${job.token}` : `/pilot/${job.token}`;
  const network = [];
  const context = await chromium.launchPersistentContext(job.profile, { executablePath: EDGE, headless: true,
    timeout: 15000, serviceWorkers: 'block', viewport: { width: 1280, height: 1000 }, deviceScaleFactor: job.rasterScale ?? 1, args: ['--disable-background-networking', '--no-first-run'] });
  ownedContexts.add(context);
  try {
  if (documentMode) await context.setExtraHTTPHeaders({ 'X-Print-Token': job.token });
  await context.route('**/*', route => {
    const url = new URL(route.request().url());
    const allowed = url.origin === origin && !url.search && (url.pathname === entry || job.assets.has(url.pathname));
    network.push({ url: url.pathname === entry ? '/print/[capability]' : url.origin === origin ? url.pathname : '[external denied]', allowed });
    return allowed ? route.continue() : route.abort('blockedbyclient');
  });
  await context.routeWebSocket('**/*', socket => { network.push({ url: '[websocket denied]', allowed: false, websocket: true }); socket.close(); });
  const page = context.pages()[0] ?? await bounded(context.newPage(), 5000, 'Owned page creation');
  const errors = [];
  page.on('pageerror', error => errors.push(String(error.stack ?? error).replaceAll(job.token, '[capability]')));
  page.setDefaultTimeout(15000);
  await page.goto(`${origin}${entry}`, { timeout: 10000, waitUntil: 'domcontentloaded' });
  return { context, page, network, errors };
  } catch (error) {
    if (error instanceof Error) { error.message = error.message.replaceAll(job.token, '[capability]'); error.stack = error.stack?.replaceAll(job.token, '[capability]'); }
    try { await closePrintSession(context); }
    catch (cleanupError) { throw new AggregateError([error, cleanupError], 'Print session setup and cleanup failed'); }
    throw error;
  }
}

let printing = false;
export async function printCurrentPage(page, job) {
  if (printing) throw Error('PDF worker busy; retry after the current build finishes');
  if (!Number.isInteger(job.inputBytes) || job.inputBytes < 0 || job.inputBytes > LIMITS.inputBytes) throw Error('Document exceeds 20 MiB limit');
  if (!Number.isInteger(job.pages) || job.pages < 1 || job.pages > LIMITS.pages) throw Error('Document exceeds 200 page limit');
  const timeoutMs = job.timeoutMs ?? LIMITS.timeoutMs;
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > LIMITS.timeoutMs) throw Error('Invalid render timeout');
  const context = page.context();
  if (!ownedContexts.has(context) || closingContexts.has(context)) throw Error('Print requires a fresh owned browser session');
  printing = true;
  let timer;
  let onAbort;
  let stopped;
  let publishing = false;
  let renderTask;
  let pendingWritten = false;
  const pending = `${job.output}.pending`;
  const requireActive = () => { if (stopped) throw stopped; };
  try {
    const interrupted = new Promise((_, reject) => {
      const stop = message => {
        if (publishing || stopped) return;
        stopped = Error(message);
        reject(stopped);
      };
      onAbort = () => stop('PDF interrupted; previous artifact retained');
      job.signal?.addEventListener('abort', onAbort, { once: true });
      timer = setTimeout(() => stop('PDF exceeded render timeout; previous artifact retained'), timeoutMs);
      if (job.signal?.aborted) onAbort();
    });
    renderTask = (async () => {
      requireActive();
      if (job.mode === 'document') {
        await page.waitForFunction(() => ['ready', 'blocked'].includes(window.documentPrint?.status), undefined, { timeout: LIMITS.timeoutMs });
        const readiness = await page.evaluate(() => window.documentPrint);
        if (readiness.status !== 'ready') throw Error(`${readiness.code}: ${readiness.message}`);
        if (readiness.pages.length > LIMITS.pages || readiness.geometry.outside) throw Error('Invalid measured PDF bounds');
      } else await page.waitForFunction(() => window.documentPilot?.state.mounted && window.documentPilot.state.applies >= 4, undefined, { timeout: LIMITS.timeoutMs });
      requireActive();
      await page.evaluate(async () => { await document.fonts.ready; await Promise.all([...document.images].map(image => image.decode())); });
      requireActive();
      return page.pdf({ format: 'A4', preferCSSPageSize: true, printBackground: true, scale: 1 });
    })();
    const bytes = await Promise.race([interrupted, renderTask]);
    requireActive();
    await mkdir(path.dirname(job.output), { recursive: true });
    requireActive();
    await writeFile(pending, bytes, { flag: 'wx' });
    pendingWritten = true;
    requireActive();
    publishing = true;
    await rename(pending, job.output);
    pendingWritten = false;
    return { bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex') };
  } finally {
    clearTimeout(timer);
    job.signal?.removeEventListener('abort', onAbort);
    if (stopped) {
      await closePrintSession(context);
      await bounded(Promise.allSettled([renderTask]), 5000, 'Canceled print settlement');
    }
    if (pendingWritten) await unlink(pending);
    printing = false;
  }
}
