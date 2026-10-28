#!/usr/bin/env python3
"""Fail the build if a secret is committed.

GitHub's own secret scanning and push protection are free on PUBLIC
repositories. On a private repository they need GitHub Advanced Security,
which is a paid add-on -- so until this repo is public, this script is
the enforcement.

It scans BOTH:

  1. the working tree -- every file tracked at HEAD;
  2. every blob in every commit -- so a secret that was committed and
     then deleted is still found. (GitHub's secret scanning does the
     same thing, and it is the reason a "fix the leak and move on"
     commit is not enough.)

A scanner that scanned 0 objects must report INCONCLUSIVE, never
CLEAN, and must exit non-zero. Silence is not a pass.

Exit codes:
  0  no secret found
  1  a secret was found
  2  INCONCLUSIVE -- nothing could be scanned. That is a failure too:
     a gate that cannot see is not a gate.
"""
from __future__ import annotations

import re
import subprocess
import sys
from collections import defaultdict

# --- the patterns -------------------------------------------------------
# Only PROVIDER FORMATS, with a length floor. A generic "something =
# something_secret" is too noisy to gate a build on -- it fires on
# `config.llm.api_key = config.llm.api_key` and on the word "password".
# A provider's key format is specific enough to be a real signal.
SECRET_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("anthropic/openai-style key", re.compile(r"\bsk-(?:ant-|cp-)?[A-Za-z0-9_-]{20,}")),
    ("embedding-service key", re.compile(r"\b(?:ep|em)_[A-Za-z0-9_-]{20,}")),
    ("github classic token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("github fine-grained token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}")),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("stripe key", re.compile(r"\b[sr]k_(?:live|test)_[A-Za-z0-9]{20,}")),
    # A password LITERAL assigned to a password-ish key. This is the class
    # that leaked here: a live credential committed as a test fixture. The
    # generic form ("something = something_secret") is too noisy to gate on
    # -- it fires on os.environ.get("SECRET_KEY") and on the word "password"
    # -- so this matches only a QUOTED literal of 8+ characters, and the
    # placeholder/template rules below excuse the obvious ones.
    ("a password literal", re.compile(
        r"(?i)\b(?:password|passwd|pwd|passphrase|secret|token|api[_-]?key)\b['\"]?"
        r"\s*[:=]\s*['\"]([^'\"\n]{8,})['\"]")),
    ("json web token", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")),
    ("private key block", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("http bearer header", re.compile(r"(?i)\bauthorization:\s*bearer\s+([A-Za-z0-9._-]{20,})")),
    ("password in a connection string",
     re.compile(r"\b(?:postgres|postgresql|mysql|mongodb(?:\+srv)?|redis|amqp|https?)://[^\s:/@]+:([^\s/@]{8,})@")),
]

# --- the known non-secrets ---------------------------------------------
# Each entry is (path-glob, exact value, why). A finding is excused only
# if BOTH the place and the value match, so a real secret in a test file
# is still a finding.
ALLOWLIST: list[tuple[str, str, str]] = [
    ("tests/*", "not-a-real-password", "the suite's throwaway fixture password"),
    ("tests/*", "nobody", "a test fixture's placeholder password"),
    ("tests/*", "keepthispassword", "a test fixture's placeholder password"),
    ("tests/*", "***", "a test fixture's masked value"),
    ("*.github/workflows/*.yml", "password", "the CI's own throwaway postgres password"),
    ("docs/*", "password", "a documentation example"),
]

# --- the exempt files --------------------------------------------------
# A file whose PURPOSE is to contain secret-shaped strings cannot be
# scanned for secret-shaped strings. This is the only such file, and the
# exemption is printed on every run -- it is never silent, and it cannot
# grow without someone editing this list.
EXEMPT_PATHS: list[tuple[str, str]] = [
    ("tests/test_check_no_secrets.py",
     "the scanner's own test file: it plants real-shaped secrets to "
     "prove the scanner catches them"),
]

# --- the placeholders ---------------------------------------------------
# A value that is obviously a placeholder is never a secret, whatever
# its shape.
PLACEHOLDER = re.compile(
    r"^(?:YOUR_[A-Z_]*|REPLACE_ME|replace_me|\*\*\*+|xxx+|\.\.\.|changeme|"
    r"placeholder|example|dummy|test|redacted|null|none|true|false|"
    r"ci-not-a-real-[a-z-]*|not-a-real-[a-z-]*|your-[a-z-]*)$",
    re.I)

# A value that is WRAPPED IN A TEMPLATE is a placeholder, whatever is
# inside it: `<password>` in a README example, `{runtime_pw}` in an
# f-string, `${VAR}` in a config. A real credential is never spelled
# that way -- it is a bare value -- so recognising the shape does not
# weaken the gate. Before this, the gate failed on the repository it
# guards: the README's own connection-string example and the
# installer's own f-string were both read as leaked passwords.
TEMPLATED = re.compile(r"^(?:<[^<>]+>|\{[^<>{}]+\}|\$\{[^<>{}]+\})$")


def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True,
                          errors="replace").stdout


def excused(path: str, value: str) -> str | None:
    """The reason this (place, value) pair is not a secret, if it is."""
    import fnmatch
    for pattern, allowed, why in ALLOWLIST:
        if fnmatch.fnmatch(path, pattern) and value == allowed:
            return why
    return None


def is_exempt(path: str) -> str | None:
    """The reason this path is not scanned, if it is."""
    import fnmatch
    for pattern, why in EXEMPT_PATHS:
        if fnmatch.fnmatch(path, pattern):
            return why
    return None


def scan_text(text: str, where: str, findings: list) -> None:
    for name, rx in SECRET_PATTERNS:
        for m in rx.finditer(text):
            value = m.group(1) if m.groups() else m.group(0)
            value = value.strip()
            if PLACEHOLDER.match(value) or TEMPLATED.match(value):
                continue
            path = where.split(":", 1)[-1] if ":" in where else where
            if is_exempt(path):
                continue
            why = excused(path, value)
            if why:
                continue
            line = text.count("\n", 0, m.start()) + 1
            # print only a short prefix of the value: the point is to find
            # it, not to reprint it into a build log.
            shown = value[:6] + "…" if len(value) > 6 else value
            findings.append((name, where, line, shown))


def main() -> int:
    findings: list = []
    exempted: dict[str, str] = {}

    # ---- 1. the working tree -----------------------------------------
    tracked = [f for f in run(["git", "ls-files", "-z"]).split("\0") if f]
    tree_bytes = 0
    for path in tracked:
        why = is_exempt(path)
        if why:
            exempted[path] = why
            continue
        try:
            data = open(path, "rb").read()
        except OSError:
            continue
        tree_bytes += len(data)
        scan_text(data.decode("utf-8", errors="replace"), f"HEAD:{path}", findings)
    print(f"scanned the working tree: {len(tracked)} tracked files, {tree_bytes:,} bytes")

    # ---- 2. every blob in every commit -------------------------------
    objects = []
    for line in run(["git", "rev-list", "--objects", "--all"]).splitlines():
        parts = line.split(" ", 1)
        objects.append((parts[0], parts[1] if len(parts) > 1 else ""))
    blobs = [(oid, name) for oid, name in objects
             if run(["git", "cat-file", "-t", oid]).strip() == "blob"]
    scanned = 0
    for oid, name in blobs:
        if name and is_exempt(name):
            exempted.setdefault(name, is_exempt(name))
            continue
        data = subprocess.run(["git", "cat-file", "blob", oid],
                              capture_output=True).stdout
        scanned += len(data)
        scan_text(data.decode("utf-8", errors="replace"),
                  f"history:{name or oid[:10]}", findings)
    print(f"scanned the history: {len(blobs)} blobs, {scanned:,} bytes")

    # ---- 3. the commit messages --------------------------------------
    msgs = run(["git", "log", "--all", "--format=%B"])
    scan_text(msgs, "COMMIT MESSAGES", findings)

    # ---- the coverage gate -------------------------------------------
    # INCONCLUSIVE only when NOTHING could be scanned. One of the two
    # surfaces being empty is not a failure: a repository whose working
    # tree is empty still has a history worth scanning, and a repository
    # with no history still has a tree worth scanning.
    if not tracked and not blobs:
        print()
        print("INCONCLUSIVE -- neither the working tree nor the history "
              "could be scanned")
        return 2

    # ---- the report ---------------------------------------------------
    # The exemptions are printed on every run. An exemption nobody can
    # see is a hole; an exemption that is printed is a decision.
    if exempted:
        print()
        print(f"exempted {len(exempted)} file(s) -- these are NOT scanned:")
        for path, why in sorted(exempted.items()):
            print(f"  {path}\n      {why}")

    if findings:
        print()
        print("=" * 72)
        print(f"A SECRET WAS COMMITTED: {len(findings)} finding(s)")
        print("=" * 72)
        by_name = defaultdict(list)
        for name, where, line, shown in findings:
            by_name[name].append((where, line, shown))
        for name, items in sorted(by_name.items()):
            print(f"  [{name}] {len(items)} hit(s)")
            for where, line, shown in items[:10]:
                print(f"      {where}:{line}  {shown}")
            if len(items) > 10:
                print(f"      ... and {len(items) - 10} more")
        print()
        print("If one of these is a real secret, it is already compromised:")
        print("rotate it FIRST, then remove it, then rewrite the history if")
        print("it was committed -- deleting the file does not un-leak it.")
        return 1

    print()
    print("no secret found (working tree, full history, commit messages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
