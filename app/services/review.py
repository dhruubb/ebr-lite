"""QA review by exception and batch disposition (S11 / 21 CFR 211.192).

QA does not re-read every value. The system lists only the exceptions: out-of-limit values,
rejected weighings, corrections, missed IPCs and deviations (URS-011). Approval is blocked while
any deviation is open (URS-012) and needs an APPROVED e-signature (URS-007).
"""
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..errors import Blocked, Forbidden, Invalid
from ..models import Batch, Deviation, User
from . import audit, signatures, states
from .records import batch_detail


def _require_qa(user: User):
    if user.role != "QA":
        raise Forbidden("Only QA can review and disposition batches.")


def exceptions(db: Session, b: Batch) -> list[dict]:
    rec = batch_detail(db, b)
    out = []
    for s in rec["steps"]:
        for blk in s["blocks"]:
            for e in blk["entries"]:
                if not e["in_limits"]:
                    kind = "REJECTED_WEIGHING" if blk["type"] == "MATERIAL_DISPENSE" else "OUT_OF_LIMIT"
                    out.append({"type": kind, "step": s["code"], "ref": f"entry #{e['id']}",
                                "detail": f"{e['label']}: {e['value']} {e['unit'] or ''} (limits {e['limits']})"
                                          + ("" if e["accepted"] or blk["type"] != "MATERIAL_DISPENSE" else " – re-weighed / not used"),
                                "by": e["entered_by"], "at": e["entered_at"]})
                if e["corrected"]:
                    out.append({"type": "CORRECTION", "step": s["code"], "ref": f"entry #{e['id']}",
                                "detail": f"{e['label']}: {e['original_value']} → {e['value']} (see audit trail for reason)",
                                "by": e["entered_by"], "at": e["entered_at"]})
            for sm in blk["samples"]:
                if sm["status"] == "MISSED":
                    out.append({"type": "MISSED_IPC", "step": s["code"], "ref": f"sample #{sm['sample_no']}",
                                "detail": f"IPC sample due {clock.ist(datetime.fromisoformat(sm['due_at'].rstrip('Z')))} was not taken", "by": None, "at": sm["due_at"]})
                elif not sm["in_limits"]:
                    bad = ", ".join(f"{r['label']} {r['value']}" for r in sm["results"] if not r["in_limits"])
                    out.append({"type": "OUT_OF_LIMIT", "step": s["code"], "ref": f"sample #{sm['sample_no']}",
                                "detail": f"IPC: {bad}", "by": sm["taken_by"], "at": sm["taken_at"]})
    for d in rec["deviations"]:
        out.append({"type": "DEVIATION", "step": d["step_code"], "ref": d["number"],
                    "detail": f"[{d['status']}] {d['description']}"
                              + (f" — closed by {d['closed_by']}: {d['justification']}" if d["status"] == "CLOSED" else ""),
                    "by": d["raised_by"], "at": d["raised_at"], "status": d["status"]})
    return out


def start_review(db: Session, b: Batch, user: User):
    _require_qa(user)
    states.transition(db, b, states.BATCH, "UNDER_REVIEW", user, entity="batch", batch_id=b.id)
    b.review_started_at = clock.utcnow()


def approve(db: Session, b: Batch, user: User, username: str, password: str, comment: str | None):
    _require_qa(user)
    if b.status != "UNDER_REVIEW":
        raise Blocked(f"Batch is {b.status}; start the review first.")
    open_devs = db.scalar(select(func.count()).select_from(Deviation)
                          .where(Deviation.batch_id == b.id, Deviation.status == "OPEN")) or 0
    if open_devs:
        # URS-012: no approval while any deviation is open.
        audit.log(db, user, "APPROVAL_BLOCKED", "batch", b.id, reason=f"{open_devs} open deviation(s)", batch_id=b.id)
        db.commit()
        raise Blocked(f"Batch cannot be approved: {open_devs} deviation(s) still open.")
    signatures.sign(db, user, username=username, password=password, meaning="APPROVED", entity="batch",
                    entity_id=b.id, batch_id=b.id, allowed_roles=["QA"], must_be_session_user=True)
    states.transition(db, b, states.BATCH, "APPROVED", user, entity="batch", batch_id=b.id, reason=comment)
    b.disposition_at, b.disposition_by_id, b.disposition_comment = clock.utcnow(), user.id, comment


def reject(db: Session, b: Batch, user: User, username: str, password: str, reason: str):
    _require_qa(user)
    if b.status != "UNDER_REVIEW":
        raise Blocked(f"Batch is {b.status}; start the review first.")
    if not reason or not reason.strip():
        raise Invalid("A reason is required to reject a batch.")
    signatures.sign(db, user, username=username, password=password, meaning="REJECTED", entity="batch",
                    entity_id=b.id, batch_id=b.id, allowed_roles=["QA"], must_be_session_user=True)
    states.transition(db, b, states.BATCH, "REJECTED", user, entity="batch", batch_id=b.id, reason=reason.strip())
    b.disposition_at, b.disposition_by_id, b.disposition_comment = clock.utcnow(), user.id, reason.strip()
