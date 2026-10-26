"""
Payee directory — org-scoped bank destinations for outbound payments.

Stores third-party bank accounts that the business pays on a recurring
basis: super clearing houses (Wrkr, etc.), the ATO, suppliers, etc.

Why a separate model:
- Bank details for non-employee payees previously had no home in the
  schema — they'd be supplied ad-hoc each time a payment file (e.g. an
  ABA for super) was generated.
- Wages (employee→employee bank account) already live on the
  ``employees`` table; this model is for everything ELSE.
- Org-scoped (``user_id`` FK) because the operator operates one business at a
  time.  When he spins up another tenant, they'll get their own directory.

PII columns (``bsb``, ``account_number``) are Fernet-encrypted at rest
(matching the F-05 pattern from ``Employee``). Use ``bsb_plain`` and
``account_number_plain`` to read; the default ``to_dict()`` returns
masked values for safe logging / API responses.

Added 2026-09-21 to support the Wrkr Super (clearing house) payment for
Sample Employee's 2026-09-08..2026-09-14 super contribution.
"""
import uuid

from sqlalchemy.ext.hybrid import hybrid_property

from app.db import db, get_utc_now
from config import accounts_config
from app.shared import pii


# Soft taxonomies — kept as module constants rather than DB enums so
# we can extend without a migration. Use the accessors on Payee if you
# need validation; the ``to_dict()`` just exposes the string.
PAYEE_USE_CASES = (
    'super_clearing_house',
    'ato',
    'wages',  # for completeness; per-employee wages still live on Employee
    'supplier',
    'other',
)


class Payee(db.Model):
    __tablename__ = 'payee_directory'

    id = db.Column(db.String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = db.Column(db.BigInteger, db.ForeignKey(accounts_config.users_fk), nullable=False)

    # Human-facing
    label = db.Column(db.String(64), nullable=False)
    use_case = db.Column(db.String(32), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    # Bank destination — F-05 encrypted BSB and account number.
    account_name = db.Column(db.String(255), nullable=False)  # plaintext
    _bsb = db.Column('bsb', db.String(500), nullable=True)
    _account_number = db.Column('account_number', db.String(500), nullable=True)

    # Lifecycle
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=get_utc_now, onupdate=get_utc_now)

    # --- bank_bsb ---
    @hybrid_property
    def bsb_plain(self):
        return pii.decrypt_pii(self._bsb)

    @bsb_plain.setter
    def bsb_plain(self, value):
        self._bsb = pii.encrypt_pii(value) if value else None

    @hybrid_property
    def bsb_masked(self):
        bsb = self.bsb_plain
        return pii.mask_bsb(bsb) if bsb else None

    @hybrid_property
    def bsb(self):
        return self.bsb_masked

    # --- account_number ---
    @hybrid_property
    def account_number_plain(self):
        return pii.decrypt_pii(self._account_number)

    @account_number_plain.setter
    def account_number_plain(self, value):
        self._account_number = pii.encrypt_pii(value) if value else None

    @hybrid_property
    def account_number_masked(self):
        acct = self.account_number_plain
        return pii.mask_account(acct) if acct else None

    @hybrid_property
    def account_number(self):
        return self.account_number_masked

    # --- serialisation ---
    def to_dict(self, *, reveal_pii: bool = False):
        """Return a JSON-safe dict.

        By default, BSB and account number are masked (e.g. ``***4380``).
        Pass ``reveal_pii=True`` to expose the plaintext — only do this
        when generating payment files server-side, never in API
        responses.
        """
        d = {
            'id': self.id,
            'user_id': self.user_id,
            'label': self.label,
            'use_case': self.use_case,
            'account_name': self.account_name,
            'bsb': self.bsb_plain if reveal_pii else self.bsb_masked,
            'account_number': self.account_number_plain if reveal_pii else self.account_number_masked,
            'is_active': self.is_active,
            'notes': self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
        return d

    def __repr__(self):
        return f'<Payee {self.label!r} (use_case={self.use_case}, active={self.is_active})>'
