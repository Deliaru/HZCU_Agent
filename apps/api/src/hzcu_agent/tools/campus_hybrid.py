from __future__ import annotations

import asyncio
import json
import logging
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import jieba
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from hzcu_agent.db import Database
from hzcu_agent.ingestion.parsers import normalize_text
from hzcu_agent.ingestion.search_index import (
    SOURCE_SEARCH_FTS_TABLE,
    ensure_source_search_index,
)
from hzcu_agent.models import new_id
from hzcu_agent.schemas import Evidence, ToolError, ToolResult

DOCUMENT_FTS_TABLE = "campus_search_fts_v2"
DOCUMENT_FTS_INSERT_TRIGGER = "campus_search_fts_v2_ai"
DOCUMENT_FTS_DELETE_TRIGGER = "campus_search_fts_v2_ad"
DOCUMENT_FTS_UPDATE_TRIGGER = "campus_search_fts_v2_au"
RRF_K = 60
CHANNEL_LIMIT = 32
MAX_CANDIDATES = 160
logger = logging.getLogger(__name__)

_EXCLUDED_QUALITY = (
    "rejected",
    "excluded_temporal",
    "excluded_expired_event",
    "image_pending_transcription",
    "pdf_pending_ocr",
)
_QUERY_STOP_TERMS = {
    "一下",
    "为什么",
    "了解",
    "什么",
    "什么时候",
    "几个",
    "哪些",
    "哪里",
    "多少",
    "如何",
    "怎么",
    "怎样",
    "我想",
    "我们",
    "是否",
    "有",
    "有没有",
    "有几个",
    "注意",
    "注意看",
    "现在",
    "目前",
    "看看",
    "查询",
    "全部",
    "列出",
    "名称",
    "所有",
    "分别",
    "完整",
    "请",
    "请问",
    "这个",
    "那个",
}
_ENUMERATION_PATTERN = re.compile(
    r"几个|多少|哪些|有什么|列出|列表|名单|目录|一览|全部|所有|分别"
)
_GENERIC_FILENAME_PATTERN = re.compile(
    r"^(?:image|img|pic|picture|photo|截图|微信图片|[0-9a-f_-]{8,}|\d+)"
    r"(?:\.(?:png|jpe?g|gif|webp))?$",
    re.IGNORECASE,
)
_LISTING_BOILERPLATE_PATTERN = re.compile(
    r"^(?:首页|当前位置|发布日期|访问次数|字号|大|中|小|打印本页|关闭窗口|"
    r"返回顶部|联系我们|更多|上一页|下一页|附件|下载)(?:[:：].*)?$",
    re.IGNORECASE,
)


@dataclass
class HybridCandidate:
    row: dict[str, Any]
    rrf_score: float = 0.0
    channels: set[str] = field(default_factory=set)
    final_score: float = 0.0
    coverage: float = 0.0
    title_coverage: float = 0.0
    scope_listing: bool = False
    variant_scores: dict[int, float] = field(default_factory=dict)


class CampusHybridRetriever:
    """Bounded, source-aware retrieval over the existing SQLite mirror."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._tokenizer = jieba.Tokenizer()
        self._initialize_lock = asyncio.Lock()
        self._initialized = False
        self._source_routing_available = True

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await self._ensure_document_index()
            try:
                await ensure_source_search_index(self._database)
            except SQLAlchemyError as exc:
                self._source_routing_available = False
                logger.warning(
                    "campus source routing index unavailable; using global retrieval: %s",
                    type(exc).__name__,
                )
            self._tokenizer.initialize()
            async with self._database.session_factory() as session:
                words = list(
                    (
                        await session.execute(
                            text(
                                """
                                SELECT name AS value FROM source_definitions
                                UNION
                                SELECT owner_department AS value FROM source_definitions
                                UNION
                                SELECT DISTINCT v.title AS value
                                FROM source_resources AS r
                                JOIN document_versions AS v ON v.id = r.current_version_id
                                WHERE length(v.title) BETWEEN 2 AND 16
                                LIMIT 20000
                                """
                            )
                        )
                    ).scalars()
                )
            for value in words:
                word = normalize_text(str(value))
                if 2 <= len(word) <= 16 and not re.search(r"[，。！？；：\n]", word):
                    self._tokenizer.add_word(word, freq=100_000)
            self._initialized = True

    async def rebuild(self) -> int:
        await self.initialize()
        async with self._initialize_lock:
            async with self._database.session_factory() as session:
                await session.execute(text(f"DELETE FROM {DOCUMENT_FTS_TABLE}"))
                await session.execute(
                    text(
                        f"""
                        INSERT INTO {DOCUMENT_FTS_TABLE}(
                            rowid, document_version_id, title, body
                        )
                        SELECT
                            rowid,
                            id,
                            replace(replace(replace(title, '—', '-'), '–', '-'), '－', '-'),
                            replace(
                                replace(replace(normalized_text, '—', '-'), '–', '-'),
                                '－',
                                '-'
                            )
                        FROM document_versions
                        """
                    )
                )
                count = await session.scalar(
                    text(f"SELECT count(*) FROM {DOCUMENT_FTS_TABLE}")
                )
                await session.commit()
        return int(count or 0)

    async def recreate(self) -> int:
        """Recreate the disposable v2 FTS table and synchronization triggers."""

        async with self._initialize_lock:
            async with self._database.session_factory() as session:
                for trigger in (
                    DOCUMENT_FTS_INSERT_TRIGGER,
                    DOCUMENT_FTS_DELETE_TRIGGER,
                    DOCUMENT_FTS_UPDATE_TRIGGER,
                ):
                    await session.execute(text(f"DROP TRIGGER IF EXISTS {trigger}"))
                await session.execute(text(f"DROP TABLE IF EXISTS {DOCUMENT_FTS_TABLE}"))
                await session.commit()
            self._initialized = False
            self._source_routing_available = True
        await self.initialize()
        async with self._database.session_factory() as session:
            count = await session.scalar(
                text(f"SELECT count(*) FROM {DOCUMENT_FTS_TABLE}")
            )
        return int(count or 0)

    async def _ensure_document_index(self) -> None:
        async with self._database.session_factory() as session:
            await session.execute(
                text(
                    f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS {DOCUMENT_FTS_TABLE}
                    USING fts5(
                        document_version_id UNINDEXED,
                        title,
                        body,
                        tokenize='trigram'
                    )
                    """
                )
            )
            indexed = await session.scalar(
                text(f"SELECT 1 FROM {DOCUMENT_FTS_TABLE} LIMIT 1")
            )
            if indexed is None:
                await session.execute(
                    text(
                        f"""
                        INSERT INTO {DOCUMENT_FTS_TABLE}(
                            rowid, document_version_id, title, body
                        )
                        SELECT
                            rowid,
                            id,
                            replace(replace(replace(title, '—', '-'), '–', '-'), '－', '-'),
                            replace(
                                replace(replace(normalized_text, '—', '-'), '–', '-'),
                                '－',
                                '-'
                            )
                        FROM document_versions
                        """
                    )
                )
            await session.execute(
                text(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {DOCUMENT_FTS_INSERT_TRIGGER}
                    AFTER INSERT ON document_versions
                    BEGIN
                        INSERT INTO {DOCUMENT_FTS_TABLE}(
                            rowid, document_version_id, title, body
                        ) VALUES (
                            new.rowid,
                            new.id,
                            replace(replace(replace(new.title, '—', '-'), '–', '-'), '－', '-'),
                            replace(
                                replace(replace(new.normalized_text, '—', '-'), '–', '-'),
                                '－',
                                '-'
                            )
                        );
                    END
                    """
                )
            )
            await session.execute(
                text(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {DOCUMENT_FTS_DELETE_TRIGGER}
                    AFTER DELETE ON document_versions
                    BEGIN
                        DELETE FROM {DOCUMENT_FTS_TABLE} WHERE rowid = old.rowid;
                    END
                    """
                )
            )
            await session.execute(
                text(
                    f"""
                    CREATE TRIGGER IF NOT EXISTS {DOCUMENT_FTS_UPDATE_TRIGGER}
                    AFTER UPDATE OF title, normalized_text ON document_versions
                    BEGIN
                        DELETE FROM {DOCUMENT_FTS_TABLE} WHERE rowid = old.rowid;
                        INSERT INTO {DOCUMENT_FTS_TABLE}(
                            rowid, document_version_id, title, body
                        ) VALUES (
                            new.rowid,
                            new.id,
                            replace(replace(replace(new.title, '—', '-'), '–', '-'), '－', '-'),
                            replace(
                                replace(replace(new.normalized_text, '—', '-'), '–', '-'),
                                '－',
                                '-'
                            )
                        );
                    END
                    """
                )
            )
            await session.commit()

    async def run(
        self,
        arguments,
        trace_id: str,
        *,
        allowed_visibilities: frozenset[str] | None = None,
    ) -> ToolResult:
        await self.initialize()
        visible_scopes = sorted(allowed_visibilities or frozenset({"public"}))
        if not visible_scopes:
            return ToolResult(
                tool="search_campus_memory",
                version="3.0.0",
                status="error",
                error=ToolError(
                    code="SOURCE_SCOPE_DENIED",
                    message="当前服务端身份不允许读取校园镜像。",
                    retryable=False,
                ),
                trace_id=trace_id,
            )

        query_variants = _unique_queries(arguments.query, arguments.queries)
        term_sets = [self._query_terms(value) for value in query_variants]
        all_terms = _unique(item for terms in term_sets for item in terms)[:12]
        if not any(_fts_eligible(term) for term in all_terms) and not any(
            _fts_eligible(value.replace(" ", "")) for value in query_variants
        ):
            return ToolResult(
                tool="search_campus_memory",
                version="3.0.0",
                status="error",
                data={
                    "query": arguments.query,
                    "query_variants": query_variants,
                    "retrieval": "sqlite-fts5-hybrid-rrf",
                    "strategy": "hybrid_v2",
                    "visibility_scopes": visible_scopes,
                },
                error=ToolError(
                    code="QUERY_TOO_SHORT",
                    message="本地全文查询至少需要一个三字以上的具体名称。",
                    retryable=True,
                ),
                trace_id=trace_id,
            )

        async with self._database.session_factory() as session:
            routed_sources, model_sources = await self._route_sources(
                session,
                arguments.source_ids,
                all_terms,
                visible_scopes,
            )
            candidates: dict[str, HybridCandidate] = {}
            channel_counts: dict[str, int] = {}
            strict_candidate_rows = 0
            successful_short_terms: list[str] = []

            channel_specs: list[
                tuple[str, str, list[str], list[str] | None, float, set[int]]
            ] = []
            seen_specs: dict[tuple[str, str, tuple[str, ...]], int] = {}
            for variant_index, (variant, terms) in enumerate(
                zip(query_variants, term_sets, strict=True)
            ):
                compact = variant.replace(" ", "")
                exact_expression = _fts_expression([compact])
                if exact_expression:
                    _append_channel(
                        channel_specs,
                        seen_specs,
                        "exact",
                        exact_expression,
                        [],
                        None,
                        3.0,
                        variant_index,
                    )
                long_terms = [term for term in terms if _fts_eligible(term)]
                short_terms = [term for term in terms if not _fts_eligible(term)]
                and_expression = _fts_expression(long_terms, operator="AND")
                if and_expression:
                    _append_channel(
                        channel_specs,
                        seen_specs,
                        "token_and",
                        and_expression,
                        short_terms,
                        None,
                        4.0,
                        variant_index,
                    )
                or_expression = _fts_expression(long_terms, operator="OR")
                if or_expression:
                    _append_channel(
                        channel_specs,
                        seen_specs,
                        "token_or",
                        or_expression,
                        [],
                        None,
                        0.8,
                        variant_index,
                    )

            routed_expression = _fts_expression(
                [term for term in all_terms if _fts_eligible(term)],
                operator="OR",
            )
            if routed_expression:
                for source_id in routed_sources:
                    _append_channel(
                        channel_specs,
                        seen_specs,
                        "source_routed",
                        routed_expression,
                        [],
                        [source_id],
                        1.2 if source_id in model_sources else 1.0,
                        None,
                    )

            for index, (
                kind,
                expression,
                short_terms,
                sources,
                weight,
                variant_indices,
            ) in enumerate(
                channel_specs,
                start=1,
            ):
                name = f"{kind}_{index}"
                rows = await self._fetch_candidates(
                    session,
                    expression=expression,
                    required_short_terms=short_terms,
                    source_ids=sources,
                    visible_scopes=visible_scopes,
                )
                channel_counts[name] = len(rows)
                if kind == "exact":
                    strict_candidate_rows += len(rows)
                if kind == "token_and" and rows:
                    successful_short_terms.extend(short_terms)
                for rank, row in enumerate(rows, start=1):
                    url = str(row["canonical_uri"]).strip()
                    if not url:
                        continue
                    candidate = candidates.setdefault(url, HybridCandidate(row=dict(row)))
                    contribution = weight / (RRF_K + rank)
                    candidate.rrf_score += contribution
                    candidate.channels.add(kind)
                    for variant_index in variant_indices:
                        candidate.variant_scores[variant_index] = (
                            candidate.variant_scores.get(variant_index, 0.0)
                            + contribution
                        )

            bounded = sorted(
                candidates.values(),
                key=lambda item: item.rrf_score,
                reverse=True,
            )[:MAX_CANDIDATES]
            is_enumeration = any(
                _ENUMERATION_PATTERN.search(value) for value in query_variants
            )
            inferred_sources = set(routed_sources) - set(model_sources)
            for candidate in bounded:
                self._score_candidate(
                    candidate,
                    query_variants=query_variants,
                    term_sets=term_sets,
                    model_sources=set(model_sources),
                    inferred_sources=inferred_sources,
                    is_enumeration=is_enumeration,
                )

            clustered = _cluster_parent_assets(bounded)
            ranked = sorted(clustered, key=lambda item: item.final_score, reverse=True)
            selected = _diversified(
                ranked,
                arguments.top_k,
                variant_count=len(query_variants),
            )
            selected_urls = {str(item.row["canonical_uri"]) for item in selected}
            candidate_ranking = [
                {
                    "rank": index,
                    "canonical_url": item.row["canonical_uri"],
                    "source_id": item.row["source_id"],
                    "score": round(item.final_score, 6),
                    "channels": sorted(item.channels),
                    "query_variants": sorted(item.variant_scores),
                    "selected": str(item.row["canonical_uri"]) in selected_urls,
                }
                for index, item in enumerate(ranked[:48], start=1)
            ]
            chunks = await self._best_chunks(
                session,
                [str(item.row["document_version_id"]) for item in selected],
                all_terms,
            )

        evidence = [
            _candidate_evidence(candidate, chunks, all_terms)
            for candidate in selected
        ]
        selected_sources = {item.source_id for item in evidence}
        unmatched_model_sources = [
            source_id for source_id in model_sources if source_id not in selected_sources
        ]
        source_counts = Counter(item.source_id for item in evidence)
        concentration = (
            max(source_counts.values(), default=0) / len(evidence) if evidence else 0.0
        )
        has_scope_listing = any(candidate.scope_listing for candidate in selected)
        coverage_risk = bool(
            not evidence
            or unmatched_model_sources
            or (concentration > 0.75 and not has_scope_listing)
            or (is_enumeration and not has_scope_listing)
        )
        warnings: list[str] = []
        if not evidence:
            warnings.append("本地镜像多路检索未命中，请补充具体名称或进行实时搜索。")
        elif coverage_risk:
            warnings.append("候选证据可能未覆盖完整范围，回答前应检查目录、名单或其他来源。")

        return ToolResult(
            tool="search_campus_memory",
            version="3.0.0",
            status="ok",
            data={
                "query": arguments.query,
                "query_variants": query_variants,
                "source_hints": model_sources,
                "routed_sources": routed_sources,
                "unmatched_source_hints": unmatched_model_sources,
                "result_count": len(evidence),
                "candidate_rows": len(candidates),
                "strict_candidate_rows": strict_candidate_rows,
                "retrieval": "sqlite-fts5-hybrid-rrf",
                "strategy": "hybrid_v2",
                "visibility_scopes": visible_scopes,
                "match_mode": "strict" if strict_candidate_rows else "relaxed",
                "exact_short_terms": _unique(successful_short_terms),
                "lexical_backoff_terms": all_terms if not strict_candidate_rows else [],
                "channel_counts": channel_counts,
                "candidate_ranking": candidate_ranking,
                "deduplication": {
                    "bounded_candidates": len(bounded),
                    "parent_assets_collapsed": len(bounded) - len(clustered),
                },
                "source_concentration": round(concentration, 4),
                "coverage_risk": coverage_risk,
                "answer_shape": "enumeration" if is_enumeration else "fact",
                "ranking": [
                    {
                        "rank": index,
                        "canonical_url": item.row["canonical_uri"],
                        "source_id": item.row["source_id"],
                        "score": round(item.final_score, 6),
                        "channels": sorted(item.channels),
                        "term_coverage": round(item.coverage, 4),
                        "scope_listing": item.scope_listing,
                    }
                    for index, item in enumerate(selected, start=1)
                ],
            },
            evidence=evidence,
            warnings=warnings,
            trace_id=trace_id,
        )

    def _query_terms(self, value: str) -> list[str]:
        normalized = _normalize_query(value)
        terms: list[str] = []
        for group in normalized.split(" "):
            compact = _clean_term(group)
            group_tokens = [
                _clean_term(token)
                for token in self._tokenizer.cut(group, cut_all=False)
            ]
            if (
                3 <= len(compact) <= 24
                and not any(token in _QUERY_STOP_TERMS for token in group_tokens)
            ):
                terms.append(compact)
        for token in self._tokenizer.cut(normalized, cut_all=False):
            cleaned = _clean_term(token)
            if len(cleaned) < 2 or cleaned in _QUERY_STOP_TERMS:
                continue
            terms.append(cleaned)
        return _unique(terms)[:8]

    async def _route_sources(
        self,
        session,
        requested_source_ids: list[str],
        terms: list[str],
        visible_scopes: list[str],
    ) -> tuple[list[str], list[str]]:
        visibility_bindings = {
            f"visibility_{index}": value for index, value in enumerate(visible_scopes)
        }
        visibility_clause = ", ".join(f":{key}" for key in visibility_bindings)
        requested = _unique(requested_source_ids)[:3]
        model_sources: list[str] = []
        if requested:
            source_bindings = {
                f"requested_source_{index}": value
                for index, value in enumerate(requested)
            }
            source_clause = ", ".join(f":{key}" for key in source_bindings)
            model_sources = list(
                (
                    await session.execute(
                        text(
                            f"""
                            SELECT id FROM source_definitions
                            WHERE id IN ({source_clause})
                              AND enabled = 1
                              AND visibility IN ({visibility_clause})
                            """
                        ),
                        {**visibility_bindings, **source_bindings},
                    )
                ).scalars()
            )
            model_sources.sort(key=requested.index)

        term_bindings = {
            f"route_term_{index}": term for index, term in enumerate(terms[:10])
        }
        coverage_expression = " + ".join(
            "CASE WHEN instr(lower(p.name || ' ' || p.owner || ' ' || p.titles), "
            f":{key}) > 0 THEN 1 ELSE 0 END"
            for key in term_bindings
        ) or "0"
        if not self._source_routing_available:
            return model_sources, model_sources
        try:
            rows = list(
                (
                    await session.execute(
                        text(
                            f"""
                        SELECT
                            p.source_id,
                            s.base_url,
                            ({coverage_expression}) AS coverage
                        FROM {SOURCE_SEARCH_FTS_TABLE} AS p
                        JOIN source_definitions AS s ON s.id = p.source_id
                        WHERE s.enabled = 1
                          AND s.visibility IN ({visibility_clause})
                        ORDER BY coverage DESC,
                            CASE s.authority_level WHEN 'official' THEN 0 ELSE 1 END,
                            p.source_id
                        LIMIT 12
                        """
                        ),
                        {**visibility_bindings, **term_bindings},
                    )
                ).mappings()
            )
        except SQLAlchemyError as exc:
            self._source_routing_available = False
            logger.warning(
                "campus source routing query failed; using global retrieval: %s",
                type(exc).__name__,
            )
            return model_sources, model_sources
        routed = list(model_sources)
        positive_rows = [row for row in rows if int(row["coverage"] or 0) > 0]
        for row in positive_rows:
            related = [row]
            base_url = str(row.get("base_url") or "").rstrip("/").lower()
            if base_url:
                related.extend(
                    sibling
                    for sibling in positive_rows
                    if sibling is not row
                    and str(sibling.get("base_url") or "").rstrip("/").lower()
                    == base_url
                )
            for item in related:
                source_id = str(item["source_id"])
                if source_id not in routed:
                    routed.append(source_id)
                if len(routed) >= 5:
                    return routed[:5], model_sources
        return routed[:5], model_sources

    async def _fetch_candidates(
        self,
        session,
        *,
        expression: str,
        required_short_terms: list[str],
        source_ids: list[str] | None,
        visible_scopes: list[str],
    ) -> list[dict[str, Any]]:
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
            for index, value in enumerate(required_short_terms[:4])
        }
        short_clause = "".join(
            "\n AND instr(lower(v.title || ' ' || v.normalized_text), "
            f":{key}) > 0"
            for key in short_bindings
        )
        source_bindings: dict[str, str] = {}
        source_clause = ""
        if source_ids:
            source_bindings = {
                f"route_source_{index}": value
                for index, value in enumerate(source_ids)
            }
            source_values = ", ".join(f":{key}" for key in source_bindings)
            source_clause = f"\n AND s.id IN ({source_values})"
        rows = list(
            (
                await session.execute(
                    text(
                        f"""
                        SELECT
                            v.id AS document_version_id,
                            v.title,
                            v.publisher,
                            v.normalized_text,
                            v.metadata AS document_metadata,
                            v.published_at,
                            v.observed_at,
                            v.effective_from,
                            v.effective_to,
                            r.canonical_uri,
                            r.resource_type,
                            s.id AS source_id,
                            s.authority_level,
                            bm25({DOCUMENT_FTS_TABLE}, 0.0, 5.0, 1.0) AS search_score
                        FROM {DOCUMENT_FTS_TABLE}
                        JOIN document_versions AS v
                            ON v.id = {DOCUMENT_FTS_TABLE}.document_version_id
                        JOIN source_resources AS r ON r.current_version_id = v.id
                        JOIN source_definitions AS s ON s.id = r.source_id
                        WHERE {DOCUMENT_FTS_TABLE} MATCH :match_expression
                          AND s.enabled = 1
                          AND s.visibility IN ({visibility_clause})
                          AND v.quality_status NOT IN ({quality_clause})
                          {short_clause}
                          {source_clause}
                        ORDER BY search_score ASC,
                            COALESCE(v.published_at, v.observed_at) DESC
                        LIMIT :result_limit
                        """
                    ),
                    {
                        "match_expression": expression,
                        "result_limit": CHANNEL_LIMIT,
                        **visibility_bindings,
                        **quality_bindings,
                        **short_bindings,
                        **source_bindings,
                    },
                )
            ).mappings()
        )
        return [dict(row) for row in rows]

    def _score_candidate(
        self,
        candidate: HybridCandidate,
        *,
        query_variants: list[str],
        term_sets: list[list[str]],
        model_sources: set[str],
        inferred_sources: set[str],
        is_enumeration: bool,
    ) -> None:
        row = candidate.row
        title = normalize_text(str(row["title"])).lower()
        body = normalize_text(str(row["normalized_text"])).lower()
        haystack = f"{title}\n{body}"
        coverage_values = [
            sum(term in haystack for term in terms) / max(1, len(terms))
            for terms in term_sets
            if terms
        ]
        title_values = [
            sum(term in title for term in terms) / max(1, len(terms))
            for terms in term_sets
            if terms
        ]
        candidate.coverage = max(coverage_values, default=0.0)
        candidate.title_coverage = max(title_values, default=0.0)
        focus_terms = _unique(terms[-1] for terms in term_sets if terms)
        candidate.scope_listing = _looks_like_scope_listing(
            body,
            title=title,
            focus_terms=focus_terms,
        )
        source_id = str(row["source_id"])
        metadata = _metadata(row.get("document_metadata"))
        resource_type = str(row.get("resource_type") or "")
        score = candidate.rrf_score
        score += 0.030 * candidate.coverage
        score += 0.018 * candidate.title_coverage
        if source_id in model_sources:
            score += 0.032
        elif source_id in inferred_sources:
            score += 0.012
        if str(row.get("authority_level")) == "official":
            score += 0.012
        elif str(row.get("authority_level")) == "official_secondary":
            score += 0.006
        if resource_type in {"html", "live_html"}:
            score += 0.005
            if len(body) < max(180, len(title) * 3):
                score -= 0.015
        score += _freshness_bonus(row.get("published_at"))
        if is_enumeration and candidate.scope_listing and candidate.coverage >= 0.75:
            score += 0.080
        if is_enumeration and any(term in title for term in focus_terms):
            score += 0.022
        if any(_normalize_query(value) in title for value in query_variants):
            score += 0.018
        if metadata.get("article_image") is True:
            score -= 0.035
        if resource_type == "image" and _GENERIC_FILENAME_PATTERN.match(title):
            score -= 0.035
        candidate.final_score = score

    async def _best_chunks(
        self,
        session,
        version_ids: list[str],
        terms: list[str],
    ) -> dict[str, str]:
        if not version_ids:
            return {}
        bindings = {f"version_{index}": value for index, value in enumerate(version_ids)}
        clause = ", ".join(f":{key}" for key in bindings)
        rows = list(
            (
                await session.execute(
                    text(
                        f"""
                        SELECT document_version_id, ordinal, heading, content
                        FROM document_chunks
                        WHERE document_version_id IN ({clause})
                        ORDER BY document_version_id, ordinal
                        """
                    ),
                    bindings,
                )
            ).mappings()
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row["document_version_id"])].append(dict(row))
        selected: dict[str, str] = {}
        for version_id, chunks in grouped.items():
            best = max(
                chunks,
                key=lambda row: _passage_relevance(
                    f"{row.get('heading') or ''}\n{row['content']}", terms
                ),
            )
            selected[version_id] = str(best["content"])
        return selected


def _append_channel(
    channels: list[
        tuple[str, str, list[str], list[str] | None, float, set[int]]
    ],
    seen: dict[tuple[str, str, tuple[str, ...]], int],
    kind: str,
    expression: str,
    short_terms: list[str],
    source_ids: list[str] | None,
    weight: float,
    variant_index: int | None,
) -> None:
    key = (expression, "|".join(short_terms), tuple(source_ids or ()))
    if key in seen:
        if variant_index is not None:
            channels[seen[key]][5].add(variant_index)
        return
    seen[key] = len(channels)
    channels.append(
        (
            kind,
            expression,
            short_terms,
            source_ids,
            weight,
            {variant_index} if variant_index is not None else set(),
        )
    )


def _unique_queries(query: str, variants: list[str]) -> list[str]:
    return _unique(
        _normalize_query(value)
        for value in (query, *variants[:3])
        if value and _normalize_query(value)
    )


def _normalize_query(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).replace("\u200b", "")
    normalized = re.sub(r"[\"'“”‘’]", "", normalized)
    normalized = re.sub(r"[—–－]", "-", normalized)
    return re.sub(r"\s+", " ", normalized).strip().lower()


def _clean_term(value: str) -> str:
    return re.sub(r"[^\u3400-\u9fffa-z0-9._+-]", "", value.lower())


def _fts_eligible(value: str) -> bool:
    return len(re.sub(r"[^\u3400-\u9fffa-z0-9]", "", value)) >= 3


def _fts_expression(terms: list[str], *, operator: str = "AND") -> str | None:
    values = _unique(term for term in terms if _fts_eligible(term))
    if not values:
        return None
    return f" {operator} ".join(_quote_fts(value) for value in values[:8])


def _quote_fts(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _unique(values) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = str(value).strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _looks_like_scope_listing(
    body: str,
    *,
    title: str,
    focus_terms: list[str],
) -> bool:
    if not body or len(body) > 24_000:
        return False
    if focus_terms and not any(term in title for term in focus_terms):
        return False
    lines = [normalize_text(line) for line in body.splitlines() if normalize_text(line)]
    if len(lines) < 8:
        return False
    substantive_lines = {
        line
        for line in lines
        if 2 <= len(line) <= 80
        and not _LISTING_BOILERPLATE_PATTERN.match(line)
        and not _GENERIC_FILENAME_PATTERN.match(line)
    }
    if len(substantive_lines) < 5:
        return False
    short_lines = sum(2 <= len(line) <= 40 for line in lines)
    sentence_marks = sum(body.count(mark) for mark in "。！？；")
    return short_lines / len(lines) >= 0.55 and sentence_marks <= max(8, len(lines) // 3)


def _cluster_parent_assets(candidates: list[HybridCandidate]) -> list[HybridCandidate]:
    clusters: dict[str, HybridCandidate] = {}
    for candidate in sorted(candidates, key=lambda item: item.final_score, reverse=True):
        metadata = _metadata(candidate.row.get("document_metadata"))
        parent_uri = str(metadata.get("parent_uri") or "").strip()
        is_article_image = metadata.get("article_image") is True
        key = parent_uri if is_article_image and parent_uri else str(candidate.row["canonical_uri"])
        existing = clusters.get(key)
        if existing is None:
            clusters[key] = candidate
            continue
        existing.rrf_score += candidate.rrf_score * 0.5
        existing.final_score += candidate.rrf_score * 0.5
        existing.channels.update(candidate.channels)
    return list(clusters.values())


def _diversified(
    candidates: list[HybridCandidate],
    limit: int,
    *,
    variant_count: int,
) -> list[HybridCandidate]:
    selected: list[HybridCandidate] = []
    deferred: list[HybridCandidate] = []
    counts: Counter[str] = Counter()
    selected_urls: set[str] = set()

    variant_seeds: list[HybridCandidate] = []
    for variant_index in range(variant_count if variant_count > 1 else 0):
        eligible = [
            candidate
            for candidate in candidates
            if candidate.variant_scores.get(variant_index, 0.0) > 0
        ]
        if not eligible:
            continue
        best = max(
            eligible,
            key=lambda candidate: (
                candidate.variant_scores.get(variant_index, 0.0)
                + 0.030 * candidate.title_coverage
                + 0.250 * candidate.final_score,
                candidate.final_score,
            ),
        )
        url = str(best.row["canonical_uri"])
        if url not in {str(item.row["canonical_uri"]) for item in variant_seeds}:
            variant_seeds.append(best)

    ordered = [
        *variant_seeds,
        *(
            candidate
            for candidate in candidates
            if str(candidate.row["canonical_uri"])
            not in {str(item.row["canonical_uri"]) for item in variant_seeds}
        ),
    ]
    for candidate in ordered:
        source_id = str(candidate.row["source_id"])
        if counts[source_id] >= 3:
            deferred.append(candidate)
            continue
        selected.append(candidate)
        selected_urls.add(str(candidate.row["canonical_uri"]))
        counts[source_id] += 1
        if len(selected) >= limit:
            return selected
    for candidate in deferred:
        if str(candidate.row["canonical_uri"]) in selected_urls:
            continue
        selected.append(candidate)
        if len(selected) >= limit:
            break
    return selected


def _passage_relevance(value: str, terms: list[str]) -> tuple[int, int, int]:
    lowered = value.lower()
    present = [term for term in terms if term in lowered]
    return len(present), sum(lowered.count(term) for term in present), -len(value)


def _excerpt(value: str, terms: list[str], width: int = 1400) -> str:
    normalized = normalize_text(value)
    if len(normalized) <= width:
        return normalized
    lowered = normalized.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, min(positions, default=0) - 220)
    excerpt = normalized[start : start + width]
    if start:
        excerpt = f"…{excerpt}"
    if start + width < len(normalized):
        excerpt = f"{excerpt}…"
    return excerpt


def _candidate_evidence(
    candidate: HybridCandidate,
    chunks: dict[str, str],
    terms: list[str],
) -> Evidence:
    row = candidate.row
    version_id = str(row["document_version_id"])
    excerpt_source = chunks.get(version_id) or str(row["normalized_text"])
    return Evidence(
        evidence_id=new_id("ev"),
        title=str(row["title"]),
        publisher=str(row["publisher"]),
        canonical_url=str(row["canonical_uri"]),
        published_at=_aware(row.get("published_at")),
        observed_at=_aware(row.get("observed_at")) or datetime.now(UTC),
        fresh_until=None,
        excerpt=_excerpt(excerpt_source, terms),
        source_id=str(row["source_id"]),
        resource_ref=f"campus-memory:{version_id}",
        document_version_id=version_id,
        authority_level=str(row["authority_level"]),
        effective_from=_aware(row.get("effective_from")),
        effective_to=_aware(row.get("effective_to")),
        retrieval_mode="memory",
    )


def _aware(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _freshness_bonus(value: datetime | str | None) -> float:
    published_at = _aware(value)
    if published_at is None:
        return 0.0
    age_days = max(0, (datetime.now(UTC) - published_at).days)
    if age_days <= 180:
        return 0.025
    if age_days <= 400:
        return 0.018
    if age_days <= 800:
        return 0.008
    return 0.0
