import asyncio
import logging
from collections import Counter
from dataclasses import dataclass

import httpx
from sqlalchemy import select

from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.ingestion.catalog import SourceConfig, SourceRegistry
from hzcu_agent.ingestion.connectors import SafeSourceFetcher, build_connector
from hzcu_agent.ingestion.indexing import DocumentIndexer
from hzcu_agent.ingestion.parsers import (
    content_hash,
    expected_parser_version,
    parse_document,
)
from hzcu_agent.ingestion.search_index import refresh_source_search_profile
from hzcu_agent.ingestion.security import (
    SourceUrlRejected,
    canonicalize_source_url,
)
from hzcu_agent.ingestion.snapshot import SnapshotStore
from hzcu_agent.ingestion.types import DiscoveredResource, FetchPayload, ParsedDocument
from hzcu_agent.models import (
    DocumentVersion,
    SourceResource,
    SyncRun,
    new_id,
    utc_now,
)
from hzcu_agent.schemas import Evidence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SyncOutcome:
    run_id: str
    source_id: str
    status: str
    discovered_count: int
    fetched_count: int
    created_count: int
    unchanged_count: int
    failed_count: int
    error_code: str | None = None


class _StaticPayloadConnector:
    def __init__(self, payload: FetchPayload) -> None:
        self._payload = payload

    async def fetch(
        self,
        source: SourceConfig,
        resource: DiscoveredResource,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchPayload:
        del source, resource, etag, last_modified
        return self._payload


class IngestionService:
    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        registry: SourceRegistry,
        client: httpx.AsyncClient | None = None,
        indexer: DocumentIndexer | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._registry = registry
        self._fetcher = SafeSourceFetcher(
            max_download_bytes=settings.max_download_bytes,
            client=client,
        )
        self._snapshots = SnapshotStore(settings.resolved_snapshot_directory)
        self._indexer = indexer or DocumentIndexer()

    async def close(self) -> None:
        await self._fetcher.close()

    def source_summaries(
        self,
        allowed_visibilities: frozenset[str],
    ) -> list[dict[str, str]]:
        return [
            {
                "id": source.id,
                "name": source.name,
                "owner_department": source.owner_department,
                "authority_level": source.authority_level,
            }
            for source in self._registry.sources
            if source.enabled and source.visibility in allowed_visibilities
        ]

    async def sync_all(
        self,
        limit_override: int | None = None,
        *,
        full_scan: bool = False,
    ) -> list[SyncOutcome]:
        outcomes: list[SyncOutcome] = []
        for source in self._registry.sources:
            if source.enabled and source.visibility in self._settings.ingestion_visibility_set:
                outcomes.append(
                    await self.sync_source(
                        source.id,
                        limit_override=limit_override,
                        full_scan=full_scan,
                    )
                )
        return outcomes

    async def sync_source(
        self,
        source_id: str,
        *,
        limit_override: int | None = None,
        full_scan: bool = False,
    ) -> SyncOutcome:
        source = self._registry.require(source_id)
        run = SyncRun(
            id=new_id("sync"),
            source_id=source.id,
            status="running",
            started_at=utc_now(),
            cursor={},
        )
        async with self._database.session_factory() as session:
            session.add(run)
            await session.commit()

        connector = build_connector(source, self._fetcher)
        effective_full_scan = full_scan or source.connector.full_scan_by_default
        try:
            resources = await connector.discover(
                source,
                limit_override,
                full_scan=effective_full_scan,
            )
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish_run(
                    run.id,
                    source.id,
                    Counter(),
                    discovered_count=0,
                    status="interrupted",
                    error_code="SYNC_CANCELLED",
                )
            )
            raise
        except Exception:
            logger.exception(
                "Source discovery failed",
                extra={
                    "event": "source.discovery.failed",
                    "source_id": source.id,
                    "run_id": run.id,
                },
            )
            return await self._finish_run(
                run.id,
                source.id,
                Counter(),
                discovered_count=0,
                status="failed",
                error_code="SOURCE_DISCOVERY_FAILED",
            )

        semaphore = asyncio.Semaphore(self._settings.effective_sync_max_concurrency)

        async def guarded(resource: DiscoveredResource) -> str:
            async with semaphore:
                try:
                    return await self._process_resource(source, connector, resource)
                except Exception:
                    logger.exception(
                        "Source resource ingestion failed",
                        extra={
                            "event": "source.resource.failed",
                            "source_id": source.id,
                            "run_id": run.id,
                            "canonical_uri": resource.canonical_uri,
                        },
                    )
                    return "failed"

        try:
            results = await asyncio.gather(*(guarded(resource) for resource in resources))
        except asyncio.CancelledError:
            await asyncio.shield(
                self._finish_run(
                    run.id,
                    source.id,
                    Counter(),
                    discovered_count=len(resources),
                    status="interrupted",
                    error_code="SYNC_CANCELLED",
                )
            )
            raise
        counts = Counter(results)
        status = "completed_with_errors" if counts["failed"] else "completed"
        outcome = await self._finish_run(
            run.id,
            source.id,
            counts,
            discovered_count=len(resources),
            status=status,
        )
        await self._refresh_search_profile(source.id)
        return outcome

    async def ingest_live_evidence(self, evidence: Evidence) -> Evidence:
        source = self._registry.get(evidence.source_id) if evidence.source_id else None
        if source is None:
            source = self._registry.match_url(evidence.canonical_url)
        if (
            source is None
            or source.visibility not in {"public", "campus"}
            or not self._registry.accepts_detail_url(
                source.id,
                evidence.canonical_url,
            )
        ):
            return evidence
        try:
            canonical_uri = canonicalize_source_url(evidence.canonical_url, source)
        except SourceUrlRejected:
            return evidence

        now = utc_now()
        document = ParsedDocument(
            title=evidence.title[:500],
            publisher=evidence.publisher[:200],
            normalized_text=evidence.excerpt,
            media_type="text/plain+live-excerpt",
            published_at=evidence.published_at,
            effective_from=None,
            effective_to=None,
            parser_version="live-search-backfill-v1",
            quality_status="partial_live",
            metadata={
                "capture": "live_search_excerpt",
                "original_source_id": evidence.source_id,
            },
        )
        digest = content_hash(document)
        async with self._database.session_factory() as session:
            resource = await session.scalar(
                select(SourceResource).where(
                    SourceResource.source_id == source.id,
                    SourceResource.canonical_uri == canonical_uri,
                )
            )
            if resource is None:
                resource = SourceResource(
                    id=new_id("res"),
                    source_id=source.id,
                    canonical_uri=canonical_uri,
                    resource_type="live_html",
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(resource)
                await session.flush()
            else:
                resource.last_seen_at = now

            version = await session.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.resource_id == resource.id,
                    DocumentVersion.content_hash == digest,
                )
            )
            if version is None:
                version = DocumentVersion(
                    id=new_id("docv"),
                    resource_id=resource.id,
                    content_hash=digest,
                    raw_snapshot_uri=None,
                    media_type=document.media_type,
                    normalized_text=document.normalized_text,
                    title=document.title,
                    publisher=document.publisher,
                    published_at=document.published_at,
                    effective_from=None,
                    effective_to=None,
                    observed_at=now,
                    parser_version=document.parser_version,
                    quality_status=document.quality_status,
                    document_metadata=document.metadata,
                )
                session.add(version)
                await session.flush()

            await self._indexer.ensure_version_index(session, version)
            current_version = (
                await session.get(DocumentVersion, resource.current_version_id)
                if resource.current_version_id
                else None
            )
            if current_version is None or current_version.quality_status == "partial_live":
                resource.current_version_id = version.id
            await session.commit()
            evidence.document_version_id = version.id
        await self._refresh_search_profile(source.id)
        return evidence

    async def _refresh_search_profile(self, source_id: str) -> None:
        try:
            await refresh_source_search_profile(self._database, source_id)
        except Exception:
            # The source index is disposable. A failed refresh must never turn
            # a successful mirror sync into a source failure; API startup can
            # rebuild it from the current-version ledger.
            logger.exception(
                "Source search profile refresh failed",
                extra={
                    "event": "source.search_profile.failed",
                    "source_id": source_id,
                },
            )

    async def ingest_payload(
        self,
        source_id: str,
        discovered: DiscoveredResource,
        payload: FetchPayload,
    ) -> str:
        """Import one payload captured by an approved alternate transport."""

        source = self._registry.require(source_id)
        canonical = canonicalize_source_url(discovered.canonical_uri, source)
        if canonical != discovered.canonical_uri:
            raise SourceUrlRejected("Staged resource canonical URI is not normalized")
        if payload.status_code >= 400:
            raise ValueError("Staged payload is not a successful read")
        return await self._process_resource(
            source,
            _StaticPayloadConnector(payload),
            discovered,
        )

    async def _process_resource(
        self,
        source: SourceConfig,
        connector,
        discovered: DiscoveredResource,
    ) -> str:
        now = utc_now()
        async with self._database.session_factory() as session:
            resource = await session.scalar(
                select(SourceResource).where(
                    SourceResource.source_id == source.id,
                    SourceResource.canonical_uri == discovered.canonical_uri,
                )
            )
            if resource is None:
                resource = SourceResource(
                    id=new_id("res"),
                    source_id=source.id,
                    canonical_uri=discovered.canonical_uri,
                    external_id=discovered.external_id,
                    resource_type=discovered.resource_type,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                session.add(resource)
                await session.commit()
            else:
                resource.last_seen_at = now
                if discovered.external_id:
                    resource.external_id = discovered.external_id
                if resource.resource_type != discovered.resource_type:
                    resource.resource_type = discovered.resource_type
                await session.commit()
            resource_id = resource.id
            etag = resource.etag
            last_modified = resource.last_modified
            current_version = (
                await session.get(DocumentVersion, resource.current_version_id)
                if resource.current_version_id
                else None
            )
            if (
                current_version is not None
                and current_version.parser_version
                != expected_parser_version(source, resource.resource_type)
            ):
                etag = None
                last_modified = None

        payload = await connector.fetch(
            source,
            discovered,
            etag=etag,
            last_modified=last_modified,
        )
        if payload.status_code == 304:
            async with self._database.session_factory() as session:
                resource = await session.get(SourceResource, resource_id)
                if resource:
                    resource.last_seen_at = now
                    if resource.current_version_id:
                        current = await session.get(
                            DocumentVersion,
                            resource.current_version_id,
                        )
                        if current is not None:
                            await self._indexer.ensure_version_index(session, current)
                    await session.commit()
            return "unchanged"

        document = parse_document(source, discovered, payload)
        digest = content_hash(document)
        async with self._database.session_factory() as session:
            existing = await session.scalar(
                select(DocumentVersion).where(
                    DocumentVersion.resource_id == resource_id,
                    DocumentVersion.content_hash == digest,
                )
            )
            resource = await session.get(SourceResource, resource_id)
            if resource is None:
                raise RuntimeError("Source resource disappeared during ingestion")
            resource.last_seen_at = now
            resource.etag = payload.etag
            resource.last_modified = payload.last_modified
            if existing is not None:
                await self._indexer.ensure_version_index(session, existing)
                resource.current_version_id = existing.id
                await session.commit()
                return "unchanged"

            snapshot_uri = await self._snapshots.put(payload.body)
            version = DocumentVersion(
                id=new_id("docv"),
                resource_id=resource.id,
                content_hash=digest,
                raw_snapshot_uri=snapshot_uri,
                media_type=document.media_type,
                normalized_text=document.normalized_text,
                title=document.title,
                publisher=document.publisher,
                published_at=document.published_at,
                effective_from=document.effective_from,
                effective_to=document.effective_to,
                observed_at=now,
                parser_version=document.parser_version,
                quality_status=document.quality_status,
                document_metadata=document.metadata,
            )
            session.add(version)
            await session.flush()
            await self._indexer.ensure_version_index(session, version)
            resource.current_version_id = version.id
            await session.commit()
        return "created"

    async def _finish_run(
        self,
        run_id: str,
        source_id: str,
        counts: Counter,
        *,
        discovered_count: int,
        status: str,
        error_code: str | None = None,
    ) -> SyncOutcome:
        finished_at = utc_now()
        async with self._database.session_factory() as session:
            run = await session.get(SyncRun, run_id)
            if run is None:
                raise RuntimeError("Sync run disappeared")
            run.status = status
            run.finished_at = finished_at
            run.discovered_count = discovered_count
            run.fetched_count = counts["created"] + counts["unchanged"]
            run.created_count = counts["created"]
            run.unchanged_count = counts["unchanged"]
            run.failed_count = counts["failed"]
            run.error_code = error_code
            await session.commit()
        return SyncOutcome(
            run_id=run_id,
            source_id=source_id,
            status=status,
            discovered_count=discovered_count,
            fetched_count=counts["created"] + counts["unchanged"],
            created_count=counts["created"],
            unchanged_count=counts["unchanged"],
            failed_count=counts["failed"],
            error_code=error_code,
        )
