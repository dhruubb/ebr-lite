"""The ONLY place that writes to the audit trail. Every service calls log().

URS-008 / 21 CFR 11.10(e) / ALCOA+: who (user), what (action, entity, field, old -> new),
when (UTC timestamp, set by the server, never by the client), why (reason).
"""
from sqlalchemy.orm import Session

from .. import clock
from ..models import AuditTrail, User


def _s(v):
    return None if v is None else str(v)


def log(db: Session, user: User | None, action: str, entity: str, entity_id=None, *,
        field: str | None = None, old=None, new=None, reason: str | None = None,
        batch_id: int | None = None, username: str | None = None) -> AuditTrail:
    row = AuditTrail(
        utc_ts=clock.utcnow(),
        user_id=user.id if user else None,
        username=user.username if user else (username or "system"),
        action=action,
        entity=entity,
        entity_id=_s(entity_id),
        field=field,
        old_value=_s(old),
        new_value=_s(new),
        reason=reason,
        batch_id=batch_id,
    )
    db.add(row)
    return row


def serialize(a: AuditTrail) -> dict:
    return {
        "id": a.id, "utc_ts": clock.iso(a.utc_ts), "user_id": a.user_id, "username": a.username,
        "action": a.action, "entity": a.entity, "entity_id": a.entity_id, "field": a.field,
        "old_value": a.old_value, "new_value": a.new_value, "reason": a.reason, "batch_id": a.batch_id,
    }
