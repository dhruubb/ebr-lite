"""Phase 1: data model, seed data, recipe configuration, audit trail and e-signature services."""
import pytest
from sqlalchemy import text

from app import config, db
from app.services import recipes



def test_seed_loads_master_data(api):
    users = api.get("admin", "/api/users").json()
    assert {u["username"] for u in users} == {"op1", "op2", "sup1", "qa1", "admin"}
    lots = {l["lot_no"]: l for l in api.get("op1", "/api/lots").json()}
    assert lots["PCM-2602"]["status"] == "QUARANTINE"
    assert lots["PCM-2401"]["expired"] is True
    eq = {e["code"]: e for e in api.get("op1", "/api/equipment").json()}
    assert eq["BAL-02"]["calibration_overdue"] is True
    assert eq["RMG-02"]["status"] == "DIRTY"
    codes = {r["code"] for r in api.get("op1", "/api/recipes").json()}
    assert codes == {"PCM500-FCT", "VITC-DEMO"}


def test_recipe_has_eleven_production_steps(api):
    r = api.get("op1", f"/api/recipes/{api.recipe_id('PCM500-FCT')}").json()
    assert [s["code"] for s in r["steps"]] == [f"S{i}" for i in range(11)]
    assert all(s["blocks"][-1]["type"] == "SIGNATURE" for s in r["steps"])


def test_recipe_validation_rejects_bad_configuration(api):
    bad = {"code": "BAD", "version": 1, "name": "x", "product": "x", "batch_size": 1, "batch_size_uom": "kg",
           "steps": [{"code": "S0", "name": "x", "blocks": [
               {"type": "TELEPORT", "config": {}},
               {"type": "MATERIAL_DISPENSE", "config": {"items": [{"material_code": "MAT-NOPE"}], "allowed_balances": ["BAL-99"]}}]}]}
    res = api.post("admin", "/api/recipes/validate", bad).json()
    assert res["valid"] is False
    joined = " | ".join(res["errors"])
    assert "unknown block type 'TELEPORT'" in joined
    assert "last block must be SIGNATURE" in joined
    assert "unknown material 'MAT-NOPE'" in joined
    assert "unknown balance 'BAL-99'" in joined
    api.post("admin", "/api/recipes", bad, expect=422)


def test_only_admin_imports_recipes(api):
    api.post("sup1", "/api/recipes", {"code": "X"}, expect=403)


def test_login_success_and_failure_are_audited(api, client):
    r = client.post("/api/auth/login", json={"username": "op1", "password": "wrong"})
    assert r.status_code == 401
    api.headers("op1")
    actions = [a["action"] for a in api.audit()]
    assert "LOGIN_FAILED" in actions and "LOGIN" in actions


def test_endpoints_require_login(client):
    assert client.get("/api/batches").status_code == 401
    assert client.get("/api/batches", headers={"Authorization": "Bearer junk"}).status_code == 401


def test_audit_trail_triggers_block_update_and_delete(api):
    api.headers("op1")  # creates a LOGIN audit row
    with db.engine.connect() as conn:
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text("UPDATE audit_trail SET username='hacker'"))
        with pytest.raises(Exception, match="append-only"):
            conn.execute(text("DELETE FROM audit_trail"))


def test_signature_records_manifestation(api):
    from .helpers import Runner
    run = Runner(api)
    run.start("S0")
    run.act("S0", 0, "submit", {"answers": {i: True for i in
            ["prev_product_removed", "prev_docs_removed", "area_clean", "env_within_limits", "clearance_label"]}})
    run.sign("S0", 1, "op1", "PERFORMED")
    sig = run.block("S0", 1)["signatures"][0]
    assert sig["full_name"] == "Omkar Patil" and sig["meaning"] == "PERFORMED" and sig["utc_ts"].endswith("Z")


def test_recipe_validate_function_directly():
    for path in config.SEED_DIR.glob("recipe_*.json"):
        assert recipes.validate(recipes.load_file(path)) == [], path.name


def test_demo_clock_is_audited(api):
    before = api.c.get("/api/health").json()["server_utc"]
    r = api.post("op1", "/api/demo/advance-clock", {"minutes": 30}).json()
    assert r["server_utc"] > before
    row = next(a for a in api.audit() if a["action"] == "DEMO_CLOCK_ADVANCED")
    assert row["username"] == "op1" and "+30 min" in row["reason"]
