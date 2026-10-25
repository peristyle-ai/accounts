# BAS Lodgements

Records the **fact** of BAS (Business Activity Statement) lodgement with the ATO: receipt ID, timestamp, account name, final settled amount, and any manual adjustments.

**Why a separate model:** The BAS report endpoint (`/api/reports/quarterly-bas`) returns the **computed** net GST (1A − 1B). The actual amount ATO settles often differs due to prior-period credits, instalment interest, label adjustments, deferred BAS, etc. We need to record the **final settled amount** separately for reconciliation.

This is an audit/reconciliation table — it's separate from the invoice and expense tables, but cross-references the same period.

## Schema (22 columns)

```
bas_lodgements
  id                  UUID PK
  user_id             FK -> users.id
  financial_year      e.g. 'FY2025/26'
  quarter             1..4 (Australian FY)
  period_start        first day of quarter
  period_end          last day of quarter
  ato_receipt_id      e.g. '9021291175'
  ato_account_name    e.g. '<your ATO account name as shown on the BAS>'
  lodged_at           ISO-8601 datetime when lodged
  lodgement_method    'online' | 'paper' | 'agent' (default 'online')
  gst_collected       BAS label 1A (snapshot at lodgement)
  gst_paid            BAS label 1B (snapshot at lodgement)
  computed_net_gst    1A - 1B before ATO adjustments
  final_amount        amount actually settled
  final_amount_type   'credit' | 'owe' | 'zero'
  prior_credit_carried credit brought from prior BAS (default 0)
  other_adjustments   manual adjustments (default 0)
  adjustments_note    explanation of adjustments
  notes               free-text
  screenshot_path     optional ATO confirmation screenshot
  created_at, updated_at
```

## API

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/bas-lodgements` | Record a new lodgement |
| GET | `/api/bas-lodgements` | List (filter FY/quarter) |
| GET | `/api/bas-lodgements/<id>` | Get one |
| PUT | `/api/bas-lodgements/<id>` | Update notes/adjustments |
| DELETE | `/api/bas-lodgements/<id>` | Delete (admin only) |

## Australian Financial Year

| Q | Period | FY label example |
|---|---|---|
| 1 | Jul-Sep | e.g. FY2025/26 Q1 |
| 2 | Oct-Dec | |
| 3 | Jan-Mar | (next CY) |
| 4 | Apr-Jun | (next CY) |

So **Apr-Jun 2026 = FY2025/26 Q4** — pass `financial_year='FY2025/26'`, `quarter=4`.

## Common workflows

### End-of-quarter: record BAS lodgement after submitting to ATO

```python
from datetime import datetime
from skills.accounts.accounting_skill import AccountingSkill

skill = AccountingSkill(api_key="...")

# 1. Get the COMPUTED figures from the BAS report
report = skill.get_quarterly_bas_report(year=2025, quarter=4)
gst_collected = report['gst_collected']     # 1A
gst_paid = report['gst_paid']                # 1B
computed_net_gst = report['gst_owing']       # 1A - 1B (positive = owe, negative = refund)

# 2. After lodging via myGov/BAS agent and getting the ATO receipt,
#    record the FACT of lodgement with snapshot of computed + actual settled.
lodgement = skill.create_bas_lodgement(
    financial_year="FY2025/26",
    quarter=4,
    period_start="2026-04-01",
    period_end="2026-06-30",
    lodged_at=datetime.utcnow().isoformat() + "Z",
    final_amount=abs(computed_net_gst),       # what ATO actually settled
    final_amount_type="owe" if computed_net_gst > 0 else (
        "credit" if computed_net_gst < 0 else "zero"
    ),
    ato_receipt_id="9021291175",              # from the ATO confirmation page
    ato_account_name="<your ATO account name as shown on the BAS>",
    lodgement_method="online",
    gst_collected=gst_collected,
    gst_paid=gst_paid,
    computed_net_gst=computed_net_gst,
    notes="Submitted via myGov BAS agent portal.",
)
```

The triple `gst_collected`/`gst_paid`/`computed_net_gst` is a **snapshot** at lodgement time — if we later restate an invoice or expense that affects this period, the snapshot still reflects what was on the BAS at submission, so reconciliation stays auditable.

### Record lodgement with manual adjustments

If the ATO settlement differs from the computed figure (e.g. they applied a prior-period credit, or you had to amend label 1B):

```python
lodgement = skill.create_bas_lodgement(
    financial_year="FY2025/26",
    quarter=3,
    period_start="2026-01-01",
    period_end="2026-03-31",
    lodged_at="2026-04-28T10:15:00+10:00",
    final_amount=4250.00,
    final_amount_type="owe",
    ato_receipt_id="9021291234",
    gst_collected=5500.00,
    gst_paid=850.00,
    computed_net_gst=4650.00,                  # what we expected to owe
    prior_credit_carried=400.00,              # ATO applied a Q2 credit
    other_adjustments=0.00,
    adjustments_note="Q2 credit of $400 applied per ATO statement.",
    notes="Reconciled with ATO portal statement.",
)
```

`final_amount` should equal `computed_net_gst - prior_credit_carried - other_adjustments` for a clean reconciliation. If it doesn't, that's a signal to investigate.

### List all lodgements for a financial year

```python
fy_lodgements = skill.list_bas_lodgements(financial_year="FY2025/26")
for lod in fy_lodgements['lodgements']:
    print(f"Q{lod['quarter']}: ${lod['final_amount']} ({lod['final_amount_type']}) - "
          f"lodged {lod['lodged_at']}")
```

### Update notes after lodgement

If you realise you made a typo in the `notes` field or want to record a reconciliation finding:

```python
skill.update_bas_lodgement(
    lodgement_id="...",
    notes="Reconciled against bank statement on 2026-05-15. Settlement matched.",
    adjustments_note="Q2 credit applied - confirmed with ATO 13XXXX",
)
```

Mutable fields: `notes`, `adjustments_note`, `other_adjustments`, `screenshot_path`. The financial year, quarter, period dates, and computed/settled amounts are lodgement provenance — if wrong, delete and re-record.

## Uniqueness

The DB enforces one lodgement per `(user_id, financial_year, quarter)` via the unique index `idx_bas_lodgements_user_fy_quarter`. POST returns 409 if you try to record two for the same period. If you genuinely need to amend, delete the old one first then create the new one.

## Reconciliation pattern

After recording all lodgements for a FY, compare against the bank statement:

```python
fy = "FY2025/26"
lodgements = skill.list_bas_lodgements(financial_year=fy)['lodgements']

# Get all bank outflows in the BAS payment date range
bas_payments = skill.list_bank_transactions(date_from="2026-07-01", date_to="2026-10-28")

# Match lodgement amounts against bank payments (ATO debits)
for lod in lodgements:
    expected_amount = float(lod['final_amount'])
    matched = False
    for bp in bas_payments['bank_transactions']:
        if (bp['payer_name'] and 'ATO' in bp['payer_name'].upper()
                and abs(float(bp['amount']) - expected_amount) < 0.01):
            matched = True
            break
    if not matched:
        print(f"Q{lod['quarter']}: ${expected_amount} - NO MATCHING BANK PAYMENT FOUND")
```

## Related

- See `bank-transactions.md` for recording received customer payments
- See `SKILL.md` for general skill setup
- `get_quarterly_bas_report()` in the main skill returns the COMPUTED figures
