"""Deviation handling (21 CFR 211.192).

open  : raised automatically by the engine (out-of-limit, checklist No, missed IPC, yield) or manually
        by an operator/supervisor. The step goes ON_HOLD.            URS-005
close : SUPERVISOR only, with justification + e-signature. When the last open deviation of a step
        is closed the step resumes (ON_HOLD -> IN_PROGRESS).        URS-005, URS-007
"""
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock
from ..errors import Blocked, Forbidden, Invalid, NotFound
from ..models import Batch, DataEntry, Deviation, IpcSample, StepExecution, User
from . import audit, signatures, states


def open_deviation(db: Session, step: StepExecution | None, user: User, category: str, description: str, *,
                   source: str = "AUTO", entry: DataEntry | None = None, sample: IpcSample | None = None,
                   batch: Batch | None = None) -> Deviation:
    """Open a deviation on a step (step goes ON_HOLD) or, with step=None, on the whole batch (QA review)."""
    batch_id = step.batch_id if step else batch.id
    year = clock.utcnow().year
    n = db.scalar(select(func.count()).select_from(Deviation).where(Deviation.number.like(f"DEV-{year}-%"))) or 0
    dev = Deviation(number=f"DEV-{year}-{n + 1:04d}", batch_id=batch_id, step_execution_id=step.id if step else None,
                    related_entry_id=entry.id if entry else None, related_sample_id=sample.id if sample else None,
                    source=source, category=category, description=description, status="OPEN",
                    raised_by_id=user.id, raised_at=clock.utcnow())
    db.add(dev)
    db.flush()
    audit.log(db, user, "DEVIATION_OPENED", "deviation", dev.id, field="status", new="OPEN",
              reason=f"{dev.number}: {description}", batch_id=batch_id)
    if step is not None and step.status == "IN_PROGRESS":
        states.transition(db, step, states.STEP, "ON_HOLD", user, entity="step", batch_id=batch_id,
                          reason=f"{dev.number} opened")
    return dev


def raise_manual(db: Session, user: User, description: str, *, step_id: int | None = None,
                 batch_id: int | None = None, related_entry_id: int | None = None) -> Deviation:
    if not description or not description.strip():
        raise Invalid("A description is required.")

    if step_id is None:
        # Batch-level deviation, e.g. found by QA during review of a completed batch.
        if user.role not in ("QA", "SUPERVISOR"):
            raise Forbidden("Only QA or a SUPERVISOR can raise a batch-level deviation.")
        batch = db.get(Batch, batch_id) if batch_id else None
        if batch is None:
            raise NotFound("Give a step_execution_id or a valid batch_id.")
        if batch.status not in ("COMPLETED", "UNDER_REVIEW"):
            raise Blocked("Batch-level deviations are raised on completed batches; raise shop-floor deviations on the step.")
        return open_deviation(db, None, user, "OTHER", description.strip(), source="MANUAL", batch=batch)

    if user.role not in ("OPERATOR", "SUPERVISOR"):
        raise Forbidden(f"Role {user.role} cannot raise a deviation on a shop-floor step.")
    step = db.get(StepExecution, step_id)
    if step is None:
        raise NotFound(f"Step {step_id} not found.")
    if step.status not in ("IN_PROGRESS", "ON_HOLD"):
        raise Blocked(f"Deviations can be raised only on an active step (step {step.code} is {step.status}).")
    entry = None
    category = "OTHER"
    if related_entry_id is not None:
        entry = db.get(DataEntry, related_entry_id)
        if entry is None or entry.block.step_execution_id != step.id:
            raise Invalid("The related entry does not belong to this step.")
        if not entry.accepted:
            category = "WEIGHT"
            if any(e.accepted and e.key == entry.key for e in entry.block.entries):
                raise Blocked("This material has already been re-weighed and accepted.")
    return open_deviation(db, step, user, category, description.strip(), source="MANUAL", entry=entry)


def close(db: Session, dev_id: int, user: User, justification: str, username: str, password: str) -> Deviation:
    if user.role != "SUPERVISOR":
        raise Forbidden("Only a SUPERVISOR can close a deviation.")
    dev = db.get(Deviation, dev_id)
    if dev is None:
        raise NotFound(f"Deviation {dev_id} not found.")
    if dev.status != "OPEN":
        raise Blocked(f"{dev.number} is already {dev.status}.")
    if not justification or not justification.strip():
        raise Invalid("A justification is required to close a deviation.")

    signatures.sign(db, user, username=username, password=password, meaning="REVIEWED", entity="deviation",
                    entity_id=dev.id, batch_id=dev.batch_id, allowed_roles=["SUPERVISOR"], must_be_session_user=True)
    dev.status, dev.closed_by_id, dev.closed_at, dev.justification = "CLOSED", user.id, clock.utcnow(), justification.strip()
    audit.log(db, user, "DEVIATION_CLOSED", "deviation", dev.id, field="status", old="OPEN", new="CLOSED",
              reason=f"{dev.number}: {dev.justification}", batch_id=dev.batch_id)

    # A rejected weighing covered by this deviation is accepted "use as is" by the supervisor.
    from . import engine   # local import: engine imports this module

    if dev.related_entry_id:
        e = db.get(DataEntry, dev.related_entry_id)
        if not e.accepted and not any(x.accepted and x.key == e.key for x in e.block.entries):
            e.accepted = True
            audit.log(db, user, "ENTRY_ACCEPTED", "data_entry", e.id, field="accepted", old="False", new="True",
                      reason=f"accepted under {dev.number}", batch_id=dev.batch_id)

    db.flush()   # make the CLOSED status visible to the count below (autoflush is off)
    step = dev.step
    if step is not None and step.status == "ON_HOLD":
        still_open = db.scalar(select(func.count()).select_from(Deviation).where(
            Deviation.step_execution_id == step.id, Deviation.status == "OPEN")) or 0
        if still_open == 0:
            states.transition(db, step, states.STEP, "IN_PROGRESS", user, entity="step", batch_id=step.batch_id,
                              reason=f"{dev.number} closed")
            for b in step.blocks:
                if b.block_type == "MATERIAL_DISPENSE":
                    engine.maybe_complete_dispense(db, step, b, user)
            engine.after_block(db, step, user)
    return dev


def serialize(d: Deviation) -> dict:
    return {
        "id": d.id, "number": d.number, "batch_id": d.batch_id, "step_execution_id": d.step_execution_id,
        "step_code": d.step.code if d.step else None, "related_entry_id": d.related_entry_id,
        "related_sample_id": d.related_sample_id, "source": d.source, "category": d.category,
        "description": d.description, "status": d.status,
        "raised_by": d.raised_by.full_name, "raised_at": clock.iso(d.raised_at),
        "closed_by": d.closed_by.full_name if d.closed_by else None, "closed_at": clock.iso(d.closed_at),
        "justification": d.justification,
    }
