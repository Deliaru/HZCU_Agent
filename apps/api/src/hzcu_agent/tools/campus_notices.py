import re
from datetime import timedelta

from pydantic import BaseModel, Field, field_validator, model_validator

from hzcu_agent.auth.campus_access import CampusAccessBroker, CampusAccessError
from hzcu_agent.ingestion.catalog import SourceRegistry
from hzcu_agent.ingestion.service import IngestionService
from hzcu_agent.models import new_id, utc_now
from hzcu_agent.schemas import Evidence, ToolError, ToolResult
from hzcu_agent.services.image_reader import CampusImageReader
from hzcu_agent.tools.campus_memory import (
    CampusMemorySearchArguments,
    CampusMemorySearchTool,
)


class CampusNoticeSearchArguments(BaseModel):
    query: str | None = Field(default=None, min_length=1, max_length=200)
    queries: list[str] = Field(default_factory=list, max_length=4)
    limit: int = Field(default=6, ge=1, le=10)

    @field_validator("queries")
    @classmethod
    def _check_query_lengths(cls, value: list[str]) -> list[str]:
        for item in value:
            if not 1 <= len(item) <= 200:
                raise ValueError("每条查询必须是 1—200 字符")
        return value

    @model_validator(mode="after")
    def _require_a_query(self) -> "CampusNoticeSearchArguments":
        if not self.resolved_queries():
            raise ValueError("必须提供 query 或 queries")
        return self

    def resolved_queries(self) -> list[str]:
        """Ordered, de-duplicated batch; single-query plans stay valid."""

        candidates = [*([self.query] if self.query else []), *self.queries]
        return list(dict.fromkeys(item for item in candidates if item))[:4]


class CampusNoticeSearchTool:
    """Live, read-only notice lookup over direct campus routing or VPN sidecar."""

    name = "search_campus_notices_live"
    version = "1.0.0"

    def __init__(
        self,
        *,
        access: CampusAccessBroker,
        registry: SourceRegistry,
        ingestion: IngestionService,
        memory: CampusMemorySearchTool,
        image_reader: CampusImageReader,
    ) -> None:
        self._access = access
        self._registry = registry
        self._ingestion = ingestion
        self._memory = memory
        self._image_reader = image_reader

    @property
    def enabled(self) -> bool:
        return self._access.enabled

    async def close(self) -> None:
        await self._image_reader.close()

    async def run(
        self,
        arguments: CampusNoticeSearchArguments,
        trace_id: str,
        *,
        actor_user_id: str | None,
        allowed_visibilities: frozenset[str],
    ) -> ToolResult:
        access = await self._access.status(actor_user_id)
        if access.mode == "direct":
            return await self._run_direct(
                arguments,
                trace_id,
                allowed_visibilities=allowed_visibilities,
            )
        if access.mode == "vpn":
            if actor_user_id is None or "campus" not in allowed_visibilities:
                return ToolResult(
                    tool=self.name,
                    version=self.version,
                    status="error",
                    error=ToolError(
                        code="CAMPUS_IDENTITY_REQUIRED",
                        message="校外校园通知实时查询需要先验证校园身份。",
                        retryable=False,
                    ),
                    trace_id=trace_id,
                )
            return await self._run_vpn(
                arguments,
                trace_id,
                actor_user_id=actor_user_id,
            )
        if actor_user_id is None:
            return ToolResult(
                tool=self.name,
                version=self.version,
                status="error",
                error=ToolError(
                    code="CAMPUS_IDENTITY_REQUIRED",
                    message="当前网络无法直连校园站点；校外实时查询需要先验证校园身份。",
                    retryable=False,
                ),
                trace_id=trace_id,
            )
        return ToolResult(
            tool=self.name,
            version=self.version,
            status="error",
            error=ToolError(
                code="CAMPUS_QUERY_ROUTE_UNAVAILABLE",
                message="服务节点当前不在校园网内，也没有有效的校外只读查询会话。",
                retryable=True,
            ),
            trace_id=trace_id,
        )

    async def _run_direct(
        self,
        arguments: CampusNoticeSearchArguments,
        trace_id: str,
        *,
        allowed_visibilities: frozenset[str],
    ) -> ToolResult:
        queries = arguments.resolved_queries()
        ranked_sources = _rank_direct_sources(
            [
                source
                for source in self._registry.sources
                if (
                    source.enabled
                    and source.visibility in allowed_visibilities
                    and source.connector.kind == "linked_html"
                )
            ],
            queries,
        )
        batch_limit = min(arguments.limit * len(queries), 16)
        warnings: list[str] = []
        attempted_source_ids: list[str] = []
        result: ToolResult | None = None
        wave_size = 6
        for offset in range(0, len(ranked_sources), wave_size):
            wave = ranked_sources[offset : offset + wave_size]
            attempted_source_ids.extend(source.id for source in wave)
            for source in wave:
                outcome = await self._ingestion.sync_source(
                    source.id,
                    limit_override=max(arguments.limit * 2, 10),
                )
                if outcome.status != "completed":
                    warnings.append(f"{source.id} 本轮实时同步不完整，已保留旧版本作为补充。")

            query_results = [
                await self._memory.run(
                    CampusMemorySearchArguments(
                        query=query,
                        top_k=min(arguments.limit, 12),
                    ),
                    trace_id,
                    allowed_visibilities=allowed_visibilities,
                )
                for query in queries
            ]
            result = _merge_memory_results(
                query_results,
                trace_id=trace_id,
                limit=batch_limit,
            )
            if len(result.evidence) >= batch_limit:
                break
        if result is None:
            result = ToolResult(tool=self.name, status="ok", trace_id=trace_id)
        return ToolResult(
            tool=self.name,
            version=self.version,
            status=result.status,
            data={
                **result.data,
                "access_route": "direct",
                "capability": "campus_notice.read",
                "search_trace": {
                    "attempted_source_ids": attempted_source_ids,
                    "waves": ((len(attempted_source_ids) + wave_size - 1) // wave_size),
                    "exhausted": len(attempted_source_ids) == len(ranked_sources),
                    "queries": queries,
                },
            },
            evidence=result.evidence,
            warnings=[*warnings, *result.warnings],
            error=result.error,
            trace_id=trace_id,
        )

    async def _run_vpn(
        self,
        arguments: CampusNoticeSearchArguments,
        trace_id: str,
        *,
        actor_user_id: str,
    ) -> ToolResult:
        queries = arguments.resolved_queries()
        try:
            outcome = await self._access.query_vpn(
                user_id=actor_user_id,
                queries=queries,
                limit=arguments.limit,
            )
        except CampusAccessError as exc:
            return ToolResult(
                tool=self.name,
                version=self.version,
                status="error",
                error=ToolError(
                    code=exc.code,
                    message=str(exc),
                    retryable=exc.retryable,
                ),
                trace_id=trace_id,
            )

        image_text, image_warnings = await self._read_relevant_images(
            outcome,
            queries=queries,
        )
        now = utc_now()
        evidence = [
            Evidence(
                evidence_id=new_id("ev"),
                title=item.title,
                publisher=item.publisher,
                canonical_url=item.canonical_url,
                excerpt=(
                    item.excerpt
                    + (
                        "\n\n[官方页面图片文字]\n" + image_text[index]
                        if image_text.get(index)
                        else ""
                    )
                )[:7000],
                source_id=item.source_id,
                published_at=item.published_at,
                observed_at=item.observed_at,
                fresh_until=now + timedelta(minutes=30),
                resource_ref=None,
                authority_level="official",
                retrieval_mode="live_authenticated",
            )
            for index, item in enumerate(outcome.evidence)
        ]
        if evidence:
            warnings: list[str] = list(image_warnings)
        elif outcome.exhausted:
            warnings = [
                "已遍历当前登记来源入口，但未形成可用证据；这不代表学校没有相关"
                "信息，仍可能受站点导航深度或页面解析影响。"
            ]
        else:
            warnings = ["实时检索未完成全部来源，当前不能据此判断学校没有相关信息。"]
        return ToolResult(
            tool=self.name,
            version=self.version,
            status="ok",
            data={
                "queries": queries,
                "result_count": len(evidence),
                "access_route": "vpn",
                "capability": "campus_notice.read",
                "search_trace": {
                    "attempted_source_ids": list(outcome.attempted_source_ids),
                    "waves": outcome.waves,
                    "exhausted": outcome.exhausted,
                    "candidate_count": outcome.candidate_count,
                    "hydrated_candidate_count": outcome.hydrated_candidate_count,
                    "per_query_result_counts": dict(outcome.per_query_result_counts),
                },
            },
            evidence=evidence,
            warnings=warnings,
            trace_id=trace_id,
        )

    async def _read_relevant_images(
        self,
        outcome,
        *,
        queries: list[str],
    ) -> tuple[dict[int, str], list[str]]:
        tokens = _query_tokens(" ".join(queries))
        candidates = [
            (
                _image_title_score(image.title, tokens),
                evidence_index,
                image,
            )
            for evidence_index, item in enumerate(outcome.evidence)
            for image in item.images
        ]
        candidates.sort(key=lambda item: item[0], reverse=True)
        selected = [
            item
            for item in candidates
            if _should_read_image(
                item[0],
                outcome.evidence[item[1]].excerpt,
            )
        ][:4]
        if not selected:
            return {}, []
        try:
            readings = await self._image_reader.read(
                [item[2] for item in selected],
                query="；".join(queries),
            )
        except Exception:
            return {}, ["校园页面图片读取失败，文本证据仍可正常使用。"]
        by_evidence: dict[int, list[str]] = {}
        for (_, evidence_index, image), reading in zip(selected, readings, strict=False):
            if not reading:
                continue
            by_evidence.setdefault(evidence_index, []).append(f"{image.title}：{reading}")
        return (
            {evidence_index: "\n".join(values) for evidence_index, values in by_evidence.items()},
            [],
        )


def _merge_memory_results(
    results: list[ToolResult],
    *,
    trace_id: str,
    limit: int,
) -> ToolResult:
    evidence: list[Evidence] = []
    warnings: list[str] = []
    seen_urls: set[str] = set()
    per_query_result_counts: dict[str, int] = {}
    for result in results:
        query = str(result.data.get("query") or "")
        per_query_result_counts[query] = len(result.evidence)
        warnings.extend(result.warnings)
        for item in result.evidence:
            if item.canonical_url in seen_urls:
                continue
            seen_urls.add(item.canonical_url)
            evidence.append(item)
            if len(evidence) >= limit:
                break
        if len(evidence) >= limit:
            break
    successful = [result for result in results if result.status == "ok"]
    first_error = next((result.error for result in results if result.error), None)
    return ToolResult(
        tool="search_campus_memory",
        version=CampusMemorySearchTool.version,
        status="ok" if successful else "error",
        data={
            "queries": list(per_query_result_counts),
            "result_count": len(evidence),
            "per_query_result_counts": per_query_result_counts,
            "retrieval": "independent sqlite-fts5 queries",
        },
        evidence=evidence,
        warnings=list(dict.fromkeys(warnings)),
        error=None if successful else first_error,
        trace_id=trace_id,
    )


def _rank_direct_sources(sources: list, queries: list[str]) -> list:
    """Order sources by their best score across the batch, not the merged text."""

    token_sets = [_query_tokens(query) for query in queries]

    def score(indexed):
        index, source = indexed
        haystack = f"{source.name} {' '.join(source.live_required_for)}".casefold()
        value = max(
            (sum(16 for token in tokens if token in haystack) for tokens in token_sets),
            default=0,
        )
        return value, -index

    return [source for _, source in sorted(enumerate(sources), key=score, reverse=True)]


def _query_tokens(value: str) -> set[str]:
    normalized = re.sub(r"\s+", " ", value).strip().casefold()
    tokens = set(re.findall(r"[a-z0-9]{2,}|[\u3400-\u9fff]{2,8}", normalized))
    for chunk in re.findall(r"[\u3400-\u9fff]{4,}", normalized):
        tokens.update(chunk[index : index + 2] for index in range(len(chunk) - 1))
    return tokens


def _image_title_score(title: str, tokens: set[str]) -> float:
    normalized = re.sub(r"\s+", "", title).casefold()
    return sum(min(8.0, float(len(token))) for token in tokens if token in normalized)


def _should_read_image(title_score: float, excerpt: str) -> bool:
    """OCR only query-signaled images or pages whose text extraction is sparse."""

    return title_score > 0 or len(excerpt.strip()) < 160
