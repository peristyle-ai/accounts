#!/usr/bin/env python3
"""Refuse identifiers that identify a real person or a real customer.

The secret scan (``check_no_secrets.py``) looks for CREDENTIALS. This
looks for IDENTITIES -- a person's name, a customer's trading name, a
customer's email domain, a customer's Unix account in a path. Those are
not secrets, so no scanner will ever flag them, and the accounts repo
is public.

The tree is checked, and the tree is what a reader sees. History is
reported separately (``--history``) because the published history may
still carry an identifier that a tree rewrite cannot reach -- and a
report that hides that is worse than no report.

Design rules this file follows, learned the hard way:

  * The scanner must ASSERT ITS OWN INPUT. If it cannot find the files
    it was told to scan, it fails loudly rather than reporting zero
    findings.
  * It must have a SELF-TEST, and the self-test must prove each rule
    FAILS when the pattern is present. A check that has never been
    shown to fail is not evidence.
  * A placeholder is not an identifier. The allowlists below name the
    placeholders this repo legitimately uses; without them the scan is
    noise, and a noisy scan gets ignored.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------
# Allowlists -- the placeholders this repository legitimately uses.
# Anything on these lists is NOT an identifier.
# ---------------------------------------------------------------------

ALLOWED_EMAIL_DOMAINS = {
    "example.com", "example.org", "example.net", "example.invalid",
    "example.test", "example-customer.com", "yourcompany.com.au",
    "your-company.com", "acme.com", "a.com.au",
    # fixture tenants in the access-control sweep tests
    "b.com", "z.com",
}

ALLOWED_CORPORATE_NAMES = {
    "Acme Corp", "Acme Pty Ltd", "Example Corp", "Example Pty Ltd",
    "Your Company", "Your Company Pty Ltd", "YourCompany Pty Ltd",
    "Foo Corp", "Foo Pty Ltd", "Sample Corp", "Sample Pty Ltd",
    "Test Corp", "Test Pty Ltd", "Placeholder Corp", "Placeholder Pty Ltd",
    "Widget Corp", "Widget Pty Ltd", "Contoso Ltd", "Initech Inc",
    # the super clearing house this repo integrates with, and the ATO's
    # own published sample rows
    "Wrkr Pay", "Salsac Other",
}

ALLOWED_HOME_USERS = {"somewhere", "user", "username"}

# A corporate suffix is the tell for a trading name. A person's name is
# caught by the name-shaped rule below.
CORPORATE_SUFFIXES = (
    "Inc", "Inc.", "Pty", "Pty Ltd", "Ltd", "Ltd.", "LLC", "Srl", "S.r.l.",
    "GmbH", "Corp", "Corp.", "Corporation", "Co.", "Company", "Limited",
    "Pty.Ltd", "Group", "Holdings", "Technologies", "Solutions",
)

# Every token of every suffix, so a phrase made only of suffixes can be
# recognised as vocabulary rather than as a name.
_SUFFIX_TOKENS = {
    t for s in CORPORATE_SUFFIXES for t in s.replace(".", " ").split()
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
_CORPORATE_RE = re.compile(
    r"\b([A-Z][A-Za-z&'.-]+(?: [A-Z][A-Za-z&'.-]+){0,3} (?:"
    + "|".join(re.escape(s) for s in CORPORATE_SUFFIXES)
    + r"))\b"
)
_HOME_RE = re.compile(r"/(?:home|Users)/([A-Za-z0-9._-]+)/")

# A name-shaped token: two capitalised words, e.g. PLACEHOLDER_A
PLACEHOLDER_B -- "Open the Original", "Payment Reference"
# and every sentence start must not match.
NAME_SHAPED_RE = re.compile(
    r"\b([A-Z][a-z]{2,15}) ([A-Z][a-z]{2,15})\b"
)
_CASE_THAT_RE = re.compile(r"the work\s*:\s*", re.IGNORECASE)
_PROPER_AFTER_COLON_RE = re.compile(r"[A-Z][A-Za-z&'.-]+")
# A ROLE, not the word "contact". "contact" on its own appears in
# "Xero Contact" and "Contact Email" -- ordinary labels -- and a rule
# that fires on ordinary labels is a rule that gets deleted.
_CONTACT_KEYWORDS = (
    "ceo", "cfo", "cto", "coo", "director", "manager", "accountant",
    "proprietor", "founder", "attn", "attention", "chief executive",
)
# Word-boundary matching, or "refactor" contains "cto" and "cookie"
# contains "coo" -- which is how the first version of this rule flagged
# half the repository.
_CONTACT_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(k) for k in _CONTACT_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# Words that look like a name but are ordinary English in this codebase.
# Without this list the rule is unusable, and an unusable rule gets
# deleted.
NAME_STOPWORDS = {
    "Open the", "The Request", "Payment Reference", "Bank Reference",
    "Load Staffing", "Superannuation", "Australian Business", "Business Name",
    "Account Name", "Customer Name", "Employee Name", "Payment Method",
    "Upload Root", "View Receipt", "Single Touch", "Message Repository",
    "Business Identifier", "Digital Service", "Payroll Area", "Social Security",
    "Australian Taxation", "Financial Year", "Service Provider", "Sending Service",
    "Sender Service", "Error Code", "Status Code", "Response Code", "Request Body",
    "Payment Reference", "Money Transfer", "Bank Transfer", "Direct Debit",
    "Proof Of", "Proof of", "Add Expense", "Edit Expense", "New Expense",
    "Chief Executive", "Operating System", "This Case", "The Case",
    "The Work", "Case That", "A Quote", "Quote Number", "Invoice Number",
    "Customer Record", "Public Release", "Initial Public", "Open Source",
    "The Name", "The Case That", "A Customer", "The Customer",
    "Peristyle Accounts", "Open The", "Single File", "Test Suite",
    "Releases Notes", "Release Notes", "Working Tree", "Public Remote",
    "Private Archive", "Branch Protection", "Pull Request", "Pre Push",
    "Secret Scan", "Identifier Scan", "Corporate Suffix", "Place Holder",
    "Placeholder Values", "Real Person", "Real Customer", "Unix Account",
    "Absent File", "Same Line", "New File", "Old File", "Last Commit",
    "Merge Commit", "Root Commit", "First Commit", "Deep Scan",
    "In Memory", "Live Value", "Prod Db", "Front End", "Back End",
}


@dataclass
class Finding:
    rule: str
    path: str
    line_no: int
    text: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.path}:{self.line_no}: {self.text}"


def tracked_files(repo: Path) -> list[Path]:
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        capture_output=True, text=True, check=True,
    )
    names = [n for n in out.stdout.split("\0") if n]
    if not names:
        raise SystemExit(
            "check_no_identifiers: no tracked files found -- the scanner "
            "cannot examine the tree. Refusing to report zero findings."
        )
    return [repo / n for n in names]


def scan_text(rule: str, path: str, text: str, findings: list[Finding]) -> None:
    for i, line in enumerate(text.split("\n"), start=1):
        for m in _EMAIL_RE.finditer(line):
            domain = m.group(1).lower()
            if domain in ALLOWED_EMAIL_DOMAINS:
                continue
            findings.append(Finding(rule, path, i, f"email domain {domain!r}: {m.group(0)}"))

        for m in _CORPORATE_RE.finditer(line):
            name = m.group(1)
            if name in ALLOWED_CORPORATE_NAMES:
                continue
            # A phrase made ENTIRELY of suffix words ("Pty Ltd") is not a
            # trading name -- it is the vocabulary of one. Without this,
            # the scanner flags its own list of suffixes.
            tokens = [t.strip(".,'&") for t in name.split()]
            if tokens and all(t in _SUFFIX_TOKENS for t in tokens):
                continue
            findings.append(Finding(rule, path, i, f"trading name {name!r}"))

        for m in _HOME_RE.finditer(line):
            user = m.group(1)
            if user.lower() in ALLOWED_HOME_USERS:
                continue
            findings.append(Finding(rule, path, i, f"unix account /{user}/ in a path"))

        for m in NAME_SHAPED_RE.finditer(line):
            phrase = m.group(0)
            if phrase in NAME_STOPWORDS:
                continue
            first, second = m.group(1), m.group(2)
            if second.lower() in _COMMON_SECOND_WORDS:
                continue
            if first in ("This", "The", "A", "An", "And", "But", "For", "If",
                         "When", "Each", "Both", "Some", "Only", "Then",
                         "Also", "That", "These", "Those", "One", "Two"):
                continue
            # A bare pair of capitalised words is usually prose, not a
            # person. It becomes an identifier only in a CONTACT context,
            # where a name is what the line is doing.
            if not _CONTACT_RE.search(line):
                continue
            findings.append(Finding(rule, path, i, f"name-shaped {phrase!r}"))

        # "This is the case that started the work: <Customer>." -- the
        # exact sentence that put a customer on a public page. The fix is
        # a description, not a name, so a capitalised word straight
        # after the colon is the tell.
        m = _CASE_THAT_RE.search(line)
        if m and _PROPER_AFTER_COLON_RE.match(line, m.end()):
            findings.append(Finding(rule, path, i,
                                    "a proper name after 'the work:'"))


_COMMON_SECOND_WORDS = {
    "the", "a", "an", "and", "or", "but", "if", "when", "each", "both",
    "some", "only", "then", "also", "that", "these", "those", "one", "two",
    "reference", "name", "code", "path", "file", "root", "value", "values",
    "number", "method", "status", "provider", "service", "client", "server",
    "request", "response", "error", "id", "key", "url", "uri", "json",
    "form", "list", "item", "items", "row", "rows", "line", "lines",
    "payroll", "invoice", "quote", "quotes", "customer", "customers",
    "payment", "payments", "bank", "super", "gst", "ato", "stp", "aba",
    "csv", "pdf", "api", "url", "html", "css", "sql", "yaml", "toml",
    "receipt", "receipts", "expense", "expenses", "record", "records",
    "case", "cases", "work", "release", "releases", "public", "private",
    "superannuation", "business", "company", "organisation", "organization",
    "deployment", "deploy", "install", "installation", "config",
    "configuration", "settings", "options", "options", "fields", "field",
}


def scan_tree(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    files = tracked_files(repo)
    for f in files:
        if not f.exists():
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "\0" in text[:4096]:
            continue  # binary
        scan_text("identifier", str(f.relative_to(repo)), text, findings)
    return findings


def scan_commit(repo: Path, commit: str) -> list[Finding]:
    """Scan the TREE OF ONE COMMIT. This is what the pre-push guard needs:
    it must see what is about to be PUBLISHED, not what happens to be in
    the working tree."""
    findings: list[Finding] = []
    names = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", commit],
        capture_output=True, text=True,
    ).stdout.split("\n")
    if not any(n for n in names if n):
        raise SystemExit(
            f"check_no_identifiers: commit {commit} has no files -- the "
            "scanner cannot examine it. Refusing to report zero findings."
        )
    for name in names:
        if not name:
            continue
        blob = subprocess.run(
            ["git", "-C", str(repo), "show", f"{commit}:{name}"],
            capture_output=True,
        )
        if blob.returncode != 0:
            continue
        text = blob.stdout.decode("utf-8", "replace")
        if "\0" in text[:4096]:
            continue
        scan_text("identifier", f"{commit[:10]}:{name}", text, findings)
    return findings


def scan_history(repo: Path) -> dict[str, list[str]]:
    """Which commits carry an identifier in their tree? Reported, not fatal."""
    commits = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--all"],
        capture_output=True, text=True, check=True,
    ).stdout.split()
    out: dict[str, list[str]] = {}
    for c in commits:
        files = subprocess.run(
            ["git", "-C", str(repo), "ls-tree", "-r", "--name-only", c],
            capture_output=True, text=True,
        ).stdout.split("\n")
        hits = []
        for name in files:
            if not name:
                continue
            blob = subprocess.run(
                ["git", "-C", str(repo), "show", f"{c}:{name}"],
                capture_output=True,
            )
            if blob.returncode != 0:
                continue
            text = blob.stdout.decode("utf-8", "replace")
            if "\0" in text[:4096]:
                continue
            local: list[Finding] = []
            scan_text("history", name, text, local)
            if local:
                hits.append(name)
        if hits:
            subject = subprocess.run(
                ["git", "-C", str(repo), "log", "-1", "--format=%s", c],
                capture_output=True, text=True,
            ).stdout.strip()
            out[f"{c[:10]} {subject}"] = sorted(set(hits))
    return out


# SYNTHETIC samples, deliberately, and assembled at RUNTIME from parts.
#
# The real customer names that reached a public page must not appear in
# a tracked file -- not even inside the regression fixture for the gate
# that stops them. A fixture that spelled the name out would put it
# back on a public page, and the scanner would (correctly) refuse to
# publish the repository that holds it.
#
# So the samples are concatenated here rather than written out. At
# runtime they are exactly what the rules must catch; in the source
# file there is no contiguous string for any rule to match.
_TRADING = "North" + "wind Trading " + "Inc."
_OTHER_TRADING = "West" + "mere Srl"
_DOMAIN = "northwind" + "-trading.example"
_LEAD = "le" + "ad"
_PERSON = "Ameli" + "a Hart" + "ley"
_ACCOUNT = "some" + "user"

SELF_TEST_CASES = [
    ("email", f"contact us at {_LEAD}@{_DOMAIN} please"),
    ("email", "reach someone at accounts@sample" + "-services.example"),
    ("corporate", "This is the case that started the work: " + _TRADING),
    ("corporate", "customer " + _OTHER_TRADING + " in Italy"),
    ("home", 'absolute: "/home/' + _ACCOUNT + '/py_pg_accounts/uploads/x.jpg"'),
    ("name", "raise it for " + _PERSON + ", CEO"),
]


def self_test() -> int:
    """Each rule must FAIL on a sample that contains the pattern, and
    PASS on the placeholders this repo legitimately uses. A rule that has
    never been shown to fail is not a check."""
    failures = 0

    for rule, sample in SELF_TEST_CASES:
        local: list[Finding] = []
        scan_text(rule, "sample", sample, local)
        if not local:
            print(f"  FAIL  rule {rule}: did NOT fire on {sample!r}")
            failures += 1
        else:
            print(f"  PASS  rule {rule} fires ({local[0].text})")

    placeholders = [
        "billing@acme.com and payroll@example.com",
        "the Acme Corp example, per the AGENTS.md",
        'absolute: "/home/somewhere/uploads/receipts/x.jpg"',
        "Open the Original receipt, Payment Reference and Bank Reference",
        "The Request was for a quote; the customer name is stored, not here",
    ]
    for sample in placeholders:
        local: list[Finding] = []
        scan_text("allowlist", "sample", sample, local)
        if local:
            print(f"  FAIL  placeholder flagged: {sample!r} -> {[str(f) for f in local]}")
            failures += 1
        else:
            print(f"  PASS  placeholder accepted: {sample!r}")

    print()
    print(f"self-test: {'PASSED' if not failures else 'FAILED'} "
          f"({failures} failure(s))")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=str(REPO_ROOT))
    ap.add_argument("--history", action="store_true",
                    help="also report identifiers carried by commits in the history")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--commit", default=None,
                    help="scan the tree of this commit instead of the working tree")
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    repo = Path(args.repo).resolve()
    if args.commit:
        findings = scan_commit(repo, args.commit)
        n_files = "tree of " + args.commit[:10]
    else:
        findings = scan_tree(repo)
        n_files = f"{len(tracked_files(repo))} tracked file(s)"
    print(f"identifier scan: {n_files}, {len(findings)} finding(s)")
    for f in findings:
        print(f"  {f}")

    if args.history:
        print()
        print("history residue (reported, not fatal -- a tree rewrite "
              "cannot reach an unreachable commit):")
        hist = scan_history(repo)
        if not hist:
            print("  none")
        for k, v in hist.items():
            print(f"  {k}")
            for name in v:
                print(f"      {name}")

    if findings:
        print()
        print("IDENTIFIER SCAN FAILED -- an identifier that identifies a "
              "person or a customer is in a tracked file.")
        return 1
    print("IDENTIFIER SCAN GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
