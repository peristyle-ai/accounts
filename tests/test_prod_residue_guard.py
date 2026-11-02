"""The suite must not leave residue in the LIVE production database.

The suite runs against live production Postgres (there is no test
database), so a row a test creates and does not delete is a row in the
business's ledger. It happened: 1,029 junk customers and 8 junk invoices
accumulated in production and were visible in the customer list.

These tests cover the two guards that prevent it:

  1. `sweep_test_residue` (session-scoped, autouse) deletes rows the
     session created, so a test that forgets to clean up cannot leak.
  2. `restore_user` snapshots and restores the PROFILE row, because the
     business address lives in `accounts.user_profiles` and NOT in the
     `users` table -- so snapshotting the user's columns snapshots
     nothing of the address.

The guard's own safety property is tested here too: it must NEVER
sweep a row that is not test residue. A guard that deletes real
business data is worse than no guard.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENV = Path(__file__).resolve().parent.parent / '.env'
if ENV.exists():
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        import os
        os.environ.setdefault(k.strip(), v.strip())

from tests.conftest import TEST_RESIDUE_PREFIXES, _is_residue_name


class TestResidueRecognition:
    """The name matcher must recognise the suite's own rows, and only
    those."""

    @pytest.mark.parametrize('prefix', TEST_RESIDUE_PREFIXES)
    def test_a_test_prefix_is_recognised(self, prefix):
        assert _is_residue_name(prefix + ' abc12345') is True

    def test_a_real_customer_name_is_not_residue(self, sweep_test_residue):
        """No REAL customer may be recognised as residue.

        The real names are read from the LIVE database at runtime, not
        written here: this repository is PUBLIC, so a customer's name
        must never appear in a tracked file. Asserting against the live
        rows is also a stronger test than a hardcoded list -- it covers
        every real customer, including any added after this test was
        written.

        Only rows that existed BEFORE this session count as real: a row
        another test in this session created is test residue by
        definition, and including it would make this test fail for the
        wrong reason.
        """
        from app import create_app
        from app.models.customer import Customer

        before = sweep_test_residue['customers']

        app = create_app()
        with app.app_context():
            names = [
                c.name for c in Customer.query.all()
                if c.name and c.id in before
            ]

        # A CI database is fresh: it has no customers at all, so there
        # may be nothing to check. The property under test is that the
        # matcher never claims a REAL customer -- and "there are no real
        # customers" satisfies it vacuously. The local suite runs
        # against live production, where the list is non-empty.
        for name in names:
            assert _is_residue_name(name) is False, name

    def test_an_empty_name_is_not_residue(self):
        assert _is_residue_name('') is False
        assert _is_residue_name(None) is False

    def test_a_prefix_must_be_at_the_start(self):
        # 'ACME Update Test Co' is not a row the suite created.
        assert _is_residue_name('ACME Update Test Co') is False

    def test_a_name_shaped_like_a_real_customer_is_not_residue(self):
        """The property must hold even where there are no real rows to
        read -- a fresh CI database has no customers at all, so the
        live-database scan above is vacuous there. These names are the
        ALLOWED fixture names from the identifier gate, not real
        customers, so they are safe to write in a public repository."""
        for name in (
            'Example Pty Ltd',
            'Acme Corp',
            'Widget Corp',
            'Sample Pty Ltd',
        ):
            assert _is_residue_name(name) is False, name


class TestGuardSafety:
    """The guard must refuse to sweep anything that is not unmistakably
    test residue."""

    def _guard_would_sweep(self, *, name=None, status='draft', existed_before=True):
        """Re-implements the guard's decision for one row, using the same
        rules as `sweep_test_residue`. Kept in step with it by
        `test_the_guard_decision_matches_the_guard`."""
        from tests.conftest import _is_residue_name
        if existed_before:
            return False
        if status != 'draft':
            return False
        return _is_residue_name(name or '')

    def test_a_paid_invoice_is_never_swept(self):
        assert self._guard_would_sweep(name='ZZ Invoice Tax Co', status='paid') is False

    def test_a_sent_invoice_is_never_swept(self):
        assert self._guard_would_sweep(name='ZZ Invoice Tax Co', status='sent') is False

    def test_a_real_invoice_is_never_swept(self, sweep_test_residue):
        """A real invoice's client_name is never swept. The real names
        come from the live database -- this repository is PUBLIC."""
        from app import create_app
        from app.models.invoice import Invoice

        before = sweep_test_residue['invoices']

        app = create_app()
        with app.app_context():
            names = [
                i.client_name for i in Invoice.query.all()
                if i.client_name and i.id in before
            ]

        # Vacuously true on a fresh CI database -- see the note in the
        # customer test above.
        for name in names:
            assert self._guard_would_sweep(name=name) is False, name

    def test_a_pre_existing_row_is_never_swept(self):
        assert self._guard_would_sweep(name='ZZ Invoice Tax Co', existed_before=False) is True
        assert self._guard_would_sweep(name='ZZ Invoice Tax Co', existed_before=True) is False

    def test_the_guard_decision_matches_the_guard(self):
        """The helper above must agree with the real guard's rules. If
        the guard changes and this test does not, the guard's safety
        property is no longer being checked."""
        import inspect

        from tests import conftest
        src = inspect.getsource(conftest.sweep_test_residue)
        assert "getattr(i, 'status', None) != 'draft'" in src
        assert "_is_residue_name(getattr(i, 'client_name', '') or '')" in src
        assert "if i.id in before_invoices:" in src
        assert "if c.id in before_customers:" in src
        assert "if c.invoices.first() is not None:" in src
