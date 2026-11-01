## What

One commit. It does two things: it removes two identifiers that were on
a public page, and it adds the gate that should have stopped them.

| file | change |
|---|---|
| `app/hmi/routes.py` | a customer's UNIX ACCOUNT, in a comment about receipt previews |
| `app/api/routes.py` | two CRM names, in comments about `project_code` / `project_name` |
| `scripts/check_no_identifiers.py` | **new**: the identifier gate |
| `scripts/pre-push-guard` | the guard now scans the tree being PUSHED |
| `scripts/pre_push_guard_selftest.sh` | a ninth case: a structurally valid branch carrying an identifier is REFUSED |
| `.github/workflows/tests.yml` | a CI step beside the secret scan |
| `tests/test_no_identifiers.py` | **new**: 15 tests |

## The identifiers

A credential is secret-shaped, so the secret scan finds it. A person's
or a customer's NAME is not secret-shaped, so nothing finds it — and the
name sits on a public page until somebody reads the diff.

Both of these were in COMMENTS, which is the part nobody scrubs. They
are now described without the names: `<upload root>`, "a CRM", "the
CRM". The explanation is unchanged, because the names added nothing to
it.

## The gate

`scripts/check_no_identifiers.py` refuses:

* a trading name with a corporate suffix,
* an email at a domain that is not a reserved placeholder,
* a unix account in a path,
* a name-shaped pair of words in a contact context,
* a proper name after "the work:" — the exact sentence that started this.

It **asserts its own input** (with no files to scan it fails loudly
rather than reporting zero findings), it has a **self-test**, and each
rule is shown to fire. Its own samples are assembled at RUNTIME from
parts: a regression fixture that spelled the name out would put the
name back into a tracked file — the very defect the gate exists to
catch. The source of the gate must not trip the gate.

## Why the guard changed

The pre-push guard proved which refs may be pushed and that the history
extends the published `main`. It could not see a name inside a file —
structure is not content. It now scans the tree being PUSHED, not the
working tree, because a branch can be checked out at something else
entirely and what is published is the tree of the commit being pushed.

The self-test gained the case that matters: a branch that is a pure
extension of the published `main` — so every ancestry and parents rule
passes — but whose tree carries an identifier is REFUSED. Only a
content check can refuse it, which is exactly why it needed one.

## Verification

* `scripts/check_no_identifiers.py` — GREEN on this tree, and it
  **FAILS (exit 1) on the pre-scrub tree it was written for**. A check
  that has never been shown to fail is not evidence.
* `scripts/pre_push_guard_selftest.sh` — 9 cases, 9 passed.
* `pytest` — 430 passed, 4 skipped.

## Still open (not this PR)

The published `main`'s HISTORY carries an identifier in its root commit,
and a tree fix cannot reach it. That is the separate, larger job — a
repository replace — and it is not something a branch can do.
