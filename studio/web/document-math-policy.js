import katex from 'katex';

export const mathOptions = Object.freeze({ throwOnError: true, trust: false, strict: 'error', maxExpand: 1000, maxSize: 20 });
const commands = new Set(`frac dfrac tfrac genfrac binom dbinom tbinom sqrt acute grave dot ddot bar vec hat widehat tilde widetilde check breve
overrightarrow overleftarrow overleftrightarrow underrightarrow underleftarrow boxed overbrace underbrace overline underline overset underset stackrel
left right middle begin end mathrm text textbf textit textrm textsf texttt textnormal mathbf mathit mathsf mathbb mathcal mathscr mathfrak boldsymbol bm rm
infty ldots dots to Rightarrow Leftarrow Leftrightarrow Longrightarrow Longleftrightarrow int oint iint iiint sum prod coprod pm mp times div cdot
bigcup bigcap land lor quad qquad lbrace rbrace langle rangle vert lvert rvert Vert lVert rVert lfloor rfloor lceil rceil
alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau upsilon phi chi psi omega
Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega hbar imath jmath vartheta varpi varsigma varphi varepsilon
cap cup sqcap sqcup oplus otimes odot wedge vee subset supset subseteq supseteq in ni notin leq geq le ge ll gg prec succ uplus circ bullet ast star bigcirc
emptyset varnothing therefore because equiv exists neq ne doteq sim approx simeq cong asymp diamond forall prime partial neg propto nabla dagger ddagger
leftarrow rightarrow uparrow downarrow leftrightarrow updownarrow Uparrow Downarrow Updownarrow nwarrow searrow nearrow swarrow mapsto
cdots vdots ddots triangle angle vdash dashv bot top models frown sin cos tan cot sec csc sinh cosh tanh log ln exp lim max min sup inf det gcd
limits nolimits displaystyle textstyle scriptstyle scriptscriptstyle big Big bigg Bigg bigl bigr Bigl Bigr biggl biggr Biggl Biggr
operatorname substack cases hline hdashline cr cancel bcancel xcancel mathchoice pmb xrightarrow xleftarrow`.split(/\s+/));
const environments = new Set(['matrix', 'pmatrix', 'bmatrix', 'Bmatrix', 'vmatrix', 'Vmatrix', 'smallmatrix', 'cases', 'aligned', 'alignedat', 'gathered', 'array']);
const escaped = new Set(['{', '}', '_', '%', '&', '#', '$', ',', ';', '!', ':', '|', '\\', ' ', '>', '<']);

export function scanMath(value) {
  if (typeof value !== 'string' || !value.trim() || [...value].length > 16384) throw Error('수식은 1~16,384자여야 합니다.');
  if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f\u202a-\u202e\u2066-\u2069]/u.test(value) || /[\ud800-\udfff]/u.test(value)) throw Error('수식에 허용되지 않는 제어 문자가 있습니다.');
  let index = 0, depth = 0, tokens = 0;
  const stack = [];
  const group = () => {
    while (/\s/.test(value[index] ?? '') && index < value.length) index++;
    if (value[index++] !== '{') throw Error('명령의 필수 인수를 확인하세요.');
    const start = index;
    while (index < value.length && value[index] !== '}') index++;
    if (index === value.length) throw Error('닫는 중괄호가 없습니다.');
    return value.slice(start, index++);
  };
  while (index < value.length) {
    if (++tokens > 8000) throw Error('수식 토큰이 너무 많습니다.');
    const char = value[index++];
    if (char === '%') { while (index < value.length && value[index] !== '\n') index++; continue; }
    if (char === '{' && ++depth > 32) throw Error('수식 중첩은 32단계까지 지원합니다.');
    if (char === '}' && --depth < 0) throw Error('중괄호의 짝을 확인하세요.');
    const environment = stack.at(-1);
    if (char === '&' && environment && depth === environment.depth && ++environment.columns > 12) throw Error('행렬은 12열까지 지원합니다.');
    if (char !== '\\') continue;
    const start = index;
    while (/[A-Za-z]/.test(value[index] ?? '') && index < value.length) index++;
    const command = value.slice(start, index);
    if (!command) {
      const symbol = value[index++];
      if (!escaped.has(symbol)) throw Error('지원하지 않는 제어 기호입니다.');
      if (symbol === '\\' && environment && depth === environment.depth) {
        if (++environment.rows > 12) throw Error('행렬은 12행까지 지원합니다.');
        environment.columns = 1;
      }
      continue;
    }
    if (!commands.has(command)) throw Error(`허용되지 않거나 지원하지 않는 명령: \\${command}`);
    if (command === 'cr' && environment && depth === environment.depth) {
      if (++environment.rows > 12) throw Error('행렬은 12행까지 지원합니다.');
      environment.columns = 1;
    }
    if (command === 'begin') {
      const name = group();
      if (!environments.has(name) || stack.length >= 16) throw Error('지원하지 않는 수식 환경입니다.');
      stack.push({ name, depth, rows: 1, columns: 1 });
      if (name === 'array') {
        const spec = group();
        if (!/^[clr|: ]+$/.test(spec) || (spec.match(/[clr]/g) ?? []).length > 12) throw Error('배열 열 지정이 지원 범위를 벗어났습니다.');
      }
      if (name === 'alignedat' && !/^[1-6]$/.test(group())) throw Error('정렬 열 수가 지원 범위를 벗어났습니다.');
    }
    if (command === 'end' && stack.pop()?.name !== group()) throw Error('수식 환경의 짝을 확인하세요.');
  }
  if (depth || stack.length) throw Error('닫히지 않은 수식 그룹이 있습니다.');
  return { tokens };
}

export function validateTex(value, display = 'inline') {
  scanMath(value);
  if (!['inline', 'display'].includes(display)) throw Error('수식 표시 형식이 올바르지 않습니다.');
  return katex.renderToString(value, { ...mathOptions, displayMode: display === 'display', macros: {} });
}

export function validateLiteral(value) {
  if (typeof value !== 'string' || !value.trim() || [...value].length > 16384) throw Error('문자 수식을 확인하세요.');
  for (const char of value) {
    const point = char.codePointAt(0);
    if (!(point === 9 || point === 10 || point >= 32 && point <= 0x3ff && !(point >= 127 && point <= 159) || point >= 0x1100 && point <= 0x11ff || point >= 0x3130 && point <= 0x318f || point >= 0xac00 && point <= 0xd7a3)) throw Error('문자 수식에서 지원하지 않는 글자입니다. TeX로 입력해 주세요.');
  }
  return value;
}
