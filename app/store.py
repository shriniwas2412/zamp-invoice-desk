"""SQLite run ledger: every run, every stage, every decision, every human review."""
import json
import sqlite3
import threading
import time
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "runs.db"
_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filename TEXT, created REAL, finished REAL, state TEXT,
  stages TEXT, decision TEXT, final_status TEXT,
  vendor_id TEXT, vendor_name TEXT, invoice_number TEXT, invoice_date TEXT,
  po_number TEXT, amount_pre_tax REAL, total REAL, sha256 TEXT,
  result TEXT, review TEXT
)"""


def _conn():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init():
    with _conn() as c:
        c.execute(SCHEMA)


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in ("stages", "result", "review"):
        d[k] = json.loads(d[k]) if d[k] else None
    return d


def create(filename, stage_list):
    stages = {k: {"label": label, "status": "pending"} for k, label in stage_list}
    with _lock, _conn() as c:
        cur = c.execute("INSERT INTO runs (filename, created, state, stages) VALUES (?,?,?,?)",
                        (filename, time.time(), "running", json.dumps(stages)))
        return cur.lastrowid


def update_stage(run_id, key, patch):
    with _lock, _conn() as c:
        stages = json.loads(c.execute("SELECT stages FROM runs WHERE id=?", (run_id,)).fetchone()[0])
        stages[key].update(patch)
        c.execute("UPDATE runs SET stages=? WHERE id=?", (json.dumps(stages, default=str), run_id))


def finish(run_id, f):
    status = {"APPROVE": "approved", "HOLD": "needs review", "REJECT": "rejected"}[f["decision"]]
    with _lock, _conn() as c:
        c.execute("""UPDATE runs SET state='done', finished=?, decision=?, final_status=?, vendor_id=?,
                     vendor_name=?, invoice_number=?, invoice_date=?, po_number=?, amount_pre_tax=?, total=?,
                     sha256=?, result=? WHERE id=?""",
                  (time.time(), f["decision"], status, f["vendor_id"], f["vendor_name"], f["invoice_number"],
                   f["invoice_date"], f["po_number"], f["amount_pre_tax"], f["total"], f["sha256"],
                   json.dumps(f["result"], default=str), run_id))


def review(run_id, action, note, reviewer):
    status = "approved (manual)" if action == "approve" else "rejected (manual)"
    rec = {"action": action, "note": note, "reviewer": reviewer, "at": time.time()}
    with _lock, _conn() as c:
        c.execute("UPDATE runs SET final_status=?, review=? WHERE id=?", (status, json.dumps(rec), run_id))


def get(run_id):
    with _conn() as c:
        return _row(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())


def all_runs():
    with _conn() as c:
        return [_row(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC")]


def find_by_hash(sha, exclude):
    with _conn() as c:
        return _row(c.execute("SELECT * FROM runs WHERE sha256=? AND id<>? AND final_status NOT LIKE 'rejected%'",
                              (sha, exclude)).fetchone())


def approved_against_po(po_number, exclude):
    with _conn() as c:
        rows = c.execute("""SELECT id, invoice_number, amount_pre_tax FROM runs WHERE po_number=? AND id<>?
                            AND final_status IN ('approved', 'approved (manual)')""", (po_number, exclude))
        return [{"invoice_number": r["invoice_number"], "amount_pre_tax": r["amount_pre_tax"] or 0,
                 "source": f"run #{r['id']}"} for r in rows]


def invoices_for_vendor(vendor_id, exclude):
    with _conn() as c:
        rows = c.execute("""SELECT id, invoice_number, invoice_date, total FROM runs WHERE vendor_id=? AND id<>?
                            AND state='done' AND final_status NOT LIKE 'rejected%'""", (vendor_id, exclude))
        return [{"run_id": r["id"], "invoice_number": r["invoice_number"], "invoice_date": r["invoice_date"],
                 "total": r["total"] or 0} for r in rows]


def reset():
    with _lock, _conn() as c:
        c.execute("DELETE FROM runs")
        c.execute("DELETE FROM sqlite_sequence WHERE name='runs'")
