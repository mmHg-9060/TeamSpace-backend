import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.resource import Resource

logger = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


def _push_allowed(db: Session, user_id: int, kind: str) -> bool:
    settings_row = db.scalar(
        select(Resource).where(
            Resource.resource_type == "notification_settings",
            Resource.owner_id == user_id,
        )
    )
    if not settings_row:
        return True
    config = settings_row.payload or {}
    kinds = config.get("kinds", {})
    if kind in kinds and not kinds[kind]:
        return False
    nested_dnd = config.get("dnd") if isinstance(config.get("dnd"), dict) else {}
    dnd_enabled = nested_dnd.get("enabled", config.get("dnd", False))
    if not dnd_enabled:
        return True
    now = datetime.now(KST)
    day_labels = ["월", "화", "수", "목", "금", "토", "일"]
    days = nested_dnd.get("days", config.get("dndDays", []))
    if day_labels[now.weekday()] not in days:
        return True
    start = nested_dnd.get("from", config.get("dndFrom", "23:00"))
    end = nested_dnd.get("to", config.get("dndTo", "08:00"))
    current = now.strftime("%H:%M")
    return (
        not (start <= current or current < end)
        if start > end
        else not (start <= current < end)
    )


def send_push(db: Session, user_id: int, notification: dict) -> None:
    if not (settings.vapid_public_key and settings.vapid_private_key):
        return
    if not _push_allowed(
        db, user_id, notification.get("sourceKind", notification.get("kind", ""))
    ):
        return
    subscriptions = list(
        db.scalars(
            select(Resource).where(
                Resource.resource_type == "push_subscription",
                Resource.owner_id == user_id,
            )
        )
    )
    if not subscriptions:
        return
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        logger.warning(
            "pywebpush is missing; reinstall requirements to enable browser push"
        )
        return
    for subscription in subscriptions:
        try:
            webpush(
                subscription_info=subscription.payload,
                data=json.dumps(notification, ensure_ascii=False),
                vapid_private_key=settings.vapid_private_key,
                vapid_claims={"sub": settings.vapid_subject},
            )
        except WebPushException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in {404, 410}:
                db.delete(subscription)
                db.commit()
            logger.warning(
                "Web Push delivery failed for subscription %s (%s)",
                subscription.id,
                status_code,
            )
