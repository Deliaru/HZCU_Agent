import asyncio
import base64
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, Field

from hzcu_agent.models import new_id, utc_now
from hzcu_agent.schemas import Evidence, ToolError, ToolResult

SEARCH_URL = "https://www.hzcu.edu.cn/ssjg.jsp?wbtreeid=1001"
OFFICIAL_ROOT = "hzcu.edu.cn"
CONTENT_SELECTORS = (
    ".v_news_content",
    ".wp_articlecontent",
    "#vsb_content",
    ".article-content",
    ".news-content",
    ".content",
    "article",
)
DATE_PATTERN = re.compile(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?")


class OfficialSearchArguments(BaseModel):
    query: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=5, ge=1, le=8)


class _SearchHit(BaseModel):
    title: str
    url: str
    published_at: datetime | None = None
    search_excerpt: str = ""


def is_allowed_official_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port in (None, 443)
        and (host == OFFICIAL_ROOT or host.endswith(f".{OFFICIAL_ROOT}"))
    )


def normalize_official_url(url: str, base_url: str = SEARCH_URL) -> str | None:
    absolute = urljoin(base_url, url.strip())
    try:
        parsed = urlsplit(absolute)
    except ValueError:
        return None
    if parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
    normalized = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    return normalized if is_allowed_official_url(normalized) else None


def encode_resource_ref(url: str) -> str:
    token = base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")
    return f"hzcu-official:{token}"


def decode_resource_ref(resource_ref: str) -> str:
    prefix = "hzcu-official:"
    if not resource_ref.startswith(prefix):
        raise ValueError("Unsupported resource reference")
    token = resource_ref.removeprefix(prefix)
    token += "=" * (-len(token) % 4)
    url = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    if not is_allowed_official_url(url):
        raise ValueError("Resource reference is outside the official allowlist")
    return url


class HzcuOfficialSearchTool:
    name = "search_official_live"
    version = "1.0.0"

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=8.0),
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "HZCU-Campus-Agent/0.1 (+student information assistant; "
                    "official public pages only)"
                )
            },
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def run(self, arguments: OfficialSearchArguments, trace_id: str) -> ToolResult:
        query = arguments.query.strip()
        try:
            response = await self._request_official(
                "POST",
                SEARCH_URL,
                data={
                    "lucenenewssearchkey": base64.b64encode(query.encode("utf-8")).decode("ascii"),
                    "_lucenesearchtype": "1",
                    "searchScope": "0",
                    "s": "search",
                },
            )
            response.raise_for_status()
            hits = self._parse_search_results(response.text)
            hits = _rank_search_hits(query, hits)[: arguments.limit]
            evidence = await asyncio.gather(*(self._hydrate_hit(hit) for hit in hits))
            usable_evidence = [item for item in evidence if item is not None]
            warnings: list[str] = []
            if not usable_evidence:
                warnings.append("官网搜索未返回可读取的校内页面，请调整关键词后重试。")
            return ToolResult(
                tool=self.name,
                version=self.version,
                status="ok",
                data={"query": query, "result_count": len(usable_evidence)},
                evidence=usable_evidence,
                warnings=warnings,
                trace_id=trace_id,
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult(
                tool=self.name,
                version=self.version,
                status="error",
                error=ToolError(
                    code="OFFICIAL_SEARCH_UNAVAILABLE",
                    message="暂时无法完成校园官网实时检索。",
                    retryable=True,
                    details={"type": type(exc).__name__},
                ),
                trace_id=trace_id,
            )

    def _parse_search_results(self, html: str) -> list[_SearchHit]:
        soup = BeautifulSoup(html, "html.parser")
        hits: list[_SearchHit] = []
        seen_urls: set[str] = set()

        result_nodes = soup.select("ul.ul-txtq3 li")
        if not result_nodes:
            result_nodes = soup.select(".search-list li, .search-result li, li")

        for node in result_nodes:
            anchor = node.select_one("a.con[href], a[href]")
            if anchor is None:
                continue
            url = normalize_official_url(str(anchor.get("href", "")), SEARCH_URL)
            if url is None or url in seen_urls:
                continue
            title_node = node.select_one("h3") or anchor
            title = _clean_text(title_node.get_text(" ", strip=True))
            if not title:
                continue
            published_at = _parse_date(node.get_text(" ", strip=True))
            excerpt_node = node.select_one("p, .summary, .txt")
            excerpt = _clean_text(excerpt_node.get_text(" ", strip=True) if excerpt_node else "")
            hits.append(
                _SearchHit(
                    title=title,
                    url=url,
                    published_at=published_at,
                    search_excerpt=excerpt[:600],
                )
            )
            seen_urls.add(url)
        return hits

    async def _hydrate_hit(self, hit: _SearchHit) -> Evidence | None:
        if not is_allowed_official_url(hit.url):
            return None
        observed_at = utc_now()
        excerpt = hit.search_excerpt
        title = hit.title
        published_at = hit.published_at

        try:
            response = await self._request_official("GET", hit.url)
            response.raise_for_status()
            if not is_allowed_official_url(str(response.url)):
                return None
            soup = BeautifulSoup(response.text, "html.parser")
            for unwanted in soup.select("script, style, noscript, nav, header, footer"):
                unwanted.decompose()
            title_node = soup.select_one("h1, .article-title, .news-title")
            if title_node:
                title = _clean_text(title_node.get_text(" ", strip=True)) or title
            if published_at is None:
                published_at = _parse_date(soup.get_text(" ", strip=True))
            for selector in CONTENT_SELECTORS:
                content = soup.select_one(selector)
                if content:
                    candidate = _clean_text(content.get_text("\n", strip=True))
                    if len(candidate) >= 20:
                        excerpt = candidate[:1800]
                        break
        except (httpx.HTTPError, ValueError):
            if not excerpt:
                return None

        host = urlsplit(hit.url).hostname or OFFICIAL_ROOT
        return Evidence(
            evidence_id=new_id("ev"),
            title=title[:500],
            publisher=f"浙大城市学院官方站点（{host}）",
            canonical_url=hit.url,
            published_at=published_at,
            observed_at=observed_at,
            fresh_until=observed_at + timedelta(hours=6),
            excerpt=excerpt[:1800] or "该官方页面未提供可解析的正文摘要。",
            source_id=f"hzcu-official:{host}",
            resource_ref=encode_resource_ref(hit.url),
            authority_level="official",
            retrieval_mode="live_public",
        )

    async def _request_official(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> httpx.Response:
        current_url = normalize_official_url(url)
        if current_url is None:
            raise ValueError("URL is outside the official allowlist")
        current_method = method.upper()
        request_kwargs = dict(kwargs)

        for _ in range(4):
            response = await self._client.request(
                current_method,
                current_url,
                follow_redirects=False,
                **request_kwargs,
            )
            if not response.is_redirect:
                if not is_allowed_official_url(str(response.url)):
                    raise ValueError("Final response is outside the official allowlist")
                return response
            location = response.headers.get("location")
            redirected_url = normalize_official_url(location or "", current_url)
            if redirected_url is None:
                raise ValueError("Redirect is outside the official allowlist")
            if response.status_code in {301, 302, 303} and current_method != "HEAD":
                current_method = "GET"
                request_kwargs.pop("data", None)
            current_url = redirected_url
        raise ValueError("Too many redirects")


def _clean_text(value: str) -> str:
    compact = re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n{3,}", "\n\n", value)).strip()
    return re.sub(r"(?<=[\u3400-\u9fff]) +(?=[\u3400-\u9fff])", "", compact)


_SEARCH_STOP_TERMS = {
    "一般",
    "什么",
    "什么时候",
    "学校",
    "学生",
    "学院",
    "我们",
    "我想",
    "可以",
    "怎么",
    "如何",
    "相关",
    "关于",
    "通知",
    "查询",
}


def _rank_search_hits(query: str, hits: list[_SearchHit]) -> list[_SearchHit]:
    """Keep the official search engine's order unless topical evidence beats it."""

    terms = _search_terms(query)
    ranked = sorted(
        enumerate(hits),
        key=lambda item: (
            _search_relevance(item[1], query, terms),
            -item[0],
        ),
        reverse=True,
    )
    return [hit for _, hit in ranked]


def _search_terms(query: str) -> list[str]:
    normalized = _clean_text(query).lower()
    terms = set(re.findall(r"[a-z0-9]{2,}", normalized))
    for sequence in re.findall(r"[\u3400-\u9fff]+", normalized):
        if 2 <= len(sequence) <= 16 and sequence not in _SEARCH_STOP_TERMS:
            terms.add(sequence)
        for size in (2, 3, 4, 5, 6):
            for index in range(max(0, len(sequence) - size + 1)):
                term = sequence[index : index + size]
                if term not in _SEARCH_STOP_TERMS:
                    terms.add(term)
    return sorted(terms, key=lambda value: (-len(value), value))[:100]


def _search_relevance(hit: _SearchHit, query: str, terms: list[str]) -> float:
    title = _clean_text(hit.title).lower()
    excerpt = _clean_text(hit.search_excerpt).lower()
    normalized_query = _clean_text(query).lower()
    score = 0.0
    if normalized_query and normalized_query in title:
        score += 50.0
    elif normalized_query and normalized_query in excerpt:
        score += 15.0
    for term in terms:
        weight = min(8.0, float(len(term)))
        if term in title:
            score += 3.0 + weight * 2.0
        if term in excerpt:
            score += 1.0 + weight * 0.5
    return score


def _parse_date(value: str) -> datetime | None:
    match = DATE_PATTERN.search(value)
    if match is None:
        return None
    normalized = (
        match.group(0)
        .replace("年", "-")
        .replace("月", "-")
        .replace("日", "")
        .replace("/", "-")
        .replace(".", "-")
    )
    try:
        return (
            datetime.strptime(normalized, "%Y-%m-%d")
            .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            .astimezone(UTC)
        )
    except ValueError:
        return None
