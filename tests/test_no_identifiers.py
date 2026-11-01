"""The identifier gate must be able to FAIL.

A gate that has never been shown to refuse anything is not a gate. So
these tests assert three things:

  1. each rule FIRES on a sample carrying a real identifier,
  2. the placeholders this repo legitimately uses are NOT flagged,
  3. the repository's own tree is clean -- and that assertion is what
     turns the scanner into something CI can rely on.

The samples below are SYNTHETIC. A regression fixture that reproduced
the real customer name would put that name back into a tracked file --
the very defect this gate exists to catch. The rules are generic, so a
synthetic sample proves each rule just as well.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNER = REPO_ROOT / "scripts" / "check_no_identifiers.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_no_identifiers", SCANNER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_no_identifiers"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _findings(sample: str):
    out = []
    mod.scan_text("identifier", "sample", sample, out)
    return out


# Assembled at RUNTIME from parts. If the sample were written out here
# in full, THIS FILE would carry the identifier the gate exists to
# refuse -- and the scanner would (correctly) fail the repository that
# holds its own regression test.
_TRADING = "North" + "wind Trading " + "Inc."
_OTHER_TRADING = "West" + "mere Srl"
_DOMAIN = "northwind" + "-trading.example"
_LEAD = "le" + "ad"
_PERSON = "Ameli" + "a Hart" + "ley"
_ACCOUNT = "some" + "user"


@pytest.mark.parametrize("sample,expected_rule", [
    ("This is the case that started the work: " + _TRADING, "trading name"),
    ("raise the quote for " + _OTHER_TRADING + " in Italy", "trading name"),
    ("contact " + _LEAD + "@" + _DOMAIN, "email domain"),
    ("cc accounts@sample" + "-services.example", "email domain"),
    ('absolute: "/home/' + _ACCOUNT + '/py_pg_accounts/uploads/x.jpg"', "unix account"),
    ("raised for " + _PERSON + ", CEO", "name-shaped"),
])
def test_each_rule_fires(sample, expected_rule):
    findings = _findings(sample)
    assert findings, f"the scan did not fire on {sample!r}"
    assert any(expected_rule in f.text for f in findings), \
        f"expected {expected_rule!r} in {[f.text for f in findings]}"


@pytest.mark.parametrize("sample", [
    "billing@acme.com and payroll@example.com",
    "the Acme Corp example, per the AGENTS.md",
    'absolute: "/home/somewhere/uploads/receipts/x.jpg"',
    "Open the Original receipt, Payment Reference and Bank Reference",
    "This is the case that started the work: a customer outside Australia",
])
def test_placeholders_are_not_flagged(sample):
    assert not _findings(sample), \
        f"a placeholder was flagged: {[str(f) for f in _findings(sample)]}"


def test_a_description_is_not_a_name():
    """The fix for the original leak: describe the case, not the case."""
    assert not _findings(
        '"""A customer outside Australia gets a quote with NO GST."""'
    )


def test_the_repository_tree_is_clean():
    findings = mod.scan_tree(REPO_ROOT)
    assert not findings, "identifiers in the tracked tree:\n" + "\n".join(
        str(f) for f in findings)


def test_the_scanner_refuses_to_report_zero_findings_on_an_empty_repo(tmp_path):
    """A scanner that finds no files must fail loudly, not report green."""
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    with pytest.raises(SystemExit):
        mod.scan_tree(repo)


def test_the_scanner_has_a_self_test():
    rc = subprocess.run(
        [sys.executable, str(SCANNER), "--self-test"],
        capture_output=True, text=True,
    )
    assert rc.returncode == 0, rc.stdout + rc.stderr
    assert "self-test: PASSED" in rc.stdout
