"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.LatexParser = void 0;
const tokenTypes_1 = require("./tokenTypes");
class LatexParser {
    constructor(tokens) {
        this.pos = 0;
        /** 현재 기대하는 닫는 구분자 (")" / "]" / "}" / ""=최상위). atSequenceBoundary 가 사용. */
        this.closer = "";
        this.tokens = tokens.filter(t => t.type !== tokenTypes_1.TokenType.SPACE && t.type !== tokenTypes_1.TokenType.UNKNOWN);
    }
    peek() { return this.tokens[this.pos] || { type: tokenTypes_1.TokenType.EOF, value: "" }; }
    next() { const t = this.peek(); if (t.type !== tokenTypes_1.TokenType.EOF)
        this.pos++; return t; }
    matchSymbol(v) {
        const t = this.peek();
        return (t.type === tokenTypes_1.TokenType.SYMBOL && t.value === v);
    }
    matchKeyword(k) {
        const t = this.peek();
        return (t.type === tokenTypes_1.TokenType.KEYWORD && t.value.toLowerCase() === k.toLowerCase());
    }
    parseExpression() {
        // exam-studio 패치: 원본은 parseExpr() 한 번만 호출해 `+ - times /` 사슬만
        // 받고 `=` `,` 관계연산·병치(juxtaposition)에서 멈춰 나머지 토큰을 버렸다.
        // 경계 토큰을 만날 때까지 항을 반복 수집해 Sequence 로 보존한다.
        const items = [];
        while (!this.atSequenceBoundary()) {
            const before = this.pos;
            items.push(this.parseExpr());
            if (this.pos === before) {
                // 안전장치: 진전이 없으면 한 토큰을 리터럴로 소비 (무한루프 방지)
                const t = this.next();
                items.push({ type: "Literal", value: t.value });
            }
        }
        if (items.length === 0)
            return { type: "Literal", value: "" };
        if (items.length === 1)
            return items[0];
        return { type: "Sequence", items };
    }
    /** 닫는 구분자 `close` 를 경계로 하여 그 안의 식을 파싱 (closer 컨텍스트 저장/복원). */
    parseDelimited(close) {
        const prev = this.closer;
        this.closer = close;
        const node = this.parseExpression();
        this.closer = prev;
        return node;
    }
    /**
     * 시퀀스/하위식의 끝인가. 닫는 괄호 `) ] }` 는 '현재 기대하는 닫는 구분자'일 때만
     * 경계다 — 그래서 `{A)^2}`(중괄호 안의 ')') 처럼 짝 안 맞는 닫는기호는 리터럴로 흡수돼
     * 그룹을 일찍 끊지 않는다(HWP rm-스코프 LaTeX·조각수식 대응).
     */
    atSequenceBoundary() {
        const t = this.peek();
        if (t.type === tokenTypes_1.TokenType.EOF)
            return true;
        if (t.type === tokenTypes_1.TokenType.SYMBOL && (t.value === "&" || t.value === "#" || t.value === "\\\\"))
            return true;
        if (t.type === tokenTypes_1.TokenType.SYMBOL && this.closer !== "" && t.value === this.closer)
            return true;
        if (t.type === tokenTypes_1.TokenType.KEYWORD && (t.value === "end" || t.value === "right"))
            return true;
        return false;
    }
    parseExpr() {
        let node = this.parseTerm();
        while (true) {
            const t = this.peek();
            if (t.type === tokenTypes_1.TokenType.SYMBOL && (t.value === '+' || t.value === '-')) {
                this.next();
                const r = this.parseTerm();
                node = { type: "BinaryOp", operator: t.value, left: node, right: r };
            }
            else {
                break;
            }
        }
        return node;
    }
    parseTerm() {
        let node = this.parseFactor();
        while (true) {
            const t = this.peek();
            if (t.type === tokenTypes_1.TokenType.KEYWORD && t.value === "times") {
                this.next();
                const r = this.parseFactor();
                node = { type: "BinaryOp", operator: "times", left: node, right: r };
            }
            else if (t.type === tokenTypes_1.TokenType.SYMBOL && t.value === "/") {
                this.next();
                const r = this.parseFactor();
                node = { type: "BinaryOp", operator: "/", left: node, right: r };
            }
            else {
                break;
            }
        }
        return node;
    }
    parseFactor() {
        const fc = this.tryParseFunctionCall();
        if (fc)
            return fc;
        const t = this.peek();
        if (t.type === tokenTypes_1.TokenType.NUMBER) {
            return this.parseLiteralFactor(t.value);
        }
        if (t.type === tokenTypes_1.TokenType.IDENT) {
            return this.parseLiteralFactor(t.value);
        }
        if (t.type === tokenTypes_1.TokenType.KEYWORD) {
            this.next();
            return this.parseKeywordFactor(t.value.toLowerCase());
        }
        if (t.type === tokenTypes_1.TokenType.SYMBOL) {
            return this.parseSymbolFactor(t.value);
        }
        // else
        this.next();
        return { type: "Literal", value: "" };
    }
    parseLiteralFactor(value) {
        this.next();
        const lit = { type: "Literal", value };
        return this.maybeParseSubSup(lit);
    }
    parseKeywordFactor(kw) {
        if (kw === "left")
            return this.parseLeftRightBracket();
        if (kw === "right")
            return { type: "Literal", value: "" }; // 짝 없는 \right → 무시
        if (kw === "begin")
            return this.parseBeginEnv();
        if (kw === "end")
            return this.parseEndKeywordFactor();
        if (kw === "sqrt")
            return this.parseSqrtFactor();
        if (kw === "binom")
            return this.parseBinomFactor();
        if (kw === "frac")
            return this.parseFracFactor();
        if (kw === "int" || kw === "oint")
            return this.parseIntegralFactor(kw);
        if (kw === "sum")
            return this.parseSumFactor();
        if (kw === "overset")
            return this.parseOversetFactor();
        if (LatexParser.DECORATED_KEYWORDS.has(kw))
            return this.parseDecoratedFactor(kw);
        // fallback: 미처리 키워드(예: factor 위치의 times)는 백슬래시 없이 그대로.
        // (HWP 문법이 TeX 계열이라 bare 이름이 곧 HWP 토큰. \times→times→×)
        return { type: "Literal", value: kw };
    }
    parseEndKeywordFactor() {
        this.next();
        return { type: "Literal", value: "" };
    }
    parseSqrtFactor() {
        // \sqrt[n]{x} 의 선택적 차수 [n]
        let degree;
        if (this.matchSymbol("[")) {
            this.next();
            degree = this.parseDelimited("]");
            if (this.matchSymbol("]"))
                this.next();
        }
        // 인자는 subsup 없이 읽는다(중괄호 그룹 또는 단일 토큰). 뒤따르는 ^/_ 는
        // 근호 안이 아니라 근호 '전체'에 붙어야 한다: \sqrt{x}^2 = (√x)^2.
        const rad = this.parseSubOrSupContent();
        const root = { type: "Root", radicand: rad };
        if (degree)
            root.degree = degree;
        return this.maybeParseSubSup(root);
    }
    parseBinomFactor() {
        // \binom{n}{k} → 두 중괄호 인수
        const upper = this.parseOptionalBraceArgument();
        const lower = this.parseOptionalBraceArgument();
        return this.maybeParseSubSup({ type: "Binom", upper, lower });
    }
    parseFracFactor() {
        // parse \frac{...}{...}
        const num = this.parseOptionalBraceArgument();
        const den = this.parseOptionalBraceArgument();
        return this.maybeParseSubSup({ type: "Fraction", numerator: num, denominator: den, withBar: true });
    }
    parseOptionalBraceArgument() {
        if (!this.matchSymbol("{")) {
            return { type: "Literal", value: "" };
        }
        this.next();
        const node = this.parseDelimited("}");
        if (this.matchSymbol("}"))
            this.next();
        return node;
    }
    parseIntegralFactor(kw) {
        const variant = (kw === "int") ? "int" : "oint";
        const iNode = { type: "Integral", variant };
        this.parseIntegralSubSup(iNode);
        iNode.body = this.removeOuterBodyBracket(this.parseFactor());
        return iNode;
    }
    parseSumFactor() {
        const sNode = { type: "Summation" };
        this.parseSumSubSup(sNode);
        sNode.body = this.removeOuterBodyBracket(this.parseFactor());
        return sNode;
    }
    removeOuterBodyBracket(node) {
        if (node.type !== "Bracket")
            return node;
        return this.removeBracket(node);
    }
    parseOversetFactor() {
        // \overset{위}{밑}. 호(弧) `\overset{\frown}{AB}` → HWP arch{AB}.
        // 그 외 overset 은 HWP 직접 대응이 없어 밑(base)만 남긴다.
        const over = this.parseSubOrSupContent();
        const base = this.parseSubOrSupContent();
        const isFrown = over.type === "Literal" && over.value === "frown";
        if (isFrown) {
            return this.maybeParseSubSup({ type: "Decorated", decoType: "arch", child: base });
        }
        return this.maybeParseSubSup(base);
    }
    parseDecoratedFactor(kw) {
        const child = this.parseSubOrSupContent();
        return this.maybeParseSubSup({ type: "Decorated", decoType: kw, child });
    }
    parseSymbolFactor(value) {
        if ("([{".includes(value))
            return this.parseBracketFactor(value);
        if (value === "\\{")
            return this.parseVisibleBraceFactor();
        if (")]}".includes(value))
            return this.parseClosingSymbolFactor(value);
        if (value === "\\\\") {
            this.next();
            return { type: "Literal", value: "\\\\" };
        }
        this.next();
        return { type: "Literal", value };
    }
    parseBracketFactor(left) {
        this.next();
        const close = left === "(" ? ")" : left === "[" ? "]" : "}";
        const content = this.parseDelimited(close);
        let right = "";
        if (this.matchSymbol(close)) {
            right = this.peek().value;
            this.next();
        }
        const br = { type: "Bracket", leftDelim: left, rightDelim: right, content };
        return this.maybeParseSubSup(br);
    }
    parseVisibleBraceFactor() {
        // 보이는 집합/묶음 중괄호 \{ … \} = \left\{ … \right\} 와 동치로 취급.
        // (HWP `{ }` 는 안 보이는 묶음이라, 보이는 중괄호는 LEFT { … RIGHT } 로만 표현된다.)
        // 단일 factor 로 묶이므로 적분/합 body 가 통째로 흡수 → 내용 누수·빈 중괄호 방지.
        this.next();
        const content = this.parseDelimited("\\}");
        const rightD = this.matchSymbol("\\}") ? (this.next(), "\\right}") : "\\right.";
        const br = { type: "Bracket", leftDelim: "\\left{", rightDelim: rightD, content };
        return this.maybeParseSubSup(br);
    }
    parseClosingSymbolFactor(value) {
        // 현재 그룹의 닫는기호면 소비하지 않는다 — 상위 경계검사가 처리.
        // (연산자 루프 `a+}` 처럼 +/- 뒤에서 parseFactor 가 불려 닫는기호를 피연산자로
        //  삼키면 `\lim_{x\to 0+}` 우극한 등에서 그룹 경계가 깨져 중괄호 불균형이 난다.)
        if (value === this.closer) {
            return { type: "Literal", value: "" };
        }
        // 짝 없는 닫는기호 → 리터럴 (뒤따르는 ^/_ 도 흡수)
        this.next();
        return this.maybeParseSubSup({ type: "Literal", value });
    }
    /** f(...) or f\left(...) => operator= "apply" */
    tryParseFunctionCall() {
        const t1 = this.peek();
        if (t1.type === tokenTypes_1.TokenType.IDENT) {
            const t2 = this.tokens[this.pos + 1];
            if (t2 && t2.type === tokenTypes_1.TokenType.KEYWORD && t2.value === "left") {
                // f \left(...)  — parseLeftRightBracket 은 `\left` 가 소비된 상태를 전제로 하므로
                // (parseFactor 의 left 분기와 동일) 여기서도 IDENT 와 `\left` 를 둘 다 소비해야 한다.
                // 안 그러면 consumeDelim 이 델리미터를 못 찾아 closer="" 로 파싱 → 바깥 `}` 등을
                // 경계로 인식 못 하고 뒤 토큰(분모 등)을 통째로 삼킨다.
                this.next(); // IDENT (f)
                this.next(); // \left
                const fLit = { type: "Literal", value: t1.value };
                const bracket = this.parseLeftRightBracket();
                const node = { type: "BinaryOp", operator: "apply", left: fLit, right: bracket };
                return this.maybeParseSubSup(node);
            }
            else if (t2 && t2.type === tokenTypes_1.TokenType.SYMBOL && "([{".includes(t2.value)) {
                // f(
                this.next();
                const fLit = { type: "Literal", value: t1.value };
                const bracket = this.parseFactor();
                const node = { type: "BinaryOp", operator: "apply", left: fLit, right: bracket };
                return this.maybeParseSubSup(node);
            }
        }
        return undefined;
    }
    parseLeftRightBracket() {
        const leftRaw = this.consumeDelim();
        const leftD = leftRaw === "" ? "" : "\\left" + leftRaw;
        // \right 키워드가 구조적 경계라 closer="" 로도 안전. 내부 ')' 등은 리터럴 흡수.
        const content = this.parseDelimited("");
        let rightD = "";
        if (this.matchKeyword("right")) {
            this.next();
            const rightRaw = this.consumeDelim();
            rightD = rightRaw === "" ? "\\right." : "\\right" + rightRaw;
        }
        if (leftD === "" && rightD === "") {
            return content;
        }
        let br = { type: "Bracket", leftDelim: leftD, rightDelim: rightD, content };
        br = this.flattenBracket(br);
        return this.maybeParseSubSup(br);
    }
    /** \left / \right 뒤의 델리미터 한 개를 소비해 원시 표기를 반환. 없으면 "". */
    consumeDelim() {
        const t = this.peek();
        if (t.type === tokenTypes_1.TokenType.SYMBOL && t.value === "\\{") {
            this.next();
            return "{";
        }
        if (t.type === tokenTypes_1.TokenType.SYMBOL && t.value === "\\}") {
            this.next();
            return "}";
        }
        if (t.type === tokenTypes_1.TokenType.SYMBOL && "()[]{}|.<>".includes(t.value)) {
            this.next();
            return t.value;
        }
        if (t.type === tokenTypes_1.TokenType.IDENT && LatexParser.DELIM_IDENTS.has(t.value)) {
            this.next();
            return t.value;
        }
        return "";
    }
    flattenBracket(br) {
        if (br.content.type === "Bracket") {
            const inner = br.content;
            if (br.leftDelim === inner.leftDelim && br.rightDelim === inner.rightDelim) {
                if (br.leftDelim === "{" || br.leftDelim === "") {
                    return inner;
                }
            }
        }
        return br;
    }
    removeBracket(br) {
        if (br.leftDelim === "{" || br.leftDelim === "") {
            return br.content;
        }
        return br;
    }
    /** \begin{pmatrix}, \begin{cases} => parse until \end{...} */
    parseBeginEnv() {
        if (!this.matchSymbol("{")) {
            return { type: "Literal", value: "\\begin" };
        }
        this.next();
        const envTok = this.peek();
        let envName = "";
        if (envTok.type === tokenTypes_1.TokenType.IDENT || envTok.type === tokenTypes_1.TokenType.KEYWORD) {
            envName = envTok.value.toLowerCase();
            this.next();
        }
        if (this.matchSymbol("}"))
            this.next();
        const node = this.parseBeginEnvContent(envName);
        return node;
    }
    parseBeginEnvContent(envName) {
        // 행은 `\\`, 열(셀)은 `&` 로 구분. 각 셀은 그 안의 factor 들을 모은 Sequence.
        // (원본은 `&` 를 무시하고 모든 factor 를 평면 리스트로 모아, 디코더가 토큰마다
        //  열로 쪼개는 버그가 있었다 — piecewise 함수가 깨짐.)
        const rows = [];
        let row = [];
        let cell = [];
        const flushCell = () => {
            row.push(cell.length === 1 ? cell[0] : { type: "Sequence", items: cell });
            cell = [];
        };
        const flushRow = () => { flushCell(); rows.push(row); row = []; };
        while (!this.isEndEnv(envName)) {
            const tk = this.peek();
            if (tk.type === tokenTypes_1.TokenType.EOF)
                break;
            if (tk.type === tokenTypes_1.TokenType.SYMBOL && tk.value === "\\\\") {
                this.next();
                flushRow();
            }
            else if (tk.type === tokenTypes_1.TokenType.SYMBOL && tk.value === "&") {
                this.next();
                flushCell();
            }
            else {
                const f = this.parseFactor();
                if (f.type !== "Literal" || f.value !== "")
                    cell.push(f);
            }
        }
        if (cell.length > 0 || row.length > 0)
            flushRow();
        this.parseEndEnv();
        return this.maybeParseSubSup({ type: "BeginEnv", envName, rows });
    }
    isEndEnv(envName) {
        if (this.matchKeyword("end")) {
            const s1 = this.tokens[this.pos + 1];
            if (s1 && s1.type === tokenTypes_1.TokenType.SYMBOL && s1.value === "{") {
                const s2 = this.tokens[this.pos + 2];
                if (s2 && (s2.type === tokenTypes_1.TokenType.IDENT || s2.type === tokenTypes_1.TokenType.KEYWORD)
                    && s2.value.toLowerCase() === envName) {
                    return true;
                }
            }
        }
        return false;
    }
    parseEndEnv() {
        if (this.matchKeyword("end")) {
            this.next();
            if (this.matchSymbol("{")) {
                this.next();
                const n = this.peek();
                if (n.type === tokenTypes_1.TokenType.IDENT || n.type === tokenTypes_1.TokenType.KEYWORD) {
                    this.next();
                }
                if (this.matchSymbol("}"))
                    this.next();
            }
        }
    }
    /** parse sub/sup for integrals, sums => handle { ... } */
    parseIntegralSubSup(iNode) {
        if (this.matchSymbol("_")) {
            this.next();
            iNode.lower = this.parseSubOrSupContent();
        }
        if (this.matchSymbol("^")) {
            this.next();
            iNode.upper = this.parseSubOrSupContent();
        }
    }
    parseSumSubSup(sNode) {
        if (this.matchSymbol("_")) {
            this.next();
            sNode.lower = this.parseSubOrSupContent();
        }
        if (this.matchSymbol("^")) {
            this.next();
            sNode.upper = this.parseSubOrSupContent();
        }
    }
    /** if next == '{', parse expression inside '}', else single token */
    parseSubOrSupContent() {
        if (this.matchSymbol("{")) {
            this.next();
            const expr = this.parseDelimited("}");
            if (this.matchSymbol("}")) {
                this.next();
            }
            return expr;
        }
        else {
            // single token
            const t = this.peek();
            if (t.type !== tokenTypes_1.TokenType.EOF) {
                this.next();
                return { type: "Literal", value: t.value };
            }
            return { type: "Literal", value: "" };
        }
    }
    /** after factor => maybe ^ or _ => similar logic */
    maybeParseSubSup(base) {
        let node = base;
        while (true) {
            const t = this.peek();
            if (t.type === tokenTypes_1.TokenType.SYMBOL && t.value === "^") {
                this.next();
                const exp = this.parseSubOrSupContent();
                node = { type: "Superscript", base: node, exponent: exp };
            }
            else if (t.type === tokenTypes_1.TokenType.SYMBOL && t.value === "_") {
                this.next();
                const sub = this.parseSubOrSupContent();
                node = { type: "Subscript", base: node, sub };
            }
            else {
                break;
            }
        }
        return node;
    }
}
exports.LatexParser = LatexParser;
/** \left / \right 뒤에 올 수 있는 IDENT 델리미터 (이스케이프/명령형). */
LatexParser.DELIM_IDENTS = new Set([
    "lbrace", "rbrace", "lfloor", "rfloor", "lceil", "rceil",
    "langle", "rangle", "vert", "Vert", "lvert", "rvert",
]);
LatexParser.DECORATED_KEYWORDS = new Set([
    "acute", "grave", "dot", "ddot", "bar", "vec", "hat", "tilde", "check",
    "overrightarrow", "overleftarrow", "boxed", "overbrace", "underbrace",
]);
