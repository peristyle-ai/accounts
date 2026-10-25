# accounts AI Agent Skill

A structured Python interface for AI agents to interact with the accounts REST API.

## What's here

```
skills/accounts/
  README.md              - this file
  SKILL.md               - lean overview (start here)
  accounting_skill.py    - the AccountingSkill class (importable)
  BAS.md                 - BAS lodgement tracking workflow
  bank-transactions.md   - received payments + bank provenance workflow
  statements.md          - Statement of Account (PDF + JSON) workflow
  payroll.md             - employees, pay events, super payments workflow
  ocr-queue.md           - OCR pipeline manual-review workflow
```

## Quick start

```python
from skills.accounts.accounting_skill import AccountingSkill

skill = AccountingSkill(api_key="...")  # default base_url: http://127.0.0.1:5061 (OSS upstream)
expense = skill.create_expense(vendor_name="Acme", ex_gst_amount=100.00, expense_date="2026-03-15")
```

## Configuration

Default base URL is `http://127.0.0.1:5061` (local development; the OSS upstream default). Override via:

1. Constructor parameter: `AccountingSkill(api_key="...", base_url="https://your-deployment.example.com")`
2. Environment variable: `PY_PG_ACCOUNTS_BASE_URL=https://your-deployment.example.com`

## Documentation

Read `SKILL.md` for the overview. Each topic-specific `.md` file covers one feature area with workflows and field-by-field reference.

## Versioning

This skill matches the accounts API. When new endpoints are added to accounts, update the corresponding methods in `accounting_skill.py` and the topic docs.
