export interface ASTNode {
  type: string;
}

export interface LiteralNode extends ASTNode {
  type: "Literal";
  value: string;
}

export interface BinaryOpNode extends ASTNode {
  type: "BinaryOp";
  operator: string; // +, -, times, /, apply...
  left: ASTNode;
  right: ASTNode;
}

/**
 * 병치/관계 시퀀스 (exam-studio 추가).
 * 원본 hwp-eqn-ts 파서는 `+ - times /` 만 이어 붙이고 `=` `,` 관계연산·병치에서
 * 멈춰 나머지를 버렸다. 이 노드로 한 식의 항들을 순서대로 보존한다.
 */
export interface SequenceNode extends ASTNode {
  type: "Sequence";
  items: ASTNode[];
}

export interface FractionNode extends ASTNode {
  type: "Fraction";
  numerator: ASTNode;
  denominator: ASTNode;
  withBar: boolean; // over => true, atop => false, latex=>\frac
}

export interface RootNode extends ASTNode {
  type: "Root";
  radicand: ASTNode;
  degree?: ASTNode; // \sqrt[n]{x} 의 n (있으면 n제곱근 → HWP `root {n} of {x}`)
}

/** \binom{n}{k} → HWP `{n} choose {k}` (조합 ₙCᵣ). exam-studio 추가. */
export interface BinomNode extends ASTNode {
  type: "Binom";
  upper: ASTNode; // 전체항 n
  lower: ASTNode; // 선택항 k
}

export interface SuperscriptNode extends ASTNode {
  type: "Superscript";
  base: ASTNode;
  exponent: ASTNode;
}

export interface SubscriptNode extends ASTNode {
  type: "Subscript";
  base: ASTNode;
  sub: ASTNode;
}

export interface IntegralNode extends ASTNode {
  type: "Integral";
  variant: "int" | "oint";
  lower?: ASTNode;
  upper?: ASTNode;
  body?: ASTNode;
}

export interface SummationNode extends ASTNode {
  type: "Summation";
  lower?: ASTNode;
  upper?: ASTNode;
  body?: ASTNode;
}

export interface DecoratedNode extends ASTNode {
  type: "Decorated";
  decoType: string;
  child: ASTNode;
}

/** \begin{pmatrix}...\end{pmatrix} or \begin{cases}... => one node */
export interface BeginEnvNode extends ASTNode {
  type: "BeginEnv";
  envName: string;
  rows: ASTNode[][]; // row-based
}

/** ( ... ), { ... }, \left(\right) => bracket node */
export interface BracketNode extends ASTNode {
  type: "Bracket";
  leftDelim: string;
  rightDelim: string;
  content: ASTNode;
}