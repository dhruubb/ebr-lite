"""Master recipes as data (URS-014: configurable without code changes).

A recipe is a JSON document: steps, and inside each step an ordered list of building blocks.
validate() checks the structure and that every material / equipment code exists, so a typo in
configuration is caught at import time, not on the shop floor.
"""
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..errors import Blocked, Invalid
from ..models import Equipment, Material, MasterRecipe, RecipeStep, User
from . import audit

# block type -> config keys it must have
BLOCK_TYPES: dict[str, list[str]] = {
    "CHECKLIST": ["items"],
    "MATERIAL_DISPENSE": ["items", "allowed_balances"],
    "EQUIPMENT_CHECK": ["slots"],
    "PARAMETER_ENTRY": ["parameters"],
    "IPC_SAMPLING": ["interval_min", "min_samples", "checks"],
    "YIELD_CALC": ["theoretical_qty", "uom", "min_pct", "max_pct"],
    "SIGNATURE": ["meaning"],
}
PARAM_TYPES = ("number", "boolean", "text")
ROLES = ("OPERATOR", "SUPERVISOR", "QA", "ADMIN")


def validate(d: dict, db: Session | None = None) -> list[str]:
    """Return a list of human-readable problems. Empty list = valid."""
    errs: list[str] = []
    for k in ("code", "version", "name", "product", "batch_size", "batch_size_uom", "steps"):
        if k not in d:
            errs.append(f"missing top-level key '{k}'")
    steps = d.get("steps") or []
    if not steps:
        errs.append("recipe has no steps")

    materials = equipment = None
    if db is not None:
        materials = set(db.scalars(select(Material.code)))
        equipment = set(db.scalars(select(Equipment.code)))

    seen_codes = set()
    for si, step in enumerate(steps):
        where = f"step[{si}] {step.get('code', '?')}"
        if not step.get("code") or not step.get("name"):
            errs.append(f"{where}: needs 'code' and 'name'")
        if step.get("code") in seen_codes:
            errs.append(f"{where}: duplicate step code")
        seen_codes.add(step.get("code"))
        blocks = step.get("blocks") or []
        if not blocks:
            errs.append(f"{where}: has no blocks")
            continue
        # 21 CFR 211.188(b)(11): every step is signed. The last block must be a SIGNATURE.
        if blocks[-1].get("type") != "SIGNATURE":
            errs.append(f"{where}: last block must be SIGNATURE")
        for bi, block in enumerate(blocks):
            bwhere = f"{where} block[{bi}]"
            btype, cfg = block.get("type"), block.get("config") or {}
            if btype not in BLOCK_TYPES:
                errs.append(f"{bwhere}: unknown block type '{btype}'")
                continue
            for req in BLOCK_TYPES[btype]:
                if req not in cfg:
                    errs.append(f"{bwhere} ({btype}): config missing '{req}'")
            for r in cfg.get("roles", []) + cfg.get("verifier_roles", []):
                if r not in ROLES:
                    errs.append(f"{bwhere}: unknown role '{r}'")
            if btype == "PARAMETER_ENTRY":
                for p in cfg.get("parameters", []):
                    if not p.get("key") or p.get("type") not in PARAM_TYPES:
                        errs.append(f"{bwhere}: parameter needs 'key' and a type in {PARAM_TYPES}")
            if btype == "MATERIAL_DISPENSE" and materials is not None:
                for it in cfg.get("items", []):
                    if it.get("material_code") not in materials:
                        errs.append(f"{bwhere}: unknown material '{it.get('material_code')}'")
            if btype == "MATERIAL_DISPENSE" and equipment is not None:
                for b in cfg.get("allowed_balances", []):
                    if b not in equipment:
                        errs.append(f"{bwhere}: unknown balance '{b}'")
            if btype == "EQUIPMENT_CHECK" and equipment is not None:
                for slot in cfg.get("slots", []):
                    for code in slot.get("allowed", []):
                        if code not in equipment:
                            errs.append(f"{bwhere}: unknown equipment '{code}'")
            if btype == "SIGNATURE" and cfg.get("meaning") not in ("PERFORMED", "APPROVED", "REVIEWED"):
                errs.append(f"{bwhere}: SIGNATURE meaning must be PERFORMED, APPROVED or REVIEWED")
    return errs


def import_recipe(db: Session, d: dict, user: User | None) -> MasterRecipe:
    errs = validate(d, db)
    if errs:
        raise Invalid("Recipe is not valid: " + "; ".join(errs))
    exists = db.scalar(select(MasterRecipe).where(MasterRecipe.code == d["code"], MasterRecipe.version == d["version"]))
    if exists:
        raise Blocked(f"Recipe {d['code']} v{d['version']} already exists. Increase the version.")

    # Note: a real MES would route a new recipe through review/approval. Here imports are APPROVED directly.
    recipe = MasterRecipe(code=d["code"], version=int(d["version"]), name=d["name"], product=d["product"],
                          status="APPROVED", batch_size=float(d["batch_size"]), batch_size_uom=d["batch_size_uom"],
                          batch_prefix=d.get("batch_prefix", d["code"][:3]), definition=d, created_at=clock.utcnow())
    db.add(recipe)
    db.flush()
    for seq, s in enumerate(d["steps"]):
        db.add(RecipeStep(recipe_id=recipe.id, seq=seq, code=s["code"], name=s["name"], blocks=s["blocks"]))
    audit.log(db, user, "RECIPE_IMPORTED", "master_recipe", recipe.id, new=f"{recipe.code} v{recipe.version}")
    db.flush()
    return recipe


def load_file(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def serialize(r: MasterRecipe, with_steps: bool = False) -> dict:
    out = {"id": r.id, "code": r.code, "version": r.version, "name": r.name, "product": r.product,
           "status": r.status, "batch_size": r.batch_size, "batch_size_uom": r.batch_size_uom,
           "step_count": len(r.steps)}
    if with_steps:
        out["steps"] = [{"seq": s.seq, "code": s.code, "name": s.name, "blocks": s.blocks} for s in r.steps]
    return out
