from __future__ import annotations

from collections.abc import Callable, Iterable

from hzcu_agent.schemas import Evidence


class EvidenceWorkspace:
    """Task-local evidence ledger with stable IDs and newest-observation deduplication."""

    def __init__(self, task_id: str) -> None:
        self._task_token = task_id.removeprefix("task_")[:12]
        self._items: list[Evidence] = []
        self._indexes: dict[str, int] = {}

    @property
    def items(self) -> list[Evidence]:
        return list(self._items)

    def merge(self, candidates: Iterable[Evidence]) -> list[Evidence]:
        changed: list[Evidence] = []
        for candidate in candidates:
            key = candidate.canonical_url.strip()
            existing_index = self._indexes.get(key)
            if existing_index is None:
                evidence_id = f"ev{len(self._items) + 1:03d}_{self._task_token}"
                item = candidate.model_copy(update={"evidence_id": evidence_id})
                self._indexes[key] = len(self._items)
                self._items.append(item)
                changed.append(item)
                continue
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
