from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.shared import envelope, iso, user_can_finance, user_profile
from app.db.session import get_db
from app.models.resource import Resource
from app.models.user import User

router = APIRouter()

SEARCH_TYPES = {
    "ALL": {"doc", "rule", "project", "person", "post", "proposal"},
    "DOC": {"doc", "rule"},
    "DOCS": {"doc", "rule"},
    "DOCUMENT": {"doc", "rule"},
    "DOCUMENTS": {"doc", "rule"},
    "RULE": {"rule", "doc"},
    "PROJECT": {"project"},
    "PEOPLE": {"person"},
    "PERSON": {"person"},
    "POST": {"post"},
    "PROPOSAL": {"proposal"},
}


def _match(value: object, needle: str) -> bool:
    return needle.casefold() in str(value or "").casefold()


def _within_period(created_at: datetime | None, period: str) -> bool:
    if period == "all":
        return True
    days = {"7d": 7, "30d": 30, "90d": 90}.get(period)
    if days is None:
        raise HTTPException(400, "period는 all, 7d, 30d, 90d 중 하나여야 합니다.")
    if created_at is None:
        return False
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return created_at >= datetime.now(timezone.utc) - timedelta(days=days)


@router.get("/search", summary="권한에 따른 통합 검색")
def search(
    query: str | None = Query(None, min_length=2),
    q: str | None = Query(None, min_length=2),
    type: str = "ALL",
    period: str = "all",
    dept: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    needle = (query or q or "").strip()
    if len(needle) < 2:
        raise HTTPException(422, "검색어는 2자 이상 입력해주세요.")
    kinds = SEARCH_TYPES.get(type.upper())
    if kinds is None:
        raise HTTPException(400, "지원하지 않는 검색 유형입니다.")

    matches = []
    for kind in kinds:
        if kind == "person":
            for person in db.scalars(select(User).order_by(User.name)):
                profile = user_profile(db, person)
                if dept and profile.get("dept") != dept:
                    continue
                title = profile.get("name", "")
                searchable = " ".join(
                    str(profile.get(key, "")) for key in ("name", "dept", "role", "bio")
                )
                if not _match(searchable, needle):
                    continue
                matches.append(
                    {
                        "category": "people",
                        "id": str(person.id),
                        "title": title,
                        "snippet": " · ".join(
                            value
                            for value in (profile.get("dept"), profile.get("role"))
                            if value
                        ),
                        "createdAt": iso(person.created_at),
                        "href": f"/inhouse/people/{person.id}",
                    }
                )
            continue

        resource_type = kind
        records = db.scalars(
            select(Resource)
            .where(Resource.resource_type == resource_type)
            .order_by(Resource.created_at.desc())
            .limit(1000)
        )
        for record in records:
            data = record.payload or {}
            if not _within_period(record.created_at, period):
                continue
            if kind == "post":
                if data.get("status", "published") != "published":
                    continue
                # Anonymous content is kept out of global search to avoid identity inference.
                if data.get("anonymous") or data.get("isAnonymous"):
                    continue
            if kind == "proposal" and data.get("anonymous"):
                continue
            doc_type = str(data.get("type", data.get("category", ""))).upper()
            if kind == "doc" and doc_type == "FINANCE" and not user_can_finance(user):
                continue
            record_dept = str(data.get("dept", data.get("department", "")))
            if dept and kind == "project" and record_dept != dept:
                continue
            title = str(data.get("title", data.get("name", "")))
            content = str(
                data.get(
                    "copy",
                    data.get("body", data.get("description", data.get("content", ""))),
                )
            )
            if not _match(f"{title} {content}", needle):
                continue
            category = {
                "doc": "documents",
                "rule": "documents",
                "project": "projects",
                "post": "posts",
                "proposal": "proposals",
            }[kind]
            item = {
                "category": category,
                "id": str(record.id),
                "title": title,
                "snippet": content[:160],
                "createdAt": iso(record.created_at),
                "href": {
                    "doc": f"/inhouse/documents/{data.get('slug', record.id)}",
                    "rule": f"/inhouse/documents/{data.get('slug', record.id)}",
                    "project": f"/inhouse/projects/{data.get('slug', record.id)}",
                    "post": f"/inhouse/community/{record.id}",
                    "proposal": f"/inhouse/community/proposals/{record.id}",
                }[kind],
            }
            if kind in {"doc", "rule"}:
                item["type"] = doc_type
                item["version"] = data.get("version")
            elif kind == "post":
                item["author"] = data.get("author", "")
                item["comments"] = int(data.get("commentCount", 0) or 0)
            elif kind == "proposal":
                item["status"] = data.get("status", "")
                item["likes"] = int(
                    data.get("likes", data.get("recommendations", 0)) or 0
                )
            elif kind == "project":
                item["dept"] = record_dept
                item["pm"] = data.get("pm", "")
                item["progress"] = data.get("progress", 0)
            matches.append(item)

    matches.sort(
        key=lambda item: (
            needle.casefold() in item["title"].casefold(),
            item.get("createdAt") or "",
        ),
        reverse=True,
    )
    groups: dict[str, list] = {
        "documents": [],
        "projects": [],
        "people": [],
        "posts": [],
        "proposals": [],
    }
    for item in matches:
        groups[item["category"]].append(item)
    counts = {key: len(value) for key, value in groups.items()}
    try:
        offset = int(cursor or 0)
    except ValueError:
        raise HTTPException(400, "cursor 값이 올바르지 않습니다.") from None
    if offset < 0 or offset > len(matches):
        raise HTTPException(400, "cursor 값이 현재 검색 결과 범위를 벗어났습니다.")
    page = matches[offset : offset + limit]
    return envelope(
        {
            "query": needle,
            "groups": groups,
            "counts": counts,
            "items": page,
            "result": page,
            "nextCursor": str(offset + limit)
            if offset + limit < len(matches)
            else None,
        }
    )
