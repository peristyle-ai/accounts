"""The agent-pr workflow must trigger for every branch the guard permits.

The pre-push guard allows refs/heads/{feat,fix,publish}/* to the PUBLIC
remote, so those are legitimate PR branches. But if the agent-pr
workflow does not trigger on them, a branch is published with NO pull
request ever opened for it -- and there is then no way to merge it.

That is exactly what happened to feat/quotes: it was pushed, the guard
allowed it, and the workflow that was supposed to open the PR never
fired. So the two lists are asserted to agree.

This test does NOT touch .github/workflows/, so it does not itself
require a maintainer's approval to run -- which is the point of keeping
the assertion in a test rather than in a comment.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GUARD = REPO / "scripts" / "pre-push-guard"
WORKFLOW = REPO / ".github" / "workflows" / "agent-pr.yml"


def guard_permitted_patterns():
    """The branch patterns the guard allows to the public remote."""
    text = GUARD.read_text(encoding="utf-8")
    m = re.search(r"refs/heads/\{([^}]*)\}", text)
    assert m, "the guard no longer names its permitted branch patterns"
    return {p.strip() for p in m.group(1).split(",")}


def workflow_trigger_patterns():
    """The branch patterns the agent-pr workflow triggers on.

    Parsed as YAML rather than by regex: a comment line between the
    entries is enough to stop a naive pattern-matching regex, and a
    scanner that silently stops at a comment is worse than none.
    """
    import yaml

    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    # PyYAML reads the `on:` key as the boolean True.
    triggers = data.get("on", data.get(True))
    assert triggers, "the workflow no longer declares its triggers"
    branches = (triggers.get("push") or {}).get("branches")
    assert branches, "the workflow no longer triggers on a push to any branch"
    return set(branches)


def test_every_guarded_pattern_has_a_workflow_trigger():
    guarded = guard_permitted_patterns()
    triggered = workflow_trigger_patterns()
    missing = {p for p in guarded if f"{p}/**" not in triggered}
    assert not missing, (
        f"the guard permits {sorted(guarded)} but the agent-pr workflow only "
        f"triggers on {sorted(triggered)} -- a branch in {sorted(missing)} "
        f"would be published with no pull request and no way to merge it"
    )


def test_the_workflow_still_opens_and_updates_rather_than_failing():
    """The job must tolerate an existing PR, or a second push fails."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "gh pr edit" in text, "the workflow no longer updates an existing PR"
    assert "--state open" in text, (
        "the workflow no longer filters to open PRs, so it would try to edit "
        "a closed one instead of opening a fresh one"
    )
