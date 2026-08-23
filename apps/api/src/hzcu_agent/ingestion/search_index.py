from __future__ import annotations

import asyncio

from sqlalchemy import text

from hzcu_agent.db import Database

SOURCE_SEARCH_FTS_TABLE = "campus_source_search_fts_v1"

_initialize_lock = asyncio.Lock()


async def ensure_source_search_index(database: Database) -> None:
    """Create the disposable source-level catalog used by hybrid retrieval."""

    if database.engine.dialect.name != "sqlite":
        return
    async with _initialize_lock:
        async with database.session_factory() as session:
            await session.execute(
                text(
                    f"""
                    CREATE VIRTUAL TABLE IF NOT EXISTS {SOURCE_SEARCH_FTS_TABLE}
                    USING fts5(
                        source_id UNINDEXED,
                        name,
                        owner,
                        titles,
                        tokenize='trigram'
                    )
                    """
                )
            )
            indexed = await session.scalar(
                text(f"SELECT 1 FROM {SOURCE_SEARCH_FTS_TABLE} LIMIT 1")
            )
            if indexed is None:
                await _replace_profiles(session)
            await session.commit()


async def refresh_source_search_profile(database: Database, source_id: str) -> None:
    """Refresh one source profile after its current-version ledger changes."""

    if database.engine.dialect.name != "sqlite":
        return
    await ensure_source_search_index(database)
    async with database.session_factory() as session:
        await _replace_profiles(session, source_id=source_id)
        await session.commit()


async def rebuild_source_search_index(database: Database) -> int:
    if database.engine.dialect.name != "sqlite":
        return 0
    await ensure_source_search_index(database)
    async with _initialize_lock:
        async with database.session_factory() as session:
            await session.execute(text(f"DELETE FROM {SOURCE_SEARCH_FTS_TABLE}"))
            await _replace_profiles(session)
            count = await session.scalar(
                text(f"SELECT count(*) FROM {SOURCE_SEARCH_FTS_TABLE}")
            )
            await session.commit()
    return int(count or 0)


async def recreate_source_search_index(database: Database) -> int:
    """Drop and recreate the disposable source catalog, including corruption recovery."""

    if database.engine.dialect.name != "sqlite":
        return 0
    async with _initialize_lock:
        async with database.session_factory() as session:
            await session.execute(text(f"DROP TABLE IF EXISTS {SOURCE_SEARCH_FTS_TABLE}"))
            await session.execute(
                text(
                    f"""
                    CREATE VIRTUAL TABLE {SOURCE_SEARCH_FTS_TABLE}
                    USING fts5(
                        source_id UNINDEXED,
                        name,
                        owner,
                        titles,
                        tokenize='trigram'
                    )
                    """
                )
            )
            await _replace_profiles(session)
            count = await session.scalar(
                text(f"SELECT count(*) FROM {SOURCE_SEARCH_FTS_TABLE}")
            )
            await session.commit()
    return int(count or 0)


async def _replace_profiles(session, *, source_id: str | None = None) -> None:
    bindings: dict[str, str] = {}
    source_filter = ""
    if source_id is not None:
        bindings["source_id"] = source_id
        source_filter = "WHERE s.id = :source_id"
        await session.execute(
            text(
                f"DELETE FROM {SOURCE_SEARCH_FTS_TABLE} "
                "WHERE source_id = :source_id"
            ),
            bindings,
        )

    await session.execute(
        text(
            f"""
            INSERT INTO {SOURCE_SEARCH_FTS_TABLE}(source_id, name, owner, titles)
            SELECT
                s.id,
                s.name,
                s.owner_department,
                COALESCE(group_concat(substr(v.title, 1, 160), ' '), '')
            FROM source_definitions AS s
            LEFT JOIN source_resources AS r
                ON r.source_id = s.id
            LEFT JOIN document_versions AS v
                ON v.id = r.current_version_id
                AND v.quality_status NOT IN (
                    'rejected',
                    'excluded_temporal',
                    'excluded_expired_event',
                    'binary_mirrored',
                    'image_pending_transcription',
                    'pdf_pending_ocr'
                )
            {source_filter}
            GROUP BY s.id, s.name, s.owner_department
            """
        ),
        bindings,
    )
