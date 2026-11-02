"""Regression check: every demo input must reach its expected decision.

Run: STAGE_DELAY=0 python scripts/check_samples.py   (uses a throwaway database)
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("STAGE_DELAY", "0")

from app import pipeline, store  # noqa: E402

store.DB = Path(tempfile.mkdtemp()) / "check.db"
store.init()

EXPECTED = [  # order matters: 05 is a duplicate of 01
    ("01_happy_northwind_INV-2026-042.pdf", "APPROVE", set()),
    ("02_edge_scanned_no_po_greenleaf.pdf", "APPROVE", {"po_inferred", "within_tolerance", "ocr"}),
    ("03_edge_split_po_overrun_swiftroute.pdf", "HOLD", {"po_exceeded"}),
    ("04_edge_missing_fields_math_pixelprint.pdf", "HOLD", {"missing_fields", "lines_vs_subtotal"}),
    ("05_edge_duplicate_resubmission_northwind.pdf", "REJECT", {"duplicate"}),
]

failed = 0
for name, want, codes in EXPECTED:
    rid = store.create(name, pipeline.STAGES)
    run = pipeline.Run(rid, name, (ROOT / "samples" / name).read_bytes())
    run.execute()
    got = store.get(rid)["decision"]
    seen = {c for _, c, _ in run.flags}
    ok = got == want and codes <= seen
    failed += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name:48} {got:8} flags={sorted(seen)}")
sys.exit(1 if failed else 0)
