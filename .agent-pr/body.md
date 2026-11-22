## What

`POST /api/payroll/pay-events/<id>/reopen` answered **HTTP 500 on every
call**. It wrote its activity-log row with `request.remote_address`, and
Flask's `Request` object has no such attribute -- the attribute is
`remote_addr`. Reading the missing attribute raised `AttributeError`
before the response was built.

The route is the documented way to correct a prepared payroll week
(`POST /api/payroll/pay-events/<id>/reopen` -> `PUT` -> `finalize` ->
`mark-paid`). With it broken, a prepared week that needs correcting could
be neither corrected through the app nor cleanly refused -- which is how
it was found: a week whose payment date and bank reference had to be
corrected to match the bank's record.

## The part that mattered more than the 500

The two commits were in the wrong order. The status change was committed
to the database FIRST and the activity row was written and committed
SECOND:

```python
pe.status = 'draft'
db.session.commit()          # <- the row is already changed on disk
log_activity(..., ip_address=request.remote_address)   # <- raises
db.session.commit()
```

So the 500 did not mean "nothing happened". It meant **the pay event had
already moved to `draft`** while the caller was told the call had failed.
A caller that trusted the 500 would retry -- and get a 409, because the
event was no longer `finalized`. The endpoint's answer and the store
disagreed, which is the same failure shape as "HTTP 200 does not mean
the data was written", with the sign reversed: here a 500 did not mean
the data was NOT written.

The status change and the activity row are now committed together, in
one commit, so the answer and the store agree either way.

## How it is verified

`tests/test_payroll_reopen_route.py` exercises the route through a real
Flask request context -- the bug lived in a Flask `Request` attribute, so
a test that only read the source would not have caught it. Four cases:

1. the route answers 200 and moves the event to `draft`;
2. the activity row carries the caller's real IP (`remote_addr`), not
   `None`;
3. if the activity write raises, the status change is NOT committed --
   one commit for the whole operation;
4. a non-`finalized` event is still refused with 409.

Both defects were **mutation-tested**: restoring `request.remote_address`
fails three of the four tests, and moving the commit back ahead of the
activity write fails the third. A check that has never been shown to
fail is unproven, not passing.

The full suite passes: **586 passed, 2 skipped**, against live prod
Postgres, with the row counts unchanged and the business address intact.

## Notes for review

- The change is on `app/api/payroll.py` only, plus the new test file.
- `request.remote_address` appears nowhere else in `app/` or `scripts/`
  (verified by grep); the sibling routes already used `remote_addr`.
- This branch is stacked on the payroll-config branch, so its diff
  against `main` includes the stacked changes. **The reopen fix itself is
  the 11-line change in `app/api/payroll.py` plus the new test file.**
