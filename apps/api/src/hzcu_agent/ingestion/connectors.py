import asyncio
import hashlib
import json
import logging
import re
import time
from collections import defaultdict, deque
from datetime import UTC, datetime
from html import unescape
from typing import Protocol
from urllib.parse import parse_qs, parse_qsl, urlsplit

import httpx
from bs4 import BeautifulSoup

from hzcu_agent.ingestion.catalog import SourceConfig, is_campus_content_path
from hzcu_agent.ingestion.parsers import normalize_text, parse_date
from hzcu_agent.ingestion.security import (
    SourceUrlRejected,
    canonicalize_source_url,
)
from hzcu_agent.ingestion.types import DiscoveredResource, FetchPayload

USER_AGENT = (
    "HZCU-Campus-Agent/0.2 (public-information-indexer; contact the project maintainer for issues)"
)
logger = logging.getLogger(__name__)
LECTURE_SAFE_FIELDS = {
    "id",
    "name",
    "speakerName",
    "speakerJob",
    "speakerUnit",
    "speakerResume",
    "appliedUnitName",
    "type",
    "categoryName",
    "content",
    "address",
    "addressType",
    "startTime",
    "endTime",
    "registrationDeadline",
    "registrationNumber",
    "state",
    "poster",
    "scored",
    "insertTime",
    "updateTime",
}
CMS_SAFE_FIELDS = {
    "id",
    "title",
    "author",
    "authorOffice",
    "content",
    "status",
    "channelId",
    "type",
    "publicTime",
}
ARTICLE_CONTENT_SELECTORS = (
    "#zoom",
    ".bt_content",
    ".article_content",
    ".v_news_content",
    ".wp_articlecontent",
    "#vsb_content",
    ".edit-con",
    ".article-content",
    ".news-content",
    ".detail-content",
    ".content",
    "article",
    "main",
)
EMBEDDED_ARTICLE_SELECTORS = (
    "#zoom",
    ".bt_content",
    ".article_content",
    ".v_news_content",
    ".wp_articlecontent",
    "#vsb_content",
    ".article-content",
    ".news-content",
    ".detail-content",
)
LISTING_CONTENT_SELECTORS = (
    ".wp_article_list",
    ".column-news-list",
    ".article-list",
    ".news-list",
    ".list-content",
    ".list_con",
    ".right_list",
    ".ny_list",
    ".lm_list",
    ".lm_right",
    "main",
)
MIRRORED_ATTACHMENT_SUFFIXES = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".txt",
        ".csv",
        ".zip",
        ".rar",
        ".7z",
    }
)
MIRRORED_IMAGE_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".tif",
        ".tiff",
        ".bmp",
        ".svg",
    }
)


class SourceFetchError(RuntimeError):
    pass


class SourceConnector(Protocol):
    async def discover(
        self,
        source: SourceConfig,
        limit_override: int | None = None,
        *,
        full_scan: bool = False,
    ) -> list[DiscoveredResource]: ...

    async def fetch(
        self,
        source: SourceConfig,
        resource: DiscoveredResource,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchPayload: ...


class OperatorImportConnector:
    """A non-network connector for explicitly verified operator artifacts."""

    async def discover(
        self,
        source: SourceConfig,
        limit_override: int | None = None,
        *,
        full_scan: bool = False,
    ) -> list[DiscoveredResource]:
        del source, limit_override, full_scan
        return []

    async def fetch(
        self,
        source: SourceConfig,
        resource: DiscoveredResource,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchPayload:
        del source, resource, etag, last_modified
        raise SourceFetchError("Operator-import artifacts have no network fetch operation")


class SafeSourceFetcher:
    def __init__(
        self,
        *,
        max_download_bytes: int,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )
        self._max_download_bytes = max_download_bytes
        self._request_times: dict[str, deque[float]] = defaultdict(deque)
        self._rate_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def request(
        self,
        source: SourceConfig,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict | None = None,
        read_only_post: bool = False,
    ) -> FetchPayload:
        current_url = canonicalize_source_url(url, source)
        current_method = method.upper()
        if current_method not in {"GET", "HEAD"}:
            if current_method != "POST" or not read_only_post:
                raise SourceFetchError("Source fetcher permits only read-only requests")
            query_endpoints = {
                candidate
                for candidate in (
                    source.connector.list_endpoint,
                    source.connector.detail_endpoint,
                )
                if candidate
            }
            if current_url not in {
                canonicalize_source_url(candidate, source) for candidate in query_endpoints
            }:
                raise SourceFetchError("Read-only POST target is not a registered query endpoint")
        current_json = json_body
        request_headers = dict(headers or {})
        for _ in range(5):
            await self._apply_rate_limit(source)
            response = await self._request_with_retry(
                current_method,
                current_url,
                headers=request_headers,
                json_body=current_json,
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                redirected = canonicalize_source_url(
                    response.headers.get("location", ""),
                    source,
                    current_url,
                )
                if response.status_code in {301, 302, 303} and current_method != "HEAD":
                    current_method = "GET"
                    current_json = None
                current_url = redirected
                continue
            canonicalize_source_url(str(response.url), source)
            if len(response.content) > self._max_download_bytes:
                raise SourceFetchError("Source response exceeds configured size limit")
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
            return FetchPayload(
                status_code=response.status_code,
                body=response.content,
                media_type=media_type,
                final_url=str(response.url),
                etag=response.headers.get("etag"),
                last_modified=response.headers.get("last-modified"),
                encoding=response.encoding,
            )
        raise SourceFetchError("Too many source redirects")

    async def _request_with_retry(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: dict | None,
    ) -> httpx.Response:
        for attempt in range(3):
            try:
                return await self._client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    follow_redirects=False,
                )
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise SourceFetchError("Source transport failed after bounded retries") from exc
                await asyncio.sleep(0.25 * (2**attempt))
        raise SourceFetchError("Source transport retry invariant failed")

    async def _apply_rate_limit(self, source: SourceConfig) -> None:
        async with self._rate_locks[source.id]:
            history = self._request_times[source.id]
            now = time.monotonic()
            while history and now - history[0] >= 60:
                history.popleft()
            if len(history) >= source.rate_limit_per_minute:
                delay = 60 - (now - history[0])
                if delay > 0:
                    await asyncio.sleep(delay)
                now = time.monotonic()
                while history and now - history[0] >= 60:
                    history.popleft()
            history.append(time.monotonic())


class LinkedPageConnector:
    def __init__(self, fetcher: SafeSourceFetcher) -> None:
        self._fetcher = fetcher
        self._prefetched: dict[str, FetchPayload] = {}

    async def discover(
        self,
        source: SourceConfig,
        limit_override: int | None = None,
        *,
        full_scan: bool = False,
    ) -> list[DiscoveredResource]:
        if full_scan:
            return await self._discover_full(source)
        configured_limit = source.connector.max_resources_per_run
        limit = min(configured_limit, limit_override or configured_limit)
        discovery_scan_limit = max(limit * 4, limit + 20)
        patterns = [re.compile(value) for value in source.connector.include_patterns]
        per_seed_resources: list[list[DiscoveredResource]] = []
        successful_seeds = 0
        failed_seeds = 0

        for seed_url in source.connector.seed_urls:
            seed_url = canonicalize_source_url(seed_url, source)
            try:
                payload = await self._fetcher.request(source, "GET", seed_url)
            except SourceFetchError:
                failed_seeds += 1
                logger.warning(
                    "Discovery seed request failed",
                    extra={
                        "event": "source.discovery.seed_failed",
                        "source_id": source.id,
                    },
                )
                continue
            if payload.status_code >= 400:
                failed_seeds += 1
                logger.warning(
                    "Discovery seed returned an error response",
                    extra={
                        "event": "source.discovery.seed_failed",
                        "source_id": source.id,
                        "status_code": payload.status_code,
                    },
                )
                continue
            successful_seeds += 1
            seed_resources: list[DiscoveredResource] = []
            seed_seen: set[str] = set()
            if source.connector.include_seed:
                seed_resources.append(
                    DiscoveredResource(
                        canonical_uri=seed_url,
                        fetch_uri=seed_url,
                        resource_type="html",
                        title_hint=source.name,
                        metadata={"is_index": True},
                    )
                )
                seed_seen.add(seed_url)
            soup = BeautifulSoup(
                payload.body,
                "html.parser",
                from_encoding=payload.encoding,
            )
            for anchor in soup.select("a[href]"):
                raw_href = str(anchor.get("href", "")).strip()
                if not raw_href or raw_href.lower().startswith(("javascript:", "mailto:")):
                    continue
                title = _anchor_title(anchor)
                context = normalize_text(
                    anchor.parent.get_text(" ", strip=True) if anchor.parent is not None else title
                )
                if _try_add_discovered(
                    seed_resources,
                    seed_seen,
                    patterns,
                    source,
                    seed_url,
                    raw_href,
                    title=title,
                    context=context,
                    limit=discovery_scan_limit,
                ):
                    break
            # Dahan / legacy CMS often embeds list links inside JS/CDATA with escaped quotes.
            if len(seed_resources) < discovery_scan_limit:
                for raw_href, title in _extract_embedded_links(payload.body, payload.encoding):
                    if _try_add_discovered(
                        seed_resources,
                        seed_seen,
                        patterns,
                        source,
                        seed_url,
                        raw_href,
                        title=title,
                        context=title,
                        limit=discovery_scan_limit,
                    ):
                        break
            per_seed_resources.append(_soft_prioritize_discovered(seed_resources, limit))
        if successful_seeds == 0 and failed_seeds:
            raise SourceFetchError(f"All {failed_seeds} discovery seeds failed")
        return _round_robin_resources(per_seed_resources, limit)

    async def _discover_full(
        self,
        source: SourceConfig,
    ) -> list[DiscoveredResource]:
        """Exhaust every reachable registered list/pagination frontier.

        Full mode is structural, not query-driven: every readable list page,
        detail page, article attachment and article image on the exact
        allowlisted host is mirrored. Detail pages are not used as a new site
        navigation frontier, which prevents menus from turning one registered
        notice source into an unrelated whole-domain crawl.
        """

        patterns = [re.compile(value, re.I) for value in source.connector.include_patterns]
        earliest_published_at = datetime(
            source.connector.earliest_published_year,
            1,
            1,
            tzinfo=UTC,
        )
        frontier: deque[tuple[str, str, str, str]] = deque()
        queued: set[str] = set()
        visited: set[str] = set()
        resources: dict[str, DiscoveredResource] = {}
        seed_urls = {
            canonicalize_source_url(seed_url, source) for seed_url in source.connector.seed_urls
        }
        for seed_url in seed_urls:
            frontier.append((seed_url, "listing", source.name, ""))
            queued.add(seed_url)

        successful_seeds = 0
        failed_seeds = 0
        while frontier:
            page_url, page_kind, title_hint, parent_url = frontier.popleft()
            if page_url in visited:
                continue
            visited.add(page_url)
            try:
                payload = await self._fetcher.request(source, "GET", page_url)
            except SourceFetchError:
                if page_url in seed_urls:
                    failed_seeds += 1
                logger.warning(
                    "Full discovery page request failed",
                    extra={
                        "event": "source.discovery.page_failed",
                        "source_id": source.id,
                        "page_kind": page_kind,
                    },
                )
                continue
            if payload.status_code >= 400:
                if page_url in seed_urls:
                    failed_seeds += 1
                logger.warning(
                    "Full discovery page returned an error response",
                    extra={
                        "event": "source.discovery.page_failed",
                        "source_id": source.id,
                        "page_kind": page_kind,
                        "status_code": payload.status_code,
                    },
                )
                continue
            if page_url in seed_urls:
                successful_seeds += 1
            self._prefetched[page_url] = payload
            soup = BeautifulSoup(
                payload.body,
                "html.parser",
                from_encoding=payload.encoding,
            )
            effective_page_kind = (
                "detail" if page_kind == "listing" and _has_embedded_article(soup) else page_kind
            )
            page_title = _document_title(soup, title_hint or source.name)
            page_context = normalize_text(soup.get_text(" ", strip=True))[:1500]
            page_published_at = parse_date(page_context)
            if page_published_at is not None and page_published_at < earliest_published_at:
                continue
            if effective_page_kind != "listing" or source.connector.mirror_listing_pages:
                resources[page_url] = DiscoveredResource(
                    canonical_uri=page_url,
                    fetch_uri=page_url,
                    resource_type="html",
                    title_hint=page_title,
                    published_hint=page_published_at,
                    external_id=_external_id(page_url),
                    metadata={
                        "full_scan": True,
                        "is_index": effective_page_kind == "listing",
                        "parent_uri": parent_url,
                        "pattern_hint": any(pattern.search(page_url) for pattern in patterns),
                    },
                )

            roots = (
                _listing_roots(soup) if effective_page_kind == "listing" else _article_roots(soup)
            )
            listing_root_is_scoped = not (len(roots) == 1 and roots[0] is soup)
            anchors = [anchor for root in roots for anchor in root.select("a[href]")]
            embedded_links = (
                _extract_embedded_links(payload.body, payload.encoding)
                if effective_page_kind == "listing"
                else []
            )
            link_candidates = [
                (
                    str(anchor.get("href", "")).strip(),
                    _anchor_title(anchor),
                    normalize_text(
                        anchor.parent.get_text(" ", strip=True)
                        if anchor.parent is not None
                        else anchor.get_text(" ", strip=True)
                    ),
                )
                for anchor in anchors
            ]
            # The regex fallback sees ordinary anchors too, but loses the nearby
            # date context used by the temporal cutoff. Only add genuinely new
            # embedded/escaped links so an undated duplicate cannot resurrect a
            # detail link already excluded by its visible publication date.
            anchor_hrefs = {raw_href for raw_href, _, _ in link_candidates}
            link_candidates.extend(
                (href, title, title) for href, title in embedded_links if href not in anchor_hrefs
            )
            for raw_href, title, context in link_candidates:
                candidate = _full_scan_candidate(
                    source,
                    page_url,
                    raw_href,
                    include_patterns=patterns,
                )
                if candidate is None:
                    continue
                resource_type = _resource_type(candidate, title)
                pattern_hint = any(pattern.search(candidate) for pattern in patterns)
                published_hint = parse_date(context)
                if published_hint is not None and published_hint < earliest_published_at:
                    continue
                if resource_type == "html":
                    if effective_page_kind != "listing":
                        continue
                    looks_like_listing = _is_listing_or_pagination(
                        candidate,
                        page_url,
                        seed_urls,
                    )
                    same_listing_scope = (
                        source.connector.follow_site_navigation
                        or _same_listing_scope(
                            candidate,
                            page_url,
                            seed_urls,
                        )
                    )
                    if looks_like_listing and same_listing_scope:
                        candidate_kind = "listing"
                    elif pattern_hint:
                        candidate_kind = "detail"
                    elif looks_like_listing:
                        continue
                    else:
                        candidate_kind = "detail"
                    if (
                        candidate_kind == "detail"
                        and not listing_root_is_scoped
                        and not pattern_hint
                    ):
                        continue
                    if candidate not in queued and candidate not in visited:
                        frontier.append(
                            (
                                candidate,
                                candidate_kind,
                                title or candidate,
                                page_url,
                            )
                        )
                        queued.add(candidate)
                    continue
                resources.setdefault(
                    candidate,
                    DiscoveredResource(
                        canonical_uri=candidate,
                        fetch_uri=candidate,
                        resource_type=resource_type,
                        title_hint=(title or candidate)[:500],
                        published_hint=(
                            published_hint if resource_type == "html" else published_hint
                        ),
                        external_id=_external_id(candidate),
                        metadata={
                            "full_scan": True,
                            "parent_uri": page_url,
                            "pattern_hint": pattern_hint,
                        },
                    ),
                )

            if effective_page_kind == "detail":
                for root in roots:
                    for image in root.select("img"):
                        raw_src = next(
                            (
                                str(image.get(attribute, "")).strip()
                                for attribute in ("src", "data-src", "data-original")
                                if image.get(attribute)
                            ),
                            "",
                        )
                        candidate = _full_scan_candidate(
                            source,
                            page_url,
                            raw_src,
                            image=True,
                        )
                        if candidate is None:
                            continue
                        image_title = normalize_text(
                            str(image.get("alt") or image.get("title") or page_title)
                        )
                        resources.setdefault(
                            candidate,
                            DiscoveredResource(
                                canonical_uri=candidate,
                                fetch_uri=candidate,
                                resource_type="image",
                                title_hint=(image_title or page_title)[:500],
                                external_id=_external_id(candidate),
                                metadata={
                                    "full_scan": True,
                                    "parent_uri": page_url,
                                    "article_image": True,
                                },
                            ),
                        )

        if successful_seeds == 0 and failed_seeds:
            raise SourceFetchError(f"All {failed_seeds} discovery seeds failed")
        return list(resources.values())

    async def fetch(
        self,
        source: SourceConfig,
        resource: DiscoveredResource,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchPayload:
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        fetch_uri = resource.fetch_uri
        cached = self._prefetched.pop(fetch_uri, None)
        if cached is not None:
            return cached
        payload = await self._fetcher.request(
            source,
            "GET",
            fetch_uri,
            headers=headers,
        )
        if payload.status_code != 304 and payload.status_code >= 400:
            raise SourceFetchError(f"Resource returned HTTP {payload.status_code}")
        # Campus Dahan permission columns return a 200 shell with JS location.href.
        redirected = _js_location_href(payload.body, payload.encoding)
        if redirected and len(payload.body) < 4096:
            try:
                next_url = canonicalize_source_url(redirected, source, fetch_uri)
            except SourceUrlRejected:
                return payload
            if next_url != canonicalize_source_url(fetch_uri, source):
                payload = await self._fetcher.request(source, "GET", next_url)
                if payload.status_code != 304 and payload.status_code >= 400:
                    raise SourceFetchError(f"Resource returned HTTP {payload.status_code}")
        return payload


class LectureApiConnector:
    def __init__(self, fetcher: SafeSourceFetcher) -> None:
        self._fetcher = fetcher

    async def discover(
        self,
        source: SourceConfig,
        limit_override: int | None = None,
        *,
        full_scan: bool = False,
    ) -> list[DiscoveredResource]:
        configured_limit = source.connector.max_resources_per_run
        limit = None if full_scan else min(configured_limit, limit_override or configured_limit)
        page_size = (
            source.connector.page_size
            if limit is None
            else min(
                source.connector.page_size,
                limit,
            )
        )
        observed_at = datetime.now(UTC)
        resources: list[DiscoveredResource] = []
        seen: set[str] = set()
        page_number = 1
        while limit is None or len(resources) < limit:
            payload = await self._fetcher.request(
                source,
                "POST",
                source.connector.list_endpoint or "",
                headers=_lecture_headers(),
                json_body={"pageNum": page_number, "pageSize": page_size},
                read_only_post=True,
            )
            if payload.status_code >= 400:
                raise SourceFetchError(f"Lecture list returned HTTP {payload.status_code}")
            try:
                response = json.loads(payload.body)
                items = response["data"]["data"]
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise SourceFetchError("Lecture list response is invalid") from exc
            if not isinstance(items, list):
                raise SourceFetchError("Lecture list data is invalid")
            added = 0
            page_event_times: list[datetime] = []
            for item in items:
                if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
                    continue
                event_ends_at = _lecture_event_ends_at(item)
                if event_ends_at is not None:
                    page_event_times.append(event_ends_at)
                    if event_ends_at < observed_at:
                        continue
                external_id = str(item["id"])
                if external_id in seen:
                    continue
                public_uri = (source.connector.public_detail_template or "").format(id=external_id)
                canonical_uri = canonicalize_source_url(public_uri, source)
                safe_metadata = {key: item[key] for key in LECTURE_SAFE_FIELDS if key in item}
                resources.append(
                    DiscoveredResource(
                        canonical_uri=canonical_uri,
                        fetch_uri=source.connector.detail_endpoint or "",
                        resource_type="lecture",
                        title_hint=normalize_text(str(item["name"]))[:500],
                        published_hint=_epoch_datetime(item.get("insertTime")),
                        external_id=external_id,
                        metadata=safe_metadata,
                    )
                )
                seen.add(external_id)
                added += 1
                if limit is not None and len(resources) >= limit:
                    break
            page_is_past = bool(page_event_times) and max(page_event_times) < observed_at
            if len(items) < page_size or added == 0 or page_is_past:
                break
            page_number += 1
        return resources

    async def fetch(
        self,
        source: SourceConfig,
        resource: DiscoveredResource,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchPayload:
        del last_modified
        payload = await self._fetcher.request(
            source,
            "POST",
            resource.fetch_uri,
            headers=_lecture_headers(),
            json_body={"id": resource.external_id},
            read_only_post=True,
        )
        if payload.status_code >= 400:
            raise SourceFetchError(f"Lecture detail returned HTTP {payload.status_code}")
        try:
            response = json.loads(payload.body)
            item = response["data"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise SourceFetchError("Lecture detail response is invalid") from exc
        if not isinstance(item, dict):
            raise SourceFetchError("Lecture detail data is invalid")
        safe_item = {key: item[key] for key in LECTURE_SAFE_FIELDS if key in item}
        sanitized = json.dumps(
            {"code": 200, "data": safe_item},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        synthetic_etag = f'"safe-{hashlib.sha256(sanitized).hexdigest()}"'
        status_code = 304 if etag == synthetic_etag else 200
        return FetchPayload(
            status_code=status_code,
            body=b"" if status_code == 304 else sanitized,
            media_type="application/vnd.hzcu.lecture+json",
            final_url=resource.canonical_uri,
            etag=synthetic_etag,
            encoding="utf-8",
        )


class CmsApiConnector:
    def __init__(self, fetcher: SafeSourceFetcher) -> None:
        self._fetcher = fetcher

    async def discover(
        self,
        source: SourceConfig,
        limit_override: int | None = None,
        *,
        full_scan: bool = False,
    ) -> list[DiscoveredResource]:
        configured_limit = source.connector.max_resources_per_run
        limit = None if full_scan else min(configured_limit, limit_override or configured_limit)
        per_channel_resources: list[list[DiscoveredResource]] = []
        successful_channels = 0
        failed_channels = 0
        for channel in source.connector.channels:
            page_size = (
                source.connector.page_size
                if limit is None
                else min(
                    source.connector.page_size,
                    limit,
                )
            )
            channel_resources: list[DiscoveredResource] = []
            channel_seen: set[str] = set()
            page_number = 0
            channel_succeeded = False
            earliest_published_at = datetime(
                source.connector.earliest_published_year,
                1,
                1,
                tzinfo=UTC,
            )
            while limit is None or len(channel_resources) < limit:
                try:
                    payload = await self._fetcher.request(
                        source,
                        "POST",
                        source.connector.list_endpoint or "",
                        headers=_public_api_headers(),
                        json_body={
                            "code": channel.code,
                            "channelId": channel.channel_id,
                            "pageNum": page_number,
                            "pageSize": page_size,
                        },
                        read_only_post=True,
                    )
                except SourceFetchError:
                    break
                if payload.status_code >= 400:
                    break
                try:
                    response = json.loads(payload.body)
                    items = response["data"]["data"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    break
                if not isinstance(items, list):
                    break
                channel_succeeded = True
                added = 0
                page_published_times: list[datetime] = []
                for item in items:
                    if not isinstance(item, dict) or not item.get("id") or not item.get("title"):
                        continue
                    external_id = str(item["id"])
                    if external_id in channel_seen:
                        continue
                    published_at = _epoch_datetime(item.get("publicTime"))
                    if published_at is not None:
                        page_published_times.append(published_at)
                        if published_at < earliest_published_at:
                            continue
                    public_uri = (source.connector.public_detail_template or "").format(
                        channel_code=channel.code,
                        id=external_id,
                    )
                    canonical_uri = canonicalize_source_url(public_uri, source)
                    safe_metadata = {key: item[key] for key in CMS_SAFE_FIELDS if key in item}
                    safe_metadata["channelCode"] = channel.code
                    channel_resources.append(
                        DiscoveredResource(
                            canonical_uri=canonical_uri,
                            fetch_uri=source.connector.list_endpoint or "",
                            resource_type="cms_message",
                            title_hint=normalize_text(str(item["title"]))[:500],
                            published_hint=published_at,
                            external_id=external_id,
                            metadata=safe_metadata,
                        )
                    )
                    channel_seen.add(external_id)
                    added += 1
                    if limit is not None and len(channel_resources) >= limit:
                        break
                page_is_before_cutoff = (
                    bool(page_published_times) and max(page_published_times) < earliest_published_at
                )
                if len(items) < page_size or added == 0 or page_is_before_cutoff:
                    break
                page_number += 1
            if not channel_succeeded:
                failed_channels += 1
                logger.warning(
                    "CMS discovery channel failed",
                    extra={
                        "event": "source.discovery.channel_failed",
                        "source_id": source.id,
                    },
                )
                continue
            successful_channels += 1
            per_channel_resources.append(channel_resources)
        if successful_channels == 0 and failed_channels:
            raise SourceFetchError(f"All {failed_channels} CMS discovery channels failed")
        if limit is None:
            return _round_robin_resources(
                per_channel_resources,
                sum(len(group) for group in per_channel_resources),
            )
        return _round_robin_resources(per_channel_resources, limit)

    async def fetch(
        self,
        source: SourceConfig,
        resource: DiscoveredResource,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> FetchPayload:
        del source, last_modified
        safe_item = {
            key: resource.metadata[key]
            for key in (*CMS_SAFE_FIELDS, "channelCode")
            if key in resource.metadata
        }
        sanitized = json.dumps(
            {"code": 200, "data": safe_item},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        synthetic_etag = f'"safe-{hashlib.sha256(sanitized).hexdigest()}"'
        status_code = 304 if etag == synthetic_etag else 200
        return FetchPayload(
            status_code=status_code,
            body=b"" if status_code == 304 else sanitized,
            media_type="application/vnd.hzcu.cms-message+json",
            final_url=resource.canonical_uri,
            etag=synthetic_etag,
            encoding="utf-8",
        )


def build_connector(source: SourceConfig, fetcher: SafeSourceFetcher) -> SourceConnector:
    if source.connector.kind == "operator_import":
        return OperatorImportConnector()
    if source.connector.kind == "linked_html":
        return LinkedPageConnector(fetcher)
    if source.connector.kind == "lecture_api":
        return LectureApiConnector(fetcher)
    if source.connector.kind == "cms_api":
        return CmsApiConnector(fetcher)
    raise SourceFetchError(f"Unsupported connector kind: {source.connector.kind}")


def _document_title(soup: BeautifulSoup, fallback: str) -> str:
    for selector in (
        "h1",
        ".article-title",
        ".news-title",
        ".top .tit",
        ".tc-tit .d1",
        ".detail-title",
        "title",
    ):
        node = soup.select_one(selector)
        candidate = normalize_text(
            unescape(node.get_text(" ", strip=True)) if node is not None else ""
        )
        if 2 <= len(candidate) <= 500:
            return candidate[:500]
    return normalize_text(fallback)[:500]


def _article_roots(soup: BeautifulSoup) -> list:
    roots = [
        node
        for selector in ARTICLE_CONTENT_SELECTORS
        if (node := soup.select_one(selector)) is not None
    ]
    if roots:
        return roots
    body = soup.body
    return [body] if body is not None else [soup]


def _listing_roots(soup: BeautifulSoup) -> list:
    for selector in LISTING_CONTENT_SELECTORS:
        roots = [node for node in soup.select(selector) if node.select_one("a[href]") is not None]
        if roots:
            return roots
    return [soup]


def _has_embedded_article(soup: BeautifulSoup) -> bool:
    for selector in EMBEDDED_ARTICLE_SELECTORS:
        node = soup.select_one(selector)
        if node is None:
            continue
        if (
            len(normalize_text(node.get_text(" ", strip=True))) >= 20
            or node.select_one("img, a[href]") is not None
        ):
            return True
    return False


def _full_scan_candidate(
    source: SourceConfig,
    page_url: str,
    raw_url: str,
    *,
    image: bool = False,
    include_patterns: list[re.Pattern[str]] | None = None,
) -> str | None:
    if not raw_url or raw_url.lower().startswith(("javascript:", "mailto:", "tel:", "data:", "#")):
        return None
    try:
        candidate = canonicalize_source_url(raw_url, source, page_url)
    except SourceUrlRejected:
        return None
    path = urlsplit(candidate).path.casefold()
    if image and any(
        marker in path
        for marker in (
            "/_visitcount",
            "/visitcount/",
            "/visit/count",
        )
    ):
        return None
    if image:
        return candidate
    if include_patterns and any(pattern.search(candidate) for pattern in include_patterns):
        return candidate
    if is_campus_content_path(path):
        return candidate
    suffix = _path_suffix(path)
    if suffix in MIRRORED_ATTACHMENT_SUFFIXES | MIRRORED_IMAGE_SUFFIXES:
        return candidate
    return None


def _resource_type(url: str, title: str = "") -> str:
    parsed = urlsplit(url)
    path = parsed.path.casefold()
    suffix = _path_suffix(path)
    title_suffix = _path_suffix(title.casefold())
    known_suffixes = MIRRORED_ATTACHMENT_SUFFIXES | MIRRORED_IMAGE_SUFFIXES
    query_suffix = next(
        (
            candidate
            for key in ("filename", "file", "name")
            for value in parse_qs(parsed.query).get(key, [])
            if (candidate := _path_suffix(value.casefold())) in known_suffixes
        ),
        "",
    )
    resolved_suffix = suffix if suffix in known_suffixes else query_suffix or title_suffix
    if resolved_suffix == ".pdf":
        return "pdf"
    if resolved_suffix in MIRRORED_IMAGE_SUFFIXES:
        return "image"
    if (
        resolved_suffix in MIRRORED_ATTACHMENT_SUFFIXES
        or "download" in path
        or "attachment" in path
    ):
        return "attachment"
    return "html"


def _path_suffix(value: str) -> str:
    leaf = value.rsplit("/", 1)[-1].split("?", 1)[0]
    if "." not in leaf:
        return ""
    return "." + leaf.rsplit(".", 1)[-1]


def _is_listing_or_pagination(
    candidate: str,
    current_url: str,
    seed_urls: set[str],
) -> bool:
    if candidate in seed_urls:
        return True
    parsed = urlsplit(candidate)
    path = parsed.path.casefold()
    query = parsed.query.casefold()
    current = urlsplit(current_url)
    if (
        parsed.path == current.path
        and query != current.query.casefold()
        and any(
            marker in query
            for marker in (
                "p=",
                "page=",
                "pageno=",
                "pageindex=",
                "page_num=",
            )
        )
    ):
        return True
    return (
        "/col/col" in path
        or "/module/web/jpage/dataproxy.jsp" in path
        or "permissionunit.jsp" in path
        or "a=tlist" in query
        or "catalog_id=" in query
        or bool(
            re.search(
                r"/(?:list|index)(?:[_-]?\d+)?\.(?:s?html?|aspx|php|jsp)$",
                path,
            )
        )
    )


def _same_listing_scope(
    candidate: str,
    current_url: str,
    seed_urls: set[str],
) -> bool:
    candidate_key = _listing_scope_key(candidate)
    current_key = _listing_scope_key(current_url)
    if candidate_key == current_key:
        return True
    for seed_url in seed_urls:
        seed = urlsplit(seed_url)
        if seed.path in {"", "/"} and current_url == seed_url:
            return True
        if candidate_key == _listing_scope_key(seed_url):
            return True
    return False


def _listing_scope_key(url: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    parsed = urlsplit(url)
    path = re.sub(
        r"/(?P<name>index|list)[_-]\d+(?P<suffix>\.(?:s?html?|aspx|php|jsp))$",
        r"/\g<name>\g<suffix>",
        parsed.path.casefold(),
    )
    page_keys = {
        "p",
        "page",
        "pageno",
        "pageindex",
        "page_num",
        "pagenum",
        "currentpage",
    }
    stable_query = tuple(
        sorted(
            (key.casefold(), value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in page_keys
        )
    )
    return path, stable_query


def _external_id(url: str) -> str | None:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    for key in ("NewsNo", "id", "ID", "object_id", "i_artid"):
        if query.get(key):
            return query[key][0][:200]
    path_id = re.search(r"([0-9]{3,}|[a-f0-9]{20,})(?:\.[a-z]+)?$", parsed.path)
    return path_id.group(1)[:200] if path_id else None


def _round_robin_resources(
    groups: list[list[DiscoveredResource]],
    limit: int,
) -> list[DiscoveredResource]:
    """Select fairly across seeds/channels while preserving each local order."""
    selected: list[DiscoveredResource] = []
    seen: set[str] = set()
    queues = [deque(group) for group in groups if group]
    while queues and len(selected) < limit:
        remaining: list[deque[DiscoveredResource]] = []
        for queue in queues:
            while queue:
                resource = queue.popleft()
                if resource.canonical_uri in seen:
                    continue
                selected.append(resource)
                seen.add(resource.canonical_uri)
                break
            if queue:
                remaining.append(queue)
            if len(selected) >= limit:
                break
        queues = remaining
    return selected


def _try_add_discovered(
    discovered: list[DiscoveredResource],
    seen: set[str],
    patterns: list[re.Pattern[str]],
    source: SourceConfig,
    seed_url: str,
    raw_href: str,
    *,
    title: str,
    context: str,
    limit: int,
) -> bool:
    """Append one discovered resource. Returns True when the limit is reached."""
    if not raw_href or raw_href.lower().startswith(("javascript:", "mailto:", "#")):
        return False
    try:
        candidate = canonicalize_source_url(raw_href, source, seed_url)
    except SourceUrlRejected:
        return False
    if candidate in seen or not is_campus_content_path(urlsplit(candidate).path):
        return False
    pattern_hint = any(pattern.search(candidate) for pattern in patterns)
    path = urlsplit(candidate).path.lower()
    resource_type = (
        "pdf"
        if path.endswith(".pdf") or "download" in path or title.lower().endswith(".pdf")
        else "html"
    )
    discovered.append(
        DiscoveredResource(
            canonical_uri=candidate,
            fetch_uri=candidate,
            resource_type=resource_type,
            title_hint=(title or candidate)[:500],
            published_hint=parse_date(context),
            external_id=_external_id(candidate),
            metadata={"pattern_hint": pattern_hint},
        )
    )
    seen.add(candidate)
    return len(discovered) >= limit


def _soft_prioritize_discovered(
    resources: list[DiscoveredResource],
    limit: int,
) -> list[DiscoveredResource]:
    """Keep structural patterns as hints while preserving open-world discovery."""

    pinned = [item for item in resources if item.metadata.get("is_index")]
    hinted = [
        item
        for item in resources
        if not item.metadata.get("is_index") and item.metadata.get("pattern_hint")
    ]
    open_world = [
        item
        for item in resources
        if not item.metadata.get("is_index") and not item.metadata.get("pattern_hint")
    ]
    remaining = max(0, limit - len(pinned))
    return [*pinned, *_round_robin_resources([hinted, open_world], remaining)][:limit]


def _extract_embedded_links(
    body: bytes,
    encoding: str | None,
) -> list[tuple[str, str]]:
    """Pull href/title pairs from HTML and JS-escaped campus CMS payloads."""
    text = body.decode(encoding or "utf-8", errors="replace")
    text = text.replace('\\"', '"').replace("\\'", "'").replace("\\/", "/")
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(
        r"""href=["']([^"']+)["'][^>]*(?:title=["']([^"']*)["'])?""",
        text,
        flags=re.I,
    ):
        href = match.group(1).strip()
        title = normalize_text(match.group(2) or "")
        if href in seen:
            continue
        seen.add(href)
        pairs.append((href, title))
    # title-first variant common in Dahan record CDATA
    for match in re.finditer(
        r"""title=["']([^"']+)["'][^>]*href=["']([^"']+)["']""",
        text,
        flags=re.I,
    ):
        title = normalize_text(match.group(1))
        href = match.group(2).strip()
        if href in seen:
            continue
        seen.add(href)
        pairs.append((href, title))
    return pairs


def _js_location_href(body: bytes, encoding: str | None) -> str | None:
    text = body.decode(encoding or "utf-8", errors="replace")
    match = re.search(
        r"""(?:window\.)?location(?:\.href)?\s*=\s*['"]([^'"]+)['"]""",
        text,
        flags=re.I,
    )
    return match.group(1).strip() if match else None


def _anchor_title(anchor) -> str:
    title_node = anchor.select_one("h1, h2, h3, h4, .d1, .tit, .title")
    title = normalize_text(
        title_node.get_text(" ", strip=True)
        if title_node is not None
        else anchor.get_text(" ", strip=True)
    )
    if not title:
        image = anchor.select_one("img")
        title = normalize_text(
            str(image.get("title") or image.get("alt") or "") if image is not None else ""
        )
    title = re.sub(r"^\d{1,2}\s+20\d{2}[./-]\d{1,2}\s+", "", title)
    title = re.sub(r"\s*请点击查看详情\s*$", "", title)
    return title[:240]


def _epoch_datetime(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _lecture_event_ends_at(item: dict) -> datetime | None:
    """Return the best available event boundary for excluding expired lectures."""

    for key in ("endTime", "startTime", "registrationDeadline"):
        if timestamp := _epoch_datetime(item.get(key)):
            return timestamp
    return None


def _lecture_headers() -> dict[str, str]:
    return _public_api_headers() | {"lang": "zh-CN"}


def _public_api_headers() -> dict[str, str]:
    return {
        "App-Code": "6",
        "Content-Type": "application/json",
        "platform": "WEB",
    }
