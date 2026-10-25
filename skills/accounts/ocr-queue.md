import os
# OCR Queue

Receipt → OCR pipeline → Expense. The PWA receipt-capture uploads photos that go into the `ocr_queue` table; a background process runs tesseract + regex parser to extract vendor/amount/date; on success it creates the Expense and marks the job `completed`. On low-confidence failure, the job is flagged for **manual review** by an operating agent (via native vision).

**Why tesseract?** Originally this used a remote vision LLM. Switched 2026-08-04 to local tesseract + regex parser. Tesseract is fast + free but fails on:
- Thermal-printed receipts (low contrast, faded)
- Handwritten receipts
- Curled/crumpled paper
- Photos with strong shadows

When more than ~3 OCR jobs fail in a row, fall back to native vision and update expenses manually via `/api/expenses/<id>` PUT.

## Schema

```
ocr_queue
  id              UUID PK
  expense_id      FK -> expenses.id (the expense created from the PWA upload)
  image_path      server path to the uploaded photo
  status          'pending' | 'processing' | 'completed' | 'failed'
  extracted_data  JSONB: {raw_text, parsed: {vendor, amount, date, gst}, confidence}
  error_message   error details on failure
  attempts        int (default 0)
  created_at
  processed_at    when status changed from pending/processing
```

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/ocr-queue[?needs_review=1][&status=pending]` | List jobs |
| GET | `/api/ocr-queue/<id>` | Get one (with raw_text + parsed dict) |
| POST | `/api/ocr-queue/<id>/mark-completed` | Mark done after manual review |

## Manual review workflow (when OCR fails)

```python
import requests
from pathlib import Path

# Base URL is read from ACCOUNTS_BASE_URL / PY_PG_ACCOUNTS_BASE_URL
API = os.environ.get("ACCOUNTS_BASE_URL") or os.environ.get("PY_PG_ACCOUNTS_BASE_URL") or "http://127.0.0.1:5061"
KEY = "..."

# 1. List jobs needing manual review
jobs = requests.get(
    f"{API}/api/ocr-queue",
    params={"needs_review": "1"},
    headers={"X-API-Key": KEY},
).json()["jobs"]

# 2. For each job, get the raw text + image path
for job in jobs:
    detail = requests.get(
        f"{API}/api/ocr-queue/{job['id']}",
        headers={"X-API-Key": KEY},
    ).json()["job"]
    print(f"Image: {detail['image_path']}")
    print(f"OCR text: {detail['extracted_data']['raw_text'][:300]}")

# 3. Agent inspects the image via its vision tool, then PATCHes the expense
requests.put(
    f"{API}/api/expenses/{job['expense_id']}",
    headers={"X-API-Key": KEY},
    json={
        "vendor_name": "Ipswich Waste Services",
        "ex_gst_amount": 19.09,
        "gst_amount": 1.91,
        "gst_type": 0.1,
        "total_amount": 21.00,
        "expense_date": "2026-04-28",
    },
)
# PUT auto-clears requires_review=False when the supplied vendor_name is real.

# 4. Mark the OCR job done
requests.post(
    f"{API}/api/ocr-queue/{job['id']}/mark-completed",
    headers={"X-API-Key": KEY},
)
```

## From the skill class

```python
from skills.accounts.accounting_skill import AccountingSkill
skill = AccountingSkill(api_key="...")

# List jobs needing review
jobs = skill.list_ocr_jobs(needs_review=True)

# Get details
for j in jobs:
    detail = skill.get_ocr_job(j['id'])
    print(f"Image: {detail['image_path']}")
    print(f"Raw text: {detail['extracted_data']['raw_text'][:300]}")

# After editing the expense via update_expense() (which clears requires_review):
skill.mark_ocr_job_completed(j['id'])
```

## The `requires_review` flag on Expense

The Expense has a `requires_review: bool` field. It's set True when:
- OCR creates an expense but confidence is below threshold
- The expense is marked with `vendor_name='REQUIRES REVIEW'` as a placeholder

PUT to `/api/expenses/<id>` with a real `vendor_name` auto-clears it to False. So the manual-review loop is:

1. OCR fails → expense created with `vendor_name='REQUIRES REVIEW'`, `requires_review=True`, OCR job in queue with `needs_review=1`
2. Agent (you!) sees the image via vision, parses the receipt
3. Agent PATCHes the expense with real vendor/amount/etc → `requires_review=False`
4. Agent POSTs `/api/ocr-queue/<id>/mark-completed` → job removed from review queue

## Cron-driven fallback: native vision

`scripts/process_accounts_email.py` runs every 30 min via cron (per `memory/2026-08-31`). It also reads the accounts inboxes, but **does not retry OCR** — OCR retries are handled by the `accounts` service's own queue worker.

For OCR specifically, the fallback is when >3 consecutive jobs fail with low confidence. The skill class doesn't do this fallback automatically — it's an agent-driven workflow.

## Pipeline state diagram

```
PWA upload ──> status=pending
                  │
                  ▼
              tesseract OCR + regex
                  │
        ┌─────────┴─────────┐
        ▼                   ▼
   confidence >= threshold   confidence < threshold
        │                   │
        ▼                   ▼
   status=completed      status=failed
   expense created       expense created with
   with real vendor      vendor_name='REQUIRES REVIEW',
                         requires_review=True
                         │
                         ▼
                    needs_review=1
                    ┌────────────────┐
                    │ agent reviews  │
                    │ via vision     │
                    └────────────────┘
                         │
                         ▼
                    PUT /api/expenses/<id> with real data
                         │
                         ▼
                    POST /api/ocr-queue/<id>/mark-completed
```

## Common gotchas

- **`status='processing'`** jobs are stuck in flight. If a job has been processing for >2 min, it's probably crashed. Safe to POST `mark-completed` to clean up (but you lose the raw_text — only do this if you've manually fixed the expense already).
- **`attempts` counter** — every retry of tesseract bumps this. Don't retry more than 3 times; after that the receipt is probably unreadable.
- **Image path is server-side** (`/path/to/uploads/ocr/<id>.jpg` or similar). To view it, you need to fetch via `/api/expenses/<expense_id>/attachment` or directly from the server filesystem.

## Related

- See `SKILL.md` for general skill setup
- See the operating agent's workspace `MEMORY.md` (not part of this repository) for the cron schedule and the overall expense pipeline
