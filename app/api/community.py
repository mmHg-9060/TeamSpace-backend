from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

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
)
from app.core.config import settings
from app.db.session import get_db
from app.models.resource import Resource
from app.models.user import User
from app.services.anonymity import (
    ANONYMOUS_LABEL,
    anonymize_payload,
    anonymous_owner_key,
    is_record_owner,
    owner_filter,
    public_content_payload,
    resolve_anonymous_owner,
)

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
        "anonymous": bool(data.get("anonymous") or data.get("isAnonymous")),
        "anonComments": bool(data.get("anonComments")),
        "views": int(data.get("views", 0)),
        "likes": int(data.get("likes", 0)),
        "createdAt": iso(record.created_at),
        "when": iso(record.created_at),
        "pinned": bool(data.get("pinned", False)),
        "mine": is_record_owner(record, viewer),
        "commentCount": len(comments),
        "attachments": data.get("attachments", []),
    }
    if not result["anonymous"]:
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
    if post_anonymous and data.get("isPostAuthor"):
        result["author"] = "글쓴이"
    elif not data.get("anonymous"):
        result["author"] = author.name if author else "알 수 없음"
    result["mine"] = is_record_owner(record, viewer)
    return result


@router.get("/posts", summary="게시글 목록")
def get_posts(
    category: str | None = None,
    sort: str = "latest",
    q: str | None = None,
    period: str = "all",
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    mine: bool = False,
    status: str = "published",
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if sort not in {"latest", "likes", "comments"}:
        raise HTTPException(400, "sort는 latest, likes, comments 중 하나여야 합니다.")
    if status not in {"published", "draft"}:
        raise HTTPException(400, "status는 published 또는 draft여야 합니다.")
    if status == "draft" and not mine:
        raise HTTPException(400, "임시 저장 글은 mine=true로만 조회할 수 있습니다.")
    if period not in {"all", "today", "week", "month"}:
        raise HTTPException(400, "period는 all, today, week, month 중 하나여야 합니다.")
    records = list(
        db.scalars(
            select(Resource)
            .where(Resource.resource_type == "post")
            .order_by(Resource.id.desc())
        )
    )
    local_now = datetime.now(ZoneInfo(settings.app_timezone))
    period_start = {
        "today": local_now.replace(
            hour=0, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc),
        "week": local_now.astimezone(timezone.utc) - timedelta(days=7),
        "month": local_now.astimezone(timezone.utc) - timedelta(days=30),
    }.get(period)
    for value in (from_date, to_date):
        if value:
            try:
                date.fromisoformat(value)
            except ValueError:
                raise HTTPException(
                    400, "from과 to는 YYYY-MM-DD 형식이어야 합니다."
                ) from None
    if from_date and to_date and from_date > to_date:
        raise HTTPException(400, "from은 to보다 늦을 수 없습니다.")
    values = []
    for record in records:
        data = record.payload
        record_status = data.get("status", "published")
        if record_status != status:
            continue
        if mine and not is_record_owner(record, user):
            continue
        if not mine and record_status != "published":
            continue
        if category and data.get("category") != category:
            continue
        if period_start and record.created_at and record.created_at < period_start:
            continue
        created_date = (
            record.created_at.astimezone(ZoneInfo(settings.app_timezone))
            .date()
            .isoformat()
            if record.created_at
            else ""
        )
        if from_date and created_date < from_date:
            continue
        if to_date and created_date > to_date:
            continue
        if (
            q
            and q.casefold()
            not in (
                str(data.get("title", "")) + " " + str(data.get("body", ""))
            ).casefold()
        ):
            continue
        values.append(_post_view(record, db, user))
    sort_key = {
        "latest": lambda x: x.get("createdAt") or "",
        "likes": lambda x: x.get("likes", 0),
        "comments": lambda x: x.get("commentCount", 0),
    }[sort]
    values.sort(
        key=lambda x: (bool(x.get("pinned", False)), sort_key(x), int(x["id"])),
        reverse=True,
    )
    start = 0
    if cursor:
        positions = [index for index, item in enumerate(values) if item["id"] == cursor]
        if not positions:
            raise HTTPException(400, "cursor가 현재 게시글 목록에 없습니다.")
        start = positions[0] + 1
    page = values[start : start + limit]
    return envelope(
        {
            "items": page,
            "nextCursor": page[-1]["id"]
            if page and start + limit < len(values)
            else None,
        }
    )


@router.get("/posts/{post_id}", summary="게시글 상세")
def get_post(
    post_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    post = get_record(db, "post", post_id)
    status = post.payload.get("status", "published")
    if status != "published" and not is_record_owner(post, user):
        raise HTTPException(404, "게시글을 찾을 수 없습니다.")
    post.payload = {**post.payload, "views": int(post.payload.get("views", 0)) + 1}
    db.commit()
    return envelope(_post_view(post, db, user, True))


class PostCreate(BaseModel):
    category: str
    title: str = Field(default="", max_length=120)
    body: str = Field(default="", max_length=10000)
    anonymous: bool = False
    anonComments: bool = False
    attachments: list[Any] = Field(default_factory=list, max_length=5)
    status: str = "published"


class PostPatch(BaseModel):
    category: str | None = None
    title: str | None = Field(default=None, max_length=120)
    body: str | None = Field(default=None, max_length=10000)
    anonymous: bool | None = None
    anonComments: bool | None = None
    attachments: list[Any] | None = Field(default=None, max_length=5)
    status: str | None = None


class InquiryCreate(BaseModel):
    type: Literal["문의", "건의", "불편 신고"]
    dept: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=10000)
    anonymous: bool = False


class ProposalCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str | None = Field(default=None, max_length=10000)
    content: str | None = Field(default=None, max_length=10000)
    category: str = Field(default="업무환경", min_length=1, max_length=80)
    dept: str | None = Field(default=None, max_length=100)
    anonymous: bool = False


def _proposal_comments(db: Session, proposal_id: int) -> list[Resource]:
    return list(
        db.scalars(
            select(Resource)
            .where(
                Resource.resource_type == "proposal_comment",
                Resource.payload["proposalId"].as_string() == str(proposal_id),
            )
            .order_by(Resource.id.asc())
        )
    )


def _proposal_view(
    proposal: Resource, db: Session, user: User, detail: bool = False
) -> dict[str, Any]:
    data = proposal.payload
    anonymous = bool(data.get("anonymous"))
    author = db.get(User, proposal.owner_id) if proposal.owner_id else None
    reactions = list(
        db.scalars(
            select(Resource).where(
                Resource.resource_type == "proposal_recommend",
                Resource.payload["proposalId"].as_string() == str(proposal.id),
            )
        )
    )
    comments = _proposal_comments(db, proposal.id)
    result = {
        "id": str(proposal.id),
        "category": data.get("category", data.get("dept", "업무환경")),
        "title": data.get("title", ""),
        "body": data.get("body", data.get("content", "")),
        "status": data.get("status", "접수"),
        "anonymous": anonymous,
        "likes": len(reactions) or int(data.get("recommendCount", 0) or 0),
        "recommendCount": len(reactions) or int(data.get("recommendCount", 0) or 0),
        "commentCount": len(comments) + len(data.get("comments", [])),
        "createdAt": iso(proposal.created_at),
        "mine": is_record_owner(proposal, user),
        "answer": data.get("answer"),
        "history": data.get("history", []),
    }
    if not anonymous:
        result["author"] = author.name if author else data.get("author", "알 수 없음")
        result["dept"] = author.department if author else data.get("dept", "")
    if detail:
        comment_items = []
        for comment in comments:
            comment_data = comment.payload
            item = {
                "id": str(comment.id),
                "text": comment_data.get("text", ""),
                "createdAt": iso(comment.created_at),
                "anonymous": bool(comment_data.get("anonymous")),
                "mine": is_record_owner(comment, user),
            }
            if data.get("anonymous") and comment_data.get("isProposalAuthor"):
                item["author"] = "글쓴이"
            elif not comment_data.get("anonymous"):
                comment_author = (
                    db.get(User, comment.owner_id) if comment.owner_id else None
                )
                item["author"] = comment_author.name if comment_author else "알 수 없음"
            comment_items.append(item)
        for comment in data.get("comments", []):
            legacy_comment = public_content_payload(comment)
            if legacy_comment.get("anonymous"):
                is_author = legacy_comment.pop("isProposalAuthor", False)
                if is_author:
                    legacy_comment["author"] = "글쓴이"
                else:
                    legacy_comment.pop("author", None)
            comment_items.append(legacy_comment)
        result["comments"] = comment_items
    return result


@router.get("/proposals", summary="운영 제안 목록")
def get_proposals(
    category: str | None = None,
    status: str | None = None,
    q: str | None = None,
    sort: str = "latest",
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if sort not in {"latest", "likes", "comments"}:
        raise HTTPException(400, "sort는 latest, likes, comments 중 하나여야 합니다.")
    proposals = list(
        db.scalars(
            select(Resource)
            .where(Resource.resource_type == "proposal")
            .order_by(Resource.id.desc())
        )
    )
    values = []
    for proposal in proposals:
        data = proposal.payload
        if category and data.get("category", data.get("dept")) != category:
            continue
        if status and data.get("status", "접수") != status:
            continue
        if (
            q
            and q.casefold()
            not in (
                str(data.get("title", ""))
                + " "
                + str(data.get("body", data.get("content", "")))
            ).casefold()
        ):
            continue
        values.append(_proposal_view(proposal, db, user))
    sort_key = {
        "latest": lambda item: item.get("createdAt") or "",
        "likes": lambda item: item.get("likes", 0),
        "comments": lambda item: item.get("commentCount", 0),
    }[sort]
    values.sort(key=lambda item: (sort_key(item), int(item["id"])), reverse=True)
    start = 0
    if cursor:
        positions = [index for index, item in enumerate(values) if item["id"] == cursor]
        if not positions:
            raise HTTPException(400, "cursor가 현재 제안 목록에 없습니다.")
        start = positions[0] + 1
    page = values[start : start + limit]
    return envelope(
        {
            "items": page,
            "nextCursor": page[-1]["id"]
            if page and start + limit < len(values)
            else None,
        }
    )


@router.get("/proposals/{proposal_id}", summary="운영 제안 상세")
def get_proposal(
    proposal_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    return envelope(
        _proposal_view(get_record(db, "proposal", proposal_id), db, user, True)
    )


@router.post("/proposals", status_code=201, summary="운영 제안 작성")
def create_proposal(
    payload: ProposalCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    body = (payload.body or payload.content or "").strip()
    if not body:
        raise HTTPException(422, "제안 내용을 입력해주세요.")
    data = {
        "title": payload.title.strip(),
        "body": body,
        "content": body,
        "category": payload.category,
        "dept": payload.dept,
        "status": "접수",
        "anonymous": payload.anonymous,
        "recommendCount": 0,
        "history": [
            {
                "status": "접수",
                "who": ANONYMOUS_LABEL if payload.anonymous else user.name,
                "at": iso(datetime.now(timezone.utc)),
            }
        ],
    }
    anonymous = payload.anonymous
    if anonymous:
        data = anonymize_payload(data, user)
    else:
        data["author"] = user.name
    record = create_record(db, "proposal", data, None if anonymous else user)
    return envelope(
        {"id": str(record.id), "status": "접수"}, "제안을 등록했습니다.", 201
    )


@router.post("/proposals/{proposal_id}/recommend", summary="운영 제안 추천 토글")
@router.post(
    "/proposals/{proposal_id}/recommendation",
    summary="운영 제안 추천 토글",
    include_in_schema=False,
)
def recommend_proposal(
    proposal_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    proposal = get_record(db, "proposal", proposal_id)
    reaction = db.scalar(
        select(Resource).where(
            Resource.resource_type == "proposal_recommend",
            Resource.owner_id == user.id,
            Resource.payload["proposalId"].as_string() == proposal_id,
        )
    )
    if reaction:
        db.delete(reaction)
        recommended = False
    else:
        db.add(
            Resource(
                resource_type="proposal_recommend",
                owner_id=user.id,
                payload={"proposalId": proposal_id},
            )
        )
        recommended = True
    db.flush()
    count = (
        db.scalar(
            select(func.count())
            .select_from(Resource)
            .where(
                Resource.resource_type == "proposal_recommend",
                Resource.payload["proposalId"].as_string() == proposal_id,
            )
        )
        or 0
    )
    proposal.payload = {**proposal.payload, "recommendCount": count}
    db.commit()
    return envelope(
        {"recommended": recommended, "likes": count, "recommendCount": count}
    )


@router.post(
    "/proposals/{proposal_id}/comments", status_code=201, summary="운영 제안 댓글 작성"
)
def comment_on_proposal(
    proposal_id: str,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    proposal = get_record(db, "proposal", proposal_id)
    text = str(payload.get("text", payload.get("content", ""))).strip()
    if not text:
        raise HTTPException(422, "댓글 내용을 입력해주세요.")
    anonymous = bool(payload.get("anonymous", False))
    data = {
        "proposalId": proposal_id,
        "text": text,
        "anonymous": anonymous,
        "isProposalAuthor": is_record_owner(proposal, user),
    }
    if anonymous:
        data = anonymize_payload(data, user)
    else:
        data["author"] = user.name
    record = create_record(db, "proposal_comment", data, None if anonymous else user)
    return envelope(
        {
            "id": str(record.id),
            "text": text,
            "anonymous": anonymous,
            "author": "글쓴이"
            if proposal.payload.get("anonymous") and data.get("isProposalAuthor")
            else (None if anonymous else user.name),
        },
        "댓글을 등록했습니다.",
        201,
    )


POST_CATEGORIES = {
    "공지",
    "자유",
    "질문",
    "정보 공유",
    "모집",
    "칭찬",
    "제안",
    "팀 소식",
}


def _post_status(value: str) -> str:
    normalized = {"작성 중": "draft", "임시 저장": "draft"}.get(value, value)
    if normalized not in {"draft", "published"}:
        raise HTTPException(422, "게시글 상태는 draft 또는 published여야 합니다.")
    return normalized


def _validate_post_content(data: dict[str, Any]) -> None:
    if data.get("category") not in POST_CATEGORIES:
        raise HTTPException(422, "지원하지 않는 게시판 분류입니다.")
    if data.get("status") == "published":
        if not str(data.get("title", "")).strip():
            raise HTTPException(422, "게시글 제목을 입력해주세요.")
        if not str(data.get("body", "")).strip():
            raise HTTPException(422, "게시글 내용을 입력해주세요.")


def _post_attachments(
    db: Session, values: list[Any], user: User, anonymous: bool = False
) -> list[dict[str, Any]]:
    if len(values) > 5:
        raise HTTPException(422, "게시글 이미지는 최대 5개까지 첨부할 수 있습니다.")
    result = []
    seen = set()
    for value in values:
        file_id = (
            value.get("id", value.get("uploadId", value.get("fileId")))
            if isinstance(value, dict)
            else value
        )
        if not file_id and isinstance(value, dict):
            file_id = str(value.get("url", "")).split("/uploads/")[-1].split("?")[0]
        upload = get_record(db, "upload", str(file_id))
        if not is_record_owner(upload, user):
            raise HTTPException(403, "본인이 업로드한 이미지만 첨부할 수 있습니다.")
        content_type = upload.payload.get("contentType", "")
        if content_type not in {"image/jpeg", "image/png"}:
            raise HTTPException(
                422, "게시글 이미지는 JPG 또는 PNG만 첨부할 수 있습니다."
            )
        if int(upload.payload.get("size", 0)) > 10 * 1024 * 1024:
            raise HTTPException(413, "이미지는 한 장당 10MB 이하만 첨부할 수 있습니다.")
        if anonymous:
            upload.owner_id = None
            upload.payload = {
                **upload.payload,
                "anonymousOwnerKey": anonymous_owner_key(user.id),
            }
        if upload.id not in seen:
            result.append(
                {
                    "id": str(upload.id),
                    "url": f"/uploads/{upload.id}",
                    "filename": upload.payload.get("filename", "image"),
                    "size": int(upload.payload.get("size", 0)),
                    "contentType": content_type,
                }
            )
            seen.add(upload.id)
    return result


@router.post("/posts", status_code=201, summary="게시글 작성")
def create_post(
    payload: PostCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = payload.model_dump()
    data["status"] = _post_status(data["status"])
    anonymous = bool(data.get("anonymous"))
    data["attachments"] = _post_attachments(
        db, data["attachments"], user, anonymous=anonymous
    )
    _validate_post_content(data)
    if data["category"] == "공지" and "Admin" not in (user.roles or []):
        raise HTTPException(403, "공지 게시글 작성 권한이 없습니다.")
    data.update(
        views=0,
        likes=0,
        pinned=data["category"] == "공지" and "Admin" in (user.roles or []),
    )
    if anonymous:
        data = anonymize_payload(data, user)
    else:
        data["author"] = user.name
    post = create_record(db, "post", data, None if anonymous else user)
    return envelope({"id": str(post.id)}, "게시글을 등록했습니다.", 201)


@router.patch("/posts/{post_id}", summary="게시글 수정 또는 임시 저장 글 공개")
def patch_post(
    post_id: str,
    payload: PostPatch,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    post = get_record(db, "post", post_id)
    if not is_record_owner(post, user):
        raise HTTPException(403, "작성자만 게시글을 수정할 수 있습니다.")
    data = {**post.payload, **payload.model_dump(exclude_unset=True)}
    if "status" in payload.model_fields_set and payload.status is not None:
        data["status"] = _post_status(payload.status)
    anonymous = bool(data.get("anonymous"))
    if "attachments" in payload.model_fields_set and payload.attachments is not None:
        data["attachments"] = _post_attachments(
            db, payload.attachments, user, anonymous=anonymous
        )
    elif anonymous:
        data["attachments"] = _post_attachments(
            db, data.get("attachments", []), user, anonymous=True
        )
    _validate_post_content(data)
    if anonymous:
        data = anonymize_payload(data, user)
        post.owner_id = None
    else:
        data.pop("anonymousOwnerKey", None)
        data["author"] = user.name
        post.owner_id = user.id
    post.payload = data
    db.commit()
    db.refresh(post)
    return envelope({"id": post_id, "status": data["status"]}, "게시글을 수정했습니다.")


@router.delete("/posts/{post_id}", summary="게시글 삭제")
def delete_post(
    post_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    post = get_record(db, "post", post_id)
    if not is_record_owner(post, user) and "Admin" not in (user.roles or []):
        raise HTTPException(403, "작성자 또는 관리자만 삭제할 수 있습니다.")
    post.payload = {**post.payload, "status": "deleted"}
    db.commit()
    return envelope({"id": post_id, "deleted": True}, "게시글을 삭제했습니다.")


@router.post("/posts/{post_id}/like", summary="게시글 공감 토글")
def like_post(
    post_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    post = get_record(db, "post", post_id)
    if post.payload.get("status", "published") != "published":
        raise HTTPException(404, "게시글을 찾을 수 없습니다.")
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
    if post.payload.get("status", "published") != "published":
        raise HTTPException(404, "게시글을 찾을 수 없습니다.")
    text = str(payload.get("text", "")).strip()
    if not text:
        raise HTTPException(400, "댓글 내용을 입력해주세요.")
    reply_to = payload.get("replyTo")
    if reply_to:
        parent = get_record(db, "comment", reply_to)
        if str(parent.payload.get("postId")) != post_id:
            raise HTTPException(404, "상위 댓글을 찾을 수 없습니다.")
    is_post_author = is_record_owner(post, user)
    anonymous = (
        bool(payload.get("anonymous", False))
        or bool(post.payload.get("anonComments", False))
        or bool(post.payload.get("anonymous"))
        and is_post_author
    )
    comment_data = {
        "postId": post_id,
        "text": text,
        "anonymous": anonymous,
        "replyTo": reply_to,
        "likes": 0,
        "isPostAuthor": is_post_author,
    }
    if anonymous:
        comment_data = anonymize_payload(comment_data, user)
    else:
        comment_data["author"] = user.name
    record = create_record(db, "comment", comment_data, None if anonymous else user)
    post_author = (
        db.get(User, post.owner_id)
        if post.owner_id
        else resolve_anonymous_owner(db, post.payload.get("anonymousOwnerKey"))
    )
    if post_author is not None and not is_post_author:
        notify_user = __import__("app.api.shared", fromlist=["notify_user"]).notify_user
        notify_user(
            db,
            post_author.id,
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
    post = get_record(db, "post", post_id)
    if post.payload.get("status", "published") != "published":
        raise HTTPException(404, "게시글을 찾을 수 없습니다.")
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
    stmt = (
        select(Resource)
        .where(Resource.resource_type == "inquiry", owner_filter(user))
        .order_by(Resource.id.desc())
    )
    if cursor:
        try:
            stmt = stmt.where(Resource.id < int(cursor))
        except ValueError:
            raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    records = list(db.scalars(stmt.limit(limit + 1)))
    items = [
        {"id": str(record.id), **public_content_payload(record.payload)}
        for record in records[:limit]
    ]
    return envelope(
        {
            "items": items,
            "nextCursor": str(records[limit - 1].id) if len(records) > limit else None,
        }
    )


@router.post("/inquiries", status_code=201, summary="문의·건의 작성")
def create_inquiry(
    payload: InquiryCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = payload.model_dump()
    anonymous = payload.anonymous
    data = {
        **data,
        "status": "접수",
        "steps": [iso(datetime.now().astimezone()), "", ""],
        "answer": None,
        "replies": [],
    }
    if anonymous:
        data = anonymize_payload(data, user)
    else:
        data["author"] = user.name
    record = create_record(db, "inquiry", data, None if anonymous else user)
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
    if not is_record_owner(record, user):
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
    is_owner = is_record_owner(record, user)
    if not is_owner and not set(user.roles or []).intersection(
        {"Admin", record.payload.get("dept")}
    ):
        raise HTTPException(404, "문의를 찾을 수 없습니다.")
    text = payload.get("text", "").strip()
    if not text:
        raise HTTPException(400, "메시지를 입력해주세요.")
    entry = {
        "text": text,
        "at": iso(datetime.now().astimezone()),
        "who": ANONYMOUS_LABEL
        if record.payload.get("anonymous") and is_owner
        else user.name,
    }
    if not (record.payload.get("anonymous") and is_owner):
        entry["whoId"] = str(user.id)
    record.payload = {
        **record.payload,
        "replies": [*record.payload.get("replies", []), entry],
    }
    db.commit()
    return envelope(entry, "추가 문의를 등록했습니다.", 201)


@router.get("/notices", summary="공지 목록")
def get_notices(
    kind: str | None = None,
    q: str | None = None,
    pinned: bool | None = None,
    must_read: bool | None = Query(None, alias="mustRead"),
    sort: str = "latest",
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if sort not in {"latest", "pinned"}:
        raise HTTPException(400, "sort는 latest 또는 pinned여야 합니다.")
    records = list(
        db.scalars(
            select(Resource)
            .where(Resource.resource_type == "notice")
            .order_by(Resource.id.desc())
        )
    )
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
        if kind and data.get("kind") != kind:
            continue
        if pinned is not None and bool(data.get("pinned", False)) != pinned:
            continue
        if must_read is not None and bool(data.get("mustRead", False)) != must_read:
            continue
        if (
            q
            and q.casefold()
            not in (
                str(data.get("title", "")) + " " + str(data.get("body", ""))
            ).casefold()
        ):
            continue
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
    values.sort(
        key=lambda item: (
            bool(item.get("pinned", False)),
            item.get("createdAt", ""),
            int(item["id"]),
        ),
        reverse=True,
    )
    start = 0
    if cursor:
        positions = [index for index, item in enumerate(values) if item["id"] == cursor]
        if not positions:
            raise HTTPException(400, "cursor가 현재 공지 목록에 없습니다.")
        start = positions[0] + 1
    page = values[start : start + limit]
    return envelope(
        {
            "items": page,
            "nextCursor": page[-1]["id"]
            if page and start + limit < len(values)
            else None,
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
