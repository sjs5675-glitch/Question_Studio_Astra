import { tokenizeLatex } from "./latexTokenizer";
import { LatexParser } from "./latexParser";
import { toHwpEqn } from "./toHwpEqn";

/**
 * LaTeX 수식 문자열 → HWP 수식 스크립트.
 *
 * 기반: franknoh/hwp-eqn-ts (MIT, LICENSE.hwp-eqn-ts 참조)의 AST·토크나이저·디코더.
 * exam-studio 가 파서(시퀀스/관계연산/병치 보존)와 디코더(괄호 보존, 발산 매핑)를 보강.
 *
 * 주의: rm 단위·comma~·cdots·통수식 분리·좌측첨자(LSUB)·순열조합 등 HWP 고유
 * 후처리는 equation.py 의 normalize_parts(R-01~R-10)가 담당한다. 이 함수는
 * 구조 변환(분수·루트·첨자·적분·합·행렬·괄호)만 책임진다.
 */
export function latexToHwp(latex: string): string {
  const tokens = tokenizeLatex(latex);
  const ast = new LatexParser(tokens).parseExpression();
  return toHwpEqn(ast).replace(/\s+/g, " ").trim();
}
