from __future__ import annotations

import bisect
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, Field
from sqlalchemy import select

from hzcu_agent.db import Database
from hzcu_agent.models import (
    DocumentVersion,
    SourceDefinitionRecord,
    SourceResource,
    new_id,
)
from hzcu_agent.schemas import Evidence, ToolError, ToolResult

_EXCLUDED_QUALITY = (
    "rejected",
    "retracted",
    "excluded_temporal",
    "excluded_expired_event",
    "binary_mirrored",
    "image_pending_transcription",
    "pdf_pending_ocr",
)
_PDF_PAGE_MARKER = re.compile(r"(?m)^【PDF 第 (?P<page>\d+) 页】$")
_GENERIC_BLOCK_CHARS = 4_000


class CampusDocumentInspectArguments(BaseModel):
    document_version_id: Annotated[str, Field(min_length=1, max_length=64)]


class CampusDocumentFindArguments(BaseModel):
    document_version_id: Annotated[str, Field(min_length=1, max_length=64)]
    query: Annotated[str, Field(min_length=1, max_length=200)]
    top_k: int = Field(default=8, ge=1, le=16)
    context_chars: int = Field(default=800, ge=300, le=2_000)


class CampusDocumentReadArguments(BaseModel):
    document_version_id: Annotated[str, Field(min_length=1, max_length=64)]
    offset: int = Field(default=0, ge=0, le=600_000)
    max_chars: int = Field(default=6_000, ge=1_000, le=12_000)


class CampusDocumentReadLocatorArguments(BaseModel):
    document_version_id: Annotated[str, Field(min_length=1, max_length=64)]
    locator: int = Field(ge=1, le=10_000)


@dataclass(frozen=True)
class _ResolvedDocument:
    version: DocumentVersion
    resource: SourceResource
    source: SourceDefinitionRecord


class CampusDocumentExplorer:
    """Atomic, topic-agnostic exploration over one model-selected document."""

    inspect_name = "inspect_campus_document"
    find_name = "find_in_campus_document"
    read_locator_name = "read_campus_document_locator"
    read_name = "read_campus_document_segment"
    version = "1.0.0"

    def __init__(self, database: Database) -> None:
        self._database = database

    async def inspect(
        self,
        arguments: CampusDocumentInspectArguments,
        trace_id: str,
        *,
        allowed_visibilities: frozenset[str] | None = None,
    ) -> ToolResult:
        resolved = await self._resolve(
            arguments.document_version_id,
            allowed_visibilities,
        )
        if resolved is None:
            return self._not_available(self.inspect_name, trace_id)

        body = resolved.version.normalized_text
        locators = _document_locators(body)
        return ToolResult(
            tool=self.inspect_name,
            version=self.version,
            status="ok",
            data={
                **self._document_metadata(resolved),
                "total_chars": len(body),
                "locator_kind": "page" if _page_offsets(body) else "block",
                "locators": locators,
            },
            trace_id=trace_id,
        )

    async def find(
        self,
        arguments: CampusDocumentFindArguments,
        trace_id: str,
        *,
        allowed_visibilities: frozenset[str] | None = None,
    ) -> ToolResult:
        resolved = await self._resolve(
            arguments.document_version_id,
            allowed_visibilities,
        )
        if resolved is None:
            return self._not_available(self.find_name, trace_id)

        body = resolved.version.normalized_text
        matches = _find_passages(
            body,
            arguments.query,
            top_k=arguments.top_k,
            context_chars=arguments.context_chars,
        )
        data_matches = [
            {
                "offset": item["offset"],
                "end_offset": item["end_offset"],
                "context_start": item["context_start"],
                "context_end": item["context_end"],
                "page": item["page"],
                "matched_terms": item["matched_terms"],
                "preview": item["preview"],
            }
            for item in matches
        ]
        if not matches:
            return ToolResult(
                tool=self.find_name,
                version=self.version,
                status="ok",
                data={
                    **self._document_metadata(resolved),
                    "query": arguments.query,
                    "match_count": 0,
                    "matches": [],
                },
                warnings=["该表达在所选文档中没有直接文本命中，可由模型换词或检查文档结构。"],
                trace_id=trace_id,
            )

        excerpt = "\n\n".join(
            (
                f"【文内命中 {index}；offset={item['offset']}"
                f"{f'；PDF第{item["page"]}页' if item['page'] is not None else ''}】\n"
                f"{item['excerpt']}"
            )
            for index, item in enumerate(matches, start=1)
        )
        return ToolResult(
            tool=self.find_name,
            version=self.version,
            status="ok",
            data={
                **self._document_metadata(resolved),
                "query": arguments.query,
                "match_count": len(matches),
                "matches": data_matches,
            },
            evidence=[self._evidence(resolved, excerpt, suffix=f"find:{arguments.query}")],
            trace_id=trace_id,
        )

    async def read(
        self,
        arguments: CampusDocumentReadArguments,
        trace_id: str,
        *,
        allowed_visibilities: frozenset[str] | None = None,
    ) -> ToolResult:
        resolved = await self._resolve(
            arguments.document_version_id,
            allowed_visibilities,
        )
        if resolved is None:
            return self._not_available(self.read_name, trace_id)

        body = resolved.version.normalized_text
        total_chars = len(body)
        if arguments.offset >= total_chars and total_chars:
            return ToolResult(
                tool=self.read_name,
                version=self.version,
                status="error",
                data={
                    "document_version_id": resolved.version.id,
                    "total_chars": total_chars,
                },
                error=ToolError(
                    code="DOCUMENT_OFFSET_OUT_OF_RANGE",
                    message="读取起点超过文档正文长度。",
                    retryable=False,
                ),
                trace_id=trace_id,
            )

        end = min(total_chars, arguments.offset + arguments.max_chars)
        excerpt = body[arguments.offset : end]
        page_starts = _page_offsets(body)
        return ToolResult(
            tool=self.read_name,
            version=self.version,
            status="ok",
            data={
                **self._document_metadata(resolved),
                "offset": arguments.offset,
                "end_offset": end,
                "returned_chars": len(excerpt),
                "total_chars": total_chars,
                "at_document_end": end >= total_chars,
                "previous_offset": (
                    None
                    if arguments.offset == 0
                    else max(0, arguments.offset - arguments.max_chars)
                ),
                "next_offset": None if end >= total_chars else end,
                "start_page": _page_at_offset(page_starts, arguments.offset),
                "end_page": _page_at_offset(page_starts, max(arguments.offset, end - 1)),
            },
            evidence=[
                self._evidence(
                    resolved,
                    excerpt,
                    suffix=f"segment:{arguments.offset}:{end}",
                )
            ],
            trace_id=trace_id,
        )

    async def read_locator(
        self,
        arguments: CampusDocumentReadLocatorArguments,
        trace_id: str,
        *,
        allowed_visibilities: frozenset[str] | None = None,
    ) -> ToolResult:
        resolved = await self._resolve(
            arguments.document_version_id,
            allowed_visibilities,
        )
        if resolved is None:
            return self._not_available(self.read_locator_name, trace_id)

        body = resolved.version.normalized_text
        located = _locate_document_unit(body, arguments.locator)
        if located is None:
            locator_kind = "page" if _page_offsets(body) else "block"
            return ToolResult(
                tool=self.read_locator_name,
                version=self.version,
                status="error",
                data={
                    "document_version_id": resolved.version.id,
                    "locator_kind": locator_kind,
                    "locator": arguments.locator,
                    "locator_count": len(_document_locators(body)),
                },
                error=ToolError(
                    code="DOCUMENT_LOCATOR_OUT_OF_RANGE",
                    message="指定的页或文本块不在当前文档中。",
                    retryable=False,
                ),
                trace_id=trace_id,
            )

        (
            locator_kind,
            start,
            end,
            locator_count,
            previous_locator,
            next_locator,
            unit_text,
        ) = located
        excerpt = _locator_evidence(unit_text, locator_kind, arguments.locator)
        return ToolResult(
            tool=self.read_locator_name,
            version=self.version,
            status="ok",
            data={
                **self._document_metadata(resolved),
                "locator_kind": locator_kind,
                "locator": arguments.locator,
                "locator_count": locator_count,
                "offset": start,
                "end_offset": end,
                "returned_chars": len(unit_text),
                "previous_locator": previous_locator,
                "next_locator": next_locator,
                "representation": (
                    "continuous_text_with_line_column_map"
                    if locator_kind == "page"
                    else "continuous_text"
                ),
            },
            evidence=[
                self._evidence(
                    resolved,
                    excerpt,
                    suffix=f"locator:{locator_kind}:{arguments.locator}",
                )
            ],
            trace_id=trace_id,
        )

    async def _resolve(
        self,
        document_version_id: str,
        allowed_visibilities: frozenset[str] | None,
    ) -> _ResolvedDocument | None:
        visible_scopes = sorted(allowed_visibilities or frozenset({"public"}))
        if not visible_scopes:
            return None
        async with self._database.session_factory() as session:
            row = (
                await session.execute(
                    select(
                        DocumentVersion,
                        SourceResource,
                        SourceDefinitionRecord,
                    )
                    .join(
                        SourceResource,
                        SourceResource.current_version_id == DocumentVersion.id,
                    )
                    .join(
                        SourceDefinitionRecord,
                        SourceDefinitionRecord.id == SourceResource.source_id,
                    )
                    .where(
                        DocumentVersion.id == document_version_id,
                        SourceDefinitionRecord.enabled.is_(True),
                        SourceDefinitionRecord.visibility.in_(visible_scopes),
                        DocumentVersion.quality_status.not_in(_EXCLUDED_QUALITY),
                    )
                )
            ).first()
        if row is None:
            return None
        return _ResolvedDocument(
            version=row[0],
            resource=row[1],
            source=row[2],
        )

    @staticmethod
    def _document_metadata(resolved: _ResolvedDocument) -> dict:
        return {
            "document_version_id": resolved.version.id,
            "title": resolved.version.title,
            "canonical_url": resolved.resource.canonical_uri,
            "media_type": resolved.version.media_type,
            "published_at": (
                resolved.version.published_at.isoformat()
                if resolved.version.published_at is not None
                else None
            ),
            "text_mode": resolved.version.document_metadata.get("text_mode"),
        }

    @staticmethod
    def _evidence(
        resolved: _ResolvedDocument,
        excerpt: str,
        *,
        suffix: str,
    ) -> Evidence:
        return Evidence(
            evidence_id=new_id("ev"),
            title=resolved.version.title,
            publisher=resolved.version.publisher,
            canonical_url=resolved.resource.canonical_uri,
            published_at=_aware(resolved.version.published_at),
            observed_at=_aware(resolved.version.observed_at) or datetime.now(UTC),
            fresh_until=None,
            excerpt=excerpt,
            source_id=resolved.source.id,
            resource_ref=f"campus-document:{resolved.version.id}:{suffix}",
            document_version_id=resolved.version.id,
            authority_level=resolved.source.authority_level,
            audience_scopes=[resolved.source.visibility],
            effective_from=_aware(resolved.version.effective_from),
            effective_to=_aware(resolved.version.effective_to),
            retrieval_mode="memory",
        )

    def _not_available(self, tool: str, trace_id: str) -> ToolResult:
        return ToolResult(
            tool=tool,
            version=self.version,
            status="error",
            error=ToolError(
                code="DOCUMENT_NOT_AVAILABLE",
                message="该文档不是当前身份可读取的镜像当前版本。",
                retryable=False,
            ),
            trace_id=trace_id,
        )


def _document_locators(body: str) -> list[dict]:
    page_matches = list(_PDF_PAGE_MARKER.finditer(body))
    if page_matches:
        return [
            {
                "locator": int(match.group("page")),
                "page": int(match.group("page")),
                "offset": match.start(),
                "preview": _preview(
                    body[
                        match.end() : (
                            page_matches[index + 1].start()
                            if index + 1 < len(page_matches)
                            else len(body)
                        )
                    ]
                ),
            }
            for index, match in enumerate(page_matches)
        ]
    return [
        {
            "locator": index + 1,
            "block": index + 1,
            "offset": offset,
            "preview": _preview(body[offset : offset + _GENERIC_BLOCK_CHARS]),
        }
        for index, offset in enumerate(range(0, len(body), _GENERIC_BLOCK_CHARS))
    ]


def _locate_document_unit(
    body: str,
    locator: int,
) -> tuple[str, int, int, int, int | None, int | None, str] | None:
    page_matches = list(_PDF_PAGE_MARKER.finditer(body))
    if page_matches:
        for index, match in enumerate(page_matches):
            if int(match.group("page")) != locator:
                continue
            start = match.start()
            end = page_matches[index + 1].start() if index + 1 < len(page_matches) else len(body)
            previous_locator = int(page_matches[index - 1].group("page")) if index > 0 else None
            next_locator = (
                int(page_matches[index + 1].group("page"))
                if index + 1 < len(page_matches)
                else None
            )
            return (
                "page",
                start,
                end,
                len(page_matches),
                previous_locator,
                next_locator,
                body[start:end].rstrip(),
            )
        return None

    start = (locator - 1) * _GENERIC_BLOCK_CHARS
    if start >= len(body):
        return None
    end = min(len(body), start + _GENERIC_BLOCK_CHARS)
    locator_count = max(1, (len(body) + _GENERIC_BLOCK_CHARS - 1) // _GENERIC_BLOCK_CHARS)
    previous_locator = locator - 1 if locator > 1 else None
    next_locator = locator + 1 if locator < locator_count else None
    return (
        "block",
        start,
        end,
        locator_count,
        previous_locator,
        next_locator,
        body[start:end],
    )


def _locator_evidence(unit_text: str, locator_kind: str, locator: int) -> str:
    label = f"PDF 第 {locator} 页" if locator_kind == "page" else f"文本块 {locator}"
    if locator_kind != "page":
        return f"【定位单元：{label}；连续原文】\n{unit_text}"

    return (
        f"【定位单元：{label}；连续原文】\n{unit_text}\n\n"
        "【同一定位单元的通用版面坐标视图】\n"
        "每个 [cNNN] 表示随后文本在该行的起始字符列；它只还原版面位置，"
        "不解释表格、栏目或业务含义。\n"
        f"{_line_column_map(unit_text)}"
    )


def _line_column_map(value: str) -> str:
    mapped_lines: list[str] = []
    for line_number, line in enumerate(value.splitlines(), start=1):
        fragments = [
            f"[c{match.start():03d}]{match.group()}"
            for match in re.finditer(r"[^\s]+(?: [^\s]+)*", line)
        ]
        mapped_lines.append(f"L{line_number:03d} " + " ".join(fragments))
    return "\n".join(mapped_lines)


def _find_passages(
    body: str,
    query: str,
    *,
    top_k: int,
    context_chars: int,
) -> list[dict]:
    normalized_query = unicodedata.normalize("NFKC", query).strip().casefold()
    terms = list(dict.fromkeys(term for term in normalized_query.split() if term))
    if not terms:
        return []
    lowered = body.casefold()
    occurrences: list[tuple[int, str]] = []
    for term in terms:
        occurrences.extend((match.start(), term) for match in re.finditer(re.escape(term), lowered))
    if not occurrences:
        return []

    candidates: list[dict] = []
    half_context = context_chars // 2
    page_starts = _page_offsets(body)
    for position, matched_term in sorted(occurrences):
        start = max(0, position - half_context)
        end = min(len(body), start + context_chars)
        start = max(0, end - context_chars)
        if candidates and start <= candidates[-1]["context_end"]:
            existing = candidates[-1]
            existing["matched_terms"] = sorted(set(existing["matched_terms"]) | {matched_term})
            continue
        window = lowered[start:end]
        matched_terms = [term for term in terms if term in window]
        excerpt = body[start:end]
        candidates.append(
            {
                "offset": position,
                "end_offset": position + len(matched_term),
                "context_start": start,
                "context_end": end,
                "page": _page_at_offset(page_starts, position),
                "matched_terms": matched_terms,
                "preview": _preview(excerpt),
                "excerpt": excerpt,
            }
        )

    candidates.sort(key=lambda item: (-len(item["matched_terms"]), item["offset"]))
    return candidates[:top_k]


def _page_offsets(body: str) -> list[tuple[int, int]]:
    return [(match.start(), int(match.group("page"))) for match in _PDF_PAGE_MARKER.finditer(body)]


def _page_at_offset(page_starts: list[tuple[int, int]], offset: int) -> int | None:
    if not page_starts:
        return None
    positions = [item[0] for item in page_starts]
    index = bisect.bisect_right(positions, offset) - 1
    return page_starts[index][1] if index >= 0 else None


def _preview(value: str, limit: int = 180) -> str:
    compact = re.sub(r"\s+", " ", value).strip()
    return compact[:limit]


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
