from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from hzcu_agent.auth.campus_access import CampusNoticeImage
from hzcu_agent.config import Settings
from hzcu_agent.services.performance import current_performance_trace


class ImageTranscription(BaseModel):
    image_number: int = Field(ge=1, le=6)
    text: str


class ImageTranscriptionBatch(BaseModel):
    items: list[ImageTranscription] = Field(default_factory=list, max_length=6)


class CampusImageReader:
    """Use the configured multimodal model to read text-heavy official images."""

    def __init__(self, settings: Settings) -> None:
        self._model = settings.utility_model
        self._client: AsyncOpenAI | None = None
        if settings.model_provider == "openai" and settings.openai_api_key is not None:
            options = {
                "api_key": settings.openai_api_key.get_secret_value(),
                "timeout": settings.model_timeout_seconds,
            }
            if settings.openai_base_url:
                options["base_url"] = settings.openai_base_url
            self._client = AsyncOpenAI(**options)

    async def read(
        self,
        images: list[CampusNoticeImage],
        *,
        query: str,
    ) -> list[str]:
        return await self._transcribe(
            images,
            prompt=(
                "用户正在查询：" + query + "。下面是校园官方页面中的图片。"
                "逐张忠实转录与问题相关的标题、日期、事项和说明；看不清就明确写"
                "“无法辨认”，不要推测。"
            ),
            instructions=(
                "你是校园官方图片的文字读取器。只报告图片中真实可见的内容，"
                "保留日期与事项的对应关系，输出符合结构。"
            ),
            operation_name="image_reader",
        )

    async def transcribe(self, images: list[CampusNoticeImage]) -> list[str]:
        """Faithfully transcribe complete page images without interpreting them."""

        return await self._transcribe(
            images,
            prompt=(
                "下面是同一份文档的页面图像。逐张完整转录所有真实可见文字，"
                "不要摘要、解释、筛选或补写。保留标题层级、段落顺序、编号、"
                "数值、单位和注释。表格应保持行列对应关系，优先用 Markdown "
                "表格；版面无法可靠还原时，逐行写成“字段：内容”，不得把不同"
                "单元格拼成一个事实。看不清的局部标为【无法辨认】，不要推测。"
            ),
            instructions=(
                "你是通用文档页面转写器。任务仅是忠实读取图像中的文本与版面"
                "关系，不做业务判断。每张图片必须对应一个结构化结果。"
            ),
            operation_name="document_page_transcriber",
        )

    async def _transcribe(
        self,
        images: list[CampusNoticeImage],
        *,
        prompt: str,
        instructions: str,
        operation_name: str,
    ) -> list[str]:
        if self._client is None or not images:
            return []
        content: list[dict[str, str]] = [
            {
                "type": "input_text",
                "text": prompt,
            }
        ]
        for index, image in enumerate(images, start=1):
            content.append(
                {
                    "type": "input_text",
                    "text": f"图片 {index}，页面标注：{image.title}",
                }
            )
            content.append(
                {
                    "type": "input_image",
                    "image_url": image.data_url,
                    "detail": "high",
                }
            )
        request = {
            "model": self._model,
            "instructions": instructions,
            "input": [{"role": "user", "content": content}],
            "text_format": ImageTranscriptionBatch,
            "reasoning": {"effort": "low"},
            "store": False,
        }
        trace = current_performance_trace()
        if trace is None:
            response = await self._client.responses.parse(**request)
        else:
            trace.add_scenario_hint("image")
            span = trace.start_span("model", operation_name)
            try:
                async with self._client.responses.stream(**request) as stream:
                    async for event in stream:
                        if getattr(event, "type", "") in {
                            "response.output_text.delta",
                            "response.refusal.delta",
                        }:
                            trace.mark_first_event(span)
                    response = await stream.get_final_response()
                if span.first_event_ns is None:
                    trace.mark_unmeasurable(span)
            except Exception:
                trace.mark_unmeasurable(span)
                raise
            finally:
                trace.finish_span(span)
        parsed = response.output_parsed
        if parsed is None:
            return []
        by_number = {
            item.image_number: item.text.strip() for item in parsed.items if item.text.strip()
        }
        return [by_number.get(index, "") for index in range(1, len(images) + 1)]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
