from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.shared import (
    create_record,
    envelope,
    get_record,
    iso,
    list_records,
)
from app.db.session import get_db
from app.models.resource import Resource
from app.models.user import User

router = APIRouter()
REPORT_REASONS = {"욕설·비하", "개인정보 노출", "도배·광고", "사실과 다른 내용", "기타"}


def _post_view(
    record: Resource, db: Session, viewer: User, detail: bool = False
) -> dict[str, Any]:
    data = record.payload
    author = db.get(User, record.owner_id) if record.owner_id else None
    comments = list(
        db.scalars(
            select(Resource).where(
                Resource.resource_type == "comment",
                Resource.payload["postId"].as_string() == str(record.id),
            )
        )
    )
    result = {
        "id": str(record.id),
        "category": data.get("category", "자유"),
        "title": data.get("title", ""),
        "body": data.get("body", ""),
        "anonymous": bool(data.get("anonymous")),
        "anonComments": bool(data.get("anonComments")),
        "views": int(data.get("views", 0)),
        "likes": int(data.get("likes", 0)),
        "createdAt": iso(record.created_at),
        "when": iso(record.created_at),
        "pinned": bool(data.get("pinned", False)),
        "mine": record.owner_id == viewer.id,
        "commentCount": len(comments),
    }
    if not data.get("anonymous"):
        result["author"] = author.name if author else "알 수 없음"
    if detail:
        result["comments"] = [
            _comment_view(c, db, viewer, bool(data.get("anonymous"))) for c in comments
        ]
    return result


def _comment_view(
    record: Resource, db: Session, viewer: User, post_anonymous: bool = False
) -> dict[str, Any]:
    data = record.payload
    author = db.get(User, record.owner_id) if record.owner_id else None
    result = {
        "id": str(record.id),
        "postId": str(data.get("postId", "")),
        "anonymous": bool(data.get("anonymous")),
        "text": data.get("text", ""),
        "createdAt": iso(record.created_at),
        "when": iso(record.created_at),
        "likes": int(data.get("likes", 0)),
        "replyTo": data.get("replyTo"),
    }
    if not data.get("anonymous"):
        result["author"] = (
            "글쓴이"
            if post_anonymous and record.owner_id and data.get("isPostAuthor")
            else (author.name if author else "알 수 없음")
        )
    return result


@router.get("/posts", summary="게시글 목록")
def get_posts(
    category: str | None = None,
    sort: str = "latest",
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if sort not in {"latest", "likes", "comments"}:
        raise HTTPException(400, "sort는 latest, likes, comments 중 하나여야 합니다.")
    records, next_cursor = list_records(db, "post", cursor, min(limit * 5, 500))
    values = [
        _post_view(record, db, user)
        for record in records
        if record.payload.get("status", "published") == "published"
        and (not category or record.payload.get("category") == category)
    ]
    sort_key = {
        "latest": lambda x: x.get("createdAt") or "",
        "likes": lambda x: x.get("likes", 0),
        "comments": lambda x: x.get("commentCount", 0),
    }[sort]
    values.sort(key=sort_key, reverse=True)
    page = values[:limit]
    return envelope(
        {"items": page, "nextCursor": page[-1]["id"] if len(values) > limit else None}
    )


@router.get("/posts/{post_id}", summary="게시글 상세")
def get_post(
    post_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    post = get_record(db, "post", post_id)
    if post.payload.get("status", "published") != "published":
        raise HTTPException(404, "게시글을 찾을 수 없습니다.")
    post.payload = {**post.payload, "views": int(post.payload.get("views", 0)) + 1}
    db.commit()
    return envelope(_post_view(post, db, user, True))


class PostCreate(BaseModel):
    category: str
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=10000)
    anonymous: bool = False
    anonComments: bool = False


@router.post("/posts", status_code=201, summary="게시글 작성")
def create_post(
    payload: PostCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if payload.category not in {"자유", "질문", "모집", "칭찬"}:
        raise HTTPException(422, "지원하지 않는 게시판 분류입니다.")
    post = create_record(
        db,
        "post",
        {
            **payload.model_dump(),
            "views": 0,
            "likes": 0,
            "pinned": False,
            "status": "published",
        },
        user,
    )
    return envelope({"id": str(post.id)}, "게시글을 등록했습니다.", 201)


@router.delete("/posts/{post_id}", summary="게시글 삭제")
def delete_post(
    post_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    post = get_record(db, "post", post_id)
    if post.owner_id != user.id and "Admin" not in (user.roles or []):
        raise HTTPException(403, "작성자 또는 관리자만 삭제할 수 있습니다.")
    post.payload = {**post.payload, "status": "deleted"}
    db.commit()
    return envelope({"id": post_id, "deleted": True}, "게시글을 삭제했습니다.")


@router.post("/posts/{post_id}/like", summary="게시글 공감 토글")
def like_post(
    post_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    post = get_record(db, "post", post_id)
    reaction = db.scalar(
        select(Resource).where(
            Resource.resource_type == "post_like",
            Resource.owner_id == user.id,
            Resource.payload["postId"].as_string() == post_id,
        )
    )
    if reaction:
        db.delete(reaction)
        likes = max(0, int(post.payload.get("likes", 0)) - 1)
        liked = False
    else:
        db.add(
            Resource(
                resource_type="post_like", owner_id=user.id, payload={"postId": post_id}
            )
        )
        likes = int(post.payload.get("likes", 0)) + 1
        liked = True
    post.payload = {**post.payload, "likes": likes}
    db.commit()
    return envelope({"liked": liked, "likes": likes})


@router.post(
    "/posts/{post_id}/comments", status_code=201, summary="댓글 및 대댓글 작성"
)
def create_comment(
    post_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    post = get_record(db, "post", post_id)
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "댓글 내용을 입력해주세요.")
    reply_to = payload.get("replyTo")
    if reply_to:
        parent = get_record(db, "comment", reply_to)
        if str(parent.payload.get("postId")) != post_id:
            raise HTTPException(404, "상위 댓글을 찾을 수 없습니다.")
    anonymous = bool(payload.get("anonymous", False)) and bool(
        post.payload.get("anonComments", False)
    )
    is_post_author = post.owner_id == user.id
    record = create_record(
        db,
        "comment",
        {
            "postId": post_id,
            "text": text,
            "anonymous": anonymous,
            "replyTo": reply_to,
            "likes": 0,
            "isPostAuthor": is_post_author,
        },
        user,
    )
    if post.owner_id != user.id:
        notify_user = __import__("app.api.shared", fromlist=["notify_user"]).notify_user
        notify_user(
            db,
            post.owner_id,
            "게시판",
            "게시글에 새 댓글이 달렸습니다.",
            f"/inhouse/community/{post_id}",
        )
    return envelope(
        _comment_view(record, db, user, bool(post.payload.get("anonymous"))),
        "댓글을 등록했습니다.",
        201,
    )


@router.post("/posts/{post_id}/comments/{comment_id}/like", summary="댓글 공감")
def like_comment(
    post_id: str,
    comment_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    comment = get_record(db, "comment", comment_id)
    if str(comment.payload.get("postId")) != post_id:
        raise HTTPException(404, "댓글을 찾을 수 없습니다.")
    key = {"postId": post_id, "commentId": comment_id}
    reaction = db.scalar(
        select(Resource).where(
            Resource.resource_type == "comment_like",
            Resource.owner_id == user.id,
            Resource.payload["commentId"].as_string() == comment_id,
        )
    )
    if reaction:
        db.delete(reaction)
        count = max(0, int(comment.payload.get("likes", 0)) - 1)
        liked = False
    else:
        db.add(Resource(resource_type="comment_like", owner_id=user.id, payload=key))
        count = int(comment.payload.get("likes", 0)) + 1
        liked = True
    comment.payload = {**comment.payload, "likes": count}
    db.commit()
    return envelope({"liked": liked, "likes": count})


@router.post("/posts/{post_id}/report", status_code=201, summary="게시글 신고")
def report_post(
    post_id: str,
    payload: dict[str, str],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    get_record(db, "post", post_id)
    reason = payload.get("reason", "")
    if reason not in REPORT_REASONS:
        raise HTTPException(422, "신고 사유가 올바르지 않습니다.")
    record = create_record(
        db,
        "report",
        {
            "postId": post_id,
            "reason": reason,
            "detail": payload.get("detail", ""),
            "status": "PENDING",
        },
        user,
    )
    return envelope(
        {"id": str(record.id), "status": "PENDING"}, "신고를 접수했습니다.", 201
    )


@router.get("/inquiries", summary="내 문의 목록")
def get_inquiries(
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    records, next_cursor = list_records(db, "inquiry", cursor, min(limit * 5, 500))
    mine = [r for r in records if r.owner_id == user.id]
    items = []
    for record in mine[:limit]:
        data = record.payload
        result = {"id": str(record.id), **data}
        if data.get("anonymous"):
            result.pop("author", None)
            result.pop("authorId", None)
        items.append(result)
    return envelope(
        {"items": items, "nextCursor": next_cursor if len(mine) > limit else None}
    )


@router.post("/inquiries", status_code=201, summary="문의·건의 작성")
def create_inquiry(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if not all(payload.get(k) for k in ("type", "dept", "title", "body")):
        raise HTTPException(400, "유형, 부서, 제목, 내용을 입력해주세요.")
    if payload.get("type") not in {"문의", "건의", "불편 신고"}:
        raise HTTPException(422, "문의 유형이 올바르지 않습니다.")
    data = {
        **payload,
        "status": "접수",
        "steps": [iso(datetime.now().astimezone()), "", ""],
        "answer": None,
        "replies": [],
    }
    record = create_record(db, "inquiry", data, user)
    return envelope(
        {"id": str(record.id), "status": "접수"}, "문의가 접수됐습니다.", 201
    )


@router.post("/inquiries/{inquiry_id}/resolve", summary="문의 해결 확인")
def resolve_inquiry(
    inquiry_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = get_record(db, "inquiry", inquiry_id)
    if record.owner_id != user.id:
        raise HTTPException(404, "문의를 찾을 수 없습니다.")
    data = {
        **record.payload,
        "status": "해결됨",
        "resolvedAt": iso(datetime.now().astimezone()),
    }
    record.payload = data
    db.commit()
    return envelope({"id": inquiry_id, "status": "해결됨"}, "문의 상태를 갱신했습니다.")


@router.post(
    "/inquiries/{inquiry_id}/comments", status_code=201, summary="문의 추가 메시지"
)
def inquiry_comment(
    inquiry_id: str,
    payload: dict[str, str],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = get_record(db, "inquiry", inquiry_id)
    if record.owner_id != user.id and not set(user.roles or []).intersection(
        {"Admin", record.payload.get("dept")}
    ):
        raise HTTPException(404, "문의를 찾을 수 없습니다.")
    text = payload.get("text", "").strip()
    if not text:
        raise HTTPException(400, "메시지를 입력해주세요.")
    entry = {
        "text": text,
        "at": iso(datetime.now().astimezone()),
        "who": "익명"
        if record.payload.get("anonymous") and record.owner_id == user.id
        else user.name,
        "whoId": str(user.id),
    }
    record.payload = {
        **record.payload,
        "replies": [*record.payload.get("replies", []), entry],
    }
    db.commit()
    return envelope(entry, "추가 문의를 등록했습니다.", 201)


@router.get("/notices", summary="공지 목록")
def get_notices(
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    records, next_cursor = list_records(db, "notice", cursor, min(limit * 4, 400))
    confirmed = {
        str(r.payload.get("noticeId"))
        for r in db.scalars(
            select(Resource).where(
                Resource.resource_type == "notice_confirmation",
                Resource.owner_id == user.id,
            )
        )
    }
    values = []
    for record in records:
        data = record.payload
        total = int(data.get("total", 0))
        count = (
            db.scalar(
                select(func.count())
                .select_from(Resource)
                .where(
                    Resource.resource_type == "notice_confirmation",
                    Resource.payload["noticeId"].as_string() == str(record.id),
                )
            )
            or 0
        )
        values.append(
            {
                "id": str(record.id),
                **data,
                "confirmed": count,
                "total": total or count,
                "confirmedByMe": str(record.id) in confirmed,
            }
        )
    return envelope(
        {
            "items": values[:limit],
            "nextCursor": next_cursor if len(values) > limit else None,
        }
    )


@router.get("/notices/{notice_id}", summary="공지 상세")
def get_notice(
    notice_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    notice = get_record(db, "notice", notice_id)
    data = notice.payload
    confirmed = (
        db.scalar(
            select(Resource).where(
                Resource.resource_type == "notice_confirmation",
                Resource.owner_id == user.id,
                Resource.payload["noticeId"].as_string() == notice_id,
            )
        )
        is not None
    )
    total = (
        db.scalar(
            select(func.count())
            .select_from(Resource)
            .where(
                Resource.resource_type == "notice_confirmation",
                Resource.payload["noticeId"].as_string() == notice_id,
            )
        )
        or 0
    )
    return envelope(
        {
            "id": notice_id,
            **data,
            "confirmed": total,
            "total": int(data.get("total", 0)) or total,
            "confirmedByMe": confirmed,
        }
    )


@router.post("/notices/{notice_id}/confirm", summary="필독 공지 확인")
def confirm_notice(
    notice_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    get_record(db, "notice", notice_id)
    exists = db.scalar(
        select(Resource).where(
            Resource.resource_type == "notice_confirmation",
            Resource.owner_id == user.id,
            Resource.payload["noticeId"].as_string() == notice_id,
        )
    )
    if not exists:
        create_record(
            db,
            "notice_confirmation",
            {"noticeId": notice_id, "confirmedAt": iso(datetime.now().astimezone())},
            user,
        )
    total = (
        db.scalar(
            select(func.count())
            .select_from(Resource)
            .where(
                Resource.resource_type == "notice_confirmation",
                Resource.payload["noticeId"].as_string() == notice_id,
            )
        )
        or 0
    )
    return envelope(
        {"id": notice_id, "confirmedByMe": True, "confirmed": total}, "확인했습니다."
    )


def notify_user(
    db: Session,
    user_id: int | None,
    kind: str,
    title: str,
    href: str,
    action: bool = False,
):
    from app.api.shared import notify_user as send_notification

    send_notification(db, user_id, kind, title, href, action)
