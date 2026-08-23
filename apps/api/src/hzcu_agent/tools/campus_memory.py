from __future__ import annotations

import asyncio
import re
import unicodedata
from datetime import UTC, datetime
from typing import Annotated

from pydantic import BaseModel, Field
from sqlalchemy import text

from hzcu_agent.db import Database
from hzcu_agent.ingestion.parsers import normalize_text
from hzcu_agent.models import new_id
from hzcu_agent.schemas import Evidence, ToolError, ToolResult
from hzcu_agent.tools.campus_hybrid import CampusHybridRetriever

_FTS_TABLE = "campus_search_fts_v1"
_FTS_INSERT_TRIGGER = "campus_search_fts_v1_ai"
_FTS_DELETE_TRIGGER = "campus_search_fts_v1_ad"
_FTS_UPDATE_TRIGGER = "campus_search_fts_v1_au"
_EXCLUDED_QUALITY = (
    "rejected",
    "excluded_temporal",
    "excluded_expired_event",
    "binary_mirrored",
    "image_pending_transcription",
    "pdf_pending_ocr",
)
_DASHES = ("-", "—", "–", "－")


class CampusMemorySearchArguments(BaseModel):
    query: Annotated[str, Field(min_length=1, max_length=200)]
    queries: list[Annotated[str, Field(min_length=1, max_length=200)]] = Field(
        default_factory=list,
        max_length=3,
    )
    source_ids: list[
        Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{2,119}$")]
    ] = Field(default_factory=list, max_length=3)
    top_k: int = Field(default=8, ge=1, le=12)


class CampusMemorySearchTool:
    """Search the current campus mirror with one independent FTS query."""

    name = "search_campus_memory"
    version = "2.2.0"

    def __init__(self, database: Database, *, strategy: str = "hybrid_v2") -> None:
        self._database = database
        self._strategy = strategy
        self._hybrid = CampusHybridRetriever(database)
        self._initialize_lock = asyncio.Lock()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            if self._strategy == "hybrid_v2":
                await self._hybrid.initialize()
                self._initialized = True
                return
            if self._database.engine.dialect.name != "sqlite":
                raise RuntimeError("校园镜像全文检索仅支持 SQLite FTS5")
            async with self._database.session_factory() as session:
                await session.execute(
                    text(
                        f"""
                        CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE}
                        USING fts5(
                            document_version_id UNINDEXED,
                            title,
                            body,
                            tokenize='trigram'
                        )
                        """
                    )
                )
                # The first backfill and trigger creation commit atomically.
                # Once the table has one row, persistent triggers are the sole
                # synchronization mechanism; startup must not rescan the corpus.
                indexed = await session.scalar(text(f"SELECT 1 FROM {_FTS_TABLE} LIMIT 1"))
                if indexed is None:
                    await session.execute(
                        text(
                            f"""
                            INSERT INTO {_FTS_TABLE}(
                                rowid,
                                document_version_id,
                                title,
                                body
                            )
                            SELECT rowid, id, title, normalized_text
                            FROM document_versions
                            """
                        )
                    )
                await session.execute(
                    text(
                        f"""
                        CREATE TRIGGER IF NOT EXISTS {_FTS_INSERT_TRIGGER}
                        AFTER INSERT ON document_versions
                        BEGIN
                            INSERT INTO {_FTS_TABLE}(
                                rowid,
                                document_version_id,
                                title,
                                body
                            )
                            VALUES (
                                new.rowid,
                                new.id,
                                new.title,
                                new.normalized_text
                            );
                        END
                        """
                    )
                )
                await session.execute(
                    text(
                        f"""
                        CREATE TRIGGER IF NOT EXISTS {_FTS_DELETE_TRIGGER}
                        AFTER DELETE ON document_versions
                        BEGIN
                            DELETE FROM {_FTS_TABLE} WHERE rowid = old.rowid;
                        END
                        """
                    )
                )
                await session.execute(
                    text(
                        f"""
                        CREATE TRIGGER IF NOT EXISTS {_FTS_UPDATE_TRIGGER}
                        AFTER UPDATE OF title, normalized_text ON document_versions
                        BEGIN
                            DELETE FROM {_FTS_TABLE} WHERE rowid = old.rowid;
                            INSERT INTO {_FTS_TABLE}(
                                rowid,
                                document_version_id,
                                title,
                                body
                            )
                            VALUES (
                                new.rowid,
                                new.id,
                                new.title,
                                new.normalized_text
                            );
                        END
                        """
                    )
                )
                await session.commit()
            self._initialized = True

    async def rebuild(self) -> int:
        """Rebuild the disposable FTS index from all stored document versions."""

        await self.initialize()
        if self._strategy == "hybrid_v2":
            return await self._hybrid.rebuild()
        async with self._initialize_lock:
            async with self._database.session_factory() as session:
                await session.execute(text(f"DELETE FROM {_FTS_TABLE}"))
                result = await session.execute(
                    text(
                        f"""
                        INSERT INTO {_FTS_TABLE}(rowid, document_version_id, title, body)
                        SELECT rowid, id, title, normalized_text
                        FROM document_versions
                        """
                    )
                )
                await session.commit()
                count = await session.scalar(text(f"SELECT count(*) FROM {_FTS_TABLE}"))
                del result
                return int(count or 0)

    async def recreate(self) -> int:
        """Drop and recreate the selected disposable FTS index and triggers."""

        if self._strategy == "hybrid_v2":
            return await self._hybrid.recreate()
        async with self._initialize_lock:
            async with self._database.session_factory() as session:
                for trigger in (
                    _FTS_INSERT_TRIGGER,
                    _FTS_DELETE_TRIGGER,
                    _FTS_UPDATE_TRIGGER,
                ):
                    await session.execute(text(f"DROP TRIGGER IF EXISTS {trigger}"))
                await session.execute(text(f"DROP TABLE IF EXISTS {_FTS_TABLE}"))
                await session.commit()
            self._initialized = False
        await self.initialize()
        async with self._database.session_factory() as session:
            count = await session.scalar(text(f"SELECT count(*) FROM {_FTS_TABLE}"))
        return int(count or 0)

    async def run(
        self,
        arguments: CampusMemorySearchArguments,
        trace_id: str,
        *,
        allowed_visibilities: frozenset[str] | None = None,
    ) -> ToolResult:
        await self.initialize()
        if self._strategy == "hybrid_v2":
            return await self._hybrid.run(
                arguments,
                trace_id,
                allowed_visibilities=allowed_visibilities,
            )
        return await self._run_legacy(
            arguments,
            trace_id,
            allowed_visibilities=allowed_visibilities,
        )

    async def _run_legacy(
        self,
        arguments: CampusMemorySearchArguments,
        trace_id: str,
        *,
        allowed_visibilities: frozenset[str] | None = None,
    ) -> ToolResult:
        await self.initialize()
        visible_scopes = sorted(allowed_visibilities or frozenset({"public"}))
        if not visible_scopes:
            return ToolResult(
                tool=self.name,
                version=self.version,
                status="error",
                error=ToolError(
                    code="SOURCE_SCOPE_DENIED",
                    message="当前服务端身份不允许读取校园镜像。",
                    retryable=False,
                ),
                trace_id=trace_id,
            )

        normalized_query = _normalize_query(arguments.query)
        match_expression = _fts_match_expression(normalized_query)
        if match_expression is None:
            return ToolResult(
                tool=self.name,
                version=self.version,
                status="error",
                data={
                    "query": normalized_query,
                    "retrieval": "sqlite-fts5-trigram-bm25",
                    "visibility_scopes": visible_scopes,
                },
                error=ToolError(
                    code="QUERY_TOO_SHORT",
                    message="本地全文查询至少需要一个三字以上的具体名称，请换用更完整的官方表述。",
                    retryable=True,
                ),
                trace_id=trace_id,
            )

        visibility_bindings = {
            f"visibility_{index}": value for index, value in enumerate(visible_scopes)
        }
        visibility_clause = ", ".join(f":{key}" for key in visibility_bindings)
        quality_bindings = {
            f"quality_{index}": value for index, value in enumerate(_EXCLUDED_QUALITY)
        }
        quality_clause = ", ".join(f":{key}" for key in quality_bindings)
        short_bindings = {
            f"short_term_{index}": value
            for index, value in enumerate(_short_query_terms(normalized_query))
        }
        phrase_bindings = {
            f"phrase_{index}": value
            for index, value in enumerate(_long_query_terms(normalized_query))
        }

        def search_statement(
            active_short_bindings: dict[str, str],
            active_phrase_bindings: dict[str, str],
        ):
            active_short_clauses = [
                (f"(instr(v.title, :{key}) > 0 OR instr(v.normalized_text, :{key}) > 0)")
                for key in active_short_bindings
            ]
            title_match_count = " + ".join(
                f"CASE WHEN instr(lower(v.title), :{key}) > 0 THEN 1 ELSE 0 END"
                for key in active_phrase_bindings
            )
            body_match_count = " + ".join(
                (f"CASE WHEN instr(lower(v.normalized_text), :{key}) > 0 THEN 1 ELSE 0 END")
                for key in active_phrase_bindings
            )
            active_short_clause = (
                "\n              AND " + "\n              AND ".join(active_short_clauses)
                if active_short_clauses
                else ""
            )
            return text(
                f"""
            SELECT
                v.id AS document_version_id,
                v.title,
                v.publisher,
                v.normalized_text,
                v.published_at,
                v.observed_at,
                v.effective_from,
                v.effective_to,
                r.canonical_uri,
                r.resource_type,
                s.id AS source_id,
                s.authority_level,
                snippet({_FTS_TABLE}, 2, '', '', '…', 96) AS match_snippet,
                ({title_match_count}) AS title_match_count,
                ({body_match_count}) AS body_match_count,
                bm25({_FTS_TABLE}, 0.0, 5.0, 1.0) AS search_score
            FROM {_FTS_TABLE}
            JOIN document_versions AS v
                ON v.id = {_FTS_TABLE}.document_version_id
            JOIN source_resources AS r
                ON r.current_version_id = v.id
            JOIN source_definitions AS s
                ON s.id = r.source_id
            WHERE {_FTS_TABLE} MATCH :match_expression
              AND s.enabled = 1
              AND s.visibility IN ({visibility_clause})
              AND v.quality_status NOT IN ({quality_clause}){active_short_clause}
            ORDER BY
                title_match_count DESC,
                CASE r.resource_type
                    WHEN 'pdf' THEN 0
                    WHEN 'attachment' THEN 1
                    WHEN 'html' THEN 2
                    ELSE 3
                END ASC,
                body_match_count DESC,
                search_score ASC,
                COALESCE(v.published_at, v.observed_at) DESC
            LIMIT :result_limit
            """
            )

        base_parameters = {
            "result_limit": max(arguments.top_k * 4, 24),
            **visibility_bindings,
            **quality_bindings,
        }
        async with self._database.session_factory() as session:
            rows = list(
                (
                    await session.execute(
                        search_statement(short_bindings, phrase_bindings),
                        {
                            **base_parameters,
                            **short_bindings,
                            **phrase_bindings,
                            "match_expression": match_expression,
                        },
                    )
                )
                .mappings()
                .all()
            )
            strict_candidate_rows = len(rows)
            match_mode = "strict"
            relaxed_expression = _fts_match_expression(
                normalized_query,
                operator="OR",
            )
            should_relax = not rows and (
                bool(short_bindings) or relaxed_expression != match_expression
            )
            if should_relax and relaxed_expression is not None:
                rows = list(
                    (
                        await session.execute(
                            search_statement({}, phrase_bindings),
                            {
                                **base_parameters,
                                **phrase_bindings,
                                "match_expression": relaxed_expression,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
                match_mode = "relaxed"

            lexical_terms = _lexical_backoff_terms(normalized_query)
            lexical_bindings = {
                f"lexical_term_{index}": value for index, value in enumerate(lexical_terms)
            }
            lexical_expression = _fts_match_expression(
                " ".join(lexical_terms),
                operator="OR",
            )
            should_use_lexical_backoff = (
                not rows
                and lexical_expression is not None
                and lexical_expression not in {match_expression, relaxed_expression}
            )
            if should_use_lexical_backoff:
                rows = list(
                    (
                        await session.execute(
                            search_statement({}, lexical_bindings),
                            {
                                **base_parameters,
                                **lexical_bindings,
                                "match_expression": lexical_expression,
                            },
                        )
                    )
                    .mappings()
                    .all()
                )
                match_mode = "lexical_backoff"

        excerpt_query = (
            " ".join(lexical_terms) if match_mode == "lexical_backoff" else normalized_query
        )

        evidence: list[Evidence] = []
        seen_urls: set[str] = set()
        for row in rows:
            canonical_url = str(row["canonical_uri"]).strip()
            if not canonical_url or canonical_url in seen_urls:
                continue
            seen_urls.add(canonical_url)
            evidence.append(
                Evidence(
                    evidence_id=new_id("ev"),
                    title=str(row["title"]),
                    publisher=str(row["publisher"]),
                    canonical_url=canonical_url,
                    published_at=_aware(row["published_at"]),
                    observed_at=_aware(row["observed_at"]) or datetime.now(UTC),
                    fresh_until=None,
                    excerpt=_evidence_excerpt(
                        body=str(row["normalized_text"]),
                        query=excerpt_query,
                        match_snippet=str(row["match_snippet"]),
                    ),
                    source_id=str(row["source_id"]),
                    resource_ref=f"campus-memory:{row['document_version_id']}",
                    document_version_id=str(row["document_version_id"]),
                    authority_level=str(row["authority_level"]),
                    effective_from=_aware(row["effective_from"]),
                    effective_to=_aware(row["effective_to"]),
                    retrieval_mode="memory",
                )
            )
            if len(evidence) >= arguments.top_k:
                break

        warnings = [] if evidence else ["本地镜像未命中，请改用更具体的官方名称重新检索。"]
        return ToolResult(
            tool=self.name,
            version=self.version,
            status="ok",
            data={
                "query": normalized_query,
                "result_count": len(evidence),
                "retrieval": "sqlite-fts5-trigram-bm25",
                "visibility_scopes": visible_scopes,
                "candidate_rows": len(rows),
                "strict_candidate_rows": strict_candidate_rows,
                "match_mode": match_mode,
                "title_weight": 5.0,
                "body_weight": 1.0,
                "exact_short_terms": (
                    list(short_bindings.values()) if match_mode == "strict" else []
                ),
                "lexical_backoff_terms": (lexical_terms if match_mode == "lexical_backoff" else []),
            },
            evidence=evidence,
            warnings=warnings,
            trace_id=trace_id,
        )


def _normalize_query(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).replace("\u200b", "")
    normalized = re.sub(r"[\"'“”‘’]", "", normalized)
    normalized = re.sub(r"[—–－]", "-", normalized)
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _fts_match_expression(query: str, *, operator: str = "AND") -> str | None:
    phrases = _long_query_terms(query)
    if not phrases:
        return None
    return f" {operator} ".join(_dash_aware_phrase(value) for value in phrases)


def _long_query_terms(query: str) -> list[str]:
    return [
        value.strip()
        for value in query.split(" ")
        if len(re.sub(r"[^\u3400-\u9fffa-z0-9]", "", value)) >= 3
    ]


def _short_query_terms(query: str) -> list[str]:
    return [
        value
        for value in query.split(" ")
        if 1 <= len(re.sub(r"[^\u3400-\u9fffa-z0-9]", "", value)) < 3
    ]


def _lexical_backoff_terms(query: str) -> list[str]:
    """Build a small set of high-coverage windows for zero-hit long phrases.

    FTS phrase matching is deliberately strict. A natural-language query can
    therefore miss a document when its stable subject is followed by different
    wording (for example, a query ending in one action noun while the source uses
    another). This fallback keeps the search operation atomic: it derives at most
    three overlapping windows from each long term and lets FTS/BM25 perform the
    same visibility, quality and current-version filtering as the strict query.
    """

    windows: list[str] = []
    seen: set[str] = set()
    for value in _long_query_terms(query):
        compact = re.sub(r"[^\u3400-\u9fffa-z0-9-]", "", value)
        if len(compact) < 5:
            continue
        window_size = min(8, max(4, (len(compact) + 1) // 2))
        if window_size >= len(compact):
            continue
        last_start = len(compact) - window_size
        for start in (0, last_start // 2, last_start):
            window = compact[start : start + window_size]
            if window not in seen:
                seen.add(window)
                windows.append(window)
    return windows


def _dash_aware_phrase(value: str) -> str:
    variants = {value}
    if any(dash in value for dash in _DASHES):
        base = value
        for dash in _DASHES[1:]:
            base = base.replace(dash, "-")
        variants.update(base.replace("-", dash) for dash in _DASHES)
    quoted = [_quote_fts(value) for value in sorted(variants)]
    return quoted[0] if len(quoted) == 1 else f"({' OR '.join(quoted)})"


def _quote_fts(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _make_excerpt(text_value: str, query: str, width: int = 1200) -> str:
    normalized = text_value.strip()
    lowered = normalized.lower()
    terms = sorted(
        {
            value
            for value in query.split(" ")
            if len(re.sub(r"[^\u3400-\u9fffa-z0-9]", "", value)) >= 2 and value in lowered
        },
        key=len,
        reverse=True,
    )
    candidate_starts = {0}
    occurrence_counts: dict[str, int] = {}
    for term in terms:
        positions = [match.start() for match in re.finditer(re.escape(term), lowered)]
        occurrence_counts[term] = len(positions)
        for position in positions[:256]:
            candidate_starts.add(max(0, position - 180))
            candidate_starts.add(max(0, position - (width // 2)))

    def passage_score(start: int) -> tuple[float, float, int]:
        passage = lowered[start : start + width]
        present = [term for term in terms if term in passage]
        rare_term_score = sum(1 / occurrence_counts[term] for term in present)
        return (
            float(len(present)),
            rare_term_score,
            start,
        )

    start = max(candidate_starts, key=passage_score)
    excerpt = normalized[start : start + width]
    if start:
        excerpt = f"…{excerpt}"
    if start + width < len(normalized):
        excerpt = f"{excerpt}…"
    return excerpt


def _evidence_excerpt(
    *,
    body: str,
    query: str,
    match_snippet: str,
) -> str:
    snippet = normalize_text(match_snippet)
    context = _make_excerpt(body, query)
    if context:
        return context[:1200]
    return snippet[:1200]


def _aware(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
