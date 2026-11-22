## What

`POST /api/expenses` answered **HTTP 500 on every call**, on `main` as
well as on every open branch.

The route has passed `project_code` / `project_name` / `attachment_sha256`
to the `Expense` constructor since 2026-09-24 and 2026-09-25. The ORM
model never grew those attributes and **no migration ever created the
columns**, so SQLAlchemy rejected the unmapped kwargs:

```
TypeError: 'project_code' is an invalid keyword argument for Expense
```

The route is the documented way to add an expense — the PWA's "add
expense" button and the mail intake both POST to it — so this takes out
expense creation for the whole installation, not just one caller.

## The fix

- `app/models/expense.py` grows the three columns (nullable).
- `schema/migrations/2026-11-23_add_expense_project_fields.sql` creates
  them in the `accounts` schema (nullable, additive, idempotent).

The route is unchanged: it already validated and meant to store these
fields, and nothing else in the app referenced them.

## Evidence

Found by running the mail intake against the live mailbox: ten
`POST /api/expenses` calls, ten HTTP 500s. The store was read BEFORE
retrying anything — the row count was unchanged, so the 500 meant "no
write", not "a write that needs repeating".

After the migration was applied (against a `pg_dump` backup of the
`accounts` schema) and the service restarted, the same ten calls created
ten expenses, and the public `/health` returns 200.

`tests/test_expense_create_route.py` pins it, and the check is
**structural rather than a snapshot**: every kwarg the route passes to the
constructor must be a mapped attribute of the model, so the next field
added to the route fails the test until the model and the database grow
to match. The defect was reintroduced and the tests were shown to fail
(2 failed) before the fix was restored.

## Notes for review

- The migration is applied to the live database already. It is additive
  and idempotent, so re-running it is a no-op.
- `accounts.expenses` gains three nullable columns and two indexes; no
  other table is touched.
- This branch is stacked on `fix/payroll-reopen-remote-addr` (which is
  itself stacked on the payroll and invoice branches), because the
  production checkout is on that stack and the payroll timer runs from
  the checkout. **Do not move the production checkout to `main` until
  the payroll PR is merged** — `main` still carries the hardcoded
  five-day payroll defaults.
