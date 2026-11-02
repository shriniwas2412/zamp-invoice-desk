"""Invoice -> decision pipeline.

Eight stages, each recorded with its status, timing and evidence so a run can
be replayed and explained. The model never makes the decision: extraction
(rules, optionally helped by an LLM) proposes facts, deterministic checks
against the vendor master, POs and history decide.
"""
import csv
import difflib
import hashlib
import io
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pymupdf
from PIL import Image

from . import store

DATA = Path(__file__).resolve().parent.parent / "data"

BUYER_GSTIN = "29AAACA1234F1Z9"  # Acme Retail India, the (fictional) company we process for
TOLERANCE_PCT = 1.0              # price variance allowed against the PO balance
TOLERANCE_ABS = 2000.0           # ...capped at this many rupees
DUP_WINDOW_DAYS = 30
OCR_MIN_CONFIDENCE = 70
STAGE_DELAY = float(os.getenv("STAGE_DELAY", "0.6"))  # pacing so the live view is watchable

STAGES = [
    ("intake", "Intake"),
    ("read", "Read document"),
    ("extract", "Extract fields"),
    ("arithmetic", "Check the maths"),
    ("vendor", "Verify vendor"),
    ("po_match", "Match purchase order"),
    ("duplicate", "Duplicate check"),
    ("decision", "Decision"),
]


# ---------- reference data ----------

def load_csv(name):
    with open(DATA / name, newline="") as f:
        return list(csv.DictReader(f))


def vendors():
    return load_csv("vendors.csv")


def purchase_orders():
    rows = load_csv("purchase_orders.csv")
    for r in rows:
        r["amount_pre_tax"] = float(r["amount_pre_tax"])
        r["tax_rate"] = float(r["tax_rate"])
    return rows


def erp_history():
    rows = load_csv("invoice_history.csv")
    for r in rows:
        r["amount_pre_tax"] = float(r["amount_pre_tax"])
        r["total"] = float(r["total"])
        r["source"] = "ERP history"
    return rows


# ---------- parsing helpers ----------

AMOUNT = r"\d{1,3}(?:,\d{2,3})+(?:\.\d{1,2})?|\d+\.\d{2}"
GSTIN = r"\b\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]\b"
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def inr(x):
    """Rs 1,12,000.00 - Indian digit grouping for every amount a person reads."""
    neg, x = x < 0, abs(x)
    whole, frac = f"{x:.2f}".split(".")
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ("-" if neg else "") + "Rs " + ",".join(groups + [tail]) + "." + frac


def money(s):
    return float(s.replace(",", "").rstrip("."))


def amounts_in(line):
    return [money(a) for a in re.findall(AMOUNT, line)]


def parse_date(s):
    m = re.search(r"\b(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})\b", s)
    if m:
        d, mo, y = map(int, m.groups())
        return datetime(y, mo, d).date().isoformat()
    m = re.search(r"\b(\d{1,2})[\s\-]([A-Za-z]{3})[a-z]*[\s\-,]+(\d{4})\b", s)
    if m and m.group(2).lower() in MONTHS:
        return datetime(int(m.group(3)), MONTHS[m.group(2).lower()], int(m.group(1))).date().isoformat()
    return None


def norm_invoice_no(s):
    """INV/2026/0042 and INV-2026-042 are the same invoice: compare token by token."""
    if not s:
        return None
    toks = re.findall(r"[A-Za-z]+|\d+", s.upper())
    return "-".join(str(int(t)) if t.isdigit() else t for t in toks)


def norm_po(s):
    m = re.search(r"(\d{4})", s or "")
    return f"PO-{m.group(1)}" if m else None


def text_lines_from_pdf(page):
    """Rebuild visual lines from word boxes (PDF text order is not reading order)."""
    words = page.get_text("words")
    rows = {}
    for x0, y0, x1, y1, w, *_ in words:
        key = round(y1 / 3)
        rows.setdefault(key, []).append((x0, w))
    return [" ".join(w for _, w in sorted(ws)) for _, ws in sorted(rows.items())]


def ocr_page(page):
    import pytesseract
    pix = page.get_pixmap(dpi=200)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    confs = [float(c) for c, t in zip(data["conf"], data["text"]) if t.strip() and float(c) >= 0]
    text = pytesseract.image_to_string(img)
    return [l for l in text.splitlines() if l.strip()], (sum(confs) / len(confs) if confs else 0)


def rule_extract(lines):
    text = "\n".join(lines)
    f = {"vendor_name": lines[0].strip() if lines else None}
    gst = [g for g in re.findall(GSTIN, text) if g != BUYER_GSTIN]
    f["vendor_gstin"] = gst[0] if gst else None

    f["invoice_number"] = None
    for l in lines:
        m = re.search(r"(?:invoice\s*(?:no\.?|number|#)|bill\s*no\.?)\s*[:#]?\s*([A-Z0-9][A-Z0-9/\-]*\d[A-Z0-9/\-]*)", l, re.I)
        if m:
            f["invoice_number"] = m.group(1)
            break

    f["invoice_date"] = None
    for l in lines:
        if re.search(r"\b(invoice date|date of invoice|dated|date)\b", l, re.I):
            f["invoice_date"] = parse_date(l)
            if f["invoice_date"]:
                break

    f["po_reference"] = None
    for l in lines:
        m = re.search(r"\b(?:P\.?\s?O\.?(?:\s*(?:No\.?|Number|Ref))?|Order\s+Ref)\s*[:\-#]?\s*(?:PO[-\s]?)?(\d{4})\b", l, re.I)
        if m:
            f["po_reference"] = f"PO-{m.group(1)}"
            break

    def labelled(pattern, exclude=None):
        hit = None
        for l in lines:
            if re.search(pattern, l, re.I) and not (exclude and re.search(exclude, l, re.I)):
                a = amounts_in(l)
                if a:
                    hit = a[-1]
        return hit

    f["subtotal"] = labelled(r"sub\s*-?total|taxable (value|amount)")
    f["total"] = labelled(r"grand total|total amount|amount (payable|due)|^\s*total\b", exclude=r"sub\s*-?total")

    taxes = []
    for l in lines:
        m = re.search(r"\b(CGST|SGST|IGST|GST)\b[^0-9]*(\d{1,2}(?:\.\d+)?)\s*%", l, re.I)
        a = amounts_in(l)
        if m and a:
            taxes.append({"type": m.group(1).upper(), "rate_pct": float(m.group(2)), "amount": a[-1]})
    f["taxes"] = taxes

    items, in_table = [], False
    for l in lines:
        if re.search(r"\bdescription\b", l, re.I):
            in_table = True
            continue
        if in_table and re.search(r"sub\s*-?total|taxable", l, re.I):
            break
        if in_table:
            m = re.match(rf"(.+?)\s+(\d+)\s*[^\d]*?\s+({AMOUNT})\s+({AMOUNT})\.?\s*$", l.strip())
            if m:
                items.append({"description": m.group(1).strip(), "qty": int(m.group(2)),
                              "rate": money(m.group(3)), "amount": money(m.group(4))})
    f["line_items"] = items
    f["notes"] = " ".join(l for l in lines if re.search(r"reminder|agreement|part shipment|as per", l, re.I))
    return f


def llm_extract(text):
    """Optional: ask Claude for the same fields. Only used to fill gaps the rules left."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=key)
        msg = client.messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5"),
            max_tokens=800,
            messages=[{"role": "user", "content":
                       "Extract these fields from the invoice text as JSON only, null when absent, never guess: "
                       "vendor_name, vendor_gstin, invoice_number, invoice_date (YYYY-MM-DD), po_reference, "
                       "subtotal, total.\n\n" + text[:8000]}],
        )
        raw = msg.content[0].text
        return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    except Exception as e:  # never let the optional path break a run
        return {"_error": str(e)}


# ---------- the run ----------

class Run:
    def __init__(self, run_id, filename, data):
        self.id, self.filename, self.data = run_id, filename, data
        self.ctx = {}
        self.flags = []  # (severity, code, message) severity: reject | hold | note

    def stage(self, key, fn):
        store.update_stage(self.id, key, {"status": "running", "started": time.time()})
        t0 = time.time()
        try:
            status, summary, detail = fn()
        except Exception as e:
            status, summary, detail = "fail", f"Stage crashed: {e}", {}
            self.flags.append(("hold", "stage_error", f"{key} failed unexpectedly; needs a person"))
        time.sleep(STAGE_DELAY)
        store.update_stage(self.id, key, {"status": status, "summary": summary, "detail": detail,
                                          "ms": int((time.time() - t0) * 1000)})
        return status

    def flag(self, sev, code, msg):
        self.flags.append((sev, code, msg))

    # 1
    def intake(self):
        sha = hashlib.sha256(self.data).hexdigest()
        self.ctx["sha256"] = sha
        doc = pymupdf.open(stream=self.data, filetype="pdf")
        self.ctx["doc"] = doc
        same = store.find_by_hash(sha, exclude=self.id)
        detail = {"file": self.filename, "size_kb": round(len(self.data) / 1024, 1),
                  "pages": doc.page_count, "sha256": sha[:16] + "..."}
        if same:
            self.flag("reject", "exact_file_duplicate", f"Byte-identical file already processed in run #{same['id']}")
            return "fail", f"Same file already processed (run #{same['id']})", detail
        return "ok", f"{doc.page_count}-page PDF received", detail

    # 2
    def read(self):
        lines, method, conf = [], "text layer", None
        for page in self.ctx["doc"]:
            pl = text_lines_from_pdf(page)
            if sum(len(l) for l in pl) < 40:
                pl, conf = ocr_page(page)
                method = "OCR (scanned image)"
            lines += pl
        self.ctx["lines"] = lines
        detail = {"method": method, "lines": len(lines), "preview": lines[:12]}
        if conf is not None:
            detail["ocr_confidence"] = round(conf, 1)
            if conf < OCR_MIN_CONFIDENCE:
                self.flag("hold", "low_ocr", f"OCR confidence {conf:.0f}% is below {OCR_MIN_CONFIDENCE}%")
                return "warn", f"Scanned image, OCR confidence low ({conf:.0f}%)", detail
            self.flag("note", "ocr", f"Read by OCR (confidence {conf:.0f}%)")
            return "warn", f"No text layer: read by OCR, confidence {conf:.0f}%", detail
        return "ok", f"Read {len(lines)} lines from the text layer", detail

    # 3
    def extract(self):
        f = rule_extract(self.ctx["lines"])
        method = "rules"
        llm = llm_extract("\n".join(self.ctx["lines"]))
        filled = []
        if llm and "_error" not in llm:
            method = "rules + Claude"
            for k in ("vendor_gstin", "invoice_number", "invoice_date", "po_reference", "subtotal", "total"):
                if f.get(k) in (None, "") and llm.get(k) not in (None, ""):
                    f[k] = llm[k]
                    filled.append(k)
        self.ctx["f"] = f
        critical = {"invoice_number": "invoice number", "invoice_date": "invoice date", "total": "total amount"}
        missing = [label for k, label in critical.items() if not f.get(k)]
        self.ctx["missing"] = missing
        detail = {"method": method, "fields": {k: v for k, v in f.items() if k != "line_items"},
                  "line_items": f["line_items"], "missing": missing}
        if llm and "_error" in llm:
            detail["llm_error"] = llm["_error"]
        if filled:
            detail["filled_by_llm"] = filled
        if missing:
            self.flag("hold", "missing_fields", "Missing on the invoice: " + ", ".join(missing))
            return "warn", "Missing: " + ", ".join(missing), detail
        return "ok", f"{len(f['line_items'])} line items, total {inr(f['total'])}", detail

    # 4
    def arithmetic(self):
        f = self.ctx["f"]
        checks, bad = [], False
        items_sum = round(sum(i["amount"] for i in f["line_items"]), 2)
        for i in f["line_items"]:
            ok = abs(i["qty"] * i["rate"] - i["amount"]) < 1
            checks.append({"check": f"{i['description'][:40]}: qty x rate", "ok": ok})
            bad |= not ok
        if f["line_items"] and f.get("subtotal") is not None:
            ok = abs(items_sum - f["subtotal"]) < 1
            checks.append({"check": f"Line items sum {items_sum:,.2f} = stated subtotal {f['subtotal']:,.2f}", "ok": ok})
            if not ok:
                bad = True
                self.flag("hold", "lines_vs_subtotal",
                          f"Line items add up to {inr(items_sum)} but the stated subtotal is {inr(f['subtotal'])}")
        tax = round(sum(t["amount"] for t in f["taxes"]), 2)
        if f.get("subtotal") is not None and f.get("total") is not None:
            ok = abs(f["subtotal"] + tax - f["total"]) < 1
            checks.append({"check": f"Subtotal + tax {f['subtotal'] + tax:,.2f} = total {f['total']:,.2f}", "ok": ok})
            if not ok:
                bad = True
                self.flag("hold", "total_mismatch", "Subtotal plus tax does not equal the invoice total")
        # GST type: same state -> CGST+SGST, different state -> IGST
        if f.get("vendor_gstin") and f["taxes"]:
            interstate = f["vendor_gstin"][:2] != BUYER_GSTIN[:2]
            types = {t["type"] for t in f["taxes"]}
            ok = ("IGST" in types) if interstate else ("IGST" not in types)
            checks.append({"check": f"{'Inter' if interstate else 'Intra'}-state supply taxed as {'/'.join(sorted(types))}", "ok": ok})
            if not ok:
                bad = True
                self.flag("hold", "gst_type", "GST type does not match the vendor's state")
        self.ctx["pre_tax"] = f.get("subtotal") if f.get("subtotal") is not None else items_sum
        self.ctx["tax"] = tax
        return ("warn" if bad else "ok"), ("Numbers do not add up" if bad else "All totals reconcile"), {"checks": checks}

    # 5
    def vendor(self):
        f = self.ctx["f"]
        vs = vendors()
        v, how = None, None
        if f.get("vendor_gstin"):
            v = next((x for x in vs if x["gstin"] == f["vendor_gstin"]), None)
            how = "GSTIN"
        if not v and f.get("vendor_name"):
            names = {}
            for x in vs:
                for n in [x["legal_name"]] + x["aliases"].split("|"):
                    names[n.lower()] = x
            m = difflib.get_close_matches(f["vendor_name"].lower(), names.keys(), n=1, cutoff=0.75)
            if m:
                v, how = names[m[0]], "name (fuzzy)"
        self.ctx["vendor"] = v
        if not v:
            self.flag("hold", "unknown_vendor", "Vendor is not in the approved vendor master")
            return "fail", "Vendor not found in vendor master", {"searched": f.get("vendor_gstin") or f.get("vendor_name")}
        detail = {"vendor_id": v["vendor_id"], "legal_name": v["legal_name"], "matched_on": how,
                  "status": v["status"], "payment_terms_days": v["payment_terms_days"]}
        if v["status"] != "approved":
            self.flag("reject", "vendor_blocked", f"{v['legal_name']} is {v['status']} in the vendor master")
            return "fail", f"Vendor is {v['status']}", detail
        if how != "GSTIN":
            self.flag("note", "vendor_by_name", "Vendor matched by name, not GSTIN")
        return "ok", f"{v['legal_name']} ({v['vendor_id']}), approved", detail

    def _consumed(self, po_number):
        prior = [h for h in erp_history() if h["po_number"] == po_number]
        prior += [r for r in store.approved_against_po(po_number, exclude=self.id)]
        return prior, round(sum(p["amount_pre_tax"] for p in prior), 2)

    # 6
    def po_match(self):
        f, v = self.ctx["f"], self.ctx.get("vendor")
        pre_tax = self.ctx.get("pre_tax") or 0
        pos = purchase_orders()
        po, how, candidates = None, None, []
        if f.get("po_reference"):
            po = next((p for p in pos if p["po_number"] == norm_po(f["po_reference"])), None)
            how = "explicit reference on invoice"
            if not po:
                self.flag("hold", "po_not_found", f"Invoice cites {f['po_reference']}, which does not exist")
                return "fail", f"{f['po_reference']} not found", {}
            if v and po["vendor_id"] != v["vendor_id"]:
                self.flag("hold", "po_wrong_vendor", f"{po['po_number']} belongs to a different vendor")
                return "fail", "PO belongs to another vendor", {"po": po["po_number"]}
        elif v:
            # Implied PO: score this vendor's open POs on remaining balance and wording.
            words = set(re.findall(r"[a-z]{4,}", " ".join(self.ctx["lines"]).lower()))
            for p in pos:
                if p["vendor_id"] != v["vendor_id"] or p["status"] != "open":
                    continue
                _, used = self._consumed(p["po_number"])
                remaining = p["amount_pre_tax"] - used
                gap = abs(pre_tax - remaining) / remaining if remaining > 0 else 9
                overlap = len(words & set(re.findall(r"[a-z]{4,}", p["description"].lower())))
                candidates.append({"po": p["po_number"], "remaining": remaining,
                                   "amount_gap_pct": round(gap * 100, 2), "shared_words": overlap})
            candidates.sort(key=lambda c: (c["amount_gap_pct"], -c["shared_words"]))
            good = [c for c in candidates if c["amount_gap_pct"] <= 5 and c["shared_words"] >= 2]
            if len(good) == 1:
                po = next(p for p in pos if p["po_number"] == good[0]["po"])
                how = "inferred (no PO on invoice; one open PO fits amount and description)"
                self.flag("note", "po_inferred", f"No PO on the invoice; matched to {po['po_number']} as the only open PO that fits")
            else:
                self.flag("hold", "po_ambiguous", "No PO on the invoice and no single open PO fits; buyer must confirm")
                return "fail", "No PO reference and no unique match", {"candidates": candidates}
        else:
            self.flag("hold", "po_missing", "No PO reference and vendor unknown")
            return "fail", "Cannot match a PO", {}

        prior, used = self._consumed(po["po_number"])
        remaining = round(po["amount_pre_tax"] - used, 2)
        allowed = min(TOLERANCE_ABS, po["amount_pre_tax"] * TOLERANCE_PCT / 100)
        over = round(pre_tax - remaining, 2)
        self.ctx["po"] = po
        detail = {"po": po["po_number"], "matched": how, "po_description": po["description"],
                  "po_owner": po["owner"], "po_amount_pre_tax": po["amount_pre_tax"],
                  "already_invoiced": used, "remaining_before": remaining, "this_invoice_pre_tax": pre_tax,
                  "variance": over, "tolerance": round(allowed, 2),
                  "prior_invoices": [{"invoice": p.get("invoice_number"), "amount": p["amount_pre_tax"],
                                      "source": p.get("source", "this system")} for p in prior],
                  "candidates": candidates or None}
        if over > allowed:
            self.flag("hold", "po_exceeded",
                      f"Invoice is {inr(pre_tax)} but only {inr(remaining)} is left on {po['po_number']} "
                      f"({inr(used)} already invoiced); over by {inr(over)}, tolerance {inr(allowed)}")
            return "fail", f"Exceeds PO balance by {inr(over)}", detail
        if over > 0:
            self.flag("note", "within_tolerance", f"{inr(over)} above the PO balance, within the {inr(allowed)} tolerance")
            return "ok", f"{po['po_number']}: {inr(over)} over, within tolerance", detail
        return "ok", f"{po['po_number']}: fits the remaining balance", detail

    # 7
    def duplicate(self):
        f, v = self.ctx["f"], self.ctx.get("vendor")
        if not v:
            return "skip", "Skipped: vendor unknown", {}
        key = norm_invoice_no(f.get("invoice_number"))
        seen = [dict(h, source="ERP history") for h in erp_history() if h["vendor_id"] == v["vendor_id"]]
        seen += store.invoices_for_vendor(v["vendor_id"], exclude=self.id)
        detail = {"normalised_invoice_no": key, "compared_against": len(seen)}
        for s in seen:
            if key and norm_invoice_no(s["invoice_number"]) == key:
                where = s.get("source") or f"run #{s['run_id']}"
                self.flag("reject", "duplicate",
                          f"Same vendor already billed invoice {s['invoice_number']} ({where}); "
                          f"'{f['invoice_number']}' is the same number written differently")
                detail["match"] = s
                return "fail", f"Duplicate of {s['invoice_number']} ({where})", detail
        d = f.get("invoice_date")
        for s in seen:
            same_total = f.get("total") and abs(float(s["total"]) - f["total"]) < 1
            close = d and s.get("invoice_date") and abs(
                (datetime.fromisoformat(d) - datetime.fromisoformat(s["invoice_date"])).days) <= DUP_WINDOW_DAYS
            if same_total and (close or not d):
                self.flag("hold", "possible_duplicate",
                          f"Same vendor and same total as {s['invoice_number']} within {DUP_WINDOW_DAYS} days")
                detail["possible_match"] = s
                return "warn", "Possible duplicate (same amount, close date)", detail
        return "ok", "No earlier invoice matches", detail

    # 8
    def decision(self):
        f = self.ctx.get("f", {})
        rejects = [m for s, _, m in self.flags if s == "reject"]
        holds = [m for s, _, m in self.flags if s == "hold"]
        notes = [m for s, _, m in self.flags if s == "note"]
        codes = {c for _, c, _ in self.flags}
        po = self.ctx.get("po")
        if rejects:
            outcome, route = "REJECT", "AP clerk (no action unless disputed)"
            action = "Do not pay. Reply to the vendor that this invoice was already received."
        elif holds:
            outcome = "HOLD"
            if codes & {"po_exceeded", "po_ambiguous", "po_not_found", "po_wrong_vendor"}:
                route = f"PO owner: {po['owner']}" if po else "Procurement"
                action = "Ask the PO owner to approve the excess or raise a PO amendment before payment."
            elif codes & {"missing_fields", "lines_vs_subtotal", "total_mismatch", "gst_type"}:
                route = "AP clerk -> vendor"
                action = "Send the vendor the query below and wait for a corrected invoice."
            else:
                route = "AP clerk"
                action = "Review the flagged items and approve or reject."
        else:
            outcome, route = "APPROVE", "Auto-approved -> payment run"
            v = self.ctx.get("vendor") or {}
            due = None
            if f.get("invoice_date") and v.get("payment_terms_days"):
                due = (datetime.fromisoformat(f["invoice_date"]) + timedelta(days=int(v["payment_terms_days"]))).date().isoformat()
            action = f"Schedule payment of {inr(f['total'])}" + (f", due {due}." if due else ".")
        query = None
        if codes & {"missing_fields", "lines_vs_subtotal", "total_mismatch", "gst_type"}:
            asks = []
            if self.ctx.get("missing"):
                asks.append("the " + ", ".join(self.ctx["missing"]) + " (required for GST input credit and our records)")
            asks += [m + "; please confirm which is correct" for s, c, m in self.flags
                     if c in ("lines_vs_subtotal", "total_mismatch", "gst_type")]
            vname = (self.ctx.get("vendor") or {}).get("legal_name") or f.get("vendor_name") or "team"
            query = (f"Hi {vname} team,\n\nThanks for your invoice"
                     + (f" against {po['po_number']}" if po else "") + ". Before we can process it we need:\n"
                     + "\n".join(f"  - {a}" for a in asks)
                     + "\n\nPlease send a corrected invoice and we will process it on receipt.\n\nAcme Retail AP team")
        self.ctx["decision"] = {"outcome": outcome, "route_to": route, "next_action": action,
                                "reasons": rejects + holds, "notes": notes, "vendor_query": query}
        return {"APPROVE": "ok", "HOLD": "warn", "REJECT": "fail"}[outcome], outcome, self.ctx["decision"]

    def execute(self):
        for key, _ in STAGES:
            if key in ("arithmetic", "vendor", "po_match", "duplicate") and "f" not in self.ctx:
                store.update_stage(self.id, key, {"status": "skip", "summary": "Skipped: nothing extracted"})
                continue
            self.stage(key, getattr(self, key))
        f = self.ctx.get("f", {})
        v = self.ctx.get("vendor") or {}
        po = self.ctx.get("po") or {}
        store.finish(self.id, {
            "decision": self.ctx["decision"]["outcome"],
            "vendor_id": v.get("vendor_id"), "vendor_name": v.get("legal_name") or f.get("vendor_name"),
            "invoice_number": f.get("invoice_number"), "invoice_date": f.get("invoice_date"),
            "po_number": po.get("po_number"), "amount_pre_tax": self.ctx.get("pre_tax"),
            "total": f.get("total"), "sha256": self.ctx.get("sha256"), "result": self.ctx["decision"],
        })


def run_pipeline(run_id, filename, data):
    Run(run_id, filename, data).execute()
