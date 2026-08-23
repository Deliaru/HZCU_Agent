from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class DiscoveredResource:
    canonical_uri: str
    fetch_uri: str
    resource_type: str
    title_hint: str = ""
    published_hint: datetime | None = None
    external_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FetchPayload:
    status_code: int
    body: bytes
    media_type: str
    final_url: str
    etag: str | None = None
    last_modified: str | None = None
    encoding: str | None = None


@dataclass(frozen=True)
class ParsedDocument:
    title: str
    publisher: str
    normalized_text: str
    media_type: str
    published_at: datetime | None
    effective_from: datetime | None
    effective_to: datetime | None
    parser_version: str
    quality_status: str
    metadata: dict[str, Any] = field(default_factory=dict)
