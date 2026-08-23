import difflib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hzcu_agent.api.dependencies import request_principal, request_session
from hzcu_agent.auth.service import RequestPrincipal
from hzcu_agent.ingestion.snapshot import SnapshotStore
from hzcu_agent.models import (
    CampusEntityRecord,
    DocumentChunk,
    DocumentVersion,
    SourceDefinitionRecord,
    SourceResource,
    SyncRun,
    utc_now,
)
from hzcu_agent.schemas import (
    CampusEntityResponse,
    DocumentVersionDetailResponse,
    DocumentVersionSummaryResponse,
    SourceAlertResponse,
    SourceResourceResponse,
    SourceStatusResponse,
    VersionComparisonResponse,
)

router = APIRouter(tags=["sources"])
SessionDependency = Annotated[AsyncSession, Depends(request_session)]
PrincipalDependency = Annotated[RequestPrincipal, Depends(request_principal)]
MAX_DIFF_CHARACTERS = 12_000


async def _local_mirror_visibility_scopes(
    request: Request,
    principal: PrincipalDependency,
) -> frozenset[str]:
    return request.app.state.settings.local_mirror_visibility_scopes(
        principal.visibility_scopes,
        authenticated=principal.authenticated,
    )


MirrorVisibilityDependency = Annotated[
    frozenset[str],
    Depends(_local_mirror_visibility_scopes),
]


@router.get("/sources", response_model=list[SourceStatusResponse])
async def list_sources(
    session: SessionDependency,
    visibility_scopes: MirrorVisibilityDependency,
) -> list[SourceStatusResponse]:
    definitions = list(
        (
            await session.scalars(
                select(SourceDefinitionRecord)
                .where(SourceDefinitionRecord.visibility.in_(sorted(visibility_scopes)))
                .order_by(SourceDefinitionRecord.id)
            )
        ).all()
    )
    return await _source_statuses(session, definitions, utc_now())


@router.get("/sources/alerts", response_model=list[SourceAlertResponse])
async def list_source_alerts(
    session: SessionDependency,
    visibility_scopes: MirrorVisibilityDependency,
) -> list[SourceAlertResponse]:
    definitions = list(
        (
            await session.scalars(
                select(SourceDefinitionRecord)
                .where(SourceDefinitionRecord.visibility.in_(sorted(visibility_scopes)))
                .order_by(SourceDefinitionRecord.id)
            )
        ).all()
    )
    now = utc_now()
    alerts: list[SourceAlertResponse] = []
    for status in await _source_statuses(session, definitions, now):
        if status.health_state == "healthy" or status.health_state == "disabled":
            continue
        severity, code, message = {
            "failing": (
                "critical",
                "SOURCE_CONSECUTIVE_FAILURES",
                f"来源已连续失败 {status.consecutive_failures} 次，需要检查连接器或上游状态。",
            ),
            "stale": (
                "warning",
                "SOURCE_FRESHNESS_EXCEEDED",
                "来源已超过计划同步窗口，当前材料必须在回答时实时复核。",
            ),
            "degraded": (
                "warning",
                "SOURCE_DEGRADED",
                "最近同步存在失败资源或尚未形成可检索版本。",
            ),
            "waiting": (
                "info",
                "SOURCE_WAITING_FIRST_SYNC",
                "来源已登记，等待首轮同步。",
            ),
        }[status.health_state]
        alerts.append(
            SourceAlertResponse(
                source_id=status.source_id,
                source_name=status.name,
                severity=severity,
                code=code,
                message=message,
                detected_at=now,
            )
        )
    severity_order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(alerts, key=lambda item: (severity_order[item.severity], item.source_id))


@router.get("/sources/{source_id}", response_model=SourceStatusResponse)
async def get_source(
    source_id: str,
    session: SessionDependency,
    visibility_scopes: MirrorVisibilityDependency,
) -> SourceStatusResponse:
    source = await session.get(SourceDefinitionRecord, source_id)
    if source is None or source.visibility not in visibility_scopes:
        raise HTTPException(status_code=404, detail="Visible source not found")
    return await _source_status(session, source, utc_now())


@router.get(
    "/sources/{source_id}/resources",
    response_model=list[SourceResourceResponse],
)
async def list_source_resources(
    source_id: str,
    session: SessionDependency,
    visibility_scopes: MirrorVisibilityDependency,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> list[SourceResourceResponse]:
    source = await session.get(SourceDefinitionRecord, source_id)
    if source is None or source.visibility not in visibility_scopes:
        raise HTTPException(status_code=404, detail="Visible source not found")
    query = (
        select(SourceResource, DocumentVersion)
        .outerjoin(
            DocumentVersion,
            SourceResource.current_version_id == DocumentVersion.id,
        )
        .where(SourceResource.source_id == source_id)
        .order_by(SourceResource.last_seen_at.desc())
        .offset(offset)
        .limit(limit)
    )
    rows = (await session.execute(query)).all()
    responses: list[SourceResourceResponse] = []
    for resource, version in rows:
        version_count = await session.scalar(
            select(func.count(DocumentVersion.id)).where(DocumentVersion.resource_id == resource.id)
        )
        chunk_count = (
            await session.scalar(
                select(func.count(DocumentChunk.id)).where(
                    DocumentChunk.document_version_id == version.id
                )
            )
            if version
            else 0
        )
        entity = (
            await session.scalar(
                select(CampusEntityRecord)
                .where(CampusEntityRecord.document_version_id == version.id)
                .order_by(CampusEntityRecord.id)
                .limit(1)
            )
            if version
            else None
        )
        responses.append(
            SourceResourceResponse(
                resource_id=resource.id,
                canonical_uri=resource.canonical_uri,
                resource_type=resource.resource_type,
                first_seen_at=_as_utc(resource.first_seen_at),
                last_seen_at=_as_utc(resource.last_seen_at),
                current_version_id=resource.current_version_id,
                title=version.title if version else None,
                publisher=version.publisher if version else None,
                published_at=(
                    _as_utc(version.published_at) if version and version.published_at else None
                ),
                observed_at=(_as_utc(version.observed_at) if version else None),
                quality_status=version.quality_status if version else None,
                version_count=int(version_count or 0),
                chunk_count=int(chunk_count or 0),
                entity_type=entity.entity_type if entity else None,
                entity_status=entity.status if entity else None,
                deadline_at=(
                    _as_utc(entity.deadline_at) if entity and entity.deadline_at else None
                ),
                audience_scopes=entity.audience_scopes if entity else [],
            )
        )
    return responses


@router.get("/sources/{source_id}/resources/{resource_id}/original")
async def get_resource_original(
    source_id: str,
    resource_id: str,
    request: Request,
    session: SessionDependency,
    visibility_scopes: MirrorVisibilityDependency,
) -> FileResponse:
    resource = await _require_visible_resource(
        session,
        source_id,
        resource_id,
        visibility_scopes,
    )
    source = await session.get(SourceDefinitionRecord, source_id)
    version = (
        await session.get(DocumentVersion, resource.current_version_id)
        if resource.current_version_id
        else None
    )
    if (
        source is None
        or source.connector_kind != "operator_import"
        or version is None
        or version.raw_snapshot_uri is None
        or version.document_metadata.get("ingestion_method") != "operator_verified_document"
    ):
        raise HTTPException(status_code=404, detail="Original snapshot not found")
    snapshots = SnapshotStore(request.app.state.settings.resolved_snapshot_directory)
    try:
        snapshot_path = snapshots.path_for_uri(version.raw_snapshot_uri)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Original snapshot not found") from exc
    if not snapshot_path.is_file():
        raise HTTPException(status_code=404, detail="Original snapshot not found")
    filename = str(version.document_metadata.get("original_filename") or f"{resource.id}.bin")
    return FileResponse(
        snapshot_path,
        media_type=version.media_type or "application/octet-stream",
        filename=filename,
        content_disposition_type="inline",
        headers={"Content-Encoding": "gzip"},
    )


@router.get(
    "/sources/{source_id}/resources/{resource_id}/versions",
    response_model=list[DocumentVersionSummaryResponse],
)
async def list_resource_versions(
    source_id: str,
    resource_id: str,
    session: SessionDependency,
    visibility_scopes: MirrorVisibilityDependency,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[DocumentVersionSummaryResponse]:
    resource = await _require_visible_resource(
        session,
        source_id,
        resource_id,
        visibility_scopes,
    )
    versions = list(
        (
            await session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.resource_id == resource.id)
                .order_by(DocumentVersion.observed_at.desc())
                .limit(limit)
            )
        ).all()
    )
    return [
        await _version_summary(
            session,
            version,
            is_current=version.id == resource.current_version_id,
        )
        for version in versions
    ]


@router.get(
    "/sources/{source_id}/resources/{resource_id}/versions/{version_id}",
    response_model=DocumentVersionDetailResponse,
)
async def get_resource_version(
    source_id: str,
    resource_id: str,
    version_id: str,
    session: SessionDependency,
    visibility_scopes: MirrorVisibilityDependency,
) -> DocumentVersionDetailResponse:
    resource = await _require_visible_resource(
        session,
        source_id,
        resource_id,
        visibility_scopes,
    )
    version = await session.get(DocumentVersion, version_id)
    if version is None or version.resource_id != resource.id:
        raise HTTPException(status_code=404, detail="Document version not found")
    summary = await _version_summary(
        session,
        version,
        is_current=version.id == resource.current_version_id,
    )
    headings = list(
        (
            await session.scalars(
                select(DocumentChunk.heading)
                .where(
                    DocumentChunk.document_version_id == version.id,
                    DocumentChunk.heading.is_not(None),
                )
                .order_by(DocumentChunk.ordinal)
            )
        ).all()
    )
    return DocumentVersionDetailResponse(
        **summary.model_dump(),
        resource_id=resource.id,
        canonical_uri=resource.canonical_uri,
        media_type=version.media_type,
        text_excerpt=version.normalized_text[:6000],
        section_headings=[heading for heading in headings if heading][:30],
    )


@router.get(
    "/sources/{source_id}/resources/{resource_id}/compare",
    response_model=VersionComparisonResponse,
)
async def compare_resource_versions(
    source_id: str,
    resource_id: str,
    session: SessionDependency,
    visibility_scopes: MirrorVisibilityDependency,
    from_version_id: str | None = Query(default=None),
    to_version_id: str | None = Query(default=None),
) -> VersionComparisonResponse:
    resource = await _require_visible_resource(
        session,
        source_id,
        resource_id,
        visibility_scopes,
    )
    versions = list(
        (
            await session.scalars(
                select(DocumentVersion)
                .where(DocumentVersion.resource_id == resource.id)
                .order_by(DocumentVersion.observed_at.desc())
            )
        ).all()
    )
    if not versions:
        raise HTTPException(status_code=404, detail="Resource has no versions")
    by_id = {version.id: version for version in versions}
    to_version = by_id.get(to_version_id or resource.current_version_id or versions[0].id)
    if to_version is None:
        raise HTTPException(status_code=404, detail="Target version not found")
    if from_version_id:
        from_version = by_id.get(from_version_id)
        if from_version is None:
            raise HTTPException(status_code=404, detail="Base version not found")
    else:
        from_version = next(
            (version for version in versions if version.id != to_version.id),
            to_version,
        )

    diff = "\n".join(
        difflib.unified_diff(
            from_version.normalized_text.splitlines(),
            to_version.normalized_text.splitlines(),
            fromfile=from_version.id,
            tofile=to_version.id,
            lineterm="",
            n=3,
        )
    )
    truncated = len(diff) > MAX_DIFF_CHARACTERS
    if truncated:
        diff = f"{diff[:MAX_DIFF_CHARACTERS]}\n…差异过长，已截断"
    from_entity = await _first_entity(session, from_version.id)
    to_entity = await _first_entity(session, to_version.id)
    structured_changes = _structured_changes(from_entity, to_entity)
    return VersionComparisonResponse(
        resource_id=resource.id,
        from_version_id=from_version.id,
        to_version_id=to_version.id,
        changed=from_version.content_hash != to_version.content_hash,
        title_changed=from_version.title != to_version.title,
        unified_diff=diff,
        structured_changes=structured_changes,
        truncated=truncated,
    )


async def _source_status(
    session: AsyncSession,
    source: SourceDefinitionRecord,
    now: datetime,
) -> SourceStatusResponse:
    resource_count = await session.scalar(
        select(func.count(SourceResource.id)).where(SourceResource.source_id == source.id)
    )
    version_count = await session.scalar(
        select(func.count(DocumentVersion.id))
        .join(SourceResource, DocumentVersion.resource_id == SourceResource.id)
        .where(SourceResource.source_id == source.id)
    )
    chunk_count = await session.scalar(
        select(func.count(DocumentChunk.id))
        .join(DocumentVersion, DocumentChunk.document_version_id == DocumentVersion.id)
        .join(SourceResource, DocumentVersion.resource_id == SourceResource.id)
        .where(SourceResource.source_id == source.id)
    )
    entity_count = await session.scalar(
        select(func.count(CampusEntityRecord.id))
        .join(
            DocumentVersion,
            CampusEntityRecord.document_version_id == DocumentVersion.id,
        )
        .join(SourceResource, DocumentVersion.resource_id == SourceResource.id)
        .where(SourceResource.source_id == source.id)
    )
    latest_version_at = await session.scalar(
        select(func.max(DocumentVersion.observed_at))
        .join(SourceResource, DocumentVersion.resource_id == SourceResource.id)
        .where(SourceResource.source_id == source.id)
    )
    last_run = await session.scalar(
        select(SyncRun)
        .where(SyncRun.source_id == source.id)
        .order_by(SyncRun.started_at.desc())
        .limit(1)
    )
    last_success_at = await session.scalar(
        select(SyncRun.finished_at)
        .where(
            SyncRun.source_id == source.id,
            SyncRun.status.in_(["completed", "completed_with_errors"]),
        )
        .order_by(SyncRun.finished_at.desc())
        .limit(1)
    )
    recent_statuses = list(
        (
            await session.scalars(
                select(SyncRun.status)
                .where(SyncRun.source_id == source.id)
                .order_by(SyncRun.started_at.desc())
                .limit(20)
            )
        ).all()
    )
    consecutive_failures = 0
    for status in recent_statuses:
        if status in {"failed", "interrupted"}:
            consecutive_failures += 1
        else:
            break

    return _build_source_status(
        source,
        now,
        resource_count=int(resource_count or 0),
        version_count=int(version_count or 0),
        chunk_count=int(chunk_count or 0),
        entity_count=int(entity_count or 0),
        latest_version_at=latest_version_at,
        last_run=last_run,
        last_success_at=last_success_at,
        recent_statuses=recent_statuses,
    )


async def _source_statuses(
    session: AsyncSession,
    definitions: list[SourceDefinitionRecord],
    now: datetime,
) -> list[SourceStatusResponse]:
    """Build source status rows with bounded aggregate queries.

    The source observatory is a high fan-out endpoint: a pilot database can
    contain tens of thousands of versions and chunks across dozens of
    sources.  Calling ``_source_status`` once per source turns that into
    hundreds of serial count queries.  Aggregate each metric for all visible
    sources first, then compute the small health-state decision in Python.
    """

    if not definitions:
        return []

    source_ids = [source.id for source in definitions]
    resource_counts = {
        source_id: int(count)
        for source_id, count in (
            await session.execute(
                select(SourceResource.source_id, func.count(SourceResource.id))
                .where(SourceResource.source_id.in_(source_ids))
                .group_by(SourceResource.source_id)
            )
        ).all()
    }
    version_rows = (
        await session.execute(
            select(
                SourceResource.source_id,
                func.count(DocumentVersion.id),
                func.max(DocumentVersion.observed_at),
            )
            .join(
                DocumentVersion,
                DocumentVersion.resource_id == SourceResource.id,
            )
            .where(SourceResource.source_id.in_(source_ids))
            .group_by(SourceResource.source_id)
        )
    ).all()
    version_counts = {source_id: int(count) for source_id, count, _ in version_rows}
    latest_version_at = {source_id: latest for source_id, _, latest in version_rows}

    chunk_counts = {
        source_id: int(count)
        for source_id, count in (
            await session.execute(
                select(SourceResource.source_id, func.count(DocumentChunk.id))
                .join(
                    DocumentVersion,
                    DocumentVersion.resource_id == SourceResource.id,
                )
                .join(
                    DocumentChunk,
                    DocumentChunk.document_version_id == DocumentVersion.id,
                )
                .where(SourceResource.source_id.in_(source_ids))
                .group_by(SourceResource.source_id)
            )
        ).all()
    }
    entity_counts = {
        source_id: int(count)
        for source_id, count in (
            await session.execute(
                select(SourceResource.source_id, func.count(CampusEntityRecord.id))
                .join(
                    DocumentVersion,
                    DocumentVersion.resource_id == SourceResource.id,
                )
                .join(
                    CampusEntityRecord,
                    CampusEntityRecord.document_version_id == DocumentVersion.id,
                )
                .where(SourceResource.source_id.in_(source_ids))
                .group_by(SourceResource.source_id)
            )
        ).all()
    }

    runs = list(
        (
            await session.scalars(
                select(SyncRun)
                .where(SyncRun.source_id.in_(source_ids))
                .order_by(SyncRun.source_id, SyncRun.started_at.desc())
            )
        ).all()
    )
    runs_by_source: dict[str, list[SyncRun]] = {}
    for run in runs:
        runs_by_source.setdefault(run.source_id, []).append(run)
    last_success_rows = (
        await session.execute(
            select(SyncRun.source_id, func.max(SyncRun.finished_at))
            .where(
                SyncRun.source_id.in_(source_ids),
                SyncRun.status.in_(["completed", "completed_with_errors"]),
            )
            .group_by(SyncRun.source_id)
        )
    ).all()
    last_success_at = {source_id: finished_at for source_id, finished_at in last_success_rows}

    statuses: list[SourceStatusResponse] = []
    for source in definitions:
        source_runs = runs_by_source.get(source.id, [])
        statuses.append(
            _build_source_status(
                source,
                now,
                resource_count=resource_counts.get(source.id, 0),
                version_count=version_counts.get(source.id, 0),
                chunk_count=chunk_counts.get(source.id, 0),
                entity_count=entity_counts.get(source.id, 0),
                latest_version_at=latest_version_at.get(source.id),
                last_run=source_runs[0] if source_runs else None,
                last_success_at=last_success_at.get(source.id),
                recent_statuses=[run.status for run in source_runs[:20]],
            )
        )
    return statuses


def _build_source_status(
    source: SourceDefinitionRecord,
    now: datetime,
    *,
    resource_count: int,
    version_count: int,
    chunk_count: int,
    entity_count: int,
    latest_version_at: datetime | None,
    last_run: SyncRun | None,
    last_success_at: datetime | None,
    recent_statuses: list[str],
) -> SourceStatusResponse:
    consecutive_failures = 0
    for status in recent_statuses:
        if status in {"failed", "interrupted"}:
            consecutive_failures += 1
        else:
            break

    fresh_until = (
        _as_utc(last_success_at) + timedelta(seconds=max(source.poll_interval_seconds * 2, 300))
        if last_success_at
        else None
    )
    if not source.enabled:
        health_state = "disabled"
    elif last_success_at is None:
        health_state = "waiting"
    elif consecutive_failures >= 3:
        health_state = "failing"
    elif last_run and last_run.status in {"failed", "interrupted", "completed_with_errors"}:
        health_state = "degraded"
    elif not resource_count or not chunk_count:
        health_state = "degraded"
    elif fresh_until and now > fresh_until:
        health_state = "stale"
    else:
        health_state = "healthy"

    return SourceStatusResponse(
        source_id=source.id,
        name=source.name,
        owner_department=source.owner_department,
        base_url=source.base_url,
        allowed_hosts=source.allowed_hosts,
        visibility=source.visibility,
        authority_level=source.authority_level,
        connector_kind=source.connector_kind,
        poll_interval_seconds=source.poll_interval_seconds,
        default_ttl_seconds=source.default_ttl_seconds,
        enabled=source.enabled,
        resource_count=int(resource_count or 0),
        version_count=int(version_count or 0),
        last_run_status=last_run.status if last_run else None,
        last_run_started_at=(_as_utc(last_run.started_at) if last_run else None),
        last_success_at=(_as_utc(last_success_at) if last_success_at else None),
        latest_version_at=(_as_utc(latest_version_at) if latest_version_at else None),
        fresh_until=fresh_until,
        health_state=health_state,
        consecutive_failures=consecutive_failures,
        chunk_count=chunk_count,
        entity_count=entity_count,
    )


async def _require_visible_resource(
    session: AsyncSession,
    source_id: str,
    resource_id: str,
    visibility_scopes: frozenset[str],
) -> SourceResource:
    source = await session.get(SourceDefinitionRecord, source_id)
    if source is None or source.visibility not in visibility_scopes:
        raise HTTPException(status_code=404, detail="Visible source not found")
    resource = await session.get(SourceResource, resource_id)
    if resource is None or resource.source_id != source.id:
        raise HTTPException(status_code=404, detail="Source resource not found")
    return resource


async def _version_summary(
    session: AsyncSession,
    version: DocumentVersion,
    *,
    is_current: bool,
) -> DocumentVersionSummaryResponse:
    chunk_count = await session.scalar(
        select(func.count(DocumentChunk.id)).where(DocumentChunk.document_version_id == version.id)
    )
    entities = list(
        (
            await session.scalars(
                select(CampusEntityRecord)
                .where(CampusEntityRecord.document_version_id == version.id)
                .order_by(CampusEntityRecord.id)
            )
        ).all()
    )
    return DocumentVersionSummaryResponse(
        version_id=version.id,
        content_hash=version.content_hash,
        title=version.title,
        publisher=version.publisher,
        published_at=(_as_utc(version.published_at) if version.published_at else None),
        effective_from=(_as_utc(version.effective_from) if version.effective_from else None),
        effective_to=(_as_utc(version.effective_to) if version.effective_to else None),
        observed_at=_as_utc(version.observed_at),
        parser_version=version.parser_version,
        quality_status=version.quality_status,
        is_current=is_current,
        chunk_count=int(chunk_count or 0),
        entities=[_entity_response(entity) for entity in entities],
    )


def _entity_response(entity: CampusEntityRecord) -> CampusEntityResponse:
    return CampusEntityResponse(
        entity_id=entity.id,
        entity_type=entity.entity_type,
        canonical_name=entity.canonical_name,
        status=entity.status,
        department=entity.department,
        starts_at=_optional_utc(entity.starts_at),
        deadline_at=_optional_utc(entity.deadline_at),
        ends_at=_optional_utc(entity.ends_at),
        effective_from=_optional_utc(entity.effective_from),
        effective_to=_optional_utc(entity.effective_to),
        audience_scopes=entity.audience_scopes,
        action_items=entity.action_items,
        locations=entity.locations,
        document_number=entity.document_number,
        relation_kind=entity.relation_kind,
        related_title=entity.related_title,
        confidence=entity.confidence,
        extractor_version=entity.extractor_version,
    )


async def _first_entity(
    session: AsyncSession,
    version_id: str,
) -> CampusEntityRecord | None:
    return await session.scalar(
        select(CampusEntityRecord)
        .where(CampusEntityRecord.document_version_id == version_id)
        .order_by(CampusEntityRecord.id)
        .limit(1)
    )


def _structured_changes(
    before: CampusEntityRecord | None,
    after: CampusEntityRecord | None,
) -> dict[str, Any]:
    fields = (
        "entity_type",
        "status",
        "starts_at",
        "deadline_at",
        "ends_at",
        "effective_from",
        "effective_to",
        "audience_scopes",
        "action_items",
        "locations",
        "document_number",
        "relation_kind",
        "related_title",
    )
    changes: dict[str, Any] = {}
    for field_name in fields:
        before_value = getattr(before, field_name, None)
        after_value = getattr(after, field_name, None)
        if isinstance(before_value, datetime):
            before_value = _as_utc(before_value).isoformat()
        if isinstance(after_value, datetime):
            after_value = _as_utc(after_value).isoformat()
        if before_value != after_value:
            changes[field_name] = {
                "from": before_value,
                "to": after_value,
            }
    return changes


def _optional_utc(value: datetime | None) -> datetime | None:
    return _as_utc(value) if value else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
