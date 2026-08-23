import hashlib
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from hzcu_agent.db import Database
from hzcu_agent.ingestion.parsers import normalize_text
from hzcu_agent.models import (
    CampusEntityRecord,
    DocumentChunk,
    DocumentVersion,
    SourceResource,
    new_id,
    utc_now,
)

INDEX_VERSION = "semantic-structure-v1"
EXTRACTOR_VERSION = "campus-rules-v1"
LOCAL_EMBEDDING_MODEL = "hzcu-domain-subword-v1"
LOCAL_EMBEDDING_DIMENSIONS = 384
SHANGHAI = ZoneInfo("Asia/Shanghai")

HEADING_PATTERN = re.compile(
    r"^(?:"
    r"第[一二三四五六七八九十百零〇0-9]+[章节条款篇]"
    r"|[一二三四五六七八九十]+、"
    r"|（[一二三四五六七八九十0-9]+）"
    r"|\([一二三四五六七八九十0-9]+\)"
    r"|[0-9]{1,2}[.、]\s*"
    r").{0,70}$"
)
FULL_DATETIME_PATTERN = re.compile(
    r"(?P<year>20\d{2})\s*[年./-]\s*(?P<month>\d{1,2})\s*[月./-]\s*"
    r"(?P<day>\d{1,2})\s*[日号]?"
    r"(?:\s*(?P<period>上午|下午|晚上|晚|凌晨|中午))?"
    r"(?:\s*(?P<hour>\d{1,2})\s*(?:[:：时点]\s*(?P<minute>\d{1,2}))?\s*分?)?"
)
MONTH_DAY_PATTERN = re.compile(
    r"(?<!\d)(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*[日号]?"
    r"(?:\s*(?P<period>上午|下午|晚上|晚|凌晨|中午))?"
    r"(?:\s*(?P<hour>\d{1,2})\s*(?:[:：时点]\s*(?P<minute>\d{1,2}))?\s*分?)?"
)
DOCUMENT_NUMBER_PATTERN = re.compile(
    r"(?:浙大城院|浙大城|浙城院|教高|教育部)[^\s，。；]{0,12}"
    r"(?:〔20\d{2}〕|\[20\d{2}\]|﹝20\d{2}﹞)\s*\d+\s*号"
)
SENTENCE_PATTERN = re.compile(r"[^。！？；\n]+[。！？；]?")

DOMAIN_CONCEPTS: dict[str, tuple[str, ...]] = {
    "course_selection": ("选课", "补选", "退选", "正选", "预选", "课程选择"),
    "deadline": ("截止", "截至", "逾期", "最后期限", "报名截止"),
    "mentor": ("导师", "指导教师", "硕士生导师", "研究方向"),
    "competition": ("竞赛", "比赛", "挑战杯", "大创", "创新创业"),
    "policy": ("政策", "办法", "规定", "细则", "条例", "学生手册"),
    "lecture": ("讲座", "讲坛", "主讲人", "学术报告"),
    "club": ("社团", "学生组织", "协会", "社团活动"),
    "major": ("专业", "培养方案", "专业发展", "本科专业"),
}


@dataclass(frozen=True)
class SemanticChunkData:
    ordinal: int
    chunk_kind: str
    heading: str | None
    content: str
    start_offset: int
    end_offset: int


@dataclass(frozen=True)
class ExtractedEntity:
    entity_type: str
    canonical_name: str
    status: str
    department: str | None
    starts_at: datetime | None = None
    deadline_at: datetime | None = None
    ends_at: datetime | None = None
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    audience_scopes: list[str] = field(default_factory=list)
    action_items: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    document_number: str | None = None
    relation_kind: str | None = None
    related_title: str | None = None
    confidence: float = 0.5
    evidence_spans: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class IndexOutcome:
    document_version_id: str
    chunks: int
    entities: int


class LocalEmbeddingGateway:
    """Deterministic domain-aware vectors for offline development and SQLite.

    The gateway keeps retrieval fully runnable without sending campus text to an
    external provider. The model name is persisted on every chunk so a later
    production embedding migration can be explicit and rebuildable.
    """

    model_name = LOCAL_EMBEDDING_MODEL
    dimensions = LOCAL_EMBEDDING_DIMENSIONS

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [_hashed_embedding(text, self.dimensions) for text in texts]


class DocumentIndexer:
    def __init__(self, embeddings: LocalEmbeddingGateway | None = None) -> None:
        self.embeddings = embeddings or LocalEmbeddingGateway()

    async def ensure_version_index(
        self,
        session: AsyncSession,
        version: DocumentVersion,
        *,
        force: bool = False,
    ) -> IndexOutcome:
        existing_chunks = await session.scalar(
            select(func.count(DocumentChunk.id)).where(
                DocumentChunk.document_version_id == version.id
            )
        )
        existing_entities = await session.scalar(
            select(func.count(CampusEntityRecord.id)).where(
                CampusEntityRecord.document_version_id == version.id
            )
        )
        if not force and existing_chunks and existing_entities:
            return IndexOutcome(
                document_version_id=version.id,
                chunks=int(existing_chunks),
                entities=int(existing_entities),
            )

        await session.execute(
            delete(DocumentChunk).where(DocumentChunk.document_version_id == version.id)
        )
        await session.execute(
            delete(CampusEntityRecord).where(CampusEntityRecord.document_version_id == version.id)
        )

        chunks = semantic_chunks(version.title, version.normalized_text)
        vectors = await self.embeddings.embed(
            [f"{version.title}\n{chunk.heading or ''}\n{chunk.content}" for chunk in chunks]
        )
        now = utc_now()
        for chunk, vector in zip(chunks, vectors, strict=True):
            session.add(
                DocumentChunk(
                    id=new_id("chunk"),
                    document_version_id=version.id,
                    ordinal=chunk.ordinal,
                    chunk_kind=chunk.chunk_kind,
                    heading=chunk.heading,
                    content=chunk.content,
                    start_offset=chunk.start_offset,
                    end_offset=chunk.end_offset,
                    content_hash=hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
                    embedding=vector,
                    embedding_model=self.embeddings.model_name,
                    embedding_dimensions=self.embeddings.dimensions,
                    index_version=INDEX_VERSION,
                    created_at=now,
                )
            )

        entities = extract_entities(version)
        for entity in entities:
            session.add(
                CampusEntityRecord(
                    id=new_id("entity"),
                    document_version_id=version.id,
                    entity_type=entity.entity_type,
                    canonical_name=entity.canonical_name,
                    status=entity.status,
                    department=entity.department,
                    starts_at=entity.starts_at,
                    deadline_at=entity.deadline_at,
                    ends_at=entity.ends_at,
                    effective_from=entity.effective_from,
                    effective_to=entity.effective_to,
                    audience_scopes=entity.audience_scopes,
                    action_items=entity.action_items,
                    locations=entity.locations,
                    document_number=entity.document_number,
                    relation_kind=entity.relation_kind,
                    related_title=entity.related_title,
                    confidence=entity.confidence,
                    extractor_version=EXTRACTOR_VERSION,
                    evidence_spans=entity.evidence_spans,
                    created_at=now,
                )
            )
        await session.flush()
        return IndexOutcome(
            document_version_id=version.id,
            chunks=len(chunks),
            entities=len(entities),
        )

    async def rebuild_versions(
        self,
        database: Database,
        *,
        current_only: bool = False,
    ) -> list[IndexOutcome]:
        async with database.session_factory() as session:
            query = select(DocumentVersion).order_by(DocumentVersion.observed_at)
            if current_only:
                query = query.join(
                    SourceResource,
                    SourceResource.current_version_id == DocumentVersion.id,
                )
            versions = list((await session.scalars(query)).all())
            outcomes = [
                await self.ensure_version_index(session, version, force=True)
                for version in versions
            ]
            await session.commit()
        return outcomes


def semantic_chunks(title: str, text: str) -> list[SemanticChunkData]:
    normalized = normalize_text(text)
    if not normalized:
        return [
            SemanticChunkData(
                ordinal=0,
                chunk_kind="summary",
                heading=title,
                content=title,
                start_offset=0,
                end_offset=len(title),
            )
        ]

    units = _semantic_units(normalized)
    chunks: list[SemanticChunkData] = []
    current: list[tuple[str, int, int]] = []
    current_heading: str | None = None
    current_length = 0

    def flush() -> None:
        nonlocal current, current_length
        if not current:
            return
        content = normalize_text("\n".join(unit[0] for unit in current))
        if content:
            chunks.append(
                SemanticChunkData(
                    ordinal=len(chunks),
                    chunk_kind=_chunk_kind(current_heading, content),
                    heading=current_heading,
                    content=content,
                    start_offset=current[0][1],
                    end_offset=current[-1][2],
                )
            )
        current = []
        current_length = 0

    for unit, start, end, is_heading in units:
        if is_heading:
            flush()
            current_heading = unit[:500]
        projected = current_length + len(unit) + (1 if current else 0)
        if current and projected > 1450:
            flush()
        current.append((unit, start, end))
        current_length += len(unit) + (1 if current_length else 0)
        if current_length >= 900 and unit.endswith(("。", "！", "？", "；")):
            flush()
    flush()

    if not chunks:
        chunks.append(
            SemanticChunkData(
                ordinal=0,
                chunk_kind="summary",
                heading=title,
                content=normalized,
                start_offset=0,
                end_offset=len(normalized),
            )
        )
    return chunks


def extract_entities(
    version: DocumentVersion,
    *,
    now: datetime | None = None,
) -> list[ExtractedEntity]:
    reference_now = _aware(now or utc_now())
    text = normalize_text(version.normalized_text)
    sample = f"{version.title}\n{text[:12000]}"
    entity_type = _entity_type(version.title, sample, version.media_type)
    reference_date = _aware(version.published_at or version.observed_at)

    deadline_at, deadline_span = _find_labeled_datetime(
        sample,
        ("报名截止", "截止时间", "申请截止", "截至", "截止"),
        reference_date,
        end_of_day=True,
    )
    starts_at, starts_span = _find_labeled_datetime(
        sample,
        ("开始时间", "报名时间", "选课时间", "开放时间", "开始"),
        reference_date,
    )
    ends_at, ends_span = _find_labeled_datetime(
        sample,
        ("结束时间", "关闭时间", "办理结束"),
        reference_date,
        end_of_day=True,
    )
    if entity_type == "event":
        starts_at = starts_at or _aware(version.effective_from)
        ends_at = ends_at or _aware(version.effective_to)

    audience_scopes = _extract_audience_scopes(sample)
    action_items = _extract_actions(sample)
    locations = _extract_locations(sample)
    document_number_match = DOCUMENT_NUMBER_PATTERN.search(sample)
    document_number = (
        normalize_text(document_number_match.group(0)) if document_number_match else None
    )
    relation_kind, related_title, relation_span = _extract_relation(sample)

    effective_from = _aware(version.effective_from)
    effective_to = _aware(version.effective_to)
    status = _entity_status(
        entity_type,
        sample,
        starts_at=starts_at,
        deadline_at=deadline_at,
        ends_at=ends_at,
        effective_from=effective_from,
        effective_to=effective_to,
        now=reference_now,
        relation_kind=relation_kind,
    )
    evidence_spans = [
        item
        for item in (
            _span_payload("deadline_at", deadline_span),
            _span_payload("starts_at", starts_span),
            _span_payload("ends_at", ends_span),
            _span_payload(
                "document_number",
                (
                    document_number_match.start(),
                    document_number_match.end(),
                    document_number_match.group(0),
                )
                if document_number_match
                else None,
            ),
            _span_payload("relation", relation_span),
        )
        if item is not None
    ]
    for scope in audience_scopes[:8]:
        position = sample.find(scope)
        if position >= 0:
            evidence_spans.append(
                {
                    "field": "audience_scopes",
                    "start": position,
                    "end": position + len(scope),
                    "text": scope,
                }
            )

    populated = sum(
        bool(value)
        for value in (
            deadline_at,
            starts_at,
            ends_at,
            audience_scopes,
            action_items,
            locations,
            document_number,
            relation_kind,
        )
    )
    confidence = min(0.96, 0.52 + populated * 0.055)
    if entity_type == "document":
        confidence = min(confidence, 0.62)
    return [
        ExtractedEntity(
            entity_type=entity_type,
            canonical_name=version.title[:500],
            status=status,
            department=version.publisher[:200] if version.publisher else None,
            starts_at=starts_at,
            deadline_at=deadline_at,
            ends_at=ends_at,
            effective_from=effective_from,
            effective_to=effective_to,
            audience_scopes=audience_scopes,
            action_items=action_items,
            locations=locations,
            document_number=document_number,
            relation_kind=relation_kind,
            related_title=related_title,
            confidence=confidence,
            evidence_spans=evidence_spans,
        )
    ]


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True))


def _semantic_units(text: str) -> list[tuple[str, int, int, bool]]:
    units: list[tuple[str, int, int, bool]] = []
    for line_match in re.finditer(r"[^\n]+", text):
        line = normalize_text(line_match.group(0))
        if not line:
            continue
        is_heading = len(line) <= 80 and bool(HEADING_PATTERN.match(line))
        if len(line) <= 1500 or is_heading:
            units.append((line, line_match.start(), line_match.end(), is_heading))
            continue
        sentence_matches = list(SENTENCE_PATTERN.finditer(line))
        if not sentence_matches:
            sentence_matches = [
                re.match(r".{1,1200}", line[index : index + 1200])
                for index in range(0, len(line), 1200)
            ]
        for sentence in sentence_matches:
            if sentence is None:
                continue
            value = normalize_text(sentence.group(0))
            if not value:
                continue
            units.append(
                (
                    value,
                    line_match.start() + sentence.start(),
                    line_match.start() + sentence.end(),
                    False,
                )
            )
    return units


def _chunk_kind(heading: str | None, content: str) -> str:
    sample = f"{heading or ''} {content[:200]}"
    if any(word in sample for word in ("时间", "截止", "日程")):
        return "time"
    if any(word in sample for word in ("适用", "对象", "范围")):
        return "audience"
    if any(word in sample for word in ("步骤", "流程", "办理", "申请")):
        return "procedure"
    if heading and "第" in heading and any(word in heading for word in ("章", "条")):
        return "clause"
    return "section"


def _hashed_embedding(text: str, dimensions: int) -> list[float]:
    normalized = normalize_text(text).lower()
    values = [0.0] * dimensions
    features: list[tuple[str, float]] = []
    chinese_sequences = re.findall(r"[\u3400-\u9fff]+", normalized)
    for sequence in chinese_sequences:
        for size, weight in ((2, 1.0), (3, 1.25), (4, 1.4)):
            for index in range(max(0, len(sequence) - size + 1)):
                features.append((sequence[index : index + size], weight))
    for token in re.findall(r"[a-z0-9][a-z0-9._+-]{1,}", normalized):
        features.append((token, 1.2))
    for concept, aliases in DOMAIN_CONCEPTS.items():
        matches = sum(1 for alias in aliases if alias in normalized)
        if matches:
            features.append((f"concept:{concept}", 2.6 + min(matches, 4) * 0.35))

    for feature, weight in features:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        raw = int.from_bytes(digest, "big")
        index = raw % dimensions
        sign = -1.0 if raw & (1 << 63) else 1.0
        values[index] += sign * weight
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        return values
    return [round(value / norm, 8) for value in values]


def _entity_type(title: str, text: str, media_type: str) -> str:
    sample = f"{title} {text[:1500]}"
    if "lecture" in media_type or any(word in title for word in ("讲座", "讲坛", "报告会")):
        return "event"
    if any(word in title for word in ("通知", "公告", "安排", "报名")):
        return "notice"
    if any(
        word in title for word in ("办法", "规定", "条例", "章程", "细则", "学生手册", "实施方案")
    ):
        return "policy"
    if "课程" in sample and any(word in sample for word in ("学分", "授课", "课程性质")):
        return "course"
    if any(word in title for word in ("竞赛", "比赛", "挑战杯")):
        return "competition"
    return "document"


def _find_labeled_datetime(
    text: str,
    labels: tuple[str, ...],
    reference: datetime,
    *,
    end_of_day: bool = False,
) -> tuple[datetime | None, tuple[int, int, str] | None]:
    candidates: list[tuple[int, int, datetime, tuple[int, int, str]]] = []
    for label in labels:
        for label_match in re.finditer(re.escape(label), text):
            window_start = max(0, label_match.start() - 45)
            window_end = min(len(text), label_match.end() + 130)
            window = text[window_start:window_end]
            for pattern in (FULL_DATETIME_PATTERN, MONTH_DAY_PATTERN):
                for match in pattern.finditer(window):
                    absolute_start = window_start + match.start()
                    absolute_end = window_start + match.end()
                    parsed = _datetime_from_match(
                        match,
                        reference,
                        end_of_day=end_of_day,
                    )
                    if parsed is None:
                        continue
                    after_penalty = 0 if absolute_start >= label_match.start() else 20
                    distance = min(
                        abs(absolute_start - label_match.end()),
                        abs(absolute_end - label_match.start()),
                    )
                    candidates.append(
                        (
                            after_penalty + distance,
                            absolute_start,
                            parsed,
                            (absolute_start, absolute_end, match.group(0)),
                        )
                    )
    if not candidates:
        return None, None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[0][2], candidates[0][3]


def _datetime_from_match(
    match: re.Match[str],
    reference: datetime,
    *,
    end_of_day: bool,
) -> datetime | None:
    groups = match.groupdict()
    year = int(groups.get("year") or reference.astimezone(SHANGHAI).year)
    month = int(groups["month"])
    day = int(groups["day"])
    hour_text = groups.get("hour")
    minute_text = groups.get("minute")
    if hour_text is None:
        hour = 23 if end_of_day else 0
        minute = 59 if end_of_day else 0
    else:
        hour = int(hour_text)
        minute = int(minute_text or 0)
        period = groups.get("period")
        if period in {"下午", "晚上", "晚"} and hour < 12:
            hour += 12
        if period == "中午" and hour < 11:
            hour += 12
        if period == "凌晨" and hour == 12:
            hour = 0
    try:
        return datetime(year, month, day, hour, minute, tzinfo=SHANGHAI).astimezone(UTC)
    except ValueError:
        return None


def _extract_audience_scopes(text: str) -> list[str]:
    values: list[str] = []
    patterns = (
        r"20\d{2}级",
        r"(?:全校|全体)(?:本科生|研究生|学生|师生|新生)",
        r"(?:本科生|研究生|新生|留学生|专升本学生)",
        r"[\u4e00-\u9fff]{2,14}专业",
        r"[\u4e00-\u9fff]{2,14}学院",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text[:8000]):
            value = normalize_text(match.group(0))
            if value in {"浙大城市学院", "城市学院"}:
                continue
            if value not in values:
                values.append(value)
            if len(values) >= 20:
                return values
    return values


def _extract_actions(text: str) -> list[str]:
    actions: list[str] = []
    for sentence_match in SENTENCE_PATTERN.finditer(text[:10000]):
        sentence = normalize_text(sentence_match.group(0))
        if not 8 <= len(sentence) <= 220:
            continue
        if any(
            marker in sentence
            for marker in (
                "请",
                "须",
                "应当",
                "应在",
                "登录",
                "提交",
                "报名",
                "办理",
                "完成",
                "携带",
            )
        ):
            if sentence not in actions:
                actions.append(sentence)
        if len(actions) >= 8:
            break
    return actions


def _extract_locations(text: str) -> list[str]:
    values: list[str] = []
    for match in re.finditer(
        r"(?:地点|地址|上课地点|讲座地点)\s*[：:]\s*([^。\n；]{2,100})",
        text[:10000],
    ):
        value = normalize_text(match.group(1)).strip("，, ")
        if value and value not in values:
            values.append(value)
    return values[:8]


def _extract_relation(
    text: str,
) -> tuple[str | None, str | None, tuple[int, int, str] | None]:
    relation_markers = (
        ("cancelled", ("取消", "作废")),
        ("superseded", ("废止", "替代")),
        ("revised", ("修订", "修正")),
        ("postponed", ("延期", "延后")),
    )
    for relation_kind, markers in relation_markers:
        for marker in markers:
            match = re.search(
                rf"{re.escape(marker)}(?:原|此前|旧)?[《“\"]?([^》”\"\n。；]{{2,80}})",
                text[:10000],
            )
            if match:
                related = normalize_text(match.group(1)).strip("，,：: ")
                return (
                    relation_kind,
                    related[:500] or None,
                    (match.start(), match.end(), match.group(0)),
                )
    return None, None, None


def _entity_status(
    entity_type: str,
    text: str,
    *,
    starts_at: datetime | None,
    deadline_at: datetime | None,
    ends_at: datetime | None,
    effective_from: datetime | None,
    effective_to: datetime | None,
    now: datetime,
    relation_kind: str | None,
) -> str:
    if relation_kind == "cancelled" or any(
        marker in text[:2500] for marker in ("本通知取消", "活动取消", "报名取消")
    ):
        return "cancelled"
    if relation_kind == "postponed":
        return "postponed"
    if entity_type == "policy":
        if relation_kind == "superseded":
            return "superseded"
        if effective_from and now < effective_from:
            return "upcoming"
        if effective_to and now > effective_to:
            return "expired"
        return "current"
    close_time = deadline_at or ends_at
    if starts_at and now < starts_at:
        return "upcoming"
    if close_time and now > close_time:
        return "closed"
    if starts_at or close_time:
        return "open"
    return "unknown"


def _span_payload(
    field_name: str,
    span: tuple[int, int, str] | None,
) -> dict[str, Any] | None:
    if span is None:
        return None
    return {
        "field": field_name,
        "start": span[0],
        "end": span[1],
        "text": normalize_text(span[2]),
    }


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
