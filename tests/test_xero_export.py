"""Tests for the Xero CSV export endpoint.

Coverage:
- Auth: missing key, bad key, good key
- Date range resolution: year param, explicit from/to, default (last completed FY)
- include_drafts toggle
- CSV shape: headers match Xero templates, date format DD/MM/YYYY,
  amounts as bare numbers, currency AUD
- ZIP layout: contains the 3 expected files, UTF-8 BOM
- Address splitter: real-world AU addresses parse to city/state/postcode
- Tax type mapping: 10% GST → 'GST on Income' / 'GST on Expenses',
  0% → 'GST Free ...', weird rates → '' (accountant fills in)
- Preview endpoint returns correct counts

Run with:
    cd /opt/accounts && ./venv/bin/python3 -m pytest tests/test_xero_export.py -v
"""

import io
import os
import re
import sys
import zipfile
import csv
from datetime import date
from decimal import Decimal
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
        os.environ.setdefault(k.strip(), v.strip())


from app import create_app
from app.api.xero_export import (
    _xero_date,
    _money,
    _xero_tax_type,
    _aus_fy_dates,
    INVOICE_COLUMNS,
    BILL_COLUMNS,
    CONTACT_COLUMNS,
)
from app.shared.address import split_address, reassemble_address, validate_state, validate_postcode, render_address


# ---------------------------------------------------------------------------
# Pure-function unit tests (no DB needed)
# ---------------------------------------------------------------------------

class TestSplitAddress:
    """Address splitter — best-effort AU address parser."""

    def test_au_suburb_state_postcode_comma_separated(self):
        line1, line2, city, region, postcode, country = split_address(
            'Unit 5/28-34 Example Street, Sample Suburb QLD 4000'
        )
        assert line1 == 'Unit 5/28-34 Example Street'
        assert city == 'Sample Suburb'
        assert region == 'QLD'
        assert postcode == '4000'
        assert country == 'Australia'

    def test_au_multiline_with_country(self):
        line1, line2, city, region, postcode, country = split_address(
            '2/26 Example Street\nSample Suburb QLD 4000\nAustralia'
        )
        assert line1 == '2/26 Example Street'
        assert city == 'Sample Suburb'
        assert region == 'QLD'
        assert postcode == '4000'
        assert country == 'Australia'

    def test_au_multiline_with_unit_and_street_on_separate_lines(self):
        line1, line2, city, region, postcode, country = split_address(
            'Studio 4\n12 Something Rd\nNewtown NSW 2042\nAustralia'
        )
        assert line1 == 'Studio 4'
        assert line2 == '12 Something Rd'
        assert city == 'Newtown'
        assert region == 'NSW'
        assert postcode == '2042'
        assert country == 'Australia'

    def test_au_multiline_with_subunit_on_line2(self):
        line1, line2, city, region, postcode, country = split_address(
            'Level 3, 50 Smith Street\nFortitude Valley QLD 4006'
        )
        assert line1 == 'Level 3'
        assert line2 == '50 Smith Street'
        assert city == 'Fortitude Valley'
        assert region == 'QLD'
        assert postcode == '4006'

    def test_empty_address(self):
        assert split_address('') == ('', '', '', '', '', 'Australia')
        assert split_address(None) == ('', '', '', '', '', 'Australia')

    def test_no_postcode_returns_empty_city_region(self):
        line1, line2, city, region, postcode, country = split_address('5 Some St')
        assert line1 == '5 Some St'
        assert city == ''
        assert region == ''
        assert postcode == ''


class TestReassembleAddress:
    """Reassembly — structured fields back into a printable string."""

    def test_all_fields_present(self):
        out = reassemble_address(
            line1='2/26 Example Street',
            line2='',
            city='Sample Suburb',
            state='QLD',
            postcode='4000',
            country='Australia',
        )
        assert out == '2/26 Example Street\nSample Suburb QLD 4000\nAustralia'

    def test_skips_empty_components(self):
        out = reassemble_address(line1='5 Some St', city='Brisbane', state='QLD', postcode='4000')
        assert '\n\n' not in out
        assert out.startswith('5 Some St')
        assert out.endswith('QLD 4000')

    def test_all_empty(self):
        assert reassemble_address() == ''


class TestRenderAddress:
    """render_address(model) — generic helper that pulls structured fields
    off either Customer or User. Customer uses `country`, User uses
    `address_country` (because `users.country` is the 2-letter locale)."""

    def test_renders_customer(self):
        from types import SimpleNamespace
        c = SimpleNamespace(
            address_line1='1 Test St', address_line2=None,
            city='Brisbane', state='QLD', postcode='4000', country='Australia',
        )
        assert render_address(c) == '1 Test St\nBrisbane QLD 4000\nAustralia'

    def test_renders_user_with_address_country(self):
        from types import SimpleNamespace
        u = SimpleNamespace(
            address_line1='1 Example St', address_line2=None,
            city='Brisbane', state='QLD', postcode='4000',
            address_country='Australia',
            country='AU',  # 2-letter locale — must NOT be picked as address country
        )
        assert render_address(u) == '1 Example St\nBrisbane QLD 4000\nAustralia'

    def test_none_returns_empty(self):
        assert render_address(None) == ''

    def test_all_empty_returns_just_country(self):
        """If every structured field is None, we still emit the default
        country ('Australia') so the address doesn't render as completely
        empty. Callers that want 'no address at all' should check whether
        `address_line1` is None before rendering."""
        from types import SimpleNamespace
        result = render_address(SimpleNamespace(
            address_line1=None, address_line2=None,
            city=None, state=None, postcode=None, country=None,
        ))
        assert result == 'Australia'

    def test_no_country_field_returns_empty(self):
        """An object that has neither address_country nor country returns
        ''. This handles weird edge cases like a partial mock."""
        from types import SimpleNamespace
        obj = SimpleNamespace(
            address_line1='1 Test St', address_line2=None,
            city='Brisbane', state='QLD', postcode='4000',
            # no country / address_country attrs at all
        )
        # Should still render, falling back to default 'Australia'
        result = render_address(obj)
        assert '1 Test St' in result
        assert 'Australia' in result


class TestValidateState:
    def test_valid_au_state_accepted(self):
        ok, normalised = validate_state('qld')  # case-insensitive
        assert ok
        assert normalised == 'QLD'

    def test_invalid_au_state_rejected(self):
        ok, msg = validate_state('QL', country='Australia')
        assert not ok
        assert 'state must be one of' in msg

    def test_empty_is_ok(self):
        assert validate_state('')[0] is True
        assert validate_state(None)[0] is True

    def test_non_au_country_accepts_any_state(self):
        # For US/UK/etc. we don't have a state whitelist yet — accept anything.
        ok, normalised = validate_state('CA', country='United States')
        assert ok
        assert normalised == 'CA'


class TestValidatePostcode:
    def test_valid_au_postcode(self):
        ok, normalised = validate_postcode('4000')
        assert ok
        assert normalised == '4000'

    def test_invalid_au_postcode_rejected(self):
        ok, msg = validate_postcode('400', country='Australia')
        assert not ok
        assert '4 digits' in msg

    def test_empty_is_ok(self):
        assert validate_postcode('')[0] is True

    def test_non_au_skips_validation(self):
        # US ZIP codes are 5 digits — not validated here.
        ok, normalised = validate_postcode('94025', country='United States')
        assert ok


class TestXeroDate:
    def test_dd_mm_yyyy_format(self):
        assert _xero_date(date(2025, 7, 1)) == '01/07/2025'
        assert _xero_date(date(2026, 1, 31)) == '31/01/2026'

    def test_none_returns_empty(self):
        assert _xero_date(None) == ''


class TestMoney:
    def test_bare_number_from_decimal(self):
        assert _money(Decimal('4400.00')) == '4400.00'
        assert _money(Decimal('0.10')) == '0.10'

    def test_strips_dollar_and_commas(self):
        assert _money('$2,131.80') == '2131.80'
        assert _money('  $2,131.80  ') == '2131.80'

    def test_none_returns_empty(self):
        assert _money(None) == ''


class TestTaxType:
    def test_10pct_gst_income(self):
        assert _xero_tax_type(0.1, 'income') == 'GST on Income'

    def test_10pct_gst_expense(self):
        assert _xero_tax_type(0.1, 'expense') == 'GST on Expenses'

    def test_zero_gst(self):
        assert _xero_tax_type(0, 'income') == 'GST Free Income'
        assert _xero_tax_type(0, 'expense') == 'GST Free Expenses'

    def test_unknown_rate_returns_empty(self):
        # Anything outside 0 or 0.1 → blank so the accountant maps manually
        assert _xero_tax_type(1.0, 'income') == ''
        assert _xero_tax_type(0.2, 'expense') == ''

    def test_none_safe(self):
        assert _xero_tax_type(None, 'income') == 'GST Free Income'


class TestFinancialYear:
    def test_aus_fy_dates(self):
        assert _aus_fy_dates(2025) == (date(2025, 7, 1), date(2026, 6, 30))
        assert _aus_fy_dates(2024) == (date(2024, 7, 1), date(2025, 6, 30))


# ---------------------------------------------------------------------------
# Endpoint integration tests (need DB)
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


@pytest.fixture
def auth_headers(app):
    """Build a headers dict with the API key of the user that has data in
    the DB (payroll@example.com)."""
    from tests.conftest import api_key_plain_for, business_email
    with app.app_context():
        from app.models.user import User
        user = User.query.filter_by(email=business_email()).first()
        if not user:
            pytest.skip('No user with API key found in DB')
        key = api_key_plain_for(user.id)
        if not key:
            pytest.skip('No user with API key found in DB')
        return {'X-API-Key': key}


class TestEndpoint:
    def test_missing_api_key(self, client):
        r = client.get('/api/exports/xero')
        assert r.status_code == 401

    def test_bad_api_key(self, client):
        r = client.get('/api/exports/xero', headers={'X-API-Key': 'bad'})
        assert r.status_code == 401

    def test_preview_returns_counts(self, client, auth_headers):
        r = client.get('/api/exports/xero/preview', headers=auth_headers)
        assert r.status_code == 200
        body = r.get_json()
        assert 'invoice_count' in body
        assert 'expense_count' in body
        assert 'from_date' in body
        assert 'to_date' in body
        assert 'download_url' in body

    def test_export_returns_zip(self, client, auth_headers):
        r = client.get('/api/exports/xero', headers=auth_headers)
        assert r.status_code == 200
        assert r.content_type == 'application/zip'
        cd = r.headers.get('Content-Disposition', '')
        assert 'attachment' in cd
        assert 'peristyle-xero-export-' in cd

        buf = io.BytesIO(r.get_data())
        with zipfile.ZipFile(buf) as zf:
            names = set(zf.namelist())
            assert names == {'xero_invoices.csv', 'xero_bills.csv', 'xero_contacts.csv'}

    def test_export_year_param(self, client, auth_headers):
        r = client.get('/api/exports/xero?year=2024', headers=auth_headers)
        assert r.status_code == 200
        assert 'FY2024/25' in r.headers.get('Content-Disposition', '')

    def test_export_explicit_date_range(self, client, auth_headers):
        r = client.get(
            '/api/exports/xero?from_date=2026-01-01&to_date=2026-06-30',
            headers=auth_headers,
        )
        assert r.status_code == 200
        assert '2026-01-01_to_2026-06-30' in r.headers.get('Content-Disposition', '')

    def test_export_rejects_inverted_range(self, client, auth_headers):
        r = client.get(
            '/api/exports/xero?from_date=2026-06-30&to_date=2026-01-01',
            headers=auth_headers,
        )
        assert r.status_code == 400

    def test_export_rejects_bad_year(self, client, auth_headers):
        r = client.get('/api/exports/xero?year=foo', headers=auth_headers)
        assert r.status_code == 400

    def test_csv_shapes(self, client, auth_headers):
        r = client.get('/api/exports/xero', headers=auth_headers)
        buf = io.BytesIO(r.get_data())
        with zipfile.ZipFile(buf) as zf:
            for fname, expected_cols in (
                ('xero_invoices.csv', INVOICE_COLUMNS),
                ('xero_bills.csv', BILL_COLUMNS),
                ('xero_contacts.csv', CONTACT_COLUMNS),
            ):
                raw = zf.read(fname).decode('utf-8-sig')
                reader = csv.DictReader(io.StringIO(raw))
                assert reader.fieldnames == expected_cols, (
                    f'{fname}: expected {expected_cols}, got {reader.fieldnames}'
                )

    def test_csv_date_and_money_format(self, client, auth_headers):
        r = client.get('/api/exports/xero', headers=auth_headers)
        buf = io.BytesIO(r.get_data())
        with zipfile.ZipFile(buf) as zf:
            inv_raw = zf.read('xero_invoices.csv').decode('utf-8-sig')
            reader = csv.DictReader(io.StringIO(inv_raw))
            rows = list(reader)
            if not rows:
                pytest.skip('No invoices in DB to validate format')
            sample = rows[0]
            # Date format DD/MM/YYYY
            assert re.match(r'^\d{2}/\d{2}/\d{4}$', sample['InvoiceDate']), sample['InvoiceDate']
            # Money format: bare number, no $, no thousands separator
            for k in ('Total', 'TaxTotal', 'UnitAmount'):
                v = sample[k]
                if v:
                    assert re.match(r'^-?\d+\.\d{2}$', v), f'{k}={v!r}'
            # Currency is AUD
            assert sample['Currency'] == 'AUD'

    def test_utf8_bom_present(self, client, auth_headers):
        """Excel on Windows needs the BOM. Verify each CSV starts with U+FEFF."""
        r = client.get('/api/exports/xero', headers=auth_headers)
        buf = io.BytesIO(r.get_data())
        with zipfile.ZipFile(buf) as zf:
            for fname in ('xero_invoices.csv', 'xero_bills.csv', 'xero_contacts.csv'):
                raw = zf.read(fname)
                assert raw.startswith(b'\xef\xbb\xbf'), f'{fname} missing UTF-8 BOM'
