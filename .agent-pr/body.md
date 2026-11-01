## What

The `agent-pr` workflow only triggered on `agent/**` and `fix/**`, but the
pre-push guard permits `refs/heads/{feat,fix,publish}/*` to the public
remote. A `feat/` branch was therefore **published with no pull request
ever opened for it**: the guard allowed the push and the workflow that was
supposed to open the PR never fired.

That is how `feat/quotes` ended up with no PR — it sat published and
unmergeable, and the only route to merging it was for a human to open
the PR by hand.

| file | change |
|---|---|
| `.github/workflows/agent-pr.yml` | the trigger list now covers every pattern the guard permits |
| `tests/test_agent_pr_triggers.py` | **new**: asserts the two lists agree |

## Why the test, and not just the change

The two lists have to agree, and they can drift apart silently — that is
exactly how this happened. So the agreement is asserted on every CI run.

The test lives in `tests/`, not in a comment, and it does **not** touch
`.github/workflows/`. That matters: a pull request that modifies a
workflow file has its `pull_request` CI **held** awaiting a maintainer's
approval, so asserting the rule from inside a workflow file would mean
the assertion could not run without a human.

The test was proven by mutation — removing `feat/**` from the workflow
makes it fail, and restoring it makes it pass. Its first version parsed
the workflow with a regex, and the regex stopped at a comment line and
saw only two of the four patterns; it now parses the YAML, because a
scanner that silently stops early is worse than no scanner.

## Why this is a separate PR from the quotes feature

Same reason, in the other direction: bundling a workflow change with a
feature would hold that feature's CI too. `feat/quotes` is a feature PR
and its CI runs normally.
