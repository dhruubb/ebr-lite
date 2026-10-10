"""State machines. Every status change goes through transition(), which also audits it.

Batch: CREATED -> IN_PROGRESS -> COMPLETED -> UNDER_REVIEW -> APPROVED | REJECTED
Step : LOCKED -> READY -> IN_PROGRESS -> (ON_HOLD <-> IN_PROGRESS) -> COMPLETED
"""
from ..errors import Blocked
from . import audit

BATCH = {
    "CREATED": {"IN_PROGRESS"},
    "IN_PROGRESS": {"COMPLETED"},
    "COMPLETED": {"UNDER_REVIEW"},
    "UNDER_REVIEW": {"APPROVED", "REJECTED"},
}

STEP = {
    "LOCKED": {"READY"},
    "READY": {"IN_PROGRESS"},
    "IN_PROGRESS": {"ON_HOLD", "COMPLETED"},
    "ON_HOLD": {"IN_PROGRESS"},
}


def transition(db, obj, machine: dict, new: str, user, *, entity: str, batch_id: int, reason: str | None = None):
    old = obj.status
    if new not in machine.get(old, set()):
        raise Blocked(f"{entity} cannot change from {old} to {new}.")
    obj.status = new
    audit.log(db, user, "STATUS_CHANGE", entity, obj.id, field="status", old=old, new=new,
              reason=reason, batch_id=batch_id)
