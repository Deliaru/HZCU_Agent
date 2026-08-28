import hashlib
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, or_, select
from sqlalchemy.exc import IntegrityError

from hzcu_agent.auth.service import RequestPrincipal
from hzcu_agent.config import Settings
from hzcu_agent.db import Database
from hzcu_agent.models import (
    AnswerFeedback,
    CommunityQuestion,
    Conversation,
    ProductSubject,
    ProfileAttribute,
    StudentProfile,
    UserTodo,
    VisitorSession,
    new_id,
    utc_now,
)


@dataclass(frozen=True)
class ProductIdentityResolution:
    principal: RequestPrincipal
    visitor_token: str | None = None
    csrf_token: str | None = None
    visitor_expires_at: datetime | None = None


class ProductIdentityService:
    """Bind every product request to either a device or campus subject."""

    def __init__(self, *, settings: Settings, database: Database) -> None:
        self._settings = settings
        self._database = database

    async def resolve(
        self,
        principal: RequestPrincipal,
        visitor_token: str | None,
        csrf_token: str | None,
    ) -> ProductIdentityResolution:
        visitor = await self._resolve_visitor(visitor_token, csrf_token)
        if principal.authenticated and principal.user_id is not None:
            campus_subject_id = await self._ensure_campus_subject(principal.user_id)
            visitor_subject_id = (
                visitor.principal.product_subject_id
                if visitor is not None and visitor.principal.product_subject_id != campus_subject_id
                else None
            )
            visitor_data_available = (
                await self.has_visitor_data(visitor_subject_id)
                if visitor_subject_id is not None
                else False
            )
            return ProductIdentityResolution(
                principal=replace(
                    principal,
                    product_subject_id=campus_subject_id,
                    visitor_subject_id=visitor_subject_id,
                    visitor_data_available=visitor_data_available,
                )
            )
        if visitor is not None:
            has_data = await self.has_visitor_data(visitor.principal.product_subject_id)
            return ProductIdentityResolution(
                principal=replace(
                    visitor.principal,
                    visitor_data_available=has_data,
                ),
                visitor_token=visitor.visitor_token,
                csrf_token=visitor.csrf_token,
                visitor_expires_at=visitor.visitor_expires_at,
            )
        return await self._create_visitor()

    async def _ensure_campus_subject(self, campus_user_id: str) -> str:
        now = utc_now()
        async with self._database.session_factory() as session:
            subject = await session.scalar(
                select(ProductSubject).where(ProductSubject.campus_user_id == campus_user_id)
            )
            try:
                if subject is None:
                    subject = ProductSubject(
                        id=new_id("psub"),
                        subject_kind="campus",
                        campus_user_id=campus_user_id,
                        status="active",
                        created_at=now,
                        last_seen_at=now,
                    )
                    session.add(subject)
                    await session.flush()
                    session.add(
                        StudentProfile(
                            subject_id=subject.id,
                            personalization_enabled=True,
                            onboarding_completed=False,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    subject.last_seen_at = now
                await session.commit()
            except IntegrityError:
                # Two first-time CAS requests for the same subject can race
                # before the unique campus_user_id constraint is visible.
                # Resolve the winner rather than returning a transient 500.
                await session.rollback()
                existing = await session.scalar(
                    select(ProductSubject).where(ProductSubject.campus_user_id == campus_user_id)
                )
                if existing is None:
                    raise
                existing.last_seen_at = now
                await session.commit()
                subject = existing
            return subject.id

    async def _resolve_visitor(
        self,
        token: str | None,
        csrf_token: str | None,
    ) -> ProductIdentityResolution | None:
        if not token or len(token) > 512:
            return None
        now = utc_now()
        async with self._database.session_factory() as session:
            row = (
                await session.execute(
                    select(VisitorSession, ProductSubject)
                    .join(ProductSubject, VisitorSession.subject_id == ProductSubject.id)
                    .where(VisitorSession.token_hash == _token_hash(token))
                )
            ).one_or_none()
            if row is None:
                return None
            visitor, subject = row
            if (
                visitor.revoked_at is not None
                or _aware_utc(visitor.expires_at) <= now
                or subject.status != "active"
                or subject.merged_into_subject_id is not None
            ):
                if visitor.revoked_at is None:
                    visitor.revoked_at = now
                    await session.commit()
                return None
            rotated_csrf: str | None = None
            csrf_matches = (
                bool(csrf_token)
                and len(csrf_token or "") <= 512
                and _token_hash(csrf_token or "") == visitor.csrf_hash
            )
            if not csrf_matches:
                rotated_csrf = secrets.token_urlsafe(32)
                visitor.csrf_hash = _token_hash(rotated_csrf)
            if rotated_csrf is not None or now - _aware_utc(visitor.last_seen_at) >= timedelta(
                minutes=5
            ):
                visitor.last_seen_at = now
                subject.last_seen_at = now
                await session.commit()
            return ProductIdentityResolution(
                principal=RequestPrincipal(
                    user_id=None,
                    subject_hint=None,
                    visibility_scopes=frozenset({"public"}),
                    session_id=visitor.id,
                    csrf_hash=visitor.csrf_hash,
                    product_subject_id=subject.id,
                    role="visitor",
                    csrf_required=rotated_csrf is None,
                ),
                csrf_token=rotated_csrf,
                visitor_expires_at=_aware_utc(visitor.expires_at),
            )

    async def has_visitor_data(self, subject_id: str | None) -> bool:
        """Return whether a visitor subject contains user-created data.

        The automatically-created StudentProfile row is intentionally not
        counted.  This keeps a fresh browser from receiving a needless merge
        prompt after CAS login.
        """

        if not subject_id:
            return False
        async with self._database.session_factory() as session:
            profile_data = exists(
                select(StudentProfile.subject_id).where(
                    StudentProfile.subject_id == subject_id,
                    or_(
                        StudentProfile.onboarding_completed.is_(True),
                        StudentProfile.personalization_enabled.is_(False),
                    ),
                )
            )
            personal_rows = exists(
                select(ProfileAttribute.id).where(ProfileAttribute.subject_id == subject_id)
            )
            conversation_rows = exists(
                select(Conversation.id).where(Conversation.owner_subject_id == subject_id)
            )
            todo_rows = exists(select(UserTodo.id).where(UserTodo.subject_id == subject_id))
            feedback_rows = exists(
                select(AnswerFeedback.id).where(AnswerFeedback.subject_id == subject_id)
            )
            question_rows = exists(
                select(CommunityQuestion.id).where(CommunityQuestion.owner_subject_id == subject_id)
            )
            return bool(
                await session.scalar(
                    select(
                        or_(
                            profile_data,
                            personal_rows,
                            conversation_rows,
                            todo_rows,
                            feedback_rows,
                            question_rows,
                        )
                    )
                )
            )

    async def _create_visitor(self) -> ProductIdentityResolution:
        now = utc_now()
        expires_at = now + timedelta(days=self._settings.visitor_session_days)
        visitor_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        async with self._database.session_factory() as session:
            subject = ProductSubject(
                id=new_id("psub"),
                subject_kind="visitor",
                status="active",
                created_at=now,
                last_seen_at=now,
            )
            session.add(subject)
            await session.flush()
            visitor = VisitorSession(
                id=new_id("vsess"),
                subject_id=subject.id,
                token_hash=_token_hash(visitor_token),
                csrf_hash=_token_hash(csrf_token),
                created_at=now,
                last_seen_at=now,
                expires_at=expires_at,
            )
            session.add(visitor)
            session.add(
                StudentProfile(
                    subject_id=subject.id,
                    personalization_enabled=True,
                    onboarding_completed=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            await session.commit()
        return ProductIdentityResolution(
            principal=RequestPrincipal(
                user_id=None,
                subject_hint=None,
                visibility_scopes=frozenset({"public"}),
                session_id=visitor.id,
                csrf_hash=visitor.csrf_hash,
                product_subject_id=subject.id,
                role="visitor",
                csrf_required=False,
            ),
            visitor_token=visitor_token,
            csrf_token=csrf_token,
            visitor_expires_at=expires_at,
        )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
