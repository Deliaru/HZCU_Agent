from __future__ import annotations

from collections.abc import Callable, Iterable

from hzcu_agent.schemas import Evidence


class EvidenceWorkspace:
    """Task-local evidence ledger with stable IDs and newest-observation deduplication."""

    def __init__(self, task_id: str) -> None:
        self._task_token = task_id.removeprefix("task_")[:12]
        self._items: list[Evidence] = []
        self._indexes: dict[str, int] = {}
        self._scores: dict[str, float] = {}

    @property
    def items(self) -> list[Evidence]:
        return list(self._items)

    def merge(
        self,
        candidates: Iterable[Evidence],
        *,
        retrieval_scores: dict[str, float] | None = None,
    ) -> list[Evidence]:
        changed: list[Evidence] = []
        scores = retrieval_scores or {}
        for arrival_rank, candidate in enumerate(candidates, start=1):
            key = candidate.canonical_url.strip()
            incoming_score = scores.get(key, 1.0 / (60 + arrival_rank))
            existing_index = self._indexes.get(key)
            if existing_index is None:
                evidence_id = f"ev{len(self._items) + 1:03d}_{self._task_token}"
                item = candidate.model_copy(update={"evidence_id": evidence_id})
                self._indexes[key] = len(self._items)
                self._items.append(item)
                self._scores[key] = incoming_score
                changed.append(item)
                continue
            previous_score = self._scores.get(key, 0.0)
            self._scores[key] = max(previous_score, incoming_score) + min(
                previous_score,
                incoming_score,
            ) * 0.25
            existing = self._items[existing_index]
            if (
                candidate.document_version_id
                and candidate.document_version_id == existing.document_version_id
                and candidate.observed_at == existing.observed_at
            ):
                item = candidate.model_copy(
                    update={
                        "evidence_id": existing.evidence_id,
                        "excerpt": _merge_excerpts(existing.excerpt, candidate.excerpt),
                    }
                )
                self._items[existing_index] = item
                changed.append(item)
                continue
            if candidate.observed_at >= existing.observed_at:
                item = candidate.model_copy(update={"evidence_id": existing.evidence_id})
                self._items[existing_index] = item
                changed.append(item)
        return changed

    def ranked(self, *, limit: int = 24) -> list[Evidence]:
        """Return globally ranked, source-diverse evidence across tool calls."""

        ranked = sorted(
            self._items,
            key=lambda item: (
                self._scores.get(item.canonical_url.strip(), 0.0),
                _authority_score(item.authority_level),
                item.observed_at,
            ),
            reverse=True,
        )
        selected: list[Evidence] = []
        deferred: list[Evidence] = []
        source_counts: dict[str, int] = {}
        for item in ranked:
            count = source_counts.get(item.source_id, 0)
            if len(selected) < 12 and count >= 3:
                deferred.append(item)
                continue
            selected.append(item)
            source_counts[item.source_id] = count + 1
            if len(selected) >= limit:
                return selected
        for item in deferred:
            selected.append(item)
            if len(selected) >= limit:
                break
        return selected

    def view(
        self,
        ranker: Callable[[list[Evidence]], list[Evidence]],
    ) -> list[Evidence]:
        return ranker(self.items)

    def evidence_ids(self) -> frozenset[str]:
        return frozenset(item.evidence_id for item in self._items)


def _merge_excerpts(first: str, second: str, *, max_chars: int = 160_000) -> str:
    first = first.strip()
    second = second.strip()
    if not first:
        return second[:max_chars]
    if not second or second in first:
        return first[:max_chars]
    if first in second:
        return second[:max_chars]
    return f"{first}\n…\n{second}"[:max_chars]


def _authority_score(value: str) -> int:
    return {
        "official": 3,
        "official_secondary": 2,
        "curated": 1,
    }.get(value, 0)
