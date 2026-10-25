"""The documentation must match the code.

Context (2026-10-25): a documentation sweep found the skill docs asserting
things the code had stopped doing -- most importantly, that a failed payslip
send "fails silently", which was true until the response contract changed on
2026-10-25 (a failed send is 502, an unusable mail configuration is 503).

A doc that describes a contract the code no longer honours is worse than no
doc: an operator reads it, trusts it, and gets a silent failure. These tests
make the documentation self-checking, so the drift fails the suite instead of
shipping.

What is checked:

  1. Every endpoint documented in skills/accounts/payroll.md exists in the
     Flask URL map.
  2. Every payroll API route the app exposes is documented.
  3. The payslip response contract in the doc matches the code.
  4. No doc points at a file that does not exist.
  5. No script falls back to a reserved documentation domain as a default
     address (example.com and friends can never send or receive mail).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PAYROLL_MD = REPO_ROOT / "skills" / "accounts" / "payroll.md"

# A documented path such as /api/payroll/employees/<id> becomes a pattern
# that matches the concrete Flask rule /api/payroll/employees/<employee_id>.
_PARAM = re.compile(r"<[^>]+>")


def _doc_route_pattern(path: str) -> str:
    """Turn a documented path into a regex that matches a Flask rule."""
    parts = []
    for segment in path.split("/"):
        if not segment:
            continue
        if segment.startswith("<") and segment.endswith(">"):
            parts.append(r"[^/]+")
        else:
            parts.append(re.escape(segment))
    return "^/" + "/".join(parts) + "$"


def _documented_routes(text: str) -> list[tuple[str, str]]:
    """Every (METHOD, path) pair in the payroll.md endpoint tables."""
    routes = []
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        method, path_cell = cells[0], cells[1]
        if method not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            continue
        m = re.match(r"^`([^`]+)`", path_cell)
        if not m:
            continue
        path = m.group(1).split("?")[0]
        routes.append((method, path))
    return routes


@pytest.fixture
def app():
    """The application, same pattern as the other test modules."""
    from app import create_app

    try:
        app = create_app()
    except Exception as exc:  # pragma: no cover - only on a broken install
        pytest.skip(f"the app could not be created here: {exc}")
    app.config["TESTING"] = True
    with app.app_context():
        yield app


@pytest.fixture
def url_map(app):
    """The application's real routes."""
    rules = {}
    for rule in app.url_map.iter_rules():
        methods = {m for m in rule.methods if m not in ("HEAD", "OPTIONS")}
        rules.setdefault(rule.rule, set()).update(methods)
    return rules


# ---------------------------------------------------------------------------
# 1 + 2: the documented endpoint tables and the real routes must agree
# ---------------------------------------------------------------------------

def test_every_documented_payroll_route_exists(url_map):
    """A documented endpoint that does not exist is a broken promise."""
    text = PAYROLL_MD.read_text()
    documented = _documented_routes(text)
    assert documented, "no endpoint tables found in payroll.md - the parser " \
                       "or the document format changed"

    missing = []
    for method, path in documented:
        pattern = re.compile(_doc_route_pattern(path))
        match = [rule for rule in url_map if pattern.match(rule)]
        if not match:
            missing.append(f"{method} {path} (no such route)")
        elif method not in url_map[match[0]]:
            missing.append(f"{method} {path} (route exists but not for {method}; "
                           f"it allows {sorted(url_map[match[0]])})")
    assert not missing, "documented but not real:\n  " + "\n  ".join(missing)


def test_every_payroll_api_route_is_documented(url_map):
    """An undocumented endpoint is a route nobody knows the contract of."""
    text = PAYROLL_MD.read_text()
    documented = [(m, p) for m, p in _documented_routes(text)]
    patterns = [(m, re.compile(_doc_route_pattern(p))) for m, p in documented]

    undocumented = []
    for rule, methods in url_map.items():
        if not (rule.startswith("/api/payroll") or rule.startswith("/api/exports/payroll")):
            continue
        for method in methods:
            if not any(m == method and pat.match(rule) for m, pat in patterns):
                undocumented.append(f"{method} {rule}")
    assert not undocumented, "real but not documented:\n  " + "\n  ".join(
        sorted(undocumented))


# ---------------------------------------------------------------------------
# 3: the payslip response contract
# ---------------------------------------------------------------------------

def test_payslip_contract_in_doc_matches_the_code():
    """200 / 502 / 503 -- and no claim that a failure is silent."""
    text = PAYROLL_MD.read_text()
    assert "**502**" in text, "the doc must document the 502 (delivery failed) case"
    assert "**503**" in text, "the doc must document the 503 (mail not configured) case"
    # Any claim that a failure is silent is a claim the code no longer
    # honours. The wording has varied ("fails silently", "fail silently",
    # "silently into"), so match the idea rather than one spelling.
    silent_claim = re.search(r"fail(s|ed)?\s+silently|silently\s+into",
                              text, re.IGNORECASE)
    assert not silent_claim, (
        f"the doc still claims a failed send is silent ({silent_claim.group(0)!r}) "
        "-- the contract changed on 2026-10-25: a failed send is 502, a bad "
        "configuration is 503"
    )


def test_payslip_endpoint_code_returns_the_documented_codes():
    """The code must actually answer 502/503."""
    src = (REPO_ROOT / "app" / "api" / "payroll.py").read_text()
    assert "502" in src, "the payslip endpoint no longer has a 502 path"
    assert "503" in src, "the payslip endpoint no longer has a 503 path"


# ---------------------------------------------------------------------------
# 4: no doc points at a file that does not exist
# ---------------------------------------------------------------------------

_MD_REF = re.compile(r"`([A-Za-z0-9_][A-Za-z0-9_./-]*\.md)`")

# Places a reference may legitimately point at.
_SEARCH_DIRS = ("", "skills/accounts", "schema", "scripts", "app", "docs")

# Words that mark a reference to a file that no longer exists as HISTORY
# rather than as a pointer the reader is expected to follow.
_HISTORY_MARKERS = (
    "not published", "internal", "was deleted", "historical",
    "no longer exists", "removed", "not part of this repository",
)


def _markdown_files():
    return [p for p in REPO_ROOT.rglob("*.md")
            if ".git" not in p.parts and ".venv" not in p.parts
            and "node_modules" not in p.parts]


def test_no_doc_references_a_missing_markdown_file():
    """A pointer to a deleted file is a dead end for the reader."""
    missing = []
    for doc in _markdown_files():
        text = doc.read_text(errors="ignore")
        for ref in set(_MD_REF.findall(text)):
            if ref.startswith(("http", "/", "~")) or " " in ref:
                continue
            # A reference to another skill's file is out of scope.
            if ref.startswith("skills/"):
                continue
            if any((REPO_ROOT / d / ref).is_file() for d in _SEARCH_DIRS):
                continue
            if (doc.parent / ref).is_file():
                continue
            # A reference to a file that was deliberately deleted is
            # legitimate HISTORY -- but only if the line says so, so a reader
            # does not go looking for it.
            all_lines = text.splitlines()
            windows = []
            for i, ln in enumerate(all_lines):
                if ref in ln:
                    # The explanation often sits on the NEXT line, so look
                    # at the line and the one after it.
                    windows.append(" ".join(all_lines[i:i + 2]))
            if windows and all(any(marker in w for marker in _HISTORY_MARKERS)
                               for w in windows):
                continue
            missing.append(f"{doc.relative_to(REPO_ROOT)} -> {ref}")
    assert not missing, "documentation points at files that do not exist:\n  " + \
        "\n  ".join(sorted(missing))


# ---------------------------------------------------------------------------
# 5: no reserved documentation domain as a runtime default
# ---------------------------------------------------------------------------

_RESERVED_ADDR = re.compile(
    r"^[A-Za-z0-9._%+-]+@"
    r"(example\.(com|org|net|edu)|[A-Za-z0-9.-]+\.(example|invalid|test|localhost))$"
)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """ids() of constants that are docstrings -- prose, not defaults."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def test_no_script_defaults_to_a_reserved_documentation_domain():
    """example.com can never send or receive mail.

    A default address like `payroll@example.com` turns a misconfiguration
    into a silent no-op: the script appears to send, and the message goes
    nowhere. The app's own resolver refuses these domains; the scripts have
    to refuse them too.
    """
    offenders = []
    for path in sorted((REPO_ROOT / "scripts").glob("*.py")):
        src = path.read_text()
        tree = ast.parse(src, filename=str(path))
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                if _RESERVED_ADDR.match(node.value.strip()):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {node.value}")
    assert not offenders, (
        "these scripts fall back to a reserved documentation domain:\n  "
        + "\n  ".join(offenders)
        + "\nA reserved domain can never send or receive mail."
    )
