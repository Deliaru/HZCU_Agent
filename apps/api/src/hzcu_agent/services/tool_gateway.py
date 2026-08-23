import asyncio
import logging
from typing import Any

from pydantic import ValidationError

from hzcu_agent.ingestion.service import IngestionService
from hzcu_agent.models import new_id
from hzcu_agent.schemas import InvestigationStep, ToolError, ToolResult
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
)
from hzcu_agent.tools.campus_notices import (
    CampusNoticeSearchArguments,
    CampusNoticeSearchTool,
)
from hzcu_agent.tools.hzcu_official import (
    HzcuOfficialSearchTool,
    OfficialSearchArguments,
)

logger = logging.getLogger(__name__)


class ToolGateway:
    """Allowlisted tool execution boundary. Models never call arbitrary URLs."""

    def __init__(
        self,
        official_search: HzcuOfficialSearchTool,
        campus_memory: CampusMemorySearchTool,
        campus_notices: CampusNoticeSearchTool,
        ingestion: IngestionService,
        campus_documents: CampusDocumentExplorer | None = None,
    ) -> None:
        self._official_search = official_search
        self._campus_memory = campus_memory
        self._campus_documents = campus_documents
        self._campus_notices = campus_notices
        self._ingestion = ingestion
        self._live_backfill_lock = asyncio.Lock()
        self._live_backfill_tasks: set[asyncio.Task[None]] = set()

    def catalog(
        self,
        allowed_visibilities: frozenset[str] | None = None,
        *,
        memory_visibilities: frozenset[str] | None = None,
    ) -> list[dict[str, Any]]:
        visibility = allowed_visibilities or frozenset({"public"})
        memory_visibility = memory_visibilities or visibility
        if "campus" in visibility:
            scope_description = "已认证，可检索 Public 与 Campus 校园材料"
        elif "campus" in memory_visibility:
            scope_description = (
                "匿名试用，可检索 Public 与已批准的 Campus 本地镜像；不具备 Campus 实时查询权限"
            )
        else:
            scope_description = "匿名访问，只可检索 Public 材料"
        tools = [
            {
                "name": "search_campus_memory",
                "description": (
                    "用一个证据目标及最多三个正式近义表达检索本地镜像当前版本，"
                    "返回候选材料、相关正文"
                    "片段和 document_version_id；"
                    f"{scope_description}。"
                    "source_ids 只能从 corpus_sources 选择且仅作软排序，不能排除"
                    "其他来源。每个独立信息点单独调用；搜索片段只用于发现材料；"
                    "当回答依赖表格、附件"
                    "后半部分或跨段上下文时，模型应选择候选并使用 inspect、find、"
                    "read-locator/read-segment 原子工具继续探索。"
                ),
                "input_schema": CampusMemorySearchArguments.model_json_schema(),
                "side_effect": "read_only",
                "authority": "registered_official_sources",
                "corpus_sources": (
                    self._ingestion.source_summaries(memory_visibility)
                    if hasattr(self._ingestion, "source_summaries")
                    else []
                ),
            },
        ]
        if self._campus_documents is not None:
            tools.extend(
                [
                    {
                        "name": self._campus_documents.inspect_name,
                        "description": (
                            "查看模型所选当前文档的通用结构索引：文档元数据、总长度，"
                            "以及 PDF 页或普通文本块的 offset 和短预览。不读取整篇，"
                            "不判断业务主题。"
                        ),
                        "input_schema": CampusDocumentInspectArguments.model_json_schema(),
                        "side_effect": "read_only",
                        "authority": "registered_official_sources",
                    },
                    {
                        "name": self._campus_documents.find_name,
                        "description": (
                            "像在材料内使用查找功能一样，按模型给出的表达在一份当前"
                            "文档中定位，返回命中 offset、PDF 页码和短上下文。不替"
                            "模型选择关键词、章节或业务类型；短上下文用于定位，"
                            "涉及表格行列、顺序或跨段关系时应继续 read locator 或"
                            " read segment。"
                        ),
                        "input_schema": CampusDocumentFindArguments.model_json_schema(),
                        "side_effect": "read_only",
                        "authority": "registered_official_sources",
                    },
                    {
                        "name": self._campus_documents.read_locator_name,
                        "description": (
                            "像人翻到某一页或某个文本块一样，读取 inspect 返回的一个"
                            "定位单元。PDF 页会同时返回完整连续原文和通用行列坐标视图，"
                            "帮助模型自行理解横向表格、分栏和版面关系；普通通知、新闻"
                            "等材料按固定文本块读取。工具不解释主题或选择内容。"
                        ),
                        "input_schema": CampusDocumentReadLocatorArguments.model_json_schema(),
                        "side_effect": "read_only",
                        "authority": "registered_official_sources",
                    },
                    {
                        "name": self._campus_documents.read_name,
                        "description": (
                            "从模型指定的 offset 开始，读取一段连续原文，单次最多"
                            " 12000 字；返回相邻 offset 和 PDF 页范围。适用于 PDF、"
                            "通知、新闻和其他已解析材料，不按主题裁剪。"
                        ),
                        "input_schema": CampusDocumentReadArguments.model_json_schema(),
                        "side_effect": "read_only",
                        "authority": "registered_official_sources",
                    },
                ]
            )
        tools.append(
            {
                "name": "search_official_live",
                "description": ("实时搜索浙大城市学院官网及校内官方子站，并读取最相关的公开页面。"),
                "input_schema": OfficialSearchArguments.model_json_schema(),
                "side_effect": "read_only",
                "authority": "official_public",
            }
        )
        if self._campus_notices is not None and self._campus_notices.enabled:
            tools.append(
                {
                    "name": self._campus_notices.name,
                    "description": (
                        "通过校内直连或已认证 VPN 通道，对可信校园官网主机进行"
                        "软排序、分批扩展的实时发现。来源主题只决定搜索顺序，不会"
                        "排除其他来源；能力固定为 campus_notice.read，不执行申请代办。"
                    ),
                    "input_schema": CampusNoticeSearchArguments.model_json_schema(),
                    "side_effect": "read_only",
                    "authority": "official_campus_notice_only",
                    "available_now": "campus" in visibility,
                }
            )
        return tools

    async def execute(
        self,
        step: InvestigationStep,
        *,
        allowed_visibilities: frozenset[str] | None = None,
        actor_user_id: str | None = None,
    ) -> ToolResult:
        trace_id = new_id("trace")
        tool_payload = step.arguments.tool_payload()
        if step.tool == self._campus_memory.name:
            try:
                arguments = CampusMemorySearchArguments.model_validate(tool_payload)
            except ValidationError as exc:
                return _invalid_arguments_result(step.tool, trace_id, exc)
            return await self._campus_memory.run(
                arguments,
                trace_id,
                allowed_visibilities=allowed_visibilities,
            )

        if self._campus_documents is not None and step.tool == self._campus_documents.inspect_name:
            try:
                arguments = CampusDocumentInspectArguments.model_validate(tool_payload)
            except ValidationError as exc:
                return _invalid_arguments_result(step.tool, trace_id, exc)
            return await self._campus_documents.inspect(
                arguments,
                trace_id,
                allowed_visibilities=allowed_visibilities,
            )

        if self._campus_documents is not None and step.tool == self._campus_documents.find_name:
            try:
                arguments = CampusDocumentFindArguments.model_validate(tool_payload)
            except ValidationError as exc:
                return _invalid_arguments_result(step.tool, trace_id, exc)
            return await self._campus_documents.find(
                arguments,
                trace_id,
                allowed_visibilities=allowed_visibilities,
            )

        if (
            self._campus_documents is not None
            and step.tool == self._campus_documents.read_locator_name
        ):
            try:
                arguments = CampusDocumentReadLocatorArguments.model_validate(tool_payload)
            except ValidationError as exc:
                return _invalid_arguments_result(step.tool, trace_id, exc)
            return await self._campus_documents.read_locator(
                arguments,
                trace_id,
                allowed_visibilities=allowed_visibilities,
            )

        if self._campus_documents is not None and step.tool == self._campus_documents.read_name:
            try:
                arguments = CampusDocumentReadArguments.model_validate(tool_payload)
            except ValidationError as exc:
                return _invalid_arguments_result(step.tool, trace_id, exc)
            return await self._campus_documents.read(
                arguments,
                trace_id,
                allowed_visibilities=allowed_visibilities,
            )

        if step.tool == self._official_search.name:
            try:
                arguments = OfficialSearchArguments.model_validate(tool_payload)
            except ValidationError as exc:
                return _invalid_arguments_result(step.tool, trace_id, exc)
            result = await self._official_search.run(arguments, trace_id)
            if result.status == "ok":
                self._schedule_live_backfill(result.evidence)
            return result

        if step.tool == self._campus_notices.name:
            try:
                arguments = CampusNoticeSearchArguments.model_validate(tool_payload)
            except ValidationError as exc:
                return _invalid_arguments_result(step.tool, trace_id, exc)
            result = await self._campus_notices.run(
                arguments,
                trace_id,
                actor_user_id=actor_user_id,
                allowed_visibilities=allowed_visibilities or frozenset({"public"}),
            )
            if result.status == "ok":
                self._schedule_live_backfill(result.evidence)
            return result

        return ToolResult(
            tool=step.tool,
            status="error",
            error=ToolError(
                code="TOOL_NOT_ALLOWED",
                message=f"工具 {step.tool} 不在当前允许目录中。",
                retryable=False,
            ),
            trace_id=trace_id,
        )

    async def close(self) -> None:
        if self._live_backfill_tasks:
            await asyncio.gather(
                *tuple(self._live_backfill_tasks),
                return_exceptions=True,
            )
        await self._official_search.close()
        await self._campus_notices.close()
        await self._ingestion.close()

    def _schedule_live_backfill(self, evidence_items: list) -> None:
        if not evidence_items:
            return
        task = asyncio.create_task(
            self._backfill_live_evidence(
                tuple(item.model_copy(deep=True) for item in evidence_items)
            ),
            name="campus-live-evidence-backfill",
        )
        self._live_backfill_tasks.add(task)
        task.add_done_callback(self._live_backfill_tasks.discard)

    async def _backfill_live_evidence(self, evidence_items: tuple) -> None:
        # Return live evidence to the answer path immediately. Only the short
        # database/index update is serialized in the background.
        async with self._live_backfill_lock:
            for evidence in evidence_items:
                try:
                    await self._ingestion.ingest_live_evidence(evidence)
                except Exception:
                    logger.exception(
                        "Live evidence backfill failed",
                        extra={
                            "event": "source.live_backfill.failed",
                            "canonical_uri": evidence.canonical_url,
                        },
                    )


def _invalid_arguments_result(
    tool: str,
    trace_id: str,
    error: ValidationError,
) -> ToolResult:
    return ToolResult(
        tool=tool,
        status="error",
        error=ToolError(
            code="INVALID_TOOL_ARGUMENTS",
            message="模型生成的工具参数无效。",
            retryable=False,
            details={"errors": error.errors(include_url=False)},
        ),
        trace_id=trace_id,
    )
