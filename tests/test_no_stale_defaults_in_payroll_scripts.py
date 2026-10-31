"""A payroll artefact script must not carry a DEFAULT for a value that
ends up in a bank or fund file.

Context (2026-10-29): `scripts/gen_super_aba.py` defaulted an AMOUNT
and a LODGEMENT_REF -- a real employee's weekly super contribution amount
and a September lodgement reference naming that employee. The amount
happened to be right for one pay run; the lodgement reference was a
SEPTEMBER artefact. A run that forgot to pass them wrote a stale
reference -- and a stale amount beside it -- into a file destined for a
bank. The same class as the old `payroll@example.com` sender default: a
placeholder that reaches a live artefact.

The old values are not quoted here either. This repository is PUBLIC, so
a lodgement reference naming an employee is payroll data wherever it
sits. The check below therefore matches the SHAPE of a stale reference,
not one value.

This module makes the class self-enforcing for the three values that
must be stated explicitly by the caller.

How the check works
-------------------
Two directions, because either alone can be fooled:

  1. STATIC: the script may not read a REQUIRED variable with a
     default (`os.environ.get('AMOUNT', '123.45')` is forbidden).
     Parsed with `ast`, so a mention in a docstring or a comment is
     not mistaken for a default -- and a default that hides in a
     different form is still found.
  2. BEHAVIOURAL: running the script with the variable unset must
     fail loudly, name the variable, and write no file. The refusal is
     checked BEFORE the app is built, so it needs no database -- a
     check that cannot run is not a check.
"""
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / 'scripts' / 'gen_super_aba.py'

# Variables the caller must state. None may have a default.
REQUIRED_VARS = ('AMOUNT', 'LODGEMENT_REF', 'PAYER_EMAIL')


def _env_get_calls(tree):
    """Yield (var_name, has_default) for every os.environ.get call."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and func.attr == 'get'
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == 'environ'):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            # A default may be given positionally or as `default=`.
            has_default = len(node.args) > 1 or any(
                kw.arg == 'default' for kw in node.keywords)
            yield first.value, has_default


@pytest.mark.parametrize('var', REQUIRED_VARS)
def test_required_var_has_no_default(var):
    tree = ast.parse(SCRIPT.read_text())
    found = [(v, d) for v, d in _env_get_calls(tree) if v == var]
    assert found, (f'{var} is no longer read from the environment at all -- '
                   'the script was rewritten; update this test.')
    for _, has_default in found:
        assert not has_default, (
            f'{var} is read with a DEFAULT. A default that reaches a bank '
            'or fund file is a placeholder in a live artefact. Require it.')


def _docstring_nodes(tree):
    """The docstrings in a module -- documentation, not behaviour."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, 'body', None) or []
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def test_no_stale_lodgement_reference_in_the_repo():
    """The September reference must not survive as CODE anywhere.

    A mention in a comment or docstring is documentation -- and the
    comment that explains WHY the default was removed has to be able
    to name it. So this inspects string literals, minus docstrings,
    rather than raw text.
    """
    # The SHAPE of a stale super lodgement reference, not one value --
    # this repository is public, and the reference names an employee.
    stale = re.compile(r'SUPER\s+\d{4,}')
    for path in list((REPO_ROOT / 'scripts').rglob('*.py')) + \
            list((REPO_ROOT / 'app').rglob('*.py')):
        if '.venv' in path.parts:
            continue
        tree = ast.parse(path.read_text(errors='replace'))
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)):
                continue
            if id(node) in docstrings:
                continue
            assert not stale.search(node.value), (
                f'{path.relative_to(REPO_ROOT)} still contains a string '
                'literal that looks like a super lodgement reference '
                '(SUPER <digits>) -- a stale reference from a previous '
                'remittance.')


@pytest.mark.parametrize('var,expect,other', [
    ('AMOUNT', 'AMOUNT is required', None),
    ('LODGEMENT_REF', 'LODGEMENT_REF is required', 'AMOUNT=123.45'),
])
def test_script_refuses_and_names_the_variable(var, expect, other, tmp_path):
    out = tmp_path / 'must-not-exist.aba'
    env = {k: v for k, v in os.environ.items()
           if k not in ('AMOUNT', 'LODGEMENT_REF', 'PAYER_EMAIL')}
    env['OUT_PATH'] = str(out)
    if other:
        env[other.split('=')[0]] = other.split('=')[1]
    env['PYTHONPATH'] = str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0, (
        'the script ran to completion with a required variable missing -- '
        'it must refuse rather than invent a value')
    assert expect in proc.stderr, (
        f'the refusal must name {var}; stderr was: {proc.stderr!r}')
    assert not out.exists(), 'a refused run must not write an artefact'
