"""Executed batch record as a print-friendly HTML page (URS-015 / 21 CFR 11.10(b)).
Open it in a browser and use Print -> Save as PDF."""
from datetime import datetime
from html import escape

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import clock
from ..models import AuditTrail, Batch
from .records import batch_detail
from .review import exceptions


def _t(iso: str | None) -> str:
    if not iso:
        return ""
    return clock.ist(datetime.fromisoformat(iso.rstrip("Z")))


def _e(v) -> str:
    return escape("" if v is None else str(v))


def _flag(ok: bool, limits: str | None = "x") -> str:
    if limits is None:
        return '<span class="muted">recorded</span>'
    return '<span class="ok">OK</span>' if ok else '<span class="bad">OUT OF LIMIT</span>'


def _sig(s: dict) -> str:
    return f'<div class="sig">✍ <b>{_e(s["full_name"])}</b> ({_e(s["username"])}) · <b>{_e(s["meaning"])}</b> · {_t(s["utc_ts"])}</div>'


CSS = """
body{font:13px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1d2433;margin:24px auto;max-width:1000px;padding:0 16px}
h1{font-size:20px;margin:0}h2{font-size:15px;margin:22px 0 6px;border-bottom:2px solid #1d2433;padding-bottom:3px}
h3{font-size:13px;margin:12px 0 4px}table{width:100%;border-collapse:collapse;margin:4px 0 8px}
th,td{border:1px solid #c9ced8;padding:3px 6px;text-align:left;vertical-align:top}th{background:#eef1f5;font-weight:600}
.meta td{border:none;padding:1px 10px 1px 0}.ok{color:#17663a;font-weight:600}.bad{color:#b42318;font-weight:700}
.sig{margin:3px 0;padding:3px 6px;background:#f6f8fb;border-left:3px solid #3a5a8c}.muted{color:#667085}
.status{display:inline-block;padding:2px 8px;border-radius:10px;background:#1d2433;color:#fff;font-size:12px}
.step{page-break-inside:avoid}.banner{background:#fff4e5;border:1px solid #f5c26b;padding:6px 10px;margin:10px 0}
@media print{body{margin:0}.noprint{display:none}}
"""


def render(db: Session, b: Batch) -> str:
    r = batch_detail(db, b)
    ex = exceptions(db, b)
    audit_rows = db.scalars(select(AuditTrail).where(AuditTrail.batch_id == b.id).order_by(AuditTrail.id)).all()
    h = []
    h.append(f"<!doctype html><html><head><meta charset='utf-8'><title>Batch record {_e(r['batch_no'])}</title>"
             f"<style>{CSS}</style></head><body>")
    h.append(f"<h1>Executed Batch Record — {_e(r['batch_no'])} <span class='status'>{_e(r['status'])}</span></h1>")
    h.append("<div class='banner'>eBR-Lite demo system. Illustrative data only — not a validated system.</div>")
    h.append("<table class='meta'>"
             f"<tr><td>Product</td><td><b>{_e(r['product'])}</b></td><td>Master recipe</td><td>{_e(r['recipe']['code'])} v{r['recipe']['version']}</td></tr>"
             f"<tr><td>Batch size</td><td>{r['batch_size']:,.0f} {_e(r['batch_size_uom'])}</td><td>Created</td><td>{_t(r['created_at'])} by {_e(r['created_by'])}</td></tr>"
             f"<tr><td>Started</td><td>{_t(r['started_at'])}</td><td>Completed</td><td>{_t(r['completed_at'])}</td></tr>"
             f"<tr><td>Disposition</td><td>{_e(r['status'])} {('by ' + _e(r['disposition_by'])) if r['disposition_by'] else ''}</td>"
             f"<td>Printed</td><td>{clock.ist(clock.utcnow())}</td></tr></table>")
    for s in r["batch_signatures"]:
        h.append(_sig(s))
    if r["disposition_comment"]:
        h.append(f"<p class='muted'>Disposition comment: {_e(r['disposition_comment'])}</p>")

    h.append(f"<h2>Exceptions for QA review ({len(ex)})</h2>")
    if ex:
        h.append("<table><tr><th>Type</th><th>Step</th><th>Ref</th><th>Detail</th><th>By</th><th>When</th></tr>")
        for x in ex:
            h.append(f"<tr><td class='bad'>{_e(x['type'])}</td><td>{_e(x['step'])}</td><td>{_e(x['ref'])}</td>"
                     f"<td>{_e(x['detail'])}</td><td>{_e(x['by'])}</td><td>{_t(x['at'])}</td></tr>")
        h.append("</table>")
    else:
        h.append("<p class='ok'>No exceptions: every value within limits, no corrections, no deviations.</p>")

    h.append("<h2>Steps</h2>")
    for s in r["steps"]:
        h.append(f"<div class='step'><h3>{_e(s['code'])} · {_e(s['name'])} — {_e(s['status'])}</h3>"
                 f"<div class='muted'>Started {_t(s['started_at'])} by {_e(s['started_by'])} · Completed {_t(s['completed_at'])}</div>")
        for blk in s["blocks"]:
            if blk["entries"]:
                h.append(f"<table><tr><th colspan='7'>{_e(blk['type'])}</th></tr>"
                         "<tr><th>Item</th><th>Value</th><th>Limits</th><th>Result</th><th>Detail</th><th>By</th><th>When</th></tr>")
                for e in blk["entries"]:
                    unit = f" {e['unit']}" if e["unit"] else ""
                    detail = []
                    ctx = e.get("context") or {}
                    if ctx.get("lot_no"):
                        detail.append(f"lot {ctx['lot_no']} on {ctx.get('balance')}")
                    if not e["accepted"]:
                        detail.append("rejected weighing")
                    if e["corrected"]:
                        detail.append(f"corrected (was {e['original_value']})")
                    h.append(f"<tr><td>{_e(e['label'])}</td><td><b>{_e(e['value'])}{_e(unit)}</b></td>"
                             f"<td>{_e(e['limits'] or '—')}</td><td>{_flag(e['in_limits'], e['limits'])}</td><td>{_e('; '.join(detail))}</td>"
                             f"<td>{_e(e['entered_by'])}</td><td>{_t(e['entered_at'])}</td></tr>")
                h.append("</table>")
            if blk["samples"]:
                checks = blk["config"]["checks"]
                h.append("<table><tr><th>IPC #</th>" + "".join(f"<th>{_e(c.get('label', c['key']))}<br><span class='muted'>{_e(c.get('unit', ''))}</span></th>" for c in checks)
                         + "<th>Result</th><th>By</th><th>Taken</th></tr>")
                for sm in blk["samples"]:
                    if sm["status"] == "MISSED":
                        h.append(f"<tr><td>{sm['sample_no']}</td><td colspan='{len(checks)}' class='bad'>MISSED (due {_t(sm['due_at'])})</td>"
                                 "<td class='bad'>MISSED</td><td></td><td></td></tr>")
                        continue
                    vals = {x["key"]: x for x in sm["results"]}
                    cells = "".join(f"<td class='{'' if vals[c['key']]['in_limits'] else 'bad'}'>{_e(vals[c['key']]['value'])}</td>" for c in checks)
                    h.append(f"<tr><td>{sm['sample_no']}</td>{cells}<td>{_flag(sm['in_limits'])}</td>"
                             f"<td>{_e(sm['taken_by'])}</td><td>{_t(sm['taken_at'])}</td></tr>")
                h.append("</table>")
            for sg in blk["signatures"]:
                h.append(_sig(sg))
        h.append("</div>")

    h.append(f"<h2>Deviations ({len(r['deviations'])})</h2>")
    if r["deviations"]:
        h.append("<table><tr><th>No.</th><th>Step</th><th>Category</th><th>Description</th><th>Raised</th><th>Status / closure</th></tr>")
        for d in r["deviations"]:
            closure = (f"CLOSED by {_e(d['closed_by'])}, {_t(d['closed_at'])}<br>{_e(d['justification'])}"
                       if d["status"] == "CLOSED" else "<span class='bad'>OPEN</span>")
            h.append(f"<tr><td>{_e(d['number'])}</td><td>{_e(d['step_code'])}</td><td>{_e(d['category'])} ({_e(d['source'])})</td>"
                     f"<td>{_e(d['description'])}</td><td>{_e(d['raised_by'])}<br>{_t(d['raised_at'])}</td><td>{closure}</td></tr>")
        h.append("</table>")
    else:
        h.append("<p class='muted'>None.</p>")

    h.append(f"<h2>Audit trail ({len(audit_rows)} entries)</h2>"
             "<table><tr><th>#</th><th>When (IST)</th><th>User</th><th>Action</th><th>Entity</th><th>Field</th><th>Old → New</th><th>Reason</th></tr>")
    for a in audit_rows:
        change = f"{_e(a.old_value)} → {_e(a.new_value)}" if a.old_value is not None else _e(a.new_value)
        h.append(f"<tr><td>{a.id}</td><td>{clock.ist(a.utc_ts)}</td><td>{_e(a.username)}</td><td>{_e(a.action)}</td>"
                 f"<td>{_e(a.entity)} {_e(a.entity_id)}</td><td>{_e(a.field)}</td><td>{change}</td><td>{_e(a.reason)}</td></tr>")
    h.append("</table><p class='muted'>End of batch record.</p></body></html>")
    return "".join(h)
