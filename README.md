# Invoice Desk: PDF in, decision out

Case study for Zamp, **PS-1 (Finance / AP): invoice processing from PDF to decision**.
Shriniwas Maheshwari, October 2026.

A vendor invoice (PDF, text or scanned) goes through eight visible stages and comes out as
**APPROVE**, **HOLD** (with who it is routed to and what they must do) or **REJECT**, with the
reasons. Every stage keeps its evidence, so any decision can be explained after the fact.

## Run it (about 1 minute)

Needs Python 3.10+ and Tesseract for scanned invoices (`brew install tesseract` on a Mac,
`apt install tesseract-ocr` on Linux).

```bash
cd invoice-agent
./run.sh            # creates .venv on first run, then serves http://localhost:8077
```

Then open http://localhost:8077.

Check that all five demo inputs still reach their expected decisions:

```bash
.venv/bin/python scripts/check_samples.py
```

Regenerate the sample PDFs (they're already in `samples/`):

```bash
.venv/bin/python scripts/make_samples.py
```

## Demo order

Click **Reset demo**, then run the inputs in this order. #5 only counts as a duplicate because #1
was processed first.

| # | Input | What is hard about it | Expected |
|---|---|---|---|
| 1 | Northwind, clean PDF | nothing: the happy path | **APPROVE**, payment due date from vendor terms |
| 2 | Greenleaf, **scanned image, no PO number** | no text layer; PO has to be inferred; ₹350 extra consumables | **APPROVE** with audit notes: OCR used, PO-4512 inferred as the only open PO that fits, overage inside tolerance |
| 3 | Swift Route, **second invoice on a split PO** | invoice alone is fine; together with the ₹1,00,000 already billed (ERP history) it overruns PO-4517 by ₹12,000 | **HOLD**, routed to the PO owner with the numbers |
| 4 | Pixel Print, **no invoice number or date; lines don't add up** | line items sum to ₹48,500, stated subtotal ₹45,800 (matches the PO, so a naive check passes it) | **HOLD**, with a ready-to-send vendor query |
| 5 | Northwind **"payment reminder" resend** | invoice number written `INV/2026/0042` instead of `INV-2026-042`, different layout, so the file hash differs | **REJECT** as a duplicate of run #1 |

On a HOLD, the reviewer can **Approve anyway** or **Reject**, with a note. Manual approvals count
against the PO balance just like automatic ones.

## How it works

```
PDF ─▶ 1 Intake ─▶ 2 Read ─▶ 3 Extract ─▶ 4 Maths ─▶ 5 Vendor ─▶ 6 PO match ─▶ 7 Duplicate ─▶ 8 Decision
       hash,       text layer  fields +     lines,    GSTIN →     explicit or    normalised      APPROVE /
       file dup    or OCR      line items   tax, GST  master,     inferred PO,   number +        HOLD (+route,
                   (+conf.)                 type      status      balance, tol.  amount/date     vendor query) /
                                                                                                 REJECT
```

| Stage | What it checks | Fails to |
|---|---|---|
| Intake | SHA-256 of the file; same bytes already processed | REJECT |
| Read | Text layer; if under 40 characters, OCR with Tesseract and record mean confidence | HOLD if confidence < 70% |
| Extract | Vendor name and GSTIN, invoice number, date, PO reference, line items, subtotal, taxes, total. Labels vary by vendor ("Bill No.", "Invoice #", "Your Order Ref", "Amount Payable"...) | HOLD if invoice no., date or total is missing |
| Maths | qty × rate per line; lines = subtotal; subtotal + tax = total; CGST+SGST for same-state vendors, IGST for inter-state (from GSTIN state codes) | HOLD |
| Vendor | Match on GSTIN, fall back to fuzzy name match; vendor status | REJECT if blocked, HOLD if unknown |
| PO match | Explicit reference (normalises `4501` to `PO-4501`), or infer: score the vendor's open POs on remaining balance and shared words; only accept a single clear fit. Then: already invoiced (ERP + this system) vs remaining balance, with tolerance of 1% of the PO capped at ₹2,000 | HOLD, routed to the PO owner |
| Duplicate | Invoice number compared token by token (`INV/2026/0042` = `INV-2026-042`); then same vendor + same total within 30 days | REJECT on number match, HOLD on amount/date match |
| Decision | Any reject flag → REJECT; any hold flag → HOLD with route and next action; otherwise APPROVE with due date | |

### Where AI is, and where it isn't

- **Reading:** OCR for scans. An optional Claude call (`ANTHROPIC_API_KEY`, plus `pip install anthropic`)
  fills in fields the rules couldn't find. It only fills gaps; it never overrides a value the rules
  found, and its output still goes through every deterministic check. I haven't tested this path
  without a key: with no key the app runs on rules alone, which is how the demo runs.
- **Deciding:** no model. Approve, hold and reject come from explicit rules against the vendor
  master, POs and history, so the same invoice always gets the same decision and a finance
  person can read why. A model that is right 98% of the time is not good enough to release a
  payment on its own.

### Reference data (`data/`)

The ERP stand-ins are CSVs, editable and visible on the **Reference data** page:

- `vendors.csv`: vendor master (GSTIN, status, payment terms). V-105 is blocked.
- `purchase_orders.csv`: open POs with owner and pre-tax amount.
- `invoice_history.csv`: invoices already in the ERP (this is where the first part-shipment of PO-4517 lives).
- `runs.db` (created on first run): SQLite ledger of every run, stage, decision and human review.

## Assumptions (to say out loud in the interview)

- The buyer is a fictional Indian mid-size company (Acme Retail, GSTIN state 29, Karnataka), so
  amounts are in INR with GST. All vendors, POs and invoices are made up.
- 2-way match (invoice ↔ PO). There is no goods-receipt (GRN) data in the brief, so no 3-way match yet.
- PO amounts are pre-tax; tolerance applies to the pre-tax amount.
- Tolerance is 1% of the PO, capped at ₹2,000. It is a placeholder: every finance team sets its own.
- One invoice per PDF and the first page is the invoice.
- Input is an uploaded PDF. In production it would come from an AP mailbox; that is a connector, not new logic.

## Known limits / next steps

- Rule-based extraction is tuned on five vendor layouts. New layouts are where the LLM fallback
  (or a per-vendor template learned from corrections) earns its place.
- Multi-page invoices, multiple invoices per PDF, credit notes and foreign currency aren't handled.
- No email intake, ERP write-back or payment-file export yet.
- No authentication or roles on the reviewer actions.
- SQLite and threads are fine for a demo, not for hundreds of invoices a day (next step: a queue + Postgres).

## Deploying a live link

The case asks for a live link. The app is a single container (`Dockerfile` installs Tesseract).
On Render, Railway or Fly.io: create a web service from this folder, Docker runtime, no
environment variables needed. Data resets on every redeploy, which is fine for a demo. Deploying
needs your own account, so it's not done here.

## Files

```
app/main.py          FastAPI routes (runs, samples, review, reference, reset)
app/pipeline.py      the eight stages and the decision policy
app/store.py         SQLite run ledger
app/static/index.html  dashboard, live run view, reference page (no build step)
data/                vendor master, POs, ERP invoice history
samples/             five demo invoices (01 happy path, 02-05 edge cases)
scripts/make_samples.py   generates the sample PDFs
scripts/check_samples.py  asserts each sample's expected decision
```
