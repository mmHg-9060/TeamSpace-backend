from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.shared import create_record, envelope, iso, list_records, update_record
from app.db.session import get_db
from app.models.resource import Resource
from app.models.user import User
from app.services.security import hash_password, verify_password

router = APIRouter()


@router.get("/projects", summary="프로젝트 목록")
def get_projects(
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    records, next_cursor = list_records(db, "project", cursor, limit)
    items = []
    for project in records:
        data = project.payload
        items.append(
            {
                "id": data.get("slug", str(project.id)),
                "name": data.get("name", ""),
                "desc": data.get("description", data.get("desc", "")),
                "role": data.get("role", ""),
                "progress": data.get("progress", 0),
                "status": data.get("status", "진행 중"),
                "tone": data.get("tone", ""),
                "due": data.get("due", ""),
                "milestone": data.get("milestone", ""),
                "pm": data.get("pm", ""),
                "members": data.get("members", []),
                "openings": data.get("openings", []),
            }
        )
    return envelope({"items": items, "nextCursor": next_cursor})


@router.get("/me/summary", summary="내 공간 요약")
def my_summary(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    notifications = list(
        db.scalars(
            select(Resource).where(
                Resource.resource_type == "notification", Resource.owner_id == user.id
            )
        )
    )
    approvals = list(
        db.scalars(select(Resource).where(Resource.resource_type == "approval"))
    )
    posts = list(
        db.scalars(
            select(Resource).where(
                Resource.resource_type == "post", Resource.owner_id == user.id
            )
        )
    )
    inquiries = list(
        db.scalars(
            select(Resource).where(
                Resource.resource_type == "inquiry", Resource.owner_id == user.id
            )
        )
    )
    mine = [a for a in approvals if a.owner_id == user.id]
    received = [
        a
        for a in approvals
        if any(
            int(s.get("userId", 0) or 0) == user.id and s.get("state") == "now"
            for s in a.payload.get("line", [])
        )
    ]
    return envelope(
        {
            "unreadNotifications": sum(
                bool(n.payload.get("unread", False)) for n in notifications
            ),
            "submittedApprovals": len(mine),
            "myPosts": len(posts),
            "myInquiries": len(inquiries),
            "receivedApprovals": len(received),
        }
    )


@router.get("/me/activity", summary="내 최근 활동")
def my_activity(
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    candidates = list(
        db.scalars(
            select(Resource)
            .where(Resource.owner_id == user.id)
            .order_by(Resource.id.desc())
            .limit(300)
        )
    )
    allowed = {"approval", "post", "comment", "inquiry", "application", "event_rsvp"}
    candidates = [r for r in candidates if r.resource_type in allowed]
    if cursor:
        try:
            candidates = [r for r in candidates if r.id < int(cursor)]
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    page = candidates[:limit]
    items = [
        {
            "id": str(r.id),
            "kind": r.resource_type,
            "title": r.payload.get(
                "title", r.payload.get("text", r.payload.get("status", "활동"))
            ),
            "createdAt": iso(r.created_at),
            "href": r.payload.get("href", ""),
        }
        for r in page
    ]
    return envelope(
        {
            "items": items,
            "nextCursor": str(page[-1].id) if len(candidates) > limit else None,
        }
    )


@router.get("/me/projects", summary="내 프로젝트")
def my_projects(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    projects = list(
        db.scalars(
            select(Resource)
            .where(Resource.resource_type == "project")
            .order_by(Resource.id.desc())
        )
    )
    ids = {str(user.id), str(user.email)}
    values = []
    for project in projects:
        members = project.payload.get("memberIds", [])
        if (
            user.id in members
            or ids.intersection(map(str, members))
            or project.owner_id == user.id
        ):
            values.append({"id": str(project.id), **project.payload})
    return envelope({"items": values, "nextCursor": None})


@router.get("/projects/{project_id}", summary="프로젝트 상세")
def project_detail(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        project = db.scalar(
            select(Resource).where(
                Resource.resource_type == "project", Resource.id == int(project_id)
            )
        )
    except ValueError:
        project = None
    if project is None:
        project = db.scalar(
            select(Resource).where(
                Resource.resource_type == "project",
                Resource.payload["slug"].as_string() == project_id,
            )
        )
    if project is None:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
    data = project.payload
    member_ids = data.get("memberIds", [])
    members = []
    for member_id in member_ids:
        try:
            member = db.get(User, int(member_id))
        except (ValueError, TypeError):
            member = None
        if member:
            profile = db.scalar(
                select(Resource).where(
                    Resource.resource_type == "profile", Resource.owner_id == member.id
                )
            )
            members.append(
                [
                    member.id.__str__(),
                    (profile.payload if profile else {}).get("major", "Member"),
                ]
            )
    return envelope(
        {
            "id": data.get("slug", str(project.id)),
            "name": data.get("name", ""),
            "desc": data.get("description", data.get("desc", "")),
            "role": data.get("role", ""),
            "progress": data.get("progress", 0),
            "status": data.get("status", "진행 중"),
            "tone": data.get("tone", ""),
            "due": data.get("due", ""),
            "milestone": data.get("milestone", ""),
            "pm": data.get("pm", ""),
            "members": data.get("members", members),
            "openings": data.get("openings", []),
        }
    )


@router.post(
    "/projects/{project_id}/apply", status_code=201, summary="프로젝트 참여 신청"
)
def apply_project(
    project_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    try:
        project = db.scalar(
            select(Resource).where(
                Resource.resource_type == "project", Resource.id == int(project_id)
            )
        )
    except ValueError:
        project = None
    if project is None:
        project = db.scalar(
            select(Resource).where(
                Resource.resource_type == "project",
                Resource.payload["slug"].as_string() == project_id,
            )
        )
    if project is None:
        raise HTTPException(404, "프로젝트를 찾을 수 없습니다.")
    duplicate = db.scalar(
        select(Resource).where(
            Resource.resource_type == "application",
            Resource.owner_id == user.id,
            Resource.payload["projectId"].as_string() == str(project.id),
        )
    )
    if duplicate:
        raise HTTPException(409, "이미 참여 신청한 프로젝트입니다.")
    if not payload.get("position") and not payload.get("role"):
        raise HTTPException(400, "지원 자리를 선택해주세요.")
    application = create_record(
        db,
        "application",
        {
            **payload,
            "projectId": str(project.id),
            "projectName": project.payload.get("name", ""),
            "status": "APPLIED",
            "createdAt": iso(datetime.now().astimezone()),
        },
        user,
    )
    for member_id in project.payload.get("pmIds", []):
        try:
            notify_user(
                db,
                int(member_id),
                "내 업무",
                f"{user.name}님이 프로젝트 참여를 신청했습니다.",
                f"/inhouse/my/projects/{project_id}",
                True,
            )
        except (TypeError, ValueError):
            pass
    return envelope(
        {"applicationId": str(application.id), "status": "APPLIED"},
        "참여 신청을 접수했습니다.",
        201,
    )


class Rsvp(BaseModel):
    rsvp: str = Field(pattern="^(참석|불참|미정)$")
    reason: str | None = None


@router.get("/events", summary="월별 일정")
def get_events(
    month: str,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if len(month) != 7 or month[4] != "-":
        raise HTTPException(400, "month는 YYYY-MM 형식이어야 합니다.")
    records, next_cursor = list_records(db, "event", cursor, limit)
    events = []
    for record in records:
        data = record.payload
        if not str(data.get("date", "")).startswith(month):
            continue
        event = {
            "id": str(record.id),
            **{k: v for k, v in data.items() if k != "rsvpByUser"},
        }
        event["rsvp"] = bool(data.get("rsvpEnabled", data.get("rsvp", False)))
        event["myRsvp"] = data.get("rsvpByUser", {}).get(str(user.id))
        events.append(event)
    return envelope(
        {"items": events, "nextCursor": next_cursor if len(events) == limit else None}
    )


@router.post("/events/{event_id}/rsvp", summary="일정 참석 응답")
def rsvp(
    event_id: str,
    payload: Rsvp,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    from app.api.shared import get_record

    event = get_record(db, "event", event_id)
    replies = dict(event.payload.get("rsvpByUser", {}))
    replies[str(user.id)] = {
        "rsvp": payload.rsvp,
        "reason": payload.reason,
        "at": iso(datetime.now().astimezone()),
    }
    event.payload = {**event.payload, "rsvpByUser": replies}
    db.commit()
    db.add(
        Resource(
            resource_type="event_rsvp",
            owner_id=user.id,
            payload={
                "eventId": event_id,
                "title": event.payload.get("title"),
                "rsvp": payload.rsvp,
            },
        )
    )
    db.commit()
    return envelope(
        {"eventId": event_id, "rsvp": payload.rsvp}, "참석 응답을 저장했습니다."
    )


@router.get("/me/settings", summary="내 설정 조회")
def get_settings(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    record = db.scalar(
        select(Resource).where(
            Resource.resource_type == "user_settings", Resource.owner_id == user.id
        )
    )
    return envelope(record.payload if record else {"theme": "시스템"})


@router.patch("/me/settings", summary="내 설정 수정")
def patch_settings(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    values = dict(payload)
    current_password = values.pop("currentPassword", None)
    new_password = values.pop("newPassword", None)
    if new_password:
        if not current_password or not verify_password(
            current_password, user.password_hash
        ):
            raise HTTPException(400, "현재 비밀번호가 일치하지 않습니다.")
        if len(new_password) < 8:
            raise HTTPException(422, "새 비밀번호는 8자 이상이어야 합니다.")
        user.password_hash = hash_password(new_password)
    theme = values.get("theme")
    if theme and theme not in {"시스템", "라이트", "다크"}:
        raise HTTPException(422, "지원하지 않는 테마입니다.")
    record = db.scalar(
        select(Resource).where(
            Resource.resource_type == "user_settings", Resource.owner_id == user.id
        )
    )
    current = record.payload if record else {}
    updated = {**current, **values}
    if record:
        update_record(db, record, updated)
    else:
        create_record(db, "user_settings", updated, user)
    db.commit()
    return envelope(updated, "설정을 저장했습니다.")


def notify_user(
    db: Session,
    user_id: int | None,
    kind: str,
    title: str,
    href: str,
    action: bool = False,
):
    if user_id is None:
        return
    from app.api.shared import notify_user as create_notification

    create_notification(db, user_id, kind, title, href, action)
