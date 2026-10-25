"""Tests for payslip recipient resolution and ABA lodgement references.

Both features are pure functions (no Flask/DB), so these tests run
without a database and without the app fixture.

Covered:
  - app/shared/payslip_recipients.py: parse_modes, resolve_targets
  - app/shared/aba.py: lodgement_reference
"""

from datetime import date
from types import SimpleNamespace

import pytest

from app.shared.aba import lodgement_reference
from app.shared.payslip_recipients import parse_modes, resolve_targets


# ---------------------------------------------------------------------------
# Payslip recipients
# ---------------------------------------------------------------------------

class _Emp:
    def __init__(self, work=None, personal=None):
        self.email_work = work
        self.email_personal = personal


class TestParseModes:
    def test_none_is_empty(self):
        assert parse_modes(None) == []

    def test_default_string(self):
        assert parse_modes('work,personal') == ['work', 'personal']

    def test_single(self):
        assert parse_modes('personal') == ['personal']

    def test_whitespace_and_case(self):
        assert parse_modes('  Work , PERSONAL ') == ['work', 'personal']

    def test_unknown_kinds_dropped(self):
        assert parse_modes('work,bogus,personal') == ['work', 'personal']

    def test_duplicates_collapsed(self):
        assert parse_modes('work,work,personal') == ['work', 'personal']

    def test_list_input(self):
        assert parse_modes(['personal']) == ['personal']

    def test_empty_string(self):
        assert parse_modes('') == []


class TestResolveTargets:
    def test_both_by_default(self):
        emp = _Emp('employee.work@example.com', 'employee.personal@example.com')
        assert resolve_targets(emp, 'work,personal') == [
            ('work', 'employee.work@example.com'),
            ('personal', 'employee.personal@example.com'),
        ]

    def test_personal_only(self):
        """the operator's 2026-10-22 request: Sample Employee gets her Gmail only."""
        emp = _Emp('employee.work@example.com', 'employee.personal@example.com')
        assert resolve_targets(emp, 'personal') == [
            ('personal', 'employee.personal@example.com'),
        ]

    def test_work_only(self):
        emp = _Emp('employee.work@example.com', 'employee.personal@example.com')
        assert resolve_targets(emp, 'work') == [
            ('work', 'employee.work@example.com'),
        ]

    def test_missing_address_skipped(self):
        emp = _Emp(None, 'employee.personal@example.com')
        assert resolve_targets(emp, 'work,personal') == [
            ('personal', 'employee.personal@example.com'),
        ]

    def test_no_modes_resolves_nothing(self):
        emp = _Emp('employee.work@example.com', 'employee.personal@example.com')
        assert resolve_targets(emp, None) == []
        assert resolve_targets(emp, []) == []

    def test_no_addresses_at_all(self):
        assert resolve_targets(_Emp(), 'work,personal') == []


# ---------------------------------------------------------------------------
# ABA lodgement reference
# ---------------------------------------------------------------------------

class TestLodgementReference:
    def _ev(self, **kw):
        base = dict(id='abc12345', bank_reference=None,
                    pay_period_end=date(2026, 10, 19))
        base.update(kw)
        return SimpleNamespace(**base)

    def _emp(self, prefix='JP'):
        return SimpleNamespace(bank_reference_prefix=prefix)

    def test_bank_reference_wins(self):
        """The reference on the real transfer was 20260101XX."""
        ev = self._ev(bank_reference='20260101XX')
        assert lodgement_reference(ev, self._emp()) == '20260101XX'

    def test_fallback_to_prefix_and_period_end(self):
        ev = self._ev(bank_reference=None)
        assert lodgement_reference(ev, self._emp()) == 'JP 191026'

    def test_fallback_without_prefix(self):
        ev = self._ev(bank_reference=None)
        assert lodgement_reference(ev, self._emp(prefix='')) == '191026'

    def test_last_resort_is_event_id(self):
        ev = self._ev(id='deadbeef', bank_reference=None, pay_period_end=None)
        assert lodgement_reference(ev, self._emp(prefix='')) == 'DEADBEEF'

    def test_blank_bank_reference_treated_as_absent(self):
        ev = self._ev(bank_reference='   ')
        assert lodgement_reference(ev, self._emp()) == 'JP 191026'

    def test_whitespace_stripped(self):
        ev = self._ev(bank_reference='  20260101XX  ')
        assert lodgement_reference(ev, self._emp()) == '20260101XX'
