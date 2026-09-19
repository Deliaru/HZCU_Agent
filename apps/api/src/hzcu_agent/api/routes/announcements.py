from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import exists, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from hzcu_agent.api.dependencies import enforce_csrf, request_principal, request_session
from hzcu_agent.auth.service import RequestPrincipal
from hzcu_agent.models import Announcement, AnnouncementRead, SecurityAuditEvent, new_id, utc_now

router = APIRouter(tags=["announcements"])
Session = Annotated[AsyncSession, Depends(request_session)]
Principal = Annotated[RequestPrincipal, Depends(request_principal)]


class PublishAnnouncement(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(min_length=1, max_length=20000)


class AnnouncementResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    title: str
    content: str
    active: bool
    created_at: datetime


def require_admin(principal: RequestPrincipal) -> None:
    if not principal.authenticated or principal.role != "admin":
        raise HTTPException(404, "Not found")


def subject_id(principal: RequestPrincipal) -> str:
    if principal.product_subject_id is None:
        raise HTTPException(503, "Identity unavailable")
    return principal.product_subject_id


def audit(session: AsyncSession, principal: RequestPrincipal, action: str, item_id: str) -> None:
    session.add(
        SecurityAuditEvent(
            id=new_id("audit"),
            actor_user_id=principal.user_id,
            event_type=f"admin.announcement.{action}",
            outcome="succeeded",
            event_metadata={"announcement_id": item_id},
            occurred_at=utc_now(),
        )
    )


@router.get("/announcements/unread", response_model=list[AnnouncementResponse])
async def unread(session: Session, principal: Principal, response: Response):
    response.headers["Cache-Control"] = "no-store"
    read = exists().where(
        AnnouncementRead.announcement_id == Announcement.id,
        AnnouncementRead.subject_id == subject_id(principal),
    )
    return (
        await session.scalars(
            select(Announcement)
            .where(
                Announcement.active.is_(True),
                ~read,
            )
            .order_by(Announcement.created_at, Announcement.id)
        )
    ).all()


@router.post("/announcements/{announcement_id}/read", status_code=204)
async def mark_read(announcement_id: str, request: Request, session: Session, principal: Principal):
    enforce_csrf(request, principal)
    if await session.get(Announcement, announcement_id) is None:
        raise HTTPException(404, "Announcement not found")
    key = (announcement_id, subject_id(principal))
    if await session.get(AnnouncementRead, key) is None:
        session.add(AnnouncementRead(announcement_id=key[0], subject_id=key[1]))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if await session.get(AnnouncementRead, key) is None:
                raise
    return Response(status_code=204)


@router.get("/admin/announcements", response_model=list[AnnouncementResponse])
async def admin_list(session: Session, principal: Principal, response: Response):
    require_admin(principal)
    response.headers["Cache-Control"] = "no-store"
    return (
        await session.scalars(
            select(Announcement)
            .order_by(
                Announcement.created_at.desc(),
                Announcement.id.desc(),
            )
            .limit(200)
        )
    ).all()


@router.post("/admin/announcements", response_model=AnnouncementResponse, status_code=201)
async def publish(
    body: PublishAnnouncement, request: Request, session: Session, principal: Principal
):
    require_admin(principal)
    enforce_csrf(request, principal)
    item = Announcement(id=new_id("notice"), title=body.title, content=body.content, active=True)
    session.add(item)
    audit(session, principal, "publish", item.id)
    await session.commit()
    return item


@router.post("/admin/announcements/{announcement_id}/withdraw", status_code=204)
async def withdraw(announcement_id: str, request: Request, session: Session, principal: Principal):
    require_admin(principal)
    enforce_csrf(request, principal)
    item = await session.get(Announcement, announcement_id)
    if item is None:
        raise HTTPException(404, "Announcement not found")
    if item.active:
        item.active = False
        audit(session, principal, "withdraw", item.id)
        await session.commit()
    return Response(status_code=204)
