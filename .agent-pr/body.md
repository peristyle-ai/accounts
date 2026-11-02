## What

The accounts test suite runs against **live production Postgres** — there
is no test database. So a row a test creates and does not delete is a row
in the business's ledger, visible in the customer list and the invoice
list.

It happened. **1,029 junk customers and 8 junk invoices** had accumulated
in production from the address-refactor and invoice-tax suites. The
production business address was also overwritten with a test placeholder
(`99 New St, Brisbane, QLD 4000`) — twice, on 2026-11-02 and
2026-11-03 — and **the tests passed both times**.

## The three changes

| file | change |
|---|---|
| `tests/conftest.py` | `restore_user` also snapshots and restores the **profile** row; a session-scoped autouse guard sweeps residue; the guard **asserts** the business address is unchanged and fails the session loudly if it is not |
| `tests/test_prod_residue_guard.py` | **new**: covers the guard's rules *and its safety property* |

### 1. `restore_user` never reached the address

The business ADDRESS lives in `accounts.user_profiles`, not in the `users`
table: `User.address_line1` is a **property** that reads through the
profile. So snapshotting `User.__table__.columns` snapshots *nothing* of
the address, and a test that mutates it leaves production holding
whatever the test wrote.

The fixture was introduced to stop exactly this corruption, and its own
docstring describes it. It did not reach the address, because the address
had already moved out of the table it snapshotted.

### 2. A session-scoped guard, not per-test cleanup

The guard is **session-scoped and autouse**, so it runs for every test
session — including one that runs a single file. A per-test cleanup is
only as good as the tests that remembered to write one, and the leaking
tests predate the rule.

It recognises residue by the **test-name prefixes the suite uses**, never
by a real customer's name, and it refuses to sweep anything that is not
unmistakably a draft — a sent or paid invoice is business state whatever
it is called. It also sweeps the audit rows that recorded the test
writes (the log grew 164 rows per run, recording creations of rows that
no longer exist), under a deliberately narrow rule: a log row goes only
if the row it describes did not exist before this session **and** does not
exist now — so a real business action taken during the session keeps its
audit trail.

### 3. The guard asserts the address

The corruption passed twice because nothing checked. The guard compares
the business address before and after the session and **fails the session
loudly** if it changed.

## Proven by mutation, not by inspection

- **Neuter the residue matcher** → exactly **5 customers leaked into the
  live database** during a normal run. The suite still passed. The guard
  is what prevents the leak, which is why this needed proving.
- **Disable the profile restore** → the guard's assertion fired with the
  exact corruption (`99 New St, Brisbane, QLD 4000`) and failed the
  session.

## The guard's own safety property is tested

A guard that deletes real business data is worse than no guard. The tests
assert that **no real customer and no real invoice** can be recognised as
residue, and that a sent/paid invoice is never swept.

The real names are read from the **live database at runtime**, not written
here: this repository is **public**, and a hardcoded list of customer
names in a tracked file is the same mistake this branch exists to prevent.
Reading them live is also the stronger test — it covers every real
customer, including any added later.

## Verification

- **496 passed, 2 skipped** on the deployed code (521 on the branch).
- Production verified **unchanged** after a full run: 5 customers, 21
  invoices, 3 quotes, business address intact.
- The identifier gate and the secret scan are green on the branch.

## Not in this PR

The history rewrite of the published `main` (a pre-scrub name still
carried by 14 of its commits) is a separate decision and is **not**
touched here.
