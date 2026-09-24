"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.toHwpEqn = toHwpEqn;
/**
 * LaTeX 명령명 → HWP 수식 토큰이 갈라지는 소수의 경우만 매핑.
 * (그리스·관계연산·cdot 등 대다수는 TeX 파생이라 그대로 일치하므로 매핑 불필요.)
 */
const LITERAL_MAP = {
    // 폰트/구조
    mathrm: "rm",
    text: "rm",
    mathbf: "bold",
    mathit: "it",
    // 이름이 갈라지는 기호 (참조 카탈로그 대조로 확정)
    infty: "inf",
    ldots: "cdots",
    dots: "cdots",
    to: "rightarrow",
    Rightarrow: "RARROW",
    Leftarrow: "LARROW",
    Leftrightarrow: "LRARROW",
    Longrightarrow: "RARROW",
    Longleftrightarrow: "LRARROW",
    iint: "dint",
    iiint: "tint",
    pm: "±",
    bigcup: "union",
    bigcap: "inter",
    land: "wedge",
    lor: "vee",
    quad: "~",
    qquad: "~ ~",
    // 독립(\left 밖) 리터럴 중괄호·set 명령
    "\\{": "{",
    "\\}": "}",
    lbrace: "{",
    rbrace: "}",
};
/**
 * over/sqrt/첨자의 피연산자는 단일 리터럴이 아니면 `{ }` 로 묶어 HWP 결합을 보존한다.
 * Bracket(절댓값 LEFT|…RIGHT| 포함)도 묶는다 — 안 묶으면 `r^ LEFT|…` 처럼 ^/over 가
 * 첫 토큰(LEFT)에만 붙어 깨진다.
 */
function grp(node) {
    const s = toHwpEqn(node);
    if (node.type === "Literal")
        return s;
    return `{ ${s} }`;
}
/** 빈 base(`{}` 또는 빈 리터럴)에 붙은 아래첨자인가 — 좌측첨자(prescript)의 머리. */
function isPrescriptHead(node) {
    if (node.type !== "Subscript")
        return false;
    const base = node.base;
    if (base.type === "Literal")
        return base.value === "";
    if (base.type === "Bracket") {
        const c = base.content;
        return c.type === "Literal" && c.value === "";
    }
    return false;
}
/** P/C/H 글자를 찾아 반환 — `\mathrm{P}`/`{P}`/`P` 등 래핑을 벗긴다. */
function combLetter(node) {
    if (node.type === "Bracket")
        return combLetter(node.content);
    if (node.type === "Literal") {
        const v = node.value;
        return v === "P" || v === "C" || v === "H" ? v : null;
    }
    return null;
}
/**
 * P/C/H(순열·조합·중복조합)에 오른쪽 아래첨자가 붙은 형태인가.
 * 두 형태 모두 인식: bare `P_{r}`(Subscript(Literal P)), 그리고 프롬프트가 지시하는
 * `\mathrm{P}_{r}`(apply(mathrm, Subscript(Bracket{P}))). 후자를 놓치면 좌측첨자 접기가
 * 안 돼 `{ {} }_n rm{ {P} }_r` 처럼 깨진다.
 */
function asCombOp(node) {
    // \mathrm{P}_r → apply(mathrm, Subscript(...)) : 폰트 래퍼를 벗기고 안쪽으로.
    if (node.type === "BinaryOp" && node.operator === "apply") {
        const l = node.left;
        if (l.type === "Literal" && ["mathrm", "rm", "text"].includes(l.value)) {
            return asCombOp(node.right);
        }
        return null;
    }
    if (node.type !== "Subscript")
        return null;
    const op = combLetter(node.base);
    return op ? { op, r: node.sub } : null;
}
/**
 * 좌측첨자(prescript) 접기 — 순열 nPr / 조합 nCr / 중복조합 nHr.
 * LaTeX `{}_{n}P_{r}` → AST [Subscript(빈base, n), Subscript("P", r)] 를
 * HWP 정식형 `{rm P it _r} LSUB {n}` 으로 합친다.
 * `{rm P}` 형태라 equation.py R-08 의 skip-guard 가 걸려 후처리 중복이 없다.
 */
function foldPrescripts(items) {
    const out = [];
    for (let i = 0; i < items.length; i++) {
        const head = items[i];
        const chainedComb = i + 2 < items.length ? splitBinaryWithTrailingPrescript(items[i + 1], items[i + 2]) : null;
        if (isPrescriptHead(head) && chainedComb) {
            const n = toHwpEqn(head.sub);
            const left = formatPrescriptComb(chainedComb.left.op, n, toHwpEqn(chainedComb.left.r));
            const rightN = toHwpEqn(chainedComb.rightHead.sub);
            const right = formatPrescriptComb(chainedComb.rightComb.op, rightN, toHwpEqn(chainedComb.rightComb.r));
            out.push(`${left} ${chainedComb.operator} ${right}${chainedComb.rightComb.suffix}`);
            i += 2;
            continue;
        }
        const prescriptComb = i + 1 < items.length ? splitLeadingCombExpression(items[i + 1]) : null;
        if (isPrescriptHead(head) && prescriptComb) {
            const n = toHwpEqn(head.sub);
            out.push(formatPrescriptComb(prescriptComb.op, n, toHwpEqn(prescriptComb.r)) + prescriptComb.suffix);
            i++; // P/C/H 항 또는 P/C/H로 시작하는 이항식을 소비
        }
        else {
            out.push(toHwpEqn(head));
        }
    }
    return out;
}
function formatPrescriptComb(op, n, r) {
    const rSub = /^[A-Za-z0-9]+$/.test(r) ? `_${r}` : `_{${r}}`;
    return `{rm ${op} it ${rSub}} LSUB {${n}}`;
}
function splitBinaryWithTrailingPrescript(node, following) {
    if (node.type !== "BinaryOp")
        return null;
    const b = node;
    const left = asCombOp(b.left);
    if (!left || !isPrescriptHead(b.right) || !["+", "-", "/", "times"].includes(b.operator))
        return null;
    const rightComb = splitLeadingCombExpression(following);
    if (!rightComb)
        return null;
    return {
        left,
        operator: b.operator === "times" ? "times" : b.operator,
        rightHead: b.right,
        rightComb,
    };
}
function splitLeadingCombExpression(node) {
    const direct = asCombOp(node);
    if (direct)
        return { ...direct, suffix: "" };
    if (node.type !== "BinaryOp")
        return null;
    const b = node;
    const leftComb = asCombOp(b.left);
    if (!leftComb || !["+", "-", "/", "times"].includes(b.operator))
        return null;
    const opText = b.operator === "times" ? "times" : b.operator;
    return { ...leftComb, suffix: ` ${opText} ${toHwpEqn(b.right)}` };
}
/**
 * \left/\right 델리미터 원시표기 → HWP (참조 변환기 되읽기로 검증한 형태).
 * 집합괄호=리터럴 { }, angle=< >, vert=|, floor/ceil=유니코드 ⌊⌋⌈⌉.
 */
const DELIM_HWP = {
    "(": "(", ")": ")", "[": "[", "]": "]", "|": "|", "<": "<", ">": ">", ".": ".",
    "{": "{", "}": "}", lbrace: "{", rbrace: "}",
    langle: "<", rangle: ">", vert: "|", lvert: "|", rvert: "|", Vert: "|",
    lfloor: "⌊", rfloor: "⌋", lceil: "⌈", rceil: "⌉",
};
/** 괄호기호 ( ) [ ] | 는 LEFT 에 붙이고(LEFT(), 그 외는 띄운다(LEFT {). */
function hwpDelim(kw, raw) {
    const d = DELIM_HWP[raw] ?? raw;
    return "()[]|".includes(d) ? `${kw}${d}` : `${kw} ${d}`;
}
function toHwpEqn(node) {
    switch (node.type) {
        case "Literal": {
            const v = node.value;
            return LITERAL_MAP[v] ?? v;
        }
        case "Sequence":
            return foldPrescripts(node.items).join(" ");
        case "BinaryOp": {
            const b = node;
            const left = toHwpEqn(b.left);
            const right = toHwpEqn(b.right);
            if (b.operator === "apply") {
                return left + right;
            }
            switch (b.operator) {
                case "times": return `${left} times ${right}`;
                case "+":
                case "-":
                case "/":
                    return `${left} ${b.operator} ${right}`;
                default:
                    return `${left} ${b.operator} ${right}`;
            }
        }
        case "Fraction": {
            const f = node;
            const num = grp(f.numerator);
            const den = grp(f.denominator);
            return f.withBar ? `${num} over ${den}` : `${num} atop ${den}`;
        }
        case "Root": {
            const r = node;
            // n제곱근: root {n} of {x} (참조 카탈로그 generator base.py:517 / 한컴매뉴얼)
            if (r.degree)
                return `root ${grp(r.degree)} of ${grp(r.radicand)}`;
            return `sqrt ${grp(r.radicand)}`;
        }
        case "Binom": {
            // \binom{n}{k} → {n} choose {k} (조합 ₙCᵣ, 한컴매뉴얼 CHOOSE)
            const bn = node;
            return `${grp(bn.upper)} choose ${grp(bn.lower)}`;
        }
        case "Superscript": {
            const s = node;
            // base 가 복합(루트·분수 등)이면 묶는다: (√x)^2 = { sqrt x }^2
            return `${grp(s.base)}^${grp(s.exponent)}`;
        }
        case "Subscript": {
            const sb = node;
            return `${grp(sb.base)}_${grp(sb.sub)}`;
        }
        case "Integral": {
            const i = node;
            let out = (i.variant === "int") ? "int" : "oint";
            if (i.lower)
                out += `_${grp(i.lower)}`;
            if (i.upper)
                out += `^${grp(i.upper)}`;
            if (i.body) {
                // keep double braces => no flatten
                out += ` { { ${toHwpEqn(i.body)} } }`;
            }
            return out;
        }
        case "Summation": {
            const s = node;
            let out = "sum";
            if (s.lower)
                out += `_${grp(s.lower)}`;
            if (s.upper)
                out += `^${grp(s.upper)}`;
            if (s.body) {
                out += ` { ${toHwpEqn(s.body)} }`;
            }
            return out;
        }
        case "Decorated": {
            const d = node;
            // 중괄호 인수형: \boxed{ }→box{ }(빈칸), \overbrace/\underbrace 그대로
            if (d.decoType === "arch")
                return `arch{${toHwpEqn(d.child)}}`; // 호(弧)
            if (d.decoType === "boxed")
                return `box{${toHwpEqn(d.child)}}`;
            if (d.decoType === "overbrace" || d.decoType === "underbrace")
                return `${d.decoType}{${toHwpEqn(d.child)}}`;
            // \overrightarrow/\overleftarrow(벡터) → HWP vec. 나머지 악센트는 이름 일치.
            const deco = (d.decoType === "overrightarrow" || d.decoType === "overleftarrow") ? "vec" : d.decoType;
            return `${deco} ${toHwpEqn(d.child)}`;
        }
        case "BeginEnv": {
            // pmatrix{1 & 2 # 3 & 4}, cases{ ... # ... } — 셀은 이미 Sequence 로 묶여 옴.
            const be = node;
            const rowStrs = be.rows.map(r => r.map(toHwpEqn).join(" & ")).join(" # ");
            return `${be.envName}{${rowStrs}}`;
        }
        case "Bracket": {
            const br = node;
            const content = toHwpEqn(br.content);
            if (br.leftDelim.startsWith("\\left") || br.rightDelim.startsWith("\\right")) {
                const left = br.leftDelim.startsWith("\\left")
                    ? hwpDelim("LEFT", br.leftDelim.slice(5)) : "";
                const right = br.rightDelim.startsWith("\\right")
                    ? hwpDelim("RIGHT", br.rightDelim.slice(6)) : "";
                return ` ${left} ${content} ${right}`;
            }
            // 평범한 ( ) [ ] { } 그룹
            return `${br.leftDelim}${content}${br.rightDelim}`;
        }
        default:
            return "";
    }
}
