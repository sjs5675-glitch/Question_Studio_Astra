import { createServer } from 'node:http';
import { readFile, writeFile, mkdir, readdir } from 'node:fs/promises';
import { randomBytes, createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import assert from 'node:assert/strict';
import { parseArgs } from 'node:util';
import { openPrintSession, closePrintSession, printCurrentPage, EDGE, LIMITS } from './document-pdf.mjs';
import { runScenarios } from './document-scenarios.mjs';

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const root = path.resolve(web, '../..');
const { values } = parseArgs({ options: { evidence: { type: 'string', default: '.omo/evidence/astra-cli/task-11/pilot' } }, strict: true });
const evidenceBase = path.resolve(root, values.evidence);
if (path.relative(root, evidenceBase).startsWith('..') || path.isAbsolute(path.relative(root, evidenceBase))) throw Error('Evidence must stay within the isolated workspace');
const runId = `run-${Date.now()}-${randomBytes(6).toString('hex')}`;
const evidence = path.join(evidenceBase, runId);
const profile = path.join(root, '.runtime/document-browser/t11a', runId);
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
await mkdir(evidence, { recursive: true });
const token = randomBytes(16).toString('hex');
const manifest = JSON.parse(await readFile(path.join(web, 'dist/document/manifest.json'), 'utf8'));
const assets = new Map();
for (const item of manifest.files) {
  if (item.path.startsWith('notices/')) continue;
  const bytes = await readFile(path.join(web, 'dist/document', item.path));
  assert.equal(hash(bytes), item.sha256, `Changed bundle asset ${item.path}`);
  assets.set(`/assets/${item.path}`, bytes);
}
const html = `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>수식 출력 검증</title>
<link rel="stylesheet" href="/assets/document.css"><style>
:root{--bg:#f4f5f2;--surface:#fff;--line:#d9ddd6;--text:#18211c;--brand:#176149;font:16px 'Noto Sans KR',sans-serif;color:var(--text);background:var(--bg)}
*{box-sizing:border-box}body{margin:0}main{max-width:1000px;margin:auto;padding:24px}h1{font-size:24px}button{font:inherit;border:1px solid var(--line);border-radius:6px;background:var(--surface);padding:8px 14px;color:var(--text)}button:focus-visible{outline:3px solid #0b72d0}button:hover{border-color:var(--brand)}
.controls{background:var(--surface);padding:20px;border:1px solid var(--line);border-radius:8px;margin-bottom:24px}#palette,.actions{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}math-field{width:100%;font-size:24px;border:1px solid var(--line);padding:12px}.tiptap{min-height:60px;white-space:pre-wrap;outline-offset:4px}.sheet{background:white;padding:20mm;width:210mm;min-height:297mm;color:black;margin:auto}.sheet h2{font-family:'Noto Serif KR',serif;font-size:22px}.columns{display:grid;grid-template-columns:1fr 1fr;gap:12mm}.problem{margin:12mm 0;break-inside:avoid}.problem p{line-height:1.7;word-break:keep-all}.katex-display{margin:12px 0}#status{word-break:keep-all}.print-scroll{overflow:auto}
@page{size:A4;margin:0}@media print{main{padding:0;margin:0;max-width:none}.controls{display:none}.print-scroll{overflow:visible}.sheet{margin:0;height:297mm;break-after:page}.sheet:last-child{break-after:auto}body{background:white}}
</style></head><body><main><section class="controls"><h1>수식 출력 검증</h1><p>수식 초안에서 수정한 뒤 적용하세요.</p><div id="palette" aria-label="수식 선택"></div><h2 id="draft-name">수식 초안</h2><div id="math-editor"></div><div class="actions"><button id="apply">적용</button><button id="cancel">취소</button></div><p id="status" role="status"></p><label id="body-label">본문 편집</label><div id="body-editor" aria-labelledby="body-label"></div></section>
<div class="print-scroll"><article class="sheet"><h2>물리 수식과 한글 출력</h2><p id="print-body"></p><div class="columns"><div><section class="problem"><p>1. 분자의 값과 분모의 값을 확인하시오.</p><div data-formula></div></section><section class="problem"><p>2. 제곱근 기호와 내부 숫자를 확인하시오.</p><div data-formula></div></section></div><div><section class="problem"><p>3. 벡터의 방향과 문자를 확인하시오.</p><div data-formula></div></section><section class="problem"><p>4. 행렬의 행과 열을 확인하시오.</p><div data-formula></div></section></div></div></article></div></main><script type="module" src="/assets/document.js"></script></body></html>`;
const requests = [];
const server = createServer((request, response) => {
  const url = request.url;
  const bytes = url === `/pilot/${token}` ? Buffer.from(html) : assets.get(url);
  requests.push({ url, served: Boolean(bytes) });
  response.setHeader('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'none'; object-src 'none'; base-uri 'none'; frame-src 'none'");
  const type = url?.endsWith('.js') ? 'text/javascript' : url?.endsWith('.css') ? 'text/css' : url?.endsWith('.woff2') ? 'font/woff2' : url?.endsWith('.woff') ? 'font/woff' : url?.endsWith('.ttf') ? 'font/ttf' : 'text/html; charset=utf-8';
  response.writeHead(bytes ? 200 : 404, { 'Content-Type': type, 'Cache-Control': 'no-store' });
  response.end(bytes ?? 'Not found');
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const port = server.address().port;
const resource = { pid: process.pid, port, profile, evidence, edge: EDGE, started: new Date().toISOString() };
await writeFile(path.join(evidence, 'resources.json'), JSON.stringify(resource, null, 2));
let session;
let failure;
const errors = [];
const output = path.join(evidence, `pilot-${path.basename(profile)}.pdf`);
let closing;
function closeOwned() {
  closing ??= (async () => {
    try { if (session) await closePrintSession(session.context); }
    finally {
      server.closeAllConnections();
      await new Promise(resolve => server.close(resolve));
    }
  })();
  return closing;
}
const watchdog = setTimeout(async () => {
  failure = 'Whole pilot exceeded 120 seconds';
  await writeFile(path.join(evidence, 'watchdog.json'), JSON.stringify({ ...resource, failure }));
  try { await closeOwned(); } finally { process.exit(1); }
}, 120000);
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, async () => { try { await closeOwned(); } finally { process.exit(1); } });
try {
  session = await openPrintSession({ port, token, profile, assets });
  session.page.on('pageerror', error => errors.push(String(error)));
  await session.page.waitForFunction(() => window.documentPilot?.state.mounted);
  const scenarios = await runScenarios(session.page, evidence);
  const receipt = await printCurrentPage(session.page, { inputBytes: Buffer.byteLength(JSON.stringify(scenarios)), pages: 1, output });
  const good = hash(await readFile(output));
  const failureControls = [];
  for (const control of ['page-limit', 'size-limit']) {
    const job = { inputBytes: 1, pages: 1, output };
    if (control === 'page-limit') job.pages = 201;
    if (control === 'size-limit') job.inputBytes = LIMITS.inputBytes + 1;
    let rejected = false;
    try { await printCurrentPage(session.page, job); } catch (error) { rejected = true; failureControls.push({ control, error: String(error) }); }
    assert(rejected, `${control} did not reject`);
    assert.equal(hash(await readFile(output)), good, `${control} replaced the last good artifact`);
  }
  const runtime = JSON.parse(await readFile(path.join(web, 'package.json'), 'utf8'));
  for (const address of ['https://example.invalid/blocked', `http://127.0.0.1:${port}/unexpected`, 'http://127.0.0.1:49152/blocked']) {
    await assert.rejects(() => session.page.goto(address, { timeout: 5000 }), /ERR_BLOCKED_BY_CLIENT/);
  }
  assert.equal(session.network.filter(request => !request.allowed).length, 3);
  assert(runtime.dependencies['playwright-core']);
  assert(runtime.devDependencies.esbuild);
  const result = { status: 'BROWSER_PDF_COMPLETE_PENDING_PDF_INSPECTION', evidence, output, receipt, scenarios, failureControls,
    edgeVersion: session.context.browser()?.version(), edgeSha256: hash(await readFile(EDGE)), node: process.version,
    lockSha256: hash(await readFile(path.join(web, 'package-lock.json'))), manifestSha256: hash(await readFile(path.join(web, 'dist/document/manifest.json'))),
    runtimeSplit: { driver: runtime.dependencies['playwright-core'], prebuiltAssets: assets.size, buildOnly: runtime.devDependencies.esbuild }, errors };
  assert.deepEqual(errors, []);
  assert.deepEqual(session.errors, []);
  await writeFile(path.join(evidence, 'browser-result.json'), JSON.stringify(result, null, 2));
  console.log(JSON.stringify(result));
} catch (error) {
  failure = String(error.stack ?? error);
  if (session) await session.page.screenshot({ path: path.join(evidence, 'failure.png'), fullPage: true }).catch(() => {});
  await writeFile(path.join(evidence, 'failure.json'), JSON.stringify({ failure, errors: session?.errors ?? errors, output }, null, 2));
  process.exitCode = 1;
  console.error(failure);
} finally {
  const closeStarted = Date.now();
  try { await closeOwned(); }
  catch (error) { failure = String(error.stack ?? error); process.exitCode = 1; }
  clearTimeout(watchdog);
  await writeFile(path.join(evidence, 'network.json'), JSON.stringify({ browser: session?.network ?? [], server: requests }, null, 2));
  const connected = session?.context.browser()?.isConnected() ?? false;
  await writeFile(path.join(evidence, 'cleanup.json'), JSON.stringify({ ...resource, ended: new Date().toISOString(), closeMilliseconds: Date.now() - closeStarted,
    contextClosed: Boolean(session) && !connected, serverClosed: !server.listening, ownedLiveResources: connected ? [{ profile, status: 'browser close unresolved' }] : [], profileRetained: true, failure }, null, 2));
}
