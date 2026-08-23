import argparse
import asyncio
import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy import func, select, text

from hzcu_agent.config import Settings, get_settings
from hzcu_agent.db import Database
from hzcu_agent.ingestion.catalog import SourceRegistry
from hzcu_agent.ingestion.indexing import DocumentIndexer
from hzcu_agent.ingestion.service import IngestionService
from hzcu_agent.local_model_config import load_local_openai_config
from hzcu_agent.models import SourceDefinitionRecord, SyncRun, utc_now
from hzcu_agent.tools.campus_memory import (
    CampusMemorySearchArguments,
    CampusMemorySearchTool,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hzcu-agent")
    subcommands = parser.add_subparsers(dest="command", required=True)
    sync_parser = subcommands.add_parser(
        "sync-sources",
        help="Synchronize explicitly selected or registered campus sources.",
    )
    sync_parser.add_argument(
        "--source",
        action="append",
        dest="source_ids",
        help="A registered source id. Repeat to synchronize several sources.",
    )
    sync_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Maximum resources discovered from each source for this run.",
    )
    sync_parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "Exhaust registered list/pagination frontiers and mirror every readable "
            "detail, article image and attachment. Ignores per-run sampling caps."
        ),
    )
    subcommands.add_parser(
        "list-sources",
        help="List the mirrored Source Registry without fetching remote content.",
    )
    search_parser = subcommands.add_parser(
        "search-memory",
        help="Inspect current-version campus memory retrieval.",
    )
    search_parser.add_argument("query", help="One independent retrieval query.")
    search_parser.add_argument("--top-k", type=int, default=8)
    worker_parser = subcommands.add_parser(
        "sync-worker",
        help="Continuously synchronize sources when their configured interval is due.",
    )
    worker_parser.add_argument(
        "--poll-seconds",
        type=int,
        default=30,
        help="How often to check the Source Registry for due sources.",
    )
    worker_parser.add_argument(
        "--source",
        action="append",
        dest="source_ids",
        help="Restrict the worker to a source id. Repeat for several sources.",
    )
    subcommands.add_parser(
        "reindex-memory",
        help="Rebuild semantic chunks, vectors and structured entities for all versions.",
    )
    subcommands.add_parser(
        "pilot-preflight",
        help="Run read-only checks for the single-node Stage 6 pilot.",
    )
    backup_parser = subcommands.add_parser(
        "pilot-backup",
        help="Create a consistent SQLite backup in the pilot data directory.",
    )
    backup_parser.add_argument("--output", default=None)
    restore_parser = subcommands.add_parser(
        "pilot-restore",
        help="Restore SQLite from an explicit pilot backup file.",
    )
    restore_parser.add_argument("--backup", required=True)
    serve_parser = subcommands.add_parser(
        "serve",
        help="Run the API, optionally loading an OpenAI-compatible local config file.",
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument(
        "--model-config",
        default=None,
        help="UTF-8 file containing an API key and optional base URL; values stay in memory.",
    )
    serve_parser.add_argument(
        "--anonymous-campus-mirror",
        action="store_true",
        help="Allow anonymous subjects to read the approved local Campus mirror.",
    )
    serve_parser.add_argument(
        "--model-timeout",
        type=float,
        default=None,
        help="Per-model-call timeout in seconds.",
    )
    return parser


def _settings_for_serve(args: argparse.Namespace) -> Settings:
    settings = get_settings()
    updates: dict[str, object] = {}
    if args.model_config:
        local_config = load_local_openai_config(args.model_config)
        updates.update(
            {
                "model_provider": "openai",
                "openai_api_key": SecretStr(local_config.api_key),
            }
        )
        if local_config.base_url is not None:
            updates["openai_base_url"] = local_config.base_url
    if getattr(args, "anonymous_campus_mirror", False):
        updates["pilot_anonymous_campus_mirror"] = True
    if getattr(args, "model_timeout", None) is not None:
        updates["model_timeout_seconds"] = args.model_timeout
    if not updates:
        return settings
    return settings.model_copy(update=updates)


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from hzcu_agent.main import create_app

    settings = _settings_for_serve(args)
    uvicorn.run(
        create_app(settings),
        host=args.host,
        port=args.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    return 0


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.command == "pilot-backup":
        return _pilot_backup(settings.database_url, args.output)
    if args.command == "pilot-restore":
        return _pilot_restore(settings.database_url, args.backup)

    database = Database(settings)
    await database.initialize()
    registry = SourceRegistry(database, settings.resolved_source_registry_path)
    try:
        if args.command == "pilot-preflight":
            return await _pilot_preflight(database, settings)

        await registry.sync_definitions()
        if args.command == "list-sources":
            async with database.session_factory() as session:
                records = list(
                    (
                        await session.scalars(
                            select(SourceDefinitionRecord).order_by(SourceDefinitionRecord.id)
                        )
                    ).all()
                )
            print(
                json.dumps(
                    [
                        {
                            "source_id": record.id,
                            "name": record.name,
                            "connector": record.connector_kind,
                            "enabled": record.enabled,
                        }
                        for record in records
                    ],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.command == "search-memory":
            memory = CampusMemorySearchTool(database)
            result = await memory.run(
                CampusMemorySearchArguments(query=args.query, top_k=args.top_k),
                trace_id="trace_cli",
            )
            print(
                json.dumps(
                    result.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        if args.command == "reindex-memory":
            outcomes = await DocumentIndexer().rebuild_versions(database)
            search_versions = await CampusMemorySearchTool(database).rebuild()
            print(
                json.dumps(
                    {
                        "indexed_versions": len(outcomes),
                        "search_versions": search_versions,
                        "chunks": sum(outcome.chunks for outcome in outcomes),
                        "entities": sum(outcome.entities for outcome in outcomes),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0

        ingestion = IngestionService(
            settings=settings,
            database=database,
            registry=registry,
        )
        try:
            if args.command == "sync-worker":
                if args.poll_seconds < 5:
                    raise ValueError("--poll-seconds must be at least 5")
                while True:
                    await registry.sync_definitions()
                    due_ids = await _due_source_ids(
                        database,
                        registry,
                        allowed_source_ids=set(args.source_ids or []),
                        allowed_visibilities=settings.ingestion_visibility_set,
                    )
                    for source_id in due_ids:
                        outcome = await ingestion.sync_source(source_id)
                        print(
                            json.dumps(
                                asdict(outcome),
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                    await asyncio.sleep(args.poll_seconds)

            source_ids = args.source_ids or [
                source.id for source in registry.sources if source.enabled
            ]
            if args.full and args.limit is not None:
                raise ValueError("--full and --limit cannot be used together")
            outcomes = [
                await ingestion.sync_source(
                    source_id,
                    limit_override=args.limit,
                    full_scan=args.full,
                )
                for source_id in source_ids
            ]
            print(
                json.dumps(
                    [asdict(outcome) for outcome in outcomes],
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1 if any(outcome.status == "failed" for outcome in outcomes) else 0
        finally:
            await ingestion.close()
    finally:
        await database.close()


async def _pilot_preflight(database: Database, settings) -> int:
    checks: list[dict[str, object]] = []
    fatal = False

    def add(name: str, ok: bool, detail: object, *, required: bool = True) -> None:
        nonlocal fatal
        checks.append(
            {
                "check": name,
                "status": "pass" if ok else ("fail" if required else "warning"),
                "detail": detail,
            }
        )
        if required and not ok:
            fatal = True

    add(
        "database.dialect",
        database.engine.dialect.name == "sqlite",
        database.engine.dialect.name,
    )
    async with database.session_factory() as session:
        integrity = await session.scalar(text("PRAGMA integrity_check"))
        add("database.integrity", integrity == "ok", integrity)
        compile_options = list(
            (await session.execute(text("PRAGMA compile_options"))).scalars().all()
        )
        fts_supported = any("ENABLE_FTS5" in item for item in compile_options)
        if not fts_supported:
            try:
                await session.execute(
                    text(
                        "CREATE VIRTUAL TABLE temp.pilot_fts_probe "
                        "USING fts5(value, tokenize='trigram')"
                    )
                )
                fts_supported = True
            except Exception:
                fts_supported = False
        add("sqlite.fts5_trigram", fts_supported, "available" if fts_supported else "missing")
        revision = await session.scalar(
            text("SELECT version_num FROM alembic_version ORDER BY version_num DESC LIMIT 1")
        )
        add(
            "alembic.revision",
            revision == "0008_answer_evidence_provenance",
            revision or "missing",
        )
        fts_exists = await session.scalar(
            text(
                "SELECT count(*) FROM sqlite_master "
                "WHERE type='table' AND name='campus_search_fts_v1'"
            )
        )
        add("search.fts_index", bool(fts_exists), "present" if fts_exists else "missing")
        searchable = (
            await session.scalar(text("SELECT count(*) FROM campus_search_fts_v1"))
            if fts_exists
            else 0
        )
        add("search.document_versions", int(searchable or 0) > 0, int(searchable or 0))
        enabled_sources = await session.scalar(
            text("SELECT count(*) FROM source_definitions WHERE enabled = 1")
        )
        add("sources.enabled", int(enabled_sources or 0) > 0, int(enabled_sources or 0))
        failed_sources = await session.scalar(
            text("SELECT count(*) FROM sync_runs WHERE status IN ('failed', 'partial')")
        )
        add(
            "sources.failed_runs",
            int(failed_sources or 0) == 0,
            int(failed_sources or 0),
            required=False,
        )

    add(
        "model.gateway",
        settings.model_is_configured,
        {
            "provider": settings.model_provider,
            "agent_model": settings.agent_model,
        },
    )
    add(
        "cas.optional",
        settings.auth_mode == "anonymous" or settings.cas_login_ready,
        (
            "disabled"
            if settings.auth_mode == "anonymous"
            else ("ready" if settings.cas_login_ready else "hidden_until_registered")
        ),
        required=False,
    )
    add(
        "task.concurrency",
        settings.max_concurrent_agent_tasks == 4 and settings.max_active_tasks_per_subject == 1,
        {
            "global": settings.max_concurrent_agent_tasks,
            "per_subject": settings.max_active_tasks_per_subject,
        },
    )
    print(
        json.dumps(
            {
                "status": "ready" if not fatal else "blocked",
                "checks": checks,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if fatal else 0


def _pilot_backup(database_url: str, output: str | None) -> int:
    database_path = _sqlite_path(database_url)
    if not database_path.exists():
        raise FileNotFoundError(f"pilot database does not exist: {database_path}")
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_path = (
        Path(output).expanduser()
        if output
        else database_path.parent / "backups" / f"hzcu-pilot-{timestamp}.db"
    ).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as source, sqlite3.connect(output_path) as target:
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()
    if not integrity or integrity[0] != "ok":
        output_path.unlink(missing_ok=True)
        raise RuntimeError("backup integrity check failed")
    print(
        json.dumps(
            {"status": "created", "backup": str(output_path)},
            ensure_ascii=False,
        )
    )
    return 0


def _pilot_restore(database_url: str, backup: str) -> int:
    database_path = _sqlite_path(database_url)
    backup_path = Path(backup).expanduser().resolve()
    if not backup_path.is_file():
        raise FileNotFoundError(f"backup does not exist: {backup_path}")
    with sqlite3.connect(backup_path) as source:
        integrity = source.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError("backup integrity check failed")
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database_path) as target:
            source.backup(target)
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print(
        json.dumps(
            {"status": "restored", "backup": str(backup_path)},
            ensure_ascii=False,
        )
    )
    return 0


def _sqlite_path(database_url: str) -> Path:
    prefix = "sqlite+aiosqlite:///"
    if not database_url.startswith(prefix):
        raise ValueError("pilot backup and restore require SQLite")
    raw_path = database_url[len(prefix) :]
    if raw_path == ":memory:":
        raise ValueError("in-memory SQLite cannot be backed up")
    return Path(raw_path).expanduser().resolve()


async def _due_source_ids(
    database: Database,
    registry: SourceRegistry,
    *,
    allowed_source_ids: set[str],
    allowed_visibilities: frozenset[str] = frozenset({"public"}),
) -> list[str]:
    now = utc_now()
    due: list[str] = []
    async with database.session_factory() as session:
        for source in registry.sources:
            if (
                not source.enabled
                or source.visibility not in allowed_visibilities
                or (allowed_source_ids and source.id not in allowed_source_ids)
            ):
                continue
            last_started_at = await session.scalar(
                select(func.max(SyncRun.started_at)).where(
                    SyncRun.source_id == source.id,
                    SyncRun.status.in_(["completed", "completed_with_errors", "failed"]),
                )
            )
            if (
                last_started_at is None
                or (now - _as_utc(last_started_at)).total_seconds() >= source.poll_interval_seconds
            ):
                due.append(source.id)
    return due


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "serve":
        raise SystemExit(_serve(args))
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
