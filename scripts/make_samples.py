"""Generate the demo invoice PDFs in samples/.

Each vendor gets its own layout and labels on purpose, so the extractor
cannot rely on one template. Run: python scripts/make_samples.py
"""
import io
import random
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageFilter
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT = Path(__file__).resolve().parent.parent / "samples"
W, H = A4


def inr(x):
    """Indian digit grouping: 112000 -> 1,12,000.00"""
    s = f"{x:.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups) + "," + tail
    return f"{whole}.{frac}"


def table(c, y, rows, cols=(50, 330, 400, 480)):
    c.setFont("Helvetica-Bold", 9)
    for x, h in zip(cols, ("Description", "Qty", "Rate", "Amount")):
        c.drawString(x, y, h)
    y -= 6
    c.line(50, y, W - 50, y)
    y -= 14
    c.setFont("Helvetica", 9)
    for desc, qty, rate in rows:
        c.drawString(cols[0], y, desc)
        c.drawString(cols[1], y, str(qty))
        c.drawString(cols[2], y, inr(rate))
        c.drawString(cols[3], y, inr(qty * rate))
        y -= 16
    c.line(50, y + 6, W - 50, y + 6)
    return y - 10


def kv(c, y, pairs, x=360, bold_last=True):
    for i, (k, v) in enumerate(pairs):
        last = bold_last and i == len(pairs) - 1
        c.setFont("Helvetica-Bold" if last else "Helvetica", 10 if last else 9)
        c.drawString(x, y, k)
        c.drawRightString(W - 50, y, v)
        y -= 15
    return y


def header(c, name, addr, gstin, title="TAX INVOICE"):
    c.setFont("Helvetica-Bold", 15)
    c.drawString(50, H - 60, name)
    c.setFont("Helvetica", 9)
    c.drawString(50, H - 75, addr)
    c.drawString(50, H - 88, f"GSTIN: {gstin}")
    c.setFont("Helvetica-Bold", 12)
    c.drawRightString(W - 50, H - 60, title)


BILL_TO = ["Bill to: Acme Retail India Pvt Ltd", "4th Floor, 80 Feet Rd, Koramangala, Bengaluru 560034",
           "GSTIN: 29AAACA1234F1Z9"]


def bill_to(c, y):
    c.setFont("Helvetica", 9)
    for line in BILL_TO:
        c.drawString(50, y, line)
        y -= 13
    return y


def happy(path, inv_no="INV-2026-042", date="15/10/2026", po="PO-4501", reminder=False):
    c = canvas.Canvas(str(path), pagesize=A4)
    header(c, "Northwind Office Supplies Pvt Ltd", "12 Residency Road, Bengaluru 560025",
           "29AAGCN4821K1Z5", "PAYMENT REMINDER" if reminder else "TAX INVOICE")
    c.setFont("Helvetica", 9)
    label = "Invoice #" if reminder else "Invoice No"
    c.drawRightString(W - 50, H - 78, f"{label}: {inv_no}")
    c.drawRightString(W - 50, H - 91, f"Invoice Date: {date}")
    c.drawRightString(W - 50, H - 104, f"{'P.O. No.' if reminder else 'PO Ref'}: {po}")
    y = bill_to(c, H - 130)
    if reminder:
        c.setFont("Helvetica-Oblique", 9)
        c.drawString(50, y - 4, "Gentle reminder: this invoice is still unpaid. Kindly process at the earliest.")
        y -= 16
    rows = [("A4 copier paper, 75 GSM (box of 5 reams)", 60, 240.00),
            ("Toner cartridge, HP 88A compatible", 6, 3900.00),
            ("Box files, foolscap", 120, 40.00)]
    y = table(c, y - 20, rows)
    kv(c, y, [("Subtotal", inr(42600)), ("CGST @ 9%", inr(3834)), ("SGST @ 9%", inr(3834)),
              ("Grand Total (INR)", inr(50268))])
    c.setFont("Helvetica", 8)
    c.drawString(50, 80, "Payment terms: 30 days. Bank: HDFC Bank, A/c ending 4417, IFSC HDFC0000123")
    c.save()


def scanned(path):
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 17)
    c.drawCentredString(W / 2, H - 60, "GREENLEAF FACILITY SERVICES LLP")
    c.setFont("Helvetica", 10)
    c.drawCentredString(W / 2, H - 76, "No. 7, 2nd Cross, HSR Layout, Bengaluru 560102  |  GSTIN 29AAKFG7310M1Z2")
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, H - 115, "BILL")
    c.setFont("Helvetica", 10)
    c.drawString(50, H - 135, "Bill No. GFS-1093")
    c.drawString(50, H - 150, "Dated: 28.10.2026")
    y = bill_to(c, H - 175)
    c.setFont("Helvetica", 10)
    c.drawString(50, y - 8, "As per our service agreement - housekeeping for the month of October 2026.")
    rows = [("Housekeeping services, Koramangala office, Oct 2026", 1, 68000.00),
            ("Cleaning consumables (extra, as requested)", 1, 350.00)]
    y = table(c, y - 34, rows)
    kv(c, y, [("Taxable Value", inr(68350)), ("CGST 9%", inr(6151.50)), ("SGST 9%", inr(6151.50)),
              ("Amount Payable", inr(80653))])
    c.drawString(50, 110, "For Greenleaf Facility Services LLP")
    c.drawString(50, 80, "(Authorised Signatory)")
    c.save()
    # Rasterise, tilt and add noise so the PDF has no text layer: a real "scan".
    doc = fitz.open(stream=buf.getvalue(), filetype="pdf")
    pix = doc[0].get_pixmap(dpi=200, colorspace=fitz.csGRAY)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    img = img.rotate(0.6, expand=True, fillcolor=245).filter(ImageFilter.GaussianBlur(0.6))
    rnd = random.Random(7)
    px = img.load()
    for _ in range(25000):
        x, y = rnd.randrange(img.width), rnd.randrange(img.height)
        px[x, y] = max(0, px[x, y] - rnd.randrange(60, 140))
    img.convert("RGB").save(path, "PDF", resolution=200)


def split_po(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    header(c, "Swift Route Logistics Pvt Ltd", "Plot 44, Sector 18, Gurugram, Haryana 122015", "06AAECS5521P1ZQ")
    c.setFont("Helvetica", 9)
    c.drawRightString(W - 50, H - 78, "Invoice Number: SRL/26-27/0907")
    c.drawRightString(W - 50, H - 91, "Date of Invoice: 20-Oct-2026")
    c.drawRightString(W - 50, H - 104, "Your Order Ref: PO-4517 (part shipment 2 of 2)")
    y = bill_to(c, H - 130)
    rows = [("Freight, Bengaluru to Delhi NCR, 32 ft container (trips)", 8, 11500.00),
            ("Loading and unloading charges (per trip)", 8, 2500.00)]
    y = table(c, y - 20, rows)
    kv(c, y, [("Taxable Amount", inr(112000)), ("IGST @ 18%", inr(20160)), ("Total Amount Due", inr(132160))])
    c.save()


def missing_fields(path):
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(50, H - 65, "pixel print studio")
    c.setFont("Helvetica", 9)
    c.drawString(50, H - 80, "Indiranagar, Bengaluru  |  GSTIN 29BXRPS9081C1Z7  |  hello@pixelprint.example")
    c.setFont("Helvetica-Bold", 11)
    c.drawString(50, H - 110, "INVOICE")
    c.setFont("Helvetica", 9)
    c.drawString(50, H - 125, "Against PO-4520 - event collateral")
    y = bill_to(c, H - 150)
    rows = [("Tri-fold brochures, 130 GSM art paper", 2000, 15.50),
            ("Roll-up standees, 6x3 ft with stand", 6, 2250.00),
            ("Design and artwork charges", 1, 4000.00)]
    y = table(c, y - 20, rows)
    kv(c, y, [("Sub Total", inr(45800)), ("GST 18%", inr(8244)), ("Total", inr(54044))])
    c.drawString(50, 80, "Thank you for your business!")
    c.save()


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    happy(OUT / "01_happy_northwind_INV-2026-042.pdf")
    scanned(OUT / "02_edge_scanned_no_po_greenleaf.pdf")
    split_po(OUT / "03_edge_split_po_overrun_swiftroute.pdf")
    missing_fields(OUT / "04_edge_missing_fields_math_pixelprint.pdf")
    happy(OUT / "05_edge_duplicate_resubmission_northwind.pdf", inv_no="INV/2026/0042",
          date="22-Oct-2026", po="4501", reminder=True)
    print("samples written to", OUT)
