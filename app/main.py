"""FastAPI app: upload an invoice, watch it run, review the outcome."""
import threading
from pathlib import Path

import pymupdf
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import pipeline, store

ROOT = Path(__file__).resolve().parent.parent
STATIC = Path(__file__).resolve().parent / "static"
SAMPLES = ROOT / "samples"
UPLOADS = ROOT / "data" / "uploads"
UPLOADS.mkdir(exist_ok=True)

app = FastAPI(title="Invoice Desk")
store.init()


def start_run(filename, data):
    run_id = store.create(filename, pipeline.STAGES)
    (UPLOADS / f"{run_id}.pdf").write_bytes(data)  # kept for the audit trail and the preview pane
    threading.Thread(target=pipeline.run_pipeline, args=(run_id, filename, data), daemon=True).start()
    return {"id": run_id}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.post("/api/runs")
async def upload(file: UploadFile = File(...)):
    data = await file.read()
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "Please upload a PDF invoice.")
    return start_run(file.filename, data)


@app.get("/api/samples")
def samples():
    return [p.name for p in sorted(SAMPLES.glob("*.pdf"))]


@app.post("/api/samples/{name}")
def run_sample(name: str):
    path = SAMPLES / name
    if path.parent != SAMPLES or not path.exists():
        raise HTTPException(404, "No such sample")
    return start_run(name, path.read_bytes())


@app.get("/api/samples/{name}/pdf")
def sample_pdf(name: str):
    path = SAMPLES / name
    if path.parent != SAMPLES or not path.exists():
        raise HTTPException(404, "No such sample")
    return FileResponse(path, media_type="application/pdf")


@app.get("/api/runs")
def runs():
    return store.all_runs()


@app.get("/api/runs/{run_id}")
def run(run_id: int):
    r = store.get(run_id)
    if not r:
        raise HTTPException(404, "Run not found")
    return r


@app.get("/api/runs/{run_id}/pdf")
def run_pdf(run_id: int):
    path = UPLOADS / f"{run_id}.pdf"
    if not path.exists():
        raise HTTPException(404, "File not kept for this run")
    return FileResponse(path, media_type="application/pdf")


@app.get("/api/runs/{run_id}/page.png")
def run_page_png(run_id: int):
    path = UPLOADS / f"{run_id}.pdf"
    if not path.exists():
        raise HTTPException(404, "File not kept for this run")
    try:
        png = pymupdf.open(path)[0].get_pixmap(dpi=110).tobytes("png")
    except Exception:
        raise HTTPException(422, "Could not render this PDF")
    return Response(png, media_type="image/png")


class Review(BaseModel):
    action: str  # approve | reject
    note: str = ""
    reviewer: str = "AP reviewer"


@app.post("/api/runs/{run_id}/review")
def review(run_id: int, body: Review):
    r = store.get(run_id)
    if not r or r["state"] != "done":
        raise HTTPException(400, "Run is not finished")
    if r["decision"] != "HOLD":
        raise HTTPException(400, "Only held invoices need a human decision")
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action must be approve or reject")
    store.review(run_id, body.action, body.note, body.reviewer)
    return store.get(run_id)


@app.post("/api/reset")
def reset():
    store.reset()
    for f in UPLOADS.glob("*.pdf"):
        f.unlink()
    return {"ok": True}


@app.get("/api/reference")
def reference():
    return {"vendors": pipeline.vendors(), "purchase_orders": pipeline.purchase_orders(),
            "erp_history": pipeline.erp_history(),
            "rules": {"tolerance_pct": pipeline.TOLERANCE_PCT, "tolerance_abs": pipeline.TOLERANCE_ABS,
                      "duplicate_window_days": pipeline.DUP_WINDOW_DAYS,
                      "ocr_min_confidence": pipeline.OCR_MIN_CONFIDENCE}}


app.mount("/static", StaticFiles(directory=STATIC), name="static")
