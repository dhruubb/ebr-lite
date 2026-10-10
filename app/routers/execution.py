"""Batch execution: create batch, start steps, act on blocks, correct data, deviations."""
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user, require_roles
from ..errors import NotFound
from ..models import Batch, Deviation, StepExecution, User
from ..services import deviations, engine, records

router = APIRouter(prefix="/api", tags=["execution"])


def _batch(db: Session, batch_id: int) -> Batch:
    b = db.get(Batch, batch_id)
    if b is None:
        raise NotFound(f"Batch {batch_id} not found.")
    return b


def _step(db: Session, step_id: int) -> StepExecution:
    s = db.get(StepExecution, step_id)
    if s is None:
        raise NotFound(f"Step {step_id} not found.")
    return s


class BatchIn(BaseModel):
    recipe_id: int


@router.get("/batches")
def list_batches(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [records.batch_summary(db, b) for b in db.scalars(select(Batch).order_by(Batch.id.desc()))]


@router.post("/batches", status_code=201)
def create_batch(body: BatchIn, db: Session = Depends(get_db), user: User = Depends(require_roles("SUPERVISOR"))):
    b = engine.create_batch(db, body.recipe_id, user)
    db.commit()
    return records.batch_detail(db, b)


@router.get("/batches/{batch_id}")
def get_batch(batch_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return records.batch_detail(db, _batch(db, batch_id))


@router.post("/steps/{step_id}/start")
def start_step(step_id: int, db: Session = Depends(get_db), user: User = Depends(require_roles("OPERATOR", "SUPERVISOR"))):
    s = _step(db, step_id)
    engine.start_step(db, s, user)
    db.commit()
    return {"message": f"Step {s.code} started.", "step_status": s.status}


class BlockActionIn(BaseModel):
    action: str
    data: dict[str, Any] = {}


@router.post("/steps/{step_id}/blocks/{idx}")
def block_action(step_id: int, idx: int, body: BlockActionIn, db: Session = Depends(get_db),
                 user: User = Depends(require_roles("OPERATOR", "SUPERVISOR"))):
    """One endpoint for every building block. Examples of body:
    CHECKLIST          {"action":"submit","data":{"answers":{"area_clean":true}}}
    MATERIAL_DISPENSE  {"action":"dispense","data":{"material_code":"MAT-PCM","lot_no":"PCM-2601","balance_code":"BAL-01","net_qty":50.01}}
    EQUIPMENT_CHECK    {"action":"bind","data":{"slot":"dryer","equipment_code":"FBD-01"}}
    PARAMETER_ENTRY    {"action":"submit","data":{"values":{"inlet_temp_c":60,"lod_pct":1.8}}}
    IPC_SAMPLING       {"action":"sample","data":{"values":{...}}}  /  {"action":"complete"}
    YIELD_CALC         {"action":"submit","data":{"good_output":98500,"rejects":800,"samples":300,"waste":200}}
    SIGNATURE          {"action":"sign","data":{"username":"op1","password":"...","meaning":"PERFORMED"}}
    """
    s = _step(db, step_id)
    result = engine.block_action(db, s, idx, body.action, body.data, user)
    db.commit()
    return result


class CorrectionIn(BaseModel):
    value: Any
    reason: str | None = None


@router.post("/entries/{entry_id}/correct")
def correct(entry_id: int, body: CorrectionIn, db: Session = Depends(get_db),
            user: User = Depends(require_roles("OPERATOR", "SUPERVISOR"))):
    e = engine.correct_entry(db, entry_id, user, body.value, body.reason)
    db.commit()
    return {"message": "Correction recorded.", "entry_id": e.id, "value": e.value, "in_limits": e.in_limits}


# ----------------------------------------------------------------------- deviations
class DeviationIn(BaseModel):
    description: str
    step_execution_id: int | None = None     # shop-floor deviation on a step (operator / supervisor)
    batch_id: int | None = None              # or batch-level deviation (QA during review)
    related_entry_id: int | None = None


class CloseIn(BaseModel):
    justification: str
    username: str
    password: str


@router.get("/deviations")
def list_deviations(batch_id: int | None = None, status: str | None = None, db: Session = Depends(get_db),
                    _: User = Depends(current_user)):
    q = select(Deviation).order_by(Deviation.id.desc())
    if batch_id:
        q = q.where(Deviation.batch_id == batch_id)
    if status:
        q = q.where(Deviation.status == status)
    return [deviations.serialize(d) for d in db.scalars(q)]


@router.post("/deviations", status_code=201)
def raise_deviation(body: DeviationIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles("OPERATOR", "SUPERVISOR", "QA"))):
    d = deviations.raise_manual(db, user, body.description, step_id=body.step_execution_id,
                               batch_id=body.batch_id, related_entry_id=body.related_entry_id)
    db.commit()
    return deviations.serialize(d)


@router.post("/deviations/{dev_id}/close")
def close_deviation(dev_id: int, body: CloseIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles("SUPERVISOR"))):
    d = deviations.close(db, dev_id, user, body.justification, body.username, body.password)
    db.commit()
    return deviations.serialize(d)
