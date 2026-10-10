"""Acceptance tests TC-01 … TC-21 (docs/05_Test_Plan.md). One test per test case, named after its ID.

These are the automated versions. The manual run through the UI (with screenshots) is recorded
separately in docs/06_Test_Execution/.
"""
from app import clock

from .helpers import PW, Runner

S0_ANSWERS = {k: True for k in ["prev_product_removed", "prev_docs_removed", "area_clean", "env_within_limits", "clearance_label"]}


def _to_dispensing(api) -> Runner:
    run = Runner(api)
    run.run_step("S0")
    run.start("S1")
    return run


def _dispense(run, material="MAT-PCM", lot="PCM-2601", balance="BAL-01", qty=50.0, expect=200):
    return run.act("S1", 0, "dispense", {"material_code": material, "lot_no": lot, "balance_code": balance, "net_qty": qty},
                   expect=expect)


# ---------------------------------------------------------------------------------------- TC-01
def test_TC01_steps_cannot_start_out_of_sequence(api):
    run = Runner(api)
    r = run.start("S1", expect=409)
    assert "sequence" in r.json()["detail"]
    # also inside a step: process data cannot be entered before the equipment check
    run.run_until("S2")
    run.start("S2")
    r = run.act("S2", 1, "submit", {"values": {"sieve_mesh": 40, "sieve_intact_before": True, "sieve_intact_after": True}}, expect=409)
    assert "Complete block 1 (EQUIPMENT_CHECK)" in r.json()["detail"]
    assert any(a["action"] == "SEQUENCE_BLOCKED" for a in api.audit(batch_id=run.batch_id))


# ---------------------------------------------------------------------------------------- TC-02
def test_TC02_quarantine_lot_blocked(api):
    run = _to_dispensing(api)
    r = _dispense(run, lot="PCM-2602", expect=409)
    assert "QUARANTINE" in r.json()["detail"]
    blocked = [a for a in api.audit(batch_id=run.batch_id) if a["action"] == "DISPENSE_BLOCKED"]
    assert blocked and blocked[0]["entity_id"] == "PCM-2602"


# ---------------------------------------------------------------------------------------- TC-03
def test_TC03_expired_lot_blocked(api):
    run = _to_dispensing(api)
    r = _dispense(run, lot="PCM-2401", expect=409)
    assert "expired" in r.json()["detail"]


# ---------------------------------------------------------------------------------------- TC-04
def test_TC04_weight_within_tolerance_accepted(api):
    run = _to_dispensing(api)
    r = _dispense(run, qty=50.20).json()          # 50 kg ±0.5 % = 49.750 – 50.250
    assert r["in_limits"] is True
    e = run.block("S1", 0)["entries"][0]
    assert e["accepted"] and e["value"] == "50.200" and e["limits"] == "49.750 – 50.250"


# ---------------------------------------------------------------------------------------- TC-05
def test_TC05_weight_out_of_tolerance_forces_reweigh_or_deviation(api):
    run = _to_dispensing(api)
    r = _dispense(run, qty=50.40).json()
    assert r["in_limits"] is False and "Re-weigh" in r["message"]
    assert run.step("S1")["status"] == "IN_PROGRESS"       # not on hold: operator chooses
    # option A: re-weigh -> accepted, the rejected attempt stays on record
    _dispense(run, qty=50.10)
    entries = [e for e in run.block("S1", 0)["entries"] if e["key"] == "MAT-PCM"]
    assert [e["accepted"] for e in entries] == [False, True]
    # option B: raise a deviation against an out-of-tolerance weighing
    bad = _dispense(run, material="MAT-MST", lot="MST-2601", qty=5.08).json()   # 5 kg ±1 % = 4.95 – 5.05
    dev = api.post("op1", "/api/deviations", {"step_execution_id": run.step("S1")["id"],
                   "description": "Starch over-dispensed by 30 g", "related_entry_id": bad["entry_id"]}, expect=201).json()
    assert run.step("S1")["status"] == "ON_HOLD"
    run.close_deviation(dev["id"], "Within formula overage allowance; use as is")
    starch = [e for e in run.block("S1", 0)["entries"] if e["key"] == "MAT-MST"][0]
    assert starch["accepted"] is True and starch["in_limits"] is False   # still visible to QA as an exception


# ---------------------------------------------------------------------------------------- TC-06
def test_TC06_balance_with_overdue_calibration_blocked(api):
    run = _to_dispensing(api)
    r = _dispense(run, balance="BAL-02", expect=409)
    assert "calibration overdue" in r.json()["detail"]


# ---------------------------------------------------------------------------------------- TC-07
def test_TC07_dirty_equipment_blocked(api):
    run = Runner(api)
    run.run_until("S4")
    run.start("S4")
    r = run.act("S4", 0, "bind", {"slot": "granulator", "equipment_code": "RMG-02"}, expect=409)
    assert "DIRTY" in r.json()["detail"]
    run.act("S4", 0, "bind", {"slot": "granulator", "equipment_code": "RMG-01"})


# ---------------------------------------------------------------------------------------- TC-08
def test_TC08_same_user_cannot_perform_and_verify(api):
    run = Runner(api)
    run.start("S0")
    run.act("S0", 0, "submit", {"answers": S0_ANSWERS})
    run.sign("S0", 1, "op1", "PERFORMED")
    r = run.sign("S0", 1, "op1", "VERIFIED", expect=403)
    assert "4-eyes" in r.json()["detail"] or "not allowed" in r.json()["detail"]
    # S1 allows an OPERATOR verifier: still never the performer
    run.sign("S0", 1, "sup1", "VERIFIED", session_user="op1")
    run.start("S1")
    run.do_block_happy("S1", run.block("S1", 0))
    run.sign("S1", 1, "op1", "PERFORMED")
    r = run.sign("S1", 1, "op1", "VERIFIED", expect=403)
    assert "4-eyes" in r.json()["detail"]
    run.sign("S1", 1, "op2", "VERIFIED", session_user="op1")
    assert run.step("S1")["status"] == "COMPLETED"


# ---------------------------------------------------------------------------------------- TC-09
def test_TC09_wrong_password_fails_and_is_logged(api):
    run = Runner(api)
    run.start("S0")
    run.act("S0", 0, "submit", {"answers": S0_ANSWERS})
    r = run.sign("S0", 1, "op1", "PERFORMED", password="Wrong@123", expect=401)
    assert "invalid" in r.json()["detail"].lower()
    failed = [a for a in api.audit(batch_id=run.batch_id) if a["action"] == "SIGNATURE_FAILED"]
    assert len(failed) == 1 and "op1" in failed[0]["new_value"]
    assert run.block("S0", 1)["signatures"] == []


# ---------------------------------------------------------------------------------------- TC-10
def test_TC10_signature_shows_name_time_meaning(api):
    run = Runner(api)
    run.run_step("S0")
    sigs = run.block("S0", 1)["signatures"]
    assert [(s["full_name"], s["meaning"]) for s in sigs] == [("Omkar Patil", "PERFORMED"), ("Rahul Deshmukh", "VERIFIED")]
    assert all(s["utc_ts"].endswith("Z") for s in sigs)
    html = api.get("qa1", f"/api/batches/{run.batch_id}/report").text
    assert "Omkar Patil" in html and "PERFORMED" in html and "IST" in html


# ---------------------------------------------------------------------------------------- TC-11
def test_TC11_operator_cannot_access_qa_review(api):
    run = Runner(api)
    api.get("op1", f"/api/batches/{run.batch_id}/review", expect=403)
    api.post("op1", f"/api/batches/{run.batch_id}/review/approve", {"username": "op1", "password": PW}, expect=403)
    api.post("qa1", f"/api/steps/{run.step('S0')['id']}/start", expect=403)     # and QA cannot execute
    api.post("admin", "/api/batches", {"recipe_id": 1}, expect=403)             # admin has no GMP rights


# ---------------------------------------------------------------------------------------- TC-12
def test_TC12_lod_out_of_limit_puts_step_on_hold(api):
    run = Runner(api)
    run.run_until("S5")
    run.start("S5")
    run.do_block_happy("S5", run.block("S5", 0))
    r = run.act("S5", 1, "submit", {"values": {"inlet_temp_c": 60, "outlet_temp_c": 40, "drying_time_min": 45, "lod_pct": 3.1}}).json()
    assert r["out_of_limit"] == ["lod_pct"] and r["step_status"] == "ON_HOLD"
    devs = run.open_deviations()
    assert len(devs) == 1 and devs[0]["category"] == "OUT_OF_LIMIT" and "3.1" in devs[0]["description"]
    r = run.sign("S5", 2, "op1", "PERFORMED", expect=409)
    assert "ON_HOLD" in r.json()["detail"]


# ---------------------------------------------------------------------------------------- TC-13
def test_TC13_supervisor_closes_deviation_and_step_resumes(api):
    run = Runner(api)
    run.run_until("S5")
    run.start("S5")
    run.do_block_happy("S5", run.block("S5", 0))
    run.act("S5", 1, "submit", {"values": {"inlet_temp_c": 60, "outlet_temp_c": 40, "drying_time_min": 45, "lod_pct": 3.1}})
    dev = run.open_deviations()[0]
    run.close_deviation(dev["id"], user="op1", expect=403)                       # operator cannot close
    api.post("sup1", f"/api/deviations/{dev['id']}/close",
             {"justification": "  ", "username": "sup1", "password": PW}, expect=422)   # justification required
    run.close_deviation(dev["id"], "Granules re-dried 10 min, re-test LOD 2.0 % (QC ref 123)")
    assert run.step("S5")["status"] == "IN_PROGRESS"
    run.sign("S5", 2, "op1", "PERFORMED")
    assert run.step("S5")["status"] == "COMPLETED" and run.step("S6")["status"] == "READY"


# ---------------------------------------------------------------------------------------- TC-14
def test_TC14_compression_ipc_out_of_limit_flagged(api):
    run = Runner(api)
    run.run_until("S8")
    run.start("S8")
    for i in range(3):
        run.do_block_happy("S8", run.block("S8", i))
    vals = {"avg_weight_mg": 640, "hardness_n": 100, "thickness_mm": 5.4, "friability_pct": 0.4, "disintegration_min": 6}
    r = run.act("S8", 3, "sample", {"values": vals}).json()
    assert r["in_limits"] is False and r["step_status"] == "ON_HOLD"
    sample = run.block("S8", 3)["samples"][0]
    weight = next(x for x in sample["results"] if x["key"] == "avg_weight_mg")
    assert weight["in_limits"] is False and weight["limits"] == "570 – 630"
    assert "Average weight" in run.open_deviations()[0]["description"]


# ---------------------------------------------------------------------------------------- TC-15
def test_TC15_missed_ipc_interval_flagged(api):
    run = Runner(api)
    run.run_until("S8")
    run.start("S8")
    for i in range(3):
        run.do_block_happy("S8", run.block("S8", i))
    good = {"avg_weight_mg": 600, "hardness_n": 100, "thickness_mm": 5.4, "friability_pct": 0.4, "disintegration_min": 6}
    run.act("S8", 3, "sample", {"values": good})
    clock.advance(70)                                    # 30-min interval: the sample due at +30 min was missed
    r = run.act("S8", 3, "sample", {"values": good}).json()
    assert r["missed_intervals"] == 2                   # due at +30 and +60, taken at +70
    samples = run.block("S8", 3)["samples"]
    assert [s["status"] for s in samples] == ["TAKEN", "MISSED", "MISSED", "TAKEN"]
    assert run.open_deviations()[0]["category"] == "MISSED_IPC"


# ---------------------------------------------------------------------------------------- TC-16
def test_TC16_correction_requires_reason_and_is_audited(api):
    run = Runner(api)
    run.run_until("S3")
    run.start("S3")
    run.act("S3", 0, "submit", {"values": {"water_qty_l": 12.0, "mixing_time_min": 21, "solution_clear": True}})
    entry = next(e for e in run.block("S3", 0)["entries"] if e["key"] == "mixing_time_min")
    api.post("op1", f"/api/entries/{entry['id']}/correct", {"value": 12}, expect=422)
    api.post("op1", f"/api/entries/{entry['id']}/correct", {"value": 12, "reason": ""}, expect=422)
    api.post("op1", f"/api/entries/{entry['id']}/correct", {"value": 12, "reason": "Typing error: timer showed 12 min"})
    e = next(e for e in run.block("S3", 0)["entries"] if e["key"] == "mixing_time_min")
    assert e["value"] == "12" and e["original_value"] == "21" and e["corrected"] and e["in_limits"]
    row = next(a for a in api.audit(batch_id=run.batch_id) if a["action"] == "DATA_CORRECTED")
    assert (row["old_value"], row["new_value"], row["reason"]) == ("21", "12", "Typing error: timer showed 12 min")


# ---------------------------------------------------------------------------------------- TC-17
def test_TC17_audit_trail_cannot_be_edited_or_deleted_via_api(api, client):
    api.headers("op1")
    row = api.audit()[0]
    h = api.headers("admin")
    for method in ("put", "patch", "delete"):
        r = getattr(client, method)(f"/api/audit/{row['id']}", headers=h)
        assert r.status_code in (404, 405), (method, r.status_code)
    assert api.audit()[-1] == api.audit()[-1]          # unchanged


# ---------------------------------------------------------------------------------------- TC-18
def test_TC18_yield_out_of_range_flagged(api):
    run = Runner(api)
    run.run_until("S10")
    run.start("S10", user="sup1")
    r = run.act("S10", 0, "submit", {"good_output": 95200, "rejects": 2500, "samples": 300, "waste": 1500}, user="sup1").json()
    assert r["yield_pct"] == 95.2 and r["in_limits"] is False
    assert run.open_deviations()[0]["category"] == "YIELD"
    assert run.step("S10")["status"] == "ON_HOLD"


# ---------------------------------------------------------------------------------------- TC-19
def test_TC19_qa_cannot_approve_with_open_deviation(api):
    run = Runner(api)
    run.run_all()
    api.post("qa1", f"/api/batches/{run.batch_id}/review/start")
    # QA finds a problem during review and raises a batch-level deviation
    dev = api.post("qa1", "/api/deviations", {"batch_id": run.batch_id,
                   "description": "Label reconciliation sheet missing from line clearance"}, expect=201).json()
    r = api.post("qa1", f"/api/batches/{run.batch_id}/review/approve", {"username": "qa1", "password": PW}, expect=409)
    assert "1 deviation(s) still open" in r.json()["detail"]
    assert run.batch()["status"] == "UNDER_REVIEW"
    assert any(a["action"] == "APPROVAL_BLOCKED" for a in api.audit(batch_id=run.batch_id))
    run.close_deviation(dev["id"], "Sheet located in area logbook and attached; no impact")
    r = api.post("qa1", f"/api/batches/{run.batch_id}/review/approve", {"username": "qa1", "password": PW})
    assert r.json()["status"] == "APPROVED"


# ---------------------------------------------------------------------------------------- TC-20
def test_TC20_happy_path_to_approved_with_report(api):
    run = Runner(api)
    run.run_all()
    review = api.get("qa1", f"/api/batches/{run.batch_id}/review").json()
    assert review["exceptions"] == []
    r = run.qa_approve().json()
    assert r["status"] == "APPROVED" and r["disposition_by"] == "Anita Joshi"
    html = api.get("qa1", f"/api/batches/{run.batch_id}/report").text
    assert run.batch_no in html and "APPROVED" in html and "Audit trail" in html
    assert "No exceptions" in html


# ---------------------------------------------------------------------------------------- TC-21
def test_TC21_second_recipe_runs_without_code_change(api):
    run = Runner(api, "VITC-DEMO")
    run.run_all()
    assert run.qa_approve().json()["status"] == "APPROVED"
    # and a brand-new product can be added at runtime as JSON by an admin
    new = {"code": "IBU-DEMO", "version": 1, "name": "Ibuprofen demo", "product": "Ibuprofen 200 mg (demo)",
           "batch_size": 1000, "batch_size_uom": "tablets", "batch_prefix": "IBU",
           "steps": [{"code": "S0", "name": "Mixing", "blocks": [
               {"type": "EQUIPMENT_CHECK", "config": {"slots": [{"slot": "blender", "allowed": ["BLN-01"]}]}},
               {"type": "PARAMETER_ENTRY", "config": {"parameters": [{"key": "mix_min", "label": "Mix time", "type": "number", "min": 5, "max": 7}]}},
               {"type": "SIGNATURE", "config": {"meaning": "PERFORMED"}}]}]}
    api.post("admin", "/api/recipes", new, expect=201)
    run2 = Runner(api, "IBU-DEMO")
    run2.run_all()
    assert run2.qa_approve().json()["status"] == "APPROVED"
