"""Problem-scope ↔ solution-episode alignment (Phase 2b, pure).

Aligns problem-side numbering scopes to solution-side scope episodes (built by
``solution_scope``) with a monotonic DP over a trusted, precision-first
6-tuple score. Keys are always ``(problem_scope_id, role)`` — there is no
single-scope mapping. Detailed alignment runs first; quick alignment may
borrow corroboration from the detailed result but can never exceed it in
authority. Full-tuple ties are never broken arbitrarily: the scope stays
unaligned with all optimal candidates preserved (``allows``) so downstream
linking can only ever reach REVIEW through them.

Stdlib only. No provider access, no engine imports.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

SCOPE_ALIGNMENT_VERSION = "1"

ROLE_DETAILED = "detailed_solution"
ROLE_QUICK = "quick_answer"
ROLE_MIXED = "mixed"

# Trust thresholds (documented contract, C4): an edge is auto-capable trusted
# only via (explicit label match AND ordered numbers) or (non-generic
# hierarchy match AND ordered numbers).
TRUST_ORDER_MILLI = 800
TRUST_MIN_NUMBERS = 2
TRUST_HIERARCHY_SHARED_TOKENS = 2

# Admissibility floor: weaker edges never enter the DP at all.
FLOOR_ORDER_MILLI = 500

# Selection gates (auto-selection requires ALL of these; anything weaker is
# allows-only, i.e. REVIEW-only downstream).
MIN_SCOPE_MARGIN = 0.15
ANSWER_CORROB_MIN_COMMON = 2
ANSWER_CORROB_COVERAGE_NUM = 4  # common >= 4/5 of the smaller answer set
ANSWER_CORROB_COVERAGE_DEN = 5

GENERIC_TOKENS = frozenset({"정답", "해설", "문제", "풀이", "정답과", "빠른", "상세"})
_TOKEN_RE = re.compile(r"[0-9a-z가-힣]+")

Score = tuple[int, int, int, int, int, int]
ZERO: Score = (0, 0, 0, 0, 0, 0)


def _tokens(values: Sequence[str]) -> tuple[str, ...]:
    text = " ".join(unicodedata.normalize("NFKC", v).casefold() for v in values)
    out: dict[str, None] = {}
    for token in _TOKEN_RE.findall(text):
        if token not in GENERIC_TOKENS:
            out.setdefault(token, None)
    return tuple(out)


def normalize_tokens(values: Sequence[str]) -> tuple[str, ...]:
    """Public alias used by runtime profile builders."""
    return _tokens(values)


def _norm_number(value: str) -> str:
    text = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    parts = re.split(r"[-.]", text)
    if parts and all(p.isdigit() for p in parts):
        return "-".join(str(int(p)) for p in parts)
    return text


@dataclass(frozen=True)
class ProblemScopeProfile:
    """Problem-side inputs for one numbering scope (role-agnostic)."""

    scope_id: str
    first_sequence: int
    section_tokens: tuple[str, ...]
    explicit_label_tokens: tuple[str, ...]
    ordered_numbers: tuple[str, ...]
    printed_pages: tuple[int, ...]
    anchor_printed_ranges: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class EpisodeProfile:
    """Solution-side inputs for one scope episode (single role pool)."""

    episode_id: str
    order_key: tuple[int, int]
    roles: tuple[str, ...]
    section_tokens: tuple[str, ...]
    label_tokens: tuple[str, ...]
    ordered_numbers: tuple[str, ...]
    printed_problem_page_range: tuple[int, int] | None
    printed_solution_pages: tuple[int, ...]
    explicit_answers: tuple[tuple[str, str], ...] = ()  # (number, answer)
    coverage_complete: bool = True


@dataclass(frozen=True)
class ScopeEdge:
    scope_id: str
    episode_id: str
    role: str
    score: Score
    trusted: bool
    admissible: bool
    status: str  # rejected | admissible | allowed | selected
    reject_reason: str | None
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class AlignedScope:
    scope_id: str
    role: str
    aligned_episode_id: str | None
    allows: tuple[str, ...]
    margin: float
    ambiguous: bool
    evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "role": self.role,
            "aligned_episode_id": self.aligned_episode_id,
            "allows": list(self.allows),
            "margin": self.margin,
            "ambiguous": self.ambiguous,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class ScopeCandidateGroup:
    scope_ids: tuple[str, ...]
    episode_ids: tuple[str, ...]
    role: str
    evidence: tuple[str, ...] = ("compatible_scope_group", "review_required")

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_ids": list(self.scope_ids),
            "episode_ids": list(self.episode_ids),
            "role": self.role,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class ScopeAlignment:
    """Full role-aware alignment result, serializable and cache-key-stable."""

    entries: tuple[AlignedScope, ...]
    edges: tuple[ScopeEdge, ...] = ()
    candidate_groups: tuple[ScopeCandidateGroup, ...] = ()

    def get(self, scope_id: str, role: str) -> AlignedScope | None:
        for entry in self.entries:
            if entry.scope_id == scope_id and entry.role == role:
                return entry
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_alignment_version": SCOPE_ALIGNMENT_VERSION,
            "entries": [entry.to_dict() for entry in sorted(
                self.entries, key=lambda e: (e.scope_id, e.role)
            )],
            "edges": [
                {
                    "scope_id": edge.scope_id,
                    "episode_id": edge.episode_id,
                    "role": edge.role,
                    "score": list(edge.score),
                    "trusted": edge.trusted,
                    "admissible": edge.admissible,
                    "status": edge.status,
                    "reject_reason": edge.reject_reason,
                    "evidence": list(edge.evidence),
                }
                for edge in sorted(
                    self.edges, key=lambda e: (e.scope_id, e.role, e.episode_id)
                )
            ],
            "candidate_groups": [
                group.to_dict()
                for group in sorted(
                    self.candidate_groups,
                    key=lambda group: (group.role, group.scope_ids, group.episode_ids),
                )
            ],
        }

    def signature(self) -> str:
        canonical = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _ordered_overlap_milli(
    scope_numbers: Sequence[str], episode_numbers: Sequence[str]
) -> int:
    """Order-preserving common subsequence coverage of the scope numbers."""
    left = [_norm_number(n) for n in scope_numbers]
    right = [_norm_number(n) for n in episode_numbers]
    if not left or not right:
        return 0
    n, m = len(left), len(right)
    table = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if left[i] == right[j]:
                table[i][j] = 1 + table[i + 1][j + 1]
            else:
                table[i][j] = max(table[i + 1][j], table[i][j + 1])
    return round(1000 * table[0][0] / len(left))


def _jaccard_milli(left: Sequence[str], right: Sequence[str]) -> int:
    first, second = set(left), set(right)
    if not first or not second:
        return 0
    return round(1000 * len(first & second) / len(first | second))


def _range_corrob_milli(
    episode_range: tuple[int, int] | None, printed_pages: Sequence[int]
) -> int:
    if episode_range is None or not printed_pages:
        return 0
    start, end = episode_range
    inside = sum(1 for page in printed_pages if start <= page <= end)
    if inside == len(printed_pages):
        return 1000
    if inside > 0:
        return 500
    return 0


def _anchor_bonus_milli(
    anchor_ranges: Sequence[tuple[int, int]], solution_pages: Sequence[int]
) -> int:
    if not anchor_ranges or not solution_pages:
        return 0
    pages = set(solution_pages)
    hits = sum(
        1
        for start, end in anchor_ranges
        if any(start <= page <= end for page in pages)
    )
    return round(1000 * hits / len(anchor_ranges))


def score_edge(
    scope: ProblemScopeProfile, episode: EpisodeProfile, role: str = ROLE_DETAILED
) -> ScopeEdge:
    """Score one (scope, episode) pair; inadmissible edges are kept for the
    snapshot with status="rejected" and a reason, but never enter the DP."""
    ordered = _ordered_overlap_milli(scope.ordered_numbers, episode.ordered_numbers)
    hierarchy = _jaccard_milli(scope.section_tokens, episode.section_tokens)
    # Any-token label intersection is evidence only; the TRUST tier demands
    # an exact match of the normalized non-generic label token sets.
    label_hit = bool(
        scope.explicit_label_tokens
        and episode.label_tokens
        and set(scope.explicit_label_tokens) & set(episode.label_tokens)
    )
    label_exact = bool(
        scope.explicit_label_tokens
        and episode.label_tokens
        and set(scope.explicit_label_tokens) == set(episode.label_tokens)
    )
    range_corrob = _range_corrob_milli(
        episode.printed_problem_page_range, scope.printed_pages
    )
    anchor_bonus = _anchor_bonus_milli(
        scope.anchor_printed_ranges, episode.printed_solution_pages
    )
    ordered_ok = (
        ordered >= TRUST_ORDER_MILLI
        and len(scope.ordered_numbers) >= TRUST_MIN_NUMBERS
    )
    shared_hierarchy = len(
        set(scope.section_tokens) & set(episode.section_tokens)
    )
    trusted = ordered_ok and (
        label_exact or shared_hierarchy >= TRUST_HIERARCHY_SHARED_TOKENS
    )
    evidence: list[str] = []
    if trusted:
        evidence.append("trusted:label" if label_exact else "trusted:hierarchy")
    elif label_hit:
        evidence.append("label_partial")
    admissible = True
    reject_reason: str | None = None
    if not trusted:
        if ordered >= TRUST_ORDER_MILLI:
            pass  # strong number-only edge: admissible but never auto-trusted
        elif ordered >= FLOOR_ORDER_MILLI and (
            hierarchy or range_corrob or anchor_bonus or label_hit
        ):
            pass
        else:
            admissible = False
            reject_reason = (
                "ordered_below_floor"
                if ordered < FLOOR_ORDER_MILLI
                else "no_corroborating_signal"
            )
    score: Score = (
        1 if trusted else 0,
        1,
        ordered,
        hierarchy,
        range_corrob,
        anchor_bonus,
    )
    return ScopeEdge(
        scope_id=scope.scope_id,
        episode_id=episode.episode_id,
        role=role,
        score=score,
        trusted=trusted,
        admissible=admissible,
        status="admissible" if admissible else "rejected",
        reject_reason=reject_reason,
        evidence=tuple(evidence),
    )


def _add(left: Score, right: Score) -> Score:
    return tuple(a + b for a, b in zip(left, right))  # type: ignore[return-value]


def _margin(optimum: Score, runner_up: Score) -> float:
    if optimum[0] != runner_up[0] or optimum[1] != runner_up[1]:
        return 1.0
    for tier in range(2, 6):
        if optimum[tier] != runner_up[tier]:
            return min(1.0, max(0.0, (optimum[tier] - runner_up[tier]) / 1000))
    return 0.0


def align_role(
    scopes: Sequence[ProblemScopeProfile],
    episodes: Sequence[EpisodeProfile],
    role: str,
) -> tuple[tuple[AlignedScope, ...], tuple[ScopeEdge, ...]]:
    """Monotonic DP over scopes (sequence order) x episodes (page order)."""
    ordered_scopes = sorted(scopes, key=lambda s: (s.first_sequence, s.scope_id))
    if len({s.first_sequence for s in ordered_scopes}) != len(ordered_scopes):
        raise ValueError("problem scopes must have distinct first_sequence")
    pool = sorted(
        (e for e in episodes if role in e.roles or ROLE_MIXED in e.roles),
        key=lambda e: (e.order_key, e.episode_id),
    )
    all_edges: list[ScopeEdge] = []
    edge_map: dict[tuple[int, int], ScopeEdge] = {}
    edge_index: dict[tuple[int, int], int] = {}
    for i, scope in enumerate(ordered_scopes):
        for j, episode in enumerate(pool):
            edge = score_edge(scope, episode, role)
            edge_index[(i, j)] = len(all_edges)
            all_edges.append(edge)
            if edge.admissible:
                edge_map[(i, j)] = edge

    n, m = len(ordered_scopes), len(pool)
    forward: list[list[Score | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    forward[0][0] = ZERO
    for i in range(n + 1):
        for j in range(m + 1):
            cur = forward[i][j]
            if cur is None:
                continue
            if i < n and (forward[i + 1][j] is None or cur > forward[i + 1][j]):
                forward[i + 1][j] = cur
            if j < m and (forward[i][j + 1] is None or cur > forward[i][j + 1]):
                forward[i][j + 1] = cur
            if i < n and j < m and (i, j) in edge_map:
                new = _add(cur, edge_map[(i, j)].score)
                if forward[i + 1][j + 1] is None or new > forward[i + 1][j + 1]:
                    forward[i + 1][j + 1] = new
    backward: list[list[Score]] = [[ZERO] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            options = [backward[i + 1][j], backward[i][j + 1]]
            if (i, j) in edge_map:
                options.append(_add(edge_map[(i, j)].score, backward[i + 1][j + 1]))
            backward[i][j] = max(options)
    optimum = forward[n][m] or ZERO

    entries: list[AlignedScope] = []
    for i, scope in enumerate(ordered_scopes):
        pair_scores: list[tuple[EpisodeProfile, Score]] = []
        for j, episode in enumerate(pool):
            prefix = forward[i][j]
            if prefix is None or (i, j) not in edge_map:
                continue
            total = _add(_add(prefix, edge_map[(i, j)].score), backward[i + 1][j + 1])
            pair_scores.append((episode, total))
        on_optimal = [ep for ep, sc in pair_scores if sc == optimum]
        unmatched = [
            _add(forward[i][j] or ZERO, backward[i + 1][j])
            for j in range(m + 1)
            if forward[i][j] is not None
        ]
        unmatched_optimal = optimum in unmatched
        allows = tuple(ep.episode_id for ep in on_optimal)
        evidence: list[str] = []
        selected: EpisodeProfile | None = None
        margin = 0.0
        ambiguous = False
        if len(on_optimal) > 1:
            ambiguous = True
            evidence.append("scope_alignment_ambiguous")
        elif len(on_optimal) == 1:
            chosen = on_optimal[0]
            edge = next(
                edge_map[(i, j)]
                for j, ep in enumerate(pool)
                if ep.episode_id == chosen.episode_id and (i, j) in edge_map
            )
            evidence.extend(edge.evidence)
            alternatives = [
                sc for ep, sc in pair_scores if ep.episode_id != chosen.episode_id
            ] + unmatched
            margin = _margin(optimum, max(alternatives, default=ZERO))
            # Auto-selection gates — anything weaker stays allows-only:
            if unmatched_optimal:
                evidence.append("scope_alignment_unresolved")
            elif not edge.trusted:
                evidence.append("scope_alignment_untrusted")
            elif not chosen.coverage_complete:
                evidence.append("episode_coverage_incomplete")
            elif margin < MIN_SCOPE_MARGIN:
                evidence.append("scope_alignment_low_margin")
            else:
                selected = chosen
        if selected is not None:
            for j, ep in enumerate(pool):
                if ep.episode_id == selected.episode_id and (i, j) in edge_map:
                    all_edges[edge_index[(i, j)]] = _with_status(
                        all_edges[edge_index[(i, j)]], "selected"
                    )
            entries.append(
                AlignedScope(
                    scope_id=scope.scope_id,
                    role=role,
                    aligned_episode_id=selected.episode_id,
                    allows=allows,
                    margin=margin,
                    ambiguous=False,
                    evidence=tuple(evidence),
                )
            )
        else:
            allowed_ids = set(allows)
            for j, ep in enumerate(pool):
                if ep.episode_id in allowed_ids and (i, j) in edge_map:
                    all_edges[edge_index[(i, j)]] = _with_status(
                        all_edges[edge_index[(i, j)]], "allowed"
                    )
            entries.append(
                AlignedScope(
                    scope_id=scope.scope_id,
                    role=role,
                    aligned_episode_id=None,
                    allows=allows,
                    margin=0.0 if ambiguous else margin,
                    ambiguous=ambiguous,
                    evidence=tuple(evidence),
                )
            )
    return tuple(entries), tuple(all_edges)


def _with_status(
    edge: ScopeEdge, status: str, extra_evidence: tuple[str, ...] = ()
) -> ScopeEdge:
    evidence = edge.evidence + tuple(
        item for item in extra_evidence if item not in edge.evidence
    )
    return ScopeEdge(
        scope_id=edge.scope_id,
        episode_id=edge.episode_id,
        role=edge.role,
        score=edge.score,
        trusted=edge.trusted,
        admissible=edge.admissible,
        status=status,
        reject_reason=edge.reject_reason,
        evidence=evidence,
    )


def _answers_congruent(
    quick: EpisodeProfile, detail: EpisodeProfile
) -> bool:
    """Corroboration demands >=2 common answers covering >=4/5 of the smaller
    answer set, all in agreement. One lucky match proves nothing."""
    quick_map = { _norm_number(n): a for n, a in quick.explicit_answers if a }
    detail_map = { _norm_number(n): a for n, a in detail.explicit_answers if a }
    common = set(quick_map) & set(detail_map)
    smaller = min(len(quick_map), len(detail_map))
    if len(common) < ANSWER_CORROB_MIN_COMMON or smaller == 0:
        return False
    if len(common) * ANSWER_CORROB_COVERAGE_DEN < smaller * ANSWER_CORROB_COVERAGE_NUM:
        return False
    return all(
        unicodedata.normalize("NFKC", quick_map[n]).casefold()
        == unicodedata.normalize("NFKC", detail_map[n]).casefold()
        for n in common
    )


def _candidate_groups(
    scopes: Sequence[ProblemScopeProfile],
    episodes: Sequence[EpisodeProfile],
    role: str,
) -> tuple[ScopeCandidateGroup, ...]:
    def section_compatible(left: Sequence[str], right: Sequence[str]) -> bool:
        return not left or not right or bool(set(left) & set(right))

    ordered_scopes = sorted(scopes, key=lambda item: (item.first_sequence, item.scope_id))
    ordered_episodes = sorted(
        (item for item in episodes if role in item.roles or ROLE_MIXED in item.roles),
        key=lambda item: (item.order_key, item.episode_id),
    )
    groups: list[ScopeCandidateGroup] = []
    for scope in ordered_scopes:
        target_count = len({_norm_number(number) for number in scope.ordered_numbers})
        for start in range(len(ordered_episodes)):
            for end in range(start + 2, len(ordered_episodes) + 1):
                window = ordered_episodes[start:end]
                covered = {
                    _norm_number(number)
                    for item in window
                    for number in item.ordered_numbers
                }
                if len(covered) < target_count:
                    continue
                if not all(
                    section_compatible(scope.section_tokens, item.section_tokens)
                    for item in window
                ):
                    break
                combined = EpisodeProfile(
                    episode_id="+".join(item.episode_id for item in window),
                    order_key=window[0].order_key,
                    roles=(role,),
                    section_tokens=tuple(dict.fromkeys(token for item in window for token in item.section_tokens)),
                    label_tokens=tuple(dict.fromkeys(token for item in window for token in item.label_tokens)),
                    ordered_numbers=tuple(number for item in window for number in item.ordered_numbers),
                    printed_problem_page_range=None,
                    printed_solution_pages=tuple(page for item in window for page in item.printed_solution_pages),
                    coverage_complete=all(item.coverage_complete for item in window),
                )
                edge = score_edge(scope, combined, role)
                if edge.trusted and combined.coverage_complete:
                    groups.append(ScopeCandidateGroup((scope.scope_id,), tuple(item.episode_id for item in window), role))
                break
    for episode in ordered_episodes:
        target_count = len({_norm_number(number) for number in episode.ordered_numbers})
        for start in range(len(ordered_scopes)):
            for end in range(start + 2, len(ordered_scopes) + 1):
                window = ordered_scopes[start:end]
                covered = {
                    _norm_number(number)
                    for item in window
                    for number in item.ordered_numbers
                }
                if len(covered) < target_count:
                    continue
                if not all(
                    section_compatible(item.section_tokens, episode.section_tokens)
                    for item in window
                ):
                    break
                combined = ProblemScopeProfile(
                    scope_id="+".join(item.scope_id for item in window),
                    first_sequence=window[0].first_sequence,
                    section_tokens=tuple(dict.fromkeys(token for item in window for token in item.section_tokens)),
                    explicit_label_tokens=tuple(dict.fromkeys(token for item in window for token in item.explicit_label_tokens)),
                    ordered_numbers=tuple(number for item in window for number in item.ordered_numbers),
                    printed_pages=tuple(page for item in window for page in item.printed_pages),
                    anchor_printed_ranges=tuple(value for item in window for value in item.anchor_printed_ranges),
                )
                if score_edge(combined, episode, role).trusted and episode.coverage_complete:
                    groups.append(ScopeCandidateGroup(tuple(item.scope_id for item in window), (episode.episode_id,), role))
                break
    return tuple(dict.fromkeys(groups))


def align_scopes(
    scopes: Sequence[ProblemScopeProfile],
    episodes: Sequence[EpisodeProfile],
) -> ScopeAlignment:
    """Role-aware alignment: detailed first, quick with detail corroboration.

    Quick alignment is computed independently but a quick selection is
    downgraded to allows-only (REVIEW-only) unless either its own edge is
    trusted, or the same scope has a trusted detailed alignment whose
    explicit answers agree with the quick episode (B2). A quick episode that
    sits on the optimal path of more than one scope (pooled table) is never
    auto-selected.
    """
    detail_entries, detail_edges = align_role(scopes, episodes, ROLE_DETAILED)
    quick_entries, quick_edges = align_role(scopes, episodes, ROLE_QUICK)
    episode_by_id = {e.episode_id: e for e in episodes}
    detail_by_scope = {entry.scope_id: entry for entry in detail_entries}
    numbers_by_scope = {
        scope.scope_id: {_norm_number(n) for n in scope.ordered_numbers}
        for scope in scopes
    }

    def _is_pooled(scope_id: str, episode_id: str) -> bool:
        """A quick table substantially larger than the scope it would serve
        is a pooled (multi-scope) answer table: allows-only, never selected.
        (Number-value intersection with other scopes is useless here — with
        repeated numbering every scope shares 01..NN values.)"""
        episode = episode_by_id.get(episode_id)
        if episode is None:
            return False
        episode_count = len({_norm_number(n) for n in episode.ordered_numbers})
        scope_count = len(numbers_by_scope.get(scope_id, ()))
        if scope_count == 0:
            return True
        return (
            episode_count - scope_count >= TRUST_MIN_NUMBERS
            and 2 * episode_count > 3 * scope_count
        )

    def _detail_corroborates(scope_id: str, episode_id: str) -> bool:
        detail_entry = detail_by_scope.get(scope_id)
        if (
            detail_entry is None
            or detail_entry.aligned_episode_id is None
            or detail_entry.ambiguous
        ):
            return False
        detail_episode = episode_by_id.get(detail_entry.aligned_episode_id)
        quick_episode = episode_by_id.get(episode_id)
        if detail_episode is None or quick_episode is None:
            return False
        return _answers_congruent(quick_episode, detail_episode)

    final_quick_edges = list(quick_edges)
    quick_edge_pos = {
        (edge.scope_id, edge.episode_id): index
        for index, edge in enumerate(final_quick_edges)
    }

    def _rewrite_edge(
        scope_id: str, episode_id: str, status: str, extra: tuple[str, ...]
    ) -> None:
        index = quick_edge_pos.get((scope_id, episode_id))
        if index is not None:
            final_quick_edges[index] = _with_status(
                final_quick_edges[index], status, extra
            )

    adjusted_quick: list[AlignedScope] = []
    for entry in quick_entries:
        if entry.aligned_episode_id is not None:
            # Trusted, gate-passing quick selection: only the pooled check
            # can still demote it.
            if _is_pooled(entry.scope_id, entry.aligned_episode_id):
                _rewrite_edge(
                    entry.scope_id,
                    entry.aligned_episode_id,
                    "allowed",
                    ("pooled_quick_candidate",),
                )
                adjusted_quick.append(
                    AlignedScope(
                        scope_id=entry.scope_id,
                        role=entry.role,
                        aligned_episode_id=None,
                        allows=tuple(
                            dict.fromkeys(
                                (entry.aligned_episode_id,) + entry.allows
                            )
                        ),
                        margin=0.0,
                        ambiguous=False,
                        evidence=entry.evidence + ("pooled_quick_candidate",),
                    )
                )
            else:
                adjusted_quick.append(entry)
            continue
        # Allows-only entry blocked solely by trust: detail answer
        # corroboration (>=2 answers, >=80% of the smaller set) may promote
        # it — never past pooled/ambiguity/coverage/margin blocks (B2).
        promotable = (
            "scope_alignment_untrusted" in entry.evidence
            and not entry.ambiguous
            and len(entry.allows) == 1
            and not any(
                marker in entry.evidence
                for marker in (
                    "scope_alignment_unresolved",
                    "episode_coverage_incomplete",
                    "scope_alignment_low_margin",
                )
            )
        )
        if promotable:
            candidate = entry.allows[0]
            if not _is_pooled(entry.scope_id, candidate) and _detail_corroborates(
                entry.scope_id, candidate
            ):
                # Promotion replaces the hard untrusted marker with explicit
                # corroboration provenance so downstream gates do not re-block,
                # and the snapshot edge is rewritten to match the entry.
                promoted_evidence = tuple(
                    marker
                    for marker in entry.evidence
                    if marker != "scope_alignment_untrusted"
                ) + ("detail_answer_corroborated",)
                _rewrite_edge(
                    entry.scope_id,
                    candidate,
                    "selected",
                    ("detail_answer_corroborated",),
                )
                adjusted_quick.append(
                    AlignedScope(
                        scope_id=entry.scope_id,
                        role=entry.role,
                        aligned_episode_id=candidate,
                        allows=entry.allows,
                        margin=entry.margin,
                        ambiguous=False,
                        evidence=promoted_evidence,
                    )
                )
                continue
            evidence = entry.evidence
            if "quick_uncorroborated" not in evidence:
                evidence = evidence + ("quick_uncorroborated",)
            adjusted_quick.append(
                AlignedScope(
                    scope_id=entry.scope_id,
                    role=entry.role,
                    aligned_episode_id=None,
                    allows=entry.allows,
                    margin=entry.margin,
                    ambiguous=False,
                    evidence=evidence,
                )
            )
            continue
        adjusted_quick.append(entry)
    return ScopeAlignment(
        entries=tuple(detail_entries) + tuple(adjusted_quick),
        edges=tuple(detail_edges) + tuple(final_quick_edges),
        candidate_groups=(
            _candidate_groups(scopes, episodes, ROLE_DETAILED)
            + _candidate_groups(scopes, episodes, ROLE_QUICK)
        ),
    )
