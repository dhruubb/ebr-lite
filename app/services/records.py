"""Read models: the executed batch record as JSON (used by the UI, the review and the report)."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import Batch, Deviation, Signature, User
from . import deviations as dev_svc
from . import signatures as sig_svc


def _users(db: Session) -> dict[int, User]:
    return {u.id: u for u in db.scalars(select(User))}


def batch_summary(db: Session, b: Batch) -> dict:
    open_devs = sum(1 for _ in db.scalars(select(Deviation.id).where(Deviation.batch_id == b.id, Deviation.status == "OPEN")))
    current = next((s for s in b.steps if s.status in ("READY", "IN_PROGRESS", "ON_HOLD")), None)
    return {
        "id": b.id, "batch_no": b.batch_no, "status": b.status,
        "recipe": {"id": b.recipe.id, "code": b.recipe.code, "version": b.recipe.version, "name": b.recipe.name},
        "product": b.recipe.product, "batch_size": b.recipe.batch_size, "batch_size_uom": b.recipe.batch_size_uom,
        "created_by": b.created_by.full_name, "created_at": clock.iso(b.created_at),
        "started_at": clock.iso(b.started_at), "completed_at": clock.iso(b.completed_at),
        "review_started_at": clock.iso(b.review_started_at), "disposition_at": clock.iso(b.disposition_at),
        "disposition_by": b.disposition_by.full_name if b.disposition_by else None,
        "disposition_comment": b.disposition_comment,
        "current_step": current.code if current else None,
        "open_deviations": open_devs,
        "steps_completed": sum(1 for s in b.steps if s.status == "COMPLETED"), "steps_total": len(b.steps),
    }


def batch_detail(db: Session, b: Batch) -> dict:
    users = _users(db)
    name = lambda uid: users[uid].full_name if uid in users else None  # noqa: E731
    block_ids = [blk.id for s in b.steps for blk in s.blocks]
    sigs: dict[int, list] = {}
    for sg in db.scalars(select(Signature).where(Signature.entity == "block", Signature.entity_id.in_(block_ids))
                         .order_by(Signature.id)):
        sigs.setdefault(sg.entity_id, []).append(sig_svc.serialize(sg))

    steps = []
    for s in b.steps:
        current = next((blk.idx for blk in s.blocks if blk.status == "PENDING"), None)
        blocks = []
        for blk in s.blocks:
            blocks.append({
                "id": blk.id, "idx": blk.idx, "type": blk.block_type, "status": blk.status, "config": blk.config,
                "state": blk.state, "completed_at": clock.iso(blk.completed_at), "completed_by": name(blk.completed_by_id),
                "entries": [{
                    "id": e.id, "key": e.key, "label": e.label, "value": e.value, "unit": e.unit, "limits": e.limits,
                    "in_limits": e.in_limits, "accepted": e.accepted, "corrected": e.corrected,
                    "original_value": e.original_value, "context": e.context,
                    "entered_by": name(e.entered_by_id), "entered_at": clock.iso(e.entered_at),
                } for e in blk.entries],
                "samples": [{
                    "id": sm.id, "sample_no": sm.sample_no, "status": sm.status, "due_at": clock.iso(sm.due_at),
                    "taken_at": clock.iso(sm.taken_at), "taken_by": name(sm.taken_by_id) if sm.taken_by_id else None,
                    "results": sm.results, "in_limits": sm.in_limits,
                } for sm in blk.samples],
                "signatures": sigs.get(blk.id, []),
            })
        steps.append({
            "id": s.id, "seq": s.seq, "code": s.code, "name": s.name, "status": s.status,
            "started_at": clock.iso(s.started_at), "started_by": name(s.started_by_id),
            "completed_at": clock.iso(s.completed_at), "current_block": current, "blocks": blocks,
        })

    devs = db.scalars(select(Deviation).where(Deviation.batch_id == b.id).order_by(Deviation.id))
    batch_sigs = db.scalars(select(Signature).where(Signature.entity == "batch", Signature.entity_id == b.id))
    return {**batch_summary(db, b), "steps": steps,
            "deviations": [dev_svc.serialize(d) for d in devs],
            "batch_signatures": [sig_svc.serialize(x) for x in batch_sigs]}
