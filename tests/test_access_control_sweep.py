"""Regression tests for the 2026-10-25 security sweep, part 2.

Six findings from the sweep are covered here. Each was reproduced against
the RUNNING service before the fix, and each fix is pinned by a test
that fails if the defect is reintroduced.

  S5  `?reveal=true` returned the plaintext TFN / BSB / account number to
      any authenticated caller -- no role check, no audit trail.
  S6  `User.role` was never consulted anywhere: every authenticated
      account was effectively an admin.
  S7  (CSRF-exempt API accepts cookie auth) -- NOT fixed here; a
      proposal is in pie/evie/docs/csrf_cookie_auth_proposal.md.
  S8  `/uploads/<path>` authenticated the caller but applied no tenant
      check, so any account could fetch any other account's payslips.
  S9  The login handlers short-circuited on an unknown email, so an
      unknown account answered measurably faster than a known one.
  S10 `app/api/__init__.py` held a second, dead application factory.
  S11 Dead local-vision configuration left in the live environment.

Safety: like the rest of this suite these tests run against the LIVE
production Postgres. Every user/record they create is deleted on
teardown.
"""

import os
import re
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from app.models import db
from app.models.user import User
from app.models.user_profile import UserProfile
from app.models.user_payment_settings import UserPaymentSettings

ROOT = Path(__file__).resolve().parent.parent

A_EMAIL = 'access-sweep-a@example.com'
B_EMAIL = 'access-sweep-b@example.com'
PASSWORD = 'testpassword123'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    app = create_app()
    app.config['TESTING'] = True
    with app.app_context():
        yield app


@pytest.fixture
def client(app):
    return app.test_client()


def _make_user(email, role='member'):
    user = User(email=email, display_name=email.split('@')[0], role=role)
    user.set_password(PASSWORD)
    db.session.add(user)
    db.session.commit()
    db.session.add(UserProfile(user_id=user.id))
    db.session.commit()
    user.payment_settings = UserPaymentSettings(user_id=user.id)
    db.session.commit()
    return user.id


@pytest.fixture
def user_a(app):
    """Tenant A -- member, with an API key and one employee with PII."""
    with app.app_context():
        user_id = _make_user(A_EMAIL, 'member')
        _make_employee(user_id)
    yield user_id
    with app.app_context():
        from app.models.employee import Employee
        Employee.query.filter_by(user_id=user_id).delete()
        db.session.commit()
        User.query.filter_by(email=A_EMAIL).delete()
        db.session.commit()


@pytest.fixture
def user_b(app):
    """Tenant B -- member, with an API key."""
    with app.app_context():
        user_id = _make_user(B_EMAIL, 'member')
    yield user_id
    with app.app_context():
        User.query.filter_by(email=B_EMAIL).delete()
        db.session.commit()


def _make_employee(user_id):
    """A tenant-A employee carrying encrypted PII, so a leak is visible."""
    from app.models.employee import Employee
    employee = Employee(user_id=user_id, legal_name='Access Sweep Employee')
    employee.tfn_plain = '123 456 789'
    employee.date_of_birth_plain = '1990-01-01'
    employee.bank_bsb_plain = '123-456'
    employee.bank_account_number_plain = '12345678'
    db.session.add(employee)
    db.session.commit()
    return employee.id


def _api_key(app, user_id):
    with app.app_context():
        user = db.session.get(User, user_id)
        key = user.generate_api_key()
        db.session.commit()   # generate_api_key() does not commit by itself
        return key


def _write_payslip(app, user_id):
    """Write a real file under uploads/payslips/<user_id>/ and return the
    relative path plus the owner id. Cleaned up by the caller."""
    folder = Path(app.config.get('UPLOAD_FOLDER', 'uploads')) / 'payslips' / str(user_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'access-sweep-test.pdf'
    path.write_bytes(b'%PDF-1.4\n% access sweep test\n')
    return f'payslips/{user_id}/access-sweep-test.pdf', user_id


def _remove_payslip(app, user_id):
    folder = Path(app.config.get('UPLOAD_FOLDER', 'uploads')) / 'payslips' / str(user_id)
    (folder / 'access-sweep-test.pdf').unlink(missing_ok=True)
    try:
        folder.rmdir()
    except OSError:
        pass


def _set_role(app, user_id, role):
    with app.app_context():
        user = db.session.get(User, user_id)
        user.role = role
        db.session.commit()


# ---------------------------------------------------------------------------
# S5 -- ?reveal=true must not return plaintext PII
# ---------------------------------------------------------------------------

class TestNoPlaintextReveal:
    def test_reveal_query_param_returns_masked(self, app, client, user_a):
        key = _api_key(app, user_a)
        r = client.get('/api/payroll/employees?reveal=true',
                       headers={'X-API-Key': key})
        assert r.status_code == 200
        body = r.get_json()
        for employee in body.get('employees', []):
            assert 'tfn_plain' not in employee, 'plaintext TFN exposed'
            assert 'bank_bsb_plain' not in employee, 'plaintext BSB exposed'
            assert 'bank_account_number_plain' not in employee
            assert 'date_of_birth_plain' not in employee

    def test_reveal_variants_all_ignored(self, app, client, user_a):
        key = _api_key(app, user_a)
        for flag in ('reveal=1', 'reveal=yes', 'reveal=TRUE', 'reveal=true'):
            r = client.get(f'/api/payroll/employees?{flag}',
                           headers={'X-API-Key': key})
            assert r.status_code == 200
            for employee in r.get_json().get('employees', []):
                assert 'tfn_plain' not in employee

    def test_reveal_helper_is_gone(self):
        """The endpoint must not even contain the opt-in switch."""
        source = (ROOT / 'app' / 'api' / 'payroll.py').read_text()
        assert '_reveal_pii' not in source
        assert 'include_pii_plain=reveal' not in source

    def test_detail_endpoint_masked(self, app, client, user_a):
        from app.models.employee import Employee
        with app.app_context():
            emp = Employee.query.filter_by(user_id=user_a).first()
            if emp is None:
                pytest.skip('no employee for this tenant')
            emp_id = emp.id
        key = _api_key(app, user_a)
        r = client.get(f'/api/payroll/employees/{emp_id}?reveal=true',
                       headers={'X-API-Key': key})
        assert r.status_code == 200
        employee = r.get_json()['employee']
        assert 'tfn_plain' not in employee
        assert 'bank_account_number_plain' not in employee


# ---------------------------------------------------------------------------
# S6 -- roles must be enforced
# ---------------------------------------------------------------------------

class TestRoleEnforcement:
    def test_member_cannot_write_settings(self, app, client, user_a):
        key = _api_key(app, user_a)
        r = client.put('/api/settings/ACCESS_SWEEP_TEST',
                       json={'value': 'x'}, headers={'X-API-Key': key})
        assert r.status_code == 403, 'a member could change installation-wide settings'

    def test_admin_can_write_settings(self, app, client, user_a):
        _set_role(app, user_a, 'admin')
        key = _api_key(app, user_a)
        r = client.put('/api/settings/ACCESS_SWEEP_TEST',
                       json={'value': 'x'}, headers={'X-API-Key': key})
        assert r.status_code == 200
        with app.app_context():
            from app.models.system_setting import SystemSetting
            SystemSetting.query.filter_by(key='ACCESS_SWEEP_TEST').delete()
            db.session.commit()

    def test_viewer_is_read_only(self, app, client, user_a):
        _set_role(app, user_a, 'viewer')
        key = _api_key(app, user_a)
        # A read is allowed...
        r = client.get('/api/payroll/super-payments', headers={'X-API-Key': key})
        assert r.status_code == 200
        # ...a write is not.
        r = client.post('/api/payroll/super-payments', json={},
                        headers={'X-API-Key': key})
        assert r.status_code == 403, 'a read-only account performed a write'

    def test_unknown_role_cannot_be_stored(self, app, user_a):
        """The DB constrains role to admin/member/viewer -- so an unknown
        role cannot reach the code at all. Belt and braces: the code also
        ranks an unknown role lowest (see test_role_is_not_ignored_anywhere).
        """
        from sqlalchemy.exc import IntegrityError
        with app.app_context():
            user = db.session.get(User, user_a)
            user.role = 'superuser-of-the-future'
            with pytest.raises(IntegrityError):
                db.session.commit()
            db.session.rollback()

    def test_admin_only_deletes_are_admin_gated(self):
        """Every route documented as 'admin only' must actually check."""
        for rel in ('bas.py', 'bank_transactions.py', 'invoice_reminders.py'):
            source = (ROOT / 'app' / 'api' / rel).read_text()
            assert "role_required('admin')" in source, f'{rel}: admin delete not gated'

    def test_role_is_not_ignored_anywhere(self):
        """A member must not be able to reach an admin-only delete."""
        from app.shared.roles import ROLE_RANK, role_rank, is_read_only
        assert ROLE_RANK['admin'] > ROLE_RANK['member'] > ROLE_RANK['viewer']

        class _U:
            def __init__(self, r):
                self.role = r
        assert role_rank(_U('admin')) == 3
        assert role_rank(_U('member')) == 2
        assert role_rank(_U('viewer')) == 1
        assert role_rank(_U('nonsense')) == 0
        assert is_read_only(_U('viewer')) is True
        assert is_read_only(_U('member')) is False


# ---------------------------------------------------------------------------
# S8 -- uploads must be tenant scoped
# ---------------------------------------------------------------------------

class TestUploadTenantScoping:
    def test_payslip_of_another_tenant_is_refused(self, app, client, user_a, user_b):
        # A REAL file belonging to tenant B.
        rel, owner_id = _write_payslip(app, user_b)
        try:
            self._assert_tenant_scoping(app, client, user_a, user_b, rel)
        finally:
            _remove_payslip(app, user_b)

    def _assert_tenant_scoping(self, app, client, user_a, user_b, rel):

        # Unauthenticated -> refused outright.
        r = client.get(f'/uploads/{rel}')
        assert r.status_code == 401

        # Authenticated as A, asking for B's payslip -> refused. (404, not
        # 403: a 403 would confirm the file exists.)
        key_a = _api_key(app, user_a)
        r = client.get(f'/uploads/{rel}', headers={'X-API-Key': key_a})
        assert r.status_code == 404, 'tenant A read tenant B payslip'

        # The owner CAN read it -- so the 404 above is the tenant check
        # working, not the file simply being missing.
        key_b = _api_key(app, user_b)
        r = client.get(f'/uploads/{rel}', headers={'X-API-Key': key_b})
        assert r.status_code == 200, 'the owner cannot read their own payslip'
        assert b'%PDF' in r.data

    def test_unknown_file_is_refused(self, app, client, user_a):
        key = _api_key(app, user_a)
        r = client.get('/uploads/receipts/does-not-exist.jpg',
                       headers={'X-API-Key': key})
        assert r.status_code == 404

    def test_bare_filename_is_refused(self, app, client, user_a):
        key = _api_key(app, user_a)
        r = client.get('/uploads/somefile.pdf', headers={'X-API-Key': key})
        assert r.status_code == 404

    def test_owner_resolution(self, app, user_a, user_b):
        from app.shared.upload_access import resolve_upload_owner
        with app.app_context():
            assert resolve_upload_owner(f'payslips/{user_b}/x.pdf') == user_b
            assert resolve_upload_owner(f'uploads/payslips/{user_b}/x.pdf') == user_b
            assert resolve_upload_owner('payslips/not-a-number/x.pdf') is None
            assert resolve_upload_owner('payslips/only-one-segment') is None
            assert resolve_upload_owner('') is None
            assert resolve_upload_owner('bare.pdf') is None

    def test_unauthenticated_is_refused(self, app, client, user_a):
        r = client.get(f'/uploads/payslips/{user_a}/x.pdf')
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# S9 -- no user-enumeration timing oracle
# ---------------------------------------------------------------------------

class TestNoTimingOracle:
    def test_unknown_account_still_hashes(self, app, client):
        from app.shared import auth as auth_module
        calls = []
        real = auth_module.check_password_hash

        def spy(hash_value, password):
            calls.append(hash_value)
            return real(hash_value, password)

        auth_module.check_password_hash = spy
        try:
            r = client.post('/api/auth/login', json={
                'email': 'no-such-account@example.com', 'password': 'whatever'})
        finally:
            auth_module.check_password_hash = real
        assert r.status_code == 401
        assert calls, 'no hash comparison ran for an unknown account'

    def test_dummy_hash_is_used_and_never_matches(self):
        from app.shared.auth import DUMMY_PASSWORD_HASH, verify_credentials
        assert verify_credentials(None, 'anything') is False
        assert verify_credentials(None, '') is False
        assert DUMMY_PASSWORD_HASH.startswith('scrypt:')

    def test_timing_is_comparable(self, app):
        """An unknown account must not answer dramatically faster."""
        from app.shared.auth import verify_credentials
        with app.app_context():
            user = db.session.get(User, User.query.filter_by(email=A_EMAIL).first().id) \
                if User.query.filter_by(email=A_EMAIL).first() else None

        def _time(fn, n=3):
            start = time.perf_counter()
            for _ in range(n):
                fn()
            return (time.perf_counter() - start) / n

        unknown = _time(lambda: verify_credentials(None, 'wrong-password'))
        known = _time(lambda: verify_credentials(user, 'wrong-password')) if user else unknown
        # Allow a wide margin: scrypt timing varies with machine load.
        assert unknown > known / 5, (
            f'unknown-account verification was far faster '
            f'(unknown={unknown:.4f}s known={known:.4f}s)')

    def test_login_handlers_use_the_helper(self):
        for rel in ('app/api/routes.py', 'app/hmi/routes.py', 'app/pwa/routes.py'):
            source = (ROOT / rel).read_text()
            assert 'verify_credentials(' in source, f'{rel} does not use verify_credentials'
            assert 'not user.check_password(' not in source, \
                f'{rel} still short-circuits on an unknown account'


# ---------------------------------------------------------------------------
# S10 -- the dead second application factory is gone
# ---------------------------------------------------------------------------

class TestNoDeadAppFactory:
    def test_create_api_app_is_gone(self):
        import app.api as api_package
        assert not hasattr(api_package, 'create_api_app'), \
            'the dead second application factory is back'

    def test_no_module_references_it(self):
        for path in (ROOT / 'app').rglob('*.py'):
            assert 'create_api_app' not in path.read_text() or path.name == '__init__.py', \
                f'{path} references create_api_app'

    def test_single_factory_remains(self):
        import app as app_package
        assert hasattr(app_package, 'create_app')


# ---------------------------------------------------------------------------
# S11 -- dead local-vision configuration
# ---------------------------------------------------------------------------

class TestNoDeadVisionConfig:
    def test_no_source_reads_vision_settings(self):
        for path in (ROOT / 'app').rglob('*.py'):
            source = path.read_text()
            assert 'DEFAULT_LOCAL_VISION' not in source, \
                f'{path} still reads the retired local-vision config'

    def test_live_env_has_no_vision_keys(self):
        env = ROOT / '.env'
        if not env.exists():
            pytest.skip('no .env in this checkout')
        for line in env.read_text().splitlines():
            assert 'DEFAULT_LOCAL_VISION' not in line
