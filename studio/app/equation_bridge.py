"""Isolated LaTeX-to-HWP bridge with science review gates."""

from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "vendor" / "exam_studio_equation" / "cli.cjs"
CONVERTER_VERSION: Final = "astra-equation-v1:" + hashlib.sha256(b"".join(
    path.read_bytes() for path in [Path(__file__), CLI, *sorted((CLI.parent / "dist").glob("*.js")),
                                  ROOT / "web/vendor/katex-0.18.1.min.js"]
)).hexdigest()
MAX_LATEX_LENGTH: Final = 16384
_COMMAND = re.compile(r"\\([A-Za-z]+)")
_REVIEW_COMMANDS = {
    "ce": "mhchem \\ce syntax is not supported",
    "rightleftharpoons": "chemical equilibrium arrow needs an HWP ISO mapping",
    "leftrightharpoons": "chemical equilibrium arrow needs an HWP ISO mapping",
    "circ": "degree/composition symbol is context-sensitive and needs review",
    "xcancel": "LaTeX xcancel is not a native HWP equation command; use text strike styling",
    "overleftarrow": "semantic_loss: overleftarrow left direction becomes right-pointing vec",
    "overrightarrow": "semantic_loss: overrightarrow directional extent becomes generic vec",
    "Vert": "semantic_loss: Vert double bar becomes a single bar",
    "lVert": "semantic_loss: lVert double bar has no verified HWP mapping",
    "rVert": "semantic_loss: rVert double bar has no verified HWP mapping",
}

_SUPPORTED_COMMANDS: Final = frozenset("""
int oint sum sqrt frac dfrac tfrac binom acute grave dot ddot bar vec hat tilde check
 overrightarrow overleftarrow boxed overbrace underbrace times overset left right begin end
 mathrm text mathbf mathit rm infty ldots dots to Rightarrow Leftarrow Leftrightarrow
 Longrightarrow Longleftrightarrow iint iiint pm bigcup bigcap land lor quad qquad
 lbrace rbrace langle rangle vert lvert rvert Vert lVert rVert lfloor rfloor lceil rceil
 alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi
 rho sigma tau upsilon phi chi psi omega Gamma Delta Theta Lambda Xi Pi Sigma Upsilon Phi Psi Omega
 hbar imath jmath vartheta varpi varsigma varphi varepsilon
 cap cup sqcap sqcup oplus otimes odot wedge vee subset supset subseteq supseteq
 in ni notin leq geq le ge ll gg prec succ uplus mp div circ bullet ast star bigcirc
 emptyset therefore because equiv exists neq ne doteq sim approx simeq cong asymp
 diamond forall prime partial neg propto nabla dagger ddagger
 leftarrow rightarrow uparrow downarrow leftrightarrow updownarrow
 Uparrow Downarrow Updownarrow nwarrow searrow nearrow swarrow mapsto
 cdot cdots vdots ddots triangle angle vdash dashv bot top models frown
 sin cos tan cot sec csc sinh cosh tanh log ln exp lim max min sup inf det gcd
""".split()) | frozenset(_REVIEW_COMMANDS)


class EquationBridgeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EquationConversion:
    latex: str
    hwp_script: str
    warnings: tuple[str, ...]

    @property
    def review_required(self) -> bool:
        return bool(self.warnings)


def science_warnings(latex: str) -> tuple[str, ...]:
    warnings = []
    for command in sorted(set(_COMMAND.findall(latex))):
        if command in _REVIEW_COMMANDS:
            warnings.append(_REVIEW_COMMANDS[command])
    if re.search(r"\{\}\s*\^\{?[^}]+\}?\s*_\{?[^}]+\}?", latex):
        warnings.append("left isotope superscript/subscript needs LSUP/LSUB review")
    if "overset" in _COMMAND.findall(latex) and not re.fullmatch(r"\\overset\s*\{\\frown\}\s*\{[^{}]+\}", latex):
        warnings.append("semantic_loss: overset annotation may be discarded by the converter")
    if r"\|" in latex or "‖" in latex:
        warnings.append("semantic_loss: double-bar delimiter has no verified HWP mapping")
    return tuple(dict.fromkeys(warnings))


def convert_latex(latex: str, *, timeout_seconds: float = 15) -> EquationConversion:
    if not isinstance(latex, str) or not latex.strip():
        raise EquationBridgeError("latex must be a non-empty string")
    if len(latex) > MAX_LATEX_LENGTH:
        raise EquationBridgeError(f"latex exceeds {MAX_LATEX_LENGTH} characters")
    return _convert_latex(latex, timeout_seconds, CONVERTER_VERSION)


@lru_cache(maxsize=512)
def _convert_latex(latex: str, timeout_seconds: float, converter_version: str) -> EquationConversion:
    unsupported = sorted(set(_COMMAND.findall(latex)) - _SUPPORTED_COMMANDS)
    if unsupported:
        raise EquationBridgeError("unsupported LaTeX command: " + ", ".join(unsupported))
    node = shutil.which("node")
    if not node:
        raise EquationBridgeError("Node.js is required for the vendored equation converter")
    if not CLI.is_file():
        raise EquationBridgeError(f"equation converter CLI is missing: {CLI}")
    environment = {
        key: value
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
        if (value := os.environ.get(key))
    }
    try:
        process = subprocess.run(
            [node, str(CLI)],
            input=json.dumps({"latex": latex}, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout_seconds,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise EquationBridgeError("equation converter timed out") from exc
    except OSError as exc:
        raise EquationBridgeError(f"equation converter could not start: {exc.strerror}") from exc
    if process.returncode != 0:
        message = process.stderr.strip().splitlines()[-1] if process.stderr.strip() else "unknown error"
        raise EquationBridgeError(f"equation converter failed: {message[:300]}")
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        raise EquationBridgeError("equation converter returned invalid JSON") from exc
    hwp_script = payload.get("hwp_script") if isinstance(payload, dict) else None
    if not isinstance(hwp_script, str) or not hwp_script.strip():
        raise EquationBridgeError("equation converter returned an empty HWP script")
    return EquationConversion(latex, hwp_script.strip(), science_warnings(latex))


type Json = None | bool | int | float | str | list[Json] | dict[str, Json]


def normalize_equations(value: Json) -> None:
    """Rebuild only editable equation derivatives; source evidence stays immutable."""
    if isinstance(value, list):
        for child in value:
            normalize_equations(child)
        return
    if not isinstance(value, dict):
        return
    if value.get("type") != "equation":
        for child in value.values():
            normalize_equations(child)
        return
    native = value.get("source_format") == "hwp_script" and not value.get("latex")
    if value.get("hwp_script_mode") == "manual" or native:
        value["hwp_script_mode"] = "manual"
        value.pop("hwp_derivation", None)
        return
    latex = value.get("latex")
    value["hwp_script_mode"] = "auto"
    value.pop("hwp_script", None)
    diagnostics: list[Json] = []
    status = "unsupported"
    if isinstance(latex, str):
        try:
            converted = convert_latex(latex)
        except EquationBridgeError as exc:
            diagnostics.append(str(exc))
        else:
            diagnostics.extend(converted.warnings)
            if not any(warning.startswith("semantic_loss:") for warning in converted.warnings):
                value["hwp_script"] = converted.hwp_script
                status = "review" if diagnostics else "derived"
    else:
        diagnostics.append("editable LaTeX is missing")
    value["hwp_derivation"] = {
        "latex_sha256": hashlib.sha256((latex if isinstance(latex, str) else "").encode("utf-8")).hexdigest(),
        "converter_version": CONVERTER_VERSION, "status": status, "diagnostics": diagnostics,
    }


def equation_runs(value: Json) -> list[dict[str, Json]]:
    if isinstance(value, list):
        return [run for child in value for run in equation_runs(child)]
    if not isinstance(value, dict):
        return []
    if value.get("type") == "equation":
        return [value]
    return [run for child in value.values() for run in equation_runs(child)]


def equation_source_changed(previous: Json, candidate: Json) -> bool:
    """Reject source replacement on retained equations; permit add/delete/reorder."""
    remaining = list(equation_runs(previous))
    unmatched = []
    for run in equation_runs(candidate):
        match = next((old for old in remaining if old.get("source_text") == run.get("source_text")), None)
        if match is None:
            unmatched.append(run)
        else:
            remaining.remove(match)
    return any(old.get(key) and old.get(key) == new.get(key)
               for old in remaining for new in unmatched for key in ("latex", "hwp_script"))
