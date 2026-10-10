"""Master data (simulated ERP/WMS) and master recipes."""
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..db import get_db
from ..deps import current_user, require_roles
from ..errors import NotFound
from ..models import Equipment, Material, MaterialLot, MasterRecipe, User
from ..services import recipes

router = APIRouter(prefix="/api", tags=["master data"])


@router.get("/materials")
def list_materials(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [{"code": m.code, "name": m.name, "material_type": m.material_type, "uom": m.uom}
            for m in db.scalars(select(Material).order_by(Material.code))]


@router.get("/lots")
def list_lots(material_code: str | None = None, db: Session = Depends(get_db), _: User = Depends(current_user)):
    q = select(MaterialLot).order_by(MaterialLot.lot_no)
    if material_code:
        q = q.where(MaterialLot.material_code == material_code)
    today = clock.plant_today()
    return [{"lot_no": l.lot_no, "material_code": l.material_code, "status": l.status,
             "expiry_date": l.expiry_date.isoformat(), "expired": l.expiry_date < today,
             "qty_available": l.qty_available} for l in db.scalars(q)]


@router.get("/equipment")
def list_equipment(db: Session = Depends(get_db), _: User = Depends(current_user)):
    today = clock.plant_today()
    return [{"code": e.code, "name": e.name, "equipment_type": e.equipment_type, "status": e.status,
             "calibration_due_date": e.calibration_due_date.isoformat() if e.calibration_due_date else None,
             "calibration_overdue": bool(e.calibration_due_date and e.calibration_due_date < today),
             "punch_set_id": e.punch_set_id} for e in db.scalars(select(Equipment).order_by(Equipment.code))]


@router.get("/users")
def list_users(db: Session = Depends(get_db), _: User = Depends(require_roles("ADMIN", "QA", "SUPERVISOR"))):
    return [{"id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role, "active": u.active}
            for u in db.scalars(select(User).order_by(User.id))]


@router.get("/recipes")
def list_recipes(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [recipes.serialize(r) for r in db.scalars(select(MasterRecipe).order_by(MasterRecipe.code, MasterRecipe.version))]


@router.get("/recipes/{recipe_id}")
def get_recipe(recipe_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    r = db.get(MasterRecipe, recipe_id)
    if r is None:
        raise NotFound(f"Recipe {recipe_id} not found.")
    return recipes.serialize(r, with_steps=True)


@router.post("/recipes/validate")
def validate_recipe(body: dict, db: Session = Depends(get_db), _: User = Depends(require_roles("ADMIN"))):
    errs = recipes.validate(body, db)
    return {"valid": not errs, "errors": errs}


@router.post("/recipes", status_code=201)
def import_recipe(body: dict, db: Session = Depends(get_db), user: User = Depends(require_roles("ADMIN"))):
    """URS-014: a new product is configured by uploading a recipe JSON. No code change."""
    r = recipes.import_recipe(db, body, user)
    db.commit()
    return recipes.serialize(r, with_steps=True)
