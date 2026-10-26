from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.api.shared import create_record, envelope, iso, list_records
from app.db.session import get_db
from app.models.resource import Resource
from app.models.user import User

router = APIRouter()


def _doc(db: Session, slug: str) -> Resource:
    record = db.scalar(
        select(Resource).where(
            Resource.resource_type == "doc",
            Resource.payload["slug"].as_string() == slug,
        )
    )
    if record is None:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    return record


@router.get("/docs", summary="문서함 목록")
def get_docs(
    q: str | None = None,
    category: str | None = None,
    type: str | None = None,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    records, next_cursor = list_records(db, "doc", cursor, min(limit * 4, 400))
    docs = []
    for record in records:
        data = record.payload
        if category and data.get("category") != category:
            continue
        if type and data.get("type") != type:
            continue
        if (
            q
            and q.casefold()
            not in (
                str(data.get("title", "")) + " " + str(data.get("copy", ""))
            ).casefold()
        ):
            continue
        docs.append(
            {
                k: data.get(k)
                for k in (
                    "slug",
                    "category",
                    "type",
                    "title",
                    "copy",
                    "date",
                    "version",
                    "effective",
                    "owner",
                    "revised",
                )
                if k in data
            }
        )
    page = docs[:limit]
    return envelope(
        {"items": page, "nextCursor": next_cursor if len(docs) > limit else None}
    )


@router.get("/docs/{slug}", summary="문서 상세")
def get_doc(
    slug: str,
    version: str | None = None,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    record = _doc(db, slug)
    data = record.payload
    if version and version != data.get("version"):
        history = db.scalar(
            select(Resource).where(
                Resource.resource_type == "doc_revision",
                Resource.payload["slug"].as_string() == slug,
                Resource.payload["version"].as_string() == version,
            )
        )
        if history is None:
            raise HTTPException(404, "요청한 문서 버전을 찾을 수 없습니다.")
        data = history.payload.get("document", data)
    result = {**data}
    if data.get("type") == "TEMPLATE":
        result.setdefault("fields", data.get("fields", []))
        if data.get("fileId"):
            result["downloadUrl"] = f"/uploads/{data['fileId']}"
    return envelope(result)


@router.get("/docs/{slug}/history", summary="문서 개정 이력")
def get_doc_history(
    slug: str,
    version: str | None = None,
    cursor: str | None = None,
    limit: int = Query(50, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _doc(db, slug)
    records, next_cursor = list_records(db, "doc_revision", cursor, min(limit * 5, 500))
    history = [r.payload for r in records if r.payload.get("slug") == slug]
    if version:
        chosen = next(
            (
                r
                for r in history
                if r.get("ver") == version or r.get("version") == version
            ),
            None,
        )
        if chosen is None:
            raise HTTPException(404, "문서 버전을 찾을 수 없습니다.")
        return envelope(chosen.get("document", chosen))
    rows = [
        {k: r.get(k) for k in ("ver", "date", "who", "note")} for r in history[:limit]
    ]
    return envelope(
        {"items": rows, "nextCursor": next_cursor if len(history) > limit else None}
    )


@router.post("/docs/{slug}/read", summary="문서 읽음 확인")
def read_doc(
    slug: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    _doc(db, slug)
    receipt = db.scalar(
        select(Resource).where(
            Resource.resource_type == "doc_read",
            Resource.owner_id == user.id,
            Resource.payload["slug"].as_string() == slug,
        )
    )
    if receipt is None:
        create_record(
            db,
            "doc_read",
            {"slug": slug, "readAt": iso(datetime.now().astimezone())},
            user,
        )
    from sqlalchemy import func

    total = (
        db.scalar(
            select(func.count())
            .select_from(Resource)
            .where(
                Resource.resource_type == "doc_read",
                Resource.payload["slug"].as_string() == slug,
            )
        )
        or 0
    )
    return envelope({"slug": slug, "read": True, "confirmed": total})
