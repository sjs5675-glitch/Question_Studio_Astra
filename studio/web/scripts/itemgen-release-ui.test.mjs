// Reproducible, offline UI acceptance. Installed playwright-core + empty Edge profile.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdir, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const require = createRequire(import.meta.url);
const { chromium } = require('C:/Question_Studio_Astra/studio/web/node_modules/playwright-core');
const studio = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const candidate = path.dirname(studio);
const artifacts = path.join(candidate, 'docs/itemgen-ui-evidence');
await mkdir(artifacts, { recursive: true });
let child, context, logs = '', requests = [], errors = [];
try {
  child = spawn('C:/Question_Studio_Astra/.venv/Scripts/python.exe', ['-u', 'scripts/itemgen_ui_fixture.py'], {
    cwd: studio, env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONPATH: `${studio};${candidate};C:/Question_Studio_Astra` },
    windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
  });
  const ready = await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`fixture timeout: ${logs}`)), 30000);
    child.stderr.on('data', (chunk) => { logs += chunk; });
    child.stdout.on('data', (chunk) => {
      logs += chunk;
      const match = logs.match(/READY (\{[^\n]+\})/);
      if (match) { clearTimeout(timer); resolve(JSON.parse(match[1])); }
    });
    child.once('exit', (code) => { clearTimeout(timer); reject(new Error(`fixture exited ${code}: ${logs}`)); });
  });
  // An empty, task-owned profile: never uses the real browser profile or a remote provider.
  const profile = path.join(candidate, '.test-tmp', `edge-ui-${Date.now()}`);
  context = await chromium.launchPersistentContext(profile, { channel: 'msedge', headless: true,
    viewport: { width: 1440, height: 1100 } });
  const page = await context.newPage();
  page.on('pageerror', (e) => errors.push(e.message));
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    if (request.method() === 'POST') requests.push({ url: request.url(), body: request.postDataJSON() });
    await route.continue({ headers: { ...request.headers(), 'X-Session-Token': 'synthetic-itemgen-ui' } });
  });
  await page.route('**/__fixture', (route) => route.fulfill({ contentType: 'text/html', body: `<!doctype html><html lang="ko"><meta charset="utf-8"><link rel="stylesheet" href="/styles.css"><title>합성 문항 생성 검증</title><main id="fixture"></main><script type="module">import { mountItemGeneration } from '/item-generation.mjs'; mountItemGeneration(document.querySelector('#fixture'));</script></html>` }));
  await page.goto(`http://127.0.0.1:${ready.port}/__fixture`);
  await page.waitForSelector('.gen-jobbar select option[value="' + ready.job_id + '"]', { state: 'attached' });
  await page.waitForSelector('.gen-jobbar select', { state: 'visible' });
  assert.equal(requests.filter((r) => /\/jobs$/.test(r.url)).length, 0, 'opening UI must not create a job');
  await page.locator('.gen-jobbar select').selectOption(ready.job_id);
  await page.waitForSelector('.gen-draft');
  await page.locator('.gen-field').filter({has:page.locator('.gen-label').filter({hasText:/^과목$/})}).locator('select').selectOption('고2-물리학');
  await page.waitForFunction(() => document.querySelector('.gen-estimate')?.textContent.includes('Claude Opus max')
    && document.querySelector('.gen-estimate')?.textContent.includes('Codex 검증(max)'));
  await page.getByText('현재 canonical 수정 본문', { exact: false }).waitFor();
  assert.match(await page.locator('.gen-draft').nth(1).innerText(), /같은 모델 자체 검산/);
  await page.getByLabel('검토 상태 필터').selectOption('self_check');
  assert.equal(await page.locator('.gen-draft').count(), 1);
  await page.getByLabel('검토 상태 필터').selectOption('all');
  await page.getByLabel('2번 검토 선택', { exact: true }).check();
  assert.equal(await page.getByLabel('검토 모델', { exact: true }).inputValue(), 'claude');
  await page.getByRole('button', { name: '선택 1문항 교차 검토 / 재검토', exact: true }).click();
  await page.getByText('교차 검토 · 답 일치', { exact: true }).waitFor();
  const sent = requests.find((r) => /\/reviews$/.test(r.url));
  assert.deepEqual(sent.body.numbers, [2]); assert.equal(sent.body.model, 'claude');
  assert.equal(typeof sent.body.expected_revision, 'number');
  await page.locator('.gen-draft').nth(1).locator('.gen-review-history summary').click();
  await page.getByText('검토 풀이: 풀이', { exact: true }).waitFor();
  await page.getByRole('button', { name: '선택 1문항 교차 검토 / 재검토', exact: true }).click();
  await page.getByText('검토 전체 이력 (2)', { exact: true }).waitFor();
  assert.equal(await page.locator('.gen-draft').nth(1).locator('.gen-review-history').getAttribute('open'), '');
  await page.getByLabel('1번 검토 선택', { exact: true }).check();
  await page.getByLabel('2번 검토 선택', { exact: true }).uncheck();
  await page.getByLabel('검토 모델', { exact: true }).selectOption('codex');
  await page.getByRole('button', { name: '선택 1문항 교차 검토 / 재검토', exact: true }).click();
  assert.deepEqual(requests.at(-1).body.numbers, [1], 'confirmed item can be reviewed');
  await page.waitForFunction(() => document.querySelector('.gen-draft')?.textContent.includes('검토 전체 이력 (1)'));
  await page.screenshot({ path: path.join(artifacts, 'desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 430, height: 1000 });
  await page.screenshot({ path: path.join(artifacts, 'mobile.png'), fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'no mobile horizontal overflow');
  assert.equal(requests.filter((r) => /\/jobs$/.test(r.url)).length, 0);
  assert.deepEqual(errors, []);
  await writeFile(path.join(artifacts, 'results.json'), JSON.stringify({ passed: true, assertions: 18, requests, errors }, null, 2));
  console.log('UI GREEN: 18 assertions; desktop/mobile screenshots and request evidence saved.');
} catch (error) {
  await writeFile(path.join(artifacts, 'results.json'), JSON.stringify({ passed: false, error: String(error), requests, errors }, null, 2));
  throw error;
} finally {
  if (context) await context.close();
  if (child) {
    child.stdin.write('shutdown\n');
    const exit = new Promise((resolve) => child.once('exit', resolve));
    const graceful = await Promise.race([exit.then(() => true), new Promise((resolve) => setTimeout(() => resolve(false), 5000))]);
    if (!graceful) child.kill(); // Only this fixture process; never the operating runtime.
  }
  await writeFile(path.join(artifacts, 'fixture.log'), logs);
}
