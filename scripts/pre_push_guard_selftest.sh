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
# The PUBLISHED ROOT carries the scanner, because once the gate has been
# merged that is what main looks like -- and the rules the guard applies
# differ depending on whether the published main has a scanner. A fixture
# where main does NOT have one would exercise the wrong branch of the
# logic and let a broken guard pass.
mkdir -p scripts
cp "$REPO_ROOT/scripts/check_no_identifiers.py" scripts/check_no_identifiers.py
git add a.txt scripts/check_no_identifiers.py
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

# a branch that is STRUCTURALLY fine -- it descends from the published
# main, so every ancestry and parents rule passes -- but whose TREE
# carries a customer's identifier. Only a CONTENT check can refuse it.
git checkout -q -b feat/has-identifier "$PUBLISHED"
# The sample names are SYNTHETIC on purpose, and assembled by printf
# from separate arguments. A regression fixture that spelled the name
# out would put that name back into a tracked file -- which is the very
# defect this gate exists to catch. (The scanner's own samples are
# built the same way, in scripts/check_no_identifiers.py.)
# Every piece is split, so that no contiguous string in THIS FILE can
# match any rule -- the source of the gate must not trip the gate.
printf 'This is the case that started the work: %s%s %s%s\n' \
  "Nor" "thwind" "Tra" "ding Inc." > leak.txt
printf 'contact %s%s@%s%s\n' \
  "le" "ad" "northwind-trading" ".example" >> leak.txt
git add leak.txt
git commit -q -m "E: a branch carrying a customer identifier"
IDENT=$(git rev-parse HEAD)

# A branch that ships a WEAKENED scanner -- one that finds nothing --
# while the PUBLISHED main carries the real one. The guard must run the
# published scanner too, so the weakening buys nothing.
git checkout -q -b feat/weak-scanner "$PUBLISHED"
cat > scripts/check_no_identifiers.py <<'WEAKEOF'
#!/usr/bin/env python3
# A scanner that never finds anything.
import sys
print("identifier scan: 0 finding(s)")
print("IDENTIFIER SCAN GREEN")
sys.exit(0)
WEAKEOF
# The identifier rides along with the weakened scanner -- that is the
# realistic attack: silence the scanner AND publish the name.
printf 'This is the case that started the work: %s%s %s%s\n' \
  "Nor" "thwind" "Tra" "ding Inc." > leak.txt
git add scripts/check_no_identifiers.py leak.txt
git commit -q -m "G: a branch that ships a weakened scanner"
WEAK=$(git rev-parse HEAD)

# A branch that DELETES the scanner the published main carries. That is
# the gate being removed, so it must be REFUSED.
git checkout -q -b fix/drops-scanner "$PUBLISHED"
git rm -q scripts/check_no_identifiers.py
git commit -q -m "H: a branch that removes the gate"
DROPS=$(git rev-parse HEAD)

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

# The CONTENT check. This branch is a pure extension of the published
# main, so every structural rule passes; only the identifier scan can
# refuse it.
check "a structurally valid branch carrying an identifier is REFUSED" 1 github \
  "refs/heads/feat/has-identifier $IDENT refs/heads/feat/has-identifier $PUBLISHED"

# The scanner in the PUBLISHED main is the one that must not be
# silenced. A branch shipping a scanner that finds nothing must still be
# REFUSED, because the published scanner catches the identifier.
check "a branch shipping a WEAKENED scanner is still REFUSED" 1 github \
  "refs/heads/feat/weak-scanner $WEAK refs/heads/feat/weak-scanner $PUBLISHED"

check "a branch that DELETES the gate is REFUSED" 1 github \
  "refs/heads/fix/drops-scanner $DROPS refs/heads/fix/drops-scanner $PUBLISHED"

echo
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ] || exit 1
