import assert from 'node:assert/strict';
import path from 'node:path';
import { writeFile } from 'node:fs/promises';

export async function runScenarios(page, evidence) {
  const results = [];
  const field = page.locator('math-field');
  for (const [index, label, target, replacement] of [[0, '분수', '1', '7'], [1, '근호', '3', '5'], [2, '벡터', 'v', 'a'], [3, '행렬', '1', '9']]) {
    await page.getByRole('button', { name: label, exact: true }).click();
    const before = await page.evaluate(() => window.documentPilot.snapshot());
    await field.click();
    const sink = field.locator('[part="keyboard-sink"]');
    await sink.focus();
    await field.evaluate(element => {
      if (element.shadowRoot.activeElement?.getAttribute('part') !== 'keyboard-sink') throw Error('Math keyboard sink lacks native focus');
    });
    await field.evaluate((element, value) => {
      for (let offset = 0; offset < element.lastOffset; offset++) {
        if (element.getValue(offset, offset + 1, 'latex') === value) {
          element.selection = { ranges: [[offset, offset + 1]] };
          return;
        }
      }
      throw Error(`Cannot locate editable atom: ${value}`);
    }, target);
    await sink.press(replacement);
    const draft = await page.evaluate(() => window.documentPilot.snapshot());
    assert.equal(draft.committed[index], before.committed[index], 'Draft changed committed equation');
    await page.screenshot({ path: path.join(evidence, `draft-${index}.png`), fullPage: true });
    await page.getByRole('button', { name: '적용', exact: true }).click();
    const after = await page.evaluate(() => window.documentPilot.snapshot());
    await writeFile(path.join(evidence, `edit-${index}.json`), JSON.stringify({ before, draft, after }, null, 2));
    assert.notEqual(after.committed[index], before.committed[index], `Keyboard ${label} did not change equation`);
    assert(after.committed[index].includes(replacement));
    assert(after.committed[index].includes(['\\frac', '\\sqrt', '\\vec', '\\begin{pmatrix}'][index]), 'Edit removed math structure');
    assert.deepEqual(after.errors, []);
    assert.deepEqual(after.mathErrors, []);
    results.push({ family: label, before: before.committed[index], draft: draft.mountedValue, committed: after.committed[index] });
  }
  await page.getByRole('button', { name: '분수', exact: true }).click();
  const beforeNoop = await page.evaluate(() => window.documentPilot.snapshot().committed);
  await page.getByRole('button', { name: '적용', exact: true }).click();
  assert.deepEqual(await page.evaluate(() => window.documentPilot.snapshot().committed), beforeNoop);
  await field.click();
  await field.locator('[part="keyboard-sink"]').press('x');
  const changedDraft = await page.evaluate(() => window.documentPilot.snapshot().mountedValue);
  assert.notEqual(changedDraft, beforeNoop[0]);
  await page.getByRole('button', { name: '취소', exact: true }).click();
  assert.deepEqual(await page.evaluate(() => window.documentPilot.snapshot().committed), beforeNoop);
  assert.equal(await page.evaluate(() => window.documentPilot.snapshot().mountedValue), beforeNoop[0]);
  const editor = page.locator('.tiptap');
  await editor.click();
  await page.keyboard.press('Control+End');
  await page.keyboard.insertText(' 한글 입력 확인');
  assert((await editor.innerText()).includes('한글 입력 확인'));
  await page.evaluate(async () => { await document.fonts.ready; });
  for (const width of [375, 768, 1280]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.screenshot({ path: path.join(evidence, `committed-${width}.png`), fullPage: true });
  }
  await page.setViewportSize({ width: 1280, height: 1000 });
  return { edits: results, noOpExact: true, cancelExact: true, koreanInsertText: true, nativeIME: 'Not claimed by insertText; later mounted IME scenarios remain C/G', final: await page.evaluate(() => window.documentPilot.snapshot()) };
}
