import gzip
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from hzcu_agent.cli import _due_source_ids
from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.ingestion.catalog import SourceRegistry
from hzcu_agent.ingestion.connectors import (
    LectureApiConnector,
    LinkedPageConnector,
    OperatorImportConnector,
    SafeSourceFetcher,
    SourceFetchError,
)
from hzcu_agent.ingestion.ocr_layout import normalized_ocr_text
from hzcu_agent.ingestion.operator_import import OperatorDocumentImporter
from hzcu_agent.ingestion.parsers import (
    DocumentParseError,
    _extract_pdf_layout,
    content_hash,
    normalize_text,
    parse_document,
)
from hzcu_agent.ingestion.service import IngestionService
from hzcu_agent.ingestion.types import DiscoveredResource, FetchPayload
from hzcu_agent.main import create_app
from hzcu_agent.models import (
    CampusEntityRecord,
    DocumentChunk,
    DocumentVersion,
    SourceResource,
    SyncRun,
)
from hzcu_agent.tools.campus_memory import (
    CampusMemorySearchArguments,
    CampusMemorySearchTool,
)


def _linked_registry(path: Path) -> None:
    path.write_text(
        """
version: 1
sources:
  - id: test-notices
    name: 测试通知源
    owner_department: 测试教务处
    base_url: https://source.test/
    allowed_hosts: [source.test]
    visibility: public
    authority_level: official
    acquisition_methods: [scheduled_crawl]
    poll_interval_seconds: 300
    rate_limit_per_minute: 120
    default_ttl_seconds: 3600
    live_required_for: [截止时间]
    parser_profile: test_html
    snapshot_policy: raw
    enabled: true
    connector:
      kind: linked_html
      seed_urls: [https://source.test/index.htm]
      include_patterns: ['/notice/[0-9]+\\.htm$']
      max_resources_per_run: 10
""".strip(),
        encoding="utf-8",
    )


def _operator_registry(path: Path) -> None:
    path.write_text(
        """
version: 1
sources:
  - id: verified-artifacts
    name: 已核验正式材料
    owner_department: 测试学校
    base_url: https://source.test/
    allowed_hosts: [source.test]
    visibility: public
    authority_level: official_secondary
    acquisition_methods: [operator_verified_official_artifact, multimodal_page_ocr]
    poll_interval_seconds: 604800
    rate_limit_per_minute: 1
    default_ttl_seconds: 31536000
    live_required_for: [当前规则]
    parser_profile: operator_verified_document
    snapshot_policy: raw
    enabled: true
    connector:
      kind: operator_import
      earliest_published_year: 1990
      max_resources_per_run: 1
""".strip(),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_operator_import_preserves_original_and_indexes_page_text(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "operator-sources.yaml"
    _operator_registry(registry_path)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'operator.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "snapshots"),
    )
    database = Database(settings)
    await database.initialize()
    registry = SourceRegistry(database, registry_path)
    await registry.sync_definitions()
    source = registry.require("verified-artifacts")
    connector = OperatorImportConnector()

    assert await connector.discover(source) == []
    with pytest.raises(SourceFetchError, match="no network fetch"):
        await connector.fetch(
            source,
            DiscoveredResource(
                canonical_uri="https://source.test/document.pdf",
                fetch_uri="https://source.test/document.pdf",
                resource_type="pdf",
            ),
        )

    original = b"%PDF-1.4\nverified scanned artifact\n%%EOF"
    text = (
        "【PDF 第 1 页】\n第二三四课堂支撑项目评价细则。\n\n"
        "【PDF 第 2 页】\n志愿服务按 2 分/小时认定。"
    )
    importer = OperatorDocumentImporter(
        settings=settings,
        database=database,
        registry=registry,
    )
    first = await importer.import_document(
        source_id=source.id,
        original=original,
        original_filename="正式材料.pdf",
        normalized_text=text,
        title="2021级第二三四课堂支撑项目评价细则（扫描版）",
        publisher="测试学校学生工作部",
        media_type="application/pdf",
        published_at=datetime(2021, 9, 1, tzinfo=UTC),
        effective_from=datetime(2021, 9, 1, tzinfo=UTC),
        effective_to=None,
        parser_version="multimodal-page-ocr-v1",
        metadata={"audience_scopes": ["2021级本科生"]},
    )
    second = await importer.import_document(
        source_id=source.id,
        original=original,
        original_filename="正式材料.pdf",
        normalized_text=text,
        title="2021级第二三四课堂支撑项目评价细则（扫描版）",
        publisher="测试学校学生工作部",
        media_type="application/pdf",
        published_at=datetime(2021, 9, 1, tzinfo=UTC),
        effective_from=datetime(2021, 9, 1, tzinfo=UTC),
        effective_to=None,
        parser_version="multimodal-page-ocr-v1",
        metadata={"audience_scopes": ["2021级本科生"]},
    )

    assert first.status == "created"
    assert second.status == "unchanged"
    assert first.resource_id == second.resource_id
    assert first.document_version_id == second.document_version_id
    assert first.canonical_uri == (
        f"/api/v1/sources/{source.id}/resources/{first.resource_id}/original"
    )
    assert first.chunks >= 1

    memory = CampusMemorySearchTool(database)
    result = await memory.run(
        CampusMemorySearchArguments(query="志愿服务 小时 认定"),
        "trace-operator-import",
    )
    assert [item.document_version_id for item in result.evidence] == [first.document_version_id]
    assert result.evidence[0].published_at == datetime(2021, 9, 1, tzinfo=UTC)

    async with database.session_factory() as session:
        version = await session.get(DocumentVersion, first.document_version_id)
        assert version is not None
        snapshot_path = (
            tmp_path
            / "snapshots"
            / version.document_metadata["raw_sha256"][:2]
            / f"{version.document_metadata['raw_sha256']}.gz"
        )
        with gzip.open(snapshot_path, "rb") as handle:
            assert handle.read() == original
        runs = list(
            (
                await session.scalars(
                    select(SyncRun)
                    .where(SyncRun.source_id == source.id)
                    .order_by(SyncRun.started_at)
                )
            ).all()
        )
        assert [run.created_count for run in runs] == [1, 0]
        assert [run.unchanged_count for run in runs] == [0, 1]

    await database.close()

    with TestClient(create_app(settings)) as client:
        response = client.get(f"/api/v1/sources/{source.id}/resources/{first.resource_id}/original")
        assert response.status_code == 200
        assert response.content == original
        assert response.headers["content-type"].startswith("application/pdf")


def test_sqlite_serializes_resource_writes_but_server_database_keeps_concurrency() -> None:
    sqlite_settings = Settings(
        environment="test",
        database_url="sqlite+aiosqlite:///:memory:",
        sync_max_concurrency=8,
    )
    postgres_settings = Settings(
        environment="test",
        database_url="postgresql+asyncpg://agent:agent@database/agent",
        sync_max_concurrency=8,
    )

    assert sqlite_settings.effective_sync_max_concurrency == 1
    assert postgres_settings.effective_sync_max_concurrency == 8


@pytest.mark.asyncio
async def test_source_fetcher_retries_reads_and_rejects_unregistered_posts(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "sources.yaml"
    _linked_registry(registry_path)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'fetcher.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "snapshots"),
    )
    database = Database(settings)
    await database.initialize()
    registry = SourceRegistry(database, registry_path)
    source = registry.require("test-notices")
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("transient", request=request)
        return httpx.Response(200, text="ok", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeSourceFetcher(max_download_bytes=1024, client=client)
    result = await fetcher.request(source, "GET", "https://source.test/index.htm")
    assert result.status_code == 200
    assert attempts == 3

    with pytest.raises(SourceFetchError, match="read-only"):
        await fetcher.request(
            source,
            "POST",
            "https://source.test/index.htm",
            json_body={"action": "submit"},
        )

    await fetcher.close()
    await client.aclose()
    await database.close()


@pytest.mark.asyncio
async def test_linked_discovery_allocates_limit_fairly_across_seeds(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "fair-sources.yaml"
    registry_path.write_text(
        """
version: 1
sources:
  - id: test-fair-notices
    name: 多栏目测试源
    owner_department: 测试教务处
    base_url: https://source.test/
    allowed_hosts: [source.test]
    visibility: public
    authority_level: official
    acquisition_methods: [scheduled_crawl]
    poll_interval_seconds: 300
    rate_limit_per_minute: 120
    default_ttl_seconds: 3600
    live_required_for: [选课, 考试]
    parser_profile: test_html
    snapshot_policy: raw
    enabled: true
    connector:
      kind: linked_html
      seed_urls:
        - https://source.test/channel-a.htm
        - https://source.test/channel-b.htm
      include_patterns: ['/notice/[ab][0-9]+\\.htm$']
      max_resources_per_run: 4
""".strip(),
        encoding="utf-8",
    )
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'fair.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "snapshots"),
    )
    database = Database(settings)
    await database.initialize()
    registry = SourceRegistry(database, settings.resolved_source_registry_path)
    await registry.sync_definitions()
    source = registry.require("test-fair-notices")

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/channel-a.htm":
            links = "".join(
                f'<a href="/notice/a{index}.htm">栏目 A {index}</a>' for index in range(1, 7)
            )
        else:
            links = '<a href="/notice/b1.htm">栏目 B 1</a>'
        return httpx.Response(200, text=links, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeSourceFetcher(max_download_bytes=1024 * 1024, client=client)
    connector = LinkedPageConnector(fetcher)
    resources = await connector.discover(source)

    assert [item.title_hint for item in resources] == [
        "栏目 A 1",
        "栏目 B 1",
        "栏目 A 2",
        "栏目 A 3",
    ]

    await fetcher.close()
    await client.aclose()
    await database.close()


@pytest.mark.asyncio
async def test_linked_discovery_keeps_unpatterned_official_content_as_open_world_candidate(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "sources.yaml"
    _linked_registry(registry_path)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'open-world.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "snapshots"),
    )
    database = Database(settings)
    await database.initialize()
    registry = SourceRegistry(database, settings.resolved_source_registry_path)
    await registry.sync_definitions()
    source = registry.require("test-notices")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                '<a href="/notice/1.htm">已知通知结构</a>'
                '<a href="/emerging/calendar.aspx">新栏目校历入口</a>'
            ),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeSourceFetcher(max_download_bytes=1024 * 1024, client=client)
    resources = await LinkedPageConnector(fetcher).discover(source)

    assert {item.title_hint for item in resources} == {
        "已知通知结构",
        "新栏目校历入口",
    }
    assert (
        next(item for item in resources if item.title_hint == "新栏目校历入口").metadata[
            "pattern_hint"
        ]
        is False
    )

    await fetcher.close()
    await client.aclose()
    await database.close()


@pytest.mark.asyncio
async def test_full_linked_discovery_exhausts_pagination_and_collects_article_files(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "sources.yaml"
    _linked_registry(registry_path)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'full.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "snapshots"),
    )
    database = Database(settings)
    await database.initialize()
    registry = SourceRegistry(database, settings.resolved_source_registry_path)
    source = registry.require("test-notices")

    def handler(request: httpx.Request) -> httpx.Response:
        pages = {
            "/index.htm": ('<a href="/index_2.htm">下一页</a><a href="/notice/1.htm">通知一</a>'),
            "/index_2.htm": '<a href="/notice/2.htm">通知二</a>',
            "/notice/1.htm": (
                "<h1>通知一</h1><div class='v_news_content'>"
                '<a href="/files/form.docx">附件表格</a>'
                '<img src="/files/calendar.png" alt="校历">'
                "</div>"
            ),
            "/notice/2.htm": "<h1>通知二</h1><main>第二条通知正文。</main>",
        }
        return httpx.Response(
            200,
            text=pages.get(request.url.path, "binary"),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeSourceFetcher(max_download_bytes=1024 * 1024, client=client)
    resources = await LinkedPageConnector(fetcher).discover(source, full_scan=True)
    by_url = {item.canonical_uri: item for item in resources}

    assert set(by_url) == {
        "https://source.test/index.htm",
        "https://source.test/index_2.htm",
        "https://source.test/notice/1.htm",
        "https://source.test/notice/2.htm",
        "https://source.test/files/form.docx",
        "https://source.test/files/calendar.png",
    }
    assert by_url["https://source.test/index_2.htm"].metadata["is_index"] is True
    assert by_url["https://source.test/files/form.docx"].resource_type == "attachment"
    assert by_url["https://source.test/files/calendar.png"].resource_type == "image"

    await fetcher.close()
    await client.aclose()
    await database.close()


@pytest.mark.asyncio
async def test_full_site_discovery_follows_sibling_columns_without_mirroring_lists(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "full-site-sources.yaml"
    registry_path.write_text(
        """
version: 1
sources:
  - id: test-full-site
    name: 测试学院全站
    owner_department: 测试学院
    base_url: https://source.test/
    allowed_hosts: [source.test]
    visibility: public
    authority_level: official
    acquisition_methods: [full_site_mirror]
    poll_interval_seconds: 300
    rate_limit_per_minute: 120
    default_ttl_seconds: 3600
    live_required_for: [培养方案]
    parser_profile: test_html
    snapshot_policy: raw
    enabled: true
    connector:
      kind: linked_html
      earliest_published_year: 2023
      follow_site_navigation: true
      mirror_listing_pages: false
      full_scan_by_default: true
      seed_urls: [https://source.test/col/col411/index.html]
      include_patterns: ['/art/\\d+/\\d+/\\d+/art_\\d+_\\d+\\.html$']
      max_resources_per_run: 10
""".strip(),
        encoding="utf-8",
    )
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'full-site.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "snapshots"),
    )
    database = Database(settings)
    await database.initialize()
    registry = SourceRegistry(database, settings.resolved_source_registry_path)
    source = registry.require("test-full-site")

    def handler(request: httpx.Request) -> httpx.Response:
        pages = {
            "/col/col411/index.html": (
                '<a href="/col/col412/index.html">课程建设</a>'
                '<a href="/module/web/jpage/dataproxy.jsp?'
                'page=1&columnid=411">下一页</a>'
                "<p><a href='/art/2024/8/28/art_411_2.html'>"
                "2024级本科专业培养方案</a><i>2024-08-28</i></p>"
                "<p><a href='/art/2022/8/28/art_411_1.html'>"
                "2022级本科专业培养方案</a><i>2022-08-28</i></p>"
            ),
            "/module/web/jpage/dataproxy.jsp": (
                "<p><a href='/art/2023/8/28/art_411_4.html'>"
                "2023级本科课程计划</a><i>2023-08-28</i></p>"
            ),
            "/col/col412/index.html": (
                "<main><p><a href='/art/2023/9/1/art_412_3.html'>"
                "2023年课程建设成果</a><i>2023-09-01</i></p></main>"
            ),
            "/art/2024/8/28/art_411_2.html": (
                "<h1>2024级本科专业培养方案</h1>"
                "<div class='v_news_content'>"
                "<a href='/module/download/downfile.jsp?filename=2024-plan.pdf'>"
                "2024级培养方案.pd</a></div>"
            ),
            "/art/2023/9/1/art_412_3.html": (
                "<h1>2023年课程建设成果</h1><div class='v_news_content'>课程建设正文。</div>"
            ),
            "/art/2023/8/28/art_411_4.html": (
                "<h1>2023级本科课程计划</h1><div class='v_news_content'>课程计划正文。</div>"
            ),
        }
        return httpx.Response(
            200,
            text=pages.get(request.url.path, "pdf"),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeSourceFetcher(max_download_bytes=1024 * 1024, client=client)
    resources = await LinkedPageConnector(fetcher).discover(source, full_scan=True)

    assert {item.canonical_uri for item in resources} == {
        "https://source.test/art/2024/8/28/art_411_2.html",
        "https://source.test/art/2023/9/1/art_412_3.html",
        "https://source.test/art/2023/8/28/art_411_4.html",
        "https://source.test/module/download/downfile.jsp?filename=2024-plan.pdf",
    }
    assert source.connector.full_scan_by_default is True
    assert all(not item.metadata.get("is_index") for item in resources)
    assert (
        next(item for item in resources if "downfile.jsp" in item.canonical_uri).resource_type
        == "pdf"
    )

    await fetcher.close()
    await client.aclose()
    await database.close()


@pytest.mark.asyncio
async def test_full_discovery_treats_calendar_column_as_embedded_article(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "calendar-sources.yaml"
    registry_path.write_text(
        """
version: 1
sources:
  - id: test-calendar
    name: 测试校历
    owner_department: 测试办公室
    base_url: https://source.test/
    allowed_hosts: [source.test]
    visibility: public
    authority_level: official
    acquisition_methods: [scheduled_crawl]
    poll_interval_seconds: 300
    rate_limit_per_minute: 120
    default_ttl_seconds: 3600
    live_required_for: [校历]
    parser_profile: test_html
    snapshot_policy: raw
    enabled: true
    connector:
      kind: linked_html
      seed_urls: [https://source.test/col/col10382/index.html]
      include_patterns: ['/col/col10382/index\\.html$']
      include_seed: true
      max_resources_per_run: 10
""".strip(),
        encoding="utf-8",
    )
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'calendar.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "snapshots"),
    )
    database = Database(settings)
    source = SourceRegistry(database, registry_path).require("test-calendar")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/col/col10382/index.html"
        return httpx.Response(
            200,
            text=(
                '<nav><a href="/col/col88/index.html">行政文件</a></nav>'
                '<div id="zoom">'
                '<a href="/picture/calendar-1.png">'
                '<img title="2026—2027学年校历第一页" src="/picture/calendar-1.png"></a>'
                '<a href="/picture/calendar-2.png"><img src="/picture/calendar-2.png"></a>'
                "</div>"
            ),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeSourceFetcher(max_download_bytes=1024 * 1024, client=client)
    resources = await LinkedPageConnector(fetcher).discover(source, full_scan=True)

    assert {item.canonical_uri for item in resources} == {
        "https://source.test/col/col10382/index.html",
        "https://source.test/picture/calendar-1.png",
        "https://source.test/picture/calendar-2.png",
    }
    assert resources[0].metadata["is_index"] is False
    assert (
        next(item for item in resources if item.canonical_uri.endswith("calendar-1.png")).title_hint
        == "2026—2027学年校历第一页"
    )

    await fetcher.close()
    await client.aclose()
    await database.close()


@pytest.mark.asyncio
async def test_full_discovery_follows_registered_query_style_detail_links(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "query-sources.yaml"
    registry_path.write_text(
        """
version: 1
sources:
  - id: query-notices
    name: 查询式通知
    owner_department: 测试教务处
    base_url: https://source.test/
    allowed_hosts: [source.test]
    visibility: public
    authority_level: official
    acquisition_methods: [scheduled_crawl]
    poll_interval_seconds: 300
    rate_limit_per_minute: 120
    default_ttl_seconds: 3600
    live_required_for: []
    parser_profile: test_html
    snapshot_policy: raw
    enabled: true
    connector:
      kind: linked_html
      seed_urls:
        - https://source.test/index.php?c=main&a=tlist&id=57
      include_patterns:
        - 'index\\.php\\?a=detail&c=main&id=\\d+$'
      include_seed: false
      max_resources_per_run: 10
""".strip(),
        encoding="utf-8",
    )
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'query.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "snapshots"),
    )
    database = Database(settings)
    source = SourceRegistry(database, registry_path).require("query-notices")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("a") == "detail":
            return httpx.Response(
                200,
                text="<html><body><article>完整通知正文与时间安排</article></body></html>",
                request=request,
            )
        return httpx.Response(
            200,
            text=(
                "<html><body><div class='list'>"
                "<p><a href='/index.php?c=main&a=detail&id=7120'>"
                "2026年项目通知</a><span>2026-01-22</span></p>"
                "<p><a href='/index.php?c=main&a=detail&id=1000'>"
                "2022年历史通知</a><span>2022-12-31</span></p>"
                "<p><a href='/files/current-form.docx'>"
                "2026年项目附件</a><span>2026-01-22</span></p>"
                "<p><a href='/files/old-form.docx'>"
                "2022年历史附件</a><span>2022-12-31</span></p>"
                "</div></body></html>"
            ),
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeSourceFetcher(max_download_bytes=1024 * 1024, client=client)
    resources = await LinkedPageConnector(fetcher).discover(source, full_scan=True)

    assert {item.canonical_uri for item in resources} == {
        "https://source.test/index.php?a=tlist&c=main&id=57",
        "https://source.test/index.php?a=detail&c=main&id=7120",
        "https://source.test/files/current-form.docx",
    }

    await fetcher.close()
    await client.aclose()
    await database.close()


def test_html_article_h2_outranks_generic_site_title(tmp_path: Path) -> None:
    registry_path = tmp_path / "sources.yaml"
    _linked_registry(registry_path)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'parse.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "snapshots"),
    )
    database = Database(settings)
    source = SourceRegistry(database, registry_path).require("test-notices")
    resource = DiscoveredResource(
        canonical_uri="https://source.test/notice/6926.htm",
        fetch_uri="https://source.test/notice/6926.htm",
        resource_type="html",
    )

    document = parse_document(
        source,
        resource,
        FetchPayload(
            status_code=200,
            body=(
                "<html><head><title>浙大城市学院教务处</title></head>"
                "<body><div class='contenter news_detail'><div class='t'>"
                "<h2>关于组织开展大学生创新训练计划项目中期检查的通知</h2></div>"
                "<div class='v_news_content'>各学院组织国创、校创项目参加中期检查，"
                "并在规定日期前提交项目中期检查材料。</div></div></body></html>"
            ).encode(),
            media_type="text/html",
            final_url=resource.canonical_uri,
            encoding="utf-8",
        ),
    )

    assert document.title == "关于组织开展大学生创新训练计划项目中期检查的通知"
    assert document.parser_version == "test_html-v6"


def test_html_styled_article_heading_outranks_generic_site_title(
    tmp_path: Path,
) -> None:
    registry_path = tmp_path / "sources.yaml"
    _linked_registry(registry_path)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'parse.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "snapshots"),
    )
    database = Database(settings)
    source = SourceRegistry(database, registry_path).require("test-notices")
    resource = DiscoveredResource(
        canonical_uri="https://source.test/cxxl/ShowNews.aspx?NewsNo=100C",
        fetch_uri="https://source.test/cxxl/ShowNews.aspx?NewsNo=100C",
        resource_type="html",
    )

    document = parse_document(
        source,
        resource,
        FetchPayload(
            status_code=200,
            body=(
                "<html><head><title>大学生创新创业训练计划系统</title></head>"
                "<body><div class='news_detail'>"
                "<p style='font-size: 24px; font-weight: bold; text-align: center'>"
                "关于开展国家级大学生创新训练计划项目中期检查的通知</p>"
                "<div class='v_news_content'>各学院应组织项目自查，并按通知时间"
                "提交国家级大学生创新训练计划项目中期检查材料。</div>"
                "</div></body></html>"
            ).encode(),
            media_type="text/html",
            final_url=resource.canonical_uri,
            encoding="utf-8",
        ),
    )

    assert document.title == "关于开展国家级大学生创新训练计划项目中期检查的通知"
    assert document.parser_version == "test_html-v6"


def test_ocr_layout_preserves_date_and_event_rows() -> None:
    payload = {
        "lines": [
            {
                "words": [
                    {
                        "text": "8月28日",
                        "box": {"x": 100, "y": 100, "width": 80, "height": 24},
                    }
                ]
            },
            {
                "words": [
                    {
                        "text": "9月11日",
                        "box": {"x": 100, "y": 140, "width": 80, "height": 24},
                    }
                ]
            },
            {
                "words": [
                    {
                        "text": "本科生新生、研究生新生报到注册",
                        "box": {"x": 260, "y": 101, "width": 380, "height": 24},
                    }
                ]
            },
            {
                "words": [
                    {
                        "text": "本科生老生、研究生老生报到注册",
                        "box": {"x": 260, "y": 141, "width": 380, "height": 24},
                    }
                ]
            },
        ]
    }

    text = normalized_ocr_text(payload)

    assert "8月28日 | 本科生新生、研究生新生报到注册" in text
    assert "9月11日 | 本科生老生、研究生老生报到注册" in text


def test_openxml_and_image_attachments_are_mirrored_and_indexable(tmp_path: Path) -> None:
    registry_path = tmp_path / "sources.yaml"
    _linked_registry(registry_path)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'parse.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "snapshots"),
    )
    database = Database(settings)
    source = SourceRegistry(database, registry_path).require("test-notices")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            (
                "<w:document xmlns:w='urn:test'><w:body><w:p><w:r>"
                "<w:t>校创项目中期检查安排</w:t>"
                "</w:r></w:p></w:body></w:document>"
            ),
        )
    attachment = DiscoveredResource(
        canonical_uri=("https://source.test/module/download/downfile.jsp?filename=check.docx"),
        fetch_uri="https://source.test/module/download/downfile.jsp?filename=check.docx",
        resource_type="attachment",
        title_hint="中期检查附件.docx",
        metadata={"parent_uri": "https://source.test/notice/1.htm"},
    )
    parsed_attachment = parse_document(
        source,
        attachment,
        FetchPayload(
            status_code=200,
            body=buffer.getvalue(),
            media_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            final_url=attachment.canonical_uri,
        ),
    )
    image = attachment.__class__(
        canonical_uri="https://source.test/files/calendar.png",
        fetch_uri="https://source.test/files/calendar.png",
        resource_type="image",
        title_hint="2026—2027学年校历",
        metadata={"parent_uri": "https://source.test/notice/2.htm"},
    )
    parsed_image = parse_document(
        source,
        image,
        FetchPayload(
            status_code=200,
            body=b"\x89PNG\r\n\x1a\nfake",
            media_type="image/png",
            final_url=image.canonical_uri,
        ),
    )

    assert "校创项目中期检查安排" in parsed_attachment.normalized_text
    assert parsed_attachment.quality_status == "low_text"
    assert parsed_image.quality_status == "image_pending_transcription"
    assert "2026—2027学年校历" in parsed_image.normalized_text


def test_invalid_openxml_is_preserved_as_mirrored_binary(tmp_path: Path) -> None:
    registry_path = tmp_path / "sources.yaml"
    _linked_registry(registry_path)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'parse.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "snapshots"),
    )
    database = Database(settings)
    source = SourceRegistry(database, registry_path).require("test-notices")
    resource = DiscoveredResource(
        canonical_uri="https://source.test/files/legacy.docx",
        fetch_uri="https://source.test/files/legacy.docx",
        resource_type="attachment",
        title_hint="旧格式附件",
        metadata={"parent_uri": "https://source.test/info/2026/notice.html"},
    )

    parsed = parse_document(
        source,
        resource,
        FetchPayload(
            status_code=200,
            body=b"not-an-openxml-archive",
            media_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            final_url=resource.canonical_uri,
        ),
    )

    assert parsed.quality_status == "binary_mirrored"
    assert parsed.metadata["parent_uri"].endswith("/notice.html")


def test_pdf_surrogates_are_made_utf8_safe_before_hashing() -> None:
    assert "\ud835" not in normalize_text("竞赛规则\ud835")


def test_pdf_layout_preserves_every_page_and_fixed_width_columns() -> None:
    class FakePage:
        def __init__(self, layout: str) -> None:
            self.layout = layout

        def extract_text(self, *, extraction_mode: str | None = None) -> str:
            assert extraction_mode == "layout"
            return self.layout

    class FakeReader:
        pages = [
            FakePage("奖学金评审表\n姓名        学号        分数\n张三        30001       95\n"),
            FakePage(
                "机械电子工程专业指导性课程计划\n"
                "课程号 中文课程名     1     2     3     4\n"
                "A01002 微积分 II            5-1\n"
            ),
            FakePage("机械电子工程专业课程修读关系图"),
        ]

    layout, page_count = _extract_pdf_layout(FakeReader())

    assert page_count == 3
    assert "【PDF 第 1 页】" in layout
    assert "奖学金评审表" in layout
    assert "姓名        学号        分数" in layout
    assert "【PDF 第 2 页】" in layout
    assert "1     2     3     4" in layout
    assert "A01002 微积分 II            5-1" in layout
    assert layout.endswith("机械电子工程专业课程修读关系图")


def test_scanned_pdf_is_mirrored_for_batch_ocr(tmp_path: Path) -> None:
    registry_path = tmp_path / "sources.yaml"
    _linked_registry(registry_path)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'pdf.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "snapshots"),
    )
    database = Database(settings)
    source = SourceRegistry(database, registry_path).require("test-notices")
    resource = DiscoveredResource(
        canonical_uri="https://source.test/files/scanned.pdf",
        fetch_uri="https://source.test/files/scanned.pdf",
        resource_type="pdf",
        title_hint="扫描版通知",
        metadata={"parent_uri": "https://source.test/notice/1.htm"},
    )

    parsed = parse_document(
        source,
        resource,
        FetchPayload(
            status_code=200,
            body=b"%PDF-this-is-an-intentionally-unparseable-test-payload",
            media_type="application/pdf",
            final_url=resource.canonical_uri,
        ),
    )

    assert parsed.quality_status == "pdf_pending_ocr"
    assert parsed.metadata["ocr_reason"] == "text_extraction_failed"
    assert parsed.metadata["raw_sha256"]
    assert "扫描版通知" in parsed.normalized_text

    changed = parse_document(
        source,
        resource,
        FetchPayload(
            status_code=200,
            body=b"%PDF-a-different-unparseable-test-payload",
            media_type="application/pdf",
            final_url=resource.canonical_uri,
        ),
    )
    assert content_hash(changed) != content_hash(parsed)

    with pytest.raises(DocumentParseError, match="HTML viewer shell"):
        parse_document(
            source,
            resource,
            FetchPayload(
                status_code=200,
                body=b"<!doctype html><embed type='application/pdf' src='about:blank'>",
                media_type="application/pdf",
                final_url=resource.canonical_uri,
            ),
        )


@pytest.mark.asyncio
async def test_incremental_sync_versions_snapshots_and_memory(tmp_path: Path) -> None:
    registry_path = tmp_path / "sources.yaml"
    _linked_registry(registry_path)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'agent.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "snapshots"),
        sync_max_concurrency=2,
    )
    database = Database(settings)
    await database.initialize()
    registry = SourceRegistry(database, settings.resolved_source_registry_path)
    assert await registry.sync_definitions() == 1
    assert await registry.sync_definitions() == 0
    source = registry.require("test-notices")
    discovered = DiscoveredResource(
        canonical_uri="https://source.test/notice/stable.htm",
        fetch_uri="https://source.test/notice/stable.htm",
        resource_type="html",
        title_hint="稳定通知",
    )
    documents = [
        parse_document(
            source,
            discovered,
            FetchPayload(
                status_code=200,
                body=(
                    "<html><body><h1>稳定通知</h1>"
                    f"<p>这是不会变化的通知正文，浏览量：{views}</p>"
                    "</body></html>"
                ).encode(),
                media_type="text/html",
                final_url=discovered.canonical_uri,
                encoding="utf-8",
            ),
        )
        for views in (100, 101)
    ]
    assert content_hash(documents[0]) == content_hash(documents[1])
    assert await _due_source_ids(
        database,
        registry,
        allowed_source_ids=set(),
    ) == ["test-notices"]

    state = {"revision": 1}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/index.htm":
            return httpx.Response(
                200,
                text='<a href="/notice/1.htm">关于新学期选课工作的通知</a>',
                request=request,
            )
        revision = state["revision"]
        etag = f'"v{revision}"'
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"ETag": etag}, request=request)
        detail = (
            "2026级本科生应在规定时间内完成预选和正选。"
            "选课开始时间为2026年7月28日9:00，截止时间为2026年8月1日17:00。"
            if revision == 1
            else "2026级本科生应在规定时间内完成预选、正选和补退选。"
            "选课开始时间为2026年7月28日9:00，截止时间为2026年8月2日17:00，"
            "逾期不再办理。"
        )
        return httpx.Response(
            200,
            text=(
                "<html><body><form><h1>关于新学期选课工作的通知</h1>"
                f"<div class='v_news_content'>{detail}{detail}</div>"
                "<span>发布日期：2026-07-25</span></form></body></html>"
            ),
            headers={"ETag": etag, "Content-Type": "text/html; charset=utf-8"},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ingestion = IngestionService(
        settings=settings,
        database=database,
        registry=registry,
        client=client,
    )
    first = await ingestion.sync_source("test-notices")
    assert (
        await _due_source_ids(
            database,
            registry,
            allowed_source_ids=set(),
        )
        == []
    )
    second = await ingestion.sync_source("test-notices")
    state["revision"] = 2
    third = await ingestion.sync_source("test-notices")

    assert (first.created_count, first.unchanged_count) == (1, 0)
    assert (second.created_count, second.unchanged_count) == (0, 1)
    assert (third.created_count, third.unchanged_count) == (1, 0)

    async with database.session_factory() as session:
        resource = await session.scalar(select(SourceResource))
        version_count = await session.scalar(select(func.count(DocumentVersion.id)))
        chunk_count = await session.scalar(select(func.count(DocumentChunk.id)))
        entity = await session.scalar(
            select(CampusEntityRecord).where(
                CampusEntityRecord.document_version_id == resource.current_version_id
            )
        )
        current = await session.get(DocumentVersion, resource.current_version_id)
    assert resource is not None
    assert version_count == 2
    assert current is not None
    assert "补退选" in current.normalized_text
    assert chunk_count >= 2
    assert entity is not None
    assert entity.entity_type == "notice"
    assert "2026级" in entity.audience_scopes
    assert entity.deadline_at is not None
    assert entity.deadline_at.day == 2
    assert len(list((tmp_path / "snapshots").rglob("*.gz"))) == 2

    memory = CampusMemorySearchTool(database)
    result = await memory.run(
        CampusMemorySearchArguments(
            query="补退选",
            top_k=3,
        ),
        "trace_memory",
    )
    assert result.status == "ok"
    assert len(result.evidence) == 1
    assert result.evidence[0].document_version_id == current.id
    assert "补退选" in result.evidence[0].excerpt
    assert result.data["retrieval"] == "sqlite-fts5-hybrid-rrf"
    assert result.data["query"] == "补退选"

    await ingestion.close()
    await client.aclose()
    await database.close()


def _lecture_registry(path: Path) -> None:
    path.write_text(
        """
version: 1
sources:
  - id: test-lectures
    name: 测试讲座
    owner_department: 测试学校
    base_url: https://lectures.test/lectureExternal
    allowed_hosts: [lectures.test]
    visibility: public
    authority_level: official
    acquisition_methods: [public_api]
    poll_interval_seconds: 300
    rate_limit_per_minute: 120
    default_ttl_seconds: 3600
    live_required_for: [讲座时间]
    parser_profile: lecture_json
    snapshot_policy: sanitized
    enabled: true
    connector:
      kind: lecture_api
      list_endpoint: https://lectures.test/api/list
      detail_endpoint: https://lectures.test/api/detail
      public_detail_template: https://lectures.test/lectureExternal/detail?id={id}
      page_size: 10
      max_resources_per_run: 10
""".strip(),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_lecture_full_discovery_excludes_expired_events(tmp_path: Path) -> None:
    registry_path = tmp_path / "lecture-sources.yaml"
    _lecture_registry(registry_path)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'lectures.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "lecture-snapshots"),
    )
    database = Database(settings)
    source = SourceRegistry(database, registry_path).require("test-lectures")
    now = datetime.now(UTC)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/list"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "data": [
                        {
                            "id": "expired",
                            "name": "已经结束的讲座",
                            "endTime": int((now - timedelta(days=1)).timestamp() * 1000),
                        },
                        {
                            "id": "upcoming",
                            "name": "即将开始的讲座",
                            "endTime": int((now + timedelta(days=1)).timestamp() * 1000),
                        },
                    ]
                },
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    fetcher = SafeSourceFetcher(max_download_bytes=1024 * 1024, client=client)
    resources = await LectureApiConnector(fetcher).discover(source, full_scan=True)

    assert [item.external_id for item in resources] == ["upcoming"]

    await fetcher.close()
    await client.aclose()
    await database.close()


@pytest.mark.asyncio
async def test_lecture_connector_snapshots_only_allowlisted_fields(tmp_path: Path) -> None:
    registry_path = tmp_path / "lecture-sources.yaml"
    _lecture_registry(registry_path)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'lectures.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "lecture-snapshots"),
    )
    database = Database(settings)
    await database.initialize()
    registry = SourceRegistry(database, settings.resolved_source_registry_path)
    await registry.sync_definitions()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/list":
            payload = {
                "code": 200,
                "data": {
                    "data": [
                        {
                            "id": "lecture-1",
                            "name": "人工智能与城市未来",
                            "insertTime": 1784995200000,
                            "appliedUserName": "不应保存的负责人",
                        }
                    ]
                },
            }
        else:
            payload = {
                "code": 200,
                "data": {
                    "id": "lecture-1",
                    "name": "人工智能与城市未来",
                    "speakerName": "张老师",
                    "content": "面向全校学生的公开学术讲座。",
                    "address": "图信报告厅",
                    "startTime": 1785081600000,
                    "endTime": 1785088800000,
                    "appliedPhone": "13900000000",
                    "headPhone": "13800000000",
                },
            }
        return httpx.Response(200, json=payload, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ingestion = IngestionService(
        settings=settings,
        database=database,
        registry=registry,
        client=client,
    )
    outcome = await ingestion.sync_source("test-lectures")
    assert outcome.created_count == 1

    async with database.session_factory() as session:
        version = await session.scalar(select(DocumentVersion))
    assert version is not None
    assert "图信报告厅" in version.normalized_text
    assert "13900000000" not in version.normalized_text
    snapshot_path = next((tmp_path / "lecture-snapshots").rglob("*.gz"))
    with gzip.open(snapshot_path, "rt", encoding="utf-8") as handle:
        snapshot = handle.read()
    assert "appliedPhone" not in snapshot
    assert "headPhone" not in snapshot
    assert "13900000000" not in snapshot

    await ingestion.close()
    await client.aclose()
    await database.close()


def _cms_registry(path: Path) -> None:
    path.write_text(
        """
version: 1
sources:
  - id: test-learning-cms
    name: 测试教学通知
    owner_department: 测试信息中心
    base_url: https://course.test/hzcu
    allowed_hosts: [course.test]
    visibility: public
    authority_level: official
    acquisition_methods: [public_api]
    poll_interval_seconds: 300
    rate_limit_per_minute: 120
    default_ttl_seconds: 3600
    live_required_for: [教学通知]
    parser_profile: cms_message_json
    snapshot_policy: sanitized
    enabled: true
    connector:
      kind: cms_api
      list_endpoint: https://course.test/api/messages
      public_detail_template: https://course.test/hzcu/channeldetail?code={channel_code}&id={id}
      channels:
        - code: tongzhi
          channel_id: channel-1
      page_size: 10
      max_resources_per_run: 10
""".strip(),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_public_cms_connector_sanitizes_and_indexes_notices(tmp_path: Path) -> None:
    registry_path = tmp_path / "cms-sources.yaml"
    _cms_registry(registry_path)
    settings = Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'cms.db'}",
        source_registry_path=str(registry_path),
        snapshot_directory=str(tmp_path / "cms-snapshots"),
    )
    database = Database(settings)
    await database.initialize()
    registry = SourceRegistry(database, settings.resolved_source_registry_path)
    await registry.sync_definitions()
    observed_requests: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "data": [
                        {
                            "id": "message-1",
                            "title": "关于2026级本科生课程补选的通知",
                            "author": "教务处",
                            "authorOffice": "教务处",
                            "content": (
                                "<p>2026级本科生请登录教学平台完成补选。</p>"
                                "<p>补选截止时间：2026年8月2日17:00，逾期不再办理。</p>"
                            ),
                            "status": "public",
                            "channelId": "channel-1",
                            "type": "news",
                            "publicTime": 1784995200000,
                            "managerPhone": "13900000000",
                            "internalOwner": "不应保存的负责人",
                        }
                    ]
                },
            },
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ingestion = IngestionService(
        settings=settings,
        database=database,
        registry=registry,
        client=client,
    )
    first = await ingestion.sync_source("test-learning-cms")
    second = await ingestion.sync_source("test-learning-cms")
    assert first.created_count == 1
    assert second.unchanged_count == 1
    assert observed_requests == [
        {
            "code": "tongzhi",
            "channelId": "channel-1",
            "pageNum": 0,
            "pageSize": 10,
        },
        {
            "code": "tongzhi",
            "channelId": "channel-1",
            "pageNum": 0,
            "pageSize": 10,
        },
    ]

    async with database.session_factory() as session:
        version = await session.scalar(select(DocumentVersion))
        chunks = list((await session.scalars(select(DocumentChunk))).all())
        entity = await session.scalar(select(CampusEntityRecord))
    assert version is not None
    assert "课程补选" in version.normalized_text
    assert "13900000000" not in version.normalized_text
    assert chunks
    assert entity is not None
    assert entity.entity_type == "notice"
    assert entity.deadline_at is not None
    assert entity.deadline_at.day == 2
    assert "2026级" in entity.audience_scopes

    snapshot_path = next((tmp_path / "cms-snapshots").rglob("*.gz"))
    with gzip.open(snapshot_path, "rt", encoding="utf-8") as handle:
        snapshot = handle.read()
    assert "managerPhone" not in snapshot
    assert "internalOwner" not in snapshot
    assert "13900000000" not in snapshot

    await ingestion.close()
    await client.aclose()
    await database.close()
