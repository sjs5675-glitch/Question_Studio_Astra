# Exam Studio equation core

Selected equation parser sources adapted from `PNKmath/exam-studio` commit
`bd97d15ed67e325692455b60c483d5e592d0d1c4`.

The underlying `hwp-eqn-ts` portions are MIT licensed; see
`LICENSE.hwp-eqn-ts`. The checked-in `dist/` directory is a mechanical CommonJS
compilation of `src/` so the Python app needs no npm install at runtime.

This module converts a LaTeX candidate to an HWP equation script. It does not
replace or overwrite the canonical `source_text` or `latex` fields.

