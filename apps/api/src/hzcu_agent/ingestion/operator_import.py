from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath
from typing import Any

from sqlalchemy import select

from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.ingestion.catalog import SourceRegistry
from hzcu_agent.ingestion.indexing import DocumentIndexer
from hzcu_agent.ingestion.parsers import content_hash, normalize_text
from hzcu_agent.ingestion.snapshot import SnapshotStore
from hzcu_agent.ingestion.types import ParsedDocument
from hzcu_agent.models import (
    DocumentVersion,
    SourceResource,
    SyncRun,
    new_id,
    utc_now,
)


@dataclass(frozen=True)
class OperatorImportOutcome:
    status: str
    source_id: str
    resource_id: str
    document_version_id: str
    canonical_uri: str
    chunks: int
    entities: int


class OperatorDocumentImporter:
    """Persist a verified pre-parsed artifact without inventing a fetch URL."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        registry: SourceRegistry,
        indexer: DocumentIndexer | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._registry = registry
        self._snapshots = SnapshotStore(settings.resolved_snapshot_directory)
        self._indexer = indexer or DocumentIndexer()

    async def import_document(
        self,
        *,
        source_id: str,
        original: bytes,
        original_filename: str,
        normalized_text: str,
        title: str,
        publisher: str,
        media_type: str,
        published_at: datetime | None,
        effective_from: datetime | None,
        effective_to: datetime | None,
        parser_version: str,
        metadata: dict[str, Any] | None = None,
    ) -> OperatorImportOutcome:
        source = self._registry.require(source_id)
        if source.connector.kind != "operator_import":
            raise ValueError("Source does not permit operator-import artifacts")
        if not original:
            raise ValueError("Original artifact is empty")
        if len(original) > self._settings.max_download_bytes:
            raise ValueError("Original artifact exceeds the configured size limit")

        text = normalize_text(normalized_text)
        clean_title = normalize_text(title)[:500]
        clean_publisher = normalize_text(publisher)[:200]
        if not text or not clean_title or not clean_publisher:
            raise ValueError("Imported title, publisher and normalized text must be non-empty")
        if len(parser_version) > 80:
            raise ValueError("Parser version is too long")

        filename = PurePath(original_filename).name[:255] or "document"
        raw_sha256 = hashlib.sha256(original).hexdigest()
        document_metadata = {
            **(metadata or {}),
            "ingestion_method": "operator_verified_document",
            "original_filename": filename,
            "raw_sha256": raw_sha256,
        }
        document = ParsedDocument(
            title=clean_title,
            publisher=clean_publisher,
            normalized_text=text,
            media_type=media_type,
            published_at=published_at,
            effective_from=effective_from,
            effective_to=effective_to,
            parser_version=parser_version,
            quality_status="accepted",
            metadata=document_metadata,
        )
        digest = content_hash(document)
        snapshot_uri = await self._snapshots.put(original)
        now = utc_now()

        async with self._database.session_factory() as session:
            resource = await session.scalar(
                select(SourceResource).where(
                    SourceResource.source_id == source.id,
                    SourceResource.external_id == raw_sha256,
                )
            )
            if resource is None:
                resource_id = new_id("res")
                canonical_uri = (
                    f"{self._settings.api_prefix}/sources/{source.id}/resources/"
                    f"{resource_id}/original"
                )
                resource = SourceResource(
                    id=resource_id,
                    source_id=source.id,
                    canonical_uri=canonical_uri,
                    external_id=raw_sha256,
                    resource_type=_resource_type(filename, media_type),
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
            created = version is None
            if version is None:
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
            elif version.raw_snapshot_uri is None:
                version.raw_snapshot_uri = snapshot_uri

            index_outcome = await self._indexer.ensure_version_index(session, version)
            resource.current_version_id = version.id
            session.add(
                SyncRun(
                    id=new_id("sync"),
                    source_id=source.id,
                    status="completed",
                    started_at=now,
                    finished_at=utc_now(),
                    discovered_count=1,
                    fetched_count=1,
                    created_count=1 if created else 0,
                    unchanged_count=0 if created else 1,
                    failed_count=0,
                    cursor={
                        "mode": "operator_import",
                        "resource_id": resource.id,
                        "raw_sha256": raw_sha256,
                    },
                )
            )
            await session.commit()
            return OperatorImportOutcome(
                status="created" if created else "unchanged",
                source_id=source.id,
                resource_id=resource.id,
                document_version_id=version.id,
                canonical_uri=resource.canonical_uri,
                chunks=index_outcome.chunks,
                entities=index_outcome.entities,
            )


def _resource_type(filename: str, media_type: str) -> str:
    if media_type.casefold() == "application/pdf" or filename.casefold().endswith(".pdf"):
        return "pdf"
    return "attachment"
