"""QA review by exception, disposition, batch report and audit trail viewer."""
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, require_roles
from ..errors import NotFound
from ..models import AuditTrail, Batch, User
from ..services import audit, records, report, review

router = APIRouter(prefix="/api", tags=["review"])


def _batch(db: Session, batch_id: int) -> Batch:
    b = db.get(Batch, batch_id)
    if b is None:
        raise NotFound(f"Batch {batch_id} not found.")
    return b


class SignIn(BaseModel):
    username: str
    password: str
    comment: str | None = None


@router.get("/batches/{batch_id}/review")
def get_review(batch_id: int, db: Session = Depends(get_db), _: User = Depends(require_roles("QA"))):
    b = _batch(db, batch_id)
    return {"batch": records.batch_summary(db, b), "exceptions": review.exceptions(db, b)}


@router.post("/batches/{batch_id}/review/start")
def start_review(batch_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("QA"))):
    b = _batch(db, batch_id)
    review.start_review(db, b, user)
    db.commit()
    return records.batch_summary(db, b)


@router.post("/batches/{batch_id}/review/approve")
def approve(batch_id: int, body: SignIn, db: Session = Depends(get_db), user: User = Depends(require_roles("QA"))):
    b = _batch(db, batch_id)
    review.approve(db, b, user, body.username, body.password, body.comment)
    db.commit()
    return records.batch_summary(db, b)


@router.post("/batches/{batch_id}/review/reject")
def reject(batch_id: int, body: SignIn, db: Session = Depends(get_db), user: User = Depends(require_roles("QA"))):
    b = _batch(db, batch_id)
    review.reject(db, b, user, body.username, body.password, body.comment or "")
    db.commit()
    return records.batch_summary(db, b)


@router.get("/batches/{batch_id}/report", response_class=HTMLResponse)
def batch_report(batch_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return HTMLResponse(report.render(db, _batch(db, batch_id)))


# --------------------------------------------------------------------- audit trail (read-only)
@router.get("/audit")
def audit_trail(batch_id: int | None = None, username: str | None = None, action: str | None = None,
                entity: str | None = None, date_from: datetime | None = None, date_to: datetime | None = None,
                limit: int = 500, db: Session = Depends(get_db),
                _: User = Depends(require_roles("QA", "SUPERVISOR", "ADMIN"))):
    """There is deliberately no PUT/PATCH/DELETE route for the audit trail (URS-008)."""
    q = select(AuditTrail).order_by(AuditTrail.id.desc()).limit(min(limit, 5000))
    if batch_id:
        q = q.where(AuditTrail.batch_id == batch_id)
    if username:
        q = q.where(AuditTrail.username == username)
    if action:
        q = q.where(AuditTrail.action == action)
    if entity:
        q = q.where(AuditTrail.entity == entity)
    if date_from:
        q = q.where(AuditTrail.utc_ts >= date_from.replace(tzinfo=None))
    if date_to:
        q = q.where(AuditTrail.utc_ts <= date_to.replace(tzinfo=None))
    return [audit.serialize(a) for a in db.scalars(q)]
