"use strict";

const fs = require("node:fs");
const katex = require("../../web/vendor/katex-0.18.1.min.js");
const { tokenizeLatex } = require("./dist/latexTokenizer.js");
const { LatexParser } = require("./dist/latexParser.js");
const { TokenType } = require("./dist/tokenTypes.js");
const { toHwpEqn } = require("./dist/toHwpEqn.js");

function checkArguments(node) {
  if (!node || typeof node !== "object") return;
  const required = {
    Fraction: ["numerator", "denominator"], Binom: ["upper", "lower"],
    Root: ["radicand"], Superscript: ["exponent"], Subscript: ["sub"],
  }[node.type] || [];
  for (const name of required) {
    if (!node[name] || !toHwpEqn(node[name]).trim()) {
      throw new Error(`unsupported or missing ${node.type}.${name}`);
    }
  }
  for (const child of Object.values(node)) {
    if (Array.isArray(child)) child.forEach(checkArguments);
    else if (child && typeof child === "object") checkArguments(child);
  }
}

try {
  const input = JSON.parse(fs.readFileSync(0, "utf8"));
  if (!input || typeof input.latex !== "string") {
    throw new Error("stdin must be JSON with a latex string");
  }
  katex.renderToString(input.latex, { throwOnError: true, trust: false, strict: "ignore" });
  const latex = input.latex.replace(/\\(?:dfrac|tfrac)(?![A-Za-z])/g, "\\frac");
  const parser = new LatexParser(tokenizeLatex(latex));
  const ast = parser.parseExpression();
  if (parser.peek().type !== TokenType.EOF) {
    throw new Error(`unconsumed equation input at ${parser.peek().value}`);
  }
  checkArguments(ast);
  const hwp_script = toHwpEqn(ast).replace(/\s+/g, " ").trim();
  process.stdout.write(JSON.stringify({ hwp_script }));
} catch (error) {
  process.stderr.write(String(error.message || error));
  process.exitCode = 1;
}
