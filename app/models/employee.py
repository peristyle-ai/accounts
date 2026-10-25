"""Employee master record.

PII columns (tfn, bank_bsb, bank_account_number) are stored as Fernet
ciphertext (see app/shared/pii.py). Use the `*_plain` accessors to read the
real value; the default `to_dict()` returns masked values.
"""

import uuid
from decimal import Decimal

from sqlalchemy.ext.hybrid import hybrid_property

from app.db import db, get_utc_now
from config import accounts_config
from app.shared import pii


class Employee(db.Model):
    __tablename__ = 'employees'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.BigInteger, db.ForeignKey(accounts_config.users_fk, ondelete='CASCADE'), nullable=False, index=True)
    legal_name = db.Column(db.String(255), nullable=False)
    preferred_name = db.Column(db.String(255), nullable=True)
    position = db.Column(db.String(255), nullable=True)
    email_work = db.Column(db.String(255), nullable=True)
    email_personal = db.Column(db.String(255), nullable=True)

    # Stored as Fernet ciphertext (VARCHAR(500) holds the base64 blob).
    # Read via the *_plain accessors; write via the *_plain setters.
    _tfn = db.Column('tfn', db.String(500), nullable=True)
    _date_of_birth = db.Column('date_of_birth', db.String(500), nullable=True)
    _bank_bsb = db.Column('bank_bsb', db.String(500), nullable=True)
    _bank_account_number = db.Column('bank_account_number', db.String(500), nullable=True)

    # Address (added 2026-09-17 alongside SAFF work for AusSuper exports).
    # Plaintext, not encrypted — address data isn't really secret, and
    # matching the Customer/User schema keeps things consistent. The
    # encrypted columns above are the truly sensitive stuff (TFN, DOB,
    # bank details).
    address_line1 = db.Column(db.String(255), nullable=True)
    address_line2 = db.Column(db.String(255), nullable=True)
    city = db.Column(db.String(100), nullable=True)
    state = db.Column(db.String(50), nullable=True)
    postcode = db.Column(db.String(20), nullable=True)
    country = db.Column(db.String(100), nullable=False, default='Australia')

    # Sex + phone (added 2026-09-17). Sex stored as a short code per
    # the ATO/AusSuper standard ('M' / 'F' / 'X') so the SAFF builder
    # can emit it without mapping logic. Plaintext, low sensitivity.
    sex = db.Column(db.String(10), nullable=True)
    phone = db.Column(db.String(30), nullable=True)

    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True)
    employment_status = db.Column(db.String(20), nullable=False, default='active')
    pay_frequency = db.Column(db.String(20), nullable=False, default='weekly')
    default_gross_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    super_fund_name = db.Column(db.String(255), nullable=True)
    super_fund_member_no = db.Column(db.String(50), nullable=True)
    super_rate_pct = db.Column(db.Numeric(5, 2), nullable=False, default=Decimal('12.00'))

    bank_account_name = db.Column(db.String(255), nullable=True)
    bank_reference_prefix = db.Column(db.String(10), nullable=True)

    # --- STP (One Touch Payroll / ATO) fields, added 2026-10-18 ---
    # See schema/migrate_employees_stp_fields.sql for the DDL.
    tax_treatment_code = db.Column(db.String(8), nullable=False, server_default='RTXXXX')
    basis_of_employment_code = db.Column(db.String(4), nullable=False, server_default='F')
    middle_name = db.Column(db.String(128), nullable=True)
    hired_date = db.Column(db.Date, nullable=True)
    termination_date = db.Column(db.Date, nullable=True)
    termination_type = db.Column(db.String(32), nullable=True)
    previous_employee_id = db.Column(db.String(64), nullable=True)
    previous_bms_id = db.Column(db.String(32), nullable=True)
    payroll_number = db.Column(db.Integer, nullable=True)
    address_2 = db.Column(db.String(256), nullable=True)
    country_code = db.Column(db.String(2), nullable=False, server_default='au')

    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now, onupdate=get_utc_now)

    # ------------------------------------------------------------------
    # PII accessors
    # ------------------------------------------------------------------
    # `tfn`, `bank_bsb`, `bank_account_number` are stored encrypted. We hide
    # the underlying column behind `_<name>` attributes and expose:
    #   - `<name>_plain`        : plaintext (str or None)
    #   - `<name>_masked`       : display-safe masked form
    #   - `<name>` (property)   : masked form (safe default for to_dict)
    # Plain setters go through encrypt_pii() — callers that want to assign
    # raw ciphertext should not exist. Use tfn_plain = '...' in code, not
    # `employee._tfn = '...'`.
    # ------------------------------------------------------------------

    # --- TFN ---
    @hybrid_property
    def tfn_plain(self):
        return pii.decrypt_pii(self._tfn)

    @tfn_plain.setter
    def tfn_plain(self, value):
        self._tfn = pii.encrypt_pii(value)

    @hybrid_property
    def tfn_masked(self):
        return pii.mask_tfn(self.tfn_plain)

    @hybrid_property
    def tfn(self):
        return self.tfn_masked

    # --- Date of Birth ---
    # Stored encrypted as ISO date string ('YYYY-MM-DD'). Same pattern as
    # TFN — encrypted at rest, decrypted only via *_plain accessor.
    @hybrid_property
    def date_of_birth_plain(self):
        from datetime import date
        raw = pii.decrypt_pii(self._date_of_birth)
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None

    @date_of_birth_plain.setter
    def date_of_birth_plain(self, value):
        if value is None:
            self._date_of_birth = None
            return
        # Accept date or string; serialise to ISO before encrypting.
        from datetime import date
        if isinstance(value, date):
            iso = value.isoformat()
        else:
            iso = str(value)
            # Sanity-check it parses so we don't write garbage.
            date.fromisoformat(iso)
        self._date_of_birth = pii.encrypt_pii(iso)

    @hybrid_property
    def date_of_birth_masked(self):
        # DOB doesn't have a "last few digits" masking scheme like TFN
        # (everyone knows their own birthday). Show year only as a
        # privacy-aware default; callers who need the full date must
        # explicitly request date_of_birth_plain.
        d = self.date_of_birth_plain
        if not d:
            return None
        return f'****-**-**'  # fully masked by default — only year reveal is useful and we don't need that

    @hybrid_property
    def date_of_birth(self):
        return self.date_of_birth_masked

    # --- bank_bsb ---
    @hybrid_property
    def bank_bsb_plain(self):
        return pii.decrypt_pii(self._bank_bsb)

    @bank_bsb_plain.setter
    def bank_bsb_plain(self, value):
        self._bank_bsb = pii.encrypt_pii(value)

    @hybrid_property
    def bank_bsb_masked(self):
        return pii.mask_bsb(self.bank_bsb_plain)

    @hybrid_property
    def bank_bsb(self):
        return self.bank_bsb_masked

    # --- bank_account_number ---
    @hybrid_property
    def bank_account_number_plain(self):
        return pii.decrypt_pii(self._bank_account_number)

    @bank_account_number_plain.setter
    def bank_account_number_plain(self, value):
        self._bank_account_number = pii.encrypt_pii(value)

    @hybrid_property
    def bank_account_number_masked(self):
        return pii.mask_account(self.bank_account_number_plain)

    @hybrid_property
    def bank_account_number(self):
        return self.bank_account_number_masked

    # ------------------------------------------------------------------
    # to_dict — masked PII by default. Call `to_dict(include_pii_plain=True)`
    # if you really need the plaintext (PDF generator, encrypted at rest so
    # decrypt is just a fetch + a logged server-side op).
    # ------------------------------------------------------------------
    def to_dict(self, include_pii_plain=False):
        out = {
            'id': self.id,
            'user_id': self.user_id,
            'legal_name': self.legal_name,
            'preferred_name': self.preferred_name,
            'position': self.position,
            'email_work': self.email_work,
            'email_personal': self.email_personal,
            'tfn': self.tfn_masked,
            'start_date': self.start_date.isoformat() if self.start_date else None,
            'end_date': self.end_date.isoformat() if self.end_date else None,
            'employment_status': self.employment_status,
            'pay_frequency': self.pay_frequency,
            'default_gross_amount': str(self.default_gross_amount) if self.default_gross_amount is not None else None,
            'super_fund_name': self.super_fund_name,
            'super_fund_member_no': self.super_fund_member_no,
            'super_rate_pct': str(self.super_rate_pct),
            'bank_account_name': self.bank_account_name,
            'bank_bsb': self.bank_bsb_masked,
            'bank_account_number': self.bank_account_number_masked,
            'bank_reference_prefix': self.bank_reference_prefix,
            'address_line1': self.address_line1,
            'address_line2': self.address_line2,
            'city': self.city,
            'state': self.state,
            'postcode': self.postcode,
            'country': self.country,
            'sex': self.sex,
            'phone': self.phone,
            'notes': self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        if include_pii_plain:
            out['tfn_plain'] = self.tfn_plain
            out['date_of_birth_plain'] = self.date_of_birth_plain
            out['bank_bsb_plain'] = self.bank_bsb_plain
            out['bank_account_number_plain'] = self.bank_account_number_plain
        return out
