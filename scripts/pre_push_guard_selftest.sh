#!/bin/bash
# Self-test for scripts/pre-push-guard.
#
# The guard is a SECURITY CONTROL: it decides what may reach the public
# remote. A guard that has never been shown to refuse anything is not a
# guard. So this builds a throwaway repository with a local "public"
# remote and exercises every rule -- including the one that matters
# most: a branch carrying a foreign history must be REFUSED.
#
# Usage: scripts/pre_push_guard_selftest.sh
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GUARD="$REPO_ROOT/scripts/pre-push-guard"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

pass=0
fail=0

check() {  # check <name> <expected exit> <remote> <stdin lines>
  local name="$1" expect="$2" remote="$3" lines="$4" out rc
  out=$(printf '%s\n' "$lines" | bash "$GUARD" "$remote" "$TMP/pub.git" 2>&1)
  rc=$?
  if [ "$rc" = "$expect" ]; then
    echo "  PASS  $name (exit $rc)"
    pass=$((pass + 1))
  else
    echo "  FAIL  $name (exit $rc, expected $expect)"
    echo "        $out"
    fail=$((fail + 1))
  fi
}

# --- build the fixture -------------------------------------------------
git init -q --bare "$TMP/pub.git"
git init -q "$TMP/work"
cd "$TMP/work"
git config user.name test
git config user.email test@example.invalid
git config commit.gpgsign false
echo a > a.txt
git add a.txt
git commit -q -m "A: the published root"
A=$(git rev-parse HEAD)
git branch -M main
git remote add github "$TMP/pub.git"
git remote add origin "$TMP/pub.git"
git push -q github main
PUBLISHED=$(git rev-parse HEAD)

# a branch that descends from the published main
git checkout -q -b feat/good
echo b > b.txt
git add b.txt
git commit -q -m "B: a legitimate PR branch"
GOOD=$(git rev-parse HEAD)

# a branch with a FOREIGN history -- rooted at an orphan commit, which
# is what the archive's pre-scrub lineage looks like from the outside
git checkout -q --orphan fix/orphan
git rm -q --cached a.txt 2>/dev/null || true
rm -f a.txt
echo c > c.txt
git add c.txt
git commit -q -m "C: an unrelated root"
ORPHAN=$(git rev-parse HEAD)

# a MERGE of the foreign root into the published main. This is the case
# a descendance-only rule would wave through: the merge commit's first
# parent is the published main, so the branch "descends" from it, while
# its second parent drags the foreign lineage in.
git checkout -q -b publish/grafted "$PUBLISHED"
git merge -q --no-ff --allow-unrelated-histories -m "graft" "$ORPHAN" >/dev/null 2>&1 || true
GRAFTED=$(git rev-parse HEAD)

echo "pre-push guard self-test"
echo

check "a PR branch descending from the published main is ALLOWED" 0 github \
  "refs/heads/feat/good $GOOD refs/heads/feat/good $PUBLISHED"

check "a branch with a foreign root is REFUSED" 1 github \
  "refs/heads/fix/orphan $ORPHAN refs/heads/fix/orphan $PUBLISHED"

check "a MERGE of a foreign root into the published main is REFUSED" 1 github \
  "refs/heads/publish/grafted $GRAFTED refs/heads/publish/grafted $PUBLISHED"

check "an arbitrary ref is REFUSED" 1 github \
  "refs/heads/archive-head $A refs/heads/archive-head $PUBLISHED"

check "a ref outside the branch patterns is REFUSED" 1 github \
  "refs/heads/other-branch $GOOD refs/heads/other-branch $PUBLISHED"

check "a DELETION is ALLOWED" 0 github \
  "refs/heads/feat/good 0000000000000000000000000000000000000000 refs/heads/feat/good $PUBLISHED"

check "main is passed through to GitHub's own protection" 0 github \
  "refs/heads/main $A refs/heads/main $PUBLISHED"

check "a non-public remote is not governed by the guard" 0 origin \
  "refs/heads/archive-head $ORPHAN refs/heads/archive-head $A"

# --- second stage: the published main ADVANCES ------------------------
# The checks above all ran while the published main was $PUBLISHED, so
# each one exercised the clause it names. Now the published main moves
# on -- a scrub lands and is published -- and the tree on main no longer
# holds what that scrub removed.
#
# This stage is deliberately AFTER the checks above: advancing main first
# would make the foreign-root cases fail for the WRONG reason (the new
# behind-main clause, not the foreign-parent rule), and a test that
# passes for the wrong reason is worse than no test.
git checkout -q main
echo scrub > scrub.txt
git add scrub.txt
git commit -q -m "S: the published main removes a name"
PUBLISHED_MAIN=$(git rev-parse HEAD)
git push -q github main

# a branch cut BEFORE the scrub -- so it is BEHIND the published main, and
# its tree still holds what the scrub removed. This is the branch that
# would REPUBLISH the scrubbed content if the guard allowed it.
git checkout -q -b feat/behind "$PUBLISHED"
echo d > d.txt
git add d.txt
git commit -q -m "D: a PR branch cut before the scrub"
BEHIND=$(git rev-parse HEAD)

# and a branch that DOES carry the published main, cut after the scrub
git checkout -q -b feat/ahead "$PUBLISHED_MAIN"
echo e > e.txt
git add e.txt
git commit -q -m "E: a PR branch that carries the published main"
AHEAD=$(git rev-parse HEAD)

# THE CASE THAT ACTUALLY BIT: a branch that is BEHIND the published main.
# It satisfies the parents rule (its commits' parents are all older, and
# so already in the published main) yet does not carry main's latest
# scrub -- so it can publish the scrubbed content back. This must be
# REFUSED, or a name comes back to the public repository.
check "a branch BEHIND the published main is REFUSED" 1 github \
  "refs/heads/feat/behind $BEHIND refs/heads/feat/behind $PUBLISHED_MAIN"

check "a branch that CARRIES the published main is ALLOWED" 0 github \
  "refs/heads/feat/ahead $AHEAD refs/heads/feat/ahead $PUBLISHED_MAIN"

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ] || exit 1
