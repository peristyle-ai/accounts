## What

Five commits. Two of them are the payroll fix; one is the pre-push guard
widening; two came out of the review that opened this pull request.

| commit | change |
|---|---|
| `db5ba08` | the super ABA populates the trace account and the name of remitter |
| `3f9ecab` | AMOUNT and LODGEMENT_REF are REQUIRED for the super ABA — no defaults |
| `098b832` | the pre-push guard allows PR branches to the public remote, under a proof |
| `160bcab` | **review**: no payroll values quoted in prose in a PUBLIC repository |
| `09794e8` | **review**: the super lodgement reference is deployment data, not a constant |

## The ABA fix

`build_detail_record()` left positions 88-96 (trace account number) and
97-112 (name of remitter) blank, and wrote blanks into 113-120 (amount of
withholding tax). The trace account is what the bank reconciles the debit
against — a file with a blank trace account cannot be tied back to the
payer's own account. The withholding field is now zero-filled, which is
what "not applicable" means in that format.

## No defaults for values that reach a bank

`scripts/gen_super_aba.py` defaulted `AMOUNT` and `LODGEMENT_REF`. The
default amount happened to be right for one pay run, and the default
reference was a previous month's artefact — so a run that forgot to pass
them wrote a stale reference into a file destined for a fund. Both are now
required, and the refusal happens BEFORE the app is built, so the check
needs no database.

Two directions of check, because either alone can be fooled:

- **STATIC** — parsed with `ast`, so the script may not read a REQUIRED
  variable with a default in any form.
- **BEHAVIOURAL** — running the script with the variable unset must fail
  loudly, name the variable, and write no artefact.

## The pre-push guard, widened under a proof

GitHub's branch protection refuses a direct push to main, so the only
publish path left is a branch plus a pull request — and the guard refused
every ref except main, which meant it refused the only path GitHub
accepts. It now allows `feat/*`, `fix/*` and `publish/*` **under a proof**:
every commit the push would add must have all of its parents either
already in the published main, or already among the commits being added.

The first version of that rule asked only whether the branch DESCENDED
from the published main, and the self-test caught the hole: a merge
commit whose second parent is a foreign root still descends from the
published main while dragging that lineage into the public repository. The
rule is now stated over PARENTS, not over ancestry. The self-test
(`scripts/pre_push_guard_selftest.sh`) exercises every rule, including the
merge-with-a-foreign-root case — a guard that has never been shown to
refuse anything is not a guard.

## What the review changed

Two findings, both about the same thing: **this repository is public**.

1. The fix removed a payroll value from the CODE and quoted it in the
   comment that explains why. Moving a lodgement reference naming an
   employee out of the code and into a comment is still publishing it. The
   prose now describes the values instead of quoting them, and the check
   that guards against a stale reference now matches the **shape** of one
   (`SUPER <digits>`) rather than one specific value — stricter than
   before, and carrying no personal data.
2. `scripts/run_weekly_payroll.py` hardcoded an employee's name in the
   lodgement reference it generates for every weekly run — published since
   the initial release. The employer's ABN a few lines above already
   follows the right rule (read from `config.yaml`, not hardcoded), so the
   reference now follows it: `config.yaml` (`stp.lodgement_ref_prefix`),
   then `SUPER_LODGEMENT_PREFIX` in the environment, then the employee's
   own `bank_reference_prefix`, then `JP`. A deployment that wants a
   particular wording states it in its own config; one that says nothing
   gets the employee's own prefix rather than a name out of this
   repository.

> **Operator note:** if the exact previous wording matters to the fund,
> set `stp.lodgement_ref_prefix` in your `config.yaml` before the next
> run. It is documented (blank) in `config.yaml.example`.

## Verification

- Full suite: **348 passed, 71 skipped** (the skips are "no users in the
  database yet" fixtures — a fresh install), run locally against a
  PostgreSQL database with the CI's own environment variables.
- The repo's own secret-scan gate (`scripts/check_no_secrets.py`, the
  synchronous CI job): **no secret found** — working tree, full history,
  commit messages.
- The guard's self-test: **8 passed, 0 failed**.
- The guard was installed as the hook for the push of the review commits
  and allowed them: the branch is a pure extension of the published main.

## Not included

No change to the amount, the reference format, the fund or the bank.
No change to the payroll maths — the withholding field moves from blank to
zero-filled, which is the format's own "not applicable".

## Also: the workflow this repository was missing

Branch protection here requires one approving review **from a code owner**,
and the only code owner is the Peristyle account — which is also the only
account with write access. GitHub does not let an author approve their own
pull request, so a pull request opened by that account can never satisfy
the rule: it deadlocks.

`.github/workflows/agent-pr.yml` is the way out, and this repository's own
CODEOWNERS already describes it as the design ("the agents propose changes
through pull requests opened by a GitHub Actions workflow using the
repository's own GITHUB_TOKEN — so the PR author is `github-actions[bot]`,
NOT the Peristyle account. The bot cannot approve its own pull request,
and it is not a code owner, so an agent's pull request can only ever be
approved and merged by Grove"). The workflow was simply missing here, so
the design could not work.

It is the same file, with the same permissions (`contents: read`,
`pull-requests: write`), already in use in `peristyle-ai/harness`. It opens
or updates the pull request for a pushed `agent/**` or `fix/**` branch,
reading the title and body from `.agent-pr/` in that branch.

An agent branch pushed with a deploy key cannot open a pull request
itself — a deploy key is a git credential only, and handing an agent an
API token would let it open a pull request as an account that could also
approve it. This workflow keeps that separation.
