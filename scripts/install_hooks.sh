#!/bin/bash
# Install the pre-push guard into the local clone's hooks directory.
#
# The hook lives in .git/hooks/, which is NOT version-controlled, so it
# must be re-installed after a fresh clone. Run this after cloning.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$REPO_ROOT/.git/hooks/pre-push"
if [ ! -d "$REPO_ROOT/.git/hooks" ]; then
  echo "not a git checkout: $REPO_ROOT" >&2
  exit 1
fi
cp "$REPO_ROOT/scripts/pre-push-guard" "$HOOK"
chmod +x "$HOOK"
echo "installed $HOOK"
echo
echo "remotes:"
git -C "$REPO_ROOT" remote -v
