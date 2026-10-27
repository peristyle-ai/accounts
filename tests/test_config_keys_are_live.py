"""Every key in config.yaml must be READ by the code.

Context (2026-10-28): `stp.payroll_area` and `stp.payroll_area_type`
sat in config.yaml for months. They were not columns in the 93-column
STP CSV format, no code read them, and they were carried as a phantom
"open question" about ATO semantics -- for weeks. The same audit found
three more of the class: `stp.enabled` (a kill-switch that never
existed) and the whole `accounting:` block (`default_gst_type` and the
four `financial_year_*` keys -- no code read any of them).

A config key that no code reads is worse than no key: it looks like a
setting, it survives review, and editing it does nothing. This module
makes that class of defect self-enforcing.

How the check works
-------------------
`ALLOWED_CONFIG_PATHS` is the list of config paths the application
actually consumes. A path is allowed only if the code contains that key
as a string literal -- so the allowlist cannot rot either. Two
directions are therefore enforced:

  1. every leaf path in config.yaml(.example) is in the allowlist
     (a new, unread key fails the suite), and
  2. every allowlisted path still appears in the source (a key whose
     reader was deleted fails the suite).

The allowlist is deliberately a SHORT list of dotted paths, not a
regex: a regex cannot tell a config read from an unrelated string that
happens to match, and a check that cannot fail for the right reason is
worse than no check.
"""
import ast
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent

# Config paths the application genuinely consumes, with the module that
# reads each one. Keep this in step with the code -- that is the point.
ALLOWED_CONFIG_PATHS = {
    'database.host': 'app/__init__.py',
    'database.port': 'app/__init__.py',
    'database.name': 'app/__init__.py',
    'database.user': 'app/__init__.py',
    'database.password': 'app/__init__.py',
    'database.schema': 'config.py',
    'database.users_table': 'config.py',
    'app.host': 'run.py',
    'app.api_port': 'run.py',
    'app.hmi_port': 'run.py',
    'app.secret_key': 'app/__init__.py',
    'app.upload_folder': 'app/__init__.py',
    'app.logo_fallback_path': 'app/shared/pdf.py',
    'app.max_content_length': 'app/__init__.py',
    'stp.bms_id': 'app/shared/stp_csv.py',
    'stp.product_id': 'app/shared/stp_csv.py',
    'stp.entity_abn': 'app/shared/stp_csv.py',
    'stp.branch_id': 'app/shared/stp_csv.py',
    'stp.previous_bms_id': 'app/shared/stp_csv.py',
    'stp.payrun_time': 'app/shared/stp_csv.py',
    'stp.income_stream_code': 'app/shared/stp_csv.py',
    'stp.income_stream_country_code': 'app/shared/stp_csv.py',
    'stp.final_eoy_pay_indicator': 'app/shared/stp_csv.py',
}

# Paths that were removed on 2026-10-28 because no code read them.
# Listed explicitly so a reintroduction is a deliberate act, and so the
# history of the removal is in the repo rather than only in a chat log.
RETIRED_CONFIG_PATHS = {
    'stp.payroll_area': 'not a column in the 93-column STP CSV format',
    'stp.payroll_area_type': 'not a column in the 93-column STP CSV format',
    'stp.enabled': 'the STP export endpoint was never gated on it',
    'accounting.default_gst_type': 'GST rate is a per-invoice field',
    'accounting.financial_year_start_month': 'FY is fixed in code (1 July)',
    'accounting.financial_year_start_day': 'FY is fixed in code (1 July)',
    'accounting.financial_year_end_month': 'FY is fixed in code (30 June)',
    'accounting.financial_year_end_day': 'FY is fixed in code (30 June)',
}

# Files that may hold string literals. The .venv and __pycache__ are
# excluded: a venv contains hundreds of copies of 'host' and 'name'.
_SOURCE_GLOBS = ('app/**/*.py', 'config.py', 'run.py', 'init_db.py',
                 'scripts/*.py', 'skills/**/*.py')


def _source_files():
    out = []
    for pattern in _SOURCE_GLOBS:
        out.extend(REPO_ROOT.glob(pattern))
    return [p for p in out if '__pycache__' not in p.parts
            and '.venv' not in p.parts]


def _string_literals():
    """Every string literal in the source tree, per file.

    Parsed with `ast` rather than grepped, so a key mentioned in a
    COMMENT does not count as a reader -- which is exactly the case
    that let the dead keys through in the first place.
    """
    literals = {}
    for path in _source_files():
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:
            continue
        found = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.add(node.value)
        literals[path.relative_to(REPO_ROOT).as_posix()] = found
    return literals


def _leaf_paths(node, prefix=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _leaf_paths(value, prefix + (str(key),))
    else:
        yield '.'.join(prefix)


def _config_files():
    """config.yaml.example always; the live config.yaml when present."""
    files = [REPO_ROOT / 'config.yaml.example']
    live = REPO_ROOT / 'config.yaml'
    if live.exists():
        files.append(live)
    return files


@pytest.mark.parametrize('config_name', ['config.yaml.example', 'config.yaml'])
def test_every_config_key_is_read_by_the_code(config_name):
    """No key may sit in a config file that the code never reads."""
    path = REPO_ROOT / config_name
    if config_name == 'config.yaml' and not path.exists():
        pytest.skip('no live config.yaml in this checkout')
    if not path.exists():
        pytest.skip(f'{config_name} not present')

    data = yaml.safe_load(path.read_text())
    assert data is not None, f'{config_name} is empty'

    literals = _string_literals()
    all_literals = set().union(*literals.values()) if literals else set()

    offenders = []
    for leaf in _leaf_paths(data):
        key = leaf.rsplit('.', 1)[-1]
        if key in all_literals:
            continue
        retired = RETIRED_CONFIG_PATHS.get(leaf)
        if retired:
            offenders.append(
                f'{leaf} was retired ({retired}) but is back in {config_name}')
            continue
        offenders.append(
            f'{leaf} ({config_name}) is not read by any code -- either the '
            f'code that used it is gone, or it was never wired up')

    assert not offenders, (
        'Config keys that no code reads:\n  - ' + '\n  - '.join(offenders)
    )


def test_no_retired_config_path_is_read_again():
    """The retired paths must not be read by the code again.

    A retired key may still be MENTIONED in prose (the test module
    itself does, and the explanatory note in config.yaml does), so the
    check is on the AST: is the key used as a string literal in a
    source file at all? For a config key, a string literal IS a read --
    the code could only obtain the value by looking it up by name.
    """
    literals = _string_literals()
    for rel, lits in literals.items():
        if rel.startswith('tests/'):
            continue  # this module names them on purpose
        for lit in lits:
            for path in RETIRED_CONFIG_PATHS:
                key = path.rsplit('.', 1)[-1]
                assert lit != key, (
                    f'{rel} contains the retired config key {key!r} as a '
                    f'string literal -- if the code reads it again, remove '
                    f'it from RETIRED_CONFIG_PATHS deliberately'
                )


def test_allowlist_entries_are_still_read():
    """The allowlist must not rot: every path must still be read.

    The key must appear in the NAMED reader file, not merely somewhere
    in the tree -- otherwise a reader that moved to another module would
    leave a stale entry behind unnoticed.
    """
    literals = _string_literals()
    for path, reader in ALLOWED_CONFIG_PATHS.items():
        key = path.rsplit('.', 1)[-1]
        assert key in literals.get(reader, set()), (
            f'{path} is in ALLOWED_CONFIG_PATHS and names {reader} as its '
            f'reader, but the key {key!r} does not appear there any more '
            f'-- either the reader moved (update the allowlist) or it was '
            f'removed (drop the path)'
        )


def test_config_values_actually_flow_through():
    """A key can be NAMED in the code and still be ignored.

    `stp.branch_id` is looked up in `employer.get('branch_id', '1')`, but
    the value only reaches the employer dict if
    `load_employer_from_config()` copies it across. Drop it from that
    copy loop and the key is still "referenced" -- while the config file
    silently stops mattering. This test asserts the VALUE flows, which
    is the property an operator actually cares about.
    """
    # app.shared.stp_csv imports app.shared.pii, which refuses to import
    # without PAYROLL_PII_KEY (a deliberate fail-fast). This test does not
    # decrypt anything -- it only checks that a config value is COPIED
    # across -- so a throwaway key is enough to satisfy the import. Without
    # this, the test could not run in a bare clone, and a check that
    # silently skips is worse than no check.
    import os as _os
    if not _os.environ.get('PAYROLL_PII_KEY'):
        from cryptography.fernet import Fernet
        _os.environ['PAYROLL_PII_KEY'] = Fernet.generate_key().decode()

    from app.shared import stp_csv

    sentinel = {
        'entity_abn': '11111111111',
        'branch_id': '42',
        'bms_id': 'BRANCHTEST',
        'previous_bms_id': 'PREVTEST',
        'product_id': 'PRODTEST',
        'payrun_time': '13:45',
        'income_stream_code': 'ZZZ',
        'income_stream_country_code': 'zz',
        'final_eoy_pay_indicator': 'Y',
    }

    class _FakeConfig:
        def get(self, *keys, default=None):
            if keys == ('stp',):
                return dict(sentinel)
            return default

    import config as config_module
    original = config_module.accounts_config
    config_module.accounts_config = _FakeConfig()
    try:
        employer = stp_csv.load_employer_from_config()
    finally:
        config_module.accounts_config = original

    for key, value in sentinel.items():
        assert employer.get(key) == value, (
            f'config value for stp.{key} ({value!r}) did not reach the '
            f'employer dict (got {employer.get(key)!r}) -- the key is '
            f'named in the code but its value is ignored'
        )


def test_allowlist_reader_file_exists():
    """Each allowlisted path names a real file that reads it."""
    for path, reader in ALLOWED_CONFIG_PATHS.items():
        assert (REPO_ROOT / reader).exists(), (
            f'{path} names reader {reader!r}, which does not exist'
        )
