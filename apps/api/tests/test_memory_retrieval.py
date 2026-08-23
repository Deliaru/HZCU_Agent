from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.models import (
    DocumentVersion,
    SourceDefinitionRecord,
    SourceResource,
    new_id,
)
from hzcu_agent.tools.campus_document import (
    CampusDocumentExplorer,
    CampusDocumentFindArguments,
    CampusDocumentInspectArguments,
    CampusDocumentReadArguments,
    CampusDocumentReadLocatorArguments,
)
from hzcu_agent.tools.campus_memory import (
    CampusMemorySearchArguments,
    CampusMemorySearchTool,
    _evidence_excerpt,
    _fts_match_expression,
    _lexical_backoff_terms,
    _normalize_query,
)


async def _database(tmp_path: Path) -> Database:
    database = Database(
        Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}",
        )
    )
    await database.initialize()
    return database


async def _add_document(
    database: Database,
    *,
    source_id: str,
    uri: str,
    title: str,
    body: str,
    visibility: str = "public",
    enabled: bool = True,
    quality: str = "accepted",
    published_at: datetime | None = None,
    resource_type: str = "html",
    media_type: str = "text/html",
    metadata: dict | None = None,
) -> str:
    version_id = new_id("ver")
    resource_id = new_id("res")
    now = datetime(2026, 7, 27, tzinfo=UTC)
    async with database.session_factory() as session:
        source = await session.get(SourceDefinitionRecord, source_id)
        if source is None:
            session.add(
                SourceDefinitionRecord(
                    id=source_id,
                    name=source_id,
                    owner_department="测试部门",
                    base_url="https://example.test/",
                    allowed_hosts=["example.test"],
                    visibility=visibility,
                    authority_level="official",
                    acquisition_methods=["scheduled_crawl"],
                    connector_kind="linked_html",
                    poll_interval_seconds=3600,
                    rate_limit_per_minute=60,
                    default_ttl_seconds=3600,
                    live_required_for=[],
                    parser_profile="test_html",
                    snapshot_policy="raw",
                    config_payload={},
                    config_hash=source_id,
                    enabled=enabled,
                )
            )
        resource = SourceResource(
            id=resource_id,
            source_id=source_id,
            canonical_uri=uri,
            resource_type=resource_type,
            first_seen_at=now,
            last_seen_at=now,
        )
        session.add(resource)
        await session.flush()
        session.add(
            DocumentVersion(
                id=version_id,
                resource_id=resource_id,
                content_hash=version_id,
                media_type=media_type,
                normalized_text=body,
                title=title,
                publisher="测试部门",
                published_at=published_at or now,
                observed_at=now,
                parser_version="test_html-v6",
                quality_status=quality,
                document_metadata=metadata or {},
            )
        )
        await session.flush()
        resource.current_version_id = version_id
        await session.commit()
    return version_id


@pytest.mark.asyncio
async def test_fts_initializes_backfills_and_tracks_new_versions(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    first_id = await _add_document(
        database,
        source_id="public-source",
        uri="https://example.test/calendar",
        title="2026—2027学年校历",
        body="老生报到时间为9月13日，本科生开课时间为9月14日。",
    )
    memory = CampusMemorySearchTool(database)

    first = await memory.run(
        CampusMemorySearchArguments(query="2026-2027学年校历"),
        "trace-first",
    )
    await memory.initialize()
    async with database.session_factory() as session:
        indexed_before = await session.scalar(text("SELECT count(*) FROM campus_search_fts_v2"))

    second_id = await _add_document(
        database,
        source_id="notice-source",
        uri="https://example.test/midterm",
        title="大学生创新训练计划项目中期检查通知",
        body="国创项目和校创项目均须参加中期检查。",
    )
    second = await memory.run(
        CampusMemorySearchArguments(query="校创 中期检查"),
        "trace-second",
    )
    rebuilt = await memory.rebuild()
    recreated = await memory.recreate()

    assert [item.document_version_id for item in first.evidence] == [first_id]
    assert [item.document_version_id for item in second.evidence] == [second_id]
    assert "校创" in second.data["exact_short_terms"]
    assert indexed_before == 1
    assert rebuilt == 2
    assert recreated == 2
    await database.close()


@pytest.mark.asyncio
async def test_fts_enforces_current_visibility_and_quality_boundaries(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    public_id = await _add_document(
        database,
        source_id="public-source",
        uri="https://example.test/public",
        title="本科生课程补退选安排",
        body="课程补退选从9月14日开始。",
    )
    async with database.session_factory() as session:
        public_resource = await session.scalar(
            select(SourceResource).where(
                SourceResource.canonical_uri == "https://example.test/public"
            )
        )
        assert public_resource is not None
        session.add(
            DocumentVersion(
                id=new_id("ver"),
                resource_id=public_resource.id,
                content_hash=new_id("hash"),
                media_type="text/html",
                normalized_text="历史专属校历关键词仅存在于非当前版本。",
                title="历史专属校历关键词",
                publisher="测试部门",
                published_at=datetime(2025, 1, 1, tzinfo=UTC),
                observed_at=datetime(2025, 1, 1, tzinfo=UTC),
                parser_version="test_html-v6",
                quality_status="accepted",
                document_metadata={},
            )
        )
        await session.commit()
    campus_id = await _add_document(
        database,
        source_id="campus-source",
        uri="https://example.test/campus",
        title="本科生课程补退选校内通知",
        body="课程补退选从9月15日开始。",
        visibility="campus",
    )
    await _add_document(
        database,
        source_id="disabled-source",
        uri="https://example.test/disabled",
        title="本科生课程补退选停用来源",
        body="停用来源不应出现。",
        enabled=False,
    )
    await _add_document(
        database,
        source_id="rejected-source",
        uri="https://example.test/rejected",
        title="本科生课程补退选无效材料",
        body="无效材料不应出现。",
        quality="rejected",
    )
    await _add_document(
        database,
        source_id="old-source",
        uri="https://example.test/old",
        title="本科生课程补退选旧材料",
        body="旧材料不应出现。",
        quality="excluded_temporal",
        published_at=datetime(2022, 12, 31, tzinfo=UTC),
    )
    accepted_historical_id = await _add_document(
        database,
        source_id="historical-rules-source",
        uri="https://example.test/historical-rules",
        title="2021级第二三四课堂历史规则",
        body="2021级学生的志愿服务按每小时认定素质分值。",
        published_at=datetime(2021, 9, 1, tzinfo=UTC),
    )
    memory = CampusMemorySearchTool(database)

    public = await memory.run(
        CampusMemorySearchArguments(query="本科生课程补退选", top_k=12),
        "trace-public",
        allowed_visibilities=frozenset({"public"}),
    )
    campus = await memory.run(
        CampusMemorySearchArguments(query="本科生课程补退选", top_k=12),
        "trace-campus",
        allowed_visibilities=frozenset({"public", "campus"}),
    )
    historical = await memory.run(
        CampusMemorySearchArguments(query="历史专属校历关键词"),
        "trace-historical",
        allowed_visibilities=frozenset({"public", "campus"}),
    )
    accepted_historical = await memory.run(
        CampusMemorySearchArguments(query="2021级 第二三四课堂 历史规则"),
        "trace-accepted-historical",
        allowed_visibilities=frozenset({"public", "campus"}),
    )

    assert {item.document_version_id for item in public.evidence} == {public_id}
    assert {item.document_version_id for item in campus.evidence} == {
        public_id,
        campus_id,
    }
    assert all(item.fresh_until is None for item in campus.evidence)
    assert historical.evidence == []
    assert [item.document_version_id for item in accepted_historical.evidence] == [
        accepted_historical_id
    ]
    await database.close()


@pytest.mark.asyncio
async def test_title_weight_and_url_dedup_preserve_fts_order(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    title_id = await _add_document(
        database,
        source_id="title-source",
        uri="https://example.test/shared",
        title="奖学金评审办法",
        body="本办法说明申请对象和评审程序。",
    )
    await _add_document(
        database,
        source_id="body-source",
        uri="https://example.test/body",
        title="学生资助工作说明",
        body="材料包括奖学金评审办法以及其他学生资助规定。",
    )
    await _add_document(
        database,
        source_id="duplicate-source",
        uri="https://example.test/shared",
        title="学生资助材料转载",
        body="转载材料包括奖学金评审办法。",
    )
    memory = CampusMemorySearchTool(database)

    result = await memory.run(
        CampusMemorySearchArguments(query="奖学金评审办法", top_k=8),
        "trace-ranking",
    )

    assert result.evidence[0].document_version_id == title_id
    assert len({item.canonical_url for item in result.evidence}) == len(result.evidence)
    assert result.data["retrieval"] == "sqlite-fts5-hybrid-rrf"
    await database.close()


@pytest.mark.asyncio
async def test_relaxed_search_promotes_complete_attachment_over_thin_shells(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    plan_id = await _add_document(
        database,
        source_id="engineering-source",
        uri="https://example.test/engineering-plan.pdf",
        title="工程学院2025级本科专业培养方案.pdf",
        body=(
            ("工程学院本科教学资料。" * 600)
            + "智能建造专业培养方案包括培养目标、毕业要求、课程体系和最低学分。"
        ),
        resource_type="pdf",
        media_type="application/pdf",
    )
    await _add_document(
        database,
        source_id="engineering-source",
        uri="https://example.test/engineering-plan",
        title="工程学院2025级本科专业培养方案",
        body="工程学院2025级本科专业培养方案，附件见页面下方。",
    )
    await _add_document(
        database,
        source_id="engineering-source",
        uri="https://example.test/engineering-plan-icon.png",
        title="工程学院2025级本科专业培养方案",
        body="工程学院2025级本科专业培养方案 智能建造",
        quality="image_pending_transcription",
        resource_type="image",
        media_type="image/png",
    )
    for index in range(30):
        await _add_document(
            database,
            source_id="engineering-source",
            uri=f"https://example.test/news/{index}",
            title=f"工程学院智能建造专业2023级培养方案研讨会 {index}",
            body="工程学院围绕智能建造本科专业培养方案开展研讨。",
        )
    memory = CampusMemorySearchTool(database)

    result = await memory.run(
        CampusMemorySearchArguments(
            query="工程学院 2025级 本科专业培养方案 智能建造 毕业学分",
            top_k=8,
        ),
        "trace-engineering-plan",
    )

    assert result.data["match_mode"] == "relaxed"
    assert result.evidence[0].document_version_id == plan_id
    assert all(
        item.canonical_url != "https://example.test/engineering-plan-icon.png"
        for item in result.evidence
    )
    await database.close()


@pytest.mark.asyncio
async def test_model_can_search_then_read_a_selected_full_layout_document(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    layout = (
        "【PDF 第 1 页】\n机械电子工程专业培养方案。\n"
        + ("培养目标与毕业要求。\n" * 300)
        + "【PDF 第 10 页】\n"
        "机械电子工程专业指导性课程计划\n"
        "课程号 中文课程名             1     2     3     4\n"
        "A01001 微积分 I               3-3\n"
        "A01002 微积分 II                    5-1\n"
        "B03100 工程力学 I                         2.5-0\n"
    )
    expected_id = await _add_document(
        database,
        source_id="engineering-source",
        uri="https://example.test/mechatronics.pdf",
        title="2024级本科课程计划-机械电子工程.pdf",
        body=layout,
        resource_type="pdf",
        media_type="application/pdf",
    )
    await _add_document(
        database,
        source_id="engineering-source",
        uri="https://example.test/mechatronics-top-up.pdf",
        title="2024级本科课程计划-机械电子工程专升本.pdf",
        body=layout.replace(
            "机械电子工程专业培养方案",
            "机械电子工程专升本培养方案",
        ).replace(
            "机械电子工程专业指导性课程计划",
            "机械电子工程专业(专升本)指导性课程计划",
        ),
        resource_type="pdf",
        media_type="application/pdf",
    )
    memory = CampusMemorySearchTool(database)

    search = await memory.run(
        CampusMemorySearchArguments(
            query="机械电子工程 培养方案",
            top_k=8,
        ),
        "trace-search",
    )
    explorer = CampusDocumentExplorer(database)
    inspection = await explorer.inspect(
        CampusDocumentInspectArguments(document_version_id=expected_id),
        "trace-inspect",
    )
    found = await explorer.find(
        CampusDocumentFindArguments(
            document_version_id=expected_id,
            query="指导性课程计划",
        ),
        "trace-find",
    )
    page = await explorer.read_locator(
        CampusDocumentReadLocatorArguments(
            document_version_id=expected_id,
            locator=10,
        ),
        "trace-page",
    )
    result = await explorer.read(
        CampusDocumentReadArguments(
            document_version_id=expected_id,
            offset=found.data["matches"][0]["offset"],
            max_chars=1_000,
        ),
        "trace-segment",
    )

    assert expected_id in {item.document_version_id for item in search.evidence}
    assert [item["page"] for item in inspection.data["locators"]] == [1, 10]
    assert [item["locator"] for item in inspection.data["locators"]] == [1, 10]
    assert found.data["match_count"] == 1
    assert found.data["matches"][0]["page"] == 10
    assert page.data["locator_kind"] == "page"
    assert page.data["locator"] == 10
    assert page.data["previous_locator"] == 1
    assert page.data["next_locator"] is None
    assert page.data["representation"] == "continuous_text_with_line_column_map"
    assert "【同一定位单元的通用版面坐标视图】" in page.evidence[0].excerpt
    assert "[c000]课程号" in page.evidence[0].excerpt
    assert "微积分 II" in page.evidence[0].excerpt
    assert result.data["returned_chars"] <= 1_000
    assert result.data["start_page"] in {1, 10}
    assert result.evidence[0].document_version_id == expected_id
    assert "1     2     3     4" in result.evidence[0].excerpt
    assert "微积分 II                    5-1" in result.evidence[0].excerpt
    await database.close()


@pytest.mark.asyncio
async def test_locator_reader_uses_fixed_blocks_for_non_pdf_materials(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    body = ("普通通知正文。" * 700) + "报名材料清单在第二个文本块。"
    version_id = await _add_document(
        database,
        source_id="notice-source",
        uri="https://example.test/notice",
        title="普通通知",
        body=body,
    )
    explorer = CampusDocumentExplorer(database)

    inspection = await explorer.inspect(
        CampusDocumentInspectArguments(document_version_id=version_id),
        "trace-inspect-html",
    )
    result = await explorer.read_locator(
        CampusDocumentReadLocatorArguments(
            document_version_id=version_id,
            locator=2,
        ),
        "trace-block",
    )

    assert inspection.data["locator_kind"] == "block"
    assert result.data["locator_kind"] == "block"
    assert result.data["offset"] == 4_000
    assert result.data["representation"] == "continuous_text"
    assert "通用版面坐标视图" not in result.evidence[0].excerpt
    assert result.evidence[0].excerpt.startswith("【定位单元：文本块 2；连续原文】")
    await database.close()


@pytest.mark.asyncio
async def test_zero_hit_relaxes_phrase_join_and_short_terms(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    expected_id = await _add_document(
        database,
        source_id="selection-source",
        uri="https://example.test/course-selection",
        title="2026-2027学年第一学期选课时间安排",
        body="当课程容量已满时，学生可以提交容量已满课程申请。",
    )
    memory = CampusMemorySearchTool(database)

    stacked_phrases = await memory.run(
        CampusMemorySearchArguments(
            query="2026-2027学年第一学期 本科生选课通知",
        ),
        "trace-relaxed-phrases",
    )
    missing_short_term = await memory.run(
        CampusMemorySearchArguments(query="课程容量已满 加课"),
        "trace-relaxed-short",
    )

    assert stacked_phrases.data["strict_candidate_rows"] == 0
    assert stacked_phrases.data["match_mode"] == "relaxed"
    assert [item.document_version_id for item in stacked_phrases.evidence] == [expected_id]
    assert missing_short_term.data["strict_candidate_rows"] == 0
    assert missing_short_term.data["match_mode"] == "relaxed"
    assert missing_short_term.data["exact_short_terms"] == []
    assert [item.document_version_id for item in missing_short_term.evidence] == [expected_id]
    await database.close()


@pytest.mark.asyncio
async def test_zero_hit_long_phrase_uses_business_agnostic_lexical_backoff(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    notice_id = await _add_document(
        database,
        source_id="student-affairs-source",
        uri="https://example.test/provincial-award-notice",
        title="2024-2025学年省政府奖学金评选工作的通知",
        body=(
            ("学生工作通知。" * 300)
            + "省政府奖学金奖励标准为每生每年6000元，申请人须为二年级以上学生。"
        ),
    )
    handbook_id = await _add_document(
        database,
        source_id="handbook-source",
        uri="https://example.test/student-handbook.pdf",
        title="浙江省政府奖学金评审及管理办法",
        body="本办法规定浙江省政府奖学金的申请条件、评审程序和奖励标准。",
        resource_type="pdf",
        media_type="application/pdf",
    )
    await _add_document(
        database,
        source_id="distractor-source",
        uri="https://example.test/unrelated-award",
        title="研究生国家奖学金评选要求",
        body="本通知只适用于研究生国家奖学金。",
    )
    memory = CampusMemorySearchTool(database)

    requirements = await memory.run(
        CampusMemorySearchArguments(query="省政府奖学金评选要求"),
        "trace-long-requirements",
    )
    regulations = await memory.run(
        CampusMemorySearchArguments(query="浙江省政府奖学金评选办法"),
        "trace-long-regulations",
    )

    assert requirements.data["strict_candidate_rows"] == 0
    assert requirements.data["match_mode"] == "relaxed"
    assert {item.document_version_id for item in requirements.evidence[:2]} == {
        notice_id,
        handbook_id,
    }
    notice = next(item for item in requirements.evidence if item.document_version_id == notice_id)
    assert "每生每年6000元" in notice.excerpt
    assert regulations.data["strict_candidate_rows"] == 0
    assert regulations.data["match_mode"] == "relaxed"
    assert regulations.evidence[0].document_version_id == handbook_id
    assert requirements.data["lexical_backoff_terms"]
    assert regulations.data["lexical_backoff_terms"]
    await database.close()


@pytest.mark.asyncio
async def test_abstract_enumeration_recalls_scope_page_without_source_hint(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    expected_id = await _add_document(
        database,
        source_id="undergraduate-admissions",
        uri="https://example.test/majors",
        title="院系专业",
        body=(
            "首页\n院系专业\n院系设置\n工程学院\n能源与环境系统工程\n"
            "智能建造\n智能制造工程\n机械电子工程\n土木工程\n招生咨询\n联系我们"
        ),
    )
    for index in range(24):
        await _add_document(
            database,
            source_id="engineering-site",
            uri=f"https://example.test/engineering/{index}",
            title=f"工程学院2025级本科专业培养方案研讨材料 {index}",
            body="工程学院围绕智能建造专业培养方案开展研讨。",
        )
    memory = CampusMemorySearchTool(database)

    result = await memory.run(
        CampusMemorySearchArguments(query="工程学院有几个专业", top_k=12),
        "trace-abstract-enumeration",
    )

    assert expected_id in {
        item.document_version_id for item in result.evidence[:3]
    }
    assert {item.source_id for item in result.evidence[:6]} >= {
        "undergraduate-admissions",
        "engineering-site",
    }
    assert result.data["answer_shape"] == "enumeration"
    assert result.data["coverage_risk"] is False
    await database.close()


@pytest.mark.asyncio
async def test_abstract_enumeration_ignores_answer_format_instructions(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    expected_id = await _add_document(
        database,
        source_id="undergraduate-admissions",
        uri="https://example.test/majors",
        title="院系专业",
        body=(
            "首页\n院系专业\n院系设置\n工程学院\n能源与环境系统工程\n"
            "智能建造\n智能制造工程\n机械电子工程\n土木工程\n招生咨询\n联系我们"
        ),
    )
    for index in range(24):
        await _add_document(
            database,
            source_id="engineering-site",
            uri=f"https://example.test/engineering/{index}",
            title=f"工程学院2025级本科专业培养方案研讨材料 {index}",
            body="工程学院围绕智能建造专业培养方案开展研讨。",
        )
    memory = CampusMemorySearchTool(database)

    result = await memory.run(
        CampusMemorySearchArguments(
            query="工程学院有几个专业？请列出全部专业名称。",
            top_k=12,
        ),
        "trace-answer-format-instructions",
    )

    assert expected_id in {
        item.document_version_id for item in result.evidence[:3]
    }
    assert result.data["answer_shape"] == "enumeration"
    assert result.data["coverage_risk"] is False
    await database.close()


@pytest.mark.asyncio
async def test_source_hints_boost_but_never_filter_other_sources(tmp_path: Path) -> None:
    database = await _database(tmp_path)
    expected_id = await _add_document(
        database,
        source_id="authoritative-source",
        uri="https://example.test/complete-list",
        title="学院完整名单",
        body="学院完整名单包括工程学院、商学院、医学院和法学院。",
    )
    await _add_document(
        database,
        source_id="hinted-source",
        uri="https://example.test/unrelated",
        title="工程学院新闻",
        body="工程学院召开教师会议。",
    )
    memory = CampusMemorySearchTool(database)

    result = await memory.run(
        CampusMemorySearchArguments(
            query="学院完整名单",
            source_ids=["hinted-source"],
            top_k=8,
        ),
        "trace-soft-source",
    )

    assert expected_id in {item.document_version_id for item in result.evidence}
    await database.close()


@pytest.mark.asyncio
async def test_broken_source_routing_index_falls_back_to_global_retrieval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = await _database(tmp_path)
    expected_id = await _add_document(
        database,
        source_id="teaching-source",
        uri="https://example.test/course-capacity",
        title="课程容量申请安排",
        body="课程容量已满时可以在教务系统提交容量申请。",
    )

    async def broken_source_index(_database: Database) -> None:
        raise OperationalError("source index unavailable", {}, RuntimeError())

    monkeypatch.setattr(
        "hzcu_agent.tools.campus_hybrid.ensure_source_search_index",
        broken_source_index,
    )
    memory = CampusMemorySearchTool(database)

    result = await memory.run(
        CampusMemorySearchArguments(query="课程容量申请", top_k=8),
        "trace-source-index-fallback",
    )

    assert expected_id in {item.document_version_id for item in result.evidence}
    assert result.data["routed_sources"] == []
    await database.close()


@pytest.mark.asyncio
async def test_article_images_collapse_to_parent_but_standalone_images_survive(
    tmp_path: Path,
) -> None:
    database = await _database(tmp_path)
    parent_uri = "https://example.test/admissions-list"
    await _add_document(
        database,
        source_id="admissions-source",
        uri=parent_uri,
        title="本科招生完整名单",
        body="本科招生完整名单\n工程学院\n商学院\n医学院\n法学院",
    )
    child_uri = "https://example.test/image.png"
    await _add_document(
        database,
        source_id="admissions-source",
        uri=child_uri,
        title="image.png",
        body="本科招生完整名单 工程学院 商学院 医学院 法学院",
        resource_type="image",
        media_type="image/png",
        metadata={"article_image": True, "parent_uri": parent_uri},
    )
    calendar_id = await _add_document(
        database,
        source_id="calendar-source",
        uri="https://example.test/calendar.png",
        title="2026-2027学年校历_01.png",
        body="2026-2027学年校历 老生报到9月13日 本科生开课9月14日",
        resource_type="image",
        media_type="image/png",
        metadata={"article_image": False},
    )
    memory = CampusMemorySearchTool(database)

    list_result = await memory.run(
        CampusMemorySearchArguments(query="本科招生完整名单", top_k=8),
        "trace-parent-image",
    )
    calendar_result = await memory.run(
        CampusMemorySearchArguments(query="2026-2027学年校历", top_k=8),
        "trace-standalone-image",
    )

    assert parent_uri in {item.canonical_url for item in list_result.evidence}
    assert child_uri not in {item.canonical_url for item in list_result.evidence}
    assert calendar_id in {
        item.document_version_id for item in calendar_result.evidence
    }
    await database.close()


def test_scalar_contract_and_query_syntax_normalization() -> None:
    with pytest.raises(ValidationError):
        CampusMemorySearchArguments.model_validate({"queries": ["校历"], "top_k": 8})

    normalized = _normalize_query("  2026－2027学年“校历”  ")
    expression = _fts_match_expression(normalized)

    assert normalized == "2026-2027学年校历"
    assert expression is not None
    assert " OR " in expression
    assert '"2026-2027学年校历"' in expression
    assert _fts_match_expression("选课时间 容量申请", operator="OR") == ('"选课时间" OR "容量申请"')
    assert _lexical_backoff_terms("省政府奖学金评选要求") == [
        "省政府奖学",
        "府奖学金评",
        "金评选要求",
    ]


def test_title_only_fts_hit_still_returns_body_context() -> None:
    excerpt = _evidence_excerpt(
        body=(
            "正文图片:浙大城市学院2026-2027学年校历。"
            "本科生新生9月11日报到注册，本科生老生9月13日报到注册，"
            "全校9月14日开始上课。"
        ),
        query="2026-2027学年 校历",
        match_snippet="正文图片:浙大城市学院2026-2027学年校历。",
    )

    assert "9月11日" in excerpt
    assert "9月13日" in excerpt
    assert "9月14日" in excerpt


def test_long_document_excerpt_prefers_dense_answer_passage_over_cover() -> None:
    excerpt = _evidence_excerpt(
        body=(
            "2025级本科专业培养方案 工程学院 机械电子工程 智能建造\n"
            + ("其他专业培养内容和课程表。" * 500)
            + (
                "智能建造专业培养方案。计划学制及授予学位："
                "计划学制四年，授予学位工学学士。"
                "学分分配和最低毕业学分要求：最低毕业学分要求165.0。"
                "专业核心课程包括工程制图与BIM建模、智能感知与物联网。"
            )
        ),
        query="工程学院 2025级 智能建造 学制 授予学位 毕业学分 核心课程",
        match_snippet="2025级本科专业培养方案 工程学院 机械电子工程 智能建造",
    )

    assert "计划学制四年" in excerpt
    assert "工学学士" in excerpt
    assert "165.0" in excerpt
    assert "工程制图与BIM建模" in excerpt
