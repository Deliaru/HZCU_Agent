import hashlib
import json
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select

from hzcu_agent.db import Database
from hzcu_agent.models import SourceDefinitionRecord, utc_now


class CmsChannelConfig(BaseModel):
    code: str = Field(min_length=1, max_length=80)
    channel_id: str = Field(min_length=1, max_length=120)


class ConnectorConfig(BaseModel):
    kind: Literal["linked_html", "lecture_api", "cms_api", "operator_import"]
    earliest_published_year: int = Field(default=2023, ge=1990, le=2100)
    follow_site_navigation: bool = False
    mirror_listing_pages: bool = True
    full_scan_by_default: bool = False
    seed_urls: list[str] = Field(default_factory=list)
    include_patterns: list[str] = Field(default_factory=list)
    include_seed: bool = False
    max_resources_per_run: int = Field(default=30, ge=1, le=500)
    list_endpoint: str | None = None
    detail_endpoint: str | None = None
    public_detail_template: str | None = None
    page_size: int = Field(default=30, ge=1, le=100)
    channels: list[CmsChannelConfig] = Field(default_factory=list)

    @field_validator("include_patterns")
    @classmethod
    def patterns_must_compile(cls, values: list[str]) -> list[str]:
        for value in values:
            re.compile(value)
        return values

    @model_validator(mode="after")
    def required_fields_for_kind(self) -> "ConnectorConfig":
        if self.kind == "linked_html" and not self.seed_urls:
            raise ValueError("linked_html connector requires seed_urls")
        if self.kind == "lecture_api":
            required = (
                self.list_endpoint,
                self.detail_endpoint,
                self.public_detail_template,
            )
            if not all(required):
                raise ValueError(
                    "lecture_api connector requires both endpoints and a detail template"
                )
        if self.kind == "cms_api":
            required = (
                self.list_endpoint,
                self.public_detail_template,
                self.channels,
            )
            if not all(required):
                raise ValueError(
                    "cms_api connector requires a list endpoint, detail template and channels"
                )
        return self


class SourceConfig(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,119}$")
    name: str = Field(min_length=1, max_length=200)
    owner_department: str = Field(min_length=1, max_length=200)
    base_url: str
    allowed_hosts: list[str] = Field(min_length=1)
    visibility: Literal["public", "campus", "restricted"] = "public"
    authority_level: Literal["official", "official_secondary", "curated"] = "official"
    acquisition_methods: list[str] = Field(default_factory=list)
    poll_interval_seconds: int = Field(ge=60)
    rate_limit_per_minute: int = Field(default=20, ge=1, le=120)
    default_ttl_seconds: int = Field(ge=60)
    live_required_for: list[str] = Field(default_factory=list)
    parser_profile: str = Field(min_length=1, max_length=80)
    snapshot_policy: Literal["raw", "sanitized"] = "raw"
    vpn_browser_base_url: str | None = None
    enabled: bool = True
    connector: ConnectorConfig

    @field_validator("allowed_hosts")
    @classmethod
    def normalize_hosts(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            host = value.strip().rstrip(".").lower()
            if not host or "*" in host or "/" in host or ":" in host:
                raise ValueError(f"invalid exact allowed host: {value}")
            if host not in normalized:
                normalized.append(host)
        return normalized

    @model_validator(mode="after")
    def urls_must_be_exact_host_allowlisted(self) -> "SourceConfig":
        """Exact host allowlist; HTTPS preferred, HTTP allowed for campus CMS hosts."""
        urls = [
            self.base_url,
            *self.connector.seed_urls,
            self.connector.list_endpoint,
            self.connector.detail_endpoint,
            self.connector.public_detail_template,
        ]
        for raw_url in (value for value in urls if value):
            parsed = urlsplit(raw_url)
            scheme = (parsed.scheme or "").lower()
            host = (parsed.hostname or "").rstrip(".").lower()
            port = parsed.port
            if parsed.username is not None or parsed.password is not None:
                raise ValueError(f"source URL must not embed credentials: {raw_url}")
            if host not in self.allowed_hosts:
                raise ValueError(f"source URL host is not on the exact allowlist: {raw_url}")
            if scheme == "https" and port in (None, 443):
                continue
            if scheme == "http" and port in (None, 80):
                continue
            raise ValueError(
                f"source URL must use http/https on default ports with exact host: {raw_url}"
            )
        if self.vpn_browser_base_url:
            parsed = urlsplit(self.vpn_browser_base_url)
            host = (parsed.hostname or "").rstrip(".").lower()
            expected_proxy_hosts = {
                f"{allowed_host.replace('.', '-')}.vpn.hzcu.edu.cn"
                for allowed_host in self.allowed_hosts
            }
            if (
                parsed.scheme != "http"
                or not host.endswith(".vpn.hzcu.edu.cn")
                or host not in expected_proxy_hosts
                or parsed.port != 8118
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "VPN browser route must use an exact *.vpn.hzcu.edu.cn:8118 HTTP base"
                )
        return self


class SourceCatalog(BaseModel):
    version: int = Field(ge=1)
    sources: list[SourceConfig] = Field(min_length=1)

    @model_validator(mode="after")
    def source_ids_must_be_unique(self) -> "SourceCatalog":
        ids = [source.id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source ids must be unique")
        return self


def load_source_catalog(path: Path) -> SourceCatalog:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return SourceCatalog.model_validate(payload)


class SourceRegistry:
    """Source hosts are the trust boundary; path patterns are discovery hints."""

    def __init__(self, database: Database, catalog_path: Path) -> None:
        self._database = database
        self.catalog_path = catalog_path
        self.catalog = load_source_catalog(catalog_path)
        self._sources = {source.id: source for source in self.catalog.sources}

    @property
    def sources(self) -> tuple[SourceConfig, ...]:
        return tuple(self._sources.values())

    def get(self, source_id: str) -> SourceConfig | None:
        return self._sources.get(source_id)

    def require(self, source_id: str) -> SourceConfig:
        source = self.get(source_id)
        if source is None or not source.enabled:
            raise KeyError(f"Unknown or disabled source: {source_id}")
        return source

    def match_url(self, url: str) -> SourceConfig | None:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        candidates = [
            source for source in self.sources if source.enabled and host in source.allowed_hosts
        ]
        if not candidates:
            return None
        candidates.sort(
            key=lambda source: len(urlsplit(source.base_url).path),
            reverse=True,
        )
        for source in candidates:
            base_path = urlsplit(source.base_url).path.rstrip("/")
            if base_path and parsed.path.startswith(base_path):
                return source
        for source in candidates:
            if any(re.search(pattern, url) for pattern in source.connector.include_patterns):
                return source
        return candidates[-1]

    def accepts_detail_url(self, source_id: str, url: str) -> bool:
        source = self.get(source_id)
        if source is None or not source.enabled:
            return False
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            return False
        host = (parsed.hostname or "").rstrip(".").lower()
        if (
            parsed.scheme not in {"http", "https"}
            or host not in source.allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or not (
                (parsed.scheme == "http" and port in {None, 80})
                or (parsed.scheme == "https" and port in {None, 443})
            )
            or parsed.fragment
            or any(ord(character) < 32 for character in url)
        ):
            return False
        if any(re.search(pattern, url) for pattern in source.connector.include_patterns):
            return True
        return source.connector.kind == "linked_html" and is_campus_content_path(parsed.path)

    async def sync_definitions(self) -> int:
        now = utc_now()
        configured_ids = set(self._sources)
        changed = 0
        async with self._database.session_factory() as session:
            records = {
                item.id: item
                for item in (await session.scalars(select(SourceDefinitionRecord))).all()
            }
            for source in self.sources:
                payload = source.model_dump(mode="json")
                config_hash = hashlib.sha256(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                record = records.get(source.id)
                if record is None:
                    record = SourceDefinitionRecord(
                        id=source.id,
                        created_at=now,
                        updated_at=now,
                        name=source.name,
                        owner_department=source.owner_department,
                        base_url=source.base_url,
                        allowed_hosts=source.allowed_hosts,
                        visibility=source.visibility,
                        authority_level=source.authority_level,
                        acquisition_methods=source.acquisition_methods,
                        connector_kind=source.connector.kind,
                        poll_interval_seconds=source.poll_interval_seconds,
                        rate_limit_per_minute=source.rate_limit_per_minute,
                        default_ttl_seconds=source.default_ttl_seconds,
                        live_required_for=source.live_required_for,
                        parser_profile=source.parser_profile,
                        snapshot_policy=source.snapshot_policy,
                        config_payload=payload,
                        config_hash=config_hash,
                        enabled=source.enabled,
                    )
                    session.add(record)
                    changed += 1
                    continue
                if record.config_hash != config_hash or record.enabled != source.enabled:
                    record.name = source.name
                    record.owner_department = source.owner_department
                    record.base_url = source.base_url
                    record.allowed_hosts = source.allowed_hosts
                    record.visibility = source.visibility
                    record.authority_level = source.authority_level
                    record.acquisition_methods = source.acquisition_methods
                    record.connector_kind = source.connector.kind
                    record.poll_interval_seconds = source.poll_interval_seconds
                    record.rate_limit_per_minute = source.rate_limit_per_minute
                    record.default_ttl_seconds = source.default_ttl_seconds
                    record.live_required_for = source.live_required_for
                    record.parser_profile = source.parser_profile
                    record.snapshot_policy = source.snapshot_policy
                    record.config_payload = payload
                    record.config_hash = config_hash
                    record.enabled = source.enabled
                    record.updated_at = now
                    changed += 1

            for source_id, record in records.items():
                if source_id not in configured_ids and record.enabled:
                    record.enabled = False
                    record.updated_at = now
                    changed += 1
            await session.commit()
        return changed


_CONTENT_SUFFIXES = frozenset(
    {
        ".html",
        ".htm",
        ".shtml",
        ".jsp",
        ".php",
        ".aspx",
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
    }
)


def is_campus_content_path(path: str) -> bool:
    """Accept read-only content/list pages on an already allowlisted official host.

    Campus CMS products routinely add columns and rewrite article URLs without notice.
    Exact connector patterns remain useful ranking and parser hints, but must not turn
    the source registry into a semantic gate that makes new official pages invisible.
    """

    normalized = (path or "/").casefold()
    if normalized in {"", "/"}:
        return True
    if any(
        segment in normalized
        for segment in (
            "/admin",
            "/manage",
            "/login",
            "/logout",
            "/cas/",
            "/oauth/",
            "/apply",
            "/submit",
            "/register",
        )
    ):
        return False
    leaf = normalized.rsplit("/", 1)[-1]
    if "." not in leaf:
        return True
    return any(normalized.endswith(suffix) for suffix in _CONTENT_SUFFIXES)
