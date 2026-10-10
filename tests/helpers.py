"""Test helpers.

Runner drives a batch through the API using only the recipe configuration: it reads each block's
config and produces in-limit values. Because it contains no product knowledge, the same runner
executing a second recipe is itself evidence that the engine is generic (TC-21).
"""
from app import clock

PW = "Demo@123"


class Api:
    def __init__(self, client):
        self.c = client
        self._tokens: dict[str, str] = {}

    def headers(self, user: str) -> dict:
        if user not in self._tokens:
            r = self.c.post("/api/auth/login", json={"username": user, "password": PW})
            assert r.status_code == 200, r.text
            self._tokens[user] = r.json()["access_token"]
        return {"Authorization": f"Bearer {self._tokens[user]}"}

    def get(self, user, url, expect=200, **kw):
        r = self.c.get(url, headers=self.headers(user), **kw)
        assert r.status_code == expect, f"GET {url} -> {r.status_code}: {r.text}"
        return r

    def post(self, user, url, json=None, expect=200):
        r = self.c.post(url, json=json or {}, headers=self.headers(user))
        assert r.status_code == expect, f"POST {url} -> {r.status_code}: {r.text}"
        return r

    def recipe_id(self, code: str) -> int:
        return next(r["id"] for r in self.get("sup1", "/api/recipes").json() if r["code"] == code)

    def audit(self, **params):
        return self.get("qa1", "/api/audit", params=params).json()


def good_number(p: dict) -> float:
    lo, hi = p.get("min"), p.get("max")
    if lo is not None and hi is not None:
        return round((lo + hi) / 2, 3)
    if hi is not None:
        return round(hi / 2, 3)
    if lo is not None:
        return lo + 1
    return 100


def good_value(p: dict):
    t = p.get("type", "number")
    if t == "boolean":
        return p.get("expected", True)
    if t == "text":
        return p.get("expected", "N/A")
    return good_number(p)


class Runner:
    def __init__(self, api: Api, recipe_code: str = "PCM500-FCT"):
        self.api = api
        r = api.post("sup1", "/api/batches", {"recipe_id": api.recipe_id(recipe_code)}, expect=201)
        self.batch_id = r.json()["id"]
        self.batch_no = r.json()["batch_no"]

    # ------------------------------------------------------------------ reading
    def batch(self) -> dict:
        return self.api.get("sup1", f"/api/batches/{self.batch_id}").json()

    def step(self, code: str) -> dict:
        return next(s for s in self.batch()["steps"] if s["code"] == code)

    def block(self, code: str, idx: int) -> dict:
        return self.step(code)["blocks"][idx]

    # ------------------------------------------------------------------ acting
    def start(self, code: str, user="op1", expect=200):
        return self.api.post(user, f"/api/steps/{self.step(code)['id']}/start", expect=expect)

    def act(self, code: str, idx: int, action: str, data: dict | None = None, user="op1", expect=200):
        return self.api.post(user, f"/api/steps/{self.step(code)['id']}/blocks/{idx}",
                             {"action": action, "data": data or {}}, expect=expect)

    def sign(self, code, idx, signer, meaning, session_user=None, password=PW, expect=200):
        return self.act(code, idx, "sign", {"username": signer, "password": password, "meaning": meaning},
                        user=session_user or signer, expect=expect)

    @staticmethod
    def actor(roles) -> str:
        return "op1" if "OPERATOR" in roles else "sup1"

    def usable_equipment(self, allowed: list[str]) -> str:
        eq = {e["code"]: e for e in self.api.get("op1", "/api/equipment").json()}
        return next(c for c in allowed if eq[c]["status"] == "CLEAN" and not eq[c]["calibration_overdue"])

    def released_lot(self, material_code: str) -> str:
        lots = self.api.get("op1", "/api/lots", params={"material_code": material_code}).json()
        return next(l["lot_no"] for l in lots if l["status"] == "RELEASED" and not l["expired"])

    def do_block_happy(self, code: str, blk: dict):
        cfg, idx, t = blk["config"], blk["idx"], blk["type"]
        who = self.actor(cfg.get("roles", ["OPERATOR", "SUPERVISOR"]))
        if t == "CHECKLIST":
            self.act(code, idx, "submit", {"answers": {i["key"]: True for i in cfg["items"]}}, user=who)
        elif t == "EQUIPMENT_CHECK":
            for slot in cfg["slots"]:
                self.act(code, idx, "bind", {"slot": slot["slot"], "equipment_code": self.usable_equipment(slot["allowed"])}, user=who)
        elif t == "MATERIAL_DISPENSE":
            bal = self.usable_equipment(cfg["allowed_balances"])
            for it in cfg["items"]:
                self.act(code, idx, "dispense", {"material_code": it["material_code"], "lot_no": self.released_lot(it["material_code"]),
                                                 "balance_code": bal, "net_qty": it["target_qty"]}, user=who)
        elif t == "PARAMETER_ENTRY":
            self.act(code, idx, "submit", {"values": {p["key"]: good_value(p) for p in cfg["parameters"]}}, user=who)
        elif t == "IPC_SAMPLING":
            for n in range(cfg["min_samples"]):
                if n:
                    clock.advance(cfg["interval_min"])
                self.act(code, idx, "sample", {"values": {c["key"]: good_number(c) for c in cfg["checks"]}}, user=who)
            self.act(code, idx, "complete", user=who)
        elif t == "YIELD_CALC":
            pct = (cfg["min_pct"] + cfg["max_pct"]) / 2
            self.act(code, idx, "submit", {"good_output": round(cfg["theoretical_qty"] * pct / 100, 3)}, user=who)
        elif t == "SIGNATURE":
            roles = cfg.get("roles", ["OPERATOR", "SUPERVISOR"])
            signer = self.actor(roles)
            session = signer if cfg["meaning"] == "PERFORMED" else "op1"
            self.sign(code, idx, signer, cfg["meaning"], session_user=session)
            if cfg.get("verifier_required"):
                vroles = cfg.get("verifier_roles", ["OPERATOR", "SUPERVISOR"])
                verifier = "op2" if "OPERATOR" in vroles and signer != "op2" else "sup1"
                self.sign(code, idx, verifier, "VERIFIED", session_user=session)
        else:
            raise AssertionError(f"runner does not know block type {t}")

    def run_step(self, code: str, skip_start=False, from_block=0):
        if not skip_start:
            self.start(code)
        for blk in self.step(code)["blocks"][from_block:]:
            self.do_block_happy(code, blk)
        assert self.step(code)["status"] == "COMPLETED", self.step(code)

    def run_until(self, code: str):
        """Complete every step before `code` (exclusive)."""
        for s in self.batch()["steps"]:
            if s["code"] == code:
                return
            self.run_step(s["code"])

    def run_all(self):
        for s in self.batch()["steps"]:
            self.run_step(s["code"])
        assert self.batch()["status"] == "COMPLETED"

    def qa_approve(self, expect=200):
        self.api.post("qa1", f"/api/batches/{self.batch_id}/review/start")
        return self.api.post("qa1", f"/api/batches/{self.batch_id}/review/approve",
                             {"username": "qa1", "password": PW, "comment": "Reviewed by exception"}, expect=expect)

    def close_deviation(self, dev_id: int, justification="Investigated; no product impact", user="sup1", expect=200):
        return self.api.post(user, f"/api/deviations/{dev_id}/close",
                             {"justification": justification, "username": user, "password": PW}, expect=expect)

    def open_deviations(self):
        return [d for d in self.batch()["deviations"] if d["status"] == "OPEN"]
