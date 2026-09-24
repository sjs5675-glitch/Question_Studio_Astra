import { readFileSync } from 'node:fs';
import { validateTex, validateLiteral } from '../document-math-policy.js';

try {
  const request = JSON.parse(readFileSync(0, 'utf8'));
  if (request.mode === 'tex') validateTex(request.value, request.display);
  else if (request.mode === 'literal') validateLiteral(request.value);
  else throw Error('수식 입력 방식을 확인하세요.');
  process.stdout.write(JSON.stringify({ valid: true }));
} catch (error) {
  process.stdout.write(JSON.stringify({ valid: false, message: String(error.message ?? error) }));
  process.exitCode = 1;
}
