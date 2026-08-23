"""Import one verified, pre-parsed official artifact into the campus mirror."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.ingestion.catalog import SourceRegistry
from hzcu_agent.ingestion.operator_import import OperatorDocumentImporter

SHANGHAI = ZoneInfo("Asia/Shanghai")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Store a verified original artifact and its page-addressable text."
    )
    parser.add_argument("original", type=Path)
    parser.add_argument("text", type=Path)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--published-at")
    parser.add_argument("--effective-from")
    parser.add_argument("--effective-to")
    parser.add_argument("--media-type", default="application/pdf")
    parser.add_argument("--parser-version", default="multimodal-page-ocr-v1")
    parser.add_argument(
        "--database-url",
        default="sqlite+aiosqlite:///./data/hzcu_agent.db",
    )
    parser.add_argument("--snapshot-directory", default="./data/snapshots")
    parser.add_argument("--audience", action="append", default=[])
    parser.add_argument("--corroborating-url", action="append", default=[])
    parser.add_argument("--note")
    return parser.parse_args()


def _datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(UTC)


async def _run(args: argparse.Namespace) -> dict[str, object]:
    original_path = args.original.resolve()
    text_path = args.text.resolve()
    if not original_path.is_file() or not text_path.is_file():
        raise RuntimeError("Original artifact and OCR text must both exist")

    settings = Settings(
        environment="development",
        database_url=args.database_url,
        snapshot_directory=args.snapshot_directory,
    )
    database = Database(settings)
    await database.initialize()
    registry = SourceRegistry(database, settings.resolved_source_registry_path)
    await registry.sync_definitions()
    importer = OperatorDocumentImporter(
        settings=settings,
        database=database,
        registry=registry,
    )
    try:
        outcome = await importer.import_document(
            source_id=args.source_id,
            original=original_path.read_bytes(),
            original_filename=original_path.name,
            normalized_text=text_path.read_text(encoding="utf-8"),
            title=args.title,
            publisher=args.publisher,
            media_type=args.media_type,
            published_at=_datetime(args.published_at),
            effective_from=_datetime(args.effective_from),
            effective_to=_datetime(args.effective_to),
            parser_version=args.parser_version,
            metadata={
                "audience_scopes": args.audience,
                "corroborating_urls": args.corroborating_url,
                "operator_note": args.note,
                "text_mode": "page_addressable_multimodal_ocr",
            },
        )
        return {
            "status": outcome.status,
            "source_id": outcome.source_id,
            "resource_id": outcome.resource_id,
            "document_version_id": outcome.document_version_id,
            "canonical_uri": outcome.canonical_uri,
            "chunks": outcome.chunks,
            "entities": outcome.entities,
        }
    finally:
        await database.close()


def main() -> int:
    result = asyncio.run(_run(_arguments()))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
