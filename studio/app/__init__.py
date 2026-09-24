"""Question JSON Studio core import, persistence, and OCR protocol API."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from astra_runtime import bootstrap

ASTRA = bootstrap()

from .importer import (
    ImportConflictError,
    ImportResult,
    SourceIntegrityError,
    find_leaf_runs,
    import_leaf_run,
)

from .model import (
    ModelError,
    RevisionConflict,
    approval_digest,
    approve_question_cas,
    atomic_write_json,
    load_json_object,
    save_json_cas,
    stable_item_id,
)
from .ocr_protocol import ModelJsonError, parse_model_json, sanitize_choice_prefixes
from .equation_bridge import EquationBridgeError, EquationConversion, convert_latex
from .solution_shadow import (
    RECOVERY_STAGING_DIR,
    SolutionShadowError,
    SolutionShadowResult,
    create_solution_shadow,
    finalize_solution_shadow,
    publish_solution_shadow,
)

__all__ = [
    "ImportConflictError",
    "ImportResult",
    "ModelError",
    "ModelJsonError",
    "RevisionConflict",
    "RECOVERY_STAGING_DIR",
    "SolutionShadowError",
    "SolutionShadowResult",
    "SourceIntegrityError",
    "atomic_write_json",
    "approval_digest",
    "approve_question_cas",
    "convert_latex",
    "create_solution_shadow",
    "finalize_solution_shadow",
    "EquationBridgeError",
    "EquationConversion",
    "find_leaf_runs",
    "import_leaf_run",
    "load_json_object",
    "parse_model_json",
    "publish_solution_shadow",
    "sanitize_choice_prefixes",
    "save_json_cas",
    "stable_item_id",
]
