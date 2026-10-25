#!/usr/bin/env python3
"""
OCR Queue — Manual Review Helper (2026-08-04 refactor)

Workflow for an operating agent picking up low-confidence OCR jobs:

    1. Run this script to list pending manual-review jobs.
    2. For each job, the script prints:
         - the expense_id
         - the embedded raw_text + parsed dict (from ocr_queue.extracted_data)
         - the local image path
    3. The agent uses an image-capable tool (e.g. minimax native vision) to
       inspect the image at the printed path.
    4. The agent fetches the full expense and patches the corrected fields
       via the API:
           PATCH /api/expenses/<id>   {vendor_name:..., ex_gst_amount:..., ...}
       (and the API has already wired clear_requires_review=True support;
        see app/api/routes.py — needs updating if not present)
    5. After patching, the script can mark the OcrQueue job "completed" so
       it stops showing in the review queue.

This script is READ-ONLY by default. It does not touch the database unless
--mark-completed is passed.

Usage:
    python3 scripts/review_ocr_queue.py list            # show pending review jobs
    python3 scripts/review_ocr_queue.py show <job_id>   # show one job detail
    python3 scripts/review_ocr_queue.py mark-completed <job_id>
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from app import create_app
from app.models import db
from app.models.expense import Expense
from app.models.ocr_queue import OcrQueue


def list_pending():
    app = create_app()
    with app.app_context():
        # Failed jobs that have raw_text in extracted_data are the manual-review pool
        failed = OcrQueue.query.filter_by(status="failed").order_by(OcrQueue.created_at.desc()).all()
        review = []
        for j in failed:
            data = j.extracted_data or {}
            if not isinstance(data, dict):
                continue
            if "raw_text" not in data:
                continue
            exp = Expense.query.get(j.expense_id)
            review.append((j, exp))
        if not review:
            print("No manual-review jobs pending.")
            return
        print(f"\n{len(review)} job(s) awaiting manual review:\n")
        for j, exp in review:
            print(f"  job_id      {j.id}")
            print(f"  expense_id  {j.expense_id}")
            print(f"  vendor      {exp.vendor_name if exp else '?'}")
            print(f"  date        {exp.expense_date if exp else '?'}")
            print(f"  total       {exp.total_amount if exp else '?'}")
            print(f"  image       {j.image_path}")
            print(f"  confidence  {(j.extracted_data or {}).get('parsed', {}).get('confidence', '?')}")
            print(f"  reason      {j.error_message}")
            print()


def show_job(job_id: str):
    app = create_app()
    with app.app_context():
        job = OcrQueue.query.get(job_id)
        if not job:
            print(f"No job with id {job_id}")
            sys.exit(1)
        exp = Expense.query.get(job.expense_id)
        print(json.dumps({
            "job": job.to_dict(),
            "expense": exp.to_dict() if exp else None,
            "raw_text": (job.extracted_data or {}).get("raw_text", ""),
            "parsed": (job.extracted_data or {}).get("parsed", {}),
        }, indent=2, default=str))


def mark_completed(job_id: str):
    app = create_app()
    with app.app_context():
        job = OcrQueue.query.get(job_id)
        if not job:
            print(f"No job with id {job_id}")
            sys.exit(1)
        job.status = "completed"
        job.processed_at = datetime.now(timezone.utc)
        if job.expense:
            job.expense.requires_review = False
        db.session.commit()
        print(f"Job {job_id} marked completed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OCR manual-review helper")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List pending manual-review jobs")

    p_show = sub.add_parser("show", help="Show one job in detail")
    p_show.add_argument("job_id")

    p_done = sub.add_parser("mark-completed", help="Mark job done after manual review")
    p_done.add_argument("job_id")

    args = parser.parse_args()
    if args.cmd == "list":
        list_pending()
    elif args.cmd == "show":
        show_job(args.job_id)
    elif args.cmd == "mark-completed":
        mark_completed(args.job_id)
