import hashlib
import io
import json
import logging
import re
import unicodedata
import zipfile
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from pypdf import PdfReader

from hzcu_agent.ingestion.catalog import SourceConfig
from hzcu_agent.ingestion.types import (
    DiscoveredResource,
    FetchPayload,
    ParsedDocument,
)

logging.getLogger("pypdf").setLevel(logging.ERROR)

DATE_PATTERN = re.compile(r"20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?")
REVERSED_DATE_PATTERN = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})\s+(?P<year>20\d{2})[./年](?P<month>\d{1,2})"
)
CONTENT_SELECTORS = (
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
MAX_NORMALIZED_CHARACTERS = 600_000


class DocumentParseError(ValueError):
    pass


def normalize_text(value: str) -> str:
    # PDF text extractors can surface isolated UTF-16 surrogate code points.
    # Replace those invalid scalar values before hashing/indexing as UTF-8.
    utf8_safe = value.encode("utf-8", errors="replace").decode("utf-8")
    normalized = unicodedata.normalize("NFKC", utf8_safe).replace("\u200b", "")
    normalized = re.sub(r"[ \t\r\f\v]+", " ", normalized)
    normalized = re.sub(r"\n[ \t]+", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized).strip()
    return re.sub(r"(?<=[\u3400-\u9fff]) +(?=[\u3400-\u9fff])", "", normalized)


def parse_date(value: str) -> datetime | None:
    match = DATE_PATTERN.search(value)
    if match is not None:
        normalized = (
            match.group(0)
            .replace("年", "-")
            .replace("月", "-")
            .replace("日", "")
            .replace("/", "-")
            .replace(".", "-")
        )
    else:
        reversed_match = REVERSED_DATE_PATTERN.search(value)
        if reversed_match is None:
            return None
        normalized = (
            f"{reversed_match.group('year')}-{reversed_match.group('month')}-"
            f"{reversed_match.group('day')}"
        )
    try:
        return (
            datetime.strptime(normalized, "%Y-%m-%d")
            .replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            .astimezone(UTC)
        )
    except ValueError:
        return None


def parse_document(
    source: SourceConfig,
    resource: DiscoveredResource,
    payload: FetchPayload,
) -> ParsedDocument:
    media_type = payload.media_type.lower()
    suffix = _resource_suffix(resource)
    if payload.body.startswith(b"%PDF") or "application/pdf" in media_type:
        return _parse_pdf(source, resource, payload)
    if suffix in {".docx", ".xlsx", ".pptx"}:
        return _parse_openxml(source, resource, payload, suffix=suffix)
    if suffix in {".txt", ".csv"} or media_type.startswith("text/plain"):
        return _parse_text_attachment(source, resource, payload)
    if resource.resource_type in {"attachment", "image"} or media_type.startswith("image/"):
        return _parse_mirrored_binary(source, resource, payload)
    if source.parser_profile == "lecture_json":
        return _parse_lecture(source, resource, payload)
    if source.parser_profile == "cms_message_json":
        return _parse_cms_message(source, resource, payload)
    return _parse_html(source, resource, payload)


def _resource_suffix(resource: DiscoveredResource) -> str:
    parsed = urlsplit(resource.canonical_uri)
    path_suffix = PurePosixPath(parsed.path).suffix.casefold()
    supported = {".docx", ".xlsx", ".pptx", ".txt", ".csv"}
    if path_suffix in supported:
        return path_suffix
    query_suffix = next(
        (
            suffix
            for key in ("filename", "file", "name")
            for value in parse_qs(parsed.query).get(key, [])
            if (suffix := PurePosixPath(value).suffix.casefold()) in supported
        ),
        "",
    )
    if query_suffix:
        return query_suffix
    title_suffix = PurePosixPath(resource.title_hint).suffix.casefold()
    return title_suffix if title_suffix in supported else path_suffix


def expected_parser_version(
    source: SourceConfig,
    resource_type: str,
) -> str:
    if source.parser_profile == "lecture_json":
        return "lecture-json-safe-v1"
    if source.parser_profile == "cms_message_json":
        return "cms-message-safe-v1"
    if resource_type == "image":
        return "mirrored-image-v1"
    if resource_type == "attachment":
        return "mirrored-attachment-v1"
    if resource_type == "pdf":
        return "pypdf-6-layout-v3"
    return f"{source.parser_profile}-v6"


def content_hash(document: ParsedDocument) -> str:
    stable_payload = {
        "title": document.title,
        "publisher": document.publisher,
        "normalized_text": document.normalized_text,
        "published_at": (document.published_at.isoformat() if document.published_at else None),
        "effective_from": (
            document.effective_from.isoformat() if document.effective_from else None
        ),
        "effective_to": document.effective_to.isoformat() if document.effective_to else None,
        "parser_version": document.parser_version,
        # Textless scanned/binary documents must still receive a new version
        # when the underlying file changes.
        "raw_sha256": document.metadata.get("raw_sha256"),
    }
    return hashlib.sha256(
        json.dumps(
            stable_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _parse_html(
    source: SourceConfig,
    resource: DiscoveredResource,
    payload: FetchPayload,
) -> ParsedDocument:
    soup = BeautifulSoup(
        payload.body,
        "html.parser",
        from_encoding=payload.encoding,
    )
    for unwanted in soup.select(
        "script, style, noscript, nav, header, footer, input, button, select, textarea, svg, canvas"
    ):
        unwanted.decompose()
    html_title = ""
    for selector in (
        "h1",
        ".news_detail h2",
        ".contenter h2",
        "article h2",
        "p[style*='font-size: 24px'][style*='font-weight: bold']",
        ".article-title",
        ".news-title",
        ".top .tit",
        ".tc-tit .d1",
        ".detail-title",
    ):
        title_node = soup.select_one(selector)
        candidate = normalize_text(title_node.get_text(" ", strip=True) if title_node else "")
        if 2 <= len(candidate) <= 240:
            html_title = candidate
            break
    if not html_title:
        html_title = resource.title_hint
    if not html_title and soup.title:
        html_title = normalize_text(soup.title.get_text(" ", strip=True))
    title = (html_title or resource.canonical_uri)[:500]

    content_text = ""
    for selector in CONTENT_SELECTORS:
        content = soup.select_one(selector)
        if content is None:
            continue
        candidate = normalize_text(content.get_text("\n", strip=True))
        if len(candidate) >= 30:
            content_text = candidate
            break
    if not content_text and soup.body:
        content_text = normalize_text(soup.body.get_text("\n", strip=True))
    if not content_text:
        raise DocumentParseError("HTML document has no extractable text")
    if "请输入验证码下载附件" in content_text:
        raise DocumentParseError("Attachment requires interactive verification")
    content_text = re.sub(
        r"(?:浏览量|访问量|点击量)\s*[:：]?\s*\d+",
        "",
        content_text,
    )
    content_text = normalize_text(content_text)
    content_text = content_text[:MAX_NORMALIZED_CHARACTERS]
    published_at = resource.published_hint
    if published_at is None and not resource.metadata.get("is_index"):
        published_at = parse_date(content_text)
    quality = "accepted" if len(content_text) >= 80 else "low_text"
    return ParsedDocument(
        title=title,
        publisher=source.owner_department,
        normalized_text=content_text,
        media_type=payload.media_type or "text/html",
        published_at=published_at,
        effective_from=None,
        effective_to=None,
        parser_version=f"{source.parser_profile}-v6",
        quality_status=quality,
        metadata={
            "source_profile": source.parser_profile,
            "canonical_uri": resource.canonical_uri,
        },
    )


def _parse_pdf(
    source: SourceConfig,
    resource: DiscoveredResource,
    payload: FetchPayload,
) -> ParsedDocument:
    if payload.body.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise DocumentParseError("PDF resource returned an HTML viewer shell")
    try:
        reader = PdfReader(io.BytesIO(payload.body))
    except Exception as exc:
        return _parse_mirrored_pdf(
            source,
            resource,
            payload,
            reason="text_extraction_failed",
            error_type=type(exc).__name__,
        )
    normalized, extracted_pages = _extract_pdf_layout(reader)
    if not normalized:
        return _parse_mirrored_pdf(
            source,
            resource,
            payload,
            reason="no_text_layer",
            page_count=len(reader.pages),
        )
    normalized = normalized[:MAX_NORMALIZED_CHARACTERS]
    metadata = reader.metadata or {}
    metadata_title = normalize_text(str(metadata.get("/Title", "")))
    title = (resource.title_hint or metadata_title or "官方 PDF 文档")[:500]
    published_at = resource.published_hint or parse_date(normalized[:5000])
    return ParsedDocument(
        title=title,
        publisher=source.owner_department,
        normalized_text=normalized,
        media_type="application/pdf",
        published_at=published_at,
        effective_from=None,
        effective_to=None,
        parser_version="pypdf-6-layout-v3",
        quality_status="accepted" if len(normalized) >= 200 else "low_text",
        metadata={
            "page_count": len(reader.pages),
            "layout_pages": extracted_pages,
            "text_mode": "layout",
            "source_profile": source.parser_profile,
            "canonical_uri": resource.canonical_uri,
        },
    )


def _extract_pdf_layout(reader: PdfReader) -> tuple[str, int]:
    """Extract every readable PDF page while preserving its visual text layout."""

    extracted: list[str] = []
    for page_number, page in enumerate(reader.pages[:160], start=1):
        try:
            layout = _normalize_pdf_layout(page.extract_text(extraction_mode="layout") or "")
        except Exception:
            layout = ""
        if not layout:
            try:
                layout = _normalize_pdf_layout(page.extract_text() or "")
            except Exception:
                layout = ""
        if layout:
            extracted.append(f"【PDF 第 {page_number} 页】\n{layout}")
    return "\n\n".join(extracted), len(extracted)


def _normalize_pdf_layout(value: str) -> str:
    utf8_safe = value.encode("utf-8", errors="replace").decode("utf-8")
    normalized = unicodedata.normalize("NFKC", utf8_safe).replace("\u200b", "")
    lines = [line.rstrip() for line in normalized.replace("\r", "\n").splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return re.sub(r"\n{4,}", "\n\n\n", "\n".join(lines))


def _parse_mirrored_pdf(
    source: SourceConfig,
    resource: DiscoveredResource,
    payload: FetchPayload,
    *,
    reason: str,
    page_count: int | None = None,
    error_type: str | None = None,
) -> ParsedDocument:
    title = (resource.title_hint or PurePosixPath(resource.canonical_uri).name)[:500]
    parent_uri = str(resource.metadata.get("parent_uri") or "")
    normalized = normalize_text(
        "\n".join(
            part
            for part in (
                f"扫描型 PDF：{title}",
                f"所属页面：{parent_uri}" if parent_uri else "",
                f"文件地址：{resource.canonical_uri}",
            )
            if part
        )
    )
    return ParsedDocument(
        title=title,
        publisher=source.owner_department,
        normalized_text=normalized,
        media_type="application/pdf",
        published_at=resource.published_hint,
        effective_from=None,
        effective_to=None,
        parser_version="pypdf-6-layout-v3",
        quality_status="pdf_pending_ocr",
        metadata={
            "source_profile": source.parser_profile,
            "canonical_uri": resource.canonical_uri,
            "parent_uri": parent_uri or None,
            "snapshot_bytes": len(payload.body),
            "raw_sha256": hashlib.sha256(payload.body).hexdigest(),
            "page_count": page_count,
            "ocr_reason": reason,
            "extract_error_type": error_type,
        },
    )


def _parse_openxml(
    source: SourceConfig,
    resource: DiscoveredResource,
    payload: FetchPayload,
    *,
    suffix: str,
) -> ParsedDocument:
    prefixes = {
        ".docx": ("word/document.xml", "word/header", "word/footer"),
        ".xlsx": ("xl/sharedStrings.xml", "xl/worksheets/"),
        ".pptx": ("ppt/slides/", "ppt/notesSlides/"),
    }[suffix]
    text_parts: list[str] = []
    try:
        with zipfile.ZipFile(io.BytesIO(payload.body)) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.endswith(".xml") and any(name.startswith(prefix) for prefix in prefixes)
            ]
            for name in names[:2000]:
                try:
                    root = ElementTree.fromstring(archive.read(name))
                except ElementTree.ParseError:
                    continue
                text_parts.extend(
                    value.strip()
                    for element in root.iter()
                    if (value := element.text) and value.strip()
                )
    except (zipfile.BadZipFile, OSError):
        # Some campus servers label legacy Office files or error-tolerant
        # binaries with an OpenXML suffix. Preserve the mirrored resource and
        # its parent relationship even when textual extraction is impossible.
        return _parse_mirrored_binary(source, resource, payload)
    normalized = normalize_text("\n".join(text_parts))[:MAX_NORMALIZED_CHARACTERS]
    if not normalized:
        return _parse_mirrored_binary(source, resource, payload)
    title = (resource.title_hint or PurePosixPath(resource.canonical_uri).name)[:500]
    return ParsedDocument(
        title=title,
        publisher=source.owner_department,
        normalized_text=normalized,
        media_type=payload.media_type or "application/octet-stream",
        published_at=resource.published_hint or parse_date(normalized[:5000]),
        effective_from=None,
        effective_to=None,
        parser_version="mirrored-attachment-v1",
        quality_status="accepted" if len(normalized) >= 80 else "low_text",
        metadata={
            "source_profile": source.parser_profile,
            "canonical_uri": resource.canonical_uri,
            "parent_uri": resource.metadata.get("parent_uri"),
            "attachment_format": suffix.removeprefix("."),
        },
    )


def _parse_text_attachment(
    source: SourceConfig,
    resource: DiscoveredResource,
    payload: FetchPayload,
) -> ParsedDocument:
    text = payload.body.decode(payload.encoding or "utf-8", errors="replace")
    normalized = normalize_text(text)[:MAX_NORMALIZED_CHARACTERS]
    if not normalized:
        return _parse_mirrored_binary(source, resource, payload)
    title = (resource.title_hint or PurePosixPath(resource.canonical_uri).name)[:500]
    return ParsedDocument(
        title=title,
        publisher=source.owner_department,
        normalized_text=normalized,
        media_type=payload.media_type or "text/plain",
        published_at=resource.published_hint or parse_date(normalized[:5000]),
        effective_from=None,
        effective_to=None,
        parser_version="mirrored-attachment-v1",
        quality_status="accepted" if len(normalized) >= 80 else "low_text",
        metadata={
            "source_profile": source.parser_profile,
            "canonical_uri": resource.canonical_uri,
            "parent_uri": resource.metadata.get("parent_uri"),
        },
    )


def _parse_mirrored_binary(
    source: SourceConfig,
    resource: DiscoveredResource,
    payload: FetchPayload,
) -> ParsedDocument:
    title = (resource.title_hint or PurePosixPath(resource.canonical_uri).name)[:500]
    kind = "正文图片" if resource.resource_type == "image" else "附件"
    parent_uri = str(resource.metadata.get("parent_uri") or "")
    normalized = normalize_text(
        "\n".join(
            part
            for part in (
                f"{kind}：{title}",
                f"所属页面：{parent_uri}" if parent_uri else "",
                f"文件地址：{resource.canonical_uri}",
            )
            if part
        )
    )
    return ParsedDocument(
        title=title,
        publisher=source.owner_department,
        normalized_text=normalized,
        media_type=payload.media_type or "application/octet-stream",
        published_at=resource.published_hint,
        effective_from=None,
        effective_to=None,
        parser_version=(
            "mirrored-image-v1" if resource.resource_type == "image" else "mirrored-attachment-v1"
        ),
        quality_status=(
            "image_pending_transcription"
            if resource.resource_type == "image"
            else "binary_mirrored"
        ),
        metadata={
            "source_profile": source.parser_profile,
            "canonical_uri": resource.canonical_uri,
            "parent_uri": parent_uri or None,
            "snapshot_bytes": len(payload.body),
            "raw_sha256": hashlib.sha256(payload.body).hexdigest(),
            "article_image": bool(resource.metadata.get("article_image")),
        },
    )


def _parse_lecture(
    source: SourceConfig,
    resource: DiscoveredResource,
    payload: FetchPayload,
) -> ParsedDocument:
    try:
        body = json.loads(payload.body)
        item = body["data"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DocumentParseError("Lecture payload is invalid") from exc
    if not isinstance(item, dict):
        raise DocumentParseError("Lecture payload data is invalid")

    sections = [
        ("讲座名称", item.get("name")),
        ("主讲人", item.get("speakerName")),
        ("主讲人职务", item.get("speakerJob")),
        ("主讲人单位", item.get("speakerUnit")),
        ("讲座类别", item.get("categoryName") or item.get("type")),
        ("讲座内容", item.get("content")),
        ("主讲人简介", item.get("speakerResume")),
        ("地点", item.get("address")),
        ("开始时间", _format_epoch(item.get("startTime"))),
        ("结束时间", _format_epoch(item.get("endTime"))),
        ("报名截止", _format_epoch(item.get("registrationDeadline"))),
        ("名额", item.get("registrationNumber")),
        ("状态", item.get("state")),
    ]
    normalized = normalize_text(
        "\n".join(f"{label}：{value}" for label, value in sections if value not in (None, ""))
    )
    if not normalized:
        raise DocumentParseError("Lecture payload has no safe extractable fields")
    title = normalize_text(str(item.get("name") or resource.title_hint or "校园讲座"))
    published_at = resource.published_hint or _epoch_datetime(item.get("insertTime"))
    return ParsedDocument(
        title=title[:500],
        publisher=source.owner_department,
        normalized_text=normalized[:MAX_NORMALIZED_CHARACTERS],
        media_type="application/vnd.hzcu.lecture+json",
        published_at=published_at,
        effective_from=_epoch_datetime(item.get("startTime")),
        effective_to=_epoch_datetime(item.get("endTime")),
        parser_version="lecture-json-safe-v1",
        quality_status="accepted",
        metadata={
            "external_id": item.get("id"),
            "category": item.get("categoryName") or item.get("type"),
            "snapshot_policy": "sanitized",
        },
    )


def _parse_cms_message(
    source: SourceConfig,
    resource: DiscoveredResource,
    payload: FetchPayload,
) -> ParsedDocument:
    try:
        body = json.loads(payload.body)
        item = body["data"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DocumentParseError("CMS message payload is invalid") from exc
    if not isinstance(item, dict):
        raise DocumentParseError("CMS message data is invalid")

    title = normalize_text(str(item.get("title") or resource.title_hint or "公开通知"))
    raw_content = str(item.get("content") or "")
    if item.get("type") == "url":
        content = normalize_text(raw_content)
    else:
        soup = BeautifulSoup(raw_content, "html.parser")
        for unwanted in soup.select("script, style, noscript, iframe, form"):
            unwanted.decompose()
        content = normalize_text(soup.get_text("\n", strip=True))
    if not content:
        raise DocumentParseError("CMS message has no safe extractable content")
    author = normalize_text(str(item.get("authorOffice") or item.get("author") or ""))
    publisher = (author or source.owner_department)[:200]
    published_at = resource.published_hint or _epoch_datetime(item.get("publicTime"))
    normalized = normalize_text(
        "\n".join(
            part
            for part in (
                f"标题：{title}",
                f"发布单位：{publisher}",
                content,
            )
            if part
        )
    )
    return ParsedDocument(
        title=title[:500],
        publisher=publisher,
        normalized_text=normalized[:MAX_NORMALIZED_CHARACTERS],
        media_type="application/vnd.hzcu.cms-message+json",
        published_at=published_at,
        effective_from=None,
        effective_to=None,
        parser_version="cms-message-safe-v1",
        quality_status="accepted" if len(normalized) >= 40 else "low_text",
        metadata={
            "external_id": item.get("id"),
            "channel_code": item.get("channelCode"),
            "channel_id": item.get("channelId"),
            "message_type": item.get("type"),
            "snapshot_policy": "sanitized",
        },
    )


def _epoch_datetime(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _format_epoch(value: object) -> str | None:
    timestamp = _epoch_datetime(value)
    return (
        timestamp.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
        if timestamp
        else None
    )
