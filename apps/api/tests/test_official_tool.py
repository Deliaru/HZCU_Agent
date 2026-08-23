from datetime import datetime

import httpx
import pytest

from hzcu_agent.tools.hzcu_official import (
    HzcuOfficialSearchTool,
    OfficialSearchArguments,
    decode_resource_ref,
    encode_resource_ref,
    is_allowed_official_url,
    normalize_official_url,
)


def test_official_url_allowlist_and_resource_refs() -> None:
    valid_urls = [
        "https://www.hzcu.edu.cn/info/1001/123.htm",
        "https://jwc.hzcu.edu.cn/",
        "https://hzcu.edu.cn/",
    ]
    invalid_urls = [
        "http://www.hzcu.edu.cn/info/1.htm",
        "https://hzcu.edu.cn.evil.example/info/1.htm",
        "https://evil.example/?next=hzcu.edu.cn",
        "https://user@hzcu.edu.cn/info/1.htm",
        "https://hzcu.edu.cn:not-a-port/info/1.htm",
        "file:///etc/passwd",
    ]

    assert all(is_allowed_official_url(url) for url in valid_urls)
    assert not any(is_allowed_official_url(url) for url in invalid_urls)
    assert (
        normalize_official_url("http://jwc.hzcu.edu.cn/info/1.htm")
        == "https://jwc.hzcu.edu.cn/info/1.htm"
    )

    resource_ref = encode_resource_ref(valid_urls[0])
    assert decode_resource_ref(resource_ref) == valid_urls[0]
    with pytest.raises(ValueError):
        decode_resource_ref("hzcu-official:aHR0cHM6Ly9ldmlsLmV4YW1wbGUv")


@pytest.mark.asyncio
async def test_live_search_parser_filters_external_sites_and_hydrates_content() -> None:
    search_html = """
    <html><body>
      <ul class="ul-txtq3">
        <li>
          <a class="con" href="/info/1001/12345.htm"><h3>2026级学生选课通知</h3></a>
          <span class="date2">2026-07-25</span>
          <p>学校发布了新学期选课安排。</p>
        </li>
        <li>
          <a class="con" href="https://outside.example/notice"><h3>外部结果</h3></a>
          <span class="date2">2026-07-26</span>
        </li>
      </ul>
    </body></html>
    """
    detail_html = """
    <html><body>
      <h1>关于2026级学生选课工作的通知</h1>
      <div class="v_news_content">
        选课分为预选、正选和补退选三个阶段。学生应在教务系统规定时间内完成操作。
      </div>
      <div>发布日期：2026年07月25日</div>
    </body></html>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, text=search_html, request=request)
        return httpx.Response(200, text=detail_html, request=request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    tool = HzcuOfficialSearchTool(client)
    result = await tool.run(
        OfficialSearchArguments(query="选课", limit=5),
        trace_id="trace_test",
    )
    await client.aclose()

    assert result.status == "ok"
    assert result.data["result_count"] == 1
    assert len(result.evidence) == 1
    assert result.evidence[0].title == "关于2026级学生选课工作的通知"
    assert "预选、正选和补退选" in result.evidence[0].excerpt
    assert result.evidence[0].published_at == datetime.fromisoformat("2026-07-25T00:00:00+08:00")


@pytest.mark.asyncio
async def test_live_search_ranks_relevance_before_recency() -> None:
    search_html = """
    <ul class="ul-txtq3">
      <li>
        <a class="con" href="/info/1001/news.htm"><h3>学校举行最新校园活动</h3></a>
        <span class="date2">2026-07-26</span>
        <p>校园新闻。</p>
      </li>
      <li>
        <a class="con" href="/info/1001/innovation.htm">
          <h3>关于申报大学生创新创业训练计划项目的通知</h3>
        </a>
        <span class="date2">2025-03-20</span>
        <p>国家级大学生创新创业训练计划项目申报安排。</p>
      </li>
    </ul>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, text=search_html, request=request)
        return httpx.Response(503, request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tool = HzcuOfficialSearchTool(client)
    result = await tool.run(
        OfficialSearchArguments(query="国家级大学生创新创业训练计划 申报", limit=1),
        trace_id="trace_relevance",
    )
    await client.aclose()

    assert result.status == "ok"
    assert len(result.evidence) == 1
    assert "创新创业训练计划" in result.evidence[0].title


@pytest.mark.asyncio
async def test_detail_redirect_cannot_escape_official_allowlist() -> None:
    requested_hosts: list[str] = []
    search_html = """
    <ul class="ul-txtq3"><li>
      <a class="con" href="/redirect.htm"><h3>校内页面</h3></a>
      <p>搜索结果摘要足够作为降级证据。</p>
    </li></ul>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        requested_hosts.append(request.url.host)
        if request.method == "POST":
            return httpx.Response(200, text=search_html, request=request)
        return httpx.Response(
            302,
            headers={"Location": "https://evil.example/private"},
            request=request,
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tool = HzcuOfficialSearchTool(client)
    result = await tool.run(
        OfficialSearchArguments(query="通知", limit=2),
        trace_id="trace_redirect",
    )
    await client.aclose()

    assert result.status == "ok"
    assert requested_hosts == ["www.hzcu.edu.cn", "www.hzcu.edu.cn"]
    assert len(result.evidence) == 1
    assert result.evidence[0].excerpt == "搜索结果摘要足够作为降级证据。"
