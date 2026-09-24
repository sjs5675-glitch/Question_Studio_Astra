"""Solution-side scope episode index (Phase 2a).

Builds a native, problem-independent index of the solution PDF from cached
per-page block detections (``detections/solution-blocks/consensus``) and the
solution ``page_map.json``.  Pure file reads only: this module never calls a
provider and never writes into an existing run directory.

Outputs:
  * ``SolutionScopeEpisode`` — one contiguous numbering scope on the solution
    side (unit/section run), built from block reading order, hierarchy labels,
    role transitions, parent-number restarts and continuation flags.
  * ``NativeSolutionAsset`` — one asset per block entry with a native
    ``solution_scope_id`` (episode identity).  No problem-side scope is ever
    projected here and tied entries are all preserved.
  * ``ScopeIndexSnapshot`` — a source-bound, reproducible artifact
    (``alignment_index.json``) that refuses to load against a different
    solution PDF or index version.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astra_runtime import bootstrap, inside, validate_parsed_paths
import solution_layout

ASTRA = bootstrap()
from typing import Any, Mapping, Sequence

SCOPE_INDEX_VERSION = "2"
SNAPSHOT_SCHEMA = "solution-scope-index"

# Tokens too generic to distinguish one section run from another.
GENERIC_SECTION_TOKENS = frozenset(
    {"정답", "해설", "문제", "풀이", "정답과", "빠른", "상세"}
)

_TOKEN_RE = re.compile(r"[0-9a-z가-힣]+")
_PAGE_RANGE_RE = re.compile(r"(\d+)\s*쪽\s*[~∼–-]\s*(\d+)\s*쪽")
_SINGLE_PAGE_RE = re.compile(r"(\d+)\s*쪽")

_ROLES = ("quick_answer", "detailed_solution", "mixed")


def _norm_tokens(values: Sequence[str]) -> tuple[str, ...]:
    text = " ".join(unicodedata.normalize("NFKC", value).casefold() for value in values)
    seen: dict[str, None] = {}
    for token in _TOKEN_RE.findall(text):
        if token not in GENERIC_SECTION_TOKENS:
            seen.setdefault(token, None)
    return tuple(seen)


def _numeric_rank(parent_number: str | None) -> tuple[int, ...] | None:
    """Rank for restart detection; compound numbers ("1-1") compare as tuples."""
    if parent_number is None:
        return None
    text = unicodedata.normalize("NFKC", str(parent_number)).strip()
    parts = re.split(r"[-.]", text)
    if parts and all(part.isdigit() for part in parts):
        return tuple(int(part) for part in parts)
    return None


def parse_printed_problem_page_range(label: str | None) -> tuple[int, int] | None:
    """Parse a problem-page range printed in a scope label, e.g. ``38쪽~41쪽``."""
    if not label:
        return None
    match = _PAGE_RANGE_RE.search(label)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
        return (start, end) if start <= end else None
    match = _SINGLE_PAGE_RE.search(label)
    if match:
        page = int(match.group(1))
        return (page, page)
    return None


@dataclass(frozen=True)
class SolutionEntryRecord:
    entry_id: str
    parent_number: str | None
    subquestion_label: str | None
    explicit_answer_text: str | None
    stem_fingerprint_tokens: tuple[str, ...]
    figure_labels: tuple[str, ...]
    confidence: float


@dataclass(frozen=True)
class SolutionBlockRecord:
    pdf_index: int
    pdf_page: int
    printed_page: int | None
    segment_id: str
    block_index: int
    local_id: str
    role: str
    bbox: tuple[int, int, int, int]
    hierarchy_path: tuple[str, ...]
    numbering_scope_label: str | None
    coverage_kind: str
    coverage_parent_number: str | None
    group_label: str | None
    entries: tuple[SolutionEntryRecord, ...]
    starts_on_previous_page: bool
    continues_next_page: bool
    continuation_label: str | None
    confidence: float
    page_hierarchy_path: tuple[str, ...]
    page_reset_detected: bool

    @property
    def section_signature(self) -> tuple[str, ...]:
        primary = tuple(self.hierarchy_path or self.page_hierarchy_path) + ((self.numbering_scope_label,) if self.numbering_scope_label else ())
        return _norm_tokens([part for part in primary if part])

    @property
    def printed_ranges(self) -> tuple[tuple[int, int], ...]:
        return tuple(dict.fromkeys(
            value for label in (self.numbering_scope_label, *self.hierarchy_path)
            if (value := parse_printed_problem_page_range(label)) is not None
        ))

    @property
    def printed_range(self) -> tuple[int, int] | None:
        values = self.printed_ranges
        return values[0] if len(values) == 1 else None


@dataclass(frozen=True)
class SolutionScopeEpisode:
    episode_id: str
    section_tokens: tuple[str, ...]
    raw_label: str | None
    occurrence: int
    role_mix: tuple[str, ...]
    page_span: tuple[int, int]
    block_span: tuple[int, int]
    entry_numbers: tuple[str, ...]
    printed_problem_page_range: tuple[int, int] | None
    boundary_evidence: tuple[str, ...]
    coverage_complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "section_tokens": list(self.section_tokens),
            "raw_label": self.raw_label,
            "occurrence": self.occurrence,
            "role_mix": list(self.role_mix),
            "page_span": list(self.page_span),
            "block_span": list(self.block_span),
            "entry_numbers": list(self.entry_numbers),
            "printed_problem_page_range": (
                list(self.printed_problem_page_range)
                if self.printed_problem_page_range
                else None
            ),
            "boundary_evidence": list(self.boundary_evidence),
            "coverage_complete": self.coverage_complete,
        }


@dataclass(frozen=True)
class NativePart:
    pdf_index: int
    pdf_page: int
    printed_page: int | None
    segment_id: str
    bbox: tuple[int, int, int, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdf_index": self.pdf_index,
            "pdf_page": self.pdf_page,
            "printed_page": self.printed_page,
            "segment_id": self.segment_id,
            "bbox": list(self.bbox),
        }


@dataclass(frozen=True)
class NativeSolutionAsset:
    native_asset_id: str
    solution_scope_id: str
    role: str
    parent_number: str | None
    subquestion_label: str | None
    explicit_answer: str | None
    fingerprints: tuple[str, ...]
    parts: tuple[NativePart, ...]
    sequence: int
    confidence: float
    continuation_unresolved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "native_asset_id": self.native_asset_id,
            "solution_scope_id": self.solution_scope_id,
            "role": self.role,
            "parent_number": self.parent_number,
            "subquestion_label": self.subquestion_label,
            "explicit_answer": self.explicit_answer,
            "fingerprints": list(self.fingerprints),
            "parts": [part.to_dict() for part in self.parts],
            "sequence": self.sequence,
            "confidence": self.confidence,
            "continuation_unresolved": self.continuation_unresolved,
        }


@dataclass(frozen=True)
class ScopeIndexSnapshot:
    solution_sha256: str
    total_pages: int
    indexed_pages: tuple[int, ...]
    missing_pages: tuple[int, ...]
    episodes: tuple[SolutionScopeEpisode, ...]
    native_assets: tuple[NativeSolutionAsset, ...]
    provider_profile: str = "full"
    input_files: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    input_cache_versions: tuple[tuple[str, str | None], ...] = field(
        default_factory=tuple
    )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": SNAPSHOT_SCHEMA,
            "scope_index_version": SCOPE_INDEX_VERSION,
            "source": {
                "solution_sha256": self.solution_sha256,
                "provider_profile": self.provider_profile,
                "input_files": list(self.input_files),
                "input_cache_versions": dict(self.input_cache_versions),
            },
            "page_coverage": {
                "total_pages": self.total_pages,
                "indexed_pages": list(self.indexed_pages),
                "missing_pages": list(self.missing_pages),
            },
            "episodes": [episode.to_dict() for episode in self.episodes],
            "native_assets": [asset.to_dict() for asset in self.native_assets],
        }
        payload["content_sha256"] = _payload_sha256(payload)
        return payload


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    trimmed = {key: value for key, value in payload.items() if key != "content_sha256"}
    canonical = json.dumps(trimmed, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_CACHE_VERSION_PATTERNS = {
    "ai_contract_cache_version": re.compile(
        r'^AI_CONTRACT_CACHE_VERSION\s*=\s*"([^"]+)"', re.MULTILINE
    ),
    "boundary_consensus_version": re.compile(
        r'^BOUNDARY_CONSENSUS_VERSION\s*=\s*"([^"]+)"', re.MULTILINE
    ),
}


def read_input_cache_versions() -> tuple[tuple[str, str | None], ...]:
    """Read detection-cache version constants from the runtime source text.

    Text-parsed on purpose: importing solution_runtime would pull provider
    dependencies into this stdlib-only module.
    """
    runtime_path = Path(__file__).with_name("solution_runtime.py")
    try:
        text = runtime_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    return tuple(
        (name, match.group(1) if (match := pattern.search(text)) else None)
        for name, pattern in _CACHE_VERSION_PATTERNS.items()
    )


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _consensus_provenance(
    payload: Mapping[str, Any], provider_profile: str
) -> dict[str, Any]:
    meta = payload.get("meta")
    if not isinstance(meta, Mapping):
        raise ScopeIndexBindingError("consensus wrapper metadata is missing")
    recorded_profile = meta.get("provider_profile")
    providers = meta.get("providers")
    complete = meta.get("complete_primary_agreement")
    if (
        not isinstance(providers, list)
        or any(type(provider) is not str for provider in providers)
        or type(complete) is not bool
    ):
        raise ScopeIndexBindingError("consensus wrapper provenance is invalid")
    if providers == ["astra"]:
        import astra_cli
        if recorded_profile != provider_profile or meta.get("model") != astra_cli.MODEL or meta.get("policy_version") != astra_cli.POLICY_VERSION or complete is not False:
            raise ScopeIndexBindingError("Astra boundary metadata must remain current and non-authoritative")
        return {"provider_profile": provider_profile, "providers": ["astra"], "complete_primary_agreement": False, "model": meta["model"], "policy_version": meta["policy_version"]}
    if provider_profile == "codex_only":
        if (
            recorded_profile != "codex_only"
            or providers != ["codex"]
            or complete is not False
        ):
            raise ScopeIndexBindingError(
                "codex_only consensus wrapper provenance is invalid"
            )
    elif (
        recorded_profile not in {None, "full"}
        or "codex" not in providers
        or (complete is True and "gemini" not in providers)
    ):
        raise ScopeIndexBindingError("full consensus wrapper provenance is invalid")
    return {
        "provider_profile": provider_profile,
        "providers": list(providers),
        "complete_primary_agreement": complete,
    }


def load_page_records(
    consensus_dir: Path,
    page_map: Mapping[int, Mapping[str, Any]],
    *,
    provider_profile: str = "full",
) -> list[SolutionBlockRecord]:
    """Read consensus pages into a flat, reading-ordered block list."""
    blocks: list[SolutionBlockRecord] = []
    for path in sorted(consensus_dir.glob("page-*.json")):
        payload = _load_json(path)
        _consensus_provenance(payload, provider_profile)
        result = payload.get("result", payload)
        pdf_index = int(result["solution_pdf_index"])
        page_identity = page_map.get(pdf_index, {})
        page_hierarchy = tuple(
            str(part) for part in result.get("hierarchy_path") or () if part
        )
        scope_meta = result.get("numbering_scope") or {}
        for block_index, raw in enumerate(result.get("blocks") or ()):
            coverage = raw.get("coverage") or {}
            entries = tuple(
                SolutionEntryRecord(
                    entry_id=str(entry.get("entry_id", "")),
                    parent_number=(
                        str(entry["parent_number"])
                        if entry.get("parent_number") is not None
                        else None
                    ),
                    subquestion_label=(
                        str(entry["subquestion_label"])
                        if entry.get("subquestion_label") is not None
                        else None
                    ),
                    explicit_answer_text=(
                        str(entry["explicit_answer_text"])
                        if entry.get("explicit_answer_text") is not None
                        else None
                    ),
                    stem_fingerprint_tokens=tuple(
                        str(token) for token in entry.get("stem_fingerprint_tokens") or ()
                    ),
                    figure_labels=tuple(
                        str(label) for label in entry.get("figure_labels") or ()
                    ),
                    confidence=float(entry.get("confidence", 0.0)),
                )
                for entry in raw.get("entries") or ()
            )
            blocks.append(
                SolutionBlockRecord(
                    pdf_index=pdf_index,
                    pdf_page=int(result.get("solution_pdf_page", pdf_index + 1)),
                    printed_page=(
                        int(result["solution_printed_page"])
                        if result.get("solution_printed_page") is not None
                        else page_identity.get("printed_page")
                    ),
                    segment_id=str(page_identity.get("segment_id", "default")),
                    block_index=block_index,
                    local_id=str(raw.get("local_id", f"block-{block_index}")),
                    role=str(raw.get("role", "detailed_solution")),
                    bbox=tuple(int(v) for v in raw.get("bbox") or (0, 0, 0, 0)),
                    hierarchy_path=tuple(
                        str(part) for part in raw.get("hierarchy_path") or () if part
                    ),
                    numbering_scope_label=(
                        str(raw["numbering_scope_label"])
                        if raw.get("numbering_scope_label")
                        else None
                    ),
                    coverage_kind=str(coverage.get("kind", "single")),
                    coverage_parent_number=(
                        str(coverage["parent_number"])
                        if coverage.get("parent_number") is not None
                        else None
                    ),
                    group_label=(
                        str(coverage["group_label"])
                        if coverage.get("group_label") is not None
                        else None
                    ),
                    entries=entries,
                    starts_on_previous_page=bool(raw.get("starts_on_previous_page")),
                    continues_next_page=bool(raw.get("continues_next_page")),
                    continuation_label=(
                        str(raw["continuation_label"])
                        if raw.get("continuation_label")
                        else None
                    ),
                    confidence=float(raw.get("confidence", 0.0)),
                    page_hierarchy_path=page_hierarchy,
                    page_reset_detected=bool(scope_meta.get("reset_detected")),
                )
            )
    blocks.sort(key=lambda record: (record.pdf_index, record.block_index))
    return blocks


def _hierarchy_tokens(block: SolutionBlockRecord) -> tuple[str, ...]:
    return _norm_tokens(
        [part for part in (block.hierarchy_path or block.page_hierarchy_path) if part]
    )


def _heading_signatures(block: SolutionBlockRecord) -> tuple[tuple[str, ...], ...]:
    values = tuple(block.hierarchy_path or block.page_hierarchy_path) + (
        (block.numbering_scope_label,) if block.numbering_scope_label else ()
    )
    return tuple(dict.fromkeys(tokens for value in values if (tokens := _norm_tokens([value]))))


def _hierarchy_compatible(
    previous: SolutionBlockRecord, current: SolutionBlockRecord
) -> bool:
    left, right = set(_hierarchy_tokens(previous)), set(_hierarchy_tokens(current))
    if left and right:
        return bool(left & right)
    # Hierarchy missing on a side: demand a different strong identity signal
    # instead of assuming compatibility (fail-closed).
    if previous.continuation_label and current.continuation_label:
        return previous.continuation_label == current.continuation_label
    left_label = set(_norm_tokens([previous.numbering_scope_label or ""]))
    right_label = set(_norm_tokens([current.numbering_scope_label or ""]))
    return bool(left_label and right_label and left_label & right_label)


def continuation_joinable(
    previous: SolutionBlockRecord, current: SolutionBlockRecord
) -> bool:
    """C6 invariant: merge only same/adjacent pages with both explicit flags,
    the same role and page-map segment, matching continuation labels (when
    both are printed) and compatible hierarchy context."""
    if not (current.starts_on_previous_page and previous.continues_next_page):
        return False
    if not 0 <= current.pdf_index - previous.pdf_index <= 1:
        return False
    if previous.role != current.role:
        return False
    if previous.segment_id != current.segment_id:
        return False
    if (
        previous.continuation_label
        and current.continuation_label
        and previous.continuation_label != current.continuation_label
    ):
        return False
    return _hierarchy_compatible(previous, current)


def build_scope_episodes(
    blocks: Sequence[SolutionBlockRecord],
    *,
    total_pages: int,
    indexed_pages: Sequence[int],
) -> tuple[tuple[SolutionScopeEpisode, ...], dict[int, str]]:
    """Split reading-ordered blocks into scope episodes.

    Returns the episodes plus a mapping of global block position -> episode_id.
    """
    indexed = set(indexed_pages)
    page_blocks: dict[tuple[int, str, str], list[SolutionBlockRecord]] = {}
    for block in blocks:
        page_blocks.setdefault((block.pdf_index, block.segment_id, block.role), []).append(block)
    observations: list[solution_layout.PageLayoutObservation] = []
    for (page_index, segment_id, role), grouped in page_blocks.items():
        ranks = [
            rank
            for item in grouped
            for entry in item.entries
            if (rank := _numeric_rank(entry.parent_number)) is not None
        ]
        observations.append(
            solution_layout.PageLayoutObservation(
                page_index=page_index,
                stream_id=f"{segment_id}:{role}",
                heading_signatures=tuple(
                    dict.fromkeys(
                        heading for item in grouped for heading in _heading_signatures(item)
                    )
                ),
                first_number=min(ranks, default=None),
                last_number=max(ranks, default=None),
                reset_detected=any(item.page_reset_detected for item in grouped),
            )
        )
    furniture = solution_layout.running_furniture_tokens(tuple(observations))
    episodes: list[dict[str, Any]] = []
    block_to_episode: dict[int, str] = {}
    occurrences: dict[tuple[str, ...], int] = {}
    current: dict[str, Any] | None = None
    previous_block: SolutionBlockRecord | None = None

    def close(entry: dict[str, Any]) -> None:
        episodes.append(entry)

    for position, block in enumerate(blocks):
        signature = tuple(
            token
            for heading in _heading_signatures(block)
            if (f"{block.segment_id}:{block.role}", heading) not in furniture
            for token in heading
        )
        evidence: list[str] = ["range_conflict"] if len(block.printed_ranges) > 1 else []
        joined = previous_block is not None and continuation_joinable(
            previous_block, block
        )
        if current is None:
            evidence.append("index_start")
            boundary = True
        elif joined:
            boundary = False
        elif block.coverage_kind == "continuation":
            # A continuation block that is NOT C6-joinable may belong to a
            # numbering run whose source lies elsewhere; absorbing it would
            # silently delete an episode boundary. Isolate it instead.
            boundary = True
            evidence.append("non_joinable_continuation")
        else:
            boundary = False
            role_change = (
                previous_block is not None and previous_block.role != block.role
            )
            restart = _entry_restart(current, block)
            advances = _entry_advances(current, block)
            if role_change:
                # A non-continuation role transition is always a boundary:
                # quick tables and detailed runs are separate episodes (B2).
                boundary = True
                evidence.append("role_transition")
            # An explicit printed-range change is a strong scope boundary in
            # its own right — never label noise (it would poison the
            # printed_range_corrob score in 2b if merged).
            block_range = block.printed_range
            if (
                block_range is not None
                and current["printed_range"] is not None
                and block_range != current["printed_range"]
            ):
                boundary = True
                evidence.append("printed_range_change")
            # A label change while numbers keep advancing may be label noise
            # (a stray unit heading), but only within the same role, on the
            # SAME page, with compatible hierarchy tokens. A label change on
            # a new page always splits — over-splitting is the safe direction.
            suppress_label_noise = (
                advances
                and not role_change
                and previous_block is not None
                and block.pdf_index == previous_block.pdf_index
                and bool(set(_hierarchy_tokens(block)) & current["hier_tokens"])
                and _norm_tokens(block.hierarchy_path[-1:]) == _norm_tokens(previous_block.hierarchy_path[-1:])
            )
            if (
                signature
                and signature != current["signature"]
                and not suppress_label_noise
            ):
                boundary = True
                evidence.append("hierarchy_change")
            if restart:
                boundary = True
                evidence.append(restart)
                if block.page_reset_detected:
                    evidence.append("page_reset_corroborated")
        if boundary:
            if current is not None:
                close(current)
            occurrence = occurrences.get(signature, 0)
            occurrences[signature] = occurrence + 1
            current = {
                "signature": signature,
                "raw_label": block.numbering_scope_label
                or (block.hierarchy_path[-1] if block.hierarchy_path else None),
                "occurrence": occurrence,
                "roles": [],
                "page_start": block.pdf_index,
                "page_end": block.pdf_index,
                "block_start": position,
                "block_end": position,
                "entry_numbers": [],
                "max_rank": None,
                "last_parent": None,
                "last_sublabel": None,
                "hier_tokens": set(),
                "evidence": list(evidence),
                "printed_range": block.printed_range,
            }
        assert current is not None
        current["page_end"] = block.pdf_index
        current["block_end"] = position
        if block.role not in current["roles"]:
            current["roles"].append(block.role)
        current["hier_tokens"].update(_hierarchy_tokens(block))
        late_range = block.printed_range
        if len(block.printed_ranges) > 1:
            current["printed_range"] = None
            if "range_conflict" not in current["evidence"]:
                current["evidence"].append("range_conflict")
        if late_range is not None and "range_conflict" not in current["evidence"]:
            if current["printed_range"] is None:
                current["printed_range"] = late_range
            elif late_range != current["printed_range"]:
                # Never overwrite: a conflicting late range is boundary-shaped
                # evidence, not data (would poison printed_range_corrob in 2b).
                if "range_conflict" not in current["evidence"]:
                    current["evidence"].append("range_conflict")
                current["printed_range"] = None
        for entry in block.entries:
            if entry.parent_number is not None:
                current["entry_numbers"].append(entry.parent_number)
                rank = _numeric_rank(entry.parent_number)
                if rank is not None and not joined:
                    if current["max_rank"] is None or rank > current["max_rank"]:
                        current["max_rank"] = rank
                current["last_parent"] = entry.parent_number
                current["last_sublabel"] = entry.subquestion_label
        block_to_episode[position] = ""  # filled after ids exist
        previous_block = block

    if current is not None:
        close(current)

    built: list[SolutionScopeEpisode] = []
    for entry in episodes:
        identity = hashlib.sha256(
            json.dumps(
                [SCOPE_INDEX_VERSION, list(entry["signature"]), entry["occurrence"]],
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:20]
        episode_id = f"episode-{identity}"
        page_start, page_end = entry["page_start"], entry["page_end"]
        coverage_complete = True
        if page_start - 1 >= 0 and page_start - 1 not in indexed:
            coverage_complete = page_start == 0
        if page_end + 1 < total_pages and page_end + 1 not in indexed:
            coverage_complete = False
        built.append(
            SolutionScopeEpisode(
                episode_id=episode_id,
                section_tokens=entry["signature"],
                raw_label=entry["raw_label"],
                occurrence=entry["occurrence"],
                role_mix=tuple(entry["roles"]),
                page_span=(page_start, page_end),
                block_span=(entry["block_start"], entry["block_end"]),
                entry_numbers=tuple(entry["entry_numbers"]),
                printed_problem_page_range=entry["printed_range"],
                boundary_evidence=tuple(entry["evidence"]),
                coverage_complete=coverage_complete,
            )
        )
        for position in range(entry["block_start"], entry["block_end"] + 1):
            block_to_episode[position] = episode_id
    return tuple(built), block_to_episode


def _first_rank(block: SolutionBlockRecord) -> tuple[tuple[int, ...] | None, Any]:
    first_numbered = next(
        (entry for entry in block.entries if entry.parent_number is not None), None
    )
    if first_numbered is None:
        return None, None
    return _numeric_rank(first_numbered.parent_number), first_numbered


def _entry_advances(current: dict[str, Any], block: SolutionBlockRecord) -> bool:
    """True when the block's first number strictly continues the current run.

    Continuation blocks never reach here: C6-joined ones skip boundary logic
    entirely and non-joinable ones are forced onto their own episode.
    """
    rank, _ = _first_rank(block)
    if rank is None:
        return False
    if current["max_rank"] is None:
        return False
    return rank > current["max_rank"]


def _entry_restart(current: dict[str, Any], block: SolutionBlockRecord) -> str | None:
    """Mirror of the problem-side reset heuristic (repeat/decrease splits)."""
    rank, first_numbered = _first_rank(block)
    if rank is None or current["max_rank"] is None:
        return None
    if rank < current["max_rank"]:
        return "number_decrease"
    if rank == current["max_rank"]:
        sibling = (
            first_numbered.parent_number == current["last_parent"]
            and first_numbered.subquestion_label is not None
            and current["last_sublabel"] is not None
            and first_numbered.subquestion_label != current["last_sublabel"]
        )
        if not sibling:
            return "number_repeat"
    return None


_ROLE_PREFIX = {
    "quick_answer": "native-answer",
    "detailed_solution": "native-detail",
    "mixed": "native-mixed",
}


def build_native_assets(
    blocks: Sequence[SolutionBlockRecord],
    block_to_episode: Mapping[int, str],
) -> tuple[NativeSolutionAsset, ...]:
    """One native asset per block entry; ties preserved, no problem projection.

    Continuation blocks (C6-joinable) extend the previous asset for the same
    parent number instead of creating a new one; a continuation block that is
    NOT joinable is kept standalone and flagged ``continuation_unresolved``.
    """
    assets: list[NativeSolutionAsset] = []
    open_by_parent: dict[tuple[str, str | None], int] = {}
    previous_block: SolutionBlockRecord | None = None

    for position, block in enumerate(blocks):
        episode_id = block_to_episode.get(position, "")
        part = NativePart(
            pdf_index=block.pdf_index,
            pdf_page=block.pdf_page,
            printed_page=block.printed_page,
            segment_id=block.segment_id,
            bbox=block.bbox,
        )
        joinable = previous_block is not None and continuation_joinable(
            previous_block, block
        )
        if block.coverage_kind == "continuation" and block.starts_on_previous_page:
            # Tail of a continued asset: merge only under the C6 invariant.
            key = (episode_id, block.coverage_parent_number)
            index = open_by_parent.get(key)
            if joinable and index is not None:
                existing = assets[index]
                assets[index] = NativeSolutionAsset(
                    native_asset_id=existing.native_asset_id,
                    solution_scope_id=existing.solution_scope_id,
                    role=existing.role,
                    parent_number=existing.parent_number,
                    subquestion_label=existing.subquestion_label,
                    explicit_answer=existing.explicit_answer,
                    fingerprints=existing.fingerprints,
                    parts=existing.parts + (part,),
                    sequence=existing.sequence,
                    confidence=min(existing.confidence, block.confidence),
                )
                if not block.continues_next_page:
                    open_by_parent.pop(key, None)
                previous_block = block
                continue
            # Non-joinable continuation tail: standalone, flagged, never merged.
            for entry_index, entry in enumerate(block.entries or _synthetic_entry(block)):
                assets.append(
                    _entry_asset(
                        block, entry, entry_index, episode_id, part,
                        continuation_unresolved=True,
                    )
                )
            previous_block = block
            continue

        # Ordinary block, or the *source* part of a continued asset
        # (kind=continuation with starts_on_previous_page=False).
        entries = tuple(block.entries or _synthetic_entry(block))
        for entry_index, entry in enumerate(entries):
            asset = _entry_asset(block, entry, entry_index, episode_id, part)
            if block.continues_next_page and entry_index == len(entries) - 1:
                open_by_parent[(episode_id, entry.parent_number)] = len(assets)
            assets.append(asset)
        previous_block = block
    return tuple(assets)


def _synthetic_entry(block: SolutionBlockRecord) -> tuple[SolutionEntryRecord, ...]:
    return (
        SolutionEntryRecord(
            entry_id=block.coverage_parent_number or block.local_id,
            parent_number=block.coverage_parent_number,
            subquestion_label=None,
            explicit_answer_text=None,
            stem_fingerprint_tokens=(),
            figure_labels=(),
            confidence=block.confidence,
        ),
    )


def _entry_asset(
    block: SolutionBlockRecord,
    entry: SolutionEntryRecord,
    entry_index: int,
    episode_id: str,
    part: NativePart,
    *,
    continuation_unresolved: bool = False,
) -> NativeSolutionAsset:
    prefix = _ROLE_PREFIX.get(block.role, "native-detail")
    return NativeSolutionAsset(
        native_asset_id=(
            f"{prefix}-p{block.pdf_page:04d}-b{block.block_index:02d}-e{entry_index:02d}"
        ),
        solution_scope_id=episode_id,
        role=block.role,
        parent_number=entry.parent_number,
        subquestion_label=entry.subquestion_label,
        explicit_answer=entry.explicit_answer_text,
        fingerprints=entry.stem_fingerprint_tokens[:2] + entry.figure_labels[:1],
        parts=(part,),
        sequence=block.pdf_index * 10000 + block.block_index * 100 + entry_index,
        confidence=min(block.confidence, entry.confidence)
        if entry.confidence
        else block.confidence,
        continuation_unresolved=continuation_unresolved,
    )


def build_scope_index(
    run_solutions_dir: Path, *, provider_profile: str = "full"
) -> ScopeIndexSnapshot:
    """Build the full snapshot from an existing run's ``solutions`` directory."""
    page_map_path = run_solutions_dir / "page_map.json"
    page_map_payload = _load_json(page_map_path)
    pages = {int(page["pdf_index"]): page for page in page_map_payload["pages"]}
    if provider_profile not in {"full", "codex_only"}:
        raise ScopeIndexBindingError("unsupported provider profile")
    consensus_dir = run_solutions_dir / "detections" / "solution-blocks" / "consensus"
    if provider_profile == "codex_only":
        consensus_dir = consensus_dir / "codex_only"
    blocks = load_page_records(
        consensus_dir, pages, provider_profile=provider_profile
    )
    # "Indexed" means the page was analyzed at all (a consensus file exists),
    # even if it holds zero blocks (e.g. the cover). "Missing" means the page
    # was never sent to block detection — those may hide real solutions.
    analyzed: set[int] = set()
    for path in consensus_dir.glob("page-*.json"):
        payload = _load_json(path)
        result = payload.get("result", payload)
        analyzed.add(int(result["solution_pdf_index"]))
    indexed_pages = sorted(analyzed)
    total_pages = len(pages)
    missing_pages = sorted(set(pages) - analyzed)
    episodes, block_to_episode = build_scope_episodes(
        blocks, total_pages=total_pages, indexed_pages=indexed_pages
    )
    native_assets = build_native_assets(blocks, block_to_episode)
    input_files = tuple(
        {
            "path": path.relative_to(run_solutions_dir).as_posix(),
            "sha256": _file_sha256(path),
            **_consensus_provenance(_load_json(path), provider_profile),
        }
        for path in sorted(consensus_dir.glob("page-*.json"))
    ) + ({"path": "page_map.json", "sha256": _file_sha256(page_map_path)},)
    return ScopeIndexSnapshot(
        solution_sha256=str(page_map_payload["source_sha256"]),
        total_pages=total_pages,
        indexed_pages=tuple(indexed_pages),
        missing_pages=tuple(missing_pages),
        episodes=episodes,
        native_assets=native_assets,
        provider_profile=provider_profile,
        input_files=input_files,
        input_cache_versions=read_input_cache_versions(),
    )


class ScopeIndexBindingError(ValueError):
    """Snapshot does not match the requested source or version."""


def write_scope_index(snapshot: ScopeIndexSnapshot, output_path: Path) -> None:
    output_path = inside(output_path.resolve(), "scope output")
    payload = json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(payload, encoding="utf-8")


def read_scope_index(
    path: Path,
    *,
    expected_solution_sha256: str,
    expected_provider_profile: str = "full",
    solutions_dir: Path | None = None,
) -> dict[str, Any]:
    """Load a snapshot, refusing wrong source, tampered payload or stale inputs.

    When ``solutions_dir`` is given, every recorded input file (consensus pages
    and page_map.json) is re-hashed against the current run — a snapshot built
    from different caches must never silently feed 2b alignment.
    """
    payload = _load_json(path)
    if payload.get("schema") != SNAPSHOT_SCHEMA:
        raise ScopeIndexBindingError(f"unexpected schema: {payload.get('schema')!r}")
    if payload.get("scope_index_version") != SCOPE_INDEX_VERSION:
        raise ScopeIndexBindingError(
            f"scope index version mismatch: {payload.get('scope_index_version')!r}"
        )
    actual = payload.get("source", {}).get("solution_sha256")
    if actual != expected_solution_sha256:
        raise ScopeIndexBindingError(
            "snapshot is bound to a different solution source"
        )
    recorded_profile = payload.get("source", {}).get("provider_profile", "full")
    if recorded_profile != expected_provider_profile:
        raise ScopeIndexBindingError("snapshot is bound to a different provider profile")
    expected_consensus = "detections/solution-blocks/consensus"
    if recorded_profile == "codex_only":
        expected_consensus += "/codex_only"
    input_files = payload.get("source", {}).get("input_files", ())
    input_paths = [str(entry.get("path", "")) for entry in input_files]
    if (
        input_paths.count("page_map.json") != 1
        or len(input_paths) != len(set(input_paths))
        or any(
            path != "page_map.json"
            and re.fullmatch(
                re.escape(expected_consensus) + r"/page-\d{4,}\.json", path
            )
            is None
            for path in input_paths
        )
    ):
        raise ScopeIndexBindingError(
            "snapshot inputs do not match the selected provider profile"
        )
    provenance_fields = {
        "provider_profile",
        "providers",
        "complete_primary_agreement",
    }
    all_provenance_fields = provenance_fields | {"model", "policy_version"}
    for entry in input_files:
        if entry.get("path") == "page_map.json":
            if all_provenance_fields.intersection(entry):
                raise ScopeIndexBindingError("page map cannot claim consensus provenance")
            continue
        recorded = all_provenance_fields.intersection(entry)
        if recorded and not provenance_fields.issubset(entry):
            raise ScopeIndexBindingError("consensus input provenance is incomplete")
        if recorded:
            expected = _consensus_provenance({"meta": entry}, recorded_profile)
            if any(entry.get(field) != value for field, value in expected.items()):
                raise ScopeIndexBindingError("consensus input provenance is inconsistent")
        elif recorded_profile == "codex_only":
            raise ScopeIndexBindingError("codex_only consensus provenance is missing")
    recorded_hash = payload.get("content_sha256")
    if recorded_hash != _payload_sha256(payload):
        raise ScopeIndexBindingError("snapshot payload hash mismatch (tampered?)")
    recorded_versions = payload.get("source", {}).get("input_cache_versions", {})
    for name, current_version in read_input_cache_versions():
        if current_version is None:
            # Cannot verify against the runtime — refuse rather than fail open.
            raise ScopeIndexBindingError(
                f"cannot resolve current {name} from runtime source"
            )
        if recorded_versions.get(name) != current_version:
            raise ScopeIndexBindingError(
                f"snapshot built under different {name}: "
                f"{recorded_versions.get(name)!r} != {current_version!r}"
            )
    if solutions_dir is not None:
        root = solutions_dir.resolve()
        for entry in payload.get("source", {}).get("input_files", ()):
            raw_path = str(entry["path"])
            candidate = Path(raw_path)
            if candidate.is_absolute() or ".." in candidate.parts:
                raise ScopeIndexBindingError(
                    f"snapshot input path escapes the run: {raw_path}"
                )
            input_path = (root / candidate).resolve()
            if root not in input_path.parents and input_path != root:
                raise ScopeIndexBindingError(
                    f"snapshot input path escapes the run: {raw_path}"
                )
            if not input_path.is_file():
                raise ScopeIndexBindingError(
                    f"snapshot input missing from run: {raw_path}"
                )
            if _file_sha256(input_path) != entry["sha256"]:
                raise ScopeIndexBindingError(
                    f"snapshot input changed since build: {raw_path}"
                )
            if raw_path != "page_map.json":
                actual_provenance = _consensus_provenance(
                    _load_json(input_path), recorded_profile
                )
                if (provenance_fields.issubset(entry) or actual_provenance["providers"] == ["astra"]) and any(
                    entry.get(field) != value
                    for field, value in actual_provenance.items()
                ):
                    raise ScopeIndexBindingError(
                        "snapshot consensus provenance changed since build"
                    )
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="Build a scope index from a run directory")
    build.add_argument("--solutions-dir", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    validate_parsed_paths(args)
    if args.command == "build":
        solutions_dir = args.solutions_dir.resolve()
        output = args.output.resolve()
        if solutions_dir in output.parents:
            parser.error("refusing to write the snapshot inside the source run")
        snapshot = build_scope_index(solutions_dir)
        write_scope_index(snapshot, output)
        print(
            json.dumps(
                {
                    "episodes": len(snapshot.episodes),
                    "native_assets": len(snapshot.native_assets),
                    "indexed_pages": len(snapshot.indexed_pages),
                    "missing_pages": len(snapshot.missing_pages),
                    "output": str(output),
                },
                ensure_ascii=False,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
