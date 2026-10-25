"""UserPaymentSettings model — bank + payment + API key, 1:1 with public.users.

Phase 1b (schema-mode install). The bank details (account_number, bsb,
de_user_id) and the api_key are encrypted at rest with Fernet (F-05
pattern, see app/shared/pii.py). Use the *-plain accessors to read
decrypted values; the raw columns store ciphertext.

Columns:
    user_id           BIGINT PK + FK to public.users(id)
    bank_name         e.g. 'NAB', 'CBA', 'ANZ' (plain)
    account_name      the name on the bank account (plain; not encrypted
                      because it's not actually sensitive per F-05 scope)
    account_number    Fernet ciphertext (use account_number_plain accessor)
    bsb               Fernet ciphertext (use bsb_plain accessor)
    de_user_id        Fernet ciphertext of the 6-digit NAB Direct Entry
                      Credit User ID; use de_user_id_plain accessor
    payment_terms     default 14 days
    api_key           Fernet ciphertext of the API key (used by
                      @api_key_required); use api_key_plain accessor
    created_at, updated_at
"""
import hmac

from cryptography.fernet import InvalidToken

from app.models import db, get_utc_now
from config import accounts_config
from app.shared.pii import (
    encrypt_pii,
    decrypt_pii,
    is_encrypted_pii,
    mask_bsb,
    mask_account,
)


class UserPaymentSettings(db.Model):
    __tablename__ = 'user_payment_settings'
    __table_args__ = {'schema': accounts_config.accounts_schema}

    user_id = db.Column(
        db.BigInteger,
        db.ForeignKey(accounts_config.users_fk, ondelete='CASCADE'),
        primary_key=True,
    )
    bank_name = db.Column(db.String(255), nullable=True)
    account_name = db.Column(db.String(255), nullable=True)
    account_number = db.Column(db.String(500), nullable=True)  # Fernet ciphertext
    bsb = db.Column(db.String(500), nullable=True)              # Fernet ciphertext
    de_user_id = db.Column(db.String(500), nullable=True)       # Fernet ciphertext
    payment_terms = db.Column(db.Integer, nullable=False, default=14)
    api_key = db.Column(db.String(500), nullable=True)          # Fernet ciphertext
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=get_utc_now)
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=get_utc_now, onupdate=get_utc_now
    )

    # --- Bank detail encryption accessors (F-05) ---

    @property
    def account_number_plain(self):
        """Decrypted account number. None if not set."""
        return decrypt_pii(self.account_number)

    @account_number_plain.setter
    def account_number_plain(self, value):
        self.account_number = encrypt_pii(value) if value else None

    @property
    def bsb_plain(self):
        """Decrypted BSB. None if not set."""
        return decrypt_pii(self.bsb)

    @bsb_plain.setter
    def bsb_plain(self, value):
        self.bsb = encrypt_pii(value) if value else None

    @property
    def de_user_id_plain(self):
        """Decrypted Direct Entry Credit User ID. None if not set.
        Validated to be ≤6 digits when read; malformed stored values
        return None rather than raising, since this is on a hot path
        for aba.py:build_header_record which explicitly handles None.
        """
        plain = decrypt_pii(self.de_user_id)
        if plain and (not plain.isdigit() or len(plain) > 6):
            return None
        return plain

    @de_user_id_plain.setter
    def de_user_id_plain(self, value):
        """Set the DE User ID. Accepts a 1-6 digit string; pads to 6 on
        storage. Strips whitespace. Raises ValueError on non-numeric input.
        """
        if value is None or value == '':
            self.de_user_id = None
            return
        stripped = str(value).strip()
        if not stripped.isdigit():
            raise ValueError(f'DE User ID must be all digits, got {value!r}')
        if len(stripped) > 6:
            raise ValueError(
                f'DE User ID must be ≤6 digits, got {len(stripped)} in {value!r}'
            )
        # Normalise: right-justify zero-filled per ABA spec position 57-62.
        normalised = stripped.rjust(6, '0')
        self.de_user_id = encrypt_pii(normalised)

    @property
    def de_user_id_masked(self):
        """Masked representation for API responses (e.g. '••3015').
        Returns None if not set."""
        plain = self.de_user_id_plain
        if not plain:
            return None
        if len(plain) <= 2:
            return '•' * len(plain)
        return '••' + plain[-4:]

    @property
    def api_key_plain(self):
        """Plaintext API key, or None if there is not a usable one.

        The column only ever holds Fernet ciphertext. There is one
        storage format: a row that is not valid ciphertext simply has no
        usable key.

        A value that is not valid ciphertext yields None rather than
        raising: it can never authenticate, and this accessor sits on
        the API-key page and in `to_dict()`, where an exception would
        turn one corrupt row into a 500 for the whole user.
        """
        if not self.api_key:
            return None
        try:
            return decrypt_pii(self.api_key)
        except (InvalidToken, ValueError, TypeError):
            return None

    @property
    def api_key_is_encrypted(self):
        """True when the stored api_key is a Fernet ciphertext."""
        return is_encrypted_pii(self.api_key)

    @classmethod
    def find_by_api_key(cls, provided):
        """Resolve the row whose API key matches `provided`.

        The column only ever holds Fernet ciphertext, so every
        candidate row is decrypted and compared. The ciphertext itself
        is deliberately NOT accepted as a key: it is stored in the DB
        and surfaced by the `User.api_key` property, so it must not
        double as a credential.

        Comparisons use `hmac.compare_digest` so the endpoint does not
        leak the key by timing.

        Returns the UserPaymentSettings row, or None.
        """
        if not provided:
            return None
        provided = str(provided)

        for row in cls.query.filter(cls.api_key.isnot(None)).all():
            candidate = row.api_key_plain
            if candidate and hmac.compare_digest(str(candidate), provided):
                return row
        return None

    @api_key_plain.setter
    def api_key_plain(self, value):
        self.api_key = encrypt_pii(value) if value else None

    def to_dict(self, masked: bool = True):
        """Plain dict representation. If masked=True (the default for API
        responses), bank details are masked; if False, plaintext is returned
        (use only for internal trusted callers)."""
        if masked:
            return {
                'user_id': self.user_id,
                'bank_name': self.bank_name,
                'account_name': self.account_name,
                'account_number': mask_account(self.account_number_plain),
                'bsb': mask_bsb(self.bsb_plain),
                'de_user_id': self.de_user_id_masked,
                'payment_terms': self.payment_terms,
                'api_key_set': bool(self.api_key),
                'created_at': self.created_at.isoformat() if self.created_at else None,
                'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            }
        return {
            'user_id': self.user_id,
            'bank_name': self.bank_name,
            'account_name': self.account_name,
            'account_number': self.account_number_plain,
            'bsb': self.bsb_plain,
            'de_user_id': self.de_user_id_plain,
            'payment_terms': self.payment_terms,
            'api_key': self.api_key_plain,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }
