"""Reset the database and load the demo master data.

    python -m app.seed            (run from app/backend)

WARNING: drops all tables, including the audit trail. Demo use only.
"""
import json
from datetime import date

from . import config, db
from .models import Equipment, Material, MaterialLot, Role, User
from .security import hash_password
from .services import audit, recipes


def _load(name: str) -> dict:
    return json.loads((config.SEED_DIR / name).read_text(encoding="utf-8"))


def run(verbose: bool = True):
    db.create_schema(drop=True)
    s = db.SessionLocal()
    try:
        u = _load("users.json")
        for r in u["roles"]:
            s.add(Role(code=r["code"], description=r["description"]))
        pw_hash = hash_password(u["default_password"])
        for x in u["users"]:
            s.add(User(username=x["username"], full_name=x["full_name"], role=x["role"], password_hash=pw_hash))
        for m in _load("materials.json")["materials"]:
            s.add(Material(**m))
        for l in _load("lots.json")["lots"]:
            s.add(MaterialLot(**{**l, "expiry_date": date.fromisoformat(l["expiry_date"])}))
        for e in _load("equipment.json")["equipment"]:
            due = e.get("calibration_due_date")
            s.add(Equipment(**{**e, "calibration_due_date": date.fromisoformat(due) if due else None}))
        s.flush()
        audit.log(s, None, "SEED", "database", None, new="demo master data loaded")
        # the main product first, then any other recipe JSON files in seed/
        for path in sorted(config.SEED_DIR.glob("recipe_*.json"), key=lambda p: (p.name != "recipe_paracetamol_500.json", p.name)):
            r = recipes.import_recipe(s, recipes.load_file(path), None)
            if verbose:
                print(f"  recipe {r.code} v{r.version}: {len(r.definition['steps'])} steps  ({path.name})")
        s.commit()
        if verbose:
            print(f"Seeded {config.DATABASE_URL}")
            print("Users: op1, op2 (OPERATOR) · sup1 (SUPERVISOR) · qa1 (QA) · admin (ADMIN) — password Demo@123")
    finally:
        s.close()


if __name__ == "__main__":
    run()
