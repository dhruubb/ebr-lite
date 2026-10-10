"""Batch execution engine.

The engine knows a few generic BUILDING BLOCK types and nothing about any product:

    CHECKLIST · MATERIAL_DISPENSE · EQUIPMENT_CHECK · PARAMETER_ENTRY · IPC_SAMPLING · YIELD_CALC · SIGNATURE

A recipe step is an ordered list of these blocks (see seed/recipe_*.json). Adding a product means
adding a JSON file, not changing this module (URS-014).

Rules enforced here are tagged with the URS they implement.
"""
import copy
from datetime import timedelta

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clock, config
from ..errors import Blocked, Forbidden, Invalid, NotFound
from ..models import Batch, BlockExecution, DataEntry, Equipment, IpcSample, Material, MaterialLot, MasterRecipe, StepExecution, User
from . import audit, deviations, signatures, states

EXEC_ROLES = ["OPERATOR", "SUPERVISOR"]


# =========================================================================== helpers
def require_role(user: User, roles: list[str], what: str):
    # URS-009: users may only perform actions permitted by their role.
    if user.role not in roles:
        raise Forbidden(f"Role {user.role} is not allowed to {what}.")


def _block(db: Session, user: User, action: str, entity: str, entity_id, message: str, batch_id: int):
    """Record a GMP block in the audit trail (committed), then stop the request."""
    audit.log(db, user, action, entity, entity_id, reason=message, batch_id=batch_id)
    db.commit()
    raise Blocked(message)


def _parse(model: type[BaseModel], data: dict):
    try:
        return model(**data)
    except ValidationError as e:
        raise Invalid("; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()))


def _num(v: float) -> str:
    return ("%.6f" % v).rstrip("0").rstrip(".")


def limits_text(p: dict) -> str | None:
    """Readable limits for a parameter / check definition."""
    t = p.get("type", "number")
    if t == "boolean":
        return None if p.get("expected") is None else ("Yes" if p["expected"] else "No")
    if t == "text":
        return None if p.get("expected") is None else f"= {p['expected']}"
    lo, hi = p.get("min"), p.get("max")
    if lo is not None and hi is not None:
        return f"= {_num(lo)}" if lo == hi else f"{_num(lo)} – {_num(hi)}"
    if hi is not None:
        return f"≤ {_num(hi)}"
    if lo is not None:
        return f"≥ {_num(lo)}"
    return None


def evaluate(p: dict, raw) -> tuple[str, bool]:
    """Check one entered value against its definition. Returns (stored text, within limits)."""
    t = p.get("type", "number")
    label = p.get("label", p["key"])
    if t == "number":
        if isinstance(raw, bool) or raw is None or raw == "":
            raise Invalid(f"{label}: a numeric value is required")
        try:
            v = round(float(raw), 6)
        except (TypeError, ValueError):
            raise Invalid(f"{label}: '{raw}' is not a number")
        ok = (p.get("min") is None or v >= p["min"]) and (p.get("max") is None or v <= p["max"])
        return _num(v), ok
    if t == "boolean":
        if not isinstance(raw, bool):
            raise Invalid(f"{label}: answer Yes (true) or No (false)")
        return ("Yes" if raw else "No"), (p.get("expected") is None or raw == p["expected"])
    s = str(raw if raw is not None else "").strip()
    if not s:
        raise Invalid(f"{label}: a value is required")
    return s, (p.get("expected") is None or s == p["expected"])


def _entry(db, block: BlockExecution, user: User, *, key, label, value, unit=None, limits=None,
           in_limits=True, accepted=True, context=None) -> DataEntry:
    e = DataEntry(key=key, label=label, value=str(value), unit=unit, limits=limits, in_limits=in_limits,
                  accepted=accepted, context=context or {}, entered_by_id=user.id, entered_at=clock.utcnow())
    block.entries.append(e)
    db.flush()
    flag = "" if in_limits else "  [OUT OF LIMIT]"
    audit.log(db, user, "DATA_ENTERED", "data_entry", e.id, field=key,
              new=f"{value}{(' ' + unit) if unit else ''}{flag}", batch_id=block.step.batch_id)
    return e


def _current_block(step: StepExecution) -> BlockExecution | None:
    return next((b for b in step.blocks if b.status == "PENDING"), None)


def _complete_block(db, step: StepExecution, block: BlockExecution, user: User):
    block.status = "COMPLETED"
    block.completed_at = clock.utcnow()
    block.completed_by_id = user.id
    audit.log(db, user, "BLOCK_COMPLETED", "block", block.id, new=f"{step.code} block {block.idx + 1} {block.block_type}",
              batch_id=step.batch_id)
    after_block(db, step, user)


def after_block(db, step: StepExecution, user: User):
    """When every block of a step is done (and nothing is on hold) the step completes,
    the next step unlocks, and after the last step the batch completes. URS-001."""
    if step.status != "IN_PROGRESS" or any(b.status != "COMPLETED" for b in step.blocks):
        return
    batch = step.batch
    states.transition(db, step, states.STEP, "COMPLETED", user, entity="step", batch_id=batch.id)
    step.completed_at = clock.utcnow()
    nxt = next((s for s in batch.steps if s.seq == step.seq + 1), None)
    if nxt:
        states.transition(db, nxt, states.STEP, "READY", user, entity="step", batch_id=batch.id,
                          reason=f"previous step {step.code} completed")
    else:
        states.transition(db, batch, states.BATCH, "COMPLETED", user, entity="batch", batch_id=batch.id,
                          reason="all steps completed")
        batch.completed_at = clock.utcnow()


def _check_equipment(db, user: User, step: StepExecution, code: str, allowed: list[str], purpose: str) -> Equipment:
    if code not in allowed:
        _block(db, user, "EQUIPMENT_BLOCKED", "equipment", code,
               f"{code} is not allowed as {purpose} in step {step.code} (allowed: {', '.join(allowed)}).", step.batch_id)
    eq = db.get(Equipment, code)
    if eq is None:
        raise Invalid(f"Equipment {code} not found.")
    # URS-004: only clean equipment may be used.
    if eq.status != "CLEAN":
        _block(db, user, "EQUIPMENT_BLOCKED", "equipment", code,
               f"{code} status is {eq.status}. Only CLEAN equipment may be used.", step.batch_id)
    # URS-004: no use of equipment whose calibration is overdue.
    if eq.calibration_due_date and eq.calibration_due_date < clock.plant_today():
        _block(db, user, "EQUIPMENT_BLOCKED", "equipment", code,
               f"{code} calibration overdue (due {eq.calibration_due_date.isoformat()}). Use a calibrated instrument.",
               step.batch_id)
    return eq


# =========================================================================== batch & step lifecycle
def create_batch(db: Session, recipe_id: int, user: User) -> Batch:
    require_role(user, ["SUPERVISOR"], "create a batch")
    recipe = db.get(MasterRecipe, recipe_id)
    if recipe is None:
        raise NotFound(f"Recipe {recipe_id} not found.")
    if recipe.status != "APPROVED":
        raise Blocked("Only APPROVED master recipes can be used to create a batch.")

    now = clock.utcnow()
    prefix = f"{recipe.batch_prefix}{now:%y}"
    n = db.scalar(select(func.count()).select_from(Batch).where(Batch.batch_no.like(f"{prefix}%"))) or 0
    batch = Batch(batch_no=f"{prefix}{n + 1:04d}", recipe_id=recipe.id, status="CREATED",
                  created_by_id=user.id, created_at=now)
    db.add(batch)
    db.flush()
    audit.log(db, user, "BATCH_CREATED", "batch", batch.id, new=f"{batch.batch_no} from {recipe.code} v{recipe.version}",
              batch_id=batch.id)

    for rs in recipe.steps:
        se = StepExecution(recipe_step_id=rs.id, seq=rs.seq, code=rs.code, name=rs.name, status="LOCKED")
        batch.steps.append(se)
        db.flush()
        for i, b in enumerate(rs.blocks):
            # Snapshot the configuration: later recipe edits never change a running batch.
            se.blocks.append(BlockExecution(idx=i, block_type=b["type"], config=copy.deepcopy(b["config"]), state={}))
    db.flush()
    states.transition(db, batch.steps[0], states.STEP, "READY", user, entity="step", batch_id=batch.id,
                      reason="first step of new batch")
    return batch


def start_step(db: Session, step: StepExecution, user: User):
    require_role(user, EXEC_ROLES, "execute batch steps")
    batch = step.batch
    if batch.status not in ("CREATED", "IN_PROGRESS"):
        raise Blocked(f"Batch {batch.batch_no} is {batch.status}; no further execution allowed.")
    if step.status != "READY":
        # URS-001: steps run strictly in the sequence of the master recipe.
        _block(db, user, "SEQUENCE_BLOCKED", "step", step.id,
               f"Step {step.code} is {step.status}. Steps must be executed in recipe sequence.", batch.id)
    if batch.status == "CREATED":
        states.transition(db, batch, states.BATCH, "IN_PROGRESS", user, entity="batch", batch_id=batch.id)
        batch.started_at = clock.utcnow()
    states.transition(db, step, states.STEP, "IN_PROGRESS", user, entity="step", batch_id=batch.id)
    step.started_at = clock.utcnow()
    step.started_by_id = user.id


def block_action(db: Session, step: StepExecution, idx: int, action: str, data: dict, user: User) -> dict:
    """Single entry point for every block action. Looks up the handler for (block type, action)."""
    block = next((b for b in step.blocks if b.idx == idx), None)
    if block is None:
        raise NotFound(f"Step {step.code} has no block {idx}.")
    handler = HANDLERS.get((block.block_type, action))
    if handler is None:
        valid = [a for (t, a) in HANDLERS if t == block.block_type]
        raise Invalid(f"Action '{action}' is not valid for {block.block_type} (valid: {', '.join(valid)}).")

    batch = step.batch
    if batch.status != "IN_PROGRESS":
        raise Blocked(f"Batch {batch.batch_no} is {batch.status}.")
    if step.status == "ON_HOLD":
        raise Blocked(f"Step {step.code} is ON_HOLD. Open deviation(s) must be closed by a supervisor first.")
    if step.status != "IN_PROGRESS":
        _block(db, user, "SEQUENCE_BLOCKED", "step", step.id,
               f"Step {step.code} is {step.status}. Start it first (steps run in sequence).", batch.id)
    if block.status == "COMPLETED":
        raise Blocked(f"Block {idx + 1} of step {step.code} is already completed.")
    current = _current_block(step)
    if current is not None and current.idx != block.idx:
        # URS-001 also applies inside a step: e.g. equipment must be checked before process data.
        _block(db, user, "SEQUENCE_BLOCKED", "block", block.id,
               f"Complete block {current.idx + 1} ({current.block_type}) of step {step.code} first.", batch.id)
    if block.block_type == "SIGNATURE":
        require_role(user, EXEC_ROLES, "operate a signature block")   # the signer's own role is checked in sign()
    else:
        require_role(user, block.config.get("roles", EXEC_ROLES), f"perform {block.block_type} in step {step.code}")

    result = handler(db, step, block, data or {}, user)
    result.setdefault("step_status", step.status)
    result.setdefault("block_status", block.status)
    return result


# =========================================================================== block handlers
def checklist_submit(db, step, block, data, user):
    answers = data.get("answers")
    items = block.config["items"]
    if not isinstance(answers, dict):
        raise Invalid("'answers' must be an object of item key -> true/false.")
    missing = [i["key"] for i in items if i["key"] not in answers]
    if missing:
        raise Invalid(f"Every checklist item must be answered. Missing: {', '.join(missing)}")
    if any(not isinstance(answers[i["key"]], bool) for i in items):
        raise Invalid("Checklist answers must be true (Yes) or false (No).")

    failed = []
    for item in items:
        yes = answers[item["key"]]
        e = _entry(db, block, user, key=item["key"], label=item["text"], value="Yes" if yes else "No",
                   limits="Yes", in_limits=yes)
        if not yes:
            failed.append((item, e))
    _complete_block(db, step, block, user)
    for item, e in failed:
        deviations.open_deviation(db, step, user, "CHECKLIST",
                                  f"{step.code} {step.name}: checklist item answered No – {item['text']}", entry=e)
    return {"message": "Checklist recorded." + (f" {len(failed)} item(s) answered No: deviation raised, step ON_HOLD." if failed else "")}


class DispenseIn(BaseModel):
    material_code: str
    lot_no: str
    balance_code: str
    net_qty: float = Field(gt=0)


def dispense(db, step, block, data, user):
    p = _parse(DispenseIn, data)
    cfg = block.config
    items = {i["material_code"]: i for i in cfg["items"]}
    item = items.get(p.material_code)
    if item is None:
        raise Invalid(f"{p.material_code} is not part of this dispensing step.")
    if any(e.accepted and e.key == p.material_code for e in block.entries):
        raise Blocked(f"{p.material_code} has already been dispensed for this batch.")

    lot = db.scalar(select(MaterialLot).where(MaterialLot.lot_no == p.lot_no))
    if lot is None:
        raise Invalid(f"Lot {p.lot_no} not found.")
    if lot.material_code != p.material_code:
        _block(db, user, "DISPENSE_BLOCKED", "material_lot", lot.lot_no,
               f"Lot {lot.lot_no} is {lot.material_code}, not {p.material_code}. Wrong material.", step.batch_id)
    # URS-002: only RELEASED lots may be dispensed.
    if lot.status != "RELEASED":
        _block(db, user, "DISPENSE_BLOCKED", "material_lot", lot.lot_no,
               f"Lot {lot.lot_no} status is {lot.status}. Only RELEASED lots may be dispensed.", step.batch_id)
    # URS-003: expired lots may not be dispensed.
    if lot.expiry_date < clock.plant_today():
        _block(db, user, "DISPENSE_BLOCKED", "material_lot", lot.lot_no,
               f"Lot {lot.lot_no} expired on {lot.expiry_date.isoformat()}.", step.batch_id)
    _check_equipment(db, user, step, p.balance_code, cfg["allowed_balances"], "balance")

    # URS-005: weight must be within tolerance (API ±0.5 %, excipients ±1.0 % in the seed recipe).
    target, tol = float(item["target_qty"]), float(item["tolerance_pct"])
    low, high = round(target * (1 - tol / 100), 4), round(target * (1 + tol / 100), 4)
    qty = round(p.net_qty, 4)
    ok = low <= qty <= high
    material = db.get(Material, p.material_code)
    e = _entry(db, block, user, key=p.material_code, label=f"{material.name if material else p.material_code}",
               value=f"{qty:.3f}", unit=item.get("uom", "kg"), limits=f"{low:.3f} – {high:.3f}",
               in_limits=ok, accepted=ok,
               context={"lot_no": lot.lot_no, "balance": p.balance_code, "target_qty": target, "tolerance_pct": tol})
    if ok:
        maybe_complete_dispense(db, step, block, user)
        msg = f"{p.material_code} dispensed: {qty:.3f} {item.get('uom', 'kg')} from lot {lot.lot_no}."
    else:
        msg = (f"Weight {qty:.3f} is outside tolerance {low:.3f} – {high:.3f}. "
               f"Re-weigh, or raise a deviation against entry #{e.id}.")
    return {"message": msg, "entry_id": e.id, "in_limits": ok}


def maybe_complete_dispense(db, step, block, user):
    done = {e.key for e in block.entries if e.accepted}
    if block.status == "PENDING" and all(i["material_code"] in done for i in block.config["items"]):
        _complete_block(db, step, block, user)


class BindIn(BaseModel):
    slot: str
    equipment_code: str


def equipment_bind(db, step, block, data, user):
    p = _parse(BindIn, data)
    slot = next((s for s in block.config["slots"] if s["slot"] == p.slot), None)
    if slot is None:
        raise Invalid(f"Unknown equipment slot '{p.slot}'.")
    if any(e.key == p.slot for e in block.entries):
        raise Blocked(f"Equipment for '{p.slot}' is already recorded.")
    eq = _check_equipment(db, user, step, p.equipment_code, slot["allowed"], p.slot)
    _entry(db, block, user, key=p.slot, label=f"Equipment used ({p.slot})", value=f"{eq.code} – {eq.name}",
           limits="CLEAN, calibration valid", context={"equipment_code": eq.code})
    if all(any(e.key == s["slot"] for e in block.entries) for s in block.config["slots"]):
        _complete_block(db, step, block, user)
    return {"message": f"{eq.code} checked: CLEAN" + (", calibration valid." if eq.calibration_due_date else ".")}


def parameters_submit(db, step, block, data, user):
    values = data.get("values")
    params = block.config["parameters"]
    if not isinstance(values, dict):
        raise Invalid("'values' must be an object of parameter key -> value.")
    missing = [p["key"] for p in params if p["key"] not in values]
    if missing:
        raise Invalid(f"All parameters are required. Missing: {', '.join(missing)}")
    evaluated = [(p, *evaluate(p, values[p["key"]])) for p in params]   # validate everything before writing

    out = []
    for p, text, ok in evaluated:
        e = _entry(db, block, user, key=p["key"], label=p.get("label", p["key"]), value=text, unit=p.get("unit"),
                   limits=limits_text(p), in_limits=ok)
        if not ok:
            out.append((p, e))
    _complete_block(db, step, block, user)
    # URS-005: an out-of-limit value creates a deviation and puts the step ON_HOLD.
    for p, e in out:
        deviations.open_deviation(
            db, step, user, "OUT_OF_LIMIT",
            f"{step.code} {step.name}: {e.label} = {e.value}{(' ' + e.unit) if e.unit else ''} "
            f"outside limits {e.limits}{(' ' + e.unit) if e.unit else ''}", entry=e)
    return {"message": "Parameters recorded." + (f" {len(out)} value(s) out of limit: deviation raised, step ON_HOLD." if out else ""),
            "out_of_limit": [p["key"] for p, _ in out]}


def _record_missed_ipc(db, step, block, user) -> int:
    """URS-013: flag every IPC interval that passed without a sample."""
    taken = [s for s in block.samples if s.status == "TAKEN"]
    if not taken:
        return 0
    interval = timedelta(minutes=block.config["interval_min"])
    grace = timedelta(minutes=config.IPC_GRACE_MINUTES)
    already = {s.due_at for s in block.samples if s.status == "MISSED"}
    now = clock.utcnow()
    due = taken[-1].taken_at + interval
    missed = []
    while now > due + grace:
        if due not in already:
            missed.append(due)
        due += interval
    first = None
    for d in missed:
        s = IpcSample(sample_no=len(block.samples) + 1, status="MISSED", due_at=d, in_limits=False, results=[])
        block.samples.append(s)
        db.flush()
        first = first or s
        audit.log(db, user, "IPC_MISSED", "ipc_sample", s.id, new=f"sample #{s.sample_no} due {clock.iso(d)} not taken",
                  batch_id=step.batch_id)
    if missed:
        deviations.open_deviation(
            db, step, user, "MISSED_IPC",
            f"{step.code} {step.name}: {len(missed)} IPC sample(s) missed (interval {block.config['interval_min']} min; "
            f"due {', '.join(clock.ist(d) for d in missed)}).", sample=first)
    return len(missed)


def ipc_sample(db, step, block, data, user):
    values = data.get("values")
    checks = block.config["checks"]
    if not isinstance(values, dict):
        raise Invalid("'values' must be an object of check key -> number.")
    missing = [c["key"] for c in checks if c["key"] not in values]
    if missing:
        raise Invalid(f"All IPC checks are required. Missing: {', '.join(missing)}")
    results = []
    for c in checks:
        text, ok = evaluate({**c, "type": "number"}, values[c["key"]])
        results.append({"key": c["key"], "label": c.get("label", c["key"]), "value": text, "unit": c.get("unit"),
                        "limits": limits_text(c), "in_limits": ok})

    missed = _record_missed_ipc(db, step, block, user)
    taken = [s for s in block.samples if s.status == "TAKEN"]
    due = taken[-1].taken_at + timedelta(minutes=block.config["interval_min"]) if taken else None
    all_ok = all(r["in_limits"] for r in results)
    s = IpcSample(sample_no=len(block.samples) + 1, status="TAKEN", due_at=due, taken_at=clock.utcnow(),
                  taken_by_id=user.id, results=results, in_limits=all_ok)
    block.samples.append(s)
    db.flush()
    audit.log(db, user, "IPC_SAMPLE_TAKEN", "ipc_sample", s.id,
              new="; ".join(f"{r['key']}={r['value']}{'' if r['in_limits'] else ' [OOL]'}" for r in results),
              batch_id=step.batch_id)
    if not all_ok:
        bad = ", ".join(f"{r['label']} {r['value']} {r['unit'] or ''} (limit {r['limits']})" for r in results if not r["in_limits"])
        deviations.open_deviation(db, step, user, "OUT_OF_LIMIT",
                                  f"{step.code} {step.name}: IPC sample #{s.sample_no} out of limit – {bad}", sample=s)
    msg = f"IPC sample #{s.sample_no} recorded" + ("" if all_ok else " – OUT OF LIMIT, deviation raised, step ON_HOLD")
    if missed:
        msg += f". {missed} missed interval(s) flagged."
    return {"message": msg + ".", "sample_id": s.id, "in_limits": all_ok, "missed_intervals": missed}


def ipc_complete(db, step, block, data, user):
    n = sum(1 for s in block.samples if s.status == "TAKEN")
    need = block.config["min_samples"]
    if n < need:
        raise Blocked(f"At least {need} IPC samples are required before ending sampling ({n} taken).")
    missed = _record_missed_ipc(db, step, block, user)
    _complete_block(db, step, block, user)
    return {"message": f"IPC sampling ended with {n} samples." + (f" {missed} missed interval(s) flagged." if missed else "")}


class YieldIn(BaseModel):
    good_output: float = Field(ge=0)
    rejects: float = Field(default=0, ge=0)
    samples: float = Field(default=0, ge=0)
    waste: float = Field(default=0, ge=0)


def yield_submit(db, step, block, data, user):
    p = _parse(YieldIn, data)
    cfg = block.config
    theo, uom = float(cfg["theoretical_qty"]), cfg["uom"]
    # URS-010 / 21 CFR 211.103: actual vs theoretical yield, flagged outside limits.
    yield_pct = round(p.good_output / theo * 100, 2)
    recon_pct = round((p.good_output + p.rejects + p.samples + p.waste) / theo * 100, 2)
    ok = cfg["min_pct"] <= yield_pct <= cfg["max_pct"]
    for key, label, val in (("good_output", "Good output", p.good_output), ("rejects", "Rejects", p.rejects),
                            ("samples", "QC / retention samples", p.samples), ("waste", "Waste", p.waste)):
        _entry(db, block, user, key=key, label=label, value=_num(val), unit=uom)
    calc = {"computed": True}   # calculated by the system, not entered by a person
    _entry(db, block, user, key="theoretical", label="Theoretical quantity", value=_num(theo), unit=uom, context=calc)
    e = _entry(db, block, user, key="yield_pct", label="Yield (good output / theoretical)", value=_num(yield_pct),
               unit="%", limits=f"{_num(cfg['min_pct'])} – {_num(cfg['max_pct'])}", in_limits=ok, context=calc)
    _entry(db, block, user, key="reconciliation_pct", label="Reconciliation (all accounted / theoretical)",
           value=_num(recon_pct), unit="%", context=calc)
    _complete_block(db, step, block, user)
    if not ok:
        deviations.open_deviation(db, step, user, "YIELD",
                                  f"{step.code} {step.name}: yield {_num(yield_pct)} % outside {e.limits} %", entry=e)
    return {"message": f"Yield {_num(yield_pct)} %, reconciliation {_num(recon_pct)} %." + ("" if ok else " Out of limit: deviation raised."),
            "yield_pct": yield_pct, "reconciliation_pct": recon_pct, "in_limits": ok}


class SignIn(BaseModel):
    username: str
    password: str
    meaning: str


def sign_block(db, step, block, data, user):
    p = _parse(SignIn, data)
    cfg, st = block.config, dict(block.state or {})
    if "first_sig" not in st:
        if p.meaning != cfg["meaning"]:
            raise Invalid(f"This signature must have the meaning {cfg['meaning']}.")
        # A PERFORMED signature can only be given by the logged-in user (no signing for someone else).
        sig = signatures.sign(db, user, username=p.username, password=p.password, meaning=p.meaning,
                              entity="block", entity_id=block.id, batch_id=step.batch_id,
                              allowed_roles=cfg.get("roles", EXEC_ROLES),
                              must_be_session_user=(cfg["meaning"] == "PERFORMED"))
        st.update(first_sig=sig.id, signer_id=sig.user_id)
        block.state = st
        if not cfg.get("verifier_required"):
            _complete_block(db, step, block, user)
            return {"message": f"Signed as {p.meaning} by {sig.full_name}.", "signature_id": sig.id}
        return {"message": f"Signed as {p.meaning} by {sig.full_name}. Awaiting VERIFIED signature by a second person.",
                "signature_id": sig.id, "awaiting": "VERIFIED"}

    # URS-006: 4-eyes. Second signature must be VERIFIED and from a different person.
    if p.meaning != "VERIFIED":
        raise Invalid("The second signature on this step must have the meaning VERIFIED.")
    sig = signatures.sign(db, user, username=p.username, password=p.password, meaning="VERIFIED",
                          entity="block", entity_id=block.id, batch_id=step.batch_id,
                          allowed_roles=cfg.get("verifier_roles", EXEC_ROLES), not_user_id=st["signer_id"])
    st["verify_sig"] = sig.id
    block.state = st
    _complete_block(db, step, block, user)
    return {"message": f"Verified by {sig.full_name}.", "signature_id": sig.id}


HANDLERS = {
    ("CHECKLIST", "submit"): checklist_submit,
    ("MATERIAL_DISPENSE", "dispense"): dispense,
    ("EQUIPMENT_CHECK", "bind"): equipment_bind,
    ("PARAMETER_ENTRY", "submit"): parameters_submit,
    ("IPC_SAMPLING", "sample"): ipc_sample,
    ("IPC_SAMPLING", "complete"): ipc_complete,
    ("YIELD_CALC", "submit"): yield_submit,
    ("SIGNATURE", "sign"): sign_block,
}


# =========================================================================== corrections
def correct_entry(db: Session, entry_id: int, user: User, value, reason: str | None) -> DataEntry:
    """URS-016: a correction needs a reason; the audit trail keeps old -> new. Nothing is overwritten silently."""
    require_role(user, EXEC_ROLES, "correct batch data")
    e = db.get(DataEntry, entry_id)
    if e is None:
        raise NotFound(f"Data entry {entry_id} not found.")
    block, step = e.block, e.block.step
    if block.block_type != "PARAMETER_ENTRY":
        raise Blocked("Only process parameters can be corrected. For weights re-weigh; otherwise raise a deviation.")
    if step.batch.status != "IN_PROGRESS" or step.status not in ("IN_PROGRESS", "ON_HOLD"):
        raise Blocked(f"Step {step.code} is {step.status}; its records can no longer be corrected.")
    if not reason or not reason.strip():
        raise Invalid("A reason is required for every correction.")
    param = next(p for p in block.config["parameters"] if p["key"] == e.key)
    new_text, ok = evaluate(param, value)
    if new_text == e.value:
        raise Invalid("New value is the same as the current value.")

    old, was_ok = e.value, e.in_limits
    if not e.corrected:
        e.original_value = old
    e.value, e.in_limits, e.corrected = new_text, ok, True
    audit.log(db, user, "DATA_CORRECTED", "data_entry", e.id, field=e.key, old=old, new=new_text,
              reason=reason.strip(), batch_id=step.batch_id)
    if was_ok and not ok:
        deviations.open_deviation(db, step, user, "OUT_OF_LIMIT",
                                  f"{step.code} {step.name}: corrected value {e.label} = {new_text} outside limits {e.limits}",
                                  entry=e)
    return e
