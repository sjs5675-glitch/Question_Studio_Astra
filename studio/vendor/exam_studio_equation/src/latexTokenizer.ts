import { Token, TokenType } from "./tokenTypes";
import { isAlpha, isDigit } from "./utils";

const LATEX_KEYWORDS = new Set([
  "int", "oint", "sum", "sqrt", "frac", "binom",
  "acute", "grave", "dot", "ddot", "bar", "vec", "hat", "tilde", "check",
  "overrightarrow", "overleftarrow", "boxed", "overbrace", "underbrace", "times",
  "overset",
  "left", "right",
  "begin", "end"
]);
export function tokenizeLatex(input: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  while (i < input.length) {
    const ch = input[i];
    if (/\s/.test(ch)) {
      tokens.push({ type: TokenType.SPACE, value: ch });
      i++; continue;
    }
    // exam-studio 패치: < > ! : ; < > 추가. (원본 기호집합엔 없어 UNKNOWN→파서가
    // 통째로 버려 부등식 < > 와 계승 ! 가 사라지던 치명 버그.)
    if ("^_{}()#&~'/,.-+*=|[]\\<>!:;".includes(ch)) {
      if (ch === '\\' && i + 1 < input.length && input[i + 1] === '\\') {
        tokens.push({ type: TokenType.SYMBOL, value: "\\\\" });
        i += 2; continue;
      }
      if (ch === '\\') {
        i++;
        let cmd = "";
        while (i < input.length && /[A-Za-z]/.test(input[i])) {
          cmd += input[i]; i++;
        }
        const lower = cmd.toLowerCase();
        if (cmd.length > 0 && LATEX_KEYWORDS.has(lower)) {
          tokens.push({ type: TokenType.KEYWORD, value: lower });
        } else if (cmd.length > 0) {
          tokens.push({ type: TokenType.IDENT, value: cmd });
        } else if (i < input.length && ",;:! ".includes(input[i])) {
          // exam-studio 패치: \, \; \: \! \(공백) = 간격 명령 → HWP 빈칸 `~`.
          // (원본은 백슬래시를 그대로 흘려 HWP 렌더링을 깨뜨렸다.)
          i++;
          tokens.push({ type: TokenType.IDENT, value: "~" });
        } else if (i < input.length && (input[i] === '{' || input[i] === '}')) {
          // \{ \} = 보이는 중괄호 델리미터(집합 표기). 그룹용 { 와 구분하려 \{ 그대로 보존.
          tokens.push({ type: TokenType.SYMBOL, value: input[i] === '{' ? "\\{" : "\\}" });
          i++;
        } else if (i < input.length && "%$#&_".includes(input[i])) {
          // \% \$ \# \& \_ = 이스케이프 특수문자 → 리터럴 그 자체.
          tokens.push({ type: TokenType.SYMBOL, value: input[i] });
          i++;
        } else {
          tokens.push({ type: TokenType.SYMBOL, value: '\\' });
        }
      } else {
        tokens.push({ type: TokenType.SYMBOL, value: ch });
        i++;
      }
      continue;
    }
    if (isDigit(ch)) {
      let num = ch; i++;
      while (i < input.length && isDigit(input[i])) {
        num += input[i]; i++;
      }
      tokens.push({ type: TokenType.NUMBER, value: num });
      continue;
    }
    if (isAlpha(ch)) {
      let ident = ch; i++;
      while (i < input.length && isAlpha(input[i])) {
        ident += input[i]; i++;
      }
      tokens.push({ type: TokenType.IDENT, value: ident });
      continue;
    }
    // 미지의 문자도 버리지 않고 리터럴로 보존 (원본은 UNKNOWN→파서가 폐기).
    tokens.push({ type: TokenType.SYMBOL, value: ch });
    i++;
  }
  tokens.push({ type: TokenType.EOF, value: "" });
  return tokens;
}