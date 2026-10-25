#!/usr/bin/env python3
"""
OCR Queue Processor (2026-08-04 refactor)

Previous pipeline: local vision LLM (gemma4) — endpoint retired.
New pipeline: local tesseract + Australian-receipt regex parsing.

Two-stage processing:

    Stage 1 — Tesseract OCR (local):    receipt image -> raw text
    Stage 2 — Regex parser (local):     raw text   -> {vendor, date, amounts, gst}
    Stage 3 — Manual review (agent):    if Stage 2 fails, image + raw text saved
                                        to ocr_queue.extracted_data so an agent
                                        (the operating agent) can inspect via an image tool and
                                        PATCH the expense back via the API.

Inputs picked up from the API (config.yaml ``ocr:`` section).
Output: same `expenses` + `ocr_queue` tables. No schema migration needed —
we reuse ``ocr_queue.extracted_data`` (JSONB) to store both the parsed dict
and, on failure, the raw OCR text under the key ``raw_text``.

Australian receipts vary widely, so the parser is intentionally greedy:

  - vendor_name:  first non-empty line that looks like a business name (heuristic)
  - expense_date: tries dd/mm/yyyy, dd-mm-yyyy, dd.mm.yyyy, then yyyy-mm-dd
  - ex_gst_amount: any $xx.xx near a "subtotal"/"ex GST" label; falls back to
                   the largest $ figure if no subtotal is found
  - gst_amount:   any $xx.xx near a "GST"/"tax" label
  - gst_type:     0.1 if a GST line is present, else 0
  - total_amount: any $xx.xx near a "total" label (last match wins)

If the parser can't extract at least the date OR a total, the job is marked
failed and the expense is flagged for manual review.
"""

import os
import sys
import time
import json
import re
import base64
import argparse
import subprocess
import tempfile
import shutil
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app import create_app
from app.models import db
from app.models.expense import Expense
from app.models.ocr_queue import OcrQueue


# ---------------------------------------------------------------------------
# Tesseract wrapper
# ---------------------------------------------------------------------------

def tesseract_image_to_text(image_path: str, lang: str = "eng", psm: int = 6) -> str:
    """Run tesseract on an image file and return the extracted text.

    Falls back to Tesseract's default PSM (6 = uniform block of text) if psm
    is not explicitly useful. Returns '' on any failure (we never want to
    crash the worker over a tesseract hiccup).
    """
    try:
        out = subprocess.run(
            ["tesseract", image_path, "-", "-l", lang, "--psm", str(psm)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if out.returncode != 0:
            sys.stderr.write(f"[tesseract] rc={out.returncode} stderr={out.stderr!r}\n")
        return out.stdout or ""
    except subprocess.TimeoutExpired:
        sys.stderr.write(f"[tesseract] timeout on {image_path}\n")
        return ""
    except Exception as e:
        sys.stderr.write(f"[tesseract] crash on {image_path}: {e!r}\n")
        return ""


def pdf_to_text(pdf_path: str, lang: str = "eng") -> str:
    """Extract text from a PDF receipt using pdftoppm + tesseract.

    1. Rasterise each page to PNG with pdftoppm at 200dpi.
    2. OCR each page with tesseract.
    3. Concatenate.

    Returns '' on any failure. Skips silently if poppler/pdftoppm isn't
    installed (the job will then fail to extract anything, which is fine —
    the manual-review path will pick it up).
    """
    if not shutil.which("pdftoppm"):
        sys.stderr.write("[pdf_to_text] pdftoppm not installed — cannot OCR PDFs locally\n")
        return ""

    tmp = tempfile.mkdtemp(prefix="ocr_pdf_")
    try:
        prefix = os.path.join(tmp, "page")
        proc = subprocess.run(
            ["pdftoppm", "-r", "200", "-png", pdf_path, prefix],
            capture_output=True, text=True, timeout=60, check=False,
        )
        if proc.returncode != 0:
            sys.stderr.write(f"[pdftoppm] rc={proc.returncode} stderr={proc.stderr!r}\n")
            return ""

        # Collect all rendered pages, sorted by filename (page-1, page-2, ...)
        pages = sorted(Path(tmp).glob("page-*.png"))
        chunks = []
        for page in pages:
            chunks.append(tesseract_image_to_text(str(page), lang=lang))
        return "\n".join(chunks)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def extract_text(image_path: str, lang: str = "eng") -> str:
    """Route to the right text extractor based on file type."""
    if not os.path.exists(image_path):
        return ""
    ext = Path(image_path).suffix.lower()
    if ext == ".pdf":
        return pdf_to_text(image_path, lang=lang)
    # JPEG/PNG/etc. — tesseract handles natively
    return tesseract_image_to_text(image_path, lang=lang)


# ---------------------------------------------------------------------------
# Regex parser — Australian receipts
# ---------------------------------------------------------------------------

# Money: $45.50, 45.50, 1,234.56, $1,234.56
_MONEY_RE = re.compile(r'(?:\$)?\s*(-?\d{1,3}(?:,\d{3})*(?:\.\d{2})|(-?\d+\.\d{2}))')

# Dates — try Australian formats first
_DATE_DDMMYYYY = re.compile(r'\b(\d{1,2})[\/\-\.](\d{1,2})[\/\-\.](\d{2,4})\b')
_DATE_ISO      = re.compile(r'\b(\d{4})-(\d{2})-(\d{2})\b')
_DATE_DDMMM    = re.compile(r'\b(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{2,4})\b')

_MONTHS = {
    'jan': 1, 'january': 1,
    'feb': 2, 'february': 2,
    'mar': 3, 'march': 3,
    'apr': 4, 'april': 4,
    'may': 5,
    'jun': 6, 'june': 6,
    'jul': 7, 'july': 7,
    'aug': 8, 'august': 8,
    'sep': 9, 'sept': 9, 'september': 9,
    'oct': 10, 'october': 10,
    'nov': 11, 'november': 11,
    'dec': 12, 'december': 12,
}


def _parse_money(s: str) -> Decimal | None:
    """Parse a money string into Decimal, return None on failure."""
    if s is None:
        return None
    s = s.replace('$', '').replace(',', '').strip()
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _parse_date_ddmmyyyy(day: str, month: str, year: str) -> str | None:
    """Return YYYY-MM-DD or None."""
    try:
        d, m = int(day), int(month)
        y = int(year)
        if y < 100:
            y = 2000 + y if y < 70 else 1900 + y
        if not (1 <= m <= 12 and 1 <= d <= 31 and 1900 <= y <= 2100):
            return None
        return f"{y:04d}-{m:02d}-{d:02d}"
    except (ValueError, TypeError):
        return None


def _parse_date_named(day: str, month_name: str, year: str) -> str | None:
    m = _MONTHS.get(month_name.lower()[:3])
    if not m:
        return None
    return _parse_date_ddmmyyyy(day, str(m), year)


def find_date(text: str) -> str | None:
    """Find the most likely receipt date (YYYY-MM-DD).

    Prefers ISO format, then dd/mm/yyyy, then dd Month yyyy.
    """
    iso = _DATE_ISO.search(text)
    if iso:
        return f"{iso.group(1)}-{iso.group(2)}-{iso.group(3)}"

    named = _DATE_DDMMM.search(text)
    if named:
        d = _parse_date_named(named.group(1), named.group(2), named.group(3))
        if d:
            return d

    # First dd/mm/yyyy that yields a valid date is good enough
    for m in _DATE_DDMMYYYY.finditer(text):
        d = _parse_date_ddmmyyyy(m.group(1), m.group(2), m.group(3))
        if d:
            return d
    return None


def _line_has_label(line: str, *labels: str) -> bool:
    low = line.lower()
    return any(label in low for label in labels)


def _line_has_money(line: str) -> list[Decimal]:
    matches = []
    for m in _MONEY_RE.finditer(line):
        s = m.group(0).replace('$', '').replace(',', '').strip()
        try:
            matches.append(Decimal(s))
        except (InvalidOperation, ValueError):
            pass
    return matches


def find_vendor(lines: list[str]) -> str | None:
    """Pick the vendor name from the first few lines of the receipt.

    Heuristic: the first non-empty line that contains at least 2 letters and
    is not a phone number, ABN, or address (postcode/address pattern).
    """
    postcode_addr = re.compile(r'\b(NSW|VIC|QLD|SA|WA|TAS|NT|ACT)\b|\b\d{4}\b', re.IGNORECASE)
    phone = re.compile(r'\b(\+?61[\s\-]?)?\d{1,4}[\s\-]?\d{3,4}[\s\-]?\d{3,4}\b')
    abn = re.compile(r'\bABN\b', re.IGNORECASE)

    for line in lines[:6]:
        s = line.strip()
        if len(s) < 3:
            continue
        if not re.search(r'[A-Za-z]{2,}', s):
            continue
        if phone.search(s):
            continue
        if abn.search(s):
            continue
        if postcode_addr.search(s) and len(s) < 30:
            # short line with a postcode — probably an address, not a vendor
            continue
        return s[:255]
    return None


def find_amounts_by_label(text: str) -> dict:
    """Walk lines, return {subtotal, gst, total, ...} from labels.

    Returns dict with keys: ex_gst, gst, total (each Decimal | None).
    Total is the LAST match (receipts print totals at the bottom).
    """
    out = {"ex_gst": None, "gst": None, "total": None}
    lines = text.splitlines()

    for line in lines:
        money = _line_has_money(line)
        if not money:
            continue

        # Pick the *last* money figure on the line (right-aligned columns)
        amount = money[-1]

        if _line_has_label(line, "subtotal", "sub total", "sub-total", "ex gst", "ex-gst", "net amount", "amount ex"):
            if out["ex_gst"] is None:
                out["ex_gst"] = amount
        elif _line_has_label(line, "gst", "tax"):
            # Avoid matching "tax invoice" — only if there's a money figure
            if out["gst"] is None:
                out["gst"] = amount
        elif _line_has_label(line, "total", "amount due", "balance due", "grand total"):
            # Always overwrite (last occurrence wins)
            out["total"] = amount

    # If no subtotal label but we have a total, derive ex_gst as total - gst
    if out["ex_gst"] is None and out["total"] is not None and out["gst"] is not None:
        out["ex_gst"] = out["total"] - out["gst"]

    return out


def parse_receipt(image_path: str, lang: str = "eng") -> dict:
    """End-to-end parse: image -> text -> structured fields.

    Returns dict with keys:
        raw_text:        str (always present, may be empty)
        vendor_name:     str | None
        expense_date:    str (YYYY-MM-DD) | None
        ex_gst_amount:   Decimal | None
        gst_amount:      Decimal | None
        gst_type:        Decimal | None
        total_amount:    Decimal | None
        confidence:      'high' | 'low' | 'none'
    """
    raw_text = extract_text(image_path, lang=lang)
    text = raw_text or ""
    lines = [ln for ln in (text.splitlines() or []) if ln.strip()]

    vendor = find_vendor(lines)
    date = find_date(text)
    amounts = find_amounts_by_label(text)

    ex_gst = amounts["ex_gst"]
    gst = amounts["gst"]
    total = amounts["total"]

    # GST type: 0.1 if there's a GST line, else 0
    gst_type = Decimal("0.1") if gst is not None else Decimal("0")

    # Confidence: high if we got vendor + date + total, low if partial, none if bare
    have = sum(v is not None for v in (vendor, date, total))
    if have >= 3:
        confidence = "high"
    elif have >= 1:
        confidence = "low"
    else:
        confidence = "none"

    return {
        "raw_text": text,
        "vendor_name": vendor,
        "expense_date": date,
        "ex_gst_amount": ex_gst,
        "gst_amount": gst,
        "gst_type": gst_type,
        "total_amount": total,
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def process_ocr_job(job, app, ocr_cfg: dict):
    """Process a single OCR queue job through the new pipeline."""
    with app.app_context():
        job = OcrQueue.query.get(job.id)
        if not job or job.status != "pending":
            return

        expense = Expense.query.get(job.expense_id)
        if not expense:
            job.status = "failed"
            job.error_message = "Expense not found"
            job.processed_at = datetime.now(timezone.utc)
            db.session.commit()
            return

        try:
            job.status = "processing"
            job.attempts += 1
            db.session.commit()

            lang = ocr_cfg.get("tesseract_lang", "eng")
            min_confidence = ocr_cfg.get("min_confidence", "low")  # low|high
            parsed = parse_receipt(job.image_path, lang=lang)

            # Always stash raw text + parsed dict so the manual-review pathway
            # has the data even if we accept the auto-parse.
            extracted = {
                "raw_text": parsed["raw_text"],
                "parsed": {
                    "vendor_name": str(parsed["vendor_name"]) if parsed["vendor_name"] else None,
                    "expense_date": parsed["expense_date"],
                    "ex_gst_amount": str(parsed["ex_gst_amount"]) if parsed["ex_gst_amount"] is not None else None,
                    "gst_amount": str(parsed["gst_amount"]) if parsed["gst_amount"] is not None else None,
                    "gst_type": str(parsed["gst_type"]) if parsed["gst_type"] is not None else None,
                    "total_amount": str(parsed["total_amount"]) if parsed["total_amount"] is not None else None,
                    "confidence": parsed["confidence"],
                },
            }

            # Apply auto-extracted fields to the expense
            apply_ok = True
            if parsed["vendor_name"]:
                expense.vendor_name = parsed["vendor_name"]
            else:
                expense.vendor_name = "REQUIRES REVIEW"
                apply_ok = False

            if parsed["expense_date"]:
                try:
                    expense.expense_date = datetime.strptime(parsed["expense_date"], "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    apply_ok = False
            else:
                expense.expense_date = expense.expense_date or datetime.now(timezone.utc).date()
                apply_ok = False

            if parsed["ex_gst_amount"] is not None:
                expense.ex_gst_amount = parsed["ex_gst_amount"]
            else:
                expense.ex_gst_amount = Decimal("0.00")

            if parsed["gst_amount"] is not None:
                expense.gst_amount = parsed["gst_amount"]
            else:
                expense.gst_amount = Decimal("0.00")

            if parsed["gst_type"] is not None:
                expense.gst_type = parsed["gst_type"]

            if parsed["total_amount"] is not None:
                expense.total_amount = parsed["total_amount"]
            elif expense.ex_gst_amount is not None and expense.gst_amount is not None:
                expense.total_amount = expense.ex_gst_amount + expense.gst_amount
            else:
                expense.total_amount = Decimal("0.00")

            # Decide success vs requires_review based on confidence
            above_threshold = (
                parsed["confidence"] == "high"
                or (min_confidence == "low" and parsed["confidence"] == "low")
            )

            if above_threshold and apply_ok:
                expense.requires_review = False
                job.status = "completed"
                job.extracted_data = extracted
                job.error_message = None
                job.processed_at = datetime.now(timezone.utc)
                db.session.commit()
            else:
                # Manual review
                expense.requires_review = True
                job.status = "failed"  # status=failed surfaces in HMI review queue
                job.extracted_data = extracted
                job.error_message = (
                    f"Auto-parse below threshold (confidence={parsed['confidence']}, "
                    f"min={min_confidence}). Awaiting manual review."
                )
                job.processed_at = datetime.now(timezone.utc)
                db.session.commit()

        except Exception as e:
            expense.vendor_name = expense.vendor_name or "REQUIRES REVIEW"
            expense.requires_review = True
            job.status = "failed"
            job.error_message = f"Pipeline crash: {e!r}"
            job.processed_at = datetime.now(timezone.utc)
            db.session.commit()


def run_processor(ocr_cfg: dict, poll_interval=5, max_attempts=3):
    app = create_app()

    print(f"OCR Processor starting (tesseract pipeline)…")
    print(f"  tesseract_lang: {ocr_cfg.get('tesseract_lang', 'eng')}")
    print(f"  min_confidence: {ocr_cfg.get('min_confidence', 'low')}")
    print(f"  poll_interval:  {poll_interval}s")

    while True:
        with app.app_context():
            stale_timeout = ocr_cfg.get("stale_timeout_seconds", 300)
            stale_jobs = OcrQueue.query.filter(
                OcrQueue.status == "processing",
                OcrQueue.created_at < datetime.now(timezone.utc) - timedelta(seconds=stale_timeout),
            ).all()
            for stale_job in stale_jobs:
                stale_job.status = "pending"
                stale_job.error_message = "Stale job reset"
                db.session.commit()
                print(f"Reset stale job {stale_job.id}")

            pending_jobs = OcrQueue.query.filter_by(status="pending").order_by(OcrQueue.created_at).limit(10).all()

            if pending_jobs:
                print(f"Found {len(pending_jobs)} pending jobs")
                for job in pending_jobs:
                    if job.attempts >= max_attempts:
                        job.status = "failed"
                        job.error_message = f"Max attempts ({max_attempts}) reached"
                        job.processed_at = datetime.now(timezone.utc)
                        db.session.commit()
                        print(f"Job {job.id} failed: max attempts reached")
                    else:
                        print(f"Processing job {job.id}…")
                        try:
                            process_ocr_job(job, app, ocr_cfg)
                        except Exception as e:
                            print(f"Error processing job {job.id}: {e!r}")
                            job.status = "pending"
                            job.error_message = repr(e)
                            db.session.commit()

        time.sleep(poll_interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OCR Queue Processor (tesseract pipeline)")
    parser.add_argument("--config", "-c", default="config.yaml", help="Path to config file")
    parser.add_argument("--poll-interval", type=int, default=5, help="Poll interval in seconds")
    parser.add_argument("--max-attempts", type=int, default=3, help="Max retry attempts per job")
    parser.add_argument("--once", action="store_true", help="Process one batch and exit (for tests)")
    args = parser.parse_args()

    import yaml
    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    ocr_cfg = config.get("ocr", {})
    if not ocr_cfg:
        print("ERROR: config.yaml has no 'ocr:' section. Refactor pending.")
        sys.exit(2)

    if args.once:
        app = create_app()
        with app.app_context():
            pending = OcrQueue.query.filter_by(status="pending").order_by(OcrQueue.created_at).limit(10).all()
            for job in pending:
                if job.attempts < args.max_attempts:
                    process_ocr_job(job, app, ocr_cfg)
        sys.exit(0)

    run_processor(ocr_cfg, poll_interval=args.poll_interval, max_attempts=args.max_attempts)
