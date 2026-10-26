import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.shared import (
    create_record,
    envelope,
    iso,
    update_record,
    user_profile,
)
from app.core.config import settings
from app.db.session import get_db
from app.models.resource import Resource
from app.models.user import User
from app.services.email import EmailConfigurationError, EmailDeliveryError, send_email
from app.services.security import hash_password

router = APIRouter()


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    dept: str | None = None
    department: str | None = None
    major: str | None = None
    cohort: str | None = None
    github: str | None = None
    photo: str | None = None
    bio: str | None = Field(default=None, max_length=60)
    interests: list[str] | None = None


class SignupRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=100)
    dept: str
    major: str | None = None
    cohort: str | None = None
    message: str | None = None


class ResetRequest(BaseModel):
    email: EmailStr


class ResetPassword(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=128)


class ReadNotifications(BaseModel):
    id: str | None = None
    all: bool = False


@router.get("/me", summary="내 정보")
def get_me(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    return envelope(user_profile(db, user))


@router.patch("/me", summary="프로필 수정")
def patch_me(
    payload: ProfilePatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = payload.model_dump(exclude_unset=True)
    if "department" in data:
        data["dept"] = data.pop("department")
    if "name" in data and data["name"] is not None:
        user.name = data.pop("name")
    if "dept" in data and data["dept"] is not None:
        user.department = data["dept"]
    if (
        "interests" in data
        and data["interests"] is not None
        and len(data["interests"]) > 5
    ):
        raise HTTPException(422, "관심사는 최대 5개까지 등록할 수 있습니다.")
    profile = db.scalar(
        select(Resource).where(
            Resource.resource_type == "profile", Resource.owner_id == user.id
        )
    )
    current = profile.payload if profile else {}
    if profile:
        profile.payload = {**current, **data}
    else:
        db.add(Resource(resource_type="profile", owner_id=user.id, payload=data))
    db.commit()
    db.refresh(user)
    return envelope(user_profile(db, user), "프로필을 저장했습니다.")


@router.get("/people", summary="팀원 목록")
def get_people(
    q: str | None = None,
    dept: str | None = None,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    stmt = select(User).order_by(User.id)
    if dept:
        stmt = stmt.where(User.department == dept)
    if cursor:
        try:
            stmt = stmt.where(User.id > int(cursor))
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    users = list(db.scalars(stmt))
    data = [user_profile(db, person) for person in users]
    if q:
        needle = q.casefold()
        data = [
            p
            for p in data
            if needle
            in " ".join(
                [p["name"], p["email"], p["dept"], p["role"], p["bio"]]
            ).casefold()
        ]
    page = data[:limit]
    next_cursor = page[-1]["id"] if len(data) > limit else None
    return envelope({"items": page, "nextCursor": next_cursor})


@router.get("/people/{person_id}", summary="팀원 상세")
def get_person(
    person_id: str, db: Session = Depends(get_db), _: User = Depends(get_current_user)
):
    try:
        person = db.get(User, int(person_id))
    except ValueError:
        person = None
    if person is None:
        raise HTTPException(404, "팀원을 찾을 수 없습니다.")
    return envelope(user_profile(db, person))


@router.post("/auth/signup-requests", status_code=202, summary="가입 요청")
def signup_request(payload: SignupRequest, db: Session = Depends(get_db)):
    existing = db.scalar(select(User.id).where(User.email == str(payload.email)))
    if existing:
        raise HTTPException(409, "이미 등록된 이메일입니다.")
    record = create_record(
        db,
        "signup_request",
        {
            **payload.model_dump(),
            "email": str(payload.email),
            "status": "PENDING",
            "createdAt": iso(datetime.now(timezone.utc)),
        },
    )
    return envelope(
        {"id": str(record.id), "status": "PENDING"}, "가입 요청을 접수했습니다.", 202
    )


def send_reset_email(email: str, token: str) -> None:
    reset_url = f"{settings.frontend_base_url}/inhouse/reset-password?token={token}"
    try:
        send_email(
            to_email=email,
            subject="TeamSpace 비밀번호 재설정",
            text_body=(
                f"비밀번호 재설정 링크: {reset_url}\n"
                "요청하지 않으셨다면 이 메일을 무시하세요."
            ),
        )
    except EmailConfigurationError as exc:
        raise HTTPException(
            503, "비밀번호 재설정 메일을 보낼 SMTP 설정이 필요합니다."
        ) from exc
    except EmailDeliveryError as exc:
        raise HTTPException(
            502, "SMTP 서버에서 메일을 보내지 못했습니다. 설정을 확인해주세요."
        ) from exc


@router.post(
    "/auth/password-reset-requests",
    status_code=202,
    summary="비밀번호 재설정 메일 요청",
)
def password_reset_request(payload: ResetRequest, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == str(payload.email)))
    # Avoid exposing whether an account exists.
    if user is None:
        return envelope({"accepted": True}, "요청을 처리했습니다.", 202)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    create_record(
        db,
        "password_reset",
        {
            "userId": user.id,
            "tokenHash": token_hash,
            "expiresAt": iso(datetime.now(timezone.utc) + timedelta(minutes=30)),
            "used": False,
        },
    )
    send_reset_email(user.email, token)
    return envelope({"accepted": True}, "요청을 처리했습니다.", 202)


@router.post("/auth/password-reset", summary="비밀번호 재설정")
def password_reset(payload: ResetPassword, db: Session = Depends(get_db)):
    token_hash = hashlib.sha256(payload.token.encode()).hexdigest()
    request = db.scalar(
        select(Resource).where(
            Resource.resource_type == "password_reset",
            Resource.payload["tokenHash"].as_string() == token_hash,
            Resource.payload["used"].as_boolean().is_(False),
        )
    )
    if request is None or datetime.fromisoformat(
        request.payload["expiresAt"]
    ) < datetime.now(timezone.utc):
        raise HTTPException(400, "재설정 링크가 유효하지 않거나 만료되었습니다.")
    user = db.get(User, request.payload["userId"])
    if user is None:
        raise HTTPException(400, "재설정 요청을 확인할 수 없습니다.")
    user.password_hash = hash_password(payload.password)
    request.payload = {**request.payload, "used": True}
    db.commit()
    return envelope({"success": True}, "비밀번호를 변경했습니다.")


@router.get("/notifications", summary="알림 목록")
def get_notifications(
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    stmt = (
        select(Resource)
        .where(Resource.resource_type == "notification", Resource.owner_id == user.id)
        .order_by(Resource.id.desc())
    )
    if cursor:
        try:
            stmt = stmt.where(Resource.id < int(cursor))
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    records = list(db.scalars(stmt.limit(limit + 1)))
    next_cursor = str(records[limit - 1].id) if len(records) > limit else None
    return envelope(
        {
            "items": [{"id": str(r.id), **r.payload} for r in records[:limit]],
            "nextCursor": next_cursor,
        }
    )


@router.post("/notifications/read", summary="알림 읽음 처리")
def read_notifications(
    payload: ReadNotifications,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not payload.id and not payload.all:
        raise HTTPException(400, "id 또는 all=true를 지정해주세요.")
    stmt = select(Resource).where(
        Resource.resource_type == "notification", Resource.owner_id == user.id
    )
    if payload.id and not payload.all:
        try:
            stmt = stmt.where(Resource.id == int(payload.id))
        except ValueError:
            raise HTTPException(400, "알림 ID가 올바르지 않습니다.") from None
    records = list(db.scalars(stmt))
    for record in records:
        record.payload = {**record.payload, "unread": False}
    db.commit()
    return envelope({"updated": len(records)}, "읽음 처리했습니다.")


@router.get("/me/notification-settings", summary="알림 설정 조회")
def get_notification_settings(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    record = db.scalar(
        select(Resource).where(
            Resource.resource_type == "notification_settings",
            Resource.owner_id == user.id,
        )
    )
    defaults = {
        "push": False,
        "kinds": {
            "내 업무": True,
            "재정": True,
            "일정": True,
            "공지": True,
            "게시판": False,
        },
        "dnd": True,
        "dndFrom": "23:00",
        "dndTo": "08:00",
        "dndDays": ["월", "화", "수", "목", "금"],
    }
    return envelope(record.payload if record else defaults)


@router.patch("/me/notification-settings", summary="알림 설정 수정")
def patch_notification_settings(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = db.scalar(
        select(Resource).where(
            Resource.resource_type == "notification_settings",
            Resource.owner_id == user.id,
        )
    )
    current = record.payload if record else {}
    data = {**current, **payload}
    if record:
        update_record(db, record, data)
    else:
        create_record(db, "notification_settings", data, user)
    return envelope(data, "알림 설정을 저장했습니다.")


@router.post("/push/subscriptions", status_code=201, summary="브라우저 푸시 구독 등록")
def add_push_subscription(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    endpoint = payload.get("endpoint")
    if not endpoint or "keys" not in payload:
        raise HTTPException(400, "유효한 Push 구독 정보가 필요합니다.")
    current = db.scalar(
        select(Resource).where(
            Resource.resource_type == "push_subscription",
            Resource.owner_id == user.id,
            Resource.payload["endpoint"].as_string() == endpoint,
        )
    )
    if current:
        update_record(db, current, payload)
    else:
        create_record(db, "push_subscription", payload, user)
    return envelope({"success": True}, "푸시 구독을 저장했습니다.", 201)


@router.get("/push/vapid-public-key", summary="브라우저 푸시 공개 키")
def get_vapid_public_key():
    if not settings.vapid_public_key:
        raise HTTPException(503, "브라우저 푸시 VAPID 설정이 필요합니다.")
    return envelope({"publicKey": settings.vapid_public_key})


@router.delete("/push/subscriptions", summary="브라우저 푸시 구독 해제")
def delete_push_subscription(
    payload: dict[str, str],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    endpoint = payload.get("endpoint", "")
    subscription = db.scalar(
        select(Resource).where(
            Resource.resource_type == "push_subscription",
            Resource.owner_id == user.id,
            Resource.payload["endpoint"].as_string() == endpoint,
        )
    )
    if subscription:
        db.delete(subscription)
        db.commit()
    return envelope({"success": True}, "푸시 구독을 해제했습니다.")
